"""Per-user OAuth2 authorization_code support for MCP servers.

Used by ``auth_mode="oauth2"``. Unlike JWT forwarding (which reuses this
app's XSUAA token), this drives a real OAuth2 authorization_code + PKCE flow
against the MCP server's *own* authorization server (e.g. a separate XSUAA),
so the end user's identity reaches the target system. Tokens are stored per
(user, server) in Postgres and refreshed transparently.

Flow:
1. A specialist calls an oauth2 MCP server. ``PerUserOAuth2Auth`` finds no
   valid token for the current user and raises ``OAuthAuthorizationRequired``.
2. The delegation tool catches it and returns an "Authorize" link built by
   :func:`begin_authorization` (CSRF state + PKCE verifier persisted).
3. The user authorizes in the browser; the target redirects to
   ``/oauth/callback`` which calls :func:`complete_authorization` to exchange
   the code for tokens and persist them.
4. The user retries; ``PerUserOAuth2Auth`` now attaches the access token and
   refreshes it on expiry.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from mcp.client.auth.oauth2 import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
)
from mcp.shared.auth import OAuthClientMetadata

from agents.auth import current_base_url, current_principal
from agents.db import (
    AUTH_MODE_OAUTH2,
    SessionLocal,
    delete_oauth_client,
    delete_user_token,
    get_oauth_client,
    get_user_token,
    list_agents,
    pop_oauth_state,
    save_oauth_client,
    save_oauth_state,
    upsert_user_token,
)

logger = logging.getLogger(__name__)

CLIENT_NAME = "SAP BTP Dynamic Multi-Agent"

# Refresh access tokens this many seconds before they actually expire.
_EXPIRY_SKEW_SECONDS = 60
# How long an authorization link stays valid before the user must restart.
_STATE_TTL_SECONDS = 600

_CALLBACK_PATH = "/oauth/callback"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Oauth2Config:
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None = None
    scope: str | None = None

    @classmethod
    def from_spec(cls, oauth: dict[str, Any]) -> "Oauth2Config":
        """Build from a stored *manual* server ``oauth`` dict.

        Accepts either an XSUAA-style ``uaa_url`` (authorize/token endpoints
        are derived) or explicit ``authorize_url`` + ``token_url``.
        """
        authorize_url = (oauth.get("authorize_url") or "").strip()
        token_url = (oauth.get("token_url") or "").strip()
        uaa = (oauth.get("uaa_url") or "").strip().rstrip("/")
        if uaa:
            authorize_url = authorize_url or f"{uaa}/oauth/authorize"
            token_url = token_url or f"{uaa}/oauth/token"
        if not authorize_url or not token_url:
            raise ValueError("oauth config needs uaa_url or authorize_url+token_url")
        return cls(
            authorize_url=authorize_url,
            token_url=token_url,
            client_id=str(oauth["client_id"]),
            client_secret=str(oauth["client_secret"]),
            scope=(oauth.get("scope") or None),
        )


class OAuthAuthorizationRequired(Exception):
    """Raised by the auth flow when the current user must (re)authorize.

    Carries enough context for the delegation tool to mint an authorize URL.
    ``config`` may be None when the failure happens before a config is known
    (e.g. no bound principal).
    """

    def __init__(
        self, server_key: str, config: Oauth2Config | None = None, reason: str = ""
    ) -> None:
        self.server_key = server_key
        self.config = config
        self.reason = reason
        super().__init__(reason or f"authorization required for {server_key}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_mcp_url(base_url: str) -> str:
    """The server_key for a configured MCP URL.

    Delegates to ``agents.shared.mcp_endpoint_url`` rather than repeating the
    rule, so the key tokens are stored under always matches the URL the live
    connection actually calls.
    """
    from agents.shared import mcp_endpoint_url

    return mcp_endpoint_url(base_url)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _expiry_from(expires_in: Any) -> datetime | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at - timedelta(seconds=_EXPIRY_SKEW_SECONDS)


async def _post_token(config: Oauth2Config, data: dict[str, str]) -> httpx.Response:
    # Use client_secret_post: send client_id (and client_secret, when the
    # client is confidential) in the form body. This is what MCP servers'
    # OAuth advertises (token_endpoint_auth_methods_supported) and what XSUAA
    # accepts; client_secret_basic is not universally supported.
    form = {**data, "client_id": config.client_id}
    if config.client_secret:
        form["client_secret"] = config.client_secret
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.post(
            config.token_url,
            data=form,
            headers={"Accept": "application/json"},
        )


def _origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _callback_uri() -> str | None:
    """The redirect_uri for the current request, or None outside a request."""
    base_url = current_base_url.get()
    return base_url.rstrip("/") + _CALLBACK_PATH if base_url else None


def _is_invalid_client(resp: Any) -> bool:
    """True when the authorization server rejects our client registration.

    Servers that issue *stateless* client_ids (the metadata signed into the id
    itself, as ARC-1 does) answer ``invalid_client`` for every client they
    registered before their signing secret last changed.
    """
    if resp.status_code not in (400, 401):
        return False
    try:
        return (resp.json() or {}).get("error") == "invalid_client"
    except Exception:  # noqa: BLE001 — non-JSON error body
        return "invalid_client" in (resp.text or "")


async def _forget_registered_client(server_key: str, spec_oauth: dict[str, Any] | None) -> bool:
    """Evict a cached DCR registration the target no longer accepts.

    Returns True when a registration was dropped. Only DCR clients are evicted:
    manual credentials are operator-managed, and deleting those would silently
    discard configuration the app cannot recreate.
    """
    if not (spec_oauth or {}).get("dcr"):
        return False
    async with SessionLocal() as session:
        await delete_oauth_client(session, server_key)
    logger.info(
        "Dropped rejected DCR client registration for %s; re-registering on next use",
        server_key,
    )
    return True


# ---------------------------------------------------------------------------
# Discovery + Dynamic Client Registration (RFC 8414 / 9728 / 7591)
# ---------------------------------------------------------------------------
async def _discover_and_register(
    server_key: str, redirect_uri: str, scope: str | None
) -> Oauth2Config:
    """Discover the MCP server's authorization server and register a client.

    Uses the MCP SDK's spec-compliant discovery + DCR helpers. Raises
    ValueError if metadata can't be discovered or the server doesn't support
    Dynamic Client Registration.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        # 1. Protected-resource metadata -> authorization server issuer.
        auth_server_url: str | None = None
        for url in build_protected_resource_metadata_discovery_urls(None, server_key):
            try:
                resp = await client.send(create_oauth_metadata_request(url))
                prm = await handle_protected_resource_response(resp)
            except Exception:
                prm = None
            if prm and prm.authorization_servers:
                auth_server_url = str(prm.authorization_servers[0])
                break

        # 2. Authorization server metadata (authorize/token/registration).
        asm = None
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            auth_server_url, server_key
        ):
            try:
                resp = await client.send(create_oauth_metadata_request(url))
                ok, meta = await handle_auth_metadata_response(resp)
            except Exception:
                ok, meta = True, None
            if meta is not None:
                asm = meta
                break
            if not ok:
                break
        if asm is None:
            raise ValueError(
                f"Could not discover OAuth metadata for {server_key}. The server "
                "may not advertise OAuth; configure client credentials manually."
            )

        # 3. Dynamic Client Registration.
        client_meta = OAuthClientMetadata(
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
            client_name=CLIENT_NAME,
            scope=scope or None,
        )
        auth_base = auth_server_url or _origin_of(server_key)
        try:
            reg_resp = await client.send(
                create_client_registration_request(asm, client_meta, auth_base)
            )
            info = await handle_registration_response(reg_resp)
        except Exception as e:
            raise ValueError(
                f"Dynamic Client Registration failed for {server_key}: {e}. "
                "The server may not support DCR; configure client credentials manually."
            ) from e

    return Oauth2Config(
        authorize_url=str(asm.authorization_endpoint),
        token_url=str(asm.token_endpoint),
        client_id=info.client_id,
        client_secret=info.client_secret,
        scope=scope or None,
    )


