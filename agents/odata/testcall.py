"""The catalogue test call: one read through a STORED service, no data back.

Behind ``POST /admin/api/odata/services/{name}/test``
(``agents/odata/admin_routes.py``), which calls :func:`run_test_call`. It
tells an admin whether the destination, the identity and the service path of
a catalogue service work -- before agents use it, which is why a disabled
service may be tested.

* **What is sent.** Everything comes from the stored service; the caller can
  at most name one of its entity sets. The read is the agent's own ``list``
  (``ODataClient.list``: the same gate ``check_read``, the same dialect, the
  same headers) with ``top`` 1, every selectable field in ``$select`` and
  the count option the tools always send (``$inlinecount`` / ``$count``), so
  a green test means the request shape of the tools works; the count is
  dropped with the rows. A service without
  an entity set that has ``list`` enabled cannot be read without a key: only
  the beginning of its ``$metadata`` is fetched, a reachability and sign-on
  check, and the answer says that no data was read.
* **One request.** No retry (``preview.OneShotAuth`` with
  ``retry_on_401=False``: a repeated refused logon counts against the user
  in SAP), no redirect followed, no paging link followed (a second request
  of the client is stopped before it leaves), no cookie kept. The clock and
  the slots are the preview's: ``preview.PREVIEW_BUDGET_SECONDS`` and
  ``preview.take_slot`` -- a test call and a ``$metadata`` preview count
  together.
* **As whom.** ``user_context`` true: the admin who calls the route (the JWT
  bound to the request, through the destination); without one the answer is
  424 and nothing is resolved or sent, never the destination's own
  credential instead. False: the destination's own credential. An OnPremise
  destination is refused before anything is sent, as in the preview.
* **What comes back.** Never a row, a field value or a key: the rows read
  are counted (0 or 1) and dropped here. The answer is the outcome, the HTTP
  status, the duration, what was tried, the destination's NAME and
  authentication TYPE, and warnings. A failure carries a stable ``code`` and
  either a fixed text or SAP's own short code and message (one line, capped,
  URLs masked). The one log line of a call has the same facts minus SAP's
  message; no host, URL, token or cookie is logged.
* **No state.** Nothing is stored, no audit row is written (it is a read),
  and the HTTP client, the resolver and the OData client live for this one
  call: nothing an agent later uses under another identity is warmed.

A test that RAN and failed is an answer (``ok: false``), not an HTTP error
of this route: a 401 or 403 of SAP must not look like the admin's own
session having ended. Only what stops the test before it starts is refused
(:class:`preview.PreviewError`): 422 for an entity set the service does not
offer for ``list``, 429 ``busy``, 424 ``user_token_required``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from agents.destination import USER_PROPAGATING_AUTH_TYPES, Destination, DestinationError
from agents.destination_auth import PLACEHOLDER_BASE, DestinationUserRequired, resolver_for
from agents.odata import preview
from agents.odata.client import ODataClient, ODataError, ReadQuery
from agents.odata.models import EntitySetDef, ServiceDefinition
from agents.odata.session import NoCookieJar
from agents.odata.urls import confine_service_path, join_path
from agents.odata.v2 import V2Dialect
from agents.odata.v4 import V4Dialect

logger = logging.getLogger(__name__)

SERVER_KEY = "builtin:odata/test"
MAX_MESSAGE_CHARS = 500
# The reachability check reads this much of the `$metadata` and no more.
PROBE_BYTES = 1024
METADATA_TARGET = "$metadata"
MAX_QUERY_NAMES = 10

_DIALECTS: dict[str, Any] = {"v2": V2Dialect(), "v4": V4Dialect()}
_QUERY_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")

_ROW_TEXT = "the read worked: one row came back"
_NO_ROW_TEXT = "the read worked: the entity set answered without a row"
_METADATA_TEXT = (
    "the service answered its $metadata; no data was read, because no entity set "
    "has 'list' enabled"
)
_USER_REQUIRED_TEXT = (
    "This test runs in SAP as the signed-in user, but no user token "
    "reached the backend. Sign in again and retry."
)
_UNKNOWN_TARGET_TEXT = "entity_set: not an entity set of this service"
_NO_LIST_TEXT = "entity_set: 'list' is not enabled for this entity set"
_INVALID_TEXT = (
    "the stored service cannot be read: its definition, service path or OData "
    "version is not valid; open the service and save it again"
)
_REDIRECT_TEXT = (
    "the OData service answered with a redirect (HTTP {status}), which is not "
    "followed; a redirect to a sign-in page usually means the destination's "
    "credential was not accepted"
)
_TIMEOUT_TEXT = "the test did not finish within {seconds:g} seconds"
_FAILED_TEXT = "the test failed unexpectedly; the application log has the reason"
_WARNINGS = {
    "service_disabled": (
        "the service is disabled: agents do not see it until it is enabled"
    ),
    "technical_credential": (
        "the service is set to act as the signed-in user, but the destination's "
        "authentication type does not propagate a user: the test ran with the "
        "destination's own credential, and so will agents"
    ),
    # The same finding when the test sent nothing: no "the test ran".
    "technical_credential/unsent": (
        "the service is set to act as the signed-in user, but the destination's "
        "authentication type does not propagate a user: requests through this "
        "destination use the destination's own credential"
    ),
    "no_list_entity_set": (
        "no entity set has 'list' enabled, so no data read was possible (a 'get' "
        "cannot be tried without a key): only reachability and sign-on were checked"
    ),
    "paging_not_followed": (
        "the service answered without a row and with a paging link; the test "
        "sends one request and did not follow it"
    ),
}
_QUERIES_WARNING = (
    "the destination has URL.queries properties ({names}) that are not applied "
    "to requests yet: the test ran without them, e.g. in the default SAP client"
)
_QUERIES_WARNING_UNSENT = (
    "the destination has URL.queries properties ({names}) that are not applied "
    "to requests yet: requests through this destination go without them, e.g. "
    "to the default SAP client"
)


class _SecondRequest(httpx.TransportError):
    """The client asked for a second request (a paging link); never sent."""


@dataclass
class _Wire:
    """What went over the wire in one test: at most one request."""

    sent: int = 0
    status: int | None = None
    blocked: bool = False


@dataclass
class _Outcome:
    ok: bool
    code: str | None
    message: str
    status: int | None = None
    rows: int = 0
    warnings: list[str] = field(default_factory=list)


def _failed(code: str, message: str, status: int | None = None) -> _Outcome:
    return _Outcome(False, code, preview.plain(message, MAX_MESSAGE_CHARS), status)


# --------------------------------------------------------------------- seams


def _resolver(destination: str) -> Any:
    """The resolver of ``destination``; new per call, so nothing is cached
    beyond the one test. A seam for tests."""
    return resolver_for({}, SERVER_KEY, destination=destination)


def _transport() -> httpx.AsyncBaseTransport | None:
    """The transport of the test; ``None`` = httpx's own. A seam for tests."""
    return None


