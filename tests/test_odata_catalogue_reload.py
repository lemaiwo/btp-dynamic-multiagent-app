"""Final review C1: a catalogue edit reaches the running agents.

The registry hands every toolset a snapshot of its services when it is
built. An edit that CLOSES something (``enabled: false``, an operation
switched off, a field no longer selectable, a deleted service) therefore
used to stay open for the running agents until somebody pressed Reload.
Now the catalogue routes and the configuration import rebuild the registry
themselves when the service is one an agent uses, and say so
(``reloaded`` / ``reload_failed``).

The tools are called through a real agent run (a ``FunctionModel`` that
calls the tool), so the test sees what a NEW run of the agent sees. No
network: a refusal comes before anything is resolved.
"""

from __future__ import annotations

import copy
import json
import logging
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
from pydantic_ai.messages import (  # noqa: E402
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402
from sqlalchemy import delete  # noqa: E402

import agents.registry as registry_module  # noqa: E402
import app as app_module  # noqa: E402
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    get_odata_service,
    init_db,
)
from agents.odata import admin_routes  # noqa: E402
from tests.test_odata_registry import SERVICE, add_agent, add_service, odata_server  # noqa: E402

# The real seam, taken before any fixture replaces it (tests/conftest.py).
REAL_LIVE_REGISTRY = admin_routes._live_registry

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

BASE = "/admin/api/odata/services"
PR = "purchase-requisitions"
JOBS = "job-runs"
ONE = f"{BASE}/{PR}"
SET = "A_PurchaseRequisitionItem"


