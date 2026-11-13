"""The ``builtin:hana`` checks of ``agents/hana/checks/inspection.py``:
the eight ``inspect*`` checks of Phase 2 (category ``database``).

This file pins the module's check names; the invariants every check meets
(valid, a known category, the guard passes with ``fixed=True``, names
unique) are the shared tests of ``tests/test_hanadb_checks.py``. Per check:
registered with its parts, ``schema`` and ``object`` required, the SQL reads
only ``SYS`` catalogue views (never the inspected object itself), no free-text
comment column, and run on the fake driver with two scripted rows it answers
one part per statement with the expected binds.
"""

from __future__ import annotations

import re

import pytest

from agents.hana import checks as registry
from agents.hana.checks import inspection
from agents.hana.sqltokens import tokenize
from tests.test_hanadb_checks_monitoring import assert_registered, run_on_fake

EXPECTED: tuple[str, ...] = (
    "inspectTable", "inspectIndex", "inspectProcedure", "inspectFunction",
    "inspectView", "inspectTrigger", "inspectLibrary", "inspectLibMember")

ARGS = {"top", "schema", "object"}

PARTS: dict[str, tuple[str, ...]] = {
    "inspectTable": ("table", "size", "columns", "constraints", "foreign_keys", "indexes"),
    "inspectIndex": ("index", "columns"),
    "inspectProcedure": ("procedure", "parameters", "parameter_columns", "definition"),
    "inspectFunction": ("function", "parameters", "parameter_columns", "definition"),
    "inspectView": ("view", "columns", "parameters", "definition"),
    "inspectTrigger": ("trigger", "definition"),
    "inspectLibrary": ("library", "members", "definition"),
    "inspectLibMember": ("library", "members"),
}

WITH_DEFINITION = {"inspectProcedure", "inspectFunction", "inspectView", "inspectTrigger",
                   "inspectLibrary", "inspectLibMember"}

# Never selected: free text somebody typed, expression text that can embed
# literals (the same rule as the schema checks), or not in the documented views.
FORBIDDEN_COLUMNS = {"COMMENTS", "COMMENT", "DESCRIPTION", "PRAGMAS", "DEFAULT_VALUE",
                     "CHECK_CONDITION"}

_REFERENCE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*|\()")


def _words(sql: str) -> set[str]:
    return {t.value for t in tokenize(sql) if t.kind == "word"}


def test_the_inspection_checks_are_pinned_and_registered() -> None:
    assert tuple(c.name for c in inspection.CHECKS) == EXPECTED
    for check in inspection.CHECKS:
        assert registry.get_check(check.name) is check
        assert check.category == "database"


@pytest.mark.parametrize("name", EXPECTED)
def test_registered_with_required_schema_and_object(name: str) -> None:
    check = assert_registered(name, "database", ARGS, parts=PARTS[name])
    assert check.required == frozenset({"schema", "object"})
    assert check.source == f"bin/{name}.js"
    assert not check.requires_select
    assert not check.masked_columns


@pytest.mark.parametrize("name", EXPECTED)
def test_definitions_are_read_up_to_20000_characters(name: str) -> None:
    check = registry.get_check(name)
    if name in WITH_DEFINITION:
        assert check.lob_chars == (4_000 if name == "inspectLibMember" else 20_000)
        assert any("DEFINITION" in _words(s.sql) for s in check.statements)
    else:
        assert check.lob_chars is None
        assert not any("DEFINITION" in _words(s.sql) for s in check.statements)


@pytest.mark.parametrize("name", EXPECTED)
def test_reads_only_the_sys_catalogue_never_the_object(name: str) -> None:
    for statement in registry.get_check(name).statements:
        references = _REFERENCE.findall(statement.sql)
        assert references
        for reference in references:
            assert reference == "(" or reference.startswith("SYS."), reference
        # The object is only ever compared as a name, never selected from.
        assert "FROM ?" not in statement.sql and "JOIN ?" not in statement.sql


@pytest.mark.parametrize("name", EXPECTED)
def test_no_comment_or_free_text_column(name: str) -> None:
    for statement in registry.get_check(name).statements:
        assert not _words(statement.sql) & FORBIDDEN_COLUMNS
        assert "_SYS_BI" not in statement.sql


@pytest.mark.parametrize("name", EXPECTED)
def test_every_statement_binds_schema_then_object(name: str) -> None:
    for statement in registry.get_check(name).statements:
        binds = [b for b in statement.binds if b != "top_plus_one"]
        assert binds == ["schema", "object"], statement.label
        assert "LIKE" not in _words(statement.sql)


def test_lib_member_lists_every_member_with_its_definition() -> None:
    check = registry.get_check("inspectLibMember")
    members = check.statements[1].sql
    assert {"MEMBER_NAME", "MEMBER_TYPE", "ACCESS_MODE", "DEFINITION"} <= _words(members)
    assert "member" in check.purpose.lower()


def test_lib_member_fits_the_lob_budget() -> None:
    # Every member's definition at its cap stays inside the 60,001 character
    # budget of one statement: a library of long members is cut, not refused.
    check = registry.get_check("inspectLibMember")
    assert check.lob_chars == 4_000 and check.max_rows_cap == 8
    assert check.max_rows_cap * check.lob_chars < 60_001
    assert "4,000" in check.purpose and "at most 8" in check.purpose


