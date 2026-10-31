"""Tools and connectors for personal agents (web chat addendum (g)).

An admin marks an admin agent ``share_tools``; a personal agent names such
agents in ``tools`` and gets, per turn, the guarded server list the current
build keeps for each (``BuildResult.shared_toolsets``). Checked here: the
save-time rules (POST, PUT, import), the run (attached, skipped with the
note), the catalogue's ``tool_sources`` and connector labels, the context
view's ``connector`` entries, and the build itself.

Run:  .venv/bin/python -m pytest tests/test_webchat_tools.py -q
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ.pop("MCP_URL_ALLOWLIST", None)

from pydantic_ai import Agent, FunctionToolset  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ChatConversation,
    ChatFile,
    ChatMessage,
    ChatUserAgent,
    ChatUserSkill,
    SessionLocal,
    SkillConfig,
    init_db,
)
from agents.registry import RunCounter  # noqa: E402
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import my_routes, routes, run, user_agents  # noqa: E402

ALICE = "alice@example.com"
API = "/chat/api"
NOT_SOURCE = "Value error, tools: not a tool source you can use"
ADVANCED_TOOLS = "Value error, tools: advanced tools need the developer role"
SECRET = "Zq7SecretMarker"


class FakeValidator:
    """``has_scope`` as ``XsuaaValidator`` answers it."""

    xsappname = "app!t1"

    def has_scope(self, payload, scope):
        return f"{self.xsappname}.{scope}" in (payload.get("scope") or [])


ROLES = {
    "user": {"user_name": "u", "scope": ["app!t1.user"]},
    "developer": {"user_name": "d", "scope": ["app!t1.user", "app!t1.developer"]},
}


def role_app() -> Any:
    """The web chat routers with the principal, a JWT and the claims of the
    role in ``x-test-role`` bound per request."""
    inner = FastAPI()
    inner.include_router(routes.router)
    inner.include_router(my_routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        role = dict(scope["headers"]).get(b"x-test-role", b"user").decode()
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt-of-alice")
        c = auth.current_claims.set(ROLES[role])
        try:
            await inner(scope, receive, send)
        finally:
            auth.current_claims.reset(c)
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


def as_role(role: str) -> dict[str, dict[str, str]]:
    return {"headers": {"x-test-role": role}}


async def wipe() -> None:
    async with SessionLocal() as s:
        for model in (ChatFile, ChatMessage, ChatConversation, ChatUserAgent, ChatUserSkill,
                      AgentConfig, SkillConfig):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = FakeValidator(), True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "model-a,model-b")
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    monkeypatch.setattr(run, "HEARTBEAT_QUIET_SECONDS", 3600.0)
    await init_db()
    await wipe()
    yield
    await run.cancel_all_chat_runs()
    run._turns.clear()
    await wipe()
    auth._validator, auth._validator_checked = saved


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=role_app()),
                           base_url="http://test") as c:
        yield c


async def admin_agent(name: str, *, share: bool = True, enabled: bool = True,
                      tier: str = "basic", chat: bool = False,
                      mcp_url: str = "https://mcp.example.com/mcp",
                      extra: list[dict[str, Any]] | None = None) -> None:
    async with SessionLocal() as s:
        s.add(AgentConfig(
            name=name, description=f"The {name} agent.", instructions="x",
            mcp_url=mcp_url, auth_mode="jwt", enabled=1 if enabled else 0,
            expose_chat=1 if chat else 0, share_tools=1 if share else 0, tier=tier,
            extra_servers_json=json.dumps(extra) if extra else None))
        await s.commit()


async def set_admin(name: str, **values) -> None:
    async with SessionLocal() as s:
        row = (await s.execute(select(AgentConfig).where(AgentConfig.name == name))).scalar_one()
        for key, value in values.items():
            setattr(row, key, value)
        await s.commit()


def body(name: str = "Helper", **extra: Any) -> dict[str, Any]:
    return {"name": name, "instructions": "Help.", **extra}


def refusal(r) -> list[tuple[list, str]]:
    assert r.status_code == 422, r.text
    return [(d["loc"], d["msg"]) for d in r.json()["detail"]]


APPROVING = [{"url": "builtin:bitbucket", "auth_mode": "destination", "oauth": {
    "destination": "BITBUCKET", "workspace": "acme-ws",
    "allow_comment": True, "allow_approve": True}}]


# ---------------------------------------------------------------------------
# Save-time rules: POST and PUT
# ---------------------------------------------------------------------------
async def test_post_with_a_valid_source_stores_it(client):
    await admin_agent("jira-tools")
    r = await client.post(f"{API}/my/agents", json=body(tools=["jira-tools", "jira-tools"]))
    assert r.status_code == 201, r.text
    made = r.json()
    assert (made["tools"], made["tier"]) == (["jira-tools"], "basic")
    async with SessionLocal() as s:
        row = await s.get(ChatUserAgent, made["id"])
        assert json.loads(row.tools_json) == ["jira-tools"]
    listed = (await client.get(f"{API}/my/agents")).json()
    assert listed[0]["tools"] == ["jira-tools"]
    assert (await client.get(f"{API}/my/agents/{made['id']}")).json()["tools"] == ["jira-tools"]


@pytest.mark.parametrize("setup", ["unknown", "not_sharing", "disabled", "approving"])
async def test_a_name_that_is_no_tool_source_is_refused(client, setup):
    if setup == "not_sharing":
        await admin_agent("src", share=False, chat=True)
    elif setup == "disabled":
        await admin_agent("src", enabled=False)
    elif setup == "approving":
        await admin_agent("src")
        await set_admin("src", mcp_url="builtin:bitbucket", auth_mode="destination",
                        oauth_json=json.dumps(APPROVING[0]["oauth"]))
    r = await client.post(f"{API}/my/agents", json=body(tools=["src"]))
    assert refusal(r) == [(["body", "tools", 0], NOT_SOURCE)]
    assert "src" not in json.dumps(r.json())
    async with SessionLocal() as s:
        assert (await s.execute(select(ChatUserAgent))).first() is None


async def test_an_advanced_source_needs_the_developer_role(client):
    await admin_agent("abap-tools", tier="advanced")
    r = await client.post(f"{API}/my/agents", json=body(tools=["abap-tools"]),
                          **as_role("user"))
    assert refusal(r) == [(["body", "tools", 0], ADVANCED_TOOLS)]
    r = await client.post(f"{API}/my/agents", json=body(tools=["abap-tools"]),
                          **as_role("developer"))
    assert r.status_code == 201, r.text
    assert (r.json()["tools"], r.json()["tier"]) == (["abap-tools"], "advanced")
    # The list answers the computed tier as well.
    listed = (await client.get(f"{API}/my/agents", **as_role("user"))).json()
    assert listed[0]["tier"] == "advanced"


async def test_five_sources_at_most(client):
    for i in range(6):
        await admin_agent(f"src-{i}")
    r = await client.post(f"{API}/my/agents", json=body(tools=[f"src-{i}" for i in range(6)]))
    assert r.status_code == 422
    assert [d["loc"] for d in r.json()["detail"]] == [["body", "tools"]]
    r = await client.post(f"{API}/my/agents", json=body(tools=[f"src-{i}" for i in range(5)]))
    assert r.status_code == 201, r.text
    assert len(r.json()["tools"]) == 5


async def test_put_checks_the_same_rules(client):
    await admin_agent("src")
    await admin_agent("other", share=False)
    made = (await client.post(f"{API}/my/agents", json=body())).json()
    assert made["tools"] == []
    r = await client.put(f"{API}/my/agents/{made['id']}", json=body(tools=["src"]))
    assert r.status_code == 200 and r.json()["tools"] == ["src"]
    r = await client.put(f"{API}/my/agents/{made['id']}", json=body(tools=["src", "other"]))
    assert refusal(r) == [(["body", "tools", 1], NOT_SOURCE)]
    assert (await client.get(f"{API}/my/agents/{made['id']}")).json()["tools"] == ["src"]
    r = await client.put(f"{API}/my/agents/{made['id']}", json=body(tools=[3]))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Import and export
# ---------------------------------------------------------------------------
def bundle(*agents: dict[str, Any]) -> dict[str, Any]:
    return {"format": "webchat-bundle/1", "agents": list(agents), "skills": []}


async def test_import_drops_what_is_no_tool_source_and_writes_the_agent(client):
    await admin_agent("src")
    await admin_agent("abap-tools", tier="advanced")
    await admin_agent("quiet", share=False)
    r = await client.post(f"{API}/my/import", json=bundle(
        body("A", tools=["src", "gone", "quiet", "abap-tools"])), **as_role("user"))
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["agents"]["created"] == 1 and report["errors"] == []
    rule = NOT_SOURCE.removeprefix("Value error, ")
    assert report["dropped"] == [
        {"item": "agent:A", "field": "tools", "name": "gone", "rule": rule},
        {"item": "agent:A", "field": "tools", "name": "quiet", "rule": rule},
        {"item": "agent:A", "field": "tools", "name": "abap-tools",
         "rule": ADVANCED_TOOLS.removeprefix("Value error, ")},
    ]
    agents = (await client.get(f"{API}/my/agents")).json()
    assert [(a["name"], a["tools"]) for a in agents] == [("A", ["src"])]

    # A developer keeps the advanced source; a bundle without tools is fine.
    r = await client.post(f"{API}/my/import", json=bundle(
        body("B", tools=["abap-tools"]), body("C")), **as_role("developer"))
    assert r.status_code == 200 and r.json()["dropped"] == []
    by_name = {a["name"]: a for a in (await client.get(f"{API}/my/agents")).json()}
    assert by_name["B"]["tools"] == ["abap-tools"] and by_name["B"]["tier"] == "advanced"
    assert by_name["C"]["tools"] == []

    # Six names: an item error (too many), the agent is not written.
    r = await client.post(f"{API}/my/import", json=bundle(
        body("D", tools=[f"x{i}" for i in range(6)])))
    assert r.json()["errors"] == [{"item": "agent:D", "field": "tools",
                                   "rule": "too many entries"}]


async def test_export_carries_tools(client):
    await admin_agent("src")
    await client.post(f"{API}/my/agents", json=body(tools=["src"]))
    r = await client.get(f"{API}/my/export")
    assert json.loads(r.content)["agents"][0]["tools"] == ["src"]


# ---------------------------------------------------------------------------
# Catalogue and context
# ---------------------------------------------------------------------------
async def test_the_catalogue_lists_tool_sources_by_tier_with_connector_labels(client):
    await admin_agent("jira-tools", mcp_url=f"https://user:{SECRET}@mcp.example.com/sse?t={SECRET}",
                      extra=[{"url": "builtin:jira", "auth_mode": "destination",
                              "oauth": {"destination": f"JIRA_{SECRET}", "project": "P"}},
                             {"url": "https://mcp.example.com/other", "auth_mode": "jwt"}])
    await admin_agent("abap-tools", tier="advanced", mcp_url="https://arc1.example.org/mcp")
    await admin_agent("quiet", share=False, chat=True)
    await admin_agent("off", enabled=False)

    async def listed(role: str, query: str = "") -> list[dict[str, Any]]:
        r = await client.get(f"{API}/catalog{query}", **as_role(role))
        assert r.status_code == 200
        assert SECRET not in r.text
        return r.json()["tool_sources"]

    jira = {"name": "jira-tools", "description": "The jira-tools agent.", "tier": "basic",
            "connectors": ["mcp.example.com", "builtin:jira"]}
    abap = {"name": "abap-tools", "description": "The abap-tools agent.", "tier": "advanced",
            "connectors": ["arc1.example.org"]}
    assert await listed("user") == [jira]
    assert await listed("user", "?access=advanced") == [jira]
    assert await listed("developer") == [jira]
    assert await listed("developer", "?access=advanced") == [abap, jira]
    # A tool source need not be a chat specialist.
    agents = (await client.get(f"{API}/catalog", **as_role("user"))).json()["agents"]
    assert [a["name"] for a in agents] == ["quiet"]


async def test_the_context_lists_connector_entries(client):
    await admin_agent("jira-tools", extra=[{"url": "builtin:jira", "auth_mode": "destination",
                                            "oauth": {"destination": "J", "project": "P"}}])
    await admin_agent("abap-tools", tier="advanced", mcp_url="https://arc1.example.org/mcp")
    await admin_agent("later")
    r = await client.post(f"{API}/my/agents",
                          json=body(tools=["jira-tools", "abap-tools", "later"]),
                          **as_role("developer"))
    assert r.status_code == 201, r.text
    await set_admin("later", share_tools=0)
    r = await client.post(f"{API}/conversations",
                          json={"agent_ref": r.json()["ref"], "access": "advanced"},
                          **as_role("developer"))
    assert r.status_code == 201, r.text
    out = (await client.get(f"{API}/conversations/{r.json()['id']}/context",
                            **as_role("developer"))).json()
    assert out["agent"]["tier"] == "advanced"
    assert [t for t in out["tools"] if t["kind"] == "connector"] == [
        {"kind": "connector", "name": "mcp.example.com", "agent": "jira-tools"},
        {"kind": "connector", "name": "builtin:jira", "agent": "jira-tools"},
        {"kind": "connector", "name": "arc1.example.org", "agent": "abap-tools"},
    ]
    assert out["tools"][-1]["kind"] == "connector"


async def test_an_advanced_tool_source_makes_the_agent_unknown_in_a_basic_conversation(client):
    await admin_agent("abap-tools", tier="advanced")
    made = (await client.post(f"{API}/my/agents", json=body(tools=["abap-tools"]),
                              **as_role("developer"))).json()
    r = await client.post(f"{API}/conversations", json={"agent_ref": made["ref"]},
                          **as_role("developer"))
    assert (r.status_code, r.json()) == (422, {"detail": "Unknown agent"})


# ---------------------------------------------------------------------------
# Run time
# ---------------------------------------------------------------------------
class Recorder:
    """The default model: records the tool names of each request."""

    def __init__(self) -> None:
        self.tools: list[list[str]] = []

    async def stream(self, messages, info):
        self.tools.append(sorted(t.name for t in info.function_tools))
        yield "answer"


def shared_toolset(tool: str) -> FunctionToolset:
    toolset = FunctionToolset()

    def lookup() -> str:
        return "found"

    toolset.add_function(lookup, name=tool)
    return toolset


def frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def turn(client, cid: str) -> list[tuple[str, dict]]:
    r = await client.post(f"{API}/conversations/{cid}/messages",
                          files=[("text", (None, "Hello"))])
    assert r.status_code == 200, r.text
    return frames(r.text)


async def test_shared_tools_are_attached_and_a_source_that_stops_sharing_is_noted(
        client, monkeypatch, real_agents_and_mcp):
    model = Recorder()
    orchestrator = Agent(FunctionModel(stream_function=model.stream,
                                       model_name="orchestrator-model"))
    build = SimpleNamespace(specialists={}, in_flight=RunCounter(), shared_toolsets={
        "jira-tools": [shared_toolset("jira_search")],
        "notes": [shared_toolset("notes_list")],
    })
    monkeypatch.setattr(run, "registry",
                        SimpleNamespace(orchestrator=orchestrator, loaded=True, build=build))
    await admin_agent("jira-tools")
    await admin_agent("notes")
    made = (await client.post(f"{API}/my/agents",
                              json=body(tools=["jira-tools", "notes"]))).json()
    cid = (await client.post(f"{API}/conversations", json={"agent_ref": made["ref"]})).json()["id"]

    events = await turn(client, cid)
    # Two sources: each prefixed with its agent's name.
    assert {"jira_tools_jira_search", "notes_notes_list"} <= set(model.tools[-1])
    assert not [d for e, d in events if e == "message"]
    assert build.in_flight.value == 0

    await set_admin("notes", share_tools=0)
    events = await turn(client, cid)
    assert "jira_search" in model.tools[-1]
    assert not any("notes_list" in t for t in model.tools[-1])
    assert [d for e, d in events if e == "message"] == [
        {"text": "Skipped: 0 consult(s) / 0 skill(s) / 1 tool source(s) no longer available"}]


def test_skipped_note_keeps_the_old_text_without_tools():
    assert user_agents.skipped_note(1, 2) == (
        "Skipped: 1 consult(s) / 2 skill(s) no longer available")
    assert user_agents.skipped_note(0, 1, 2) == (
        "Skipped: 0 consult(s) / 1 skill(s) / 2 tool source(s) no longer available")


async def test_a_deep_personal_agent_hands_the_shared_tools_to_its_sub_agents(
        monkeypatch, real_agents_and_mcp):
    seen: dict[str, Any] = {}
    real = user_agents.deep_toolset

    def spy(config, *, parent_toolsets, **kwargs):
        seen["parents"] = list(parent_toolsets)
        return real(config, parent_toolsets=parent_toolsets, **kwargs)

    monkeypatch.setattr(user_agents, "deep_toolset", spy)
    await admin_agent("src")
    shared = shared_toolset("lookup_x")
    build = SimpleNamespace(specialists={}, in_flight=RunCounter(),
                            shared_toolsets={"src": [shared]})
    row = ChatUserAgent(principal=ALICE, name="Deep", name_key="deep", instructions="x",
                        deep=1, tools_json=json.dumps(["src"]))
    agent, note = await user_agents.build_personal_agent(
        row, model=FunctionModel(stream_function=Recorder().stream), build=build)
    assert note is None
    assert shared in seen["parents"] and len(seen["parents"]) == 2
    assert shared in agent._user_toolsets


async def test_an_advanced_source_is_not_attached_in_a_basic_turn(real_agents_and_mcp):
    from agents.access import current_access

    await admin_agent("abap-tools", tier="advanced")
    shared = shared_toolset("arc1")
    build = SimpleNamespace(specialists={}, in_flight=RunCounter(),
                            shared_toolsets={"abap-tools": [shared]})
    row = ChatUserAgent(principal=ALICE, name="A", name_key="a", instructions="x", deep=0,
                        tools_json=json.dumps(["abap-tools"]))
    model = FunctionModel(stream_function=Recorder().stream)
    token = current_access.set("basic")
    try:
        agent, note = await user_agents.build_personal_agent(row, model=model, build=build)
    finally:
        current_access.reset(token)
    assert shared not in agent._user_toolsets and "1 tool source(s)" in (note or "")
    token = current_access.set("advanced")
    try:
        agent, note = await user_agents.build_personal_agent(row, model=model, build=build)
    finally:
        current_access.reset(token)
    assert shared in agent._user_toolsets and note is None


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------
@pytest.fixture
def binding(monkeypatch):
    """A destination service binding by name; nothing is called."""
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_the_build_keeps_the_shared_tool_sets_and_none_of_an_approver(caplog):
    from pydantic_ai.models.test import TestModel

    from agents import registry as registry_module
    from agents.db import upsert_agent
    from agents.ide.readonly import ReadOnlyGuard

    notes = [{"url": "builtin:sapnotes", "auth_mode": "none"}]
    async with SessionLocal() as s:
        await upsert_agent(s, name="sharer", description="d", instructions="i",
                           mcp_servers=notes, share_tools=True)
        await upsert_agent(s, name="keeper", description="d", instructions="i",
                           mcp_servers=notes)
        await upsert_agent(s, name="approver", description="d", instructions="i",
                           mcp_servers=APPROVING, expose_chat=False)
        await upsert_agent(s, name="sleeper", description="d", instructions="i",
                           mcp_servers=notes, share_tools=True, enabled=False)
    # Past the gate and storage, as a row written by hand.
    await set_admin("approver", share_tools=1)
    caplog.set_level(logging.WARNING)
    real_get_model = registry_module.get_model
    registry_module.get_model = lambda *a, **k: TestModel()
    try:
        build = await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_get_model
    assert set(build.shared_toolsets) == {"sharer"}
    shared = build.shared_toolsets["sharer"]
    assert shared and all(isinstance(t, ReadOnlyGuard) for t in shared)
    # The same guarded servers the specialist got; not its function tools.
    assert all(t in build.specialists["sharer"]._user_toolsets for t in shared)
    told = [r for r in caplog.records if "shares nothing" in r.getMessage()]
    assert len(told) == 1 and "approver" in told[0].getMessage()
    assert "BITBUCKET" not in told[0].getMessage()


async def test_storage_refuses_sharing_on_an_approving_agent():
    from agents.db import BITBUCKET_APPROVER_SHARE_MESSAGE, upsert_agent

    async with SessionLocal() as s:
        with pytest.raises(ValueError) as refused:
            await upsert_agent(s, name="approver", description="d", instructions="i",
                               mcp_servers=APPROVING, expose_chat=False, share_tools=True)
    assert str(refused.value) == BITBUCKET_APPROVER_SHARE_MESSAGE
    async with SessionLocal() as s:
        row = await upsert_agent(s, name="plain", description="d", instructions="i",
                                 mcp_servers=[{"url": "builtin:sapnotes", "auth_mode": "none"}],
                                 share_tools="true")
        assert row.share_tools == 0
        row = await upsert_agent(s, name="plain", description="d", instructions="i",
                                 mcp_servers=[{"url": "builtin:sapnotes", "auth_mode": "none"}],
                                 share_tools=True)
        assert row.share_tools == 1 and row.to_dict()["share_tools"] is True
        # Not sent: kept.
        row = await upsert_agent(s, name="plain", description="e", instructions="i",
                                 mcp_servers=[{"url": "builtin:sapnotes", "auth_mode": "none"}])
        assert row.share_tools == 1
