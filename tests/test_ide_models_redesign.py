"""File revisions, comments, pins and base columns (Task B2).

The upgrade test starts from the SQLite DDL that the released 2.18.0 code
(commit 30be3f4) creates, so the migration is checked against the schema a
deployed database really has, not against today's models.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import agents.db as db_module  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IDE_CHILD_MODELS,
    IdeArtifact,
    IdeComment,
    IdeFileRevision,
    IdeSession,
    IdeWorkspaceFile,
)

FIXTURE = ROOT / "tests" / "fixtures" / "ide_schema_2_18_0.sql"


@pytest.fixture
async def old_db(tmp_path, monkeypatch):
    """A SQLite file holding the 2.18.0 ``ide_*`` schema plus legacy rows,
    with ``agents.db`` rebound to it."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text())
    con.execute(
        "INSERT INTO ide_sessions (id, owner, title, target, session_type, stage,"
        " status, requests_used) VALUES ('s1','DEVUSER01','t','DEMO','change',"
        "'plan','idle',0)"
    )
    con.execute(
        "INSERT INTO ide_workspace_files (id, session_id, path, state, proposed_source)"
        " VALUES ('f1','s1','src/CLAS/zcl_x.clas.abap','new','X')"
    )
    con.execute(
        "INSERT INTO ide_workspace_files (id, session_id, path, state, origin_source)"
        " VALUES ('f2','s1','src/CLAS/zcl_y.clas.abap','read','Y')"
    )
    con.execute(
        "INSERT INTO ide_artifacts (id, session_id, stage, kind, content, version)"
        " VALUES ('a1','s1','design','design','D',1)"
    )
    con.commit()
    con.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    try:
        yield path
    finally:
        await engine.dispose()


def _cols(con: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {r[1]: r for r in con.execute(f"PRAGMA table_info({table})")}


async def test_upgrade_from_2_18_0_schema(old_db):
    # Twice: the second start must be a no-op, not an error.
    await db_module.init_db()
    await db_module.init_db()

    con = sqlite3.connect(old_db)
    try:
        tables = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"ide_file_revisions", "ide_comments"} <= tables
        files = _cols(con, "ide_workspace_files")
        assert {"revision", "origin_version", "base_status", "base_checked_at"} <= set(files)
        # revision is NOT NULL with a server default 0 (row 3 notnull, 4 default)
        assert files["revision"][3] == 1 and str(files["revision"][4]).strip("'") == "0"
        assert "pins_json" in _cols(con, "ide_sessions")
        assert "based_on_json" in _cols(con, "ide_artifacts")
        assert {"syntax_status", "syntax_json", "syntax_checked_at"} <= set(
            _cols(con, "ide_file_revisions")
        )
        indexes = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "ix_ide_comments_session_state" in indexes
        # Legacy rows: a proposal is backfilled as revision 1 (once), the base
        # was never checked, the session is untouched.
        assert con.execute(
            "SELECT revision, origin_version, base_status, base_checked_at,"
            " proposed_source, state FROM ide_workspace_files WHERE id='f1'"
        ).fetchone() == (1, None, None, None, "X", "new")
        assert con.execute(
            "SELECT session_id, path, revision, proposed_source, run_id"
            " FROM ide_file_revisions"
        ).fetchall() == [("s1", "src/CLAS/zcl_x.clas.abap", 1, "X", None)]
        # A file without a proposal gets no revision.
        assert con.execute(
            "SELECT revision FROM ide_workspace_files WHERE id='f2'"
        ).fetchone() == (0,)
        assert con.execute(
            "SELECT owner, stage, status, session_type, pins_json FROM ide_sessions"
            " WHERE id='s1'"
        ).fetchone() == ("DEVUSER01", "plan", "idle", "change", None)
        assert con.execute(
            "SELECT version, based_on_json FROM ide_artifacts WHERE id='a1'"
        ).fetchone() == (1, None)
    finally:
        con.close()

    # The ORM reads the upgraded rows.
    async with db_module.SessionLocal() as db:
        f = await db.get(IdeWorkspaceFile, "f1")
        assert f.revision == 1 and f.base_status is None
        s = await db.get(IdeSession, "s1")
        assert s.pins_json is None

    # The backfilled revision is a valid comment anchor.
    from agents.ide import store

    async with db_module.SessionLocal() as db:
        c = await store.add_comment(
            db, "s1", anchor="file", body="why?", path="src/CLAS/zcl_x.clas.abap",
            revision=1, line_start=1, line_end=1,
        )
        assert c.state == "open"


async def test_backfill_is_idempotent(old_db):
    await db_module.init_db()
    con = sqlite3.connect(old_db)
    try:
        first = (
            con.execute("SELECT * FROM ide_file_revisions").fetchall(),
            con.execute("SELECT id, revision FROM ide_workspace_files").fetchall(),
        )
    finally:
        con.close()
    await db_module.init_db()
    con = sqlite3.connect(old_db)
    try:
        assert (
            con.execute("SELECT * FROM ide_file_revisions").fetchall(),
            con.execute("SELECT id, revision FROM ide_workspace_files").fetchall(),
        ) == first
    finally:
        con.close()


