"""The statements SAP HANA refuses, kept out of the code HANA runs.

HANA has no ``UPDATE`` / ``DELETE ... RETURNING`` and cannot compare an
``NCLOB`` (every ``Text`` column). The store functions that used either are
run here on the suite's SQLite engine while ``tests/hana_sql.py`` compiles
what they execute for HANA: they must still do what they did, and send
nothing HANA would refuse. The lock-and-compare branch HANA takes where the
other databases compare a ``Text`` column in SQL is forced with
``compares_lobs`` and run on SQLite too.

That these statements really run, wait and exclude each other on HANA is
``tests/test_hana_integration.py`` (needs a container).

Run:  python -m pytest tests/test_hana_portable_sql.py -q
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from sqlalchemy import delete, select, update  # noqa: E402
from sqlalchemy.dialects import postgresql, sqlite  # noqa: E402

import app as app_module  # noqa: E402
from agents import db as agents_db  # noqa: E402
from agents.db import AgentConfig, SessionLocal, SkillConfig, init_db  # noqa: E402
from agents.ide import runner, stages, store  # noqa: E402
from agents.ide import seed as seed_module  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeAuditLog,
    IdeComment,
    IdeMessage,
    IdeSession,
    utcnow,
)
from tests.hana_sql import HANA, hana_sql, recorded, refusals  # noqa: E402

OWNER = "alice"


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeArtifact, IdeMessage, IdeAuditLog, IdeSession):
            await db.execute(model.__table__.delete())
        await db.execute(delete(AgentConfig).where(AgentConfig.name == "portable-ag"))
        await db.execute(delete(SkillConfig).where(SkillConfig.name == "portable-sk"))
        await db.commit()


@pytest.fixture
def like_hana(monkeypatch):
    """Take the branch of a database that cannot compare a LOB. SQLite drops
    the ``FOR UPDATE`` that branch relies on, so this proves the statements
    and the outcome, not the wait (the integration suite proves that)."""
    monkeypatch.setattr(agents_db, "compares_lobs", lambda session: False)


async def _session(stage: str = "design", **values) -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner=OWNER, title="t", target="DEMO")
        s.stage = stage
        await db.commit()
        if values:
            # A Core UPDATE: the ORM would stamp ``updated_at`` itself.
            await db.execute(
                update(IdeSession).where(IdeSession.id == s.id).values(**values)
            )
            await db.commit()
        return s.id


async def _comment(sid: str, *, state: str = "open", run: str | None = None) -> str:
    async with SessionLocal() as db:
        c = IdeComment(session_id=sid, anchor="document", kind="design", version=1,
                       paragraph=0, body="b", state=state, sent_run_id=run)
        db.add(c)
        await db.commit()
        return c.id


async def _states(sid: str) -> dict[str, str]:
    async with SessionLocal() as db:
        rows = await db.execute(
            select(IdeComment.id, IdeComment.state).where(IdeComment.session_id == sid))
        return dict(rows.all())


async def _stored(sid: str, column: str):
    async with SessionLocal() as db:
        return (await db.execute(
            select(getattr(IdeSession, column)).where(IdeSession.id == sid)
        )).scalar_one_or_none()


# --- the recorder itself ------------------------------------------------------


def test_the_recorder_knows_what_hana_refuses():
    assert refusals(
        update(IdeSession).values(status="idle").returning(IdeSession.id)
    ) != []
    assert refusals(select(IdeSession.id).where(IdeSession.pins_json == "x")) != []
    assert refusals(select(IdeSession.id).order_by(IdeSession.pins_json)) != []
    assert refusals(select(IdeSession.pins_json).distinct()) != []
    assert refusals(select(IdeSession.id).where(IdeSession.pins_json.is_(None))) == []


async def test_every_statement_of_the_suite_is_checked(request):
    """``tests/conftest.py`` fails the test that executed a statement HANA
    would refuse. Shown here by executing one and taking the finding away
    before the fixture sees it."""
    from tests import conftest, hana_sql

    if not conftest.HANA_SQL_CHECK:
        pytest.skip("switched off with HANA_SQL_CHECK=0")
    async with SessionLocal() as db:
        await db.execute(select(IdeSession.id).where(IdeSession.pins_json == "x"))
        await db.execute(select(IdeSession.id).order_by(IdeSession.pins_json))
        await db.execute(select(IdeSession.id).where(IdeSession.pins_json.is_(None)))
    found = hana_sql.take_refused()
    assert len(found) == 2
    assert found[0].startswith("comparison on a LOB column: ide_sessions.pins_json =")
    assert found[1].startswith("ORDER BY / GROUP BY a LOB column: ide_sessions.pins_json")


def test_the_guarded_compare_is_the_statement_postgres_always_got():
    """``text_unchanged`` marks its value with ``ComparedText`` so the suite
    check can tell it from a hand-written compare. The SQL is unchanged."""
    from sqlalchemy import literal
    from sqlalchemy.dialects.postgresql import asyncpg

    def sql(guard) -> str:
        statement = update(IdeSession).where(IdeSession.id == "s", guard).values(title="t")
        return str(statement.compile(dialect=asyncpg.dialect()))

    plain = sql(IdeSession.pins_json == "seen")
    assert sql(IdeSession.pins_json == literal("seen", agents_db.ComparedText())) == plain
    assert "ide_sessions.pins_json = $" in plain
    assert refusals(select(IdeSession.id).where(
        IdeSession.pins_json == literal("x", agents_db.ComparedText()))) == []
    assert refusals(select(IdeSession.id).where(IdeSession.pins_json == "x")) != []


# --- which database does what ---------------------------------------------------


class _Bound:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_bind(self):
        return type("Bind", (), {"dialect": type("D", (), {"name": self._name})()})()


@pytest.mark.parametrize(
    "name, locks, lobs",
    [("sqlite", False, True), ("postgresql", True, True), ("hana", True, False)],
)
def test_row_locks_and_lob_comparison_per_database(name, locks, lobs):
    assert agents_db.has_row_locks(_Bound(name)) is locks
    assert agents_db.compares_lobs(_Bound(name)) is lobs


def test_the_heartbeat_statement_is_valid_on_all_three():
    statement = app_module.HEARTBEAT
    assert hana_sql(statement) == "SELECT 1 FROM DUMMY"
    # Unchanged where it already worked.
    assert str(statement.compile(dialect=postgresql.dialect())) == "SELECT 1"
    assert str(statement.compile(dialect=sqlite.dialect())) == "SELECT 1"


async def test_the_heartbeat_statement_runs():
    async with SessionLocal() as session:
        assert (await session.execute(app_module.HEARTBEAT)).scalar_one() == 1


# --- the five former RETURNING statements ---------------------------------------


async def test_purge_removes_the_old_idle_sessions_and_only_their_children():
    old = utcnow() - timedelta(days=90)
    gone = await _session(updated_at=old)
    busy = await _session(updated_at=old, status="running", run_id="r")
    fresh = await _session()
    kept = {sid: await _comment(sid) for sid in (gone, busy, fresh)}

    with recorded() as seen:
        async with SessionLocal() as db:
            assert await store.purge_sessions_older_than(db, 30) == 1

    assert seen.refused == []
    # The candidates are locked in one order before anything is deleted.
    assert seen.matching("FROM ide_sessions", "ORDER BY ide_sessions.id FOR UPDATE")
    async with SessionLocal() as db:
        left = set((await db.execute(select(IdeSession.id))).scalars())
        comments = set((await db.execute(select(IdeComment.id))).scalars())
    assert left == {busy, fresh}
    assert comments == {kept[busy], kept[fresh]}


async def test_purge_keeps_the_children_of_a_session_that_became_active(monkeypatch):
    """Selected as old and idle, running by the time of the DELETE (possible
    where the select takes no lock): neither the row nor its children go."""
    old = utcnow() - timedelta(days=90)
    sid = await _session(updated_at=old)
    comment = await _comment(sid)
    real = store._chunks

    def start_a_run(values, *args, **kwargs):
        # Between the select and the DELETE, as another connection would.
        import sqlite3

        raw = sqlite3.connect(str(use_test_database()))
        raw.execute("UPDATE ide_sessions SET status = 'running' WHERE id = ?", (sid,))
        raw.commit()
        raw.close()
        monkeypatch.setattr(store, "_chunks", real)
        return real(values, *args, **kwargs)

    monkeypatch.setattr(store, "_chunks", start_a_run)
    async with SessionLocal() as db:
        assert await store.purge_sessions_older_than(db, 30) == 0
    assert await _stored(sid, "status") == "running"
    assert await _states(sid) == {comment: "open"}


async def test_audit_purge_counts_what_it_deleted():
    async with SessionLocal() as db:
        for age in (400, 400, 1):
            row = await store.add_audit(db, principal="p", session_id="s", target="T",
                                        action=store.AUDIT_ACTIONS[0], params={}, outcome="ok")
            await db.execute(update(IdeAuditLog).where(IdeAuditLog.id == row.id)
                             .values(ts=utcnow() - timedelta(days=age)))
        await db.commit()
    with recorded() as seen:
        async with SessionLocal() as db:
            assert await store.purge_audit_older_than(db, 365) == 2
    assert seen.refused == []
    async with SessionLocal() as db:
        assert len((await db.execute(select(IdeAuditLog.id))).all()) == 1


async def test_startup_reset_frees_the_ghosts_and_reopens_their_comments():
    ghost = await _session(status="running", run_id="dead",
                           updated_at=utcnow() - timedelta(hours=1))
    live = await _session(status="running", run_id="live")
    idle = await _session()
    sent = {sid: await _comment(sid, state="sent", run=run)
            for sid, run in ((ghost, "dead"), (live, "live"), (idle, "old"))}

    with recorded() as seen:
        async with SessionLocal() as db:
            assert await store.reset_running_ide_sessions(db, min_age_s=60) == 1

    assert seen.refused == []
    assert seen.matching("FROM ide_sessions", "FOR UPDATE")
    assert await _stored(ghost, "status") == "idle"
    assert await _stored(ghost, "run_id") is None
    assert await _stored(live, "status") == "running"
    assert await _states(ghost) == {sent[ghost]: "open"}
    assert await _states(live) == {sent[live]: "sent"}
    assert await _states(idle) == {sent[idle]: "sent"}


async def test_marking_comments_sent_returns_exactly_what_it_moved():
    sid = await _session()
    first, second = await _comment(sid), await _comment(sid)
    dismissed = await _comment(sid, state="dismissed")
    # Sent by an earlier run and still in flight: not this run's to report.
    other = await _comment(sid, state="sent", run="run-0")

    with recorded() as seen:
        async with SessionLocal() as db:
            moved = await store.mark_comments_sent(db, sid, "run-1")
            await db.commit()

    assert seen.refused == []
    assert sorted(c.id for c in moved) == sorted([first, second])
    assert {c.state for c in moved} == {"sent"}
    assert {c.sent_run_id for c in moved} == {"run-1"}
    assert await _states(sid) == {first: "sent", second: "sent",
                                  dismissed: "dismissed", other: "sent"}


async def test_marking_with_nothing_open_moves_nothing():
    sid = await _session()
    await _comment(sid, state="dismissed")
    async with SessionLocal() as db:
        assert await store.mark_comments_sent(db, sid, "run-1") == []


async def test_reopening_returns_only_what_the_run_left_sent():
    sid = await _session()
    mine = [await _comment(sid, state="sent", run="run-1") for _ in range(2)]
    theirs = await _comment(sid, state="sent", run="run-2")
    done = await _comment(sid, state="addressed", run="run-1")

    with recorded() as seen:
        async with SessionLocal() as db:
            reopened = await store.reopen_sent_comments(db, sid, run_id="run-1")
            await db.commit()

    assert seen.refused == []
    assert seen.matching("FROM ide_comments", "FOR UPDATE")
    assert sorted(reopened) == sorted(mine)
    states = await _states(sid)
    assert [states[c] for c in mine] == ["open", "open"]
    assert states[theirs] == "sent" and states[done] == "addressed"
    async with SessionLocal() as db:
        assert await store.reopen_sent_comments(db, sid, run_id="run-1") == []


async def test_the_same_statements_as_postgres_gets_them():
    """No Postgres in this suite: the statements that replaced RETURNING are
    read in its spelling here. Each locking select is ``FOR UPDATE`` in id
    (or creation) order, each write repeats the conditions, none returns."""
    old = utcnow() - timedelta(days=90)
    await _session(updated_at=old)
    ghost = await _session(status="running", run_id="dead")
    await _comment(ghost, state="sent", run="dead")
    sid = await _session()
    await _comment(sid)
    with recorded() as seen:
        async with SessionLocal() as db:
            await store.purge_sessions_older_than(db, 30)
            await store.purge_audit_older_than(db, 365)
            await store.reset_running_ide_sessions(db)
            await store.mark_comments_sent(db, sid, "run-1")
            await store.reopen_sent_comments(db, sid, run_id="run-1")
            await db.commit()

    assert not [s for s in seen.postgres if "RETURNING" in s]
    assert seen.on_postgres(
        "SELECT ide_sessions.id FROM ide_sessions WHERE ide_sessions.updated_at <",
        "AND ide_sessions.session_type =", "AND ide_sessions.status !=",
        "ORDER BY ide_sessions.id FOR UPDATE")
    assert seen.on_postgres(
        "DELETE FROM ide_sessions WHERE ide_sessions.id IN",
        "AND ide_sessions.updated_at <", "AND ide_sessions.status !=")
    assert seen.on_postgres("DELETE FROM ide_audit_log WHERE ide_audit_log.ts <")
    assert seen.on_postgres(
        "SELECT ide_sessions.id FROM ide_sessions WHERE ide_sessions.status =",
        "ORDER BY ide_sessions.id FOR UPDATE")
    assert seen.on_postgres(
        "UPDATE ide_sessions SET status=", "WHERE ide_sessions.id IN",
        "AND ide_sessions.status =")
    assert seen.on_postgres(
        "UPDATE ide_comments SET state=", "sent_run_id=",
        "AND ide_comments.state =", "AND ide_comments.id IN")
    assert seen.on_postgres(
        "SELECT ide_comments.id, ide_comments.created_at FROM ide_comments",
        "ide_comments.state =", "ide_comments.sent_run_id =",
        "ORDER BY ide_comments.created_at, ide_comments.id FOR UPDATE")
    assert seen.on_postgres(
        "UPDATE ide_comments SET state=", "WHERE ide_comments.id IN",
        "AND ide_comments.state =", "AND ide_comments.sent_run_id =")


# --- "the Text column still holds what was read" --------------------------------


async def test_databases_that_compare_a_lob_keep_the_compare_in_the_update():
    """Postgres and SQLite: the statement is the one it always was."""
    sid = await _session()
    with recorded() as seen:
        async with SessionLocal() as db:
            session = await db.get(IdeSession, sid)
            await store.set_pin(db, session, "design", 1)
            await store.set_pin(db, session, "plan", 2)
    assert json.loads(await _stored(sid, "pins_json")) == {"design": 1, "plan": 2}
    assert seen.matching("UPDATE ide_sessions", "ide_sessions.pins_json = ?")
    assert seen.on_postgres("UPDATE ide_sessions SET pins_json=", "ide_sessions.pins_json IS NULL")
    assert seen.on_postgres("UPDATE ide_sessions SET pins_json=", "AND ide_sessions.pins_json = ")
    # No lock and no second read where the UPDATE can compare.
    assert not seen.on_postgres("FOR UPDATE")


async def test_pins_are_written_without_comparing_a_lob(like_hana):
    sid = await _session()
    with recorded() as seen:
        async with SessionLocal() as db:
            session = await db.get(IdeSession, sid)
            await store.set_pin(db, session, "design", 1)
            await store.set_pin(db, session, "plan", 2)
    assert seen.refused == []
    assert json.loads(await _stored(sid, "pins_json")) == {"design": 1, "plan": 2}
    # The row is locked, read again and compared here instead.
    assert seen.matching("SELECT ide_sessions.pins_json", "FOR UPDATE")


async def test_text_unchanged_refuses_a_row_that_moved(like_hana):
    sid = await _session(pins_json='{"design": 1}')
    row = IdeSession.id == sid
    async with SessionLocal() as db:
        for seen_text, applies in (('{"design": 1}', 1), ('{"design": 9}', 0), (None, 0)):
            guard = await agents_db.text_unchanged(db, IdeSession.pins_json, seen_text, row)
            result = await db.execute(
                update(IdeSession).where(row, guard).values(title="x")
                .execution_options(synchronize_session=False)
            )
            assert result.rowcount == applies, seen_text
        await db.rollback()
    empty = await _session()
    async with SessionLocal() as db:
        guard = await agents_db.text_unchanged(
            db, IdeSession.pins_json, None, IdeSession.id == empty)
        result = await db.execute(update(IdeSession).where(IdeSession.id == empty, guard)
                                  .values(title="x"))
        assert result.rowcount == 1


async def test_a_pin_lost_to_another_writer_is_a_conflict(like_hana, monkeypatch):
    """Whoever writes between the read and the compare makes the write a
    no-op; five lost rounds are a ``PinConflict``, as before."""
    sid = await _session()
    real = agents_db.text_unchanged
    rounds = 0

    async def racing(db, column, seen_text, *row):
        nonlocal rounds
        rounds += 1
        return await real(db, column, f'{{"design": {rounds + 40}}}', *row)

    monkeypatch.setattr(store, "text_unchanged", racing)
    async with SessionLocal() as db:
        session = await db.get(IdeSession, sid)
        with pytest.raises(store.PinConflict):
            await store.set_pin(db, session, "design", 1)
    assert rounds == 5
    assert await _stored(sid, "pins_json") is None


async def test_approve_pins_without_comparing_a_lob(like_hana):
    sid = await _session()
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="# D")
    with recorded() as seen:
        async with SessionLocal() as db:
            session = await db.get(IdeSession, sid)
            after = await stages.approve(db, session)
    assert seen.refused == []
    assert after.stage == "plan"
    assert json.loads(await _stored(sid, "pins_json")) == {"design": 1}
    # A second approve starts from stored pins, not from NULL.
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="plan", kind="plan", content="# P")
        session = await db.get(IdeSession, sid)
        with recorded() as seen:
            after = await stages.approve(db, session)
    assert seen.refused == []
    assert json.loads(await _stored(sid, "pins_json")) == {"design": 1, "plan": 1}


async def _seed_rows() -> None:
    async with SessionLocal() as db:
        db.add(AgentConfig(name="portable-ag", description="d", instructions="v1",
                           mcp_url="http://example.invalid/mcp", enabled=1))
        db.add(SkillConfig(name="portable-sk", description="d", content="v1"))
        await db.commit()


@pytest.mark.parametrize("model, column, name", [
    (AgentConfig, "instructions", "portable-ag"),
    (SkillConfig, "content", "portable-sk"),
])
async def test_seed_refresh_replaces_only_the_text_it_read(like_hana, model, column, name):
    await _seed_rows()
    with recorded() as seen:
        async with SessionLocal() as db:
            assert await seed_module._refresh_text(db, model, column, name, "v1", "v2")
            # What it read is gone now: an edit made meanwhile wins.
            assert not await seed_module._refresh_text(db, model, column, name, "v1", "v3")
            await db.commit()
    assert seen.refused == []
    assert seen.matching(f"SELECT {model.__tablename__}.{column}", "FOR UPDATE")
    async with SessionLocal() as db:
        stored = (await db.execute(
            select(getattr(model, column)).where(model.name == name))).scalar_one()
    assert stored == "v2"


async def test_seed_refresh_keeps_its_one_statement_where_a_lob_compares():
    await _seed_rows()
    with recorded() as seen:
        async with SessionLocal() as db:
            assert await seed_module._refresh_text(
                db, AgentConfig, "instructions", "portable-ag", "v1", "v2")
            await db.rollback()
    assert len(seen.sql) == 1 and "agent_configs.instructions = ?" in seen.sql[0]


# --- row locks that used to be "Postgres only" -----------------------------------


async def test_a_run_start_and_a_stale_release_lock_the_session_row(monkeypatch):
    """On every database that has row locks, not on Postgres alone."""
    sid = await _session(status="running", run_id="dead",
                         updated_at=utcnow() - timedelta(days=1))
    monkeypatch.setattr(runner, "has_row_locks", lambda db: True)
    with recorded() as seen:
        assert await runner.release_stale(sid)
    assert seen.refused == []
    assert seen.matching("FROM ide_sessions", "WHERE ide_sessions.id = ? FOR UPDATE")

    monkeypatch.setattr(runner, "has_row_locks", lambda db: False)
    async with SessionLocal() as db:
        stmt = runner._locked(db, select(IdeSession).where(IdeSession.id == sid))
    assert "FOR UPDATE" not in str(stmt.compile(dialect=HANA))


def test_the_runner_asks_the_database_layer_which_database_locks():
    assert runner.has_row_locks is agents_db.has_row_locks
