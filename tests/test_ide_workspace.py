"""IDE session workspace: load the DB rows into a DeepState and save back."""

from __future__ import annotations

import json
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
from sqlalchemy import select  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import (  # noqa: E402
    MAX_FILE_BYTES,
    DeepState,
    TodoItem,
    WorkspaceScope,
    current_workspace,
)
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.store import create_session  # noqa: E402
from agents.ide.workspace import bound_workspace, load_state, save_state  # noqa: E402

READ_PATH = "src/CLAS/zcl_read.clas.abap"
EDIT_PATH = "src/CLAS/zcl_edit.clas.abap"
NEW_PATH = "src/CLAS/zcl_new.clas.abap"


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession,
                      IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
    yield


async def _session_with_files() -> str:
    async with SessionLocal() as db:
        s = await create_session(db, owner="dev@example.com", title="t", target="dev")
        for path, name in ((READ_PATH, "ZCL_READ"), (EDIT_PATH, "ZCL_EDIT")):
            db.add(IdeWorkspaceFile(
                session_id=s.id, path=path, object_type="CLAS", object_name=name,
                origin_source=f"origin {name}", state="read",
            ))
        await db.commit()
        return s.id


async def _files(sid: str) -> dict[str, IdeWorkspaceFile]:
    async with SessionLocal() as db:
        rows = await db.execute(
            select(IdeWorkspaceFile).where(IdeWorkspaceFile.session_id == sid)
        )
        return {r.path: r for r in rows.scalars().all()}


async def test_load_state_uses_proposed_then_origin_and_todos():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        row = (await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.path == EDIT_PATH))).scalar_one()
        row.proposed_source = "proposed"
        row.state = "modified"
        s = await db.get(IdeSession, sid)
        s.todos_json = json.dumps([{"content": "a", "status": "completed"}])
        await db.commit()

    async with SessionLocal() as db:
        st = await load_state(db, sid)
    assert isinstance(st, DeepState)
    assert st.run_id == f"ide:{sid}"
    assert st.files == {READ_PATH: "origin ZCL_READ", EDIT_PATH: "proposed"}
    assert st.sizes[EDIT_PATH] == len(b"proposed")  # via put(): caps tracked
    assert st.todos == [TodoItem(content="a", status="completed")]
    assert st.semaphores == {}


async def test_load_state_tolerates_bad_todos_json():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        s = await db.get(IdeSession, sid)
        s.todos_json = "{not json"
        await db.commit()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    assert st.todos == []


async def test_load_state_skips_file_over_cap():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        db.add(IdeWorkspaceFile(
            session_id=sid, path="notes/huge.md", origin_source=None,
            proposed_source="x" * (MAX_FILE_BYTES + 1), state="new",
        ))
        await db.commit()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    assert "notes/huge.md" not in st.files
    assert READ_PATH in st.files


async def test_round_trip_modified_new_read():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    st.put(EDIT_PATH, "edited by agent")
    st.put(NEW_PATH, "CLASS zcl_new DEFINITION. ENDCLASS.")
    st.put("notes/impact.md", "# impact")

    async with SessionLocal() as db:
        changed = await save_state(db, sid, st)

    assert sorted(changed, key=lambda c: c["path"]) == [
        {"path": "notes/impact.md", "state": "new", "revision": 1,
         "base_status": None},
        {"path": EDIT_PATH, "state": "modified", "revision": 1,
         "base_status": None},
        {"path": NEW_PATH, "state": "new", "revision": 1, "base_status": None},
    ]
    files = await _files(sid)
    assert files[READ_PATH].state == "read"
    assert files[READ_PATH].proposed_source is None
    assert files[EDIT_PATH].state == "modified"
    assert files[EDIT_PATH].proposed_source == "edited by agent"
    assert files[EDIT_PATH].origin_source == "origin ZCL_EDIT"
    new = files[NEW_PATH]
    assert (new.state, new.object_type, new.object_name) == ("new", "CLAS", "ZCL_NEW")
    assert new.origin_source is None
    assert new.proposed_source == "CLASS zcl_new DEFINITION. ENDCLASS."
    scratch = files["notes/impact.md"]
    assert (scratch.state, scratch.object_type, scratch.object_name) == (
        "new", None, None)

    # Saving the same state again changes nothing.
    async with SessionLocal() as db:
        assert await save_state(db, sid, st) == []