async def test_backfill_skips_an_existing_revision_row(old_db):
    await db_module.init_db()  # adds the columns and backfills f1
    con = sqlite3.connect(old_db)
    con.execute("UPDATE ide_workspace_files SET revision = 0 WHERE id='f1'")
    con.execute("UPDATE ide_file_revisions SET proposed_source = 'kept'")
    con.commit()
    con.close()
    await db_module.init_db()
    con = sqlite3.connect(old_db)
    try:
        # The existing row is kept as it is; since its text is not the
        # proposal, the proposal gets a revision of its own after it
        # (``_resync_ide_revisions``).
        assert con.execute(
            "SELECT revision, proposed_source FROM ide_file_revisions"
            " ORDER BY revision"
        ).fetchall() == [(1, "kept"), (2, "X")]
        assert con.execute(
            "SELECT revision FROM ide_workspace_files WHERE id='f1'"
        ).fetchone() == (2,)
    finally:
        con.close()


async def test_quote_column_added_to_an_existing_comments_table(old_db):
    # A database that already has ``ide_comments`` from before ``quote``:
    # create_all leaves the table alone, so the column is added in place.
    con = sqlite3.connect(old_db)
    con.executescript(
        "CREATE TABLE ide_comments (id VARCHAR(36) NOT NULL PRIMARY KEY,"
        " session_id VARCHAR(36) NOT NULL REFERENCES ide_sessions (id)"
        " ON DELETE CASCADE, anchor VARCHAR(8) NOT NULL, path VARCHAR(200),"
        " revision INTEGER, line_start INTEGER, line_end INTEGER,"
        " kind VARCHAR(16), version INTEGER, paragraph INTEGER, body TEXT NOT NULL,"
        " state VARCHAR(10) DEFAULT 'open' NOT NULL, answer TEXT,"
        " sent_run_id VARCHAR(36), created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP),"
        " updated_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP));"
        "INSERT INTO ide_comments (id, session_id, anchor, kind, version, paragraph,"
        " body) VALUES ('c1','s1','document','design',1,2,'why?');"
    )
    con.commit()
    con.close()

    await db_module.init_db()
    await db_module.init_db()

    con = sqlite3.connect(old_db)
    try:
        cols = _cols(con, "ide_comments")
        assert "quote" in cols and cols["quote"][3] == 0  # nullable
        assert con.execute(
            "SELECT body, quote FROM ide_comments WHERE id='c1'"
        ).fetchone() == ("why?", None)
    finally:
        con.close()

    from agents.ide import stages

    async with db_module.SessionLocal() as db:
        c = await db.get(IdeComment, "c1")
        assert c.quote is None
        assert '<comment id="c1" on="block 3 of design v1">\nwhy?\n</comment>' in (
            stages._comments_block([c])
        )


def test_child_models_include_revisions_and_comments():
    assert IdeFileRevision in IDE_CHILD_MODELS
    assert IdeComment in IDE_CHILD_MODELS
    assert IdeFileRevision.__tablename__ == "ide_file_revisions"
    assert IdeComment.__tablename__ == "ide_comments"
    uq = {c.name for c in IdeFileRevision.__table__.constraints}
    assert "uq_ide_file_revision" in uq
    ix = {i.name: [c.name for c in i.columns] for i in IdeComment.__table__.indexes}
    assert ix["ix_ide_comments_session_state"] == ["session_id", "state"]
    for model in (IdeFileRevision, IdeComment):
        fk = next(iter(model.__table__.c.session_id.foreign_keys))
        assert fk.column.table.name == "ide_sessions" and fk.ondelete == "CASCADE"


def test_new_column_defaults():
    t = IdeWorkspaceFile.__table__
    assert t.c.revision.nullable is False
    assert t.c.revision.server_default.arg == "0"
    assert t.c.base_checked_at.type.timezone is True
    assert IdeComment.__table__.c.state.default.arg == "open"
    assert IdeFileRevision.__table__.c.syntax_checked_at.type.timezone is True


async def test_comment_and_revision_rows_cascade_with_session_delete(old_db):
    await db_module.init_db()
    async with db_module.SessionLocal() as db:
        # Revision 1 of zcl_x is the backfill of the legacy proposal.
        db.add(IdeComment(
            session_id="s1", anchor="file", path="src/CLAS/zcl_x.clas.abap",
            revision=1, line_start=1, line_end=1, body="why?",
        ))
        await db.commit()
        c = (await db.execute(select(IdeComment))).scalar_one()
        assert c.state == "open" and c.created_at is not None

        # Same loop as store/routes: SQLite does not enforce ON DELETE CASCADE.
        for model in IDE_CHILD_MODELS:
            await db.execute(delete(model).where(model.session_id == "s1"))
        await db.execute(delete(IdeSession).where(IdeSession.id == "s1"))
        await db.commit()
        for model in (IdeFileRevision, IdeComment, IdeArtifact, IdeWorkspaceFile):
            n = (await db.execute(select(func.count()).select_from(model))).scalar_one()
            assert n == 0, model.__tablename__


