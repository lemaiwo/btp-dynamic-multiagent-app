"""Run id and agent name at the job, invoke and workflow run sites.

Each site binds the run's id (``nexus_run``) around the whole run and the
agent's name (``calling_agent``) around the specialist's ``run``, so every
model call made inside is logged with both. The specialists here are fakes
whose ``run`` makes one real model call through the logging wrapper.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from tests.testdb import use_test_database

use_test_database()

from pydantic_ai import Agent  # noqa: E402

from agents import invoke, job_runner, workflow_runner  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    JobRun,
    SessionLocal,
    create_job_run,
    get_workflow_by_name,
    init_db,
    upsert_agent,
    upsert_workflow,
)
from agents.nexus import logged_model, register, sender, state  # noqa: E402
from agents.nexus.logged_model import wrap  # noqa: E402
from agents.registry import BuildResult, registry  # noqa: E402
from tests.nexus_helpers import Recorder, register_on, scripted_model  # noqa: E402
from tests.test_invoke_api import SCHEMA, bound_app, form, wait_done  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")
SERVERS = [{"url": "https://mcp.example.test/mcp", "auth_mode": "none"}]


@pytest.fixture
async def rec(monkeypatch):
    monkeypatch.delenv("NEXUS_SEND_PAYLOAD", raising=False)
    monkeypatch.delenv("NEXUS_FALLBACK_USE_CASE", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))

    async def ok(agent, principal=None):
        return True
    monkeypatch.setattr(job_runner, "_has_usable_credentials", ok)
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(JobRun))
        await s.commit()
    r = Recorder()
    await register_on(monkeypatch, r)
    yield r
    await invoke.cancel_all_invoke_runs()
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


async def one_model_call() -> None:
    await Agent(wrap(scripted_model([(3, 1)]), "gpt-4o")).run("x")


class Calling:
    """A specialist whose run makes one model call and returns ``output``."""

    def __init__(self, output):
        self.output = output

    async def run(self, prompt, **kw):
        await one_model_call()
        out = self.output
        if "output_type" in kw and isinstance(kw["output_type"], list):  # invoke
            out = kw["output_type"][0].output(
                result={"expense_id": "4711", "status": "draft"})
        return SimpleNamespace(output=out, all_messages=lambda: [])


def pairs(rec):
    return {(r["body"]["agent"], r["body"]["runId"]) for r in rec.of("logRequest")}


async def test_job_run_logs_the_run_id_and_the_agent_name(rec, monkeypatch):
    from agents.reports import RunReport
    async with SessionLocal() as s:
        job = await upsert_agent(
            s, name="daily-check", description="d", instructions="i",
            mcp_servers=SERVERS, api_slug="daily-check", run_as_principal="svc@example.test")
        run = await create_job_run(s, agent=job, trigger="manual")
    monkeypatch.setattr(registry, "_build", BuildResult(
        orchestrator=None, mcp_clients=[], configs=[],
        specialists={"daily-check": Calling(RunReport(summary="s", body_md="b"))}))
    await job_runner.execute_run(run.id, job.id)
    await sender.drain()
    assert pairs(rec) == {("daily-check", run.id)}


async def test_invoke_run_logs_the_run_id_and_the_agent_name(rec, monkeypatch):
    async with SessionLocal() as s:
        await upsert_agent(
            s, name="expense", description="d", instructions="i", mcp_servers=SERVERS,
            expose_chat=False, api_slug="expense", expose_invoke=True, result_schema=SCHEMA)
    monkeypatch.setattr(invoke, "registry", SimpleNamespace(
        build=SimpleNamespace(specialists={"expense": Calling(None)})))
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        r = await c.post("/api/invoke/expense", **form())
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        answer = await wait_done(c, run_id)
    assert answer["status"] == "completed"
    await sender.drain()
    assert pairs(rec) == {("expense", run_id)}


async def test_workflow_run_logs_the_run_id_and_each_steps_agent(rec, monkeypatch):
    async with SessionLocal() as s:
        for n in ("first", "second"):
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.test")
        name = f"wf-{uuid.uuid4().hex[:8]}"
        await upsert_workflow(
            s, name=name, description="d", enabled=True, branches=[],
            steps=[{"branch_key": None, "position": p, "agent_name": a,
                    "instructions": "go", "fan_out": False, "step_timeout_seconds": 60}
                   for p, a in ((1, "first"), (2, "second"))])
    monkeypatch.setattr(registry, "_build", BuildResult(
        orchestrator=None, mcp_clients=[], configs=[],
        specialists={"first": Calling("one"), "second": Calling("two")}))
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, name)
    run_id = await workflow_runner.start_workflow_run(wf, trigger="manual")
    await asyncio.gather(*[t for t in workflow_runner._tasks if not t.done()],
                         return_exceptions=True)
    await sender.drain()
    assert pairs(rec) == {("first", run_id), ("second", run_id)}
