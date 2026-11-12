"""The ``builtin:hana`` checks of ``agents/hana/checks/schema.py``:
the schema checks of Phase 3.

This file pins the module's check names; the invariants every check meets
(valid, a known category, the guard passes with ``fixed=True``, names
unique) are the shared tests of ``tests/test_hanadb_checks.py``. Add a
check: add its name here, in registration order.

Per check: registered with its category, arguments and required ones, its
statements read only catalogue views, name their columns, and run on the
fake driver with two scripted rows they answer the expected shape and binds.
"""

from __future__ import annotations

from agents.hana import checks as registry
from agents.hana.checks import schema
from agents.hana.sqltokens import tokenize
from tests.test_hanadb_checks_monitoring import assert_registered, assert_single, run_on_fake

EXPECTED: tuple[str, ...] = ("compareSchema", "erdDiagram", "graphWorkspaces")

COMPARE_PARTS = ("tables_only_in_one", "column_differences", "index_differences",
                 "trigger_differences")
ERD_PARTS = ("tables", "columns", "relations")

# Free text, object definitions and default values: never selected. Rows of
# user tables are never read (only SYS catalogue views are referenced).
FORBIDDEN_COLUMNS = {"COMMENTS", "COMMENT", "DEFINITION", "DEFAULT_VALUE", "SQL_SECURITY",
                     "PASSWORD", "CREDENTIAL", "CONNECTION_INFO"}
ALLOWED_VIEWS = {"SYS.TABLES", "SYS.TABLE_COLUMNS", "SYS.INDEX_COLUMNS", "SYS.TRIGGERS",
                 "SYS.CONSTRAINTS", "SYS.REFERENTIAL_CONSTRAINTS", "SYS.GRAPH_WORKSPACES"}


def test_the_schema_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in schema.CHECKS) == EXPECTED
    for check in schema.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "schema"


def _words(sql: str) -> set[str]:
    return {t.value for t in tokenize(sql) if t.kind == "word"}


def _references(sql: str) -> set[str]:
    tokens = tokenize(sql)
    refs = set()
    for i, token in enumerate(tokens[:-2]):
        if token.is_word("SYS") and tokens[i + 1].is_symbol("."):
            refs.add(f"SYS.{tokens[i + 2].value}")
    return refs


def test_no_free_text_definition_or_secret_column_is_selected() -> None:
    for check in schema.CHECKS:
        for statement in check.statements:
            assert not _words(statement.sql) & FORBIDDEN_COLUMNS, (check.name, statement.label)


def test_only_catalogue_views_are_read() -> None:
    for check in schema.CHECKS:
        for statement in check.statements:
            refs = _references(statement.sql)
            assert refs and refs <= ALLOWED_VIEWS, (check.name, refs)
            assert "_SYS_STATISTICS" not in statement.sql


def test_purposes_are_short() -> None:
    for check in schema.CHECKS:
        assert len(check.purpose) <= 120, check.name


def test_compare_schema_is_one_full_outer_join_per_part() -> None:
    check = registry.get_check("compareSchema")
    assert check.required == frozenset({"schema", "other_schema"})
    for statement in check.statements:
        assert "FULL OUTER JOIN" in statement.sql
        assert statement.binds[0] == "top_plus_one"
        assert {"schema", "other_schema"} <= set(statement.binds)


def test_compare_schema_columns_only_of_tables_in_both() -> None:
    # A table only in one schema is reported once in tables_only_in_one; its
    # columns, indexes and triggers would only repeat that.
    check = registry.get_check("compareSchema")
    for statement in check.statements[1:]:
        assert statement.sql.count("TABLE_NAME IN (SELECT TABLE_NAME FROM SYS.TABLES") == 2


async def test_compare_schema(monkeypatch) -> None:
    assert_registered("compareSchema", "schema", {"top", "schema", "other_schema", "object"},
                      parts=COMPARE_PARTS)
    columns = ["ONLY_IN", "TABLE_NAME"]
    rows = [("schema", "T1"), ("other_schema", "T2")]
    answer, executes = await run_on_fake(monkeypatch, "compareSchema", columns, rows,
                                         schema="APP_A", other_schema="APP_B", top=10)
    assert "error" not in answer, answer
    assert [p["label"] for p in answer["parts"]] == list(COMPARE_PARTS)
    for part in answer["parts"]:
        assert part["columns"] == columns and part["row_count"] == 2
    binds = [e[2] for e in executes]
    assert binds[0] == (11, "APP_A", "APP_B")
    for later in binds[1:]:
        assert later == (11, "APP_A", "APP_B", None, None, "APP_B", "APP_A", None, None)


