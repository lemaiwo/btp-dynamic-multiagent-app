"""``PG_SCHEMA`` adoption against a real Postgres (opt-in, see tests/pg.py).

The move itself (``ALTER TABLE ... SET SCHEMA`` with rows, the foreign table
left where it is, a second run that changes nothing) can only be proven on
Postgres. SKIPPED unless ``TEST_POSTGRES_URL`` names a throw-away database;
the test works on two schemas of its own and drops both.

Run:  TEST_POSTGRES_URL=postgresql+asyncpg://... python -m pytest tests/test_pg_schema_postgres.py -q
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import agents.ide.models  # noqa: E402,F401
from agents import db as agents_db  # noqa: E402
from agents.db import Base  # noqa: E402
from tests.pg import SKIP_REASON, postgres_url  # noqa: E402

pytestmark = pytest.mark.skipif(postgres_url() is None, reason=SKIP_REASON)

# The other app's table on EC as recorded on 2026-11-04 (see tests/test_pg_schema.py).
FOREIGN_CHAT_CONVERSATIONS = (
    "CREATE TABLE chat_conversations (id text primary key, user_id text, title text, "
    "created_at timestamptz, updated_at timestamptz, agent_ref varchar(80), "
    "access varchar(16) not null default 'basic')"
)


def _schema() -> str:
    return f"agents_test_{uuid.uuid4().hex[:16]}"


def _engine(url: str, schema: str):
    return create_async_engine(
        url, poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )


async def test_adoption_moves_ours_and_leaves_the_foreign_table():
    url = postgres_url()
    assert url is not None
    src, dst = _schema(), _schema()
    owner = create_async_engine(url, poolclass=NullPool)
    on_src = _engine(url, src)
    on_dst = _engine(url, dst)
    try:
        async with owner.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{src}"'))
        # The 2.22.0 world: our tables with a row, plus the other app's table
        # under one of our names (ours of that name dropped first).
        async with on_src.begin() as c:
            await c.run_sync(Base.metadata.create_all)
            await c.execute(text("DROP TABLE chat_conversations CASCADE"))
            await c.execute(text(FOREIGN_CHAT_CONVERSATIONS))
            await c.execute(text("INSERT INTO chat_conversations (id, user_id) VALUES ('c1', 'u1')"))
            await c.execute(text(
                "INSERT INTO skill_configs (name, description, content) VALUES ('s1', 'd', 'c')"
            ))
        for _ in range(2):  # the second run finds everything in place
            async with on_dst.begin() as c:
                await agents_db._adopt_schema(c, dst, source_schema=src)
                await c.run_sync(Base.metadata.create_all)
        async with owner.begin() as c:
            rows = await c.execute(text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN (:a, :b)"
            ), {"a": src, "b": dst})
            tables = {(r[0], r[1]) for r in rows}
            assert (dst, "skill_configs") in tables and (src, "skill_configs") not in tables
            assert (src, "chat_conversations") in tables and (dst, "chat_conversations") in tables
            assert (await c.execute(text(f'SELECT name FROM "{dst}".skill_configs'))).scalar() == "s1"
            assert (await c.execute(text(f'SELECT user_id FROM "{src}".chat_conversations'))).scalar() == "u1"
            cols = {r[0] for r in await c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'chat_conversations'"
            ), {"s": dst})}
            assert "principal" in cols and "user_id" not in cols
    finally:
        await on_src.dispose()
        await on_dst.dispose()
        async with owner.begin() as c:
            for s in (src, dst):
                await c.execute(text(f'DROP SCHEMA IF EXISTS "{s}" CASCADE'))
        await owner.dispose()