async def _get_or_register_client(server_key: str, spec: dict[str, Any]) -> Oauth2Config:
    """Return the cached registered client for a DCR server, registering once
    on first use.

    The cache is reused only while it still matches this request's callback
    URL: the redirect_uri is baked into the registration, so once the app moves
    to a different public hostname the cached client is dead and must be
    replaced rather than served.
    """
    wanted_redirect = _callback_uri()

    def _usable(row) -> bool:
        # Outside a request there is no callback URL to compare against — serve
        # the cache rather than failing a background caller.
        return not (wanted_redirect and row.redirect_uri and row.redirect_uri != wanted_redirect)

    def _config_from(row) -> Oauth2Config:
        return Oauth2Config(
            authorize_url=row.authorize_url,
            token_url=row.token_url,
            client_id=row.client_id,
            client_secret=row.client_secret,
            scope=row.scope or (spec.get("scope") or None),
        )

    async with SessionLocal() as session:
        row = await get_oauth_client(session, server_key)
    if row is not None and _usable(row):
        return _config_from(row)

    async with _lock_for(f"register|{server_key}"):
        # Re-check under the lock — another request may have just registered.
        async with SessionLocal() as session:
            row = await get_oauth_client(session, server_key)
        if row is not None and _usable(row):
            return _config_from(row)
        if row is not None:
            logger.info(
                "Registered client for %s has redirect_uri %s but this request needs %s; "
                "re-registering",
                server_key, row.redirect_uri, wanted_redirect,
            )
        redirect_uri = wanted_redirect
        if not redirect_uri:
            raise OAuthAuthorizationRequired(server_key, None, "no-base-url")
        cfg = await _discover_and_register(server_key, redirect_uri, spec.get("scope"))
        async with SessionLocal() as session:
            await save_oauth_client(
                session,
                server_key=server_key,
                authorize_url=cfg.authorize_url,
                token_url=cfg.token_url,
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
                scope=cfg.scope,
                redirect_uri=redirect_uri,
            )
        return cfg


