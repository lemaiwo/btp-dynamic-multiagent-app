"""Request-level auth hardening (review findings C1, H2, H3, M1-M4, M6).

Drives the real FastAPI app over an ASGI transport with ``ON_CF`` forced on
and a fake validator installed, so the middleware behaves as it does on Cloud
Foundry without an XSUAA. No lifespan is run: everything under test happens
in ``JWTBindingMiddleware`` or in DB-free admin endpoints.

Run:  pytest tests/test_auth_middleware.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./tests/_test_auth_mw.db")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ.pop("AUTH_REQUIRED", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ["PUBLIC_BASE_URL"] = ""
os.environ["A2A_PUBLIC_URL"] = ""

# Stub the pieces that would reach AI Core / MCP if a registry build ran.
import agents.shared as shared  # noqa: E402


class _FakeModel:
    model_name = "fake"


shared.get_model = lambda name=None: _FakeModel()  # type: ignore[assignment]
shared.create_mcp_server = lambda name, base_url, *a, **k: object()  # type: ignore[assignment]

from httpx import ASGITransport, AsyncClient  # noqa: E402

import agents.auth as auth  # noqa: E402
import app as app_module  # noqa: E402

XSAPPNAME = "pydantic-agent-test!t1"

# token string -> claims the fake validator hands back
TOKENS = {
    "user-tok": {"sub": "u-1", "user_uuid": "u-1", "scope": [f"{XSAPPNAME}.user"]},
    "admin-tok": {"sub": "u-2", "user_uuid": "u-2", "email": "admin@example.com",
                  "scope": [f"{XSAPPNAME}.user", f"{XSAPPNAME}.admin"]},
    "a2a-tok": {"sub": "sb-joule!t1", "scope": [f"{XSAPPNAME}.a2a"]},
    "no-principal-tok": {"scope": [f"{XSAPPNAME}.user"]},
}


class _FakeValidator:
    xsappname = XSAPPNAME
    client_id = f"sb-{XSAPPNAME}"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def validate(self, token: str) -> dict:
        self.calls.append(token)
        claims = TOKENS.get(token)
        if claims is None:
            raise HTTPException(status_code=401, detail="Invalid JWT: fake")
        return dict(claims)

    def has_scope(self, payload: dict, scope: str) -> bool:
        scopes = payload.get("scope") or []
        return f"{self.xsappname}.{scope}" in scopes or scope in scopes


@pytest.fixture(autouse=True)
def _no_public_url(monkeypatch):
    """Start every test without a configured public URL.

    pytest imports every tests/test_*.py at collection time, and some of the
    script-style modules set PUBLIC_BASE_URL in os.environ as they import, so
    the module-level value here is not what a test would otherwise see.
    """
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("A2A_PUBLIC_URL", "")


@pytest.fixture
def cf():
    """Run the app as if on CF with an XSUAA binding (fake validator)."""
    fake = _FakeValidator()
    saved = (auth._validator, auth._validator_checked, app_module.ON_CF,
             app_module._warned_no_public_url)
    auth._validator, auth._validator_checked = fake, True  # type: ignore[assignment]
    app_module.ON_CF = True
    app_module._warned_no_public_url = False
    try:
        yield fake
    finally:
        auth._validator, auth._validator_checked, app_module.ON_CF, \
            app_module._warned_no_public_url = saved


@pytest.fixture
def local():
    """Run the app as locally: no validator, ON_CF off."""
    saved = (auth._validator, auth._validator_checked, app_module.ON_CF)
    auth._validator, auth._validator_checked = None, True
    app_module.ON_CF = False
    try:
        yield
    finally:
        auth._validator, auth._validator_checked, app_module.ON_CF = saved


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test")


def _bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


RPC_UNKNOWN = {"jsonrpc": "2.0", "id": 1, "method": "no/such", "params": {}}

# CSRF probe: an admin route whose request validation fails (422) before the
# handler runs, so no DB or registry is needed. 422 means the request got past
# the middleware; 403 means the Origin check refused it.
PROBE = "/admin/api/import"
PROBE_BODY = [1, 2, 3]  # ImportPayload wants an object
PASSED = 422


# ---------------------------------------------------------------------------
# C1 / M1: the middleware validates once and rejects what does not validate
# ---------------------------------------------------------------------------
async def test_cf_missing_token_on_api_path_is_401(cf):
    async with _client() as c:
        r = await c.post("/a2a", json=RPC_UNKNOWN)
    assert r.status_code == 401
    assert "Missing bearer token" in r.json()["detail"]


async def test_cf_garbage_token_is_401_not_forwarded(cf):
    async with _client() as c:
        r = await c.post("/a2a", json=RPC_UNKNOWN, headers=_bearer("garbage"))
    assert r.status_code == 401
    assert "Invalid bearer token" in r.json()["detail"]
    assert cf.calls == ["garbage"]


async def test_cf_valid_token_is_validated_exactly_once(cf):
    async with _client() as c:
        r = await c.post("/a2a", json=RPC_UNKNOWN, headers=_bearer("a2a-tok"))
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601  # reached the JSON-RPC handler
    # Middleware validated; require_a2a reused current_claims instead of
    # decoding (and possibly fetching the JWKS) a second time.
    assert cf.calls == ["a2a-tok"]


async def test_cf_token_without_principal_is_401(cf):
    async with _client() as c:
        r = await c.post("/a2a", json=RPC_UNKNOWN, headers=_bearer("no-principal-tok"))
    assert r.status_code == 401
    assert "no user identity" in r.json()["detail"]


async def test_cf_public_paths_need_no_token(cf):
    async with _client() as c:
        r = await c.get("/healthz")
        assert r.status_code == 200
        assert cf.calls == []
        # A stale token on an anonymous path is ignored, not fatal.
        r = await c.get("/healthz", headers=_bearer("expired"))
        assert r.status_code == 200


async def test_cf_admin_without_token_falls_to_require_admin(cf):
    async with _client() as c:
        r = await c.get("/admin/api/whoami")
    assert r.status_code == 401
    assert r.json()["detail"] == "Missing bearer token"


async def test_cf_admin_with_bad_token_is_401(cf):
    async with _client() as c:
        r = await c.get("/admin/api/whoami", headers=_bearer("nope"))
    assert r.status_code == 401
    assert cf.calls == ["nope"]


async def test_cf_admin_with_user_token_is_403(cf):
    async with _client() as c:
        r = await c.get("/admin/api/whoami", headers=_bearer("user-tok"))
    assert r.status_code == 403
    assert cf.calls == ["user-tok"]


async def test_cf_admin_with_admin_token_reuses_claims(cf):
    async with _client() as c:
        r = await c.get("/admin/api/whoami", headers=_bearer("admin-tok"))
    assert r.status_code == 200
    assert r.json() == {"principal": "u-2", "label": "admin@example.com"}
    assert cf.calls == ["admin-tok"]


# ---------------------------------------------------------------------------
# H2: /a2a needs the a2a scope on the backend
# ---------------------------------------------------------------------------
async def test_cf_a2a_requires_a2a_scope(cf):
    async with _client() as c:
        r = await c.post("/a2a", json=RPC_UNKNOWN, headers=_bearer("user-tok"))
    assert r.status_code == 403
    assert r.json()["detail"] == "A2A scope required"


def test_require_a2a_is_open_locally(local):
    from fastapi import Request

    req = Request({"type": "http", "headers": [], "method": "POST", "path": "/a2a"})
    assert auth.require_a2a(req)["scope"] == ["a2a"]


# ---------------------------------------------------------------------------
# H2: contexts and tasks are keyed by principal
# ---------------------------------------------------------------------------
async def test_a2a_store_isolates_principals():
    from agents.a2a import _ConversationStore

    s = _ConversationStore()
    await s.set_history("alice", "ctx-1", ["m1", "m2"])
    assert await s.get_history("alice", "ctx-1") == ["m1", "m2"]
    assert await s.get_history("bob", "ctx-1") == []
    # Bob writing under the same id does not clobber Alice.
    await s.set_history("bob", "ctx-1", ["b1"])
    assert await s.get_history("alice", "ctx-1") == ["m1", "m2"]

    task = {"id": "t-1", "status": {"state": "working"}}
    await s.save_task("alice", task)
    assert await s.get_task("alice", "t-1") is task
    assert await s.get_task("bob", "t-1") is None
    assert await s.cancel_task("bob", "t-1") is None
    assert task["status"]["state"] == "working"
    assert (await s.cancel_task("alice", "t-1"))["status"]["state"] == "canceled"


# ---------------------------------------------------------------------------
# Local behaviour stays open
# ---------------------------------------------------------------------------
async def test_local_garbage_token_still_passes(local):
    async with _client() as c:
        r = await c.post("/a2a", json=RPC_UNKNOWN, headers=_bearer("not-a-jwt"))
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601


async def test_local_no_token_still_passes(local):
    async with _client() as c:
        r = await c.get("/admin/api/whoami")
    assert r.status_code == 200
    assert r.json()["principal"] == "local-dev"


# ---------------------------------------------------------------------------
# M4: Origin check on state-changing /admin requests
# ---------------------------------------------------------------------------
async def test_csrf_cross_site_origin_on_admin_post_is_403(local):
    async with _client() as c:
        r = await c.post(PROBE, json=PROBE_BODY, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "Cross-site" in r.json()["detail"]


async def test_csrf_same_host_origin_passes(local):
    async with _client() as c:
        r = await c.post(PROBE, json=PROBE_BODY, headers={"Origin": "http://test"})
    assert r.status_code == PASSED


async def test_csrf_origin_matching_forwarded_host_passes(local):
    async with _client() as c:
        r = await c.post(PROBE, json=PROBE_BODY, headers={
            "Origin": "https://router.example.com",
            "X-Forwarded-Host": "router.example.com",
            "X-Forwarded-Proto": "https",
        })
    assert r.status_code == PASSED


async def test_csrf_origin_matching_public_base_url_passes(cf, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com/")
    async with _client() as c:
        r = await c.post(PROBE, json=PROBE_BODY, headers={
            "Origin": "https://app.example.com", **_bearer("admin-tok")})
        assert r.status_code == PASSED
        r = await c.post(PROBE, json=PROBE_BODY, headers={
            "Origin": "https://evil.example", **_bearer("admin-tok")})
        assert r.status_code == 403


async def test_csrf_sec_fetch_site_same_origin_passes(local):
    async with _client() as c:
        for site in ("same-origin", "none"):
            r = await c.post(PROBE, json=PROBE_BODY, headers={
                "Origin": "https://evil.example", "Sec-Fetch-Site": site})
            assert r.status_code == PASSED, site


async def test_csrf_cross_site_fetch_site_with_bad_origin_is_403(local):
    async with _client() as c:
        r = await c.post(PROBE, json=PROBE_BODY, headers={
            "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


async def test_csrf_no_origin_is_unaffected(local):
    async with _client() as c:
        r = await c.post(PROBE, json=PROBE_BODY)
        assert r.status_code == PASSED
        r = await c.delete("/admin/api/agents/not-an-id")  # 422: agent_id is int
        assert r.status_code == PASSED


async def test_csrf_get_and_non_admin_paths_are_unaffected(local):
    async with _client() as c:
        r = await c.get("/admin/api/whoami", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200
        r = await c.delete("/admin/api/agents/not-an-id", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403  # DELETE is state-changing too
        # A2A is not under /admin: the Origin check does not apply.
        r = await c.post("/a2a", json=RPC_UNKNOWN, headers={"Origin": "https://evil.example"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# M3: no X-Forwarded-Host derived base URL on CF
# ---------------------------------------------------------------------------
async def test_cf_without_public_url_ignores_forwarded_host(cf):
    async with _client() as c:
        r = await c.get("/admin/api/config", headers={
            "X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https",
            **_bearer("admin-tok")})
    assert r.status_code == 200
    assert r.json()["public_base_url"] == ""


async def test_cf_with_public_url_uses_it(cf, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    async with _client() as c:
        r = await c.get("/admin/api/config", headers={
            "X-Forwarded-Host": "attacker.example", **_bearer("admin-tok")})
    assert r.json()["public_base_url"] == "https://app.example.com"


async def test_local_keeps_forwarded_host_fallback(local):
    async with _client() as c:
        r = await c.get("/admin/api/config", headers={
            "X-Forwarded-Host": "router.example.com", "X-Forwarded-Proto": "https"})
    assert r.json()["public_base_url"] == "https://router.example.com"


def test_a2a_card_url_ignores_forwarded_host_on_cf(monkeypatch):
    from starlette.requests import Request

    from agents.a2a import _base_url

    scope = {
        "type": "http", "method": "GET", "path": "/.well-known/agent-card.json",
        "scheme": "https", "server": ("backend.internal", 443), "root_path": "",
        "query_string": b"", "headers": [
            (b"host", b"backend.internal"),
            (b"x-forwarded-host", b"attacker.example"),
            (b"x-forwarded-proto", b"https"),
        ],
    }
    monkeypatch.delenv("A2A_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("VCAP_APPLICATION", raising=False)
    assert _base_url(Request(scope)) == "https://attacker.example"  # local dev
    monkeypatch.setenv("VCAP_APPLICATION", "{}")
    assert _base_url(Request(scope)) == "https://backend.internal"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    assert _base_url(Request(scope)) == "https://app.example.com"


# ---------------------------------------------------------------------------
# M2: AUTH_REQUIRED fails closed
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_validator_state():
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, False
    try:
        yield
    finally:
        auth._validator, auth._validator_checked = saved


def test_auth_required_defaults(monkeypatch):
    monkeypatch.delenv("AUTH_REQUIRED", raising=False)
    monkeypatch.delenv("VCAP_APPLICATION", raising=False)
    assert auth.auth_required() is False
    monkeypatch.setenv("VCAP_APPLICATION", "{}")
    assert auth.auth_required() is True
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    assert auth.auth_required() is False
    monkeypatch.delenv("VCAP_APPLICATION")
    monkeypatch.setenv("AUTH_REQUIRED", "TRUE")
    assert auth.auth_required() is True


def test_get_validator_raises_when_required_and_unbound(fresh_validator_state, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    with pytest.raises(auth.AuthConfigurationError, match="AUTH_REQUIRED"):
        auth.get_validator()
    # Not cached as "open": a later call still refuses.
    with pytest.raises(auth.AuthConfigurationError):
        auth.get_validator()
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    assert auth.get_validator() is None


async def test_lifespan_refuses_to_start_when_required_and_unbound(
    fresh_validator_state, monkeypatch
):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    with pytest.raises(auth.AuthConfigurationError):
        async with app_module.app.router.lifespan_context(app_module.app):
            pass  # pragma: no cover - startup must raise first


# ---------------------------------------------------------------------------
# H3: OAuth result pages escape what they interpolate
# ---------------------------------------------------------------------------
def test_oauth_page_escapes_title_and_body():
    from agents.oauth_routes import _page

    r = _page("<b>t</b>", "<script>alert('x')</script> & \"q\"", ok=False)
    body = r.body.decode()
    assert "<script>alert" not in body
    assert "<b>t</b>" not in body
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; &quot;q&quot;" in body
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# M6: sign-in requirements propagate out of the resilient tool wrapper
# ---------------------------------------------------------------------------
async def test_resilient_tool_call_reraises_oauth_required():
    from agents.oauth2 import OAuthAuthorizationRequired
    from agents.shared import _resilient_tool_call

    class Ctx:
        retry = 0
        max_retries = 3

    async def needs_signin(name, args, metadata):
        raise OAuthAuthorizationRequired("https://mcp.example/mcp", None, reason="no-token")

    async def plain_failure(name, args, metadata):
        raise RuntimeError("boom")

    with pytest.raises(OAuthAuthorizationRequired):
        await _resilient_tool_call(Ctx(), needs_signin, "t", {})
    out = await _resilient_tool_call(Ctx(), plain_failure, "t", {})
    assert "RuntimeError: boom" in out


# ---------------------------------------------------------------------------
# Low: refresh-lock table is bounded by concurrency
# ---------------------------------------------------------------------------
async def test_refresh_locks_are_released_when_idle():
    from agents import oauth2

    order: list[str] = []

    async def worker(tag: str, hold: float):
        async with oauth2._lock_for("u|s"):
            order.append(f"{tag}-in")
            assert "u|s" in oauth2._refresh_locks
            await asyncio.sleep(hold)
            order.append(f"{tag}-out")

    await asyncio.gather(worker("a", 0.02), worker("b", 0.0))
    # Serialized: b entered only after a left.
    assert order == ["a-in", "a-out", "b-in", "b-out"]
    assert "u|s" not in oauth2._refresh_locks
    assert oauth2._refresh_locks == {}
