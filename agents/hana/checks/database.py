"""``builtin:hana`` checks of the category ``database``.

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

Phase 2: catalogue reads of hana-cli's database-info commands. The list
checks (``schemas`` ... ``libraries``) return names and metadata of catalogue
objects: type, owner, validity, create time. Never a ``DEFINITION`` (an
object's source is the inspect checks' job), never ``COMMENTS`` (free text
somebody typed) and never a row of a table. The composite checks
(``dependencies`` ... ``tableGroups``) read the catalogue views behind
hana-cli's analysis commands; whatever hana-cli computes in JavaScript or
reads from the tables themselves (a recursive tree, time buckets of a series
table, fixed "forecast" rows) is left out.

Optional ``schema`` / ``object`` filters compare with ``=``: an exact name.
hana-cli takes a ``LIKE`` pattern there, but the argument rule (letters,
digits, ``_ $ # . /``) lets no ``%`` or ``*`` through, and with ``LIKE`` an
``_`` would match any character: ``MY_TABLE`` would also return ``MYXTABLE``,
a superset the model would read as the exact answer. ``spatialData``,
``objects`` and ``indexes`` require ``schema``: unfiltered, they scan and sort
the whole catalogue, and ``top`` bounds the rows, not the work.

``status`` hands the model ``CURRENT_USER``, the database account of the
destination; that may be a person's account, and the name then stays in the
run activity and the chat's stored history like any tool result.

The target is on-premise SAP HANA 2.0 (SPS07); a ``# on-prem:`` comment per
check says how it differs from hana-cli. The columns are those of the SAP
HANA 2.0 System Views Reference (SPS08 edition read). Every statement of this
module ran on a landscape on 2026-11-13 (SPS07 revision 79), in the probe's
wrapped form with sample binds.
:data:`CHECKS` holds them in registration order; its test file
(``tests/test_hanadb_checks_database.py``) pins their names.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register


def _named(column: str) -> str:
    """The optional exact-name filter on ``column``: no value, no filter."""
    return f"(CAST(? AS NVARCHAR(256)) IS NULL OR {column} = ?)"


_IN_SCHEMA = _named("SCHEMA_NAME")
_SCHEMA = ("schema", "schema")
_OBJECT = ("object", "object")

# Session context keys that describe the connection, not a person or an
# application's own variables (default deny: APPLICATIONUSER names a person,
# a USER section key is whatever an application set).
_SESSION_KEYS = (
    "'APPLICATION', 'CLIENT', 'LOCALE', 'LOCALE_SAP', 'DATE_FORMAT', 'TIME_FORMAT',"
    " 'TIMESTAMP_FORMAT'"
)

# on-prem: hana-cli reads CURRENT_USER and CURRENT_SCHEMA from DUMMY, every
# key of M_SESSION_CONTEXT of this connection and GRANTED_ROLES /
# GRANTED_PRIVILEGES with GRANTOR. Here: CURRENT_SCHEMA is left out (the app
# pins the session to SYS, so it always says SYS); the session context is
# read for named keys only; roles and privileges are names, without GRANTOR
# (a user name) and without the column of a column privilege. The user
# part answers the database account name (possibly a person's), which the
# model sees and the run activity / chat history keep.
# on-prem: verified 2026-11-13 on SPS07 rev 79
STATUS = Check(
    name="status",
    category="database",
    purpose=("The database user of this connection, named session settings, its granted"
             " roles and privileges (no grantors)."),
    source="bin/status.js",
    statements=(
        Statement(
            "user",
            "SELECT CURRENT_USER AS USER_NAME FROM SYS.DUMMY",
            (),
        ),
        Statement(
            "session",
            "SELECT \"KEY\", \"VALUE\", SECTION FROM SYS.M_SESSION_CONTEXT"
            " WHERE CONNECTION_ID = CURRENT_CONNECTION"
            f" AND \"KEY\" IN ({_SESSION_KEYS})"
            " ORDER BY \"KEY\"",
            (),
        ),
        Statement(
            "roles",
            "SELECT TOP ? ROLE_SCHEMA_NAME, ROLE_NAME, IS_GRANTABLE"
            " FROM SYS.GRANTED_ROLES"
            " WHERE GRANTEE = CURRENT_USER"
            " ORDER BY ROLE_SCHEMA_NAME, ROLE_NAME",
            ("top_plus_one",),
        ),
        Statement(
            "privileges",
            "SELECT TOP ? PRIVILEGE, OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, IS_GRANTABLE,"
            " IS_VALID"
            " FROM SYS.GRANTED_PRIVILEGES"
            " WHERE GRANTEE = CURRENT_USER"
            " ORDER BY OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME, PRIVILEGE",
            ("top_plus_one",),
        ),
    ),
)

# on-prem: hana-cli's version reports its own, Node's and the CF / btp CLI
# versions (utils/cf.js, utils/btp.js); only the database part is kept, from
# M_DATABASE. HOST is left out (a host name of the database).
# on-prem: verified 2026-11-13 on SPS07 rev 79
VERSION = Check(
    name="version",
    category="database",
    purpose="The database's system id, name, version, usage and start time (no host).",
    source="bin/version.js",
    statements=(Statement(
        "",
        "SELECT SYSTEM_ID, DATABASE_NAME, VERSION, USAGE, START_TIME FROM SYS.M_DATABASE",
        (),
    ),),
    args=frozenset(),
)

# on-prem: hana-cli selects * and by default only schemas with
# HAS_PRIVILEGES = 'TRUE'; here every schema the user may see, with the flag
# as a column.
# on-prem: verified 2026-11-13 on SPS07 rev 79
SCHEMAS = Check(
    name="schemas",
    category="database",
    purpose="Schemas with owner, create time and whether this user has privileges on them.",
    source="bin/schemas.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, SCHEMA_OWNER, HAS_PRIVILEGES, CREATE_TIME"
        " FROM SYS.SCHEMAS"
        f" WHERE {_IN_SCHEMA}"
        " ORDER BY SCHEMA_NAME",
        ("top_plus_one", *_SCHEMA),
    ),),
    args=frozenset({"top", "schema"}),
)

# on-prem: hana-cli (utils/database/hanaDirect.js listTables) selects
# TABLE_OID and COMMENTS; here the table's type flags and create time,
# COMMENTS left out. hana-cli shows no record count, so M_TABLES is not read.
# on-prem: verified 2026-11-13 on SPS07 rev 79
TABLES = Check(
    name="tables",
    category="database",
    purpose=("Tables with type, column or row store, temporary, primary key, create time"
             " (no comments)."),
    source="bin/tables.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, TABLE_TYPE, IS_COLUMN_TABLE, IS_TEMPORARY,"
        " HAS_PRIMARY_KEY, CREATE_TIME"
        " FROM SYS.TABLES"
        f" WHERE {_IN_SCHEMA} AND {_named('TABLE_NAME')}"
        " ORDER BY SCHEMA_NAME, TABLE_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: hana-cli selects VIEW_OID and COMMENTS; here type, validity,
# parameters and create time, COMMENTS and DEFINITION left out.
# on-prem: verified 2026-11-13 on SPS07 rev 79
VIEWS = Check(
    name="views",
    category="database",
    purpose="Views with type, validity, parameters and create time (no definition or comments).",
    source="bin/views.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, VIEW_NAME, VIEW_TYPE, IS_VALID, HAS_PARAMETERS, CREATE_TIME"
        " FROM SYS.VIEWS"
        f" WHERE {_IN_SCHEMA} AND {_named('VIEW_NAME')}"
        " ORDER BY SCHEMA_NAME, VIEW_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: as hana-cli plus type, read-only flag, validity and owner;
# DEFINITION left out.
# on-prem: verified 2026-11-13 on SPS07 rev 79
PROCEDURES = Check(
    name="procedures",
    category="database",
    purpose="Procedures with type, security, read-only, validity, owner, create time (no source).",
    source="bin/procedures.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, PROCEDURE_NAME, PROCEDURE_TYPE, SQL_SECURITY, READ_ONLY,"
        " IS_VALID, OWNER_NAME, CREATE_TIME"
        " FROM SYS.PROCEDURES"
        f" WHERE {_IN_SCHEMA} AND {_named('PROCEDURE_NAME')}"
        " ORDER BY SCHEMA_NAME, PROCEDURE_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: as hana-cli plus usage type, validity and owner; DEFINITION left
# out.
# on-prem: verified 2026-11-13 on SPS07 rev 79
FUNCTIONS = Check(
    name="functions",
    category="database",
    purpose="Functions with usage type, security, validity, owner, create time (no source).",
    source="bin/functions.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, FUNCTION_NAME, FUNCTION_USAGE_TYPE, SQL_SECURITY, IS_VALID,"
        " OWNER_NAME, CREATE_TIME"
        " FROM SYS.FUNCTIONS"
        f" WHERE {_IN_SCHEMA} AND {_named('FUNCTION_NAME')}"
        " ORDER BY SCHEMA_NAME, FUNCTION_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: as hana-cli; CONSTRAINT is quoted (a reserved word); the object
# filter is on the index name, as in hana-cli.
# on-prem: verified 2026-11-13 on SPS07 rev 79
INDEXES = Check(
    name="indexes",
    category="database",
    purpose="Indexes of one schema with their table, index type, constraint and create time.",
    source="bin/indexes.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, INDEX_NAME, INDEX_TYPE, \"CONSTRAINT\","
        " CREATE_TIME"
        " FROM SYS.INDEXES"
        f" WHERE {_IN_SCHEMA} AND {_named('INDEX_NAME')}"
        " ORDER BY SCHEMA_NAME, TABLE_NAME, INDEX_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
    required=frozenset({"schema"}),
)

# on-prem: as hana-cli without OBJECT_OID and OBJECT_CATEGORY.
# on-prem: verified 2026-11-13 on SPS07 rev 79
OBJECTS = Check(
    name="objects",
    category="database",
    purpose="Catalogue objects of every type in one schema, with name and type.",
    source="bin/objects.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, OBJECT_NAME, OBJECT_TYPE"
        " FROM SYS.OBJECTS"
        f" WHERE {_IN_SCHEMA} AND {_named('OBJECT_NAME')}"
        " ORDER BY OBJECT_TYPE, SCHEMA_NAME, OBJECT_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
    required=frozenset({"schema"}),
)

# on-prem: hana-cli reads M_SEQUENCES (cache statistics with CURRENT_VALUE);
# here the definition from SEQUENCES. RESET_BY_QUERY (a query text) is left
# out, and so is the current value.
# on-prem: verified 2026-11-13 on SPS07 rev 79
SEQUENCES = Check(
    name="sequences",
    category="database",
    purpose=("Sequences with start, bounds, increment, cycling, cache size, create time"
             " (no reset query)."),
    source="bin/sequences.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, SEQUENCE_NAME, START_NUMBER, MIN_VALUE, MAX_VALUE,"
        " INCREMENT_BY, IS_CYCLED, CACHE_SIZE, CREATE_TIME"
        " FROM SYS.SEQUENCES"
        f" WHERE {_IN_SCHEMA} AND {_named('SEQUENCE_NAME')}"
        " ORDER BY SCHEMA_NAME, SEQUENCE_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: as hana-cli plus validity; hana-cli's third filter on the target
# object name is left out (the object filter is on the synonym's name).
# on-prem: verified 2026-11-13 on SPS07 rev 79
SYNONYMS = Check(
    name="synonyms",
    category="database",
    purpose="Synonyms with the object they point to, its type, validity and create time.",
    source="bin/synonyms.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, SYNONYM_NAME, OBJECT_SCHEMA, OBJECT_NAME, OBJECT_TYPE,"
        " IS_VALID, CREATE_TIME"
        " FROM SYS.SYNONYMS"
        f" WHERE {_IN_SCHEMA} AND {_named('SYNONYM_NAME')}"
        " ORDER BY SCHEMA_NAME, SYNONYM_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: as hana-cli plus validity, enabled flag and create time;
# DEFINITION left out. The object filter matches the trigger's name or its
# table's name (hana-cli filters both, as two arguments).
# on-prem: verified 2026-11-13 on SPS07 rev 79
TRIGGERS = Check(
    name="triggers",
    category="database",
    purpose="Triggers with their table, timing, event, level, validity, enabled flag (no source).",
    source="bin/triggers.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, TRIGGER_NAME, OWNER_NAME, SUBJECT_TABLE_SCHEMA,"
        " SUBJECT_TABLE_NAME, TRIGGER_ACTION_TIME, TRIGGER_EVENT, TRIGGERED_ACTION_LEVEL,"
        " IS_VALID, IS_ENABLED, CREATE_TIME"
        " FROM SYS.TRIGGERS"
        f" WHERE {_IN_SCHEMA}"
        " AND (CAST(? AS NVARCHAR(256)) IS NULL OR TRIGGER_NAME = ?"
        " OR SUBJECT_TABLE_NAME = ?)"
        " ORDER BY SCHEMA_NAME, TRIGGER_NAME",
        ("top_plus_one", *_SCHEMA, "object", "object", "object"),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: hana-cli selects OWNER_NAME, which LIBRARIES has no column of on
# HANA 2.0; DEFINITION left out.
# on-prem: verified 2026-11-13 on SPS07 rev 79
LIBRARIES = Check(
    name="libraries",
    category="database",
    purpose="SQLScript and built-in libraries with type, validity and create time (no source).",
    source="bin/libraries.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, LIBRARY_NAME, LIBRARY_TYPE, IS_VALID, CREATE_TIME"
        " FROM SYS.LIBRARIES"
        f" WHERE {_IN_SCHEMA} AND {_named('LIBRARY_NAME')}"
        " ORDER BY SCHEMA_NAME, LIBRARY_NAME",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: hana-cli builds a tree by querying OBJECT_DEPENDENCIES once per
# node in JavaScript. Here one level each way, as HANA records it:
# DEPENDENCY_TYPE 1 is direct, 2 indirect (through another object), 5 a
# foreign key; 3 and 4 are SAP internal. The model can call the check again
# for a node it wants to follow.
# on-prem: verified 2026-11-13 on SPS07 rev 79
DEPENDENCIES = Check(
    name="dependencies",
    category="database",
    purpose=("What one object uses and what uses it, one level each way with HANA's dependency"
             " type (no recursive tree)."),
    source="bin/dependencies.js",
    statements=(
        Statement(
            "uses",
            "SELECT TOP ? BASE_SCHEMA_NAME, BASE_OBJECT_NAME, BASE_OBJECT_TYPE, DEPENDENCY_TYPE"
            " FROM SYS.OBJECT_DEPENDENCIES"
            " WHERE DEPENDENT_SCHEMA_NAME = ? AND DEPENDENT_OBJECT_NAME = ?"
            " ORDER BY DEPENDENCY_TYPE, BASE_SCHEMA_NAME, BASE_OBJECT_NAME",
            ("top_plus_one", "schema", "object"),
        ),
        Statement(
            "used_by",
            "SELECT TOP ? DEPENDENT_SCHEMA_NAME, DEPENDENT_OBJECT_NAME, DEPENDENT_OBJECT_TYPE,"
            " DEPENDENCY_TYPE"
            " FROM SYS.OBJECT_DEPENDENCIES"
            " WHERE BASE_SCHEMA_NAME = ? AND BASE_OBJECT_NAME = ?"
            " ORDER BY DEPENDENCY_TYPE, DEPENDENT_SCHEMA_NAME, DEPENDENT_OBJECT_NAME",
            ("top_plus_one", "schema", "object"),
        ),
    ),
    args=frozenset({"top", "schema", "object"}),
    required=frozenset({"schema", "object"}),
)

# on-prem: hana-cli's column list of TABLE_COLUMNS with its spatial type
# rule (ST_* plus the four plain names); the column filter is left out (the
# object filter is on the table name). Only the column catalogue is read,
# never the spatial values.
# on-prem: verified 2026-11-13 on SPS07 rev 79
SPATIAL_DATA = Check(
    name="spatialData",
    category="database",
    purpose=("Table columns of a spatial type (ST_GEOMETRY, ST_POINT, ...) in one schema,"
             " names only, no values."),
    source="bin/spatialData.js",
    statements=(Statement(
        "",
        "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, DATA_TYPE_NAME, POSITION"
        " FROM SYS.TABLE_COLUMNS"
        f" WHERE {_IN_SCHEMA} AND {_named('TABLE_NAME')}"
        " AND (DATA_TYPE_NAME LIKE 'ST\\_%' ESCAPE '\\'"
        " OR DATA_TYPE_NAME IN ('GEOMETRY', 'POINT', 'LINESTRING', 'POLYGON'))"
        " ORDER BY SCHEMA_NAME, TABLE_NAME, POSITION",
        ("top_plus_one", *_SCHEMA, *_OBJECT),
    ),),
    args=frozenset({"top", "schema", "object"}),
    required=frozenset({"schema"}),
)

# on-prem: hana-cli reads SYS.TIMESERIES_TABLES, which HANA 2.0 does not
# have; the series tables are in SERIES_TABLES (definition) and
# SERIES_KEY_COLUMNS. MIN_VALUE / MAX_VALUE (the allowed range, a value) and
# the reserved calendar columns are left out. hana-cli's other subcommands
# read the series table's rows (time buckets, min / max of a column) or
# return fixed text; they are dropped: no table row is ever read.
# on-prem: verified 2026-11-13 on SPS07 rev 79
TIME_SERIES_TOOLS = Check(
    name="timeSeriesTools",
    category="database",
    purpose="Series tables with their period definition and series key columns; never their rows.",
    source="bin/timeSeriesTools.js",
    statements=(
        Statement(
            "series_tables",
            "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, PERIOD_TYPE_NAME, IS_EQUIDISTANT, IS_INSTANT,"
            " INCREMENT_BY, IS_MISSING_ALLOWED, PERIOD_START_COLUMN, PERIOD_END_COLUMN,"
            " PERIOD_INSTANT_COLUMN"
            " FROM SYS.SERIES_TABLES"
            f" WHERE {_IN_SCHEMA} AND {_named('TABLE_NAME')}"
            " ORDER BY SCHEMA_NAME, TABLE_NAME",
            ("top_plus_one", *_SCHEMA, *_OBJECT),
        ),
        Statement(
            "series_keys",
            "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, POSITION"
            " FROM SYS.SERIES_KEY_COLUMNS"
            f" WHERE {_IN_SCHEMA} AND {_named('TABLE_NAME')}"
            " ORDER BY SCHEMA_NAME, TABLE_NAME, POSITION",
            ("top_plus_one", *_SCHEMA, *_OBJECT),
        ),
    ),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: hana-cli lists VIEWS of type CALC twice (by name, by create time)
# with COMMENTS. Here the list without COMMENTS or DEFINITION, and a count
# per schema (invalid ones, ones with parameters) in place of its metrics.
# The repository's _SYS_BI tables are not read (the guard allows SYS and
# _SYS_STATISTICS only).
# on-prem: verified 2026-11-13 on SPS07 rev 79
CALC_VIEW_ANALYZER = Check(
    name="calcViewAnalyzer",
    category="database",
    purpose=("Calculation views with validity, parameters, cache and create time, and counts per"
             " schema (no definition)."),
    source="bin/calcViewAnalyzer.js",
    statements=(
        Statement(
            "views",
            "SELECT TOP ? SCHEMA_NAME, VIEW_NAME, IS_VALID, HAS_PARAMETERS, HAS_CACHE, CREATE_TIME"
            " FROM SYS.VIEWS"
            f" WHERE VIEW_TYPE = 'CALC' AND {_IN_SCHEMA} AND {_named('VIEW_NAME')}"
            " ORDER BY SCHEMA_NAME, VIEW_NAME",
            ("top_plus_one", *_SCHEMA, *_OBJECT),
        ),
        Statement(
            "per_schema",
            "SELECT TOP ? SCHEMA_NAME, COUNT(VIEW_NAME) AS CALC_VIEWS,"
            " SUM(CASE WHEN IS_VALID = 'TRUE' THEN 0 ELSE 1 END) AS INVALID_VIEWS,"
            " SUM(CASE WHEN HAS_PARAMETERS = 'TRUE' THEN 1 ELSE 0 END) AS WITH_PARAMETERS,"
            " MAX(CREATE_TIME) AS LATEST_CREATE_TIME"
            " FROM SYS.VIEWS"
            f" WHERE VIEW_TYPE = 'CALC' AND {_IN_SCHEMA}"
            " GROUP BY SCHEMA_NAME"
            " ORDER BY COUNT(VIEW_NAME) DESC, SCHEMA_NAME",
            ("top_plus_one", *_SCHEMA),
        ),
    ),
    args=frozenset({"top", "schema", "object"}),
)

# on-prem: hana-cli reads TABLE_GROUP_SCHEMA, TABLE_GROUP_NAME,
# TABLE_GROUP_TYPE, TABLE_GROUP_SUBTYPE and COMMENT of TABLE_GROUPS and a
# view TABLE_GROUP_TABLES; HANA 2.0 has neither those columns nor that view.
# TABLE_GROUPS there is one row per table: SCHEMA_NAME, TABLE_NAME,
# GROUP_TYPE, SUBTYPE, GROUP_NAME, IS_GROUP_LEAD. The groups part counts the
# tables per group and names its lead table; the object filter is on the
# group name.
# on-prem: verified 2026-11-13 on SPS07 rev 79
TABLE_GROUPS = Check(
    name="tableGroups",
    category="database",
    purpose="Table groups (type, subtype, name) with table count and lead table, and their tables.",
    source="bin/tableGroups.js",
    statements=(
        Statement(
            "groups",
            "SELECT TOP ? SCHEMA_NAME, GROUP_TYPE, SUBTYPE, GROUP_NAME,"
            " COUNT(TABLE_NAME) AS TABLE_COUNT,"
            " MAX(CASE WHEN IS_GROUP_LEAD = 'TRUE' THEN TABLE_NAME END) AS LEAD_TABLE"
            " FROM SYS.TABLE_GROUPS"
            f" WHERE {_IN_SCHEMA} AND {_named('GROUP_NAME')}"
            " GROUP BY SCHEMA_NAME, GROUP_TYPE, SUBTYPE, GROUP_NAME"
            " ORDER BY SCHEMA_NAME, GROUP_TYPE, SUBTYPE, GROUP_NAME",
            ("top_plus_one", *_SCHEMA, *_OBJECT),
        ),
        Statement(
            "tables",
            "SELECT TOP ? SCHEMA_NAME, TABLE_NAME, GROUP_TYPE, SUBTYPE, GROUP_NAME, IS_GROUP_LEAD"
            " FROM SYS.TABLE_GROUPS"
            f" WHERE {_IN_SCHEMA} AND {_named('GROUP_NAME')}"
            " ORDER BY SCHEMA_NAME, GROUP_NAME, TABLE_NAME",
            ("top_plus_one", *_SCHEMA, *_OBJECT),
        ),
    ),
    args=frozenset({"top", "schema", "object"}),
)

CHECKS: tuple[Check, ...] = (
    STATUS, VERSION, SCHEMAS, TABLES, VIEWS, PROCEDURES, FUNCTIONS, INDEXES, OBJECTS,
    SEQUENCES, SYNONYMS, TRIGGERS, LIBRARIES, DEPENDENCIES, SPATIAL_DATA, TIME_SERIES_TOOLS,
    CALC_VIEW_ANALYZER, TABLE_GROUPS,
)

register(*CHECKS)
