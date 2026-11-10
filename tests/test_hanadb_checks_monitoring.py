"""The ``monitoring`` checks of ``builtin:hana`` (``agents/hana/checks/monitoring.py``).

Per check: it is registered with its category and arguments, its SQL reads
only the monitoring views, and run through the real toolset on the fake driver
with two scripted rows it answers the expected shape with the expected binds.
The generic invariants (guard, row limit, binds) are in ``test_hanadb_checks.py``.
``run_on_fake`` is shared with the performance and backup tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.hana import checks as registry
from agents.hana import tools
from agents.hana.connection import HanaSystem
from agents.hana.guard import check_select
from agents.hana.sqltokens import tokenize
from tests import hanadb_helpers as h


def star_columns(sql: str) -> int:
    """How many ``*`` of ``sql`` stand for "every column" (``SELECT *``,
    ``SELECT TOP ? *``, ``, *``, ``T.*``), read on tokens: a ``*`` after
    ``(`` (``COUNT(*)``), a number, a column or a bind is arithmetic."""
    tokens = tokenize(sql)
    count = 0
    for index, token in enumerate(tokens):
        if not token.is_symbol("*") or index == 0:
            continue
        before = tokens[index - 1]
        if before.is_word("SELECT", "DISTINCT") or before.is_symbol(",", "."):
            count += 1
        elif (before.kind == "param" and index >= 2
              and tokens[index - 2].is_word("TOP")):
            count += 1
    return count


@pytest.mark.parametrize("sql, stars", [
    ("SELECT * FROM SYS.M_TABLES", 1),
    ("SELECT TOP ? * FROM SYS.M_TABLES", 1),
    ("SELECT DISTINCT * FROM SYS.M_TABLES", 1),
    ("SELECT A, * FROM SYS.M_TABLES", 1),
    ("SELECT T.* FROM SYS.M_TABLES T", 1),
    ("SELECT COUNT(*) AS N FROM SYS.M_TABLES", 0),
    ("SELECT 100 * A, A * B, ? * 2 FROM SYS.M_TABLES WHERE X > 3600 * ?", 0),
    ("SELECT TOP ? A FROM SYS.M_TABLES WHERE B = '*'", 0),
])
def test_star_columns_reads_tokens(sql: str, stars: int) -> None:
    assert star_columns(sql) == stars


OAUTH = {"systems": [{"name": "sib_tenant", "destination": "hana-dest-one"}]}


def _factory(name: str, destination: str) -> HanaSystem:
    return HanaSystem(name, destination, resolver=h.FakeResolver(), tokens=h.FakeTokens())


async def run_on_fake(monkeypatch: pytest.MonkeyPatch, check: str, columns: list[str],
                      rows: list[tuple], *, allow_select: bool = False,
                      **args: Any) -> tuple[dict[str, Any], list[tuple]]:
    """Run ``check`` through ``run_check`` on the fake driver; every statement
    answers ``columns`` / ``rows``. Returns the answer and the executes."""
    db = h.install(monkeypatch)
    db.description = tuple((c,) for c in columns)
    db.rows = rows
    oauth = {**OAUTH, "allow_select": True} if allow_select else OAUTH
    toolset = tools.hana_toolset(oauth, auth_mode="destination", system_factory=_factory)
    answer = await toolset.tools["run_check"].function(
        system="sib_tenant", check=check, **args)
    return answer, [c for c in db.calls if c[0] == "execute"]


def assert_registered(name: str, category: str, args: set[str], *,
                      parts: tuple[str, ...] = ("",)) -> registry.Check:
    check = registry.get_check(name)
    assert check is not None, name
    assert check.category == category
    assert check.args == frozenset(args)
    assert tuple(s.label for s in check.statements) == parts
    for statement in check.statements:
        assert tokenize(statement.sql)
        check_select(statement.sql)
        assert star_columns(statement.sql) == 0, "select named columns"
    return check


def assert_single(answer: dict[str, Any], columns: list[str], rows: int = 2) -> None:
    assert "error" not in answer, answer
    assert answer["columns"] == columns
    assert answer["row_count"] == rows and len(answer["rows"]) == rows
    assert answer["truncated"] is False


MONITORING = ("alerts", "blocking", "deadlocks", "traces", "traceContents", "crashDumps",
              "replicationStatus")


def test_the_seven_monitoring_checks() -> None:
    names = [c.name for c in registry.all_checks() if c.category == "monitoring"]
    assert sorted(names) == sorted(MONITORING)


async def test_alerts(monkeypatch) -> None:
    check = assert_registered("alerts", "monitoring", {"top"})
    sql = check.statements[0].sql
    assert "_SYS_STATISTICS.STATISTICS_CURRENT_ALERTS" in sql
    # SPS07 (2026-11-10): the current alerts carry their name and no HOST / PORT,
    # so there is no join to STATISTICS_ALERT_INFORMATION and no host column.
    words = {t.value for t in tokenize(sql) if t.kind == "word"}
    assert not words & {"HOST", "PORT", "JOIN", "STATISTICS_ALERT_INFORMATION"}
    columns = ["ALERT_TIMESTAMP", "ALERT_ID", "ALERT_NAME", "ALERT_RATING",
               "ALERT_DETAILS", "ALERT_USERACTION"]
    rows = [("2026-11-10 10:00:00", 2, "Disk usage", 4, "d", "u"),
            ("2026-11-10 09:00:00", 3, "Inactive services", 3, "d", "u")]
    answer, executes = await run_on_fake(monkeypatch, "alerts", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51,)


async def test_blocking(monkeypatch) -> None:
    check = assert_registered("blocking", "monitoring", {"top"})
    assert "M_BLOCKED_TRANSACTIONS" in check.statements[0].sql
    columns = ["HOST", "PORT", "BLOCKED_TIME", "LOCK_TYPE", "LOCK_MODE",
               "WAITING_SCHEMA_NAME", "WAITING_OBJECT_NAME", "BLOCKED_TRANSACTION_ID",
               "BLOCKED_CONNECTION_ID", "BLOCKED_USER_NAME", "LOCK_OWNER_TRANSACTION_ID",
               "OWNER_CONNECTION_ID", "OWNER_USER_NAME", "OWNER_CONNECTION_STATUS"]
    rows = [tuple(range(14)), tuple(range(14, 28))]
    answer, executes = await run_on_fake(monkeypatch, "blocking", columns, rows, top=5)
    assert_single(answer, columns)
    assert executes[0][2] == (6,)


async def test_deadlocks(monkeypatch) -> None:
    assert_registered("deadlocks", "monitoring", {"top"})
    columns = ["HOST", "PORT", "BLOCKED_TIME", "BLOCKED_TRANSACTION_ID",
               "LOCK_OWNER_TRANSACTION_ID", "LOCK_TYPE", "LOCK_MODE",
               "WAITING_SCHEMA_NAME", "WAITING_OBJECT_NAME", "OTHER_LOCK_TYPE",
               "OTHER_LOCK_MODE", "OTHER_SCHEMA_NAME", "OTHER_OBJECT_NAME"]
    rows = [tuple(range(13)), tuple(range(13, 26))]
    answer, executes = await run_on_fake(monkeypatch, "deadlocks", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51,)


async def test_traces(monkeypatch) -> None:
    check = assert_registered("traces", "monitoring", {"top"})
    # traceContents takes the host and the file name as traces lists them.
    words = {t.value for t in tokenize(check.statements[0].sql) if t.kind == "word"}
    assert {"HOST", "FILE_NAME"} <= words
    columns = ["HOST", "FILE_NAME", "FILE_SIZE", "FILE_MTIME"]
    rows = [("h1", "indexserver_h1.30003.000.trc", 10, "t"),
            ("h1", "nameserver_h1.30001.000.trc", 20, "t")]
    answer, executes = await run_on_fake(monkeypatch, "traces", columns, rows, top=2)
    assert_single(answer, columns)
    assert executes[0][2] == (3,)


async def test_trace_contents(monkeypatch) -> None:
    check = assert_registered("traceContents", "monitoring", {"top", "object", "host"})
    assert check.requires_select is True and check.max_rows_cap == 200
    # SQL code 500 on SPS07 without both: M_TRACEFILE_CONTENTS needs equality
    # predicates on HOST and FILE_NAME.
    assert "WHERE HOST = ? AND FILE_NAME = ?" in check.statements[0].sql
    columns = ["HOST", "FILE_NAME", "OFFSET", "CONTENT"]
    rows = [("h1", "f.trc", 2000, "line b"), ("h1", "f.trc", 1000, "line a")]
    answer, executes = await run_on_fake(
        monkeypatch, "traceContents", columns, rows, allow_select=True, object="f.trc",
        host="h1")
    assert_single(answer, columns)
    assert executes[0][2] == (51, "h1", "f.trc")
    assert "unmasked" in check.purpose
    answer, _ = await run_on_fake(
        monkeypatch, "traceContents", columns, rows, allow_select=True, object="f.trc",
        host="h1", top=201)
    assert answer["error"]["argument"] == "top"


@pytest.mark.parametrize("given, missing", [
    ({}, "object"), ({"host": "h1"}, "object"), ({"object": "f.trc"}, "host")])
async def test_trace_contents_requires_host_and_file_name(monkeypatch, given, missing) -> None:
    assert registry.get_check("traceContents").required == frozenset({"object", "host"})
    answer, executes = await run_on_fake(
        monkeypatch, "traceContents", ["A"], [(1,)], allow_select=True, **given)
    assert answer["error"]["code"] == "invalid_argument"
    assert answer["error"]["argument"] == missing
    assert executes == []


async def test_trace_contents_is_hidden_without_allow_select(monkeypatch) -> None:
    assert "traceContents" not in {
        c.name for c in registry.visible_checks(allow_select=False)}
    assert "traceContents" in {c.name for c in registry.visible_checks(allow_select=True)}
    answer, executes = await run_on_fake(
        monkeypatch, "traceContents", ["A"], [(1,)], object="f.trc", host="h1")
    assert answer["error"]["code"] == "unknown_check"
    assert executes == []


async def test_crash_dumps(monkeypatch) -> None:
    check = assert_registered("crashDumps", "monitoring", {"top", "hours"})
    assert "M_CRASH_DUMPS" not in check.statements[0].sql
    columns = ["HOST", "FILE_NAME", "FILE_SIZE", "FILE_MTIME"]
    rows = [("h1", "indexserver_h1.30003.crashdump.20261110.trc", 1, "t"),
            ("h1", "indexserver_h1.30003.emergencydump.20261109.trc", 2, "t")]
    answer, executes = await run_on_fake(monkeypatch, "crashDumps", columns, rows, hours=48)
    assert_single(answer, columns)
    assert executes[0][2] == (51, 48)


async def test_replication_status(monkeypatch) -> None:
    check = assert_registered("replicationStatus", "monitoring", {"top"})
    # SPS07: M_SERVICE_REPLICATION has no OPERATION_MODE (it is a column of
    # M_SYSTEM_REPLICATION only).
    words = {t.value for t in tokenize(check.statements[0].sql) if t.kind == "word"}
    assert "OPERATION_MODE" not in words
    columns = ["HOST", "PORT", "SITE_NAME", "SECONDARY_HOST", "SECONDARY_PORT",
               "SECONDARY_SITE_NAME", "REPLICATION_MODE",
               "REPLICATION_STATUS", "REPLICATION_STATUS_DETAILS",
               "SECONDARY_ACTIVE_STATUS", "SECONDARY_FULLY_RECOVERABLE",
               "SHIPPED_LOG_POSITION_TIME", "LAST_LOG_POSITION_TIME",
               "LOG_SHIPPING_DELAY_SECONDS", "BACKLOG_SIZE", "BACKLOG_TIME"]
    rows = [tuple(range(16)), tuple(range(16, 32))]
    answer, executes = await run_on_fake(monkeypatch, "replicationStatus", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51,)
