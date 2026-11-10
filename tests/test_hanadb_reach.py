"""Task 20b: who can reach an agent with a ``builtin:hana`` entry.

The database user behind such an entry is a destination's technical user,
and on the first landscape it has wide privileges. The customer decided
(2026-11-10):

R1  An agent with a ``builtin:hana`` entry (with or without ``allow_select``)
    never lends its tools to personal agents: ``share_tools`` is false.
R2  An agent whose entry has ``allow_select`` (``asks_to_select``: anything
    but false / absent, so a hand-written ``"true"`` counts):
    a. is not exposed to the invoke API;
    b. is chat-exposed only with tier ``advanced``;
    c. is nobody's peer (it may have peers of its own).
    The scheduler run endpoint and Run now stay allowed.

Modelled on the approving ``builtin:bitbucket`` agent
(`tests/test_bitbucket_reach.py`): refused at the save gate, by storage (the
last gate: a script or a direct ``upsert_agent`` passes no payload) and, for a
row written by hand, closed again at registry build, there on the side that
keeps monitoring working: the HANA toolset is built without ``run_select``.

Run:  python -m pytest tests/test_hanadb_reach.py
"""

from __future__ import annotations

import json
import logging
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
from sqlalchemy import delete, select  # noqa: E402

URL = "builtin:hana"
DEST = "HANA_PRD_SECRET_DEST"
READING = {"systems": [{"name": "prd", "destination": DEST}]}
SELECTING = {**READING, "allow_select": True}
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}
AGENTS = "/admin/api/agents"
READ_TOOLS = {"list_systems", "list_checks", "run_check"}

SHARE_RULE = "share_tools: an agent with a builtin:hana entry cannot share its tools"
INVOKE_RULE = ("expose_invoke: must be false for an agent whose builtin:hana entry "
               "has allow_select")
CHAT_RULE = ("tier: must be advanced for a chat-exposed agent whose builtin:hana "
             "entry has allow_select")
PEER_RULE = "an agent that may run SELECT may not be a peer"


def entry(block: dict[str, Any], url: str = URL) -> dict[str, Any]:
    return {"url": url, "auth_mode": "destination", "oauth": json.loads(json.dumps(block))}


def body(name: str, *servers: dict, **patch: Any) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": list(servers), **patch}


def _said(exc: ValidationError) -> str:
    return json.dumps(exc.errors(include_input=False, include_context=False,
                                 include_url=False))


async def _wipe() -> None:
    from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db

    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        await s.commit()


@pytest.fixture
async def client():
    import app as app_module

    await _wipe()
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


async def _names() -> list[str]:
    from agents.db import SessionLocal, list_agents

    async with SessionLocal() as s:
        return sorted(r.name for r in await list_agents(s))


# --- the predicate ---------------------------------------------------------------

@pytest.mark.parametrize("cfg, expected", [
    (SELECTING, True), ({**READING, "allow_select": "true"}, True),
    ({**READING, "allow_select": 1}, True), ({**READING, "allow_select": "false"}, True),
    (READING, False), ({**READING, "allow_select": False}, False),
    ({**READING, "allow_select": None}, False), (None, False), ("x", False),
])
def test_asks_to_select_reads_anything_but_false_or_absent_as_selecting(cfg, expected):
    from agents.hana.config import asks_to_select

    assert asks_to_select(cfg) is expected


# --- the payload gate --------------------------------------------------------------

@pytest.mark.parametrize("block", [READING, SELECTING])
@pytest.mark.parametrize("url", [URL, "Builtin:HANA", " builtin:hana/ "])
def test_r1_the_gate_refuses_sharing_for_any_hana_entry(block, url):
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate(body("dba", entry(block, url), expose_chat=False,
                                         share_tools=True))
    said = _said(refused.value)
    assert SHARE_RULE in said and '"share_tools"' in said and DEST not in said


def test_r2a_the_gate_refuses_the_invoke_api_for_a_selecting_agent():
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate(body("dba", entry(SELECTING), expose_chat=False,
                                         expose_invoke=True, api_slug="dba"))
    said = _said(refused.value)
    assert INVOKE_RULE in said and DEST not in said


@pytest.mark.parametrize("patch", [
    {}, {"expose_chat": True}, {"tier": "basic"}, {"expose_chat": True, "tier": "basic"},
])
def test_r2b_the_gate_refuses_basic_chat_for_a_selecting_agent(patch):
    """Without the keys as well: ``expose_chat`` defaults to true, and an
    absent tier is not "advanced"."""
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate(body("dba", entry(SELECTING), **patch))
    said = _said(refused.value)
    assert CHAT_RULE in said and DEST not in said


