"""The HTTP client of the agent register: the only code that talks to it.

The register is reached through a BTP destination, which names the host and
mints the ``Authorization`` header. Every call is a JSON ``POST`` to
``<destination URL>/logging/<action>``.

What this client holds to, because the register is a side channel of every
model call and must never become a way out for a credential or somebody
else's text:

* **Fail closed, never raise.** A failed resolve, a refused or unreachable
  register, a timeout: each is a :class:`SendOutcome` with ``ok=False`` and a
  failure that is an exception CLASS name or ``http_<status>``. Exception
  text, URLs, header values and bodies stay out of the outcome and the log.
* **The body of a failure is never read.** A status of 300 or more closes the
  streamed response without reading it (an error page may echo the request).
* **No redirect is followed** (the credential would go with it) and **nothing
  is retried**. A 401 drops the cached destination so the NEXT call resolves
  it afresh; the call that got the 401 is not sent again.
* **One deadline per call**, ``asyncio.timeout`` around resolve, send and
  read as well as httpx's own timeout: a register or destination service that
  hangs costs at most ``timeout`` seconds.
* httpx's ``HTTP Request: POST <url>`` INFO line is held back by the context
  variable filter of :mod:`agents.bitbucket_tools`, set only in the sending
  task.

Task scope: the technical identity only. ``identity`` (a signed-in user) is
accepted by :meth:`RegisterClient.post` and not used yet.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from agents.bitbucket_tools import _install_once, _sending
from agents.destination import Destination
from agents.nexus.context import Identity

logger = logging.getLogger(__name__)

BASE_PATH = "/logging"
# The answer of ``registerAgents`` for 500 agents is a few tens of KB.
MAX_ANSWER_BYTES = 2 * 1024 * 1024


class _Resolver(Protocol):
    async def resolve(self, *, force: bool = False, user_token: str | None = None,
                      principal: str | None = None) -> Destination: ...

    def invalidate(self, principal: str | None = None) -> None: ...


@dataclass(frozen=True)
class SendOutcome:
    """What became of one register call; never carries a text from outside."""

    ok: bool
    status: int | None
    failure: str | None
    user_fallback: bool = False


class _AnswerTooLarge(Exception):
    """The 2xx answer passed :data:`MAX_ANSWER_BYTES`."""


def action_url(base: str, action: str) -> str:
    """``<base>/logging/<action>``, without doubling a ``/logging`` the URL has."""
    root = base.rstrip("/")
    if not root.endswith(BASE_PATH):
        root += BASE_PATH
    return f"{root}/{action}"


class RegisterClient:
    """Sends register calls through a destination (technical identity)."""

    def __init__(self, technical: _Resolver, user: _Resolver | None = None, *,
                 timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._technical = technical
        self._user = user
        self._timeout = float(timeout)
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, action: str, body: dict, *,
                   identity: Identity | None = None) -> SendOutcome:
        """Send one call; the answer's body is not read."""
        outcome, _ = await self._exchange(action, body, read_answer=False)
        return outcome

    async def register(self, agents: list[dict]) -> list[dict] | None:
        """Push the agent list; the register's ``value`` list, or ``None``.

        ``None`` for any failure and for an answer that is not
        ``{"value": [<object>, ...]}``; the reason is logged by class only.
        """
        outcome, answer = await self._exchange("registerAgents", {"agents": agents},
                                               read_answer=True)
        if not outcome.ok:
            return None
        value = answer.get("value") if isinstance(answer, dict) else None
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            logger.warning("agent register: registerAgents answer not usable (%s)",
                           type(value).__name__)
            return None
        return value

    async def _exchange(self, action: str, body: dict, *,
                        read_answer: bool) -> tuple[SendOutcome, Any]:
        answer: Any = None
        try:
            # Ours or a callee's, a ``TimeoutError`` is the same outcome here;
            # httpx's own timeouts are ``httpx.TimeoutException`` subclasses.
            async with asyncio.timeout(self._timeout):
                destination = await self._technical.resolve()
                status, answer = await self._send(destination, action, body, read_answer)
        except TimeoutError:
            return self._failed(action, "TimeoutError"), None
        except Exception as e:  # noqa: BLE001 - its text is not ours to pass on
            return self._failed(action, type(e).__name__), None
        if status >= 300:
            if status == 401:
                self._technical.invalidate()
            return self._failed(action, f"http_{status}", status), None
        return SendOutcome(ok=True, status=status, failure=None), answer

    async def _send(self, destination: Destination, action: str, body: dict,
                    read_answer: bool) -> tuple[int, Any]:
        request = self._http.build_request(
            "POST", action_url(destination.url, action),
            json=body, headers=dict(destination.headers),
        )
        _install_once()
        quiet = _sending.set(True)
        try:
            response = await self._http.send(request, stream=True)
            try:
                status = response.status_code
                if status >= 300 or not read_answer:
                    return status, None
                return status, await _read_json(response)
            finally:
                await response.aclose()
        finally:
            _sending.reset(quiet)

    @staticmethod
    def _failed(action: str, failure: str, status: int | None = None) -> SendOutcome:
        logger.debug("agent register: %s failed (%s)", action, failure)
        return SendOutcome(ok=False, status=status, failure=failure)


async def _read_json(response: httpx.Response) -> Any:
    """The answer as JSON, read up to :data:`MAX_ANSWER_BYTES`."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_ANSWER_BYTES:
            raise _AnswerTooLarge
        chunks.append(chunk)
    return json.loads(b"".join(chunks))
