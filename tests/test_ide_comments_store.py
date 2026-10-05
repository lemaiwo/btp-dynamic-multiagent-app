"""Review comments, pins and the worklist marker in ``agents.ide.store``."""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy import event, update  # noqa: E402

from agents.db import SessionLocal, engine, init_db  # noqa: E402
from agents.ide import store  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeApproval,
    IdeArtifact,
    IdeComment,
    IdeFileRevision,
    IdeSession,
    IdeWorkspaceFile,
    utcnow,
)
from agents.ide.store import CommentError  # noqa: E402

PATH = "src/CLAS/zcl_x.clas.abap"
NOTE = "notes/n.md"
TEXT = "\n".join(f"line {i}" for i in range(1, 11))  # 10 lines


@pytest.fixture(autouse=True)
async def _clean_db():
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeComment, IdeFileRevision, IdeApproval, IdeArtifact,
                      IdeWorkspaceFile, IdeSession):
            await db.execute(model.__table__.delete())
        await db.commit()
    yield


async def _session(*, session_type: str = "change", stage: str | None = None,
                   revision: int = 2) -> str:
    async with SessionLocal() as db:
        s = await store.create_session(db, owner="DEVUSER01", title="t",
                                       target="DEMO", session_type=session_type)
        if stage:
            s.stage = stage
        db.add(IdeWorkspaceFile(
            session_id=s.id, path=PATH, object_type="CLAS", object_name="ZCL_X",
            origin_source="o", proposed_source=TEXT, state="modified",
            revision=revision,
        ))
        for n in range(1, revision + 1):
            db.add(IdeFileRevision(session_id=s.id, path=PATH, revision=n,
                                   proposed_source=TEXT))
        db.add(IdeArtifact(session_id=s.id, stage="design", kind="design",
                           content="d", version=1))
        await db.commit()
        return s.id


def _file(**kw):
    base = {"anchor": "file", "body": "fix this", "path": PATH, "revision": 1,
            "line_start": 3, "line_end": 4}
    base.update(kw)
    return base


def _doc(**kw):
    base = {"anchor": "document", "body": "why?", "kind": "design", "version": 1,
            "paragraph": 0}
    base.update(kw)
    return base


async def _add(sid: str, spec: dict) -> IdeComment:
    async with SessionLocal() as db:
        return await store.add_comment(db, sid, **spec)


async def _state(sid: str, cid: str, state: str) -> None:
    async with SessionLocal() as db:
        # A ``sent`` comment belongs to a run (``resolve_comments`` scope).
        await db.execute(update(IdeComment).where(IdeComment.id == cid)
                         .values(state=state,
                                 sent_run_id="run-1" if state == "sent" else None))
        await db.commit()


# --- add / validation ---------------------------------------------------------


async def test_add_file_and_document_comment():
    sid = await _session()
    c = await _add(sid, _file())
    assert (c.anchor, c.path, c.revision, c.line_start, c.line_end, c.state) == (
        "file", PATH, 1, 3, 4, "open")
    assert c.kind is None and c.paragraph is None
    d = await _add(sid, _doc(paragraph=4))
    assert (d.anchor, d.kind, d.version, d.paragraph, d.path) == (
        "document", "design", 1, 4, None)


@pytest.mark.parametrize("spec", [
    _file(path="src/CLAS/zcl_unknown.clas.abap"),
    _file(revision=3),               # > row.revision (2)
    _file(revision=0),
    _file(revision=True),
    _file(revision="1"),
    _file(line_end=2),               # < line_start
    _file(line_start=0, line_end=1),
    _file(line_start=10, line_end=11),  # past the revision's last line
    _file(line_start=None),
    _file(path=None),
    _file(kind="design"),            # document field on a file anchor
    _doc(kind="plan"),               # no plan artifact
    _doc(version=2),                 # no design v2
    _doc(paragraph=-1),
    _doc(paragraph=None),
    _doc(path=PATH),                 # file field on a document anchor
    _doc(anchor="line"),
])
async def test_add_comment_invalid_anchor(spec):
    sid = await _session()
    with pytest.raises(CommentError) as exc:
        await _add(sid, spec)
    assert exc.value.code == "invalid_anchor"


@pytest.mark.parametrize("body", ["", "   \n", "x" * (store.MAX_COMMENT_CHARS + 1), 7])
async def test_add_comment_invalid_body(body):
    sid = await _session()
    with pytest.raises(CommentError) as exc:
        await _add(sid, _file(body=body))
    assert exc.value.code == "invalid_body"