async def resolve_config(server_key: str, spec_oauth: dict[str, Any] | None) -> Oauth2Config:
    """Resolve the effective OAuth config for a server: discover+register a
    client for DCR specs, or build directly from manual credentials."""
    spec = spec_oauth or {}
    if spec.get("dcr"):
        return await _get_or_register_client(server_key, spec)
    return Oauth2Config.from_spec(spec)


# Serialize concurrent refreshes for the same (user, server).
_refresh_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _refresh_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[key] = lock
    return lock


# ---------------------------------------------------------------------------
# httpx auth — attaches the user's token, refreshing on expiry / 401
# ---------------------------------------------------------------------------
class PerUserOAuth2Auth(httpx.Auth):
    """Attaches the current user's stored access token to MCP requests.

    Holds the stored ``oauth`` spec (manual creds or a ``{dcr: true}`` marker)
    and resolves the effective client config per request — registering a DCR
    client on first use.
    """

    requires_response_body = False

    def __init__(self, server_key: str, spec_oauth: dict[str, Any]) -> None:
        self.server_key = server_key
        self.spec_oauth = spec_oauth

    async def async_auth_flow(self, request):  # type: ignore[override]
        user_id = current_principal.get()
        if not user_id:
            raise OAuthAuthorizationRequired(self.server_key, None, reason="no-principal")

        config = await resolve_config(self.server_key, self.spec_oauth)

        access_token, token_type = await self._current_access_token(user_id, config)
        if not access_token:
            raise OAuthAuthorizationRequired(self.server_key, config, reason="no-token")

        request.headers["Authorization"] = f"{token_type} {access_token}"
        response = yield request

        if response.status_code == 401:
            # Token may have been revoked server-side; try one forced refresh.
            refreshed, token_type = await self._force_refresh(user_id, config)
            if refreshed:
                request.headers["Authorization"] = f"{token_type} {refreshed}"
                yield request
            else:
                async with SessionLocal() as session:
                    await delete_user_token(session, user_id, self.server_key)
                raise OAuthAuthorizationRequired(self.server_key, config, reason="rejected-401")

    async def _current_access_token(
        self, user_id: str, config: Oauth2Config
    ) -> tuple[str | None, str]:
        async with SessionLocal() as session:
            row = await get_user_token(session, user_id, self.server_key)
            if row is None:
                return None, "Bearer"
            token_type = row.token_type or "Bearer"
            if not _is_expired(row.expires_at):
                return row.access_token, token_type
            refresh_token = row.refresh_token
        if not refresh_token:
            return None, token_type
        new_access, new_type = await self._refresh(user_id, refresh_token, config)
        return new_access, (new_type or token_type)

    async def _force_refresh(
        self, user_id: str, config: Oauth2Config
    ) -> tuple[str | None, str]:
        async with SessionLocal() as session:
            row = await get_user_token(session, user_id, self.server_key)
            refresh_token = row.refresh_token if row else None
        if not refresh_token:
            return None, "Bearer"
        return await self._refresh(user_id, refresh_token, config)

    async def _refresh(
        self, user_id: str, refresh_token: str, config: Oauth2Config
    ) -> tuple[str | None, str]:
        async with _lock_for(f"{user_id}|{self.server_key}"):
            # Re-check under the lock: another request may have just refreshed.
            async with SessionLocal() as session:
                row = await get_user_token(session, user_id, self.server_key)
                if row and not _is_expired(row.expires_at):
                    return row.access_token, (row.token_type or "Bearer")
            try:
                resp = await _post_token(
                    config,
                    {"grant_type": "refresh_token", "refresh_token": refresh_token},
                )
            except Exception:
                logger.warning("Token refresh request failed for %s", self.server_key, exc_info=True)
                return None, "Bearer"
            if resp.status_code >= 400:
                logger.info(
                    "Refresh rejected (%s) for %s: %s",
                    resp.status_code, self.server_key, resp.text[:200],
                )
                # An invalid_client here is about our *registration*, not the
                # user's token. Evict it so the sign-in link the caller is
                # about to show mints a fresh client_id — otherwise that
                # sign-in reuses the dead one and fails identically.
                if _is_invalid_client(resp):
                    await _forget_registered_client(self.server_key, self.spec_oauth)
                return None, "Bearer"
            payload = resp.json()
            access = payload.get("access_token")
            if not access:
                return None, "Bearer"
            async with SessionLocal() as session:
                await upsert_user_token(
                    session,
                    user_id=user_id,
                    server_key=self.server_key,
                    access_token=access,
                    refresh_token=payload.get("refresh_token") or refresh_token,
                    token_type=payload.get("token_type") or "Bearer",
                    scope=payload.get("scope"),
                    expires_at=_expiry_from(payload.get("expires_in")),
                )
            return access, (payload.get("token_type") or "Bearer")


