"""File revisions written by ``workspace.save_state`` and read by the store."""

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

import json  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide import store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeFileRevision,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.workspace import bound_workspace, load_state, save_state  # noqa: E402

EDIT = "src/CLAS/zcl_edit.clas.abap"
NEW = "src/CLAS/zcl_new.clas.abap"
NOTE = "notes/impact.md"


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeFileRevision, IdeWorkspaceFile, IdeSession):
            await db.execute(model.__table__.delete())
        await db.commit()
    yield


async def _session() -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner="DEVUSER01", title="t", target="DEMO")
        db.add(IdeWorkspaceFile(
            session_id=s.id, path=EDIT, object_type="CLAS", object_name="ZCL_EDIT",
            origin_source="origin", state="read", base_status="sap",
        ))
        await db.commit()
        return s.id


async def _save(sid: str, files: dict[str, str], run_id: str | None = None) -> list[dict]:
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    for path, content in files.items():
        st.put(path, content)
    async with SessionLocal() as db:
        return await save_state(db, sid, st, run_id=run_id)


async def _revs(sid: str, path: str) -> list[IdeFileRevision]:
    async with SessionLocal() as db:
        return await store.list_revisions(db, sid, path)


async def _row(sid: str, path: str) -> IdeWorkspaceFile:
    async with SessionLocal() as db:
        return (await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid, IdeWorkspaceFile.path == path
        ))).scalar_one()


async def test_first_save_creates_revision_1():
    sid = await _session()
    await _save(sid, {EDIT: "edited", NEW: "new class", NOTE: "# note"}, run_id="r1")
    for path, text in ((EDIT, "edited"), (NEW, "new class"), (NOTE, "# note")):
        revs = await _revs(sid, path)
        assert [(r.revision, r.proposed_source, r.run_id) for r in revs] == [
            (1, text, "r1")]
        assert revs[0].syntax_status is None
        assert (await _row(sid, path)).revision == 1


async def test_unchanged_content_creates_no_revision():
    sid = await _session()
    await _save(sid, {EDIT: "edited"})
    assert await _save(sid, {}) == []
    assert await _save(sid, {EDIT: "edited"}) == []
    assert len(await _revs(sid, EDIT)) == 1
    # A file that was only read never gets a revision.
    await _save(sid, {EDIT: "origin"})
    async with SessionLocal() as db:
        n = (await db.execute(select(IdeFileRevision).where(
            IdeFileRevision.session_id == sid))).scalars().all()
    assert len(n) == 1


async def test_second_change_creates_revision_2_with_run_id():
    sid = await _session()
    await _save(sid, {EDIT: "v1"}, run_id="run-a")
    await _save(sid, {EDIT: "v2"}, run_id="run-b")
    revs = await _revs(sid, EDIT)
    assert [(r.revision, r.proposed_source, r.run_id) for r in revs] == [
        (2, "v2", "run-b"), (1, "v1", "run-a")]
    assert (await _row(sid, EDIT)).revision == 2
    async with SessionLocal() as db:
        got = await store.get_revision(db, sid, EDIT, 1)
        assert got.proposed_source == "v1"
        assert await store.get_revision(db, sid, EDIT, 3) is None
        assert await store.get_revision(db, "other", EDIT, 1) is None
        assert await store.list_revisions(db, "other", EDIT) == []


async def test_revert_to_origin_keeps_revisions_and_state_read():
    sid = await _session()
    await _save(sid, {EDIT: "v1"})
    changed = await _save(sid, {EDIT: "origin"})
    assert changed == [{"path": EDIT, "state": "read", "revision": 1,
                        "base_status": "sap"}]
    row = await _row(sid, EDIT)
    assert (row.state, row.proposed_source, row.revision) == ("read", None, 1)
    assert len(await _revs(sid, EDIT)) == 1
    # Re-proposing the text of the latest revision does not repeat it.
    await _save(sid, {EDIT: "v1"})
    assert len(await _revs(sid, EDIT)) == 1
    assert (await _row(sid, EDIT)).state == "modified"


async def test_changed_entries_carry_revision():
    sid = await _session()
    changed = await _save(sid, {EDIT: "v1", NEW: "n1"})
    assert sorted(changed, key=lambda c: c["path"]) == [
        {"path": EDIT, "state": "modified", "revision": 1, "base_status": "sap"},
        {"path": NEW, "state": "new", "revision": 1, "base_status": None},
    ]
    changed = await _save(sid, {NEW: "n2"})
    assert changed == [{"path": NEW, "state": "new", "revision": 2,
                        "base_status": None}]


async def test_save_touches_session_only_on_change():
    sid = await _session()
    async with SessionLocal() as db:
        before = (await db.get(IdeSession, sid)).updated_at
    await _save(sid, {EDIT: "v1"})
    async with SessionLocal() as db:
        after = (await db.get(IdeSession, sid)).updated_at
    assert after > before


async def test_bound_workspace_passes_run_id():
    sid = await _session()
    async with bound_workspace(sid, allow_subagents=False, request_limit=None,
                               run_id="run-x") as scope:
        scope.state.put(EDIT, "bound edit")
    assert scope.changed_files == [{"path": EDIT, "state": "modified",
                                    "revision": 1, "base_status": "sap"}]
    assert (await _revs(sid, EDIT))[0].run_id == "run-x"


async def test_set_syntax_result_validates_and_cuts():
    sid = await _session()
    await _save(sid, {EDIT: "v1"})
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await store.set_syntax_result(db, sid, EDIT, 1, "fine", [])
        with pytest.raises(LookupError):
            await store.set_syntax_result(db, sid, EDIT, 9, "ok", [])
        with pytest.raises(LookupError):
            await store.set_syntax_result(db, "other", EDIT, 1, "ok", [])
        items = [{"line": i, "message": "x" * 400, "severity": "error"}
                 for i in range(60)]
        items.append("garbage")
        rev = await store.set_syntax_result(db, sid, EDIT, 1, "errors", items)
    stored = json.loads(rev.syntax_json)
    assert rev.syntax_status == "errors" and rev.syntax_checked_at is not None
    assert len(stored) == 50
    assert stored[0] == {"line": 0, "message": "x" * 300, "severity": "error"}


async def test_set_syntax_result_normalises_items():
    sid = await _session()
    await _save(sid, {EDIT: "v1"})
    async with SessionLocal() as db:
        rev = await store.set_syntax_result(db, sid, EDIT, 1, "ok", [
            {"line": True, "message": "w", "severity": "warning"},
            {"line": "3", "message": "m", "severity": "fatal"},
            {"line": 4},
        ])
    assert json.loads(rev.syntax_json) == [
        {"line": None, "message": "w", "severity": "warning"},
        {"line": None, "message": "m", "severity": "error"},
    ]


async def test_revision_conflict_is_a_clear_error_without_source(caplog):
    from agents.ide.workspace import RevisionConflict

    sid = await _session()
    async with SessionLocal() as db:
        # Another writer already stored revision 1 of a path this save adds.
        db.add(IdeFileRevision(session_id=sid, path=NEW, revision=1,
                               proposed_source="theirs"))
        await db.commit()
    with pytest.raises(RevisionConflict) as exc:
        await _save(sid, {NEW: "SECRET-SOURCE"})
    assert sid in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert "SECRET-SOURCE" not in caplog.text and sid in caplog.text
    async with SessionLocal() as db:
        got = await store.get_revision(db, sid, NEW, 1)
    assert got.proposed_source == "theirs"
