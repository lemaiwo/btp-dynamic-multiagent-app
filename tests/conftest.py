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
