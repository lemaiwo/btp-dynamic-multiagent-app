"""Table views and the caps of ``agents/sharepoint_workbook.py``.

The workbook is generated in the test (``tests/sharepoint_helpers.py``);
nothing is read from disk and there is no HTTP here.

Run:  python -m pytest tests/test_sharepoint_workbook.py
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import agents.sharepoint_workbook as workbook  # noqa: E402
from agents.sharepoint_views import TableView, parse_views  # noqa: E402
from agents.sharepoint_workbook import (  # noqa: E402
    Refused,
    check_archive,
    check_window,
    read_calendar,
    read_table,
)
from tests.sharepoint_helpers import VIEWS, build_workbook  # noqa: E402

TEAM_VIEW = parse_views(VIEWS)["team"]


def _refused(call, *args) -> Refused:
    with pytest.raises(Refused) as refused:
        call(*args)
    return refused.value


def test_openpyxl_parses_with_the_defused_parser():
    import openpyxl

    assert openpyxl.DEFUSEDXML is True


def test_a_table_view_returns_the_pinned_columns_only():
    out = read_table(build_workbook(), TEAM_VIEW)
    assert out["columns"] == ["Name", "Team", "ID"]
    assert out["rows"][0] == {"Name": "Ann Example", "Team": "Basis", "ID": 1001}
    assert len(out["rows"]) == 3
    assert all("UserID" not in row for row in out["rows"])
    assert "u-ann" not in repr(out)


def test_table_values_are_plain_and_blank_rows_are_dropped():
    data = build_workbook(team=[("  Ann Example ", "Basis", 1001.0, "u"),
                                (None, None, None, "only-unpinned"),
                                ("Bob Sample", None, "#N/A", "u")])
    rows = read_table(data, TEAM_VIEW)["rows"]
    assert rows == [{"Name": "Ann Example", "Team": "Basis", "ID": 1001},
                    {"Name": "Bob Sample", "Team": None, "ID": None}]


def test_unknown_table_and_missing_column_are_refusals_without_values():
    data = build_workbook()
    missing = _refused(read_table, data, TableView("team", "Absentees", ("Name",)))
    assert missing.code == "table_not_found" and "Absentees" not in missing.message
    column = _refused(read_table, data, TableView("team", "TeamMembers", ("Name", "Salary")))
    assert column.code == "column_not_found" and "Salary" not in column.message


def test_a_table_of_formulas_without_results_is_refused():
    data = build_workbook(team=[("=A1", "=A1", "=1+1", "u")])
    assert _refused(read_table, data, TEAM_VIEW).code == "no_cached_values"


def test_what_is_not_a_workbook_is_refused():
    assert _refused(check_archive, b"not a zip").code == "not_a_workbook"
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("hello.txt", "hello")
    assert _refused(read_table, plain.getvalue(), TEAM_VIEW).code == "not_a_workbook"


def test_size_caps_apply_before_parsing(monkeypatch):
    data = build_workbook()
    monkeypatch.setattr(workbook, "MAX_FILE_BYTES", len(data) - 1)
    assert _refused(check_archive, data).code == "too_large"
    monkeypatch.setattr(workbook, "MAX_FILE_BYTES", len(data))
    check_archive(data)
    monkeypatch.setattr(workbook, "MAX_UNCOMPRESSED_BYTES", 1000)
    assert _refused(read_table, data, TEAM_VIEW).code == "too_large"
    monkeypatch.setattr(workbook, "MAX_UNCOMPRESSED_BYTES", 10**9)
    monkeypatch.setattr(workbook, "MAX_ARCHIVE_MEMBERS", 2)
    assert _refused(check_archive, data).code == "too_large"


def test_a_table_larger_than_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(workbook, "MAX_TABLE_ROWS", 2)
    assert _refused(read_table, build_workbook(), TEAM_VIEW).code == "too_large"


def test_the_window_is_two_iso_dates_of_at_most_120_days():
    assert check_window("2026-01-05", "2026-01-11") == (date(2026, 1, 5), date(2026, 1, 11))
    assert check_window("2026-01-01", "2026-04-30")[1] == date(2026, 4, 30)  # 120 days
    assert _refused(check_window, "2026-01-01", "2026-05-01").code == "window_too_long"
    for bad in (("2026-01-11", "2026-01-05"), ("05/01/2026", "2026-01-11"),
                ("2026-1-5", "2026-01-11"), (None, "2026-01-11"), ("20260105", "20260111")):
        assert _refused(check_window, *bad).code == "invalid_dates"


# --- AMENDMENT 2: bounded text in a table view --------------------------------

CELL_MARK = "zz-planted-cell-text"


@pytest.mark.parametrize("bad", [
    "N" * 256,
    "Ann\tExample " + CELL_MARK,
    "Ann Example " + CELL_MARK,
    "Ann Example " + CELL_MARK,
    "Ann\nExample " + CELL_MARK,
    "Ann\x7fExample " + CELL_MARK,
    "Ann​Example " + CELL_MARK,
])
@pytest.mark.parametrize("column", [0, 1])
def test_a_row_with_text_that_is_not_bounded_one_line_text_is_skipped(bad, column):
    row = ["Row " + CELL_MARK, "Basis", 1009, "u"]
    row[column] = bad
    data = build_workbook(team=[("Ann Example", "Basis", 1001, "u"), tuple(row),
                                ("Bob Sample", "Basis", 1002, "u")])
    out = read_table(data, TEAM_VIEW)
    assert out["skipped_rows"] == 1
    assert [r["Name"] for r in out["rows"]] == ["Ann Example", "Bob Sample"]
    text = json.dumps(out, ensure_ascii=False)
    assert CELL_MARK not in text and "NNNN" not in text and "1009" not in text


def test_bounded_text_numbers_dates_and_booleans_pass_as_before():
    data = build_workbook(team=[("N" * 255, "Basis", 1001, "u"),
                                ("Bob Sample", date(2026, 1, 5), 12.5, "u"),
                                ("Cy Placeholder", True, 0, "u")])
    out = read_table(data, TEAM_VIEW)
    assert out["skipped_rows"] == 0
    assert out["rows"] == [{"Name": "N" * 255, "Team": "Basis", "ID": 1001},
                           {"Name": "Bob Sample", "Team": "2026-01-05", "ID": 12.5},
                           {"Name": "Cy Placeholder", "Team": True, "ID": 0}]


def test_text_in_a_column_that_is_not_pinned_does_not_skip_the_row():
    data = build_workbook(team=[("Ann Example", "Basis", 1001, "U" * 300 + "\tx")])
    out = read_table(data, TEAM_VIEW)
    assert out["skipped_rows"] == 0 and len(out["rows"]) == 1
    assert read_table(build_workbook(), TEAM_VIEW)["skipped_rows"] == 0


# --- final review: what a small file may inflate to, entities, the date form ---

def _declaring(data: bytes, member: str, size: int) -> bytes:
    """``data`` with ``size`` as the uncompressed size its directory declares
    for ``member``. Nothing that large is in the file: the caps are decided
    from the declared sizes, before anything is inflated."""
    out = bytearray(data)
    at = out.find(b"PK\x01\x02")
    while at != -1:
        name_len = int.from_bytes(out[at + 28:at + 30], "little")
        if bytes(out[at + 46:at + 46 + name_len]) == member.encode():
            out[at + 24:at + 28] = size.to_bytes(4, "little")
            return bytes(out)
        at = out.find(b"PK\x01\x02", at + 46)
    raise AssertionError(member)


def _with_member(data: bytes, member: str, change) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        assert member in src.namelist(), member
        for info in src.infolist():
            body = src.read(info.filename)
            dst.writestr(info, change(body) if info.filename == member else body)
    return out.getvalue()


SST = (b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
       b"<si><t>one</t></si></sst>")


def _with_shared_strings(data: bytes, body: bytes = SST) -> bytes:
    """``data`` with a shared strings part, as Excel writes one (the
    generated workbook holds its text inline)."""
    declared = _with_member(data, "[Content_Types].xml", lambda types: types.replace(
        b"</Types>",
        b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.'
        b'openxmlformats-officedocument.spreadsheetml.sharedStrings+xml" /></Types>'))
    out = io.BytesIO(declared)
    with zipfile.ZipFile(out, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/sharedStrings.xml", body)
    return out.getvalue()


def _never(*args, **kwargs):
    raise AssertionError("the workbook was parsed")


CALENDAR_VIEW = parse_views(VIEWS)["planning"]
YEAR = {2026: {("Ann Example", "Presence"): {"2026-01-05": "H"}}}


def test_the_archive_caps_in_total_and_per_part_read_whole():
    assert workbook.MAX_UNCOMPRESSED_BYTES == 64 * 1024 * 1024
    assert workbook.MAX_MEMBER_BYTES == 16 * 1024 * 1024


@pytest.mark.parametrize("member", [
    "xl/styles.xml", "xl/sharedStrings.xml", "xl/workbook.xml", "[Content_Types].xml",
    "xl/tables/table1.xml", "xl/_rels/workbook.xml.rels",
    # Below the sheets' folder, but read whole like every other part.
    "xl/worksheets/_rels/sheet1.xml.rels",
])
def test_a_part_that_is_not_streamed_has_its_own_cap_and_nothing_is_parsed(
        member, monkeypatch):
    data = _declaring(_with_shared_strings(build_workbook(YEAR)), member,
                      workbook.MAX_MEMBER_BYTES + 1)
    monkeypatch.setattr(workbook, "_open", _never)
    monkeypatch.setattr(workbook, "_find_table", _never)
    monkeypatch.setattr(zipfile.ZipFile, "read", _never)
    monkeypatch.setattr(zipfile.ZipFile, "open", _never)
    for refused in (
        _refused(check_archive, data),
        _refused(read_table, data, TEAM_VIEW),
        _refused(read_calendar, data, CALENDAR_VIEW, date(2026, 1, 5), date(2026, 1, 6),
                 TEAM_VIEW),
    ):
        assert refused.code == "too_large"
        assert refused.message == "the workbook is larger than this tool reads"


def test_a_sheet_is_streamed_and_counts_towards_the_total_only(monkeypatch):
    sheet = "xl/worksheets/sheet1.xml"
    check_archive(_declaring(build_workbook(), sheet, workbook.MAX_MEMBER_BYTES + 1))
    check_archive(_declaring(build_workbook(), "xl/styles.xml", workbook.MAX_MEMBER_BYTES))
    data = _declaring(build_workbook(), sheet, workbook.MAX_UNCOMPRESSED_BYTES)
    monkeypatch.setattr(workbook, "_open", _never)
    monkeypatch.setattr(workbook, "_find_table", _never)
    assert _refused(read_table, data, TEAM_VIEW).code == "too_large"


def _entity(body: bytes) -> bytes:
    import re

    root = re.search(rb"<(?![?!])", body).start()
    return body[:root] + b'<!DOCTYPE x [<!ENTITY planted "zz-entity-text">]>' + body[root:]


@pytest.mark.parametrize("member", [
    "xl/workbook.xml", "xl/styles.xml",
    "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml", "xl/tables/table1.xml",
    "xl/_rels/workbook.xml.rels", "xl/worksheets/_rels/sheet1.xml.rels",
    "[Content_Types].xml", "_rels/.rels",
])
def test_an_entity_declaration_in_any_part_is_not_a_workbook(member):
    data = _with_member(build_workbook(YEAR), member, _entity)
    for refused in (
        _refused(read_table, data, TEAM_VIEW),
        _refused(read_calendar, data, CALENDAR_VIEW, date(2026, 1, 5), date(2026, 1, 6),
                 TEAM_VIEW),
    ):
        assert refused.code == "not_a_workbook"
        assert "zz-entity" not in refused.message + (refused.hint or "")


def test_an_entity_declaration_in_the_shared_strings_is_not_a_workbook():
    # The part is read: without the declaration the same workbook is fine.
    plain = _with_shared_strings(build_workbook(YEAR))
    assert read_table(plain, TEAM_VIEW)["rows"]
    data = _with_shared_strings(build_workbook(YEAR), _entity(SST).replace(
        b"<t>one</t>", b"<t>&planted;</t>"))
    for refused in (
        _refused(read_table, data, TEAM_VIEW),
        _refused(read_calendar, data, CALENDAR_VIEW, date(2026, 1, 5), date(2026, 1, 6),
                 TEAM_VIEW),
    ):
        assert refused.code == "not_a_workbook"
        assert "zz-entity" not in refused.message + (refused.hint or "")


@pytest.mark.parametrize("bad", [
    "2026-W01-1", "2026-W011 ", "２０２６-01-05", "2026-01-5 ", " 2026-01-05", "2026/01/05",
    "2026-01-05\n", "+026-01-05",
])
def test_a_date_is_exactly_year_month_day(bad):
    assert _refused(check_window, bad, "2026-12-31").code == "invalid_dates"
    assert _refused(check_window, "2025-12-31", bad).code == "invalid_dates"


def _padded(body: bytes, size: int) -> bytes:
    return body + b"<!--" + b" " * size + b"-->"


def test_a_member_is_never_inflated_beyond_the_size_it_declares():
    """``ZipFile.read`` inflates the whole stream and cuts it to the declared
    size afterwards: 64 MB behind a declared 100 bytes would be in memory."""
    import tracemalloc

    grown = _with_member(build_workbook(), "xl/styles.xml",
                         lambda body: _padded(body, 64 * 1024 * 1024))
    data = _declaring(grown, "xl/styles.xml", 100)
    assert len(data) < 200_000
    check_archive(data)  # what it declares is small
    tracemalloc.start()
    try:
        refused = _refused(read_table, data, TEAM_VIEW)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert refused.code == "not_a_workbook"
    assert peak < 16 * 1024 * 1024


def test_a_part_read_whole_is_capped_wherever_the_package_puts_it():
    """The cap by name is the early refusal; the read itself is capped too. A
    sheet part the workbook calls a chart sheet is read whole by openpyxl."""
    over = workbook.MAX_MEMBER_BYTES + 1
    big = _with_member(build_workbook(YEAR), "xl/worksheets/sheet2.xml",
                       lambda body: _padded(body, over))
    check_archive(big)
    # As a worksheet it is streamed, whatever its size.
    assert read_table(big, TEAM_VIEW)["rows"]
    assert read_calendar(big, CALENDAR_VIEW, date(2026, 1, 5), date(2026, 1, 6),
                         TEAM_VIEW)["runs"]

    def retyped(rels: bytes) -> bytes:
        text = rels.decode()
        at = text.index("sheet2.xml")
        start = text.rindex("<Relationship", 0, at)
        end = text.index(">", at) + 1
        assert "relationships/worksheet" in text[start:end]
        return (text[:start] + text[start:end].replace(
            "relationships/worksheet", "relationships/chartsheet") + text[end:]).encode()

    chart = _with_member(big, "xl/_rels/workbook.xml.rels", retyped)
    check_archive(chart)
    assert _refused(read_table, chart, TEAM_VIEW).code == "too_large"


def test_a_workbook_that_does_not_read_through_the_guarded_archive_is_refused(monkeypatch):
    """As after a change in openpyxl that makes handing over the archive do
    nothing: the reader refuses, it does not read unguarded."""
    import openpyxl.reader.excel as excel

    opened: list = []

    class Changed(excel.ExcelReader):
        def read(self):
            super().read()
            self.wb._archive = zipfile.ZipFile(io.BytesIO(DATA))
            opened.append(self.wb._archive)

    DATA = build_workbook(YEAR)
    monkeypatch.setattr(excel, "ExcelReader", Changed)
    for refused in (
        _refused(read_table, DATA, TEAM_VIEW),
        _refused(read_calendar, DATA, CALENDAR_VIEW, date(2026, 1, 5), date(2026, 1, 6),
                 TEAM_VIEW),
    ):
        assert refused.code == "read_failed"
        assert refused.message == "the workbook could not be read"
    assert opened and all(archive.fp is None for archive in opened)  # closed, not handed on
