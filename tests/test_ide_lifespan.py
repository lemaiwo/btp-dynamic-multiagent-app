"""App lifespan: ghost IDE sessions reset, old ones purged, runner cancelled."""

from __future__ import annotations

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
from agents.ide.models import IdeSession  # noqa: E402
from agents.ide.store import create_session, get_owned_session  # noqa: E402


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
