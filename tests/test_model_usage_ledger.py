"""The token ledger per model (``model_usage``, cost in euros part 1).

``finish_job_run(..., model_usage=[...])`` stages one ``ModelUsage`` row per
model in the run's own commit, replacing what ``(source, ref_id)`` held
(``_finalize`` retries). The job runner and the invoke API bind a collector
(``agents.model_usage.collecting``) around the run, so every outcome that got
as far as the model stores its rows; a cancel recorded by invoke's
``_interrupted`` writes them only when its conditional update reached the row.

The fake specialists call ``agents.model_usage.add`` the way the model
wrapper does for each answered request, and fill ``usage`` as pydantic-ai
does, with the same totals.

Run:  .venv/bin/python -m pytest tests/test_model_usage_ledger.py -q
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
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import invoke, job_runner, model_usage  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    JobRun,
    ModelUsage,
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
# The same three requests, split per model as the wrapper reports them.
LEDGER = [
    {"model": "model-a", "input_tokens": 1000, "output_tokens": 300,
     "cache_read_tokens": 100, "requests": 1},
    {"model": "model-b", "input_tokens": 200, "output_tokens": 45,
     "cache_read_tokens": 0, "requests": 2},
]


def _spend_per_model() -> None:
    model_usage.add("model-a", RequestUsage(input_tokens=1000, output_tokens=300,
                                            cache_read_tokens=100))
    model_usage.add("model-b", RequestUsage(input_tokens=120, output_tokens=25))
    model_usage.add("model-b", RequestUsage(input_tokens=80, output_tokens=20))


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://approuter.example.com")
    monkeypatch.delenv("INVOKE_MAX_CONCURRENT", raising=False)
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig))
        await s.execute(delete(JobRun))
        await s.execute(delete(ModelUsage))
        await s.commit()
    yield
    await invoke.cancel_all_invoke_runs()
    invoke._active.clear()


async def _row(run_id: str) -> JobRun:
    async with SessionLocal() as s:
        row = await s.get(JobRun, run_id)
    assert row is not None
    return row


async def _ledger(run_id: str) -> list[dict[str, Any]]:
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(ModelUsage).where(ModelUsage.ref_id == run_id)
            .order_by(ModelUsage.model))).scalars().all()
    return [{"model": r.model, "input_tokens": r.input_tokens,
             "output_tokens": r.output_tokens, "cache_read_tokens": r.cache_read_tokens,
             "requests": r.requests, "source": r.source, "parent_id": r.parent_id}
            for r in rows]


def _as_stored(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "source": "job", "parent_id": None} for r in rows]


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


# ---------------------------------------------------------------------------
# finish_job_run and the stored rows
# ---------------------------------------------------------------------------
async def test_finish_job_run_writes_one_row_per_model_stamped_with_finished_at():
    run_id = await _new_run(await _job_agent())
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="success", model_usage=LEDGER)
    assert await _ledger(run_id) == _as_stored(LEDGER)
    row = await _row(run_id)
    async with SessionLocal() as s:
        stamps = (await s.execute(select(ModelUsage.created_at)
                                  .where(ModelUsage.ref_id == run_id))).scalars().all()
    assert stamps and all(st == row.finished_at for st in stamps)


async def test_a_second_finish_replaces_the_rows_of_the_run():
    run_id = await _new_run(await _job_agent())
    for _ in range(2):
        async with SessionLocal() as s:
            await finish_job_run(s, run_id, status="success", model_usage=LEDGER)
    assert await _ledger(run_id) == _as_stored(LEDGER)
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="success", model_usage=LEDGER[:1])
    assert await _ledger(run_id) == _as_stored(LEDGER[:1])


async def test_finish_without_model_usage_leaves_the_ledger_alone():
    run_id = await _new_run(await _job_agent())
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="success", model_usage=LEDGER)
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="failed", error="x", model_usage=None)
    assert await _ledger(run_id) == _as_stored(LEDGER)
    other = await _new_run(await _job_agent())
    async with SessionLocal() as s:
        await finish_job_run(s, other, status="failed", error="x")
    assert await _ledger(other) == []


async def test_a_model_without_requests_is_not_stored():
    run_id = await _new_run(await _job_agent())
    rows = [*LEDGER, {"model": "idle", "input_tokens": 0, "output_tokens": 0,
                      "cache_read_tokens": 0, "requests": 0}]
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="success", model_usage=rows)
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_init_db_adds_the_price_columns_to_an_existing_orchestrator_table():
    columns = ("capacity_unit_price", "prices_updated_at")
    async with engine.begin() as conn:
        for column in columns:
            await conn.exec_driver_sql(f"ALTER TABLE orchestrator_config DROP COLUMN {column}")
    await init_db()
    async with engine.begin() as conn:
        cols = {r[1]: r for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(orchestrator_config)")).fetchall()}
    assert set(columns) <= set(cols)
    assert all(cols[c][3] == 0 for c in columns)  # nullable


# ---------------------------------------------------------------------------
# The job runner
# ---------------------------------------------------------------------------
class JobSpecialist:
    def __init__(self, *, report: RunReport | None = None,
                 exc: BaseException | None = None, block: bool = False) -> None:
        self.report, self.exc, self.block = report, exc, block
        self.kwargs: dict[str, Any] = {}
        self.started = asyncio.Event()

    async def run(self, prompt, **kw):
        self.kwargs = kw
        _spend_per_model()
        kw["usage"].incr(SPENT)
        self.started.set()
        if self.block:
            await asyncio.sleep(3600)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(output=self.report or RunReport(summary="ok", body_md="# ok"))


def _use_job_specialist(monkeypatch, sp: Any, name: str = "Daily") -> None:
    monkeypatch.setattr(job_runner, "registry",
                        SimpleNamespace(build=SimpleNamespace(specialists={name: sp})))


async def _run_job(monkeypatch, sp: Any, *, timeout: float | None = None,
                   **agent_kw: Any) -> str:
    _use_job_specialist(monkeypatch, sp)
    agent = await _job_agent(**agent_kw)
    if timeout is not None:
        async with SessionLocal() as s:
            row = await s.get(AgentConfig, agent.id)
            row.run_timeout_seconds = timeout  # type: ignore[assignment]
            await s.commit()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    return run_id


async def test_a_job_run_stores_its_ledger_on_success(monkeypatch):
    run_id = await _run_job(monkeypatch, JobSpecialist())
    assert (await _row(run_id)).status == "success"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_a_job_run_that_fails_after_the_model_keeps_its_ledger(monkeypatch):
    run_id = await _run_job(monkeypatch, JobSpecialist(exc=RuntimeError("boom")))
    assert (await _row(run_id)).status == "failed"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_a_job_run_ending_in_a_signin_prompt_keeps_its_ledger(monkeypatch):
    marker = job_runner.SIGNIN_PROMPT_MARKERS[0]
    run_id = await _run_job(monkeypatch, JobSpecialist(
        report=RunReport(summary="s", body_md=f"please {marker}")))
    assert (await _row(run_id)).status == "failed"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_a_job_run_that_times_out_keeps_its_ledger(monkeypatch):
    run_id = await _run_job(monkeypatch, JobSpecialist(block=True), timeout=0.05)
    assert "timeout" in ((await _row(run_id)).error or "")
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_a_cancelled_job_run_keeps_its_ledger(monkeypatch):
    sp = JobSpecialist(block=True)
    _use_job_specialist(monkeypatch, sp)
    agent = await _job_agent()
    run_id = await _new_run(agent)
    task = asyncio.create_task(job_runner.execute_run(run_id, agent.id))
    await sp.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await _row(run_id)).status == "interrupted"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_a_job_run_without_a_run_as_principal_stores_no_ledger(monkeypatch):
    sp = JobSpecialist()
    run_id = await _run_job(monkeypatch, sp, run_as_principal=None)
    assert (await _row(run_id)).status == "failed" and not sp.kwargs
    assert await _ledger(run_id) == []


async def test_a_job_run_of_an_agent_not_built_stores_no_ledger(monkeypatch):
    _use_job_specialist(monkeypatch, JobSpecialist(), name="someone-else")
    agent = await _job_agent()
    run_id = await _new_run(agent)
    await job_runner.execute_run(run_id, agent.id)
    assert await _ledger(run_id) == []


async def test_the_collector_is_not_left_bound_after_a_job_run(monkeypatch):
    await _run_job(monkeypatch, JobSpecialist())
    assert model_usage.current() is None


@pytest.mark.usefixtures("real_agents_and_mcp")
async def test_the_ledger_of_a_real_agent_equals_the_run_row(monkeypatch):
    """D4: the wrapper adds exactly the requests pydantic-ai's RunUsage counts."""
    from agents.nexus.logged_model import NexusLoggedModel

    specialist = Agent(NexusLoggedModel(TestModel(), "model-a"), output_type=RunReport)
    run_id = await _run_job(monkeypatch, specialist)
    row = await _row(run_id)
    ledger = await _ledger(run_id)
    assert row.status == "success" and row.requests and row.requests > 0
    assert [r["model"] for r in ledger] == ["model-a"]
    assert (ledger[0]["input_tokens"], ledger[0]["output_tokens"],
            ledger[0]["cache_read_tokens"], ledger[0]["requests"]) == (
        row.input_tokens, row.output_tokens, row.cache_read_tokens, row.requests)


