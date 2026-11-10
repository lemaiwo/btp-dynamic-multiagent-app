"""The check registry of ``builtin:hana`` (``agents/hana/checks/``).

Generic: every test that judges a check runs over every registered check, so a
later task that adds checks adds data only. Until the real checks exist two
dummy checks stand in (``DUMMY_CHECKS``); they are never registered globally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents.hana import checks as registry
from agents.hana.checks import base
from agents.hana.checks.base import (
    ARGS,
    CATEGORIES,
    HANA_IDENT_RE,
    Check,
    InvalidArgument,
    Statement,
)
from agents.hana.errors import HanaError
from agents.hana.guard import check_select
from agents.hana.sqltokens import SelectRefused, tokenize

ROOT = Path(__file__).resolve().parent.parent

DUMMY_ONE = Check(
    name="dummyOne",
    category="monitoring",
    purpose="Dummy single-statement check for the registry tests.",
    source="bin/dummyOne.js",
    statements=(
        Statement(
            label="",
            sql=(
                "SELECT TOP ? SCHEMA_NAME, STATEMENT_STRING FROM SYS.M_ACTIVE_STATEMENTS"
                " WHERE (? IS NULL OR SCHEMA_NAME = ?)"
            ),
            binds=("top_plus_one", "schema", "schema"),
        ),
    ),
    args=frozenset({"top", "schema"}),
    masked_columns=frozenset({"STATEMENT_STRING"}),
)

DUMMY_TWO = Check(
    name="dummyTwo",
    category="performance",
    purpose="Dummy composite check for the registry tests.",
    source="bin/dummyTwo.js",
    statements=(
        Statement(
            label="summary",
            sql="SELECT COUNT(*) AS N FROM SYS.M_TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
            binds=("schema", "object"),
        ),
        Statement(
            label="recent",
            sql=(
                "SELECT TOP ? HOST FROM SYS.M_SERVICE_THREADS"
                " WHERE START_TIME > ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)"
            ),
            binds=("top_plus_one", "hours"),
        ),
    ),
    args=frozenset({"top", "hours", "schema", "object"}),
    requires_select=True,
    max_rows_cap=100,
)

DUMMY_CHECKS = (DUMMY_ONE, DUMMY_TWO)
EVERY_CHECK = tuple(registry.all_checks()) + DUMMY_CHECKS


@pytest.fixture
def dummies(monkeypatch):
    """The registry holding exactly the two dummies for one test."""
    monkeypatch.setattr(base, "_REGISTRY", {})
    registry.register(*DUMMY_CHECKS)
    return DUMMY_CHECKS


# ---- the invariants of every check ----------------------------------------------------


@pytest.mark.parametrize("check", EVERY_CHECK, ids=lambda c: c.name)
def test_check_shape(check: Check) -> None:
    assert base.CHECK_NAME_RE.fullmatch(check.name)
    assert check.category in CATEGORIES
    assert check.purpose and len(check.purpose) <= 120
    assert "\n" not in check.purpose and check.purpose == check.purpose.strip()
    assert check.source == f"bin/{check.name}.js"
    assert len(check.statements) >= 1
    assert check.args <= set(ARGS)
    assert 1 <= check.max_rows_cap <= 500
    base.validate_check(check)


@pytest.mark.parametrize("check", EVERY_CHECK, ids=lambda c: c.name)
def test_statements_are_bound_never_built(check: Check) -> None:
    labels = [s.label for s in check.statements]
    if len(check.statements) == 1:
        assert labels == [""]
    else:
        assert all(base.LABEL_RE.fullmatch(lbl) for lbl in labels)
        assert len(set(labels)) == len(labels)
    for statement in check.statements:
        for marker in ("{", "}", "%s", "%("):
            assert marker not in statement.sql
        tokens = tokenize(statement.sql)
        params = [t for t in tokens if t.kind == "param"]
        assert all(t.text == "?" for t in params)
        assert len(params) == len(statement.binds)
        assert set(statement.binds) <= check.args | {"top_plus_one"}
        if "top_plus_one" in statement.binds:
            assert "top" in check.args


def _row_limit_problems(statement: Statement) -> list[str]:
    """Why a statement could hide ``truncated``: the connection fetches
    ``top + 1`` rows to tell, so an SQL row limit must be ``top_plus_one``."""
    problems = []
    if "top" in statement.binds:
        problems.append("binds plain top: a row limit binds top_plus_one")
    tokens = tokenize(statement.sql)
    params = [i for i, t in enumerate(tokens) if t.kind == "param"]
    for position, index in enumerate(params):
        if index and tokens[index - 1].is_word("TOP", "LIMIT"):
            if statement.binds[position] != "top_plus_one":
                problems.append("TOP ? / LIMIT ? is not bound to top_plus_one")
    return problems


@pytest.mark.parametrize("check", EVERY_CHECK, ids=lambda c: c.name)
def test_a_row_limit_leaves_room_for_truncated(check: Check) -> None:
    for statement in check.statements:
        assert _row_limit_problems(statement) == []


@pytest.mark.parametrize(
    "statement",
    [
        Statement("", "SELECT TOP ? A FROM SYS.M_TABLES", ("top",)),
        Statement("", "SELECT A FROM SYS.M_TABLES LIMIT ?", ("top",)),
        Statement("", "SELECT A FROM SYS.M_TABLES WHERE B < ?", ("top",)),
        Statement("", "SELECT TOP ? A FROM SYS.M_TABLES", ("hours",)),
    ],
)
def test_the_row_limit_rule_catches_a_plain_top(statement: Statement) -> None:
    assert _row_limit_problems(statement)


def test_top_plus_one_needs_top_declared() -> None:
    with pytest.raises(ValueError):
        base.validate_check(_variant(
            args=frozenset({"hours"}),
            statements=(Statement("", "SELECT TOP ? 1 FROM DUMMY", ("top_plus_one",)),)))


@pytest.mark.parametrize("check", EVERY_CHECK, ids=lambda c: c.name)
def test_masked_columns_are_column_names(check: Check) -> None:
    for column in check.masked_columns:
        assert base.COLUMN_RE.fullmatch(column)


@pytest.mark.skipif(not os.environ.get("HCLI"), reason="no hana-cli clone in $HCLI")
@pytest.mark.parametrize("check", EVERY_CHECK, ids=lambda c: c.name)
def test_source_names_an_upstream_file(check: Check) -> None:
    if check in DUMMY_CHECKS:
        pytest.skip("dummy")
    assert (Path(os.environ["HCLI"]) / check.source).is_file()


# ---- validate_check refuses what the invariants forbid --------------------------------


def _variant(**changes) -> Check:
    fields = {
        "name": "dummyOne",
        "category": "monitoring",
        "purpose": "p",
        "source": "bin/dummyOne.js",
        "statements": (Statement("", "SELECT TOP ? 1 FROM DUMMY", ("top",)),),
    }
    fields.update(changes)
    return Check(**fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "bad name"},
        {"category": "nope"},
        {"purpose": ""},
        {"purpose": "x" * 121},
        {"purpose": "two\nlines"},
        {"source": "bin/other.js"},
        {"statements": ()},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY WHERE A = ?", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY WHERE A = :x", ("top",)),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY WHERE A = '{x}'", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY WHERE A = '%s'", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY WHERE A = ?", ("hours",)),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY WHERE A = ?", ("user",)),)},
        {"statements": (Statement("x", "SELECT 1 FROM DUMMY", ()),)},
        {"statements": (Statement("a", "SELECT 1 FROM DUMMY", ()),
                        Statement("a", "SELECT 2 FROM DUMMY", ()))},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY \x00", ()),)},
        # a registered check must be a read (token-based, at registration)
        {"statements": (Statement("", "DELETE FROM SAPABAP1.VBAK WHERE MANDT = ?",
                                  ("top",)),)},
        {"statements": (Statement("", "UPDATE SYS.M_TABLES SET A = 1", ()),)},
        {"statements": (Statement("", "SELECT A INTO B FROM DUMMY", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY; DROP TABLE X", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY;", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY -- note", ()),)},
        {"statements": (Statement("", "SELECT 1 /* note */ FROM DUMMY", ()),)},
        {"statements": (Statement("", "SELECT 1 FROM DUMMY FOR UPDATE", ()),)},
        {"statements": (Statement("", "SELECT NEXTVAL FROM DUMMY", ()),)},
        {"statements": (Statement("", "CALL SYS.SOMETHING()", ()),)},
        {"statements": (Statement("", "(SELECT 1 FROM DUMMY)", ()),)},
        {"statements": (Statement("", "WITH A AS (SELECT 1 FROM DUMMY) "
                                      "SELECT * FROM A; COMMIT", ()),)},
        {"args": frozenset({"top", "user"})},
        {"masked_columns": frozenset({"A B"})},
        {"max_rows_cap": 0},
        {"max_rows_cap": 501},
    ],
)
def test_validate_check_refuses(changes) -> None:
    with pytest.raises(ValueError):
        base.validate_check(_variant(**changes))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'DELETE; -- INTO' AS A FROM DUMMY",
        'SELECT "UPDATE" FROM DUMMY',
        "WITH A AS (SELECT 1 AS X FROM DUMMY) SELECT X FROM A",
        "select 1 from dummy",
    ],
)
def test_validate_check_ignores_words_inside_literals(sql) -> None:
    base.validate_check(_variant(statements=(Statement("", sql, ()),)))


def test_register_refuses_a_write(monkeypatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})
    with pytest.raises(ValueError):
        registry.register(_variant(statements=(
            Statement("", "DELETE FROM SAPABAP1.VBAK WHERE MANDT = ?", ("top",)),)))
    assert registry.all_checks() == ()


# ---- the SELECT guard over every statement --------------------------------------------

GUARD_EXCEPTIONS: dict[tuple[str, str], str] = {}
"""``(check name, statement label) -> one-line reason`` for a statement that may
not pass ``guard.check_select`` (it reads outside the monitoring schemas). An
entry whose statement passes the guard is stale and fails the test."""


def _assert_guard(check: Check, statement: Statement,
                  exceptions: dict[tuple[str, str], str]) -> None:
    key = (check.name, statement.label)
    try:
        check_select(statement.sql)
    except SelectRefused:
        assert key in exceptions, f"{key} fails the SELECT guard and is not excepted"
        assert exceptions[key].strip() and "\n" not in exceptions[key]
        return
    assert key not in exceptions, f"{key} passes the SELECT guard: stale exception"


@pytest.mark.parametrize(
    "check, statement",
    [(c, s) for c in EVERY_CHECK for s in c.statements],
    ids=lambda v: getattr(v, "name", None) or getattr(v, "label", None) or "single",
)
def test_every_statement_passes_the_select_guard(check: Check, statement: Statement) -> None:
    _assert_guard(check, statement, GUARD_EXCEPTIONS)


BUSINESS_READ = _variant(
    name="dummyBusiness", source="bin/dummyBusiness.js",
    statements=(Statement("", "SELECT TOP ? VBELN FROM SAPABAP1.VBAK", ("top",)),))


def test_guard_exceptions_work_in_both_directions() -> None:
    base.validate_check(BUSINESS_READ)  # a read, so registration accepts it
    statement = BUSINESS_READ.statements[0]
    excepted = {("dummyBusiness", ""): "reads a business table on purpose"}
    _assert_guard(BUSINESS_READ, statement, excepted)
    with pytest.raises(AssertionError, match="not excepted"):
        _assert_guard(BUSINESS_READ, statement, {})
    stale = {("dummyOne", ""): "no longer needed"}
    with pytest.raises(AssertionError, match="stale exception"):
        _assert_guard(DUMMY_ONE, DUMMY_ONE.statements[0], stale)


# ---- the registry ---------------------------------------------------------------------


def test_register_lookup_and_order(dummies) -> None:
    assert registry.all_checks() == DUMMY_CHECKS
    assert registry.get_check("dummyOne") is DUMMY_ONE
    assert registry.get_check("missing") is None
    assert registry.get_check(None) is None  # type: ignore[arg-type]


def test_register_refuses_a_duplicate_and_an_invalid_check(dummies) -> None:
    with pytest.raises(ValueError):
        registry.register(DUMMY_ONE)
    with pytest.raises(ValueError):
        registry.register(_variant(name="other", source="bin/x.js"))
    assert registry.all_checks() == DUMMY_CHECKS


def test_requires_select_hides_and_refuses(dummies) -> None:
    assert registry.visible_checks(allow_select=False) == (DUMMY_ONE,)
    assert registry.visible_checks(allow_select=True) == DUMMY_CHECKS
    assert registry.lookup("dummyOne", allow_select=False) is DUMMY_ONE
    assert registry.lookup("dummyTwo", allow_select=True) is DUMMY_TWO
    for name, allow in (("dummyTwo", False), ("missing", True), (7, True)):
        with pytest.raises(HanaError) as info:
            registry.lookup(name, allow_select=allow)  # type: ignore[arg-type]
        assert info.value.code == "unknown_check"
        assert "dummyTwo" not in str(info.value)


def test_categories_are_the_seven() -> None:
    assert CATEGORIES == (
        "monitoring", "performance", "system", "backup", "security", "database", "schema")


def test_registry_is_import_light() -> None:
    code = (
        "import sys, agents.hana.checks;"
        "bad = [m for m in ('hdbcli', 'pydantic_ai', 'agents.db', 'sqlalchemy', 'httpx')"
        " if m in sys.modules]; print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ---- arguments ------------------------------------------------------------------------


def test_defaults() -> None:
    assert base.resolve_args(DUMMY_ONE) == {"top": 50, "schema": None}
    assert base.resolve_args(DUMMY_TWO) == {
        "top": 50, "hours": 24, "schema": None, "object": None}
    assert ARGS["top"].default == 50 and ARGS["hours"].default == 24


@pytest.mark.parametrize(
    "given, expected",
    [
        ({"top": 1}, {"top": 1}),
        ({"top": 100}, {"top": 100}),
        ({"hours": 1}, {"hours": 1}),
        ({"hours": 720}, {"hours": 720}),
        ({"schema": "SAPHANADB"}, {"schema": "SAPHANADB"}),
        ({"schema": "_SYS_STATISTICS"}, {"schema": "_SYS_STATISTICS"}),
        ({"object": "my.pkg/TABLE$1#"}, {"object": "my.pkg/TABLE$1#"}),
        ({"object": "x" * 127}, {"object": "x" * 127}),
    ],
)
def test_valid_arguments(given, expected) -> None:
    resolved = base.resolve_args(DUMMY_TWO, **given)
    for key, value in expected.items():
        assert resolved[key] == value


def test_top_is_capped_by_the_check() -> None:
    assert base.resolve_args(DUMMY_ONE, top=500)["top"] == 500
    with pytest.raises(InvalidArgument) as info:
        base.resolve_args(DUMMY_TWO, top=101)
    assert info.value.argument == "top"


DUMMY_CAPPED = Check(
    name="dummyCapped", category="system", purpose="Dummy check capped at 10 rows.",
    source="bin/dummyCapped.js",
    statements=(Statement("", "SELECT TOP ? A FROM SYS.M_DATABASE", ("top_plus_one",)),),
    max_rows_cap=10,
)


def test_default_top_under_a_cap() -> None:
    base.validate_check(DUMMY_CAPPED)
    args = base.resolve_args(DUMMY_CAPPED)
    assert args == {"top": 10}
    assert base.bind_values(DUMMY_CAPPED.statements[0], args) == (11,)
    assert base.resolve_args(DUMMY_CAPPED, top=3) == {"top": 3}
    with pytest.raises(InvalidArgument) as info:
        base.resolve_args(DUMMY_CAPPED, top=11)
    assert info.value.argument == "top"


SECRET ="S3cret Value'--"


@pytest.mark.parametrize(
    "argument, value",
    [
        ("top", 0), ("top", 501), ("top", True), ("top", "5"), ("top", 1.0),
        ("hours", 0), ("hours", 721), ("hours", False), ("hours", "24"),
        ("schema", ""), ("schema", "1ABC"), ("schema", "A B"), ("schema", "A'B"),
        ("schema", 'A"B'), ("schema", "x" * 128), ("schema", 5), ("schema", "A::B"),
        ("object", "a;DROP"), ("object", "é"), ("object", ["A"]), ("object", "A\n"),
    ],
)
def test_invalid_arguments_name_the_argument_not_the_value(argument, value) -> None:
    with pytest.raises(InvalidArgument) as info:
        base.resolve_args(DUMMY_TWO, **{argument: value})
    err = info.value
    assert isinstance(err, HanaError)
    assert err.code == "invalid_argument"
    assert err.argument == argument
    payload = json.dumps(err.to_dict())
    assert argument in payload
    assert err.message == "An argument of the call is not valid."
    with pytest.raises(InvalidArgument) as info2:
        base.resolve_args(DUMMY_TWO, **{argument: SECRET if argument in ("schema", "object")
                                         else value})
    assert SECRET not in json.dumps(info2.value.to_dict()) + str(info2.value)


def test_undeclared_argument_is_refused() -> None:
    for argument, value in (("hours", 24), ("object", "DUMMY"), ("hours", 1)):
        with pytest.raises(InvalidArgument) as info:
            base.resolve_args(DUMMY_ONE, **{argument: value})
        assert info.value.argument == argument
        assert info.value.rule == "not an argument of this check"


def test_unknown_keyword_is_refused() -> None:
    with pytest.raises(TypeError):
        base.resolve_args(DUMMY_ONE, user="x")  # type: ignore[call-arg]


def test_bind_values_follow_the_statement() -> None:
    args = base.resolve_args(DUMMY_TWO, schema="SYS", object="DUMMY", top=5, hours=2)
    assert base.bind_values(DUMMY_TWO.statements[0], args) == ("SYS", "DUMMY")
    assert base.bind_values(DUMMY_TWO.statements[1], args) == (6, 2)


def test_hana_ident_re() -> None:
    assert HANA_IDENT_RE.pattern == r"^[A-Za-z_][A-Za-z0-9_$#./]{0,126}$"


# ---- answer and masking ---------------------------------------------------------------


def test_single_answer_masks_columns() -> None:
    answer = base.build_answer(
        DUMMY_ONE,
        [(["SCHEMA_NAME", "STATEMENT_STRING"],
          [("SYS", "SELECT * FROM T WHERE KUNNR = '0000123'")], False)],
    )
    assert answer == {
        "columns": ["SCHEMA_NAME", "STATEMENT_STRING"],
        "rows": [["SYS", "SELECT * FROM T WHERE KUNNR = ?"]],
        "row_count": 1,
        "truncated": False,
    }


def test_composite_answer_has_parts() -> None:
    answer = base.build_answer(
        DUMMY_TWO, [(["N"], [(3,)], False), (["HOST"], [("h1",), ("h2",)], True)])
    assert answer == {"parts": [
        {"label": "summary", "columns": ["N"], "rows": [[3]], "row_count": 1,
         "truncated": False},
        {"label": "recent", "columns": ["HOST"], "rows": [["h1"], ["h2"]], "row_count": 2,
         "truncated": True},
    ]}


def test_build_answer_needs_one_result_per_statement() -> None:
    with pytest.raises(ValueError):
        base.build_answer(DUMMY_TWO, [(["N"], [], False)])


# ---- --dump ---------------------------------------------------------------------------


def _load_probe():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import probe_hana_connectivity as probe
    finally:
        sys.path.pop(0)
    return probe


def test_dump_entries_are_what_the_probe_loads(tmp_path, dummies) -> None:
    entries = base.dump_entries()
    assert entries == [
        {"name": "dummyOne",
         "sql": DUMMY_ONE.statements[0].sql, "binds": [2, "SYS", "SYS"]},
        {"name": "dummyTwo.summary",
         "sql": DUMMY_TWO.statements[0].sql, "binds": ["SYS", "DUMMY"]},
        {"name": "dummyTwo.recent",
         "sql": DUMMY_TWO.statements[1].sql, "binds": [2, 24]},
    ]
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    assert _load_probe().load_checks(str(path)) == entries


def test_dump_command_prints_json() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "agents.hana.checks", "--dump"],
        cwd=ROOT, capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == base.dump_entries()


def test_main_without_dump_is_usage_error() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "agents.hana.checks"],
        cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 2
