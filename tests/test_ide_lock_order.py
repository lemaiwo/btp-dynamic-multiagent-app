"""Lock order: session row first, then comments.

``runner._start`` / ``_reclaim`` lock the session row (``FOR UPDATE``) and
then write comments (``mark_comments_sent``). A comment writer that updated
the comment row first and the session row second (``touch_session``) could
deadlock against them on Postgres: one of the two requests would fail. So
every comment writer takes ``store.lock_session_row`` before its first
statement.

SQLite cannot show the deadlock (one writer, and SQLAlchemy drops ``FOR
UPDATE`` there), so this suite pins the *statement order* instead: a spy on
``lock_session_row`` plus a cursor listener; the lock must come before any
SQL the function sends.

Run:  python -m pytest tests/test_ide_lock_order.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from pydantic_ai.usage import RunUsage  # noqa: E402
from sqlalchemy import event, update  # noqa: E402

from agents.db import SessionLocal, engine, init_db  # noqa: E402
from agents.ide import runner, store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeComment,
    IdeFileRevision,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.stages import Stage  # noqa: E402

PATH = "src/CLAS/zcl_x.clas.abap"
TEXT = "a\nb\nc"
LOCK = "<lock_session_row>"


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeFileRevision, IdeWorkspaceFile, IdeSession):
            await db.execute(model.__table__.delete())
        await db.commit()
    yield


async def _session_with_comment(state: str = "open") -> tuple[str, str]:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner="DEVUSER01", title="t",
                                       target="DEMO")
        db.add(IdeWorkspaceFile(
            session_id=s.id, path=PATH, object_type="CLAS", object_name="ZCL_X",
            origin_source="o", proposed_source=TEXT, state="modified", revision=1,
        ))
        db.add(IdeFileRevision(session_id=s.id, path=PATH, revision=1,
                               proposed_source=TEXT))
        await db.commit()
        sid = s.id
    async with SessionLocal() as db:
        c = await store.add_comment(db, sid, anchor="file", body="fix",
                                    path=PATH, revision=1, line_start=1,
                                    line_end=2)
        cid = c.id
    if state != "open":
        async with SessionLocal() as db:
            await db.execute(update(IdeComment).where(IdeComment.id == cid)
                             .values(state=state, sent_run_id="run-1"))
            await db.commit()
    return sid, cid


@pytest.fixture
def trail(monkeypatch):
    """Every SQL statement sent, with ``LOCK`` where the session lock was
    taken (the spy runs before the real lock's own SELECT)."""
    seen: list[str] = []
    real = store.lock_session_row

    async def spy(db, sid):
        seen.append(LOCK)
        await real(db, sid)

    monkeypatch.setattr(store, "lock_session_row", spy)
    if hasattr(runner, "lock_session_row"):
        monkeypatch.setattr(runner, "lock_session_row", spy)

    def record(conn, cursor, statement, *a):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


def _assert_lock_first(seen: list[str]) -> None:
    assert seen, "nothing was sent"
    assert seen[0] == LOCK, seen[:3]


async def test_edit_comment_locks_the_session_first(trail):
    sid, cid = await _session_with_comment()
    trail.clear()
    async with SessionLocal() as db:
        await store.edit_comment(db, sid, cid, "better")
    _assert_lock_first(trail)


@pytest.mark.parametrize("state", ["dismissed"])
async def test_set_comment_state_locks_the_session_first(trail, state):
    sid, cid = await _session_with_comment()
    trail.clear()
    async with SessionLocal() as db:
        await store.set_comment_state(db, sid, cid, state)
    _assert_lock_first(trail)


async def test_delete_comment_locks_the_session_first(trail):
    sid, cid = await _session_with_comment()
    trail.clear()
    async with SessionLocal() as db:
        await store.delete_comment(db, sid, cid)
    _assert_lock_first(trail)


async def test_resolve_comments_locks_the_session_first(trail):
    sid, cid = await _session_with_comment("sent")
    trail.clear()
    async with SessionLocal() as db:
        resolved, _ = await store.resolve_comments(
            db, sid, [(cid, "done")], run_id="run-1")
    assert resolved == [cid]
    _assert_lock_first(trail)


async def test_release_locks_the_session_before_reopening(trail):
    sid, _ = await _session_with_comment("sent")
    async with SessionLocal() as db:
        await db.execute(update(IdeSession).where(IdeSession.id == sid)
                         .values(status="running", run_id="run-1"))
        await db.commit()
    trail.clear()
    start = runner._Start(sid=sid, run_id="run-1", stage=Stage.propose,
                          message_id="m", requests_used=0,
                          request_changes=runner.RequestChanges())
    row, reopened = await runner._release(start, RunUsage())
    assert row is not None and row.status == "idle"
    assert len(reopened) == 1
    _assert_lock_first(trail)


async def test_set_syntax_result_locks_the_session_first(trail):
    sid, _ = await _session_with_comment()
    trail.clear()
    async with SessionLocal() as db:
        await store.set_syntax_result(db, sid, PATH, 1, "ok", [])
    _assert_lock_first(trail)


async def test_save_state_locks_the_session_before_the_files(lock_trail):
    from agents.deep import DeepState
    from agents.ide.workspace import save_state

    sid, _ = await _session_with_comment()
    state = DeepState(run_id="r")
    state.files[PATH] = "changed\ntext"
    lock_trail.clear()
    async with SessionLocal() as db:
        changed = await save_state(db, sid, state, run_id="run-2")
    assert changed and changed[0]["revision"] == 2
    lock_trail.assert_lock_before_writes_to("ide_workspace_files")
