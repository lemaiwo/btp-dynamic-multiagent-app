"""CSRF tokens and SAP session cookies, per destination and per user.

A modifying OData request needs a CSRF token, and SAP only honours the token
together with the session cookie it was issued with. That pair *is* a SAP
session: whoever sends it is the user it was issued to, whatever credential
the request carries besides. The HTTP client of a service is shared by every
user of an agent, so the pair must never sit in the client. It lives here
and only here, in memory, keyed by ``(destination, user)``, and the write
path attaches the cookies to each request as an explicit ``Cookie`` header.

What keeps two users apart:

* The key is minted by :meth:`CsrfSessionStore.key` from the request context
  (``agents.auth.current_jwt`` / ``current_principal``), the same way
  ``DestinationAuth._user`` finds the user. It is never taken from an
  argument a model could set.
* A user's key part is prefixed (``user:`` or, when no principal could be
  derived, ``token:`` plus a digest of the JWT), so no principal can ever
  spell the technical entry's marker and two users without a principal do
  not meet in one slot.
* :meth:`CsrfSessionStore.get` serves a user's entry only while that same
  user is bound. A key that was carried into another request is refused
  (:class:`SessionIdentityError`), not served.
* A caller gets a private copy; nothing it does to the copy reaches the
  next caller.

A user-context service without a signed-in user is refused with
``DestinationUserRequired`` -- there is no fall-back to the technical entry.

No token or cookie value appears in a ``repr``, a log line or an exception
of this module. Nothing is persisted: a restart means one more token fetch.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, CookiePolicy
from typing import Any

import httpx

from agents.destination import DestinationError
from agents.destination_auth import DestinationUserRequired
from agents.odata import BUILTIN_ODATA_URL

logger = logging.getLogger(__name__)

# An ICF session times out after 30 minutes by default; a token is dropped a
# little earlier so a write does not start with one that is about to die.
SESSION_TTL_SECONDS = 25 * 60
SESSION_CACHE_MAX = 256
TECHNICAL = "technical"

_USER_PREFIX = "user:"
_TOKEN_PREFIX = "token:"

SessionKey = tuple[str, str]

# RFC 6265: a cookie name is an HTTP token; a value has no control character,
# whitespace or ";". Anything else is not stored and not sent, so a response
# cannot smuggle a second cookie or a header line into a later request.
_COOKIE_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_COOKIE_VALUE_RE = re.compile(r"[\x21-\x3A\x3C-\x7E]+")


def _now() -> float:
    """The clock of this module (a seam for the tests)."""
    return time.monotonic()


class SessionIdentityError(DestinationError):
    """A user's session entry was asked for while another user is bound."""

    def __init__(self, destination: str) -> None:
        self.destination = destination
        # No principal in the text: it would name one user to another.
        super().__init__(
            f"the SAP session of destination {destination!r} belongs to another "
            f"signed-in user than the one this request runs as"
        )


def _cookie_ok(name: object, value: object) -> bool:
    return (
        isinstance(name, str)
        and isinstance(value, str)
        and _COOKIE_NAME_RE.fullmatch(name) is not None
        and _COOKIE_VALUE_RE.fullmatch(value) is not None
    )


@dataclass(repr=False)
class CsrfSession:
    """A CSRF token and the cookies it is valid with, for one user on one destination."""

    token: str = field(repr=False)
    cookies: dict[str, str] = field(repr=False)
    expires_at: float

    @classmethod
    def fresh(cls, token: str, cookies: dict[str, str]) -> CsrfSession:
        """A session fetched just now, with the store's lifetime."""
        return cls(token=token, cookies=dict(cookies), expires_at=_now() + SESSION_TTL_SECONDS)

    def cookie_header(self) -> str:
        """The ``Cookie`` header value of this session, ``""`` without cookies.

        The caller sets it on the request explicitly; the shared client's
        jar never holds a cookie (:class:`NoCookieJar`).
        """
        return "; ".join(
            f"{name}={value}" for name, value in self.cookies.items() if _cookie_ok(name, value)
        )

    def __repr__(self) -> str:
        # Neither the token nor a cookie value: this ends up in logs and tracebacks.
        return (
            f"CsrfSession(token=<{'set' if self.token else 'empty'}>, "
            f"cookies=<{len(self.cookies)}>, expires_at={self.expires_at!r})"
        )

    __str__ = __repr__


def cookies_from_response(response: httpx.Response) -> dict[str, str]:
    """Name and value of every cookie a response sets; attributes are dropped.

    Read from the ``Set-Cookie`` headers, not from a jar: the client's jar
    stores nothing. A cookie with an empty value (a deletion) or with a
    name or value that could not be sent back safely is left out.
    """
    cookies: dict[str, str] = {}
    for header in response.headers.get_list("set-cookie"):
        pair = header.split(";", 1)[0]
        name, separator, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if separator and _cookie_ok(name, value):
            cookies[name] = value
    return cookies