async def test_revert_to_origin_returns_to_read():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    st.put(EDIT_PATH, "edited")
    async with SessionLocal() as db:
        await save_state(db, sid, st)
    st.put(EDIT_PATH, "origin ZCL_EDIT")
    async with SessionLocal() as db:
        changed = await save_state(db, sid, st)
    assert changed == [{"path": EDIT_PATH, "state": "read", "revision": 1,
                        "base_status": None}]
    row = (await _files(sid))[EDIT_PATH]
    assert row.state == "read" and row.proposed_source is None


async def test_deleted_paths_are_kept():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    st.files.pop(READ_PATH)
    st.sizes.pop(READ_PATH)
    async with SessionLocal() as db:
        assert await save_state(db, sid, st) == []
    assert READ_PATH in await _files(sid)


async def test_todos_persisted():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    st.todos = [TodoItem(content="read", status="completed"),
                TodoItem(content="propose", status="in_progress")]
    async with SessionLocal() as db:
        await save_state(db, sid, st)
    async with SessionLocal() as db:
        s = await db.get(IdeSession, sid)
        assert json.loads(s.todos_json) == [
            {"content": "read", "status": "completed"},
            {"content": "propose", "status": "in_progress"},
        ]
        assert (await load_state(db, sid)).todos == st.todos


async def test_bound_workspace_binds_and_resets():
    sid = await _session_with_files()
    assert current_workspace.get() is None
    async with bound_workspace(sid, allow_subagents=False, request_limit=7) as scope:
        assert isinstance(scope, WorkspaceScope)
        assert current_workspace.get() is scope
        assert scope.session_id == sid
        assert scope.allow_subagents is False
        assert scope.request_limit == 7
        assert scope.state.run_id == f"ide:{sid}"
        assert READ_PATH in scope.state.files
    assert current_workspace.get() is None


async def test_bound_workspace_saves_on_exception():
    sid = await _session_with_files()
    with pytest.raises(RuntimeError, match="boom"):
        async with bound_workspace(sid, allow_subagents=True, request_limit=None) as scope:
            scope.state.put(NEW_PATH, "partial work")
            scope.state.todos = [TodoItem(content="half done")]
            raise RuntimeError("boom")
    assert current_workspace.get() is None
    files = await _files(sid)
    assert files[NEW_PATH].state == "new"
    assert files[NEW_PATH].proposed_source == "partial work"
    async with SessionLocal() as db:
        s = await db.get(IdeSession, sid)
        assert json.loads(s.todos_json) == [{"content": "half done", "status": "pending"}]


async def test_bound_workspace_persists_across_runs():
    sid = await _session_with_files()
    async with bound_workspace(sid, allow_subagents=True, request_limit=None) as one:
        one.state.put(NEW_PATH, "from run one")
        one.state.put(EDIT_PATH, "edited in run one")
        one.state.todos = [TodoItem(content="carry over")]
        first_state = one.state
    async with bound_workspace(sid, allow_subagents=True, request_limit=None) as two:
        assert two.state is not first_state  # fresh state, fresh semaphores
        assert two.state.files[NEW_PATH] == "from run one"
        assert two.state.files[EDIT_PATH] == "edited in run one"
        assert two.state.todos == [TodoItem(content="carry over")]


async def test_bound_workspace_saves_on_cancel():
    import asyncio

    sid = await _session_with_files()
    started = asyncio.Event()

    async def run():
        async with bound_workspace(sid, allow_subagents=True, request_limit=None) as s:
            s.state.put(NEW_PATH, "before cancel")
            started.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await _files(sid))[NEW_PATH].proposed_source == "before cancel"


async def _backdate(sid: str, days: int) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    async with SessionLocal() as db:
        await db.execute(
            update(IdeSession)
            .where(IdeSession.id == sid)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(days=days))
        )
        await db.commit()


async def test_workspace_save_counts_as_session_activity():
    from agents.ide.store import purge_sessions_older_than

    sid = await _session_with_files()
    await _backdate(sid, 40)
    async with bound_workspace(sid, allow_subagents=True, request_limit=None) as s:
        s.state.put(EDIT_PATH, "edited while active")
    async with SessionLocal() as db:
        assert await purge_sessions_older_than(db, 30) == 0
        assert await db.get(IdeSession, sid) is not None


