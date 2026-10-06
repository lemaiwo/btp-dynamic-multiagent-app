"""The ``$metadata`` import preview: fetch a service's document, show what it offers.

Behind ``POST /admin/api/odata/metadata`` (``agents/odata/admin_routes.py``),
which calls :func:`run_preview`. Nothing here stores or caches anything.

* :func:`fetch_metadata` makes the app read ONE document from a remote
  system an admin names -- by destination and service path, never by URL.
  That is a server-side fetch on somebody's identity, so it is narrow on
  purpose: the path is ``join_path(service_path, "$metadata")`` and nothing
  else (no query, no fragment), one ``GET``, no redirect followed, no
  second attempt, a body cap enforced on the bytes as they arrive
  (``Accept-Encoding: identity`` is pinned and any other
  ``Content-Encoding`` refused, so nothing is inflated). The host and the
  credential are the destination's; with ``user_context`` the destination
  is resolved as the admin who calls the route (the JWT bound to the
  request), and without a bound JWT the fetch is refused -- never sent
  with the destination's own credential instead.
* :func:`build_preview` turns the parsed document into the answer: names,
  types, labels and what the service DECLARES, the latter always under a
  ``declared`` (or ``suggested``) key so that no key of the answer has the
  name of a catalogue switch. It enables nothing -- ``selectable``,
  ``filterable``, ``writable``, entity operations and an operation's
  ``enabled`` stay the admin's choice. When a stored service is given, the
  document is compared with its stored NAMES and types; none of the stored
  hints travels back.
* :func:`run_preview` puts the two under one clock and one gate.
  ``PREVIEW_BUDGET_SECONDS`` covers the fetch AND the parse and is kept
  below the timeout of the approuter in front of this app (about 30 s by
  default; ``mta.yaml`` sets none for the backend destination): a slower
  preview would end as the approuter's own 504, without this route's
  error code, while the app went on fetching as the admin. The parse runs
  in a thread, which cannot be cancelled: when the clock runs out the
  route answers 504 and the thread ends on its own, bounded by the
  parser's work budget (``metadata.MAX_PARSE_WORK``) and the document
  cap. At most ``MAX_CONCURRENT_PREVIEWS`` run at a time -- a document may
  be 20 MB and its tree many times that -- and a slot is held until its
  thread has really ended.

The answer of the remote system is untrusted. A non-2xx answer is reduced to
SAP's own short code and message (``common.read_error``); a sign-in page or
anything else that is not XML is refused before the parser sees it. No host,
URL, token or cookie is put into an error text or a log line: exception
texts of ``httpx`` and of the destination service can carry all four, so
only their type (or a fixed text) is used.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from agents.destination import (
    USER_PROPAGATING_AUTH_TYPES,
    Destination,
    DestinationError,
    connectivity_from_environment,
)
from agents.destination_auth import (
    PLACEHOLDER_BASE,
    PROXY_REFUSED_TEXT,
    DestinationAuth,
    DestinationUserRequired,
    OnPremiseRefused,
    proxy_refused_hint,
    resolver_for,
    routed_auth,
    went_through_proxy,
)
from agents.odata import common
from agents.odata.metadata import (
    MAX_METADATA_BYTES,
    MetadataError,
    ParsedEntitySet,
    ParsedMetadata,
    ParsedOperation,
    parse_metadata,
)
from agents.odata.models import MAX_ENTITY_SETS, MAX_FIELDS, MAX_OPERATIONS
from agents.odata.session import NoCookieJar
from agents.odata.urls import join_path

logger = logging.getLogger(__name__)

SERVER_KEY = "builtin:odata/metadata"
METADATA_SEGMENT = "$metadata"
# Fetch plus parse, in all (see the module docstring: below the approuter's
# timeout). Read at call time (tests shorten it).
PREVIEW_BUDGET_SECONDS = 25.0
MAX_CONCURRENT_PREVIEWS = 2
# The read stops with the chunk that crosses this: the parser's own limit,
# so a body is never buffered that it would refuse anyway. The overshoot is
# at most one chunk of the wire (nothing is decompressed).
MAX_FETCH_BYTES = MAX_METADATA_BYTES
MAX_MESSAGE_CHARS = 500
# A stored definition is at most 2 MB (`MAX_DEFINITION_BYTES`), which is
# about this many fields; 200 entity sets of 500 fields each would be a
# preview five times that. Entity sets past the budget are listed with
# their key fields only, and say so.
MAX_PREVIEW_FIELDS = 20_000
MAX_PREVIEW_SKIPPED = 1_000
# Per entity set / operation. A real key has a handful of parts.
MAX_PREVIEW_KEYS = 64
MAX_PREVIEW_NAVIGATIONS = 200
MAX_PREVIEW_PARAMETERS = 100  # client.MAX_CALL_PARAMS

_USER_REQUIRED_TEXT = (
    "This fetch runs in SAP as the signed-in user, but no user token "
    "reached the backend. Sign in again and retry."
)
DESTINATION_TEXT = (
    "the destination could not be resolved or used; check its name, its "
    "authentication type and this app's destination service binding "
    "(the application log has the reason)"
)
UNREACHABLE_TEXT = "the OData service could not be reached"
_FAILED_TEXT = (
    "the $metadata preview failed unexpectedly; the application log has the reason"
)
# The slots are shared with the catalogue test call (`take_slot`).
INCOMPLETE_TEXT = (
    "parts of the $metadata document were too long to read and were left out, "
    "so this preview may be incomplete"
)
BUSY_TEXT = "other previews or test calls are running; try again in a moment"
_TECHNICAL_WARNING = (
    "the preview was asked for as the signed-in user, but the destination's "
    "authentication type does not propagate a user: it was fetched with the "
    "destination's own credential"
)
_NOT_XML_TEXT = (
    "the OData service did not answer with an XML document; a sign-in page "
    "instead of the $metadata usually means the destination's credential was "
    "not accepted"
)
_STATUS_HINTS = {
    401: "the destination's credential was not accepted",
    403: "the user is not authorised for this service in SAP",
    404: "no service answers at this path (is it activated, and is the path right?)",
}
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_HTML_STARTS = (b"<!doctype html", b"<html")
_UTF16_MARKS = (b"\xff\xfe", b"\xfe\xff")


class PreviewError(Exception):
    """A fetch that produced no document: the status and text of the answer.

    ``detail`` is always one of the fixed texts of this module, or SAP's own
    code and message; ``code`` is stable and travels as ``X-OData-Error``.
    """

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class OneShotAuth(DestinationAuth):
    """``DestinationAuth`` for one admin-triggered request: it remembers the
    destination the request was resolved for.

    Shared by the preview (``MetadataAuth``) and the catalogue test call
    (``agents.odata.testcall``), so that both go by the same rules. Built
    with ``retry_on_401=False``: the resolver is new for this call, so a
    second attempt could only repeat a refused logon -- against a system
    that counts those. (That also turns off the retry after a 407 of the
    connectivity proxy: the connectivity tokens are new for this call too.)

    An OnPremise destination goes through the connectivity proxy, by the
    rules of ``DestinationAuth``; the auth and its transport are built
    together by :func:`one_shot`.
    """

    resolved: Destination | None = None

    def on_resolved(self, destination: Destination) -> None:
        # Before the rules decide: also the type of a destination that is
        # refused (an OnPremise one without a binding, or of the wrong
        # authentication type for a user) is what a caller reports.
        self.resolved = destination


class MetadataAuth(OneShotAuth):
    """``OneShotAuth`` for the ``$metadata`` fetch: the two headers the fetch
    relies on cannot be changed by the destination."""

    def send_through(self, request: httpx.Request, destination: Destination) -> None:
        super().send_through(request, destination)
        # After the destination's own headers (`URL.headers.*`), which are
        # applied last and would otherwise win: a compressed answer would
        # make the size cap count the wrong bytes.
        request.headers["Accept"] = "application/xml"
        request.headers["Accept-Encoding"] = "identity"


@dataclass
class Fetched:
    """A fetched document and how it was fetched (for the log and the
    ``warnings`` of the answer; nothing of it identifies the target)."""

    document: bytearray | None
    size: int
    auth_type: str
    per_user: bool


# --------------------------------------------------------------------- seams


def _resolver(destination: str) -> Any:
    """The resolver of ``destination``; new per call, so nothing is cached
    beyond the one fetch. A seam for tests."""
    return resolver_for({}, SERVER_KEY, destination=destination)


def _transport() -> httpx.AsyncBaseTransport | None:
    """The transport of the fetch; ``None`` = httpx's own. A seam for tests."""
    return None


