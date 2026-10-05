"""The OData client of the built-in: confined requests, normalised results.

One client serves one catalogue service for one call. It is handed an
``httpx.AsyncClient`` whose base URL is the destination placeholder
(``agents.destination_auth.destination_http_client``), so the host and the
credential are decided per request by ``DestinationAuth`` and never here:
this module only ever builds a *relative* URL below the service path
(``urls.join_path``) and passes query options through ``params=``.

The catalogue is enforced here a second time, on purpose. The tools check a
call against the catalogue to give the model a precise refusal; this client
checks again because it is the last code before the request leaves, and a
caller that forgot a check must not turn into a request that reads a field
the admin never released. Both directions are covered: what is asked
(``$select``, ``$filter``, ``$orderby``, ``$expand``, the navigation) and
what comes back (rows are cut to the selectable fields that were asked for,
whatever SAP sent).

Nothing a back end or a transport says reaches the caller verbatim except
the SAP error text of a proper OData error envelope: an HTML error page, a
sign-in page or an httpx exception text (which can carry a URL) becomes a
fixed message.

The dialect (``v2.V2Dialect``, later ``v4.V4Dialect``) holds everything
that differs between protocol versions: literals, the key predicate, the
query option names and the payload shapes.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx
from pydantic import ValidationError

from .models import EDM_NAME_RE, EntitySetDef, NavigationDef, ServiceDefinition
from .urls import FilterError, check_filter, confine_next_link, confine_service_path, join_path

logger = logging.getLogger(__name__)

# How often a list call follows the server's paging link to fill one page.
MAX_NEXT_HOPS = 5
# What one answer may weigh before it is parsed: the body is read as a
# stream and dropped beyond this, so a wide `$expand` cannot exhaust memory.
MAX_RESPONSE_BYTES = 8_000_000
# A hard ceiling on `$top`, above the tools' own MAX_TOP.
MAX_PAGE_SIZE = 1000
MAX_MESSAGE_CHARS = 500
_MAX_ETAG_CHARS = 512

_ORDERBY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)(?: +(asc|desc))?$")
_STATUS_HINTS = {
    401: "the destination's credential was not accepted by the back end",
    403: "the SAP user is not authorised for this service or entity",
    404: "the service path, the entity set or the entity does not exist "
    "(or the service is not activated)",
}


class ODataError(Exception):
    """A refusal or a failed call, in the shape the tools hand to the model.

    ``code`` is one of the tool error codes; ``message`` is short and safe
    to show (no URL, no token, no markup from an error page).
    """

    def __init__(
        self, code: str, message: str, *, status: int | None = None, hint: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class ReadQuery:
    """The query options of a read. ``top=0`` means none (an entity read)."""

    select: list[str]
    filter: str | None
    expand: list[str]
    orderby: list[str]
    top: int
    skip: int
    count: bool = True


def _shown(name: object) -> str:
    """A name for a refusal: repeated only when it has the form of a name."""
    if isinstance(name, str) and re.fullmatch(EDM_NAME_RE, name) and len(name) <= 64:
        return repr(name)
    return "that name"


def _plain(text: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    """One line of printable text (control and format characters dropped)."""
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text[: limit * 4])
    return " ".join(cleaned.split())[:limit]


_DROP = object()


def _value(value: Any) -> Any:
    """A field value without the protocol's bookkeeping keys."""
    if isinstance(value, dict):
        if "__deferred" in value:
            return _DROP
        cleaned = {
            k: _value(v)
            for k, v in value.items()
            if isinstance(k, str) and not k.startswith(("__", "@"))
        }
        return {k: v for k, v in cleaned.items() if v is not _DROP}
    if isinstance(value, list):
        return [v for v in (_value(item) for item in value) if v is not _DROP]
    return value


