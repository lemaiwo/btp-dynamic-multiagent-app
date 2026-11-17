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
* Of the destination only the URL and the headers are used: destination
  query properties are not sent. A stored ``Host``, ``Content-Length``,
  ``Content-Type``, ``Transfer-Encoding`` or ``Cookie`` header is dropped
  (they would steer or break the request), and the HTTP client keeps no
  cookies: a ``Set-Cookie`` of one answer is never sent with the next call.

Two identities. ``registerAgents`` always goes as the technical user (the
register refuses a user token there). ``logRequest`` / ``logResponse`` of a
model call made by a signed-in user carry that user's :class:`Identity`; with
a user destination configured (``user``), its JWT is exchanged there for a
token the register sees as the user. The destination counts only when it signs
in as that user, the rule the OData callers apply: resolved for the user, an
``Authentication`` of ``OAuth2JWTBearer``, ``OAuth2UserTokenExchange`` or
``OAuth2SAMLBearerAssertion``, no ``SystemUser`` property and an
``Authorization`` the destination service minted (a Basic, client-credentials
or fixed-user destination would log a technical token as the user). When that
exchange fails, or the destination does not sign in as the user, the call goes
as the technical user instead and the outcome says ``user_fallback``; nothing
is dropped for it. Both calls of one model request go as the same identity:
the sender drops the identity of a response whose request fell back, and the
user destination a request was sent with is kept (by ``correlationId``, at
most :data:`PAIR_MEMO_MAX`, until it expires) for its response, so an exchange
that fails in between does not split the pair. A 401 under the user drops that
user's cached destination only. The JWT is never logged, never in an outcome
and never a key of anything but the resolver's own digest-keyed cache.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from agents.bitbucket_tools import install_request_log_filter, request_log_quiet
from agents.destination import Destination, DestinationResolver
from agents.destination_auth import signs_in_as_user
from agents.nexus.context import Identity
from agents.odata.session import NoCookieJar

logger = logging.getLogger(__name__)

BASE_PATH = "/logging"
# The answer of ``registerAgents`` for 500 agents is a few tens of KB.
MAX_ANSWER_BYTES = 2 * 1024 * 1024
# How many user destinations are kept for the response of their request.
PAIR_MEMO_MAX = 10_000
# The two actions a signed-in user's identity applies to.
USER_ACTIONS = frozenset({"logRequest", "logResponse"})
SENT_AS_USER = "user"
SENT_AS_TECHNICAL = "technical"
# Stored destination headers that are never sent (lower case).
DROPPED_HEADERS = frozenset({"host", "content-length", "content-type",
                             "transfer-encoding", "cookie"})


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
    # ``"user"`` or ``"technical"``: whose token the call went out with;
    # ``None`` when no destination was obtained (nothing was sent).
    sent_as: str | None = None


@dataclass
class _Pick:
    """Which destination one call took; filled in step by step."""

    user_key: str | None = None      # set when the call goes as the user
    fallback: bool = False

    @property
    def sent_as(self) -> str:
        return SENT_AS_USER if self.user_key is not None else SENT_AS_TECHNICAL


class _AnswerTooLarge(Exception):
    """The 2xx answer passed :data:`MAX_ANSWER_BYTES`."""


def action_url(base: str, action: str) -> str:
    """``<base>/logging/<action>``, without doubling a ``/logging`` the URL has."""
    root = base.rstrip("/")
    if not root.endswith(BASE_PATH):
        root += BASE_PATH
    return f"{root}/{action}"