def _connectivity() -> Any:
    """The connectivity tokens of the fetch, ``None`` without a binding; new
    per call, like the resolver. A seam for tests."""
    return connectivity_from_environment()


def _proxy_transport() -> httpx.AsyncBaseTransport | None:
    """The transport towards the connectivity proxy; ``None`` = one built
    from the binding. A seam for tests."""
    return None


def one_shot(
    auth_class: type[OneShotAuth],
    resolver: Any,
    connectivity: Any,
    direct: httpx.AsyncBaseTransport | None,
    proxied: httpx.AsyncBaseTransport | None,
    *,
    user_context: bool,
    server_key: str,
) -> tuple[OneShotAuth, httpx.AsyncBaseTransport | None]:
    """The auth and the transport of one admin-triggered request, built
    together (``destination_auth.routed_auth``): with ``connectivity`` the
    router that keeps the proxy path and the direct path apart, without it
    ``direct`` as before. No retry, see :class:`OneShotAuth`."""
    auth, transport = routed_auth(
        resolver,
        connectivity,
        auth_class=auth_class,
        direct=direct,
        proxied=proxied,
        user_context=user_context,
        server_key=server_key,
        retry_on_401=False,
    )
    return auth, transport  # type: ignore[return-value]


def refused_text(exc: OnPremiseRefused) -> str:
    """The fixed text of an OnPremise refusal, for an admin: it names the
    destination (which the admin typed) and nothing else of the landscape."""
    return plain(exc.admin_text, MAX_MESSAGE_CHARS)


