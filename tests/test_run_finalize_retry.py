"""A run row is the overlap lock: it must not stay `running` after a DB hiccup.

Covers the two halves of that rule for both runners (agents/job_runner.py,
agents/workflow_runner.py):

* ``_finalize`` retries a failed write of the outcome a few times with a short
  backoff, and still never raises;
* the next trigger sweeps a `running` row older than its timeout before the
  overlap check, so a wedged row no longer answers every trigger 409 until the
  app restarts. A failing sweep never blocks a start.

Plus the A2A run site passing the app's usage limits (``run_usage_limits``).

Run:  .venv/bin/python -m pytest tests/test_run_finalize_retry.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import a2a, job_runner, workflow_runner  # noqa: E402
from agents.db import (  # noqa: E402
    JobRun,
    SessionLocal,
    WorkflowRun,
    create_job_run,
    create_workflow_run,
    get_agent_by_name,
    init_db,
    upsert_agent,
    upsert_workflow,
)
from agents.shared import AGENT_REQUEST_LIMIT  # noqa: E402

SERVERS = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]

RUNNERS = [
    pytest.param(job_runner, "finish_job_run", id="job"),
    pytest.param(workflow_runner, "finish_workflow_run", id="workflow"),
]


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """The backoff is real seconds in production; the tests do not wait."""
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(workflow_runner, "FINALIZE_RETRY_DELAYS", (0, 0))


@pytest.fixture(autouse=True)
def _no_background_run(monkeypatch):
    """start_* launches the run as a task; these tests only look at the start."""
    started: list[tuple] = []

    async def _noop(*args):
        started.append(args)

    monkeypatch.setattr(job_runner, "execute_run", _noop)
    monkeypatch.setattr(workflow_runner, "execute_workflow_run", _noop)
    return started


# --- _finalize retries ------------------------------------------------------

@pytest.mark.parametrize("runner,finish_name", RUNNERS)
async def test_finalize_succeeds_on_second_attempt(runner, finish_name, monkeypatch):
    calls: list[dict] = []

    async def flaky(session, run_id, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("connection reset")

    monkeypatch.setattr(runner, finish_name, flaky)
    await runner._finalize("run-1", status="success", summary="ok")
    assert len(calls) == 2
    assert calls[1]["status"] == "success"


@pytest.mark.parametrize("runner,finish_name", RUNNERS)
async def test_finalize_gives_up_after_three_attempts_without_raising(
    runner, finish_name, monkeypatch, caplog
):
    calls: list[str] = []

    async def broken(session, run_id, **kwargs):
        calls.append(run_id)
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(runner, finish_name, broken)
    await runner._finalize("run-2", status="failed", error="x")
    assert len(calls) == 3
    assert any("run-2" in r.getMessage() and r.levelname == "ERROR"
               for r in caplog.records)


@pytest.mark.parametrize("runner,finish_name", RUNNERS)
async def test_finalize_waits_between_attempts(runner, finish_name, monkeypatch):
    monkeypatch.setattr(runner, "FINALIZE_RETRY_DELAYS", (0.5, 1.0))
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    async def broken(session, run_id, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(runner, finish_name, broken)
    await runner._finalize("run-3", status="failed")
    assert slept == [0.5, 1.0]


@pytest.mark.parametrize("runner,finish_name", RUNNERS)
async def test_finalize_stops_retrying_when_cancelled(runner, finish_name, monkeypatch):
    """A cancel during the backoff (shutdown) ends the retries; the startup
    sweep owns the row then."""
    calls: list[str] = []

    async def cancelled_sleep(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(runner.asyncio, "sleep", cancelled_sleep)

    async def broken(session, run_id, **kwargs):
        calls.append(run_id)
        raise RuntimeError("down")

    monkeypatch.setattr(runner, finish_name, broken)
    with pytest.raises(asyncio.CancelledError):
        await runner._finalize("run-4", status="interrupted")
    assert len(calls) == 1


# --- the next start sweeps a stale row --------------------------------------

async def _agent(name: str):
    await init_db()
    async with SessionLocal() as s:
        await upsert_agent(
            s, name=name, description="d", instructions="i",
            mcp_servers=SERVERS, expose_chat=False, expose_api=True,
            api_slug=name.lower(), run_as_principal="svc@example.com",
        )
        return await get_agent_by_name(s, name)


async def _job_row(agent, *, age_s: int) -> str:
    async with SessionLocal() as s:
        run = await create_job_run(s, agent=agent, trigger="manual")
        row = await s.get(JobRun, run.id)
        row.started_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        await s.commit()
        return run.id


async def test_start_run_sweeps_a_stale_running_row(_no_background_run):
    agent = await _agent(f"Stale{uuid.uuid4().hex[:8]}")
    stale_id = await _job_row(agent, age_s=agent.run_timeout_seconds + 60)

    new_id = await job_runner.start_run(agent, trigger="manual")
    await asyncio.sleep(0)

    assert new_id != stale_id
    async with SessionLocal() as s:
        stale = await s.get(JobRun, stale_id)
        assert stale.status == "interrupted"
        assert (await s.get(JobRun, new_id)).status == "running"


async def test_start_run_still_refuses_a_young_running_row():
    agent = await _agent(f"Young{uuid.uuid4().hex[:8]}")
    young_id = await _job_row(agent, age_s=5)
    with pytest.raises(job_runner.RunRefused):
        await job_runner.start_run(agent, trigger="manual")
    async with SessionLocal() as s:
        assert (await s.get(JobRun, young_id)).status == "running"


async def test_start_run_proceeds_when_the_sweep_fails(monkeypatch):
    agent = await _agent(f"SweepFail{uuid.uuid4().hex[:8]}")

    async def broken_sweep(session, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(job_runner, "sweep_stale_runs", broken_sweep)
    run_id = await job_runner.start_run(agent, trigger="manual")
    async with SessionLocal() as s:
        assert (await s.get(JobRun, run_id)).status == "running"


async def _workflow(name: str, *, timeout: int = 600):
    reader = await _agent(f"Reader{uuid.uuid4().hex[:8]}")
    async with SessionLocal() as s:
        return await upsert_workflow(
            s, name=name, description="d", enabled=True,
            run_timeout_seconds=timeout,
            steps=[{"branch_key": None, "position": 1, "agent_name": reader.name,
                    "instructions": "go", "fan_out": False,
                    "step_timeout_seconds": 300}],
        )


async def _workflow_row(wf, *, age_s: int) -> str:
    async with SessionLocal() as s:
        run = await create_workflow_run(s, workflow=wf, trigger="manual")
        row = await s.get(WorkflowRun, run.id)
        row.started_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        await s.commit()
        return run.id


async def test_start_workflow_run_sweeps_a_stale_running_row():
    wf = await _workflow(f"wf-{uuid.uuid4().hex[:8]}", timeout=600)
    stale_id = await _workflow_row(wf, age_s=700)

    new_id = await workflow_runner.start_workflow_run(wf, trigger="manual")

    async with SessionLocal() as s:
        assert (await s.get(WorkflowRun, stale_id)).status == "interrupted"
        assert (await s.get(WorkflowRun, new_id)).status == "running"


async def test_start_workflow_run_still_refuses_a_young_running_row():
    wf = await _workflow(f"wf-{uuid.uuid4().hex[:8]}", timeout=600)
    await _workflow_row(wf, age_s=5)
    with pytest.raises(workflow_runner.RunRefused):
        await workflow_runner.start_workflow_run(wf, trigger="manual")


async def test_start_workflow_run_proceeds_when_the_sweep_fails(monkeypatch):
    wf = await _workflow(f"wf-{uuid.uuid4().hex[:8]}")

    async def broken_sweep(session, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(workflow_runner, "sweep_stale_workflow_runs", broken_sweep)
    run_id = await workflow_runner.start_workflow_run(wf, trigger="manual")
    async with SessionLocal() as s:
        assert (await s.get(WorkflowRun, run_id)).status == "running"


# --- A2A passes the app's usage limits ---------------------------------------

async def test_a2a_run_passes_the_app_usage_limits(monkeypatch):
    seen: dict = {}

    class _Result:
        output = "done"

        def all_messages(self):
            return []

    class _Orchestrator:
        async def run(self, text, **kwargs):
            seen.update(kwargs)
            return _Result()

    class _Registry:
        orchestrator = _Orchestrator()

    monkeypatch.setattr(a2a, "registry", _Registry())
    out = await a2a._run_orchestrator("hello", f"ctx-{uuid.uuid4().hex}")
    assert out == "done"
    limits = seen.get("usage_limits")
    assert limits is not None
    assert limits.request_limit == AGENT_REQUEST_LIMIT
    assert AGENT_REQUEST_LIMIT > 50
