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
and closes it there; the task holds one for the whole run
(``batches.ReopeningReader``, ``check_fields=False``: the fields were
checked), reopened once when the session breaks at the transport. Each batch
read is bounded by :data:`READ_TIMEOUT_SECONDS` (``asyncio.timeout``;
``expired()`` tells our deadline from the callee's ``TimeoutError``):
``arc1_timeout``.

**Batches** (``batches.batches``, also the survey's rule): CSV rows in order
until :attr:`batch_size` distinct keys are pending, one ``SAPQuery`` for
them; keys the answer did not hold are read once more on their own before
they count as missing (an answer cut by ARC-1's byte limit must not read as
"Missing in ECC"). ``rows_done`` counts the data rows dealt with.

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
the save shielded and bounded by :data:`FINISH_SECONDS`). The report is
stored before ``finish_run``; on every exit but ``done`` it is removed again.
Refused at start as well: ``too_many_runs`` (``RECONCILE_MAX_RUNS``, default
2, per process) and ``shutting_down`` (after :func:`cancel_all`).

**What another instance knows.** The row: status, ``rows_total``,
``rows_done`` as of the last heartbeat, the outcome. :func:`progress` (rows
done between heartbeats) and the task itself are in this process only, and a
restart forgets them (the startup sweep marks the row ``interrupted``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete

import agents.db as agents_db
from agents.auth import current_principal
from agents.ide.store import env_int
from agents.reconcile import runs_store
from agents.reconcile.arc1_reader import MAX_BATCH_KEYS, Arc1Reader, Arc1ReadError
from agents.reconcile.batches import ReopeningReader, batches, survey
from agents.reconcile.counts import (  # noqa: F401  (re-exported: they lived here)
    COLUMN_METRICS,
    COLUMN_PREFIX,
    column_counts,
    run_counts,
    split_counts,
)
from agents.reconcile.engine import (
    Comparer,
    CsvError,
    iter_csv,
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
# A run's last row write (outcome) may take this long at most; at shutdown it
# runs shielded from the cancel, so it must be bounded itself.
FINISH_SECONDS = 10.0
# Runs at the same time in one process (each holds a CSV, its code tables and
# an ARC-1 session); read at every start.
MAX_RUNS_ENV, DEFAULT_MAX_RUNS = "RECONCILE_MAX_RUNS", 2

_MESSAGES = {
    "already_running": "A reconciliation run of this conversation is still running.",
    "not_signed_in": "A reconciliation run needs the signed-in tester.",
    "csv_invalid": "The CSV file cannot be used for this spec.",
    "csv_empty": "The CSV file has no data rows.",
    "xref_required": "The spec translates values through code tables, "
    "but no code-table workbook was given.",
    "too_many_runs": "Too many reconciliation runs are in progress on this app "
    "instance; try again when one has finished.",
    "shutting_down": "The app is stopping; start the run again in a moment.",
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
    # The stored report, until the row says done: removed on any other exit.
    report_file_id: str | None = None
    done: bool = False


_tasks: dict[str, asyncio.Task] = {}
_jobs: dict[str, _Job] = {}
_starting: set[str] = set()
# Set by cancel_all: a start still in its checks must not start a task after it.
_closing = False


def max_runs() -> int:
    """``RECONCILE_MAX_RUNS`` (default 2, at least 1), read at every start."""
    return env_int(MAX_RUNS_ENV, DEFAULT_MAX_RUNS, minimum=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _running(conversation_id: str) -> bool:
    task = _tasks.get(conversation_id)
    return task is not None and not task.done()


def task_of(conversation_id: str) -> asyncio.Task | None:
    """The conversation's task in this process, while it runs (tests, status)."""
    task = _tasks.get(conversation_id)
    return task if task is not None and not task.done() else None


def in_flight() -> int:
    """Runs in flight in this process, starts still in preflight included.

    The registry's drain counts them: a reload must not close the retired
    build's MCP clients while a run still reads through one of them."""
    return sum(1 for t in _tasks.values() if not t.done()) + len(_starting)


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


async def start(
    *, conversation_id: str, principal: str, message_id: str, spec: ReconcileSpec,
    spec_file_id: str | None, csv_data: bytes, xref_data: bytes | None,
    server: Any, target: str, batch_size: int, mismatch_cap: int,
    check_fields: bool = True, spec_name: str | None = None,
) -> dict[str, Any]:
    """Check, preflight, create the row and start the run; ``{run_id, rows_total, batches}``.

    Raises :class:`RunError`, ``Arc1ReadError`` (the preflight),
    ``xref_reader.Refused`` (a code table) or ``ValueError`` (a ``batch_size``
    outside 1..9,999, a ``mismatch_cap`` below 1, a bad target name: the
    entry's configuration, not the tester's input).
    """
    _whole(batch_size, "batch_size", 1, MAX_BATCH_KEYS)
    _whole(mismatch_cap, "mismatch_cap", 1)
    if not principal or current_principal.get() != principal:
        raise RunError("not_signed_in")
    if _closing:
        raise RunError("shutting_down")
    if _running(conversation_id) or conversation_id in _starting:
        raise RunError("already_running")
    if in_flight() >= max_runs():
        raise RunError("too_many_runs")
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
        rows_total, batch_count = await asyncio.to_thread(survey, csv_data, spec, batch_size)
    except CsvError as err:
        raise RunError("csv_invalid", csv_code=err.code) from None
    if rows_total == 0:
        raise RunError("csv_empty")
    reader = Arc1Reader(server, table=spec.table, key_field=spec.key.ecc_field,
                        fields=comparer.needed_fields(), target=target,
                        check_fields=check_fields)
    async with reader:
        await reader.preflight()
    if _closing:
        raise RunError("shutting_down")
    async with agents_db.SessionLocal() as db:
        run_id = await runs_store.create_run(
            db, conversation_id=conversation_id, principal=principal,
            message_id=message_id, table_name=spec.table, wave=spec.wave,
            spec_file_id=spec_file_id, rows_total=rows_total,
        )
    if run_id is None:
        # Another tool call or instance won the row lock, or the conversation is gone.
        raise RunError("already_running")
    if _closing:
        # cancel_all ran while the row was written: close it as a cancelled run would.
        async with agents_db.SessionLocal() as db:
            await runs_store.finish_run(db, run_id, status="interrupted",
                                        error_code=runs_store.INTERRUPTED)
        raise RunError("shutting_down")
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
                run_id, spec.table, rows_total, batch_count)
    return {"run_id": run_id, "rows_total": rows_total, "batches": batch_count}


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
    def new_reader() -> Arc1Reader:
        return Arc1Reader(job.server, table=spec.table, key_field=spec.key.ecc_field,
                          fields=comparer.needed_fields(), target=job.target,
                          check_fields=False)

    async with ReopeningReader(new_reader, _read, job.run_id) as session:
        for done, pending in batches(rows, comparer.key_of, job.batch_size):
            if pending is None:  # a stretch without a query: let the loop run
                job.rows_done = done
                await asyncio.sleep(0)
                continue
            if pending:
                keys = list(dict.fromkeys(key for key, _, _ in pending))
                found = await session.read(keys)
                # An answer cut below its keys (ARC-1's byte limit) looks like
                # keys that are not there: read those once more on their own.
                absent = [k for k in keys if k not in found]
                if absent:
                    found.update(await session.read(absent))
                for key, row_no, row in pending:
                    comparer.compare(key, row_no, row, found.get(key))
            job.rows_done = done
            await _beat_or_stop(job)
    result = comparer.result(malformed_rows=rows.malformed)
    job.extra = run_counts(result, job.lookups)
    meta = ReportMeta(run_id=job.run_id, spec_name=job.spec_name,
                      started_at=job.started_at, finished_at=_now(),
                      lookups_truncated=job.extra.get("lookups_truncated", 0))
    data = await asyncio.to_thread(build_report, spec, result, meta)
    await _beat_or_stop(job)
    async with agents_db.SessionLocal() as db:
        stored = await chat_store.add_generated_file(
            db, conversation_id=job.conversation_id, message_id=job.message_id,
            principal=job.principal, name=report_file_name(spec, meta),
            media_type=REPORT_MEDIA_TYPE, data=data,
        )
    job.report_file_id = stored["file_id"]
    async with agents_db.SessionLocal() as db:
        finished = await runs_store.finish_run(
            db, job.run_id, status="done", counts=job.extra,
            report_file_id=job.report_file_id)
    if not finished:
        raise _Lost
    job.done = True
    logger.info("[reconcile] run %s of %s done", job.run_id, spec.table)


async def _drop_report(job: _Job) -> None:
    """Remove the stored report of a run that did not end ``done`` (bounded)."""
    file_id, job.report_file_id = job.report_file_id, None
    try:
        async with asyncio.timeout(FINISH_SECONDS):
            async with agents_db.SessionLocal() as db:
                await db.execute(delete(agents_db.ChatFile)
                                 .where(agents_db.ChatFile.id == file_id)
                                 .execution_options(synchronize_session=False))
                await db.commit()
    except Exception as exc:  # noqa: BLE001 - logged by class; the row says not done
        logger.warning("[reconcile] could not remove the report of run %s (%s)",
                       job.run_id, type(exc).__name__)


def _partial_counts(job: _Job) -> dict[str, int]:
    try:
        return job.extra or run_counts(job.comparer.result(), job.lookups)
    except Exception:  # noqa: BLE001 - counters are a courtesy on a failed row
        return {}


async def _finish(job: _Job, status: str, error_code: str) -> None:
    try:
        # Bounded: at shutdown this runs shielded from the cancel.
        async with asyncio.timeout(FINISH_SECONDS):
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
        if job.report_file_id is not None and not job.done:
            await asyncio.shield(_drop_report(job))
        if _tasks.get(job.conversation_id) is asyncio.current_task():
            _tasks.pop(job.conversation_id, None)
            _jobs.pop(job.conversation_id, None)


async def cancel_all() -> None:
    """Cancel every run in flight and wait for each to close its row (shutdown).

    A start still in its checks sees ``_closing`` and starts no task."""
    global _closing
    _closing = True
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
    global _closing
    _closing = False
