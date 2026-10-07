"""Races of the IDE store, written once and run on every real database.

The statements that replaced ``UPDATE`` / ``DELETE ... RETURNING`` (SAP HANA
has none) and the compare-and-set on a ``Text`` column are "select ``FOR
UPDATE``, write under the same conditions, read back". Their guarantee is
about two connections, which SQLite cannot show (it has no row locks and one
writer). Each scenario here takes a session maker on a real database and is
called from ``tests/test_ide_postgres.py`` and ``tests/test_hana_integration.py``,
so Postgres and HANA are held to the same behaviour.

A scenario creates what it needs and asserts on its own; the caller provides
empty tables.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from agents.db import SkillConfig
from agents.ide import seed as seed_module
from agents.ide import stages, store
from agents.ide.models import IdeComment, IdeSession, utcnow

OWNER = "alice"
# How long a statement that must WAIT for a lock is watched not finishing.
WAITS = 1.5


async def new_session(sessions: Any, stage: str = "design", **values: Any) -> str:
    async with sessions() as db:
        s = await store.create_session(db, owner=OWNER, title="t", target="DEMO")
        s.stage = stage
        await db.commit()
        if values:
            # A Core UPDATE: the ORM would stamp ``updated_at`` itself.
            await db.execute(update(IdeSession).where(IdeSession.id == s.id).values(**values))
            await db.commit()
        return s.id


async def new_comment(sessions: Any, sid: str, *, state: str = "open",
                      run: str | None = None, body: str = "b") -> str:
    async with sessions() as db:
        c = IdeComment(session_id=sid, anchor="document", kind="design", version=1,
                       paragraph=0, body=body, state=state, sent_run_id=run)
        db.add(c)
        await db.commit()
        return c.id


async def states(sessions: Any, sid: str) -> dict[str, str]:
    async with sessions() as db:
        rows = await db.execute(
            select(IdeComment.id, IdeComment.state).where(IdeComment.session_id == sid))
        return dict(rows.all())


async def stored(sessions: Any, sid: str, column: str) -> Any:
    async with sessions() as db:
        return (await db.execute(
            select(getattr(IdeSession, column)).where(IdeSession.id == sid)
        )).scalar_one_or_none()


async def _after_the_holder(holder_work, late) -> Any:
    """Run ``late`` while ``holder_work``'s transaction is open; it must not
    finish before that transaction commits. Returns ``late``'s result."""
    task: asyncio.Task[Any] | None = None
    try:
        async with holder_work() as commit:
            task = asyncio.create_task(late())
            done, _ = await asyncio.wait({task}, timeout=WAITS)
            assert not done, "the second transaction did not wait for the first"
            await commit()
        return await asyncio.wait_for(task, timeout=60)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _Holding:
    """A transaction that has done ``work`` and commits when asked."""

    def __init__(self, sessions: Any, work) -> None:
        self._sessions, self._work = sessions, work

    def __call__(self) -> _Holding:
        return self

    async def __aenter__(self):
        self._db = self._sessions()
        await self._db.__aenter__()
        await self._work(self._db)
        return self._db.commit

    async def __aexit__(self, *exc: Any) -> None:
        await self._db.__aexit__(*exc)


# --- purge against a run that starts ----------------------------------------------


async def purge_waits_for_a_run_start_and_then_keeps_the_session(sessions: Any) -> None:
    """Old and idle when the purge selects it, but a run start holds the row
    and commits ``running``: the purge waits, then removes neither the
    session nor its children."""
    sid = await new_session(sessions, updated_at=utcnow() - timedelta(days=90))
    comment = await new_comment(sessions, sid)

    async def start_a_run(db) -> None:
        await store.lock_session_row(db, sid)
        await db.execute(update(IdeSession).where(IdeSession.id == sid).values(
            status="running", run_id="run-1", updated_at=utcnow()))

    async def purge() -> int:
        async with sessions() as db:
            return await store.purge_sessions_older_than(db, 30)

    assert await _after_the_holder(_Holding(sessions, start_a_run), purge) == 0
    assert await stored(sessions, sid, "status") == "running"
    assert await states(sessions, sid) == {comment: "open"}


