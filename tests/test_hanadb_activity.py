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
    result = {"system": "erp", "check": "alerts", "columns": [LEAK], "rows": _rows(12),
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
    result = {"system": "erp", "columns": [LEAK], "rows": _rows(40), "row_count": 40,
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


# --- where the lines are used: the registry's preview and the argument preview

def test_the_registry_preview_is_the_fixed_line():
    from types import SimpleNamespace

    from agents.registry import _short_tool_output

    result = {"system": "erp", "check": "alerts", "columns": [LEAK], "rows": _rows(12),
              "row_count": 12, "truncated": False}
    for name in ("run_check", "hana_run_check", "hana_0_run_check"):
        said = _short_tool_output(SimpleNamespace(tool_name=name, content=result))
        assert said == "run_check: alerts, 12 rows"
    said = _short_tool_output(SimpleNamespace(
        tool_name="run_select", content={"system": "erp", "columns": [LEAK],
                                         "rows": _rows(3), "row_count": 3,
                                         "truncated": False}))
    assert LEAK not in said and said.startswith("run_select: 3 rows")


SQL = "SELECT HOST, PORT FROM SYS.M_SERVICES WHERE SERVICE = 'x'"


@pytest.mark.parametrize("name", ["run_select", "hana_run_select", "hana_0_run_select"])
def test_the_argument_preview_of_run_select_keeps_the_length_of_the_sql(name):
    from agents.run_activity import _detail_for

    assert len(SQL) == 57
    args = {"system": "erp", "sql": SQL, "top": 100}
    assert _detail_for(name, args) == "system=erp, sql=<57 chars>, top=100"
    # pydantic-ai may hand the arguments over as their JSON text.
    import json

    assert _detail_for(name, json.dumps(args)) == "system=erp, sql=<57 chars>, top=100"
    assert "M_SERVICES" not in _detail_for(name, f"not json {SQL}")


def test_other_tools_keep_their_preview():
    from agents.run_activity import _detail, _detail_for

    args = {"system": "erp", "sql": SQL}
    for name in ("run_check", "other_run_select", "run_selection", None):
        assert _detail_for(name, args) == _detail(args)


def test_a_run_records_and_the_chat_card_shows_the_length_only():
    from agents.progress import ProgressUpdate
    from agents.run_activity import RunActivity
    from agents.webchat.run import _preview_args

    activity = RunActivity()
    activity.record(ProgressUpdate(kind="tool_start", agent="dba",
                                   tool_name="hana_run_select", tool_call_id="c1",
                                   args={"system": "erp", "sql": SQL, "top": 100}))
    assert activity.events[0]["detail"] == "system=erp, sql=<57 chars>, top=100"
    shown = _preview_args("run_select", {"system": "erp", "sql": SQL, "top": 100})
    assert shown == "system=erp, sql=<57 chars>, top=100"
    assert "M_SERVICES" in _preview_args("run_check", {"sql": SQL})


@pytest.mark.parametrize("args", [
    ["SELECT", SQL], (SQL,), 42, {"sql": ["SELECT", SQL]}, {"system": "erp", "sql": None},
    {"system": "erp", "query": SQL}, b"SELECT HOST FROM SYS.M_SERVICES"])
def test_run_select_of_any_other_shape_is_shown_as_its_length_only(args):
    from agents.run_activity import _detail_for, redact_args

    said = _detail_for("hana_run_select", args)
    assert "M_SERVICES" not in said and said.startswith("<") and said.endswith(" chars>")
    shown = redact_args("run_select", args)
    assert isinstance(shown, str) and "M_SERVICES" not in shown


def test_redact_args_hands_back_the_object_to_show():
    from agents.run_activity import redact_args

    args = {"system": "erp", "sql": SQL, "top": 100}
    assert redact_args("hana_0_run_select", args) == {"system": "erp", "sql": "<57 chars>",
                                                      "top": 100}
    assert args["sql"] == SQL  # the caller's object is not changed
    assert redact_args("run_check", args) is args
    assert redact_args("run_select", None) is None


def test_the_classic_chat_tool_card_shows_the_length_only():
    from agents.chat_app import _tool_card_input

    shown = _tool_card_input("hana_run_select", {"system": "erp", "sql": SQL, "top": 100})
    assert shown == {"system": "erp", "sql": "<57 chars>", "top": 100}
    assert _tool_card_input("run_check", {"sql": SQL}) == {"sql": SQL}
    assert _tool_card_input("run_check", None) == {}
    assert "M_SERVICES" not in str(_tool_card_input("run_select", f"not json {SQL}"))


def test_the_classic_chat_sink_uses_the_card_helper():
    """The sink is a closure inside the ASGI call: its tool card must take
    its input from `_tool_card_input`, never from `update.args` itself."""
    import inspect

    import agents.chat_app as chat_app

    source = inspect.getsource(chat_app)
    assert "input=update.args" not in source
    assert "input=_tool_card_input(name, update.args)" in source


def test_run_select_preview_shows_system_sql_length_and_top_only():
    from agents.run_activity import _detail_for, redact_args

    args = {"system": "erp", "sql": SQL, "top": 5, "note": "M_SERVICES leak",
            "schema": "SAPABAP1"}
    assert redact_args("run_select", args) == {"system": "erp", "sql": "<57 chars>",
                                               "top": 5}
    assert _detail_for("hana_run_select", {"sql": SQL, "other": "x"}) == "sql=<57 chars>"
    said = _detail_for("hana_0_run_select", args)
    assert said == "system=erp, sql=<57 chars>, top=5"


# --- export_result ------------------------------------------------------------

@pytest.mark.parametrize("name", ["export_result", "hana_export_result",
                                  "hana_0_export_result"])
def test_export_result_line(name: str) -> None:
    result = {"system": "erp", "check": "alerts", "file_id": "f-" + LEAK,
              "filename": LEAK + ".xlsx", "download_url": "/chat/api/files/" + LEAK,
              "row_count": 200, "truncated": False, "columns": [LEAK],
              "preview": _rows(20)}
    said = activity_summary(name, result)
    assert said == "export_result: 200 rows"
    assert LEAK not in said
    said = activity_summary(name, {**result, "row_count": 10000, "truncated": True})
    assert said == "export_result: 10000 rows, truncated"
    assert activity_summary(name, {"error": {"code": "no_chat", "message": LEAK}}) == \
        "error: no_chat"


def test_export_result_line_of_an_odd_count() -> None:
    for count in (None, True, "12", LEAK, 1.5):
        said = activity_summary("export_result", {"row_count": count, "preview": _rows(3)})
        assert said == "export_result: ? rows"


def test_export_result_line_of_a_composite_check() -> None:
    result = {"check": "alerts", "file_id": LEAK, "parts": [
        {"label": "a", "row_count": 2, "truncated": False, "preview": _rows(2)},
        {"label": "b", "row_count": 3, "truncated": True, "preview": _rows(3)}]}
    assert activity_summary("export_result", result) == "export_result: 5 rows, truncated"


EXPORT_DROPPED = ("filename", "schema", "object", "section", "key", "host",
                  "other_schema", "hours", "note")


@pytest.mark.parametrize("name", ["export_result", "hana_export_result",
                                  "hana_0_export_result"])
def test_the_argument_preview_of_export_result_keeps_the_length_of_the_sql(name):
    import json

    from agents.run_activity import _detail_for, redact_args

    args = {key: LEAK for key in EXPORT_DROPPED}
    args.update({"top": 500, "sql": SQL, "check": "alerts", "system": "erp"})
    shown = redact_args(name, args)
    assert shown == {"system": "erp", "check": "alerts", "sql": "<57 chars>", "top": 500}
    assert list(shown) == ["system", "check", "sql", "top"]
    assert args["sql"] == SQL and args["filename"] == LEAK  # the caller's object stays
    said = _detail_for(name, args)
    assert said == "system=erp, check=alerts, sql=<57 chars>, top=500"
    assert LEAK not in said and "M_SERVICES" not in said
    assert _detail_for(name, json.dumps(args)) == said


def test_export_result_of_a_check_keeps_no_sql_key():
    from agents.run_activity import redact_args

    args = {"system": "erp", "check": "alerts", "filename": LEAK, "schema": LEAK,
            "top": 10}
    assert redact_args("export_result", args) == {"system": "erp", "check": "alerts",
                                                  "top": 10}


def test_export_result_drops_a_check_that_is_no_check_name():
    from agents.run_activity import redact_args

    for check in (LEAK + " ' OR 1=1", "a.b", "", 7, None, ["alerts"]):
        shown = redact_args("export_result", {"system": "erp", "check": check,
                                              "filename": LEAK})
        assert shown == {"system": "erp"}


@pytest.mark.parametrize("args", [
    ["SELECT", SQL], (SQL,), 42, {"sql": ["SELECT", SQL]}, {"system": "erp", "sql": None},
    {"system": "erp", "sql": 5, "check": "alerts"}, b"SELECT HOST FROM SYS.M_SERVICES",
    f"not json {SQL}", '["SELECT HOST FROM SYS.M_SERVICES"]'])
def test_export_result_of_any_other_shape_is_shown_as_its_length_only(args):
    from agents.run_activity import _detail_for, redact_args

    said = _detail_for("hana_export_result", args)
    assert "M_SERVICES" not in said and said.startswith("<") and said.endswith(" chars>")
    shown = redact_args("export_result", args)
    assert isinstance(shown, str) and "M_SERVICES" not in shown


def test_run_select_preview_keeps_offset():
    from agents.run_activity import _detail_for, redact_args

    args = {"offset": 200, "top": 100, "sql": SQL, "system": "erp", "filename": LEAK}
    shown = redact_args("run_select", args)
    assert shown == {"system": "erp", "sql": "<57 chars>", "top": 100, "offset": 200}
    assert list(shown) == ["system", "sql", "top", "offset"]
    assert _detail_for("hana_0_run_select", args) == \
        "system=erp, sql=<57 chars>, top=100, offset=200"


def test_a_run_records_export_result_without_its_file_name():
    from agents.chat_app import _tool_card_input
    from agents.progress import ProgressUpdate
    from agents.run_activity import RunActivity
    from agents.webchat.run import _preview_args

    args = {"system": "erp", "filename": LEAK, "sql": SQL, "top": 100}
    activity = RunActivity()
    activity.record(ProgressUpdate(kind="tool_start", agent="dba",
                                   tool_name="hana_export_result", tool_call_id="c1",
                                   args=args))
    assert activity.events[0]["detail"] == "system=erp, sql=<57 chars>, top=100"
    assert _preview_args("export_result", args) == "system=erp, sql=<57 chars>, top=100"
    assert _tool_card_input("hana_export_result", args) == {
        "system": "erp", "sql": "<57 chars>", "top": 100}


def test_error_line_shows_only_a_listed_code() -> None:
    assert activity_summary("run_check", {"error": {"code": "something_else"}}) == \
        "error: call_failed"
    assert activity_summary("run_check", {"error": {"code": "login_failed"}}) == \
        "error: login_failed"


@pytest.mark.parametrize("name", ["run_select", "hana_run_select", "hana_0_export_result"])
@pytest.mark.parametrize("key,bad", [
    ("top", "customer4711"), ("top", True), ("top", 1.5), ("offset", "customer4711"),
    ("system", {"a": "customer4711"}), ("system", "x" * 40), ("system", "Erp"),
    ("system", 5),
])
def test_preview_drops_a_value_of_the_wrong_form(name: str, key: str, bad: object) -> None:
    from agents.run_activity import redact_args

    shown = redact_args(name, {"system": "erp", "sql": "SELECT 1", "top": 5, "offset": 2,
                               key: bad})
    assert key not in shown
    assert "customer4711" not in str(shown)


def test_preview_keeps_values_of_their_own_form() -> None:
    from agents.run_activity import redact_args

    assert redact_args("hana_run_select",
                       {"system": "erp_1", "sql": "SELECT 1", "top": 5, "offset": 0}) == \
        {"system": "erp_1", "sql": "<8 chars>", "top": 5, "offset": 0}


def test_other_tools_preview_unchanged() -> None:
    from agents.run_activity import redact_args

    args = {"top": "anything"}
    assert redact_args("run_check", args) is args
