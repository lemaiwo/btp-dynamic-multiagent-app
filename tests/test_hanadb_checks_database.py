"""The ``builtin:hana`` checks of ``agents/hana/checks/database.py``:
hana-cli's database-info commands (catalogue lists and the composite reads).

Per check: registered with its category, arguments and parts, every statement
tokenizes and passes the guard with ``fixed=True`` and names its columns, and
run through the real toolset on the fake driver with two scripted rows it
answers the expected shape with the expected binds. Plus the "never selected"
rules of this module: no object source, no free text, no grantor, no host, no
value of a series table or the session's own variables. The generic invariants
are the shared tests of ``tests/test_hanadb_checks.py``.
"""

from __future__ import annotations

import pytest

from agents.hana import checks as registry
from agents.hana.checks import database
from agents.hana.sqltokens import tokenize
from tests.test_hanadb_checks_monitoring import assert_registered, assert_single, run_on_fake

EXPECTED: tuple[str, ...] = (
    "status", "version", "schemas", "tables", "views", "procedures", "functions", "indexes",
    "objects", "sequences", "synonyms", "triggers", "libraries", "dependencies", "spatialData",
    "timeSeriesTools", "calcViewAnalyzer", "tableGroups",
)

FILTERED = {"top", "schema", "object"}

# name -> (args, parts)
SHAPES: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "status": ({"top"}, ("user", "session", "roles", "privileges")),
    "version": (set(), ("",)),
    "schemas": ({"top", "schema"}, ("",)),
    "tables": (FILTERED, ("",)),
    "views": (FILTERED, ("",)),
    "procedures": (FILTERED, ("",)),
    "functions": (FILTERED, ("",)),
    "indexes": (FILTERED, ("",)),
    "objects": (FILTERED, ("",)),
    "sequences": (FILTERED, ("",)),
    "synonyms": (FILTERED, ("",)),
    "triggers": (FILTERED, ("",)),
    "libraries": (FILTERED, ("",)),
    "dependencies": (FILTERED, ("uses", "used_by")),
    "spatialData": (FILTERED, ("",)),
    "timeSeriesTools": (FILTERED, ("series_tables", "series_keys")),
    "calcViewAnalyzer": (FILTERED, ("views", "per_schema")),
    "tableGroups": (FILTERED, ("groups", "tables")),
}

# What no statement of this module may select or filter on.
NEVER = {
    "DEFINITION",          # an object's source: the inspect checks' job
    "COMMENTS", "COMMENT",  # free text somebody typed
    "GRANTOR",             # a user name of who granted
    "HOST",                # a host name of the database
    "RESET_BY_QUERY",      # a query text
    "PARTITION_SPEC",
    "MIN_VALUE_OF_SERIES",
    "CACHE_FILTER",
    "CURRENT_SCHEMA",      # always SYS under the app's schema pin
}


def _words(sql: str) -> set[str]:
    return {t.value for t in tokenize(sql) if t.kind == "word"}


def test_the_database_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in database.CHECKS) == EXPECTED
    assert set(SHAPES) == set(EXPECTED)
    for check in database.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "database"
        assert check.lob_chars is None
        assert check.masked_columns == frozenset()
        assert not check.requires_select


@pytest.mark.parametrize("name", EXPECTED)
def test_registered_with_args_and_parts(name: str) -> None:
    args, parts = SHAPES[name]
    assert_registered(name, "database", args, parts=parts)


@pytest.mark.parametrize("name", EXPECTED)
def test_no_source_free_text_grantor_or_host_is_selected(name: str) -> None:
    for statement in registry.get_check(name).statements:
        words = _words(statement.sql)
        assert not words & NEVER, (name, statement.label, words & NEVER)


