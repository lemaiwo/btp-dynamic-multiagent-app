"""Direct ARC-1 calls for the IDE routes (open, refresh, lint, search).

These calls do not go through an agent run, so the ``ReadOnlyGuard`` around
an agent's toolsets does not see them. :class:`Arc1Client` therefore runs
:func:`agents.ide.readonly.check_call` itself, first, on every call, and
refuses with 403 before any connection is built.

Identity: with a destination configured for the target (``IdeConventions.
destination``) the MCP server is built with ``auth_mode="destination"`` and
``user_context: true``, so the destination service exchanges the signed-in
user's JWT and ARC-1 runs the call in SAP as that user. There is no app-level
fallback: a call without a bound JWT is refused with 424
(:class:`Arc1UserRequired`, code ``user_token_required``; not 401, which an
approuter-fronted UI would read as "session expired" and loop on re-login
that cannot fix a missing token), and a ``DestinationUserRequired`` raised
inside the MCP client's task group -- which can arrive wrapped in an
``ExceptionGroup`` -- maps to the same 424.

ARC-1 errors: ARC-1 reports failures as a JSON object with an ``error`` code
plus ``retryable``/``requestId`` (for example ``SAP_AUTHENTICATION_FAILED``
after principal propagation). It can arrive as an MCP ``isError`` result
(pydantic-ai raises ``ModelRetry`` with that text) or as plain result text.
Both become :class:`Arc1Error` 502 with the lower-cased error as ``code``,
so the routes never store such a payload as source or parse it as an empty
lint/search result.

URL: ``IDE_ARC1_URL_<TARGET>`` (target upper-cased, non-alphanumerics as
``_``). In destination mode only its path counts and the destination names
the host; without the variable the path is ``/mcp``. Local development
without a destination service: an empty ``destination`` falls back to
``auth_mode="jwt"`` against ``IDE_ARC1_URL_<TARGET>`` (424 when unset).

Session type: ``policy`` is the session's type (``change`` by default) and
selects the read-only policy, so only a diagnose session can read dumps and
traces here. ``trace_start``/``trace_cancel`` are never run by :meth:`call`
(decision D1). Results are returned as ARC-1 sent them: the routes build a
diagnose client only while the target's conventions say ``non_production``
(``agents.ide.diagnose.is_non_production``) and refuse otherwise. An ARC-1
error payload is an error, not data, and is raised.

Trace approvals: :meth:`Arc1Client.arm_trace` and :meth:`Arc1Client.
cancel_trace` are the only code that sends ``trace_start``/``trace_cancel``,
and only ``agents.ide.approvals.decide`` calls them, after the developer
approved. They build the arguments themselves from validated parameters and
never send a ``traceUser``, so ARC-1 traces the connected user -- under
principal propagation the developer. They need a bound JWT and, on CF, a
destination (:func:`require_user_context`).

Seam for tests: :func:`get_arc1_client`; the routes look it up on this
module at call time, so ``monkeypatch.setattr(arc1, "get_arc1_client", ...)``
replaces it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from pydantic_ai.exceptions import ModelRetry

from agents import shared
from agents.auth import current_jwt, current_principal
from agents.destination import DestinationError
from agents.destination_auth import PLACEHOLDER_BASE, DestinationUserRequired
from agents.ide import readonly
from agents.ide.diagnose import result_text as _result_text

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 500


class Arc1Error(HTTPException):
    """An ARC-1 call that did not produce a result; carries a stable ``code``."""

    code = "arc1_error"

    def __init__(
        self,
        status_code: int,
        message: str,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(status_code=status_code, detail=message)
        if code:
            self.code = code
        self.message = message
        # Extra response fields (request_id, retryable, target); never secrets.
        self.extra = dict(extra or {})

    def body(self) -> dict[str, Any]:
        return {"detail": self.message, "code": self.code, **self.extra}


class Arc1Refused(Arc1Error):
    """The read-only policy refused the call (403)."""

    code = "readonly_refused"

    def __init__(self, reason: str):
        super().__init__(403, f"{readonly.REFUSED_PREFIX}: {reason}")


class Arc1UserRequired(Arc1Error):
    """No signed-in user's token is bound, so the call cannot run as the user."""

    code = "user_token_required"

    def __init__(self, message: str | None = None):
        super().__init__(
            424,
            message
            or "This call runs in SAP as the signed-in user, but no user token "
            "reached the backend. Sign in again and retry.",
        )


