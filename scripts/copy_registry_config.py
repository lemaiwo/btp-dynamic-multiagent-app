"""Copy the registry's configuration from one database to another.

For a landscape that switches database (PostgreSQL to SAP HANA, or back): it
must keep its agents INCLUDING the client secrets stored with them, which
``/admin/api/export`` redacts on purpose. This script is the only supported
way to carry those secrets over: it reads the rows from one database and
writes them to the other inside one process, so a secret never passes
through a file, a bundle, an argument or a log.

It copies configuration, not history:

* copied -- ``agent_configs`` (every column), ``skill_configs``, the
  orchestrator row, ``workflows`` with their branches and steps,
  ``odata_services``, ``ide_conventions``, ``mcp_oauth_clients``,
  ``mcp_oauth_tokens``;
* not copied -- job and workflow runs, the OData and IDE audit logs, IDE
  sessions and everything below them, pending OAuth flow states.

Rows are matched by their natural key (an agent by name, a token by user and
server, ...), never by integer id: ids are assigned by the target, and the
branches and steps of a workflow are attached to the target's id of that
workflow. A target row with the same key is replaced by the source row; a
target row the source does not have is left alone. A second run changes
nothing. Everything is written in one transaction on the target: all of it
or none.

The target's tables must exist already (start the app once on it: on HANA
that is the HDI deploy). The script deploys nothing and sends the source no
statement that writes; on Postgres the source transaction is READ ONLY.

Where the two databases come from, without a URL on the command line:

* as a Cloud Foundry task of an app that has BOTH services bound::

      python scripts/copy_registry_config.py --from postgres --to hana

* anywhere else, from two environment variables that hold the URLs::

      python scripts/copy_registry_config.py --from-env OLD_URL --to-env NEW_URL

Both go through the resolver of ``agents/db.py`` (same drivers, same TLS
rules). ``DB_KIND`` does not choose either side. (``agents.db`` refuses to
be imported with two bound services and no ``DB_KIND``, so for ITS OWN
process this script sets ``DB_KIND=postgres`` when the variable is unset;
that only decides the engine ``agents.db`` builds for itself, through which
this script reads and writes nothing.) Without ``--apply`` nothing is written:
the default is a dry run that prints, per table, how many rows would be
inserted, replaced or are equal already, and the NAMES concerned (for the
OAuth tables counts only). No other column value is ever printed or logged,
and a failure is reported by its class, never with the driver's text
(which repeats the statement's parameters).

Before and after ``--apply``:

* The app that uses the source should be stopped, or at least no longer in
  use. What is changed there after the copy is not on the target, and a
  later run would bring it over on top of what was changed on the target.
* The app on the target must be restarted (or reloaded in the admin UI)
  afterwards: it built its agents from the rows it found at start.
* User tokens are copied as they are at that moment. A token that either
  app refreshes afterwards makes the other side's copy stale (a provider
  that rotates refresh tokens then asks that user to sign in again), and a
  second run overwrites the target's tokens with the source's.
* Rows that exist only in the target (an agent the app seeded and the
  source had deleted, say) are left alone and listed, so they can be
  removed by hand.
* A target row that holds a unique value a source row of another name
  needs (an ``api_slug``) is a refusal, named up front; nothing is written.
* Setting ``non_production`` on a target, or the destination of a flagged
  one, is audited by the app (``conventions_flag``,
  ``conventions_destination`` in ``ide_audit_log``). The copy writes the
  same rows for what it sets, in its transaction, as actor :data:`ACTOR`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class CopyRefused(RuntimeError):
    """The copy cannot run as asked; the message is safe to print."""


def _importable_with_two_bindings() -> None:
    """``agents.db`` chooses the app's database while it is imported and
    refuses to choose between two bound services without ``DB_KIND``. This
    script names both itself, so for its own process any bound kind will do
    as the app's; nothing is read or written through that engine."""
    if os.environ.get("DB_KIND", "").strip():
        return
    try:
        services = json.loads(os.environ.get("VCAP_SERVICES") or "{}")
    except ValueError:
        return
    if isinstance(services, dict) and services.get("hana") and any(
        services.get(label)
        for label in ("postgresql-db", "postgresql", "hyperscaler-option-postgresql")
    ):
        os.environ["DB_KIND"] = "postgres"