def _references(sql: str) -> list[tuple[str, str]]:
    """(first, second) token values after every FROM, JOIN and comma of a FROM
    list: the place a table reference stands."""
    tokens = tokenize(sql)
    refs = []
    in_from = False
    depth_of_from: list[int] = []
    depth = 0
    for index, token in enumerate(tokens):
        if token.is_symbol("("):
            depth += 1
        elif token.is_symbol(")"):
            depth -= 1
            while depth_of_from and depth_of_from[-1] > depth:
                depth_of_from.pop()
            in_from = bool(depth_of_from) and depth_of_from[-1] == depth
        opens = token.is_word("FROM", "JOIN")
        comma = token.is_symbol(",") and in_from and depth_of_from[-1] == depth
        if opens or comma:
            nxt = tokens[index + 1]
            after = tokens[index + 2] if index + 2 < len(tokens) else nxt
            refs.append((nxt.value or nxt.text, after.text))
            if opens:
                in_from = True
                depth_of_from.append(depth)
        elif token.is_word("WHERE", "GROUP", "ORDER", "HAVING", "ON"):
            if depth_of_from and depth_of_from[-1] == depth:
                depth_of_from.pop()
            in_from = False
    return refs


@pytest.mark.parametrize("sql, expected", [
    ("SELECT A FROM SYS.T1 X, SYS.T2 Y WHERE A = 1", [("SYS", "."), ("SYS", ".")]),
    ("SELECT A FROM SYS.T1 JOIN APP.T2 ON 1 = 1", [("SYS", "."), ("APP", ".")]),
    ("SELECT A, B FROM SYS.T1 ORDER BY A, B", [("SYS", ".")]),
])
def test_references_reads_from_join_and_comma_lists(sql, expected) -> None:
    assert _references(sql) == expected


@pytest.mark.parametrize("name", EXPECTED)
def test_only_catalogue_views_are_read(name: str) -> None:
    # Every FROM, JOIN and FROM-list comma names a SYS view: no application
    # table, no _SYS_BI.
    for statement in registry.get_check(name).statements:
        refs = _references(statement.sql)
        assert refs, (name, statement.label)
        for first, after in refs:
            assert (first, after) == ("SYS", "."), (name, statement.label, first)
        assert "_SYS_BI" not in statement.sql


def test_dependencies_needs_schema_and_object_and_compares_names() -> None:
    check = registry.get_check("dependencies")
    assert check.required == frozenset({"schema", "object"})
    for statement in check.statements:
        assert "LIKE" not in _words(statement.sql)
        assert statement.binds == ("top_plus_one", "schema", "object")


def test_filters_compare_exact_names() -> None:
    # The identifier rule lets no wildcard through, and LIKE would read an _
    # as "any character": every filter is an exact name.
    for check in database.CHECKS:
        for statement in check.statements:
            for token in tokenize(statement.sql):
                if token.is_word("LIKE"):
                    # the one LIKE is spatialData's fixed type pattern
                    assert check.name == "spatialData"
                    assert "LIKE 'ST\\_%' ESCAPE" in statement.sql
    for name in ("tables", "views", "procedures", "functions", "indexes", "objects",
                 "sequences", "synonyms", "libraries"):
        sql = registry.get_check(name).statements[0].sql
        assert sql.count("CAST(? AS NVARCHAR(256)) IS NULL OR") == 2
        assert sql.count(" = ?") == 2
    triggers = registry.get_check("triggers").statements[0].sql
    assert "TRIGGER_NAME = ? OR SUBJECT_TABLE_NAME = ?" in triggers


@pytest.mark.parametrize("name", ["spatialData", "objects", "indexes"])
def test_wide_scans_require_a_schema(name: str) -> None:
    check = registry.get_check(name)
    assert check.required == frozenset({"schema"})
    assert "one schema" in check.purpose


@pytest.mark.parametrize("name", ["spatialData", "objects", "indexes"])
async def test_wide_scans_refused_without_schema(monkeypatch, name: str) -> None:
    answer, executes = await run_on_fake(monkeypatch, name, ["A"], [("x",)])
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "schema"
    assert executes == []


def test_status_reads_named_session_keys_and_no_grantor() -> None:
    check = registry.get_check("status")
    session = check.statements[1].sql
    assert "CONNECTION_ID = CURRENT_CONNECTION" in session
    assert '"KEY" IN (' in session
    assert "APPLICATIONUSER" not in session
    for statement in check.statements[2:]:
        assert "GRANTEE = CURRENT_USER" in statement.sql


