"""Deep-agent config through the admin API: POST, PUT, export, import, seed.

Run:  pytest tests/test_admin_deep.py

Every agent is created under a ``deep-api-`` prefix and removed again, so the
module can share a database with the other pytest modules.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_admin_deep.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""
os.environ.setdefault("AICORE_AVAILABLE_MODELS", "gpt-4o")

from httpx import ASGITransport, AsyncClient  # noqa: E402

import app as app_module  # noqa: E402
from agents import admin as admin_module  # noqa: E402
from agents.admin import seed_from_file_if_empty  # noqa: E402
from agents.db import SessionLocal, get_agent_by_name, init_db  # noqa: E402
from agents.deep import DeepConfig  # noqa: E402

SERVERS = [{"url": "https://deep-api.example.com/mcp", "auth_mode": "none"}]
PREFIX = "deep-api-"
DEFAULTS = DeepConfig().model_dump()
CUSTOM = {
    "enabled": True,
    "planning": True,
    "scratchpad": False,
    "subagents": True,
    "max_subagents": 3,
    "subagent_max_depth": 2,
    "subagent_instructions": "Keep it short.",
}


def agent(name: str, **extra) -> dict:
    return {
        "name": PREFIX + name, "description": f"{name} d", "instructions": f"{name} i",
        "mcp_servers": SERVERS, **extra,
    }


@pytest.fixture
async def client():
    await init_db()
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
        r = await c.get("/admin/api/agents")
        for a in r.json():
            if a["name"].startswith(PREFIX):
                await c.delete(f"/admin/api/agents/{a['id']}?force=true")


async def test_deep_is_always_present_and_defaults_off(client):
    r = await client.post("/admin/api/agents", json=agent("plain"))
    assert r.status_code == 201, r.text
    assert r.json()["deep"] == DEFAULTS
    r = await client.get(f"/admin/api/agents/{r.json()['id']}")
    assert r.json()["deep"] == DEFAULTS
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, PREFIX + "plain")
        assert row.deep_json is None


async def test_post_put_round_trip_and_keep_semantics(client):
    r = await client.post("/admin/api/agents", json=agent("rt", deep=CUSTOM))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["deep"] == CUSTOM
    agent_id = created["id"]

    # A PUT without a `deep` key (an older client, or the UI5 admin before it
    # grew the panel) keeps the stored config, like peers/model_name.
    body = agent("rt", description="edited")
    r = await client.put(f"/admin/api/agents/{agent_id}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "edited"
    assert r.json()["deep"] == CUSTOM

    # A PUT that sends a changed `deep` replaces it.
    changed = {**CUSTOM, "max_subagents": 9, "subagent_max_depth": 1}
    r = await client.put(f"/admin/api/agents/{agent_id}", json=agent("rt", deep=changed))
    assert r.status_code == 200, r.text
    assert r.json()["deep"] == changed

    # Sending the defaults explicitly resets it (and nulls the column).
    r = await client.put(f"/admin/api/agents/{agent_id}", json=agent("rt", deep=DEFAULTS))
    assert r.status_code == 200, r.text
    assert r.json()["deep"] == DEFAULTS
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, PREFIX + "rt")
        assert row.deep_json is None

    # Partial objects are filled with defaults; out-of-range and unknown keys
    # are a 422 that names the field.
    r = await client.put(f"/admin/api/agents/{agent_id}", json=agent("rt", deep={"enabled": True}))
    assert r.status_code == 200 and r.json()["deep"] == {**DEFAULTS, "enabled": True}
    for bad in ({"max_subagents": 0}, {"subagent_max_depth": 4}, {"max_subagent": 2}):
        r = await client.put(f"/admin/api/agents/{agent_id}", json=agent("rt", deep=bad))
        assert r.status_code == 422, (bad, r.text)
        assert "deep" in json.dumps(r.json())


async def test_export_and_import_carry_deep(client):
    r = await client.post("/admin/api/agents", json=agent("exp", deep=CUSTOM))
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]

    r = await client.get("/admin/api/export")
    assert r.status_code == 200
    exported = next(a for a in r.json()["agents"] if a["name"] == PREFIX + "exp")
    assert exported["deep"] == CUSTOM

    # Delete, then import the exported entry verbatim: the config comes back.
    r = await client.delete(f"/admin/api/agents/{agent_id}?force=true")
    assert r.status_code == 204
    r = await client.post("/admin/api/import", json={
        "version": 1, "orchestrator_instructions": "", "skills": [], "agents": [exported],
    })
    assert r.status_code == 200, r.text
    r = await client.get("/admin/api/agents")
    back = next(a for a in r.json() if a["name"] == PREFIX + "exp")
    assert back["deep"] == CUSTOM

    # A bundle exported before deep existed carries no key and must not wipe
    # what this landscape has configured.
    legacy = {k: v for k, v in exported.items() if k != "deep"}
    legacy["description"] = "from a legacy bundle"
    r = await client.post("/admin/api/import", json={
        "version": 1, "orchestrator_instructions": "", "skills": [], "agents": [legacy],
    })
    assert r.status_code == 200, r.text
    r = await client.get("/admin/api/agents")
    back = next(a for a in r.json() if a["name"] == PREFIX + "exp")
    assert back["description"] == "from a legacy bundle"
    assert back["deep"] == CUSTOM


async def test_seed_file_carries_deep(client, tmp_path, monkeypatch):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({
        "version": 1,
        "orchestrator_instructions": "",
        "agents": [agent("seeded", deep=CUSTOM)],
    }))

    # The seed only runs on an empty agent table, and this database is shared
    # with the other pytest modules, so lift the gate for this one call.
    async def _no_agents(session):
        return []

    monkeypatch.setattr(admin_module, "list_agents", _no_agents)
    await seed_from_file_if_empty(seed)
    async with SessionLocal() as s:
        row = await get_agent_by_name(s, PREFIX + "seeded")
        assert row is not None and row.deep.model_dump() == CUSTOM