class RegisterClient:
    """Sends register calls through a destination, as the technical user or a user."""

    def __init__(self, technical: _Resolver, user: _Resolver | None = None, *,
                 timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._technical = technical
        self._user = user
        self._timeout = float(timeout)
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
            cookies=NoCookieJar(),
        )
        self._pair_destinations: OrderedDict[str, Destination] = OrderedDict()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, action: str, body: dict, *,
                   identity: Identity | None = None) -> SendOutcome:
        """Send one call; the answer's body is not read.

        ``identity`` is used for ``logRequest`` / ``logResponse`` only.
        """
        if action not in USER_ACTIONS:
            identity = None
        outcome, _ = await self._exchange(action, body, read_answer=False, identity=identity)
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

    async def _exchange(self, action: str, body: dict, *, read_answer: bool,
                        identity: Identity | None = None) -> tuple[SendOutcome, Any]:
        answer: Any = None
        pick = _Pick()
        sent_as: str | None = None
        try:
            # Ours or a callee's, a ``TimeoutError`` is the same outcome here;
            # httpx's own timeouts are ``httpx.TimeoutException`` subclasses.
            async with asyncio.timeout(self._timeout):
                destination = await self._destination(pick, action, body, identity)
                sent_as = pick.sent_as
                status, answer = await self._send(destination, action, body, read_answer)
        except TimeoutError:
            return self._failed(action, "TimeoutError", pick=pick, sent_as=sent_as), None
        except Exception as e:  # noqa: BLE001 - its text is not ours to pass on
            return self._failed(action, type(e).__name__, pick=pick, sent_as=sent_as), None
        if status >= 300:
            if status == 401:
                self._unauthorized(pick, body)
            return self._failed(action, f"http_{status}", status, pick=pick,
                                sent_as=sent_as), None
        return SendOutcome(ok=True, status=status, failure=None,
                           user_fallback=pick.fallback, sent_as=sent_as), answer

    async def _destination(self, pick: _Pick, action: str, body: dict,
                           identity: Identity | None) -> Destination:
        """The user's destination when it can be had, else the technical one."""
        if identity is not None and self._user is not None:
            correlation_id = body.get("correlationId") if isinstance(body, dict) else None
            if not isinstance(correlation_id, str):
                correlation_id = None
            user_key = DestinationResolver.user_key(identity.principal, identity.jwt)
            # Popped for any response with an identity: the entry exists only
            # when this pair's request went as the user, and correlation ids
            # are fresh UUIDs, so it is this pair's (and is used at most once).
            if action == "logResponse" and correlation_id is not None:
                kept = self._pair_destinations.pop(correlation_id, None)
                if kept is not None and time.monotonic() < kept.expires_at:
                    pick.user_key = user_key
                    return kept
            try:
                destination = await self._user.resolve(user_token=identity.jwt,
                                                       principal=identity.principal)
            except Exception as e:  # noqa: BLE001 - its text may carry the token
                logger.debug("agent register: user destination failed (%s); "
                             "technical instead", type(e).__name__)
            else:
                if _signs_in_as_user(destination):
                    pick.user_key = user_key
                    if action == "logRequest" and correlation_id is not None:
                        self._keep_for_response(correlation_id, destination)
                    return destination
                logger.debug("agent register: user destination does not sign in as "
                             "the user; technical instead")
            pick.fallback = True
        return await self._technical.resolve()

    def _keep_for_response(self, correlation_id: str, destination: Destination) -> None:
        memo = self._pair_destinations
        now = time.monotonic()
        # Expired entries go first: a response that never comes must not keep
        # a minted register token alive until the bound pushes it out.
        while memo and next(iter(memo.values())).expires_at <= now:
            memo.popitem(last=False)
        memo[correlation_id] = destination
        memo.move_to_end(correlation_id)
        while len(memo) > PAIR_MEMO_MAX:
            memo.popitem(last=False)

    def _unauthorized(self, pick: _Pick, body: dict) -> None:
        """Drop the refused credential so the NEXT call resolves it afresh."""
        if pick.user_key is None:
            self._technical.invalidate()
            return
        assert self._user is not None
        self._user.invalidate(pick.user_key)
        correlation_id = body.get("correlationId") if isinstance(body, dict) else None
        if isinstance(correlation_id, str):
            self._pair_destinations.pop(correlation_id, None)

    async def _send(self, destination: Destination, action: str, body: dict,
                    read_answer: bool) -> tuple[int, Any]:
        headers = {name: value for name, value in destination.headers.items()
                   if name.lower() not in DROPPED_HEADERS}
        request = self._http.build_request(
            "POST", action_url(destination.url, action), json=body, headers=headers,
        )
        install_request_log_filter()
        quiet = request_log_quiet.set(True)
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
            request_log_quiet.reset(quiet)

    @staticmethod
    def _failed(action: str, failure: str, status: int | None = None, *,
                pick: _Pick | None = None, sent_as: str | None = None) -> SendOutcome:
        logger.debug("agent register: %s failed (%s)", action, failure)
        return SendOutcome(ok=False, status=status, failure=failure,
                           user_fallback=bool(pick and pick.fallback), sent_as=sent_as)


def _signs_in_as_user(destination: Destination) -> bool:
    """``destination_auth.signs_in_as_user`` plus a minted ``Authorization``.

    The register gets only the header the destination service minted for
    the user: a stored ``Authorization`` would be somebody else's.
    """
    if not signs_in_as_user(destination):
        return False
    if "authorization" in destination.static_headers:
        return False
    return any(name.lower() == "authorization" and value
               for name, value in destination.headers.items())


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
