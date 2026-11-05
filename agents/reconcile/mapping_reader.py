"""The generic mapping reader: rows of one sheet, filtered, in the columns asked for.

The layout (sheet, header row, which column filters and which columns to
return) is a tool argument the agent's instructions supply, so this reader
holds no customer layout. It is CPU-bound: callers run it in
``asyncio.to_thread``.

Arguments come from a model, so each is checked by type before use (a non-string
is refused, not coerced), and every refusal is a fixed text with a stable code.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from agents.reconcile.spec import ColumnRef
from agents.reconcile.workbook import (
    MAX_SHEET_COLUMNS,
    Refused,
    cell_text,
    find_header_row,
    header,
    norm_label,
    opened,
    resolve_column,
)
from agents.reconcile.workbook import sheet as find_sheet
from agents.sharepoint_workbook import BLANK_ROWS_END, _rows

MAX_MAPPING_ROWS = 2000
MAX_HEADER_ROW = 1000  # XrefLookup.header_row's own bound


def _invalid(message: str) -> Refused:
    return Refused("invalid_arguments", message)


def _ref(value: Any) -> ColumnRef:
    if isinstance(value, ColumnRef):
        return value
    if not isinstance(value, dict):
        raise _invalid("a column is an object with either letter or label")
    try:
        return ColumnRef.model_validate(value)
    except ValidationError:
        raise _invalid("a column is an object with either letter or label") from None


def _check(
    sheet: Any, header_row: Any, filter_column: Any, filter_value: Any, columns: Any
) -> tuple[ColumnRef | None, list[ColumnRef]]:
    if not isinstance(sheet, str) or not 1 <= len(sheet) <= 31:
        raise _invalid("sheet must be a sheet name of 1 to 31 characters")
    if header_row is not None and (
        isinstance(header_row, bool) or not isinstance(header_row, int)
        or not 1 <= header_row <= MAX_HEADER_ROW
    ):
        raise _invalid(f"header_row must be a row number from 1 to {MAX_HEADER_ROW}, or null")
    if (filter_column is None) != (filter_value is None):
        raise _invalid("give both filter_column and filter_value, or neither")
    if filter_value is not None and (not isinstance(filter_value, str) or not filter_value.strip()
                                     or len(filter_value) > 255):
        raise _invalid("filter_value must be a text of 1 to 255 characters")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_SHEET_COLUMNS:
        raise _invalid(f"columns must be a list of 1 to {MAX_SHEET_COLUMNS} columns")
    return (None if filter_column is None else _ref(filter_column)), [_ref(c) for c in columns]


def read_mapping(
    data: bytes,
    *,
    sheet: Any,
    header_row: Any,
    filter_column: Any,
    filter_value: Any,
    columns: Any,
) -> dict[str, Any]:
    """The rows below the header whose filter cell equals ``filter_value``.

    ``header_row`` ``None`` finds the header from the labels asked for.
    Returns ``{"sheet", "header_row", "columns", "rows", "truncated"}``:
    ``columns`` are the header labels as written (whitespace collapsed),
    each row is ``{"row": <sheet row number>, "values": [...]}`` in the order
    of ``columns``. Rows whose requested cells are all blank are left out;
    reading ends after :data:`BLANK_ROWS_END` consecutive blank rows or at
    :data:`MAX_MAPPING_ROWS` matching rows (``truncated``).
    """
    fcol, refs = _check(sheet, header_row, filter_column, filter_value, columns)
    wanted = None if filter_value is None else norm_label(filter_value)
    with opened(data) as book:
        ws = find_sheet(book, sheet)
        if header_row is None:
            header_row = find_header_row(ws, [*([fcol] if fcol else []), *refs])
        labels = header(ws, header_row)
        indices = [resolve_column(labels, r) for r in refs]
        findex = None if fcol is None else resolve_column(labels, fcol)
        width = max([*indices, *([findex] if findex is not None else [])]) + 1

        rows: list[dict[str, Any]] = []
        truncated = False
        blank_run = 0
        for number, cells in enumerate(_rows(ws, header_row + 1, None, width), header_row + 1):
            texts = [cell_text(c) for c in cells]
            texts += [""] * (width - len(texts))
            if not any(texts):
                blank_run += 1
                if blank_run >= BLANK_ROWS_END:
                    break
                continue
            blank_run = 0
            if findex is not None and norm_label(texts[findex]) != wanted:
                continue
            values = [texts[i] for i in indices]
            if not any(values):
                continue
            if len(rows) == MAX_MAPPING_ROWS:
                truncated = True
                break
            rows.append({"row": number, "values": values})
        title = ws.title
    names = [labels[i] if i < len(labels) else "" for i in indices]
    return {"sheet": title, "header_row": header_row, "columns": names,
            "rows": rows, "truncated": truncated}
