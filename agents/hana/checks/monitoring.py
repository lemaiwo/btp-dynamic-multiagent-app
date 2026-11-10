"""``builtin:hana`` checks of the category ``monitoring``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

The target is on-premise SAP HANA 2.0 (SPS07). hana-cli's SQL for these
commands was written for SAP HANA Cloud and partly names columns no HANA 2.0
view has (``M_LOCKS.LOCK_OWNER_CONNECTION_ID``, ``SYS.M_ALERTS``,
``SYS.M_CRASH_DUMPS``), so most statements here are rewritten on the
on-premise views rather than copied; the ``# on-prem:`` comment of each check
says how. ``# on-prem: verified 2026-11-10 on SPS07 rev 79`` marks a
statement that ran on SYSTEMDB and a tenant of SPS07 (probe, 2026-11-10);
"(run 2)" one rewritten on columns probed there after the first run, which
the second run then passed; ``# on-prem: unverified`` what has not run yet.

Columns are named, never ``*``: the database user of the first landscape may
read more than the check needs, so the SQL is what limits the answer.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register

# on-prem: the alerts of the statistics server live in _SYS_STATISTICS, not in
# SYS.M_ALERTS (HANA Cloud). On SPS07 STATISTICS_CURRENT_ALERTS carries
# ALERT_NAME and ALERT_USERACTION itself and has no HOST or PORT (SQL code
# 260 for both, 2026-11-10): no join to STATISTICS_ALERT_INFORMATION, and no
# host column (ALERT_DETAILS names the host or service where it matters).
# on-prem: verified 2026-11-10 on SPS07 rev 79 (run 2)
# ALERT_DETAILS is free text and is kept on purpose (lead's decision): an
# alert without its text is not usable for monitoring, and it is the
# statistics server that writes it, not a user. ALERT_USERACTION is SAP's
# fixed advice text for the alert.
ALERTS = Check(
    name="alerts",
    category="monitoring",
    purpose="Current statistics server alerts, highest rating first (rating 5 is the most severe).",
    source="bin/alerts.js",
    statements=(Statement(
        "",
        "SELECT TOP ? ALERT_TIMESTAMP, ALERT_ID, ALERT_NAME, ALERT_RATING,"
        " ALERT_DETAILS, ALERT_USERACTION"
        " FROM _SYS_STATISTICS.STATISTICS_CURRENT_ALERTS"
        " ORDER BY ALERT_RATING DESC, ALERT_TIMESTAMP DESC",
        ("top_plus_one",),
    ),),
)

# on-prem: M_LOCKS has no owner/waiter connection columns on HANA 2.0; who
# waits for whom is M_BLOCKED_TRANSACTIONS, the sessions behind both
# transactions come from M_CONNECTIONS by TRANSACTION_ID.
# on-prem: verified 2026-11-10 on SPS07 rev 79
BLOCKING = Check(
    name="blocking",
    category="monitoring",
    purpose=("Transactions waiting for a lock, with the connection and user on both"
             " sides, oldest wait first."),
    source="bin/blocking.js",
    statements=(Statement(
        "",
        "SELECT TOP ? B.HOST, B.PORT, B.BLOCKED_TIME, B.LOCK_TYPE, B.LOCK_MODE,"
        " B.WAITING_SCHEMA_NAME, B.WAITING_OBJECT_NAME,"
        " B.BLOCKED_TRANSACTION_ID, W.CONNECTION_ID AS BLOCKED_CONNECTION_ID,"
        " W.USER_NAME AS BLOCKED_USER_NAME,"
        " B.LOCK_OWNER_TRANSACTION_ID, O.CONNECTION_ID AS OWNER_CONNECTION_ID,"
        " O.USER_NAME AS OWNER_USER_NAME, O.CONNECTION_STATUS AS OWNER_CONNECTION_STATUS"
        " FROM SYS.M_BLOCKED_TRANSACTIONS B"
        " LEFT JOIN SYS.M_CONNECTIONS W ON W.TRANSACTION_ID = B.BLOCKED_TRANSACTION_ID"
        " LEFT JOIN SYS.M_CONNECTIONS O ON O.TRANSACTION_ID = B.LOCK_OWNER_TRANSACTION_ID"
        " ORDER BY B.BLOCKED_TIME ASC",
        ("top_plus_one",),
    ),),
)

# on-prem: hana-cli joins M_LOCKS on owner/waiter connection columns HANA 2.0
# does not have; a two-party lock cycle is two M_BLOCKED_TRANSACTIONS rows
# that wait for each other.
# on-prem: verified 2026-11-10 on SPS07 rev 79
DEADLOCKS = Check(
    name="deadlocks",
    category="monitoring",
    purpose=("Pairs of transactions waiting for each other; usually empty, as HANA"
             " ends a deadlock itself."),
    source="bin/deadlocks.js",
    statements=(Statement(
        "",
        "SELECT TOP ? B1.HOST, B1.PORT, B1.BLOCKED_TIME,"
        " B1.BLOCKED_TRANSACTION_ID, B1.LOCK_OWNER_TRANSACTION_ID,"
        " B1.LOCK_TYPE, B1.LOCK_MODE, B1.WAITING_SCHEMA_NAME, B1.WAITING_OBJECT_NAME,"
        " B2.LOCK_TYPE AS OTHER_LOCK_TYPE, B2.LOCK_MODE AS OTHER_LOCK_MODE,"
        " B2.WAITING_SCHEMA_NAME AS OTHER_SCHEMA_NAME,"
        " B2.WAITING_OBJECT_NAME AS OTHER_OBJECT_NAME"
        " FROM SYS.M_BLOCKED_TRANSACTIONS B1"
        " INNER JOIN SYS.M_BLOCKED_TRANSACTIONS B2"
        " ON B1.BLOCKED_TRANSACTION_ID = B2.LOCK_OWNER_TRANSACTION_ID"
        " AND B1.LOCK_OWNER_TRANSACTION_ID = B2.BLOCKED_TRANSACTION_ID"
        " ORDER BY B1.BLOCKED_TIME ASC",
        ("top_plus_one",),
    ),),
)

# on-prem: M_TRACEFILES as in hana-cli, named columns instead of *; FILE_NAME
# is a name, not a path. HOST and FILE_NAME are what traceContents takes.
# on-prem: verified 2026-11-10 on SPS07 rev 79
TRACES = Check(
    name="traces",
    category="monitoring",
    purpose="Trace files of the database services, most recently changed first.",
    source="bin/traces.js",
    statements=(Statement(
        "",
        "SELECT TOP ? HOST, FILE_NAME, FILE_SIZE, FILE_MTIME"
        " FROM SYS.M_TRACEFILES"
        " ORDER BY FILE_MTIME DESC",
        ("top_plus_one",),
    ),),
)

# on-prem: M_TRACEFILE_CONTENTS needs equality predicates on HOST and
# FILE_NAME (SQL code 500 with FILE_NAME alone, 2026-11-10 on SPS07 rev 79), so
# both are required binds: `host` and `object` as `traces` lists them. A host
# taken from a subquery or a join was refused with 500 for a file that exists
# (column-probes, 2026-11-10). The tail is read newest chunk first instead of
# from a computed offset. OFFSET is quoted: a keyword in other positions.
# Trace text may hold anything a service wrote, so the check exists only with
# allow_select.
# on-prem: unverified (first live call: the probe's host does not exist, and
# SPS07 answered SQL code 8 for it)
TRACE_CONTENTS = Check(
    name="traceContents",
    category="monitoring",
    purpose=("End of one trace file (host and object = file name from traces), newest first;"
             " unmasked, may hold statements."),
    source="bin/traceContents.js",
    statements=(Statement(
        "",
        'SELECT TOP ? HOST, FILE_NAME, "OFFSET", CONTENT'
        " FROM SYS.M_TRACEFILE_CONTENTS"
        " WHERE HOST = ? AND FILE_NAME = ?"
        ' ORDER BY "OFFSET" DESC',
        ("top_plus_one", "host", "object"),
    ),),
    args=frozenset({"top", "object", "host"}),
    required=frozenset({"object", "host"}),
    requires_select=True,
    max_rows_cap=200,
)

# on-prem: SYS.M_CRASH_DUMPS is HANA Cloud only; on-premise a crash or
# emergency dump is a trace file whose name says so (`*crashdump*.trc`,
# `*emergencydump*.trc`), listed by M_TRACEFILES.
# on-prem: verified 2026-11-10 on SPS07 rev 79
CRASH_DUMPS = Check(
    name="crashDumps",
    category="monitoring",
    purpose="Crash and emergency dump files written in the last hours, newest first.",
    source="bin/crashDumps.js",
    statements=(Statement(
        "",
        "SELECT TOP ? HOST, FILE_NAME, FILE_SIZE, FILE_MTIME"
        " FROM SYS.M_TRACEFILES"
        " WHERE (LOWER(FILE_NAME) LIKE '%crashdump%'"
        " OR LOWER(FILE_NAME) LIKE '%emergencydump%')"
        " AND FILE_MTIME >= ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)"
        " ORDER BY FILE_MTIME DESC",
        ("top_plus_one", "hours"),
    ),),
    args=frozenset({"top", "hours"}),
)

# on-prem: M_SERVICE_REPLICATION as in hana-cli with HANA 2.0 column names;
# M_SERVICE_REPLICATION_STATISTICS (HANA Cloud) is left out. OPERATION_MODE is
# no column of M_SERVICE_REPLICATION on SPS07 (SQL code 260, 2026-11-10; it is
# one of M_SYSTEM_REPLICATION) and is left out; SECONDARY_FULLY_RECOVERABLE,
# BACKLOG_SIZE and BACKLOG_TIME were probed there and added.
# on-prem: verified 2026-11-10 on SPS07 rev 79 (run 2)
REPLICATION_STATUS = Check(
    name="replicationStatus",
    category="monitoring",
    purpose=("System replication per service and secondary site; no rows means no"
             " system replication."),
    source="bin/replicationStatus.js",
    statements=(Statement(
        "",
        "SELECT TOP ? HOST, PORT, SITE_NAME, SECONDARY_HOST, SECONDARY_PORT,"
        " SECONDARY_SITE_NAME, REPLICATION_MODE,"
        " REPLICATION_STATUS, REPLICATION_STATUS_DETAILS, SECONDARY_ACTIVE_STATUS,"
        " SECONDARY_FULLY_RECOVERABLE,"
        " SHIPPED_LOG_POSITION_TIME, LAST_LOG_POSITION_TIME,"
        " SECONDS_BETWEEN(SHIPPED_LOG_POSITION_TIME, LAST_LOG_POSITION_TIME)"
        " AS LOG_SHIPPING_DELAY_SECONDS, BACKLOG_SIZE, BACKLOG_TIME"
        " FROM SYS.M_SERVICE_REPLICATION"
        " ORDER BY SITE_NAME, HOST, PORT",
        ("top_plus_one",),
    ),),
)

register(ALERTS, BLOCKING, DEADLOCKS, TRACES, TRACE_CONTENTS, CRASH_DUMPS, REPLICATION_STATUS)