# ----------------------------------------------------------------- the fetch


def plain(text: object, limit: int) -> str:
    """One line of printable text, URLs masked, at most ``limit`` characters.

    The text is cut BEFORE it is cleaned: the URL mask backtracks over a long
    run of scheme characters that never reaches ``://`` (quadratic) and runs
    in the event loop, where no timeout can stop it. Four times the limit
    leaves room for what cleaning removes; a URL the cut falls into is still
    masked.
    """
    if not isinstance(text, str):
        return ""
    head = text[: limit * 4]
    line = " ".join("".join(ch if ch.isprintable() else " " for ch in head).split())
    return _URL.sub("<url>", line)[:limit]


def _message_of(message: Any) -> Any:
    # V2 wraps the text ({"lang", "value"}), V4 gives it as a string.
    return message.get("value") if isinstance(message, dict) else message


def sap_error(
    status: int,
    content_type: str,
    body: bytes,
    *,
    proxied: bool = False,
    user_context: bool = False,
) -> PreviewError:
    """A non-2xx answer as SAP's own short code and message, or the status.

    The body of a 407 is never read. When the request went through the
    connectivity proxy (``proxied``) the 407 is the proxy's, not SAP's: the
    code is ``proxy_refused`` with what an admin has to check. On the direct
    path it is an answer of the service like any other, with a fixed text."""
    if status == 407:
        if proxied:
            return PreviewError(
                502,
                "proxy_refused",
                f"{PROXY_REFUSED_TEXT}: {proxy_refused_hint(user_context)}",
            )
        return PreviewError(502, "sap_error", f"HTTP {status} from the OData service")
    snapshot = httpx.Response(status, headers={"content-type": content_type}, content=body)
    code, text = common.read_error(snapshot, _message_of)
    code, text = plain(code, 80), plain(text, MAX_MESSAGE_CHARS)
    said = f"{code}: {text}" if code and text else text
    detail = f"HTTP {status} from the OData service"
    if said:
        detail = f"{detail}: {said}"
    elif status in _STATUS_HINTS:
        detail = f"{detail}: {_STATUS_HINTS[status]}"
    return PreviewError(502, "sap_error", detail[:MAX_MESSAGE_CHARS])


def looks_like_xml(body: bytes) -> bool:  # the first bytes are enough
    """Whether ``body`` can be an XML document at all, and is no HTML page.

    The parser decides what the document is; this only keeps a page -- which
    can carry a session or a form -- from being handed to it.
    """
    if body.startswith(_UTF16_MARKS):
        return True
    head = body.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith(b"<") and not head.startswith(_HTML_STARTS)


def identity_encoded(response: httpx.Response) -> bool:
    encoding = response.headers.get("content-encoding", "").strip().lower()
    return encoding in ("", "identity")