async def test_revision_unique_per_session_path(old_db):
    from sqlalchemy.exc import IntegrityError

    await db_module.init_db()
    async with db_module.SessionLocal() as db:
        for _ in range(2):
            db.add(IdeFileRevision(
                session_id="s1", path="p", revision=1, proposed_source="X",
            ))
        with pytest.raises(IntegrityError):
            await db.commit()


# --- roll back to 2.18.0, edit, roll forward (M4) ---------------------------


def _edit_like_2_18_0(path: Path, sql: str, *args) -> None:
    """A write the released 2.18.0 code makes on the upgraded database: it
    knows nothing of ``revision`` or ``ide_file_revisions``."""
    con = sqlite3.connect(path)
    con.execute(sql, args)
    con.commit()
    con.close()


def _revisions(path: Path, file_path: str = "src/CLAS/zcl_x.clas.abap"):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT revision, proposed_source FROM ide_file_revisions"
            " WHERE path = ? ORDER BY revision", (file_path,)
        ).fetchall()
    finally:
        con.close()


def _file_rev(path: Path, fid: str = "f1"):
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT revision, proposed_source FROM ide_workspace_files WHERE id = ?",
            (fid,),
        ).fetchone()
    finally:
        con.close()


async def test_proposal_rewritten_under_2_18_0_gets_a_new_revision(old_db):
    await db_module.init_db()  # f1: revision 1 "X"
    _edit_like_2_18_0(
        old_db, "UPDATE ide_workspace_files SET proposed_source = ? WHERE id = 'f1'",
        "X2",
    )
    await db_module.init_db()
    assert _revisions(old_db) == [(1, "X"), (2, "X2")]
    assert _file_rev(old_db) == (2, "X2")
    # Idempotent: a second start adds nothing.
    await db_module.init_db()
    assert _revisions(old_db) == [(1, "X"), (2, "X2")]
    assert _file_rev(old_db) == (2, "X2")


async def test_proposal_on_a_read_file_under_2_18_0_gets_revision_1(old_db):
    await db_module.init_db()  # f2: read, revision 0
    _edit_like_2_18_0(
        old_db, "UPDATE ide_workspace_files SET proposed_source = ?,"
        " state = 'modified' WHERE id = 'f2'", "Y2",
    )
    await db_module.init_db()
    assert _revisions(old_db, "src/CLAS/zcl_y.clas.abap") == [(1, "Y2")]
    assert _file_rev(old_db, "f2") == (1, "Y2")


async def test_file_recreated_under_2_18_0_continues_after_the_latest(old_db):
    """2.18.0 deletes a file row (its revisions stay) and writes the path
    again: the new row starts at revision 0, below the rows that exist. The
    next revision must come after the latest, or the next save collides."""
    await db_module.init_db()
    _edit_like_2_18_0(old_db, "UPDATE ide_workspace_files SET proposed_source = ?"
                      " WHERE id = 'f1'", "X2")
    await db_module.init_db()  # revisions 1 "X", 2 "X2"
    _edit_like_2_18_0(old_db, "DELETE FROM ide_workspace_files WHERE id = 'f1'")
    _edit_like_2_18_0(
        old_db, "INSERT INTO ide_workspace_files (id, session_id, path, state,"
        " proposed_source) VALUES ('f3','s1','src/CLAS/zcl_x.clas.abap','new',?)",
        "X",  # equal to revision 1's text, not to the latest
    )
    await db_module.init_db()
    assert _revisions(old_db) == [(1, "X"), (2, "X2"), (3, "X")]
    assert _file_rev(old_db, "f3") == (3, "X")


async def test_revision_behind_its_latest_equal_text_is_realigned(old_db):
    await db_module.init_db()
    _edit_like_2_18_0(old_db, "UPDATE ide_workspace_files SET proposed_source = ?"
                      " WHERE id = 'f1'", "X2")
    await db_module.init_db()  # revisions 1 "X", 2 "X2"; file at 2
    _edit_like_2_18_0(old_db, "UPDATE ide_workspace_files SET revision = 1"
                      " WHERE id = 'f1'")
    await db_module.init_db()
    assert _revisions(old_db) == [(1, "X"), (2, "X2")]
    assert _file_rev(old_db) == (2, "X2")


async def test_consistent_files_are_left_alone(old_db):
    await db_module.init_db()
    con = sqlite3.connect(old_db)
    before = con.execute("SELECT * FROM ide_file_revisions").fetchall()
    files = con.execute("SELECT * FROM ide_workspace_files").fetchall()
    con.close()
    await db_module.init_db()
    con = sqlite3.connect(old_db)
    try:
        assert con.execute("SELECT * FROM ide_file_revisions").fetchall() == before
        assert con.execute("SELECT * FROM ide_workspace_files").fetchall() == files
    finally:
        con.close()
