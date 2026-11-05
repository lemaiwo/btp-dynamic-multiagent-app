"""The reconciliation run store (``agents/reconcile/runs_store.py``).

One row per run of ``builtin:reconcile`` in ``reconcile_runs``, so a run's
outcome is visible from any app instance. Every state change is a
conditional UPDATE whose rowcount says whether it happened (no
``RETURNING``: the statements must also run on SAP HANA, which
``tests/conftest.py`` checks for every test).

Run:  .venv/bin/python -m pytest tests/test_reconcile_runs_store.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from sqlalchemy import delete, select, update  # noqa: E402

from agents.db import (  # noqa: E402
    ChatConversation,
    ChatFile,
    ChatMessage,
    ReconcileRun,
    SessionLocal,
    init_db,
)
from agents.reconcile import runs_store  # noqa: E402
from agents.webchat import store  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"


async def wipe() -> None:
    async with SessionLocal() as s:
        for model in (ReconcileRun, ChatFile, ChatMessage, ChatConversation):
            await s.execute(delete(model))
        await s.commit()


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    await wipe()
    yield
    await wipe()


async def conversation(principal: str = ALICE) -> str:
    async with SessionLocal() as s:
        conv = await store.create_conversation(s, principal, title="Recon", model=None)
        return conv.id


async def new_run(cid: str, principal: str = ALICE, **overrides) -> str:
    values = dict(conversation_id=cid, principal=principal, message_id="m-1",
                  table_name="ZTAB", wave="W1", spec_file_id="f-spec", rows_total=40)
    values.update(overrides)
    async with SessionLocal() as s:
        return await runs_store.create_run(s, **values)


async def row(run_id: str) -> ReconcileRun | None:
    async with SessionLocal() as s:
        return (await s.execute(
            select(ReconcileRun).where(ReconcileRun.id == run_id)
        )).scalar_one_or_none()


async def age(run_id: str, seconds: float) -> None:
    async with SessionLocal() as s:
        await s.execute(update(ReconcileRun).where(ReconcileRun.id == run_id).values(
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=seconds)))
        await s.commit()


async def test_create_run_starts_running_with_its_facts():
    cid = await conversation()
    run_id = await new_run(cid)
    got = await row(run_id)
    assert got is not None
    assert (got.status, got.rows_total, got.rows_done, got.table_name, got.wave) == (
        "running", 40, 0, "ZTAB", "W1")
    assert got.error_code is None and got.finished_at is None


async def test_heartbeat_moves_a_running_row_and_stops_once_the_conversation_is_gone():
    cid = await conversation()
    run_id = await new_run(cid)
    async with SessionLocal() as s:
        assert await runs_store.heartbeat(s, run_id, 15) is True
    assert (await row(run_id)).rows_done == 15
    async with SessionLocal() as s:
        assert await store.delete_conversation(s, cid, ALICE) is True
    assert await row(run_id) is None
    async with SessionLocal() as s:
        assert await runs_store.heartbeat(s, run_id, 20) is False


async def test_finish_run_moves_only_a_running_row():
    cid = await conversation()
    run_id = await new_run(cid)
    async with SessionLocal() as s:
        assert await runs_store.finish_run(
            s, run_id, status="done", counts={"matched": 38, "mismatched": 2},
            report_file_id="f-report") is True
    got = await row(run_id)
    assert got.status == "done" and got.report_file_id == "f-report"
    assert json.loads(got.counts_json) == {"matched": 38, "mismatched": 2}
    assert got.finished_at is not None
    async with SessionLocal() as s:
        # Already finished: a late failure does not overwrite the outcome.
        assert await runs_store.finish_run(
            s, run_id, status="failed", counts={}, error_code="read_failed") is False
    assert (await row(run_id)).status == "done"


@pytest.mark.parametrize("status", ["running", "canceled", ""])
async def test_finish_run_refuses_a_status_that_is_not_an_outcome(status):
    cid = await conversation()
    run_id = await new_run(cid)
    async with SessionLocal() as s:
        with pytest.raises(ValueError):
            await runs_store.finish_run(s, run_id, status=status, counts={})


@pytest.mark.parametrize("counts", [{"matched": "38"}, {"matched": True}, {1: 2},
                                    {"value": 1.5}])
async def test_finish_run_keeps_counters_only(counts):
    cid = await conversation()
    run_id = await new_run(cid)
    async with SessionLocal() as s:
        with pytest.raises(ValueError):
            await runs_store.finish_run(s, run_id, status="done", counts=counts)
    assert (await row(run_id)).status == "running"


async def test_active_run_is_a_fresh_running_row_and_a_stale_one_is_interrupted():
    cid = await conversation()
    run_id = await new_run(cid)
    async with SessionLocal() as s:
        active = await runs_store.active_run(s, cid)
    assert active is not None and active["run_id"] == run_id
    assert active["status"] == "running"

    await age(run_id, runs_store.STALE_SECONDS + 5)
    async with SessionLocal() as s:
        assert await runs_store.active_run(s, cid) is None
    got = await row(run_id)
    assert (got.status, got.error_code) == ("interrupted", "interrupted")
    assert got.finished_at is not None


async def test_active_run_ignores_a_finished_row():
    cid = await conversation()
    run_id = await new_run(cid)
    async with SessionLocal() as s:
        await runs_store.finish_run(s, run_id, status="failed", counts={},
                                    error_code="read_failed")
        assert await runs_store.active_run(s, cid) is None


async def test_latest_run_is_the_newest_of_the_owner_and_never_another_principals():
    cid = await conversation()
    first = await new_run(cid, wave="W1")
    second = await new_run(cid, wave="W2")
    async with SessionLocal() as s:
        latest = await runs_store.latest_run(s, cid, ALICE)
        assert latest["run_id"] == second
        assert set(latest) == {"run_id", "status", "table", "wave", "rows_total",
                               "rows_done", "counts", "report_file_id", "error_code",
                               "started_at", "finished_at"}
        assert (latest["table"], latest["wave"], latest["counts"]) == ("ZTAB", "W2", {})
        assert latest["started_at"].endswith("+00:00")
        assert (await runs_store.latest_run(s, cid, ALICE, run_id=first))["wave"] == "W1"
        assert await runs_store.latest_run(s, cid, BOB) is None
        assert await runs_store.latest_run(s, cid, BOB, run_id=first) is None
        assert await runs_store.latest_run(s, cid, ALICE, run_id="no-such-run") is None


async def test_latest_run_names_a_stale_running_row_interrupted():
    cid = await conversation()
    run_id = await new_run(cid)
    await age(run_id, runs_store.STALE_SECONDS + 5)
    async with SessionLocal() as s:
        latest = await runs_store.latest_run(s, cid, ALICE, run_id=run_id)
    assert (latest["status"], latest["error_code"]) == ("interrupted", "interrupted")


async def test_sweep_running_at_startup_interrupts_every_running_row():
    cid = await conversation()
    a = await new_run(cid)
    b = await new_run(cid)
    done = await new_run(cid)
    async with SessionLocal() as s:
        await runs_store.finish_run(s, done, status="done", counts={"matched": 1})
        assert await runs_store.sweep_running_at_startup(s) == 2
    for run_id in (a, b):
        got = await row(run_id)
        assert (got.status, got.error_code) == ("interrupted", "interrupted")
    assert (await row(done)).status == "done"
    async with SessionLocal() as s:
        assert await runs_store.sweep_running_at_startup(s) == 0


async def test_purge_older_than_removes_the_conversations_runs():
    old = await conversation()
    kept = await conversation()
    old_run = await new_run(old)
    kept_run = await new_run(kept)
    async with SessionLocal() as s:
        await s.execute(update(ChatConversation).where(ChatConversation.id == old).values(
            updated_at=datetime.now(timezone.utc) - timedelta(days=30)))
        await s.commit()
        assert await store.purge_older_than(s, 7) == 1
    assert await row(old_run) is None
    assert await row(kept_run) is not None