async def read_document(
    client: httpx.AsyncClient, path: str, *, head: int | None = None
) -> bytearray:
    """One ``GET`` of the XML document at ``path``; :class:`PreviewError`
    for a redirect, a non-2xx answer, a page or a compressed answer.

    The whole document, at most ``MAX_FETCH_BYTES`` (``too_large`` beyond).
    With ``head``, only the beginning: the read stops with the chunk that
    reaches ``head`` bytes and the rest is never fetched -- for a caller
    that asks whether the service answers, not what (the catalogue test
    call). An ``httpx`` error passes through to the caller.
    """
    async with client.stream(
        "GET",
        path,
        # Both are pinned again by `MetadataAuth`, after the destination's
        # static headers.
        headers={"Accept": "application/xml", "Accept-Encoding": "identity"},
        follow_redirects=False,
    ) as response:
        status = response.status_code
        content_type = response.headers.get("content-type", "")
        proxied = went_through_proxy(response)
        if 300 <= status < 400:
            # The next hop would be chosen by the answer, not the destination.
            raise PreviewError(
                502,
                "redirect",
                f"the OData service answered with a redirect (HTTP {status}), which is "
                "not followed; a redirect to a sign-in page usually means the "
                "destination's credential was not accepted",
            )
        failed = not 200 <= status < 300
        plain = identity_encoded(response)
        if not failed:
            # `identity` was asked for. A compressed answer all the same is
            # not read: inflating it is exactly what the cap must not allow.
            if not plain or "html" in content_type.lower():
                raise PreviewError(502, "not_xml", _NOT_XML_TEXT)
            declared = response.headers.get("content-length", "")
            if head is None and declared.isdigit() and int(declared) > MAX_FETCH_BYTES:
                raise PreviewError(
                    502,
                    "too_large",
                    f"the $metadata document is larger than {MAX_FETCH_BYTES} bytes",
                )
        limit = common.MAX_ERROR_BODY if failed else MAX_FETCH_BYTES
        body = bytearray()
        if plain:
            # Raw: the bytes of the wire, never a decoded form of them.
            async for chunk in response.aiter_raw():
                body += chunk
                if head is not None and not failed and len(body) >= head:
                    break  # the beginning was asked for; the rest is not read
                if len(body) > limit:
                    if failed:
                        break  # an error text needs no more than its beginning
                    raise PreviewError(
                        502, "too_large", f"the $metadata document is larger than {limit} bytes"
                    )
    if failed:
        raise sap_error(
            status,
            content_type,
            bytes(body[:limit]),
            proxied=proxied,
            user_context=getattr(client.auth, "user_context", False) is True,
        )
    if not looks_like_xml(bytes(body[:1024])):
        raise PreviewError(502, "not_xml", _NOT_XML_TEXT)
    return body


async def fetch_metadata(destination: str, service_path: str, user_context: bool) -> Fetched:
    """The ``$metadata`` document of the service; :class:`PreviewError` otherwise.

    ``destination`` and ``service_path`` are validated by the caller; the
    path is confined here once more, because this is where it becomes a
    request. ``user_context`` true sends the request as the user whose JWT
    is bound to the calling request, exactly as the tools do. The caller
    sets the deadline (``run_preview``): a ``TimeoutError`` of its
    ``asyncio.timeout`` passes through here.
    """
    from agents.auth import current_jwt

    try:
        path = join_path(service_path, METADATA_SEGMENT)
    except ValueError as exc:  # the texts of `urls` name the rule, never the value
        raise PreviewError(422, "invalid_path", str(exc)) from None
    if user_context is True and not current_jwt.get():
        # Before anything is resolved: no call to the destination service on
        # behalf of nobody, and never a fall-back to its own credential.
        raise PreviewError(424, "user_token_required", _USER_REQUIRED_TEXT)
    try:
        auth, transport = one_shot(
            MetadataAuth,
            _resolver(destination),
            _connectivity(),
            _transport(),
            _proxy_transport(),
            user_context=user_context is True,
            server_key=SERVER_KEY,
        )
        client = httpx.AsyncClient(
            base_url=PLACEHOLDER_BASE,
            auth=auth,
            timeout=httpx.Timeout(PREVIEW_BUDGET_SECONDS),
            transport=transport,
            cookies=NoCookieJar(),
            follow_redirects=False,
        )
        async with client:
            document = await read_document(client, path)
        resolved = auth.resolved
        return Fetched(
            document=document,
            size=len(document),
            auth_type=resolved.auth_type if resolved else "",
            per_user=bool(resolved and resolved.per_user),
        )
    except PreviewError:
        raise
    except DestinationUserRequired:
        raise PreviewError(424, "user_token_required", _USER_REQUIRED_TEXT) from None
    except OnPremiseRefused as exc:
        # A fixed text that says what to change; nothing was sent.
        logger.warning(
            "odata metadata: destination '%s' was refused (%s)", destination, type(exc).__name__
        )
        raise PreviewError(502, "destination_error", refused_text(exc)) from None
    except (DestinationError, ValueError) as exc:
        # ValueError: `resolver_for` on an empty name. The text can quote the
        # destination service's answer, so it goes to the log only, URLs masked.
        logger.warning(
            "odata metadata: destination '%s' could not be used (%s): %s",
            destination,
            type(exc).__name__,
            plain(str(exc), 300),
        )
        raise PreviewError(502, "destination_error", DESTINATION_TEXT) from None
    except httpx.TimeoutException:
        raise _timed_out() from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # The exception text can carry the URL; only its type is logged.
        logger.warning("odata metadata: request failed (%s)", type(exc).__name__)
        raise PreviewError(502, "unreachable", UNREACHABLE_TEXT) from None


