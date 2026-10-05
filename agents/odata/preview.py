"""The ``$metadata`` import preview: fetch a service's document, show what it offers.

Behind ``POST /admin/api/odata/metadata`` (``agents/odata/admin_routes.py``).
Two halves, and nothing here stores or caches anything:

* :func:`fetch_metadata` makes the app read ONE document from a remote
  system an admin names -- by destination and service path, never by URL.
  That is a server-side fetch on somebody's identity, so it is narrow on
  purpose: the path is ``join_path(service_path, "$metadata")`` and nothing
  else (no query, no fragment), one ``GET``, no redirect followed, no
  second attempt, a body cap enforced while reading, one total timeout.
  The host and the credential are the destination's; with ``user_context``
  the destination is resolved as the admin who calls the route (the JWT
  bound to the request), and without a bound JWT the fetch is refused --
  never sent with the destination's own credential instead.
* :func:`build_preview` turns the parsed document into the answer: names,
  types, labels and what the service DECLARES. It enables nothing -- every
  switch of the catalogue (``selectable``, ``filterable``, ``writable``,
  entity operations, an operation's ``enabled``) stays the admin's choice
  and is not part of the answer. When a stored service is given, each
  entity set is compared with its stored field NAMES; none of the stored
  hints travels back.

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
from datetime import datetime, timezone
from typing import Any

import httpx

from agents.destination import PROXY_TYPE_ON_PREMISE, DestinationError
from agents.destination_auth import (
    PLACEHOLDER_BASE,
    DestinationAuth,
    DestinationUserRequired,
    resolver_for,
)
from agents.odata import common
from agents.odata.metadata import (
    MAX_METADATA_BYTES,
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
# The whole fetch -- resolving the destination, the request, reading the
# body -- may take this long. Read at call time (tests shorten it).
FETCH_TIMEOUT_SECONDS = 60.0
# The read stops with the chunk that crosses this: the parser's own limit,
# so a body is never buffered that it would refuse anyway. The overshoot is
# at most one chunk.
MAX_FETCH_BYTES = MAX_METADATA_BYTES
MAX_MESSAGE_CHARS = 500
# A stored definition is at most 2 MB (`MAX_DEFINITION_BYTES`), which is
# about this many fields; 200 entity sets of 500 fields each would be a
# preview five times that. Entity sets past the budget are listed without
# (all of) their fields, and say so.
MAX_PREVIEW_FIELDS = 20_000
MAX_PREVIEW_SKIPPED = 1_000

_ON_PREMISE_TEXT = "on-premise destinations are not available yet in this version"
_USER_REQUIRED_TEXT = (
    "This fetch runs in SAP as the signed-in user, but no user token "
    "reached the backend. Sign in again and retry."
)
_DESTINATION_TEXT = (
    "the destination could not be resolved or used; check its name, its "
    "authentication type and this app's destination service binding "
    "(the application log has the reason)"
)
_UNREACHABLE_TEXT = "the OData service could not be reached"
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


class _OnPremise(DestinationError):
    """The destination is reached through the Cloud Connector."""


class _MetadataAuth(DestinationAuth):
    """``DestinationAuth`` for one fetch: no retry, no on-premise target.

    The base class re-sends a request once after a 401, because its cached
    destination may have aged. Here the resolver is new for this call, so a
    second attempt could only repeat a refused logon -- against a system
    that counts those.
    """

    async def async_auth_flow(self, request):  # type: ignore[override]
        token, principal = self._user()
        destination = await self._resolve(token, principal)
        if (destination.proxy_type or "").strip().lower() == PROXY_TYPE_ON_PREMISE.lower():
            # Checked on the destination this very request would use, before
            # the URL is rewritten: sent from here it would go straight to
            # the virtual host, past the connectivity proxy and the Cloud
            # Connector. The connectivity task (plan section 1.7, task C2:
            # `DestinationAuth(connectivity=...)` + `OnPremiseRouter`) adds
            # that route; this refusal goes when the fetch can take it.
            raise _OnPremise(_ON_PREMISE_TEXT)
        self._apply(request, destination)
        yield request


# --------------------------------------------------------------------- seams


def _resolver(destination: str) -> Any:
    """The resolver of ``destination``; new per call, so nothing is cached
    beyond the one fetch. A seam for tests."""
    return resolver_for({}, SERVER_KEY, destination=destination)


def _transport() -> httpx.AsyncBaseTransport | None:
    """The transport of the fetch; ``None`` = httpx's own. A seam for tests."""
    return None


# ----------------------------------------------------------------- the fetch