_importable_with_two_bindings()

from sqlalchemy import DateTime, delete, insert, select, update  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine  # noqa: E402

import agents.ide.models  # noqa: E402,F401  (registers ide_conventions)
from agents import db as agents_db  # noqa: E402
from agents.db import Base, DatabaseConfigError, DatabaseTarget  # noqa: E402
from agents.ide import store  # noqa: E402
from agents.ide.models import IdeAuditLog  # noqa: E402

TABLES = Base.metadata.tables


@dataclass(frozen=True)
class Spec:
    """One copied table: its natural key and whether its key may be shown."""

    table: str
    key: tuple[str, ...]
    # The key is a name an operator recognises (never for the OAuth tables,
    # whose keys are user ids and server URLs).
    named: bool = True


SPECS = (
    Spec("skill_configs", ("name",)),
    Spec("agent_configs", ("name",)),
    Spec("orchestrator_config", ("id",), named=False),
    Spec("workflows", ("name",)),
    Spec("odata_services", ("name",)),
    Spec("ide_conventions", ("target",)),
    Spec("mcp_oauth_clients", ("server_key",), named=False),
    Spec("mcp_oauth_tokens", ("user_id", "server_key"), named=False),
)
# Rows of a workflow, matched by the workflow's NAME: the id differs.
CHILDREN = (
    ("workflow_branches", ("key",)),
    ("workflow_steps", ("branch_key", "position")),
)
COPIED = tuple(spec.table for spec in SPECS) + tuple(name for name, _ in CHILDREN)
NOT_COPIED = tuple(sorted(set(TABLES) - set(COPIED)))


@dataclass
class TablePlan:
    table: str
    named: bool
    insert: list[str] = field(default_factory=list)
    replace: list[str] = field(default_factory=list)
    unchanged: int = 0
    # Rows the target has and the source does not: left alone, but shown.
    only_in_target: list[str] | None = None
    # ide_conventions: the targets an audit row is written for.
    audited: list[str] | None = None

    def line(self) -> str:
        def part(word: str, names: list[str]) -> str:
            shown = f" ({', '.join(sorted(names))})" if names and self.named else ""
            return f"{word} {len(names)}{shown}"

        text = (f"{self.table}: {part('insert', self.insert)}, "
                f"{part('replace', self.replace)}, unchanged {self.unchanged}")
        if self.only_in_target is not None:
            text += ", " + part("only in target", self.only_in_target)
        if self.audited:
            text += ", " + part("audited", self.audited)
        return text


class Plans(list):
    """The plans of one run. ``source_close_failed`` names the class of an
    error met while letting go of the source AFTER the target was committed:
    the copy is done then, and saying otherwise would be wrong."""

    source_close_failed: str | None = None


# Who the audit rows of a copied ``non_production`` flag name.
ACTOR = "script:copy_registry_config"
# Columns that are unique besides the natural key. A source row may need a
# value a target row of another name holds.
UNIQUE_BESIDES_KEY = {"agent_configs": ("api_slug",), "workflows": ("api_slug",)}


def _instant(value: Any) -> Any:
    """A timestamp as an aware UTC instant. Postgres hands back aware
    datetimes, SAP HANA and SQLite naive ones that ARE UTC; written as aware
    UTC, each of the three stores the same instant (the two naive ones drop
    the zone without converting, so only UTC is safe to hand them)."""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row(table: Any, raw: Any, *, skip: tuple[str, ...]) -> dict[str, Any]:
    values = dict(raw._mapping)
    return {
        column.name: _instant(values[column.name]) if isinstance(column.type, DateTime)
        else values[column.name]
        for column in table.columns
        if column.name not in skip
    }


def _generated_id(table: Any) -> tuple[str, ...]:
    """The integer id the target assigns itself; never carried over."""
    return tuple(
        c.name for c in table.primary_key.columns
        if c.type.python_type is int and table.name != "orchestrator_config"
    )


