"""What the worker thread of ``builtin:hana`` reads, within its budgets.

Split out of :mod:`agents.hana.connection` (which calls it from its
``_execute``, in the worker thread, before ``rollback`` and ``close``): the
LOB reader with its per-value cap and total budget, and the fetch of the rows
with the optional row-character budget. Nothing here connects, logs or
raises a driver's text: an overflow is a bare exception the connection layer
maps to ``result_too_large``.
"""

from __future__ import annotations

from typing import Any

LOB_CUT_MARKER = " …[cut]"
LOB_UNREADABLE = "<unreadable LOB>"
ROW_FETCH_CHUNK = 500
"""Rows per ``fetchmany`` when the caller names a ``row_chars_budget``: the
budget is checked after each chunk, so at most one chunk is fetched past it."""

__all__ = ["LOB_CUT_MARKER", "LOB_UNREADABLE", "ROW_FETCH_CHUNK", "LobBudget", "LobReader",
           "RowBudget", "fetch_rows", "row_chars"]


class LobBudget(Exception):
    """The LOB values of one call passed the call's budget (no text)."""


class LobReader:
    """Reads the LOB values of one call within a total budget.

    hdbcli hands ``NCLOB`` / ``CLOB`` / ``BLOB`` back as objects whose
    ``read`` works only while the connection is open (``STATEMENT_STRING`` of
    ``M_EXPENSIVE_STATEMENTS`` / ``M_ACTIVE_STATEMENTS`` is ``NCLOB``), so
    this runs in the worker thread before ``rollback`` and ``close``. Each
    value is read up to ``per_value`` characters (bytes for a ``BLOB``; the
    answer shows a binary value's size only) and a longer text is cut with
    :data:`LOB_CUT_MARKER`. All values of the call together may take
    ``budget`` characters: a value that would pass it, or any further value
    once it is used up, raises :class:`LobBudget` without another read (no
    more round trips; the caller answers ``result_too_large``). Why a total:
    ``top`` 500 rows of a SELECT that repeats a LOB column would otherwise
    read gigabytes before the answer's cap refuses it. A failing read is the
    fixed :data:`LOB_UNREADABLE`, never the driver's text. Unverified on the
    real driver: ``read(size)`` as the first positional argument and the
    type it returns.
    """

    def __init__(self, per_value: int, budget: int) -> None:
        self.per_value = per_value
        self.left = budget

    def value(self, value: Any) -> Any:
        read = getattr(value, "read", None)
        if not callable(read):
            return value
        if self.left <= 0:
            raise LobBudget()
        try:
            content = read(min(self.per_value, self.left) + 1)
        except Exception:  # noqa: BLE001 - the driver's text never leaves
            return LOB_UNREADABLE
        if not isinstance(content, (str, bytes, bytearray, memoryview)):
            return LOB_UNREADABLE
        if len(content) > self.left:
            raise LobBudget()
        kept = content[:self.per_value]
        self.left -= len(kept)
        if isinstance(content, str):
            return kept + LOB_CUT_MARKER if len(content) > self.per_value else kept
        return bytes(kept)


class RowBudget(Exception):
    """The plain values of one call passed its ``row_chars_budget`` (no text)."""


def row_chars(row: Any) -> int:
    """What one fetched row counts against a ``row_chars_budget``: the length
    of its text and binary values. A LOB object is not counted here (its
    reading has its own budget, :class:`LobReader`), nor is a number or a
    date (bounded in size by its type)."""
    return sum(len(v) for v in row if isinstance(v, (str, bytes, bytearray)))


def fetch_rows(cursor: Any, max_rows: int, row_budget: int | None) -> list:
    """``max_rows + 1`` rows. Without a budget ONE ``fetchmany``, as always.
    With one, chunks of :data:`ROW_FETCH_CHUNK`, counted as they arrive: once
    the sum passes the budget nothing more is fetched and :class:`RowBudget`
    ends the call (the caller answers ``result_too_large``). Why: an export of
    a wide table would otherwise hold 10,001 rows of every column in memory
    before any cap of the file could refuse it."""
    wanted = max_rows + 1
    if row_budget is None:
        return cursor.fetchmany(wanted)
    fetched: list = []
    used = 0
    while len(fetched) < wanted:
        size = min(ROW_FETCH_CHUNK, wanted - len(fetched))
        chunk = cursor.fetchmany(size)
        for row in chunk:
            used += row_chars(row)
        if used > row_budget:
            raise RowBudget()
        fetched.extend(chunk)
        if len(chunk) < size:
            break
    return fetched
