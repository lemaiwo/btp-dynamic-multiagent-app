"""Token consumption of an agent run, stored on its ``job_runs`` row.

The admin run API (``JobRun.to_dict``) answers ``usage``: ``null`` for a run
that has none recorded, else exactly the five counts. Both run sites (the job
runner: scheduler and Run now; the invoke API) pass a ``RunUsage`` into
``specialist.run`` and store its counts on every outcome that got as far as
the model call. A run that never reached the model, or reached it for 0
requests, stores nothing.

The fake specialists add to the ``usage`` they are given, as pydantic-ai does
in place (``Agent.run(usage=...)``; delegations and deep sub-agents share it).

Run:  .venv/bin/python -m pytest tests/test_run_token_usage.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import invoke, job_runner  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    JobRun,
    SessionLocal,
    create_job_run,
    engine,
    finish_job_run,
    init_db,
    upsert_agent,
)
from agents.reports import RunReport  # noqa: E402
from tests.test_invoke_api import SLUG, bound_app, form, make_agent  # noqa: E402

SERVERS = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]
SPENT = RunUsage(requests=3, input_tokens=1200, output_tokens=345,
                 cache_read_tokens=100, tool_calls=2)
EXPECTED = {"input_tokens": 1200, "output_tokens": 345, "cache_read_tokens": 100,
            "total_tokens": 1545, "requests": 3}


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://approuter.example.com")
    monkeypatch.delenv("INVOKE_MAX_CONCURRENT", raising=False)
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(JobRun))
        await s.commit()
    yield
    await invoke.cancel_all_invoke_runs()
    invoke._active.clear()


async def _row(run_id: str) -> JobRun:
    async with SessionLocal() as s:
        row = await s.get(JobRun, run_id)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# The contract of the row and of finish_job_run
# ---------------------------------------------------------------------------
async def _job_agent(**kw: Any) -> AgentConfig:
    kw.setdefault("run_as_principal", "svc@example.com")
    async with SessionLocal() as s:
        return await upsert_agent(
            s, name="Daily", description="d", instructions="i", mcp_servers=SERVERS,
            expose_chat=False, expose_api=True, api_slug="daily", **kw,
        )


async def _new_run(agent: AgentConfig) -> str:
    async with SessionLocal() as s:
        return (await create_job_run(s, agent=agent, trigger="manual")).id


async def test_a_running_or_old_row_answers_usage_null():
    run_id = await _new_run(await _job_agent())
    d = (await _row(run_id)).to_dict()
    assert "usage" in d and d["usage"] is None


async def test_finish_job_run_stores_the_counts_and_to_dict_answers_them():
    run_id = await _new_run(await _job_agent())
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="success", usage={
            "input_tokens": 1200, "output_tokens": 345, "cache_read_tokens": 100,
            "requests": 3})
    row = await _row(run_id)
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens,
            row.requests) == (1200, 345, 100, 3)
    assert row.to_dict()["usage"] == EXPECTED


async def test_finish_job_run_without_usage_leaves_the_columns_null():
    run_id = await _new_run(await _job_agent())
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="failed", error="x", usage=None)
    row = await _row(run_id)
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens,
            row.requests) == (None, None, None, None)
    assert row.to_dict()["usage"] is None


USAGE_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "requests")


async def test_init_db_adds_the_usage_columns_to_an_existing_table():
    run_id = await _new_run(await _job_agent())
    async with engine.begin() as conn:
        for column in USAGE_COLUMNS:
            await conn.exec_driver_sql(f"ALTER TABLE job_runs DROP COLUMN {column}")
    await init_db()
    async with engine.begin() as conn:
        cols = {r[1]: r for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(job_runs)")).fetchall()}
    assert set(USAGE_COLUMNS) <= set(cols)
    # Nullable (notnull flag 0): an older row reads back as usage null.
    assert all(cols[c][3] == 0 for c in USAGE_COLUMNS)
    assert (await _row(run_id)).to_dict()["usage"] is None


def test_usage_counts_of_nothing_is_none_and_never_raises():
    assert job_runner.usage_counts(RunUsage()) is None
    assert job_runner.usage_counts(None) is None
    assert job_runner.usage_counts(object()) is None  # unreadable: no raise
    assert job_runner.usage_counts(SPENT) == {
        "input_tokens": 1200, "output_tokens": 345, "cache_read_tokens": 100,
        "requests": 3}


# ---------------------------------------------------------------------------
# The job runner (scheduler and Run now)
# ---------------------------------------------------------------------------
class JobSpecialist:
    def __init__(self, *, spend: RunUsage | None = SPENT, report: RunReport | None = None,
                 exc: BaseException | None = None, block: bool = False) -> None:
        self.spend, self.report, self.exc, self.block = spend, report, exc, block
        self.kwargs: dict[str, Any] = {}
        self.started = asyncio.Event()

    async def run(self, prompt, **kw):
        self.kwargs = kw
        if self.spend is not None:
            kw["usage"].incr(self.spend)
        self.started.set()
        if self.block:
            await asyncio.sleep(3600)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(output=self.report or RunReport(summary="ok", body_md="# ok"))


def _use_job_specialist(monkeypatch, sp: JobSpecialist, name: str = "Daily") -> None:
    monkeypatch.setattr(job_runner, "registry",
                        SimpleNamespace(build=SimpleNamespace(specialists={name: sp})))


async def test_a_job_run_stores_its_usage_on_success(monkeypatch):
    sp = JobSpecialist()
    _use_job_specialist(monkeypatch, sp)
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    row = await _row(run_id)
    assert row.status == "success"
    assert isinstance(sp.kwargs["usage"], RunUsage)
    assert row.to_dict()["usage"] == EXPECTED


async def test_a_job_run_that_fails_after_the_model_keeps_its_usage(monkeypatch):
    _use_job_specialist(monkeypatch, JobSpecialist(exc=RuntimeError("boom")))
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    row = await _row(run_id)
    assert row.status == "failed" and row.to_dict()["usage"] == EXPECTED


async def test_a_job_run_ending_in_a_signin_prompt_keeps_its_usage(monkeypatch):
    marker = job_runner.SIGNIN_PROMPT_MARKERS[0]
    _use_job_specialist(monkeypatch, JobSpecialist(
        report=RunReport(summary="s", body_md=f"please {marker}")))
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    row = await _row(run_id)
    assert row.status == "failed" and row.to_dict()["usage"] == EXPECTED


async def test_a_job_run_that_times_out_keeps_its_usage(monkeypatch):
    _use_job_specialist(monkeypatch, JobSpecialist(block=True))
    agent = await _job_agent()
    async with SessionLocal() as s:
        row = await s.get(AgentConfig, agent.id)
        row.run_timeout_seconds = 0.05  # type: ignore[assignment]
        await s.commit()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    row = await _row(run_id)
    assert row.status == "failed" and "timeout" in (row.error or "")
    assert row.to_dict()["usage"] == EXPECTED


async def test_a_cancelled_job_run_keeps_its_usage(monkeypatch):
    sp = JobSpecialist(block=True)
    _use_job_specialist(monkeypatch, sp)
    agent = await _job_agent()
    run_id = await _new_run(agent)
    task = asyncio.create_task(job_runner.execute_run(run_id, agent.id))
    await sp.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await _row(run_id)
    assert row.status == "interrupted" and row.to_dict()["usage"] == EXPECTED


async def test_a_job_run_without_a_run_as_principal_stores_no_usage(monkeypatch):
    sp = JobSpecialist()
    _use_job_specialist(monkeypatch, sp)
    agent = await _job_agent(run_as_principal=None)
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    row = await _row(run_id)
    assert row.status == "failed" and not sp.kwargs
    assert row.to_dict()["usage"] is None


async def test_a_job_run_of_an_agent_not_built_stores_no_usage(monkeypatch):
    _use_job_specialist(monkeypatch, JobSpecialist(), name="someone-else")
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    assert (await _row(run_id)).to_dict()["usage"] is None


async def test_a_job_run_with_zero_requests_stores_no_usage(monkeypatch):
    _use_job_specialist(monkeypatch, JobSpecialist(spend=None))
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    row = await _row(run_id)
    assert row.status == "success" and row.to_dict()["usage"] is None
    assert row.input_tokens is None


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_a_job_run_with_a_real_agent_stores_the_whole_tree(monkeypatch):
    """A real pydantic-ai Agent with a delegation that passes ``ctx.usage``:
    the counts on the row are the sum of both agents' requests."""
    inner = Agent(TestModel())
    outer = Agent(TestModel(call_tools=["delegate"]), output_type=RunReport)

    @outer.tool
    async def delegate(ctx: RunContext[None], q: str) -> str:
        return (await inner.run(q, usage=ctx.usage)).output

    _use_job_specialist(monkeypatch, outer)  # type: ignore[arg-type]
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    usage = (await _row(run_id)).to_dict()["usage"]
    # outer: the tool call request and the final answer; inner: one answer.
    assert usage is not None and usage["requests"] == 3
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"] > 0


