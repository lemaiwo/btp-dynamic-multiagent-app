"""The run store of ``builtin:reconcile``: one ``reconcile_runs`` row per run.

The run itself lives in one process (a background task, ``run.py``), but its
row is in the database, so a later turn served by another app instance, or
after a restart, still finds how it ended. Why each rule:

* **Conditional UPDATEs, rowcount checked.** A row leaves ``running`` exactly
  once: ``finish_run``, the stale check and the startup sweep all write
  ``WHERE status = 'running'``, so a late failure never overwrites a ``done``
  and two instances never both finish a run. No ``RETURNING`` (SAP HANA).
* **Heartbeat.** The task stamps ``updated_at`` with its progress; a
  ``running`` row older than :data:`STALE_SECONDS` belongs to a process that
  is gone (another instance crashed) and is read as ``interrupted`` and
  marked so. ``heartbeat`` answering ``False`` tells the task that its row is
  gone (the conversation was deleted) or was closed, so it stops.
* **Owner-scoped reads.** :func:`latest_run` takes the caller's principal; a
  run of another principal is not found, never refused.
* **No data in the row.** ``counts`` are counters (``str`` -> ``int``) and
  ``error_code`` is a fixed code; a cell value or a message text never reaches
  ``reconcile_runs``.

Each function commits its own short transaction, so the caller passes a
session it does not hold a transaction open on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db import ReconcileRun
from agents.ide.models import iso_utc

# A running row whose heartbeat is older than this is a run nobody drives.
STALE_SECONDS = 300
RUNNING = "running"
OUTCOMES = frozenset({"done", "failed", "interrupted"})
INTERRUPTED = "interrupted"
MAX_ERROR_CODE = 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _counters(counts: dict[str, Any]) -> str:
    """``counts`` as JSON, refused unless it is counters only: a value that is
    no whole number (a text, a float, a bool) could be a cell value."""
    if not isinstance(counts, dict):
        raise ValueError("counts: not an object")
    for key, value in counts.items():
        if not isinstance(key, str) or not key:
            raise ValueError("counts: a key is not a name")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("counts: a value is not a whole number")
    return json.dumps(counts, sort_keys=True)


def _as_dict(row: ReconcileRun) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    if row.counts_json:
        try:
            loaded = json.loads(row.counts_json)
        except ValueError:
            loaded = {}
        counts = loaded if isinstance(loaded, dict) else {}
    return {
        "run_id": row.id,
        "status": row.status,
        "table": row.table_name,
        "wave": row.wave,
        "rows_total": row.rows_total,
        "rows_done": row.rows_done,
        "counts": counts,
        "report_file_id": row.report_file_id,
        "error_code": row.error_code,
        "started_at": iso_utc(row.started_at),
        "finished_at": iso_utc(row.finished_at),
    }


async def create_run(
    db: AsyncSession, *, conversation_id: str, principal: str, message_id: str | None,
    table_name: str, wave: str, spec_file_id: str | None, rows_total: int,
) -> str:
    """Insert a ``running`` row; its id."""
    now = _now()
    row = ReconcileRun(
        conversation_id=conversation_id, principal=principal, message_id=message_id,
        table_name=table_name, wave=wave, spec_file_id=spec_file_id,
        status=RUNNING, rows_total=rows_total, rows_done=0,
        started_at=now, updated_at=now,
    )
    db.add(row)
    await db.commit()
    return row.id


async def heartbeat(db: AsyncSession, run_id: str, rows_done: int) -> bool:
    """Stamp progress on a running row; ``False`` when the row is gone or no
    longer running (the task should stop)."""
    result = await db.execute(
        update(ReconcileRun)
        .where(ReconcileRun.id == run_id, ReconcileRun.status == RUNNING)
        .values(rows_done=rows_done, updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(result.rowcount)


async def finish_run(
    db: AsyncSession, run_id: str, *, status: str, counts: dict[str, int],
    report_file_id: str | None = None, error_code: str | None = None,
) -> bool:
    """``running`` -> ``status``; whether this call moved the row."""
    if status not in OUTCOMES:
        raise ValueError("status: not an outcome")
    if error_code is not None and (not error_code or len(error_code) > MAX_ERROR_CODE):
        raise ValueError("error_code: not a code")
    counts_json = _counters(counts)
    now = _now()
    result = await db.execute(
        update(ReconcileRun)
        .where(ReconcileRun.id == run_id, ReconcileRun.status == RUNNING)
        .values(status=status, counts_json=counts_json, report_file_id=report_file_id,
                error_code=error_code, updated_at=now, finished_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(result.rowcount)


async def _interrupt_stale(db: AsyncSession, conversation_id: str) -> None:
    """Mark the conversation's running rows without a recent heartbeat
    ``interrupted`` (the process that drove them is gone)."""
    now = _now()
    await db.execute(
        update(ReconcileRun)
        .where(ReconcileRun.conversation_id == conversation_id,
               ReconcileRun.status == RUNNING,
               ReconcileRun.updated_at < now - timedelta(seconds=STALE_SECONDS))
        .values(status=INTERRUPTED, error_code=INTERRUPTED, finished_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def active_run(db: AsyncSession, conversation_id: str) -> dict[str, Any] | None:
    """The conversation's running run with a live heartbeat, else ``None``."""
    await _interrupt_stale(db, conversation_id)
    row = (await db.execute(
        select(ReconcileRun)
        .where(ReconcileRun.conversation_id == conversation_id,
               ReconcileRun.status == RUNNING)
        .order_by(ReconcileRun.started_at.desc(), ReconcileRun.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return _as_dict(row) if row is not None else None


async def latest_run(
    db: AsyncSession, conversation_id: str, principal: str, run_id: str | None = None,
) -> dict[str, Any] | None:
    """The caller's run ``run_id`` of the conversation, or its newest one;
    ``None`` for another principal's run as for no run at all."""
    await _interrupt_stale(db, conversation_id)
    stmt = select(ReconcileRun).where(
        ReconcileRun.conversation_id == conversation_id,
        ReconcileRun.principal == principal,
    )
    if run_id is not None:
        stmt = stmt.where(ReconcileRun.id == run_id)
    row = (await db.execute(
        stmt.order_by(ReconcileRun.started_at.desc(), ReconcileRun.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return _as_dict(row) if row is not None else None


async def sweep_running_at_startup(db: AsyncSession) -> int:
    """Startup only: a process that has just started drives no run, so every
    ``running`` row becomes ``interrupted``; the number swept. Assumes ONE
    app instance, like the other startup sweeps (``store.reset_running``)."""
    result = await db.execute(
        update(ReconcileRun)
        .where(ReconcileRun.status == RUNNING)
        .values(status=INTERRUPTED, error_code=INTERRUPTED, finished_at=_now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return max(result.rowcount or 0, 0)
