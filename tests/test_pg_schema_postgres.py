"""``PG_SCHEMA`` adoption against a real Postgres (opt-in, see tests/pg.py).

What only Postgres can answer: ``ALTER TABLE ... SET SCHEMA`` with rows and
an owned sequence, the foreign key a table keeps through the move, what the
catalogue reads see, the privilege ``CREATE SCHEMA IF NOT EXISTS`` needs, the
advisory lock and the lock timeout. SKIPPED unless ``TEST_POSTGRES_URL``
names a throw-away database; every test works on two schemas of its own
(the "public" it adopts from is one of them) and drops both, and the one
role a test creates is dropped as well.

Not proven here: ``_engine_settings`` and ``_create_and_migrate`` on a moved
table (the engines below set ``search_path`` themselves and run
``create_all`` only), the real ``public`` schema, two app instances (only
two connections that take the lock in turn), and the privileges of a BTP
service binding's role.

Run:  TEST_POSTGRES_URL=postgresql+asyncpg://... \
          python -m pytest tests/test_pg_schema_postgres.py -q
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import agents.ide.models  # noqa: E402,F401
from agents import db as agents_db  # noqa: E402
from agents import pg_schema  # noqa: E402
from agents.db import Base, DatabaseConfigError  # noqa: E402
from tests.pg import SKIP_REASON, postgres_url  # noqa: E402

pytestmark = pytest.mark.skipif(postgres_url() is None, reason=SKIP_REASON)

# Another application's table under one of our names (made up): no
# ``principal``, so it is foreign; ``id`` fits our chat_files' key into it.
FOREIGN_CHAT_CONVERSATIONS = (
    "CREATE TABLE chat_conversations (id varchar(36) primary key, owner text, "
    "subject text, created_at timestamptz)"
)


def _name() -> str:
    return f"agents_test_{uuid.uuid4().hex[:16]}"


def _engine(url: str, schema: str):
    return create_async_engine(
        url, poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )


@pytest.fixture
async def world():
    """``src`` (made here, the "public" of these tests) and ``dst`` (the
    PG_SCHEMA, made by the adoption), an engine on each and one without a
    search path; both schemas and every role in ``roles`` dropped after."""
    url = postgres_url()
    assert url is not None
    w = SimpleNamespace(src=_name(), dst=_name(), roles=[])
    w.owner = create_async_engine(url, poolclass=NullPool)
    w.on_src, w.on_dst = _engine(url, w.src), _engine(url, w.dst)
    async with w.owner.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{w.src}"'))
    try:
        yield w
    finally:
        await w.on_src.dispose()
        await w.on_dst.dispose()
        async with w.owner.begin() as c:
            for s in (w.src, w.dst):
                await c.execute(text(f'DROP SCHEMA IF EXISTS "{s}" CASCADE'))
            for role in w.roles:
                await c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await w.owner.dispose()


async def _older_install(w, *, chat_files: bool = True) -> None:
    """An older install in ``src``: our tables with rows, another app's
    chat_conversations under our name, and (``chat_files``) our chat_files
    created next to it, so its foreign key points at the other app's table
    (what a start without PG_SCHEMA leaves on a shared instance)."""
    async with w.on_src.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.execute(text("DROP TABLE chat_files, chat_conversations CASCADE"))
        await c.execute(text(FOREIGN_CHAT_CONVERSATIONS))
        await c.execute(text("INSERT INTO chat_conversations (id, owner) VALUES ('c1', 'u1')"))
        await c.execute(text(
            "INSERT INTO skill_configs (name, description, content) VALUES ('s1', 'd', 'c')"
        ))
        if chat_files:
            await c.run_sync(lambda sync: Base.metadata.tables["chat_files"].create(sync))


async def _adopt(w, adopt):
    async with w.on_dst.begin() as c:
        plan = await agents_db._adopt_schema(c, w.dst, adopt=adopt, source_schema=w.src)
        await c.run_sync(Base.metadata.create_all)
    return plan


async def _tables(w, schema: str) -> set[str]:
    async with w.owner.connect() as c:
        rows = await c.execute(text(
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relkind = 'r'"
        ), {"s": schema})
        return {r[0] for r in rows}


async def _schema_exists(w, schema: str) -> bool:
    async with w.owner.connect() as c:
        row = await c.execute(pg_schema._SCHEMA_EXISTS, {"schema": schema})
        return row.first() is not None


async def test_unset_refuses_and_changes_nothing(world):
    await _older_install(world)
    before = await _tables(world, world.src)
    with pytest.raises(DatabaseConfigError, match="PG_SCHEMA_ADOPT"):
        await _adopt(world, None)
    assert not await _schema_exists(world, world.dst)
    assert await _tables(world, world.src) == before


async def test_true_refuses_a_dangling_key_then_moves_rows_and_sequence_once(world):
    w = world
    await _older_install(w)
    # The key really points at the other app's table.
    async with w.owner.connect() as c:
        target = await c.execute(text(
            "SELECT fn.nspname || '.' || fc.relname FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid "
            "JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace "
            "WHERE con.conname = 'chat_files_conversation_id_fkey'"
        ))
        assert target.scalar() == f"{w.src}.chat_conversations"
    with pytest.raises(DatabaseConfigError, match="chat_files_conversation_id_fkey"):
        await _adopt(w, True)
    assert not await _schema_exists(w, w.dst), "a refusal must change nothing"
    async with w.owner.begin() as c:
        await c.execute(text(f'DROP TABLE "{w.src}".chat_files'))  # empty, ours: dropped

    async with w.on_dst.begin() as c:
        plan = await agents_db._adopt_schema(c, w.dst, adopt=True, source_schema=w.src)
        assert "skill_configs" in plan.moves and "chat_conversations" not in plan.moves
        # The lock is held for the rest of the transaction, the timeout is back.
        locks = await c.execute(text(
            "SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() AND granted"
        ))
        assert locks.scalar() == 1
        assert (await c.execute(text("SHOW lock_timeout"))).scalar() == "0"
        await c.run_sync(Base.metadata.create_all)
    second = await _adopt(w, True)  # the next start: everything is in place
    assert second.moves == [] and second.pending() == []
    assert second.foreign_twins == [("chat_conversations", "principal")]

    async with w.on_dst.begin() as c:
        await c.execute(text(
            "INSERT INTO skill_configs (name, description, content) VALUES ('s2', 'd', 'c')"
        ))
        ids = await c.execute(text("SELECT id, name FROM skill_configs ORDER BY id"))
        assert [tuple(r) for r in ids] == [(1, "s1"), (2, "s2")]
    async with w.owner.connect() as c:
        seq = await c.execute(text(
            "SELECT n.nspname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'S' AND c.relname = 'skill_configs_id_seq'"
        ))
        assert [r[0] for r in seq] == [w.dst]
        kept = await c.execute(text(f'SELECT owner FROM "{w.src}".chat_conversations'))
        assert kept.scalar() == "u1"
    assert await _tables(w, w.src) == {"chat_conversations"}
    assert {"skill_configs", "agent_configs", "chat_conversations", "chat_files"} <= (
        await _tables(w, w.dst))


async def test_a_key_from_a_table_that_stays_refuses(world):
    w = world
    await _older_install(w, chat_files=False)
    async with w.on_src.begin() as c:
        await c.execute(text(
            "CREATE TABLE other_audit (id integer primary key, "
            "skill_id integer constraint other_audit_skill_fk references skill_configs(id))"
        ))
    before = await _tables(w, w.src)
    with pytest.raises(DatabaseConfigError) as info:
        await _adopt(w, True)
    assert f"{w.src}.other_audit" in str(info.value)
    assert "other_audit_skill_fk" in str(info.value)
    assert await _tables(w, w.src) == before and not await _schema_exists(w, w.dst)


async def test_false_leaves_public_alone_and_creates_ours_in_the_schema(world):
    w = world
    await _older_install(w)
    before = await _tables(w, w.src)
    plan = await _adopt(w, False)
    assert plan.moves and await _tables(w, w.src) == before
    assert set(Base.metadata.tables) <= await _tables(w, w.dst)
    async with w.owner.connect() as c:
        mine = await c.execute(text(f'SELECT count(*) FROM "{w.dst}".skill_configs'))
        theirs = await c.execute(text(f'SELECT count(*) FROM "{w.src}".skill_configs'))
        assert (mine.scalar(), theirs.scalar()) == (0, 1)


async def test_an_existing_schema_needs_no_create_privilege(world):
    w = world
    role = f"agents_test_role_{uuid.uuid4().hex[:12]}"
    async with w.owner.begin() as c:
        allowed = await c.execute(text(
            "SELECT rolsuper OR rolcreaterole FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user"
        ))
        if not allowed.scalar():
            pytest.skip("the test user may not create a role")
        await c.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        w.roles.append(role)
        await c.execute(text(f'CREATE SCHEMA "{w.dst}"'))
        await c.execute(text(f'GRANT USAGE, CREATE ON SCHEMA "{w.dst}" TO "{role}"'))
    # A role without CREATE on the database, as a DBA would set it up.
    async with w.on_dst.connect() as c:
        async with c.begin():
            await c.execute(text(f'SET LOCAL ROLE "{role}"'))
            with pytest.raises(DBAPIError, match="permission denied"):
                async with c.begin_nested():
                    await c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{w.dst}"'))
            plan = await agents_db._adopt_schema(c, w.dst, adopt=None, source_schema=w.src)
            assert plan.pending() == []
            await c.execute(text("CREATE TABLE probe (id integer)"))  # lands in dst
    assert "probe" in await _tables(w, w.dst)


async def test_two_starts_take_turns(world):
    w = world
    async with w.on_dst.connect() as first, w.on_dst.connect() as second:
        trans = await first.begin()
        await agents_db._adopt_schema(first, w.dst, adopt=None, source_schema=w.src)

        async def later():
            async with second.begin():
                return await agents_db._adopt_schema(
                    second, w.dst, adopt=None, source_schema=w.src)

        waiting = asyncio.create_task(later())
        try:
            await asyncio.sleep(0.5)
            assert not waiting.done(), "the second start must wait for the first"
            await trans.commit()
            await asyncio.wait_for(waiting, timeout=10)
        finally:
            if not waiting.done():
                waiting.cancel()


async def test_a_start_that_waits_too_long_is_refused(world, monkeypatch):
    # The real wait is 30 s; half a second proves the same mapping: asyncpg's
    # lock_not_available (55P03) through SQLAlchemy becomes the fixed refusal.
    monkeypatch.setattr(pg_schema, "_ADVISORY_WAIT", text("SET LOCAL lock_timeout = '500ms'"))
    w = world
    async with w.on_dst.connect() as first, w.on_dst.connect() as second:
        async with first.begin():
            await agents_db._adopt_schema(first, w.dst, adopt=None, source_schema=w.src)
            with pytest.raises(DatabaseConfigError) as info:
                async with second.begin():
                    await agents_db._adopt_schema(
                        second, w.dst, adopt=None, source_schema=w.src)
            assert str(info.value) == pg_schema.LOCK_WAIT_REFUSAL