class _RejectAll(CookiePolicy):
    """Stores nothing, returns nothing."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def domain_return_ok(self, domain: str, request: Any) -> bool:
        return False

    def path_return_ok(self, path: str, request: Any) -> bool:
        return False


class NoCookieJar(CookieJar):
    """A client cookie jar that never stores and never sends a cookie.

    Pass it as ``httpx.AsyncClient(cookies=NoCookieJar())``. It is a
    ``http.cookiejar.CookieJar`` and not an ``httpx.Cookies`` on purpose:
    httpx copies an ``httpx.Cookies`` argument into a plain jar of its own
    and the subclass would be gone, while a ``CookieJar`` is kept as it is.
    Besides the reject-all policy every way in is closed, so a cookie set on
    purpose (``client.cookies.set``) does not stay either.
    """

    def __init__(self) -> None:
        super().__init__(policy=_RejectAll())

    def set_policy(self, policy: CookiePolicy) -> None:
        """The policy is fixed; a caller cannot swap in a permissive one."""

    def set_cookie(self, cookie: Any) -> None:
        """Nothing is stored."""

    def set_cookie_if_ok(self, cookie: Any, request: Any) -> None:
        """Nothing is stored."""

    def extract_cookies(self, response: Any, request: Any) -> None:
        """Nothing is taken from a response."""

    def add_cookie_header(self, request: Any) -> None:
        """Nothing is added to a request."""


class CsrfSessionStore:
    """The CSRF sessions of the OData write path, one per destination and user.

    Entries live in one bounded LRU (``SESSION_CACHE_MAX``) and for at most
    ``SESSION_TTL_SECONDS``; losing one costs a token fetch, nothing else.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[SessionKey, CsrfSession] = OrderedDict()
        # One lock per key so a slow fetch of one user never holds up
        # another. Weak values: a lock exists while a call holds it, so the
        # table cannot grow past the calls in flight.
        self._locks: weakref.WeakValueDictionary[SessionKey, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        # A count only: no principal, no token, no cookie.
        return f"CsrfSessionStore(entries={len(self._entries)})"

    __str__ = __repr__

    # -- identity -----------------------------------------------------------
    @staticmethod
    def _user_part(destination: str) -> str:
        """The key part of the signed-in user, from the request context."""
        from agents.auth import current_jwt, current_principal, principal_from_token

        token = current_jwt.get()
        if not token:
            raise DestinationUserRequired(BUILTIN_ODATA_URL, destination)
        principal = current_principal.get() or principal_from_token(token)
        if principal:
            return _USER_PREFIX + principal
        # Same idea as ConnectivityTokens.user_key: without a principal the
        # token itself tells users apart, as a digest.
        return _TOKEN_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()

    def key(self, destination: str, user_context: bool) -> SessionKey:
        """``(destination, user)`` for the current request.

        ``user_context`` is the catalogue service's setting. Off: the one
        technical entry of the destination. On: the signed-in user's entry,
        or ``DestinationUserRequired`` when nobody is signed in (a scheduled
        run) -- never the technical entry.
        """
        if not isinstance(destination, str) or not destination:
            raise ValueError("a session key needs a destination name")
        if user_context is not True:
            if user_context is not False:
                raise TypeError("user_context must be a bool")
            return (destination, TECHNICAL)
        return (destination, self._user_part(destination))

    def _check_caller(self, key: SessionKey) -> None:
        """Refuse a user's key outside that user's own request."""
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not all(isinstance(part, str) and part for part in key)
        ):
            raise TypeError("a session key comes from CsrfSessionStore.key()")
        destination, owner = key
        if owner == TECHNICAL:
            return
        current = self._user_part(destination)
        if not hmac.compare_digest(current.encode("utf-8"), owner.encode("utf-8")):
            raise SessionIdentityError(destination)

    # -- entries ------------------------------------------------------------
    def _live(self, key: SessionKey) -> CsrfSession | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        if not _now() < hit.expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return hit

    @staticmethod
    def _copy(session: CsrfSession) -> CsrfSession:
        return CsrfSession(
            token=session.token, cookies=dict(session.cookies), expires_at=session.expires_at
        )

    def _store(self, key: SessionKey, session: CsrfSession) -> CsrfSession:
        if (
            not isinstance(session, CsrfSession)
            or not isinstance(session.token, str)
            or not isinstance(session.cookies, dict)
        ):
            raise TypeError("a CSRF fetch must return a CsrfSession")
        now = _now()
        kept = CsrfSession(
            token=session.token,
            cookies=dict(session.cookies),
            # A fetch may shorten the lifetime, never stretch it.
            expires_at=min(float(session.expires_at), now + SESSION_TTL_SECONDS),
        )
        for stale in [k for k, entry in self._entries.items() if not now < entry.expires_at]:
            del self._entries[stale]
        self._entries.pop(key, None)
        self._entries[key] = kept
        while len(self._entries) > SESSION_CACHE_MAX:
            self._entries.popitem(last=False)
        return kept

    async def get(
        self, key: SessionKey, fetch: Callable[[], Awaitable[CsrfSession]]
    ) -> CsrfSession:
        """The session of ``key``, fetched with ``fetch`` when there is none.

        ``fetch`` runs at most once per key at a time (a per-key lock), in
        the caller's own context, so it carries the caller's credential. Its
        failure is the caller's: nothing is stored and the next call fetches
        again. The result is a copy.
        """
        self._check_caller(key)
        hit = self._live(key)
        if hit is not None:
            return self._copy(hit)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            hit = self._live(key)
            if hit is not None:
                return self._copy(hit)
            kept = self._store(key, await fetch())
            logger.debug(
                "OData CSRF session fetched for destination %s (%s)",
                key[0],
                "technical" if key[1] == TECHNICAL else "user",
            )
            return self._copy(kept)

    def drop(self, key: SessionKey) -> None:
        """Forget the session of ``key`` (SAP answered 403); nobody else's."""
        if self._entries.pop(key, None) is not None:
            logger.debug(
                "OData CSRF session dropped for destination %s (%s)",
                key[0],
                "technical" if key[1] == TECHNICAL else "user",
            )


__all__ = [
    "SESSION_CACHE_MAX",
    "SESSION_TTL_SECONDS",
    "TECHNICAL",
    "CsrfSession",
    "CsrfSessionStore",
    "NoCookieJar",
    "SessionIdentityError",
    "SessionKey",
    "cookies_from_response",
]
