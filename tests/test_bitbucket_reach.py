"""Final review M3: who can make an approving agent run.

An agent whose ``builtin:bitbucket`` entry has ``allow_approve: true`` approves
as a technical user whose approval counts for merging. It must run only where
nobody chooses its prompt: the scheduler run endpoint and an admin's Run now
(both use the stored run prompt). So it may not be exposed to chat (which is
also how A2A reaches an agent: both talk to the orchestrator) and no agent may
delegate to it as a peer. Refused at the save gate, by storage (the last gate:
a script or a direct ``upsert_agent`` passes no payload) and, because a row can
be written by hand and in any order, closed again at registry build.

Run:  python -m pytest tests/test_bitbucket_reach.py
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

URL = "builtin:bitbucket"
BASE = {"destination": "BITBUCKET", "workspace": "acme-ws"}
COMMENTING = {**BASE, "allow_comment": True}
APPROVING = {**COMMENTING, "allow_approve": True}
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}
AGENTS = "/admin/api/agents"
READ_TOOLS = {"list_pull_requests", "get_pull_request", "get_diff", "get_file"}
WRITE_TOOLS = {"add_inline_comment", "submit_review", "complete_approval"}

CHAT_RULE = ("expose_chat: must be false for an agent whose builtin:bitbucket entry "
             "has allow_approve")
PEER_RULE = "an approving agent may not be a peer"


def entry(block: dict[str, Any], url: str = URL) -> dict[str, Any]:
    return {"url": url, "auth_mode": "destination", "oauth": block}


def body(name: str, *servers: dict, **patch: Any) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": list(servers), **patch}


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


# --- the payload gate ----------------------------------------------------------

@pytest.mark.parametrize("patch", [{}, {"expose_chat": True}])
@pytest.mark.parametrize("url", [URL, "Builtin:Bitbucket", " builtin:bitbucket/ "])
def test_the_gate_refuses_an_approving_agent_that_is_exposed_to_chat(patch, url):
    """Without the key as well: ``expose_chat`` defaults to true."""
    from agents.admin import AgentPayload

    with pytest.raises(ValidationError) as refused:
        AgentPayload.model_validate(body("reviewer", entry(APPROVING, url), **patch))
    said = json.dumps(refused.value.errors(include_input=False, include_context=False,
                                           include_url=False))
    assert CHAT_RULE in said and "acme-ws" not in said


@pytest.mark.parametrize("block, patch", [
    (APPROVING, {"expose_chat": False}),
    (APPROVING, {"expose_chat": False, "expose_api": True, "api_slug": "pr-reviewer"}),
    (COMMENTING, {}), (COMMENTING, {"expose_chat": True}),
    ({**COMMENTING, "allow_approve": False}, {"expose_chat": True}),
    (BASE, {}),
])
def test_the_gate_accepts_what_cannot_approve_and_the_scheduler_agent(block, patch):
    from agents.admin import AgentPayload

    AgentPayload.model_validate(body("reviewer", entry(block), **patch))


def test_the_seed_reviewer_still_passes_and_is_not_chat_exposed():
    from agents.admin import AgentPayload

    seed = json.loads((ROOT / "agents.seed.json").read_text(encoding="utf-8"))
    (agent,) = [a for a in seed["agents"] if a["name"] == "pr-reviewer"]
    assert agent["expose_chat"] is False
    AgentPayload.model_validate(agent)
    # And it would still pass with both write switches turned on by an admin.
    turned_on = json.loads(json.dumps(agent))
    turned_on["mcp_servers"][0]["oauth"].update(allow_comment=True, allow_approve=True)
    AgentPayload.model_validate(turned_on)


# --- the API -------------------------------------------------------------------

async def test_the_api_refuses_chat_exposure_on_create_and_update(client):
    r = await client.post(AGENTS, json=body("reviewer", entry(APPROVING)))
    assert r.status_code == 422 and CHAT_RULE in r.text
    assert await _names() == []
    r = await client.post(AGENTS, json=body("reviewer", entry(APPROVING), expose_chat=False))
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    # Turning chat on for the approving agent, or approving on for a chat agent.
    r = await client.put(f"{AGENTS}/{agent_id}",
                         json=body("reviewer", entry(APPROVING), expose_chat=True))
    assert r.status_code == 422 and CHAT_RULE in r.text
    r = await client.post(AGENTS, json=body("chatty", entry(COMMENTING)))
    assert r.status_code == 201, r.text
    r = await client.put(f"{AGENTS}/{r.json()['id']}", json=body("chatty", entry(APPROVING)))
    assert r.status_code == 422 and CHAT_RULE in r.text
    listed = {a["name"]: a for a in (await client.get(AGENTS)).json()}
    assert listed["reviewer"]["expose_chat"] is False
    assert "allow_approve" not in listed["chatty"]["mcp_servers"][0]["oauth"]


async def test_the_scheduler_agent_with_an_api_slug_is_stored(client):
    r = await client.post(AGENTS, json=body(
        "reviewer", entry(APPROVING), expose_chat=False, expose_api=True,
        api_slug="pr-reviewer", run_prompt="Review the open pull requests."))
    assert r.status_code == 201, r.text
    assert r.json()["expose_api"] is True and r.json()["api_slug"] == "pr-reviewer"


async def test_no_agent_may_name_an_approving_agent_as_a_peer(client):
    r = await client.post(AGENTS, json=body("reviewer", entry(APPROVING), expose_chat=False))
    assert r.status_code == 201, r.text
    # Create and update of the agent that would delegate.
    r = await client.post(AGENTS, json=body("helper", NOTES, peers=["reviewer"]))
    assert r.status_code == 422 and PEER_RULE in r.text and "peers" in r.text
    assert await _names() == ["reviewer"]
    r = await client.post(AGENTS, json=body("helper", NOTES))
    assert r.status_code == 201, r.text
    helper_id = r.json()["id"]
    r = await client.put(f"{AGENTS}/{helper_id}", json=body("helper", NOTES, peers=["reviewer"]))
    assert r.status_code == 422 and PEER_RULE in r.text
    # A save that carries no peers key keeps the stored list: nothing to refuse.
    r = await client.put(f"{AGENTS}/{helper_id}", json=body("helper", NOTES, description="x"))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("helper_enabled", [True, False])
async def test_an_agent_that_is_a_peer_cannot_be_given_the_approval(client, helper_enabled):
    """Also while the agent that lists it is disabled: enabling that one
    later touches neither the peer list nor the approving agent."""
    r = await client.post(AGENTS, json=body("reviewer", entry(COMMENTING), expose_chat=False))
    assert r.status_code == 201, r.text
    reviewer_id = r.json()["id"]
    r = await client.post(AGENTS, json=body("helper", NOTES, peers=["reviewer"],
                                            enabled=helper_enabled))
    assert r.status_code == 201, r.text
    approving = body("reviewer", entry(APPROVING), expose_chat=False)
    for send in (client.put(f"{AGENTS}/{reviewer_id}", json=approving),
                 client.post(AGENTS, json=approving),
                 # Renamed in the same save: the peer lists follow the rename.
                 client.put(f"{AGENTS}/{reviewer_id}", json={**approving, "name": "approver"})):
        r = await send
        assert r.status_code == 422, r.text
        assert PEER_RULE in r.text and "'helper'" in r.text and "acme-ws" not in r.text
    assert await _names() == ["helper", "reviewer"]
    # Once nobody lists it, it may approve.
    helper_id = [a for a in (await client.get(AGENTS)).json() if a["name"] == "helper"][0]["id"]
    r = await client.put(f"{AGENTS}/{helper_id}", json=body("helper", NOTES, peers=[]))
    assert r.status_code == 200, r.text
    r = await client.put(f"{AGENTS}/{reviewer_id}", json=approving)
    assert r.status_code == 200, r.text


async def test_an_approving_agent_may_have_peers_of_its_own(client):
    """Only the first half is the rule: nobody delegates TO it."""
    r = await client.post(AGENTS, json=body("helper", NOTES))
    assert r.status_code == 201, r.text
    r = await client.post(AGENTS, json=body("reviewer", entry(APPROVING), expose_chat=False,
                                            peers=["helper"]))
    assert r.status_code == 201, r.text


# --- the import ----------------------------------------------------------------

async def test_an_import_is_held_to_the_rules_whatever_the_order(client):
    reviewer = body("reviewer", entry(APPROVING), expose_chat=False)
    helper = body("helper", NOTES, peers=["reviewer"])
    for agents in ([reviewer, helper], [helper, reviewer]):
        r = await client.post("/admin/api/import", json={"agents": agents})
        assert r.status_code == 422, r.text
        assert PEER_RULE in r.text
        assert await _names() == []          # one transaction: nothing was stored
    r = await client.post("/admin/api/import",
                          json={"agents": [body("reviewer", entry(APPROVING))]})
    assert r.status_code == 422 and CHAT_RULE in r.text
    r = await client.post("/admin/api/import",
                          json={"agents": [reviewer, body("helper", NOTES)]})
    assert r.status_code == 200, r.text
    assert await _names() == ["helper", "reviewer"]


async def test_an_import_may_drop_the_peer_and_turn_approving_on_in_one_bundle(client):
    r = await client.post("/admin/api/import", json={"agents": [
        body("reviewer", entry(COMMENTING), expose_chat=False),
        body("helper", NOTES, peers=["reviewer"])]})
    assert r.status_code == 200, r.text
    reviewer = body("reviewer", entry(APPROVING), expose_chat=False)
    # Not while the bundle leaves the peer list alone (no key: kept) ...
    r = await client.post("/admin/api/import",
                          json={"agents": [reviewer, body("helper", NOTES)]})
    assert r.status_code == 422 and PEER_RULE in r.text
    # ... but in either order once the bundle empties it, or removes the lister.
    for agents in ([reviewer, body("helper", NOTES, peers=[])],):
        r = await client.post("/admin/api/import", json={"agents": agents})
        assert r.status_code == 200, r.text


async def test_a_replace_import_that_removes_the_lister_may_turn_approving_on(client):
    r = await client.post("/admin/api/import", json={"agents": [
        body("reviewer", entry(COMMENTING), expose_chat=False),
        body("helper", NOTES, peers=["reviewer"])]})
    assert r.status_code == 200, r.text
    r = await client.post("/admin/api/import", json={"replace": True, "agents": [
        body("reviewer", entry(APPROVING), expose_chat=False)]})
    assert r.status_code == 200, r.text
    assert await _names() == ["reviewer"]


# --- storage, reached without the payload ---------------------------------------

async def test_a_direct_upsert_is_held_to_both_rules():
    from agents.db import SessionLocal, upsert_agent

    await _wipe()
    kw = {"description": "d", "instructions": "i"}
    async with SessionLocal() as s:
        with pytest.raises(ValueError, match="expose_chat: must be false"):
            await upsert_agent(s, name="reviewer", mcp_servers=[entry(APPROVING)], **kw)
        await s.rollback()
        with pytest.raises(ValueError, match="expose_chat: must be false"):
            await upsert_agent(s, name="reviewer", mcp_servers=[NOTES, entry(APPROVING)],
                               expose_chat=True, **kw)
        await s.rollback()
        await upsert_agent(s, name="reviewer", mcp_servers=[entry(APPROVING)],
                           expose_chat=False, **kw)
        with pytest.raises(ValueError, match=PEER_RULE):
            await upsert_agent(s, name="helper", mcp_servers=[NOTES], peers=["reviewer"], **kw)
        await s.rollback()
    assert await _names() == ["reviewer"]


# --- the registry build: rows nobody checked ------------------------------------

@pytest.fixture
def binding(monkeypatch):
    monkeypatch.setenv("DESTINATION_CLIENT_ID", "cid")
    monkeypatch.setenv("DESTINATION_CLIENT_SECRET", "placeholder")
    monkeypatch.setenv("DESTINATION_URI", "https://destination.example.com")
    monkeypatch.setenv("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")


async def _build(agents: list[dict[str, Any]], raw: dict[str, dict[str, Any]]):
    """A registry build after ``raw`` (agent name -> column values) was
    written into the rows directly, past gate and storage."""
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
    """The delegation tools registered on an agent (or the orchestrator)."""
    names = set(agent._function_toolset.tools)
    assert all(n.startswith("delegate_") for n in names if n != "load_skill"), names
    return {n.removeprefix("delegate_") for n in names}


def _agent(name: str, *servers: dict, **patch: Any) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": list(servers), **patch}


def _warnings(caplog) -> str:
    return "\n".join(caplog.handler.format(r) for r in caplog.records
                     if r.name == "agents.registry" and r.levelno == logging.WARNING)


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_build_gives_a_chat_exposed_approving_row_no_bitbucket_toolset(caplog):
    caplog.set_level(logging.INFO)
    build = await _build(
        [_agent("reviewer", entry(APPROVING), NOTES, expose_chat=False),
         _agent("only", entry(APPROVING), expose_chat=False),
         _agent("scheduled", entry(APPROVING), expose_chat=False),
         _agent("commenter", entry(COMMENTING))],
        raw={"reviewer": {"expose_chat": 1}, "only": {"expose_chat": 1}})
    # The row keeps its other server and has no Bitbucket tool at all ...
    names = await _tool_names(build.specialists["reviewer"])
    assert names and not any(n.endswith(tuple(READ_TOOLS | WRITE_TOOLS)) for n in names)
    # ... a row with nothing else is not built ...
    assert "only" not in build.specialists
    # ... and the rows the rule does not concern are built as before.
    assert await _tool_names(build.specialists["scheduled"]) == READ_TOOLS | WRITE_TOOLS
    assert "submit_review" in await _tool_names(build.specialists["commenter"])
    assert "scheduled" not in _delegations(build.orchestrator)
    said = _warnings(caplog)
    assert said.count("is exposed to chat and its builtin:bitbucket entry approves") == 2
    assert "'reviewer'" in said and "'only'" in said and "acme-ws" not in said


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_build_attaches_no_delegation_tool_to_an_approving_agent(caplog):
    caplog.set_level(logging.INFO)
    build = await _build(
        [_agent("reviewer", entry(APPROVING), expose_chat=False),
         _agent("commenter", entry(COMMENTING), expose_chat=False),
         _agent("helper", NOTES)],
        raw={"helper": {"peers_json": json.dumps(["reviewer", "commenter"])}})
    assert _delegations(build.specialists["helper"]) >= {"commenter"}
    assert "reviewer" not in _delegations(build.specialists["helper"])
    assert "reviewer" not in _delegations(build.orchestrator)
    # The approving agent itself is built whole: the scheduler runs it.
    assert await _tool_names(build.specialists["reviewer"]) == READ_TOOLS | WRITE_TOOLS
    said = _warnings(caplog)
    assert said.count("no delegation tool is attached") == 1
    assert "'helper'" in said and "'reviewer'" in said and "acme-ws" not in said


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
@pytest.mark.parametrize("value", ["true", 1, "yes"])
async def test_a_hand_written_switch_counts_as_approving_for_the_build(value, caplog):
    """The toolset refuses such a block anyway (no tool); the reach rules read
    it as approving rather than as "not exactly true"."""
    caplog.set_level(logging.INFO)
    block = {**COMMENTING, "allow_approve": value}
    build = await _build(
        [_agent("reviewer", entry(COMMENTING), NOTES, expose_chat=False),
         _agent("helper", NOTES)],
        raw={"reviewer": {"oauth_json": json.dumps(block)},
             "helper": {"peers_json": json.dumps(["reviewer"])}})
    assert "reviewer" in build.specialists and "helper" in build.specialists
    assert "reviewer" not in _delegations(build.specialists["helper"])
    assert "no delegation tool is attached" in _warnings(caplog)


@pytest.mark.usefixtures("real_agents_and_mcp", "binding")
async def test_a_build_still_attaches_the_peers_and_chat_agents_the_rule_does_not_concern():
    build = await _build(
        [_agent("commenter", entry(COMMENTING)), _agent("helper", NOTES, peers=["commenter"])],
        raw={})
    assert _delegations(build.specialists["helper"]) == {"commenter"}
    assert _delegations(build.orchestrator) == {"commenter", "helper"}
