"""``PG_SCHEMA``: this app's tables in a Postgres schema of their own.

For a Postgres instance shared with another application whose table names
collide with this app's (``chat_conversations``, say). The setting rides on
the resolved :class:`agents.db.DatabaseTarget` and becomes the connection's
whole ``search_path`` (``agents.db._engine_settings``); at startup
:func:`adopt_schema` brings the tables this app already has in ``public``
into the schema, when ``PG_SCHEMA_ADOPT`` says so. Postgres only: on any
other dialect nothing is executed.

Ownership is decided by column shape, and another deployment of this very
app in ``public`` has the same shape. So the move is never automatic: with
``PG_SCHEMA_ADOPT`` unset a start that finds anything to move is refused,
``true`` moves, ``false`` leaves ``public`` as it is (the app's tables are
created in the schema). ``true`` is meant for the one deploy that moves the
tables and is removed (or set ``false``) in the next: left in place it would
move any table of our names that reappears in ``public``.

* :func:`schema_setting` and :func:`adopt_setting` read and check the two
  variables. The validated name is the only text ever interpolated into the
  DDL below.
* :func:`classify_table` decides whether a ``public`` table named like one
  of ours is ours, by its columns (the models are the schema, see
  :func:`required_columns`).
* :func:`plan_schema_adoption` is the pure planner: names in, decisions out.
* :func:`adopt_schema` takes the advisory lock, reads the catalogue, plans,
  refuses, leaves or moves.

Nothing here logs or raises a value of a table: a message names schemas,
tables, columns and constraints, never a row.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from agents.db_errors import DatabaseConfigError

logger = logging.getLogger(__name__)

PG_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED_SCHEMAS = frozenset({"public", "pg_catalog", "information_schema"})
PG_SCHEMA_RULE = (
    "PG_SCHEMA must be a lower-case name of letters, digits and underscores, "
    "at most 63 characters, not a reserved schema"
)


def schema_setting() -> str | None:
    """The schema ``PG_SCHEMA`` names, ``None`` when blank or unset.

    Refused, without repeating the value, when it is not a plain lower-case
    identifier (never trimmed or lower-cased: a repaired name would differ
    from what the operator typed elsewhere) or names a schema of Postgres's
    own.
    """
    value = os.environ.get("PG_SCHEMA", "")
    if not value.strip():
        return None
    if not PG_SCHEMA_RE.fullmatch(value) or value in RESERVED_SCHEMAS or value.startswith("pg_"):
        raise DatabaseConfigError(PG_SCHEMA_RULE)
    return value


PG_SCHEMA_ADOPT_RULE = "PG_SCHEMA_ADOPT must be exactly true or false"


def adopt_setting() -> bool | None:
    """``PG_SCHEMA_ADOPT``: ``True`` / ``False`` for exactly ``true`` /
    ``false``, ``None`` when blank or unset; any other value is refused
    without being repeated, and so is the switch without ``PG_SCHEMA``
    (whoever set it meant a schema)."""
    value = os.environ.get("PG_SCHEMA_ADOPT", "")
    if not value.strip():
        return None
    if value not in ("true", "false"):
        raise DatabaseConfigError(PG_SCHEMA_ADOPT_RULE)
    if schema_setting() is None:
        raise DatabaseConfigError("PG_SCHEMA_ADOPT applies only together with PG_SCHEMA")
    return value == "true"


def settings() -> tuple[str | None, bool | None]:
    """``(PG_SCHEMA, PG_SCHEMA_ADOPT)``, both checked."""
    return schema_setting(), adopt_setting()


# ---------------------------------------------------------------------------
# Which public table is ours
# ---------------------------------------------------------------------------
OURS = "ours"
FOREIGN = "foreign"
REFUSED = "refused"


def required_columns(table: Table) -> list[str]:
    """The columns a table of ours always has: NOT NULL without a server
    default. Every column ``_ensure_column`` added later as NOT NULL carries
    a ``server_default`` on the model (the HANA rule), so an older table of
    ours still holds all of these."""
    return [c.name for c in table.columns if not c.nullable and c.server_default is None]


def classify_table(table: Table, columns: Collection[str]) -> tuple[str, str | None]:
    """Is the ``public`` table named like ``table`` ours?

    ``(FOREIGN, column)`` when a required column is missing (another app's
    table of that name); ``(REFUSED, column)`` when every required column is
    there but the table holds a column our model does not have (moving it
    could adopt what we do not own, leaving it would put an empty copy of
    ours beside it: an operator must look); ``(OURS, None)`` otherwise.
    """
    present = set(columns)
    for name in required_columns(table):
        if name not in present:
            return FOREIGN, name
    known = {c.name for c in table.columns}
    for name in sorted(present - known):
        return REFUSED, name
    return OURS, None


@dataclass
class AdoptionPlan:
    moves: list[str]
    foreign: list[tuple[str, str]]
    refused: list[tuple[str, str]]
    duplicates: list[str]
    # A foreign table of one of our names while ours is already in the schema
    # (every later start on a shared instance): ``(table, missing column)``.
    foreign_twins: list[tuple[str, str]] = field(default_factory=list)
    # A move whose foreign key would keep pointing outside the schema:
    # ``(table, constraint, "schema.table")``. A refusal, like ``refused``.
    dangling: list[tuple[str, str, str]] = field(default_factory=list)
    # A move another table depends on: a table that stays outside the schema
    # has a foreign key into it, ``("schema.table", constraint, moving
    # table)``. ``SET SCHEMA`` would carry that table's key along into our
    # schema: the table is shared. A refusal, like ``dangling``.
    shared: list[tuple[str, str, str]] = field(default_factory=list)
    # Ours, but held back from ``moves`` by ``dangling`` or ``shared``.
    held: list[str] = field(default_factory=list)

    def pending(self) -> list[str]:
        """The ``public`` tables a move is about: ours (moved or held) and
        the refused ones, by name. Foreign tables and twins are not."""
        return sorted({*self.moves, *self.held, *(name for name, _ in self.refused)})


def plan_schema_adoption(
    metadata: MetaData,
    in_schema: Collection[str],
    in_source: Mapping[str, Collection[str]],
    *,
    foreign_keys: Collection[tuple[str, str, str, str, str]] = (),
    source: str = "public",
    target: str | None = None,
) -> AdoptionPlan:
    """What :func:`adopt_schema` does, decided on names alone.

    ``in_schema``: the tables already in our schema; ``in_source``: the
    tables of the source schema (``public``) with their columns;
    ``foreign_keys``: ``(schema, table, constraint, referenced schema,
    referenced table)`` of every key that starts or ends in the source
    schema. Walks our tables parents first, so a move never precedes its
    parent's. A move is only a move when every foreign key of the table
    will still point into the schema afterwards (at a table moved with it,
    or one already there): ``SET SCHEMA`` keeps a constraint by identity, so
    one into a foreign table would tie our table to it for good (a table of
    ours that an earlier start without the setting created in ``public``
    next to another app's parent table): ``dangling``. Nor when a table that
    stays outside the schema has a key into it (``shared``)."""
    plan = AdoptionPlan([], [], [], [])
    have = set(in_schema)
    for table in metadata.sorted_tables:
        name = table.name
        if name in have:
            if name in in_source:
                verdict, column = classify_table(table, in_source[name])
                if verdict == FOREIGN:
                    plan.foreign_twins.append((name, column or ""))
                else:
                    plan.duplicates.append(name)
            continue
        if name not in in_source:
            continue
        verdict, column = classify_table(table, in_source[name])
        if verdict == OURS:
            plan.moves.append(name)
        elif verdict == FOREIGN:
            plan.foreign.append((name, column or ""))
        else:
            plan.refused.append((name, column or ""))
    moving = set(plan.moves)

    def stays_with_us(table_schema: str, table: str) -> bool:
        return (table_schema == source and table in moving) or (
            target is not None and table_schema == target
        )

    for schema, name, constraint, ref_schema, ref_table in foreign_keys:
        if schema == source and name in moving and not stays_with_us(ref_schema, ref_table):
            plan.dangling.append((name, constraint, f"{ref_schema}.{ref_table}"))
        if ref_schema == source and ref_table in moving and not stays_with_us(schema, name):
            plan.shared.append((f"{schema}.{name}", constraint, ref_table))
    held = {name for name, _, _ in plan.dangling} | {name for _, _, name in plan.shared}
    plan.held = [name for name in plan.moves if name in held]
    plan.moves = [name for name in plan.moves if name not in held]
    return plan


# ---------------------------------------------------------------------------
# The startup step
# ---------------------------------------------------------------------------
# Read from pg_catalog, not information_schema: the latter lists only what
# the current role owns or has a privilege on, so a table of ours owned by
# another role would look absent and get an empty copy in the schema. From
# pg_catalog it is seen, and the SET SCHEMA then fails loudly ("must be
# owner"), which is the fail-closed answer.
_CATALOGUE_COLUMNS = text(
    "SELECT n.nspname, c.relname, a.attname "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_attribute a "
    "ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
    "WHERE c.relkind IN ('r', 'p') AND n.nspname IN (:schema, :source)"
)
_CATALOGUE_FOREIGN_KEYS = text(
    "SELECT n.nspname, c.relname, con.conname, fn.nspname, fc.relname "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid "
    "JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace "
    "WHERE con.contype = 'f' AND (n.nspname = :source OR fn.nspname = :source)"
)
# Looked up before it is created: ``CREATE SCHEMA IF NOT EXISTS`` needs the
# CREATE privilege on the database even when the schema is there, so a
# schema a DBA made for a role without that privilege failed every start.
_SCHEMA_EXISTS = text("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = :schema")
# Two instances that start together (a rolling deploy, ``instances > 1``)
# take turns: the transaction-level advisory lock is held until the schema
# step of ``init_db`` commits. One fixed key ("pgschema" in ASCII) for every
# schema: only this step takes it. The wait is bounded, 30 s, inside Cloud
# Foundry's default 60 s start timeout (as agents/hana_hdi.py's container
# lock): a start that waits longer is refused with LOCK_WAIT_REFUSAL rather
# than killed by the platform, and the timeout is back at its default before
# the catalogue is read.
ADVISORY_LOCK_KEY = int.from_bytes(b"pgschema", "big")
ADVISORY_LOCK_WAIT = "30s"
_ADVISORY_WAIT = text(f"SET LOCAL lock_timeout = '{ADVISORY_LOCK_WAIT}'")
_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:key)")
# SQLSTATE lock_not_available: what a lock_timeout that ran out raises.
_LOCK_NOT_AVAILABLE = "55P03"
LOCK_WAIT_REFUSAL = (
    "PG_SCHEMA: another instance is adopting the schema; this start waited 30 s "
    "for it and stopped, nothing was changed. Start again once that instance "
    "is up"
)
# ALTER TABLE ... SET SCHEMA takes ACCESS EXCLUSIVE: a lock somebody else
# holds (an old instance mid-deploy, an operator's open transaction) would
# otherwise block the start until the platform kills it, and again.
# Back to the default after the last move (as after the advisory lock):
# create_all and the _ensure_* chain run in the same transaction and must
# not inherit it.
_MOVE_LOCK_TIMEOUT = text("SET LOCAL lock_timeout = '10s'")
_LOCK_TIMEOUT_RESET = text("SET LOCAL lock_timeout = DEFAULT")


async def adopt_schema(
    conn: AsyncConnection,
    schema: str,
    metadata: MetaData,
    *,
    adopt: bool | None = None,
    source_schema: str = "public",
) -> AdoptionPlan:
    """The first step of ``agents.db._create_and_migrate`` under ``PG_SCHEMA``.

    Takes the advisory lock (at most 30 s of waiting, else
    ``LOCK_WAIT_REFUSAL``), reads the catalogue once and plans
    (:func:`plan_schema_adoption`). Then, by ``adopt`` (``PG_SCHEMA_ADOPT``):

    * ``None``: anything to move (:meth:`AdoptionPlan.pending`) refuses the
      start with the count; nothing is changed. Nothing to move: the start
      goes on (a new install, every start after the first).
    * ``True``: a refused table, a ``dangling`` or a ``shared`` key refuses
      the start before anything is changed; otherwise each table of ours is
      moved with ``ALTER TABLE ... SET SCHEMA`` (rows, indexes, sequences and
      constraints go along) under a lock timeout that is reset afterwards.
    * ``False``: every table of ``source_schema`` is left alone, one WARNING
      per table of one of our names; ``create_all`` makes ours in the schema.

    A foreign table of one of our names is left where it is with one
    WARNING. The schema is created only when the catalogue does not have it,
    after the decisions, so a refusal has created nothing. Only views are
    skipped by the catalogue read. Postgres only; on any other dialect
    nothing is executed. Idempotent: once moved, a table is found in the
    schema and skipped.
    """
    if conn.dialect.name != "postgresql":
        return AdoptionPlan([], [], [], [])
    await _take_the_lock(conn)
    result = await conn.execute(
        _CATALOGUE_COLUMNS, {"schema": schema, "source": source_schema}
    )
    in_schema: set[str] = set()
    in_source: dict[str, set[str]] = {}
    for table_schema, table_name, column_name in result.fetchall():
        if table_schema == schema:
            in_schema.add(table_name)
        else:
            in_source.setdefault(table_name, set()).add(column_name)
    result = await conn.execute(_CATALOGUE_FOREIGN_KEYS, {"source": source_schema})
    foreign_keys = [tuple(row) for row in result.fetchall()]
    plan = plan_schema_adoption(
        metadata, in_schema, in_source,
        foreign_keys=foreign_keys, source=source_schema, target=schema,
    )
    if adopt is None and plan.pending():
        _refuse_unset(plan, schema, source_schema)
    if adopt is True:
        _refuse_unsafe_moves(plan, schema, source_schema)
    if adopt is False:
        _log_left_alone(plan, schema, source_schema)
    else:
        _log_kept(plan, schema, source_schema)
    result = await conn.execute(_SCHEMA_EXISTS, {"schema": schema})
    if result.first() is None:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    if adopt is not True or not plan.moves:
        return plan
    await conn.execute(_MOVE_LOCK_TIMEOUT)
    for name in plan.moves:
        await conn.execute(
            text(f'ALTER TABLE "{source_schema}"."{name}" SET SCHEMA "{schema}"')
        )
        logger.info("PG_SCHEMA: moved %s.%s to %s", source_schema, name, schema)
    await conn.execute(_LOCK_TIMEOUT_RESET)
    return plan


async def _take_the_lock(conn: AsyncConnection) -> None:
    """The advisory lock, waited for at most ``ADVISORY_LOCK_WAIT``; the
    lock timeout is reset right after, so the catalogue reads and the
    decisions run under the default and only the moves set their own."""
    await conn.execute(_ADVISORY_WAIT)
    try:
        await conn.execute(_ADVISORY_LOCK, {"key": ADVISORY_LOCK_KEY})
    except DBAPIError as error:
        if _sqlstate(error) != _LOCK_NOT_AVAILABLE:
            raise
        raise DatabaseConfigError(LOCK_WAIT_REFUSAL) from None
    await conn.execute(_LOCK_TIMEOUT_RESET)


def _sqlstate(error: DBAPIError) -> str | None:
    """The SQLSTATE of a driver error: SQLAlchemy's asyncpg adapter puts it
    on ``orig`` as ``sqlstate`` (and ``pgcode``)."""
    orig = error.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _refuse_unset(plan: AdoptionPlan, schema: str, source: str) -> None:
    pending = plan.pending()
    logger.warning(
        "PG_SCHEMA: %s holds tables of this app's names and columns: %s",
        source, ", ".join(pending),
    )
    count = "1 table" if len(pending) == 1 else f"{len(pending)} tables"
    verb = "has" if len(pending) == 1 else "have"
    raise DatabaseConfigError(
        f"PG_SCHEMA: {count} in {source} {verb} this app's names and columns and "
        f"would be moved into {schema}. Set PG_SCHEMA_ADOPT to true to move them, "
        f"or to false to leave {source} as it is and create this app's tables in "
        f"{schema}; nothing was changed"
    )


def _refuse_unsafe_moves(plan: AdoptionPlan, schema: str, source: str) -> None:
    if plan.refused:
        table, column = plan.refused[0]
        raise DatabaseConfigError(
            f"PG_SCHEMA: {source}.{table} holds the column {column}, which "
            "this app's table does not have; it was not moved and nothing else "
            "was. Drop the column or move the table by hand"
        )
    if plan.dangling:
        table, constraint, target = plan.dangling[0]
        raise DatabaseConfigError(
            f"PG_SCHEMA: {source}.{table} has the foreign key {constraint} "
            f"into {target}, which would stay outside {schema}; it was not moved "
            "and nothing else was. Drop the constraint or the table (if it is "
            "empty) by hand"
        )
    if plan.shared:
        referrer, constraint, table = plan.shared[0]
        raise DatabaseConfigError(
            f"PG_SCHEMA: {referrer} has the foreign key {constraint} into "
            f"{source}.{table}, which would move into {schema}: the table is "
            "shared, so nothing was moved. Drop the constraint, or set "
            f"PG_SCHEMA_ADOPT to false to leave {source} as it is"
        )


def _log_left_alone(plan: AdoptionPlan, schema: str, source: str) -> None:
    """``PG_SCHEMA_ADOPT=false``: one WARNING per table of ours left behind;
    a twin of a table already in the schema is what the operator chose, so
    it is said below WARNING (it would repeat at every start)."""
    left = {*plan.pending(), *(name for name, _ in plan.foreign)}
    for name in sorted(left):
        logger.warning(
            "PG_SCHEMA: %s.%s left alone (PG_SCHEMA_ADOPT is false); this app's "
            "table of that name is in %s",
            source, name, schema,
        )
    for name in [*plan.duplicates, *(name for name, _ in plan.foreign_twins)]:
        logger.info(
            "PG_SCHEMA: %s.%s left alone (PG_SCHEMA_ADOPT is false); ours is in %s",
            source, name, schema,
        )


def _log_kept(plan: AdoptionPlan, schema: str, source: str) -> None:
    for name in plan.duplicates:
        logger.warning(
            "PG_SCHEMA: %s.%s also exists in %s; the one in %s is used, the other left alone",
            source, name, schema, schema,
        )
    for name, column in plan.foreign:
        logger.warning(
            "PG_SCHEMA: %s.%s is not this app's table (no column %s); left alone, "
            "ours is created in %s",
            source, name, column, schema,
        )
    for name, column in plan.foreign_twins:
        logger.info(
            "PG_SCHEMA: %s.%s is another app's table (no column %s); ours is in %s",
            source, name, column, schema,
        )
