"""Workbook helpers for the reconciliation tools: labels, columns, header rows.

The mapping workbook is an upload from the chat, so it is untrusted: it is
opened through the guarded reader of ``builtin:sharepoint``
(:mod:`agents.sharepoint_workbook`), reused on purpose so the same size caps
hold and every archive member keeps to its declared size. Nothing here knows a
layout: sheet names, header rows and column labels arrive as arguments.

Labels are compared after :func:`norm_label`, because hand-kept sheets write
the same label with newlines, no-break spaces, double spaces and odd case. A
label may also be a unique prefix of a header label, since rule columns tend to
carry long titles. :class:`Refused` texts are fixed: they never quote a cell,
and not the requested label either (it may have been copied from a cell).
"""

from __future__ import annotations

import unicodedata
from contextlib import contextmanager
from typing import Any, Iterator

from agents.reconcile.spec import ColumnRef
from agents.sharepoint_views import column_index
from agents.sharepoint_workbook import (
    ERROR_CELL,
    Refused,
    _plain,
    _rows,
    check_archive,
    open_workbook,
)

__all__ = [
    "MAX_CELL_CHARS", "MAX_HINT_NAME", "MAX_LABEL_CHARS", "MAX_SCAN_ROWS",
    "MAX_SHEET_COLUMNS", "MAX_SHEETS_LISTED", "Refused", "cell_text", "find_header_row",
    "header", "hint_name", "norm_label", "opened", "resolve_column", "sheet",
]

MAX_SCAN_ROWS = 50  # how far down a header row is looked for
MAX_SHEET_COLUMNS = 200
MAX_CELL_CHARS = 2000
MAX_LABEL_CHARS = 200  # ColumnRef.label's own limit
MAX_SHEETS_LISTED = 100
MAX_HINT_NAME = 40
MAX_SHEET_NAME = 31  # Excel's own limit for a sheet name


def norm_label(value: Any) -> str:
    """The form two labels are compared in: NFKC, whitespace collapsed, casefolded.

    NFKC turns a no-break space into a space, and ``split()`` then folds every
    run of whitespace (newlines included) into one.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hint_name(text: str, limit: int = MAX_HINT_NAME) -> str:
    """A sheet or column name for a hint: control/format characters gone, cut short.

    The name was typed into the upload (or copied from it by a model), so it
    may carry a newline or a bidi mark that would reshape the hint.
    """
    flat = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return _cut(flat, limit)


def cell_text(cell: Any, limit: int = MAX_CELL_CHARS) -> str:
    """A cell as text of at most ``limit`` characters; ``""`` for empty and error cells."""
    value = _plain(cell)
    if value is None or value is ERROR_CELL:
        return ""
    return _cut(value if isinstance(value, str) else str(value), limit)


def _label(cell: Any) -> str:
    """A header cell as written, with its whitespace collapsed (case kept)."""
    text = cell_text(cell, MAX_CELL_CHARS)
    return _cut(" ".join(unicodedata.normalize("NFKC", text).split()), MAX_LABEL_CHARS)


def _trimmed(labels: list[str]) -> list[str]:
    while labels and not labels[-1]:
        labels.pop()
    return labels


@contextmanager
def opened(data: bytes) -> Iterator[Any]:
    """The workbook opened on the guarded archive (caps checked first), closed on exit."""
    check_archive(data)
    book = open_workbook(data)
    try:
        yield book
    finally:
        book.close()


def sheet(book: Any, name: str) -> Any:
    """The sheet ``name``: exact first, then case-insensitive (Excel's own rule).

    The refusal lists the sheet names (at most :data:`MAX_SHEETS_LISTED`) in
    its hint so the model can correct itself; names are not cell values, but
    each passes :func:`hint_name` (cut to Excel's 31 characters) since a
    crafted file may hold longer names with control characters.
    """
    names = list(book.sheetnames)
    if name in names:
        return book[name]
    wanted = name.casefold()
    for candidate in names:
        if candidate.casefold() == wanted:
            return book[candidate]
    listed = ", ".join(hint_name(str(n), MAX_SHEET_NAME) for n in names[:MAX_SHEETS_LISTED])
    more = len(names) - MAX_SHEETS_LISTED
    if more > 0:
        listed += f" (and {more} more)"
    raise Refused("sheet_not_found", "the workbook has no sheet of that name",
                  f"the sheets are: {listed}")


def header(ws: Any, row: int, max_col: int = MAX_SHEET_COLUMNS) -> list[str]:
    """The labels of ``row`` as written (whitespace collapsed), trailing blanks dropped."""
    for cells in _rows(ws, row, row, max_col):
        return _trimmed([_label(c) for c in cells])
    return []


def resolve_column(header: list[str], ref: ColumnRef) -> int:
    """The 0-based column of ``ref`` in ``header``.

    A letter is its position whatever the labels say. A label matches exactly
    (after :func:`norm_label`) or else as the unique prefix of one label; an
    exact match wins over prefixes, so ``Rule`` still finds ``Rule`` next to
    ``Rule type``.
    """
    if ref.letter is not None:
        index = column_index(ref.letter) - 1
        if index >= MAX_SHEET_COLUMNS:
            raise Refused("column_not_found", "a requested column is beyond what this tool reads")
        return index
    wanted = norm_label(ref.label)
    labels = [norm_label(h) for h in header]
    exact = [i for i, h in enumerate(labels) if h == wanted]
    matches = exact or [i for i, h in enumerate(labels) if h and h.startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise Refused("column_ambiguous", "a requested column label matches more than one column",
                      "give more of the label, or the column letter")
    raise Refused("column_not_found", "a requested column label is not in the header row",
                  "check the header row and the label, or give the column letter")


def find_header_row(ws: Any, refs: list[ColumnRef], max_scan: int = MAX_SCAN_ROWS) -> int:
    """The first row (1-based, within ``max_scan`` rows) where every label of ``refs`` resolves.

    Letters say nothing about a header, so at least one label is needed. A row
    where every label is found but one matches twice is the header with an
    ambiguous label: that is refused as such rather than scanned past.
    """
    labelled = [r for r in refs if r.label is not None]
    if not labelled:
        raise Refused("header_not_found", "the header row cannot be found from column letters",
                      "give the header row")
    for number, cells in enumerate(_rows(ws, 1, max_scan, MAX_SHEET_COLUMNS), 1):
        labels = _trimmed([_label(c) for c in cells])
        if not labels:
            continue
        ambiguous: Refused | None = None
        found = True
        for ref in labelled:
            try:
                resolve_column(labels, ref)
            except Refused as err:
                if err.code != "column_ambiguous":
                    found = False
                    break
                ambiguous = ambiguous or err
        if found:
            if ambiguous is not None:
                raise ambiguous
            return number
    raise Refused("header_not_found",
                  f"no row among the first {max_scan} holds every requested label",
                  "give the header row, or check the labels")