def _timed_out() -> PreviewError:
    return PreviewError(
        504,
        "timeout",
        f"the $metadata preview did not finish within {PREVIEW_BUDGET_SECONDS:g} seconds",
    )


# --------------------------------------------------------------- the preview


@dataclass
class _StoredSet:
    """What is read of a stored entity set: names and types, nothing else."""

    fields: dict[str, str]  # name -> type, in stored order
    keys: list[str]


def _names(items: Any) -> list[dict[str, Any]]:
    """The dict members of a stored list that carry a string ``name``."""
    return [
        item
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def _stored_sets(stored: dict[str, Any]) -> dict[str, _StoredSet]:
    """Entity set name -> its stored field names/types and key names. Read
    defensively: a stored definition may be an old or a broken one."""
    known: dict[str, _StoredSet] = {}
    for entity_set in _names(stored.get("entity_sets")):
        known[entity_set["name"]] = _StoredSet(
            fields={
                f["name"]: f["type"] if isinstance(f.get("type"), str) else "Edm.String"
                for f in _names(entity_set.get("fields"))
            },
            keys=[k["name"] for k in _names(entity_set.get("keys"))],
        )
    return known


def _shown_fields(parsed: ParsedEntitySet, key_names: set[str], room: int) -> list[Any]:
    """The fields of the answer: all of them, or -- when the set has more
    than it may show -- the key fields plus the first others, in document
    order. A key field is never cut and costs no budget: a set listed
    without its key could not be saved (the catalogue's key rule)."""
    key_fields = sum(1 for f in parsed.fields if f.name in key_names)
    others = len(parsed.fields) - key_fields
    # `MAX_FIELDS` per set, keys included; `room` is for the other fields.
    slots = max(min(MAX_FIELDS - key_fields, room), 0)
    if others <= slots:
        return list(parsed.fields)
    shown = []
    for field in parsed.fields:
        if field.name in key_names:
            shown.append(field)
        elif slots > 0:
            slots -= 1
            shown.append(field)
    return shown


def _entity_set(parsed: ParsedEntitySet, stored: _StoredSet | None, room: int) -> dict[str, Any]:
    keys = parsed.keys[:MAX_PREVIEW_KEYS]
    key_names = {k.name for k in keys}
    shown = _shown_fields(parsed, key_names, room)
    navigations = parsed.navigations[:MAX_PREVIEW_NAVIGATIONS]
    status, new_fields, removed_fields = "new", [], []
    changed_keys, changed_types = False, []
    if stored is not None:
        # Against ALL fields of the document, shown or not: a set that
        # changed past the cut must not read `in_service`, and a field past
        # the cut is not "removed".
        declared = {f.name: f.type for f in parsed.fields}
        new_fields = [name for name in declared if name not in stored.fields]
        removed_fields = [name for name in stored.fields if name not in declared]
        # The stored type decides how a literal is quoted in a key or filter.
        changed_types = [
            name
            for name, stored_type in stored.fields.items()
            if name in declared and declared[name] != stored_type
        ]
        changed_keys = stored.keys != [k.name for k in parsed.keys]
        changed = new_fields or removed_fields or changed_types or changed_keys
        status = "changed" if changed else "in_service"
    return {
        "name": parsed.name,
        # The URL segment: the parser reads entity sets by name, and a set's
        # resource path is its name.
        "path": parsed.name,
        "entity_type": parsed.entity_type,
        "label": parsed.label,
        "keys": [{"name": k.name, "type": k.type} for k in keys],
        "keys_total": len(parsed.keys),
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "label": f.label,
                # What the SERVICE declares. Nested on purpose: a preview
                # field spread into a catalogue field must not switch
                # `filterable` on.
                "declared": {
                    "filterable": f.filterable,
                    "creatable": f.creatable,
                    "updatable": f.updatable,
                },
            }
            for f in shown
        ],
        "fields_total": len(parsed.fields),
        "navigations": [
            {"name": n.name, "target": n.target, "collection": n.collection} for n in navigations
        ],
        "navigations_total": len(parsed.navigations),
        "declared": {
            "creatable": parsed.creatable,
            "updatable": parsed.updatable,
            "deletable": parsed.deletable,
        },
        "status": status,
        "new_fields": new_fields[:MAX_FIELDS],
        "new_fields_total": len(new_fields),
        "removed_fields": removed_fields,
        "changed_keys": changed_keys,
        "changed_types": changed_types,
        "truncated": (
            len(shown) < len(parsed.fields)
            or len(keys) < len(parsed.keys)
            or len(navigations) < len(parsed.navigations)
            or len(new_fields) > MAX_FIELDS
        ),
    }


