"""The estimated cost next to every ``usage`` (cost in euros, task 5).

``GET /admin/api/runs[/{id}]`` and ``GET /admin/api/workflow-runs[/{id}]``
answer ``cost`` from the model usage ledger, priced with the admin's rates
and CU price; the web chat's context view answers ``usage.cost``. The shape
is exactly ``{"amount", "currency": "EUR", "complete"}`` or ``null`` (no CU
price, or nothing to price). Lists read the ledger in one grouped statement.

Run:  .venv/bin/python -m pytest tests/test_cost_answers.py -q
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import admin, invoke, model_prices  # noqa: E402
from agents.db import (  # noqa: E402
    ChatMessage,
    JobRun,
    ModelPrice,
    ModelUsage,
    OrchestratorConfig,
    SessionLocal,
    WorkflowRun,
    WorkflowStepRun,
    create_step_run,
    create_workflow_run,
    engine,
    init_db,
    replace_model_usage,
)
from tests.test_webchat_context import ALICE, API, BOB  # noqa: E402
from tests.test_webchat_context import _clean as _chat_clean  # noqa: E402,F401
from tests.test_webchat_context import app as chat_app  # noqa: E402

RATES = [
    {"model": "model-a", "input_rate": "2", "output_rate": "4", "cache_read_rate": None},
    {"model": "model-b", "input_rate": "1", "output_rate": "1", "cache_read_rate": "0.5"},
]


def eur(amount: float, complete: bool = True) -> dict:
    return {"amount": amount, "currency": "EUR", "complete": complete}


def use(model: str, i: int, o: int, c: int = 0, r: int = 1) -> dict:
    return {"model": model, "input_tokens": i, "output_tokens": o,
            "cache_read_tokens": c, "requests": r}


async def set_prices(cu: str | None, rates: list[dict] = RATES) -> None:
    async with SessionLocal() as s:
        current = await model_prices.read_table(s)
    async with SessionLocal() as s:
        await model_prices.replace_table(s, {
            "expected_updated_at": current["updated_at"],
            "capacity_unit_price": cu, "prices": rates,
        })


async def ledger(source: str, ref_id: str, parent_id: str | None, rows: list[dict]) -> None:
    async with SessionLocal() as s:
        await replace_model_usage(s, source=source, ref_id=ref_id, parent_id=parent_id,
                                  rows=rows)
        await s.commit()


async def job_run(run_id: str | None = None) -> str:
    rid = run_id or str(uuid.uuid4())
    async with SessionLocal() as s:
        s.add(JobRun(id=rid, agent_id=1, agent_name="a", trigger="manual",
                     status="succeeded", started_at=datetime.now(timezone.utc),
                     timeout_seconds=60))
        await s.commit()
    return rid


@pytest.fixture(autouse=True)
async def _wipe():
    await init_db()

    async def clear() -> None:
        async with SessionLocal() as s:
            for model in (ModelUsage, ModelPrice, JobRun, WorkflowRun, WorkflowStepRun):
                await s.execute(delete(model))
            row = await s.get(OrchestratorConfig, 1)
            if row is not None:
                row.capacity_unit_price = None
                row.prices_updated_at = None
            await s.commit()

    await clear()
    yield
    await clear()


class Counter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def on(self, table: str) -> list[str]:
        return [s for s in self.statements if table in s]


# ---------------------------------------------------------------------------
# Job runs
# ---------------------------------------------------------------------------
async def test_the_run_list_answers_cost_per_run_in_one_ledger_statement():
    await set_prices("0.5")
    priced = await job_run()
    partial = await job_run()
    older = await job_run()
    await ledger("job", priced, None, [use("model-a", 1000, 500)])  # 2 + 2 CU
    await ledger("job", partial, None, [use("model-a", 1000, 0), use("unpriced", 5, 5)])
    for _ in range(47):
        await job_run()

    counter = Counter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        runs = await admin.api_list_runs(agent_id=None, limit=50)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)
    by_id = {r["id"]: r for r in runs}
    assert len(runs) == 50
    assert by_id[priced]["cost"] == eur(2.0)
    assert by_id[partial]["cost"] == eur(1.0, complete=False)
    assert by_id[older]["cost"] is None
    assert "usage" in by_id[priced]
    on_ledger = counter.on("model_usage")
    assert len(on_ledger) == 1 and "GROUP BY" in on_ledger[0].upper()


async def test_without_a_cu_price_every_cost_is_null_and_the_ledger_is_not_read():
    await set_prices(None)
    rid = await job_run()
    await ledger("job", rid, None, [use("model-a", 1000, 500)])
    counter = Counter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        runs = await admin.api_list_runs(agent_id=None, limit=50)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)
    assert runs[0]["cost"] is None
    assert counter.on("model_usage") == []


async def test_the_run_detail_answers_cost():
    await set_prices("0.5")
    rid = await job_run()
    await ledger("job", rid, None, [use("model-b", 2000, 1000, c=1000)])  # 1 + 0.5 + 1 CU
    answer = await admin.api_get_run(rid)
    assert answer["cost"] == eur(1.25)
    assert set(answer["cost"]) == {"amount", "currency", "complete"}


async def test_the_invoke_poll_answer_carries_no_cost():
    await set_prices("0.5")
    rid = await job_run()
    await ledger("job", rid, None, [use("model-a", 1000, 500)])
    async with SessionLocal() as s:
        row = await s.get(JobRun, rid)
    assert "cost" not in await invoke.poll_answer(row)


# ---------------------------------------------------------------------------
# Workflow runs
# ---------------------------------------------------------------------------
async def workflow_with_steps(n: int) -> tuple[str, list[str]]:
    async with SessionLocal() as s:
        run = await create_workflow_run(s, workflow=SimpleNamespace(id=1, name="wf"),
                                        trigger="manual")
        steps = [
            (await create_step_run(s, run_id=run.id, item_run_id=None, branch_key=None,
                                   position=p + 1, agent_name="a")).id
            for p in range(n)
        ]
    return run.id, steps


async def test_the_workflow_list_sums_the_steps_cost_per_run():
    await set_prices("0.5")
    run_id, (s1, s2) = await workflow_with_steps(2)
    bare_run, _ = await workflow_with_steps(1)
    await ledger("workflow_step", s1, run_id, [use("model-a", 1000, 500)])  # 4 CU
    await ledger("workflow_step", s2, run_id, [use("model-a", 500, 0)])     # 1 CU
    counter = Counter()
    event.listen(engine.sync_engine, "before_cursor_execute", counter)
    try:
        runs = await admin.api_list_workflow_runs(limit=50, workflow_id=None)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", counter)
    by_id = {r["id"]: r for r in runs}
    assert by_id[run_id]["cost"] == eur(2.5)
    assert by_id[bare_run]["cost"] is None
    assert "usage" in by_id[run_id]
    on_ledger = counter.on("model_usage")
    assert len(on_ledger) == 1 and "GROUP BY" in on_ledger[0].upper()


async def test_the_workflow_detail_has_cost_on_the_run_and_each_step():
    await set_prices("0.5")
    run_id, (s1, s2, s3) = await workflow_with_steps(3)
    await ledger("workflow_step", s1, run_id, [use("model-a", 1000, 500)])
    await ledger("workflow_step", s3, run_id, [use("unpriced", 1, 1)])
    answer = await admin.api_get_workflow_run(run_id)
    assert answer["run"]["cost"] == eur(2.0, complete=False)
    costs = {s["id"]: s["cost"] for s in answer["steps"]}
    assert costs == {s1: eur(2.0), s2: None, s3: eur(0.0, complete=False)}
    assert all("usage" in s for s in answer["steps"])


# ---------------------------------------------------------------------------
# The chat context view
# ---------------------------------------------------------------------------
@pytest.fixture
async def chat():
    async with AsyncClient(transport=ASGITransport(app=chat_app()),
                           base_url="http://test") as c:
        yield c


async def chat_turns(cid: str, parts_per_turn: list[list[dict]]) -> list[str]:
    ids = []
    async with SessionLocal() as s:
        for n, parts in enumerate(parts_per_turn):
            mid = str(uuid.uuid4())
            s.add(ChatMessage(id=mid, conversation_id=cid, position=n, role="assistant",
                              content="x", parts_json=json.dumps(parts), status="done"))
            ids.append(mid)
        await s.commit()
    return ids


def usage_part(model: str | None, i: int, o: int, r: int = 1) -> dict:
    part = {"type": "usage", "input_tokens": i, "output_tokens": o,
            "cache_read_tokens": 0, "total_tokens": i + o, "requests": r}
    if model is not None:
        part["model"] = model
    return part


async def new_conversation(chat) -> str:
    r = await chat.post(f"{API}/conversations", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_the_context_prices_ledger_rows_and_older_turns_once(chat):
    await set_prices("0.5")
    cid = await new_conversation(chat)
    with_ledger, older = await chat_turns(cid, [
        [usage_part("model-a", 1000, 500)],   # also in the ledger: counted once
        [usage_part("model-a", 500, 0)],      # older turn, no ledger: priced from the part
    ])
    await ledger("chat", with_ledger, cid, [use("model-a", 1000, 500)])
    r = await chat.get(f"{API}/conversations/{cid}/context")
    assert r.status_code == 200, r.text
    usage = r.json()["usage"]
    assert usage["cost"] == eur(2.5)
    assert usage["turns"] == 2


async def test_a_usage_part_without_a_model_makes_the_cost_incomplete(chat):
    await set_prices("0.5")
    cid = await new_conversation(chat)
    await chat_turns(cid, [[usage_part("model-a", 1000, 500)], [usage_part(None, 10, 10)]])
    usage = (await chat.get(f"{API}/conversations/{cid}/context")).json()["usage"]
    assert usage["cost"] == eur(2.0, complete=False)


async def test_the_context_cost_is_null_without_a_cu_price_or_anything_to_price(chat):
    cid = await new_conversation(chat)
    await chat_turns(cid, [[usage_part("model-a", 1000, 500)]])
    usage = (await chat.get(f"{API}/conversations/{cid}/context")).json()["usage"]
    assert usage["cost"] is None
    await set_prices("0.5")
    empty = await new_conversation(chat)
    usage = (await chat.get(f"{API}/conversations/{empty}/context")).json()["usage"]
    assert usage["cost"] is None


async def test_another_users_conversation_stays_404(chat):
    await set_prices("0.5")
    cid = await new_conversation(chat)
    r = await chat.get(f"{API}/conversations/{cid}/context", headers={"x-test-user": BOB})
    assert r.status_code == 404
    assert ALICE  # the owner of the conversation above