@pytest.mark.parametrize("block, patch", [
    (SELECTING, {"expose_chat": False}),
    (SELECTING, {"expose_chat": False, "tier": "basic"}),
    (SELECTING, {"expose_chat": True, "tier": "advanced"}),
    (SELECTING, {"expose_chat": False, "expose_api": True, "api_slug": "dba"}),
    # Without allow_select a basic chat agent is fine: fixed checks only.
    (READING, {}), (READING, {"expose_chat": True, "tier": "basic"}),
    (READING, {"expose_chat": False, "expose_invoke": True, "api_slug": "dba"}),
    ({**READING, "allow_select": False}, {"expose_chat": True, "tier": "basic"}),
    (READING, {"share_tools": False}),
])
def test_the_gate_accepts_what_the_rules_allow(block, patch):
    from agents.admin import AgentPayload

    AgentPayload.model_validate(body("dba", entry(block), **patch))


def test_the_seed_hana_monitor_still_passes():
    from agents.admin import AgentPayload

    seed = json.loads((ROOT / "agents.seed.json").read_text(encoding="utf-8"))
    (agent,) = [a for a in seed["agents"] if a["name"] == "hana-monitor"]
    AgentPayload.model_validate(agent)
    # And with Allow SELECT turned on by an admin: it is advanced.
    turned_on = json.loads(json.dumps(agent))
    (server,) = [s for s in turned_on["mcp_servers"] if s["url"] == URL]
    server["oauth"]["allow_select"] = True
    AgentPayload.model_validate(turned_on)


# --- the API -------------------------------------------------------------------

async def test_r1_the_api_refuses_sharing_on_create_and_update(client):
    r = await client.post(AGENTS, json=body("dba", entry(READING), share_tools=True))
    assert r.status_code == 422 and SHARE_RULE in r.text and DEST not in r.text
    assert await _names() == []
    r = await client.post(AGENTS, json=body("dba", entry(READING)))
    assert r.status_code == 201, r.text
    dba_id = r.json()["id"]
    r = await client.put(f"{AGENTS}/{dba_id}",
                         json=body("dba", entry(READING), share_tools=True))
    assert r.status_code == 422 and SHARE_RULE in r.text
    # A sharing agent that is given a hana entry.
    r = await client.post(AGENTS, json=body("lender", NOTES, share_tools=True))
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}",
                         json=body("lender", NOTES, entry(READING)))
    assert r.status_code == 422 and SHARE_RULE in r.text
    listed = {a["name"]: a for a in (await client.get(AGENTS)).json()}
    assert listed["dba"]["share_tools"] is False
    assert len(listed["lender"]["mcp_servers"]) == 1


async def test_r2a_the_api_refuses_invoke_both_ways(client):
    r = await client.post(AGENTS, json=body("dba", entry(SELECTING), expose_chat=False,
                                            expose_invoke=True, api_slug="dba"))
    assert r.status_code == 422 and INVOKE_RULE in r.text
    r = await client.post(AGENTS, json=body("dba", entry(READING), expose_chat=False,
                                            expose_invoke=True, api_slug="dba"))
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}",
                         json=body("dba", entry(SELECTING), expose_chat=False,
                                   api_slug="dba"))
    assert r.status_code == 422 and INVOKE_RULE in r.text
    r = await client.post(AGENTS, json=body("sel", entry(SELECTING), expose_chat=False))
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}",
                         json=body("sel", entry(SELECTING), expose_chat=False,
                                   expose_invoke=True, api_slug="sel"))
    assert r.status_code == 422 and INVOKE_RULE in r.text


async def test_r2b_the_api_refuses_basic_chat_both_ways(client):
    r = await client.post(AGENTS, json=body("dba", entry(READING), tier="basic"))
    assert r.status_code == 201, r.text
    dba_id = r.json()["id"]
    # Allow SELECT on for a basic chat agent.
    r = await client.put(f"{AGENTS}/{dba_id}",
                         json=body("dba", entry(SELECTING), tier="basic"))
    assert r.status_code == 422 and CHAT_RULE in r.text
    # ... fine once it is advanced, or off chat.
    r = await client.put(f"{AGENTS}/{dba_id}",
                         json=body("dba", entry(SELECTING), tier="advanced"))
    assert r.status_code == 200, r.text
    # Back to basic, or chat on for a basic one that selects.
    r = await client.put(f"{AGENTS}/{dba_id}",
                         json=body("dba", entry(SELECTING), tier="basic"))
    assert r.status_code == 422 and CHAT_RULE in r.text
    r = await client.post(AGENTS, json=body("sel", entry(SELECTING), expose_chat=False,
                                            tier="basic"))
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}",
                         json=body("sel", entry(SELECTING), expose_chat=True,
                                   tier="basic"))
    assert r.status_code == 422 and CHAT_RULE in r.text


