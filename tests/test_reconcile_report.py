"""The reconciliation Excel report (``agents.reconcile.report``).

Every test builds an ``EngineResult`` by hand, writes the report and reads it
back with openpyxl: the sheets, their header rows, the caps stated on the
Summary, and the rule that a text cell never becomes a formula.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from openpyxl import load_workbook

from agents.reconcile import report
from agents.reconcile.engine import ColumnStats, EngineResult, Mismatch, NotChecked
from agents.reconcile.report import (
    REPORT_MEDIA_TYPE,
    ReportError,
    ReportMeta,
    build_report,
    report_file_name,
)
from agents.reconcile.spec import ReconcileSpec

META = ReportMeta(
    run_id="run-0001",
    spec_name="ztab-w1.json",
    started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    finished_at=datetime(2026, 1, 2, 3, 9, 0, tzinfo=UTC),
)


def _spec() -> ReconcileSpec:
    return ReconcileSpec.model_validate({
        "table": "ZTAB",
        "wave": "W1",
        "key": {"csv_column": "Object", "ecc_field": "OBJNR", "length": 18},
        "columns": {
            "Kind": {"kind": "direct", "source": "KIND"},
            "Flag": {"kind": "constant", "value": "X"},
            "Other": {"kind": "unresolved", "text": "see the other table", "needs_table": "ZOTHER"},
        },
    })


def _mismatch(
    i: int, column: str = "Kind", actual: str = "B", reason: str = "value_differs"
) -> Mismatch:
    return Mismatch(key=f"K{i:06d}", column=column, kind="direct", source="A",
                    expected="A", actual=actual, reason=reason)


def _result(**over: Any) -> EngineResult:
    raw: dict[str, Any] = {
        "rows_total": 10,
        "rows_compared": 7,
        "malformed_rows": 1,
        "invalid_keys": 1,
        "duplicate_keys": 1,
        "missing_in_ecc": [("K000009", 9), ("K000010", 10)],
        "stats": {
            "Kind": ColumnStats(kind="direct", checked=7, matched=5, mismatched=2),
            "Flag": ColumnStats(kind="constant", checked=7, matched=7),
            "Other": ColumnStats(kind="unresolved", not_checked=7),
        },
        "mismatches": [
            _mismatch(1, actual="=SUM(A1)"),
            _mismatch(2, column="Kind", reason="must_be_blank"),
        ],
        "mismatches_dropped": 0,
        "not_checked": [NotChecked(column="Other", kind="unresolved", reason="needs_other_table",
                                   needs_table="ZOTHER")],
    }
    raw.update(over)
    return EngineResult(**raw)


def _book(data: bytes):
    return load_workbook(io.BytesIO(data))


def _rows(ws) -> list[tuple]:
    return [tuple(c.value for c in row) for row in ws.iter_rows()]


def _summary_map(ws) -> dict[str, Any]:
    return {r[0]: r[1] for r in _rows(ws) if r and r[0] is not None}


def test_sheets_in_order_with_bold_frozen_headers() -> None:
    book = _book(build_report(_spec(), _result(), META))
    assert book.sheetnames == ["Summary", "Mismatches", "Missing in ECC", "Not checked"]
    for ws in book.worksheets:
        assert ws.freeze_panes == "A2"
        assert all(c.font.bold for c in ws[1] if c.value is not None)
    assert _rows(book["Mismatches"])[0] == (
        "Key", "Column", "Rule", "ECC value", "Expected", "CSV value", "Reason")
    assert _rows(book["Missing in ECC"])[0] == ("Key", "CSV line")
    assert _rows(book["Not checked"])[0] == ("Column", "Rule", "Reason", "Needs table")


def test_summary_counts_and_column_table() -> None:
    ws = _book(build_report(_spec(), _result(), META))["Summary"]
    info = _summary_map(ws)
    assert info["Table"] == "ZTAB"
    assert info["Wave"] == "W1"
    assert info["Spec file"] == "ztab-w1.json"
    assert info["Run id"] == "run-0001"
    assert info["Started"] == "2026-01-02T03:04:05+00:00"
    assert info["Finished"] == "2026-01-02T03:09:00+00:00"
    assert info["Rows total"] == 10
    assert info["Rows compared"] == 7
    assert info["Malformed rows"] == 1
    assert info["Invalid keys"] == 1
    assert info["Duplicate keys"] == 1
    assert info["Missing in ECC"] == 2
    assert info["Mismatches"] == 2
    rows = _rows(ws)
    head = rows.index(("Column", "Rule", "Checked", "Matched", "Mismatched", "Not checked",
                       "First mismatch reason"))
    assert all(c.font.bold for c in ws[head + 1] if c.value is not None)
    table = rows[head + 1:head + 4]
    assert table[0] == ("Kind", "direct", 7, 5, 2, 0, "Value differs")
    assert table[1] == ("Flag", "constant", 7, 7, 0, 0, None)
    assert table[2] == ("Other", "unresolved", 0, 0, 0, 7, None)
    assert not any(isinstance(r[0], str) and r[0].startswith("Mismatch rows listed") for r in rows)


def test_numbers_are_ints() -> None:
    ws = _book(build_report(_spec(), _result(), META))["Missing in ECC"]
    assert _rows(ws)[1] == ("K000009", 9)
    assert ws.cell(row=2, column=2).data_type == "n"


def test_cap_lines_stated_when_rows_were_dropped() -> None:
    result = _result(mismatches=[_mismatch(i) for i in range(3)], mismatches_dropped=2,
                     missing_in_ecc_dropped=4)
    ws = _book(build_report(_spec(), result, META))["Summary"]
    texts = [r[0] for r in _rows(ws) if r]
    assert "Mismatch rows listed: 3 of 5 (cap 3)" in texts
    assert "Missing rows listed: 2 of 6 (cap 2)" in texts
    assert _summary_map(ws)["Mismatches"] == 5
    assert _summary_map(ws)["Missing in ECC"] == 6


def test_mismatch_rows_and_text_never_becomes_a_formula() -> None:
    book = _book(build_report(_spec(), _result(), META))
    ws = book["Mismatches"]
    assert _rows(ws)[1] == ("K000001", "Kind", "direct", "A", "A", "=SUM(A1)", "Value differs")
    assert _rows(ws)[2][-1] == "Must be blank"
    cell = ws.cell(row=2, column=6)
    assert cell.value == "=SUM(A1)"
    assert cell.data_type == "s"
    for row in ws.iter_rows(min_row=1):
        for c in row:
            assert c.data_type != "f"
    nc = _rows(book["Not checked"])[1]
    assert nc == ("Other", "unresolved", "Needs another table", "ZOTHER")


def test_xml_illegal_characters_are_removed() -> None:
    result = _result(mismatches=[_mismatch(1, actual="a\x00b\x1fc")])
    meta = ReportMeta(run_id="r\x07", spec_name="s\x0b.json", started_at=None, finished_at=None)
    book = _book(build_report(_spec(), result, meta))
    assert book["Mismatches"].cell(row=2, column=6).value == "abc"
    info = _summary_map(book["Summary"])
    assert info["Run id"] == "r"
    assert info["Spec file"] == "s.json"
    assert info["Started"] is None or info["Started"] == ""


def test_fifty_thousand_mismatches_fast_and_small() -> None:
    result = _result(mismatches=[_mismatch(i, actual=f"value {i}") for i in range(50_000)])
    t0 = time.monotonic()
    data = build_report(_spec(), result, META)
    assert time.monotonic() - t0 < 20
    assert len(data) < 15 * 1024 * 1024
    book = load_workbook(io.BytesIO(data), read_only=True)
    rows = list(book["Mismatches"].iter_rows(values_only=True))
    assert len(rows) == 50_001
    assert rows[-1] == ("K049999", "Kind", "direct", "A", "A", "value 49999", "Value differs")
    book.close()


def test_size_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    assert report.REPORT_MAX_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(report, "REPORT_MAX_BYTES", 1000)
    with pytest.raises(ReportError) as err:
        build_report(_spec(), _result(), META)
    assert err.value.code == "report_too_large"
    assert "ZTAB" not in str(err.value)


def test_media_type_and_file_name() -> None:
    assert REPORT_MEDIA_TYPE == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    spec = _spec().model_copy(update={"wave": "W 2"})
    assert report_file_name(spec, META) == "reconcile-ZTAB-W_2-run-0001.xlsx"
