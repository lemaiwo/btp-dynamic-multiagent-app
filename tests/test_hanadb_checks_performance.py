"""The ``performance`` checks of ``builtin:hana`` (``agents/hana/checks/performance.py``).

Per check: registered with its category and arguments, its SQL reads only the
monitoring views, and run on the fake driver with two scripted rows it answers
the expected shape and binds. The helpers are the monitoring tests'.
"""

from __future__ import annotations

from agents.hana import checks as registry
from tests.test_hanadb_checks_monitoring import assert_registered, assert_single, run_on_fake

STATEMENT = "SELECT * FROM SAPABAP1.KNA1 WHERE KUNNR = 'customer4711' AND X = 99"

PERFORMANCE = ("memoryAnalysis", "memoryLeaks", "expensiveStatements", "tableHotspots",
               "longRunning", "columnStats", "fragmentationCheck")


def test_the_seven_performance_checks() -> None:
    names = [c.name for c in registry.all_checks() if c.category == "performance"]
    assert sorted(names) == sorted(PERFORMANCE)
    assert registry.get_check("queryPlan") is None


def _composite(answer, labels, columns) -> None:
    assert "error" not in answer, answer
    assert [p["label"] for p in answer["parts"]] == list(labels)
    for part in answer["parts"]:
        assert part["columns"] == columns
        assert part["row_count"] == 2 and part["truncated"] is False


async def test_memory_analysis(monkeypatch) -> None:
    assert_registered("memoryAnalysis", "performance", {"top"},
                      parts=("services", "components"))
    columns = ["HOST", "PORT", "X"]
    rows = [("h1", 30003, 1), ("h1", 30001, 2)]
    answer, executes = await run_on_fake(monkeypatch, "memoryAnalysis", columns, rows, top=9)
    _composite(answer, ("services", "components"), columns)
    assert [e[2] for e in executes] == [(10,), (10,)]


async def test_memory_leaks(monkeypatch) -> None:
    check = assert_registered("memoryLeaks", "performance", {"top", "hours"},
                              parts=("allocators", "growth"))
    assert "_SYS_STATISTICS.HOST_HEAP_ALLOCATORS" in check.statements[1].sql
    columns = ["HOST", "PORT", "CATEGORY"]
    rows = [("h1", 30003, "Pool/A"), ("h1", 30003, "Pool/B")]
    answer, executes = await run_on_fake(monkeypatch, "memoryLeaks", columns, rows, hours=6)
    _composite(answer, ("allocators", "growth"), columns)
    assert executes[0][2] == (51,)
    assert executes[1][2] == (51, 6)


async def test_expensive_statements_masks_the_statement(monkeypatch) -> None:
    check = assert_registered("expensiveStatements", "performance", {"top", "hours"})
    assert check.masked_columns == frozenset({"STATEMENT_STRING"})
    assert "PARAMETERS" not in check.statements[0].sql
    columns = ["START_TIME", "DURATION_MICROSEC", "STATEMENT_STRING"]
    rows = [("t", 9, STATEMENT), ("t", 8, None)]
    answer, executes = await run_on_fake(monkeypatch, "expensiveStatements", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51, 24)
    masked = answer["rows"][0][2]
    assert "customer4711" not in masked and "99" not in masked and "KNA1" in masked
    assert answer["rows"][1][2] is None


async def test_table_hotspots_optional_schema(monkeypatch) -> None:
    check = assert_registered("tableHotspots", "performance", {"top", "schema"})
    assert "CAST(? AS NVARCHAR(256)) IS NULL" in check.statements[0].sql
    columns = ["SCHEMA_NAME", "TABLE_NAME", "READS", "WRITES"]
    rows = [("S", "T1", 5, 1), ("S", "T2", 4, 1)]
    answer, executes = await run_on_fake(monkeypatch, "tableHotspots", columns, rows)
    assert_single(answer, columns)
    assert executes[0][2] == (51, None, None)
    _, executes = await run_on_fake(monkeypatch, "tableHotspots", columns, rows,
                                    schema="SAPHANADB")
    assert executes[0][2] == (51, "SAPHANADB", "SAPHANADB")


async def test_long_running_masks_the_statement(monkeypatch) -> None:
    check = assert_registered("longRunning", "performance", {"top"})
    assert check.masked_columns == frozenset({"STATEMENT_STRING"})
    assert "NANO100_TIME" not in check.statements[0].sql
    columns = ["CONNECTION_ID", "DURATION_SECONDS", "STATEMENT_STRING"]
    rows = [(1, 600, STATEMENT), (2, 30, "SELECT 1 FROM DUMMY")]
    answer, executes = await run_on_fake(monkeypatch, "longRunning", columns, rows, top=2)
    assert_single(answer, columns)
    assert executes[0][2] == (3,)
    assert "customer4711" not in answer["rows"][0][2]


async def test_column_stats(monkeypatch) -> None:
    assert_registered("columnStats", "performance", {"top", "schema", "object"})
    columns = ["SCHEMA_NAME", "TABLE_NAME", "COLUMN_NAME", "ROW_COUNT"]
    rows = [("S", "T", "A", 10), ("S", "T", "B", 10)]
    answer, executes = await run_on_fake(monkeypatch, "columnStats", columns, rows,
                                         schema="SAPHANADB", object="ACDOCA")
    assert_single(answer, columns)
    assert executes[0][2] == (51, "SAPHANADB", "ACDOCA")


async def test_fragmentation_check(monkeypatch) -> None:
    check = assert_registered("fragmentationCheck", "performance", {"top", "schema"})
    assert "MEMORY_SIZE_IN_DELTA" in check.statements[0].sql
    columns = ["SCHEMA_NAME", "TABLE_NAME", "PART_ID", "DELTA_PERCENT"]
    rows = [("S", "T1", 0, 50.5), ("S", "T2", 1, 10.0)]
    answer, executes = await run_on_fake(monkeypatch, "fragmentationCheck", columns, rows,
                                         schema="SAPHANADB", top=20)
    assert_single(answer, columns)
    assert executes[0][2] == (21, "SAPHANADB", "SAPHANADB")
