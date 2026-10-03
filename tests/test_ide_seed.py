"""IDE seed loader: inserts missing skills/agents, never updates existing rows.

Run:  pytest tests/test_ide_seed.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
(ROOT / "tests" / "_test_ide_seed.db").unlink(missing_ok=True)  # fresh DB per run
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_seed.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from sqlalchemy import delete  # noqa: E402

from agents.db import (  # noqa: E402
    AgentConfig,
    SessionLocal,
    SkillConfig,
    get_agent_by_name,
    get_skill_by_name,
    init_db,
    upsert_skill,
)
from agents.ide.seed import ensure_ide_seed  # noqa: E402


def _agent(name: str, **kw) -> dict:
    server = {"url": "http://example.invalid/mcp", "auth_mode": "none"}
    return {"name": name, "description": "d", "instructions": "i", "mcp_servers": [server], **kw}


def _write(tmp_path: Path, skills=(), agents=()) -> Path:
    p = tmp_path / "seed.json"
    p.write_text(json.dumps({"version": 1, "skills": list(skills), "agents": list(agents)}))
    return p


# Every name this module seeds. The DB may be shared with other test modules
# in the same pytest process (DATABASE_URL is set by whichever module imports
# agents.db first) and may keep rows from an earlier run, so each test starts
# by removing these names instead of assuming an empty DB.
_NAMES = ("ide-seed-sk", "ide-seed-ag", "ide-edit-sk", "ide-valid-ag", "ide-off-ag")


@pytest.fixture(autouse=True)
async def _db():
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(AgentConfig).where(AgentConfig.name.in_(_NAMES)))
        await s.execute(delete(SkillConfig).where(SkillConfig.name.in_(_NAMES)))
        await s.commit()


async def test_inserts_missing(tmp_path):
    p = _write(
        tmp_path,
        skills=[{"name": "ide-seed-sk", "description": "d", "content": "c"}],
        agents=[_agent("ide-seed-ag", skills=["ide-seed-sk"])],
    )
    res = await ensure_ide_seed(p)
    assert res == {"skills_added": 1, "agents_added": 1}
    async with SessionLocal() as s:
        assert await get_skill_by_name(s, "ide-seed-sk") is not None
        assert await get_agent_by_name(s, "ide-seed-ag") is not None
    # Idempotent
    assert await ensure_ide_seed(p) == {"skills_added": 0, "agents_added": 0}


async def test_existing_row_untouched(tmp_path):
    async with SessionLocal() as s:
        await upsert_skill(s, name="ide-edit-sk", description="edited", content="edited")
    p = _write(
        tmp_path,
        skills=[{"name": "ide-edit-sk", "description": "seed", "content": "seed"}],
    )
    assert (await ensure_ide_seed(p))["skills_added"] == 0
    async with SessionLocal() as s:
        sk = await get_skill_by_name(s, "ide-edit-sk")
        assert sk.content == "edited" and sk.description == "edited"


async def test_invalid_entries_skipped_with_warning(tmp_path, caplog):
    p = _write(
        tmp_path,
        skills=[{"bogus": 1}],
        agents=[{"bogus": 1}, _agent("ide-valid-ag")],
    )
    with caplog.at_level(logging.WARNING):
        res = await ensure_ide_seed(p)
    assert res == {"skills_added": 0, "agents_added": 1}
    assert "Skipping invalid" in caplog.text


async def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("IDE_SEED", "false")
    p = _write(tmp_path, agents=[_agent("ide-off-ag")])
    assert await ensure_ide_seed(p) == {"skills_added": 0, "agents_added": 0}
    async with SessionLocal() as s:
        assert await get_agent_by_name(s, "ide-off-ag") is None


async def test_missing_or_bad_file(tmp_path):
    assert await ensure_ide_seed(tmp_path / "nope.json") == {"skills_added": 0, "agents_added": 0}
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert await ensure_ide_seed(bad) == {"skills_added": 0, "agents_added": 0}
