"""Referential integrity and atomicity of the admin API, plus reload safety.

Covers: delegation-tool name collisions (two enabled agents that sanitize to
one tool freeze the registry), agent rename/disable/delete against peers and
workflow steps, the import as one transaction with redacted secrets kept,
the api_slug charset, the age-based workflow-run sweep, and the registry
keeping a retired build's MCP clients open while a delegation is running.

Each test creates what it needs under an ``ri-`` prefix and removes it, so
it can share a database with the other pytest modules.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

from httpx import ASGITransport, AsyncClient  # noqa: E402

import agents.registry as registry_module  # noqa: E402
import app as app_module  # noqa: E402
from agents.db import (  # noqa: E402
    SessionLocal,
    Workflow,
    WorkflowRun,
    create_workflow_run,
    get_agent_by_name,
    get_workflow_by_name,
    init_db,
    sweep_stale_workflow_runs,
    upsert_workflow,
)

SERVERS = [{"url": "https://ri.example.com/mcp", "auth_mode": "none"}]
BTP_URL = "https://ri-secret.cfapps.eu20-001.hana.ondemand.com/mcp"


def agent(name: str, **extra) -> dict:
    return {"name": name, "description": f"{name} d", "instructions": f"{name} i",
            "mcp_servers": SERVERS, **extra}


@pytest.fixture
async def client():
    await init_db()
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
        # Remove everything this module created, workflows first.
        r = await c.get("/admin/api/workflows")
        for w in r.json():
            if w["name"].startswith("ri-"):
                await c.delete(f"/admin/api/workflows/{w['id']}")
        r = await c.get("/admin/api/agents")
        for a in r.json():
            if a["name"].lower().replace(" ", "-").startswith("ri-"):
                await c.delete(f"/admin/api/agents/{a['id']}?force=true")


async def _ids(c: AsyncClient) -> dict[str, int]:
    return {a["name"]: a["id"] for a in (await c.get("/admin/api/agents")).json()}


# --- H5: delegation tool name collisions -----------------------------------------
async def test_colliding_agent_names_are_refused(client):
    r = await client.post("/admin/api/agents", json=agent("RI Foo Bar"))
    assert r.status_code == 201, r.text
    before = len(await _ids(client))

    r = await client.post("/admin/api/agents", json=agent("ri_foo_bar"))
    assert r.status_code == 422, r.text
    assert "delegate_ri_foo_bar" in r.text and "RI Foo Bar" in r.text

    # A padded spelling is the same row, not a second one.
    r = await client.post("/admin/api/agents", json=agent("  RI Foo Bar "))
    assert r.status_code == 201 and r.json()["name"] == "RI Foo Bar"
    assert len(await _ids(client)) == before

    # A disabled agent registers no tool, so it may exist -- until enabled.
    r = await client.post("/admin/api/agents", json=agent("ri-foo-bar", enabled=False))
    assert r.status_code == 201, r.text
    dis_id = r.json()["id"]
    r = await client.put(f"/admin/api/agents/{dis_id}", json=agent("ri-foo-bar", enabled=True))
    assert r.status_code == 422 and "delegation tool" in r.text

    # Renaming onto a collision is refused too.
    r = await client.post("/admin/api/agents", json=agent("ri-other"))
    other_id = r.json()["id"]
    r = await client.put(f"/admin/api/agents/{other_id}", json=agent("RI-FOO-BAR"))
    assert r.status_code == 422 and "delegation tool" in r.text

    # And the import path names the offending agent.
    r = await client.post("/admin/api/import", json={"agents": [agent("RI-FOO_BAR")]})
    assert r.status_code == 422, r.text
    assert "Agent 'RI-FOO_BAR'" in r.json()["detail"]


# --- M9: rename / disable / delete keep peers and workflow steps consistent ------
async def test_rename_updates_peers_and_steps_then_delete_is_guarded(client):
    for body in (agent("ri-target"), agent("ri-caller", peers=["ri-target"])):
        assert (await client.post("/admin/api/agents", json=body)).status_code == 201
    r = await client.post("/admin/api/workflows", json={
        "name": "ri-wf", "steps": [{"branch_key": None, "position": 1,
                                     "agent_name": "ri-target", "instructions": "x"}],
    })
    assert r.status_code == 201, r.text
    wf_id = r.json()["id"]
    ids = await _ids(client)

    # Rename follows through to the peer list and the step.
    r = await client.put(f"/admin/api/agents/{ids['ri-target']}", json=agent("ri-target-2"))
    assert r.status_code == 200, r.text
    caller = (await client.get(f"/admin/api/agents/{ids['ri-caller']}")).json()
    assert caller["peers"] == ["ri-target-2"]
    wf = (await client.get(f"/admin/api/workflows/{wf_id}")).json()
    assert wf["steps"][0]["agent_name"] == "ri-target-2"

    # Disabling a referenced agent is refused, naming the referrers.
    r = await client.put(f"/admin/api/agents/{ids['ri-target']}",
                         json=agent("ri-target-2", enabled=False))
    assert r.status_code == 422, r.text
    assert "'ri-caller'" in r.text and "'ri-wf'" in r.text

    # Delete: 409 while referenced; force does not override a workflow step.
    r = await client.delete(f"/admin/api/agents/{ids['ri-target']}")
    assert r.status_code == 409 and "'ri-wf'" in r.text and "'ri-caller'" in r.text
    r = await client.delete(f"/admin/api/agents/{ids['ri-target']}?force=true")
    assert r.status_code == 409 and "'ri-wf'" in r.text

    assert (await client.delete(f"/admin/api/workflows/{wf_id}")).status_code == 204
    r = await client.delete(f"/admin/api/agents/{ids['ri-target']}")
    assert r.status_code == 409 and "force=true" in r.text
    r = await client.delete(f"/admin/api/agents/{ids['ri-target']}?force=true")
    assert r.status_code == 204, r.text
    caller = (await client.get(f"/admin/api/agents/{ids['ri-caller']}")).json()
    assert caller["peers"] == []


async def test_replace_import_refuses_to_remove_a_referenced_agent(client):
    for body in (agent("ri-gone"), agent("ri-keep", peers=["ri-gone"])):
        assert (await client.post("/admin/api/agents", json=body)).status_code == 201
    bundle = (await client.get("/admin/api/export")).json()
    bundle["agents"] = [a for a in bundle["agents"] if a["name"] != "ri-gone"]
    bundle["replace"] = True

    r = await client.post("/admin/api/import", json=bundle)
    assert r.status_code == 422, r.text
    assert "Agent 'ri-gone' cannot be removed" in r.json()["detail"]
    assert "'ri-keep'" in r.json()["detail"]
    assert "ri-gone" in await _ids(client)  # nothing was deleted

    for a in bundle["agents"]:
        if a["name"] == "ri-keep":
            a["peers"] = []
    r = await client.post("/admin/api/import", json=bundle)
    assert r.status_code == 200, r.text
    assert r.json()["removed"] >= 1
    assert "ri-gone" not in await _ids(client)


# --- M8: the import is one transaction -----------------------------------------------
async def test_import_rolls_back_everything_on_any_error(client):
    r = await client.post("/admin/api/import", json={
        "orchestrator_instructions": "ri-atomic instructions",
        "skills": [{"name": "ri-atomic-skill", "description": "d", "content": "c"}],
        "agents": [agent("ri-atomic-ok"), agent("ri-atomic-bad", skills=["ri-no-such-skill"])],
        "workflows": [{"name": "ri-atomic-wf", "steps": [
            {"branch_key": None, "position": 1, "agent_name": "ri-ghost"}]}],
    })
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    # Every problem in one round, not the first one only.
    assert "Agent 'ri-atomic-bad'" in detail and "Workflow 'ri-atomic-wf'" in detail
    assert "ri-atomic-ok" not in await _ids(client)
    skills = {s["name"] for s in (await client.get("/admin/api/skills")).json()}
    assert "ri-atomic-skill" not in skills
    orch = (await client.get("/admin/api/orchestrator")).json()["instructions"]
    assert orch != "ri-atomic instructions"


async def test_import_keeps_a_stored_secret_and_names_a_missing_one(client):
    oauth = {"client_id": "cid", "client_secret": "s3cret",
             "uaa_url": "https://uaa.authentication.eu10.hana.ondemand.com"}
    r = await client.post("/admin/api/agents", json=agent(
        "ri-secret", mcp_servers=[{"url": BTP_URL, "auth_mode": "oauth2", "oauth": oauth}]))
    assert r.status_code == 201, r.text

    exported = next(a for a in (await client.get("/admin/api/export")).json()["agents"]
                    if a["name"] == "ri-secret")
    assert exported["mcp_servers"][0]["oauth"]["client_secret"] == ""  # redacted

    # Same landscape: the stored secret survives the redacted round trip.
    r = await client.post("/admin/api/import", json={"agents": [exported]})
    assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, "ri-secret")
        assert row.mcp_servers[0]["oauth"]["client_secret"] == "s3cret"

    # Fresh landscape (no row to keep it from): a 422 naming agent and server.
    fresh = {**exported, "name": "ri-secret-fresh"}
    r = await client.post("/admin/api/import", json={"agents": [fresh]})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "ri-secret-fresh" in detail and BTP_URL in detail and "client_secret" in detail
    assert "ri-secret-fresh" not in await _ids(client)


# --- Low: api_slug charset over HTTP -----------------------------------------------
async def test_api_slug_charset_is_a_422(client):
    r = await client.post("/admin/api/agents", json=agent("ri-slug", api_slug="ri/slug"))
    assert r.status_code == 422 and "api_slug" in r.text
    r = await client.post("/admin/api/agents", json=agent("ri-slug"))
    assert r.status_code == 201
    r = await client.put(f"/admin/api/agents/{r.json()['id']}",
                         json=agent("ri-slug", api_slug="Ri Slug"))
    assert r.status_code == 422 and "api_slug" in r.text


# --- Low: age-based workflow-run sweep ----------------------------------------------------
async def test_sweep_stale_workflow_runs_applies_the_timeout(client):
    assert (await client.post("/admin/api/agents", json=agent("ri-sweep-agent"))).status_code == 201
    async with SessionLocal() as s:
        wf = await upsert_workflow(
            s, name="ri-sweep-wf", run_timeout_seconds=60,
            steps=[{"branch_key": None, "position": 1, "agent_name": "ri-sweep-agent"}],
        )
        old = await create_workflow_run(s, workflow=wf, trigger="manual")
        young = await create_workflow_run(s, workflow=wf, trigger="manual")
        old.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()
        old_id, young_id = old.id, young.id

        # Not all_running: only the run past its workflow's timeout is swept.
        assert await sweep_stale_workflow_runs(s) == 1
        assert (await s.get(WorkflowRun, old_id)).status == "interrupted"
        assert "timeout" in (await s.get(WorkflowRun, old_id)).error
        assert (await s.get(WorkflowRun, young_id)).status == "running"

        assert await sweep_stale_workflow_runs(s, all_running=True) == 1
        assert (await s.get(WorkflowRun, young_id)).status == "interrupted"

        for rid in (old_id, young_id):
            await s.delete(await s.get(WorkflowRun, rid))
        await s.commit()
        row = await get_workflow_by_name(s, "ri-sweep-wf")
        assert isinstance(row, Workflow)


# --- M7: retired builds stay open while a delegation is in flight -----------------------
class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeServer:
    def __init__(self) -> None:
        self._http_client = _FakeClient()


def _fake_build() -> registry_module.BuildResult:
    return registry_module.BuildResult(
        orchestrator=object(), specialists={}, mcp_clients=[_FakeServer()], configs=[],
    )


async def test_reload_closes_retired_builds_only_when_idle(monkeypatch):
    async def build():
        return _fake_build()

    monkeypatch.setattr(registry_module, "build_orchestrator", build)
    reg = registry_module.Registry()

    b1 = await reg.reload()
    b1.in_flight.value = 1  # a chat delegation is still running on b1
    b2 = await reg.reload()
    assert not b1.mcp_clients[0]._http_client.closed
    assert reg._retired == [b1]

    b3 = await reg.reload()
    # b1 is still busy and kept; b2 was idle and is closed, not leaked.
    assert not b1.mcp_clients[0]._http_client.closed
    assert b2.mcp_clients[0]._http_client.closed
    assert reg._retired == [b1]

    b1.in_flight.value = 0
    b4 = await reg.reload()
    assert b1.mcp_clients[0]._http_client.closed
    assert b3.mcp_clients[0]._http_client.closed
    assert reg._retired == [] and reg.build is b4


async def test_delegation_tool_counts_in_flight_runs():
    """The delegate tool holds the build's counter for exactly the specialist run.

    Calls the registered tool function directly (with a stand-in specialist
    run) rather than driving an orchestrator model: other test modules patch
    pydantic_ai.Agent at import time, and this is about the counter, not the
    model loop.
    """
    from types import SimpleNamespace

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from agents.db import AgentConfig

    counter = registry_module.RunCounter()
    specialist = Agent(TestModel(), instructions="specialist")
    orchestrator = Agent(TestModel(), instructions="orchestrator")
    row = AgentConfig(name="ri peer", description="d", instructions="i",
                      mcp_url="https://ri.example.com/mcp", auth_mode="none")
    registry_module._attach_delegation_tool(orchestrator, specialist, row, counter=counter)
    tool = orchestrator._function_toolset.tools[registry_module._sanitize_tool_name(row.name)]
    ctx = SimpleNamespace(usage=None)

    seen: list[int] = []

    async def fake_run(query, **kwargs):
        seen.append(counter.value)
        return SimpleNamespace(output="done")

    specialist.run = fake_run  # type: ignore[method-assign]
    assert await tool.function(ctx, "consult the peer") == "done"
    assert seen == [1]
    assert counter.value == 0

    # A failing specialist releases the count as well.
    async def boom(query, **kwargs):
        raise RuntimeError("down")

    specialist.run = boom  # type: ignore[method-assign]
    out = await tool.function(ctx, "again")
    assert out.startswith("Error from ri peer")
    assert counter.value == 0
