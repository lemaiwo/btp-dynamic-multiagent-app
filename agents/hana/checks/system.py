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
