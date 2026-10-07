"""Table views and the caps of ``agents/sharepoint_workbook.py``.

The workbook is generated in the test (``tests/sharepoint_helpers.py``);
nothing is read from disk and there is no HTTP here.

Run:  python -m pytest tests/test_sharepoint_workbook.py
"""

from __future__ import annotations

import io
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
    read_table,
)
from tests.sharepoint_helpers import VIEWS, build_workbook  # noqa: E402

TEAM_VIEW = parse_views(VIEWS)["team"]


def _refused(call, *args) -> Refused:
    with pytest.raises(Refused) as refused:
        call(*args)
    return refused.value


def test_openpyxl_parses_without_dtds():
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
