"""The admin's "Compute embeddings now": one background run per process.

Design addendum (l), point 2: ``POST /admin/api/data-catalog/embeddings/run``
starts it (``agents/data_catalog/embedding_routes.py``), and it embeds every
embeddable node whose text changed (``embeddings.refresh_vectors`` with no
scope and ``commit_batches``: batches of ``embeddings.BATCH_SIZE``, each
committed when it is embedded and replacing vectors of its own model only),
with the model the route resolved when it started it; a run of all nodes
also drops the vectors of every other model at its end, unless the model in
effect cannot be told then (they are kept for a run that can tell). A save
that changes the model in effect meanwhile ends the run as ``ModelChanged``
before its next write: nothing more is written or dropped.

**One at a time per process**: :func:`start` checks and sets in one step
(no ``await`` between the two, so two requests cannot both start one). A
catalogue or hint save, or the startup rebuild, that embeds meanwhile takes
turns with the run's batches (the lock of ``embeddings.refresh_vectors``):
what it embedded is not sent again by the run. The progress (``{done,
total}``, done counted in nodes dealt with: sent, or found current by then)
and the last run's outcome (``{started_at, finished_at, model, embedded,
error_class}``) are kept in memory: another app instance knows nothing of
them, and a restart forgets them; the vectors themselves are rows and
survive.

A failure is the run's ``error_class`` (the exception class, never its
text: an httpx error names the URL); ``embedded`` counts the vectors the run
wrote (after a failure: the nodes of the batches committed before it), and
the next run sends only what is still stale.
The lifespan cancels a run at shutdown (:func:`cancel_all`); a cancelled
run's ``finished_at`` stays ``null``. Nothing needs a shielded save: the
batch in flight is lost at worst and sent again by the next run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import agents.db as agents_db
from agents.data_catalog import embeddings

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_progress: dict[str, int] | None = None
_last_run: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def running() -> bool:
    return _task is not None and not _task.done()


def progress() -> dict[str, int] | None:
    """``{done, total}`` of the run in flight, else ``None``."""
    return dict(_progress) if running() and _progress is not None else None


def last_run() -> dict[str, Any] | None:
    return dict(_last_run) if _last_run is not None else None


def start(model: str) -> bool:
    """Start a run with ``model``; ``False`` while one is in flight. The task
    is created in the caller's context (the request's), as every background
    run of this app is."""
    global _task, _progress
    if running():
        return False
    _progress = {"done": 0, "total": 0}
    _task = asyncio.create_task(_run(model, _now()), name="data-catalog-embeddings")
    return True


def _committed() -> int:
    # ``done`` is reported after each committed batch.
    return _progress["done"] if _progress is not None else 0


def _report(done: int, total: int) -> None:
    global _progress
    _progress = {"done": done, "total": total}


async def _run(model: str, started_at: str) -> None:
    global _last_run, _progress
    outcome: dict[str, Any] = {
        "started_at": started_at, "finished_at": None, "model": model,
        "embedded": 0, "error_class": None,
    }
    try:
        async with agents_db.SessionLocal() as db:
            embedded = await embeddings.refresh_vectors(
                db, model=model, on_progress=_report, commit_batches=True
            )
            await db.commit()
        outcome["embedded"] = embedded
        logger.info("data catalogue: embedding run with %s wrote %d vector(s)", model, embedded)
    except asyncio.CancelledError:
        outcome["error_class"] = "CancelledError"
        outcome["embedded"] = _committed()
        raise
    except Exception as exc:  # noqa: BLE001 - the class is the run's outcome; no text leaves
        outcome["error_class"] = type(exc).__name__
        outcome["embedded"] = _committed()
        logger.warning("data catalogue: embedding run failed (%s)", type(exc).__name__)
    finally:
        if outcome["error_class"] != "CancelledError":
            outcome["finished_at"] = _now()
        _last_run = outcome
        _progress = None


async def cancel_all() -> None:
    """Cancel the run in flight and wait for it to end (shutdown)."""
    task = _task
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def reset() -> None:
    """Forget the last run (tests). A run in flight is not touched: cancel
    it first (:func:`cancel_all`)."""
    global _task, _progress, _last_run
    if not running():
        _task = None
    _progress = None
    _last_run = None
