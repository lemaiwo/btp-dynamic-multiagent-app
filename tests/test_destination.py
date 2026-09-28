"""The destination resolver's per-user mode and the ``DestinationAuth`` flow.

Covered: the app-level resolve is unchanged; a user token goes to the
destination service as ``X-user-token`` and is cached per principal, apart
from the app-level entry, bounded and with the same expiry; ``auth_type`` is
reported; ``DestinationAuth`` rewrites the placeholder base onto the
destination's URL (prefix included), sets the destination's headers, retries
once on a 401 after invalidating the right entry, refuses to carry the
credential to a foreign host, and raises a clear, non-sign-in error when a
per-user destination is used with no JWT in context.

No network: the destination service and the target are both MockTransports.

Run:  python -m pytest tests/test_destination.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents import destination as dest_mod  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination import (  # noqa: E402
    USER_TOKEN_HEADER,
    Destination,
    DestinationError,
    DestinationResolver,
    DestinationServiceConfig,
)
from agents.destination_auth import (  # noqa: E402
    PLACEHOLDER_BASE,
    DestinationAuth,
    DestinationUserRequired,
    destination_http_client,
)

CONFIG = DestinationServiceConfig(
    client_id="sb-dest",
    client_secret="shh",
    token_url="https://uaa.example/oauth/token",
    api_url="https://destination.example",
)


class DestinationService:
    """A mock destination service that hands back a token per user."""

    def __init__(self, *, auth_type: str = "OAuth2UserTokenExchange", expires_in: int = 3600):
        self.calls: list[httpx.Request] = []
        self.auth_type = auth_type
        self.expires_in = expires_in
        self.payload_override: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 3600})
        if self.payload_override is not None:
            return httpx.Response(200, json=self.payload_override)
        user = request.headers.get(USER_TOKEN_HEADER)
        who = f"user:{user}" if user else "app"
        return httpx.Response(200, json={
            "destinationConfiguration": {
                "Name": "GRAPH",
                "URL": "https://graph.microsoft.com",
                "Authentication": self.auth_type,
                "URL.headers.X-Extra": "yes",
            },
            "authTokens": [{
                "type": "Bearer", "value": f"tok-for-{who}", "expires_in": str(self.expires_in),
                "http_header": {"key": "Authorization", "value": f"Bearer tok-for-{who}"},
            }],
        })

    def resolver(self, name: str = "GRAPH", **kw) -> DestinationResolver:
        return DestinationResolver(name, CONFIG, transport=httpx.MockTransport(self.handler), **kw)

    def destination_calls(self) -> list[httpx.Request]:
        return [c for c in self.calls if "/destinations/" in c.url.path]


# --- resolver ---------------------------------------------------------------

async def test_app_level_resolve_sends_no_user_token_and_reports_auth_type():
    svc = DestinationService(auth_type="OAuth2ClientCredentials")
    resolved = await svc.resolver().resolve()
    assert resolved.headers["Authorization"] == "Bearer tok-for-app"
    assert resolved.headers["X-Extra"] == "yes"
    assert resolved.auth_type == "OAuth2ClientCredentials"
    assert resolved.per_user is False
    (call,) = svc.destination_calls()
    assert USER_TOKEN_HEADER not in call.headers


async def test_user_token_is_forwarded_as_x_user_token():
    svc = DestinationService()
    resolver = svc.resolver()
    resolved = await resolver.resolve(user_token="jwt-ann", principal="ann")
    (call,) = svc.destination_calls()
    assert call.headers[USER_TOKEN_HEADER] == "jwt-ann"
    assert resolved.headers["Authorization"] == "Bearer tok-for-user:jwt-ann"
    assert resolved.per_user is True
    assert resolved.auth_type == "OAuth2UserTokenExchange"


async def test_per_user_results_are_cached_apart_from_the_app_level_one():
    svc = DestinationService()
    resolver = svc.resolver()
    ann = await resolver.resolve(user_token="jwt-ann", principal="ann")
    bob = await resolver.resolve(user_token="jwt-bob", principal="bob")
    app = await resolver.resolve()
    tokens = {d.headers["Authorization"] for d in (ann, bob, app)}
    assert len(tokens) == 3, "each user and the app got a token of their own"
    assert len(svc.destination_calls()) == 3
    # Cache hits: no further calls, and each principal gets their own entry.
    assert (await resolver.resolve(user_token="jwt-ann", principal="ann")) is ann
    assert (await resolver.resolve()) is app
    assert len(svc.destination_calls()) == 3
    assert resolver.cached_principals == ["bob", "ann"]  # ann moved to the end on hit


async def test_invalidating_one_principal_leaves_the_others_alone():
    svc = DestinationService()
    resolver = svc.resolver()
    await resolver.resolve(user_token="jwt-ann", principal="ann")
    await resolver.resolve(user_token="jwt-bob", principal="bob")
    await resolver.resolve()
    resolver.invalidate("ann")
    assert resolver.cached_principals == ["bob"]
    await resolver.resolve(user_token="jwt-bob", principal="bob")
    await resolver.resolve()
    assert len(svc.destination_calls()) == 3, "bob and the app entry were still cached"
    await resolver.resolve(user_token="jwt-ann", principal="ann")
    assert len(svc.destination_calls()) == 4
    resolver.invalidate()
    await resolver.resolve()
    assert len(svc.destination_calls()) == 5


async def test_per_user_cache_is_bounded_lru(monkeypatch):
    monkeypatch.setattr(dest_mod, "PER_USER_CACHE_MAX", 2)
    svc = DestinationService()
    resolver = svc.resolver()
    for who in ("a", "b", "c"):
        await resolver.resolve(user_token=f"jwt-{who}", principal=who)
    assert resolver.cached_principals == ["b", "c"], "the least recently used entry was evicted"
    # A hit moves an entry to the back, so it survives the next eviction.
    await resolver.resolve(user_token="jwt-b", principal="b")
    await resolver.resolve(user_token="jwt-d", principal="d")
    assert resolver.cached_principals == ["b", "d"]


async def test_per_user_entries_expire_like_the_app_level_one():
    svc = DestinationService(expires_in=1)  # below the skew: expires immediately
    resolver = svc.resolver()
    await resolver.resolve(user_token="jwt-ann", principal="ann")
    time.sleep(1.1)
    await resolver.resolve(user_token="jwt-ann", principal="ann")
    assert len(svc.destination_calls()) == 2


async def test_a_token_without_a_principal_is_keyed_by_digest_not_shared():
    svc = DestinationService()
    resolver = svc.resolver()
    await resolver.resolve(user_token="jwt-one")
    await resolver.resolve(user_token="jwt-two")
    assert len(svc.destination_calls()) == 2
    keys = resolver.cached_principals
    assert len(keys) == 2 and all(k.startswith("token:") for k in keys)
    assert "jwt-one" not in "".join(keys), "the raw token is never a cache key"


async def test_user_token_error_from_the_service_names_the_user_case():
    svc = DestinationService()
    svc.payload_override = {
        "destinationConfiguration": {"URL": "https://graph.microsoft.com"},
        "authTokens": [{"type": "Bearer", "error": "invalid_grant: no trust"}],
    }
    with pytest.raises(DestinationError, match="for the signed-in user.*invalid_grant"):
        await svc.resolver().resolve(user_token="jwt-ann", principal="ann")


async def test_require_credential_false_accepts_a_bare_url():
    svc = DestinationService()
    svc.payload_override = {
        "destinationConfiguration": {
            "URL": "https://services.nvd.nist.gov", "Authentication": "NoAuthentication",
            "URL.headers.apiKey": "k",
        },
    }
    with pytest.raises(DestinationError, match="no authentication token"):
        await svc.resolver().resolve()
    resolved = await svc.resolver(require_credential=False).resolve()
    assert resolved.headers == {"apiKey": "k"}
    assert resolved.auth_type == "NoAuthentication"


# --- DestinationAuth --------------------------------------------------------

class FakeResolver:
    """Records how it was asked; hands back a fixed destination."""

    def __init__(self, url: str = "https://graph.microsoft.com", headers: dict | None = None):
        self.name = "FAKE"
        self.url = url
        self.headers = headers or {"Authorization": "Bearer t1"}
        self.calls: list[tuple[bool, str | None, str | None]] = []
        self.invalidated: list[str | None] = []
        self.serial = 0

    async def resolve(self, *, force: bool = False, user_token=None, principal=None) -> Destination:
        self.calls.append((force, user_token, principal))
        self.serial += 1
        headers = dict(self.headers)
        if force:
            headers["Authorization"] = f"Bearer t{self.serial}"
        return Destination(
            url=self.url, headers=headers, expires_at=time.monotonic() + 60,
            auth_type="OAuth2UserTokenExchange" if user_token else "OAuth2ClientCredentials",
            per_user=bool(user_token),
        )

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidated.append(principal)


class Target:
    def __init__(self, statuses: list[int] | None = None):
        self.requests: list[httpx.Request] = []
        # Header snapshots per attempt: the retry mutates the same Request
        # object, so the object alone cannot show what the first attempt sent.
        self.auth_headers: list[str] = []
        self.statuses = list(statuses or [])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auth_headers.append(request.headers.get("Authorization", ""))
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={"ok": status == 200})


def _client(resolver, target: Target, **kw) -> httpx.AsyncClient:
    return destination_http_client(resolver, transport=httpx.MockTransport(target.handler), **kw)


async def test_placeholder_requests_are_rewritten_onto_the_destination():
    resolver = FakeResolver(url="https://proxy.example/graph")
    target = Target()
    async with _client(resolver, target, expected_hosts=("graph.microsoft.com",)) as http:
        r = await http.get("/v1.0/me/messages", params={"$top": 5})
    assert r.status_code == 200
    (sent,) = target.requests
    assert str(sent.url) == "https://proxy.example/graph/v1.0/me/messages?%24top=5"
    assert sent.headers["Host"] == "proxy.example"
    assert sent.headers["Authorization"] == "Bearer t1"
    assert resolver.calls == [(False, None, None)]


async def test_static_headers_and_a_host_without_prefix():
    resolver = FakeResolver(headers={"Authorization": "Bearer t1", "X-Extra": "1"})
    target = Target()
    async with _client(resolver, target) as http:
        await http.post("/v1.0/me/sendMail", json={"a": 1})
    (sent,) = target.requests
    assert str(sent.url) == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert sent.headers["X-Extra"] == "1"
    assert json.loads(sent.content) == {"a": 1}


async def test_a_401_invalidates_and_retries_exactly_once():
    resolver = FakeResolver()
    target = Target(statuses=[401, 200])
    async with _client(resolver, target) as http:
        r = await http.get("/v1.0/me")
    assert r.status_code == 200
    assert target.auth_headers == ["Bearer t1", "Bearer t2"]
    assert resolver.invalidated == [None], "app-level entry dropped"
    assert resolver.calls[-1][0] is True, "the retry forced a re-resolve"

    resolver = FakeResolver()
    target = Target(statuses=[401, 401, 200])
    async with _client(resolver, target) as http:
        r = await http.get("/v1.0/me")
    assert r.status_code == 401, "a second 401 is returned, not retried again"
    assert len(target.requests) == 2


async def test_user_context_resolves_with_the_bound_jwt_and_principal():
    resolver = FakeResolver()
    target = Target(statuses=[401, 200])
    jwt_marker = current_jwt.set("jwt-ann")
    principal_marker = current_principal.set("ann")
    try:
        async with _client(resolver, target, user_context=True) as http:
            await http.get("/v1.0/me")
    finally:
        current_jwt.reset(jwt_marker)
        current_principal.reset(principal_marker)
    assert resolver.calls[0] == (False, "jwt-ann", "ann")
    assert resolver.calls[1] == (True, "jwt-ann", "ann")
    assert resolver.invalidated == ["ann"], "only this user's entry was dropped"


async def test_user_context_without_a_jwt_is_a_clear_error_not_a_sign_in():
    from agents.oauth2 import OAuthAuthorizationRequired

    resolver = FakeResolver()
    target = Target()
    assert current_jwt.get() is None
    async with _client(resolver, target, user_context=True, server_key="builtin:outlook") as http:
        with pytest.raises(DestinationUserRequired) as info:
            await http.get("/v1.0/me")
    assert not isinstance(info.value, OAuthAuthorizationRequired)
    assert isinstance(info.value, DestinationError)
    text = str(info.value)
    assert "builtin:outlook" in text and "'FAKE'" in text
    assert "no signed-in user" in text and "scheduled" in text
    assert target.requests == [], "nothing was sent"
    assert resolver.calls == [], "the destination was not even resolved"


async def test_the_credential_never_goes_to_a_foreign_host():
    resolver = FakeResolver(url="https://graph.microsoft.com")
    target = Target()
    async with _client(resolver, target, expected_hosts=("graph.microsoft.com",)) as http:
        # A Graph @odata.nextLink is absolute and back to Graph: allowed.
        await http.get("https://graph.microsoft.com/v1.0/teams/x/channels?$skiptoken=1")
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get("https://evil.example/collect")
    assert len(target.requests) == 1
    assert target.requests[0].headers["Authorization"] == "Bearer t1"


async def test_a_plain_http_destination_is_refused():
    resolver = FakeResolver(url="http://graph.microsoft.com")
    target = Target()
    async with _client(resolver, target) as http:
        with pytest.raises(DestinationError, match="must use https"):
            await http.get("/v1.0/me")
    assert target.requests == []


def test_placeholder_base_never_resolves():
    # `.invalid` is reserved by RFC 2606: a request that escapes the rewrite
    # fails at DNS rather than reaching anything.
    assert PLACEHOLDER_BASE.endswith(".invalid")
    auth = DestinationAuth(FakeResolver(), server_key="x")
    with pytest.raises(RuntimeError):
        next(auth.sync_auth_flow(httpx.Request("GET", PLACEHOLDER_BASE)))
