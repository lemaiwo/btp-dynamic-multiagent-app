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

Seam for tests: :func:`get_arc1_client`; the routes look it up on this
module at call time, so ``monkeypatch.setattr(arc1, "get_arc1_client", ...)``
replaces it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from fastapi import HTTPException
from pydantic_ai.exceptions import ModelRetry

from agents import shared
from agents.auth import current_jwt, current_principal
from agents.destination import DestinationError
from agents.destination_auth import PLACEHOLDER_BASE, DestinationUserRequired
from agents.ide import readonly

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


def _result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result)
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode("utf-8", errors="replace")
    return str(result)


# One MCP server object per (target, mode, url, destination). It holds no
# identity: the auth resolves the caller per request and every call opens its
# own session through ``for_run``.
_SERVERS: dict[tuple[str, str, str, str], Any] = {}


class Arc1Client:
    """Read-only ARC-1 calls for one target, as the signed-in user."""

    def __init__(self, target: str, destination: str = ""):
        self.target = target
        self.destination = (destination or "").strip()

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
        reason = readonly.check_call(tool, args)
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


def get_arc1_client(target: str, destination: str = "") -> Arc1Client:
    """The client the routes use; replace it in tests."""
    return Arc1Client(target, destination)


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