class Arc1NotConfigured(Arc1Error):
    code = "arc1_not_configured"

    def __init__(self, message: str):
        super().__init__(424, message)


def _short(text: Any) -> str:
    s = str(text)
    return s if len(s) <= _MAX_ERROR_CHARS else s[:_MAX_ERROR_CHARS] + "..."


def _find(
    exc: BaseException,
    cls: type[BaseException],
    _seen: set[int] | None = None,
) -> BaseException | None:
    """``exc`` or the first leaf of an exception group that is a ``cls``
    (also a group that ``exc`` was raised from). Cycle-safe: a cause chain
    can loop back through a group."""
    seen = set() if _seen is None else _seen
    if id(exc) in seen:
        return None
    seen.add(id(exc))
    if isinstance(exc, cls):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _find(inner, cls, seen)
            if found is not None:
                return found
    cause = exc.__cause__ or exc.__context__
    if cause is not None and isinstance(cause, BaseExceptionGroup):
        return _find(cause, cls, seen)
    return None


_CODE_RE = re.compile(r"[^a-z0-9_]+")


def arc1_error_from_text(tool: str, target: str, text: str) -> Arc1Error | None:
    """An :class:`Arc1Error` for an ARC-1 error payload, else ``None``.

    The payload is a JSON object with a string ``error`` and at least one of
    ``retryable``/``requestId``; a source that merely mentions "error" is
    not one.
    """
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("error"), str):
        return None
    if "retryable" not in data and "requestId" not in data:
        return None
    code = _CODE_RE.sub("_", data["error"].strip().lower()).strip("_")[:64] or "arc1_error"
    where = str(data.get("target") or target)
    message = str(data.get("message") or data["error"])
    detail = f"ARC-1 {tool} failed on {where}: {_short(message)}"
    extra: dict[str, Any] = {"target": _short(where)}
    if data.get("requestId") is not None:
        rid = _short(data["requestId"])
        detail += f" (requestId {rid})"
        extra["request_id"] = rid
    if "retryable" in data:
        extra["retryable"] = bool(data["retryable"])
    return Arc1Error(502, detail, code, extra)


def env_url_name(target: str) -> str:
    return "IDE_ARC1_URL_" + re.sub(r"[^A-Za-z0-9]", "_", target).upper()


# One MCP server object per (target, mode, url, destination). It holds no
# identity: the auth resolves the caller per request and every call opens its
# own session through ``for_run``.
_SERVERS: dict[tuple[str, str, str, str], Any] = {}


# A trace request id as ARC-1 hands it out; also the only shape accepted
# for a cancel.
TRACE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_TRACE_USER_KEYS = frozenset({"traceuser", "user"})
TRACE_EXPIRES_RE = re.compile(r"[0-9][0-9T:+\-.Z ]{7,39}")


def require_user_context(destination: str) -> None:
    """Refuse a trace action that could not run in SAP as the signed-in user.

    A user JWT must be bound, always. On CF the target must also have a
    destination (built with ``user_context: true``), because only the
    destination service exchanges that JWT for the user's SAP identity; the
    plain jwt mode is local development only.
    """
    if not current_jwt.get():
        raise Arc1UserRequired()
    if shared.ON_CF and not (destination or "").strip():
        raise Arc1NotConfigured(
            "Arming a trace needs an ARC-1 destination with user context "
            "for this target"
        )