async def test_body_stored_as_plain_text_without_control_chars():
    sid = await _session()
    c = await _add(sid, _file(body="<b>a</b>\x00\x07\n\tb"))
    assert c.body == "<b>a</b>\n\tb"
    assert len((await _add(sid, _file(body="x" * store.MAX_COMMENT_CHARS))).body) \
        == store.MAX_COMMENT_CHARS


async def test_add_comment_needs_the_revision_row():
    sid = await _session(revision=2)
    async with SessionLocal() as db:
        await db.execute(IdeFileRevision.__table__.delete().where(
            IdeFileRevision.revision == 1))
        await db.commit()
    with pytest.raises(CommentError) as exc:
        await _add(sid, _file(revision=1))
    assert exc.value.code == "invalid_anchor"
    assert (await _add(sid, _file(revision=2, line_start=10, line_end=10))).revision == 2


async def test_add_comment_line_count_is_of_the_anchored_revision():
    sid = await _session(revision=1)
    async with SessionLocal() as db:
        db.add(IdeFileRevision(session_id=sid, path=PATH, revision=2,
                               proposed_source="one\ntwo"))
        await db.execute(update(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid).values(revision=2))
        await db.commit()
    assert (await _add(sid, _file(revision=1, line_start=9, line_end=10))).line_end == 10
    with pytest.raises(CommentError):
        await _add(sid, _file(revision=2, line_start=2, line_end=3))
    assert (await _add(sid, _file(revision=2, line_start=1, line_end=2))).line_end == 2


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", 1),
        ("a", 1),
        ("a\n", 1),
        ("a\nb", 2),
        ("a\r\nb\rc\n", 3),
        ("a\x0cb c\x1cd\x85e", 1),  # not line breaks for the UI
        ("a\n\n", 2),
    ],
)
def test_line_count_matches_the_ui_split(text, lines):
    # ui5-ide/webapp/model/sourceView.ts splitLines: \r\n, \r, \n only, a
    # final empty piece dropped. An anchor valid in the UI must be valid here.
    assert store._line_count(text) == lines


async def test_add_comment_on_foreign_session_file_is_invalid():
    sid = await _session()
    other = await _session()
    with pytest.raises(CommentError):
        async with SessionLocal() as db:
            await store.add_comment(db, "no-such-session", **_file())
    async with SessionLocal() as db:
        await db.execute(IdeArtifact.__table__.delete().where(
            IdeArtifact.session_id == sid))
        await db.commit()
    with pytest.raises(CommentError):
        await _add(sid, _doc())  # the artifact is only in `other`
    assert (await _add(other, _doc())).session_id == other


async def test_add_comment_touches_session():
    sid = await _session()
    async with SessionLocal() as db:
        before = (await db.get(IdeSession, sid)).updated_at
    await _add(sid, _file())
    async with SessionLocal() as db:
        assert (await db.get(IdeSession, sid)).updated_at > before


# --- read ----------------------------------------------------------------------


async def test_list_and_get_are_session_scoped():
    sid = await _session()
    other = await _session()
    a = await _add(sid, _file())
    b = await _add(sid, _doc())
    await _add(other, _file())
    await _state(sid, b.id, "sent")
    async with SessionLocal() as db:
        assert [c.id for c in await store.list_comments(db, sid)] == [a.id, b.id]
        assert [c.id for c in await store.list_comments(db, sid, "sent")] == [b.id]
        assert (await store.get_comment(db, sid, a.id)).id == a.id
        assert await store.get_comment(db, other, a.id) is None
        with pytest.raises(ValueError):
            await store.list_comments(db, sid, "bogus")


# --- transitions ---------------------------------------------------------------


USER_ALLOWED = {("open", "dismissed"), ("addressed", "open"),
                ("addressed", "dismissed"), ("dismissed", "open")}
STATES = ("open", "sent", "addressed", "dismissed")


@pytest.mark.parametrize("src", STATES)
@pytest.mark.parametrize("dst", STATES)
async def test_set_comment_state_matrix(src, dst):
    sid = await _session()
    c = await _add(sid, _file())
    await _state(sid, c.id, src)
    async with SessionLocal() as db:
        if (src, dst) in USER_ALLOWED:
            got = await store.set_comment_state(db, sid, c.id, dst)
            assert got.state == dst
        else:
            with pytest.raises(CommentError) as exc:
                await store.set_comment_state(db, sid, c.id, dst)
            assert exc.value.code == "invalid_transition"
            assert (await store.get_comment(db, sid, c.id)).state == src


