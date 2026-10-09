"""Final review M1: a closed Bitbucket switch reaches the running agents.

A run takes its specialist, and so the pins of its ``builtin:bitbucket``
entry, from the running registry build. An admin who unticks Approving and
saves must not have to press Reload as well: until then the scheduled runs
would still hold ``complete_approval`` and still approve. So an agent save,
create, delete and import that changes the agent's Bitbucket entry rebuilds
the registry itself and says so (``reloaded`` / ``reload_failed``), exactly as
it does for a changed ``builtin:odata`` entry
(``tests/test_odata_catalogue_reload.py``).

Run:  python -m pytest tests/test_bitbucket_reload.py
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
from pydantic_ai import RunContext  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402
from pydantic_ai.usage import RunUsage  # noqa: E402
from sqlalchemy import delete  # noqa: E402

import agents.registry as registry_module  # noqa: E402
import app as app_module  # noqa: E402
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db  # noqa: E402
from agents.ide.readonly import ReadOnlyGuard  # noqa: E402
from agents.odata import admin_routes  # noqa: E402

REAL_LIVE_REGISTRY = admin_routes._live_registry

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

AGENTS = "/admin/api/agents"
URL = "builtin:bitbucket"
BASE = {"destination": "BITBUCKET", "workspace": "acme-ws"}
COMMENTING = {**BASE, "allow_comment": True}
APPROVING = {**COMMENTING, "allow_approve": True}
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}


def entry(block: dict[str, Any]) -> dict[str, Any]:
    return {"url": URL, "auth_mode": "destination", "oauth": block}


def body(*servers: dict, **patch: Any) -> dict[str, Any]:
    return {"name": "reviewer", "description": "d", "instructions": "i",
            "mcp_servers": list(servers), "expose_chat": False, **patch}


class Live:
    def __init__(self) -> None:
        self.registry = registry_module.registry
        self.reloads = 0
        self.refreshes = 0
        self.fail: Exception | None = None

    async def tools(self, name: str = "reviewer") -> set[str]:
        """The Bitbucket tools a NEW run of the agent would list."""
        agent = self.registry.build.specialists[name]
        ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
        names: set[str] = set()
        for toolset in getattr(agent, "_user_toolsets", ()) or ():
            if isinstance(toolset, ReadOnlyGuard):
                names |= set(await toolset.get_tools(ctx))
        return names


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


@pytest.fixture
async def live(monkeypatch, client) -> Live:
    """``reviewer`` saved through the API with an approving entry, the
    registry built from it by hand, its reloads counted from here on."""
    for var, value in (("DESTINATION_CLIENT_ID", "cid"),
                       ("DESTINATION_CLIENT_SECRET", "placeholder"),
                       ("DESTINATION_URI", "https://destination.example.com"),
                       ("DESTINATION_TOKEN_URL", "https://login.example.com/oauth/token")):
        monkeypatch.setenv(var, value)
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        await s.commit()
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    monkeypatch.setattr(admin_routes, "_live_registry", REAL_LIVE_REGISTRY)
    world = Live()
    monkeypatch.setattr(world.registry, "_build", None)
    r = await client.post(AGENTS, json=body(entry(APPROVING)))
    assert r.status_code == 201, r.text
    world.id = r.json()["id"]
    await world.registry.reload()

    real_reload = world.registry.reload

    async def reload():
        world.reloads += 1
        if world.fail is not None:
            raise world.fail
        return await real_reload()

    def refresh() -> None:
        world.refreshes += 1

    monkeypatch.setattr(world.registry, "reload", reload)
    monkeypatch.setattr(dynamic_chat_app, "refresh", refresh)
    return world


async def test_unticking_approving_closes_it_for_the_next_run(live, client):
    assert "complete_approval" in await live.tools()
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry(COMMENTING)))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (1, 1)
    names = await live.tools()
    assert "complete_approval" not in names and "submit_review" in names


async def test_unticking_commenting_closes_the_write_tools_for_the_next_run(live, client):
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry(BASE)))
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert not {"add_inline_comment", "submit_review", "complete_approval"} & await live.tools()


@pytest.mark.parametrize("change", [
    {"repositories": ["svc-a"]},
    {"branch": "release"},
    {"require_green_builds": False},
    {"workspace": "other-ws"},
    {"destination": "OTHER"},
])
async def test_any_change_of_the_block_reloads(live, client, change):
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry({**APPROVING, **change})))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and live.reloads == 1


async def test_an_edit_that_leaves_the_entry_alone_does_not_reload(live, client):
    """Also not for a block that only says its defaults out loud."""
    same = {**APPROVING, "branch": None, "require_green_builds": True,
            "has_client_secret": False}
    r = await client.put(f"{AGENTS}/{live.id}",
                         json=body(entry(same), description="another text"))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (0, 0)
    assert "complete_approval" in await live.tools()


async def test_removing_the_entry_and_adding_it_again_reloads(live, client):
    r = await client.put(f"{AGENTS}/{live.id}", json=body(NOTES))
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert not any(n.endswith("pull_requests") for n in await live.tools())
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry(COMMENTING)))
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert live.reloads == 2 and "submit_review" in await live.tools()


async def test_disabling_and_enabling_the_agent_reloads(live, client):
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry(APPROVING), enabled=False))
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert "reviewer" not in live.registry.build.specialists
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry(APPROVING), enabled=True))
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert live.reloads == 2 and "complete_approval" in await live.tools()


async def test_creating_and_deleting_an_agent_with_an_entry_reloads(live, client):
    r = await client.post(AGENTS, json=body(entry(COMMENTING), name="second"))
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is True and live.reloads == 1
    assert "submit_review" in await live.tools("second")

    r = await client.delete(f"{AGENTS}/{r.json()['id']}")
    assert r.status_code == 204 and r.content == b""
    assert r.headers["x-odata-reloaded"] == "true"
    assert r.headers["x-odata-reload-failed"] == "false"
    assert live.reloads == 2 and "second" not in live.registry.build.specialists


async def test_a_post_that_replaces_the_agent_of_that_name_reloads(live, client):
    r = await client.post(AGENTS, json=body(entry(COMMENTING)))
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is True
    assert "complete_approval" not in await live.tools()


async def test_a_failing_reload_after_a_save_is_said_not_raised(live, client, caplog):
    live.fail = RuntimeError("boom at https://aicore.internal/secret-path")
    r = await client.put(f"{AGENTS}/{live.id}", json=body(entry(COMMENTING)))
    assert r.status_code == 200, r.text
    assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    assert "secret-path" not in r.text and "secret-path" not in caplog.text


async def exported(client) -> list[dict[str, Any]]:
    r = await client.get("/admin/api/export")
    assert r.status_code == 200, r.text
    return r.json()["agents"]


def block_of(agent: dict[str, Any]) -> dict[str, Any]:
    (server,) = [s for s in agent["mcp_servers"] if s.get("url") == URL]
    return server["oauth"]


async def test_an_import_that_closes_the_approval_reloads(live, client):
    agents = await exported(client)
    block_of(agents[0])["allow_approve"] = False
    r = await client.post("/admin/api/import", json={"agents": agents})
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and live.reloads == 1
    assert "complete_approval" not in await live.tools()


async def test_an_import_that_disables_the_agent_reloads(live, client):
    agents = await exported(client)
    agents[0]["enabled"] = False
    r = await client.post("/admin/api/import", json={"agents": agents})
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert "reviewer" not in live.registry.build.specialists


async def test_a_replace_import_that_removes_the_agent_reloads(live, client):
    other = body(NOTES, name="notes")
    r = await client.post("/admin/api/import", json={"agents": [other], "replace": True})
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == 1 and r.json()["reloaded"] is True
    assert live.reloads == 1 and "reviewer" not in live.registry.build.specialists


async def test_an_import_that_leaves_the_entry_alone_does_not_reload(live, client):
    agents = await exported(client)
    agents[0]["description"] = "another text"
    r = await client.post("/admin/api/import",
                          json={"agents": [*agents, body(NOTES, name="notes")]})
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (0, 0)


async def test_a_failing_reload_after_an_import_is_said_not_raised(live, client):
    agents = await exported(client)
    block_of(agents[0])["allow_approve"] = False
    live.fail = RuntimeError("boom at https://aicore.internal/secret-path")
    r = await client.post("/admin/api/import", json={"agents": agents})
    assert r.status_code == 200, r.text
    assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    assert r.json()["status"] == "imported" and "secret-path" not in r.text
    assert live.reloads == 1