async def test_unchanged_save_does_not_touch_session():
    from agents.ide.store import purge_sessions_older_than

    sid = await _session_with_files()
    await _backdate(sid, 40)
    async with bound_workspace(sid, allow_subagents=True, request_limit=None):
        pass
    async with SessionLocal() as db:
        assert await purge_sessions_older_than(db, 30) == 1


async def test_exit_from_other_context_still_saves():
    import asyncio

    sid = await _session_with_files()
    cm = bound_workspace(sid, allow_subagents=True, request_limit=None)

    async def enter():
        scope = await cm.__aenter__()
        scope.state.put(NEW_PATH, "saved from another context")

    # Enter in one task, exit in another: the token belongs to a different
    # Context, so reset() raises ValueError -- that must not skip the save.
    await asyncio.create_task(enter())
    await asyncio.create_task(cm.__aexit__(None, None, None))
    assert (await _files(sid))[NEW_PATH].proposed_source == "saved from another context"


async def test_save_after_session_deleted_is_noop():
    from agents.ide.store import delete_owned_session

    sid = await _session_with_files()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    st.put(NEW_PATH, "orphan")
    async with SessionLocal() as db:
        assert await delete_owned_session(db, sid, "dev@example.com")
    async with SessionLocal() as db:
        assert await save_state(db, sid, st) == []
    assert await _files(sid) == {}


async def test_save_stores_cleaned_path():
    sid = await _session_with_files()
    async with SessionLocal() as db:
        st = await load_state(db, sid)
    st.files[f"  {NEW_PATH} "] = "padded"
    del st.files[EDIT_PATH]
    st.files[f" {EDIT_PATH}"] = "edited via padded path"
    async with SessionLocal() as db:
        changed = await save_state(db, sid, st)
    assert {c["path"] for c in changed} == {NEW_PATH, EDIT_PATH}
    files = await _files(sid)
    assert set(files) == {READ_PATH, EDIT_PATH, NEW_PATH}
    assert (files[NEW_PATH].object_type, files[NEW_PATH].object_name) == ("CLAS", "ZCL_NEW")
    assert files[EDIT_PATH].state == "modified"
    assert files[EDIT_PATH].proposed_source == "edited via padded path"


async def test_second_cancel_waits_for_save(monkeypatch, caplog):
    import asyncio

    import agents.ide.workspace as ws

    sid = await _session_with_files()
    real_save = ws._save
    save_started = asyncio.Event()
    finished = []

    async def slow_save(s, state, run_id=None):
        save_started.set()
        await asyncio.sleep(0.2)
        result = await real_save(s, state, run_id)
        finished.append(True)
        return result

    monkeypatch.setattr(ws, "_save", slow_save)
    entered = asyncio.Event()

    async def run():
        async with bound_workspace(sid, allow_subagents=True, request_limit=None) as s:
            s.state.put(NEW_PATH, "double cancel")
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await save_started.wait()
    task.cancel()  # second cancel while the save runs
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]  # the task ended only after the save completed
    assert "save failed" not in caplog.text
    assert (await _files(sid))[NEW_PATH].proposed_source == "double cancel"


async def test_hung_save_does_not_make_the_run_uncancellable(monkeypatch, caplog):
    """A save stuck in the database is abandoned IDE_SAVE_TIMEOUT_S after a
    cancel; the cancel is re-raised and the abandonment logged."""
    import asyncio
    import logging

    import agents.ide.workspace as ws

    sid = await _session_with_files()
    monkeypatch.setenv("IDE_SAVE_TIMEOUT_S", "0.2")
    save_started = asyncio.Event()
    save_cancelled = []

    async def hung_save(s, state, run_id=None):
        save_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            save_cancelled.append(True)
            raise

    monkeypatch.setattr(ws, "_save", hung_save)
    entered = asyncio.Event()

    async def run():
        async with bound_workspace(sid, allow_subagents=True, request_limit=None):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(run())
    await entered.wait()
    caplog.set_level(logging.ERROR, logger="agents.ide.workspace")
    task.cancel()
    await save_started.wait()
    task.cancel()  # a second cancel during the hung save is absorbed too
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    await asyncio.sleep(0)
    assert save_cancelled == [True]
    assert "abandoned" in caplog.text