async def _rows(conn: AsyncConnection, table: Any, skip: tuple[str, ...]) -> list[dict[str, Any]]:
    result = await conn.execute(select(table))
    return [_row(table, raw, skip=skip) for raw in result.all()]


def _where(table: Any, key: tuple[str, ...], row: dict[str, Any]) -> list[Any]:
    return [table.c[name] == row[name] for name in key]


def _label(key: tuple[Any, ...]) -> str:
    return " / ".join(str(part) for part in key)


async def _copy_table(
    source: AsyncConnection, target: AsyncConnection, spec: Spec, *, apply: bool
) -> TablePlan:
    table = TABLES[spec.table]
    skip = _generated_id(table)
    plan = TablePlan(spec.table, spec.named, only_in_target=[])
    existing = {
        tuple(row[name] for name in spec.key): row for row in await _rows(target, table, skip)
    }
    wanted = {
        tuple(row[name] for name in spec.key): row for row in await _rows(source, table, skip)
    }
    plan.only_in_target = [_label(key) for key in existing if key not in wanted]
    unique = UNIQUE_BESIDES_KEY.get(spec.table, ())
    for column in unique:
        # Up front, by name: at commit it would be a bare IntegrityError.
        held = {row[column]: key for key, row in existing.items()
                if key not in wanted and row[column] is not None}
        clashes = sorted(
            (_label(key), _label(held[row[column]]))
            for key, row in wanted.items() if row[column] in held
        )
        if clashes:
            raise CopyRefused(
                f"{spec.table}: the {column} of "
                + "; ".join(f"source '{ours}' is held by target-only '{theirs}'"
                            for ours, theirs in clashes)
                + ". Change or remove it on one side; nothing was written."
            )
    replaced = [key for key, row in wanted.items() if key in existing and existing[key] != row]
    if apply and unique and replaced:
        # Two replaced rows may swap such a value; emptied first, the final
        # values never meet the old ones.
        for key in replaced:
            await target.execute(
                update(table).where(*_where(table, spec.key, wanted[key]))
                .values({column: None for column in unique})
            )
    audits: list[Any] = []
    for key, row in wanted.items():
        if key not in existing:
            plan.insert.append(_label(key))
            if apply:
                await target.execute(insert(table).values(**row))
        elif existing[key] == row:
            plan.unchanged += 1
            continue
        else:
            plan.replace.append(_label(key))
            if apply:
                await target.execute(
                    update(table).where(*_where(table, spec.key, row)).values(**row)
                )
        if spec.table == "ide_conventions":
            staged = _convention_audits(row, existing.get(key))
            if staged:
                audits.extend(staged)
                plan.audited = [*(plan.audited or []), _label(key)]
    if apply and audits:
        log = IdeAuditLog.__table__
        for entry in audits:
            await target.execute(insert(log).values({
                column.name: getattr(entry, column.name) for column in log.columns
                if getattr(entry, column.name) is not None
            }))
    return plan


class _Staged(list):
    """Stands in for the session the app's audit helpers add their row to."""

    add = list.append


def _convention_audits(new: dict[str, Any], old: dict[str, Any] | None) -> list[Any]:
    """The audit rows the app writes when it sets what this row sets: the
    ``non_production`` flag when it changes, and the destination of a target
    that is flagged before or after (``agents.ide.store``, same helpers,
    same shape)."""
    staged = _Staged()
    old_flag = old is not None and old["non_production"] is True
    new_flag = new["non_production"] is True
    old_destination = (old or {}).get("destination") or None
    new_destination = new.get("destination") or None
    flag_changed = new_flag is not old_flag
    if flag_changed:
        store._flag_audit(staged, ACTOR, new["target"], old_flag if old else None, new_flag)
    if (old_flag or new_flag) and (
        new_destination != old_destination
        or (flag_changed and (old_destination or new_destination))
    ):
        store._destination_audit(
            staged, ACTOR, new["target"], old_destination, new_destination)
    return list(staged)


