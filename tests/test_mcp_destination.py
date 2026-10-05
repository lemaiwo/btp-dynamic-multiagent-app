"""A remote MCP server reached through a BTP destination.

``auth_mode="destination"`` on a real MCP URL (ARC-1, say) sends every MCP
request through :class:`agents.destination_auth.DestinationAuth`: the
destination names the host and supplies the credential, and with
``user_context: true`` it is resolved as the signed-in user (their XSUAA JWT
goes to the destination service as ``X-user-token``). These tests pin:

* the request lands on the destination's host, at the configured path, with
  the destination's ``Authorization`` -- never on the configured URL's host;
* no JWT bound + ``user_context`` -> ``DestinationUserRequired``, nothing sent;
* two users at once each get their own header (resolver keyed by principal);
* redirects are not followed (a redirect must not move the credential);
* storage keeps only ``{destination, user_context}`` for a remote URL;
* the admin API accepts the shape and 422s a missing destination or DCR.

No network: the destination is a fake resolver and the server a MockTransport.

Run:  python -m pytest tests/test_mcp_destination.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_mcp_destination.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

import httpx  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import agents.destination as destination_module  # noqa: E402
import agents.shared as shared  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination import Destination, DestinationError  # noqa: E402
from agents.destination_auth import DestinationUserRequired  # noqa: E402

MCP_URL = "https://arc1.example/mcp"
DEST = "arc1-abap-readonly"

# Undo the import-time Agent / create_mcp_server stubs of other suites.
pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


class FakeResolver:
    """Returns the destination's URL; the token depends on who asks."""

    def __init__(self, url: str = "https://arc1.example", name: str = DEST):
        self.name = name
        self.url = url
        self.calls: list[tuple[str | None, str | None]] = []

    async def resolve(self, *, force: bool = False, user_token=None, principal=None):
        self.calls.append((user_token, principal))
        await asyncio.sleep(0)  # let overlapping runs interleave
        token = f"U-{principal}" if user_token else "APP"
        return Destination(url=self.url, headers={"Authorization": f"Bearer {token}"},
                           expires_at=time.monotonic() + 60)

    def invalidate(self, principal: str | None = None) -> None:
        pass


@pytest.fixture
def resolver(monkeypatch):
    fake = FakeResolver()
    seen: dict[str, object] = {}

    def _from_env(name, *, server_key="", require_credential=True):
        seen.update(name=name, server_key=server_key)
        return fake

    monkeypatch.setattr(destination_module, "resolver_from_environment", _from_env)
    fake.seen = seen
    return fake


def _server(oauth: dict, url: str = MCP_URL):
    return shared.create_mcp_server("arc1", url, "destination", oauth=oauth)


async def _send(server, handler) -> httpx.Response:
    """Send one MCP-shaped POST with the server's own auth, through a mock."""
    async with httpx.AsyncClient(
        auth=server.http_client.auth, transport=httpx.MockTransport(handler),
        follow_redirects=server.http_client.follow_redirects,
    ) as client:
        return await client.post(server.url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})


def _recorder():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    return seen, handler


# --- transport -----------------------------------------------------------------

async def test_request_goes_to_the_destination_with_its_header(resolver):
    server = _server({"destination": DEST, "user_context": True})
    assert resolver.seen["name"] == DEST
    seen, handler = _recorder()
    jt = current_jwt.set("jwt-of-alice")
    pt = current_principal.set("alice")
    try:
        r = await _send(server, handler)
    finally:
        current_jwt.reset(jt)
        current_principal.reset(pt)
    assert r.status_code == 200
    assert len(seen) == 1
    assert str(seen[0].url) == "https://arc1.example/mcp"
    assert seen[0].headers["Authorization"] == "Bearer U-alice"
    assert seen[0].headers["Host"] == "arc1.example"
    # The user's JWT went to the destination service, not to the target.
    assert resolver.calls == [("jwt-of-alice", "alice")]


async def test_destination_host_wins_over_the_configured_host(resolver):
    resolver.url = "https://proxy.example/sap/arc1"
    server = _server({"destination": DEST})
    seen, handler = _recorder()
    await _send(server, handler)
    assert str(seen[0].url) == "https://proxy.example/sap/arc1/mcp"
    assert seen[0].headers["Authorization"] == "Bearer APP"
    assert resolver.calls == [(None, None)]  # app-level, no user token


async def test_user_context_without_a_jwt_fails_and_sends_nothing(resolver):
    server = _server({"destination": DEST, "user_context": True})
    seen, handler = _recorder()
    assert current_jwt.get() is None
    with pytest.raises(DestinationUserRequired):
        await _send(server, handler)
    assert seen == []
    assert resolver.calls == []


async def test_string_false_is_not_user_context(resolver):
    server = _server({"destination": DEST, "user_context": "true"})
    assert server.http_client.auth.user_context is False


async def test_two_users_concurrently_get_their_own_header(resolver):
    server = _server({"destination": DEST, "user_context": True})
    seen, handler = _recorder()

    async def as_user(who: str):
        current_jwt.set(f"jwt-of-{who}")
        current_principal.set(who)
        for _ in range(5):
            await _send(server, handler)

    await asyncio.gather(asyncio.create_task(as_user("alice")),
                         asyncio.create_task(as_user("bob")))
    assert len(seen) == 10
    by_user = {}
    for req, (tok, who) in zip(seen, resolver.calls):
        by_user.setdefault(who, set()).add(req.headers["Authorization"])
        assert tok == f"jwt-of-{who}"
    assert by_user == {"alice": {"Bearer U-alice"}, "bob": {"Bearer U-bob"}}