def _changes_data(operation: ParsedOperation) -> tuple[bool, bool]:
    """``(suggestion, known)`` for ``OperationDef.changes_data``.

    A V4 action changes data and a V4 function does not: the protocol says
    so. A V2 function import says nothing either way: one called with POST
    is taken as changing, and of one called with GET nothing is known -- it
    is suggested as changing all the same (``known`` false), the safe side
    of `client.call_changes_data`, for the admin to decide.
    """
    if operation.kind == "function":
        return False, True
    if operation.kind == "function_import" and operation.http_method == "GET":
        return True, False
    return True, True


def _returns(operation: ParsedOperation) -> dict[str, Any] | None:
    """What the document says the operation returns, for ``suggested``.

    ``entity_set`` is the name of an entity set of this preview or ``None``
    (the parser resolves it or leaves it out, it never guesses),
    ``collection`` whether many come back, ``type`` the EDM name of a
    primitive return type or ``""``. ``None`` when the document declares no
    return type. The catalogue's ``OperationDef.returns`` holds
    ``{entity_set, collection}`` only, and only the admin puts it there.
    """
    returns = operation.returns
    if returns is None:
        return None
    return {
        "entity_set": returns.entity_set,
        "collection": returns.collection,
        "type": returns.type,
    }


def _operation(parsed: ParsedOperation, stored: set[str] | None) -> dict[str, Any]:
    changes_data, known = _changes_data(parsed)
    parameters = parsed.parameters[:MAX_PREVIEW_PARAMETERS]
    return {
        "name": parsed.name,
        "qualified_name": parsed.qualified_name,
        "kind": parsed.kind,
        "http_method": parsed.http_method,
        "bound_to": parsed.bound_to,
        "parameters": [
            {"name": p.name, "type": p.type, "required": p.required} for p in parameters
        ],
        "parameters_total": len(parsed.parameters),
        "label": parsed.label,
        "status": "in_service" if stored is not None and parsed.name in stored else "new",
        # Nested like `declared`: not a value to take over unseen.
        "suggested": {"changes_data": changes_data, "known": known, "returns": _returns(parsed)},
        "truncated": len(parameters) < len(parsed.parameters),
    }