def parse_trace_request(text: str) -> dict[str, str]:
    """``{trace_request_id?, expires_at?}`` from a ``trace_start`` answer.

    The live answer's shape is not known yet (plan 1c L4), so a few key
    spellings are accepted, at the top level or one object down. Values that
    are not a well-formed id or timestamp are dropped, not passed on.
    """
    data = _loads(text)
    nodes = [data] if isinstance(data, dict) else []
    nodes += [v for v in (data.values() if isinstance(data, dict) else []) if isinstance(v, dict)]
    out: dict[str, str] = {}
    for node in nodes:
        # Not ``requestId``: ARC-1 uses that for its own call correlation id.
        rid = _first(node, "id", "traceRequestId", "trace_request_id")
        if "trace_request_id" not in out and isinstance(rid, str) and TRACE_ID_RE.fullmatch(rid):
            out["trace_request_id"] = rid
        exp = _first(node, "expiresAt", "expires_at", "expires", "expiry")
        if "expires_at" not in out and isinstance(exp, str) and TRACE_EXPIRES_RE.fullmatch(exp):
            out["expires_at"] = exp
    return out


class Arc1Client:
    """Read-only ARC-1 calls for one target, as the signed-in user."""

    def __init__(
        self,
        target: str,
        destination: str = "",
        policy: str = readonly.CHANGE,
    ):
        self.target = target
        self.destination = (destination or "").strip()
        self.policy = policy

    def _server(self) -> Any:
        url = os.environ.get(env_url_name(self.target), "").strip()
        if self.destination:
            mode = "destination"
            base = url or f"{PLACEHOLDER_BASE}/mcp"
            oauth: dict[str, Any] | None = {
                "destination": self.destination,
                "user_context": True,
            }
        else:
            if not url:
                raise Arc1NotConfigured(
                    f"Target {self.target!r} has no ARC-1 destination and "
                    f"{env_url_name(self.target)} is not set"
                )
            mode, base, oauth = "jwt", url, None
        key = (self.target, mode, base, self.destination)
        server = _SERVERS.get(key)
        if server is None:
            server = shared.create_mcp_server(
                f"ide-{self.target}", base, mode, oauth=oauth
            )
            _SERVERS[key] = server
        return server

    async def call(self, tool: str, args: dict) -> str:
        reason = readonly.check_call(tool, args, self.policy)
        if reason is None and readonly.needs_approval(tool, args, self.policy):
            reason = "this action runs only through an approval"
        if reason is not None:
            logger.warning(
                "[ide] read-only policy refused direct %s for %s on %s: %s",
                _short(tool), current_principal.get() or "?", self.target, reason,
            )
            raise Arc1Refused(reason)
        # Never an app-level call: the destination exchanges this JWT for the
        # user's SAP identity, and on CF the jwt mode forwards it.
        if (self.destination or shared.ON_CF) and not current_jwt.get():
            raise Arc1UserRequired()
        return await self._send(tool, args)

    async def _send(self, tool: str, args: dict) -> str:
        """Run one tool call and map every failure to an ``Arc1Error``.
        No policy here: :meth:`call` and :meth:`_call_unchecked` own that."""
        server = self._server()
        try:
            run = await server.for_run(None)
            result = await run.direct_call_tool(tool, args)
        except Arc1Error:
            raise
        except Exception as exc:  # noqa: BLE001 -- mapped below, groups included
            if _find(exc, DestinationUserRequired) is not None:
                raise Arc1UserRequired() from exc
            retry = _find(exc, ModelRetry)
            if retry is not None:
                payload = arc1_error_from_text(tool, self.target, str(retry))
                if payload is not None:
                    raise payload from exc
                raise Arc1Error(502, f"ARC-1 {tool} failed: {_short(retry)}") from exc
            dest = _find(exc, DestinationError)
            if dest is not None:
                # Destination texts name hosts, tenants and service URLs:
                # the detail goes to the log, the client gets a fixed text.
                logger.warning(
                    "[ide] ARC-1 %s on %s: destination error: %s",
                    tool, self.target, _short(dest), exc_info=True,
                )
                raise Arc1Error(
                    502,
                    "The ARC-1 destination could not be used. An administrator "
                    "can find the details in the application log.",
                    "destination_error",
                ) from exc
            logger.warning(
                "[ide] ARC-1 %s on %s failed", tool, self.target, exc_info=True
            )
            raise Arc1Error(
                502, f"ARC-1 {tool} failed: {type(exc).__name__}"
            ) from exc
        text = _result_text(result)
        payload = arc1_error_from_text(tool, self.target, text)
        if payload is not None:
            logger.warning(
                "[ide] ARC-1 %s on %s returned error %s", tool, self.target, payload.code
            )
            raise payload
        return text

    # --- trace approvals ----------------------------------------------------

    async def arm_trace(self, params: dict) -> dict[str, str]:
        """Arm a trace for the signed-in user; only the approval path calls it.

        The ARC-1 arguments are built here from ``params`` after they pass
        ``approvals.normalize_request`` once more, so this method cannot send
        a key outside the allowlist, a bound above the cap or -- above all --
        a ``traceUser``: without one ARC-1 traces the connected user, which
        under principal propagation is the developer who approved.
        Returns ``{trace_request_id?, expires_at?}``; nothing else of the
        answer is passed on.
        """
        from agents.ide.approvals import ApprovalError, normalize_request

        try:
            clean = normalize_request("trace_start", params)
        except ApprovalError as exc:
            raise Arc1Refused(f"invalid trace parameters: {exc.message}") from exc
        text = await self._call_unchecked(
            "SAPDiagnose", {"action": "trace_start", **clean}
        )
        return parse_trace_request(text)

    async def cancel_trace(self, request_id: str) -> dict[str, str]:
        """Cancel a trace request by id; only the approval path calls it."""
        if not isinstance(request_id, str) or not TRACE_ID_RE.fullmatch(request_id):
            raise Arc1Refused("invalid trace request id")
        await self._call_unchecked(
            "SAPDiagnose", {"action": "trace_cancel", "id": request_id}
        )
        return {"trace_request_id": request_id}

    async def _call_unchecked(self, tool: str, args: dict) -> str:
        """Send ``trace_start``/``trace_cancel`` -- the two calls :meth:`call`
        always refuses. "Unchecked" means "not by the read-only policy"; it
        still takes nothing but those two actions of ``SAPDiagnose`` in a
        diagnose client, refuses any user argument, and requires a bound JWT
        and (on CF) a user-context destination. Callers keep only an id and
        an expiry of the result.
        """
        action = args.get("action") if isinstance(args, dict) else None
        if (
            self.policy != readonly.DIAGNOSE
            or tool != "SAPDiagnose"
            or not isinstance(action, str)
            or action not in readonly.APPROVAL_ACTIONS
        ):
            raise Arc1Refused("only an approved trace action of a diagnose session")
        if any(str(key).lower() in _TRACE_USER_KEYS for key in args):
            raise Arc1Refused("a trace is always armed for the signed-in user")
        require_user_context(self.destination)
        return await self._send(tool, args)