# --- worst cases measured as the model receives them ----------------------------

_NAME = "N" * 127  # the longest HANA identifier


def _select_columns(sql: str) -> list[str]:
    """The output names of a statement's select list (last word of each item)."""
    tokens = tokenize(sql)
    depth, start, items, current = 0, None, [], []
    for i, token in enumerate(tokens):
        if start is None:
            if token.is_word("SELECT"):
                start = i
            continue
        if token.is_symbol("("):
            depth += 1
        elif token.is_symbol(")"):
            depth -= 1
        if depth == 0 and token.is_word("FROM"):
            items.append(current)
            break
        if depth == 0 and token.is_symbol(","):
            items.append(current)
            current = []
        elif not (token.is_word("TOP") or token.is_symbol("?")) or current:
            current.append(token)
    return [[t.value for t in item if t.kind in ("word", "quoted")][-1] for item in items]


def _hostile(chars: int) -> str:
    """Source text of ``chars`` characters a third of which are ``"`` and a
    third line feeds: each is escaped to two characters in the JSON the model
    gets. Cut at the cap as the reader cuts it."""
    from agents.hana.connection import LOB_CUT_MARKER

    text = ('"\na' * chars)[:chars]
    return text + LOB_CUT_MARKER


def _worst_answer(name: str, *, other_rows: int) -> dict:
    """``run_check``'s answer for ``name``: every DEFINITION at the check's
    cap, every other cell a 127 character name; a part read by name (the
    header, the definition) one row, a listed part with DEFINITION
    ``max_rows_cap`` rows, every other listed part ``other_rows``."""
    from agents.hana.checks import build_answer

    check = registry.get_check(name)
    results = []
    for statement in check.statements:
        columns = _select_columns(statement.sql)
        listed = "top_plus_one" in statement.binds  # else one object, one row
        rows_n = (1 if not listed else
                  check.max_rows_cap if "DEFINITION" in columns else other_rows)
        row = tuple(_hostile(check.lob_chars) if c == "DEFINITION" else _NAME
                    for c in columns)
        results.append((columns, [row] * rows_n, False))
    answer = build_answer(check, results)
    for part in answer["parts"]:
        if "DEFINITION" in part["columns"]:
            part["truncated_columns"] = ["DEFINITION"]
    return {"system": "prd", "check": name, **answer}


def test_a_worst_case_lib_member_answer_is_not_refused_as_too_large() -> None:
    from agents.hana.tools import MAX_RESULT_CHARS
    from agents.tool_results import result_chars

    answer = _worst_answer("inspectLibMember", other_rows=1)
    (members,) = [p for p in answer["parts"] if p["label"] == "members"]
    assert members["row_count"] == 8
    assert result_chars(answer) <= MAX_RESULT_CHARS, result_chars(answer)


@pytest.mark.parametrize("name", sorted(WITH_DEFINITION - {"inspectLibMember"}))
def test_a_worst_case_definition_answer_is_not_refused_as_too_large(name: str) -> None:
    """One definition at its 20,000 character cap, the header, and ten rows in
    every listed part (parameters, columns, members) of 127 character names."""
    from agents.hana.tools import MAX_RESULT_CHARS
    from agents.tool_results import result_chars

    assert registry.get_check(name).lob_chars == 20_000
    answer = _worst_answer(name, other_rows=10)
    assert result_chars(answer) <= MAX_RESULT_CHARS, result_chars(answer)


def test_table_has_no_definition_and_no_row_preview() -> None:
    check = registry.get_check("inspectTable")
    sqls = " ".join(s.sql for s in check.statements)
    assert "GET_OBJECT_DEFINITION" not in sqls
    size = check.statements[1].sql
    assert "SYS.M_TABLES" in size and "RECORD_COUNT" in _words(size)


async def _run(monkeypatch, name: str) -> tuple[dict, list[tuple]]:
    columns = ["SCHEMA_NAME", "NAME"]
    rows = [("SAPABAP1", "A"), ("SAPABAP1", "B")]
    answer, executes = await run_on_fake(monkeypatch, name, columns, rows,
                                         schema="SAPABAP1", object="ZOBJ", top=8)
    assert "error" not in answer, answer
    assert [p["label"] for p in answer["parts"]] == list(PARTS[name])
    for part in answer["parts"]:
        assert part["columns"] == columns and part["row_count"] == 2
        assert part["truncated"] is False
    return answer, executes


@pytest.mark.parametrize("name", EXPECTED)
async def test_runs_on_the_fake_driver(monkeypatch, name: str) -> None:
    _answer, executes = await _run(monkeypatch, name)
    check = registry.get_check(name)
    assert len(executes) == len(check.statements)
    for statement, execute in zip(check.statements, executes):
        expected = tuple(9 if b == "top_plus_one" else {"schema": "SAPABAP1",
                                                          "object": "ZOBJ"}[b]
                         for b in statement.binds)
        assert execute[2] == expected


@pytest.mark.parametrize("name", EXPECTED)
async def test_refused_without_schema_or_object(monkeypatch, name: str) -> None:
    answer, executes = await run_on_fake(monkeypatch, name, ["X"], [], schema="SYS")
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == "object"
    assert executes == []
