"""``builtin:hana`` checks of the category ``system``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

The SQL is ported from hana-cli (each check names its ``bin/<command>.js``),
written for SAP HANA Cloud; the target is on-premise SAP HANA 2.0 SPS07. What
changed per check:

- named columns instead of ``SELECT *``: the database user of the first
  landscape has wide privileges, so what a check returns is limited by its SQL
  alone;
- views that hana-cli reads but that do not exist (or hold other columns) on
  premise are replaced by the on-premise view, said in a ``# on-prem:``
  comment next to the check;
- a large view has a row limit bound to ``top_plus_one``.

``# on-prem: unverified`` marks a view or column not yet seen on a HANA 2.0
SPS07 system; the landscape run checks those first and a check that fails
there is rewritten or dropped.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register

HIDDEN = "<hidden>"
"""What ``iniContents`` answers instead of a value that may be a secret."""

# The one place a configuration value is chosen: a value is hidden when its
# key or section names a secret, or when the value itself does. Fixed SQL, so
# the words are literals of this repository, never arguments. LOCATE / RIGHT
# instead of LIKE '%...%': the registry refuses "%s" in a statement (a format
# placeholder), which '%secret%' would hold. The raw VALUE column is reachable
# only through this CASE.
_SECRET_WORDS = ("password", "secret", "credential", "passphrase", "token", "private")
_SECRET_TEST = " OR ".join(
    [f"LOCATE(LOWER(KEY), '{w}') > 0" for w in _SECRET_WORDS]
    + ["RIGHT(LOWER(KEY), 3) = 'key'"]
    + [f"LOCATE(LOWER(SECTION), '{w}') > 0" for w in _SECRET_WORDS]
    + [f"LOCATE(LOWER(VALUE), '{w}') > 0" for w in ("password", "secret", "pwd=")]
)
INI_VALUE_SQL = f"CASE WHEN {_SECRET_TEST} THEN '{HIDDEN}' ELSE VALUE END AS VALUE"

# The optional filter on a name: ``(? IS NULL OR X = ?)`` with a NULL bind is
# not verified on HANA, so the first ``?`` is typed explicitly.
_OPTIONAL_FILE = "(CAST(? AS NVARCHAR(256)) IS NULL OR FILE_NAME = ?)"


# ---- single views (task 13a) ----------------------------------------------------------

register(
    Check(
        name="systemInfo",
        category="system",
        purpose=(
            "System overview (version, status, memory, CPU, disks) and the"
            " services with their state."
        ),
        source="bin/systemInfo.js",
        statements=(
            Statement(
                "overview",
                "SELECT SECTION, NAME, STATUS, VALUE FROM SYS.M_SYSTEM_OVERVIEW"
                " ORDER BY SECTION, NAME",
                (),
            ),
            Statement(
                "services",
                "SELECT HOST, PORT, SERVICE_NAME, PROCESS_ID, ACTIVE_STATUS, SQL_PORT,"
                " COORDINATOR_TYPE FROM SYS.M_SERVICES ORDER BY HOST, PORT",
                (),
            ),
        ),
        max_rows_cap=200,
    ),
    Check(
        name="hostInformation",
        category="system",
        purpose=(
            "Host facts (key and value per host) and the memory, swap and CPU use"
            " of each host."
        ),
        source="bin/hostInformation.js",
        statements=(
            Statement(
                "information",
                "SELECT TOP ? HOST, KEY, VALUE FROM SYS.M_HOST_INFORMATION ORDER BY HOST, KEY",
                ("top_plus_one",),
            ),
            # on-prem: the memory figures hana-cli looks for in M_HOST_INFORMATION
            # are columns of M_HOST_RESOURCE_UTILIZATION on HANA 2.0.
            Statement(
                "resources",
                "SELECT HOST, FREE_PHYSICAL_MEMORY, USED_PHYSICAL_MEMORY, FREE_SWAP_SPACE,"
                " USED_SWAP_SPACE, ALLOCATION_LIMIT, INSTANCE_TOTAL_MEMORY_USED_SIZE,"
                " INSTANCE_TOTAL_MEMORY_PEAK_USED_SIZE, INSTANCE_TOTAL_MEMORY_ALLOCATED_SIZE,"
                " TOTAL_CPU_USER_TIME, TOTAL_CPU_SYSTEM_TIME, TOTAL_CPU_WIO_TIME,"
                " TOTAL_CPU_IDLE_TIME FROM SYS.M_HOST_RESOURCE_UTILIZATION ORDER BY HOST",
                (),
            ),
        ),
    ),
    Check(  # on-prem: unverified (M_DISK_USAGE and its columns)
        name="disks",
        category="system",
        purpose="Disks of the system (size and use per usage type) and disk use per usage type.",
        source="bin/disks.js",
        statements=(
            Statement(
                "disks",
                "SELECT DISK_ID, HOST, USAGE_TYPE, PATH, FILESYSTEM_TYPE, TOTAL_SIZE, USED_SIZE"
                " FROM SYS.M_DISKS ORDER BY HOST, USAGE_TYPE",
                (),
            ),
            Statement(
                "usage",
                "SELECT HOST, USAGE_TYPE, USED_SIZE FROM SYS.M_DISK_USAGE"
                " ORDER BY HOST, USAGE_TYPE",
                (),
            ),
        ),
        max_rows_cap=200,
    ),
    Check(
        name="ports",
        category="system",
        purpose="The internal and SQL port of each service.",
        source="bin/ports.js",
        statements=(
            Statement(
                "",
                "SELECT HOST, SERVICE_NAME, PORT, SQL_PORT FROM SYS.M_SERVICES"
                " ORDER BY HOST, PORT",
                (),
            ),
        ),
        max_rows_cap=200,
    ),
    Check(  # on-prem: unverified (M_DATA_VOLUMES and M_DATA_VOLUME_PAGE_STATISTICS columns)
        name="dataVolumes",
        category="system",
        purpose=(
            "Data volumes per service, their data files with size and use, and"
            " page use per size class."
        ),
        source="bin/dataVolumes.js",
        statements=(
            Statement(
                "volumes",
                "SELECT HOST, PORT, VOLUME_ID, FILE_NAME FROM SYS.M_DATA_VOLUMES"
                " ORDER BY HOST, PORT, VOLUME_ID",
                (),
            ),
            # on-prem: hana-cli's M_DATA_VOLUME_STATISTICS is replaced by
            # M_VOLUME_FILES (size and use of each data file), which HANA 2.0 has.
            Statement(
                "files",
                "SELECT HOST, PORT, VOLUME_ID, FILE_TYPE, FILE_NAME, USED_SIZE, TOTAL_SIZE"
                " FROM SYS.M_VOLUME_FILES WHERE FILE_TYPE = 'DATA'"
                " ORDER BY HOST, PORT, VOLUME_ID",
                (),
            ),
            Statement(
                "page_statistics",
                "SELECT TOP ? HOST, PORT, VOLUME_ID, PAGE_SIZECLASS, PAGE_SIZE,"
                " USED_BLOCK_COUNT, FILL_RATIO FROM SYS.M_DATA_VOLUME_PAGE_STATISTICS"
                " ORDER BY HOST, PORT, VOLUME_ID, PAGE_SIZE",
                ("top_plus_one",),
            ),
        ),
        max_rows_cap=200,
    ),
    Check(  # on-prem: unverified (M_FEATURES and FEATURE_VERSION)
        name="features",
        category="system",
        purpose="The features the database offers, per component.",
        source="bin/features.js",
        statements=(
            Statement(
                "",
                "SELECT TOP ? COMPONENT_NAME, FEATURE_NAME, FEATURE_VERSION FROM SYS.M_FEATURES"
                " ORDER BY COMPONENT_NAME, FEATURE_NAME",
                ("top_plus_one",),
            ),
        ),
    ),
    Check(  # on-prem: unverified (M_FEATURE_USAGE columns)
        name="featureUsage",
        category="system",
        purpose="How often each feature was used, and when last, per component.",
        source="bin/featureUsage.js",
        statements=(
            Statement(
                "",
                "SELECT TOP ? COMPONENT_NAME, FEATURE_NAME, IS_DEPRECATED, OBJECT_COUNT,"
                " CALL_COUNT, LAST_TIMESTAMP, LAST_USER_NAME, LAST_APPLICATION_NAME"
                " FROM SYS.M_FEATURE_USAGE ORDER BY COMPONENT_NAME, FEATURE_NAME",
                ("top_plus_one",),
            ),
        ),
    ),
    Check(  # on-prem: unverified (M_INIFILES columns LAYER_NAME, TENANT_NAME, HOST)
        name="iniFiles",
        category="system",
        purpose="The configuration (.ini) files and the layers they are set in.",
        source="bin/iniFiles.js",
        statements=(
            Statement(
                "",
                "SELECT TOP ? FILE_NAME, LAYER_NAME, TENANT_NAME, HOST FROM SYS.M_INIFILES"
                " ORDER BY FILE_NAME, LAYER_NAME",
                ("top_plus_one",),
            ),
        ),
    ),
    Check(
        name="iniContents",
        category="system",
        purpose=(
            "Configuration parameters (file, layer, section, key, value); object ="
            " one file name; secrets hidden."
        ),
        source="bin/iniContents.js",
        statements=(
            Statement(
                "",
                "SELECT TOP ? FILE_NAME, LAYER_NAME, TENANT_NAME, HOST, SECTION, KEY, "
                + INI_VALUE_SQL
                + " FROM SYS.M_INIFILE_CONTENTS WHERE "
                + _OPTIONAL_FILE
                + " ORDER BY FILE_NAME, SECTION, KEY, LAYER_NAME",
                ("top_plus_one", "object", "object"),
            ),
        ),
        args=frozenset({"top", "object"}),
    ),
)


# ---- composite checks (task 13b) ------------------------------------------------------
#
# hana-cli's JavaScript scoring (thresholds, HEALTHY / WARNING verdicts) is not
# ported: these checks answer the figures, and the model judges them.

_MEMORY_USE = (
    "SELECT HOST, ALLOCATION_LIMIT, INSTANCE_TOTAL_MEMORY_USED_SIZE,"
    " INSTANCE_TOTAL_MEMORY_ALLOCATED_SIZE, FREE_PHYSICAL_MEMORY, USED_PHYSICAL_MEMORY,"
    " CASE WHEN ALLOCATION_LIMIT > 0"
    " THEN ROUND(100.0 * INSTANCE_TOTAL_MEMORY_USED_SIZE / ALLOCATION_LIMIT, 2)"
    " END AS USED_PERCENT_OF_LIMIT"
    " FROM SYS.M_HOST_RESOURCE_UTILIZATION ORDER BY HOST"
)
"""Memory per host. on-prem: hana-cli reads INSTANCE_TOTAL_MEMORY_* from
M_HOST_INFORMATION (a key/value view on HANA 2.0) and TOTAL_MEMORY_* from
M_HOST_RESOURCE_UTILIZATION; both live in M_HOST_RESOURCE_UTILIZATION as
INSTANCE_TOTAL_MEMORY_* and ALLOCATION_LIMIT."""

_BLOCKED = (
    "SELECT COUNT(*) AS BLOCKED_COUNT,"
    " MAX(SECONDS_BETWEEN(BLOCKED_TIME, CURRENT_TIMESTAMP)) AS MAX_WAIT_SECONDS"
    " FROM SYS.M_BLOCKED_TRANSACTIONS"
)
"""Lock waits now. on-prem: hana-cli counts SYS.M_LOCKS (LOCK_WAIT_TIME), a
HANA Cloud view; HANA 2.0 reports waiting transactions in M_BLOCKED_TRANSACTIONS."""

register(
    Check(  # on-prem: unverified (M_DATABASES columns, M_TRANSACTIONS status values)
        name="healthCheck",
        category="system",
        purpose=(
            "Health figures: overview, databases, disks, memory, connections, transactions,"
            " last backups."
        ),
        source="bin/healthCheck.js",
        statements=(
            Statement(
                "overview",
                "SELECT SECTION, NAME, STATUS, VALUE FROM SYS.M_SYSTEM_OVERVIEW"
                " ORDER BY SECTION, NAME",
                (),
            ),
            # on-prem: hana-cli's replication columns are no part of M_DATABASES;
            # replication has its own check (replicationStatus).
            Statement(
                "databases",
                "SELECT DATABASE_NAME, ACTIVE_STATUS, ACTIVE_STATUS_DETAILS, RESTART_MODE"
                " FROM SYS.M_DATABASES ORDER BY DATABASE_NAME",
                (),
            ),
            Statement(
                "disks",
                "SELECT HOST, USAGE_TYPE, PATH, TOTAL_SIZE, USED_SIZE FROM SYS.M_DISKS"
                " ORDER BY HOST, USAGE_TYPE",
                (),
            ),
            Statement("memory", _MEMORY_USE, ()),
            # on-prem: M_SESSIONS (HANA Cloud) is M_CONNECTIONS on HANA 2.0.
            Statement(
                "connections",
                "SELECT CONNECTION_STATUS, COUNT(*) AS CONNECTION_COUNT FROM SYS.M_CONNECTIONS"
                " GROUP BY CONNECTION_STATUS ORDER BY CONNECTION_STATUS",
                (),
            ),
            # on-prem: grouped by status and type instead of hana-cli's filter on
            # TRANSACTION_STATUS = 'RUNNING', a value HANA 2.0 does not report.
            Statement(
                "transactions",
                "SELECT TRANSACTION_STATUS, TRANSACTION_TYPE, COUNT(*) AS TRANSACTION_COUNT,"
                " COUNT(DISTINCT CONNECTION_ID) AS CONNECTION_COUNT FROM SYS.M_TRANSACTIONS"
                " GROUP BY TRANSACTION_STATUS, TRANSACTION_TYPE"
                " ORDER BY TRANSACTION_STATUS, TRANSACTION_TYPE",
                (),
            ),
            # on-prem: M_BACKUP_CATALOG_ENTRIES is M_BACKUP_CATALOG; the latest
            # start and end per backup type and state, not one row.
            Statement(
                "backups",
                "SELECT ENTRY_TYPE_NAME, STATE_NAME, COUNT(*) AS BACKUP_COUNT,"
                " MAX(SYS_START_TIME) AS LAST_START_TIME, MAX(SYS_END_TIME) AS LAST_END_TIME"
                " FROM SYS.M_BACKUP_CATALOG GROUP BY ENTRY_TYPE_NAME, STATE_NAME"
                " ORDER BY ENTRY_TYPE_NAME, STATE_NAME",
                (),
            ),
        ),
        max_rows_cap=200,
    ),
    Check(  # on-prem: unverified (_SYS_STATISTICS.STATISTICS_CURRENT_ALERTS, BLOCKED_TIME)
        name="diagnose",
        category="system",
        purpose=(
            "Quick diagnosis: inactive services, memory per host, alerts by rating, lock"
            " waits, last data backup."
        ),
        source="bin/diagnose.js",
        statements=(
            Statement(
                "services",
                "SELECT HOST, PORT, SERVICE_NAME, ACTIVE_STATUS, DETAIL FROM SYS.M_SERVICES"
                " WHERE ACTIVE_STATUS <> 'YES' ORDER BY HOST, PORT",
                (),
            ),
            Statement("memory", _MEMORY_USE, ()),
            # on-prem: SYS.M_ALERTS is HANA Cloud; the statistics server keeps the
            # current alerts in _SYS_STATISTICS (rating 1 information .. 5 error).
            Statement(
                "alerts",
                "SELECT ALERT_RATING, COUNT(*) AS ALERT_COUNT"
                " FROM _SYS_STATISTICS.STATISTICS_CURRENT_ALERTS"
                " GROUP BY ALERT_RATING ORDER BY ALERT_RATING DESC",
                (),
            ),
            Statement("blocked", _BLOCKED, ()),
            Statement(
                "last_backup",
                "SELECT MAX(SYS_END_TIME) AS LAST_SUCCESSFUL_DATA_BACKUP FROM SYS.M_BACKUP_CATALOG"
                " WHERE STATE_NAME = 'successful' AND ENTRY_TYPE_NAME = 'complete data backup'",
                (),
            ),
        ),
        max_rows_cap=200,
    ),
    Check(  # on-prem: unverified (M_TABLE_STATISTICS and M_CACHES columns)
        name="recommendations",
        category="system",
        purpose=(
            "Tuning figures: most read tables, costliest statements, caches, large or"
            " unmerged tables, lock waits, memory."
        ),
        source="bin/recommendations.js",
        statements=(
            # on-prem: hana-cli's SCAN_COUNT > RECORD_COUNT is no column pair of
            # M_TABLE_STATISTICS on HANA 2.0; the most read tables instead.
            Statement(
                "read_tables",
                "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, READ_COUNT, WRITE_COUNT, SELECT_COUNT"
                " FROM SYS.M_TABLE_STATISTICS ORDER BY READ_COUNT DESC",
                ("top_plus_one",),
            ),
            # on-prem: TOTAL_EXECUTION_TIME and EXECUTION_COUNT per statement hash
            # are M_SQL_PLAN_CACHE columns (M_EXPENSIVE_STATEMENTS holds one row per
            # execution and only while its trace is on). No statement text.
            Statement(
                "statements",
                "SELECT TOP ? STATEMENT_HASH, EXECUTION_COUNT, TOTAL_EXECUTION_TIME,"
                " AVG_EXECUTION_TIME FROM SYS.M_SQL_PLAN_CACHE"
                " ORDER BY TOTAL_EXECUTION_TIME DESC",
                ("top_plus_one",),
            ),
            # on-prem: CACHE_HIT_RATIO / RESULT_CACHE_HIT_RATIO do not exist; the
            # counts per cache instead.
            Statement(
                "caches",
                "SELECT HOST, PORT, CACHE_ID, TOTAL_SIZE, USED_SIZE, ENTRY_COUNT, HIT_COUNT,"
                " MISS_COUNT FROM SYS.M_CACHES ORDER BY HOST, PORT, CACHE_ID",
                (),
            ),
            # on-prem: MEMORY_SIZE_IN_TOTAL is a column of M_CS_TABLES (there is no
            # COMPRESSION_RATIO); a large delta or a stale compression shows here.
            Statement(
                "large_tables",
                "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, RECORD_COUNT, MEMORY_SIZE_IN_TOTAL,"
                " MEMORY_SIZE_IN_DELTA, LAST_COMPRESSED_RECORD_COUNT FROM SYS.M_CS_TABLES"
                " ORDER BY MEMORY_SIZE_IN_TOTAL DESC",
                ("top_plus_one",),
            ),
            Statement("blocked", _BLOCKED, ()),
            Statement("memory", _MEMORY_USE, ()),
        ),
        max_rows_cap=100,
    ),
    Check(  # on-prem: unverified (M_WORKLOAD columns, SYS.WORKLOAD_CLASSES columns)
        name="workloadManagement",
        category="system",
        purpose=(
            "Workload counters per service and the workload classes with their limits"
            " (no catalogue discovery)."
        ),
        source="bin/workloadManagement.js",
        statements=(
            Statement(
                "workload",
                "SELECT HOST, PORT, EXECUTION_COUNT, COMPILATION_COUNT,"
                " UPDATE_TRANSACTION_COUNT, COMMIT_COUNT, ROLLBACK_COUNT FROM SYS.M_WORKLOAD"
                " ORDER BY HOST, PORT",
                (),
            ),
            # hana-cli discovers the workload views through SYS.TABLES and
            # SYS.TABLE_COLUMNS (catalogue reads, Phase 2); the on-premise view of
            # the classes is named here instead.
            Statement(
                "classes",
                "SELECT TOP ? WORKLOAD_CLASS_NAME, PRIORITY, STATEMENT_MEMORY_LIMIT,"
                " STATEMENT_THREAD_LIMIT, TOTAL_STATEMENT_MEMORY_LIMIT,"
                " TOTAL_STATEMENT_THREAD_LIMIT, IS_ENABLED FROM SYS.WORKLOAD_CLASSES"
                " ORDER BY WORKLOAD_CLASS_NAME",
                ("top_plus_one",),
            ),
        ),
        max_rows_cap=200,
    ),
)