async def of_two_purges_each_session_is_counted_once(sessions: Any) -> None:
    old = utcnow() - timedelta(days=90)
    sids = [await new_session(sessions, updated_at=old) for _ in range(4)]
    for sid in sids:
        await new_comment(sessions, sid)

    async def purge() -> int:
        async with sessions() as db:
            return await store.purge_sessions_older_than(db, 30)

    counts = await asyncio.gather(purge(), purge(), purge())
    assert sum(counts) == len(sids), counts
    async with sessions() as db:
        assert (await db.execute(select(IdeSession.id))).all() == []
        assert (await db.execute(select(IdeComment.id))).all() == []


# --- the startup reset against a heartbeat -------------------------------------------


async def reset_waits_for_a_heartbeat_and_then_leaves_the_live_run(sessions: Any) -> None:
    """A ``running`` row old enough to be a ghost, whose run is alive after
    all: its heartbeat holds the row and commits a fresh ``updated_at``. The
    reset waits, then leaves the run and what it sent alone."""
    sid = await new_session(sessions, status="running", run_id="live",
                            updated_at=utcnow() - timedelta(hours=1))
    sent = await new_comment(sessions, sid, state="sent", run="live")
    ghost = await new_session(sessions, status="running", run_id="dead",
                              updated_at=utcnow() - timedelta(hours=1))
    ghost_sent = await new_comment(sessions, ghost, state="sent", run="dead")

    async def heartbeat(db) -> None:
        await db.execute(update(IdeSession).where(
            IdeSession.id == sid, IdeSession.run_id == "live").values(updated_at=utcnow()))

    async def reset() -> int:
        async with sessions() as db:
            return await store.reset_running_ide_sessions(db, min_age_s=60)

    assert await _after_the_holder(_Holding(sessions, heartbeat), reset) == 1
    assert await stored(sessions, sid, "status") == "running"
    assert await stored(sessions, sid, "run_id") == "live"
    assert await states(sessions, sid) == {sent: "sent"}
    assert await stored(sessions, ghost, "status") == "idle"
    assert await states(sessions, ghost) == {ghost_sent: "open"}


# --- comments: sent and reopened -------------------------------------------------------


async def two_runs_marking_comments_sent_share_no_comment(sessions: Any) -> None:
    """Each open comment is sent by exactly one of two starts, and each start
    reports exactly the comments that now carry its run id."""
    sid = await new_session(sessions)
    ids = [await new_comment(sessions, sid) for _ in range(6)]

    async def mark(run: str) -> list[str]:
        async with sessions() as db:
            moved = await store.mark_comments_sent(db, sid, run)
            await db.commit()
            return [c.id for c in moved]

    for _ in range(3):
        first, second = await asyncio.gather(mark("run-a"), mark("run-b"))
        assert sorted(first + second) == sorted(ids), (first, second)
        async with sessions() as db:
            rows = dict((await db.execute(
                select(IdeComment.id, IdeComment.sent_run_id)
                .where(IdeComment.session_id == sid))).all())
        assert {i for i, run in rows.items() if run == "run-a"} == set(first)
        assert {i for i, run in rows.items() if run == "run-b"} == set(second)
        async with sessions() as db:
            await db.execute(update(IdeComment).where(IdeComment.session_id == sid)
                             .values(state="open", sent_run_id=None))
            await db.commit()


async def a_comment_is_reopened_by_exactly_one_of_two_releases(sessions: Any) -> None:
    """A run that ends and a reclaim of the same run, at once (each takes the
    session row first, as the runner does): no id is reported twice."""
    sid = await new_session(sessions)
    for _ in range(3):
        ids = [await new_comment(sessions, sid, state="sent", run="run-1") for _ in range(4)]
        theirs = await new_comment(sessions, sid, state="sent", run="run-2")

        async def release() -> list[str]:
            async with sessions() as db:
                await store.lock_session_row(db, sid)
                reopened = await store.reopen_sent_comments(db, sid, run_id="run-1")
                await db.commit()
                return reopened

        first, second = await asyncio.gather(release(), release())
        assert sorted(first + second) == sorted(ids), (first, second)
        now = await states(sessions, sid)
        assert all(now[i] == "open" for i in ids) and now[theirs] == "sent"
        async with sessions() as db:
            await db.execute(IdeComment.__table__.delete())
            await db.commit()


