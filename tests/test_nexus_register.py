"""The agent-register registration push and the assignment cache."""
import asyncio
import logging
from types import SimpleNamespace as Row

import pytest

from agents.nexus import register
from agents.nexus.client import RegisterClient
from tests.nexus_helpers import FakeResolver, Recorder


def row(name, enabled=True, description="d", model_name=None):
    return Row(name=name, enabled=enabled, description=description, model_name=model_name)


async def _async(value):
    return value


def _assigned(*names, status="ASSIGNED"):
    return {"value": [{"name": n, "useCase": f"UC_{n.upper()}", "resourceGroup": "rg-1",
                       "status": status} for n in names]}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("NEXUS_FALLBACK_USE_CASE", raising=False)
    register.reset_for_tests()
    yield
    register.reset_for_tests()


def test_list_has_rows_plus_two_fixed_entries():
    out = register.build_agent_list([row("invoice-assistant", model_name="gpt-4o"),
                                     row("mail-triage", enabled=False)])
    assert [a["name"] for a in out] == ["invoice-assistant", "mail-triage", "orchestrator",
                                        "personal-agent"]
    assert out[0] == {"name": "invoice-assistant", "type": "PYDANTIC", "description": "d",
                      "model": "gpt-4o", "active": True}
    assert out[1]["active"] is False
    assert out[2] == {"name": "orchestrator", "type": "PYDANTIC",
                      "description": register.ORCHESTRATOR_DESCRIPTION, "model": "",
                      "active": True}
    assert out[3]["description"] == register.PERSONAL_DESCRIPTION


def test_description_cut_model_over_120_blank_and_collisions(caplog):
    caplog.set_level(logging.WARNING, logger="agents.nexus.register")
    out = register.build_agent_list([row("a", description="x" * 2500, model_name="m" * 121),
                                     row("orchestrator"), row(" a "), row("n" * 65),
                                     row("  ", description=None)])
    names = [a["name"] for a in out]
    assert names == ["a", "orchestrator", "personal-agent"]
    assert len(out[0]["description"]) == 2000 and out[0]["model"] == ""
    assert out[1]["description"] == "d"          # the admin row won over the fixed entry
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 5                    # model, orchestrator, " a ", too long, blank
    assert all("m" * 121 not in w for w in warnings)


async def test_push_fills_cache_and_inactivates_removed(monkeypatch):
    rec = Recorder(answers={"registerAgents": {"value": [
        {"name": "invoice-assistant", "useCase": "INVOICE_QA", "resourceGroup": "rg-1",
         "status": "ASSIGNED"}]}})
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    rows = [[row("invoice-assistant"), row("old")], [row("invoice-assistant")]]
    monkeypatch.setattr(register, "_load_rows", lambda: _async(rows.pop(0)))
    assert await register.push_once()
    assert register.use_case_for("invoice-assistant") == "INVOICE_QA"
    assert register.assignment_for("invoice-assistant") == register.Assignment(
        "INVOICE_QA", "rg-1", "ASSIGNED")
    assert await register.push_once()
    second = rec.of("registerAgents")[1]["body"]["agents"]
    assert {"name": "old", "type": "PYDANTIC", "active": False} in second
    assert rec.requests[0]["auth"] == "Bearer tech-token"


async def test_removed_name_is_sent_inactive_once(monkeypatch):
    rec = Recorder(answers={"registerAgents": {"value": []}})
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    rows = [[row("old")], [], []]
    monkeypatch.setattr(register, "_load_rows", lambda: _async(rows.pop(0)))
    for _ in range(3):
        assert await register.push_once()
    calls = [c["body"]["agents"] for c in rec.of("registerAgents")]
    assert {"name": "old", "type": "PYDANTIC", "active": False} in calls[1]
    assert all(a["name"] != "old" for a in calls[2])