async def test_compare_schema_one_table(monkeypatch) -> None:
    # A large schema is compared one table at a time: `object` narrows both
    # sides of parts 2-4; part 1 still lists the tables of one side only.
    _, executes = await run_on_fake(monkeypatch, "compareSchema", ["A"], [("x",)],
                                    schema="APP_A", other_schema="APP_B", object="ORDERS")
    binds = [e[2] for e in executes]
    assert binds[0] == (51, "APP_A", "APP_B")
    for later in binds[1:]:
        assert later == (51, "APP_A", "APP_B", "ORDERS", "ORDERS",
                         "APP_B", "APP_A", "ORDERS", "ORDERS")
    assert "object" in registry.get_check("compareSchema").purpose


def test_compare_schema_leaves_out_system_index_names() -> None:
    # `_SYS_TREE_...` names are generated per schema: they would all differ.
    sql = registry.get_check("compareSchema").statements[2].sql
    assert sql.count("LEFT(INDEX_NAME, 4) <> '_SYS'") == 2


async def test_compare_schema_needs_both_schemas(monkeypatch) -> None:
    answer, executes = await run_on_fake(monkeypatch, "compareSchema", ["A"], [("x",)],
                                         schema="APP_A")
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "other_schema"
    assert executes == []


async def test_erd_diagram(monkeypatch) -> None:
    check = assert_registered("erdDiagram", "schema", {"top", "schema", "object"},
                              parts=ERD_PARTS)
    assert check.required == frozenset({"schema"})
    assert "for a diagram the caller draws" in check.purpose
    relations = check.statements[2].sql
    assert {"UPDATE_RULE", "DELETE_RULE", "REFERENCED_TABLE_NAME"} <= _words(relations)
    columns = ["TABLE_NAME", "COLUMN_NAME"]
    rows = [("ORDERS", "ID"), ("ITEMS", "ORDER_ID")]
    answer, executes = await run_on_fake(monkeypatch, "erdDiagram", columns, rows,
                                         schema="APP_A", top=5)
    assert "error" not in answer, answer
    assert [p["label"] for p in answer["parts"]] == list(ERD_PARTS)
    for part in answer["parts"]:
        assert part["columns"] == columns and part["row_count"] == 2
    binds = [e[2] for e in executes]
    assert binds == [(6, "APP_A", None, None),
                     (6, "APP_A", "APP_A", None, None),
                     (6, "APP_A", None, None, "APP_A", None)]


async def test_erd_diagram_one_table(monkeypatch) -> None:
    _, executes = await run_on_fake(monkeypatch, "erdDiagram", ["TABLE_NAME"], [("T",)],
                                    schema="APP_A", object="ORDERS")
    binds = [e[2] for e in executes]
    assert binds[2] == (51, "APP_A", "ORDERS", "ORDERS", "APP_A", "ORDERS")


def test_erd_relations_reference_the_table_of_this_schema() -> None:
    # A same-named table in another schema is not "the" table.
    check = registry.get_check("erdDiagram")
    sql = check.statements[2].sql
    assert "OR (REFERENCED_SCHEMA_NAME = ? AND REFERENCED_TABLE_NAME = ?)" in sql
    assert "owned by" in check.purpose


async def test_graph_workspaces(monkeypatch) -> None:
    check = assert_registered("graphWorkspaces", "schema", {"top", "schema"})
    assert check.required == frozenset()
    assert {"EDGE_TABLE_NAME", "VERTEX_TABLE_NAME"} <= _words(check.statements[0].sql)
    columns = ["SCHEMA_NAME", "WORKSPACE_NAME", "EDGE_TABLE_NAME", "VERTEX_TABLE_NAME"]
    rows = [("APP_A", "G1", "E1", "V1"), ("APP_B", "G2", "E2", "V2")]
    answer, executes = await run_on_fake(monkeypatch, "graphWorkspaces", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51, None, None)
    _, executes = await run_on_fake(monkeypatch, "graphWorkspaces", columns, rows,
                                    schema="APP_A")
    assert executes[0][2] == (51, "APP_A", "APP_A")
