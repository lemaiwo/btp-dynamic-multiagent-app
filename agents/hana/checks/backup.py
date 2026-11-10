"""``builtin:hana`` checks of the category ``backup``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

The target is on-premise SAP HANA 2.0 (SPS07). Every statement here ran on
SYSTEMDB and a tenant of SPS07 rev 79 (probe, 2026-11-10) except ``progress``,
rewritten on columns probed there. ``COMMENT`` of the backup catalogue is never selected: it is free
text somebody typed, and the word is refused by the registry anyway.
Destination paths and backint ids are left out: they name the backup
server's file system.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register

# Only these log backup settings are read from the configuration (default
# deny): the parameter file keys of [backup] name paths of the server.
_LOG_BACKUP_SETTINGS = (
    "(SECTION = 'persistence' AND \"KEY\" IN ('log_mode', 'enable_auto_log_backup',"
    " 'log_backup_timeout_s', 'log_backup_interval_mode'))"
    " OR (SECTION = 'backup' AND \"KEY\" IN ('log_backup_using_backint',"
    " 'catalog_backup_using_backint', 'data_backup_buffer_size',"
    " 'parallel_data_backup_backint_channels'))"
)

# on-prem: M_BACKUP_CATALOG as in hana-cli (without COMMENT and MESSAGE: a
# failed backup's message quotes file or backint paths); `last_successful`
# looks back 90 days in fixed SQL, so it never aggregates the whole
# catalogue (a type not backed up for 90 days has no row); successful log
# backups are left out of the recent list (one every few minutes per service
# would fill it), failed ones stay. The log backup settings are read from
# M_INIFILE_CONTENTS for named keys only (hana-cli: the whole section).
# on-prem: verified 2026-11-10 on SPS07 rev 79 (last_successful, recent,
# log_settings). `progress`: M_BACKUP_PROGRESS has no TOTAL_SIZE on SPS07
# (SQL code 260); BACKUP_SIZE (the size of the whole backup) stands in, next to
# TRANSFERRED_SIZE: columns verified 2026-11-10 on SPS07 rev 79, statement not
# yet run.
BACKUP_STATUS = Check(
    name="backupStatus",
    category="backup",
    purpose=("Last successful backup per type within 90 days, recent backups (not"
             " successful log ones), progress, log settings."),
    source="bin/backupStatus.js",
    statements=(
        Statement(
            "last_successful",
            "SELECT ENTRY_TYPE_NAME, MAX(SYS_END_TIME) AS LAST_SUCCESSFUL_END_TIME,"
            " SECONDS_BETWEEN(MAX(SYS_END_TIME), CURRENT_TIMESTAMP) AS SECONDS_AGO"
            " FROM SYS.M_BACKUP_CATALOG"
            " WHERE STATE_NAME = 'successful'"
            " AND SYS_END_TIME >= ADD_DAYS(CURRENT_TIMESTAMP, -90)"
            " GROUP BY ENTRY_TYPE_NAME"
            " ORDER BY ENTRY_TYPE_NAME",
            (),
        ),
        Statement(
            "recent",
            "SELECT TOP ? BACKUP_ID, ENTRY_TYPE_NAME, STATE_NAME, SYS_START_TIME, SYS_END_TIME,"
            " SECONDS_BETWEEN(SYS_START_TIME, SYS_END_TIME) AS DURATION_SECONDS"
            " FROM SYS.M_BACKUP_CATALOG"
            " WHERE SYS_START_TIME >= ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)"
            " AND NOT (ENTRY_TYPE_NAME = 'log backup' AND STATE_NAME = 'successful')"
            " ORDER BY SYS_START_TIME DESC",
            ("top_plus_one", "hours"),
        ),
        Statement(
            "progress",
            "SELECT TOP ? HOST, PORT, SERVICE_NAME, BACKUP_ID, ENTRY_TYPE_NAME, STATE_NAME,"
            " SYS_START_TIME, TRANSFERRED_SIZE, BACKUP_SIZE"
            " FROM SYS.M_BACKUP_PROGRESS"
            " ORDER BY SYS_START_TIME DESC, HOST, PORT",
            ("top_plus_one",),
        ),
        Statement(
            "log_settings",
            "SELECT LAYER_NAME, SECTION, \"KEY\", \"VALUE\""
            " FROM SYS.M_INIFILE_CONTENTS"
            " WHERE FILE_NAME = 'global.ini'"
            f" AND ({_LOG_BACKUP_SETTINGS})"
            " ORDER BY SECTION, \"KEY\", LAYER_NAME",
            (),
        ),
    ),
    args=frozenset({"top", "hours"}),
)

# Not a port: hana-cli's backupList lists the files of a local directory its
# own `backup` command wrote and reads nothing from the database. Redefined
# here as the database's data backups (catalogue plus file sizes), so the
# source names the upstream command it replaces. Log backups are left out
# (backupStatus covers the failed ones). DESTINATION_PATH and
# EXTERNAL_BACKUP_ID are left out: they name the backup server's paths. The
# `hours` window and the type filter apply to the catalogue in a subquery,
# before the join, so the file rows of older backups are never joined.
# on-prem: verified 2026-11-10 on SPS07 rev 79
BACKUP_LIST = Check(
    name="backupList",
    category="backup",
    purpose=("Data backups of the last hours from the backup catalogue, newest first, with"
             " size, file count and destination type."),
    source="bin/backupList.js",
    statements=(Statement(
        "",
        "SELECT TOP ? C.BACKUP_ID, C.ENTRY_TYPE_NAME, C.STATE_NAME, C.SYS_START_TIME,"
        " C.SYS_END_TIME, SUM(F.BACKUP_SIZE) AS BACKUP_SIZE,"
        " COUNT(F.BACKUP_ID) AS FILE_COUNT,"
        " MAX(F.DESTINATION_TYPE_NAME) AS DESTINATION_TYPE_NAME"
        " FROM (SELECT ENTRY_ID, BACKUP_ID, ENTRY_TYPE_NAME, STATE_NAME, SYS_START_TIME,"
        " SYS_END_TIME FROM SYS.M_BACKUP_CATALOG"
        " WHERE SYS_START_TIME >= ADD_SECONDS(CURRENT_TIMESTAMP, -3600 * ?)"
        " AND ENTRY_TYPE_NAME <> 'log backup') C"
        " LEFT JOIN SYS.M_BACKUP_CATALOG_FILES F ON F.ENTRY_ID = C.ENTRY_ID"
        " GROUP BY C.BACKUP_ID, C.ENTRY_TYPE_NAME, C.STATE_NAME, C.SYS_START_TIME,"
        " C.SYS_END_TIME"
        " ORDER BY C.SYS_START_TIME DESC",
        ("top_plus_one", "hours"),
    ),),
    args=frozenset({"top", "hours"}),
)

register(BACKUP_STATUS, BACKUP_LIST)