def _plain(text: object, limit: int) -> str:
    """One line of printable text, URLs masked, at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    line = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return _URL.sub("<url>", line)[:limit]


def _message_of(message: Any) -> Any:
    # V2 wraps the text ({"lang", "value"}), V4 gives it as a string.
    return message.get("value") if isinstance(message, dict) else message


def _sap_error(status: int, content_type: str, body: bytes) -> PreviewError:
    """A non-2xx answer as SAP's own short code and message, or the status."""
    snapshot = httpx.Response(status, headers={"content-type": content_type}, content=body)
    code, text = common.read_error(snapshot, _message_of)
    code, text = _plain(code, 80), _plain(text, MAX_MESSAGE_CHARS)
    said = f"{code}: {text}" if code and text else text
    detail = f"HTTP {status} from the OData service"
    if said:
        detail = f"{detail}: {said}"
    elif status in _STATUS_HINTS:
        detail = f"{detail}: {_STATUS_HINTS[status]}"
    return PreviewError(502, "sap_error", detail[:MAX_MESSAGE_CHARS])


def _looks_like_xml(body: bytes) -> bool:
    """Whether ``body`` can be an XML document at all, and is no HTML page.

    The parser decides what the document is; this only keeps a page -- which
    can carry a session or a form -- from being handed to it.
    """
    if body.startswith(_UTF16_MARKS):
        return True
    head = body[:1024].removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith(b"<") and not head.startswith(_HTML_STARTS)


async def _read(client: httpx.AsyncClient, path: str) -> bytes:
    async with client.stream(
        "GET",
        path,
        # `identity`: the cap below then counts what the remote sent, not
        # what a compressed body unpacks to.
        headers={"Accept": "application/xml", "Accept-Encoding": "identity"},
        follow_redirects=False,
    ) as response:
        status = response.status_code
        content_type = response.headers.get("content-type", "")
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
        if not failed and "html" in content_type.lower():
            raise PreviewError(502, "not_xml", _NOT_XML_TEXT)
        limit = common.MAX_ERROR_BODY if failed else MAX_FETCH_BYTES
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > limit:
                if failed:
                    break  # an error text needs no more than its beginning
                raise PreviewError(
                    502, "too_large", f"the $metadata document is larger than {limit} bytes"
                )
    if failed:
        raise _sap_error(status, content_type, bytes(body[:limit]))
    document = bytes(body)
    if not _looks_like_xml(document):
        raise PreviewError(502, "not_xml", _NOT_XML_TEXT)
    return document


