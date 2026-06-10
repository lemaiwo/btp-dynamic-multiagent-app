"""Focused tests for the per-user OAuth2 MCP auth mode (auth_mode="oauth2").

Exercises the DB layer (config storage, secret redaction + preservation),
the admin payload validation, and the oauth2 module (PKCE/state, the
authorization-code exchange with a stubbed token endpoint, the httpx auth
that attaches/refuses tokens, and the exception-tree search).

No SAP AI Core, MCP server, or network is touched.

Run:  python tests/test_oauth2.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_oauth2.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import agents.oauth2 as oauth2  # noqa: E402
from agents.auth import current_base_url, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    get_user_token,
    init_db,
    list_agents,
    upsert_agent,
)
from agents.oauth2 import (  # noqa: E402
    OAuthAuthorizationRequired,
    Oauth2Config,
    PerUserOAuth2Auth,
    begin_authorization,
    complete_authorization,
    find_oauth_required,
    normalize_mcp_url,
)

import httpx  # noqa: E402

FAILED = 0
PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


# A stub token endpoint: records the last form posted and returns canned tokens.
class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


_token_calls: list[dict] = []
_next_token_payload: dict = {}
_next_token_status: int = 200


async def _fake_post_token(config, data):
    _token_calls.append(dict(data))
    return _FakeResp(_next_token_status, _next_token_payload)


OAUTH = {
    "client_id": "sb-arc1!t1",
    "client_secret": "super-secret",
    "uaa_url": "https://tenant.authentication.eu20.hana.ondemand.com",
    "scope": "openid",
}
ARC1_URL = "https://arc1.cfapps.eu20-001.hana.ondemand.com"
SERVER_KEY = normalize_mcp_url(ARC1_URL)
USER = "user-abc-123"


async def main() -> None:
    _orig_post_token = oauth2._post_token
    oauth2._post_token = _fake_post_token  # type: ignore[assignment]
    await init_db()

    # --- DB: store an oauth2 agent ----------------------------------------
    print("\n== DB storage + redaction ==")
    async with SessionLocal() as s:
        await upsert_agent(
            s,
            name="arc1",
            description="ARC-1 ABAP system",
            instructions="You are the ARC-1 specialist.",
            mcp_servers=[{"url": ARC1_URL, "auth_mode": "oauth2", "oauth": OAUTH}],
        )
    async with SessionLocal() as s:
        rows = await list_agents(s)
        row = next(r for r in rows if r.name == "arc1")
        servers = row.mcp_servers
        check("server stored as oauth2", servers[0]["auth_mode"] == "oauth2")
        check("oauth client_id stored", servers[0]["oauth"]["client_id"] == OAUTH["client_id"])
        check("oauth secret stored (internal)", servers[0]["oauth"]["client_secret"] == "super-secret")
        d = row.to_dict()
        oauth_d = d["mcp_servers"][0]["oauth"]
        check("to_dict redacts secret", oauth_d["client_secret"] == "")
        check("to_dict has_client_secret flag", oauth_d["has_client_secret"] is True)

    # --- DB: secret preserved when re-saved blank -------------------------
    print("\n== Secret preservation on edit ==")
    async with SessionLocal() as s:
        blanked = dict(OAUTH)
        blanked["client_secret"] = ""
        await upsert_agent(
            s,
            name="arc1",
            description="ARC-1 edited",
            instructions="edited",
            mcp_servers=[{"url": ARC1_URL, "auth_mode": "oauth2", "oauth": blanked}],
        )
    async with SessionLocal() as s:
        rows = await list_agents(s)
        row = next(r for r in rows if r.name == "arc1")
        check(
            "secret preserved when re-saved blank",
            row.mcp_servers[0]["oauth"]["client_secret"] == "super-secret",
        )

    # --- admin payload validation -----------------------------------------
    print("\n== Admin payload validation ==")
    from agents.admin import McpServerPayload

    ok = McpServerPayload(url=ARC1_URL, auth_mode="oauth2", oauth=OAUTH)
    check("valid oauth2 payload accepted", ok.auth_mode == "oauth2")
    try:
        McpServerPayload(url=ARC1_URL, auth_mode="oauth2", oauth={"uaa_url": OAUTH["uaa_url"]})
        check("missing client_id rejected", False, "no error raised")
    except Exception:
        check("missing client_id rejected", True)
    try:
        McpServerPayload(url=ARC1_URL, auth_mode="oauth2", oauth={"client_id": "x"})
        check("missing endpoints rejected", False, "no error raised")
    except Exception:
        check("missing endpoints rejected", True)
    try:
        McpServerPayload(url=ARC1_URL, auth_mode="jwt", oauth=OAUTH)
        check("oauth on non-oauth2 rejected", False, "no error raised")
    except Exception:
        check("oauth on non-oauth2 rejected", True)

    # --- begin_authorization: PKCE + state + URL --------------------------
    print("\n== begin_authorization ==")
    cfg = Oauth2Config.from_spec(OAUTH)
    check("authorize_url derived from uaa_url", cfg.authorize_url.endswith("/oauth/authorize"))
    check("token_url derived from uaa_url", cfg.token_url.endswith("/oauth/token"))
    url = await begin_authorization(
        SERVER_KEY, cfg, user_id=USER, base_url="https://approuter.example.com"
    )
    check("authorize url points at provider", url.startswith(cfg.authorize_url))
    check("url carries PKCE S256", "code_challenge_method=S256" in url and "code_challenge=" in url)
    check("url carries client_id", f"client_id={OAUTH['client_id'].replace('!', '%21')}" in url or "client_id=" in url)
    check("redirect_uri is the callback", "redirect_uri=https%3A%2F%2Fapprouter.example.com%2Foauth%2Fcallback" in url)
    # Extract the state we just persisted
    import urllib.parse as up

    state = up.parse_qs(up.urlparse(url).query)["state"][0]

    # --- complete_authorization: code -> token ----------------------------
    print("\n== complete_authorization ==")
    global _next_token_payload, _next_token_status
    _next_token_payload = {
        "access_token": "ACCESS-1",
        "refresh_token": "REFRESH-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid",
    }
    _token_calls.clear()
    returned_key = await complete_authorization(code="auth-code-xyz", state=state, principal=USER)
    check("complete returns server_key", returned_key == SERVER_KEY)
    check("token endpoint got authorization_code grant", _token_calls and _token_calls[-1]["grant_type"] == "authorization_code")
    check("token endpoint got the code", _token_calls[-1]["code"] == "auth-code-xyz")
    check("token endpoint got code_verifier (PKCE)", "code_verifier" in _token_calls[-1])
    async with SessionLocal() as s:
        tok = await get_user_token(s, USER, SERVER_KEY)
        check("access token persisted", tok is not None and tok.access_token == "ACCESS-1")
        check("refresh token persisted", tok.refresh_token == "REFRESH-1")
        check("expiry persisted", tok.expires_at is not None)

    # reused state must now fail
    try:
        await complete_authorization(code="auth-code-xyz", state=state, principal=USER)
        check("state is single-use", False, "second use succeeded")
    except ValueError:
        check("state is single-use", True)

    # --- PerUserOAuth2Auth: attaches token --------------------------------
    print("\n== PerUserOAuth2Auth ==")
    token_p = current_principal.set(USER)
    base_p = current_base_url.set("https://approuter.example.com")
    try:
        auth = PerUserOAuth2Auth(SERVER_KEY, OAUTH)
        request = httpx.Request("POST", SERVER_KEY)
        gen = auth.async_auth_flow(request)
        sent = await gen.__anext__()
        check("auth attaches bearer", sent.headers.get("Authorization") == "Bearer ACCESS-1")
        await gen.aclose()
    finally:
        current_principal.reset(token_p)
        current_base_url.reset(base_p)

    # --- PerUserOAuth2Auth: no token -> raises ----------------------------
    other_p = current_principal.set("a-user-with-no-token")
    try:
        auth = PerUserOAuth2Auth(SERVER_KEY, OAUTH)
        gen = auth.async_auth_flow(httpx.Request("POST", SERVER_KEY))
        try:
            await gen.__anext__()
            check("raises when no token", False, "no exception")
        except OAuthAuthorizationRequired:
            check("raises when no token", True)
    finally:
        current_principal.reset(other_p)

    # --- PerUserOAuth2Auth: no principal -> raises ------------------------
    auth = PerUserOAuth2Auth(SERVER_KEY, OAUTH)
    gen = auth.async_auth_flow(httpx.Request("POST", SERVER_KEY))
    try:
        await gen.__anext__()
        check("raises when no principal", False, "no exception")
    except OAuthAuthorizationRequired:
        check("raises when no principal", True)

    # --- token refresh on expiry ------------------------------------------
    print("\n== Refresh on expiry ==")
    async with SessionLocal() as s:
        from agents.db import upsert_user_token

        await upsert_user_token(
            s,
            user_id=USER,
            server_key=SERVER_KEY,
            access_token="OLD",
            refresh_token="REFRESH-1",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
    _next_token_payload = {"access_token": "ACCESS-2", "token_type": "Bearer", "expires_in": 3600}
    _token_calls.clear()
    p = current_principal.set(USER)
    try:
        auth = PerUserOAuth2Auth(SERVER_KEY, OAUTH)
        gen = auth.async_auth_flow(httpx.Request("POST", SERVER_KEY))
        sent = await gen.__anext__()
        check("expired token triggers refresh", _token_calls and _token_calls[-1]["grant_type"] == "refresh_token")
        check("refreshed bearer attached", sent.headers.get("Authorization") == "Bearer ACCESS-2")
        await gen.aclose()
    finally:
        current_principal.reset(p)

    # --- DCR: auto-discover + register ------------------------------------
    print("\n== DCR (auto-discover + register) ==")
    reg_calls = {"n": 0}

    async def _fake_discover(server_key, redirect_uri, scope):
        reg_calls["n"] += 1
        return Oauth2Config(
            authorize_url="https://as.example.com/oauth/authorize",
            token_url="https://as.example.com/oauth/token",
            client_id="dcr-client-1",
            client_secret="dcr-secret-1",
            scope=scope,
        )

    _orig_discover = oauth2._discover_and_register
    oauth2._discover_and_register = _fake_discover  # type: ignore[assignment]

    DCR_URL = "https://arc1-dcr.cfapps.eu20-001.hana.ondemand.com"
    DCR_KEY = normalize_mcp_url(DCR_URL)
    async with SessionLocal() as s:
        await upsert_agent(
            s,
            name="arc1dcr",
            description="ARC-1 via DCR",
            instructions="dcr specialist",
            mcp_servers=[{"url": DCR_URL, "auth_mode": "oauth2", "oauth": {"dcr": True, "scope": "openid"}}],
        )
    async with SessionLocal() as s:
        rows = await list_agents(s)
        row = next(r for r in rows if r.name == "arc1dcr")
        check("dcr stored", row.mcp_servers[0]["oauth"] == {"dcr": True, "scope": "openid"})
        oauth_d = row.to_dict()["mcp_servers"][0]["oauth"]
        check("dcr to_dict has no secret", oauth_d.get("client_secret", "") == "")

    bp = current_base_url.set("https://approuter.example.com")
    try:
        cfg1 = await oauth2.resolve_config(DCR_KEY, {"dcr": True, "scope": "openid"})
        check("dcr resolve registers once", cfg1.client_id == "dcr-client-1" and reg_calls["n"] == 1)
        cfg2 = await oauth2.resolve_config(DCR_KEY, {"dcr": True, "scope": "openid"})
        check("dcr second resolve uses cache", cfg2.client_id == "dcr-client-1" and reg_calls["n"] == 1)
    finally:
        current_base_url.reset(bp)
    async with SessionLocal() as s:
        from agents.db import get_oauth_client

        rc = await get_oauth_client(s, DCR_KEY)
        check("registered client persisted", rc is not None and rc.client_id == "dcr-client-1")

    # /oauth/login helper builds a fresh authorize URL for the agent (cache hit)
    login_url = await oauth2.begin_authorization_for_agent(
        "arc1dcr", user_id=USER, base_url="https://approuter.example.com"
    )
    check("login builds authorize url for dcr agent", bool(login_url) and login_url.startswith("https://as.example.com/oauth/authorize"))
    check("login authorize url carries state", "state=" in (login_url or ""))
    missing = await oauth2.begin_authorization_for_agent(
        "does-not-exist", user_id=USER, base_url="https://approuter.example.com"
    )
    check("login returns None for unknown agent", missing is None)

    # DCR registration needs the request's base_url; without it -> re-prompt
    try:
        await oauth2.resolve_config(
            normalize_mcp_url("https://fresh.cfapps.eu20-001.hana.ondemand.com"), {"dcr": True}
        )
        check("dcr without base_url raises", False, "no exception")
    except OAuthAuthorizationRequired:
        check("dcr without base_url raises", True)
    check("dcr did not register without base_url", reg_calls["n"] == 1)

    # admin payload: dcr accepted without any credentials
    dcr_payload = McpServerPayload(url=DCR_URL, auth_mode="oauth2", oauth={"dcr": True})
    check("dcr payload accepted without creds", dcr_payload.oauth.dcr is True)
    check("dcr to_servers carries dcr", dcr_payload.oauth.to_config() == {"dcr": True})

    # --- real discovery + DCR glue against a mocked transport -------------
    print("\n== DCR discovery glue (mocked HTTP) ==")

    def _handler(request):
        u = str(request.url)
        if u.endswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(
                200,
                json={
                    "resource": "https://disc.example.com/mcp",
                    "authorization_servers": ["https://as.disc.example.com"],
                },
            )
        if "/.well-known/oauth-authorization-server" in u or "openid-configuration" in u:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://as.disc.example.com",
                    "authorization_endpoint": "https://as.disc.example.com/oauth/authorize",
                    "token_endpoint": "https://as.disc.example.com/oauth/token",
                    "registration_endpoint": "https://as.disc.example.com/oauth/register",
                    "response_types_supported": ["code"],
                },
            )
        if u.endswith("/oauth/register"):
            return httpx.Response(
                201,
                json={
                    "client_id": "DISCOVERED-CID",
                    "client_secret": "DISCOVERED-SECRET",
                    "redirect_uris": ["https://approuter.example.com/oauth/callback"],
                },
            )
        return httpx.Response(404)

    _transport = httpx.MockTransport(_handler)
    _real_client = httpx.AsyncClient

    def _client_factory(*a, **k):
        k.pop("transport", None)
        return _real_client(*a, transport=_transport, **k)

    oauth2.httpx.AsyncClient = _client_factory  # type: ignore[attr-defined]
    try:
        cfg = await _orig_discover(
            normalize_mcp_url("https://disc.example.com"),
            "https://approuter.example.com/oauth/callback",
            "openid",
        )
        check("discovery finds authorize endpoint", cfg.authorize_url == "https://as.disc.example.com/oauth/authorize")
        check("discovery finds token endpoint", cfg.token_url == "https://as.disc.example.com/oauth/token")
        check("DCR returns client_id", cfg.client_id == "DISCOVERED-CID")
        check("DCR returns client_secret", cfg.client_secret == "DISCOVERED-SECRET")
    finally:
        oauth2.httpx.AsyncClient = _real_client  # type: ignore[attr-defined]

    # --- _post_token uses client_secret_post (secret in body) -------------
    print("\n== _post_token client auth (client_secret_post) ==")
    captured: dict = {}

    def _tok_handler(request):
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "X"})

    _tt = httpx.MockTransport(_tok_handler)
    _real_ac = httpx.AsyncClient

    def _ac_factory(*a, **k):
        k.pop("transport", None)
        return _real_ac(*a, transport=_tt, **k)

    oauth2.httpx.AsyncClient = _ac_factory  # type: ignore[attr-defined]
    try:
        cfg_conf = Oauth2Config(
            authorize_url="https://x/authorize", token_url="https://x/token",
            client_id="CID", client_secret="SEC",
        )
        await _orig_post_token(cfg_conf, {"grant_type": "authorization_code", "code": "c"})
        check("client_id in body", "client_id=CID" in captured["body"])
        check("client_secret in body", "client_secret=SEC" in captured["body"])
        check("no HTTP Basic auth header", "authorization" not in captured["headers"])
        # public client (no secret) -> no client_secret in body
        cfg_pub = Oauth2Config(
            authorize_url="https://x/authorize", token_url="https://x/token",
            client_id="PUB", client_secret=None,
        )
        await _orig_post_token(cfg_pub, {"grant_type": "refresh_token", "refresh_token": "r"})
        check("public client omits client_secret", "client_secret=" not in captured["body"])
    finally:
        oauth2.httpx.AsyncClient = _real_ac  # type: ignore[attr-defined]

    # --- find_oauth_required walks exception trees ------------------------
    print("\n== find_oauth_required ==")
    exc = OAuthAuthorizationRequired(SERVER_KEY, cfg, "no-token")
    grouped = BaseExceptionGroup("boom", [ValueError("x"), exc])
    check("finds inside ExceptionGroup", find_oauth_required(grouped) is exc)
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = exc
    check("finds via __cause__", find_oauth_required(wrapped) is exc)
    check("returns None when absent", find_oauth_required(ValueError("nope")) is None)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    if TEST_DB.exists():
        try:
            await SessionLocal().bind.dispose()  # type: ignore[attr-defined]
        except Exception:
            pass
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
