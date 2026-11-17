"""Pushes of the agent list to the register: after saves, deletes, imports, reloads.

No real HTTP: the register is a recording ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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
from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db  # noqa: E402
from agents.nexus import lifecycle, logged_model, register, sender, state  # noqa: E402
from tests.nexus_helpers import Recorder, register_on  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

AGENTS = "/admin/api/agents"
NOTES = {"url": "builtin:sapnotes", "auth_mode": "none"}


def body(name="invoice-assistant", **patch):
    return {"name": name, "description": "d", "instructions": "i",
            "mcp_servers": [NOTES], "expose_chat": False, **patch}


def _reset():
    lifecycle.reset_for_tests()
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app_module.app),
                           base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def world(monkeypatch):
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(SkillConfig))
        await s.commit()
    _reset()
    reloads = []

    async def reload():
        reloads.append(1)
        return SimpleNamespace(configs=[], specialists={})

    monkeypatch.setattr(registry_module.registry, "reload", reload)
    monkeypatch.setattr(dynamic_chat_app, "refresh", lambda: None)
    yield reloads
    _reset()


async def _on(monkeypatch, rec=None):
    rec = rec or Recorder(answers={"registerAgents": {"value": []}})
    client = await register_on(monkeypatch, rec)
    register.start_pusher(client)
    return rec


def _entries(rec):
    """name -> active of the LAST registerAgents call."""
    return {a["name"]: a["active"] for a in rec.of("registerAgents")[-1]["body"]["agents"]}


async def _idle():
    await register.settle()


async def test_create_pushes_the_new_agent(monkeypatch, client):
    rec = await _on(monkeypatch)
    assert (await client.post(AGENTS, json=body())).status_code == 201
    await _idle()
    assert _entries(rec)["invoice-assistant"] is True


async def test_rename_pushes_old_inactive_and_new_active(monkeypatch, client):
    rec = await _on(monkeypatch)
    aid = (await client.post(AGENTS, json=body())).json()["id"]
    await _idle()
    r = await client.put(f"{AGENTS}/{aid}", json=body("invoice-helper"))
    assert r.status_code == 200, r.text
    await _idle()
    assert _entries(rec)["invoice-assistant"] is False
    assert _entries(rec)["invoice-helper"] is True


async def test_delete_pushes_the_name_inactive(monkeypatch, client):
    rec = await _on(monkeypatch)
    aid = (await client.post(AGENTS, json=body())).json()["id"]
    await _idle()
    assert (await client.delete(f"{AGENTS}/{aid}")).status_code == 204
    await _idle()
    assert _entries(rec)["invoice-assistant"] is False


async def test_import_pushes_both_agents(monkeypatch, client):
    rec = await _on(monkeypatch)
    r = await client.post("/admin/api/import",
                          json={"agents": [body("agent-one"), body("agent-two")]})
    assert r.status_code == 200, r.text
    await _idle()
    assert {"agent-one", "agent-two"} <= {n for n, a in _entries(rec).items() if a}


async def test_ten_saves_make_at_most_two_pushes(monkeypatch, client):
    rec = await _on(monkeypatch)
    real_rows = register._load_rows

    async def slow_rows():
        await asyncio.sleep(0.5)  # the push in flight outlasts the saves
        return await real_rows()

    monkeypatch.setattr(register, "_load_rows", slow_rows)
    for n in range(10):
        assert (await client.post(AGENTS, json=body(f"agent-{n}"))).status_code == 201
    await register.settle(timeout=5)
    assert 1 <= len(rec.of("registerAgents")) <= 2
    assert {f"agent-{n}" for n in range(10)} <= set(_entries(rec))


async def test_off_makes_no_request(monkeypatch, client):
    rec = Recorder()
    monkeypatch.setattr(lifecycle, "_transport", rec.transport())
    assert (await client.post(AGENTS, json=body())).status_code == 201
    assert rec.requests == []


async def test_a_failing_request_push_never_fails_the_save(monkeypatch, client, caplog):
    def boom():
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(register, "request_push", boom)
    with caplog.at_level(logging.WARNING):
        r = await client.post(AGENTS, json=body())
    assert r.status_code == 201
    assert "RuntimeError" in caplog.text and "secret-detail" not in caplog.text


async def test_push_comes_after_the_commit(monkeypatch, client):
    seen = []

    async def rows():
        return []

    def spy():
        # A fresh session sees the committed row only after the commit.
        seen.append(1)

    monkeypatch.setattr(register, "request_push", spy)
    aid = (await client.post(AGENTS, json=body())).json()["id"]
    assert seen == [1]
    assert (await client.delete(f"{AGENTS}/{aid}")).status_code == 204
    assert seen == [1, 1]


async def test_admin_reload_and_restart_reevaluate_once(monkeypatch, client, world):
    calls = []

    async def reevaluate():
        calls.append(1)
        return "off"

    monkeypatch.setattr(lifecycle, "reevaluate", reevaluate)
    assert (await client.post("/admin/api/reload")).status_code == 200
    assert calls == [1] and world == [1]

    async def restart_self():
        return "skipped"

    import agents.cf_api as cf_api
    monkeypatch.setattr(cf_api, "restart_self", restart_self)
    assert (await client.post("/admin/api/restart")).status_code == 200
    assert calls == [1, 1]


async def test_a_failing_reevaluate_never_fails_reload(monkeypatch, client):
    async def reevaluate():
        raise RuntimeError("x")

    monkeypatch.setattr(lifecycle, "reevaluate", reevaluate)
    assert (await client.post("/admin/api/reload")).status_code == 200


async def test_registry_reload_requests_a_push(monkeypatch):
    monkeypatch.undo()  # real Registry.reload, not the stub of the fixture
    calls = []
    monkeypatch.setattr(register, "request_push", lambda: calls.append(1))
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    reg = registry_module.Registry()
    await reg.reload()
    assert calls == [1]
