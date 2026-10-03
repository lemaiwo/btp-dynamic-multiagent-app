"""IDE persistence: owner-scoped sessions, artifact versions, purge."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_store.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.store import (  # noqa: E402
    add_artifact,
    add_message,
    create_session,
    delete_owned_session,
    get_conventions,
    get_owned_session,
    list_all_sessions_meta,
    list_artifacts,
    list_conventions,
    list_messages,
    list_owned_sessions,
    purge_sessions_older_than,
    reset_running_ide_sessions,
    upsert_conventions,
)


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession,
                      IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
    yield


async def test_owner_isolation():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        assert s.stage == "chat"
        assert s.status == "idle"
        assert s.requests_used == 0
        assert await get_owned_session(db, s.id, "bob") is None
        assert (await get_owned_session(db, s.id, "alice")).id == s.id
        assert [x.id for x in await list_owned_sessions(db, "bob")] == []
        assert [x.id for x in await list_owned_sessions(db, "alice")] == [s.id]
        assert await delete_owned_session(db, s.id, "bob") is False
        assert await get_owned_session(db, s.id, "alice") is not None
        assert await delete_owned_session(db, s.id, "alice") is True
        assert await get_owned_session(db, s.id, "alice") is None


async def test_delete_owned_session_removes_children():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        await add_message(db, s.id, stage="chat", role="user", content="hi")
        await add_artifact(db, s.id, stage="design", kind="design", content="d")
        db.add(IdeWorkspaceFile(session_id=s.id, path="notes/a.md", state="new"))
        await db.commit()
        assert await delete_owned_session(db, s.id, "alice") is True
        for model in (IdeMessage, IdeArtifact, IdeWorkspaceFile):
            rows = (
                await db.execute(select(model).where(model.session_id == s.id))
            ).scalars().all()
            assert rows == []


async def test_list_owned_sessions_newest_first():
    async with SessionLocal() as db:
        a = await create_session(db, owner="alice", title="a", target="T1")
        b = await create_session(db, owner="alice", title="b", target="T1")
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id == a.id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
        await db.commit()
        assert [x.id for x in await list_owned_sessions(db, "alice")] == [b.id, a.id]


async def test_messages_in_order():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        m1 = await add_message(db, s.id, stage="chat", role="user", content="q")
        m2 = await add_message(
            db, s.id, stage="chat", role="assistant", content="a", activity_json="[]"
        )
        msgs = await list_messages(db, s.id)
        assert [m.id for m in msgs] == [m1.id, m2.id]
        assert msgs[1].activity_json == "[]"


async def test_artifact_versions_increment_per_kind():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        other = await create_session(db, owner="alice", title="o", target="T1")
        d1 = await add_artifact(db, s.id, stage="design", kind="design", content="v1")
        d2 = await add_artifact(db, s.id, stage="design", kind="design", content="v2")
        p1 = await add_artifact(db, s.id, stage="plan", kind="plan", content="p")
        o1 = await add_artifact(db, other.id, stage="design", kind="design", content="x")
        assert (d1.version, d2.version, p1.version, o1.version) == (1, 2, 1, 1)
        designs = await list_artifacts(db, s.id, kind="design")
        assert [a.version for a in designs] == [2, 1]
        assert len(await list_artifacts(db, s.id)) == 3


async def test_admin_meta_has_no_content():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        await add_message(db, s.id, stage="chat", role="user", content="secret")
        meta = await list_all_sessions_meta(db)
        assert len(meta) == 1
        assert set(meta[0]) == {
            "id", "owner", "title", "target", "stage", "status",
            "created_at", "updated_at",
        }
        assert "secret" not in repr(meta)


async def test_purge_removes_children():
    async with SessionLocal() as db:
        old = await create_session(db, owner="alice", title="old", target="T1")
        new = await create_session(db, owner="alice", title="new", target="T1")
        for sid in (old.id, new.id):
            await add_message(db, sid, stage="chat", role="user", content="m")
            await add_artifact(db, sid, stage="design", kind="design", content="d")
            db.add(IdeWorkspaceFile(session_id=sid, path="src/CLAS/zcl_x.clas.abap",
                                    object_type="CLAS", object_name="ZCL_X",
                                    state="read"))
        await db.commit()
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id == old.id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(days=40))
        )
        await db.commit()

        assert await purge_sessions_older_than(db, 30) == 1

        assert await get_owned_session(db, old.id, "alice") is None
        assert await get_owned_session(db, new.id, "alice") is not None
        for model in (IdeMessage, IdeArtifact, IdeWorkspaceFile):
            gone = (
                await db.execute(select(model).where(model.session_id == old.id))
            ).scalars().all()
            kept = (
                await db.execute(select(model).where(model.session_id == new.id))
            ).scalars().all()
            assert gone == []
            assert len(kept) == 1


async def test_workspace_path_unique_per_session():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        db.add(IdeWorkspaceFile(session_id=s.id, path="notes/a.md", state="new"))
        await db.commit()
        db.add(IdeWorkspaceFile(session_id=s.id, path="notes/a.md", state="new"))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_conventions_upsert_and_list():
    async with SessionLocal() as db:
        await upsert_conventions(
            db, "T_UPSERT", label="Dev", destination="arc1-abap-readonly",
            namespace="Z", package="ZDEV", atc_variant="DEFAULT", free_text="x",
        )
        c = await get_conventions(db, "T_UPSERT")
        assert c.label == "Dev"
        assert c.clean_core_level == "A"
        await upsert_conventions(db, "T_UPSERT", label="Dev 2", clean_core_level="B")
        c = await get_conventions(db, "T_UPSERT")
        assert (c.label, c.clean_core_level, c.package) == ("Dev 2", "B", "ZDEV")
        assert "T_UPSERT" in [x.target for x in await list_conventions(db)]
        assert await get_conventions(db, "NOPE") is None


async def _backdate(db, sid, days):
    await db.execute(
        update(IdeSession)
        .where(IdeSession.id == sid)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(days=days))
    )
    await db.commit()


async def test_message_keeps_session_alive():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        await _backdate(db, s.id, 40)
        await add_message(db, s.id, stage="chat", role="user", content="still here")
        assert await purge_sessions_older_than(db, 30) == 0
        assert await get_owned_session(db, s.id, "alice") is not None


async def test_artifact_keeps_session_alive():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        await _backdate(db, s.id, 40)
        await add_artifact(db, s.id, stage="design", kind="design", content="d")
        assert await purge_sessions_older_than(db, 30) == 0
        assert await get_owned_session(db, s.id, "alice") is not None


async def test_purge_skips_running_session():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        await add_message(db, s.id, stage="chat", role="user", content="m")
        await db.execute(
            update(IdeSession).where(IdeSession.id == s.id).values(status="running")
        )
        await db.commit()
        await _backdate(db, s.id, 40)
        assert await purge_sessions_older_than(db, 30) == 0
        assert await get_owned_session(db, s.id, "alice") is not None
        assert len(await list_messages(db, s.id)) == 1


async def test_purge_rejects_days_below_one():
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await purge_sessions_older_than(db, 0)


async def test_update_bumps_updated_at_with_microseconds():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        await _backdate(db, s.id, 2)
        s.title = "renamed"
        await db.commit()
        await db.refresh(s)
        age = datetime.now(timezone.utc) - s.updated_at.replace(tzinfo=timezone.utc)
        assert age < timedelta(minutes=1)


async def test_reset_running_ide_sessions():
    async with SessionLocal() as db:
        a = await create_session(db, owner="alice", title="a", target="T1")
        b = await create_session(db, owner="bob", title="b", target="T1")
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id == a.id)
            .values(status="running", run_id="r1")
        )
        await db.commit()
        assert await reset_running_ide_sessions(db) == 1
        assert await reset_running_ide_sessions(db) == 0
    async with SessionLocal() as db:
        ra = await get_owned_session(db, a.id, "alice")
        rb = await get_owned_session(db, b.id, "bob")
        assert (ra.status, ra.run_id) == ("idle", None)
        assert rb.status == "idle"


async def test_reset_running_ide_sessions_keeps_fresh_rows():
    # Another instance's live run keeps its row fresh (heartbeat): a startup
    # reset with a minimum age leaves it alone and resets only old ghosts.
    async with SessionLocal() as db:
        live = await create_session(db, owner="alice", title="a", target="T1")
        ghost = await create_session(db, owner="bob", title="b", target="T1")
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id.in_([live.id, ghost.id]))
            .values(status="running", run_id="r")
        )
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id == ghost.id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        )
        await db.commit()
        assert await reset_running_ide_sessions(db, min_age_s=90) == 1
    async with SessionLocal() as db:
        assert (await get_owned_session(db, live.id, "alice")).status == "running"
        assert (await get_owned_session(db, ghost.id, "bob")).status == "idle"
