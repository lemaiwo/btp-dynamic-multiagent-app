"""The ``builtin:hana`` toolset on a fake hdbcli.

Dummy checks are registered into an empty registry per test (no real check
exists yet). The connection layer runs for real against the fake driver of
``tests/hanadb_helpers.py``; every error text of the fake holds the host,
the user and a marker, none of which may reach an answer or a log record.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any

import pytest

from agents.hana import tools
from agents.hana.checks import base
from agents.hana.checks.base import Check, Statement, register
from agents.hana.connection import HanaSystem
from agents.hana.errors import MESSAGES, HanaError
from tests import hanadb_helpers as h

SECRETS = (h.MARKER, h.HOST, h.USER, h.PASSWORD, h.TOKEN, "hana-dest-one", "hana-dest-two")
STATEMENT = "SELECT * FROM SAPABAP1.KNA1 WHERE KUNNR = 'customer4711' AND X = 99"

OAUTH = {"systems": [{"name": "erp_tenant", "destination": "hana-dest-one"},
                     {"name": "erp_systemdb", "destination": "hana-dest-two"}]}
OAUTH_SELECT = {**OAUTH, "allow_select": True}


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})
    register(
        Check(name="alerts", category="monitoring", purpose="Current alerts.",
              source="bin/alerts.js",
              statements=(Statement("", "SELECT A, B FROM SYS.M_ALERTS", ()),)),
        Check(name="longRunning", category="performance", purpose="Long statements.",
              source="bin/longRunning.js",
              statements=(Statement(
                  "", "SELECT A, STATEMENT_STRING FROM SYS.M_X "
                      "WHERE (? IS NULL OR SCHEMA_NAME = ?) AND H < ?",
                  ("schema", "schema", "hours")),),
              args=frozenset({"top", "schema", "hours"}),
              masked_columns=frozenset({"STATEMENT_STRING"})),
        Check(name="traceContents", category="system", purpose="Trace file contents.",
              source="bin/traceContents.js",
              statements=(Statement("", "SELECT A FROM SYS.M_TRACEFILE_CONTENTS", ()),),
              requires_select=True),
        Check(name="backups", category="backup", purpose="Backup catalog.",
              source="bin/backups.js",
              statements=(Statement("catalog", "SELECT A FROM SYS.M_BACKUP_CATALOG", ()),
                          Statement("size", "SELECT B FROM SYS.M_BACKUP_SIZE", ()))),
        Check(name="version", category="system", purpose="Database version.",
              source="bin/version.js",
              statements=(Statement("", "SELECT A FROM SYS.M_DATABASE", ()),),
              args=frozenset(), max_rows_cap=5),
    )


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> h.FakeDbapi:
    return h.install(monkeypatch)


def _factory(name: str, destination: str) -> HanaSystem:
    return HanaSystem(name, destination, resolver=h.FakeResolver(), tokens=h.FakeTokens())


def build(oauth: dict | None = None, **kw: Any):
    return tools.hana_toolset(oauth or OAUTH, auth_mode="destination",
                              system_factory=kw.pop("system_factory", _factory), **kw)


async def call(toolset, name: str, /, **kw: Any) -> dict[str, Any]:
    return await toolset.tools[name].function(**kw)


def _clean(answer: Any, *extra: str) -> None:
    text = json.dumps(answer)
    for secret in SECRETS + extra:
        assert secret not in text


def _logs_clean(caplog: pytest.LogCaptureFixture, *extra: str) -> None:
    for record in caplog.records:
        for secret in SECRETS + extra:
            assert secret not in record.getMessage()
        assert record.exc_info is None


# ---- build ------------------------------------------------------------------------------


def test_build_refuses_another_auth_mode() -> None:
    with pytest.raises(ValueError, match="requires auth_mode=destination"):
        tools.hana_toolset(OAUTH, auth_mode="oauth2")


def test_build_refuses_a_bad_entry() -> None:
    with pytest.raises(ValueError):
        tools.hana_toolset({"systems": []}, auth_mode="destination")
    with pytest.raises(ValueError):
        tools.hana_toolset({**OAUTH, "user_context": True}, auth_mode="destination")


def test_run_select_is_registered_only_with_allow_select() -> None:
    assert set(build().tools) == {"list_systems", "list_checks", "run_check"}
    assert set(build(OAUTH_SELECT).tools) == {
        "list_systems", "list_checks", "run_check", "run_select"}


def test_descriptions_state_defaults_categories_and_select_rules() -> None:
    run_check = build().tools["run_check"].description
    assert "50" in run_check and "24" in run_check
    for category in ("monitoring", "performance", "system", "backup"):
        assert category in run_check
    for name in ("alerts", "longRunning", "backups"):
        assert name not in run_check  # categories, not the checks
    select = build(OAUTH_SELECT).tools["run_select"].description
    for words in ("SYS.<view>", "_SYS_STATISTICS.<view>", "M_*", "SYS.DUMMY",
                  "comment", ";", "`INIFILE`", "`CONFIGURATION_PARAMETER`",
                  "SYS.P_*"):
        assert words in select


# ---- list_systems / list_checks ---------------------------------------------------------


async def test_list_systems_names_only() -> None:
    answer = await call(build(), "list_systems")
    assert answer == {"systems": [{"name": "erp_tenant"}, {"name": "erp_systemdb"}]}
    _clean(answer)


async def test_list_checks_without_allow_select_hides_select_checks() -> None:
    answer = await call(build(), "list_checks")
    names = [c["name"] for c in answer["checks"]]
    assert names == ["alerts", "longRunning", "backups", "version"]
    long_running = answer["checks"][1]
    assert long_running["category"] == "performance"
    assert long_running["purpose"] == "Long statements."
    assert [a["name"] for a in long_running["arguments"]] == ["schema", "top", "hours"]
    top = next(a for a in long_running["arguments"] if a["name"] == "top")
    assert top["default"] == 50 and "500" in top["rule"]
    assert answer["checks"][3]["arguments"] == []


async def test_list_checks_with_allow_select_shows_them() -> None:
    answer = await call(build(OAUTH_SELECT), "list_checks")
    assert "traceContents" in [c["name"] for c in answer["checks"]]


async def test_list_checks_by_category() -> None:
    answer = await call(build(), "list_checks", category="system")
    assert [c["name"] for c in answer["checks"]] == ["version"]


async def test_list_checks_refuses_unknown_category_without_echo() -> None:
    answer = await call(build(), "list_checks", category="evil_category_x")
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "category"
    _clean(answer, "evil_category_x")


# ---- run_check ----------------------------------------------------------------------------


async def test_run_check_ok(db: h.FakeDbapi) -> None:
    db.rows = [(1, "x"), (2, "y")]
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts")
    assert answer == {"system": "erp_tenant", "check": "alerts", "columns": ["A", "B"],
                      "rows": [[1, "x"], [2, "y"]], "row_count": 2, "truncated": False}
    assert ("fetchmany", 51) in db.calls  # default top 50, one more fetched
    assert "commit" not in db.steps()


async def test_run_check_top_plus_one_and_truncated_exact(db: h.FakeDbapi) -> None:
    db.rows = [(i, "r") for i in range(5)]
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts", top=3)
    assert ("fetchmany", 4) in db.calls
    assert answer["row_count"] == 3 and answer["truncated"] is True
    db.rows = [(i, "r") for i in range(3)]
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts", top=3)
    assert answer["row_count"] == 3 and answer["truncated"] is False


async def test_run_check_without_top_uses_the_check_cap(db: h.FakeDbapi) -> None:
    db.description = (("A",),)
    db.rows = [(1,)]
    answer = await call(build(), "run_check", system="erp_tenant", check="version")
    assert answer["rows"] == [[1]]
    assert ("fetchmany", 6) in db.calls


async def test_run_check_binds_defaults_and_masks(db: h.FakeDbapi) -> None:
    db.description = (("A",), ("STATEMENT_STRING",))
    db.rows = [(1, STATEMENT), (2, None)]
    answer = await call(build(), "run_check", system="erp_tenant", check="longRunning")
    executed = [c for c in db.calls if c[0] == "execute"][0]
    assert executed[2] == (None, None, 24)
    masked = answer["rows"][0][1]
    assert "customer4711" not in masked and "99" not in masked and "KNA1" in masked
    assert answer["rows"][1][1] is None


async def test_run_check_masks_a_statement_text_that_arrives_as_a_lob(db: h.FakeDbapi) -> None:
    # STATEMENT_STRING is NCLOB: hdbcli hands it back as a LOB object.
    db.description = (("A",), ("STATEMENT_STRING",))
    db.rows = [(1, h.FakeLob(STATEMENT, db=db))]
    answer = await call(build(), "run_check", system="erp_tenant", check="longRunning")
    masked = answer["rows"][0][1]
    assert isinstance(masked, str) and "KNA1" in masked
    assert "customer4711" not in masked and "99" not in masked
    assert not masked.startswith("<")


async def test_run_check_binds_given_arguments(db: h.FakeDbapi) -> None:
    db.description = (("A",), ("STATEMENT_STRING",))
    await call(build(), "run_check", system="erp_tenant", check="longRunning",
               schema="SAPABAP1", hours=2, top=10)
    executed = [c for c in db.calls if c[0] == "execute"][0]
    assert executed[2] == ("SAPABAP1", "SAPABAP1", 2)
    assert ("fetchmany", 11) in db.calls


async def test_run_check_composite(db: h.FakeDbapi) -> None:
    db.rows = [(1, "a")]
    answer = await call(build(), "run_check", system="erp_tenant", check="backups")
    assert answer["system"] == "erp_tenant" and answer["check"] == "backups"
    assert [p["label"] for p in answer["parts"]] == ["catalog", "size"]
    assert all(p["row_count"] == 1 for p in answer["parts"])


async def test_unknown_system_not_echoed(db: h.FakeDbapi) -> None:
    for name in ("evil_system_x", 42, None):
        answer = await call(build(), "run_check", system=name, check="alerts")
        assert answer == {"error": {"code": "unknown_system",
                                    "message": MESSAGES["unknown_system"]}}
        _clean(answer, "evil_system_x")
    assert db.calls == []


async def test_unknown_check_not_echoed(db: h.FakeDbapi) -> None:
    for name in ("evil_check_x", "traceContents", 7):
        answer = await call(build(), "run_check", system="erp_tenant", check=name)
        assert answer == {"error": {"code": "unknown_check",
                                    "message": MESSAGES["unknown_check"]}}
        _clean(answer, "evil_check_x")
    assert db.calls == []


async def test_select_check_runs_with_allow_select(db: h.FakeDbapi) -> None:
    db.description = (("A",),)
    db.rows = [(1,)]
    answer = await call(build(OAUTH_SELECT), "run_check", system="erp_tenant",
                        check="traceContents")
    assert answer["rows"] == [[1]]


@pytest.mark.parametrize("kw, argument", [
    ({"top": 0}, "top"), ({"top": 501}, "top"), ({"top": True}, "top"),
    ({"top": "5"}, "top"), ({"hours": 0}, "hours"), ({"hours": 721}, "hours"),
    ({"schema": "evil schema'x"}, "schema"), ({"schema": 5}, "schema"),
    ({"object": "SYS"}, "object"),
])
async def test_run_check_bad_arguments(db: h.FakeDbapi, kw: dict, argument: str) -> None:
    answer = await call(build(), "run_check", system="erp_tenant", check="longRunning", **kw)
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == argument
    _clean(answer, "evil schema'x")
    assert db.calls == []


async def test_run_check_passes_host_as_a_bind(db: h.FakeDbapi) -> None:
    register(Check(name="traceTail", category="monitoring", purpose="Trace tail.",
                   source="bin/traceTail.js",
                   statements=(Statement("", "SELECT A FROM SYS.M_TRACEFILE_CONTENTS "
                                             "WHERE HOST = ? AND FILE_NAME = ?",
                                         ("host", "object")),),
                   args=frozenset({"top", "host", "object"}),
                   required=frozenset({"host", "object"})))
    db.description = (("A",),)
    db.rows = [(1,)]
    await call(build(), "run_check", system="erp_tenant", check="traceTail",
               host="h1", object="f.trc")
    executed = [c for c in db.calls if c[0] == "execute"][0]
    assert executed[2] == ("h1", "f.trc")
    answer = await call(build(), "run_check", system="erp_tenant", check="traceTail",
                        host="evil host'x", object="f.trc")
    assert answer["error"]["argument"] == "host"
    _clean(answer, "evil host'x")
    answer = await call(build(), "list_checks", category="monitoring")
    tail = next(c for c in answer["checks"] if c["name"] == "traceTail")
    by_name = {a["name"]: a for a in tail["arguments"]}
    assert by_name["host"] == {"name": "host", "required": True,
                               "rule": "a host name (letters, digits, . _ -)"}


async def test_run_check_refuses_host_on_a_check_without_it(db: h.FakeDbapi) -> None:
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts", host="h1")
    assert answer["error"]["argument"] == "host"
    assert db.calls == []


def test_run_check_doc_describes_host() -> None:
    doc = tools._run_check_doc(True)
    assert "host: " in doc and "`traces`" in doc


async def test_run_check_undeclared_argument(db: h.FakeDbapi) -> None:
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts", hours=3)
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "hours"


async def test_values_made_json_safe(db: h.FakeDbapi) -> None:
    class Odd:
        def __repr__(self) -> str:
            return h.MARKER

    db.description = tuple((c,) for c in "ABCDEFGHIJK")
    db.rows = [(b"\x00\x01\x02", bytearray(b"ab"), memoryview(b"abcd"),
                dt.datetime(2026, 11, 10, 8, 30, 1), dt.date(2026, 11, 10),
                dt.time(8, 30), Decimal("12.50"), Odd(), 1.5, True, "text")]
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts")
    assert answer["rows"][0] == ["<3 bytes>", "<2 bytes>", "<4 bytes>",
                                 "2026-11-10T08:30:01", "2026-11-10", "08:30:00",
                                 "12.50", "<Odd>", 1.5, True, "text"]
    json.dumps(answer, allow_nan=False)
    _clean(answer)


async def test_result_too_large_refused(db: h.FakeDbapi) -> None:
    db.rows = [(i, "y" * 1000) for i in range(70)]
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts", top=100)
    assert answer == {"error": {"code": "result_too_large",
                                "message": MESSAGES["result_too_large"]}}


async def test_driver_failure_is_an_error_object(db: h.FakeDbapi,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    db.fail_at["execute"] = h.driver_error(258, "insufficient privilege")
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts")
    assert answer["error"]["code"] == "not_authorized"
    assert set(answer["error"]) <= {"code", "message", "sql_code"}
    _clean(answer)
    _logs_clean(caplog)


async def test_destination_failure(caplog: pytest.LogCaptureFixture,
                                   db: h.FakeDbapi) -> None:
    caplog.set_level(logging.DEBUG)

    def factory(name: str, destination: str) -> HanaSystem:
        return HanaSystem(name, destination,
                          resolver=h.FakeResolver(error=RuntimeError(h.MARKER)),
                          tokens=h.FakeTokens())

    answer = await call(build(system_factory=factory), "run_check",
                        system="erp_tenant", check="alerts")
    assert answer["error"]["code"] == "destination_error"
    _clean(answer)
    _logs_clean(caplog)


async def test_unexpected_failure_is_answered(monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    async def boom(*a: Any, **k: Any):
        raise RuntimeError(f"{h.MARKER} {h.HOST}")

    monkeypatch.setattr(tools, "run_query", boom)
    answer = await call(build(OAUTH_SELECT), "run_check", system="erp_tenant", check="alerts")
    assert answer == {"error": HanaError("call_failed").to_dict()}
    assert answer["error"]["message"] == (
        "The call could not be completed. Tell the user instead of retrying.")
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT * FROM SYS.DUMMY")
    assert answer["error"]["code"] == "call_failed"
    _clean(answer)
    _logs_clean(caplog)


# ---- run_select ---------------------------------------------------------------------------


async def test_run_select_ok_unmasked(db: h.FakeDbapi) -> None:
    db.description = (("STATEMENT_STRING",),)
    db.rows = [(STATEMENT,)]
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT STATEMENT_STRING FROM M_EXPENSIVE_STATEMENTS")
    assert answer == {"system": "erp_tenant", "columns": ["STATEMENT_STRING"],
                      "rows": [[STATEMENT]], "row_count": 1, "truncated": False}
    assert ("fetchmany", 101) in db.calls
    executed = [c for c in db.calls if c[0] == "execute"]
    assert executed == [("execute", "SELECT STATEMENT_STRING FROM M_EXPENSIVE_STATEMENTS")]


async def test_run_select_top(db: h.FakeDbapi) -> None:
    db.rows = [(i, i) for i in range(4)]
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT * FROM SYS.DUMMY", top=2)
    assert answer["row_count"] == 2 and answer["truncated"] is True
    assert ("fetchmany", 3) in db.calls


@pytest.mark.parametrize("top", [0, 501, True, "5", 2.0])
async def test_run_select_bad_top(db: h.FakeDbapi, top: Any) -> None:
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT * FROM SYS.DUMMY", top=top)
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "top"
    assert db.calls == []


@pytest.mark.parametrize("sql, rule", [
    ("DELETE FROM SAPABAP1.KNA1 WHERE KUNNR = 'customer4711'", "select_only"),
    ("SELECT * FROM SAPABAP1.KNA1 WHERE KUNNR = 'customer4711'", "table_not_allowed"),
    ("SELECT * FROM DUMMY WHERE 'customer4711' = ''", "table_not_allowed"),
    ("SELECT 1 FROM SYS.DUMMY; SELECT 'customer4711' FROM SYS.DUMMY", "one_statement"),
    (12, "unreadable"),
])
async def test_run_select_guard_refusal(db: h.FakeDbapi, sql: Any, rule: str) -> None:
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant", sql=sql)
    assert answer["error"]["code"] == "select_refused"
    assert answer["error"]["rule"] == rule
    _clean(answer, "customer4711", "KNA1")
    assert db.calls == []


@pytest.mark.parametrize("sql", [
    'SELECT "KEY", "VALUE" FROM M_INIFILE_CONTENTS',
    "SELECT * FROM SYS.M_CONFIGURATION_PARAMETER_VALUES",
    "SELECT * FROM SYS.P_USERS_",
    'SELECT "KEY", "VALUE" FROM SYS.M_INIFILE_CONTENT_HISTORY',
    "SELECT * FROM M_INIFILES",
])
async def test_run_select_refuses_the_deny_list(db: h.FakeDbapi, sql: str) -> None:
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant", sql=sql)
    assert answer["error"]["code"] == "select_refused"
    assert answer["error"]["rule"] == "table_not_allowed"
    assert db.calls == []
    _clean(answer, "INIFILE", "P_USERS_", "CONFIGURATION")


async def test_run_select_unknown_system(db: h.FakeDbapi) -> None:
    answer = await call(build(OAUTH_SELECT), "run_select", system="evil_system_x",
                        sql="SELECT * FROM SYS.DUMMY")
    assert answer["error"]["code"] == "unknown_system"
    _clean(answer, "evil_system_x")


async def test_run_select_result_too_large(db: h.FakeDbapi) -> None:
    db.rows = [("y" * 1000, i) for i in range(70)]
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT * FROM SYS.DUMMY", top=500)
    assert answer["error"]["code"] == "result_too_large"


# ---- review minors ------------------------------------------------------------------------


async def test_column_names_are_cut_and_carry_no_control_or_format_character(
        db: h.FakeDbapi) -> None:
    db.description = (("A\nB\u202eC\u2028D\u00adE",), ("N" * 300,), (7,))
    db.rows = [(1, 2, 3)]
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT * FROM SYS.DUMMY")
    assert answer["columns"] == ["A?B?C?D?E", "N" * 128, "7"]


async def test_run_check_column_names_are_cleaned_too(db: h.FakeDbapi) -> None:
    db.description = (("A\tB",), ("B",))
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts")
    assert answer["columns"] == ["A?B", "B"]


async def test_list_checks_marks_a_required_argument_and_gives_it_no_default() -> None:
    register(Check(name="columnStats", category="performance", purpose="Columns.",
                   source="bin/columnStats.js",
                   statements=(Statement("", "SELECT A FROM SYS.M_CS_COLUMNS "
                                             "WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
                                         ("schema", "object")),),
                   args=frozenset({"schema", "object", "top"}),
                   required=frozenset({"schema", "object"})))
    answer = await call(build(), "list_checks", category="performance")
    stats = next(c for c in answer["checks"] if c["name"] == "columnStats")
    by_name = {a["name"]: a for a in stats["arguments"]}
    assert by_name["schema"]["required"] is True and "default" not in by_name["schema"]
    assert by_name["object"]["required"] is True and "default" not in by_name["object"]
    assert "required" not in by_name["top"] and by_name["top"]["default"] == 50
    long_running = next(c for c in answer["checks"] if c["name"] == "longRunning")
    assert all("required" not in a and "default" in a for a in long_running["arguments"])


def test_the_result_measure_is_the_neutral_module() -> None:
    import ast
    import pathlib

    from agents import sharepoint_tools, tool_results

    assert tools.result_chars is tool_results.result_chars
    assert sharepoint_tools._result_chars is tool_results.result_chars
    tree = ast.parse(pathlib.Path(tools.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
    assert "agents.sharepoint_tools" not in imported


# ---- final review round 2: the LOB budget -------------------------------------------------


async def test_run_select_stops_reading_lobs_at_the_budget(db: h.FakeDbapi) -> None:
    db.description = (("STATEMENT_STRING",),)
    lobs = [h.FakeLob("s" * 50_000, db=db) for _ in range(5)]
    db.rows = [(lob,) for lob in lobs]
    answer = await call(build(OAUTH_SELECT), "run_select", system="erp_tenant",
                        sql="SELECT STATEMENT_STRING FROM M_EXPENSIVE_STATEMENTS", top=500)
    assert answer["error"]["code"] == "result_too_large"
    assert sum(len(lob.reads) for lob in lobs) == 2
    assert lobs[0].reads == [tools.MAX_RESULT_CHARS + 2]


async def test_run_check_reads_a_statement_lob_only_up_to_its_per_value_cap(
        db: h.FakeDbapi) -> None:
    db.description = (("A",), ("STATEMENT_STRING",))
    lob = h.FakeLob(STATEMENT + " " * 20_000, db=db)
    db.rows = [(1, lob)]
    answer = await call(build(), "run_check", system="erp_tenant", check="longRunning")
    assert lob.reads == [tools.CHECK_LOB_CHARS + 1]
    assert tools.CHECK_LOB_CHARS == 4096
    masked = answer["rows"][0][1]
    assert "KNA1" in masked and "customer4711" not in masked


# ---- Phases 2 and 3: other_schema, the per-check LOB cap, truncated_columns -------------

PAIR = Check(name="schemaPair", category="schema", purpose="Two schemas.",
             source="bin/schemaPair.js",
             statements=(Statement("", "SELECT A FROM SYS.TABLES "
                                       "WHERE SCHEMA_NAME = ? OR SCHEMA_NAME = ?",
                                   ("schema", "other_schema")),),
             args=frozenset({"top", "schema", "other_schema"}),
             required=frozenset({"other_schema"}))

SOURCE = Check(name="viewSource", category="database", purpose="A view definition.",
               source="bin/viewSource.js",
               statements=(Statement("", "SELECT VIEW_NAME, DEFINITION FROM SYS.VIEWS", ()),),
               lob_chars=20_000)

SOURCE_PARTS = Check(name="procSource", category="database", purpose="Procedure parts.",
                     source="bin/procSource.js",
                     statements=(Statement("head", "SELECT A, B FROM SYS.PROCEDURES", ()),
                                 Statement("body", "SELECT C FROM SYS.PROCEDURES", ())))


async def test_run_check_binds_other_schema(db: h.FakeDbapi) -> None:
    register(PAIR)
    db.description = (("A",),)
    db.rows = [(1,)]
    await call(build(), "run_check", system="erp_tenant", check="schemaPair",
               schema="S1", other_schema="S2")
    executed = [c for c in db.calls if c[0] == "execute"][0]
    assert executed[2] == ("S1", "S2")
    answer = await call(build(), "run_check", system="erp_tenant", check="schemaPair",
                        schema="S1", other_schema="evil schema'x")
    assert answer["error"]["argument"] == "other_schema"
    _clean(answer, "evil schema'x")
    answer = await call(build(), "run_check", system="erp_tenant", check="alerts",
                        other_schema="S2")
    assert answer["error"]["argument"] == "other_schema"
    assert answer["error"]["rule"] == "not an argument of this check"


async def test_list_checks_shows_other_schema() -> None:
    register(PAIR)
    answer = await call(build(), "list_checks", category="schema")
    pair = next(c for c in answer["checks"] if c["name"] == "schemaPair")
    assert [a["name"] for a in pair["arguments"]] == ["schema", "top", "other_schema"]
    by_name = {a["name"]: a for a in pair["arguments"]}
    assert by_name["other_schema"] == {
        "name": "other_schema", "required": True,
        "rule": "an identifier (letters, digits, _ $ # . /)"}
    assert by_name["schema"]["default"] is None


def test_run_check_doc_names_other_schema_and_truncated_columns() -> None:
    doc = tools._run_check_doc(False)
    assert "other_schema: " in doc and "truncated_columns" in doc
    run_check = build().tools["run_check"]
    assert "other_schema" in run_check.function_schema.json_schema["properties"]


def test_the_tool_docs_describe_every_phase_not_only_monitoring() -> None:
    """Phases 2 and 3 added catalogue, schema and security checks: the docs
    speak of checks, and ``object`` names what those checks take."""
    toolset = build()
    for doc in (tools._run_check_doc(False), tools._run_check_doc(True),
                toolset.tools["run_check"].description,
                toolset.tools["list_checks"].description):
        assert "monitoring check" not in doc
    doc = tools._run_check_doc(False)
    assert "Run one registered check" in doc
    for kind in ("table", "view", "index", "procedure", "library", "user", "role"):
        assert kind in doc.split("object: ", 1)[1].split("top: ", 1)[0], kind
    assert "List the checks `run_check` can run" in toolset.tools["list_checks"].description


async def test_run_check_reads_a_lob_up_to_the_checks_own_cap(db: h.FakeDbapi) -> None:
    register(SOURCE)
    lob = h.FakeLob("d" * 30_000, db=db)
    db.description = (("VIEW_NAME",), ("DEFINITION",))
    db.rows = [("V1", lob)]
    answer = await call(build(), "run_check", system="erp_tenant", check="viewSource")
    assert lob.reads == [20_001]
    assert answer["rows"][0][1].startswith("d" * 20_000)
    assert answer["truncated_columns"] == ["DEFINITION"]


async def test_no_truncated_columns_when_nothing_was_cut(db: h.FakeDbapi) -> None:
    register(SOURCE)
    db.description = (("VIEW_NAME",), ("DEFINITION",))
    db.rows = [("V1", h.FakeLob("short", db=db))]
    answer = await call(build(), "run_check", system="erp_tenant", check="viewSource")
    assert "truncated_columns" not in answer
    assert answer["rows"] == [["V1", "short"]]


async def test_truncated_columns_per_part_sorted(db: h.FakeDbapi) -> None:
    register(SOURCE_PARTS)
    db.description = (("B",), ("A",))
    db.rows = [(h.FakeLob("b" * 5000, db=db), h.FakeLob("a" * 5000, db=db))]
    answer = await call(build(), "run_check", system="erp_tenant", check="procSource")
    assert [p.get("truncated_columns") for p in answer["parts"]] == [["A", "B"], ["A", "B"]]
    db.rows = [(h.FakeLob("b", db=db), h.FakeLob("a", db=db))]
    answer = await call(build(), "run_check", system="erp_tenant", check="procSource")
    assert all("truncated_columns" not in p for p in answer["parts"])


async def test_masked_lob_cut_is_named_too(db: h.FakeDbapi) -> None:
    # A statement text read past the default cap is cut before it is masked;
    # the column is named although the masking drops the marker.
    lob = h.FakeLob(STATEMENT + " " * 5000 + "x", db=db)
    db.description = (("A",), ("STATEMENT_STRING",))
    db.rows = [(1, lob)]
    answer = await call(build(), "run_check", system="erp_tenant", check="longRunning")
    assert answer["truncated_columns"] == ["STATEMENT_STRING"]
