"""Batching and the ARC-1 session of a reconciliation run (split out of ``run.py``).

:func:`batches` is the one batching rule: the survey inside the tool call
counts batches with it and the background run reads with it, so the number
the tester is told is the number the run sends. :class:`ReopeningReader`
holds the run's one ARC-1 session and reopens it once when the session
breaks at the transport.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any

from agents.reconcile.arc1_reader import Arc1Reader, Arc1ReadError
from agents.reconcile.engine import iter_csv, normalise_key
from agents.reconcile.spec import ReconcileSpec

logger = logging.getLogger(__name__)

# Rows dealt with between two yields to the event loop (a stretch of invalid
# keys sends no query, so nothing else would yield).
YIELD_EVERY = 1_000

Pending = list[tuple[str, int, dict[str, str]]]


def batches(
    rows: Iterable[tuple[int, dict[str, str]]],
    key_of: Callable[[dict[str, str], int], str | None],
    batch_size: int,
) -> Iterator[tuple[int, Pending | None]]:
    """The one batching rule of :func:`survey` and the run (``run._drive``).

    CSV rows are taken in order until ``batch_size`` distinct keys are
    pending; yields ``(rows dealt with, pending rows)`` per batch, a last
    batch at the end (possibly empty), and ``(rows dealt with, None)`` every
    :data:`YIELD_EVERY` rows in between, so a caller can yield to the loop
    while a run of invalid keys sends no query."""
    pending: Pending = []
    distinct: set[str] = set()
    done = 0
    for row_no, row in rows:
        done += 1
        key = key_of(row, row_no)
        if key is not None:
            pending.append((key, row_no, row))
            distinct.add(key)
            if len(distinct) >= batch_size:
                yield done, pending
                pending, distinct = [], set()
                continue
        if done % YIELD_EVERY == 0:
            yield done, None
    yield done, pending


def survey(data: bytes, spec: ReconcileSpec, batch_size: int) -> tuple[int, int]:
    """``(data rows, batches)`` of the CSV, by the batching rule of :func:`batches`.

    A full pass, so a CSV error late in the file is refused before the run;
    CPU-bound (worker thread). Keys by :func:`normalise_key` (the comparer's
    ``key_of`` counts what it sees, and the survey must not)."""
    _, rows = iter_csv(data)
    total = count = 0
    for done, pending in batches(
        rows, lambda row, _no: normalise_key(row.get(spec.key.csv_column), spec.key),
        batch_size,
    ):
        total = done
        if pending:
            count += 1
    return total, count


class ReopeningReader:
    """A run's ARC-1 reader, reopened ONCE when its MCP session breaks at the
    transport (``Arc1ReadError.transport``: no answer at all, after the
    reader's own retries). A refusal, a sign-in or an odd answer is never a
    reason to reopen. Entered and left in the run's own task (MCP's session
    is). ``new`` builds a reader, ``read_batch(reader, keys)`` reads with the
    run's deadline."""

    def __init__(
        self,
        new: Callable[[], Arc1Reader],
        read_batch: Callable[[Arc1Reader, list[str]], Awaitable[dict[str, dict[str, str]]]],
        label: str,
    ) -> None:
        self._new = new
        self._read_batch = read_batch
        self._label = label
        self._reader: Arc1Reader | None = None
        self.reopened = False

    async def __aenter__(self) -> ReopeningReader:
        reader = self._new()
        await reader.__aenter__()
        self._reader = reader
        return self

    async def __aexit__(self, *exc: Any) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            await reader.__aexit__(*exc)

    async def read(self, keys: list[str]) -> dict[str, dict[str, str]]:
        assert self._reader is not None
        try:
            return await self._read_batch(self._reader, keys)
        except Arc1ReadError as err:
            if self.reopened or not err.transport:
                raise
            self.reopened = True
            logger.info("[reconcile] run %s: reopening the ARC-1 session once", self._label)
            broken, self._reader = self._reader, None
            await broken.__aexit__(type(err), err, None)  # never raises over err
            reader = self._new()
            await reader.__aenter__()
            self._reader = reader
            return await self._read_batch(reader, keys)
