"""The grid reader of a run (``agents/ina/grid.py``): SIA's Version2 answer.

``tests/fixtures/ina/run_sia_grid.json`` is the grid SIA (S/4HANA 2022,
SAP_BW 757) answered on 2026-11-04 (run 8: two row dimensions, each asked
for its key and text attribute, totals hidden, ``RowTo`` 4), with every
member key, text, amount and count replaced by synthetic ones and the
session and instance ids removed; the structure and SAP's metadata names are
as recorded. ``submit_sia.json`` is the VariableSubmit answer of that query,
cut to what the client reads of it (each dimension's key and text
attribute).

Covered: the keys, texts, values and unit of the recorded grid; nothing that
was not asked for is passed on; ``truncated`` from ``TupleCountTotal``;
every form the reader does not know (a total member, a key that is no string
or carries an exception, axes or tuples that do not match the request, sizes
that do not add up, a cell without a plain value) is ``sap_error``, never a
guess and never null; the caps are ``result_too_large``, never a cut list;
and no error text holds a value of the answer.

Run:  python -m pytest tests/test_ina_grid.py
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ina import grid as ina_grid  # noqa: E402
from agents.ina import protocol  # noqa: E402
from agents.ina.errors import InaError  # noqa: E402
from agents.ina.models import InaDefinition  # noqa: E402
from agents.ina.protocol import (  # noqa: E402
    MAX_CELLS,
    MAX_TOP,
    RunPlan,
    plan_run,
    read_description,
    read_grid,
    text_attributes,
)
from tests.test_ina_client import definition, fixture, grid  # noqa: E402

SIA_VALUES = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}
SIA_ROWS = ["2CIMMPURGORG", "2CI_SUPPLIER_CDS"]
# Values of the fixture's data: none may ever appear in an error text.
DATA = ("P100", "Purchasing organisation", "0000000101", "Supplier", "125.5", "2400", "310.25")


def sia_plan(top: int = 3, group_by: list[str] | None = None, *, texts: bool = True) -> RunPlan:
    """The plan of run 8, the text attributes taken from the submit answer."""
    described = read_description(fixture("describe_sia.json")["Cube"]).definition
    plan = plan_run(
        described,
        measures=["ELTUIDCMP1"],
        group_by=list(SIA_ROWS if group_by is None else group_by),
        filters=None,
        top=top,
        order=None,
        variables=dict(SIA_VALUES),
    )
    if texts:
        submitted = fixture("submit_sia.json")["Cube"]
        plan = replace(plan, texts=text_attributes(submitted, plan.group_by))
    return plan


def the_grid(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["Grids"][0]


def rows_axis(payload: dict[str, Any]) -> dict[str, Any]:
    return the_grid(payload)["Axes"][0]


def columns_axis(payload: dict[str, Any]) -> dict[str, Any]:
    return the_grid(payload)["Axes"][1]


def key_attribute(payload: dict[str, Any], dimension: int = 1) -> dict[str, Any]:
    return rows_axis(payload)["Dimensions"][dimension]["Attributes"][0]


def text_attribute(payload: dict[str, Any], dimension: int = 1) -> dict[str, Any]:
    return rows_axis(payload)["Dimensions"][dimension]["Attributes"][1]


def cells(payload: dict[str, Any]) -> dict[str, Any]:
    return the_grid(payload)["Cells"]


def refused(payload: Any, plan: RunPlan) -> InaError:
    with pytest.raises(InaError) as info:
        read_grid(payload, plan)
    return info.value


def few(measures: int = 1, dimensions: int = 1) -> InaDefinition:
    """A definition of plain names and no variables, for the cap cases."""
    return InaDefinition.model_validate({
        "dimensions": [{"name": f"D{i}"} for i in range(dimensions)],
        "measures": [{"name": f"M{i}"} for i in range(measures)],
    })


def few_plan(measures: int, top: int, group_by: tuple[str, ...] = ("D0",)) -> RunPlan:
    """``plan_run`` refuses more than one measure per run until the column
    order is verified; the reader is still checked on several, so a plan of
    more is made from one of one."""
    plan = plan_run(few(measures), measures=["M0"], group_by=list(group_by), filters=None,
                    top=top, order=None, variables=None)
    return replace(plan, measures=[f"M{i}" for i in range(measures)])


# ------------------------------------------------------------------ the recorded grid


def test_the_recorded_grid_reads_keys_texts_values_and_the_unit():
    assert read_grid(fixture("run_sia_grid.json"), sia_plan()) == {
        "columns": [
            "2CIMMPURGORG",
            "2CIMMPURGORG.text",
            "2CI_SUPPLIER_CDS",
            "2CI_SUPPLIER_CDS.text",
            "ELTUIDCMP1",
        ],
        "rows": [
            ["P100", "Purchasing organisation 100", "0000000101", "Supplier A", 125.5],
            ["P100", "Purchasing organisation 100", "0000000102", "Supplier B", 2400.0],
            ["P100", "Purchasing organisation 100", "0000000103", "Supplier C", 310.25],
        ],
        "units": {"ELTUIDCMP1": "EUR"},
        "row_count": 3,
        # RowTo was top + 1 = 4; TupleCountTotal says 57 rows exist.
        "truncated": True,
    }


def test_the_protocols_read_grid_is_the_grid_reader():
    assert protocol.read_grid is ina_grid.read_grid


@pytest.mark.parametrize(
    ("top", "total", "row_count", "truncated"),
    [
        (4, 57, 4, True),
        (4, 4, 4, False),
        (4, None, 4, False),
        (3, None, 3, True),
        (3, 4, 3, True),
    ],
)
def test_truncated_is_what_tuple_count_total_says(top, total, row_count, truncated):
    payload = fixture("run_sia_grid.json")
    if total is None:
        del rows_axis(payload)["TupleCountTotal"]
    else:
        rows_axis(payload)["TupleCountTotal"] = total
    out = read_grid(payload, sia_plan(top=top))
    assert (out["row_count"], out["truncated"]) == (row_count, truncated)


def test_a_text_that_was_not_asked_for_is_not_passed_on():
    """Without a text attribute from the submit answer the read asks for the
    key alone; a text SAP sends anyway stays out, and so does every other
    attribute and everything outside the axes and the cell values."""
    payload = fixture("run_sia_grid.json")
    rows_axis(payload)["Dimensions"][0]["Attributes"].append(
        {"Name": "2CIMMPURGORG.EXTRA", "IsKey": False, "Values": ["not asked for"]}
    )
    out = read_grid(payload, sia_plan(texts=False))
    assert out["columns"] == ["2CIMMPURGORG", "2CI_SUPPLIER_CDS", "ELTUIDCMP1"]
    assert out["rows"][0] == ["P100", "0000000101", 125.5]
    shown = json.dumps(out)
    for absent in ("Supplier A", "Purchasing", "not asked for", "125,50", "Analyse"):
        assert absent not in shown


def test_a_text_equal_to_its_key_gets_no_text_column():
    payload = fixture("run_sia_grid.json")
    text_attribute(payload, 0)["Values"] = list(key_attribute(payload, 0)["Values"])
    assert read_grid(payload, sia_plan())["columns"] == [
        "2CIMMPURGORG", "2CI_SUPPLIER_CDS", "2CI_SUPPLIER_CDS.text", "ELTUIDCMP1"]


def test_a_text_attribute_sap_left_out_is_no_text_column():
    payload = fixture("run_sia_grid.json")
    del rows_axis(payload)["Dimensions"][1]["Attributes"][1]
    assert read_grid(payload, sia_plan())["columns"] == [
        "2CIMMPURGORG", "2CIMMPURGORG.text", "2CI_SUPPLIER_CDS", "ELTUIDCMP1"]


def test_keys_and_texts_are_cleaned_and_texts_cut():
    payload = fixture("run_sia_grid.json")
    key_attribute(payload)["Values"][0] = "0000‮000101\x07"
    text_attribute(payload)["Values"][0] = "Supplier  A" + "x" * 300
    (row, *_) = read_grid(payload, sia_plan())["rows"]
    assert row[2] == "0000000101"
    assert row[3] == ("Supplier A" + "x" * 300)[:120]


def test_a_unit_that_differs_per_row_is_a_unit_column():
    payload = fixture("run_sia_grid.json")
    cells(payload)["Units"]["Values"][1] = "USD"
    out = read_grid(payload, sia_plan(texts=False))
    assert out["columns"] == ["2CIMMPURGORG", "2CI_SUPPLIER_CDS", "ELTUIDCMP1", "ELTUIDCMP1.unit"]
    assert [row[-2:] for row in out["rows"]] == [[125.5, "EUR"], [2400.0, "USD"], [310.25, "EUR"]]
    assert out["units"] == {}


def test_cells_without_units_have_none():
    payload = fixture("run_sia_grid.json")
    del cells(payload)["Units"]
    out = read_grid(payload, sia_plan())
    assert out["units"] == {} and out["columns"][-1] == "ELTUIDCMP1"


def test_without_group_by_the_one_row_is_the_total():
    """Run 5: no row dimension; ``ROWS`` has no dimensions and one tuple."""
    payload = grid([], [[]], ["ELTUIDCMP1"], [{"Value": 42.5, "Unit": "EUR"}],
                   structure="ELTUIDSTRCMMPURORDQUERY")
    out = read_grid(payload, sia_plan(top=1, group_by=[]))
    assert out == {"columns": ["ELTUIDCMP1"], "rows": [[42.5]], "units": {"ELTUIDCMP1": "EUR"},
                   "row_count": 1, "truncated": False}


def test_an_empty_result_is_no_rows():
    out = read_grid(grid(["D0"], [], ["M0"], []), few_plan(1, top=10))
    assert out == {"columns": ["D0", "M0"], "rows": [], "units": {}, "row_count": 0,
                   "truncated": False}


def test_an_older_definition_reads_the_measures_structure():
    """A row described before 2.37.1 holds no structure name: the request
    names ``Measures``, and so must the answer."""
    plan = few_plan(1, top=5)
    assert plan.structure == "Measures"
    out = read_grid(grid(["D0"], [["A"]], ["M0"], [{"Value": 1}]), plan)
    assert out["rows"] == [["A", 1]]


def test_the_columns_follow_the_member_indexes_of_the_measures():
    """The measure structure carries no attributes: a column's member index
    is the measure's place in the request (seen for one measure on SIA; a
    run of several is refused by ``plan_run`` until the order is verified,
    so the plan is made by hand)."""
    payload = grid(["0CALMONTH"], [["202601"]], ["0AMOUNT", "0QUANTITY"],
                   [{"Value": 10, "Unit": "PC"}, {"Value": 1200.5, "Unit": "EUR"}])
    columns_axis(payload)["Tuples"][0]["MemberIndexes"]["Values"] = [1, 0]
    plan = plan_run(definition(), measures=["0AMOUNT"], group_by=["0CALMONTH"],
                    filters=None, top=5, order=None, variables={"0P_CALYEAR": "2026"})
    plan = replace(plan, measures=["0AMOUNT", "0QUANTITY"])
    out = read_grid(payload, plan)
    assert out["rows"] == [["202601", 1200.5, 10]]
    assert out["units"] == {"0AMOUNT": "EUR", "0QUANTITY": "PC"}


# --------------------------------------------------------------- what is refused


def _set(path: Callable[[dict], Any], key: Any, value: Any) -> Callable[[dict], None]:
    return lambda g: path(g).__setitem__(key, value)


def _del(path: Callable[[dict], Any], key: Any) -> Callable[[dict], None]:
    return lambda g: path(g).__delitem__(key)


def _mi(axis: Callable[[dict], Any], tuple_index: int = 0) -> Callable[[dict], Any]:
    return lambda g: axis(g)["Tuples"][tuple_index]["MemberIndexes"]


SUPPLIER = lambda g: rows_axis(g)["Dimensions"][1]  # noqa: E731
KEY = lambda g: key_attribute(g)  # noqa: E731
TEXT = lambda g: text_attribute(g)  # noqa: E731
VALUES = lambda g: cells(g)["Values"]  # noqa: E731

MANGLES: dict[str, Callable[[dict], None]] = {
    "no grids": _del(lambda g: g, "Grids"),
    "empty grids": _set(lambda g: g, "Grids", []),
    "two grids": lambda g: g["Grids"].append(copy.deepcopy(g["Grids"][0])),
    "a grid that is no object": _set(lambda g: g["Grids"], 0, "grid"),
    "one axis": lambda g: the_grid(g)["Axes"].pop(),
    "a third axis": lambda g: the_grid(g)["Axes"].append(copy.deepcopy(rows_axis(g))),
    "an axis of another type": _set(rows_axis, "Type", "Free"),
    "a row dimension missing": lambda g: rows_axis(g)["Dimensions"].pop(),
    "another row dimension": _set(lambda g: rows_axis(g)["Dimensions"][0], "Name", "2CIPLANT"),
    "a row dimension twice": lambda g: rows_axis(g)["Dimensions"].__setitem__(
        1, copy.deepcopy(rows_axis(g)["Dimensions"][0])),
    "a tuple list missing": lambda g: rows_axis(g)["Tuples"].pop(),
    "tuple count that is no number": _set(rows_axis, "TupleCount", True),
    "a negative tuple count": _set(rows_axis, "TupleCount", -1),
    "a total below the count": _set(rows_axis, "TupleCountTotal", 2),
    "a total that is text": _set(rows_axis, "TupleCountTotal", "57"),
    "fewer member indexes than tuples": lambda g: _mi(rows_axis, 1)(g)["Values"].pop(),
    "a member index out of range": lambda g: _mi(rows_axis, 1)(g)["Values"].__setitem__(0, 9),
    "a negative member index": lambda g: _mi(rows_axis, 1)(g)["Values"].__setitem__(0, -1),
    "a member index that is text": lambda g: _mi(rows_axis, 1)(g)["Values"].__setitem__(0, "0"),
    "a member index that is a boolean": lambda g: _mi(rows_axis, 0)(g)["Values"].__setitem__(
        0, False),
    "an encoded list": _set(_mi(rows_axis, 1), "Encoding", "Delta"),
    "a size that does not match": _set(_mi(rows_axis, 1), "Size", 3),
    "no key attribute": lambda g: SUPPLIER(g)["Attributes"].pop(0),
    "a key that is no string": lambda g: KEY(g)["Values"].__setitem__(0, 101),
    "a key with an exception": lambda g: KEY(g)["ValuesException"].__setitem__(0, 1),
    "a key without its exceptions": _del(KEY, "ValuesException"),
    "fewer key exceptions than keys": lambda g: KEY(g)["ValuesException"].pop(),
    "a text that is no string": lambda g: TEXT(g)["Values"].__setitem__(0, 5),
    "fewer texts than keys": lambda g: TEXT(g)["Values"].pop(),
    "an attribute twice": lambda g: SUPPLIER(g)["Attributes"].append(copy.deepcopy(KEY(g))),
    "a total member": lambda g: SUPPLIER(g)["MemberTypes"]["Values"].__setitem__(2, 1),
    "a member of another type": lambda g: SUPPLIER(g)["MemberTypes"]["Values"].__setitem__(0, 2),
    "no member types": _del(SUPPLIER, "MemberTypes"),
    "another measure structure": _set(lambda g: columns_axis(g)["Dimensions"][0], "Name",
                                      "Measures"),
    "a structure with attributes": _set(lambda g: columns_axis(g)["Dimensions"][0], "Attributes",
                                        [{"Name": "ELTUIDSTRCMMPURORDQUERY.KEY",
                                          "Values": ["ELTUIDCMP1"]}]),
    "a total column": lambda g: columns_axis(g)["Dimensions"][0]["MemberTypes"][
        "Values"].__setitem__(0, 1),
    "more columns than measures": _set(columns_axis, "TupleCount", 2),
    "a column of no requested measure": lambda g: _mi(columns_axis)(g)["Values"].__setitem__(0, 1),
    "no cell sizes": _del(the_grid, "CellArraySizes"),
    "cell sizes that do not match": _set(the_grid, "CellArraySizes", [4, 2]),
    "cell sizes of other rows": _set(the_grid, "CellArraySizes", [3, 1]),
    "a cell missing": lambda g: VALUES(g)["Values"].pop(),
    "no cell values": _del(cells, "Values"),
    "a null cell": lambda g: VALUES(g)["Values"].__setitem__(0, None),
    "a cell that is text": lambda g: VALUES(g)["Values"].__setitem__(0, "125.5"),
    "a cell that is a boolean": lambda g: VALUES(g)["Values"].__setitem__(0, True),
    "a cell that is not a number": lambda g: VALUES(g)["Values"].__setitem__(0, float("nan")),
    "an infinite cell": lambda g: VALUES(g)["Values"].__setitem__(0, float("inf")),
    "a cell with an exception": lambda g: cells(g).__setitem__(
        "Exceptions", {"Encoding": "None", "Size": 4, "Values": [0, 1, 0, 0]}),
    "a unit that is no string": lambda g: cells(g)["Units"]["Values"].__setitem__(0, 5),
    "fewer units than cells": lambda g: cells(g)["Units"]["Values"].pop(),
}


@pytest.mark.parametrize("mangle", list(MANGLES), ids=list(MANGLES))
def test_a_grid_this_reader_does_not_know_is_sap_error(mangle):
    payload = fixture("run_sia_grid.json")
    MANGLES[mangle](payload)
    error = refused(payload, sia_plan())
    assert error.code == "sap_error"
    shown = json.dumps(error.to_dict())
    for value in DATA:
        assert value not in shown


@pytest.mark.parametrize("payload", [None, [], "x", {"Grids": "x"}, {"Grids": [{}]}])
def test_an_answer_without_a_grid_is_sap_error(payload):
    assert refused(payload, sia_plan()).code == "sap_error"


def test_more_rows_than_top_plus_one_is_result_too_large():
    error = refused(fixture("run_sia_grid.json"), sia_plan(top=2))
    assert error.code == "result_too_large"


def test_a_member_table_longer_than_the_rows_asked_for_is_sap_error():
    payload = fixture("run_sia_grid.json")
    key_attribute(payload)["Values"] += ["X"] * 4
    key_attribute(payload)["ValuesException"] += [0] * 4
    text_attribute(payload)["Values"] += ["X"] * 4
    rows_axis(payload)["Dimensions"][1]["MemberTypes"]["Values"] += [0] * 4
    rows_axis(payload)["Dimensions"][1]["MemberTypes"]["Size"] = 8
    assert refused(payload, sia_plan()).code == "sap_error"


def test_a_grid_of_more_than_max_cells_is_result_too_large_never_cut():
    rows = [[f"R{i}"] for i in range(MAX_CELLS // 3 + 1)]
    payload = grid(["D0"], rows, ["M0", "M1", "M2"],
                   [{"Value": i} for i in range(len(rows) * 3)])
    assert refused(payload, few_plan(3, top=MAX_TOP)).code == "result_too_large"


def test_a_grid_of_exactly_max_cells_is_read():
    rows = [[f"R{i}"] for i in range(MAX_CELLS // 5)]
    payload = grid(["D0"], rows, [f"M{i}" for i in range(5)],
                   [{"Value": i} for i in range(MAX_CELLS)])
    out = read_grid(payload, few_plan(5, top=MAX_TOP))
    assert out["row_count"] == MAX_CELLS // 5 and out["truncated"] is False


def test_the_cell_cap_counts_what_is_returned_not_the_extra_row():
    top = MAX_CELLS // 5
    rows = [[f"R{i}"] for i in range(top + 1)]
    payload = grid(["D0"], rows, [f"M{i}" for i in range(5)],
                   [{"Value": i} for i in range(len(rows) * 5)])
    out = read_grid(payload, few_plan(5, top=top))
    assert out["row_count"] == top and out["truncated"] is True
