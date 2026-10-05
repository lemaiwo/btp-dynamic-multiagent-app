"""Phase 1c models: session type, non_production flag, diagnose tables."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IDE_CHILD_MODELS,
    IdeApproval,
    IdeAuditLog,
    IdeFinding,
    IdeSession,
)


async def test_session_type_defaults_to_change():
    await init_db()
    async with SessionLocal() as s:
        row = IdeSession(owner="u1", title="t", target="DEMO")
        s.add(row)
        await s.commit()
        await s.refresh(row)
        assert row.session_type == "change"
        assert row.meta()["type"] == "change"


async def test_new_tables_exist_and_finding_is_unique():
    await init_db()
    async with SessionLocal() as s:
        sess = IdeSession(owner="u1", title="t", target="DEMO", session_type="diagnose")
        s.add(sess)
        await s.commit()
        s.add(IdeApproval(session_id=sess.id, action="trace_start", params_json="{}"))
        s.add(
            IdeAuditLog(
                principal="u1", session_id=sess.id, target="DEMO",
                action="trace_arm", params_json="{}", outcome="ok",
            )
        )
        s.add(IdeFinding(session_id=sess.id, kind="dump", ref_id="r1", title="x"))
        await s.commit()
        s.add(IdeFinding(session_id=sess.id, kind="dump", ref_id="r1", title="y"))
        with pytest.raises(IntegrityError):
            await s.commit()


def test_child_models_include_findings_and_approvals_not_audit():
    assert IdeFinding in IDE_CHILD_MODELS and IdeApproval in IDE_CHILD_MODELS
    assert IdeAuditLog not in IDE_CHILD_MODELS


def test_migration_adds_columns_to_an_old_table(tmp_path):
    """An 1a database has neither column; init_db must add them with defaults."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE ide_sessions (id VARCHAR(36) PRIMARY KEY, owner VARCHAR(255) NOT NULL,
          title VARCHAR(200) NOT NULL, target VARCHAR(64) NOT NULL, stage VARCHAR(16) NOT NULL,
          status VARCHAR(16) NOT NULL, run_id VARCHAR(36), requests_used INTEGER NOT NULL,
          todos_json TEXT, created_at DATETIME, updated_at DATETIME);
        INSERT INTO ide_sessions VALUES ('s1','u','t','DEMO','chat','idle',NULL,0,NULL,NULL,NULL);
        CREATE TABLE ide_conventions (target VARCHAR(64) PRIMARY KEY, label VARCHAR(120) NOT NULL,
          destination VARCHAR(200) NOT NULL, namespace VARCHAR(30) NOT NULL,
          package VARCHAR(30) NOT NULL,
          atc_variant VARCHAR(30) NOT NULL, clean_core_level VARCHAR(1) NOT NULL,
          free_text TEXT NOT NULL, updated_at DATETIME);
        INSERT INTO ide_conventions VALUES ('DEMO','','','','','','A','',NULL);
        """
    )
    con.commit()
    con.close()
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    code = (
        "import asyncio; from agents.db import init_db; "
        "asyncio.run(init_db()); asyncio.run(init_db())"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    assert con.execute("SELECT session_type FROM ide_sessions").fetchone() == ("change",)
    assert con.execute("SELECT non_production FROM ide_conventions").fetchone() == (0,)
    con.close()


def test_retention_indexes_declared():
    def cols(table):
        return {tuple(c.name for c in i.columns) for i in table.indexes}

    assert ("ts",) in cols(IdeAuditLog.__table__)
    assert ("session_id",) in cols(IdeAuditLog.__table__)
    assert ("status", "created_at") in cols(IdeApproval.__table__)