# ------------------------------------------------------------------ the read


def _http(auth: preview.OneShotAuth, wire: _Wire, budget: float) -> httpx.AsyncClient:
    """A throw-away client that lets exactly one request out."""

    async def leaving(_request: httpx.Request) -> None:
        # After the destination shaped the request, before it is sent.
        if wire.sent:
            wire.blocked = True
            raise _SecondRequest("one request per test")
        wire.sent += 1

    async def arrived(response: httpx.Response) -> None:
        wire.status = response.status_code

    return httpx.AsyncClient(
        base_url=PLACEHOLDER_BASE,
        auth=auth,
        # Above the budget: the one clock is `asyncio.timeout` in `run_test_call`.
        timeout=httpx.Timeout(budget + 5.0),
        transport=_transport(),
        cookies=NoCookieJar(),
        follow_redirects=False,
        event_hooks={"request": [leaving], "response": [arrived]},
    )


def _refused_read(exc: ODataError, wire: _Wire) -> _Outcome:
    """What an ``ODataError`` of the list means for the test.

    The client's texts are fixed ones or SAP's own short code and message;
    what happened on the wire (``wire``) decides the code.
    """
    status = wire.status
    if wire.blocked and status is not None and 200 <= status < 300:
        # The one answer was a proper, empty page; its paging link was not followed.
        return _Outcome(True, None, _NO_ROW_TEXT, status, 0, ["paging_not_followed"])
    if exc.code == "destination_error":
        # The client's code for every transport failure: no connection, or
        # one that broke while the body arrived (the status is then known).
        return _failed("unreachable", preview.UNREACHABLE_TEXT, status)
    if not wire.sent or status is None:
        # Refused by the catalogue gate: nothing was sent.
        return _failed(exc.code, exc.message)
    if 300 <= status < 400:
        return _failed("redirect", _REDIRECT_TEXT.format(status=status), status)
    if status >= 400:
        base = f"HTTP {status} from the OData service"
        said = exc.message if exc.message != base else (exc.hint or "")
        return _failed("sap_error", f"{base}: {said}" if said else base, status)
    text = f"{exc.message}; {exc.hint}" if exc.hint else exc.message
    return _failed("unexpected_answer", text, status)