# ---------------------------------------------------------------------------
# The invoke API
# ---------------------------------------------------------------------------
class InvokeSpecialist:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.kwargs: dict[str, Any] = {}
        self.started = asyncio.Event()

    async def run(self, prompt, **kw):
        self.kwargs = kw
        _spend_per_model()
        kw["usage"].incr(SPENT)
        self.started.set()
        submit = kw["output_type"][0].output
        return SimpleNamespace(output=await self.behaviour(submit), all_messages=lambda: [])


def _use_invoke_specialist(monkeypatch, sp: InvokeSpecialist) -> None:
    monkeypatch.setattr(invoke, "registry",
                        SimpleNamespace(build=SimpleNamespace(specialists={"expense": sp})))


async def _submits(submit):
    return submit(result={"expense_id": "4711", "status": "draft"})


async def _never_submits(submit):
    return "no result"


async def _blocks(submit):
    await asyncio.sleep(3600)


async def _latest_run() -> JobRun:
    async with SessionLocal() as s:
        return (await s.execute(select(JobRun))).scalars().one()


async def _invoke(behaviour, monkeypatch) -> tuple[dict, str]:
    _use_invoke_specialist(monkeypatch, InvokeSpecialist(behaviour))
    await make_agent()
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as client:
        r = await client.post(f"/api/invoke/{SLUG}?wait=5", **form())
    return r.json(), (await _latest_run()).id