async def test_bound_workspace_sets_session_type():
    sid = await _session_with_files()
    async with bound_workspace(sid, allow_subagents=True, request_limit=None) as s:
        assert s.session_type == "change"
    async with bound_workspace(
        sid, allow_subagents=True, request_limit=None, session_type="diagnose",
        persist=False,
    ) as s:
        assert s.session_type == "diagnose"
        assert current_workspace.get() is s
    assert current_workspace.get() is None


async def test_bound_workspace_persist_false_writes_nothing():
    """Decision D2: a diagnose scratchpad may hold dump text; never stored."""
    sid = await _session_with_files()
    before = {p: (r.state, r.proposed_source) for p, r in (await _files(sid)).items()}
    async with bound_workspace(
        sid, allow_subagents=True, request_limit=None, session_type="diagnose",
        persist=False,
    ) as s:
        s.state.put(NEW_PATH, "dump text from SAPDiagnose")
        s.state.put(EDIT_PATH, "edited")
        s.state.todos = [TodoItem(content="look at the dump")]
    assert s.changed_files == []
    files = await _files(sid)
    assert NEW_PATH not in files
    assert {p: (r.state, r.proposed_source) for p, r in files.items()} == before
    async with SessionLocal() as db:
        assert (await db.get(IdeSession, sid)).todos_json is None


async def test_bound_workspace_persist_false_writes_nothing_on_error():
    sid = await _session_with_files()
    with pytest.raises(RuntimeError, match="boom"):
        async with bound_workspace(
            sid, allow_subagents=True, request_limit=None, session_type="diagnose",
            persist=False,
        ) as s:
            s.state.put(NEW_PATH, "dump text")
            raise RuntimeError("boom")
    assert current_workspace.get() is None
    assert NEW_PATH not in await _files(sid)


async def test_diagnose_never_persists_by_default():
    """The type decides: a caller that forgets ``persist`` still stores nothing."""
    sid = await _session_with_files()
    async with bound_workspace(
        sid, allow_subagents=True, request_limit=None, session_type="diagnose",
    ) as s:
        s.state.put(NEW_PATH, "dump text")
        s.state.todos = [TodoItem(content="x")]
    assert s.changed_files == []
    assert NEW_PATH not in await _files(sid)
    async with SessionLocal() as db:
        assert (await db.get(IdeSession, sid)).todos_json is None


async def test_diagnose_with_persist_true_is_refused():
    sid = await _session_with_files()
    entered = False
    with pytest.raises(ValueError, match="diagnose"):
        async with bound_workspace(
            sid, allow_subagents=True, request_limit=None,
            session_type="diagnose", persist=True,
        ):
            entered = True
    assert not entered
    assert current_workspace.get() is None


async def test_unknown_session_type_does_not_persist_by_default():
    sid = await _session_with_files()
    async with bound_workspace(
        sid, allow_subagents=True, request_limit=None, session_type="nonsense",
    ) as s:
        s.state.put(NEW_PATH, "text")
    assert NEW_PATH not in await _files(sid)


async def test_change_default_still_persists():
    sid = await _session_with_files()
    async with bound_workspace(
        sid, allow_subagents=True, request_limit=None, session_type="change",
    ) as s:
        s.state.put(NEW_PATH, "fresh")
    assert (await _files(sid))[NEW_PATH].proposed_source == "fresh"


async def test_diagnose_writes_nothing_on_cancel():
    import asyncio

    sid = await _session_with_files()
    started = asyncio.Event()

    async def run():
        async with bound_workspace(
            sid, allow_subagents=True, request_limit=None, session_type="diagnose",
        ) as s:
            s.state.put(NEW_PATH, "dump text")
            s.state.todos = [TodoItem(content="x")]
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert NEW_PATH not in await _files(sid)
    async with SessionLocal() as db:
        assert (await db.get(IdeSession, sid)).todos_json is None


async def test_bound_workspace_reports_changed_files():
    sid = await _session_with_files()
    async with bound_workspace(sid, allow_subagents=True, request_limit=None) as s:
        s.state.put(NEW_PATH, "fresh")
        assert s.changed_files == []
    assert s.changed_files == [{"path": NEW_PATH, "state": "new", "revision": 1,
                                "base_status": None}]
