"""The background reconciliation run of ``builtin:reconcile``: one per conversation.

A tool call (Task 11) hands :func:`start` the spec, the CSV, the code-table
workbook and the agent's ARC-1 MCP server. Everything that can be refused
cheaply is refused **inside that call**, so the model can tell the tester at
once: the caller is not the signed-in principal (``not_signed_in``), a run of
the conversation is live here or on another instance (``already_running``),
the CSV cannot be read, has no data rows or lacks a column the spec compares
(``csv_invalid`` / ``csv_empty``), a value map has no code table
(``xref_required``), a code table cannot be built (``Refused`` of
``xref_reader``, unchanged), and the ARC-1 preflight fails (``Arc1ReadError``:
sign-in, target, missing ``SAPQuery``, a field not in the table). Only then
is the ``reconcile_runs`` row created and the task started; the call answers
``{run_id, rows_total, batches}``.

**The caller's identity.** The task is created inside the tool call, so it
runs in a copy of the caller's context: ``current_principal`` (and the JWT)
are the tester's for every ARC-1 request the task sends, through the server's
per-user auth; there is no technical-user fallback. :func:`start` refuses a
``principal`` that is not the bound ``current_principal``, so the row, the
report file and the ARC-1 calls name the same person.

**Two ARC-1 sessions.** The preflight opens its own reader in the tool call
and closes it there; the task opens one reader for the whole run (MCP's
session is entered and left in the task that owns it: anyio cancel scopes do
not cross tasks) with ``check_fields=False`` (the fields were checked). The
reader retries a transient failure itself; each batch read is bounded by
:data:`READ_TIMEOUT_SECONDS` (``asyncio.timeout``; ``expired()`` tells our
deadline from a ``TimeoutError`` of the callee), failing the run as
``arc1_timeout``.

**Batches.** CSV rows are taken in order until :attr:`batch_size` distinct
keys are pending; one ``SAPQuery`` reads them, then every pending row is
compared. ``rows_done`` counts the data rows dealt with (invalid keys too).

**Heartbeat.** After every batch, and from a ticker every
:data:`HEARTBEAT_SECONDS` while a read waits on ARC-1 or the report is built,
well inside ``runs_store.STALE_SECONDS``. A heartbeat answering ``False``
means the row is gone (the conversation was deleted) or was closed (a startup
sweep of another instance): the task stops at the next batch boundary and
stores nothing; it never finishes a row it lost.

**Outcomes** (``finish_run``, a conditional UPDATE): ``done`` with the
engine's counters and the report file; ``failed`` with the reader's code
(``sign_in_needed``, ``arc1_failed``, ...), ``arc1_timeout``,
``report_too_large``, or ``internal`` for anything unexpected (the exception
class is logged, never its text: it may quote a cell value or a URL);
``interrupted`` when the app cancels the run at shutdown (:func:`cancel_all`,
the save shielded). The report is stored before ``finish_run``; when that
finds the row no longer running, the stored file is removed again.

**What another instance knows.** The row: status, ``rows_total``,
``rows_done`` as of the last heartbeat, the outcome. :func:`progress` (rows
done between heartbeats) and the task itself are in this process only, and a
restart forgets them (the startup sweep marks the row ``interrupted``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete

import agents.db as agents_db
from agents.auth import current_principal
from agents.reconcile import runs_store
from agents.reconcile.arc1_reader import MAX_ROWS, Arc1Reader, Arc1ReadError
from agents.reconcile.engine import (
    ColumnStats,
    Comparer,
    CsvError,
    EngineResult,
    iter_csv,
    normalise_key,
)
from agents.reconcile.report import (
    REPORT_MEDIA_TYPE,
    ReportError,
    ReportMeta,
    build_report,
    report_file_name,
)
from agents.reconcile.spec import ReconcileSpec, ValueMapRule
from agents.reconcile.xref_reader import Lookup, build_lookups
from agents.webchat import store as chat_store

logger = logging.getLogger(__name__)

# Both well inside runs_store.STALE_SECONDS (300): a run that waits on ARC-1
# for a whole read still stamps its row three times in that window.
HEARTBEAT_SECONDS = 60.0
READ_TIMEOUT_SECONDS = 180.0
# Rows compared between two yields to the event loop (a batch of invalid keys
# sends no query, so nothing else would yield).
_YIELD_EVERY = 1_000

_MESSAGES = {
    "already_running": "A reconciliation run of this conversation is still running.",
    "not_signed_in": "A reconciliation run needs the signed-in tester.",
    "csv_invalid": "The CSV file cannot be used for this spec.",
    "csv_empty": "The CSV file has no data rows.",
    "xref_required": "The spec translates values through code tables, "
    "but no code-table workbook was given.",
}


class RunError(Exception):
    """A run that was not started; ``code`` is stable, the message fixed.

    For ``csv_invalid``, ``csv_code`` is the engine's ``CsvError`` code
    (``column_missing`` when spec columns are absent, then ``columns`` names
    them: they are the spec's column names, which the CSV header lacks).
    """

    def __init__(self, code: str, *, csv_code: str | None = None,
                 columns: list[str] | None = None) -> None:
        self.code = code
        self.message = _MESSAGES.get(code, "The reconciliation run could not start.")
        self.csv_code = csv_code
        self.columns = list(columns or [])
        super().__init__(self.message)


class _Lost(Exception):
    """The row is gone or no longer running: stop, store nothing."""


class _ReadTimeout(Exception):
    """A batch read outlasted READ_TIMEOUT_SECONDS."""


@dataclass
class _Job:
    run_id: str
    conversation_id: str
    principal: str
    message_id: str
    spec: ReconcileSpec
    spec_name: str
    csv_data: bytes
    lookups: dict[tuple[str, int], Lookup]
    comparer: Comparer
    server: Any
    target: str
    batch_size: int
    rows_total: int
    started_at: datetime
    rows_done: int = 0
    lost: bool = False
    extra: dict[str, int] = field(default_factory=dict)


_tasks: dict[str, asyncio.Task] = {}
_jobs: dict[str, _Job] = {}
_starting: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _running(conversation_id: str) -> bool:
    task = _tasks.get(conversation_id)
    return task is not None and not task.done()


def task_of(conversation_id: str) -> asyncio.Task | None:
    """The conversation's task in this process, while it runs (tests, status)."""
    task = _tasks.get(conversation_id)
    return task if task is not None and not task.done() else None


def progress(conversation_id: str) -> dict[str, int] | None:
    """``{rows_done, rows_total}`` of the conversation's run in this process, else ``None``.

    Finer than the row's ``rows_done``, which moves only at a heartbeat."""
    job = _jobs.get(conversation_id)
    if job is None or not _running(conversation_id):
        return None
    return {"rows_done": job.rows_done, "rows_total": job.rows_total}


def _whole(value: Any, name: str, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (
        high is not None and value > high
    ):
        raise ValueError(f"{name}: not a whole number in range")
    return value


def _survey(data: bytes, spec: ReconcileSpec, batch_size: int) -> tuple[int, int]:
    """``(data rows, batches)`` of the CSV, by the batching rule of :func:`_drive`.

    A full pass, so a CSV error late in the file is refused before the run;
    CPU-bound (worker thread)."""
    _, rows = iter_csv(data)
    total = batches = 0
    pending: set[str] = set()
    for _row_no, row in rows:
        total += 1
        key = normalise_key(row.get(spec.key.csv_column), spec.key)
        if key is None:
            continue
        pending.add(key)
        if len(pending) >= batch_size:
            batches += 1
            pending.clear()
    return total, batches + (1 if pending else 0)


async def start(
    *, conversation_id: str, principal: str, message_id: str, spec: ReconcileSpec,
    spec_file_id: str | None, csv_data: bytes, xref_data: bytes | None,
    server: Any, target: str, batch_size: int, mismatch_cap: int,
    check_fields: bool = True, spec_name: str | None = None,
) -> dict[str, Any]:
    """Check, preflight, create the row and start the run; ``{run_id, rows_total, batches}``.

    Raises :class:`RunError`, ``Arc1ReadError`` (the preflight),
    ``xref_reader.Refused`` (a code table) or ``ValueError`` (a ``batch_size``
    outside 1..10,000, a ``mismatch_cap`` below 1, a bad target name: the
    entry's configuration, not the tester's input).
    """
    _whole(batch_size, "batch_size", 1, MAX_ROWS)
    _whole(mismatch_cap, "mismatch_cap", 1)
    if not principal or current_principal.get() != principal:
        raise RunError("not_signed_in")
    if _running(conversation_id) or conversation_id in _starting:
        raise RunError("already_running")
    _starting.add(conversation_id)
    try:
        return await _start(
            conversation_id=conversation_id, principal=principal, message_id=message_id,
            spec=spec, spec_file_id=spec_file_id, csv_data=csv_data, xref_data=xref_data,
            server=server, target=target, batch_size=batch_size, mismatch_cap=mismatch_cap,
            check_fields=check_fields, spec_name=spec_name or f"{spec.table} {spec.wave}",
        )
    finally:
        _starting.discard(conversation_id)


async def _start(
    *, conversation_id: str, principal: str, message_id: str, spec: ReconcileSpec,
    spec_file_id: str | None, csv_data: bytes, xref_data: bytes | None, server: Any,
    target: str, batch_size: int, mismatch_cap: int, check_fields: bool, spec_name: str,
) -> dict[str, Any]:
    async with agents_db.SessionLocal() as db:
        if await runs_store.active_run(db, conversation_id) is not None:
            raise RunError("already_running")
    try:
        header, _ = iter_csv(csv_data)
    except CsvError as err:
        raise RunError("csv_invalid", csv_code=err.code) from None
    needs_xref = any(isinstance(r, ValueMapRule) for r in spec.columns.values())
    if needs_xref and xref_data is None:
        raise RunError("xref_required")
    lookups = await asyncio.to_thread(build_lookups, xref_data, spec) if needs_xref else {}
    comparer = Comparer(spec, lookups, mismatch_cap=mismatch_cap)
    missing = comparer.missing_columns(header)
    if missing:
        raise RunError("csv_invalid", csv_code="column_missing", columns=missing)
    try:
        rows_total, batches = await asyncio.to_thread(_survey, csv_data, spec, batch_size)
    except CsvError as err:
        raise RunError("csv_invalid", csv_code=err.code) from None
    if rows_total == 0:
        raise RunError("csv_empty")
    reader = Arc1Reader(server, table=spec.table, key_field=spec.key.ecc_field,
                        fields=comparer.needed_fields(), target=target,
                        check_fields=check_fields)
    async with reader:
        await reader.preflight()
    async with agents_db.SessionLocal() as db:
        run_id = await runs_store.create_run(
            db, conversation_id=conversation_id, principal=principal,
            message_id=message_id, table_name=spec.table, wave=spec.wave,
            spec_file_id=spec_file_id, rows_total=rows_total,
        )
    if run_id is None:
        # Another tool call or instance won the row lock, or the conversation is gone.
        raise RunError("already_running")
    job = _Job(
        run_id=run_id, conversation_id=conversation_id, principal=principal,
        message_id=message_id, spec=spec, spec_name=spec_name, csv_data=csv_data,
        lookups=lookups, comparer=comparer, server=server, target=target,
        batch_size=batch_size, rows_total=rows_total, started_at=_now(),
    )
    _jobs[conversation_id] = job
    # Created here, in the caller's context: the task keeps its principal and JWT.
    _tasks[conversation_id] = asyncio.create_task(_run(job), name=f"reconcile-{run_id}")
    logger.info("[reconcile] run %s of %s started: %d rows, %d batches",
                run_id, spec.table, rows_total, batches)
    return {"run_id": run_id, "rows_total": rows_total, "batches": batches}


def _counts(result: EngineResult, lookups: dict[tuple[str, int], Lookup]) -> dict[str, int]:
    """Counters only (``runs_store`` refuses anything else)."""
    tables = {id(t): t for t in lookups.values()}.values()
    return {
        "rows_total": result.rows_total,
        "rows_compared": result.rows_compared,
        "malformed_rows": result.malformed_rows,
        "invalid_keys": result.invalid_keys,
        "duplicate_keys": result.duplicate_keys,
        "missing_in_ecc": result.missing_count,
        "missing_in_ecc_dropped": result.missing_in_ecc_dropped,
        "mismatches": result.mismatch_count,
        "mismatches_dropped": result.mismatches_dropped,
        "not_checked": len(result.not_checked),
        "columns": len(result.stats),
        "lookups": len(tables),
        "lookups_truncated": sum(1 for t in tables if t.truncated),
        "lookup_duplicates": sum(t.duplicates for t in tables),
        **column_counts(result.stats),
    }


COLUMN_PREFIX = "col."
COLUMN_METRICS = ("checked", "matched", "mismatched", "not_checked")
_KIND_MARK = ".kind."
_RULE_KINDS = frozenset(
    {"direct", "constant", "value_map", "expression", "not_needed", "unresolved"})


def column_counts(stats: Mapping[str, ColumnStats]) -> dict[str, int]:
    """Per-column counters flattened for ``runs_store`` (counters only):
    ``col.<column>.<metric>`` per metric, plus ``col.<column>.kind.<kind>: 1``
    so the status can say a column's rule kind without reading the spec file.
    One entry per spec column, so at most ``5 * MAX_COLUMNS`` keys.
    """
    out: dict[str, int] = {}
    for column, stat in stats.items():
        for metric in COLUMN_METRICS:
            out[f"{COLUMN_PREFIX}{column}.{metric}"] = int(getattr(stat, metric))
        out[f"{COLUMN_PREFIX}{column}{_KIND_MARK}{stat.kind}"] = 1
    return out


def split_counts(counts: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A stored ``counts`` back into ``(totals, columns)``, the inverse of
    :func:`column_counts`; columns sorted by mismatches, most first, then name.

    A column name may hold dots, so the metric (or ``kind.<kind>``) is read
    from the end; a ``col.`` key of any other form, or a value that is no
    whole number, is dropped rather than guessed.
    """
    totals: dict[str, Any] = {}
    columns: dict[str, dict[str, Any]] = {}
    for key, value in counts.items():
        if not key.startswith(COLUMN_PREFIX):
            totals[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        head, _, last = key[len(COLUMN_PREFIX):].rpartition(".")
        if head and last in COLUMN_METRICS:
            columns.setdefault(head, {})[last] = value
        elif head.endswith(_KIND_MARK[:-1]) and last in _RULE_KINDS:
            name = head[: -len(_KIND_MARK) + 1]
            if name:
                columns.setdefault(name, {})["kind"] = last
    listed = [
        {"column": name, "kind": seen.get("kind"),
         **{metric: seen.get(metric, 0) for metric in COLUMN_METRICS}}
        for name, seen in columns.items()
    ]
    listed.sort(key=lambda c: (-c["mismatched"], c["column"]))
    return totals, listed


async def _heartbeat(job: _Job) -> bool:
    async with agents_db.SessionLocal() as db:
        return await runs_store.heartbeat(db, job.run_id, job.rows_done)


async def _tick(job: _Job) -> None:
    """Keep the row fresh while a read or the report takes long."""
    while not job.lost:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            alive = await _heartbeat(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the next tick tries again
            logger.warning("[reconcile] heartbeat of run %s failed (%s)",
                           job.run_id, type(exc).__name__)
            continue
        if not alive:
            job.lost = True


async def _beat_or_stop(job: _Job) -> None:
    if job.lost or not await _heartbeat(job):
        job.lost = True
        raise _Lost


async def _read(reader: Arc1Reader, keys: list[str]) -> dict[str, dict[str, str]]:
    try:
        async with asyncio.timeout(READ_TIMEOUT_SECONDS) as deadline:
            return await reader.read(keys)
    except TimeoutError:
        if deadline.expired():
            raise _ReadTimeout from None
        raise


async def _drive(job: _Job) -> None:
    """Read, compare, report, store; raises on any outcome but ``done``."""
    spec, comparer = job.spec, job.comparer
    _, rows = iter_csv(job.csv_data)
    reader = Arc1Reader(job.server, table=spec.table, key_field=spec.key.ecc_field,
                        fields=comparer.needed_fields(), target=job.target,
                        check_fields=False)
    async with reader:
        pending: list[tuple[str, int, dict[str, str]]] = []
        distinct: dict[str, None] = {}
        done = 0

        async def flush() -> None:
            if pending:
                found = await _read(reader, list(distinct))
                for key, row_no, row in pending:
                    comparer.compare(key, row_no, row, found.get(key))
                pending.clear()
                distinct.clear()
            job.rows_done = done
            await _beat_or_stop(job)

        for row_no, row in rows:
            done += 1
            key = comparer.key_of(row, row_no)
            if key is not None:
                pending.append((key, row_no, row))
                distinct[key] = None
                if len(distinct) >= job.batch_size:
                    await flush()
            if done % _YIELD_EVERY == 0:
                await asyncio.sleep(0)
        await flush()
    result = comparer.result(malformed_rows=rows.malformed)
    job.extra = _counts(result, job.lookups)
    meta = ReportMeta(run_id=job.run_id, spec_name=job.spec_name,
                      started_at=job.started_at, finished_at=_now())
    data = await asyncio.to_thread(build_report, spec, result, meta)
    await _beat_or_stop(job)
    async with agents_db.SessionLocal() as db:
        stored = await chat_store.add_generated_file(
            db, conversation_id=job.conversation_id, message_id=job.message_id,
            principal=job.principal, name=report_file_name(spec, meta),
            media_type=REPORT_MEDIA_TYPE, data=data,
        )
    file_id = stored["file_id"]
    async with agents_db.SessionLocal() as db:
        finished = await runs_store.finish_run(
            db, job.run_id, status="done", counts=job.extra, report_file_id=file_id)
        if not finished:
            await db.execute(delete(agents_db.ChatFile).where(agents_db.ChatFile.id == file_id)
                             .execution_options(synchronize_session=False))
            await db.commit()
            raise _Lost
    logger.info("[reconcile] run %s of %s done", job.run_id, spec.table)


def _partial_counts(job: _Job) -> dict[str, int]:
    try:
        return job.extra or _counts(job.comparer.result(), job.lookups)
    except Exception:  # noqa: BLE001 - counters are a courtesy on a failed row
        return {}


async def _finish(job: _Job, status: str, error_code: str) -> None:
    try:
        async with agents_db.SessionLocal() as db:
            moved = await runs_store.finish_run(
                db, job.run_id, status=status, counts=_partial_counts(job),
                error_code=error_code)
    except Exception as exc:  # noqa: BLE001 - the stale check closes the row later
        logger.warning("[reconcile] could not close run %s as %s (%s)",
                       job.run_id, status, type(exc).__name__)
        return
    logger.info("[reconcile] run %s of %s %s: %s%s", job.run_id, job.spec.table, status,
                error_code, "" if moved else " (row already closed)")


async def _run(job: _Job) -> None:
    ticker = asyncio.create_task(_tick(job), name=f"reconcile-heartbeat-{job.run_id}")
    try:
        await _drive(job)
    except asyncio.CancelledError:
        if not job.lost:
            await asyncio.shield(_finish(job, "interrupted", runs_store.INTERRUPTED))
        raise
    except _Lost:
        logger.info("[reconcile] run %s stopped: its row is gone or closed", job.run_id)
    except _ReadTimeout:
        await _finish(job, "failed", "arc1_timeout")
    except (Arc1ReadError, ReportError) as err:
        await _finish(job, "failed", err.code)
    except Exception as exc:  # noqa: BLE001 - the class is logged, never the text
        logger.warning("[reconcile] run %s failed (%s)", job.run_id, type(exc).__name__)
        await _finish(job, "failed", "internal")
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        if _tasks.get(job.conversation_id) is asyncio.current_task():
            _tasks.pop(job.conversation_id, None)
            _jobs.pop(job.conversation_id, None)


async def cancel_all() -> None:
    """Cancel every run in flight and wait for each to close its row (shutdown)."""
    tasks = [t for t in _tasks.values() if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def reset() -> None:
    """Forget finished runs (tests). A run in flight is not touched: cancel it first."""
    for cid in [c for c in _tasks if not _running(c)]:
        _tasks.pop(cid, None)
        _jobs.pop(cid, None)
    for cid in [c for c in _jobs if c not in _tasks]:
        _jobs.pop(cid, None)
    _starting.clear()
