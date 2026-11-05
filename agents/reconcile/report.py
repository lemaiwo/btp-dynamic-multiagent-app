"""The reconciliation Excel report: Summary, Mismatches, Missing in ECC, Not checked.

``agents.webchat.docgen.create_excel_workbook`` caps a sheet at 10,000 rows and
the file at 10 MiB; a reconciliation lists up to the entry's mismatch cap
(50,000 rows and more), so the report has its own openpyxl write-only writer.
It keeps docgen's rule that text never becomes a formula: every text cell is a
``WriteOnlyCell`` with ``data_type = "s"``, so a migrated value such as
``=SUM(A1)`` is shown, not evaluated, and XML-illegal characters are removed
with ``docgen.xml_text`` (openpyxl refuses them).

CPU-bound: callers run :func:`build_report` through ``asyncio.to_thread`` and
store the bytes with ``agents.webchat.store.add_generated_file`` and
:data:`REPORT_MEDIA_TYPE`.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agents.reconcile.engine import EngineResult
from agents.reconcile.spec import ReconcileSpec
from agents.webchat.docgen import XLSX, xml_text

REPORT_MEDIA_TYPE = XLSX
# Above this the file is refused rather than stored: a chat file this large
# would not open usefully anyway, and the engine's caps keep real runs far below.
REPORT_MAX_BYTES = 64 * 1024 * 1024

SHEET_SUMMARY = "Summary"
SHEET_MISMATCHES = "Mismatches"
SHEET_MISSING = "Missing in ECC"
SHEET_NOT_CHECKED = "Not checked"

MISMATCH_HEADER = ("Key", "Column", "Rule", "ECC value", "Expected", "CSV value", "Reason")
MISSING_HEADER = ("Key", "CSV line")
NOT_CHECKED_HEADER = ("Column", "Rule", "Reason", "Needs table")
COLUMN_HEADER = ("Column", "Rule", "Checked", "Matched", "Mismatched", "Not checked",
                 "First mismatch reason")

# Engine reason codes as the tester reads them; an unknown code is shown as is.
REASON_TEXTS = {
    "value_differs": "Value differs",
    "no_xref_entry": "No code table entry",
    "must_be_blank": "Must be blank",
    "unresolved": "Rule not resolved",
    "needs_other_table": "Needs another table",
}

_ERROR_TEXTS = {"report_too_large": "The reconciliation report is too large to store."}
_FILE_PART = re.compile(r"[^A-Za-z0-9_-]+")


class ReportError(Exception):
    """A report that cannot be produced; ``code`` is stable, the text fixed."""

    def __init__(self, code: str) -> None:
        super().__init__(_ERROR_TEXTS.get(code, "The reconciliation report failed."))
        self.code = code


@dataclass(frozen=True)
class ReportMeta:
    """What the Summary states about the run besides the engine's counts."""

    run_id: str
    spec_name: str
    started_at: datetime | None
    finished_at: datetime | None


def report_file_name(spec: ReconcileSpec, meta: ReportMeta) -> str:
    """A download name built from the table, wave and run id (safe characters only)."""
    parts = [_FILE_PART.sub("_", p).strip("_") or "x" for p in (spec.table, spec.wave, meta.run_id)]
    return "reconcile-" + "-".join(parts) + ".xlsx"


def _reason(code: str) -> str:
    return REASON_TEXTS.get(code, code)


def _when(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class _Writer:
    """Appends rows to a write-only sheet, text as text and numbers as ``int``."""

    def __init__(self, ws: Any) -> None:
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font

        self._ws = ws
        self._cell = WriteOnlyCell
        self._bold = Font(bold=True)

    def row(self, values: Iterable[Any], *, bold: bool = False) -> None:
        out: list[Any] = []
        for value in values:
            if value is None:
                out.append(None)
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                cell = self._cell(self._ws, value=xml_text(str(value)))
                cell.data_type = "s"  # never a formula, whatever the text starts with
            else:
                cell = self._cell(self._ws, value=int(value))
            if bold:
                cell.font = self._bold
            out.append(cell)
        self._ws.append(out)


def _sheet(book: Any, title: str, header: tuple[str, ...], widths: tuple[int, ...]) -> _Writer:
    from openpyxl.utils import get_column_letter

    ws = book.create_sheet(title=title)
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    writer = _Writer(ws)
    writer.row(header, bold=True)
    return writer


def _cap_line(label: str, listed: int, dropped: int) -> str | None:
    if dropped <= 0:
        return None
    return f"{label} rows listed: {listed} of {listed + dropped} (cap {listed})"


def _write_summary(book: Any, spec: ReconcileSpec, result: EngineResult, meta: ReportMeta) -> None:
    out = _sheet(book, SHEET_SUMMARY, ("Item", "Value"), (24, 40, 10, 10, 12, 12, 24))
    for item in (
        ("Table", spec.table),
        ("Wave", spec.wave),
        ("Spec file", meta.spec_name),
        ("Run id", meta.run_id),
        ("Started", _when(meta.started_at)),
        ("Finished", _when(meta.finished_at)),
        ("Rows total", result.rows_total),
        ("Rows compared", result.rows_compared),
        ("Malformed rows", result.malformed_rows),
        ("Invalid keys", result.invalid_keys),
        ("Duplicate keys", result.duplicate_keys),
        ("Missing in ECC", result.missing_count),
        ("Mismatches", result.mismatch_count),
    ):
        out.row(item)
    caps = [
        _cap_line("Mismatch", len(result.mismatches), result.mismatches_dropped),
        _cap_line("Missing", len(result.missing_in_ecc), result.missing_in_ecc_dropped),
    ]
    for line in caps:
        if line:
            out.row((line,))
    out.row(())
    out.row(COLUMN_HEADER, bold=True)
    first_reason: dict[str, str] = {}
    for m in result.mismatches:
        first_reason.setdefault(m.column, m.reason)
    # Spec order first (the order the tester wrote), then any column only the
    # engine knows, so no counted column is left out of the table.
    columns = list(spec.columns) + [c for c in result.stats if c not in spec.columns]
    for column in columns:
        stats = result.stats.get(column)
        kind = stats.kind if stats else spec.columns[column].kind
        reason = first_reason.get(column)
        out.row((
            column,
            kind,
            stats.checked if stats else 0,
            stats.matched if stats else 0,
            stats.mismatched if stats else 0,
            stats.not_checked if stats else 0,
            _reason(reason) if reason else None,
        ))


def build_report(spec: ReconcileSpec, result: EngineResult, meta: ReportMeta) -> bytes:
    """The ``.xlsx`` bytes of the report (CPU work: call it in a thread).

    ``ReportError("report_too_large")`` when the file passes
    :data:`REPORT_MAX_BYTES`.
    """
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    _write_summary(book, spec, result, meta)

    out = _sheet(book, SHEET_MISMATCHES, MISMATCH_HEADER, (22, 20, 12, 24, 24, 24, 20))
    for m in result.mismatches:
        out.row((m.key, m.column, m.kind, m.source, m.expected, m.actual, _reason(m.reason)))

    out = _sheet(book, SHEET_MISSING, MISSING_HEADER, (22, 10))
    for key, line in result.missing_in_ecc:
        out.row((key, line))

    out = _sheet(book, SHEET_NOT_CHECKED, NOT_CHECKED_HEADER, (20, 12, 22, 14))
    for n in result.not_checked:
        out.row((n.column, n.kind, _reason(n.reason), n.needs_table))

    buffer = io.BytesIO()
    book.save(buffer)
    data = buffer.getvalue()
    if len(data) > REPORT_MAX_BYTES:
        raise ReportError("report_too_large")
    return data