async def test_an_invoke_run_stores_its_ledger_on_success(monkeypatch):
    answer, run_id = await _invoke(_submits, monkeypatch)
    assert answer["status"] == "completed"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_an_invoke_run_without_a_result_keeps_its_ledger(monkeypatch):
    answer, run_id = await _invoke(_never_submits, monkeypatch)
    assert answer["error"] == "no_result"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_an_invoke_run_that_times_out_keeps_its_ledger(monkeypatch):
    monkeypatch.setattr(invoke, "INVOKE_TIMEOUT_SECONDS", 0.05)
    answer, run_id = await _invoke(_blocks, monkeypatch)
    assert answer["error"] == "timeout"
    assert await _ledger(run_id) == _as_stored(LEDGER)


async def test_a_cancelled_invoke_run_keeps_its_ledger(monkeypatch):
    sp = InvokeSpecialist(_blocks)
    _use_invoke_specialist(monkeypatch, sp)
    await make_agent()
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as client:
        await client.post(f"/api/invoke/{SLUG}", **form())
    await sp.started.wait()
    await invoke.cancel_all_invoke_runs()
    run_id = (await _latest_run()).id
    assert (await _row(run_id)).status == "interrupted"
    assert await _ledger(run_id) == _as_stored(LEDGER)


def _state_with_spend(rows_of: str = "model-a") -> invoke._RunState:
    state = invoke._RunState()
    with model_usage.collecting(state.collector):
        model_usage.add(rows_of, RequestUsage(input_tokens=7, output_tokens=3))
    return state


async def test_interrupted_writes_the_ledger_when_it_closes_the_row():
    agent = await make_agent()
    run_id = await _new_run(agent)
    await invoke._interrupted(run_id, _state_with_spend())
    assert (await _row(run_id)).status == "interrupted"
    assert await _ledger(run_id) == [{
        "model": "model-a", "input_tokens": 7, "output_tokens": 3,
        "cache_read_tokens": 0, "requests": 1, "source": "job", "parent_id": None}]


async def test_interrupted_after_finalize_leaves_the_stored_ledger_alone():
    agent = await make_agent()
    run_id = await _new_run(agent)
    async with SessionLocal() as s:
        await finish_job_run(s, run_id, status="success", model_usage=LEDGER)
    await invoke._interrupted(run_id, _state_with_spend("model-z"))
    assert (await _row(run_id)).status == "success"
    assert await _ledger(run_id) == _as_stored(LEDGER)