async def test_set_comment_state_foreign_or_unknown():
    sid = await _session()
    other = await _session()
    c = await _add(sid, _file())
    async with SessionLocal() as db:
        for s, cid in ((other, c.id), (sid, "nope")):
            with pytest.raises(CommentError) as exc:
                await store.set_comment_state(db, s, cid, "dismissed")
            assert exc.value.code == "comment_not_found"
        assert (await store.get_comment(db, sid, c.id)).state == "open"


@pytest.mark.parametrize("src", STATES)
async def test_edit_and_delete_only_when_open(src):
    sid = await _session()
    c = await _add(sid, _file())
    await _state(sid, c.id, src)
    async with SessionLocal() as db:
        if src == "open":
            assert (await store.edit_comment(db, sid, c.id, "new text")).body == "new text"
            await store.delete_comment(db, sid, c.id)
            assert await store.get_comment(db, sid, c.id) is None
        else:
            with pytest.raises(CommentError) as exc:
                await store.edit_comment(db, sid, c.id, "new text")
            assert exc.value.code == "comment_not_editable"
            with pytest.raises(CommentError) as exc:
                await store.delete_comment(db, sid, c.id)
            assert exc.value.code == "comment_not_editable"
            assert (await store.get_comment(db, sid, c.id)).body == "fix this"


async def test_edit_validates_body_and_scope():
    sid = await _session()
    other = await _session()
    c = await _add(sid, _file())
    async with SessionLocal() as db:
        with pytest.raises(CommentError) as exc:
            await store.edit_comment(db, sid, c.id, "")
        assert exc.value.code == "invalid_body"
        with pytest.raises(CommentError) as exc:
            await store.edit_comment(db, other, c.id, "x")
        assert exc.value.code == "comment_not_found"
        with pytest.raises(CommentError) as exc:
            await store.delete_comment(db, other, c.id)
        assert exc.value.code == "comment_not_found"
    async with SessionLocal() as db:
        assert (await store.get_comment(db, sid, c.id)).body == "fix this"


async def test_mark_comments_sent_only_touches_open_and_needs_caller_commit():
    sid = await _session()
    other = await _session()
    opened = await _add(sid, _file())
    dismissed = await _add(sid, _doc())
    await _state(sid, dismissed.id, "dismissed")
    foreign = await _add(other, _file())
    async with SessionLocal() as db:
        sent = await store.mark_comments_sent(db, sid, "run-1")
        assert [c.id for c in sent] == [opened.id]
        assert sent[0].state == "sent" and sent[0].sent_run_id == "run-1"
        await db.rollback()  # the caller decides
    async with SessionLocal() as db:
        assert (await store.get_comment(db, sid, opened.id)).state == "open"
        await store.mark_comments_sent(db, sid, "run-1")
        await db.commit()
    async with SessionLocal() as db:
        assert (await store.get_comment(db, sid, opened.id)).state == "sent"
        assert (await store.get_comment(db, sid, dismissed.id)).state == "dismissed"
        assert (await store.get_comment(db, other, foreign.id)).state == "open"
        assert await store.mark_comments_sent(db, sid, "run-2") == []


async def test_resolve_comments_only_sent_of_this_session():
    sid = await _session()
    other = await _session()
    sent = await _add(sid, _file())
    opened = await _add(sid, _file())
    dismissed = await _add(sid, _file())
    foreign = await _add(other, _file())
    for cid, st in ((sent.id, "sent"), (dismissed.id, "dismissed")):
        await _state(sid, cid, st)
    await _state(other, foreign.id, "sent")
    async with SessionLocal() as db:
        resolved, rejected = await store.resolve_comments(db, sid, [
            (sent.id, "done " + "y" * 600),
            (opened.id, "a"),
            (dismissed.id, "b"),
            (foreign.id, "c"),
            ("unknown", "d"),
            (sent.id, "again"),
        ], run_id="run-1")
    assert resolved == [sent.id]
    assert rejected == [opened.id, dismissed.id, foreign.id, "unknown", sent.id]
    async with SessionLocal() as db:
        got = await store.get_comment(db, sid, sent.id)
        assert got.state == "addressed"
        assert len(got.answer) == store.MAX_ANSWER_CHARS
        assert (await store.get_comment(db, other, foreign.id)).state == "sent"
        assert (await store.get_comment(db, sid, opened.id)).state == "open"