async def test_redirect_is_not_followed(resolver):
    server = _server({"destination": DEST})
    assert server.http_client.follow_redirects is False
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/mcp"})

    r = await _send(server, handler)
    assert r.status_code == 302
    assert [str(x.url) for x in seen] == ["https://arc1.example/mcp"]


async def test_credential_never_goes_to_the_configured_host(resolver):
    """The configured URL's host is not a destination host: an absolute
    request there must be refused, so only the destination names hosts."""
    resolver.url = "https://proxy.example"
    server = _server({"destination": DEST})
    seen, handler = _recorder()
    async with httpx.AsyncClient(auth=server.http_client.auth,
                                 transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DestinationError, match="refusing"):
            await client.post("https://arc1.example/mcp", json={})
    assert seen == []


def test_missing_destination_name_is_refused_at_build(resolver):
    with pytest.raises(ValueError, match="requires a 'destination'"):
        _server({"user_context": True})


def test_server_is_still_per_run(resolver):
    from agents.shared import PerRunMCPServer

    assert isinstance(_server({"destination": DEST}), PerRunMCPServer)


# --- storage -------------------------------------------------------------------

def test_storage_keeps_only_destination_and_user_context():
    from agents.db import _clean_destination

    raw = {"destination": f" {DEST} ", "user_context": True, "client_id": "c",
           "client_secret": "s", "project": "P", "allow_comment": True, "mailbox": "m"}
    assert _clean_destination(raw, MCP_URL) == {"destination": DEST, "user_context": True}
    assert _clean_destination({"destination": DEST, "user_context": "true"}, MCP_URL) == {
        "destination": DEST}
    with pytest.raises(ValueError):
        _clean_destination({"user_context": True}, MCP_URL)


def test_jira_storage_is_unchanged():
    from agents.db import _clean_destination

    out = _clean_destination({"destination": "J", "project": "P"}, "builtin:jira")
    assert out == {"destination": "J", "project": "P", "allow_comment": False}


# --- admin validation ------------------------------------------------------------

def _payload(**oauth):
    from agents.admin import McpServerPayload

    return McpServerPayload(url=MCP_URL, auth_mode="destination", oauth=oauth)


def test_admin_accepts_a_remote_url_on_a_destination():
    p = _payload(destination=DEST, user_context=True)
    assert p.oauth.to_config() == {"destination": DEST, "user_context": True}
    _payload(destination=DEST)


@pytest.mark.parametrize("oauth,message", [
    ({"user_context": True}, "requires oauth.destination"),
    ({"destination": DEST, "dcr": True}, "cannot use DCR"),
    ({"destination": DEST, "client_id": "c"}, "stores no credential"),
    ({"destination": "bad name!"}, "destination name"),
])
def test_admin_refuses_remote_destination_misconfiguration(oauth, message):
    with pytest.raises(ValidationError, match=message):
        _payload(**oauth)


def test_admin_still_requires_https_for_a_remote_destination():
    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match="https"):
        McpServerPayload(url="http://arc1.example/mcp", auth_mode="destination",
                         oauth={"destination": DEST})


PREFIX = "mcp-dest-"


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    import app as app_module
    from agents.db import init_db

    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c
        r = await c.get("/admin/api/agents")
        for a in r.json():
            if a["name"].startswith(PREFIX):
                await c.delete(f"/admin/api/agents/{a['id']}?force=true")


def _agent(name: str, oauth: dict) -> dict:
    return {
        "name": PREFIX + name, "description": "d", "instructions": "i",
        "mcp_servers": [{"url": MCP_URL, "auth_mode": "destination", "oauth": oauth}],
    }


async def test_admin_api_saves_a_remote_destination_server(client):
    r = await client.post("/admin/api/agents",
                          json=_agent("ok", {"destination": DEST, "user_context": True}))
    assert r.status_code in (200, 201), r.text
    srv = r.json()["mcp_servers"][0]
    assert srv["auth_mode"] == "destination"
    stored = {k: v for k, v in srv["oauth"].items() if k != "has_client_secret"}
    assert stored == {"destination": DEST, "user_context": True}
    assert srv["oauth"].get("has_client_secret") in (None, False)


@pytest.mark.parametrize("oauth", [
    {"user_context": True},
    {"destination": DEST, "dcr": True},
])
async def test_admin_api_refuses_a_bad_remote_destination(client, oauth):
    r = await client.post("/admin/api/agents", json=_agent("bad", oauth))
    assert r.status_code == 422, r.text



# --- final fix round: the configured URL never reaches an error text (FIX-12) ---


async def test_errors_name_the_server_without_query_or_userinfo(resolver):
    url = "https://admin:pw-123@arc1.example:8443/sap/mcp?sap-client=100&token=abc"
    server = _server({"destination": DEST, "user_context": True}, url=url)
    assert resolver.seen["server_key"] == "https://arc1.example:8443/sap/mcp"
    seen, handler = _recorder()
    with pytest.raises(DestinationUserRequired) as exc:
        await _send(server, handler)
    text = str(exc.value)
    assert "https://arc1.example:8443/sap/mcp" in text
    for leak in ("pw-123", "admin", "token=abc", "sap-client"):
        assert leak not in text, leak


def test_safe_server_key_shapes():
    assert shared.safe_server_key("https://u:p@h.example/x?q=1#f") == "https://h.example/x"
    assert shared.safe_server_key("https://h.example") == "https://h.example"
    assert shared.safe_server_key("not a url") == "not a url"
