"""``builtin:hana`` checks of the category ``database`` (the ``inspect*`` checks).

Each check is data: ``register(Check(...))`` at import, with its SQL as a
constant and every variable part a ``?`` bind (see :mod:`.base`).

Phase 2: the eight ``inspect*`` checks, one object of one schema in detail
(header, columns, parameters, keys, indexes, definition). Category
``database``, like :mod:`.database`; kept in a module of their own so the two
can be written side by side. ``schema`` and ``object`` are required by all
eight and compared with the catalogue as given (``=``, where hana-cli writes
``SCHEMA_NAME LIKE ?``).

What is read and what is not:

- Only the ``SYS`` catalogue (``TABLES``, ``TABLE_COLUMNS``, ``PROCEDURES``,
  ...) and ``M_TABLES`` for a table's size: never a row of the inspected
  object, no data preview.
- A definition (``DEFINITION`` of a view, procedure, function, trigger,
  library and library member) is source code and is returned, read up to
  ``lob_chars=20_000`` characters; a longer one ends in the cut marker and
  its column is named in ``truncated_columns``. A table has no definition
  column: hana-cli calls ``SYS.GET_OBJECT_DEFINITION`` (a procedure, which a
  read may not ``CALL``), so ``inspectTable`` lists its parts instead.
- ``COMMENTS`` columns are left out (free text somebody typed), and so are
  hana-cli's CDS / hdbtable / hdbview conversions and the calculation view
  branch over ``_SYS_BI.BIMC_*`` (outside the schemas the guard allows):
  a calculation view is inspected through ``SYS.VIEWS`` like any column view.
- ``inspectLibMember``: hana-cli takes a member name, which the closed
  arguments cannot carry (``object`` is the library). The check lists the
  members of the library (at most 10, each definition up to 5,000
  characters) with type and access mode instead.
- ``DEFAULT_VALUE`` and ``CHECK_CONDITION`` are never selected: expression
  text that can embed literals (the same rule as :mod:`.schema`).

The target is on-premise SAP HANA 2.0 (SPS07). Column lists follow hana-cli's
HANA 2 branch (``utils/dbInspect.js``) and the SAP HANA system view reference;
none of the statements has run on a landscape yet (``# on-prem: not yet run``).
:data:`CHECKS` holds them in registration order; its test file
(``tests/test_hanadb_checks_inspection.py``) pins their names.
"""

from __future__ import annotations

from agents.hana.checks.base import Check, Statement, register

_ARGS = frozenset({"top", "schema", "object"})
_REQUIRED = frozenset({"schema", "object"})
_ONE = ("schema", "object")
_MANY = ("top_plus_one", "schema", "object")
_DEFINITION_CHARS = 20_000
_MEMBER_ROWS = 10
_MEMBER_CHARS = 5_000

# on-prem: TABLES with hana-cli's HANA 2 columns, plus IS_COLUMN_TABLE; the
# runtime size (RECORD_COUNT, TABLE_SIZE per host and port) from M_TABLES, a
# part of its own so a table on several hosts does not repeat the header.
# Columns from TABLE_COLUMNS by schema and name (hana-cli: by TABLE_OID after a
# first read), COMMENTS left out. Constraints: every constraint, not only the
# primary key (hana-cli filters IS_PRIMARY_KEY); foreign keys from
# REFERENTIAL_CONSTRAINTS and the indexes with their columns are added, so the
# model sees keys and indexes in one call. No DDL: GET_OBJECT_DEFINITION is a
# procedure.
# on-prem: not yet run
INSPECT_TABLE = Check(
    name="inspectTable",
    category="database",
    purpose=("One table: header, size, columns, constraints, foreign keys, indexes."
             " No rows, comments or DDL."),
    source="bin/inspectTable.js",
    statements=(
        Statement(
            "table",
            "SELECT SCHEMA_NAME, TABLE_NAME, TABLE_OID, TABLE_TYPE, IS_COLUMN_TABLE,"
            " HAS_PRIMARY_KEY, UNLOAD_PRIORITY, IS_PRELOAD, CREATE_TIME"
            " FROM SYS.TABLES"
            " WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
            _ONE,
        ),
        Statement(
            "size",
            "SELECT HOST, PORT, RECORD_COUNT, TABLE_SIZE, IS_PARTITIONED"
            " FROM SYS.M_TABLES"
            " WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?"
            " ORDER BY HOST, PORT",
            _ONE,
        ),
        Statement(
            "columns",
            "SELECT TOP ? COLUMN_NAME, POSITION, DATA_TYPE_NAME, OFFSET, LENGTH, SCALE,"
            " IS_NULLABLE, COLUMN_ID"
            " FROM SYS.TABLE_COLUMNS"
            " WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?"
            " ORDER BY POSITION",
            _MANY,
        ),
        Statement(
            "constraints",
            "SELECT TOP ? CONSTRAINT_NAME, COLUMN_NAME, POSITION, IS_PRIMARY_KEY,"
            " IS_UNIQUE_KEY"
            " FROM SYS.CONSTRAINTS"
            " WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?"
            " ORDER BY CONSTRAINT_NAME, POSITION",
            _MANY,
        ),
        Statement(
            "foreign_keys",
            "SELECT TOP ? CONSTRAINT_NAME, COLUMN_NAME, POSITION, REFERENCED_SCHEMA_NAME,"
            " REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, UPDATE_RULE, DELETE_RULE"
            " FROM SYS.REFERENTIAL_CONSTRAINTS"
            " WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?"
            " ORDER BY CONSTRAINT_NAME, POSITION",
            _MANY,
        ),
        Statement(
            "indexes",
            "SELECT TOP ? I.INDEX_NAME, I.INDEX_TYPE, I.\"CONSTRAINT\", C.COLUMN_NAME,"
            " C.POSITION, C.ASCENDING_ORDER"
            " FROM SYS.INDEXES I"
            " JOIN SYS.INDEX_COLUMNS C ON C.SCHEMA_NAME = I.SCHEMA_NAME"
            " AND C.TABLE_NAME = I.TABLE_NAME AND C.INDEX_NAME = I.INDEX_NAME"
            " WHERE I.SCHEMA_NAME = ? AND I.TABLE_NAME = ?"
            " ORDER BY I.INDEX_NAME, C.POSITION",
            _MANY,
        ),
    ),
    args=_ARGS,
    required=_REQUIRED,
)

