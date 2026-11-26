"""Token consumption of workflow runs.

Each agent step's ``workflow_step_runs`` row stores what its run spent (the
step's own ``RunUsage``, passed into ``specialist.run``); deterministic step
kinds store nothing. The run's total is not stored: the admin routes sum the
step rows when they read them, one grouped statement for a list.

The fakes follow tests/test_workflow_runner.py: a built specialist is a
``FakeAgent`` that adds to the ``usage`` it is given, as pydantic-ai does in
place, and answers a scripted output.

Run:  .venv/bin/python -m pytest tests/test_workflow_token_usage.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete, event

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

import agents.workflow_runner as wr  # noqa: E402
from agents import admin, job_runner  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    SessionLocal,
    Workflow,
    WorkflowBranch,
    WorkflowItemRun,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    engine,
    finish_step_run,
    get_workflow_by_name,
    init_db,
    list_step_runs,
    upsert_agent,
    upsert_workflow,
)
from agents.registry import BuildResult, registry  # noqa: E402

SERVERS = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]
KEYS = {"input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "requests"}


def spend(n: int) -> RunUsage:
    """``n`` requests of 100 in / 10 out / 5 cached each."""
    return RunUsage(requests=n, input_tokens=100 * n, output_tokens=10 * n,
                    cache_read_tokens=5 * n)


def expected(n: int) -> dict[str, int]:
    return {"input_tokens": 100 * n, "output_tokens": 10 * n, "cache_read_tokens": 5 * n,
            "total_tokens": 110 * n, "requests": n}


class FakeResult:
    def __init__(self, output):
        self.output = output


class FakeAgent:
    def __init__(self, name, answers=None, *, spend_per_call: RunUsage | None = None,
                 fail_with: BaseException | None = None):
        self.name = name
        self.answers = list(answers or [])
        self.spend = spend_per_call
        self.fail_with = fail_with
        self.usages: list[RunUsage] = []

    async def run(self, prompt, **kwargs):
        usage = kwargs["usage"]
        self.usages.append(usage)
        if self.spend is not None:
            usage.incr(self.spend)
        if self.fail_with is not None:
            raise self.fail_with
        return FakeResult(self.answers.pop(0) if self.answers else f"{self.name}-out")


def install(mapping):
    registry._build = BuildResult(
        orchestrator=None, specialists=dict(mapping), mcp_clients=[], configs=[]
    )


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(wr, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(job_runner, "_has_usable_credentials",
                        lambda agent, principal=None: asyncio.sleep(0, result=True),
                        raising=False)
    saved = registry._build
    await init_db()
    async with SessionLocal() as s:
        for model in (WorkflowStepRun, WorkflowItemRun, WorkflowRun, WorkflowStep,
                      WorkflowBranch, Workflow, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
        for n in ("first", "second", "reader", "brancher"):
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.com")
    yield
    registry._build = saved


def step(position, kind="agent", agent="", branch=None, config=None, **over):
    base = {"branch_key": branch, "position": position, "agent_name": agent,
            "instructions": f"do step {position}", "fan_out": False,
            "step_timeout_seconds": 60, "kind": kind, "config": config or {}}
    base.update(over)
    return base


async def run_workflow(name, steps, branches=()) -> str:
    async with SessionLocal() as s:
        await upsert_workflow(s, name=name, description="d", enabled=True,
                              branches=list(branches), skip_seen_items=False, steps=steps)
        wf = await get_workflow_by_name(s, name)
    run_id = await wr.start_workflow_run(wf, trigger="manual")
    await asyncio.gather(*[t for t in wr._tasks if not t.done()], return_exceptions=True)
    return run_id


async def steps_of(run_id):
    async with SessionLocal() as s:
        return await list_step_runs(s, run_id)


# ---------------------------------------------------------------------------
# Step rows
# ---------------------------------------------------------------------------
async def test_an_agent_step_stores_its_usage_and_a_transform_stores_none():
    first = FakeAgent("first", ['{"a": "b"}'], spend_per_call=spend(2))
    second = FakeAgent("second", ["done"], spend_per_call=spend(1))
    install({"first": first, "second": second})
    run_id = await run_workflow("line", [
        step(1, agent="first"),
        step(2, kind="transform", config={"extract_json": "a"}),
        step(3, agent="second"),
    ])
    steps = {s.position: s for s in await steps_of(run_id)}
    assert steps[1].to_dict()["usage"] == expected(2)
    assert steps[2].to_dict()["usage"] is None and steps[2].input_tokens is None
    assert steps[3].to_dict()["usage"] == expected(1)
    # One fresh RunUsage per agent step, never shared between steps.
    assert first.usages[0] is not second.usages[0]


async def test_a_step_that_fails_after_the_model_keeps_its_usage():
    install({"first": FakeAgent("first", spend_per_call=spend(3),
                                fail_with=RuntimeError("boom"))})
    run_id = await run_workflow("fails", [step(1, agent="first")])
    (only,) = await steps_of(run_id)
    assert only.status == "failed" and only.to_dict()["usage"] == expected(3)


async def test_a_step_that_times_out_keeps_its_usage():
    # wait_for raises this same TimeoutError when the step's timeout expires.
    install({"first": FakeAgent("first", spend_per_call=spend(2),
                                fail_with=asyncio.TimeoutError())})
    run_id = await run_workflow("slow", [step(1, agent="first")])
    (only,) = await steps_of(run_id)
    assert only.status == "failed" and "timeout" in (only.error or "")
    assert only.to_dict()["usage"] == expected(2)


async def test_a_cancelled_step_keeps_its_usage():
    install({"first": FakeAgent("first", spend_per_call=spend(4),
                                fail_with=asyncio.CancelledError())})
    run_id = await run_workflow("cancelled", [step(1, agent="first")])
    (only,) = await steps_of(run_id)
    assert only.status == "interrupted" and only.to_dict()["usage"] == expected(4)


async def test_a_step_with_zero_requests_stores_no_usage():
    install({"first": FakeAgent("first", ["x"])})
    run_id = await run_workflow("zero", [step(1, agent="first")])
    (only,) = await steps_of(run_id)
    assert only.status == "success" and only.to_dict()["usage"] is None


async def test_finish_step_run_without_usage_leaves_the_columns_null():
    install({"first": FakeAgent("first", ["x"])})
    run_id = await run_workflow("plain", [step(1, agent="first")])
    (only,) = await steps_of(run_id)
    async with SessionLocal() as s:
        await finish_step_run(s, only.id, status="success", output="x", usage=None)
    (only,) = await steps_of(run_id)
    assert (only.input_tokens, only.output_tokens, only.cache_read_tokens,
            only.requests) == (None, None, None, None)


USAGE_COLUMNS = ("input_tokens", "output_tokens", "cache_read_tokens", "requests")


async def test_init_db_adds_the_usage_columns_to_an_existing_table():
    async with engine.begin() as conn:
        for column in USAGE_COLUMNS:
            await conn.exec_driver_sql(f"ALTER TABLE workflow_step_runs DROP COLUMN {column}")
    await init_db()
    async with engine.begin() as conn:
        cols = {r[1]: r for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(workflow_step_runs)")).fetchall()}
    assert set(USAGE_COLUMNS) <= set(cols)
    assert all(cols[c][3] == 0 for c in USAGE_COLUMNS)


# ---------------------------------------------------------------------------
# The admin routes: the run total is summed from the step rows
# ---------------------------------------------------------------------------
class StatementCounter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def on(self, table: str) -> list[str]:
        return [s for s in self.statements if table in s]


async def test_the_list_sums_main_line_and_fan_out_steps_in_one_statement():
    items = [wr.WorkItem(id="m1", title="a", branches=["billing"], text="one"),
             wr.WorkItem(id="m2", title="b", branches=["billing"], text="two")]
    reader = FakeAgent("reader", [items], spend_per_call=spend(1))
    second = FakeAgent("second", spend_per_call=spend(2))
    brancher = FakeAgent("brancher", spend_per_call=spend(4))
    install({"reader": reader, "second": second, "brancher": brancher})
    fan_run = await run_workflow(
        "fan",
        [step(1, agent="reader", fan_out=True), step(2, agent="second"),
         step(1, agent="brancher", branch="billing")],
        branches=[{"key": "billing", "description": "b", "position": 1}],
    )
    # 1 fan-out + 2 items x (main line 2 + branch 4) = 13 requests.
    install({"first": FakeAgent("first", ["x"])})
    empty_run = await run_workflow("no-usage", [step(1, agent="first")])

    counter = StatementCounter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        runs = await admin.api_list_workflow_runs(limit=50, workflow_id=None)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)
    by_id = {r["id"]: r for r in runs}
    assert by_id[fan_run]["usage"] == expected(13)
    assert set(by_id[fan_run]["usage"]) == KEYS
    assert by_id[empty_run]["usage"] is None
    # One grouped statement over the step rows for the whole list.
    on_steps = counter.on("workflow_step_runs")
    assert len(on_steps) == 1 and "GROUP BY" in on_steps[0].upper()


async def test_the_detail_carries_the_run_total_and_each_steps_usage():
    install({"first": FakeAgent("first", ['{"a": "b"}'], spend_per_call=spend(2)),
             "second": FakeAgent("second", ["done"], spend_per_call=spend(1))})
    run_id = await run_workflow("detail", [
        step(1, agent="first"),
        step(2, kind="transform", config={"extract_json": "a"}),
        step(3, agent="second"),
    ])
    answer = await admin.api_get_workflow_run(run_id)
    assert answer["run"]["usage"] == expected(3)
    usages = {s["position"]: s["usage"] for s in answer["steps"]}
    assert usages == {1: expected(2), 2: None, 3: expected(1)}


async def test_a_run_whose_steps_have_no_usage_answers_null_in_the_detail():
    install({"first": FakeAgent("first", ["x"])})
    run_id = await run_workflow("none", [step(1, agent="first")])
    answer = await admin.api_get_workflow_run(run_id)
    assert answer["run"]["usage"] is None
    assert answer["steps"][0]["usage"] is None