def payload(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(SERVICE)
    data.update(patch)
    return data


class Live:
    """The process's registry, loaded, with its reloads counted."""

    def __init__(self) -> None:
        self.registry = registry_module.registry
        self.reloads = 0
        self.refreshes = 0
        self.fail: Exception | None = None

    @property
    def buyer(self):
        return self.registry.build.specialists["buyer"]


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    async with SessionLocal() as s:
        for model in (ODataService, ODataAuditLog, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture
async def live(monkeypatch) -> Live:
    """Two services, one agent attaching both, the registry built from it."""
    monkeypatch.setattr(registry_module, "get_model", lambda *a, **k: TestModel())
    monkeypatch.setattr(admin_routes, "_live_registry", REAL_LIVE_REGISTRY)
    world = Live()
    monkeypatch.setattr(world.registry, "_build", None)
    await add_service()
    await add_service(name=JOBS, title="Background jobs", purpose="Look up job runs")
    await add_agent("buyer", odata_server(PR, JOBS))
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


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def call_tool(agent, tool: str, **args: Any) -> Any:
    """What ``tool`` answers in a new run of ``agent``."""
    seen: dict[str, Any] = {}

    def answer(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, ToolReturnPart):
                    seen["result"] = part.content
                    return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    await agent.run("go", model=FunctionModel(answer))
    return seen["result"]


async def services_found(agent) -> set[str]:
    out = await call_tool(agent, "search_operations", query="")
    return set(re_services(out))


def re_services(out: Any) -> list[str]:
    """Every ``service`` value anywhere in a search answer."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("service"), str):
                found.append(node["service"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return found


async def execute_code(agent) -> str:
    out = await call_tool(
        agent, "execute_operation", service=PR, target=SET, operation="list", top=1
    )
    return out["error"]["code"]


# ------------------------------------------------------------------------ PUT


async def test_disabling_a_service_in_use_closes_it_for_the_next_run(live, client):
    assert await services_found(live.buyer) == {PR, JOBS}

    r = await client.put(ONE, json=payload(enabled=False))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False
    assert body["reloaded"] is True and body["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (1, 1)

    # No manual reload: a new run of the agent no longer finds or reaches it.
    assert await services_found(live.buyer) == {JOBS}
    assert await execute_code(live.buyer) in ("service_disabled", "unknown_service")


async def test_closing_an_operation_reaches_the_next_run(live, client):
    closed = payload()
    closed["definition"]["entity_sets"][0]["operations"] = ["get"]
    r = await client.put(ONE, json=closed)
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert await execute_code(live.buyer) == "operation_disabled"


async def test_a_service_no_agent_uses_triggers_no_reload(live, client):
    await add_service(name="stock", title="Stock", purpose="Look up stock")
    r = await client.put(f"{BASE}/stock", json=payload(name="stock", enabled=False))
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (0, 0)


async def test_a_failing_reload_does_not_turn_a_stored_save_into_a_500(live, client, caplog):
    live.fail = RuntimeError("model 'x' at https://aicore.internal/secret-path failed")
    with caplog.at_level(logging.ERROR, logger=admin_routes.logger.name):
        r = await client.put(ONE, json=payload(enabled=False))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reload_failed"] is True and body["reloaded"] is False
    assert body["enabled"] is False and "secret-path" not in r.text
    async with SessionLocal() as s:
        assert not (await get_odata_service(s, PR)).enabled
    assert any("reload failed" in rec.getMessage() for rec in caplog.records)


async def test_without_a_loaded_registry_nothing_is_reloaded(live, client, monkeypatch):
    monkeypatch.setattr(live.registry, "_build", None)
    r = await client.put(ONE, json=payload(enabled=False))
    assert r.status_code == 200 and r.json()["reloaded"] is False
    assert live.reloads == 0


# --------------------------------------------------------------------- DELETE


async def test_deleting_after_detaching_closes_it_for_the_next_run(live, client):
    # Detached in storage (an agent save does not reload): the running build
    # still holds the service.
    async with SessionLocal() as s:
        row = (await s.execute(AgentConfig.__table__.select())).first()
        assert row is not None
        agent = await s.get(AgentConfig, row.id)
        agent.oauth_json = json.dumps({"services": [JOBS]})
        await s.commit()
    assert await services_found(live.buyer) == {PR, JOBS}

    r = await client.delete(ONE)
    assert r.status_code == 204 and r.content == b""
    assert r.headers["x-odata-reloaded"] == "true"
    assert r.headers["x-odata-reload-failed"] == "false"
    assert live.reloads == 1
    assert await services_found(live.buyer) == {JOBS}
    assert await execute_code(live.buyer) == "unknown_service"


async def test_deleting_a_service_nobody_runs_triggers_no_reload(live, client):
    await add_service(name="stock", title="Stock", purpose="Look up stock")
    r = await client.delete(f"{BASE}/stock")
    assert r.status_code == 204 and r.headers["x-odata-reloaded"] == "false"
    assert live.reloads == 0


async def test_a_failing_reload_after_a_delete_is_still_a_204(live, client):
    async with SessionLocal() as s:
        row = (await s.execute(AgentConfig.__table__.select())).first()
        agent = await s.get(AgentConfig, row.id)
        agent.oauth_json = json.dumps({"services": [JOBS]})
        await s.commit()
    live.fail = RuntimeError("boom")
    r = await client.delete(ONE)
    assert r.status_code == 204
    assert r.headers["x-odata-reload-failed"] == "true"
    assert r.headers["x-odata-reloaded"] == "false"
    async with SessionLocal() as s:
        assert await get_odata_service(s, PR) is None


# --------------------------------------------------------------------- import


async def exported(client) -> list[dict[str, Any]]:
    r = await client.get("/admin/api/export")
    assert r.status_code == 200, r.text
    return r.json()["odata_services"]


async def test_an_import_that_changes_a_service_in_use_reloads(live, client):
    services = await exported(client)
    for service in services:
        if service["name"] == PR:
            service["enabled"] = False
    r = await client.post("/admin/api/import", json={"odata_services": services})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated_odata_services"] == 1
    assert body["reloaded"] is True and body["reload_failed"] is False
    assert live.reloads == 1
    assert await services_found(live.buyer) == {JOBS}


async def test_an_import_that_changes_nothing_in_use_does_not_reload(live, client):
    services = await exported(client)
    r = await client.post("/admin/api/import", json={"odata_services": services})
    assert r.status_code == 200, r.text
    assert r.json()["updated_odata_services"] == 0
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    # A new service nobody attaches: stored, no reload either.
    extra = {**services[0], "name": "stock"}
    r = await client.post("/admin/api/import", json={"odata_services": [extra]})
    assert r.status_code == 200 and r.json()["created_odata_services"] == 1
    assert r.json()["reloaded"] is False and live.reloads == 0


async def test_a_failing_reload_after_an_import_is_said_not_raised(live, client):
    services = await exported(client)
    services[0]["purpose"] = "Changed by the import"
    live.fail = RuntimeError("boom")
    r = await client.post("/admin/api/import", json={"odata_services": services})
    assert r.status_code == 200, r.text
    assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    assert r.json()["status"] == "imported"


# ------------------------------------------------- C1b: the agent's own entry

AGENTS = "/admin/api/agents"
WRITE = {"operation": "update", "key": {"PurchaseRequisition": "1"}, "body": {"Note": "x"}}


def agent_body(*servers: dict, **patch: Any) -> dict[str, Any]:
    return {
        "name": "buyer",
        "description": "d",
        "instructions": "You help buyers.",
        "mcp_servers": list(servers),
        **patch,
    }


def writable() -> dict[str, Any]:
    data = payload()
    entity_set = data["definition"]["entity_sets"][0]
    entity_set["operations"] = ["list", "get", "update"]
    entity_set["fields"].append({"name": "Note", "selectable": True, "writable": True})
    return data


@pytest.fixture
async def saved(live, client) -> dict[str, Any]:
    """``buyer`` saved through the API with both services and Allow writes,
    the first service writable, and the registry reloaded once by hand."""
    assert (await client.put(ONE, json=writable())).status_code == 200
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.commit()
    r = await client.post(AGENTS, json=agent_body(odata_server(PR, JOBS, allow_write=True)))
    assert r.status_code == 201, r.text
    await live.registry.reload()
    live.reloads = live.refreshes = 0
    return r.json()


async def write_code(agent) -> str:
    out = await call_tool(agent, "execute_operation", service=PR, target=SET, **WRITE)
    return out["error"]["code"]


async def test_unticking_allow_writes_closes_writes_for_the_next_run(live, client, saved):
    assert await write_code(live.buyer) != "write_not_allowed"

    r = await client.put(
        f"{AGENTS}/{saved['id']}", json=agent_body(odata_server(PR, JOBS, allow_write=False))
    )
    assert r.status_code == 200, r.text
    assert r.json()["reloaded"] is True and r.json()["reload_failed"] is False
    assert (live.reloads, live.refreshes) == (1, 1)
    assert await write_code(live.buyer) == "write_not_allowed"


async def test_removing_a_service_from_an_agent_closes_it_for_the_next_run(live, client, saved):
    assert await services_found(live.buyer) == {PR, JOBS}
    r = await client.put(
        f"{AGENTS}/{saved['id']}", json=agent_body(odata_server(JOBS, allow_write=True))
    )
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert await services_found(live.buyer) == {JOBS}
    assert await execute_code(live.buyer) == "unknown_service"


async def test_an_agent_edit_that_leaves_the_odata_entry_alone_does_not_reload(
    live, client, saved
):
    body = agent_body(odata_server(PR, JOBS, allow_write=True), description="another text")
    r = await client.put(f"{AGENTS}/{saved['id']}", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "another text"
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    # The order of the services is not a change either.
    body = agent_body(odata_server(JOBS, PR, allow_write=True))
    r = await client.put(f"{AGENTS}/{saved['id']}", json=body)
    assert r.status_code == 200 and r.json()["reloaded"] is False
    assert (live.reloads, live.refreshes) == (0, 0)


async def test_a_failing_reload_after_an_agent_save_is_said_not_raised(live, client, saved):
    live.fail = RuntimeError("boom at https://aicore.internal/secret-path")
    r = await client.put(
        f"{AGENTS}/{saved['id']}", json=agent_body(odata_server(PR, JOBS, allow_write=False))
    )
    assert r.status_code == 200, r.text
    assert r.json()["reload_failed"] is True and r.json()["reloaded"] is False
    assert "secret-path" not in r.text
    stored = (await client.get(f"{AGENTS}/{saved['id']}")).json()
    assert stored["mcp_servers"][0]["oauth"].get("allow_write") is not True


async def test_creating_and_deleting_an_agent_with_an_odata_entry_reloads(live, client, saved):
    r = await client.post(
        AGENTS, json=agent_body(odata_server(JOBS), name="second", description="s")
    )
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is True and live.reloads == 1
    assert "second" in live.registry.build.specialists

    r = await client.delete(f"{AGENTS}/{r.json()['id']}")
    assert r.status_code == 204 and r.content == b""
    assert r.headers["x-odata-reloaded"] == "true"
    assert r.headers["x-odata-reload-failed"] == "false"
    assert live.reloads == 2 and "second" not in live.registry.build.specialists


async def test_an_agent_without_an_odata_entry_never_reloads(live, client, saved):
    plain = {"url": "builtin:sapnotes", "auth_mode": "none"}
    r = await client.post(AGENTS, json=agent_body(plain, name="notes"))
    assert r.status_code == 201, r.text
    assert r.json()["reloaded"] is False and r.json()["reload_failed"] is False
    r = await client.delete(f"{AGENTS}/{r.json()['id']}")
    assert r.status_code == 204 and r.headers["x-odata-reloaded"] == "false"
    assert live.reloads == 0