async def test_resolve_comments_refuses_non_strings():
    sid = await _session()
    c = await _add(sid, _file())
    await _state(sid, c.id, "sent")
    async with SessionLocal() as db:
        resolved, rejected = await store.resolve_comments(db, sid, [(1, "x"), (c.id, 5)],
                                                         run_id="run-1")
    assert resolved == []
    assert rejected == ["1", c.id]


async def test_count_open_comments():
    a = await _session()
    b = await _session()
    c = await _session()
    x1 = await _add(a, _file())
    await _add(a, _file())
    x3 = await _add(a, _file())
    x4 = await _add(a, _file())
    await _state(a, x1.id, "sent")
    await _state(a, x3.id, "addressed")
    await _state(a, x4.id, "dismissed")
    y = await _add(b, _file())
    await _state(b, y.id, "sent")
    async with SessionLocal() as db:
        got = await store.count_open_comments(db, [a, b, c])
        assert await store.count_open_comments(db, []) == {}
    assert got == {a: (1, 2), b: (0, 1), c: (0, 0)}


# --- pins ----------------------------------------------------------------------


class _S:
    def __init__(self, pins_json):
        self.pins_json = pins_json


@pytest.mark.parametrize("raw", [None, "", "not json", "[1]", "42", '"x"'])
def test_pins_of_garbage_is_empty(raw):
    assert store.pins_of(_S(raw)) == {}
    assert store.pins_of(None) == {}


def test_pins_of_drops_bad_entries():
    raw = ('{"design": 2, "plan": true, "review": "1", "bogus": 3, "report": 0,'
           ' "files": {"a": 4, "b": "x", "c": false}}')
    assert store.pins_of(_S(raw)) == {"design": 2, "files": {"a": 4}}


async def test_set_pin_round_trip():
    sid = await _session()
    async with SessionLocal() as db:
        s = await db.get(IdeSession, sid)
        before = s.updated_at
        await store.set_pin(db, s, "design", 1)
        await store.set_file_pins(db, s, {PATH: 2})
        await store.set_pin(db, s, "plan", 3)
        await store.set_file_pins(db, s, {"notes/n.md": 1})
        with pytest.raises(ValueError):
            await store.set_pin(db, s, "files", 1)
        with pytest.raises(ValueError):
            await store.set_pin(db, s, "design", 0)
        with pytest.raises(ValueError):
            await store.set_file_pins(db, s, {PATH: True})
    async with SessionLocal() as db:
        s = await db.get(IdeSession, sid)
        assert store.pins_of(s) == {"design": 1, "plan": 3,
                                    "files": {PATH: 2, "notes/n.md": 1}}
        assert s.updated_at > before


async def test_set_pin_does_not_lose_a_concurrent_pin():
    sid = await _session()
    async with SessionLocal() as db1, SessionLocal() as db2:
        s1 = await db1.get(IdeSession, sid)
        s2 = await db2.get(IdeSession, sid)  # both see pins_json = NULL
        await store.set_pin(db1, s1, "design", 1)
        await store.set_pin(db2, s2, "plan", 1)
    async with SessionLocal() as db:
        assert store.pins_of(await db.get(IdeSession, sid)) == {"design": 1, "plan": 1}


# --- waiting_for ---------------------------------------------------------------


async def _sessions(*sids: str) -> list[IdeSession]:
    async with SessionLocal() as db:
        return [await db.get(IdeSession, s) for s in sids]


async def _waiting(*sids: str) -> dict[str, str | None]:
    sessions = await _sessions(*sids)
    async with SessionLocal() as db:
        return await store.waiting_for(db, sessions)


async def test_waiting_document_legacy_without_pins():
    sid = await _session(stage="design")
    assert await _waiting(sid) == {sid: "document"}
    async with SessionLocal() as db:
        await store.set_pin(db, await db.get(IdeSession, sid), "design", 1)
    assert await _waiting(sid) == {sid: None}
    async with SessionLocal() as db:
        await store.add_artifact(db, sid, stage="design", kind="design", content="d2")
    assert await _waiting(sid) == {sid: "document"}


