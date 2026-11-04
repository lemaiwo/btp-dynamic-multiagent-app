"""``PG_SCHEMA``: this app's tables in a Postgres schema of their own.

For a Postgres instance shared with another application whose table names
collide with this app's (``chat_conversations``, say). The setting rides on
the resolved :class:`agents.db.DatabaseTarget` and becomes the connection's
whole ``search_path`` (``agents.db._engine_settings``); at startup
:func:`adopt_schema` brings the tables this app already has in ``public``
into the schema. Postgres only: on any other dialect nothing is executed.

* :func:`schema_setting` reads and checks the variable. The validated name
  is the only text ever interpolated into the DDL below.
* :func:`classify_table` decides whether a ``public`` table named like one
  of ours is ours, by its columns (the models are the schema, see
  :func:`required_columns`).
* :func:`plan_schema_adoption` is the pure planner: names in, decisions out.
* :func:`adopt_schema` reads the catalogue, plans, refuses or moves.

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


def plan_schema_adoption(
    metadata: MetaData,
    in_schema: Collection[str],
    in_source: Mapping[str, Collection[str]],
    *,
    foreign_keys: Collection[tuple[str, str, str, str]] = (),
    source: str = "public",
    target: str | None = None,
) -> AdoptionPlan:
    """What :func:`adopt_schema` does, decided on names alone.

    ``in_schema``: the tables already in our schema; ``in_source``: the
    tables of the source schema (``public``) with their columns;
    ``foreign_keys``: ``(table, constraint, referenced schema, referenced
    table)`` of the source schema's tables. Walks our tables parents first,
    so a move never precedes its parent's. A move is only a move when every
    foreign key of the table will still point into the schema afterwards
    (at a table moved with it, or one already there): ``SET SCHEMA`` keeps a
    constraint by identity, so one into a foreign table would tie our table
    to it for good (a table of ours that an earlier start without the
    setting created in ``public`` next to another app's parent table)."""
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
    for name, constraint, ref_schema, ref_table in foreign_keys:
        if name not in moving:
            continue
        if (ref_schema == source and ref_table in moving) or (
            target is not None and ref_schema == target
        ):
            continue
        plan.dangling.append((name, constraint, f"{ref_schema}.{ref_table}"))
    if plan.dangling:
        held = {name for name, _, _ in plan.dangling}
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
    "SELECT c.relname, con.conname, fn.nspname, fc.relname "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid "
    "JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace "
    "WHERE con.contype = 'f' AND n.nspname = :source"
)
# ALTER TABLE ... SET SCHEMA takes ACCESS EXCLUSIVE: a lock somebody else
# holds (an old instance mid-deploy, an operator's open transaction) would
# otherwise block the start until the platform kills it, and again.
_MOVE_LOCK_TIMEOUT = text("SET LOCAL lock_timeout = '10s'")


async def adopt_schema(
    conn: AsyncConnection,
    schema: str,
    metadata: MetaData,
    *,
    source_schema: str = "public",
) -> AdoptionPlan:
    """The first step of ``agents.db._create_and_migrate`` under ``PG_SCHEMA``.

    Creates the schema if needed, then moves each table of ``metadata`` that
    still sits in ``source_schema`` into it (``ALTER TABLE ... SET SCHEMA``:
    rows, indexes, sequences and constraints go along), leaves a foreign
    table of one of our names where it is with one WARNING, and refuses the
    start, before any move, on a table that is neither (``classify_table``).
    Only views are skipped by the catalogue read. Postgres only; on any
    other dialect nothing is executed. Idempotent: once moved, a table is
    found in the schema and skipped.
    """
    if conn.dialect.name != "postgresql":
        return AdoptionPlan([], [], [], [])
    await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
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
    if plan.refused:
        table, column = plan.refused[0]
        raise DatabaseConfigError(
            f"PG_SCHEMA: {source_schema}.{table} holds the column {column}, which "
            "this app's table does not have; it was not moved and nothing else "
            "was. Drop the column or move the table by hand"
        )
    if plan.dangling:
        table, constraint, target = plan.dangling[0]
        raise DatabaseConfigError(
            f"PG_SCHEMA: {source_schema}.{table} has the foreign key {constraint} "
            f"into {target}, which would stay outside {schema}; it was not moved "
            "and nothing else was. Drop the constraint or the table (if it is "
            "empty) by hand"
        )
    for name in plan.duplicates:
        logger.warning(
            "PG_SCHEMA: %s.%s also exists in %s; the one in %s is used, the other left alone",
            source_schema, name, schema, schema,
        )
    for name, column in plan.foreign:
        logger.warning(
            "PG_SCHEMA: %s.%s is not this app's table (no column %s); left alone, "
            "ours is created in %s",
            source_schema, name, column, schema,
        )
    for name, column in plan.foreign_twins:
        logger.info(
            "PG_SCHEMA: %s.%s is another app's table (no column %s); ours is in %s",
            source_schema, name, column, schema,
        )
    if plan.moves:
        await conn.execute(_MOVE_LOCK_TIMEOUT)
    for name in plan.moves:
        await conn.execute(
            text(f'ALTER TABLE "{source_schema}"."{name}" SET SCHEMA "{schema}"')
        )
        logger.info("PG_SCHEMA: moved %s.%s to %s", source_schema, name, schema)
    return plan
