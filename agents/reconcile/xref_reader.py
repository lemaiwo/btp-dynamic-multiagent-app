"""Cross-reference sheets: a grid for the model, typed lookups for the run.

A code-table workbook is hand-kept: tables start below title rows, sit side by
side on one sheet, repeat labels. The model therefore first looks at a sheet as
a grid (:func:`list_sheets`, :func:`read_grid`: column letters and sheet row
numbers, cells cut short) and then writes, per ``value_map`` rule, the
:class:`~agents.reconcile.spec.XrefLookup` that names the header row and the
key and value columns. The run turns those into :class:`Lookup` tables
(:func:`build_lookups`), which the compare engine consults through its
``LookupTable`` protocol; the engine does not import this module.

The workbook is an upload, so it is opened through the guarded reader
(:func:`agents.reconcile.workbook.opened`), every scan is capped, and refusals
are fixed texts: a hint may name a sheet and a header row (cut, control
characters removed), never a cell value. Everything here is CPU-bound: callers
run it in ``asyncio.to_thread``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from openpyxl.utils import get_column_letter

from agents.reconcile.spec import ReconcileSpec, ValueMapRule, XrefLookup
from agents.reconcile.values import clean
from agents.reconcile.workbook import (
    Refused,
    cell_text,
    header,
    opened,
    resolve_column,
)
from agents.reconcile.workbook import sheet as find_sheet
from agents.sharepoint_workbook import BLANK_ROWS_END, _rows

__all__ = [
    "MAX_GRID_CELL_CHARS", "MAX_GRID_COLUMNS", "MAX_GRID_ROWS", "MAX_LOOKUP_ROWS",
    "MAX_SHEETS", "Lookup", "build_lookup", "build_lookups", "list_sheets", "read_grid",
]

MAX_SHEETS = 200
MAX_GRID_ROWS = 500
MAX_GRID_COLUMNS = 60
MAX_GRID_CELL_CHARS = 120
MAX_FIRST_ROW = 1_048_576  # Excel's last row
MAX_LOOKUP_ROWS = 100_000  # data rows read below a header row
MAX_HINT_NAME = 40


@dataclass(frozen=True)
class Lookup:
    """One code table: cleaned key cell(s) -> cleaned value cell; the first row of a key wins.

    ``duplicates`` counts the later rows whose key was already taken, so the
    report can say a table was ambiguous without listing its values.
    """

    entries: dict[tuple[str, ...], str]
    duplicates: int

    def get(self, key: tuple[str, ...]) -> str | None:
        return self.entries.get(key)


def _hint_name(text: str) -> str:
    """A sheet or column name for a hint: control/format characters gone, cut short.

    The name was typed into the upload (or copied from it by a model), so it
    may carry a newline or a bidi mark that would reshape the hint.
    """
    flat = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return flat if len(flat) <= MAX_HINT_NAME else flat[: MAX_HINT_NAME - 1] + "…"


def _invalid(message: str) -> Refused:
    return Refused("invalid_arguments", message)


def _whole(value: Any, low: int, high: int | None, name: str) -> int:
    """A whole number of at least ``low``; above ``high`` it is clamped (a cap, not an error).

    ``bool`` is refused although it is an ``int``: ``True`` is no row count.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise _invalid(f"{name} must be a whole number of at least {low}")
    return value if high is None else min(value, high)


