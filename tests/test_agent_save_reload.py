"""Every admin save the running build reads reaches the running agents.

Agents, skills, the orchestrator instructions and the HTTP bundle import.

A run takes its specialist from the running registry build. Before this rule
only a save that changed an agent's data or review entry rebuilt the
registry; any other save waited for the admin to press Reload, and a new
agent picked in the web chat failed with ``agent_unavailable`` until then.
Now every successful create, update and delete through the admin API
rebuilds the registry and the chat app once after the commit and answers
the outcome (``reloaded`` / ``reload_failed``; the two ``X-OData-*`` headers
for the 204 of a delete). The data catalogue is rebuilt only when the
agent's catalogue sources changed, as before. Skills (create, update,
delete), the orchestrator instructions and an import that carries agents,
skills or orchestrator instructions, or removes agents or skills, do the
same; a workflow-only import does not (a workflow run reads its definition
from the database at run time).

Run:  python -m pytest tests/test_agent_save_reload.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402
from sqlalchemy import delete  # noqa: E402

import agents.registry as registry_module  # noqa: E402
import app as app_module  # noqa: E402
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import AgentConfig, SessionLocal, SkillConfig, get_agent, init_db  # noqa: E402
from agents.odata import admin_routes  # noqa: E402

REAL_LIVE_REGISTRY = admin_routes._live_registry

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

AGENTS = "/admin/api/agents"
BITBUCKET = {"url": "builtin:bitbucket", "auth_mode": "destination",
             "oauth": {"destination": "BITBUCKET", "workspace": "acme-ws"}}


NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}


def body(name: str = "plain", *servers: dict, **patch: Any) -> dict[str, Any]:
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": list(servers) or [NOTES], "expose_chat": False, **patch}


class Live:
    def __init__(self) -> None:
        self.registry = registry_module.registry
        self.reloads = 0
        self.refreshes = 0
        self.catalogue_rebuilds = 0
        self.fail: Exception | None = None

    @property
    def specialists(self) -> set[str]:
        return set(self.registry.build.specialists)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


async def _empty_registry() -> None:
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        await s.commit()


@pytest.fixture
async def live(monkeypatch) -> Live:
    """An empty registry with a running build; reloads, chat app refreshes
    and catalogue rebuilds counted from here on."""
    for var, value in (("DESTINATION_CLIENT_ID", "cid"),
                       ("DESTINATION_CLIENT_SECRET", "placeholder"),
                       ("DESTINATION_URI", "https://destination.example.com"),
                       ("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")):
        monkeypatch.setenv(var, value)
    await _empty_registry()
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    monkeypatch.setattr(admin_routes, "_live_registry", REAL_LIVE_REGISTRY)
    world = Live()
    monkeypatch.setattr(world.registry, "_build", None)
    await world.registry.reload()
    real_reload = world.registry.reload

    async def reload():
        world.reloads += 1
        if world.fail is not None:
            raise world.fail
        return await real_reload()

    def refresh() -> None:
        world.refreshes += 1

    async def rebuild_safely(*_a: Any, **_k: Any) -> None:
        world.catalogue_rebuilds += 1

    from agents.data_catalog import importer

    monkeypatch.setattr(world.registry, "reload", reload)
    monkeypatch.setattr(dynamic_chat_app, "refresh", refresh)
    monkeypatch.setattr(importer, "rebuild_safely", rebuild_safely)
    return world


async def test_creating_an_ordinary_agent_makes_it_runnable_at_once(live, client):
    r = await client.post(AGENTS, json=body())
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (1, 1)
    assert "plain" in live.specialists
    assert live.catalogue_rebuilds == 0


async def test_an_update_of_the_description_alone_reloads_once(live, client):
    r = await client.post(AGENTS, json=body())
    agent_id = r.json()["id"]
    r = await client.put(f"{AGENTS}/{agent_id}", json=body(description="another text"))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (2, 2)
    assert live.catalogue_rebuilds == 0


async def test_a_rename_reaches_the_running_build(live, client):
    r = await client.post(AGENTS, json=body())
    r = await client.put(f"{AGENTS}/{r.json()['id']}", json=body("renamed"))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True
    assert "renamed" in live.specialists and "plain" not in live.specialists


async def test_deleting_an_ordinary_agent_reloads_once(live, client):
    r = await client.post(AGENTS, json=body())
    agent_id = r.json()["id"]
    r = await client.delete(f"{AGENTS}/{agent_id}")
    assert r.status_code == 204, r.text
    assert r.headers["x-odata-reloaded"] == "true"
    assert r.headers["x-odata-reload-failed"] == "false"
    assert (live.reloads, live.refreshes) == (2, 2)
    assert "plain" not in live.specialists


async def test_a_changed_entry_still_reloads_only_once(live, client):
    r = await client.post(AGENTS, json=body())
    agent_id = r.json()["id"]
    r = await client.put(f"{AGENTS}/{agent_id}", json=body("plain", BITBUCKET))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True
    assert live.reloads == 2
    r = await client.delete(f"{AGENTS}/{agent_id}")
    assert r.status_code == 204 and live.reloads == 3


@pytest.mark.parametrize("step", ["create", "update", "delete"])
async def test_a_failing_reload_is_said_not_raised(live, client, caplog, step):
    agent_id = None
    if step != "create":
        r = await client.post(AGENTS, json=body())
        agent_id = r.json()["id"]
    live.fail = RuntimeError("boom at https://aicore.internal/secret-path")
    if step == "create":
        r = await client.post(AGENTS, json=body())
        assert r.status_code == 201, r.text
        assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
        agent_id = r.json()["id"]
    elif step == "update":
        r = await client.put(f"{AGENTS}/{agent_id}", json=body(description="new"))
        assert r.status_code == 200, r.text
        assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    else:
        r = await client.delete(f"{AGENTS}/{agent_id}")
        assert r.status_code == 204, r.text
        assert r.headers["x-odata-reloaded"] == "false"
        assert r.headers["x-odata-reload-failed"] == "true"
    assert "secret-path" not in r.text and "secret-path" not in caplog.text
    async with SessionLocal() as s:
        row = await get_agent(s, agent_id)
    if step == "delete":
        assert row is None
    else:
        assert row is not None
        assert row.description == ("new" if step == "update" else "d")


async def test_a_refused_save_does_not_reload(live, client):
    r = await client.post(AGENTS, json=body())
    agent_id = r.json()["id"]
    r = await client.put(f"{AGENTS}/{agent_id + 999}", json=body())
    assert r.status_code == 404
    r = await client.delete(f"{AGENTS}/{agent_id + 999}")
    assert r.status_code == 404
    assert live.reloads == 1


async def test_without_a_running_build_nothing_is_reloaded(client, monkeypatch):
    await _empty_registry()
    monkeypatch.setattr(admin_routes, "_live_registry", lambda: None)
    r = await client.post(AGENTS, json=body())
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    r = await client.delete(f"{AGENTS}/{r.json()['id']}")
    assert r.headers["x-odata-reloaded"] == "false"
    assert r.headers["x-odata-reload-failed"] == "false"


# ------------------------------------------------------------------ skills

SKILLS = "/admin/api/skills"


def skill(name: str = "triage", **patch: Any) -> dict[str, Any]:
    return {"name": name, "description": "when to triage", "content": "steps", **patch}


async def test_a_skill_create_update_and_delete_each_reload_once(live, client):
    r = await client.post(SKILLS, json=skill())
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (1, 1)
    skill_id = r.json()["id"]
    r = await client.put(f"{SKILLS}/{skill_id}", json=skill(content="new steps"))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert live.reloads == 2
    r = await client.delete(f"{SKILLS}/{skill_id}")
    assert r.status_code == 204, r.text
    assert r.headers["x-odata-reloaded"] == "true"
    assert r.headers["x-odata-reload-failed"] == "false"
    assert live.reloads == 3
    assert live.catalogue_rebuilds == 0


async def test_renaming_or_deleting_an_attached_skill_reloads_once(live, client):
    r = await client.post(SKILLS, json=skill())
    skill_id = r.json()["id"]
    r = await client.post(AGENTS, json=body(skills=["triage"]))
    assert r.status_code == 201, r.text
    before = live.reloads
    r = await client.put(f"{SKILLS}/{skill_id}", json=skill("triage-2"))
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert live.reloads == before + 1
    r = await client.delete(f"{SKILLS}/{skill_id}")
    assert r.status_code == 204 and r.headers["x-odata-reloaded"] == "true"
    assert live.reloads == before + 2


@pytest.mark.parametrize("step", ["create", "update", "delete"])
async def test_a_failing_reload_after_a_skill_save_is_said_not_raised(live, client, step):
    skill_id = None
    if step != "create":
        skill_id = (await client.post(SKILLS, json=skill())).json()["id"]
    live.fail = RuntimeError("boom at https://aicore.internal/secret-path")
    if step == "create":
        r = await client.post(SKILLS, json=skill())
        assert r.status_code == 201, r.text
        assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    elif step == "update":
        r = await client.put(f"{SKILLS}/{skill_id}", json=skill(content="new"))
        assert r.status_code == 200, r.text
        assert r.json()["reload_failed"] is True and r.json()["content"] == "new"
    else:
        r = await client.delete(f"{SKILLS}/{skill_id}")
        assert r.status_code == 204, r.text
        assert r.headers["x-odata-reload-failed"] == "true"
        assert (await client.get(f"{SKILLS}/{skill_id}")).status_code == 404
    assert "secret-path" not in r.text


async def test_a_refused_skill_save_does_not_reload(live, client):
    r = await client.put(f"{SKILLS}/999999", json=skill())
    assert r.status_code == 404
    r = await client.delete(f"{SKILLS}/999999")
    assert r.status_code == 404
    assert live.reloads == 0


async def test_a_skill_save_without_a_running_build_reloads_nothing(client, monkeypatch):
    await _empty_registry()
    monkeypatch.setattr(admin_routes, "_live_registry", lambda: None)
    r = await client.post(SKILLS, json=skill())
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    r = await client.delete(f"{SKILLS}/{r.json()['id']}")
    assert r.headers["x-odata-reloaded"] == "false"


# ------------------------------------------------------------ orchestrator


async def test_the_orchestrator_save_reloads_once(live, client):
    r = await client.put("/admin/api/orchestrator", json={"instructions": "route well"})
    assert r.status_code == 200, r.text
    assert r.json() == {"instructions": "route well", "reloaded": True, "reload_failed": False}
    assert (live.reloads, live.refreshes) == (1, 1)


async def test_a_failing_reload_after_the_orchestrator_save_is_said(live, client):
    live.fail = RuntimeError("boom")
    r = await client.put("/admin/api/orchestrator", json={"instructions": "route well"})
    assert r.status_code == 200, r.text
    assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    r = await client.get("/admin/api/orchestrator")
    assert r.json()["instructions"] == "route well"


# ------------------------------------------------------------------ import

IMPORT = "/admin/api/import"


@pytest.mark.parametrize("bundle", [
    {"agents": [body()]},
    {"skills": [skill()]},
    {"orchestrator_instructions": "route well"},
    {"agents": [body(), body("second")], "skills": [skill()],
     "orchestrator_instructions": "route well"},
], ids=["agents", "skills", "orchestrator", "all"])
async def test_an_import_of_what_the_build_reads_reloads_once(live, client, bundle):
    r = await client.post(IMPORT, json=bundle)
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (1, 1)


async def test_a_replace_import_that_removes_agents_reloads_once(live, client):
    await client.post(AGENTS, json=body())
    before = live.reloads
    r = await client.post(IMPORT, json={"replace": True, "skills": [skill()]})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == 1 and r.json()["reloaded"] is True
    assert live.reloads == before + 1
    assert "plain" not in live.specialists


async def test_an_import_with_a_changed_entry_still_reloads_once(live, client):
    await client.post(AGENTS, json=body())
    before = live.reloads
    r = await client.post(IMPORT, json={"agents": [body("plain", BITBUCKET)]})
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and live.reloads == before + 1


async def test_an_import_that_carries_nothing_the_build_reads_does_not_reload(live, client):
    r = await client.post(IMPORT, json={"workflows": []})
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    assert live.reloads == 0


async def test_a_failing_reload_after_an_import_is_said_not_raised(live, client):
    live.fail = RuntimeError("boom at https://aicore.internal/secret-path")
    r = await client.post(IMPORT, json={"agents": [body()]})
    assert r.status_code == 200, r.text
    assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    assert "secret-path" not in r.text
    assert live.reloads == 1