async def _list(
    http: httpx.AsyncClient,
    service: dict[str, Any],
    definition: ServiceDefinition,
    entity_set: EntitySetDef,
    wire: _Wire,
) -> _Outcome:
    """The agent's ``list`` of ``entity_set``: one row, every selectable field."""
    try:
        client = ODataClient(
            http, {**service, "definition": definition}, _DIALECTS[service["odata_version"]]
        )
        result = await client.list(
            entity_set,
            # An empty select is "all selectable fields"; they are always sent.
            # The count is asked for because the tools always ask for it: a
            # service that refuses the option must fail here, not for an agent.
            ReadQuery(select=[], filter=None, expand=[], orderby=[], top=1, skip=0, count=True),
        )
    except ODataError as exc:
        return _refused_read(exc, wire)
    # Counted and dropped: no row and no count leaves this function.
    rows = 1 if result.get("items") else 0
    del result
    return _Outcome(True, None, _ROW_TEXT if rows else _NO_ROW_TEXT, wire.status, rows)


async def _probe(http: httpx.AsyncClient, path: str, wire: _Wire) -> _Outcome:
    """The beginning of the service's ``$metadata``: does it answer, as XML?
    The preview's own fetch (``preview.read_document``), stopped after
    ``PROBE_BYTES``; nothing is parsed."""
    try:
        await preview.read_document(http, path, head=PROBE_BYTES)
    except preview.PreviewError as exc:
        # The preview's fixed texts, or SAP's own short code and message.
        code = "unexpected_answer" if exc.code == "not_xml" else exc.code
        return _failed(code, exc.detail, wire.status)
    return _Outcome(True, None, _METADATA_TEXT, wire.status)


# ------------------------------------------------------------------ the test


def _choose(definition: ServiceDefinition, named: str | None) -> EntitySetDef | None:
    """The entity set to list: the named one, else the first with ``list``;
    ``None`` when the service has none. A named one must be the service's own
    and have ``list`` enabled -- the catalogue decides, not the request."""
    if named is None:
        return next((e for e in definition.entity_sets if "list" in e.operations), None)
    entity_set = definition.entity_set(named)
    if entity_set is None:
        raise preview.PreviewError(422, "unknown_target", _UNKNOWN_TARGET_TEXT)
    if "list" not in entity_set.operations:
        raise preview.PreviewError(422, "operation_disabled", _NO_LIST_TEXT)
    return entity_set