async def test_failed_push_keeps_cache(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agents.nexus.register")
    rec = Recorder(answers={"registerAgents": _assigned("invoice-assistant")})
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    rows = [[row("invoice-assistant"), row("old")], [row("invoice-assistant")],
            [row("invoice-assistant")]]
    monkeypatch.setattr(register, "_load_rows", lambda: _async(rows.pop(0)))
    assert await register.push_once()
    rec.statuses["registerAgents"] = 500
    assert not await register.push_once()
    assert register.use_case_for("invoice-assistant") == "UC_INVOICE-ASSISTANT"
    # "old" was not confirmed inactive: the next push still names it.
    rec.statuses.pop("registerAgents")
    assert await register.push_once()
    third = rec.of("registerAgents")[2]["body"]["agents"]
    assert {"name": "old", "type": "PYDANTIC", "active": False} in third
    assert "register.example.test" not in caplog.text and "Bearer" not in caplog.text


async def test_failed_row_read_keeps_cache(monkeypatch):
    rec = Recorder(answers={"registerAgents": _assigned("invoice-assistant")})
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    monkeypatch.setattr(register, "_load_rows", lambda: _async([row("invoice-assistant")]))
    assert await register.push_once()

    async def boom():
        raise RuntimeError("postgresql://user:pw@host/db")
    monkeypatch.setattr(register, "_load_rows", boom)
    assert not await register.push_once()
    assert register.use_case_for("invoice-assistant") == "UC_INVOICE-ASSISTANT"


async def test_batches_of_500(monkeypatch):
    rec = Recorder(answers={"registerAgents": {"value": []}})
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    rows = [row(f"agent-{i:04d}") for i in range(1203)]
    monkeypatch.setattr(register, "_load_rows", lambda: _async(rows))
    assert await register.push_once()
    sizes = [len(c["body"]["agents"]) for c in rec.of("registerAgents")]
    assert sizes == [500, 500, 205]


async def test_one_failed_batch_fails_the_push(monkeypatch):
    calls = []

    class HalfClient:
        async def register(self, agents):
            calls.append(len(agents))
            return [] if len(calls) == 1 else None

    register.configure(HalfClient())
    monkeypatch.setattr(register, "_load_rows",
                        lambda: _async([row(f"agent-{i:04d}") for i in range(600)]))
    assert not await register.push_once()
    assert calls == [500, 102]
    assert register.use_case_for("agent-0001") is None


async def test_fallback_use_case_and_none(monkeypatch):
    rec = Recorder(answers={"registerAgents": _assigned("invoice-assistant", status="UNASSIGNED")})
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    monkeypatch.setattr(register, "_load_rows", lambda: _async([row("invoice-assistant")]))
    assert await register.push_once()
    assert register.use_case_for("invoice-assistant") is None
    assert register.use_case_for("unknown") is None
    monkeypatch.setenv("NEXUS_FALLBACK_USE_CASE", "GENERAL_QA")
    register.configure(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    assert register.use_case_for("invoice-assistant") == "GENERAL_QA"
    assert register.use_case_for("unknown") == "GENERAL_QA"


async def test_many_requests_are_one_push(monkeypatch):
    rec = Recorder(answers={"registerAgents": {"value": []}})
    monkeypatch.setattr(register, "_load_rows", lambda: _async([row("invoice-assistant")]))
    register.start_pusher(RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()))
    for _ in range(10):
        register.request_push()
    await register.settle()
    await asyncio.sleep(0.05)
    assert len(rec.of("registerAgents")) == 1
    register.request_push()
    await register.settle()
    assert len(rec.of("registerAgents")) == 2
    await register.stop_pusher()
    register.request_push()                       # no pusher: a no-op
    assert len(rec.of("registerAgents")) == 2


def test_request_push_without_pusher_is_a_no_op():
    register.request_push()


async def test_load_rows_reads_the_registry():
    from agents.db import SessionLocal, delete_agent, get_agent_by_name, init_db, upsert_agent

    await init_db()
    async with SessionLocal() as s:
        await upsert_agent(s, name="register-probe-agent", description="probe",
                           instructions="x",
                           mcp_servers=[{"url": "https://register.example.test/mcp",
                                         "auth_mode": "none"}])
    try:
        names = [r.name for r in await register._load_rows()]
        assert "register-probe-agent" in names
    finally:
        async with SessionLocal() as s:
            found = await get_agent_by_name(s, "register-probe-agent")
            if found is not None:
                await delete_agent(s, found.id)