# ---------------------------------------------------------------------------
# Authorization-code flow: begin (build URL) + complete (exchange code)
# ---------------------------------------------------------------------------
async def begin_authorization(
    server_key: str, config: Oauth2Config, *, user_id: str, base_url: str
) -> str:
    """Persist a fresh PKCE/state pair and return the authorize URL."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    redirect_uri = base_url.rstrip("/") + _CALLBACK_PATH

    async with SessionLocal() as session:
        await save_oauth_state(
            session,
            state=state,
            user_id=user_id,
            server_key=server_key,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS),
        )

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if config.scope:
        params["scope"] = config.scope
    sep = "&" if "?" in config.authorize_url else "?"
    return f"{config.authorize_url}{sep}{urlencode(params)}"


async def begin_authorization_for_agent(
    agent_name: str, *, user_id: str, base_url: str, server_key: str | None = None
) -> str | None:
    """Build a fresh authorize URL for one of an agent's oauth2 servers.

    Used by the ``/oauth/login`` redirect endpoint so the (long, PKCE/state)
    authorize URL is generated at click time rather than embedded in chat. When
    ``server_key`` is given, authorize that exact server (so an agent with
    several oauth2 servers signs into the one that needs it, and the token lands
    under the key the chat is polling); otherwise the first oauth2 server.
    Returns None if no matching oauth2 server is found.
    """
    async with SessionLocal() as session:
        rows = await list_agents(session)
    row = next((r for r in rows if r.name == agent_name), None)
    if row is None:
        return None
    for srv in row.mcp_servers:
        if srv.get("auth_mode") == AUTH_MODE_OAUTH2 and isinstance(srv.get("oauth"), dict):
            sk = normalize_mcp_url(str(srv["url"]))
            if server_key and sk != server_key:
                continue
            config = await resolve_config(sk, srv["oauth"])
            return await begin_authorization(
                sk, config, user_id=user_id, base_url=base_url
            )
    return None


async def find_oauth_spec(server_key: str) -> dict[str, Any] | None:
    """The stored ``oauth`` spec for a server_key, or None if no agent uses it.

    Callers need the spec (not just the resolved config) to tell a DCR server
    from a manually configured one.
    """
    async with SessionLocal() as session:
        rows = await list_agents(session)
    for r in rows:
        for s in r.mcp_servers:
            if s.get("auth_mode") != AUTH_MODE_OAUTH2:
                continue
            if normalize_mcp_url(str(s["url"])) == server_key and isinstance(s.get("oauth"), dict):
                return s["oauth"]
    return None


async def find_oauth_config(server_key: str) -> Oauth2Config | None:
    """Resolve the OAuth config for a server_key.

    For DCR servers this returns the already-registered client (cached during
    the authorize step). For manual servers it builds from stored credentials.
    """
    spec = await find_oauth_spec(server_key)
    if spec is not None:
        try:
            return await resolve_config(server_key, spec)
        except Exception:
            logger.warning("Bad oauth config for %s", server_key, exc_info=True)
            return None
    # Agent may have been removed mid-flow; fall back to the registered client.
    async with SessionLocal() as session:
        row = await get_oauth_client(session, server_key)
    if row is not None:
        return Oauth2Config(
            authorize_url=row.authorize_url,
            token_url=row.token_url,
            client_id=row.client_id,
            client_secret=row.client_secret,
            scope=row.scope,
        )
    return None


async def has_valid_token(user_id: str, server_key: str) -> bool:
    """True if the user currently has a non-expired access token for the server.

    Used by the chat endpoint to poll for an interactive sign-in completing, so
    it can resume the specialist automatically instead of asking the user to
    re-send their request.
    """
    async with SessionLocal() as session:
        row = await get_user_token(session, user_id, server_key)
    if row is None:
        return False
    return not _is_expired(row.expires_at)


async def has_usable_token(user_id: str, server_key: str) -> bool:
    """True if the user has a credential the live connection can use WITHOUT an
    interactive sign-in: a non-expired access token, OR a refresh token (which
    :class:`PerUserOAuth2Auth` refreshes transparently on use).

    The sign-in pre-check gates on this — NOT on :func:`has_valid_token` — so a
    returning user with a merely-expired (but refreshable) access token isn't
    needlessly prompted to sign in again.
    """
    async with SessionLocal() as session:
        row = await get_user_token(session, user_id, server_key)
    if row is None:
        return False
    return (not _is_expired(row.expires_at)) or bool(row.refresh_token)


async def token_status(user_id: str, server_key: str) -> tuple[str, datetime | None]:
    """How usable the user's stored credential for a server is, and until when.

    Returns one of:

    ``none``
        No token is stored at all — the user has never signed in.
    ``valid``
        The access token has not expired; the connection works right now.
    ``refreshable``
        The access token has expired, but a refresh token is stored, so
        :class:`PerUserOAuth2Auth` renews it without an interactive sign-in.
    ``expired``
        The access token has expired and there is nothing to refresh with —
        the user must sign in again.

    The admin UI shows this so an expiring or already-dead credential is
    visible *before* a scheduled run fails on it. ``has_usable_token`` answers
    the yes/no gate; this answers "and how healthy is it".

    The second element is the access token's expiry (``None`` when the server
    issued no ``expires_in``, which means it does not expire on its own).
    """
    async with SessionLocal() as session:
        row = await get_user_token(session, user_id, server_key)
    if row is None:
        return "none", None
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if not _is_expired(row.expires_at):
        return "valid", expires_at
    return ("refreshable" if row.refresh_token else "expired"), expires_at


async def complete_authorization(*, code: str, state: str, principal: str | None) -> str:
    """Exchange an authorization code for tokens and persist them.

    Returns the server_key that was authorized (for display). Raises
    ValueError on an invalid/expired state, principal mismatch, or token
    endpoint failure.
    """
    async with SessionLocal() as session:
        flow = await pop_oauth_state(session, state)
    if flow is None:
        raise ValueError("Authorization link is invalid or has expired. Please retry.")
    # Fail closed: bind the token only when we can confirm the caller completing
    # the callback is the same user who started the flow. A missing principal
    # (e.g. an invalid/forged token that failed validation) is rejected, not
    # treated as "skip the check".
    if not principal or flow.user_id != principal:
        raise ValueError("Authorization does not belong to the current user.")

    config = await find_oauth_config(flow.server_key)
    if config is None:
        raise ValueError("This MCP server is no longer configured for OAuth2.")

    try:
        resp = await _post_token(
            config,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": flow.redirect_uri,
                "code_verifier": flow.code_verifier,
            },
        )
    except Exception as e:
        raise ValueError(f"Token endpoint request failed: {e}") from e

    if resp.status_code >= 400:
        # invalid_client means our registration is dead, not that the user did
        # anything wrong. Evict it so retrying the sign-in registers afresh —
        # this is the only place the failure surfaces for a user who has no
        # refresh token to fail on first.
        if _is_invalid_client(resp) and await _forget_registered_client(
            flow.server_key, await find_oauth_spec(flow.server_key)
        ):
            raise ValueError(
                "This app's registration with the target was no longer accepted. "
                "It has been renewed — please retry the sign-in."
            )
        raise ValueError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")

    payload = resp.json()
    access = payload.get("access_token")
    if not access:
        raise ValueError("Token response did not include an access_token.")

    async with SessionLocal() as session:
        await upsert_user_token(
            session,
            user_id=flow.user_id,
            server_key=flow.server_key,
            access_token=access,
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type") or "Bearer",
            scope=payload.get("scope"),
            expires_at=_expiry_from(payload.get("expires_in")),
        )
    return flow.server_key


# ---------------------------------------------------------------------------
# Exception-tree search (the auth error is usually wrapped by the MCP client)
# ---------------------------------------------------------------------------
def find_oauth_required(exc: BaseException) -> OAuthAuthorizationRequired | None:
    seen: set[int] = set()

    def walk(e: BaseException | None) -> OAuthAuthorizationRequired | None:
        if e is None or id(e) in seen:
            return None
        seen.add(id(e))
        if isinstance(e, OAuthAuthorizationRequired):
            return e
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                found = walk(sub)
                if found:
                    return found
        return walk(e.__cause__) or walk(e.__context__)

    return walk(exc)
