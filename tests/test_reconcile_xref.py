"""Cross-reference sheets as grids (for the model) and as lookups (for the run).

The workbook is synthetic (``tests.reconcile_helpers.xref_book``): a plain code
table, two tables side by side under duplicate labels, a header below title
rows, and a two-key table.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.reconcile import xref_reader
from agents.reconcile.engine import Comparer
from agents.reconcile.spec import ColumnRef, ReconcileSpec, XrefLookup
from agents.reconcile.workbook import Refused, opened
from agents.reconcile.xref_reader import (
    MAX_GRID_CELL_CHARS,
    MAX_GRID_COLUMNS,
    MAX_GRID_ROWS,
    MAX_SHEETS,
    Lookup,
    build_lookup,
    build_lookups,
    list_sheets,
    read_grid,
)
from tests.reconcile_helpers import build_book, xref_book


def _xref(sheet: str, header_row: int, keys: list[dict[str, str]], value: dict[str, str]
          ) -> XrefLookup:
    return XrefLookup(sheet=sheet, header_row=header_row,
                      keys=[ColumnRef(**k) for k in keys], value=ColumnRef(**value))


def _lookup(data: bytes, xref: XrefLookup) -> Lookup:
    with opened(data) as book:
        return build_lookup(book, xref)


# --- list_sheets -------------------------------------------------------------


def test_list_sheets_names_every_sheet_with_its_dimensions() -> None:
    sheets = list_sheets(xref_book())
    assert [s["name"] for s in sheets] == ["Codes", "Groups", "Units", "Multi"]
    for s in sheets:
        assert set(s) == {"name", "max_row", "max_column"}
        assert s["max_row"] is None or isinstance(s["max_row"], int)
        assert s["max_column"] is None or isinstance(s["max_column"], int)
    codes = sheets[0]
    assert (codes["max_row"], codes["max_column"]) in {(6, 3), (None, None)}


def test_list_sheets_stops_at_the_cap() -> None:
    data = build_book({f"S{i}": [["x"]] for i in range(MAX_SHEETS + 5)})
    assert len(list_sheets(data)) == MAX_SHEETS


# --- read_grid ---------------------------------------------------------------


def test_read_grid_returns_letters_row_numbers_and_cells() -> None:
    grid = read_grid(xref_book(), "Groups", first_row=1, max_rows=10, max_columns=8)
    assert grid["sheet"] == "Groups"
    assert grid["first_row"] == 1
    assert grid["columns"] == list("ABCDEFGH")
    assert grid["truncated"] is False
    rows = {r["row"]: r["cells"] for r in grid["rows"]}
    assert 2 not in rows  # blank rows are left out; the row numbers keep the positions
    assert rows[1][0] == "Group tables"
    assert rows[3][:5] == ["Hierarchy", "Group", "", "Group", "Group"]
    assert rows[6][3:5] == ["OLD3", "NEW3"]
    assert all(len(cells) <= 8 for cells in rows.values())


def test_read_grid_window_and_truncation() -> None:
    grid = read_grid(xref_book(), "Groups", first_row=4, max_rows=2, max_columns=5)
    assert [r["row"] for r in grid["rows"]] == [4, 5]
    assert grid["truncated"] is True
    assert grid["columns"] == list("ABCDE")


def test_read_grid_caps_rows_columns_and_cell_length() -> None:
    long = "y" * 500
    rows = [[long, *range(80)], *[[f"r{i}"] for i in range(MAX_GRID_ROWS + 20)]]
    grid = read_grid(build_book({"Big": rows}), "big", first_row=1,
                     max_rows=10_000, max_columns=1000)
    assert grid["sheet"] == "Big"  # the case-insensitive match reports the real name
    assert len(grid["columns"]) == MAX_GRID_COLUMNS
    assert len(grid["rows"]) == MAX_GRID_ROWS
    assert grid["truncated"] is True
    first = grid["rows"][0]["cells"]
    assert len(first) == MAX_GRID_COLUMNS
    assert len(first[0]) == MAX_GRID_CELL_CHARS


@pytest.mark.parametrize("over", [
    {"sheet": 3},
    {"sheet": ""},
    {"first_row": "1"},
    {"first_row": True},
    {"first_row": 0},
    {"max_rows": 0},
    {"max_rows": 2.5},
    {"max_columns": None},
])
def test_read_grid_refuses_bad_arguments(over: dict[str, Any]) -> None:
    args: dict[str, Any] = {"sheet": "Codes", "first_row": 1, "max_rows": 10, "max_columns": 5}
    args.update(over)
    sheet = args.pop("sheet")
    with pytest.raises(Refused) as err:
        read_grid(xref_book(), sheet, **args)
    assert err.value.code == "invalid_arguments"


def test_read_grid_unknown_sheet_is_refused() -> None:
    with pytest.raises(Refused) as err:
        read_grid(xref_book(), "Nope")
    assert err.value.code == "sheet_not_found"


# --- build_lookup ------------------------------------------------------------


def test_letters_pick_the_second_table_of_two_side_by_side() -> None:
    lookup = _lookup(xref_book(), _xref("Groups", 3, [{"letter": "D"}], {"letter": "E"}))
    assert lookup.entries == {("OLD1",): "NEW1", ("OLD2",): "NEW2", ("OLD3",): "NEW3"}
    assert lookup.get(("OLD2",)) == "NEW2"
    assert lookup.get(("H1",)) is None


def test_a_label_that_two_columns_carry_is_ambiguous() -> None:
    with pytest.raises(Refused) as err:
        _lookup(xref_book(), _xref("Groups", 3, [{"label": "Group"}], {"letter": "E"}))
    assert err.value.code == "column_ambiguous"


def test_labels_resolve_on_the_given_header_row() -> None:
    lookup = _lookup(xref_book(), _xref("Groups", 3, [{"label": "hierarchy"}], {"letter": "B"}))
    assert lookup.entries == {("H1",): "G1", ("H2",): "G2"}
    units = _lookup(xref_book(), _xref("Units", 2, [{"label": "Source unit"}],
                                       {"label": "Target unit"}))
    assert units.entries == {("ST",): "PC", ("KG",): "KGM"}


def test_first_match_wins_blank_keys_skipped_and_cells_trimmed() -> None:
    lookup = _lookup(xref_book(), _xref("Codes", 1, [{"letter": "B"}], {"letter": "C"}))
    assert lookup.entries == {("01",): "A", ("X1",): "B", ("7",): "C"}
    assert lookup.duplicates == 1
    assert lookup.get(("01",)) == "A"


def test_two_key_lookup_is_keyed_by_tuples() -> None:
    lookup = _lookup(xref_book(), _xref("Multi", 1, [{"letter": "A"}, {"letter": "B"}],
                                        {"letter": "C"}))
    assert lookup.get(("K1", "01")) == "T1"
    assert lookup.get(("K2", "01")) == "T2"
    assert lookup.get(("K1", "02")) == "T3"
    assert lookup.get(("K2", "02")) is None


def test_reading_stops_after_a_run_of_blank_rows() -> None:
    rows: list[list[Any]] = [["k", "v"], ["a", "1"], *[[] for _ in range(50)], ["b", "2"]]
    lookup = _lookup(build_book({"T": rows}), _xref("T", 1, [{"letter": "A"}], {"letter": "B"}))
    assert lookup.entries == {("a",): "1"}


def test_reading_stops_at_the_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xref_reader, "MAX_LOOKUP_ROWS", 3)
    rows = [["k", "v"], *[[f"k{i}", f"v{i}"] for i in range(10)]]
    lookup = _lookup(build_book({"T": rows}), _xref("T", 1, [{"letter": "A"}], {"letter": "B"}))
    assert list(lookup.entries) == [("k0",), ("k1",), ("k2",)]


def test_max_lookup_rows_default() -> None:
    assert xref_reader.MAX_LOOKUP_ROWS == 100_000


# --- build_lookups -----------------------------------------------------------


def _spec(columns: dict[str, Any]) -> ReconcileSpec:
    return ReconcileSpec.model_validate({
        "table": "ZTAB", "wave": "W1",
        "key": {"csv_column": "OBJNR", "ecc_field": "OBJNR", "length": 10},
        "columns": columns,
    })


_GROUPS = {"sheet": "Groups", "header_row": 3, "keys": [{"letter": "D"}], "value": {"letter": "E"}}
_CODES = {"sheet": "Codes", "header_row": 1, "keys": [{"letter": "B"}], "value": {"letter": "C"}}


def test_build_lookups_keys_by_column_and_index_and_shares_identical_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opens = 0
    real = xref_reader.opened

    def counting(data: bytes) -> Any:
        nonlocal opens
        opens += 1
        return real(data)

    monkeypatch.setattr(xref_reader, "opened", counting)
    spec = _spec({
        "KIND": {"kind": "value_map", "sources": ["KIND"], "lookups": [_CODES, _GROUPS]},
        "GROUP": {"kind": "value_map", "sources": ["GRP"], "lookups": [_GROUPS]},
        "NOTE": {"kind": "direct", "source": "NOTE"},
    })
    lookups = build_lookups(xref_book(), spec)
    assert opens == 1
    assert set(lookups) == {("KIND", 0), ("KIND", 1), ("GROUP", 0)}
    assert lookups[("KIND", 1)] is lookups[("GROUP", 0)]
    assert lookups[("KIND", 0)].get(("X1",)) == "B"
    Comparer(spec, lookups)  # satisfies the engine's LookupTable protocol


def test_build_lookups_without_value_maps_does_not_open_the_workbook() -> None:
    spec = _spec({"NOTE": {"kind": "direct", "source": "N"}})
    assert build_lookups(b"not a workbook", spec) == {}


def test_lookup_entries_keep_workbook_order_for_the_engines_alpha_index() -> None:
    spec = _spec({"KIND": {"kind": "value_map", "sources": ["KIND"], "alpha": True,
                           "lookups": [_CODES]}})
    lookups = build_lookups(xref_book(), spec)
    assert list(lookups[("KIND", 0)].entries) == [("01",), ("X1",), ("7",)]
    comparer = Comparer(spec, lookups)
    comparer.compare("1", 2, {"KIND": "A"}, {"OBJNR": "1", "KIND": "1"})  # "1" ~ "01"
    comparer.compare("2", 3, {"KIND": "C"}, {"OBJNR": "2", "KIND": "0007"})  # "0007" ~ 7
    stats = comparer.result().stats["KIND"]
    assert (stats.checked, stats.matched) == (2, 2)


def test_an_empty_lookup_is_refused_naming_sheet_and_header_row_only() -> None:
    data = xref_book({"Empty": [["Source", "Target"], [None, "secret-value"]]})
    spec = _spec({"KIND": {"kind": "value_map", "sources": ["KIND"], "lookups": [
        {"sheet": "Empty", "header_row": 1, "keys": [{"letter": "A"}], "value": {"letter": "B"}},
    ]}})
    with pytest.raises(Refused) as err:
        build_lookups(data, spec)
    assert err.value.code == "lookup_empty"
    text = f"{err.value.message} {err.value.hint}"
    assert "Empty" in text and "1" in text
    assert "secret-value" not in text and "Source" not in text


def test_a_refused_lookup_names_its_column_and_index() -> None:
    spec = _spec({"KIND": {"kind": "value_map", "sources": ["KIND"], "lookups": [
        _CODES, {**_GROUPS, "keys": [{"label": "Group"}]},
    ]}})
    with pytest.raises(Refused) as err:
        build_lookups(xref_book(), spec)
    assert err.value.code == "column_ambiguous"
    assert "KIND" in (err.value.hint or "") and "lookup 2" in (err.value.hint or "")


def test_hint_names_are_cut_and_lose_control_characters() -> None:
    text = xref_reader._hint_name("a\x00b\nc" + "d" * 200)
    assert "\x00" not in text and "\n" not in text
    assert len(text) <= xref_reader.MAX_HINT_NAME
