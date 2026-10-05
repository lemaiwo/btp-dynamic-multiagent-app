"""B8 review follow-ups (approve races, artifact versions, reaped runs).

1. Approve and comment creation serialise on the session row (``SELECT ...
   FOR UPDATE``; SQLAlchemy drops the clause on SQLite, where the single
   writer already serialises them), so a comment committed while an approve
   is in flight is seen by the approve's ``NOT EXISTS`` on Postgres too.
2. Approve in stage ``propose`` re-reads the proposed revisions after its
   UPDATE; a mismatch is a lost race (rolled back and checked again).
3. Two concurrent approves: exactly one wins, the pins are written once.
4. ``ide_artifacts`` has a unique (session_id, kind, version) index, created
   additively and guarded; ``add_artifact`` retries on the conflict.
5. A session tool of a run whose lock was reaped (the session's ``run_id``
   is another run's, or none) writes nothing.

Run:  python -m pytest tests/test_ide_b8_followups.py -q
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from sqlalchemy import Select, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from agents import db as agents_db  # noqa: E402
from agents.db import SessionLocal, engine, init_db  # noqa: E402
from agents.deep import DeepState, WorkspaceScope, current_workspace  # noqa: E402
from agents.ide import session_tools, stages, store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeComment,
    IdeFileRevision,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.session_tools import IdeRunContext, current_ide_run  # noqa: E402
from agents.ide.stages import StageGateError  # noqa: E402

OWNER = "alice"
PATH = "src/CLAS/zcl_demo.clas.abap"


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeFileRevision, IdeWorkspaceFile, IdeArtifact,
                      IdeSession):
            await db.execute(model.__table__.delete())
        await db.commit()


async def _session(stage: str = "design", **values) -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner=OWNER, title="t", target="DEMO")
        s.stage = stage
        for k, v in values.items():
            setattr(s, k, v)
        await db.commit()
        return s.id


async def _row(sid: str) -> IdeSession:
    async with SessionLocal() as db:
        return await db.get(IdeSession, sid)


@contextmanager
def _locked_selects(monkeypatch):
    """Records every SELECT ... FOR UPDATE on ide_sessions."""
    seen: list[str] = []
    real = AsyncSession.execute

    async def spy(self, statement, *args, **kwargs):
        if isinstance(statement, Select) and statement._for_update_arg is not None:
            tables = {t.name for t in statement.get_final_froms()}
            if "ide_sessions" in tables:
                seen.append("ide_sessions")
        return await real(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", spy)
    yield seen


# --- 1. approve and add_comment lock the session row --------------------------


async def test_approve_locks_the_session_row(monkeypatch):
    sid = await _session("design")
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="d")
    with _locked_selects(monkeypatch) as seen:
        async with SessionLocal() as db:
            await stages.approve(db, await db.get(IdeSession, sid))
    assert seen, "approve must take the session row lock"


async def test_add_comment_locks_the_session_row(monkeypatch):
    sid = await _session("design")
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="d")
    with _locked_selects(monkeypatch) as seen:
        async with SessionLocal() as db:
            await store.add_comment(db, sid, anchor="document", body="b",
                                    kind="design", version=1, paragraph=0)
    assert seen, "add_comment must take the session row lock"


async def test_mark_comments_sent_locks_the_session_row(monkeypatch):
    sid = await _session("design")
    with _locked_selects(monkeypatch) as seen:
        async with SessionLocal() as db:
            await store.mark_comments_sent(db, sid, "run-1")
            await db.rollback()
    assert seen


async def test_comment_then_approve_is_refused():
    sid = await _session("design")
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="d")
        await store.add_comment(db, sid, anchor="document", body="b",
                                kind="design", version=1, paragraph=0)
    async with SessionLocal() as db:
        with pytest.raises(StageGateError) as exc:
            await stages.approve(db, await db.get(IdeSession, sid))
    assert exc.value.code == "open_comments"


# --- 2. propose approve re-reads the file revisions ---------------------------


async def test_propose_approve_treats_a_changed_revision_as_a_lost_race(monkeypatch):
    sid = await _session("propose")
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(session_id=sid, path=PATH, state="new",
                                proposed_source="x", revision=2,
                                object_type="CLAS", object_name="ZCL_DEMO"))
        await db.commit()
    real = stages._proposed_revisions
    calls: list[dict] = []

    async def racing(db, s):
        out = await real(db, s)
        # The first read sees revision 1: a revision "landed" after it.
        if not calls:
            out = {PATH: 1}
        calls.append(out)
        return out

    monkeypatch.setattr(stages, "_proposed_revisions", racing)
    async with SessionLocal() as db:
        session = await stages.approve(db, await db.get(IdeSession, sid))
    assert len(calls) >= 3  # read, re-read (mismatch), read again, re-read
    assert store.pins_of(session)["files"] == {PATH: 2}
    assert (await _row(sid)).stage == "review"


# --- 3. concurrent approves --------------------------------------------------


async def test_two_concurrent_approves_exactly_one_wins(monkeypatch):
    sid = await _session("design")
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="d")
    writes: list[str] = []
    real_execute = AsyncSession.execute

    async def counting(self, statement, *args, **kwargs):
        result = await real_execute(self, statement, *args, **kwargs)
        if getattr(statement, "is_update", False) and \
                getattr(statement, "table", None) is not None and \
                statement.table.name == "ide_sessions" and \
                "pins_json" in {c.key for c in statement._values or {}} and \
                result.rowcount == 1:
            writes.append("pins")
        return result

    monkeypatch.setattr(AsyncSession, "execute", counting)

    async def one():
        async with SessionLocal() as db:
            return await stages.approve(db, await db.get(IdeSession, sid), version=1)

    results = await asyncio.gather(one(), one(), return_exceptions=True)
    ok = [r for r in results if isinstance(r, IdeSession)]
    refused = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(refused) == 1, results
    assert isinstance(refused[0], (StageGateError, store.PinConflict))
    if isinstance(refused[0], StageGateError):
        assert refused[0].code in ("stage_changed", "invalid_stage", "missing_artifact")
    assert writes == ["pins"]
    row = await _row(sid)
    assert row.stage == "plan" and store.pins_of(row) == {"design": 1}


# --- 4. unique artifact versions ------------------------------------------------


async def _indexes() -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='ide_artifacts'")
        return {r[0] for r in rows.fetchall()}


async def test_artifact_versions_are_unique():
    assert "uq_ide_artifacts_version" in await _indexes()
    sid = await _session("design")
    async with SessionLocal() as db:
        db.add(IdeArtifact(session_id=sid, stage="design", kind="design",
                           content="a", version=1))
        await db.commit()
        db.add(IdeArtifact(session_id=sid, stage="design", kind="design",
                           content="b", version=1))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_add_artifact_retries_on_a_version_conflict(monkeypatch):
    sid = await _session("design")
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="v1")
    real = store._next_version
    calls: list[int] = []

    async def stale(db, s, kind):
        calls.append(1)
        return 1 if len(calls) == 1 else await real(db, s, kind)  # 1 is taken

    monkeypatch.setattr(store, "_next_version", stale)
    async with SessionLocal() as db:
        art = await store.add_artifact(db, sid, stage="design", kind="design",
                                       content="v2")
    assert art.version == 2 and len(calls) == 2
    async with SessionLocal() as db:
        rows = (await db.execute(select(IdeArtifact.version).where(
            IdeArtifact.session_id == sid))).scalars().all()
    assert sorted(rows) == [1, 2]


async def test_concurrent_add_artifact_gets_distinct_versions():
    sid = await _session("design")

    async def one(content):
        async with SessionLocal() as db:
            return (await store.add_artifact(db, sid, stage="design",
                                             kind="design", content=content)).version

    versions = await asyncio.gather(*(one(str(i)) for i in range(4)))
    assert sorted(versions) == [1, 2, 3, 4]


async def test_unique_index_creation_with_duplicates_only_warns(caplog):
    sid = await _session("design")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX IF EXISTS uq_ide_artifacts_version")
    async with SessionLocal() as db:
        for content in ("a", "b"):
            db.add(IdeArtifact(session_id=sid, stage="design", kind="design",
                               content=content, version=1))
        await db.commit()
    with caplog.at_level(logging.WARNING, logger="agents.db"):
        async with engine.begin() as conn:
            await agents_db._ensure_unique_index(
                conn, "uq_ide_artifacts_version", "ide_artifacts",
                ("session_id", "kind", "version"),
            )
            # The transaction is still usable after the refused CREATE.
            await conn.execute(text("SELECT 1"))
    assert "uq_ide_artifacts_version" in caplog.text
    assert "uq_ide_artifacts_version" not in await _indexes()
    async with SessionLocal() as db:
        await db.execute(IdeArtifact.__table__.delete())
        await db.commit()
    await init_db()
    assert "uq_ide_artifacts_version" in await _indexes()


# --- 5. a reaped run cannot write through the session tools --------------------


@contextmanager
def _bound(sid: str, run_id: str, stage: str = "design", request_changes=False):
    ws = current_workspace.set(WorkspaceScope(
        session_id=sid, state=DeepState(run_id=f"ide:{sid}"), session_type="change"))
    run = current_ide_run.set(IdeRunContext(
        sid=sid, owner=OWNER, target="DEMO", destination="", session_type="change",
        stage=stage, report=False, run_id=run_id, emit=lambda *_: None,
        request_changes=request_changes))
    try:
        yield
    finally:
        current_ide_run.reset(run)
        current_workspace.reset(ws)


@pytest.mark.parametrize("session_run", [None, "run-NEW"])
async def test_reaped_run_cannot_submit(session_run):
    sid = await _session("design", run_id=session_run,
                         status="running" if session_run else "idle")
    with _bound(sid, "run-OLD"):
        out = await session_tools.submit_document("design", "# D")
    assert out.startswith("Error:")
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeArtifact))).first() is None


async def test_current_run_can_submit():
    sid = await _session("design", run_id="run-1", status="running")
    with _bound(sid, "run-1"):
        out = await session_tools.submit_document("design", "# D")
    assert out == "Submitted design version 1."


async def test_reaped_run_cannot_resolve():
    sid = await _session("design", run_id="run-NEW", status="running")
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="d")
        c = await store.add_comment(db, sid, anchor="document", body="b",
                                    kind="design", version=1, paragraph=0)
        await store.mark_comments_sent(db, sid, "run-NEW")
        await db.commit()
    with _bound(sid, "run-OLD", request_changes=True):
        out = await session_tools.resolve_comments(
            [session_tools.CommentAnswer(id=c.id, answer="x")])
    assert out.startswith("Error:")
    async with SessionLocal() as db:
        assert (await store.get_comment(db, sid, c.id)).state == "sent"
