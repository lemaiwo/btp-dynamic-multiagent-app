"""``builtin:hana`` checks of the category ``performance``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

The target is on-premise SAP HANA 2.0 (SPS07). hana-cli reads several of
these views with ``SELECT *`` and falls back on another ``ORDER BY`` when a
column is missing; here every column is named (the database user may read
more than a check needs, so the SQL limits the answer) and the statement is
written for the HANA 2.0 view. ``# on-prem: unverified`` marks a view or
column not yet seen on an SPS07 system: the landscape run decides.

``queryPlan`` is not a check: ``EXPLAIN PLAN`` failed on the landscape.

An optional ``schema`` filter is written
``(CAST(? AS NVARCHAR(256)) IS NULL OR SCHEMA_NAME = ?)``: the CAST gives the
NULL bind a type (a bare ``? IS NULL`` can fail with "cannot determine
parameter type"); the form itself is unverified on HANA (``tableHotspots``,
``fragmentationCheck``).
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register

_SCHEMA_FILTER = "(CAST(? AS NVARCHAR(256)) IS NULL OR SCHEMA_NAME = ?)"

# on-prem: M_MEMORY_OBJECTS of hana-cli is replaced by the service view
# M_SERVICE_MEMORY and the per-component view M_SERVICE_COMPONENT_MEMORY
# (component view and EFFECTIVE_ALLOCATION_LIMIT: unverified).
MEMORY_ANALYSIS = Check(
    name="memoryAnalysis",
    category="performance",
    purpose=("Memory per service against its allocation limit, and the components"
             " using most memory."),
    source="bin/memoryAnalysis.js",
    statements=(
        Statement(
            "services",
            "SELECT TOP ? HOST, PORT, SERVICE_NAME, TOTAL_MEMORY_USED_SIZE,"
            " EFFECTIVE_ALLOCATION_LIMIT, HEAP_MEMORY_USED_SIZE, HEAP_MEMORY_ALLOCATED_SIZE,"
            " SHARED_MEMORY_USED_SIZE, CODE_SIZE"
            " FROM SYS.M_SERVICE_MEMORY"
            " ORDER BY TOTAL_MEMORY_USED_SIZE DESC",
            ("top_plus_one",),
        ),
        Statement(
            "components",
            "SELECT TOP ? HOST, PORT, COMPONENT, USED_MEMORY_SIZE"
            " FROM SYS.M_SERVICE_COMPONENT_MEMORY"
            " ORDER BY USED_MEMORY_SIZE DESC",
            ("top_plus_one",),
        ),
    ),
)

# on-prem: hana-cli groups M_HEAP_MEMORY by a COMPONENT column; here the
# largest allocators now, plus their growth over the last hours from the
# statistics server's history _SYS_STATISTICS.HOST_HEAP_ALLOCATORS
# (history table and its SERVER_TIMESTAMP column: unverified). A leak is an
# allocator that keeps growing, which one snapshot cannot show.
# on-prem: unverified (time zone of SERVER_TIMESTAMP; cost with hours=720)
MEMORY_LEAKS = Check(
    name="memoryLeaks",
    category="performance",
    purpose="Largest heap allocators now, and the allocators that grew most in the last hours.",
    source="bin/memoryLeaks.js",
    statements=(
        Statement(
            "allocators",
            "SELECT TOP ? HOST, PORT, CATEGORY, EXCLUSIVE_SIZE_IN_USE,"
            " EXCLUSIVE_ALLOCATED_SIZE"
            " FROM SYS.M_HEAP_MEMORY"
            " ORDER BY EXCLUSIVE_SIZE_IN_USE DESC",
            ("top_plus_one",),
        ),
        Statement(
            "growth",
            "SELECT TOP ? HOST, PORT, CATEGORY,"
            " MIN(EXCLUSIVE_SIZE_IN_USE) AS MIN_SIZE_IN_USE,"
            " MAX(EXCLUSIVE_SIZE_IN_USE) AS MAX_SIZE_IN_USE,"
            " MAX(EXCLUSIVE_SIZE_IN_USE) - MIN(EXCLUSIVE_SIZE_IN_USE) AS GROWTH,"
            " MIN(SERVER_TIMESTAMP) AS FIRST_SNAPSHOT, MAX(SERVER_TIMESTAMP) AS LAST_SNAPSHOT"
            " FROM _SYS_STATISTICS.HOST_HEAP_ALLOCATORS"
            " WHERE SERVER_TIMESTAMP >= ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)"
            " GROUP BY HOST, PORT, CATEGORY"
            " ORDER BY MAX(EXCLUSIVE_SIZE_IN_USE) - MIN(EXCLUSIVE_SIZE_IN_USE) DESC",
            ("top_plus_one", "hours"),
        ),
    ),
    args=frozenset({"top", "hours"}),
)

# on-prem: M_EXPENSIVE_STATEMENTS as in hana-cli, named columns instead of *.
# PARAMETERS (bind values: business data) and ERROR_TEXT are left out;
# STATEMENT_STRING is masked. APP_USER and APPLICATION_NAME: unverified.
EXPENSIVE_STATEMENTS = Check(
    name="expensiveStatements",
    category="performance",
    purpose=("Slowest statements of the last hours from the expensive statements trace;"
             " empty while that trace is off."),
    source="bin/expensiveStatements.js",
    statements=(Statement(
        "",
        "SELECT TOP ? START_TIME, HOST, PORT, CONNECTION_ID, DB_USER, APP_USER,"
        " APPLICATION_NAME, OPERATION, DURATION_MICROSEC, CPU_TIME, MEMORY_SIZE,"
        " LOCK_WAIT_DURATION, RECORDS, ERROR_CODE, STATEMENT_HASH, STATEMENT_STRING"
        " FROM SYS.M_EXPENSIVE_STATEMENTS"
        " WHERE START_TIME >= ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)"
        " ORDER BY DURATION_MICROSEC DESC",
        ("top_plus_one", "hours"),
    ),),
    args=frozenset({"top", "hours"}),
    masked_columns=frozenset({"STATEMENT_STRING"}),
)

# on-prem: hana-cli reads M_TABLE_STATISTICS (and M_TABLE_PARTITIONS) with *
# and an ORDER BY that falls back when TOTAL_ACCESS_COUNT is missing. The
# read and write counters of a column table are in M_CS_TABLES (one row per
# partition, summed here); row store tables are not covered. READ_COUNT and
# WRITE_COUNT: unverified.
# cost: unverified on a large system (whole M_CS_TABLES grouped when no schema)
TABLE_HOTSPOTS = Check(
    name="tableHotspots",
    category="performance",
    purpose=("Column tables with the most reads and writes since load, partitions summed;"
             " optional schema filter."),
    source="bin/tableHotspots.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, SUM(READ_COUNT) AS READS,"
        " SUM(WRITE_COUNT) AS WRITES, SUM(MERGE_COUNT) AS MERGES,"
        " SUM(RECORD_COUNT) AS RECORDS, SUM(MEMORY_SIZE_IN_TOTAL) AS MEMORY_SIZE,"
        " COUNT(*) AS PARTITION_COUNT"
        " FROM SYS.M_CS_TABLES"
        f" WHERE {_SCHEMA_FILTER}"
        " GROUP BY SCHEMA_NAME, TABLE_NAME"
        " ORDER BY SUM(READ_COUNT) + SUM(WRITE_COUNT) DESC",
        ("top_plus_one", "schema", "schema"),
    ),),
    args=frozenset({"top", "schema"}),
)

# on-prem: hana-cli computes the duration from NANO100_TIME, which HANA 2.0's
# M_ACTIVE_STATEMENTS does not have; here it is the time since
# LAST_EXECUTED_TIME of a statement whose STATEMENT_STATUS is ACTIVE, and the
# user comes from M_CONNECTIONS (LAST_EXECUTED_TIME, STATEMENT_STATUS,
# ALLOCATED_MEMORY_SIZE: unverified). The caller's own connection is left out.
LONG_RUNNING = Check(
    name="longRunning",
    category="performance",
    purpose="Statements running now, longest running first, with the user and memory they use.",
    source="bin/longRunning.js",
    statements=(Statement(
        "",
        "SELECT TOP ? A.HOST, A.PORT, A.CONNECTION_ID, C.USER_NAME, A.STATEMENT_ID,"
        " A.LAST_EXECUTED_TIME,"
        " SECONDS_BETWEEN(A.LAST_EXECUTED_TIME, CURRENT_TIMESTAMP) AS DURATION_SECONDS,"
        " A.ALLOCATED_MEMORY_SIZE, A.STATEMENT_HASH, A.STATEMENT_STRING"
        " FROM SYS.M_ACTIVE_STATEMENTS A"
        " LEFT JOIN SYS.M_CONNECTIONS C"
        " ON C.HOST = A.HOST AND C.PORT = A.PORT AND C.CONNECTION_ID = A.CONNECTION_ID"
        " WHERE A.STATEMENT_STATUS = 'ACTIVE' AND A.CONNECTION_ID <> CURRENT_CONNECTION"
        " ORDER BY A.LAST_EXECUTED_TIME ASC",
        ("top_plus_one",),
    ),),
    masked_columns=frozenset({"STATEMENT_STRING"}),
)

# on-prem: M_CS_COLUMNS as in hana-cli, named columns instead of *, with
# schema and table as exact binds (hana-cli: LIKE patterns). COUNT is quoted:
# it is also a function name.
COLUMN_STATS = Check(
    name="columnStats",
    category="performance",
    purpose=("Columns of one column table (schema and object required) by memory size,"
             " with distinct values and compression."),
    source="bin/columnStats.js",
    statements=(Statement(
        "",
        'SELECT TOP ? SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, PART_ID, "COUNT" AS ROW_COUNT,'
        " DISTINCT_COUNT, MEMORY_SIZE_IN_TOTAL, MEMORY_SIZE_IN_MAIN, MEMORY_SIZE_IN_DELTA,"
        " COMPRESSION_TYPE, LOADED"
        " FROM SYS.M_CS_COLUMNS"
        " WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?"
        " ORDER BY MEMORY_SIZE_IN_TOTAL DESC, COLUMN_NAME, PART_ID",
        ("top_plus_one", "schema", "object"),
    ),),
    args=frozenset({"top", "schema", "object"}),
    required=frozenset({"schema", "object"}),
)

# on-prem: M_CS_TABLES as in hana-cli: the share of a column table partition's
# memory held in its delta store (LAST_MERGE_TIME and RAW_RECORD_COUNT_IN_DELTA:
# unverified).
# cost: unverified on a large system (whole M_CS_TABLES sorted when no schema)
FRAGMENTATION_CHECK = Check(
    name="fragmentationCheck",
    category="performance",
    purpose=("Column table partitions with the largest delta store, with the delta share"
             " and last merge; optional schema filter."),
    source="bin/fragmentationCheck.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, PART_ID, RECORD_COUNT,"
        " RAW_RECORD_COUNT_IN_DELTA, MEMORY_SIZE_IN_TOTAL, MEMORY_SIZE_IN_MAIN,"
        " MEMORY_SIZE_IN_DELTA,"
        " ROUND(100 * MEMORY_SIZE_IN_DELTA / NULLIF(MEMORY_SIZE_IN_TOTAL, 0), 2)"
        " AS DELTA_PERCENT,"
        " LAST_MERGE_TIME"
        " FROM SYS.M_CS_TABLES"
        f" WHERE {_SCHEMA_FILTER}"
        " ORDER BY MEMORY_SIZE_IN_DELTA DESC",
        ("top_plus_one", "schema", "schema"),
    ),),
    args=frozenset({"top", "schema"}),
)

register(MEMORY_ANALYSIS, MEMORY_LEAKS, EXPENSIVE_STATEMENTS, TABLE_HOTSPOTS, LONG_RUNNING,
         COLUMN_STATS, FRAGMENTATION_CHECK)
