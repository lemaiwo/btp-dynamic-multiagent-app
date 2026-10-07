"""A real SAP HANA HDI container for the tests only HANA can answer.

That the generated artifacts deploy, that a statement really runs on an
``NCLOB`` column, that a second ``FOR UPDATE`` waits: none of that shows on
the SQLite file the suite runs on, and the HANA dialect alone only compiles.
So these tests are opt-in and SKIPPED unless ``TEST_HANA_SERVICE_KEY`` holds
the JSON of a service key (or binding) of an ``hdi-shared`` container:

    TEST_HANA_SERVICE_KEY="$(cf service-key <instance> <key> | sed -n '/{/,$p')" \\
        .venv/bin/python -m pytest -q tests/test_hana_integration.py

**The key must belong to a throw-away container.** The tests deploy the
app's tables into it (and undeploy whatever else is deployed below ``src/``),
delete every row of every table before and after each test, and leave the
current schema deployed and empty. Never the container of a deployed app.

The key belongs in the environment variable only: it is never written to a
file, printed or put into a failure message here. A key holds a certificate
with raw newlines, so it is read with ``strict=False``; the ``credentials``
wrapper of a binding is accepted.

Wiring: the key's credentials go through ``agents.db._hana_target`` and
``_engine_settings`` -- the same two functions a ``hana`` binding in
``VCAP_SERVICES`` goes through -- into an engine of its own (never
``agents.db.engine``; the process-wide SQLite engine and ``DATABASE_URL``
are not touched, so this suite can sit in a normal run). A test that needs
``init_db`` or a module's ``SessionLocal`` on HANA patches those names for
its own duration (see the ``hana`` fixture of the suite).

One container, no schema per test: run this suite serially, one process at
a time.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ENV = "TEST_HANA_SERVICE_KEY"
SKIP_REASON = f"needs the service key of a throw-away HDI container in {ENV} (see tests/hana.py)"


def service_key() -> dict[str, Any] | None:
    """The opt-in key's credentials, or None when these tests are skipped.

    Anything that is not such a key is a setup mistake and is raised rather
    than skipped, without repeating the value.
    """
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw, strict=False)
    except ValueError:
        raise RuntimeError(f"{ENV} is not JSON") from None
    if isinstance(data, dict) and isinstance(data.get("credentials"), dict):
        data = data["credentials"]
    if not isinstance(data, dict) or not data.get("hdi_user") or not data.get("schema"):
        raise RuntimeError(f"{ENV} is not the key of an hdi-shared container")
    return data


@dataclass
class Container:
    """The container as the app would see it bound."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    hdi: Any  # agents.hana_hdi.HdiCredentials

    async def empty(self) -> None:
        """Delete every row of every table, children first."""
        from agents.db import Base

        async with self.sessions() as session:
            for table in reversed(Base.metadata.sorted_tables):
                await session.execute(delete(table))
            await session.commit()


@asynccontextmanager
async def container() -> AsyncIterator[Container]:
    """An engine on the container, as its runtime user; disposed on the way out."""
    from agents import db as agents_db  # after the caller's use_test_database()

    credentials = service_key()
    if credentials is None:
        raise RuntimeError(SKIP_REASON)
    target = agents_db._hana_target(credentials)
    url, connect_args = agents_db._engine_settings(target)
    # An engine, and so a pool, per test: connections are reused inside one
    # test (a TLS connect to HANA Cloud takes about a second) and none is
    # carried from one test's event loop into the next.
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        yield Container(
            engine=engine,
            # The app's own settings: expire_on_commit off, as SessionLocal.
            sessions=async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
            hdi=target.hdi,
        )
    finally:
        await engine.dispose()
