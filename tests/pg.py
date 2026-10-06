"""A real Postgres for the few tests that only Postgres can answer.

Row locks, ``timestamptz`` and a writer that waits do not exist on the SQLite
file the suite runs on (``tests/testdb.py``), so those tests are opt-in:
they are SKIPPED unless ``TEST_POSTGRES_URL`` is set.

    TEST_POSTGRES_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/DBNAME \\
        .venv/bin/python -m pytest -q tests/test_odata_postgres.py

**The URL must name a throw-away database** (a local container, a scratch
instance) whose user may create and drop schemas. Never a shared, bound or
deployed database: the tests create a schema, fill it and drop it with
``CASCADE``. The password belongs in the environment variable only, never in
a file.

What keeps it contained:

* every test gets a schema of its own, ``agents_test_<random>``, created
  here and dropped here whether the test passed or not; nothing outside that
  schema is read or written, and no name is shared between runs;
* the engine is built here, per test, with that schema as its only
  ``search_path``. It is NOT ``agents.db.engine``: the process-wide SQLite
  engine and ``DATABASE_URL`` are never touched, so these tests can sit in a
  normal run of the suite.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ENV = "TEST_POSTGRES_URL"
_SCHEME = "postgresql+asyncpg://"
_SCHEMA_RE = re.compile(r"agents_test_[0-9a-f]{16}")
SKIP_REASON = f"needs a throw-away Postgres in {ENV} (see tests/pg.py)"


def postgres_url() -> str | None:
    """The opt-in URL, or None when these tests are to be skipped.

    Only an asyncpg URL is taken; anything else is a setup mistake and is
    raised rather than skipped, without repeating the value (it holds a
    password).
    """
    url = os.environ.get(ENV, "").strip()
    if not url:
        return None
    if not url.startswith(_SCHEME):
        raise RuntimeError(f"{ENV} must start with {_SCHEME}")
    return url


@asynccontextmanager
async def private_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session maker on a new schema holding the app's tables; the schema
    is dropped on the way out."""
    from agents.db import Base  # after the caller's use_test_database()

    url = postgres_url()
    if url is None:
        raise RuntimeError(SKIP_REASON)
    schema = f"agents_test_{uuid.uuid4().hex[:16]}"
    assert _SCHEMA_RE.fullmatch(schema)  # the only text ever put into DDL below
    # NullPool: no connection outlives the statement that needed it, so the
    # DROP at the end never waits for an idle one.
    owner = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with owner.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            # The app's own settings: expire_on_commit off, as SessionLocal.
            yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        finally:
            await engine.dispose()
            async with owner.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await owner.dispose()
