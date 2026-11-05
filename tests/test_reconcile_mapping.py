"""Workbook helpers and the generic mapping reader (``agents.reconcile``).

The mapping layout (sheet, header row, column labels) is a tool argument, so
these tests drive it with a synthetic sheet whose labels are written the way
hand-kept sheets are: newlines, no-break spaces, double spaces, odd case.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.reconcile import mapping_reader, workbook
from agents.reconcile.mapping_reader import MAX_MAPPING_ROWS, read_mapping
from agents.reconcile.spec import ColumnRef
from agents.reconcile.workbook import (
    MAX_CELL_CHARS,
    Refused,
    cell_text,
    find_header_row,
    header,
    norm_label,
    opened,
    resolve_column,
)
from tests.reconcile_helpers import MAPPING_HEADER, build_book, mapping_book

COLUMNS = [
    ColumnRef(label="Target field"),
    ColumnRef(label="Rule type W1 go live"),
    ColumnRef(label="w1 go live transformation"),
]


def _read(data: bytes | None = None, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "sheet": "Fields",
        "header_row": None,
        "filter_column": ColumnRef(label="Target table"),
        "filter_value": "ztab",
        "columns": COLUMNS,
    }
    args.update(over)
    return read_mapping(mapping_book() if data is None else data, **args)


def _refused(code: str, fn: Any, *args: Any, **kwargs: Any) -> Refused:
    with pytest.raises(Refused) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


# --- labels and columns ---------------------------------------------------

def test_norm_label_folds_whitespace_nbsp_and_case() -> None:
    assert norm_label("W1 go live\ntransformation\xa0rule") == "w1 go live transformation rule"
    assert norm_label("  Target\tTABLE ") == "target table"
    assert norm_label(None) == ""
    assert norm_label(12) == "12"


def test_header_keeps_the_labels_as_written_but_collapses_whitespace() -> None:
    with opened(mapping_book()) as book:
        labels = header(book["Fields"], 4)
    assert labels[0] == "Target table"
    assert labels[6] == "W1 go live transformation rule from source"
    assert labels[8] == "W2 go live transformation rule"
    assert len(labels) == len(MAPPING_HEADER)


def test_resolve_column_by_unique_prefix() -> None:
    labels = [norm_label(x) for x in MAPPING_HEADER]
    assert resolve_column(labels, ColumnRef(label="w1 go live transformation")) == 6
    assert resolve_column(MAPPING_HEADER, ColumnRef(label="W2 GO LIVE")) == 8


def test_an_exact_label_wins_over_prefixes() -> None:
    assert resolve_column(["Rule type", "Rule"], ColumnRef(label="rule")) == 1
    assert resolve_column(MAPPING_HEADER, ColumnRef(label="Target table")) == 0


def test_two_prefix_matches_are_ambiguous() -> None:
    err = _refused("column_ambiguous", resolve_column, MAPPING_HEADER,
                   ColumnRef(label="Rule type"))
    assert "Rule type" not in err.message


def test_no_match_is_column_not_found() -> None:
    _refused("column_not_found", resolve_column, MAPPING_HEADER, ColumnRef(label="Owner"))


def test_a_duplicated_exact_label_is_ambiguous() -> None:
    _refused("column_ambiguous", resolve_column, ["Key", "Key"], ColumnRef(label="key"))


def test_a_letter_is_its_index_whatever_the_labels() -> None:
    assert resolve_column(MAPPING_HEADER, ColumnRef(letter="C")) == 2
    assert resolve_column([], ColumnRef(letter="AA")) == 26


def test_a_letter_beyond_the_column_cap_is_refused() -> None:
    _refused("column_not_found", resolve_column, [], ColumnRef(letter="ZZZ"))


def test_cell_text_cuts_long_text_and_blanks_errors() -> None:
    class _Cell:
        def __init__(self, value: Any, data_type: str = "s") -> None:
            self.value, self.data_type = value, data_type

    assert cell_text(_Cell(None)) == ""
    assert cell_text(_Cell("#N/A", "e")) == ""
    assert cell_text(_Cell(3.0, "n")) == "3"
    assert cell_text(_Cell("  x  ")) == "x"
    long = cell_text(_Cell("y" * 5000))
    assert len(long) == MAX_CELL_CHARS and long.endswith("…")
    assert cell_text(_Cell("abcdef"), limit=4) == "abc…"


# --- header row -----------------------------------------------------------

def test_find_header_row_finds_row_four() -> None:
    refs = [ColumnRef(label="Target table"), ColumnRef(label="Target field")]
    with opened(mapping_book()) as book:
        assert find_header_row(book["Fields"], refs) == 4


def test_find_header_row_scans_a_bounded_number_of_rows() -> None:
    rows: list[list[Any]] = [[f"note {i}"] for i in range(60)] + [list(MAPPING_HEADER)]
    refs = [ColumnRef(label="Target table")]
    with opened(build_book({"S": rows})) as book:
        _refused("header_not_found", find_header_row, book["S"], refs)
        assert find_header_row(book["S"], refs, max_scan=61) == 61


def test_find_header_row_needs_a_label() -> None:
    with opened(mapping_book()) as book:
        _refused("header_not_found", find_header_row, book["Fields"],
                 [ColumnRef(letter="A")])


# --- read_mapping ---------------------------------------------------------

def test_read_mapping_returns_the_filtered_rows_in_column_order() -> None:
    out = _read()
    assert out["header_row"] == 4
    assert out["columns"] == [
        "Target field", "Rule type W1 go live", "W1 go live transformation rule from source",
    ]
    assert out["rows"] == [
        {"row": 5, "values": ["OBJNR", "Direct", "1:1"]},
        {"row": 7, "values": ["KIND", "Value map", "Use the code table"]},
        {"row": 9, "values": ["NOTE", "Not needed", ""]},
    ]
    assert out["truncated"] is False


def test_read_mapping_with_a_given_header_row_and_letters() -> None:
    out = _read(header_row=4, filter_column=ColumnRef(letter="A"), filter_value="ZOTHER",
                columns=[ColumnRef(letter="B"), ColumnRef(label="Rule type W2")])
    assert [r["values"] for r in out["rows"]] == [["KEY1", "Direct"], ["KEY2", "Constant"]]
    assert out["columns"] == ["Target field", "Rule type W2 go live"]


def test_read_mapping_without_a_filter_returns_every_row_with_data() -> None:
    out = _read(filter_column=None, filter_value=None)
    assert [r["row"] for r in out["rows"]] == [5, 6, 7, 9, 10]


def test_read_mapping_accepts_column_refs_as_dicts() -> None:
    out = _read(filter_column={"label": "Target table"},
                columns=[{"label": "Target field"}])
    assert [r["values"] for r in out["rows"]] == [["OBJNR"], ["KIND"], ["NOTE"]]


def test_long_cells_are_cut() -> None:
    data = mapping_book(extra=[["ZTAB", "LONG", None, None, "Y", "Expression", "z" * 3000]])
    row = _read(data)["rows"][-1]
    assert row["values"][2].endswith("…") and len(row["values"][2]) == MAX_CELL_CHARS


def test_reading_stops_after_fifty_blank_rows() -> None:
    gap: list[list[Any]] = [[] for _ in range(50)]
    data = mapping_book(extra=[*gap, ["ZTAB", "AFTER", None, None, "Y", "Direct", "1:1"]])
    assert "AFTER" not in [r["values"][0] for r in _read(data)["rows"]]
    data = mapping_book(extra=[*gap[:49], ["ZTAB", "AFTER", None, None, "Y", "Direct", "1:1"]])
    assert "AFTER" in [r["values"][0] for r in _read(data)["rows"]]


def test_more_matching_rows_than_the_cap_are_truncated() -> None:
    extra = [["ZTAB", f"F{i}", None, None, "Y", "Direct", "1:1"]
             for i in range(MAX_MAPPING_ROWS + 1)]
    out = _read(mapping_book(extra=extra), columns=[ColumnRef(label="Target field")])
    assert out["truncated"] is True
    assert len(out["rows"]) == MAX_MAPPING_ROWS


def test_the_sheet_name_is_matched_case_insensitively() -> None:
    assert _read(sheet="fields")["sheet"] == "Fields"


def test_an_unknown_sheet_lists_sheet_names_but_no_cell_values() -> None:
    err = _refused("sheet_not_found", _read, sheet="Nope")
    text = f"{err.message} {err.hint}"
    assert "Fields" in text and "Codes" in text
    for value in ("ZTAB", "OBJNR", "Target table", "Source value"):
        assert value not in text


def test_the_sheet_list_in_the_hint_is_capped() -> None:
    data = build_book({f"S{i}": [["x"]] for i in range(130)})
    err = _refused("sheet_not_found", read_mapping, data, sheet="Nope", header_row=1,
                   filter_column=None, filter_value=None, columns=[ColumnRef(letter="A")])
    assert "S99" in (err.hint or "") and "S100" not in (err.hint or "")


def test_not_a_workbook() -> None:
    _refused("not_a_workbook", _read, b"plain text, not a zip")


def test_a_missing_column_is_refused_without_its_label() -> None:
    err = _refused("column_not_found", _read, header_row=4,
                   columns=[ColumnRef(label="Owner of record")])
    assert "Owner" not in err.message


@pytest.mark.parametrize("over", [
    {"sheet": 7},
    {"sheet": ""},
    {"header_row": "4"},
    {"header_row": True},
    {"header_row": 0},
    {"filter_value": 5},
    {"filter_column": None},
    {"filter_value": None},
    {"columns": []},
    {"columns": [{"letter": "A", "label": "x"}]},
    {"columns": "Target field"},
    {"filter_value": "x" * 256},
    {"columns": [{"letter": "A"}] * 201},
    {"columns": [{"label": "   "}]},
    {"columns": [{"label": "\n\xa0"}]},
])
def test_bad_arguments_are_refused(over: dict[str, Any]) -> None:
    _refused("invalid_arguments", _read, **over)


def test_modules_hold_no_customer_layout() -> None:
    import inspect

    for module in (workbook, mapping_reader):
        source = inspect.getsource(module)
        for word in ("Fields", "Target table", "ZTAB"):
            assert word not in source


# --- final review hygiene -------------------------------------------------

class _Book:
    def __init__(self, names: list[str]) -> None:
        self.sheetnames = names
        self.closed = 0

    def __getitem__(self, name: str) -> Any:
        return name

    def close(self) -> None:
        self.closed += 1


def test_the_sheet_hint_cuts_each_name_and_strips_control_characters() -> None:
    long_name = "N" * 40
    err = _refused("sheet_not_found", workbook.sheet, _Book(["Ok\nname\u202e", long_name]), "x")
    hint = err.hint or ""
    assert "\n" not in hint and "\u202e" not in hint
    assert "Okname" in hint
    assert long_name not in hint and "N" * 30 + "…" in hint


def test_xref_reader_reuses_the_shared_hint_name() -> None:
    from agents.reconcile import xref_reader

    assert xref_reader._hint_name("a\nb") == workbook.hint_name("a\nb") == "ab"


def test_error_cells_have_a_public_marker() -> None:
    from agents import sharepoint_workbook

    assert sharepoint_workbook.ERROR_CELL is sharepoint_workbook._ERROR
    assert workbook.ERROR_CELL is sharepoint_workbook.ERROR_CELL


@pytest.mark.parametrize("fail", [False, True])
def test_opened_closes_the_book_on_exit_and_on_an_exception(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    book = _Book(["S"])
    monkeypatch.setattr(workbook, "check_archive", lambda data: None)
    monkeypatch.setattr(workbook, "open_workbook", lambda data: book)
    with pytest.raises(RuntimeError) if fail else _nothing():
        with opened(b"x"):
            if fail:
                raise RuntimeError("boom")
    assert book.closed == 1


class _nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return False


def test_an_ambiguous_label_in_read_mapping_is_refused() -> None:
    _refused("column_ambiguous", _read, header_row=4, columns=[ColumnRef(label="Rule type")])


def test_a_letter_beyond_the_header_reads_as_a_blank_column() -> None:
    out = _read(header_row=4, columns=[ColumnRef(label="Target field"), ColumnRef(letter="Z")])
    assert out["columns"] == ["Target field", ""]
    assert [r["values"] for r in out["rows"]] == [["OBJNR", ""], ["KIND", ""], ["NOTE", ""]]


def test_a_matching_row_blank_in_every_requested_column_is_left_out() -> None:
    data = mapping_book(extra=[["ZTAB", None, None, None, "Y"]])
    out = _read(data, columns=[ColumnRef(label="Target field")])
    assert 11 not in [r["row"] for r in out["rows"]]


def test_non_matching_rows_are_scanned_up_to_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    extra = [["ZOTHER", f"F{i}"] for i in range(20)] + [["ZTAB", "LATE"]]
    data = mapping_book(extra=extra)
    columns = [ColumnRef(label="Target field")]
    out = _read(data, columns=columns)
    assert out["truncated"] is False and "LATE" in [r["values"][0] for r in out["rows"]]
    monkeypatch.setattr(mapping_reader, "MAX_MAPPING_SCAN", 10)
    out = _read(data, columns=columns)
    assert out["truncated"] is True
    assert [r["values"][0] for r in out["rows"]] == ["OBJNR", "KIND", "NOTE"]
