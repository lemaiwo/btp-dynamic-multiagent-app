"""The ``builtin:hana`` tools in an ABAP Assistant session: not in
``READONLY_POLICY``, so the guard denies each of them by name (plain and
prefixed), for both session types, and a guarded toolset forwards none."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402

TOOLS = ("list_systems", "list_checks", "run_check", "run_select")


@pytest.mark.parametrize("policy", ["change", "diagnose"])
def test_the_tools_are_denied_in_an_abap_assistant_session(policy):
    from agents.ide.readonly import POLICIES, READONLY_POLICY, check_call

    for tool in TOOLS:
        for name in (tool, "hana_" + tool, "hana_0_" + tool):
            assert name not in READONLY_POLICY
            assert all(name not in rules for rules in POLICIES.values())
            assert check_call(name, {"system": "prd"}, policy) is not None
            assert check_call(name, {}, policy) is not None


def test_the_tool_names_are_the_toolsets_own():
    from agents.hana.tools import TOOL_NAMES

    assert tuple(TOOL_NAMES) == TOOLS


@pytest.mark.parametrize("prefix", [None, "hana"])
async def test_a_guarded_hana_toolset_forwards_none_in_a_change_session(prefix):
    # Through ReadOnlyGuard around a real built toolset (allow_select, so all
    # four tools exist): bound to an IDE session, none is listed and a direct
    # call is refused before the toolset is reached.
    from pydantic_ai import ModelRetry, RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    from agents.deep import DeepState, WorkspaceScope, current_workspace
    from agents.hana.tools import hana_toolset
    from agents.ide.readonly import ReadOnlyGuard

    toolset = hana_toolset(
        {"systems": [{"name": "prd", "destination": "HANA_PRD"}], "allow_select": True},
        auth_mode="destination")
    if prefix:
        toolset = toolset.prefixed(prefix)
    guard = ReadOnlyGuard(toolset)
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    unbound = await guard.get_tools(ctx)
    assert len(unbound) == 4  # outside a session the guard passes them on
    token = current_workspace.set(WorkspaceScope(session_id="s-hana",
                                                 state=DeepState(run_id="s-hana")))
    try:
        assert await guard.get_tools(ctx) == {}
        for name, tool in unbound.items():
            with pytest.raises(ModelRetry):
                await guard.call_tool(name, {"system": "prd"}, ctx, tool)
    finally:
        current_workspace.reset(token)
