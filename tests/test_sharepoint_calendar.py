"""Calendar views of ``agents/sharepoint_workbook.py``.

Covered: runs, the code map and its default-deny, unmapped codes, skipped
rows, the stop marker, the lookup, conflicts, a window over New Year, and the
refusals (no cached values, dates not found, no sheet for a year).

Run:  python -m pytest tests/test_sharepoint_calendar.py
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from agents.sharepoint_views import parse_views  # noqa: E402
from agents.sharepoint_workbook import Refused, read_calendar  # noqa: E402
from tests.sharepoint_helpers import (  # noqa: E402
    VIEWS,
    build_workbook,
    column_letter,
    with_results,
)

_PARSED = parse_views(VIEWS)
PLANNING, TEAM_VIEW = _PARSED["planning"], _PARSED["team"]

WEEK = {
    ("Ann Example", "Presence"): {"2026-01-05": "H", "2026-01-06": "A", "2026-01-07": "I",
                                  "2026-01-08": "½H", "2026-01-09": "T"},
    ("Ann Example", "Guard"): {"2026-01-05": "GDI", "2026-01-06": "GDI", "2026-01-08": "GDI",
                               "2026-01-09": "GDI"},
    ("Bob Sample", "Presence"): {"2026-01-05": "T", "2026-01-06": "X?", "2026-01-07": 3},
    ("Bob Sample", "Guard"): {"2026-01-05": "GDH", "2026-01-06": "GDH", "2026-01-07": "GDH"},
    ("Zed Unlisted", "Guard"): {"2026-01-10": "GDI"},
}


def _read(data, lo="2026-01-05", hi="2026-01-11", view=PLANNING, lookup=TEAM_VIEW):
    return read_calendar(data, view, date.fromisoformat(lo), date.fromisoformat(hi), lookup)


def _runs(out, member, kind):
    return [(r["status"], r["from"], r["to"]) for r in out["runs"]
            if r["member"] == member and r["kind"] == kind]


def test_consecutive_days_with_one_status_are_one_run():
    out = _read(build_workbook({2026: WEEK}))
    assert _runs(out, "Ann Example", "Guard") == [
        ("GDI", "2026-01-05", "2026-01-06"), ("GDI", "2026-01-08", "2026-01-09")]
    assert _runs(out, "Bob Sample", "Guard") == [("GDH", "2026-01-05", "2026-01-07")]
    assert out["sheets"] == ["2026"]


def test_an_absence_reason_is_never_distinguishable():
    out = _read(build_workbook({2026: WEEK}))
    # H, A and I are one run of "unavailable": the reason does not pass.
    assert _runs(out, "Ann Example", "Presence") == [
        ("unavailable", "2026-01-05", "2026-01-07"),
        ("half_day", "2026-01-08", "2026-01-08"),
        ("available", "2026-01-09", "2026-01-09")]
    text = json.dumps(out, ensure_ascii=False)
    assert '"I"' not in text and '"A"' not in text and "½" not in text


def test_a_value_outside_the_code_map_is_counted_and_not_passed_on():
    out = _read(build_workbook({2026: WEEK}))
    assert out["unmapped"] == 2
    assert _runs(out, "Bob Sample", "Presence") == [("available", "2026-01-05", "2026-01-05")]
    assert "X?" not in json.dumps(out)


def test_the_lookup_adds_pinned_columns_and_counts_misses():
    out = _read(build_workbook({2026: WEEK}))
    by_member = {r["member"]: r["ID"] for r in out["runs"]}
    assert by_member == {"Ann Example": 1001, "Bob Sample": 1002, "Zed Unlisted": None}
    assert out["lookup_misses"] == 1


def test_a_name_that_is_in_the_table_twice_is_not_looked_up():
    team = [("Ann Example", "Basis", 1001, "u"), ("Ann Example", "Dev", 1009, "u")]
    out = _read(build_workbook({2026: WEEK}, team=team))
    assert {r["ID"] for r in out["runs"]} == {None}


def test_conflicts_are_guard_days_on_which_the_member_is_not_there():
    out = _read(build_workbook({2026: WEEK}))
    assert out["conflicts"] == [
        {"member": "Ann Example", "from": "2026-01-05", "to": "2026-01-06",
         "status": "GDI", "against": "unavailable"},
        {"member": "Ann Example", "from": "2026-01-08", "to": "2026-01-08",
         "status": "GDI", "against": "half_day"},
    ]


def test_rows_that_cannot_be_used_are_skipped_and_counted():
    extra = {2026: [(None, "Basis", "Guard"), ("#N/A", "Basis", "Presence"),
                    ("Ann Example", "Basis", "Guard")]}
    out = _read(build_workbook({2026: WEEK}, extra_rows=extra))
    assert out["skipped_rows"] == 3
    assert len(_runs(out, "Ann Example", "Guard")) == 2


def test_rows_below_the_stop_marker_are_not_read():
    out = _read(build_workbook({2026: WEEK}), lo="2026-01-01", hi="2026-01-11")
    assert all(r["member"] != "Below the stop marker" for r in out["runs"])
    no_stop = copy.deepcopy(VIEWS)
    del no_stop["planning"]["stop_at"]
    out = _read(build_workbook({2026: WEEK}), lo="2026-01-01", hi="2026-01-11",
                view=parse_views(no_stop)["planning"])
    assert any(r["member"] == "Below the stop marker" for r in out["runs"])


def test_a_window_over_new_year_reads_two_sheets_and_joins_the_run():
    data = build_workbook({
        2026: {("Ann Example", "Guard"): {"2026-12-28": "GDI", "2026-12-29": "GDI",
                                          "2026-12-30": "GDI", "2026-12-31": "GDI"}},
        2027: {("Ann Example", "Guard"): {"2027-01-01": "GDI", "2027-01-02": "GDI",
                                          "2027-01-03": "GDI"}},
    })
    out = _read(data, lo="2026-12-28", hi="2027-01-03")
    assert out["sheets"] == ["2026", "2027"]
    assert _runs(out, "Ann Example", "Guard") == [("GDI", "2026-12-28", "2027-01-03")]


def test_a_year_without_a_sheet_is_refused():
    with pytest.raises(Refused) as refused:
        _read(build_workbook({2026: WEEK}), lo="2026-12-28", hi="2027-01-03")
    assert refused.value.code == "sheet_not_found"


def test_formulas_without_stored_results_are_refused_never_guessed():
    data = build_workbook({2026: WEEK}, date_formulas=True)
    with pytest.raises(Refused) as refused:
        _read(data)
    assert refused.value.code == "no_cached_values"


def test_date_formulas_with_stored_results_are_read():
    data = build_workbook({2026: WEEK}, date_formulas=True)
    # Excel's serial numbers of 5 and 6 January 2026.
    data = with_results(data, "2026", {column_letter(date(2026, 1, 5)) + "8": 46027,
                                       column_letter(date(2026, 1, 6)) + "8": 46028})
    out = _read(data, hi="2026-01-06")
    assert _runs(out, "Ann Example", "Guard") == [("GDI", "2026-01-05", "2026-01-06")]
    # A day whose date formula has no result is not guessed from its column.
    with pytest.raises(Refused) as refused:
        _read(data, hi="2026-01-07")
    assert refused.value.code == "no_cached_values"


def test_a_date_row_that_holds_no_dates_is_layout_drift():
    moved = copy.deepcopy(VIEWS)
    moved["planning"].update(date_row=7, first_row=10)
    with pytest.raises(Refused) as refused:
        _read(build_workbook({2026: WEEK}), view=parse_views(moved)["planning"])
    assert refused.value.code == "dates_not_found"