def get_arc1_client(
    target: str,
    destination: str = "",
    policy: str = readonly.CHANGE,
) -> Arc1Client:
    """The client the routes use; replace it in tests."""
    return Arc1Client(target, destination, policy)


# --- result parsing ---------------------------------------------------------


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _items(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []


def parse_findings(text: str) -> list[dict[str, Any]]:
    """SAPLint ``lint`` output as ``[{line, column, severity, message, rule}]``.

    Accepts a list of issues or an object holding one (``findings``,
    ``issues``, ``results``, ``errors``/``warnings``); positions as flat
    ``line``/``column`` or ``start: {row, col}``. Severity is lower-cased
    (live abaplint sends ``"warning"``/``"error"``). Anything unparseable yields
    no findings rather than an error.
    """
    data = _loads(text)
    raw = _items(data, "findings", "issues", "results")
    if not raw and isinstance(data, dict):
        raw = [
            {**i, "severity": i.get("severity") or sev}
            for key, sev in (("errors", "Error"), ("warnings", "Warning"))
            for i in data.get(key) or []
            if isinstance(i, dict)
        ]
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start") if isinstance(item.get("start"), dict) else {}
        out.append({
            "line": _int(_first(item, "line", "row", default=start.get("row"))),
            "column": _int(_first(item, "column", "col", default=start.get("col"))),
            "severity": str(_first(item, "severity", "type", default="error")).lower(),
            "message": str(_first(item, "message", "description", "text", default="")),
            "rule": str(_first(item, "rule", "key", "ruleKey", "code", default="")),
        })
    return out


# --- SAP version marker and "does not exist" --------------------------------

# ``IdeWorkspaceFile.origin_version`` column length.
MAX_VERSION_CHARS = 255
_VERSION_LIST_KEYS = ("revisions", "versions", "entries", "items", "results")


def _iso_key(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _newest(entries: list[dict]) -> dict:
    """The newest revision: by ``date`` when every entry carries a readable
    ISO date, else the first one (ADT lists revisions newest first)."""
    keyed = [(_iso_key(_first(e, "date", "created", "timestamp")), e) for e in entries]
    if all(stamp is not None for stamp, _ in keyed):
        return max(keyed, key=lambda pair: pair[0])[1]
    return entries[0]


def parse_version(text: str) -> str | None:
    """The version marker of a ``SAPRead type=VERSIONS`` answer, else ``None``.

    The live payload is unverified (plan A1): a list of revisions, at the top
    level or under one of a few keys, whose newest entry's ``id``, else
    ``uri``, else ``date`` is the marker. Anything else -- not JSON, no
    entries, a marker that is not a short string or number -- is ``None``
    ("version unknown"), never a guess.
    """
    data = _loads(text)
    entries = [e for e in _items(data, *_VERSION_LIST_KEYS) if isinstance(e, dict)]
    if not entries:
        return None
    newest = _newest(entries)
    for key in ("id", "uri", "date"):
        value = newest.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            value = str(value)
        if isinstance(value, str) and value.strip():
            marker = value.strip()
            if len(marker) <= MAX_VERSION_CHARS and marker.isprintable():
                return marker
            return None
    return None


async def read_version(client: Any, type_: str, name: str) -> str | None:
    """The SAP version marker of an object, read as the client's user.

    Best effort: the marker only labels the base ("version unknown" when
    absent), so any error or an answer of an unknown shape is ``None``,
    logged at INFO -- never a failure of the open it belongs to.
    """
    try:
        text = await client.call(
            "SAPRead", {"type": "VERSIONS", "objectType": type_, "name": name}
        )
    except Exception as exc:  # noqa: BLE001 -- best effort, see above
        logger.info(
            "[ide] version marker of %s %s not read: %s",
            type_, name, getattr(exc, "code", type(exc).__name__),
        )
        return None
    marker = parse_version(text)
    if marker is None:
        logger.info(
            "[ide] version marker of %s %s: answer of an unknown shape", type_, name
        )
    return marker


# "Not found" counts only when the message is about the object itself:
# "<object word or type> <NAME> does not exist / doesn't exist / (was|is)
# not found / could not be found", optionally behind up to three "<prefix>: "
# segments (ARC-1 wraps SAP's text as "ARC-1 SAPRead failed: <text>", and an
# ADT status as "ADT HTTP 404: <text>", which does not count towards the
# three) and nothing after it but a full stop. "Method FOO of class ZCL_X not
# found", "Version 3 of class ZCL_X not found" and "Include ZX_TOP not found in
# class ZCL_X" are about something else and do not count. The name slot must
# look like an object name -- quoted, or with no lowercase letter, or with a
# digit, "_", "/" or "$" -- so "Class method not found" or "Function module
# not found" (an English word where the name belongs) do not count. The
# message must also say nothing about a user, an authorisation, a
# destination, a target, a tool or a token: a wrong "absent" makes the
# reviewer read a change to an existing object as a new one. This applies to
# a 404 status and a not-found code too.
# L1 (the live check, plan A2) replaces this with ARC-1's real payload.
_OBJECT_WORDS = (
    r"object|class|interface|program|report|include|function(?:\s+module)?"
    r"|function\s+group|cds(?:\s+view)?|view|ddl\s+source|access\s+control"
    r"|metadata\s+extension|behaviou?r\s+definition|service\s+definition"
    r"|service\s+binding|data\s+definition|data\s+element|domain|table"
    r"|structure"
    r"|CLAS|INTF|PROG|INCL|FUNC|FUGR|DDLS|DCLS|DDLX|BDEF|SRVD|SRVB|TABL|DTEL"
    r"|DOMA|STRU"
)
# Quoted, or (case-sensitively) no lowercase letter, or a digit/_///$ in it.
_OBJECT_NAME = (
    r"(?:(['\"])[A-Za-z0-9_/$]+\1"
    r"|(?-i:[A-Z0-9_/$]+)(?=\s)"
    r"|(?=[A-Za-z0-9_/$]*[0-9_/$])[A-Za-z0-9_/$]+)"
)
_NOT_FOUND_OBJECT = re.compile(
    r"^(?:[^:\n]{1,120}:\s+){0,3}"
    r"(?:ADT\s+HTTP\s+\d{3}:\s+)?"
    rf"(?:the\s+)?(?:{_OBJECT_WORDS})\s+{_OBJECT_NAME}\s+"
    r"(?:does\s+not\s+exist|doesn['\u2019]t\s+exist|(?:was\s+|is\s+)?not\s+found"
    r"|could\s+not\s+be\s+found)\s*\.?$",
    re.IGNORECASE,
)
_NOT_FOUND_DENY = re.compile(
    r"\b(?:users?|authori[sz]\w*|destinations?|targets?|tools?|tokens?|logon|"
    r"credentials?|permissions?|principal)\b",
    re.IGNORECASE,
)


def is_not_found(exc: Arc1Error) -> bool:
    """Did ARC-1 say the object does not exist (plan A2)?

    Only when the message is object-shaped (see ``_NOT_FOUND_OBJECT``) and
    mentions no user, authorisation, destination, target, tool or token --
    whatever the status or code says; or when there is no message at all
    and the status is 404. A status 404 or a not-found code next to a
    message about a target, a tool or a user is not about the object.
    Anything else is not "not found" -- the base check then stores
    ``unknown``. A refusal, a missing user token or a missing configuration
    never counts: those calls did not reach SAP, so they say nothing about
    the object.
    """
    if isinstance(exc, (Arc1Refused, Arc1UserRequired, Arc1NotConfigured)):
        return False
    detail = str(exc.detail or "").strip()
    if not detail:
        return exc.status_code == 404
    if _NOT_FOUND_DENY.search(detail):
        return False
    return bool(_NOT_FOUND_OBJECT.match(detail))


def parse_search(text: str) -> list[dict[str, str]]:
    """SAPSearch object-mode output as ``[{type, name, package, description}]``."""
    data = _loads(text)
    out = []
    for item in _items(data, "results", "objects", "items"):
        if not isinstance(item, dict):
            continue
        out.append({
            "type": str(_first(item, "type", "objectType", default="")),
            "name": str(_first(item, "name", "objectName", default="")),
            "package": str(_first(item, "package", "packageName", "devclass", default="")),
            "description": str(_first(item, "description", default="")),
        })
    return out
