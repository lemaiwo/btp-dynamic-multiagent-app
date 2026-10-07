"""Reads the views of a ``builtin:sharepoint`` entry out of workbook bytes.

Pure: bytes in, plain data out, no HTTP and no config parsing (the views come
validated from :mod:`agents.sharepoint_views`). It is CPU work on a file
somebody else wrote: call it through ``asyncio.to_thread``.

What the file may do to us is bounded before and while it is read: the size,
the number of archive members and their declared uncompressed size
(:func:`check_archive`), the rows and columns of a view, and ``openpyxl`` in
read-only mode (rows are streamed; with ``defusedxml`` installed, which
``requirements.txt`` pins, it parses no DTD).

Rules of what leaves this module:

* **Table view**: the pinned columns of the Excel table, nothing else.
* **Calendar view, default-deny on cell values**: a cell reaches the caller
  only as the status its code maps to. A value that is not in ``codes`` (an
  unknown code, a number, an error value) is counted as ``unmapped`` and
  never passed on.
* **Rows that cannot be used** (no member, an error value in a label cell, a
  second row for the same member and kind) are skipped and counted.
* **No cached values is a refusal.** Formula results are read as stored by
  the application that saved the file. ``openpyxl`` cannot tell a formula
  without a stored result from one whose result is empty, so the rule is:
  a requested day that is missing from the date row while that row holds a
  formula without a result, or a view in which no formula at all has a
  result, is ``no_cached_values``. Nothing is ever calculated or guessed.

Every refusal is a :class:`Refused` with a stable code and a fixed text.
"""

from __future__ import annotations

import io
import posixpath
import unicodedata
import zipfile
from datetime import date, datetime
from typing import Any, Iterator

from agents.sharepoint_views import TableView

__all__ = [
    "MAX_FILE_BYTES", "MAX_WINDOW_DAYS", "Refused", "check_archive", "check_window",
    "read_table",
]

MAX_FILE_BYTES = 20 * 1024 * 1024
# Declared size of all archive members together, checked before anything is
# inflated. A workbook of the size cap inflates to a fraction of this.
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000
MAX_WINDOW_DAYS = 120
MAX_TABLE_ROWS = 5000
MAX_TABLE_COLUMNS = 200
# Rows of a calendar below ``first_row``; reading also ends at ``stop_at``
# and after this many rows in a row with nothing in them.
MAX_CALENDAR_ROWS = 2000
BLANK_ROWS_END = 50
# A year sheet has at most 366 day columns; a few spare for layout.
MAX_DAY_COLUMNS = 400

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_ERROR = object()  # a cell holding an Excel error value


class Refused(Exception):
    """A read that is refused: ``code`` is stable, ``message`` a fixed text."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": error}


def check_window(date_from: Any, date_to: Any) -> tuple[date, date]:
    """Two ISO dates, in order, at most :data:`MAX_WINDOW_DAYS` days."""
    try:
        if not (isinstance(date_from, str) and isinstance(date_to, str)
                and len(date_from) == 10 and len(date_to) == 10):
            raise ValueError
        lo, hi = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        raise Refused(
            "invalid_dates", "date_from and date_to must be dates as YYYY-MM-DD"
        ) from None
    if hi < lo:
        raise Refused("invalid_dates", "date_to must not be before date_from")
    if (hi - lo).days + 1 > MAX_WINDOW_DAYS:
        raise Refused(
            "window_too_long", f"a window may span at most {MAX_WINDOW_DAYS} days",
            "ask for a shorter period",
        )
    return lo, hi


def check_archive(data: bytes) -> None:
    """Size and archive caps, before the workbook is parsed."""
    if len(data) > MAX_FILE_BYTES:
        raise Refused("too_large", "the workbook is larger than this tool reads")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise Refused("not_a_workbook", "the file is not an Excel workbook") from None
    if len(infos) > MAX_ARCHIVE_MEMBERS \
            or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise Refused("too_large", "the workbook is larger than this tool reads")


def _open(data: bytes, *, data_only: bool) -> Any:
    from openpyxl import load_workbook

    try:
        return load_workbook(io.BytesIO(data), read_only=True, data_only=data_only,
                             keep_links=False)
    except Exception:  # noqa: BLE001 - whatever the parser says, the answer is one code
        raise Refused("not_a_workbook", "the file is not an Excel workbook") from None


def _plain(cell: Any) -> Any:
    """A cell's stored value as plain data; :data:`_ERROR` for an error value."""
    if getattr(cell, "data_type", "n") == "e":
        return _ERROR
    value = cell.value
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value).strip()
        return text or None
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() \
            else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _rows(
    ws: Any, min_row: int, max_row: int, max_col: int, min_col: int = 1
) -> Iterator[tuple]:
    try:
        yield from ws.iter_rows(min_row=min_row, max_row=max_row,
                                min_col=min_col, max_col=max_col)
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - a broken sheet part
        raise Refused("not_a_workbook", "the file is not an Excel workbook") from None