# on-prem: INDEXES and INDEX_COLUMNS as in hana-cli, named columns instead of
# `*`; `object` is the index name.
# on-prem: not yet run
INSPECT_INDEX = Check(
    name="inspectIndex",
    category="database",
    purpose="One index (object = index name): its table, type and constraint, and its columns.",
    source="bin/inspectIndex.js",
    statements=(
        Statement(
            "index",
            "SELECT SCHEMA_NAME, TABLE_NAME, INDEX_NAME, INDEX_OID, INDEX_TYPE,"
            " \"CONSTRAINT\", CREATE_TIME"
            " FROM SYS.INDEXES"
            " WHERE SCHEMA_NAME = ? AND INDEX_NAME = ?",
            _ONE,
        ),
        Statement(
            "columns",
            "SELECT TOP ? TABLE_NAME, COLUMN_NAME, POSITION, ASCENDING_ORDER"
            " FROM SYS.INDEX_COLUMNS"
            " WHERE SCHEMA_NAME = ? AND INDEX_NAME = ?"
            " ORDER BY POSITION",
            _MANY,
        ),
    ),
    args=_ARGS,
    required=_REQUIRED,
)


def _routine(kind: str, label: str) -> tuple[Statement, ...]:
    """Header, parameters, table-type parameter columns and definition of a
    procedure (``kind`` PROCEDURE) or function (FUNCTION), hana-cli's lists.

    The parameters are found by the routine's OID in a subquery (hana-cli
    reads the header first and passes ``<KIND>_OID``)."""
    view = f"SYS.{kind}S"
    oid = (f" WHERE {kind}_OID IN (SELECT {kind}_OID FROM {view}"
           f" WHERE SCHEMA_NAME = ? AND {kind}_NAME = ?)")
    header = (
        f"SELECT SCHEMA_NAME, {kind}_NAME, {kind}_OID, SQL_SECURITY, DEFAULT_SCHEMA_NAME,"
        " INPUT_PARAMETER_COUNT, OUTPUT_PARAMETER_COUNT, INOUT_PARAMETER_COUNT,"
        " RESULT_SET_COUNT, IS_ENCRYPTED, PROCEDURE_TYPE, READ_ONLY, IS_VALID,"
        " IS_HEADER_ONLY, OWNER_NAME, CREATE_TIME"
        if kind == "PROCEDURE" else
        f"SELECT SCHEMA_NAME, {kind}_NAME, {kind}_OID, SQL_SECURITY, DEFAULT_SCHEMA_NAME,"
        " INPUT_PARAMETER_COUNT, RETURN_VALUE_COUNT, IS_ENCRYPTED, FUNCTION_TYPE,"
        " FUNCTION_USAGE_TYPE, IS_VALID, IS_HEADER_ONLY, OWNER_NAME, CREATE_TIME"
    )
    return (
        Statement(label, f"{header} FROM {view} WHERE SCHEMA_NAME = ? AND {kind}_NAME = ?",
                  _ONE),
        Statement(
            "parameters",
            "SELECT TOP ? PARAMETER_NAME, DATA_TYPE_NAME, LENGTH, SCALE, POSITION,"
            " TABLE_TYPE_NAME, PARAMETER_TYPE, HAS_DEFAULT_VALUE, IS_NULLABLE"
            f" FROM SYS.{kind}_PARAMETERS{oid}"
            " ORDER BY POSITION",
            _MANY,
        ),
        Statement(
            "parameter_columns",
            "SELECT TOP ? PARAMETER_NAME, PARAMETER_POSITION, COLUMN_NAME, POSITION,"
            " DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE"
            f" FROM SYS.{kind}_PARAMETER_COLUMNS{oid}"
            " ORDER BY PARAMETER_POSITION, POSITION",
            _MANY,
        ),
        Statement(
            "definition",
            f"SELECT SCHEMA_NAME, {kind}_NAME, DEFINITION FROM {view}"
            f" WHERE SCHEMA_NAME = ? AND {kind}_NAME = ?",
            _ONE,
        ),
    )


