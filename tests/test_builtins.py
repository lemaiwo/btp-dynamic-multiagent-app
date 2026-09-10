"""The closed set of ``builtin:`` pseudo-URLs and the factories behind them."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402


def test_sapnotes_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert "builtin:sapnotes" in BUILTIN_URLS
    assert is_builtin_url("builtin:sapnotes")
    assert is_builtin_url("BUILTIN:SAPNOTES")


def test_build_builtin_toolset_constructs_sapnotes():
    from pydantic_ai.toolsets import FunctionToolset

    from agents.builtins import build_builtin_toolset

    toolset = build_builtin_toolset("builtin:sapnotes", {"min_score": "9.0"}, "none")
    assert isinstance(toolset, FunctionToolset)


def test_unknown_builtin_still_rejected():
    from agents.builtins import build_builtin_toolset

    with pytest.raises(ValueError, match="unknown built-in toolset"):
        build_builtin_toolset("builtin:teams", {}, "none")
