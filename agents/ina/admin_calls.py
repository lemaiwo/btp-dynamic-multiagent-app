"""The two admin calls that reach SAP for a registered InA query: Describe and Test.

Behind ``POST /admin/api/ina/queries/{name}/describe`` and ``.../test``
(``agents/ina/admin_routes.py``), as ``agents/odata/testcall.py`` is behind
the OData catalogue's test route.

* **As whom.** The stored query's destination and identity: ``user_context``
  on, the admin who calls the route (the request's bound JWT through the
  destination); without one the call is refused before anything is resolved
  or sent, never made with the destination's own credential instead. Off,
  the destination's technical user. An OnPremise destination goes through
  the connectivity proxy by the rules of ``agents.destination_auth``.
* **No state.** Each call builds its own HTTP client (as the tools build
  theirs, ``agents.ina.tools``), InA client and session store, closed when
  it ends: nothing an agent later uses is warmed, and no SAP session of the
  admin outlives the call in this app.
* **One clock.** ``CALL_BUDGET_SECONDS`` for the whole call (GetServerInfo,
  GetResponse and the one repeat after a stale token), checked with
  ``expired()`` so a timeout of the client's own is not taken for it.
* **What comes back.** Names and counts, never a value of SAP's. Every
  failure is an :class:`InaError` of the client's closed list: its fixed
  text, SAP's cleaned message for ``sap_error``, or the fixed text of a
  destination refused as configured (``admin_text``, for the admin only).
  One log line per call: the query, the destination, the caller's
  principal, the outcome, status and duration -- no content.

Reads only. Never imports ``agents.db``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from agents.auth import current_jwt, current_principal
from agents.destination import DestinationError, connectivity_from_environment
from agents.destination_auth import DestinationRefused, destination_http_client, resolver_for
from agents.odata.preview import DESTINATION_TEXT, PREVIEW_BUDGET_SECONDS, plain
from agents.odata.session import NoCookieJar

from .client import InaClient, InaSessionStore
from .errors import InaError
from .protocol import InaDescription

logger = logging.getLogger(__name__)

_Result = TypeVar("_Result")

# The server key the destination of an admin call is resolved under.
SERVER_KEY = "builtin:ina/admin"
# Kept below the approuter's timeout, as the OData preview's budget is: a
# slower call would end as the approuter's own 504, without the route's
# code, while the app went on asking SAP as the admin.
CALL_BUDGET_SECONDS = PREVIEW_BUDGET_SECONDS
USER_REQUIRED_TEXT = (
    "This query runs in SAP as the signed-in user, but no user token reached the "
    "backend. Sign in again and retry."
)
VARIABLE_REQUIRED_TEXT = (
    "the query has a mandatory variable, which a test cannot fill; Describe checks "
    "the destination, the identity and the query"
)
_FAILED_TEXT = "the call could not be completed; the application log has the reason"


# --------------------------------------------------------------------- seams


def _resolver(destination: str) -> Any:
    """The resolver of ``destination``; new per call, so nothing is cached
    beyond the one call. A seam for tests."""
    return resolver_for({}, SERVER_KEY, destination=destination)


def _transport() -> httpx.AsyncBaseTransport | None:
    """The transport towards the destination; ``None`` = httpx's own. A seam."""
    return None


def _connectivity() -> Any:
    """The connectivity tokens, ``None`` without a binding; new per call. A seam."""
    return connectivity_from_environment()


def _proxy_transport() -> httpx.AsyncBaseTransport | None:
    """The transport towards the connectivity proxy; ``None`` = from the binding."""
    return None


# ------------------------------------------------------------------- the call


@dataclass
class _Wire:
    """The HTTP status of the last answer that arrived."""

    status: int | None = None


def _http_client(stored: dict[str, Any], wire: _Wire) -> httpx.AsyncClient:
    """A client through the stored query's destination, built as the tools
    build theirs, plus a hook that notes the status of each answer."""

    async def arrived(response: httpx.Response) -> None:
        wire.status = response.status_code

    http = destination_http_client(
        _resolver(stored["destination"]),
        user_context=stored["user_context"] is True,
        server_key=f"{SERVER_KEY}/{stored['name']}",
        transport=_transport(),
        connectivity=_connectivity(),
        proxy_transport=_proxy_transport(),
    )
    http.cookies = NoCookieJar()  # type: ignore[assignment]
    hooks = http.event_hooks
    http.event_hooks = {
        "request": list(hooks.get("request", [])),
        "response": [*hooks.get("response", []), arrived],
    }
    return http