async def test_scheduled_and_advanced_chat_selecting_agents_are_stored(client):
    r = await client.post(AGENTS, json=body(
        "dba", entry(SELECTING), expose_chat=False, expose_api=True, api_slug="dba",
        run_prompt="Run the daily checks."))
    assert r.status_code == 201, r.text
    r = await client.post(AGENTS, json=body("chat-dba", entry(SELECTING), tier="advanced"))
    assert r.status_code == 201, r.text


async def test_r2c_no_agent_may_name_a_selecting_agent_as_a_peer(client):
    r = await client.post(AGENTS, json=body("dba", entry(SELECTING), expose_chat=False))
    assert r.status_code == 201, r.text
    r = await client.post(AGENTS, json=body("helper", NOTES, peers=["dba"]))
    assert r.status_code == 422 and PEER_RULE in r.text and "peers" in r.text
    assert await _names() == ["dba"]
    r = await client.post(AGENTS, json=body("helper", NOTES))
    assert r.status_code == 201, r.text
    helper_id = r.json()["id"]
    r = await client.put(f"{AGENTS}/{helper_id}", json=body("helper", NOTES, peers=["dba"]))
    assert r.status_code == 422 and PEER_RULE in r.text
    r = await client.put(f"{AGENTS}/{helper_id}", json=body("helper", NOTES, description="x"))
    assert r.status_code == 200, r.text


async def test_r2c_a_reading_agent_may_be_a_peer(client):
    r = await client.post(AGENTS, json=body("dba", entry(READING), expose_chat=False))
    assert r.status_code == 201, r.text
    r = await client.post(AGENTS, json=body("helper", NOTES, peers=["dba"]))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("helper_enabled", [True, False])
async def test_r2c_an_agent_that_is_a_peer_cannot_be_given_select(client, helper_enabled):
    r = await client.post(AGENTS, json=body("dba", entry(READING), expose_chat=False))
    assert r.status_code == 201, r.text
    dba_id = r.json()["id"]
    r = await client.post(AGENTS, json=body("helper", NOTES, peers=["dba"],
                                            enabled=helper_enabled))
    assert r.status_code == 201, r.text
    selecting = body("dba", entry(SELECTING), expose_chat=False)
    for send in (client.put(f"{AGENTS}/{dba_id}", json=selecting),
                 client.post(AGENTS, json=selecting),
                 client.put(f"{AGENTS}/{dba_id}", json={**selecting, "name": "dba2"})):
        r = await send
        assert r.status_code == 422, r.text
        assert PEER_RULE in r.text and "'helper'" in r.text and DEST not in r.text
    helper_id = [a for a in (await client.get(AGENTS)).json() if a["name"] == "helper"][0]["id"]
    r = await client.put(f"{AGENTS}/{helper_id}", json=body("helper", NOTES, peers=[]))
    assert r.status_code == 200, r.text
    r = await client.put(f"{AGENTS}/{dba_id}", json=selecting)
    assert r.status_code == 200, r.text


async def test_r2c_a_selecting_agent_may_have_peers_of_its_own(client):
    r = await client.post(AGENTS, json=body("helper", NOTES))
    assert r.status_code == 201, r.text
    r = await client.post(AGENTS, json=body("dba", entry(SELECTING), expose_chat=False,
                                            peers=["helper"]))
    assert r.status_code == 201, r.text


# --- the import ----------------------------------------------------------------

async def test_an_import_is_held_to_the_rules_whatever_the_order(client):
    dba = body("dba", entry(SELECTING), expose_chat=False)
    helper = body("helper", NOTES, peers=["dba"])
    for agents in ([dba, helper], [helper, dba]):
        r = await client.post("/admin/api/import", json={"agents": agents})
        assert r.status_code == 422, r.text
        assert PEER_RULE in r.text
        assert await _names() == []
    for bad, rule in ((body("dba", entry(SELECTING)), CHAT_RULE),
                      (body("dba", entry(READING), share_tools=True), SHARE_RULE),
                      (body("dba", entry(SELECTING), expose_chat=False,
                            expose_invoke=True, api_slug="dba"), INVOKE_RULE)):
        r = await client.post("/admin/api/import", json={"agents": [bad]})
        assert r.status_code == 422 and rule in r.text, r.text
    r = await client.post("/admin/api/import", json={"agents": [dba, body("helper", NOTES)]})
    assert r.status_code == 200, r.text
    assert await _names() == ["dba", "helper"]