async def fetch_metadata(destination: str, service_path: str, user_context: bool) -> bytes:
    """The ``$metadata`` document of the service; :class:`PreviewError` otherwise.

    ``destination`` and ``service_path`` are validated by the caller; the
    path is confined here once more, because this is where it becomes a
    request. ``user_context`` true sends the request as the user whose JWT
    is bound to the calling request, exactly as the tools do.
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
        client = httpx.AsyncClient(
            base_url=PLACEHOLDER_BASE,
            auth=_MetadataAuth(
                _resolver(destination), user_context=user_context is True, server_key=SERVER_KEY
            ),
            timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
            transport=_transport(),
            cookies=NoCookieJar(),
            follow_redirects=False,
        )
        async with client:
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                return await _read(client, path)
    except PreviewError:
        raise
    except DestinationUserRequired:
        raise PreviewError(424, "user_token_required", _USER_REQUIRED_TEXT) from None
    except _OnPremise:
        raise PreviewError(502, "on_premise_unavailable", _ON_PREMISE_TEXT) from None
    except (DestinationError, ValueError) as exc:
        # ValueError: `resolver_for` on an empty name. The text can quote the
        # destination service's answer, so it goes to the log only, URLs masked.
        logger.warning(
            "odata metadata: destination '%s' could not be used (%s): %s",
            destination,
            type(exc).__name__,
            _plain(str(exc), 300),
        )
        raise PreviewError(502, "destination_error", _DESTINATION_TEXT) from None
    except (TimeoutError, httpx.TimeoutException):
        raise PreviewError(
            504,
            "timeout",
            f"the OData service did not answer within {FETCH_TIMEOUT_SECONDS:g} seconds",
        ) from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # The exception text can carry the URL; only its type is logged.
        logger.warning("odata metadata: request failed (%s)", type(exc).__name__)
        raise PreviewError(502, "unreachable", _UNREACHABLE_TEXT) from None


# --------------------------------------------------------------- the preview


def _stored_fields(stored: dict[str, Any] | None) -> dict[str, list[str]]:
    """Entity set name -> its stored field names. Read defensively: a stored
    definition is trusted for its names only, and may be an old or broken one."""
    known: dict[str, list[str]] = {}
    sets = (stored or {}).get("entity_sets")
    for entity_set in sets if isinstance(sets, list) else []:
        if not isinstance(entity_set, dict) or not isinstance(entity_set.get("name"), str):
            continue
        fields = entity_set.get("fields")
        known[entity_set["name"]] = [
            f["name"]
            for f in (fields if isinstance(fields, list) else [])
            if isinstance(f, dict) and isinstance(f.get("name"), str)
        ]
    return known


def _stored_operations(stored: dict[str, Any] | None) -> set[str]:
    operations = (stored or {}).get("operations")
    return {
        o["name"]
        for o in (operations if isinstance(operations, list) else [])
        if isinstance(o, dict) and isinstance(o.get("name"), str)
    }


def _entity_set(
    parsed: ParsedEntitySet, stored: list[str] | None, room: int
) -> dict[str, Any]:
    shown = parsed.fields[: min(MAX_FIELDS, max(room, 0))]
    status, new_fields, removed_fields = "new", [], []
    if stored is not None:
        have = set(stored)
        # New: what can be taken over from this answer. Removed: against the
        # whole document, so a field past the cut is not reported as gone.
        declared = {f.name for f in parsed.fields}
        new_fields = [f.name for f in shown if f.name not in have]
        removed_fields = [name for name in stored if name not in declared]
        status = "changed" if new_fields or removed_fields else "in_service"
    return {
        "name": parsed.name,
        # The URL segment: the parser reads entity sets by name, and a set's
        # resource path is its name.
        "path": parsed.name,
        "entity_type": parsed.entity_type,
        "label": parsed.label,
        "keys": [{"name": k.name, "type": k.type} for k in parsed.keys],
        "fields": [
            {
                "name": f.name,
                "type": f.type,
                "label": f.label,
                # What the service declares, as information.
                "filterable": f.filterable,
                "creatable": f.creatable,
                "updatable": f.updatable,
            }
            for f in shown
        ],
        "fields_total": len(parsed.fields),
        "navigations": [
            {"name": n.name, "target": n.target, "collection": n.collection}
            for n in parsed.navigations
        ],
        "capabilities": {
            "creatable": parsed.creatable,
            "updatable": parsed.updatable,
            "deletable": parsed.deletable,
        },
        "status": status,
        "new_fields": new_fields,
        "removed_fields": removed_fields,
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


def _operation(parsed: ParsedOperation, stored: set[str] | None) -> dict[str, Any]:
    changes_data, known = _changes_data(parsed)
    return {
        "name": parsed.name,
        "qualified_name": parsed.qualified_name,
        "kind": parsed.kind,
        "http_method": parsed.http_method,
        "bound_to": parsed.bound_to,
        "parameters": [
            {"name": p.name, "type": p.type, "required": p.required} for p in parsed.parameters
        ],
        "label": parsed.label,
        "status": "in_service" if stored is not None and parsed.name in stored else "new",
        "changes_data": changes_data,
        "changes_data_known": known,
    }


def build_preview(parsed: ParsedMetadata, stored: dict[str, Any] | None = None) -> dict[str, Any]:
    """The route's answer for ``parsed``, compared with ``stored`` when given.

    ``stored`` is the definition of a catalogue service as the database has
    it (never anything a request carried); only its entity set, field and
    operation NAMES are read. The answer holds at most what the catalogue
    could store -- the first ``MAX_ENTITY_SETS`` entity sets and
    ``MAX_OPERATIONS`` operations in document order, ``MAX_FIELDS`` fields
    per set and ``MAX_PREVIEW_FIELDS`` in all -- and says so: ``truncated``
    plus the counts of the whole document (``totals``, ``fields_total``).
    """
    stored_fields = _stored_fields(stored) if stored is not None else None
    stored_operations = _stored_operations(stored) if stored is not None else None
    entity_sets: list[dict[str, Any]] = []
    room = MAX_PREVIEW_FIELDS
    truncated = (
        len(parsed.entity_sets) > MAX_ENTITY_SETS
        or len(parsed.operations) > MAX_OPERATIONS
        or len(parsed.skipped) > MAX_PREVIEW_SKIPPED
    )
    for parsed_set in parsed.entity_sets[:MAX_ENTITY_SETS]:
        known = None if stored_fields is None else stored_fields.get(parsed_set.name)
        entry = _entity_set(parsed_set, known, room)
        room -= len(entry["fields"])
        truncated = truncated or len(entry["fields"]) < entry["fields_total"]
        entity_sets.append(entry)
    operations = [
        _operation(operation, stored_operations)
        for operation in parsed.operations[:MAX_OPERATIONS]
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
            }
            for s in parsed.skipped[:MAX_PREVIEW_SKIPPED]
        ],
        "summary": {
            "entity_sets": len(entity_sets),
            "operations": len(operations),
            "in_service": sum(1 for e in entity_sets if e["status"] == "in_service"),
            "changed": sum(1 for e in entity_sets if e["status"] == "changed"),
            "skipped": len(parsed.skipped),
        },
        "truncated": truncated,
        "totals": {
            "entity_sets": len(parsed.entity_sets),
            "operations": len(parsed.operations),
            "skipped": len(parsed.skipped),
        },
    }


def parse_and_build(
    document: bytes, version: str, stored: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Parse ``document`` and build the preview. Synchronous CPU work: call
    it through ``asyncio.to_thread``. Raises ``MetadataError``."""
    return build_preview(parse_metadata(document, version), stored)  # type: ignore[arg-type]
