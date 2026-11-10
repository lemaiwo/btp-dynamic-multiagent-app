"""The ``system`` checks of ``builtin:hana`` (``agents/hana/checks/system.py``).

Per check: registered under ``system`` with the declared arguments, its SQL
tokenizes, and run through the real toolset on the fake driver of
``tests/hanadb_helpers.py`` with two scripted rows it answers the expected
shape (one answer, or one part per statement). The generic invariants (a
read, the SELECT guard, the row limit) are in ``tests/test_hanadb_checks.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from agents.hana import tools
from agents.hana.checks import all_checks, get_check, system
from agents.hana.connection import HanaSystem
from agents.hana.sqltokens import tokenize
from tests import hanadb_helpers as h

OAUTH = {"systems": [{"name": "sib_tenant", "destination": "hana-dest-one"}]}

# name -> (part labels, declared args); "" is the one label of a single statement
EXPECTED: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "systemInfo": (("overview", "services"), frozenset({"top"})),
    "hostInformation": (("information", "resources"), frozenset({"top"})),
    "disks": (("disks", "usage"), frozenset({"top"})),
    "ports": (("",), frozenset({"top"})),
    "dataVolumes": (("volumes", "files", "page_statistics"), frozenset({"top"})),
    "features": (("",), frozenset({"top"})),
    "featureUsage": (("",), frozenset({"top"})),
    "iniFiles": (("",), frozenset({"top"})),
    "iniContents": (("",), frozenset({"top", "object"})),
    # composite (task 13b)
    "healthCheck": (("overview", "databases", "disks", "memory", "connections",
                     "transactions", "backups"), frozenset({"top"})),
    "diagnose": (("services", "memory", "alerts", "blocked", "last_backup"),
                 frozenset({"top"})),
    "recommendations": (("read_tables", "statements", "caches", "large_tables", "blocked",
                         "memory"), frozenset({"top"})),
    "workloadManagement": (("workload", "classes"), frozenset({"top"})),
}

COMPOSITE = ("healthCheck", "diagnose", "recommendations", "workloadManagement")


@pytest.mark.parametrize("name", COMPOSITE)
def test_composite_parts_read_no_statement_text(name: str) -> None:
    """Only the SELECTs of hana-cli's composite commands: no statement text
    leaves through them (so nothing to mask), and the large views are limited."""
    check = get_check(name)
    assert check.composite and check.masked_columns == frozenset()
    for statement in check.statements:
        words = {t.value for t in tokenize(statement.sql) if t.kind == "word"}
        assert "STATEMENT_STRING" not in words
        large = {"M_TABLE_STATISTICS", "M_SQL_PLAN_CACHE", "M_CS_TABLES", "WORKLOAD_CLASSES"}
        if words & large:
            assert "top_plus_one" in statement.binds


def _factory(name: str, destination: str) -> HanaSystem:
    return HanaSystem(name, destination, resolver=h.FakeResolver(), tokens=h.FakeTokens())


def _toolset():
    return tools.hana_toolset(OAUTH, auth_mode="destination", system_factory=_factory)


async def _run(**kw: Any) -> dict[str, Any]:
    return await _toolset().tools["run_check"].function(system="sib_tenant", **kw)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> h.FakeDbapi:
    return h.install(monkeypatch)


def test_the_system_checks_are_registered() -> None:
    names = [c.name for c in all_checks() if c.category == "system"]
    assert names == list(EXPECTED)


@pytest.mark.parametrize("name", list(EXPECTED))
def test_registered_with_category_and_args(name: str) -> None:
    check = get_check(name)
    labels, args = EXPECTED[name]
    assert check is not None and check.category == "system"
    assert check.args == args
    assert tuple(s.label for s in check.statements) == labels
    assert not check.requires_select
    for statement in check.statements:
        tokens = tokenize(statement.sql)
        assert tokens and tokens[0].is_word("SELECT")
        # named columns, never SELECT * / t.* (COUNT(*) and a product are fine)
        for i, token in enumerate(tokens):
            if token.is_symbol("*"):
                before = tokens[i - 1]
                assert not (before.is_word("SELECT", "DISTINCT") or before.is_symbol(",", ".")
                            or before.kind == "param"), "a select-list *"


@pytest.mark.parametrize("name", list(EXPECTED))
async def test_runs_on_the_fake_with_two_rows(name: str, db: h.FakeDbapi) -> None:
    db.description = (("A",), ("B",))
    db.rows = [(1, "x"), (2, "y")]
    answer = await _run(check=name, top=5)
    labels, _ = EXPECTED[name]
    assert answer["system"] == "sib_tenant" and answer["check"] == name
    parts = answer["parts"] if len(labels) > 1 else [answer]
    assert [p.get("label", "") for p in parts] == list(labels)
    for part in parts:
        assert part["columns"] == ["A", "B"]
        assert part["rows"] == [[1, "x"], [2, "y"]]
        assert part["row_count"] == 2 and part["truncated"] is False
    executed = [c for c in db.calls if c[0] == "execute"]
    assert len(executed) == len(labels)


async def test_ini_contents_binds_the_optional_file(db: h.FakeDbapi) -> None:
    await _run(check="iniContents", top=5)
    await _run(check="iniContents", top=5, object="global.ini")
    executed = [c for c in db.calls if c[0] == "execute"]
    assert executed[0][2] == (6, None, None)
    assert executed[1][2] == (6, "global.ini", "global.ini")
    assert "CAST(? AS NVARCHAR(256)) IS NULL OR FILE_NAME = ?" in executed[0][1]


def _case_spans(tokens) -> list[tuple[int, int]]:
    """Every outermost CASE ... END of a statement, as token index pairs."""
    spans, depth, start = [], 0, 0
    for i, token in enumerate(tokens):
        if token.is_word("CASE"):
            if depth == 0:
                start = i
            depth += 1
        elif token.is_word("END") and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, i))
    assert depth == 0, "CASE has no END"
    return spans


def _is_name(token, name: str) -> bool:
    return token.kind in ("word", "quoted") and token.value == name


def test_ini_contents_value_only_through_the_hiding_case() -> None:
    """The fake does not evaluate SQL, so the rule is held on the tokens: the
    raw VALUE column appears only inside a CASE and as an alias after one."""
    sql = get_check("iniContents").statements[0].sql
    tokens = tokenize(sql)
    spans = _case_spans(tokens)
    assert len(spans) == 2  # VALUE and VALUE_HIDDEN
    aliases = {end + 2 for _, end in spans if tokens[end + 1].is_word("AS")}
    for i, token in enumerate(tokens):
        if _is_name(token, "VALUE"):
            assert any(s < i < e for s, e in spans) or i in aliases, "VALUE outside CASE"
    (s1, e1), (s2, e2) = spans
    assert _is_name(tokens[e1 + 2], "VALUE") and _is_name(tokens[e2 + 2], "VALUE_HIDDEN")
    # default-deny: the CASE ends in the hidden literal, and 1 for VALUE_HIDDEN
    assert tokens[e1 - 2].is_word("ELSE") and tokens[e1 - 1].text == f"'{system.HIDDEN}'"
    assert tokens[e2 - 2].is_word("ELSE") and tokens[e2 - 1].text == "1"


def test_ini_contents_deny_words_and_shape() -> None:
    sql = system.INI_VALUE_SQL
    for word in ("password", "passwd", "pwd", "pass", "secret", "credential", "passphrase",
                 "token", "auth", "cert", "salt", "saml", "jwt", "proxy", "keystore",
                 "private", "ssl", "ldap"):
        assert word in system.DENY_WORDS
        assert f"LOCATE(LOWER(\"KEY\"), '{word}') > 0" in sql
        assert f"LOCATE(LOWER(SECTION), '{word}') > 0" in sql
    assert "RIGHT(LOWER(\"KEY\"), 3) = 'key'" in sql
    assert f"LENGTH(\"VALUE\") <= {system.SHAPE_MAX}" in sql
    assert f"\"VALUE\" LIKE_REGEXPR '{system.SHAPE_PATTERN}'" in sql
    assert system.SHAPE_MAX == 64


def test_ini_contents_never_shows_a_multi_line_value() -> None:
    """Whether ^ and $ anchor the whole string in HANA's LIKE_REGEXPR is not
    verified: a line feed, carriage return or tab hides the value anyway, in
    both CASEs (VALUE and VALUE_HIDDEN)."""
    sql = get_check("iniContents").statements[0].sql
    for code in (10, 13, 9):
        term = f"LOCATE(\"VALUE\", CHAR({code})) = 0"
        assert term in system.INI_VALUE_SQL
        assert sql.count(term) == 2
    from agents.hana.guard import MAX_SQL_CHARS
    assert len(sql) < MAX_SQL_CHARS


@pytest.mark.parametrize("value", ["1024", "true", "on", "80%", "1.5", "row_store", "a-b,c +d"])
def test_ini_shape_lets_plain_values_through(value: str) -> None:
    assert len(value) <= system.SHAPE_MAX
    assert re.fullmatch(system.SHAPE_PATTERN.strip("^$"), value)


@pytest.mark.parametrize("value", [
    "ldap://host:389", "jdbc:sap://u:p@host:30015", "user@host", "a=b", "/hana/data/SID",
    "C:\\x", "x;y", "it's", 'say "x"', "a" * 65, "tab\tbed", "line\nbreak", "naïve"])
def test_ini_shape_hides_everything_else(value: str) -> None:
    shown = len(value) <= system.SHAPE_MAX and re.fullmatch(
        system.SHAPE_PATTERN.strip("^$"), value)
    assert not shown


def test_ini_contents_purpose_says_what_is_hidden() -> None:
    purpose = get_check("iniContents").purpose
    for word in ("secrets", "paths", "hosts", "URLs"):
        assert word in purpose


def test_host_information_returns_fixed_keys_only() -> None:
    statement = get_check("hostInformation").statements[0]
    tokens = tokenize(statement.sql)
    keys = {t.text.strip("'") for t in tokens if t.kind == "string"}
    assert keys == set(system.HOST_INFORMATION_KEYS)
    assert "os_user" not in keys and "net_ip_addresses" not in keys
    assert "\"KEY\" IN (" in statement.sql


def test_feature_usage_names_no_user() -> None:
    sql = get_check("featureUsage").statements[0].sql
    assert "LAST_USER_NAME" not in sql and "LAST_APPLICATION_NAME" not in sql


def _words(sql: str) -> set[str]:
    return {t.value for t in tokenize(sql) if t.kind == "word"}


def test_feature_usage_leaves_out_object_count() -> None:
    # SPS07 tenant (2026-11-10): OBJECT_COUNT alone runs past the 20 s timeout.
    words = _words(get_check("featureUsage").statements[0].sql)
    assert "OBJECT_COUNT" not in words
    assert {"CALL_COUNT", "LAST_TIMESTAMP", "IS_DEPRECATED"} <= words


def test_ini_files_read_the_contents_view() -> None:
    # SPS07: M_INIFILES has FILE_NAME only; layer, tenant and host per file
    # come from M_INIFILE_CONTENTS, grouped. No value or key is selected.
    sql = get_check("iniFiles").statements[0].sql
    words = _words(sql)
    assert "M_INIFILE_CONTENTS" in words and "M_INIFILES" not in words
    assert "GROUP" in words
    assert '"VALUE"' not in sql and '"KEY"' not in sql


def test_read_tables_counts_selects() -> None:
    # SPS07: M_TABLE_STATISTICS has no READ_COUNT / WRITE_COUNT.
    parts = {s.label: s for s in get_check("recommendations").statements}
    words = _words(parts["read_tables"].sql)
    assert not words & {"READ_COUNT", "WRITE_COUNT"}
    assert "SELECT_COUNT" in words
    assert parts["read_tables"].binds == ("top_plus_one",)


def test_health_check_backups_last_30_days() -> None:
    sql = dict((s.label, s.sql) for s in get_check("healthCheck").statements)["backups"]
    assert "WHERE SYS_START_TIME >= ADD_DAYS(CURRENT_TIMESTAMP, -30)" in sql


def test_large_tables_has_a_row_limit() -> None:
    parts = {s.label: s for s in get_check("recommendations").statements}
    assert parts["large_tables"].binds == ("top_plus_one",)
    assert parts["large_tables"].sql.startswith("SELECT TOP ? ")


def test_key_and_value_are_quoted() -> None:
    for check in all_checks():
        if check.category != "system":
            continue
        for statement in check.statements:
            for token in tokenize(statement.sql):
                assert not token.is_word("KEY", "VALUE"), f"{check.name}: unquoted"