async def test_an_import_may_drop_the_peer_and_turn_select_on_in_one_bundle(client):
    r = await client.post("/admin/api/import", json={"agents": [
        body("dba", entry(READING), expose_chat=False),
        body("helper", NOTES, peers=["dba"])]})
    assert r.status_code == 200, r.text
    dba = body("dba", entry(SELECTING), expose_chat=False)
    r = await client.post("/admin/api/import", json={"agents": [dba, body("helper", NOTES)]})
    assert r.status_code == 422 and PEER_RULE in r.text
    for agents in ([dba, body("helper", NOTES, peers=[])],
                   [body("helper", NOTES, peers=[]), dba]):
        r = await client.post("/admin/api/import", json={"agents": agents})
        assert r.status_code == 200, r.text


# --- storage, reached without the payload ---------------------------------------

async def test_a_direct_upsert_is_held_to_every_rule():
    from agents.db import SessionLocal, upsert_agent

    await _wipe()
    kw = {"description": "d", "instructions": "i"}
    async with SessionLocal() as s:
        with pytest.raises(ValueError, match="^tier: must be advanced"):
            await upsert_agent(s, name="dba", mcp_servers=[entry(SELECTING)], **kw)
        await s.rollback()
        with pytest.raises(ValueError, match="^share_tools: an agent with a builtin:hana"):
            await upsert_agent(s, name="dba", mcp_servers=[NOTES, entry(READING)],
                               share_tools=True, **kw)
        await s.rollback()
        with pytest.raises(ValueError, match="^expose_invoke: must be false"):
            await upsert_agent(s, name="dba", mcp_servers=[entry(SELECTING)],
                               expose_chat=False, expose_invoke=True, api_slug="dba", **kw)
        await s.rollback()
        # A hand-written "true" counts as selecting.
        with pytest.raises(ValueError):
            await upsert_agent(s, name="dba", mcp_servers=[entry(
                {**READING, "allow_select": "true"})], **kw)
        await s.rollback()
        await upsert_agent(s, name="dba", mcp_servers=[entry(SELECTING)],
                           tier="advanced", **kw)
        await upsert_agent(s, name="reader", mcp_servers=[entry(READING)], **kw)
        with pytest.raises(ValueError, match=PEER_RULE):
            await upsert_agent(s, name="helper", mcp_servers=[NOTES], peers=["dba"], **kw)
        await s.rollback()
    assert await _names() == ["dba", "reader"]


async def test_a_kept_tier_is_the_stored_one():
    """An update that sends no tier keeps it, and the rule judges the kept one."""
    from agents.db import SessionLocal, upsert_agent

    await _wipe()
    kw = {"description": "d", "instructions": "i"}
    async with SessionLocal() as s:
        await upsert_agent(s, name="dba", mcp_servers=[entry(READING)], tier="advanced", **kw)
        await upsert_agent(s, name="dba", mcp_servers=[entry(SELECTING)], **kw)
        await upsert_agent(s, name="basic", mcp_servers=[entry(READING)], **kw)
        with pytest.raises(ValueError, match="^tier: must be advanced"):
            await upsert_agent(s, name="basic", mcp_servers=[entry(SELECTING)], **kw)
        await s.rollback()


# --- the registry build: rows nobody checked ------------------------------------

async def _build(agents: list[dict[str, Any]], raw: dict[str, dict[str, Any]]):
    from pydantic_ai.models.test import TestModel

    from agents import registry as registry_module
    from agents.db import AgentConfig, SessionLocal, upsert_agent

    await _wipe()
    async with SessionLocal() as s:
        for agent in agents:
            await upsert_agent(s, **agent)
        await s.commit()
        for name, columns in raw.items():
            row = (await s.execute(
                select(AgentConfig).where(AgentConfig.name == name))).scalar_one()
            for column, value in columns.items():
                setattr(row, column, value)
        await s.commit()
    real_get_model = registry_module.get_model
    registry_module.get_model = lambda *a, **k: TestModel()
    try:
        return await registry_module.build_orchestrator()
    finally:
        registry_module.get_model = real_get_model


async def _tool_names(agent) -> set[str]:
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    from agents.ide.readonly import ReadOnlyGuard

    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    names: set[str] = set()
    for toolset in getattr(agent, "_user_toolsets", ()) or ():
        if isinstance(toolset, ReadOnlyGuard):
            names |= set(await toolset.get_tools(ctx))
    return names