# on-prem: PROCEDURES (hana-cli's HANA 2 columns), PROCEDURE_PARAMETERS and
# PROCEDURE_PARAMETER_COLUMNS by PROCEDURE_OID; the definition from
# PROCEDURES.DEFINITION (hana-cli: GET_OBJECT_DEFINITION, a CALL). An
# encrypted procedure has no readable definition (NULL).
# on-prem: not yet run
INSPECT_PROCEDURE = Check(
    name="inspectProcedure",
    category="database",
    purpose=("One procedure: header, parameters, table parameter columns and definition"
             " (up to 20,000 characters)."),
    source="bin/inspectProcedure.js",
    statements=_routine("PROCEDURE", "procedure"),
    args=_ARGS,
    required=_REQUIRED,
    lob_chars=_DEFINITION_CHARS,
)

# on-prem: FUNCTIONS, FUNCTION_PARAMETERS and FUNCTION_PARAMETER_COLUMNS as in
# hana-cli; the definition from FUNCTIONS.DEFINITION.
# on-prem: not yet run
INSPECT_FUNCTION = Check(
    name="inspectFunction",
    category="database",
    purpose=("One function: header, parameters, table parameter columns and definition"
             " (up to 20,000 characters)."),
    source="bin/inspectFunction.js",
    statements=_routine("FUNCTION", "function"),
    args=_ARGS,
    required=_REQUIRED,
    lob_chars=_DEFINITION_CHARS,
)

_VIEW_OID = (" WHERE VIEW_OID IN (SELECT VIEW_OID FROM SYS.VIEWS"
             " WHERE SCHEMA_NAME = ? AND VIEW_NAME = ?)")

# on-prem: VIEWS, VIEW_COLUMNS and VIEW_PARAMETERS as in hana-cli (COMMENTS
# left out); the definition from VIEWS.DEFINITION. hana-cli's calculation view
# branch reads _SYS_BI.BIMC_* (outside the allowed schemas): dropped, such a
# view is read through SYS.VIEWS like any column view.
# on-prem: not yet run
INSPECT_VIEW = Check(
    name="inspectView",
    category="database",
    purpose=("One view: header, columns, parameters and definition (up to 20,000"
             " characters). No rows, comments or CDS."),
    source="bin/inspectView.js",
    statements=(
        Statement(
            "view",
            "SELECT SCHEMA_NAME, VIEW_NAME, VIEW_OID, IS_COLUMN_VIEW, VIEW_TYPE,"
            " HAS_STRUCTURED_PRIVILEGE_CHECK, HAS_PARAMETERS, HAS_CACHE, CREATE_TIME"
            " FROM SYS.VIEWS"
            " WHERE SCHEMA_NAME = ? AND VIEW_NAME = ?",
            _ONE,
        ),
        Statement(
            "columns",
            "SELECT TOP ? COLUMN_NAME, POSITION, DATA_TYPE_NAME, OFFSET, LENGTH, SCALE,"
            " IS_NULLABLE, COLUMN_ID"
            f" FROM SYS.VIEW_COLUMNS{_VIEW_OID}"
            " ORDER BY POSITION",
            _MANY,
        ),
        Statement(
            "parameters",
            "SELECT TOP ? PARAMETER_NAME, DATA_TYPE_NAME, LENGTH, SCALE, POSITION,"
            " HAS_DEFAULT_VALUE"
            f" FROM SYS.VIEW_PARAMETERS{_VIEW_OID}"
            " ORDER BY POSITION",
            _MANY,
        ),
        Statement(
            "definition",
            "SELECT SCHEMA_NAME, VIEW_NAME, DEFINITION FROM SYS.VIEWS"
            " WHERE SCHEMA_NAME = ? AND VIEW_NAME = ?",
            _ONE,
        ),
    ),
    args=_ARGS,
    required=_REQUIRED,
    lob_chars=_DEFINITION_CHARS,
)