# ---------------------------------------------------------------------------
# The invoke API
# ---------------------------------------------------------------------------
class InvokeSpecialist:
    def __init__(self, behaviour, spend: RunUsage | None = SPENT) -> None:
        self.behaviour, self.spend = behaviour, spend
        self.kwargs: dict[str, Any] = {}
        self.started = asyncio.Event()

    async def run(self, prompt, **kw):
        self.kwargs = kw
        if self.spend is not None:
            kw["usage"].incr(self.spend)
        self.started.set()
        submit = kw["output_type"][0].output
        return SimpleNamespace(output=await self.behaviour(submit), all_messages=lambda: [])


def _use_invoke_specialist(monkeypatch, sp: InvokeSpecialist, name: str = "expense") -> None:
    monkeypatch.setattr(invoke, "registry",
                        SimpleNamespace(build=SimpleNamespace(specialists={name: sp})))


async def _submits(submit):
    return submit(result={"expense_id": "4711", "status": "draft"})


async def _never_submits(submit):
    return "no result"


async def _explodes(submit):
    raise RuntimeError("boom")


async def _blocks(submit):
    await asyncio.sleep(3600)


async def _invoke(behaviour, monkeypatch, *, spend: RunUsage | None = SPENT,
                  name: str = "expense") -> tuple[dict, JobRun, InvokeSpecialist]:
    sp = InvokeSpecialist(behaviour, spend)
    _use_invoke_specialist(monkeypatch, sp, name)
    await make_agent()
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as client:
        r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
        answer = r.json()
        run_id = (await _latest_run()).id
    return answer, await _row(run_id), sp