# --- pins and approve ---------------------------------------------------------------------


async def concurrent_pin_writers_all_land(sessions: Any) -> None:
    """Compare-and-set: no writer overwrites another's pin."""
    sid = await new_session(sessions)

    async def pin(kind: str, version: int) -> None:
        async with sessions() as db:
            session = await db.get(IdeSession, sid)
            await store.set_pin(db, session, kind, version)

    for round_ in range(1, 4):
        await asyncio.gather(pin("design", round_), pin("plan", round_), pin("review", round_))
        assert json.loads(await stored(sessions, sid, "pins_json")) == {
            "design": round_, "plan": round_, "review": round_}


async def of_two_concurrent_approves_one_moves_the_stage(sessions: Any) -> None:
    """Both saw ``design``; exactly one approve moves it to ``plan`` and pins
    the design, the other is told the stage changed."""
    for _ in range(3):
        sid = await new_session(sessions)
        async with sessions() as db:
            await store.add_artifact(db, sid, stage="design", kind="design", content="# D")

        # Both load the session before either approves: "both saw design"
        # must not depend on how long a connection takes to open.
        async with sessions() as one, sessions() as two:
            loaded = [(db, await db.get(IdeSession, sid)) for db in (one, two)]
            assert [row.stage for _, row in loaded] == ["design", "design"]
            for db, _ in loaded:
                await db.commit()  # ends the read; the loaded row stays usable

            async def approve(db: Any, row: IdeSession) -> str:
                return (await stages.approve(db, row)).stage

            answers = await asyncio.gather(
                *(approve(db, row) for db, row in loaded), return_exceptions=True)
        won = [a for a in answers if not isinstance(a, BaseException)]
        lost = [a for a in answers if isinstance(a, BaseException)]
        assert won == ["plan"], answers
        assert [type(a) for a in lost] == [stages.StageGateError], answers
        assert lost[0].code == "stage_changed"
        assert await stored(sessions, sid, "stage") == "plan"
        assert json.loads(await stored(sessions, sid, "pins_json")) == {"design": 1}


# --- the seed refresh ------------------------------------------------------------------------


async def of_two_seed_refreshes_from_one_read_text_one_wins(sessions: Any) -> None:
    """Both read ``v1``; one replaces it, the other finds it changed."""
    long_text = "v1 " + "x" * 9000
    async with sessions() as db:
        db.add(SkillConfig(name="race-sk", description="d", content=long_text))
        await db.commit()

    async def refresh(new: str) -> bool:
        async with sessions() as db:
            done = await seed_module._refresh_text(
                db, SkillConfig, "content", "race-sk", long_text, new)
            await db.commit()
            return done

    answers = await asyncio.gather(refresh("from a"), refresh("from b"), refresh("from c"))
    assert sorted(answers) == [False, False, True], answers
    async with sessions() as db:
        text_ = (await db.execute(select(SkillConfig.content))).scalar_one()
    assert text_ == "from " + "abc"[answers.index(True)]


SCENARIOS = (
    purge_waits_for_a_run_start_and_then_keeps_the_session,
    of_two_purges_each_session_is_counted_once,
    reset_waits_for_a_heartbeat_and_then_leaves_the_live_run,
    two_runs_marking_comments_sent_share_no_comment,
    a_comment_is_reopened_by_exactly_one_of_two_releases,
    concurrent_pin_writers_all_land,
    of_two_concurrent_approves_one_moves_the_stage,
    of_two_seed_refreshes_from_one_read_text_one_wins,
)