# on-prem: TRIGGERS, the documented columns instead of hana-cli's `*`.
# on-prem: not yet run
INSPECT_TRIGGER = Check(
    name="inspectTrigger",
    category="database",
    purpose=("One trigger: its table, timing, event, level, state and definition (up to"
             " 20,000 characters)."),
    source="bin/inspectTrigger.js",
    statements=(
        Statement(
            "trigger",
            "SELECT SCHEMA_NAME, TRIGGER_NAME, TRIGGER_OID, OWNER_NAME, SUBJECT_TABLE_SCHEMA,"
            " SUBJECT_TABLE_NAME, TRIGGER_ACTION_TIME, TRIGGER_EVENT, TRIGGERED_ACTION_LEVEL,"
            " IS_VALID, IS_ENABLED, CREATE_TIME"
            " FROM SYS.TRIGGERS"
            " WHERE SCHEMA_NAME = ? AND TRIGGER_NAME = ?",
            _ONE,
        ),
        Statement(
            "definition",
            "SELECT SCHEMA_NAME, TRIGGER_NAME, DEFINITION FROM SYS.TRIGGERS"
            " WHERE SCHEMA_NAME = ? AND TRIGGER_NAME = ?",
            _ONE,
        ),
    ),
    args=_ARGS,
    required=_REQUIRED,
    lob_chars=_DEFINITION_CHARS,
)

_LIBRARY_HEADER = Statement(
    "library",
    "SELECT SCHEMA_NAME, LIBRARY_NAME, LIBRARY_OID, OWNER_NAME, DEFAULT_SCHEMA_NAME,"
    " LIBRARY_TYPE, IS_VALID, CREATE_TIME"
    " FROM SYS.LIBRARIES"
    " WHERE SCHEMA_NAME = ? AND LIBRARY_NAME = ?",
    _ONE,
)

# on-prem: LIBRARIES and LIBRARY_MEMBERS as in hana-cli; MEMBER_TYPE added; the
# library's whole definition from LIBRARIES.DEFINITION.
# on-prem: not yet run
INSPECT_LIBRARY = Check(
    name="inspectLibrary",
    category="database",
    purpose=("One SQLScript library: header, members with type and access mode,"
             " definition (up to 20,000 characters)."),
    source="bin/inspectLibrary.js",
    statements=(
        _LIBRARY_HEADER,
        Statement(
            "members",
            "SELECT TOP ? MEMBER_NAME, MEMBER_TYPE, ACCESS_MODE"
            " FROM SYS.LIBRARY_MEMBERS"
            " WHERE SCHEMA_NAME = ? AND LIBRARY_NAME = ?"
            " ORDER BY MEMBER_NAME",
            _MANY,
        ),
        Statement(
            "definition",
            "SELECT SCHEMA_NAME, LIBRARY_NAME, DEFINITION FROM SYS.LIBRARIES"
            " WHERE SCHEMA_NAME = ? AND LIBRARY_NAME = ?",
            _ONE,
        ),
    ),
    args=_ARGS,
    required=_REQUIRED,
    lob_chars=_DEFINITION_CHARS,
)

# on-prem: hana-cli reads ONE member by name; the closed arguments have no
# member name (`object` is the library), so the members are listed with their
# own definitions, by name. PRAGMAS and COMMENTS of hana-cli are left out
# (not in the documented LIBRARY_MEMBERS; COMMENTS is free text). At most 10
# members of at most 5,000 characters each (`_MEMBER_ROWS`, `_MEMBER_CHARS`):
# 50,000 characters stay inside the statement's 60,001 character LOB budget,
# so a library of long members is cut (`truncated`, `truncated_columns`), not
# refused as `result_too_large`.
# on-prem: not yet run
INSPECT_LIB_MEMBER = Check(
    name="inspectLibMember",
    category="database",
    purpose=("Members of one library (no member argument; at most 10) with type, access mode,"
             " definition up to 5,000 characters."),
    source="bin/inspectLibMember.js",
    statements=(
        _LIBRARY_HEADER,
        Statement(
            "members",
            "SELECT TOP ? MEMBER_NAME, MEMBER_TYPE, ACCESS_MODE, DEFINITION"
            " FROM SYS.LIBRARY_MEMBERS"
            " WHERE SCHEMA_NAME = ? AND LIBRARY_NAME = ?"
            " ORDER BY MEMBER_NAME",
            _MANY,
        ),
    ),
    args=_ARGS,
    required=_REQUIRED,
    max_rows_cap=_MEMBER_ROWS,
    lob_chars=_MEMBER_CHARS,
)

CHECKS: tuple[Check, ...] = (
    INSPECT_TABLE, INSPECT_INDEX, INSPECT_PROCEDURE, INSPECT_FUNCTION, INSPECT_VIEW,
    INSPECT_TRIGGER, INSPECT_LIBRARY, INSPECT_LIB_MEMBER)

register(*CHECKS)