def _no_results(formulas: int, with_result: int) -> None:
    if formulas and not with_result:
        raise Refused(
            "no_cached_values",
            "the workbook holds formulas without stored results",
            "it was last saved by a program that does not calculate; "
            "open and save it in Excel",
        )


# --- table view -----------------------------------------------------------

def _rels(zf: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """Relationship id -> (type, target part) of ``part`` ('' = the package)."""
    from openpyxl.xml.functions import fromstring

    folder, name = posixpath.split(part)
    try:
        root = fromstring(zf.read(posixpath.join(folder, "_rels", f"{name}.rels")))
    except KeyError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for rel in root.iter(f"{{{_PKG_REL}}}Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        path = target.lstrip("/") if target.startswith("/") \
            else posixpath.normpath(posixpath.join(folder, target))
        out[rel.get("Id") or ""] = (rel.get("Type") or "", path)
    return out


def _find_table(data: bytes, name: str) -> tuple[str, str, int]:
    """(sheet title, cell range, totals rows) of the Excel table ``name``.

    Read from the package parts: a read-only worksheet of openpyxl does not
    load its tables.
    """
    from openpyxl.xml.functions import fromstring

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            book = next((p for t, p in _rels(zf, "").values()
                         if t.endswith("/officeDocument")), "xl/workbook.xml")
            parts = _rels(zf, book)
            for sheet in fromstring(zf.read(book)).iter(f"{{{_MAIN}}}sheet"):
                part = parts.get(sheet.get(f"{{{_DOC_REL}}}id") or "")
                if part is None:
                    continue
                for kind, target in _rels(zf, part[1]).values():
                    if not kind.endswith("/table"):
                        continue
                    table = fromstring(zf.read(target))
                    if name in (table.get("name"), table.get("displayName")):
                        return (sheet.get("name") or "", table.get("ref") or "",
                                int(table.get("totalsRowCount") or 0))
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - a broken package
        raise Refused("not_a_workbook", "the file is not an Excel workbook") from None
    raise Refused("table_not_found", "the workbook has no table of the configured name",
                  "an administrator must correct the view")


def read_table(data: bytes, view: TableView) -> dict[str, Any]:
    """The rows of a table view: the pinned columns only."""
    from openpyxl.utils.cell import range_boundaries

    check_archive(data)
    title, ref, totals = _find_table(data, view.table)
    try:
        min_col, min_row, max_col, max_row = range_boundaries(ref)
    except Exception:  # noqa: BLE001
        raise Refused("not_a_workbook", "the file is not an Excel workbook") from None
    max_row -= totals
    if max_row - min_row > MAX_TABLE_ROWS or max_col - min_col >= MAX_TABLE_COLUMNS:
        raise Refused("too_large", "the table is larger than this tool reads")

    book = _open(data, data_only=True)
    try:
        if title not in book.sheetnames:
            raise Refused("table_not_found", "the workbook has no table of the configured name")
        grid = [[_plain(c) for c in row]
                for row in _rows(book[title], min_row, max_row, max_col, min_col)]
    finally:
        book.close()
    header = grid[0] if grid else []
    index: dict[str, int] = {}
    for i, name in enumerate(header):
        if isinstance(name, str):
            index.setdefault(name, i)
    if any(c not in index for c in view.columns):
        raise Refused("column_not_found", "a pinned column is not in the table",
                      "an administrator must correct the view")

    formulas = with_result = 0
    book = _open(data, data_only=False)
    try:
        for r, row in enumerate(_rows(book[title], min_row + 1, max_row, max_col, min_col), 1):
            for c, cell in enumerate(row):
                if cell.data_type == "f":
                    formulas += 1
                    with_result += r < len(grid) and c < len(grid[r]) and grid[r][c] is not None
    finally:
        book.close()
    _no_results(formulas, with_result)

    rows = []
    for line in grid[1:]:
        record = {c: (None if line[index[c]] is _ERROR else line[index[c]])
                  for c in view.columns}
        if any(v is not None for v in record.values()):
            rows.append(record)
    return {"columns": list(view.columns), "rows": rows}
