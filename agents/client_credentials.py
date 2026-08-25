"""App-only OAuth2 (``client_credentials``) for MCP servers and built-in tools.

The counterpart of :mod:`agents.oauth2`. That module authenticates *a user*:
somebody signs in once in a browser, the tokens are stored per (user, server),
and every request carries the token of whoever is logged in. This module
authenticates *the application*: there is no browser leg, no user, and nothing
to store per person.

That is the whole reason it exists. A service mailbox nobody signs into cannot
be reached by the delegated flow -- there is no "me" to be. Scheduled runs get
simpler too: no refresh token to keep alive, no re-consent when someone leaves.

What is given up is the identity that scopes access. A delegated token can only
reach what its owner can reach; an app-only token reaches whatever an admin
consented to for the entire registration, which for Microsoft Graph means every
mailbox in the tenant unless an Exchange Application Access Policy narrows it.
Two consequences run through the code here and in the toolsets:

* the target is named explicitly in config (``mailbox``), never inferred, so a
  misconfigured agent fails instead of reaching the wrong mailbox; and
* capabilities are opted into per server (``allow_send``) rather than taken
  from what the token happens to permit.

Tokens are cached in memory per server key. They are not persisted: they last
minutes, are re-fetchable at any time without a human, and writing them to the
database would add a second place for a credential to leak from and buy
nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Refetch this many seconds before the token actually expires, so a request
# that takes a moment to reach the server does not arrive holding a token that
# expired in flight.
EXPIRY_SKEW_SECONDS = 60

# Used when the token response omits expires_in. Short on purpose: refetching
# an app-only token is one cheap call, and guessing long risks 401 loops.
DEFAULT_LIFETIME_SECONDS = 300


class ClientCredentialsError(RuntimeError):
    """The token endpoint refused to issue an app-only token.

    Carries the provider's own error text: for Entra that is an ``AADSTS`` code
    which says precisely what is wrong, and paraphrasing it would only lose
    information the operator needs.
    """


@dataclass(frozen=True)
class ClientCredentialsConfig:
    client_id: str
    client_secret: str
    token_url: str
    scope: str = ""
    mailbox: str = ""
    allow_send: bool = False


def config_from_oauth(oauth: dict[str, Any] | None) -> ClientCredentialsConfig:
    """Build a config from a stored ``oauth`` block.

    ``uaa_url`` is accepted as an alternative to ``token_url`` for symmetry with
    the oauth2 path, where SAP's XSUAA is given as a base URL.
    """
    src = oauth or {}
    token_url = str(src.get("token_url") or "").strip()
    if not token_url:
        uaa = str(src.get("uaa_url") or "").strip().rstrip("/")
        if uaa:
            token_url = f"{uaa}/oauth/token"
    if not token_url:
        raise ClientCredentialsError(
            "client_credentials requires a token_url (or uaa_url) in the oauth config"
        )
    client_id = str(src.get("client_id") or "").strip()
    client_secret = str(src.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ClientCredentialsError(
            "client_credentials requires both client_id and client_secret"
        )
    return ClientCredentialsConfig(
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        scope=str(src.get("scope") or "").strip(),
        mailbox=str(src.get("mailbox") or "").strip(),
        allow_send=bool(src.get("allow_send")),
    )


@dataclass
class _CachedToken:
    access_token: str
    token_type: str
    expires_at: float

    def valid(self, now: float) -> bool:
        return bool(self.access_token) and now < self.expires_at - EXPIRY_SKEW_SECONDS


class ClientCredentialsAuth(httpx.Auth):
    """Attaches an app-only bearer token, fetching and caching it as needed.

    One instance per server per built registry, so the cache lives exactly as
    long as the toolset that uses it.
    """

    requires_response_body = False

    def __init__(
        self,
        server_key: str,
        config: ClientCredentialsConfig,
        token_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.server_key = server_key
        self.config = config
        # Tests inject a transport here rather than patching httpx globally,
        # which would also intercept the API calls this auth is attached to.
        self._token_transport = token_transport
        self._token: _CachedToken | None = None
        # Without this, the first N concurrent tool calls of a run would each
        # see an empty cache and each fetch a token. Entra tolerates it, but it
        # is N times the latency on every cold start and shows up in audit logs
        # as a burst of identical grants.
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request):  # type: ignore[override]
        token = await self._access_token()
        request.headers["Authorization"] = f"{token.token_type} {token.access_token}"
        response = yield request

        if response.status_code == 401:
            # The cached token was rejected -- revoked, or the app's consent
            # changed underneath us. One forced refetch, then give up: retrying
            # a genuinely unauthorized call in a loop just multiplies the
            # failure.
            refreshed = await self._access_token(force=True)
            request.headers["Authorization"] = (
                f"{refreshed.token_type} {refreshed.access_token}"
            )
            yield request

    async def _access_token(self, *, force: bool = False) -> _CachedToken:
        now = time.monotonic()
        cached = self._token
        if not force and cached is not None and cached.valid(now):
            return cached
        async with self._lock:
            # Re-check inside the lock: while we waited, another caller may
            # have fetched a perfectly good token.
            cached = self._token
            now = time.monotonic()
            if not force and cached is not None and cached.valid(now):
                return cached
            fetched = await self._fetch()
            self._token = fetched
            return fetched

    async def _fetch(self) -> _CachedToken:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        if self.config.scope:
            data["scope"] = self.config.scope

        # A dedicated client: reusing the caller's would recurse straight back
        # into this auth flow to fetch the token it is trying to fetch.
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(30.0)}
        if self._token_transport is not None:
            kwargs["transport"] = self._token_transport
        async with httpx.AsyncClient(**kwargs) as http:
            response = await http.post(self.config.token_url, data=data)

        if response.status_code != 200:
            detail = _error_detail(response)
            logger.warning(
                "client_credentials token request for %s failed: %s %s",
                self.server_key, response.status_code, detail,
            )
            raise ClientCredentialsError(
                f"token request for {self.server_key} returned "
                f"{response.status_code}: {detail}"
            )

        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise ClientCredentialsError(
                f"token response for {self.server_key} contained no access_token"
            )
        try:
            lifetime = int(payload.get("expires_in") or DEFAULT_LIFETIME_SECONDS)
        except (TypeError, ValueError):
            lifetime = DEFAULT_LIFETIME_SECONDS

        logger.info(
            "client_credentials token obtained for %s (expires in %ss)",
            self.server_key, lifetime,
        )
        return _CachedToken(
            access_token=access_token,
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_at=time.monotonic() + max(lifetime, 1),
        )


def _error_detail(response: httpx.Response) -> str:
    """The provider's error text, without the access token if one crept in."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    parts = [
        str(payload.get(k)) for k in ("error", "error_description")
        if payload.get(k)
    ]
    return " - ".join(parts) if parts else str(payload)[:300]