def _dimension(ws: Any, attr: str) -> int | None:
    """A sheet's declared size, or ``None`` (read-only sheets without a dimension record)."""
    value = getattr(ws, attr, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def list_sheets(data: bytes) -> list[dict[str, Any]]:
    """Every sheet (at most :data:`MAX_SHEETS`) with its declared ``max_row`` / ``max_column``.

    The sizes are what the file declares, not counted: ``None`` when it
    declares none, and possibly larger than the data (formatting counts).
    """
    with opened(data) as book:
        out: list[dict[str, Any]] = []
        for name in list(book.sheetnames)[:MAX_SHEETS]:
            ws = book[name]
            out.append({"name": name, "max_row": _dimension(ws, "max_row"),
                        "max_column": _dimension(ws, "max_column")})
    return out


def _trim(cells: list[str]) -> list[str]:
    while cells and not cells[-1]:
        cells.pop()
    return cells


def read_grid(
    data: bytes,
    sheet: Any,
    *,
    first_row: Any = 1,
    max_rows: Any = 50,
    max_columns: Any = 26,
) -> dict[str, Any]:
    """A window of a sheet as text, for the model to find tables and header rows in.

    The window is sheet rows ``first_row`` to ``first_row + max_rows - 1`` and
    columns ``A`` onward; ``max_rows`` is clamped to :data:`MAX_GRID_ROWS`,
    ``max_columns`` to :data:`MAX_GRID_COLUMNS`, each cell cut at
    :data:`MAX_GRID_CELL_CHARS`. Blank rows are left out (each row carries its
    number) and trailing blank cells dropped. ``truncated`` says that a
    non-blank row follows the window within :data:`BLANK_ROWS_END` rows.
    """
    if not isinstance(sheet, str) or not 1 <= len(sheet) <= 31:
        raise _invalid("sheet must be a sheet name of 1 to 31 characters")
    first = _whole(first_row, 1, None, "first_row")
    if first > MAX_FIRST_ROW:
        raise _invalid(f"first_row must be at most {MAX_FIRST_ROW}")
    rows_wanted = _whole(max_rows, 1, MAX_GRID_ROWS, "max_rows")
    width = _whole(max_columns, 1, MAX_GRID_COLUMNS, "max_columns")
    last = first + rows_wanted - 1
    with opened(data) as book:
        ws = find_sheet(book, sheet)
        rows: list[dict[str, Any]] = []
        truncated = False
        for number, cells in enumerate(
            _rows(ws, first, last + BLANK_ROWS_END, width), first
        ):
            texts = _trim([cell_text(c, MAX_GRID_CELL_CHARS) for c in cells][:width])
            if not texts:
                continue
            if number > last:
                truncated = True
                break
            rows.append({"row": number, "cells": texts})
        title = ws.title
    return {"sheet": title, "first_row": first,
            "columns": [get_column_letter(i) for i in range(1, width + 1)],
            "rows": rows, "truncated": truncated}


def build_lookup(book: Any, xref: XrefLookup) -> Lookup:
    """The code table ``xref`` names, read from an open workbook.

    Key and value columns are resolved on ``xref.header_row`` (a letter is its
    position, a label must match one column; :func:`resolve_column`). Cells
    are cleaned with :func:`values.clean`, as the engine cleans source values.
    A row whose key cells are all blank is skipped; the first row of a key
    wins. Reading ends after :data:`BLANK_ROWS_END` consecutive rows blank in
    every key and value column, or after :data:`MAX_LOOKUP_ROWS` rows.
    """
    ws = find_sheet(book, xref.sheet)
    labels = header(ws, xref.header_row)
    keys = [resolve_column(labels, ref) for ref in xref.keys]
    value = resolve_column(labels, xref.value)
    width = max([*keys, value]) + 1
    entries: dict[tuple[str, ...], str] = {}
    duplicates = 0
    blank_run = 0
    start = xref.header_row + 1
    for cells in _rows(ws, start, start + MAX_LOOKUP_ROWS - 1, width):
        texts = [clean(cell_text(c)) for c in cells]
        texts += [""] * (width - len(texts))
        key = tuple(texts[i] for i in keys)
        if not any(key):
            if not texts[value]:
                blank_run += 1
                if blank_run >= BLANK_ROWS_END:
                    break
            else:
                blank_run = 0
            continue
        blank_run = 0
        if key in entries:
            duplicates += 1
        else:
            entries[key] = texts[value]
    return Lookup(entries=entries, duplicates=duplicates)


def _signature(xref: XrefLookup) -> tuple[Any, ...]:
    """What makes two lookups the same table (``XrefLookup`` holds a list: not hashable)."""
    return (xref.sheet, xref.header_row, tuple(xref.keys), xref.value)


def build_lookups(data: bytes, spec: ReconcileSpec) -> dict[tuple[str, int], Lookup]:
    """Every lookup of every ``value_map`` rule, keyed ``(csv column, lookup index)``.

    The workbook is opened once (not at all without a ``value_map`` rule), and
    a table named identically by several rules is read once and shared. A
    lookup without a single entry is refused as ``lookup_empty``: it almost
    always means a wrong header row or column, and every row would otherwise
    be reported as a miss. A refusal's hint names the CSV column and the
    lookup's position (1-based), cut and cleaned.
    """
    wanted = [
        (column, index, xref)
        for column, rule in spec.columns.items()
        if isinstance(rule, ValueMapRule)
        for index, xref in enumerate(rule.lookups)
    ]
    if not wanted:
        return {}
    built: dict[tuple[Any, ...], Lookup] = {}
    out: dict[tuple[str, int], Lookup] = {}
    with opened(data) as book:
        for column, index, xref in wanted:
            where = f"lookup {index + 1} of column '{_hint_name(column)}'"
            sig = _signature(xref)
            table = built.get(sig)
            if table is None:
                try:
                    table = build_lookup(book, xref)
                except Refused as err:
                    hint = f"{err.hint}; {where}" if err.hint else where
                    raise Refused(err.code, err.message, hint) from None
                if not table.entries:
                    raise Refused(
                        "lookup_empty",
                        "a code-table lookup has no entries below its header row",
                        f"{where}: sheet '{_hint_name(xref.sheet)}', header row "
                        f"{xref.header_row}; check the header row and the key columns",
                    )
                built[sig] = table
            out[(column, index)] = table
    return out