def test_time_series_reads_no_table_rows() -> None:
    check = registry.get_check("timeSeriesTools")
    views = {t.value for s in check.statements for t in tokenize(s.sql) if t.kind == "word"}
    assert {"SERIES_TABLES", "SERIES_KEY_COLUMNS"} <= views
    assert "TIMESERIES_TABLES" not in views
    assert not {"MIN_VALUE", "MAX_VALUE"} & views


def test_table_groups_use_the_on_prem_columns() -> None:
    words = set()
    for statement in registry.get_check("tableGroups").statements:
        words |= _words(statement.sql)
    assert {"GROUP_NAME", "GROUP_TYPE", "SUBTYPE", "IS_GROUP_LEAD"} <= words
    assert not words & {"TABLE_GROUP_NAME", "TABLE_GROUP_SCHEMA", "TABLE_GROUP_TABLES"}


def test_calc_views_only() -> None:
    for statement in registry.get_check("calcViewAnalyzer").statements:
        assert "VIEW_TYPE = 'CALC'" in statement.sql


# name -> (columns of the fake answer, run arguments, binds per statement)
NONE2 = (None, None)
RUNS: dict[str, tuple[dict, list[tuple]]] = {
    "status": ({}, [(), (), (51,), (51,)]),
    "version": ({}, [()]),
    "schemas": ({"schema": "SAPABC"}, [(51, "SAPABC", "SAPABC")]),
    "tables": ({"top": 10}, [(11, *NONE2, *NONE2)]),
    "views": ({"schema": "S", "object": "V"}, [(51, "S", "S", "V", "V")]),
    "procedures": ({}, [(51, *NONE2, *NONE2)]),
    "functions": ({}, [(51, *NONE2, *NONE2)]),
    "indexes": ({"schema": "S"}, [(51, "S", "S", *NONE2)]),
    "objects": ({"schema": "S"}, [(51, "S", "S", *NONE2)]),
    "sequences": ({}, [(51, *NONE2, *NONE2)]),
    "synonyms": ({}, [(51, *NONE2, *NONE2)]),
    "triggers": ({"object": "T"}, [(51, None, None, "T", "T", "T")]),
    "libraries": ({}, [(51, *NONE2, *NONE2)]),
    "dependencies": ({"schema": "S", "object": "O"}, [(51, "S", "O"), (51, "S", "O")]),
    "spatialData": ({"schema": "S"}, [(51, "S", "S", *NONE2)]),
    "timeSeriesTools": ({}, [(51, *NONE2, *NONE2), (51, *NONE2, *NONE2)]),
    "calcViewAnalyzer": ({}, [(51, *NONE2, *NONE2), (51, *NONE2)]),
    "tableGroups": ({}, [(51, *NONE2, *NONE2), (51, *NONE2, *NONE2)]),
}


@pytest.mark.parametrize("name", EXPECTED)
async def test_runs_on_the_fake_driver(monkeypatch, name: str) -> None:
    args, binds = RUNS[name]
    columns = ["SCHEMA_NAME", "NAME_OF_IT"]
    rows = [("S1", "A"), ("S2", "B")]
    answer, executes = await run_on_fake(monkeypatch, name, columns, rows, **args)
    check = registry.get_check(name)
    if check.composite:
        assert "error" not in answer, answer
        assert [p["label"] for p in answer["parts"]] == [s.label for s in check.statements]
        for part in answer["parts"]:
            assert part["columns"] == columns and part["row_count"] == 2
    else:
        assert_single(answer, columns)
    assert [tuple(e[2]) if len(e) > 2 else () for e in executes] == binds


async def test_dependencies_refused_without_object(monkeypatch) -> None:
    answer, executes = await run_on_fake(monkeypatch, "dependencies", ["A"], [("x",)],
                                         schema="S")
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "object"
    assert executes == []