async def _latest_run() -> JobRun:
    from sqlalchemy import select
    async with SessionLocal() as s:
        return (await s.execute(select(JobRun))).scalars().one()


async def test_an_invoke_run_stores_its_usage_on_success(monkeypatch):
    answer, row, sp = await _invoke(_submits, monkeypatch)
    assert answer["status"] == "completed"
    assert isinstance(sp.kwargs["usage"], RunUsage)
    assert row.to_dict()["usage"] == EXPECTED
    # The caller's poll answer is unchanged: usage is for the admin only.
    assert "usage" not in answer


async def test_an_invoke_run_without_a_result_keeps_its_usage(monkeypatch):
    answer, row, _ = await _invoke(_never_submits, monkeypatch)
    assert answer["error"] == "no_result" and row.to_dict()["usage"] == EXPECTED


async def test_an_invoke_run_that_fails_after_the_model_keeps_its_usage(monkeypatch):
    answer, row, _ = await _invoke(_explodes, monkeypatch)
    assert answer["error"] == "run_failed" and row.to_dict()["usage"] == EXPECTED


async def test_an_invoke_run_that_times_out_keeps_its_usage(monkeypatch):
    monkeypatch.setattr(invoke, "INVOKE_TIMEOUT_SECONDS", 0.05)
    answer, row, _ = await _invoke(_blocks, monkeypatch)
    assert answer["error"] == "timeout" and row.to_dict()["usage"] == EXPECTED


async def test_a_cancelled_invoke_run_keeps_its_usage(monkeypatch):
    sp = InvokeSpecialist(_blocks)
    _use_invoke_specialist(monkeypatch, sp)
    await make_agent()
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as client:
        await client.post(f"/api/invoke/{SLUG}", **form())
    await sp.started.wait()
    await invoke.cancel_all_invoke_runs()
    row = await _row((await _latest_run()).id)
    assert row.status == "interrupted" and row.to_dict()["usage"] == EXPECTED


async def test_an_invoke_run_of_an_agent_not_built_stores_no_usage(monkeypatch):
    answer, row, sp = await _invoke(_submits, monkeypatch, name="someone-else")
    assert answer["error"] == "agent_not_built" and not sp.kwargs
    assert row.to_dict()["usage"] is None


async def test_an_invoke_run_with_zero_requests_stores_no_usage(monkeypatch):
    answer, row, _ = await _invoke(_submits, monkeypatch, spend=None)
    assert answer["status"] == "completed" and row.to_dict()["usage"] is None
