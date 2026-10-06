"""IDE persistence: owner-scoped sessions, artifact versions, purge."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeArtifact,
    IdeAuditLog,
    IdeConventions,
    IdeFinding,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.store import (  # noqa: E402
    add_approval,
    add_artifact,
    add_audit,
    add_message,
    create_session,
    decide_approval,
    delete_owned_session,
    expire_pending_approvals,
    get_approval,
    get_conventions,
    get_finding,
    get_owned_session,
    list_all_sessions_meta,
    list_approvals,
    list_artifacts,
    list_conventions,
    list_findings,
    list_messages,
    list_owned_sessions,
    purge_audit_older_than,
    purge_sessions_older_than,
    reset_running_ide_sessions,
    touch_session,
    upsert_conventions,
    upsert_findings,
)


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeFinding, IdeApproval, IdeAuditLog, IdeWorkspaceFile,
                      IdeArtifact, IdeMessage, IdeSession, IdeConventions):
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
            "id", "owner", "title", "target", "type", "stage", "status",
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


# --- phase 1c: typed sessions, findings, approvals, audit ------------------


async def test_create_session_with_type():
    async with SessionLocal() as db:
        c = await create_session(db, owner="alice", title="c", target="T1")
        d = await create_session(
            db, owner="alice", title="d", target="T1", session_type="diagnose"
        )
        assert c.session_type == "change"
        assert d.session_type == "diagnose"
        assert d.meta()["type"] == "diagnose"


async def test_create_session_rejects_unknown_type():
    async with SessionLocal() as db:
        for bad in ("debug", "", "Diagnose", None, 1):
            with pytest.raises(ValueError):
                await create_session(
                    db, owner="alice", title="x", target="T1", session_type=bad
                )
        assert await list_owned_sessions(db, "alice") == []


async def test_conventions_non_production_roundtrip():
    async with SessionLocal() as db:
        await upsert_conventions(db, "T_NP", label="Dev")
        assert (await get_conventions(db, "T_NP")).non_production is False
        await upsert_conventions(db, "T_NP", actor="test-admin", non_production=True)
        assert (await get_conventions(db, "T_NP")).non_production is True
        # Other fields leave the flag alone; False switches it back off.
        await upsert_conventions(db, "T_NP", label="Dev 2")
        assert (await get_conventions(db, "T_NP")).non_production is True
        await upsert_conventions(db, "T_NP", actor="test-admin", non_production=False)
        assert (await get_conventions(db, "T_NP")).non_production is False


async def test_upsert_conventions_audits_every_flag_change():
    """No helper flips ``non_production`` without an audit row: the upsert
    refuses a flag change without an actor and audits one with an actor."""
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await upsert_conventions(db, "T_UPA", non_production=True)
        assert await get_conventions(db, "T_UPA") is None
        await upsert_conventions(db, "T_UPA", label="x")
        with pytest.raises(ValueError):
            await upsert_conventions(db, "T_UPA", non_production=True)
        await db.rollback()
        assert (await get_conventions(db, "T_UPA")).non_production is False

        await upsert_conventions(db, "T_UPA", actor="test-admin", non_production=True)
        # Unchanged value: no second row.
        await upsert_conventions(db, "T_UPA", actor="test-admin", non_production=True)
        await upsert_conventions(db, "T_UPA2", actor="test-admin", non_production=True)
        rows = (await db.execute(
            select(IdeAuditLog).where(
                IdeAuditLog.action == "conventions_flag",
                IdeAuditLog.target.in_(("T_UPA", "T_UPA2")),
            )
        )).scalars().all()
        assert sorted((r.target, r.principal) for r in rows) == [
            ("T_UPA", "test-admin"), ("T_UPA2", "test-admin"),
        ]


async def test_upsert_findings_updates_in_place():
    async with SessionLocal() as db:
        s = await create_session(
            db, owner="alice", title="t", target="T1", session_type="diagnose"
        )
        await _backdate(db, s.id, 40)
        [f1] = await upsert_findings(db, s.id, [{
            "kind": "dump", "ref_id": "D1", "title": "x" * 300,
            "program": "P" * 60, "include": "I" * 60, "line": 12,
            "occurred_at": "2026-10-03T10:00:00Z",
            "payload": "raw dump text must not be stored",
        }])
        assert len(f1.title) == 200
        assert (len(f1.program), len(f1.include)) == (40, 40)
        assert f1.line == 12
        assert not hasattr(f1, "payload")
        # The write keeps the session alive.
        assert await purge_sessions_older_than(db, 30) == 0

        [f2] = await upsert_findings(db, s.id, [{
            "kind": "dump", "ref_id": "D1", "title": "new title", "line": 13,
        }])
        assert f2.id == f1.id
        rows = await list_findings(db, s.id)
        assert len(rows) == 1
        assert (rows[0].title, rows[0].line) == ("new title", 13)

        # Same ref under another kind is a separate finding.
        await upsert_findings(db, s.id, [
            {"kind": "trace", "ref_id": "D1", "title": "t"},
        ])
        assert len(await list_findings(db, s.id)) == 2


async def test_upsert_findings_keeps_metadata_a_later_read_does_not_know():
    """A list read after a detail read knows less, not something else: a
    ``None`` (or a missing title) must not wipe what is stored."""
    async with SessionLocal() as db:
        s = await create_session(
            db, owner="alice", title="t", target="T1", session_type="diagnose"
        )
        [first] = await upsert_findings(db, s.id, [{
            "kind": "dump", "ref_id": "D1", "title": "COMPUTE_INT_ZERODIVIDE",
            "program": "ZPROG", "include": "ZINCL", "line": 12,
            "occurred_at": "2026-10-03T10:00:00Z", "detail": "text",
        }])
        [again] = await upsert_findings(db, s.id, [{
            "kind": "dump", "ref_id": "D1", "title": None, "program": None,
            "include": None, "line": None, "occurred_at": None,
        }])
        assert again.id == first.id
        [row] = await list_findings(db, s.id)
        assert (row.title, row.program, row.include, row.line, row.occurred_at) == (
            "COMPUTE_INT_ZERODIVIDE", "ZPROG", "ZINCL", 12, "2026-10-03T10:00:00Z",
        )
        assert row.detail == "text"

        # A value that is known still replaces the stored one.
        await upsert_findings(db, s.id, [{
            "kind": "dump", "ref_id": "D1", "title": "OTHER", "program": "ZNEW",
            "line": 0,
        }])
        [row] = await list_findings(db, s.id)
        assert (row.title, row.program, row.include, row.line) == (
            "OTHER", "ZNEW", "ZINCL", 0,
        )

        # The same within one call: a later item of the same key adds to the
        # earlier one, and a brand-new finding without a title gets "".
        await upsert_findings(db, s.id, [
            {"kind": "dump", "ref_id": "D2", "title": "T2", "program": "ZP2", "line": 5},
            {"kind": "dump", "ref_id": "D2", "title": None, "include": "ZI2"},
            {"kind": "dump", "ref_id": "D3"},
        ])
        by_ref = {r.ref_id: r for r in await list_findings(db, s.id)}
        assert (by_ref["D2"].title, by_ref["D2"].program, by_ref["D2"].include,
                by_ref["D2"].line) == ("T2", "ZP2", "ZI2", 5)
        assert by_ref["D3"].title == ""



async def test_upsert_findings_rejects_bad_items():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        for bad in (
            {"kind": "bogus", "ref_id": "R", "title": "t"},
            {"kind": "dump", "ref_id": "", "title": "t"},
            {"kind": "dump", "ref_id": 5, "title": "t"},
        ):
            with pytest.raises(ValueError):
                await upsert_findings(db, s.id, [bad])
        assert await list_findings(db, s.id) == []


async def test_findings_scoped_to_session():
    async with SessionLocal() as db:
        a = await create_session(db, owner="alice", title="a", target="T1")
        b = await create_session(db, owner="bob", title="b", target="T1")
        [fa] = await upsert_findings(
            db, a.id, [{"kind": "dump", "ref_id": "R", "title": "a"}]
        )
        [fb] = await upsert_findings(
            db, b.id, [{"kind": "dump", "ref_id": "R", "title": "b"}]
        )
        assert fa.id != fb.id
        assert await get_finding(db, a.id, fb.id) is None
        assert (await get_finding(db, a.id, fa.id)).title == "a"
        assert [f.id for f in await list_findings(db, a.id)] == [fa.id]


async def test_approvals_scoped_to_session():
    async with SessionLocal() as db:
        a = await create_session(db, owner="alice", title="a", target="T1")
        b = await create_session(db, owner="bob", title="b", target="T1")
        ap = await add_approval(
            db, a.id, run_id="r1", tool_call_id="c1", action="trace_start",
            params={"processType": "http"},
        )
        assert ap.status == "pending"
        assert await get_approval(db, b.id, ap.id) is None
        assert await list_approvals(db, b.id) == []
        assert [x.id for x in await list_approvals(db, a.id)] == [ap.id]
        assert await decide_approval(db, b.id, ap.id, status="approved", max_age_min=15) is None
        assert (await get_approval(db, a.id, ap.id)).status == "pending"


async def test_add_approval_rejects_unknown_action():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        with pytest.raises(ValueError):
            await add_approval(
                db, s.id, run_id=None, tool_call_id=None,
                action="set_sql_trace_state", params={},
            )


async def test_decide_approval_only_once():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        ap = await add_approval(
            db, s.id, run_id="r1", tool_call_id="c1", action="trace_start",
            params={"processType": "http"},
        )
        done = await decide_approval(
            db, s.id, ap.id, status="approved",
            result={"trace_request_id": "TR1"}, max_age_min=15,
        )
        assert done.status == "approved"
        assert done.decided_at is not None
        assert '"TR1"' in done.result_json
        assert await decide_approval(db, s.id, ap.id, status="denied", max_age_min=15) is None
        again = await get_approval(db, s.id, ap.id)
        assert again.status == "approved"
        assert '"TR1"' in again.result_json
        with pytest.raises(ValueError):
            await decide_approval(db, s.id, ap.id, status="pending", max_age_min=15)


async def test_decide_approval_refuses_expired():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        ap = await add_approval(
            db, s.id, run_id=None, tool_call_id=None, action="trace_cancel",
            params={"id": "TR1"},
        )
        await db.execute(
            update(IdeApproval).where(IdeApproval.id == ap.id).values(
                created_at=datetime.now(timezone.utc) - timedelta(minutes=30)
            )
        )
        await db.commit()
        assert await decide_approval(
            db, s.id, ap.id, status="approved", max_age_min=15
        ) is None
        assert (await get_approval(db, s.id, ap.id)).status == "pending"


async def test_expire_pending_approvals():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        old = await add_approval(
            db, s.id, run_id=None, tool_call_id=None, action="trace_start",
            params={},
        )
        fresh = await add_approval(
            db, s.id, run_id=None, tool_call_id=None, action="trace_start",
            params={},
        )
        decided = await add_approval(
            db, s.id, run_id=None, tool_call_id=None, action="trace_start",
            params={},
        )
        await decide_approval(db, s.id, decided.id, status="denied", max_age_min=15)
        await db.execute(
            update(IdeApproval)
            .where(IdeApproval.id.in_([old.id, decided.id]))
            .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )
        await db.commit()
        assert await expire_pending_approvals(db, older_than_min=15) == 1
        assert await expire_pending_approvals(db, older_than_min=15) == 0
        assert (await get_approval(db, s.id, old.id)).status == "expired"
        assert (await get_approval(db, s.id, fresh.id)).status == "pending"
        assert (await get_approval(db, s.id, decided.id)).status == "denied"
        assert await decide_approval(db, s.id, old.id, status="approved", max_age_min=15) is None


async def test_delete_session_removes_findings_and_approvals_but_not_audit():
    async with SessionLocal() as db:
        s = await create_session(
            db, owner="alice", title="t", target="T1", session_type="diagnose"
        )
        await upsert_findings(db, s.id, [{"kind": "dump", "ref_id": "R", "title": "t"}])
        await add_approval(
            db, s.id, run_id=None, tool_call_id=None, action="trace_start",
            params={},
        )
        entry = await add_audit(
            db, principal="DEVUSER01", session_id=s.id, target="T1",
            action="trace_arm", params={"maxExecutions": 1}, outcome="ok",
            request_id="req-1",
        )
        assert entry.params_json == '{"maxExecutions": 1}'
        assert await delete_owned_session(db, s.id, "alice") is True
        for model in (IdeFinding, IdeApproval):
            rows = (
                await db.execute(select(model).where(model.session_id == s.id))
            ).scalars().all()
            assert rows == []
        audit = (
            await db.execute(
                select(IdeAuditLog).where(IdeAuditLog.session_id == s.id)
            )
        ).scalars().all()
        assert [a.id for a in audit] == [entry.id]


async def test_add_audit_rejects_unknown_action():
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await add_audit(
                db, principal="DEVUSER01", session_id="s", target="T1",
                action="delete_everything", params={}, outcome="ok",
            )


def test_store_has_no_audit_mutators():
    import agents.ide.store as store

    names = [
        n for n in dir(store)
        if "audit" in n.lower() and n[0].islower() and callable(getattr(store, n))
    ]
    # purge_audit_older_than deletes by age only, never by session.
    assert names == ["add_audit", "purge_audit_older_than"]



# --- Task 13: retention by type, required expiry, truncation ---------------


async def _age(db, model, row_id, *, field, days):
    await db.execute(
        update(model).where(model.id == row_id).values(
            **{field: datetime.now(timezone.utc) - timedelta(days=days)}
        ).execution_options(synchronize_session=False)
    )
    await db.commit()


async def test_decide_approval_requires_max_age_min():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        ap = await add_approval(
            db, s.id, run_id=None, tool_call_id=None, action="trace_start", params={}
        )
        with pytest.raises(TypeError):
            await decide_approval(db, s.id, ap.id, status="approved")


async def test_diagnose_purged_after_14_days_even_if_active():
    async with SessionLocal() as db:
        s = await create_session(
            db, owner="alice", title="d", target="T1", session_type="diagnose"
        )
        await _age(db, IdeSession, s.id, field="created_at", days=20)
        await touch_session(db, s.id)  # recent activity must not extend it
        await db.commit()
        assert await purge_sessions_older_than(
            db, 14, session_type="diagnose", by="created_at"
        ) == 1
        assert await get_owned_session(db, s.id, "alice") is None


async def test_change_session_kept_at_14_days():
    async with SessionLocal() as db:
        c = await create_session(db, owner="alice", title="c", target="T1")
        # updated_at last: any UPDATE of the row would bump it (onupdate).
        await _age(db, IdeSession, c.id, field="created_at", days=20)
        await _age(db, IdeSession, c.id, field="updated_at", days=20)
        # A diagnose pass never touches a change session ...
        assert await purge_sessions_older_than(
            db, 14, session_type="diagnose", by="created_at"
        ) == 0
        assert await get_owned_session(db, c.id, "alice") is not None
        # ... and the change pass keeps it until its own (longer) window.
        assert await purge_sessions_older_than(db, 30) == 0
        assert await purge_sessions_older_than(db, 14) == 1


async def test_change_pass_ignores_diagnose_sessions():
    async with SessionLocal() as db:
        d = await create_session(
            db, owner="alice", title="d", target="T1", session_type="diagnose"
        )
        await _age(db, IdeSession, d.id, field="updated_at", days=400)
        assert await purge_sessions_older_than(db, 30) == 0
        assert await get_owned_session(db, d.id, "alice") is not None


async def test_purge_removes_findings_and_approvals():
    async with SessionLocal() as db:
        d = await create_session(
            db, owner="alice", title="d", target="T1", session_type="diagnose"
        )
        keep = await create_session(
            db, owner="alice", title="k", target="T1", session_type="diagnose"
        )
        await upsert_findings(db, d.id, [{"kind": "dump", "ref_id": "R", "title": "t"}])
        await upsert_findings(db, keep.id, [{"kind": "dump", "ref_id": "R", "title": "t"}])
        await add_approval(
            db, d.id, run_id=None, tool_call_id=None, action="trace_start", params={}
        )
        await _age(db, IdeSession, d.id, field="created_at", days=15)
        assert await purge_sessions_older_than(
            db, 14, session_type="diagnose", by="created_at"
        ) == 1
        for model in (IdeFinding, IdeApproval):
            assert (
                await db.execute(select(model).where(model.session_id == d.id))
            ).scalars().all() == []
        assert len(await list_findings(db, keep.id)) == 1


async def test_running_diagnose_session_not_purged():
    async with SessionLocal() as db:
        d = await create_session(
            db, owner="alice", title="d", target="T1", session_type="diagnose"
        )
        await db.execute(
            update(IdeSession).where(IdeSession.id == d.id).values(status="running")
        )
        await _age(db, IdeSession, d.id, field="created_at", days=60)
        assert await purge_sessions_older_than(
            db, 14, session_type="diagnose", by="created_at"
        ) == 0
        assert await get_owned_session(db, d.id, "alice") is not None


async def test_purge_rejects_bad_type_and_clock():
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await purge_sessions_older_than(db, 14, session_type="other")
        with pytest.raises(ValueError):
            await purge_sessions_older_than(db, 14, by="owner")


async def test_audit_purged_separately():
    async with SessionLocal() as db:
        s = await create_session(
            db, owner="alice", title="d", target="T1", session_type="diagnose"
        )
        old = await add_audit(
            db, principal="DEVUSER01", session_id=s.id, target="T1",
            action="trace_arm", params={}, outcome="ok",
        )
        new = await add_audit(
            db, principal="DEVUSER01", session_id=s.id, target="T1",
            action="trace_deny", params={}, outcome="ok",
        )
        await _age(db, IdeAuditLog, old.id, field="ts", days=400)
        # A session purge leaves the audit rows alone ...
        await _age(db, IdeSession, s.id, field="created_at", days=40)
        assert await purge_sessions_older_than(
            db, 14, session_type="diagnose", by="created_at"
        ) == 1
        ids = {a.id for a in (await db.execute(select(IdeAuditLog))).scalars().all()}
        assert ids == {old.id, new.id}
        # ... only the audit pass removes them, by its own age.
        assert await purge_audit_older_than(db, 365) == 1
        ids = {a.id for a in (await db.execute(select(IdeAuditLog))).scalars().all()}
        assert ids == {new.id}
        with pytest.raises(ValueError):
            await purge_audit_older_than(db, 0)


async def test_retention_defaults():
    import agents.ide.store as store

    assert store.IDE_DIAGNOSE_RETENTION_DAYS == 14
    assert store.IDE_AUDIT_RETENTION_DAYS == 365
    assert store.APPROVAL_TTL_MIN == 15


async def test_add_approval_and_audit_truncate_to_column_sizes():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        ap = await add_approval(
            db, s.id, run_id="r" * 80, tool_call_id="c" * 300,
            action="trace_start", params={},
        )
        assert len(ap.run_id) == 36 and len(ap.tool_call_id) == 100
        au = await add_audit(
            db, principal="P", session_id="s" * 80, target="t" * 200,
            action="trace_arm", params={}, outcome="ok",
        )
        assert len(au.target) == 64 and len(au.session_id) == 36


async def test_finding_reads_populate_existing():
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="a", target="T1")
        [f] = await upsert_findings(db, s.id, [{"kind": "dump", "ref_id": "R", "title": "old"}])
        async with SessionLocal() as other:
            await other.execute(
                update(IdeFinding).where(IdeFinding.id == f.id).values(title="new")
            )
            await other.commit()
        assert (await get_finding(db, s.id, f.id)).title == "new"
        await other_reset(db, f.id, "newer")
        assert (await list_findings(db, s.id))[0].title == "newer"


async def other_reset(db, fid, title):
    async with SessionLocal() as other:
        await other.execute(
            update(IdeFinding).where(IdeFinding.id == fid).values(title=title)
        )
        await other.commit()


def test_approval_ttl_min_is_clamped(monkeypatch):
    import agents.ide.store as store

    monkeypatch.setattr(store, "APPROVAL_TTL_MIN", 0)
    assert store.approval_ttl_min() == 1  # 0 only disables the sweep
    monkeypatch.setattr(store, "APPROVAL_TTL_MIN", 30)
    assert store.approval_ttl_min() == 30


def test_audit_log_is_not_a_session_child():
    from agents.ide.models import IDE_CHILD_MODELS

    assert IdeAuditLog not in IDE_CHILD_MODELS


# --- environment values ------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, 14), ("", 14), ("  ", 14), ("7", 7), (" 7 ", 7), ("0", 0),
    ("fourteen", 14), ("1.5", 14), ("-3", -3),
])
def test_env_int_never_raises(monkeypatch, raw, expected):
    import agents.ide.store as store

    if raw is None:
        monkeypatch.delenv("IDE_X_TEST", raising=False)
    else:
        monkeypatch.setenv("IDE_X_TEST", raw)
    assert store.env_int("IDE_X_TEST", 14) == expected


def test_env_int_minimum(monkeypatch):
    import agents.ide.store as store

    monkeypatch.setenv("IDE_X_TEST", "1")
    assert store.env_int("IDE_X_TEST", 60, minimum=5) == 5
    monkeypatch.setenv("IDE_X_TEST", "90")
    assert store.env_int("IDE_X_TEST", 60, minimum=5) == 90
    monkeypatch.setenv("IDE_X_TEST", "soon")
    assert store.env_int("IDE_X_TEST", 60, minimum=5) == 60


def test_env_int_warns_about_an_unusable_value(monkeypatch, caplog):
    import agents.ide.store as store

    monkeypatch.setenv("IDE_X_TEST", "p4ssw0rd-like")
    with caplog.at_level("WARNING", logger="agents.ide.store"):
        store.env_int("IDE_X_TEST", 14)
    [record] = caplog.records
    assert "IDE_X_TEST" in record.getMessage()
    assert "p4ssw0rd-like" not in record.getMessage()  # the value is not echoed


def test_bad_environment_does_not_break_the_import():
    """The retention and approval settings are read when the module is
    imported: a typo in one of them must not keep the app from starting."""
    import subprocess

    code = (
        "import agents.ide.store as s, agents.ide.approvals as a;"
        "print(s.IDE_SESSION_RETENTION_DAYS, s.IDE_DIAGNOSE_RETENTION_DAYS,"
        " s.IDE_AUDIT_RETENTION_DAYS, s.APPROVAL_TTL_MIN, a.ARM_TIMEOUT_S)"
    )
    env = {
        **os.environ,
        "IDE_SESSION_RETENTION_DAYS": "ninety",
        "IDE_DIAGNOSE_RETENTION_DAYS": "14 days",
        "IDE_AUDIT_RETENTION_DAYS": "",
        "IDE_APPROVAL_TTL_MIN": "x",
        "IDE_TRACE_ARM_TIMEOUT_S": "1",
    }
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    # Defaults for what could not be read; the arm timeout never below 5 s.
    assert out.stdout.split() == ["90", "14", "365", "15", "5.0"]