def build_preview(parsed: ParsedMetadata, stored: dict[str, Any] | None = None) -> dict[str, Any]:
    """The route's answer for ``parsed``, compared with ``stored`` when given.

    ``stored`` is the definition of a catalogue service as the database has
    it (never anything a request carried); only its entity set, field, key
    and operation NAMES and its field types are read. The answer holds at
    most what the catalogue could store -- the first ``MAX_ENTITY_SETS``
    entity sets and ``MAX_OPERATIONS`` operations in document order,
    ``MAX_FIELDS`` fields per set and ``MAX_PREVIEW_FIELDS`` in all (key
    fields always) -- and says so: ``truncated`` at the top and per entity
    set and operation, with the counts of the whole document (``totals``,
    ``*_total``).

    ``removed_entity_sets`` / ``removed_operations`` name what the stored
    service has and the document no longer declares, compared with
    everything the parser read, not with what is shown. When the parser
    itself stopped early (``parsed.truncated``) that cannot be known:
    ``removed_complete`` is then false and both lists, and their counts in
    ``summary``, are ``None`` -- unknown, which is not the same as none.

    A stored entity set that the document still declares but that the
    parser left out (its key cannot be represented any more, say) is not
    removed: it is listed in ``skipped_stored_entity_sets`` as ``{name,
    reason}`` -- from what the parser read, so possibly incomplete when
    ``removed_complete`` is false. Each ``skipped`` entry carries the
    ``entity_type`` of its entity set (``""`` when unknown or not an EDM
    name): a skipped property of a type that several sets share is listed
    under the first of them only.
    """
    stored_sets = _stored_sets(stored) if stored is not None else None
    stored_operations = (
        {o["name"] for o in _names(stored.get("operations"))} if stored is not None else None
    )
    entity_sets: list[dict[str, Any]] = []
    room = MAX_PREVIEW_FIELDS
    truncated = (
        parsed.truncated
        or len(parsed.entity_sets) > MAX_ENTITY_SETS
        or len(parsed.operations) > MAX_OPERATIONS
        or len(parsed.skipped) > MAX_PREVIEW_SKIPPED
    )
    for parsed_set in parsed.entity_sets[:MAX_ENTITY_SETS]:
        known = None if stored_sets is None else stored_sets.get(parsed_set.name)
        entry = _entity_set(parsed_set, known, room)
        key_names = {k["name"] for k in entry["keys"]}
        room -= sum(1 for f in entry["fields"] if f["name"] not in key_names)
        truncated = truncated or entry["truncated"]
        entity_sets.append(entry)
    operations = [
        _operation(operation, stored_operations)
        for operation in parsed.operations[:MAX_OPERATIONS]
    ]
    truncated = truncated or any(o["truncated"] for o in operations)

    removed_complete = not parsed.truncated
    removed_sets: list[str] | None = [] if removed_complete else None
    removed_operations: list[str] | None = [] if removed_complete else None
    skipped_stored: list[dict[str, str]] = []
    if stored_sets is not None and stored_operations is not None:
        in_document = {e.name for e in parsed.entity_sets}
        # Declared, but left out by the parser: the first reason per name.
        left_out: dict[str, str] = {}
        for entry in parsed.skipped:
            if entry.kind == "entity_set" and entry.entity_set:
                left_out.setdefault(entry.entity_set, entry.reason)
        skipped_stored = [
            {"name": name, "reason": left_out[name]}
            for name in stored_sets
            if name not in in_document and name in left_out
        ]
        if removed_complete:
            removed_sets = [
                name for name in stored_sets if name not in in_document and name not in left_out
            ]
            operations_in_document = {o.name for o in parsed.operations}
            removed_operations = [
                o["name"]
                for o in _names((stored or {}).get("operations"))
                if o["name"] not in operations_in_document
            ]
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "entity_sets": entity_sets,
        "operations": operations,
        "skipped": [
            {
                "kind": s.kind,
                "entity_set": s.entity_set,
                "position": s.position,
                "reason": s.reason,
                "entity_type": s.entity_type,
            }
            for s in parsed.skipped[:MAX_PREVIEW_SKIPPED]
        ],
        "removed_entity_sets": removed_sets,
        "removed_operations": removed_operations,
        "removed_complete": removed_complete,
        "skipped_stored_entity_sets": skipped_stored,
        "summary": {
            "entity_sets": len(entity_sets),
            "operations": len(operations),
            "in_service": sum(1 for e in entity_sets if e["status"] == "in_service"),
            "changed": sum(1 for e in entity_sets if e["status"] == "changed"),
            "skipped": len(parsed.skipped),
            "removed_entity_sets": None if removed_sets is None else len(removed_sets),
            "removed_operations": (
                None if removed_operations is None else len(removed_operations)
            ),
            "skipped_stored_entity_sets": len(skipped_stored),
        },
        "truncated": truncated,
        # Of the whole document. When the parser stopped early these count
        # the declared ELEMENTS (also ones it would have skipped) -- but
        # never fewer than were read: one V4 action is one element however
        # many imports make an operation of it.
        "totals": {
            "entity_sets": max(parsed.entity_sets_declared, len(parsed.entity_sets))
            if parsed.truncated
            else len(parsed.entity_sets),
            "operations": max(parsed.operations_declared, len(parsed.operations))
            if parsed.truncated
            else len(parsed.operations),
            "skipped": len(parsed.skipped),
        },
        # Something the parser reads was too long to keep. Not for the
        # long texts nobody reads (`attributes_dropped` alone): noise.
        "warnings": (
            [{"code": "metadata_incomplete", "message": INCOMPLETE_TEXT}]
            if parsed.unread_attributes
            else []
        ),
    }