def _warnings(
    service: dict[str, Any],
    resolved: Destination | None,
    outcome: _Outcome,
    read: str,
    sent: bool,
) -> list[dict[str, str]]:
    """``sent``: whether a request left. A warning about the destination says
    "the test ran ..." only then; the finding itself holds either way."""
    codes: list[str] = []
    if service.get("enabled") is not True:
        codes.append("service_disabled")
    if (
        service.get("user_context") is True
        and resolved is not None
        and resolved.auth_type not in USER_PROPAGATING_AUTH_TYPES
    ):
        codes.append("technical_credential")
    if read == "metadata":
        codes.append("no_list_entity_set")
    codes.extend(outcome.warnings)
    warnings = [
        {
            "code": code,
            "message": _WARNINGS.get(f"{code}/unsent", _WARNINGS[code])
            if not sent
            else _WARNINGS[code],
        }
        for code in codes
    ]
    # `DestinationAuth` does not add `URL.queries.*` (sap-client, ...) yet.
    names = [n for n in (resolved.queries if resolved else {}) if _QUERY_NAME.fullmatch(str(n))]
    if resolved is not None and resolved.queries:
        shown = ", ".join(sorted(names)[:MAX_QUERY_NAMES]) or "names not shown"
        warnings.append(
            {
                "code": "destination_queries_not_applied",
                "message": (_QUERIES_WARNING if sent else _QUERIES_WARNING_UNSENT).format(
                    names=shown
                ),
            }
        )
    return warnings