class ODataClient:
    """Reads of one catalogue service through its destination."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        service: dict[str, Any],
        dialect: Any,
        *,
        sessions: Any = None,
    ) -> None:
        self._http = http
        self._dialect = dialect
        self._sessions = sessions  # CSRF token store, used by the write path
        service = service if isinstance(service, dict) else {}
        try:
            self._service_path = confine_service_path(service.get("service_path"))  # type: ignore[arg-type]
        except ValueError:
            raise ODataError("invalid_argument", "the service has no usable service_path") from None
        definition = service.get("definition") or {}
        if isinstance(definition, ServiceDefinition):
            self._definition = definition
        else:
            try:
                self._definition = ServiceDefinition.model_validate(definition)
            except ValidationError:
                # `from None`: the ValidationError carries the input in its repr.
                raise ODataError(
                    "invalid_argument", "the stored definition of the service is not valid"
                ) from None

    # -- what is asked ------------------------------------------------------
    def _navigation_target(
        self, source: EntitySetDef, name: object, operation: str | None
    ) -> tuple[NavigationDef, EntitySetDef]:
        """The navigation ``name`` of ``source`` and the entity set it leads to.

        The target must be in the catalogue (its fields decide what may be
        read) with the matching read enabled: ``list`` for a collection,
        ``get`` for a single entity. ``operation`` is what the caller is
        doing, or ``None`` for an ``$expand``.
        """
        nav = (
            next((n for n in source.navigations if n.name == name), None)
            if isinstance(name, str)
            else None
        )
        if nav is None:
            raise ODataError(
                "unknown_target",
                f"entity set {source.name!r} has no navigation {_shown(name)}",
            )
        needed = "list" if nav.collection else "get"
        if operation is not None and operation != needed:
            raise ODataError(
                "invalid_argument",
                f"navigation {nav.name!r} leads to "
                + (
                    "a collection; read it with 'list'"
                    if nav.collection
                    else "one entity; read it with 'get'"
                ),
            )
        target = self._definition.entity_set(nav.target)
        if target is None:
            raise ODataError(
                "unknown_target",
                f"the target of navigation {nav.name!r} is not in the catalogue",
            )
        if needed not in target.operations:
            raise ODataError(
                "operation_disabled",
                f"{needed!r} is not enabled for entity set {target.name!r}",
            )
        return nav, target

    def _resolve(
        self, entity_set: EntitySetDef, key: Any, navigation: Any, operation: str
    ) -> tuple[str, EntitySetDef]:
        """The request path and the entity set whose fields the answer has."""
        segment = entity_set.path or entity_set.name
        if navigation is None:
            if operation not in entity_set.operations:
                raise ODataError(
                    "operation_disabled",
                    f"{operation!r} is not enabled for entity set {entity_set.name!r}",
                )
            if operation == "list":
                if key is not None:
                    raise ODataError(
                        "invalid_argument",
                        "a key is only used with a navigation; read one entity with 'get'",
                    )
                segments = [segment]
            else:
                segments = [segment + self._dialect.key_segment(entity_set, key)]
            target = entity_set
        else:
            nav, target = self._navigation_target(entity_set, navigation, operation)
            segments = [segment + self._dialect.key_segment(entity_set, key), nav.name]
        try:
            return join_path(self._service_path, *segments), target
        except ValueError:
            raise ODataError(
                "invalid_argument", "the request path could not be built from these arguments"
            ) from None

    def _projection(
        self, target: EntitySetDef, select: Any, expand: Any
    ) -> tuple[list[str], list[tuple[NavigationDef, EntitySetDef]], list[str]]:
        """``(fields to return, expanded navigations, the $select to send)``.

        A read never asks for "everything": without a ``select`` it asks for
        the selectable fields, so a field the admin did not release is not
        even transported. An expanded navigation contributes its target's
        selectable fields (V2 returns an expanded entity only when ``$select``
        names it).
        """
        if select is not None and not isinstance(select, list):
            raise ODataError("invalid_argument", "select must be a list of field names")
        if expand is not None and not isinstance(expand, list):
            raise ODataError("invalid_argument", "expand must be a list of navigation names")
        names: list[str] = []
        for name in select or []:
            field = target.field(name) if isinstance(name, str) else None
            if field is None:
                raise ODataError(
                    "unknown_field", f"entity set {target.name!r} has no field {_shown(name)}"
                )
            if not field.selectable:
                raise ODataError("field_not_selectable", f"field {field.name!r} cannot be read")
            if field.name not in names:
                names.append(field.name)
        if not names:
            names = target.selectable_names()
        if not names:
            raise ODataError(
                "operation_disabled", f"entity set {target.name!r} has no readable field"
            )
        expands: list[tuple[NavigationDef, EntitySetDef]] = []
        sent = list(names)
        for name in expand or []:
            nav, nav_target = self._navigation_target(target, name, None)
            if any(nav.name == seen.name for seen, _ in expands):
                continue
            expands.append((nav, nav_target))
            sent.extend(f"{nav.name}/{field}" for field in nav_target.selectable_names())
        return names, expands, sent

    @staticmethod
    def _orderby(target: EntitySetDef, orderby: Any) -> list[str]:
        if orderby is not None and not isinstance(orderby, list):
            raise ODataError("invalid_argument", "orderby must be a list like ['Field desc']")
        out: list[str] = []
        for entry in orderby or []:
            match = _ORDERBY.match(" ".join(entry.split())) if isinstance(entry, str) else None
            if match is None:
                raise ODataError(
                    "invalid_argument",
                    "each orderby entry is one field name, optionally followed by asc or desc",
                )
            field = target.field(match.group(1))
            if field is None:
                raise ODataError(
                    "unknown_field",
                    f"entity set {target.name!r} has no field {_shown(match.group(1))}",
                )
            # The order of rows tells about the field it is sorted by.
            if not field.selectable:
                raise ODataError(
                    "field_not_selectable", f"field {field.name!r} cannot be used to sort"
                )
            out.append(field.name + (f" {match.group(2)}" if match.group(2) else ""))
        return out

    # -- the call -----------------------------------------------------------
    async def _fetch(self, url: str, params: dict[str, str] | None) -> tuple[Any, httpx.Headers]:
        """GET ``url`` (relative) and return the decoded JSON and the headers.

        A redirect is never followed: the next hop would be chosen by the
        answer, not by the destination.
        """
        body = bytearray()
        try:
            async with self._http.stream(
                "GET",
                url,
                params=params,
                headers={"Accept": "application/json"},
                follow_redirects=False,
            ) as response:
                status = response.status_code
                headers = response.headers
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ODataError(
                            "sap_error",
                            f"the answer of the OData service is larger than "
                            f"{MAX_RESPONSE_BYTES} bytes",
                            status=status,
                            hint="ask for fewer rows, fewer fields or no expand",
                        )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # The exception text can carry the URL; only its type is logged.
            logger.warning("odata: request failed (%s)", type(exc).__name__)
            raise ODataError(
                "destination_error", "the OData service could not be reached"
            ) from None
        if status >= 300:
            snapshot = httpx.Response(
                status,
                headers={"content-type": headers.get("content-type", "")},
                content=bytes(body),
            )
            code, text = self._dialect.parse_error(snapshot)
            code, text = _plain(code, 80), _plain(text)
            message = f"{code}: {text}" if code and text else text
            raise ODataError(
                "sap_error",
                message[:MAX_MESSAGE_CHARS] if message else f"HTTP {status} from the OData service",
                status=status,
                hint=_STATUS_HINTS.get(status),
            )
        if status == 204 or not body:
            return None, headers
        try:
            return json.loads(body), headers
        except ValueError:
            raise ODataError(
                "sap_error",
                f"the OData service did not answer with JSON (HTTP {status})",
                status=status,
                hint="a sign-in page instead of data usually means the destination's "
                "credential was not accepted",
            ) from None

    def _row(
        self,
        row: Any,
        target: EntitySetDef,
        names: list[str],
        expands: list[tuple[NavigationDef, EntitySetDef]],
    ) -> dict[str, Any]:
        """``row`` cut to the fields that were asked for and may be read."""
        if not isinstance(row, dict):
            return {}
        allowed = set(target.selectable_names())
        out: dict[str, Any] = {}
        for name in names:
            if name in allowed and name in row:
                value = _value(row[name])
                if value is not _DROP:
                    out[name] = value
        for nav, nav_target in expands:
            if nav.name not in row:
                continue
            value = row[nav.name]
            inner_names = nav_target.selectable_names()
            if nav.collection:
                rows = value.get("results") if isinstance(value, dict) else value
                if isinstance(rows, list):
                    out[nav.name] = [self._row(r, nav_target, inner_names, []) for r in rows]
            elif value is None:
                out[nav.name] = None
            elif isinstance(value, dict) and "__deferred" not in value:
                out[nav.name] = self._row(value, nav_target, inner_names, [])
        return out

    async def list(
        self,
        entity_set: EntitySetDef,
        query: ReadQuery,
        *,
        key: dict | None = None,
        navigation: str | None = None,
    ) -> dict:
        """One page of an entity set, or of a collection navigation of one entity.

        With ``navigation`` the fields, the filter and the sort order are
        those of the navigation's target entity set.
        """
        path, target = self._resolve(entity_set, key, navigation, "list")
        names, expands, sent = self._projection(target, query.select, query.expand)
        orderby = self._orderby(target, query.orderby)
        for label, number, low in (("top", query.top, 1), ("skip", query.skip, 0)):
            if isinstance(number, bool) or not isinstance(number, int) or number < low:
                raise ODataError(
                    "invalid_argument", f"{label} must be an integer of at least {low}"
                )
        if query.top > MAX_PAGE_SIZE:
            raise ODataError("invalid_argument", f"top must be at most {MAX_PAGE_SIZE}")
        if query.filter:
            try:
                check_filter(query.filter, {f.name: f for f in target.fields})
            except FilterError as exc:
                raise ODataError(exc.code, exc.message) from None
        params = self._dialect.read_params(
            ReadQuery(
                select=sent,
                filter=query.filter or None,
                expand=[nav.name for nav, _ in expands],
                orderby=orderby,
                top=query.top,
                skip=query.skip,
                count=bool(query.count),
            )
        )
        payload, _ = await self._fetch(path, params)
        rows, count, next_link = self._dialect.parse_list(payload)
        rows = list(rows)
        hops = 0
        # The back end may page on its own and return fewer rows than `$top`.
        # Its link is followed only when it is this very resource again.
        while len(rows) < query.top and next_link and hops < MAX_NEXT_HOPS:
            link = confine_next_link(next_link, self._service_path)
            if link is None or unquote(link.partition("?")[0]) != unquote(path):
                break
            hops += 1
            payload, _ = await self._fetch(link, None)
            more, _, next_link = self._dialect.parse_list(payload)
            if not more:
                next_link = None
                break
            rows.extend(more)
        items = [self._row(row, target, names, expands) for row in rows[: query.top]]
        result: dict[str, Any] = {"items": items}
        if count is not None:
            result["count"] = count
        reached = query.skip + len(items)
        if (
            len(rows) > query.top
            or next_link
            or (count is not None and reached < count)
            or (count is None and len(items) == query.top)
        ):
            result["next_skip"] = reached
        result["truncated"] = False
        return result

    async def get(
        self,
        entity_set: EntitySetDef,
        key: dict,
        *,
        select: list[str],
        expand: list[str],
        navigation: str | None = None,
    ) -> dict:
        """One entity by key, or the single entity a navigation leads to."""
        path, target = self._resolve(entity_set, key, navigation, "get")
        names, expands, sent = self._projection(target, select, expand)
        params = self._dialect.read_params(
            ReadQuery(
                select=sent,
                filter=None,
                expand=[nav.name for nav, _ in expands],
                orderby=[],
                top=0,
                skip=0,
                count=False,
            )
        )
        payload, headers = await self._fetch(path, params)
        if payload is None or (isinstance(payload, dict) and payload.get("d", ...) is None):
            # A single-valued navigation that leads nowhere.
            return {"item": None, "truncated": False}
        row, etag = self._dialect.parse_entity(payload)
        result: dict[str, Any] = {"item": self._row(row, target, names, expands)}
        etag = etag or headers.get("etag")
        if isinstance(etag, str) and etag and len(etag) <= _MAX_ETAG_CHARS:
            result["etag"] = etag
        result["truncated"] = False
        return result


# -- the size cap ---------------------------------------------------------------


def _size(result: dict) -> int:
    return len(json.dumps(result))


def _fit_list(result: dict, key: str, max_chars: int) -> int:
    """Drop trailing entries of ``result[key]`` until it fits; how many went."""
    rows = result[key]
    result[key] = []
    budget = max_chars - _size(result)
    kept = 0
    for row in rows:
        budget -= len(json.dumps(row)) + (2 if kept else 0)  # ", " between entries
        if budget < 0:
            break
        kept += 1
    result[key] = rows[:kept]
    while result[key] and _size(result) > max_chars:
        result[key] = result[key][:-1]
    return len(rows) - len(result[key])


def _fit_value(result: dict, key: str, max_chars: int) -> None:
    """Shrink one entity (or a scalar): nested lists first, then long texts."""
    value = result[key]
    if isinstance(value, str):
        while value and _size(result) > max_chars:
            value = value[: len(value) // 2]
            result[key] = value
        return
    if not isinstance(value, dict):
        return
    for name in sorted(
        (n for n, v in value.items() if isinstance(v, list)),
        key=lambda n: -len(json.dumps(value[n])),
    ):
        if _size(result) <= max_chars:
            return
        rows = value[name]
        value[name] = []
        over = _size(result) - max_chars
        if over < 0:
            holder = {"rows": rows}
            _fit_list(holder, "rows", -over + len(json.dumps({"rows": []})))
            value[name] = holder["rows"]
    while _size(result) > max_chars:
        texts = [n for n, v in value.items() if isinstance(v, str) and v]
        if not texts:
            return
        longest = max(texts, key=lambda n: len(value[n]))
        value[longest] = value[longest][: len(value[longest]) // 2]


def clip_result(result: dict, max_chars: int, *, skip: int | None = None) -> dict:
    """``result`` cut so that ``json.dumps`` of it fits ``max_chars``.

    A tool result goes into the model's context, so its size is capped where
    the unit is whole rows: trailing items are dropped and ``truncated`` is
    set, and ``next_skip`` is moved back so that the next page starts at the
    first dropped row. When the result had no ``next_skip`` (it was the last
    page) the cut page gets one only if the caller passes the ``skip`` it
    asked with. The input is not changed.
    """
    out = copy.deepcopy(result)
    out["truncated"] = bool(out.get("truncated", False))
    if _size(out) <= max_chars:
        return out
    out["truncated"] = True
    for key in ("items", "result"):
        if isinstance(out.get(key), list):
            dropped = _fit_list(out, key, max_chars)
            if key == "items" and dropped:
                if isinstance(out.get("next_skip"), int):
                    out["next_skip"] -= dropped
                elif skip is not None:
                    out["next_skip"] = skip + len(out[key])
    for key in ("item", "result"):
        if key in out and _size(out) > max_chars:
            _fit_value(out, key, max_chars)
            if _size(out) > max_chars:
                out[key] = None
    return out