def _parse_and_build(
    holder: list[bytearray], version: str, stored: dict[str, Any] | None
) -> dict[str, Any]:
    """Parse the document in ``holder`` and build the preview. Synchronous
    CPU work for a thread. The document is taken OUT of ``holder`` and let
    go as soon as it is parsed, so that it does not live on next to the
    tree and the answer."""
    document = holder.pop()
    parsed = parse_metadata(document, version)  # type: ignore[arg-type]
    del document
    return build_preview(parsed, stored)


_active = 0


def take_slot() -> None:
    """Take one of the ``MAX_CONCURRENT_PREVIEWS`` slots, or refuse with 429
    ``busy``. For the other admin-triggered request to SAP, the catalogue
    test call: it and the previews count together. No ``await`` lies between
    the check and the increment. Give it back with :func:`free_slot`."""
    global _active
    if _active >= MAX_CONCURRENT_PREVIEWS:
        raise PreviewError(429, "busy", BUSY_TEXT)
    _active += 1


def free_slot() -> None:
    """Give back a slot taken with :func:`take_slot`."""
    global _active
    _active -= 1


def _release(job: asyncio.Future[Any]) -> None:
    global _active
    _active -= 1
    if not job.cancelled():
        job.exception()  # retrieved: an abandoned parse must not warn at exit


async def run_preview(
    destination: str,
    service_path: str,
    version: str,
    user_context: bool,
    stored: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch, parse and build one preview; :class:`PreviewError` otherwise.

    One deadline (``PREVIEW_BUDGET_SECONDS``) for all of it and one of
    ``MAX_CONCURRENT_PREVIEWS`` slots; see the module docstring. Writes the
    one log line of a preview: what was asked, by whom, as whom it was
    fetched (the destination's authentication type and whether it was
    resolved for the user -- what happened, not what was asked), the
    outcome, bytes and duration. No content, no host.
    """
    global _active
    from agents.auth import current_principal

    started = time.monotonic()
    fetched: Fetched | None = None
    job: asyncio.Future[Any] | None = None

    def log(outcome: str, *args: Any) -> None:
        logger.info(
            "odata metadata preview " + outcome + ": destination=%s path=%s version=%s "
            "by=%s user_context=%s auth_type=%s per_user=%s bytes=%d duration_ms=%d",
            *args,
            destination,
            service_path,
            version,
            current_principal.get() or "unknown principal",
            user_context is True,
            (fetched.auth_type if fetched else "") or "-",
            bool(fetched and fetched.per_user),
            fetched.size if fetched else 0,
            int((time.monotonic() - started) * 1000),
        )

    try:
        if _active >= MAX_CONCURRENT_PREVIEWS:
            raise PreviewError(429, "busy", BUSY_TEXT)
        _active += 1
        try:
            async with asyncio.timeout(PREVIEW_BUDGET_SECONDS):
                fetched = await fetch_metadata(destination, service_path, user_context)
                holder = [fetched.document] if fetched.document is not None else []
                fetched.document = None
                job = asyncio.ensure_future(
                    asyncio.to_thread(_parse_and_build, holder, version, stored)
                )
                del holder
                # The slot goes back when the THREAD ends, also when this
                # request has long been answered or dropped.
                job.add_done_callback(_release)
                answer = await asyncio.shield(job)
        except PreviewError:
            raise
        except TimeoutError:
            raise _timed_out() from None
        except MetadataError as exc:  # fixed texts that never quote the document
            raise PreviewError(422, "invalid_metadata", str(exc)) from None
        except Exception as exc:
            # A defect, in the parse thread or here. Its text may quote the
            # document or a URL: the type is logged, a fixed text answered,
            # and the refusal below writes the preview's log line.
            logger.error("odata metadata: preview failed (%s)", type(exc).__name__)
            raise PreviewError(500, "preview_failed", _FAILED_TEXT) from None
        finally:
            if job is None:
                _active -= 1
    except PreviewError as exc:
        log("refused (code=%s status=%d)", exc.code, exc.status)
        raise
    if (
        user_context is True
        and fetched is not None
        and fetched.auth_type not in USER_PROPAGATING_AUTH_TYPES
    ):
        answer["warnings"].append({"code": "technical_credential", "message": _TECHNICAL_WARNING})
    log(
        "(entity_sets=%d operations=%d skipped=%d truncated=%s)",
        answer["totals"]["entity_sets"],
        answer["totals"]["operations"],
        answer["totals"]["skipped"],
        answer["truncated"],
    )
    return answer
