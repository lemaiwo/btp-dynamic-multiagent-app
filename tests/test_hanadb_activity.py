"""The fixed-form activity lines of ``builtin:hana``: counts, fixed words, a
registered check name and a refusal's code; never a row, a column, SQL or a
name the model sent."""

from __future__ import annotations

import pytest

from agents.hana.checks import base
from agents.hana.checks.base import Check, Statement, register
from agents.hana.tools import activity_summary

LEAK = "customer4711"


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})
    register(Check(name="alerts", category="monitoring", purpose="Current alerts.",
                   source="bin/alerts.js",
                   statements=(Statement("", "SELECT A FROM SYS.M_ALERTS", ()),)))


def _rows(n: int) -> list[list[str]]:
    return [[LEAK] for _ in range(n)]


@pytest.mark.parametrize("name", ["run_check", "hana_run_check", "hana_0_run_check"])
def test_run_check_line(name: str) -> None:
    result = {"system": "sib", "check": "alerts", "columns": [LEAK], "rows": _rows(12),
              "row_count": 12, "truncated": False}
    assert activity_summary(name, result) == "run_check: alerts, 12 rows"
    result = {**result, "rows": _rows(500), "row_count": 500, "truncated": True}
    assert activity_summary(name, result) == "run_check: alerts, 500 rows, truncated"


def test_run_check_names_only_a_registered_check() -> None:
    result = {"check": LEAK, "rows": _rows(2), "truncated": False}
    said = activity_summary("run_check", result)
    assert said == "run_check: 2 rows"
    assert LEAK not in said


def test_run_check_composite_line() -> None:
    result = {"check": "alerts", "parts": [
        {"label": "a", "rows": _rows(2), "truncated": False},
        {"label": "b", "rows": _rows(3), "truncated": True}]}
    assert activity_summary("run_check", result) == "run_check: alerts, 5 rows, truncated"


def test_run_select_line() -> None:
    result = {"system": "sib", "columns": [LEAK], "rows": _rows(40), "row_count": 40,
              "truncated": False}
    assert activity_summary("run_select", result) == "run_select: 40 rows"
    assert activity_summary("hana_1_run_select", {**result, "truncated": True}) == \
        "run_select: 40 rows, truncated"


def test_list_lines() -> None:
    checks = {"checks": [{"name": LEAK} for _ in range(30)]}
    assert activity_summary("list_checks", checks) == "list_checks: 30 checks"
    systems = {"systems": [{"name": LEAK}, {"name": LEAK}]}
    assert activity_summary("hana_list_systems", systems) == "list_systems: 2 systems"


def test_error_line() -> None:
    error = {"error": {"code": "not_authorized", "message": LEAK}}
    assert activity_summary("hana_0_run_check", error) == "error: not_authorized"
    assert activity_summary("run_select", {"error": {"code": f"bad {LEAK}"}}) == "error"


def test_other_tools_and_odd_results() -> None:
    assert activity_summary("read_file", {"rows": _rows(1)}) is None
    assert activity_summary("git_run_check", {"rows": _rows(1)}) is None
    assert activity_summary("run_checks", {"rows": _rows(1)}) is None
    assert activity_summary(None, {}) is None
    assert activity_summary("run_check", LEAK) == "run_check: no summary"
    assert activity_summary("run_check", {"rows": LEAK}) == "run_check: ? rows"
