"""Shared pytest helpers.

Several script-style suites (test_a2a, test_admin_api, test_admin_ui,
test_agent_model, test_auth_middleware, test_deep_agents) stub
``agents.shared.create_mcp_server``, ``pydantic_ai.Agent.__init__`` and
``Agent.run`` at *import* time, so the stubs leak into every other suite
collected in the same pytest process. Each stub keeps the function it
replaced as ``_unpatched``; the ``real_agents_and_mcp`` fixture follows that
chain back to the real one for the duration of a test.

This module deliberately imports nothing from ``agents``: ``agents.db`` reads
``DATABASE_URL`` at import, and each suite sets its own before importing it.
"""

from __future__ import annotations

import sys

import pytest


def unpatched(fn):
    """The original behind a chain of import-time stubs (or ``fn`` itself)."""
    while getattr(fn, "_unpatched", None) is not None:
        fn = fn._unpatched
    return fn


@pytest.fixture
def real_agents_and_mcp(monkeypatch):
    from pydantic_ai import Agent

    if "run" in vars(Agent):  # the real run is inherited, a stub is not
        monkeypatch.delattr(Agent, "run")
    real_init = unpatched(Agent.__init__)
    if real_init is not Agent.__init__:
        monkeypatch.setattr(Agent, "__init__", real_init)
    for name in ("agents.shared", "agents.registry"):
        module = sys.modules.get(name)
        if module is None:
            continue
        real = unpatched(module.create_mcp_server)
        if real is not module.create_mcp_server:
            monkeypatch.setattr(module, "create_mcp_server", real)


class LockTrail(list):
    """SQL statements sent, with :data:`LOCK` where ``lock_session_row`` was
    called (the spy runs before the real lock's own SELECT)."""

    LOCK = "<lock_session_row>"

    def assert_lock_before_writes_to(self, table: str) -> None:
        """The session row is locked before the first write to ``table``
        (order session row -> child row, so no deadlock on Postgres)."""
        writes = [i for i, s in enumerate(self)
                  if s.startswith((f"UPDATE {table} ", f"INSERT INTO {table} "))]
        assert writes, f"nothing was written to {table}: {self[:5]}"
        assert self.LOCK in self, f"no session lock before {table}: {self[:5]}"
        assert self.index(self.LOCK) < writes[0], self[: writes[0] + 1]


@pytest.fixture
def lock_trail(monkeypatch):
    """Spy on ``agents.ide.store.lock_session_row`` (and every IDE module
    that imported it by name) plus a cursor listener on the app engine.
    SQLite cannot show a lock-order deadlock; the order of statements can."""
    from sqlalchemy import event

    from agents import db as db_module
    from agents.ide import store

    seen = LockTrail()
    real = store.lock_session_row

    async def spy(db, sid):
        seen.append(LockTrail.LOCK)
        await real(db, sid)

    for name in ("agents.ide.store", "agents.ide.runner", "agents.ide.routes",
                 "agents.ide.session_tools", "agents.ide.workspace",
                 "agents.ide.stages"):
        module = sys.modules.get(name)
        if module is not None and getattr(module, "lock_session_row", None) is real:
            monkeypatch.setattr(module, "lock_session_row", spy)

    def record(conn, cursor, statement, *a):
        seen.append(statement)

    engine = db_module.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)
