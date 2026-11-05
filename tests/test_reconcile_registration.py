"""``builtin:reconcile`` as a registered built-in: the factory table, its
config block (keys, defaults, refusals), the admin gate (its 422 texts), the
storage cleaner, and the registry link to the agent's own ARC-1 server entry.

Run:  python -m pytest tests/test_reconcile_registration.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

URL = "builtin:reconcile"
ARC1 = "https://arc1.example/mcp"
OTHER_ARC1 = "https://arc1-other.example/mcp"
SECRET = "Zx9secret"
BLOCK = {"arc1_server": ARC1, "target": "SRC", "batch_size": "200",
         "mismatch_cap": "50000"}


def _reconcile(block: dict[str, Any] | None = None, mode: str = "none") -> dict[str, Any]:
    entry: dict[str, Any] = {"url": URL, "auth_mode": mode}
    if block is not None:
        entry["oauth"] = block
    return entry


def _arc1(url: str = ARC1) -> dict[str, Any]:
    return {"url": url, "auth_mode": "oauth2", "oauth": {"dcr": True}}


# --- factory ------------------------------------------------------------------

def test_reconcile_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert URL in BUILTIN_URLS and is_builtin_url(" BUILTIN:Reconcile ")


def test_the_docstring_table_lists_the_builtin_and_the_context_names_the_hook():
    import agents.builtins as builtins

    assert "builtin:reconcile  --        --        --            default  --" in (
        builtins.__doc__)
    assert "mcp_server_for" in builtins.__doc__
    assert "mcp_server_for" in builtins.build_builtin_toolset.__doc__


def test_the_factory_builds_a_function_toolset_with_its_context():
    from pydantic_ai.toolsets import FunctionToolset

    from agents.builtins import build_builtin_toolset
    from agents.reconcile.config import ReconcileConfig

    seen: list[str] = []

    def lookup(url: str) -> Any:
        seen.append(url)
        return None

    toolset = build_builtin_toolset(URL, {"arc1_server": ARC1}, "none",
                                    context={"mcp_server_for": lookup,
                                             "agent_name": "recon"})
    assert isinstance(toolset, FunctionToolset)
    ctx = toolset.reconcile_context
    assert ctx.config == ReconcileConfig(arc1_server=ARC1, target="", batch_size=200,
                                         mismatch_cap=50_000)
    assert ctx.agent_name == "recon" and ctx.server_key == URL
    # Resolved when asked, not at build: the sibling may be built later.
    assert seen == []
    assert ctx.arc1_server() is None and seen == [ARC1]


def test_the_factory_without_a_context_or_an_arc1_server_resolves_nothing():
    from agents.builtins import build_builtin_toolset

    assert build_builtin_toolset(URL, None, "none").reconcile_context.arc1_server() is None
    called: list[str] = []
    toolset = build_builtin_toolset(URL, {}, None,
                                    context={"mcp_server_for": called.append})
    assert toolset.reconcile_context.arc1_server() is None and called == []


@pytest.mark.parametrize("mode", ["destination", "oauth2", "app_only", "jwt", "session"])
def test_the_factory_refuses_every_other_auth_mode(mode):
    from agents.builtins import build_builtin_toolset

    with pytest.raises(ValueError, match="requires auth_mode=none"):
        build_builtin_toolset(URL, {}, mode, context={"mcp_server_for": lambda u: None})


def test_the_factory_refuses_a_bad_block_without_its_value():
    from agents.builtins import build_builtin_toolset

    with pytest.raises(ValueError, match="^oauth.batch_size:") as info:
        build_builtin_toolset(URL, {"batch_size": SECRET}, "none")
    assert SECRET not in str(info.value)


# --- the config block ---------------------------------------------------------

def test_config_defaults_and_parsing():
    from agents.reconcile.config import (
        DEFAULT_BATCH_SIZE,
        DEFAULT_MISMATCH_CAP,
        RECONCILE_KEYS,
        ReconcileConfig,
    )

    assert RECONCILE_KEYS == ("arc1_server", "target", "batch_size", "mismatch_cap")
    assert ReconcileConfig.from_oauth(None) == ReconcileConfig(
        "", "", DEFAULT_BATCH_SIZE, DEFAULT_MISMATCH_CAP)
    assert ReconcileConfig.from_oauth({**BLOCK, "batch_size": "9999",
                                       "mismatch_cap": "200000", "target": " SRC "}) == (
        ReconcileConfig(ARC1, "SRC", 9_999, 200_000))
    assert ReconcileConfig.from_oauth({"batch_size": " ", "mismatch_cap": ""}) == (
        ReconcileConfig("", "", DEFAULT_BATCH_SIZE, DEFAULT_MISMATCH_CAP))


@pytest.mark.parametrize("change, field", [
    ({"batch_size": "0"}, "oauth.batch_size"),
    ({"batch_size": "10000"}, "oauth.batch_size"),
    ({"batch_size": "abc"}, "oauth.batch_size"),
    ({"batch_size": "-5"}, "oauth.batch_size"),
    ({"batch_size": "1.5"}, "oauth.batch_size"),
    ({"batch_size": 200}, "oauth.batch_size"),
    ({"mismatch_cap": "200001"}, "oauth.mismatch_cap"),
    ({"mismatch_cap": "0"}, "oauth.mismatch_cap"),
    ({"arc1_server": "http://x"}, "oauth.arc1_server"),
    ({"arc1_server": "builtin:sapnotes"}, "oauth.arc1_server"),
    ({"arc1_server": f"https://u:{SECRET}@arc1.example/mcp"}, "oauth.arc1_server"),
    ({"arc1_server": f"https://arc1.example/mcp?k={SECRET}"}, "oauth.arc1_server"),
    ({"arc1_server": f"https://arc1.example/mcp#{SECRET}"}, "oauth.arc1_server"),
    ({"arc1_server": "https:///mcp"}, "oauth.arc1_server"),
    ({"target": f"{SECRET} x"}, "oauth.target"),
    ({"target": "x" * 65}, "oauth.target"),
    ({"min_score": "7"}, "oauth"),
])
def test_validate_reconcile_block_names_the_key_never_the_value(change, field):
    from agents.reconcile.config import validate_reconcile_block

    with pytest.raises(ValueError) as info:
        validate_reconcile_block({**BLOCK, **change})
    assert str(info.value).startswith(field)
    assert SECRET not in str(info.value)


# --- the admin gate -----------------------------------------------------------

def _payload(oauth: Any, mode: str = "none", url: str = URL):
    from agents.admin import McpServerPayload

    data: dict[str, Any] = {"url": url, "auth_mode": mode}
    if oauth is not None:
        data["oauth"] = oauth
    return McpServerPayload(**data)


def _refusal(oauth: Any, mode: str = "none", url: str = URL) -> str:
    with pytest.raises(ValidationError) as info:
        _payload(oauth, mode, url)
    msgs = [e["msg"] for e in info.value.errors()]
    assert all(SECRET not in m for m in msgs)
    return " | ".join(msgs)


def test_the_gate_hands_storage_exactly_the_four_keys():
    assert _payload(dict(BLOCK)).oauth.to_config() == BLOCK
    assert _payload({**BLOCK, "arc1_server": f"  {ARC1} "}).oauth.to_config() == BLOCK
    assert _payload(None).oauth is None
    assert _payload({"arc1_server": None, "target": None}).oauth.to_config() == {}


@pytest.mark.parametrize("change, said", [
    ({"batch_size": "0"}, "oauth.batch_size"),
    ({"batch_size": "10000"}, "oauth.batch_size"),
    ({"batch_size": "abc"}, "oauth.batch_size"),
    ({"mismatch_cap": "200001"}, "oauth.mismatch_cap"),
    ({"arc1_server": "http://x"}, "oauth.arc1_server"),
    ({"arc1_server": "builtin:sapnotes"}, "oauth.arc1_server"),
    ({"target": f"{SECRET}!"}, "oauth.target"),
    ({"min_score": "7"}, "remove oauth.min_score"),
    ({"destination": SECRET}, "remove oauth.destination"),
])
def test_the_gate_refuses_and_names_the_field(change, said):
    assert said in _refusal({**BLOCK, **change})


def test_a_stray_key_refusal_lists_the_reconcile_keys():
    text = _refusal({**BLOCK, "min_score": "7"})
    assert "allowed: arc1_server, target, batch_size, mismatch_cap" in text


@pytest.mark.parametrize("mode", ["oauth2", "destination", "app_only", "jwt"])
def test_the_gate_refuses_every_other_auth_mode(mode):
    assert "builtin:reconcile requires auth_mode=none" in _refusal(dict(BLOCK), mode)


def test_the_reconcile_keys_belong_to_the_reconcile_entry_only():
    assert f"belong to a {URL} entry only" in _refusal({"arc1_server": ARC1},
                                                       url="builtin:sapnotes")
    for key, value in (("arc1_server", ARC1), ("target", "SRC"), ("batch_size", "10"),
                       ("mismatch_cap", "10")):
        text = _refusal({"dcr": True, key: value}, mode="oauth2", url=ARC1)
        assert f"belong to a {URL} entry only" in text


def test_a_sapnotes_entry_keeps_its_own_keys():
    assert _payload({"min_score": "7"}, url="builtin:sapnotes").oauth.to_config() == {
        "min_score": "7"}


# --- storage ------------------------------------------------------------------

def test_storage_keeps_only_the_reconcile_keys_for_the_reconcile_entry():
    from agents.db import _clean_builtin_public

    noisy = {**BLOCK, "min_score": "7", "lookback": "2d", "client_secret": SECRET}
    assert _clean_builtin_public(noisy, "builtin:reconcile") == BLOCK
    assert _clean_builtin_public(noisy, " BUILTIN:Reconcile ") == BLOCK
    assert _clean_builtin_public(noisy, "builtin:sapnotes") == {"min_score": "7",
                                                                "lookback": "2d"}
    assert _clean_builtin_public(noisy) == {"min_score": "7", "lookback": "2d"}
    assert _clean_builtin_public({}, "builtin:reconcile") is None


def test_storage_refuses_a_bad_reconcile_value_by_itself():
    from agents.db import _clean_builtin_public

    with pytest.raises(ValueError, match="^oauth.batch_size") as info:
        _clean_builtin_public({"batch_size": SECRET}, "builtin:reconcile")
    assert SECRET not in str(info.value)


def test_storage_stores_the_canonical_url_and_refuses_another_mode():
    from agents.db import prepare_servers

    primary, extras, oauth_json = prepare_servers(
        [_arc1(), {"url": "BUILTIN:Reconcile/", "auth_mode": "none", "oauth": BLOCK}], None)
    assert extras == [{"url": URL, "auth_mode": "none", "oauth": BLOCK}]
    for mode in ("destination", "oauth2", "app_only"):
        with pytest.raises(ValueError, match="requires auth_mode=none"):
            prepare_servers([{"url": URL, "auth_mode": mode, "oauth": {}}], None)


# --- the API ------------------------------------------------------------------

async def _wipe() -> None:
    from sqlalchemy import delete

    from agents.db import AgentConfig, SessionLocal

    async with SessionLocal() as session:
        await session.execute(delete(AgentConfig))
        await session.commit()


@pytest.fixture
async def client(monkeypatch):
    import app as app_module
    from agents.db import init_db

    monkeypatch.setenv("MCP_URL_ALLOWLIST", "arc1.example,arc1-other.example")
    await init_db()
    await _wipe()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


def _agent(name: str, servers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i", "mcp_servers": servers}


async def _stored(name: str):
    from agents.db import SessionLocal, get_agent_by_name

    async with SessionLocal() as session:
        row = await get_agent_by_name(session, name)
        return row and row.mcp_servers


async def test_the_api_stores_the_entry_and_refuses_bad_values_with_422(client):
    r = await client.post("/admin/api/agents",
                          json=_agent("recon", [_reconcile(dict(BLOCK)), _arc1()]))
    assert r.status_code in (200, 201), r.text
    servers = await _stored("recon")
    assert servers[0] == _reconcile(BLOCK)
    assert servers[1]["url"] == ARC1 and servers[1]["auth_mode"] == "oauth2"
    listed = await client.get("/admin/api/agents")
    (answered,) = [a for a in listed.json() if a["name"] == "recon"]
    shown = answered["mcp_servers"][0]["oauth"]
    assert {k: shown[k] for k in BLOCK} == BLOCK

    refused = [
        ({"batch_size": "0"}, "oauth.batch_size"),
        ({"batch_size": "10000"}, "oauth.batch_size"),
        ({"batch_size": "abc"}, "oauth.batch_size"),
        ({"mismatch_cap": "200001"}, "oauth.mismatch_cap"),
        ({"arc1_server": "http://x"}, "oauth.arc1_server"),
        ({"arc1_server": "builtin:sapnotes"}, "oauth.arc1_server"),
        ({"min_score": "7"}, "oauth.min_score"),
    ]
    for change, said in refused:
        r = await client.post("/admin/api/agents", json=_agent(
            "recon-refused", [_reconcile({**BLOCK, **change}), _arc1()]))
        assert r.status_code == 422, r.text
        assert said in r.text
        for item in r.json()["detail"]:
            assert set(item) == {"loc", "msg", "type"}, item
    for mode in ("oauth2", "destination"):
        r = await client.post("/admin/api/agents", json=_agent(
            "recon-refused", [_reconcile(dict(BLOCK), mode), _arc1()]))
        assert r.status_code == 422 and "requires auth_mode=none" in r.text, r.text
    r = await client.post("/admin/api/agents", json=_agent(
        "recon-refused", [{"url": "builtin:sapnotes", "auth_mode": "none",
                           "oauth": {"arc1_server": ARC1}}]))
    assert r.status_code == 422 and "oauth.arc1_server" in r.text, r.text
    assert await _stored("recon-refused") is None


# --- a real registry build ----------------------------------------------------

async def _build(agents: dict[str, list[dict[str, Any]]], monkeypatch):
    from pydantic_ai.models.test import TestModel

    from agents import registry as registry_module
    from agents.db import SessionLocal, init_db, upsert_agent

    await init_db()
    await _wipe()
    async with SessionLocal() as s:
        for name, servers in agents.items():
            await upsert_agent(s, name=name, description="d", instructions="i",
                               mcp_servers=servers)
        await s.commit()

    toolsets: dict[str, Any] = {}
    created: dict[tuple[str, str], Any] = {}
    real_builtin = registry_module.build_builtin_toolset
    real_create = registry_module.create_mcp_server

    def spy_builtin(url, oauth, auth_mode=None, *, context=None):
        toolset = real_builtin(url, oauth, auth_mode, context=context)
        if str(url).strip().lower() == URL:
            toolsets[context["agent_name"]] = toolset
        return toolset

    def spy_create(server_name, url, *args, **kwargs):
        server = real_create(server_name, url, *args, **kwargs)
        created[(server_name.split("-")[0], url)] = server
        return server

    monkeypatch.setattr(registry_module, "build_builtin_toolset", spy_builtin)
    monkeypatch.setattr(registry_module, "create_mcp_server", spy_create)
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    build = await registry_module.build_orchestrator()
    return build, toolsets, created


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_the_factory_resolves_the_very_server_of_its_own_agent(monkeypatch):
    from agents.shared import PerRunMCPServer, mcp_endpoint_url

    monkeypatch.setenv("MCP_URL_ALLOWLIST", "arc1.example,arc1-other.example")
    build, toolsets, created = await _build({
        # The reconcile entry first: the sibling is built after it.
        "alpha": [_reconcile(dict(BLOCK)), _arc1()],
        "beta": [_arc1(OTHER_ARC1), _reconcile({**BLOCK, "arc1_server": OTHER_ARC1})],
        "gamma": [_reconcile({**BLOCK, "arc1_server": ""})],
    }, monkeypatch)
    assert {"alpha", "beta", "gamma"} <= set(build.specialists)

    alpha = toolsets["alpha"].reconcile_context
    server = alpha.server_for(f"{ARC1}/")
    assert isinstance(server, PerRunMCPServer)
    assert alpha.server_for("HTTPS://ARC1.Example/mcp") is server  # host case folded
    assert server is created[("alpha", ARC1)]
    assert alpha.arc1_server() is server
    assert mcp_endpoint_url(ARC1) == mcp_endpoint_url(f"{ARC1}/")
    # Never another agent's server, even for that agent's URL.
    assert alpha.server_for(OTHER_ARC1) is None

    beta = toolsets["beta"].reconcile_context
    assert beta.arc1_server() is created[("beta", OTHER_ARC1)]
    assert beta.server_for(ARC1) is None

    # Built without the sibling: nothing to resolve (Task 11's tools answer
    # arc1_not_configured).
    assert toolsets["gamma"].reconcile_context.arc1_server() is None
    assert toolsets["gamma"].reconcile_context.agent_name == "gamma"


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_builtin_url_is_never_a_sibling(monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWLIST", "arc1.example")
    _, toolsets, _ = await _build({
        "delta": [_reconcile({**BLOCK, "arc1_server": ""}),
                  {"url": "builtin:sapnotes", "auth_mode": "none"}],
    }, monkeypatch)
    ctx = toolsets["delta"].reconcile_context
    assert ctx.server_for("builtin:sapnotes") is None
    assert ctx.server_for(URL) is None


# --- A1: a reload keeps a retired build open while a reconciliation runs ---------------

class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Server:
    def __init__(self) -> None:
        self._http_client = _Client()


async def test_a_reload_keeps_the_retired_build_open_while_a_run_is_in_flight(monkeypatch):
    import asyncio

    import agents.registry as registry_module
    from agents.reconcile import run as reconcile_run

    async def build():
        return registry_module.BuildResult(
            orchestrator=object(), specialists={}, mcp_clients=[_Server()], configs=[])

    monkeypatch.setattr(registry_module, "build_orchestrator", build)
    reg = registry_module.Registry()
    first = await reg.reload()
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    monkeypatch.setitem(reconcile_run._tasks, "conv-a1", task)
    try:
        await reg.reload()
        assert not first.mcp_clients[0]._http_client.closed
        assert reg._retired == [first]
    finally:
        gate.set()
        await task
    # A finished task no longer counts, even before its entry is removed.
    await reg.reload()
    assert first.mcp_clients[0]._http_client.closed


# --- A3: the ARC-1 sibling and a single entry -------------------------------------------

@pytest.fixture(autouse=True)
def _allow_the_test_hosts(monkeypatch):
    monkeypatch.setenv("MCP_URL_ALLOWLIST", "arc1.example,arc1-other.example")


def _agent_payload(servers: list[dict[str, Any]]):
    from agents.admin import AgentPayload

    return AgentPayload(**_agent("recon-a3", servers))


def _agent_refusal(servers: list[dict[str, Any]]) -> str:
    with pytest.raises(ValidationError) as info:
        _agent_payload(servers)
    return " | ".join(e["msg"] for e in info.value.errors())


def _sibling(mode: str, url: str = ARC1) -> dict[str, Any]:
    if mode == "oauth2":
        return _arc1(url)
    if mode == "destination":
        return {"url": url, "auth_mode": mode, "oauth": {"destination": "ARC1_DEST"}}
    if mode == "app_only":
        return {"url": url, "auth_mode": mode, "oauth": {
            "client_id": "cid", "client_secret": SECRET,
            "token_url": "https://login.example/token"}}
    return {"url": url, "auth_mode": mode}


@pytest.mark.parametrize("mode", ["oauth2", "jwt"])
def test_an_oauth2_or_jwt_sibling_is_accepted(mode):
    payload = _agent_payload([_reconcile(dict(BLOCK)), _sibling(mode)])
    assert len(payload.mcp_servers) == 2
    # The sibling may be spelled differently (host case, trailing slash, bare host).
    _agent_payload([_reconcile({**BLOCK, "arc1_server": "https://ARC1.example/mcp/"}),
                    _sibling(mode)])
    _agent_payload([_reconcile({**BLOCK, "arc1_server": "https://arc1.example"}),
                    _sibling(mode)])


@pytest.mark.parametrize("mode", ["none", "app_only", "destination"])
def test_a_sibling_on_another_mode_is_refused(mode):
    from agents.reconcile.config import ARC1_SIBLING_MESSAGE

    text = _agent_refusal([_reconcile(dict(BLOCK)), _sibling(mode)])
    assert ARC1_SIBLING_MESSAGE in text and "oauth.arc1_server" in text
    assert SECRET not in text


def test_no_sibling_is_refused_but_a_blank_arc1_server_is_not():
    from agents.reconcile.config import ARC1_SIBLING_MESSAGE

    assert ARC1_SIBLING_MESSAGE in _agent_refusal(
        [_reconcile(dict(BLOCK)), _arc1(OTHER_ARC1)])
    assert ARC1_SIBLING_MESSAGE in _agent_refusal([_reconcile(dict(BLOCK))])
    _agent_payload([_reconcile({**BLOCK, "arc1_server": ""})])


def test_two_reconcile_entries_are_refused_at_the_gate_and_in_storage():
    from agents.db import prepare_servers
    from agents.reconcile.config import RECONCILE_SINGLE_ENTRY_MESSAGE

    assert RECONCILE_SINGLE_ENTRY_MESSAGE in _agent_refusal(
        [_reconcile(dict(BLOCK)), _reconcile(dict(BLOCK)), _arc1()])
    with pytest.raises(ValueError, match="at most one builtin:reconcile entry"):
        prepare_servers([_reconcile(dict(BLOCK)),
                         {**_reconcile(dict(BLOCK)), "url": "BUILTIN:Reconcile/"}, _arc1()],
                        None)


@pytest.mark.parametrize("mode", ["none", "app_only", "destination"])
def test_storage_refuses_a_sibling_on_another_mode(mode):
    from agents.db import prepare_servers
    from agents.reconcile.config import ARC1_SIBLING_MESSAGE

    with pytest.raises(ValueError) as info:
        prepare_servers([_reconcile(dict(BLOCK)), _sibling(mode)], None)
    assert str(info.value) == ARC1_SIBLING_MESSAGE
    with pytest.raises(ValueError, match="oauth.arc1_server"):
        prepare_servers([_reconcile(dict(BLOCK))], None)
    prepare_servers([_reconcile(dict(BLOCK)), _sibling("jwt")], None)


def test_endpoint_key_folds_scheme_and_host_case_only():
    from agents.reconcile.config import endpoint_key

    assert endpoint_key("HTTPS://ARC1.Example/mcp/") == "https://arc1.example/mcp"
    assert endpoint_key(" https://arc1.example ") == "https://arc1.example/mcp"
    assert endpoint_key("https://arc1.example/Multi/MCP") == "https://arc1.example/Multi/MCP"
    assert endpoint_key("https://arc1.example:8443/mcp") == "https://arc1.example:8443/mcp"
