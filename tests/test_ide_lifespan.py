"""App lifespan: ghost IDE sessions reset, old ones purged, runner cancelled."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_lifespan.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy import update  # noqa: E402

import app as app_module  # noqa: E402
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide.models import IdeApproval, IdeAuditLog, IdeSession  # noqa: E402
from agents.ide.store import (  # noqa: E402
    add_approval,
    add_audit,
    create_session,
    get_approval,
    get_owned_session,
)


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    await init_db()
    async with SessionLocal() as db:
        await db.execute(IdeSession.__table__.delete())
        await db.commit()
    cancelled = []

    async def fake_cancel_all():
        cancelled.append(True)

    monkeypatch.setattr(app_module.ide_runner, "cancel_all", fake_cancel_all)
    async def noop(*_a, **_k):
        return None

    # No model credentials offline: skip registry build and seeding.
    monkeypatch.setattr(app_module.registry, "reload", noop)
    monkeypatch.setattr(app_module.dynamic_chat_app, "refresh", lambda: None)
    monkeypatch.setattr(app_module, "seed_from_file_if_empty", noop)
    monkeypatch.setattr(app_module, "ensure_ide_seed", noop)
    monkeypatch.setattr(app_module, "DB_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(app_module, "TOKEN_KEEPWARM_SECONDS", 0)
    yield cancelled


async def _make(owner, *, age_days=0, running=False, age_seconds=0):
    async with SessionLocal() as db:
        s = await create_session(db, owner=owner, title=owner, target="T1")
        values = {}
        if running:
            values.update(status="running", run_id="r1")
        await db.execute(update(IdeSession).where(IdeSession.id == s.id).values(**values))
        await db.commit()
        if age_days or age_seconds:
            old = datetime.now(timezone.utc) - timedelta(days=age_days, seconds=age_seconds)
            await db.execute(
                update(IdeSession)
                .where(IdeSession.id == s.id)
                .values(updated_at=old)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        return s.id


async def _get(sid, owner):
    async with SessionLocal() as db:
        return await get_owned_session(db, sid, owner)


async def test_lifespan_purges_resets_and_cancels(monkeypatch, _clean):
    monkeypatch.setattr(app_module, "IDE_SESSION_RETENTION_DAYS", 1)
    old = await _make("old", age_days=5)
    fresh = await _make("fresh")
    ghost = await _make("ghost", running=True, age_seconds=600)
    # Fresh and running: may be another instance's live run (rolling deploy).
    live = await _make("live", running=True)
    async with app_module.lifespan(app_module.app):
        assert await _get(old, "old") is None
        assert await _get(fresh, "fresh") is not None
        g = await _get(ghost, "ghost")
        assert g is not None and g.status == "idle" and g.run_id is None
        assert (await _get(live, "live")).status == "running"
    assert _clean == [True]


async def test_retention_zero_disables_purge(monkeypatch):
    monkeypatch.setattr(app_module, "IDE_SESSION_RETENTION_DAYS", 0)
    old = await _make("old", age_days=500)
    async with app_module.lifespan(app_module.app):
        assert await _get(old, "old") is not None


async def _make_diagnose(owner, *, created_days=0, updated_days=0, running=False):
    async with SessionLocal() as db:
        s = await create_session(
            db, owner=owner, title=owner, target="T1", session_type="diagnose"
        )
        now = datetime.now(timezone.utc)
        values = {
            "created_at": now - timedelta(days=created_days),
            "updated_at": now - timedelta(days=updated_days),
        }
        if running:
            values.update(status="running", run_id="r1")
        await db.execute(update(IdeSession).where(IdeSession.id == s.id).values(**values))
        await db.commit()
        return s.id


async def test_lifespan_runs_all_passes(monkeypatch):
    monkeypatch.setattr(app_module, "IDE_SESSION_RETENTION_DAYS", 90)
    monkeypatch.setattr(app_module, "IDE_DIAGNOSE_RETENTION_DAYS", 7)
    monkeypatch.setattr(app_module, "IDE_AUDIT_RETENTION_DAYS", 30)
    # change: 40 days idle is inside 90 -> kept
    change = await _make("change", age_days=40)
    # diagnose: created 10 days ago but active today -> purged (7-day cap)
    old_diag = await _make_diagnose("od", created_days=10, updated_days=0)
    new_diag = await _make_diagnose("nd", created_days=2)
    running_diag = await _make_diagnose("rd", created_days=50, running=True)
    async with SessionLocal() as db:
        await db.execute(IdeAuditLog.__table__.delete())
        a_old = await add_audit(
            db, principal="DEVUSER01", session_id=old_diag, target="T1",
            action="trace_arm", params={}, outcome="ok",
        )
        a_new = await add_audit(
            db, principal="DEVUSER01", session_id=old_diag, target="T1",
            action="trace_deny", params={}, outcome="ok",
        )
        ap = await add_approval(
            db, new_diag, run_id=None, tool_call_id=None,
            action="trace_start", params={},
        )
        await db.execute(
            update(IdeAuditLog).where(IdeAuditLog.id == a_old.id)
            .values(ts=datetime.now(timezone.utc) - timedelta(days=45))
        )
        await db.execute(
            update(IdeApproval).where(IdeApproval.id == ap.id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=60))
        )
        await db.commit()
    async with app_module.lifespan(app_module.app):
        assert await _get(change, "change") is not None
        assert await _get(old_diag, "od") is None
        assert await _get(new_diag, "nd") is not None
        assert await _get(running_diag, "rd") is not None
        async with SessionLocal() as db:
            from sqlalchemy import select

            ids = {r.id for r in (await db.execute(select(IdeAuditLog))).scalars()}
            assert ids == {a_new.id}
            assert (await get_approval(db, new_diag, ap.id)).status == "expired"


async def test_expiry_pass_closes_approvals_left_without_an_outcome():
    """A process that died during the ARC-1 call leaves ``approved`` without
    a result: the pass closes it once it is older than the arm timeout plus
    margin, with an audit row; a younger one may still be in flight."""
    import json

    from sqlalchemy import select

    limit = (app_module.ide_approvals.ARM_TIMEOUT_S
             + app_module.ide_approvals.INTERRUPT_MARGIN_S)
    sid = await _make_diagnose("crash")
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        await db.execute(IdeAuditLog.__table__.delete())
        ids = []
        for age in (limit + 30, limit - 30):
            ap = await add_approval(db, sid, run_id=None, tool_call_id=None,
                                    action="trace_start", params={})
            await db.execute(
                update(IdeApproval).where(IdeApproval.id == ap.id).values(
                    status="approved", decided_at=now - timedelta(seconds=age))
            )
            ids.append(ap.id)
        await db.commit()
    async with app_module.lifespan(app_module.app):
        async with SessionLocal() as db:
            stale = await get_approval(db, sid, ids[0])
            assert (stale.status, stale.error_code) == ("failed", "interrupted")
            assert "check trace_requests" in json.loads(stale.result_json)["note"]
            fresh = await get_approval(db, sid, ids[1])
            assert fresh.status == "approved" and fresh.result_json is None
            audit = list((await db.execute(select(IdeAuditLog))).scalars())
            assert [(a.action, a.outcome, a.principal, a.session_id) for a in audit] == [
                ("trace_failed", "failed", "crash", sid)]


async def test_each_pass_has_its_own_zero_switch(monkeypatch):
    monkeypatch.setattr(app_module, "IDE_SESSION_RETENTION_DAYS", 0)
    monkeypatch.setattr(app_module, "IDE_DIAGNOSE_RETENTION_DAYS", 0)
    monkeypatch.setattr(app_module, "IDE_AUDIT_RETENTION_DAYS", 0)
    c = await _make("c", age_days=900)
    d = await _make_diagnose("d", created_days=900)
    async with SessionLocal() as db:
        await db.execute(IdeAuditLog.__table__.delete())
        a = await add_audit(
            db, principal="P", session_id=d, target="T1",
            action="trace_arm", params={}, outcome="ok",
        )
        await db.execute(
            update(IdeAuditLog).where(IdeAuditLog.id == a.id)
            .values(ts=datetime.now(timezone.utc) - timedelta(days=900))
        )
        await db.commit()
    assert await app_module._purge_ide_sessions() == 0
    assert await _get(c, "c") is not None
    assert await _get(d, "d") is not None
    async with SessionLocal() as db:
        from sqlalchemy import select

        assert len((await db.execute(select(IdeAuditLog))).scalars().all()) == 1


async def test_failing_pass_does_not_skip_the_others(monkeypatch):
    monkeypatch.setattr(app_module, "IDE_SESSION_RETENTION_DAYS", 90)
    monkeypatch.setattr(app_module, "IDE_DIAGNOSE_RETENTION_DAYS", 7)
    old_diag = await _make_diagnose("od", created_days=10)
    real = app_module.purge_sessions_older_than

    async def flaky(db, days, **kw):
        if kw.get("session_type", "change") == "change":
            raise RuntimeError("boom")
        return await real(db, days, **kw)

    monkeypatch.setattr(app_module, "purge_sessions_older_than", flaky)
    assert await app_module._purge_ide_sessions() == 1
    assert await _get(old_diag, "od") is None


async def test_cancelled_error_is_not_swallowed(monkeypatch):
    import asyncio

    async def cancelled(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "purge_sessions_older_than", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await app_module._purge_ide_sessions()


async def test_startup_survives_housekeeping_failure(monkeypatch, _clean):
    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(app_module, "_purge_ide_sessions", broken)
    async with app_module.lifespan(app_module.app):
        pass
    assert _clean == [True]


# --- shutdown waits for approvals that are being executed ----------------------


async def test_shutdown_drains_approval_calls_after_cancelling_runs(_clean):
    """An approved trace whose ARC-1 call is in flight must get to record
    its outcome: shutdown waits for it, after the runs were cancelled."""
    order: list[str] = []

    async def arming():
        await asyncio.sleep(0.05)
        order.append(f"armed after {len(_clean)} cancel_all")

    async with app_module.lifespan(app_module.app):
        task = asyncio.create_task(arming())
        app_module.ide_approvals._TASKS.add(task)
        task.add_done_callback(app_module.ide_approvals._TASKS.discard)
    assert task.done() and not task.cancelled()
    assert order == ["armed after 1 cancel_all"]


async def test_shutdown_does_not_wait_forever_for_an_approval_call(
    monkeypatch, caplog
):
    """Bounded by the arm timeout plus a margin: a call that hangs past it
    is logged, and shutdown goes on."""
    monkeypatch.setattr(app_module.ide_approvals, "ARM_TIMEOUT_S", -4.95)
    hung = asyncio.Event()

    async def arming():
        await hung.wait()

    with caplog.at_level("WARNING"):
        async with app_module.lifespan(app_module.app):
            task = asyncio.create_task(arming())
            app_module.ide_approvals._TASKS.add(task)
            task.add_done_callback(app_module.ide_approvals._TASKS.discard)
    assert any("approval" in r.getMessage().lower() and r.levelname == "WARNING"
               for r in caplog.records)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- startup says when diagnose data is kept forever ------------------------------


@pytest.mark.parametrize("days,warned", [(0, True), (14, False)])
async def test_startup_warns_when_diagnose_retention_is_off(
    monkeypatch, caplog, days, warned
):
    monkeypatch.setattr(app_module, "IDE_DIAGNOSE_RETENTION_DAYS", days)
    with caplog.at_level("WARNING"):
        async with app_module.lifespan(app_module.app):
            pass
    hits = [r for r in caplog.records if r.levelname == "WARNING"
            and "IDE_DIAGNOSE_RETENTION_DAYS" in r.getMessage()]
    assert bool(hits) is warned