async def _workflow_ids(conn: AsyncConnection) -> dict[int, str]:
    workflows = TABLES["workflows"]
    result = await conn.execute(select(workflows.c.id, workflows.c.name))
    return {wid: name for wid, name in result.all()}


async def _copy_children(
    source: AsyncConnection, target: AsyncConnection, name: str, order: tuple[str, ...],
    *, apply: bool, planned: set[str],
) -> TablePlan:
    """The branches or steps of every source workflow. Per workflow they are
    equal to the source's already, or all replaced by them: a list has no
    natural key of its own to match row by row."""
    table = TABLES[name]
    skip = (*_generated_id(table), "workflow_id")
    plan = TablePlan(name, named=True)
    source_names = await _workflow_ids(source)
    target_ids = {wname: wid for wid, wname in (await _workflow_ids(target)).items()}

    async def grouped(conn: AsyncConnection, names: dict[int, str]) -> dict[str, list[dict]]:
        result = await conn.execute(select(table))
        groups: dict[str, list[dict]] = {}
        for raw in result.all():
            owner = names.get(raw._mapping["workflow_id"])
            if owner is not None:
                groups.setdefault(owner, []).append(_row(table, raw, skip=skip))
        for rows in groups.values():
            rows.sort(key=lambda r: tuple(str(r[c]) for c in order))
        return groups

    wanted = await grouped(source, source_names)
    held = await grouped(target, {wid: wname for wname, wid in target_ids.items()})
    for workflow in sorted(set(source_names.values())):
        rows = wanted.get(workflow, [])
        if workflow not in target_ids:
            # Its workflow is inserted in this run (or would be, in a dry run).
            if workflow in planned and rows:
                plan.insert.append(workflow)
            continue
        if held.get(workflow, []) == rows:
            plan.unchanged += 1
            continue
        (plan.replace if held.get(workflow) else plan.insert).append(workflow)
        if apply:
            wid = target_ids[workflow]
            await target.execute(delete(table).where(table.c.workflow_id == wid))
            for row in rows:
                await target.execute(insert(table).values(workflow_id=wid, **row))
    return plan


async def _require_tables(target: AsyncConnection) -> None:
    for name in COPIED:
        try:
            async with target.begin_nested():
                await target.execute(select(TABLES[name]).limit(1))
        except Exception:  # noqa: BLE001 -- the driver's text is not for output
            raise CopyRefused(
                f"The target has no usable table {name}. Start the app once on the "
                "target database (it creates the schema), then run the copy."
            ) from None


async def copy_config(
    source: AsyncEngine, target: AsyncEngine, *, apply: bool = False
) -> Plans:
    """Copy (or, without ``apply``, only plan) the configuration tables.

    One transaction on the target, committed only when every table was
    written; a dry run and any failure roll it back. The source is only
    read. A failure while letting go of the source after the target's
    commit does not undo the copy and is reported as what it is
    (``Plans.source_close_failed``)."""
    plans = Plans()
    reading = await source.connect()
    try:
        if source.dialect.name == "postgresql":
            reading = await reading.execution_options(postgresql_readonly=True)
        async with target.connect() as writing:
            await _require_tables(writing)
            for spec in SPECS:
                plans.append(await _copy_table(reading, writing, spec, apply=apply))
            new_workflows = set(next(p for p in plans if p.table == "workflows").insert)
            for name, order in CHILDREN:
                plans.append(await _copy_children(
                    reading, writing, name, order, apply=apply, planned=new_workflows))
            if apply:
                await writing.commit()
            else:
                await writing.rollback()
        committed = apply
    except BaseException:
        await _let_go(reading)
        raise
    failed = await _let_go(reading)
    if failed and committed:
        plans.source_close_failed = failed
    return plans


