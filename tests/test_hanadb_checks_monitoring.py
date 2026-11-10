"""The ``monitoring`` checks of ``builtin:hana`` (``agents/hana/checks/monitoring.py``).

Per check: it is registered with its category and arguments, its SQL reads
only the monitoring views, and run through the real toolset on the fake driver
with two scripted rows it answers the expected shape with the expected binds.
The generic invariants (guard, row limit, binds) are in ``test_hanadb_checks.py``.
``run_on_fake`` is shared with the performance and backup tests.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from agents.hana import checks as registry
from agents.hana import tools
from agents.hana.connection import HanaSystem
from agents.hana.guard import check_select
from agents.hana.sqltokens import tokenize
from tests import hanadb_helpers as h

NAMED_COLUMNS_ONLY = re.compile(r"(?:\bSELECT|,|\.)\s*(?:TOP \?\s*)?\*")
"""``SELECT *``, ``SELECT TOP ? *``, ``, *`` or ``T.*``: never in a check."""

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
        assert not NAMED_COLUMNS_ONLY.search(statement.sql), "select named columns"
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
    assert "_SYS_STATISTICS.STATISTICS_CURRENT_ALERTS" in check.statements[0].sql
    columns = ["ALERT_TIMESTAMP", "ALERT_ID", "ALERT_NAME", "ALERT_RATING",
               "ALERT_DETAILS", "HOST", "PORT"]
    rows = [("2026-11-10 10:00:00", 2, "Disk usage", 4, "d", "h1", 30003),
            ("2026-11-10 09:00:00", 3, "Inactive services", 3, "d", "h1", 30003)]
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
    assert_registered("traces", "monitoring", {"top"})
    columns = ["HOST", "FILE_NAME", "FILE_SIZE", "FILE_MTIME"]
    rows = [("h1", "indexserver_h1.30003.000.trc", 10, "t"),
            ("h1", "nameserver_h1.30001.000.trc", 20, "t")]
    answer, executes = await run_on_fake(monkeypatch, "traces", columns, rows, top=2)
    assert_single(answer, columns)
    assert executes[0][2] == (3,)


async def test_trace_contents(monkeypatch) -> None:
    check = assert_registered("traceContents", "monitoring", {"top", "object"})
    assert check.requires_select is True and check.max_rows_cap == 200
    columns = ["HOST", "FILE_NAME", "OFFSET", "CONTENT"]
    rows = [("h1", "f.trc", 2000, "line b"), ("h1", "f.trc", 1000, "line a")]
    answer, executes = await run_on_fake(
        monkeypatch, "traceContents", columns, rows, allow_select=True, object="f.trc")
    assert_single(answer, columns)
    assert executes[0][2] == (51, "f.trc")
    answer, _ = await run_on_fake(
        monkeypatch, "traceContents", columns, rows, allow_select=True, top=201)
    assert answer["error"]["argument"] == "top"


async def test_trace_contents_is_hidden_without_allow_select(monkeypatch) -> None:
    assert "traceContents" not in {
        c.name for c in registry.visible_checks(allow_select=False)}
    assert "traceContents" in {c.name for c in registry.visible_checks(allow_select=True)}
    answer, executes = await run_on_fake(
        monkeypatch, "traceContents", ["A"], [(1,)], object="f.trc")
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
    assert_registered("replicationStatus", "monitoring", {"top"})
    columns = ["HOST", "PORT", "SITE_NAME", "SECONDARY_HOST", "SECONDARY_PORT",
               "SECONDARY_SITE_NAME", "REPLICATION_MODE", "OPERATION_MODE",
               "REPLICATION_STATUS", "REPLICATION_STATUS_DETAILS",
               "SECONDARY_ACTIVE_STATUS", "SHIPPED_LOG_POSITION_TIME",
               "LAST_LOG_POSITION_TIME", "LOG_SHIPPING_DELAY_SECONDS"]
    rows = [tuple(range(14)), tuple(range(14, 28))]
    answer, executes = await run_on_fake(monkeypatch, "replicationStatus", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51,)