def _log(
    action: str, stored: dict[str, Any], code: str | None, status: int | None, started: float
) -> None:
    logger.info(
        "ina %s %s: query=%s destination=%s user_context=%s by=%s status=%s code=%s "
        "duration_ms=%d",
        action,
        "failed" if code else "ok",
        stored["name"],
        stored["destination"],
        stored["user_context"] is True,
        current_principal.get() or "unknown principal",
        status if status is not None else "-",
        code or "-",
        int((time.monotonic() - started) * 1000),
    )


async def _call(
    stored: dict[str, Any],
    action: str,
    work: Callable[[InaClient], Awaitable[_Result]],
) -> tuple[_Result, int | None, int]:
    """Run ``work`` with an InA client of the stored query (``to_export``):
    ``(result, status, duration_ms)``, or the :class:`InaError` of the
    refusal. ``test`` hands the client the stored definition; ``describe``
    reads a new one."""
    wire = _Wire()
    started = time.monotonic()
    clock: asyncio.Timeout | None = None
    user_context = stored["user_context"] is True
    try:
        if user_context and not current_jwt.get():
            # Before anything is resolved: no call to the destination service
            # on behalf of nobody, and never a fall-back to its own credential.
            raise InaError("user_token_required", USER_REQUIRED_TEXT)
        http = _http_client(stored, wire)
        try:
            client = InaClient(
                http,
                InaSessionStore(),
                destination=stored["destination"],
                user_context=user_context,
                query=stored["query"],
                package=stored.get("package") or "",
                definition=stored.get("definition") if action == "test" else None,
            )
            async with asyncio.timeout(CALL_BUDGET_SECONDS) as clock:
                result = await work(client)
        finally:
            await http.aclose()
    except InaError as exc:
        refusal = exc
    except TimeoutError:
        if clock is not None and clock.expired():
            refusal = InaError(
                "timeout", f"SAP did not answer within {CALL_BUDGET_SECONDS:g} seconds"
            )
        else:
            refusal = InaError("unreachable", "SAP could not be reached")
    except (DestinationError, ValueError) as exc:
        # ValueError: `resolver_for` on an empty name. The text can quote the
        # destination service's answer: to the log only, URLs masked.
        logger.warning(
            "ina %s: destination '%s' of query '%s' could not be used (%s): %s",
            action,
            stored["destination"],
            stored["name"],
            type(exc).__name__,
            plain(str(exc), 300),
        )
        text = exc.admin_text if isinstance(exc, DestinationRefused) else DESTINATION_TEXT
        refusal = InaError("destination_error", text)
    except Exception as exc:  # noqa: BLE001 - a defect; its text may quote an answer
        logger.error("ina %s: query '%s' failed (%s)", action, stored["name"], type(exc).__name__)
        refusal = InaError("destination_error", _FAILED_TEXT)
    else:
        _log(action, stored, None, wire.status, started)
        return result, wire.status, int((time.monotonic() - started) * 1000)
    status = refusal.status if refusal.status is not None else wire.status
    _log(action, stored, refusal.code, status, started)
    raise refusal


async def read_description(stored: dict[str, Any]) -> InaDescription:
    """What SAP describes for the stored query; nothing is stored here."""

    async def describe(client: InaClient) -> InaDescription:
        return await client.describe()

    description, _, _ = await _call(stored, "describe", describe)
    return description


async def _test_columns(client: InaClient) -> list[str]:
    """One read of the first measure, ``top`` 1, no group: the column names.

    An undescribed query is described first (not stored: a test changes
    nothing). A mandatory variable is refused before the read is sent: a
    test has no value for it, and inventing one would read data nobody
    asked for.
    """
    definition = client.definition
    if definition is None:
        definition = (await client.describe()).definition
    if any(variable.mandatory for variable in definition.variables):
        raise InaError("variable_required", VARIABLE_REQUIRED_TEXT)
    measures = [definition.measures[0].name] if definition.measures else []
    result = await client.run(measures, [], top=1)
    columns = result.get("columns") if isinstance(result, dict) else None
    return [c for c in columns or [] if isinstance(c, str)]


async def run_test_read(stored: dict[str, Any]) -> dict[str, Any]:
    """The test of the stored query: ``{ok, status, duration_ms, columns}``
    (``status`` of SAP's last answer), never a value."""
    columns, status, duration_ms = await _call(stored, "test", _test_columns)
    return {"ok": True, "status": status, "duration_ms": duration_ms, "columns": columns}


__all__ = [
    "CALL_BUDGET_SECONDS",
    "SERVER_KEY",
    "USER_REQUIRED_TEXT",
    "VARIABLE_REQUIRED_TEXT",
    "read_description",
    "run_test_read",
]
