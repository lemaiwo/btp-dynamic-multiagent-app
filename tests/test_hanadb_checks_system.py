"""The ``system`` checks of ``builtin:hana`` (``agents/hana/checks/system.py``).

Per check: registered under ``system`` with the declared arguments, its SQL
tokenizes, and run through the real toolset on the fake driver of
``tests/hanadb_helpers.py`` with two scripted rows it answers the expected
shape (one answer, or one part per statement). The generic invariants (a
read, the SELECT guard, the row limit) are in ``tests/test_hanadb_checks.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.hana import tools
from agents.hana.checks import all_checks, get_check, system
from agents.hana.checks.base import bind_values, resolve_args
from agents.hana.connection import HanaSystem
from agents.hana.sqltokens import tokenize
from tests import hanadb_helpers as h

OAUTH = {"systems": [{"name": "erp_tenant", "destination": "hana-dest-one"}]}

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
    "iniContents": (("files", "effective"), frozenset({"top", "object", "section", "key"})),
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
    return await _toolset().tools["run_check"].function(system="erp_tenant", **kw)


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
    assert answer["system"] == "erp_tenant" and answer["check"] == name
    parts = answer["parts"] if len(labels) > 1 else [answer]
    assert [p.get("label", "") for p in parts] == list(labels)
    for part in parts:
        assert part["columns"] == ["A", "B"]
        assert part["rows"] == [[1, "x"], [2, "y"]]
        assert part["row_count"] == 2 and part["truncated"] is False
    executed = [c for c in db.calls if c[0] == "execute"]
    assert len(executed) == len(labels)


_INI_FILTER_BINDS = ("top_plus_one", "object", "object", "section", "section", "key", "key")


async def test_ini_contents_binds_file_section_and_key(db: h.FakeDbapi) -> None:
    await _run(check="iniContents", top=5)
    executed = [c for c in db.calls if c[0] == "execute"]
    none = (6, None, None, None, None, None, None)
    assert [c[2] for c in executed] == [none, none]  # files, effective
    # run_check does not take section / key yet (the next task wires them):
    # the binds are held through the registry's own resolve and bind.
    check = get_check("iniContents")
    args = resolve_args(check, top=5, object="global.ini", section="memorymanager",
                        key="global_allocation")
    for statement in check.statements:
        assert statement.binds == _INI_FILTER_BINDS
        assert bind_values(statement, args) == (
            6, "global.ini", "global.ini", "memorymanager", "memorymanager",
            "global_allocation", "global_allocation")
        assert "LOWER(SECTION) = LOWER(?)" in statement.sql
        assert 'LOCATE(LOWER("KEY"), LOWER(?)) > 0' in statement.sql
        assert "CAST(? AS NVARCHAR(256)) IS NULL OR FILE_NAME = ?" in statement.sql
        assert "LIKE" not in _words(statement.sql)


def test_ini_contents_returns_values_as_stored() -> None:
    files = get_check("iniContents").statements[0]
    assert files.label == "files"
    tokens = tokenize(files.sql)
    select_list = files.sql.split(" FROM ")[0]
    columns = [c.strip().strip('"') for c in select_list.removeprefix("SELECT TOP ? ").split(",")]
    assert columns == ["FILE_NAME", "LAYER_NAME", "TENANT_NAME", "HOST", "SECTION", "KEY",
                       "VALUE"]
    assert "SYS.M_INIFILE_CONTENTS" in files.sql
    for statement in get_check("iniContents").statements:
        words = _words(statement.sql)
        assert "CASE" not in words and "VALUE_HIDDEN" not in words
        assert "<hidden>" not in statement.sql
    assert not any(t.kind == "string" for t in tokens)
    for gone in ("INI_VALUE_SQL", "DENY_WORDS", "HIDDEN", "SHAPE_PATTERN", "SHAPE_MAX"):
        assert not hasattr(system, gone), gone


def test_ini_contents_args() -> None:
    check = get_check("iniContents")
    assert check.args == {"top", "object", "section", "key"}
    assert not check.required


def test_ini_contents_purpose_names_the_filters() -> None:
    purpose = get_check("iniContents").purpose
    for word in ("section", "key", "every layer"):
        assert word in purpose
    assert "\n" not in purpose and len(purpose) <= 120


def test_ini_contents_effective_part() -> None:
    """SPS07 rev 79 has ``M_CONFIGURATION_PARAMETER_VALUES`` (probe 2026-11-18,
    SYSTEMDB and a tenant): the value in effect per host and port, with the
    same three filters; RAW_VALUE, the HAS_* flags and VIOLATED_RESTRICTIONS
    are not selected."""
    check = get_check("iniContents")
    assert check.composite
    assert tuple(s.label for s in check.statements) == ("files", "effective")
    effective = check.statements[1]
    assert "FROM SYS.M_CONFIGURATION_PARAMETER_VALUES WHERE" in effective.sql
    assert effective.sql.startswith("SELECT TOP ? ")
    select_list = effective.sql.split(" FROM ")[0]
    columns = [c.strip().strip('"') for c in select_list.removeprefix("SELECT TOP ? ").split(",")]
    assert columns == ["HOST", "PORT", "FILE_NAME", "SECTION", "KEY", "LAYER_NAME", "VALUE",
                       "RESTART_REQUIRED"]
    words = _words(effective.sql)
    for left_out in ("RAW_VALUE", "HAS_KEY_INDEX", "HAS_PROPERTIES", "VIOLATED_RESTRICTIONS",
                     "DEFAULT_VALUE"):
        assert left_out not in words


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