def _delegations(agent) -> set[str]:
    names = set(agent._function_toolset.tools)
    return {n.removeprefix("delegate_") for n in names if n.startswith("delegate_")}


def _agent(name: str, *servers: dict, **patch: Any) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": list(servers), **patch}


def _warnings(caplog) -> str:
    return "\n".join(caplog.handler.format(r) for r in caplog.records
                     if r.name == "agents.registry" and r.levelno == logging.WARNING)


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_build_drops_run_select_for_a_row_that_breaks_r2a_or_r2b(caplog):
    caplog.set_level(logging.INFO)
    build = await _build(
        [_agent("basic_chat", entry(SELECTING), expose_chat=False),
         _agent("invoked", entry(SELECTING), expose_chat=False),
         _agent("advanced_chat", entry(SELECTING), tier="advanced"),
         _agent("scheduled", entry(SELECTING), expose_chat=False, expose_api=True,
                api_slug="scheduled")],
        raw={"basic_chat": {"expose_chat": 1},
             "invoked": {"expose_invoke": 1, "api_slug": "invoked"}})
    assert await _tool_names(build.specialists["basic_chat"]) == READ_TOOLS
    assert await _tool_names(build.specialists["invoked"]) == READ_TOOLS
    assert await _tool_names(build.specialists["advanced_chat"]) == READ_TOOLS | {"run_select"}
    assert await _tool_names(build.specialists["scheduled"]) == READ_TOOLS | {"run_select"}
    said = _warnings(caplog)
    assert said.count("is built without run_select") == 2
    assert "'basic_chat'" in said and "'invoked'" in said
    assert DEST not in said and "prd" not in said


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_build_drops_run_select_for_a_hand_written_true(caplog):
    caplog.set_level(logging.INFO)
    build = await _build(
        [_agent("dba", entry(READING), tier="basic")],
        raw={"dba": {"oauth_json": json.dumps({**READING, "allow_select": "true"})}})
    assert await _tool_names(build.specialists["dba"]) == READ_TOOLS
    assert "is built without run_select" in _warnings(caplog)


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_build_shares_nothing_of_a_row_with_a_hana_entry(caplog):
    caplog.set_level(logging.INFO)
    build = await _build(
        [_agent("dba", entry(READING), NOTES), _agent("lender", NOTES, share_tools=True)],
        raw={"dba": {"share_tools": 1}})
    assert "dba" in build.specialists
    assert "dba" not in build.shared_toolsets and "lender" in build.shared_toolsets
    said = _warnings(caplog)
    assert said.count("shares nothing with personal agents") == 1
    assert "'dba'" in said and DEST not in said


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_build_attaches_no_delegation_tool_to_a_selecting_agent(caplog):
    caplog.set_level(logging.INFO)
    build = await _build(
        [_agent("dba", entry(SELECTING), expose_chat=False),
         _agent("reader", entry(READING), expose_chat=False),
         _agent("helper", NOTES)],
        raw={"helper": {"peers_json": json.dumps(["dba", "reader"])}})
    assert _delegations(build.specialists["helper"]) == {"reader"}
    assert "dba" not in _delegations(build.orchestrator)
    # The selecting agent itself is built whole: the scheduler runs it.
    assert await _tool_names(build.specialists["dba"]) == READ_TOOLS | {"run_select"}
    said = _warnings(caplog)
    assert said.count("no delegation tool is attached") == 1
    assert "'helper'" in said and "'dba'" in said and DEST not in said


# --- the invoke API: the last line ---------------------------------------------

async def test_a_hand_written_selecting_row_is_404_at_the_invoke_route():
    from fastapi import FastAPI

    from agents import invoke
    from agents.auth import current_jwt, current_principal
    from agents.db import AgentConfig, SessionLocal

    await _wipe()
    async with SessionLocal() as s:
        s.add(AgentConfig(name="dba", description="d", instructions="i",
                          mcp_url=URL, auth_mode="destination",
                          oauth_json=json.dumps(SELECTING), enabled=1,
                          expose_chat=0, expose_invoke=1, api_slug="dba"))
        await s.commit()
    inner = FastAPI()
    inner.include_router(invoke.router)

    async def app(scope, receive, send):
        # The real app's middleware binds these from the validated JWT.
        p = current_principal.set("alice@example.com")
        j = current_jwt.set("jwt-of-alice")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/invoke/dba", files={"input": (None, "hello")})
    assert r.status_code == 404 and r.json() == {"detail": "No such agent"}