async def _let_go(connection: AsyncConnection) -> str | None:
    """End the read and close; the class of what went wrong, if anything."""
    try:
        await connection.rollback()
        await connection.close()
    except Exception as exc:  # noqa: BLE001 -- reported by the caller, by class
        return type(exc).__name__
    return None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _target(kind: str | None, env: str | None, side: str) -> DatabaseTarget:
    if (kind is None) == (env is None):
        raise CopyRefused(f"Name the {side} database with --{side} OR --{side}-env")
    if kind is not None:
        return agents_db.bound_target(kind)
    url = os.environ.get(env or "", "").strip()
    if not url:
        raise CopyRefused(f"The environment variable named by --{side}-env is not set")
    return agents_db._target_from_url(url)


def _identity(target: DatabaseTarget) -> tuple[Any, ...]:
    """What makes two targets the same database (never shown)."""
    url = make_url(target.url)
    schema = url.query.get("currentSchema")
    return (target.kind, (url.host or "").lower(), url.port, url.database, schema)


def _engine(target: DatabaseTarget) -> AsyncEngine:
    url, connect_args = agents_db._engine_settings(target)
    # hide_parameters: a statement that fails must not quote its values.
    return create_async_engine(url, connect_args=connect_args, hide_parameters=True)


async def run(source: DatabaseTarget, target: DatabaseTarget, *, apply: bool) -> Plans:
    if _identity(source) == _identity(target):
        raise CopyRefused("Source and target are the same database")
    reading, writing = _engine(source), _engine(target)
    try:
        return await copy_config(reading, writing, apply=apply)
    finally:
        for engine in (reading, writing):
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001 -- the outcome is decided already
                pass


# Loggers that print statements with their parameters when a process runs at
# DEBUG (aiosqlite does on every call). The rows this script moves hold
# secrets, so for its own process they are capped whatever the root level.
_PARAMETER_LOGGERS = ("sqlalchemy.engine", "sqlalchemy.pool", "aiosqlite", "asyncpg", "hdbcli")


def _kind(value: str) -> str:
    """A bound service kind. Not ``choices=``: argparse would repeat a value
    it refuses, and what gets pasted there by mistake is a URL."""
    if value not in agents_db.DB_KINDS:
        raise argparse.ArgumentTypeError(
            "must be 'postgres' or 'hana' (a URL goes into an environment "
            "variable named with --from-env / --to-env)"
        )
    return value


AFTER_APPLY = (
    "next steps:",
    "  - the app on the source should be stopped or no longer used: what changes "
    "there now is not on the target",
    "  - restart the app on the target (or Reload in its admin UI): it built its "
    "agents from the rows it found at start",
    "  - user tokens were copied as they are now; one that either side refreshes "
    "from here on makes the other side's copy stale, and a second run overwrites "
    "the target's tokens with the source's",
)


def main(argv: list[str] | None = None) -> int:
    for name in _PARAMETER_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for side, what in (("from", "source"), ("to", "target")):
        parser.add_argument(f"--{side}", type=_kind, default=None, metavar="{postgres,hana}",
                            help=f"the bound service that is the {what}")
        parser.add_argument(f"--{side}-env", metavar="NAME", default=None,
                            help=f"environment variable holding the {what} URL")
    parser.add_argument("--apply", action="store_true",
                        help="write to the target (default: dry run)")
    args = parser.parse_args(argv)
    try:
        source = _target(getattr(args, "from"), args.from_env, "from")
        target = _target(args.to, args.to_env, "to")
        plans = asyncio.run(run(source, target, apply=args.apply))
    except (CopyRefused, DatabaseConfigError) as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    except Exception as failed:  # noqa: BLE001 -- its text may quote a row
        print(
            f"copy failed ({type(failed).__name__}); nothing was written to the target",
            file=sys.stderr,
        )
        return 1
    print(f"{source.kind} -> {target.kind}: " + ("APPLIED" if args.apply else "dry run"))
    for plan in plans:
        print("  " + plan.line())
    print("  not copied: " + ", ".join(NOT_COPIED))
    if not args.apply:
        print("nothing was written; run again with --apply")
        return 0
    if plans.source_close_failed:
        print(
            "the configuration WAS written to the target; closing the source "
            f"connection failed afterwards ({plans.source_close_failed})",
            file=sys.stderr,
        )
    for line in AFTER_APPLY:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