async def run_test_call(service: dict[str, Any], entity_set: str | None = None) -> dict[str, Any]:
    """Test the stored ``service`` with one read; the answer, never data.

    ``service`` is ``ODataService.to_export()`` of the row as the database
    has it; ``entity_set`` the only thing a caller may choose (already held
    to the form of an EDM name). Raises :class:`preview.PreviewError` for
    what stops the test before it starts; every other end is an answer:

    ``{ok, code, status, duration_ms, service, enabled, read, target, rows,
    identity, per_user, destination, auth_type, proxy_type, message,
    warnings}`` -- ``read`` is ``list`` or ``metadata`` (the reachability
    check), ``target`` the entity set (or ``$metadata``), ``rows`` 0 or 1,
    ``code`` ``None`` when ``ok``. ``auth_type``, ``proxy_type`` and
    ``per_user`` say how the destination WAS resolved (empty / false when
    it was not); ``identity`` is ``unknown`` when the destination was never
    resolved, ``user`` when it was, the service acts as the signed-in user
    and the destination's type propagates one, and ``technical`` otherwise.
    ``status`` is the HTTP status whenever headers arrived, also when the
    body then stalled or broke.
    ``duration_ms`` is the time of the call to SAP, destination lookup
    included.
    """
    from agents.auth import current_jwt, current_principal

    name = str(service.get("name") or "")
    destination = str(service.get("destination") or "")
    user_context = service.get("user_context") is True
    version = str(service.get("odata_version") or "")
    budget = preview.PREVIEW_BUDGET_SECONDS
    auth: preview.OneShotAuth | None = None
    read, target = "list", ""
    duration_ms = 0
    wire = _Wire()

    def log(word: str, code: str | None, status: int | None, rows: int) -> None:
        resolved = auth.resolved if auth else None
        logger.info(
            "odata test call %s: service=%s destination=%s version=%s target=%s read=%s "
            "by=%s user_context=%s auth_type=%s per_user=%s status=%s rows=%d code=%s "
            "duration_ms=%d",
            word,
            name,
            destination,
            version,
            target or "-",
            read,
            current_principal.get() or "unknown principal",
            user_context,
            (resolved.auth_type if resolved else "") or "-",
            bool(resolved and resolved.per_user),
            status if status is not None else "-",
            rows,
            code or "-",
            duration_ms,
        )

    def answer(outcome: _Outcome) -> dict[str, Any]:
        resolved = auth.resolved if auth else None
        auth_type = resolved.auth_type if resolved else ""
        log("ok" if outcome.ok else "failed", outcome.code, outcome.status, outcome.rows)
        return {
            "ok": outcome.ok,
            "code": outcome.code,
            "status": outcome.status,
            "duration_ms": duration_ms,
            "service": name,
            "enabled": service.get("enabled") is True,
            "read": read,
            "target": target,
            "rows": outcome.rows,
            # What happened, not what was asked: nothing resolved is unknown.
            "identity": (
                "unknown"
                if resolved is None
                else "user"
                if user_context and auth_type in USER_PROPAGATING_AUTH_TYPES
                else "technical"
            ),
            "per_user": bool(resolved and resolved.per_user),
            "destination": destination,
            "auth_type": auth_type,
            "proxy_type": resolved.proxy_type if resolved else "",
            "message": outcome.message,
            "warnings": _warnings(service, resolved, outcome, read, bool(wire.sent)),
        }

    def refuse(refusal: preview.PreviewError) -> preview.PreviewError:
        log("refused", refusal.code, None, 0)
        return refusal

    # What is stored, read once more where it becomes a request.
    try:
        if version not in _DIALECTS:
            raise ValueError
        service_path = confine_service_path(service.get("service_path"))  # type: ignore[arg-type]
        definition = ServiceDefinition.model_validate(service.get("definition") or {})
    except (ValidationError, ValueError):
        # `ValidationError` carries its input in its repr: never logged or re-raised.
        return answer(_failed("invalid_definition", _INVALID_TEXT))
    try:
        chosen = _choose(definition, entity_set)
    except preview.PreviewError as exc:
        raise refuse(exc) from None
    if chosen is None:
        read, target = "metadata", METADATA_TARGET
    else:
        target = chosen.name

    try:
        preview.take_slot()
    except preview.PreviewError as exc:
        raise refuse(exc) from None
    started = time.monotonic()
    clock: asyncio.Timeout | None = None
    try:
        if user_context and not current_jwt.get():
            # Before anything is resolved: no call to the destination service
            # on behalf of nobody, and never a fall-back to its own credential.
            raise refuse(preview.PreviewError(424, "user_token_required", _USER_REQUIRED_TEXT))
        try:
            auth_class = preview.OneShotAuth if chosen is not None else preview.MetadataAuth
            auth = auth_class(
                _resolver(destination),
                user_context=user_context,
                server_key=SERVER_KEY,
                retry_on_401=False,
            )
            async with asyncio.timeout(budget) as clock:
                async with _http(auth, wire, budget) as http:
                    if chosen is not None:
                        outcome = await _list(http, service, definition, chosen, wire)
                    else:
                        outcome = await _probe(
                            http, join_path(service_path, METADATA_TARGET), wire
                        )
        except preview.PreviewError:
            raise
        except DestinationUserRequired:
            raise refuse(
                preview.PreviewError(424, "user_token_required", _USER_REQUIRED_TEXT)
            ) from None
        except preview.OnPremise:
            outcome = _failed("on_premise_unavailable", preview.ON_PREMISE_TEXT)
        except (DestinationError, ValueError) as exc:
            # ValueError: `resolver_for` on an empty name. The text can quote the
            # destination service's answer, so it goes to the log only, URLs masked.
            logger.warning(
                "odata test call: destination '%s' could not be used (%s): %s",
                destination,
                type(exc).__name__,
                preview.plain(str(exc), 300),
            )
            outcome = _failed("destination_error", preview.DESTINATION_TEXT)
        except TimeoutError:
            if clock is not None and clock.expired():
                outcome = _failed("timeout", _TIMEOUT_TEXT.format(seconds=budget), wire.status)
            else:
                outcome = _failed("unreachable", preview.UNREACHABLE_TEXT, wire.status)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # The exception text can carry the URL; only its type is logged.
            logger.warning("odata test call: request failed (%s)", type(exc).__name__)
            outcome = _failed("unreachable", preview.UNREACHABLE_TEXT, wire.status)
        except Exception as exc:
            # A defect. Its text may quote an answer or a URL: the type is
            # logged, a fixed text answered.
            logger.error("odata test call: failed (%s)", type(exc).__name__)
            outcome = _failed("test_failed", _FAILED_TEXT)
        duration_ms = int((time.monotonic() - started) * 1000)
    finally:
        preview.free_slot()
    return answer(outcome)