async def test_waiting_document_needs_idle_and_an_artifact():
    sid = await _session(stage="plan")  # no plan artifact
    assert await _waiting(sid) == {sid: None}
    run = await _session(stage="design")
    async with SessionLocal() as db:
        await db.execute(update(IdeSession).where(IdeSession.id == run)
                         .values(status="running"))
        await db.commit()
    assert await _waiting(run) == {run: None}


async def test_waiting_changes():
    sid = await _session(stage="propose", revision=2)
    assert await _waiting(sid) == {sid: "changes"}
    async with SessionLocal() as db:
        await store.set_file_pins(db, await db.get(IdeSession, sid), {PATH: 2})
    assert await _waiting(sid) == {sid: None}
    async with SessionLocal() as db:
        await db.execute(update(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid).values(revision=3))
        await db.commit()
    assert await _waiting(sid) == {sid: "changes"}
    # A file without a proposal is nothing to review.
    async with SessionLocal() as db:
        await db.execute(update(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid).values(proposed_source=None,
                                                       state="read"))
        await db.commit()
    assert await _waiting(sid) == {sid: None}


async def test_waiting_changes_ignores_scratch_notes():
    sid = await _session(stage="propose", revision=1)
    async with SessionLocal() as db:
        await store.set_file_pins(db, await db.get(IdeSession, sid), {PATH: 1})
        db.add(IdeWorkspaceFile(session_id=sid, path=NOTE, proposed_source="n",
                                state="new", revision=1))
        await db.commit()
    assert await _waiting(sid) == {sid: None}


async def test_set_pin_gives_up_with_pin_conflict(monkeypatch):
    """Every compare-and-set UPDATE loses (another approve wins each time)."""
    sid = await _session()
    async with SessionLocal() as db:
        s = await db.get(IdeSession, sid)
        real_execute = type(db).execute

        class _Lost:
            rowcount = 0

        async def execute(stmt, *a, **kw):
            if getattr(stmt, "is_update", False) and stmt.table.name == "ide_sessions":
                return _Lost()
            return await real_execute(db, stmt, *a, **kw)

        monkeypatch.setattr(db, "execute", execute)
        with pytest.raises(store.PinConflict):
            await store.set_pin(db, s, "design", 1)


async def test_waiting_comments_beats_changes_and_document():
    sid = await _session(stage="propose")
    c = await _add(sid, _file())
    await _state(sid, c.id, "addressed")
    assert await _waiting(sid) == {sid: "comments"}


async def test_waiting_never_for_a_done_session():
    """Review minor: ``done`` is the end -- nothing there waits for the
    developer, not even a comment the agent addressed."""
    sid = await _session(stage="done")
    c = await _add(sid, _file())
    await _state(sid, c.id, "addressed")
    assert await _waiting(sid) == {sid: None}


async def test_waiting_approval_beats_comments():
    sid = await _session(session_type="diagnose")
    c = await _add(sid, _file())
    await _state(sid, c.id, "addressed")
    async with SessionLocal() as db:
        await store.add_approval(db, sid, run_id=None, tool_call_id=None,
                                 action="trace_start", params={})
    assert await _waiting(sid) == {sid: "approval"}
    # An overdue approval no longer counts.
    async with SessionLocal() as db:
        await db.execute(update(IdeApproval).values(
            created_at=utcnow() - timedelta(minutes=store.approval_ttl_min() + 1)))
        await db.commit()
    assert await _waiting(sid) == {sid: "comments"}


async def test_waiting_approval_only_for_diagnose():
    sid = await _session(stage="chat")
    async with SessionLocal() as db:
        await store.add_approval(db, sid, run_id=None, tool_call_id=None,
                                 action="trace_start", params={})
    assert await _waiting(sid) == {sid: None}


async def test_waiting_for_uses_a_fixed_number_of_queries():
    sids = [await _session(stage=st) for st in ("design", "propose", "plan", "chat")]
    sids.append(await _session(session_type="diagnose"))
    sessions = await _sessions(*sids)
    statements: list[str] = []

    def _count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        async with SessionLocal() as db:
            got = await store.waiting_for(db, sessions)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)
    assert got == {sids[0]: "document", sids[1]: "changes", sids[2]: None,
                   sids[3]: None, sids[4]: None}
    assert len(statements) <= 4
    async with SessionLocal() as db:
        assert await store.waiting_for(db, []) == {}

