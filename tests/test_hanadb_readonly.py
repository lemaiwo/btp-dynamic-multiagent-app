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
