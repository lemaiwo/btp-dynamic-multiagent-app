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
}


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
        # named columns, never SELECT * (COUNT(*) is fine: "(" before it)
        for i, token in enumerate(tokens):
            if token.is_symbol("*"):
                before = tokens[i - 1]
                assert before.is_symbol("("), "a * outside COUNT(*)"


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


def _case_span(tokens) -> tuple[int, int]:
    start = next(i for i, t in enumerate(tokens) if t.is_word("CASE"))
    depth = 0
    for i in range(start, len(tokens)):
        if tokens[i].is_word("CASE"):
            depth += 1
        elif tokens[i].is_word("END"):
            depth -= 1
            if depth == 0:
                return start, i
    raise AssertionError("CASE has no END")


def test_ini_contents_value_only_through_the_hiding_case() -> None:
    """The fake does not evaluate SQL, so the rule is held on the tokens: the
    raw VALUE column appears only inside the CASE and as its alias."""
    sql = get_check("iniContents").statements[0].sql
    tokens = tokenize(sql)
    start, end = _case_span(tokens)
    assert tokens[end + 1].is_word("AS") and tokens[end + 2].is_word("VALUE")
    for i, token in enumerate(tokens):
        if token.is_word("VALUE"):
            assert start < i < end or i == end + 2
    case_sql = system.INI_VALUE_SQL
    assert case_sql.startswith("CASE WHEN ")
    assert f"THEN '{system.HIDDEN}' ELSE VALUE END" in case_sql
    for word in ("'password'", "'secret'", "'credential'", "'passphrase'", "'token'"):
        assert f"LOCATE(LOWER(KEY), {word}) > 0" in case_sql
        assert f"LOCATE(LOWER(SECTION), {word}) > 0" in case_sql
    assert "RIGHT(LOWER(KEY), 3) = 'key'" in case_sql
    assert "LOCATE(LOWER(VALUE), 'password') > 0" in case_sql
    # exactly one CASE, and the select list has no second way to VALUE
    assert sum(1 for t in tokens if t.is_word("CASE")) == 1
