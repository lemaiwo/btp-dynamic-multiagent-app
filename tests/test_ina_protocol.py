"""The InA wire format (``agents/ina/protocol.py``) without HTTP.

The three readers and the one gate of a run, as pure functions: what a run's
arguments may be (``plan_run``: every cap and rule, refused with a code of
the closed list and a text that names the argument, never the value), what
``read_description`` makes of an odd description (cleaned, cut, skipped,
capped: always something the save-time gate takes) and what ``read_grid``
lets through (only what was asked, and nothing of a grid of a form it does
not know). The client suite (``tests/test_ina_client.py``) runs the same
paths through the real HTTP client.

Run:  python -m pytest tests/test_ina_protocol.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ina.errors import InaError  # noqa: E402
from agents.ina.models import InaDefinition  # noqa: E402
from agents.ina.protocol import (  # noqa: E402
    MAX_CELLS,
    MAX_DIMENSIONS,
    MAX_FILTERS,
    MAX_MEASURES,
    MAX_TOP,
    RunPlan,
    plan_run,
    read_description,
    read_grid,
    sap_message_error,
)
from tests.test_ina_client import definition, fixture, grid  # noqa: E402

YEAR = {"0P_CALYEAR": "2026"}


def wide() -> InaDefinition:
    """More dimensions and measures than one run may name, plus the
    variables of ``definition()``."""
    return InaDefinition.model_validate(
        {
            **definition().model_dump(),
            "dimensions": [{"name": f"D{i}"} for i in range(MAX_DIMENSIONS + 2)],
            "measures": [{"name": f"M{i}"} for i in range(MAX_MEASURES + 1)]
            + [{"name": "0AMOUNT"}],
        }
    )


def plan(known: InaDefinition | None = None, **args: Any) -> RunPlan:
    call = {
        "measures": ["0AMOUNT"],
        "group_by": ["D0"],
        "filters": None,
        "top": 100,
        "order": None,
        "variables": dict(YEAR),
        **args,
    }
    return plan_run(known or wide(), **call)


def refused(**args: Any) -> InaError:
    with pytest.raises(InaError) as info:
        plan(**args)
    return info.value


# ------------------------------------------------------------- the run's gate


def one(operator: str, value: Any, dimension: str = "D0") -> dict[str, Any]:
    return {"filters": [{"dimension": dimension, "operator": operator, "value": value}]}


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"group_by": [f"D{i}" for i in range(MAX_DIMENSIONS + 1)]}, "result_too_large"),
        ({"measures": [f"M{i}" for i in range(MAX_MEASURES + 1)]}, "result_too_large"),
        ({"filters": one("=", "x")["filters"] * (MAX_FILTERS + 1)}, "invalid_filter"),
        ({"top": MAX_TOP + 1}, "result_too_large"),
        ({"top": 0}, "invalid_filter"),
        ({"top": True}, "invalid_filter"),
        ({"top": "10"}, "invalid_filter"),
        ({"measures": []}, "invalid_filter"),
        ({"measures": ["0UNKNOWN"]}, "invalid_filter"),
        ({"measures": ["0AMOUNT", "0AMOUNT"]}, "invalid_filter"),
        ({"group_by": ["DX"]}, "invalid_filter"),
        ({"group_by": "D0"}, "invalid_filter"),
        ({"filters": "D0 = x"}, "invalid_filter"),
        (one("=", "x", dimension="DX"), "invalid_filter"),
        (one("=", 1, dimension="0AMOUNT"), "invalid_filter"),
        (one("like", "x"), "invalid_filter"),
        (one("=", True), "invalid_filter"),
        (one("=", None), "invalid_filter"),
        (one("=", {"a": 1}), "invalid_filter"),
        (one("=", ["x"]), "invalid_filter"),
        (one("=", float("nan")), "invalid_filter"),
        (one("=", 1e20), "invalid_filter"),
        (one("=", "x" * 256), "invalid_filter"),
        (one("=", 10**300), "invalid_filter"),
        (one("=", 10**5000), "invalid_filter"),
        (one("between", ["1"]), "invalid_filter"),
        (one("between", "1"), "invalid_filter"),
        (one("in", []), "invalid_filter"),
        (one("in", ["x"] * 51), "invalid_filter"),
        (
            {"filters": [{"dimension": "D0", "operator": "=", "value": "x", "extra": 1}]},
            "invalid_filter",
        ),
        ({"filters": [{"dimension": "D0", "operator": "="}]}, "invalid_filter"),
        ({"filters": ["D0 = x"]}, "invalid_filter"),
        ({"order": [{"by": "D5"}]}, "invalid_filter"),
        ({"order": [{"by": "0AMOUNT", "direction": "sideways"}]}, "invalid_filter"),
        ({"order": [{"by": "0AMOUNT"}, {"by": "0AMOUNT"}]}, "invalid_filter"),
        ({"order": [{"by": "0AMOUNT", "nulls": "last"}]}, "invalid_filter"),
        ({"order": [{"by": "D0"}] * 11}, "invalid_filter"),
        ({"variables": ["0P_CALYEAR"]}, "invalid_filter"),
        ({"variables": {**YEAR, "ZV_UNKNOWN": "x"}}, "invalid_filter"),
        ({"variables": {"0P_CALYEAR": ["2026"]}}, "invalid_filter"),
        ({"variables": {**YEAR, "ZV_PERIOD": "01"}}, "invalid_filter"),
        ({"variables": {**YEAR, "ZV_PERIOD": ["01"]}}, "invalid_filter"),
        ({"variables": {**YEAR, "ZV_REGION": "NORTH"}}, "invalid_filter"),
        ({"variables": {}}, "variable_required"),
        ({"variables": None}, "variable_required"),
        ({"variables": {"0P_CALYEAR": ""}}, "variable_required"),
        ({"variables": {"0P_CALYEAR": []}}, "variable_required"),
    ],
)
def test_a_refused_argument_names_the_argument_and_the_rule(args, code):
    error = refused(**args)
    assert error.code == code
    field, _, rule = error.message.partition(": ")
    assert field and rule
    # Never the value or a name the caller sent.
    for sent in ("x" * 20, "ZV_UNKNOWN", "0UNKNOWN", "DX", "like", "sideways", "nulls", "NORTH"):
        assert sent not in error.message


def test_a_missing_mandatory_variable_is_named_from_the_definition():
    error = refused(variables={})
    assert error.code == "variable_required"
    assert error.message.startswith("variables.0P_CALYEAR: ")


def test_filters_become_one_selection_each_on_the_key_field():
    out = plan(
        definition(),
        group_by=["ZREGION"],
        filters=[
            {"dimension": "ZREGION", "operator": "in", "value": ["N", 7, 2.5]},
            {"dimension": "0CALMONTH", "operator": "<=", "value": "202612"},
        ],
    )
    assert out.selections == [
        {
            "SetOperand": {
                "FieldName": "ZREGION.KEY",
                "Elements": [
                    {"Comparison": "=", "Low": "N"},
                    {"Comparison": "=", "Low": "7"},
                    {"Comparison": "=", "Low": "2.5"},
                ],
            }
        },
        {
            "SetOperand": {
                "FieldName": "0CALMONTH.KEY",
                "Elements": [{"Comparison": "<=", "Low": "202612"}],
            }
        },
    ]


def test_a_dimension_without_a_key_field_is_filtered_by_its_name():
    out = plan(filters=one("=", "x")["filters"])
    assert out.selections[0]["SetOperand"]["FieldName"] == "D0"


def test_a_value_is_a_json_string_never_part_of_a_name():
    """Whatever a value holds, it is the ``Low`` of an element: quotes,
    brackets and an expression stay text."""
    value = '"] OR 1=1 --{}'
    out = plan(filters=one("=", value)["filters"])
    assert out.selections[0]["SetOperand"]["Elements"] == [{"Comparison": "=", "Low": value}]


# ----------------------------------------------------------------- describe


def test_describe_cleans_cuts_skips_and_caps_what_sap_names():
    cube = fixture("describe_ok.json")["Cube"]
    month, region, measures = cube["Dimensions"]
    month["Description"] = "Month\u2028of\x00the\u202eyear " + "x" * 200
    measures["Members"][0]["Unit"] = "EUR-" + "u" * 60
    region["Name"] = "R" * 200  # does not fit the gate's name rule
    extra = [{"Name": f"ZDIM{i}", "Description": f"D{i}"} for i in range(120)]
    cube["Dimensions"] = [month, region, *extra, measures]
    cube["Variables"].append({"Name": "ZV_BAD\nNAME", "Mandatory": True})
    described = read_description(cube)
    d = described.definition
    assert len(d.dimensions) == 100 and described.truncated is True
    first = d.dimensions[0]
    assert first.name == "0CALMONTH"
    assert first.label.startswith("Month of the year x") and len(first.label) == 120
    assert d.measures[0].unit == ("EUR-" + "u" * 60)[:32]
    assert [v.name for v in d.variables] == ["0P_CALYEAR", "ZV_REGION", "ZV_PERIOD"]
    assert described.skipped == ["R" * 64, "ZV_BAD NAME"]
    # Whatever was cut or skipped, the result passes the save-time gate.
    InaDefinition.model_validate(d.model_dump())


def test_describe_skips_a_repeated_name():
    cube = fixture("describe_ok.json")["Cube"]
    cube["Dimensions"].insert(1, copy.deepcopy(cube["Dimensions"][0]))
    described = read_description(cube)
    assert [x.name for x in described.definition.dimensions] == ["0CALMONTH", "ZREGION"]
    assert described.skipped == ["0CALMONTH"]


def test_a_key_field_that_does_not_fit_is_left_out_not_the_dimension():
    cube = fixture("describe_ok.json")["Cube"]
    cube["Dimensions"][0]["KeyAttribute"] = "K\n" * 3
    described = read_description(cube)
    assert described.definition.dimensions[0].key_field is None
    assert described.skipped == []


def test_a_description_without_a_measure_structure_is_sap_error():
    cube = fixture("describe_ok.json")["Cube"]
    cube["Dimensions"] = [d for d in cube["Dimensions"] if d["Name"] != "Measures"]
    with pytest.raises(InaError) as info:
        read_description(cube)
    assert info.value.code == "sap_error"


def test_a_measure_structure_without_members_is_read_as_no_measures():
    cube = fixture("describe_ok.json")["Cube"]
    cube["Dimensions"][-1]["Members"] = []
    assert read_description(cube).definition.measures == []


@pytest.mark.parametrize(
    ("value", "mandatory"),
    [
        (True, True),
        (1, True),
        ("X", True),
        ("x", True),
        ("true", True),
        ("TRUE", True),
        (False, False),
        (0, False),
        (2, False),
        ("", False),
        ("false", False),
        ("yes", False),
        (None, False),
    ],
)
def test_the_spellings_of_mandatory(value, mandatory):
    cube = fixture("describe_ok.json")["Cube"]
    cube["Variables"][0]["Mandatory"] = value
    assert read_description(cube).definition.variables[0].mandatory is mandatory


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Query ZQ_SALES_01 ist unbekannt", "query_unknown"),
        ("Query ZQ_SALES_01 is unknown.", "query_unknown"),
        ("query 2CZVIEW does not exist", "query_unknown"),
        ("Abfrage ZQ_SALES_01 existiert nicht", "query_unknown"),
        ("Query ZQ_SALES_01 not found", "query_unknown"),
        ("Query ZQ_SALES_01, variable ZV_REGION not found", "sap_error"),
        ("Query ZQ_SALES_01 variable ZV_REGION not found", "sap_error"),
        ("Query ZQ_SALES_01: value 4711 is unknown", "sap_error"),
        ("Unknown Key Field: Analytics/MetadataCatalog//DataSource", "sap_error"),
    ],
)
def test_only_the_unknown_query_message_is_query_unknown(text, code):
    error = sap_message_error({"Messages": [{"Type": 2, "Text": text}]})
    assert error is not None and error.code == code


@pytest.mark.parametrize("cube", [None, [], {"Dimensions": "x"}, {"Variables": []}])
def test_a_description_without_dimensions_is_sap_error(cube):
    with pytest.raises(InaError) as info:
        read_description(cube)
    assert info.value.code == "sap_error"


# --------------------------------------------------------------------- grid


def run_plan(measures: list[str], group_by: list[str], top: int = 100) -> RunPlan:
    return plan_run(
        definition(),
        measures=measures,
        group_by=group_by,
        filters=None,
        top=top,
        order=None,
        variables=dict(YEAR),
    )


def unreadable(payload: Any, measures: list[str], group_by: list[str], **kw: Any) -> InaError:
    with pytest.raises(InaError) as info:
        read_grid(payload, run_plan(measures, group_by, **kw))
    return info.value


def test_a_measure_whose_unit_varies_gets_a_unit_column():
    payload = grid(
        ["ZREGION"],
        [["NORTH"], ["SOUTH"]],
        ["0AMOUNT"],
        [{"Value": 5, "Currency": "EUR"}, {"Value": 7, "Currency": "USD"}],
    )
    out = read_grid(payload, run_plan(["0AMOUNT"], ["ZREGION"]))
    assert out["columns"] == ["ZREGION", "0AMOUNT", "0AMOUNT.unit"]
    assert out["rows"] == [["NORTH", 5, "EUR"], ["SOUTH", 7, "USD"]]
    assert out["units"] == {}


def test_what_sap_returns_beyond_what_was_asked_is_not_passed_on():
    """A column of a measure that was not asked for is dropped; text in the
    grid is cleaned and cut."""
    payload = grid(
        ["ZREGION"],
        [["NORTH\u202e\x07"]],
        ["0AMOUNT", "0SECRET"],
        [{"Value": 5, "Unit": "EUR"}, {"Value": 99, "Unit": "EUR"}],
    )
    out = read_grid(payload, run_plan(["0AMOUNT"], ["ZREGION"]))
    assert out["columns"] == ["ZREGION", "0AMOUNT"]
    assert out["rows"] == [["NORTH", 5]]
    assert "0SECRET" not in json.dumps(out) and "99" not in json.dumps(out["rows"])


def test_a_long_text_value_is_cut_and_cleaned():
    payload = grid(["ZREGION"], [["N"]], ["0AMOUNT"], [{"Value": "v\u2029" + "w" * 400}])
    out = read_grid(payload, run_plan(["0AMOUNT"], ["ZREGION"]))
    (row,) = out["rows"]
    assert row[1] == ("v" + "w" * 400)[:255]


def test_an_empty_result_is_no_rows():
    payload = grid(["ZREGION"], [], ["0AMOUNT"], [])
    out = read_grid(payload, run_plan(["0AMOUNT"], ["ZREGION"]))
    assert out == {
        "columns": ["ZREGION", "0AMOUNT"],
        "rows": [],
        "units": {},
        "row_count": 0,
        "truncated": False,
    }


def test_without_group_by_the_one_row_is_the_total():
    payload = grid([], [[]], ["0AMOUNT"], [{"Value": 42, "Unit": "EUR"}])
    out = read_grid(payload, run_plan(["0AMOUNT"], []))
    assert out["rows"] == [[42]] and out["units"] == {"0AMOUNT": "EUR"}


@pytest.mark.parametrize(
    "mangle",
    [
        lambda g: g["Grids"][0].pop("Cells"),
        lambda g: g["Grids"][0]["Cells"].pop(),
        lambda g: g["Grids"][0]["Cells"].__setitem__(0, {"Value": {"nested": 1}}),
        lambda g: g["Grids"][0]["Cells"].__setitem__(0, {"Value": True}),
        lambda g: g["Grids"][0]["Cells"].__setitem__(0, {"Foo": 1}),
        lambda g: g["Grids"][0]["Cells"].__setitem__(0, {}),
        lambda g: g["Grids"][0]["Cells"].__setitem__(0, "1200.5"),
        lambda g: g["Grids"][0]["Axes"][0]["Dimensions"].__setitem__(0, {"Name": "ZOTHER"}),
        lambda g: g["Grids"][0]["Axes"][0]["Tuples"][0]["Members"].append({"Name": "X"}),
        lambda g: g["Grids"][0]["Axes"][0]["Tuples"][0]["Members"][0].pop("Name"),
        lambda g: g["Grids"][0]["Axes"][1]["Tuples"].pop(),
        lambda g: g["Grids"][0]["Axes"][1]["Dimensions"].append({"Name": "ZREGION"}),
        lambda g: g["Grids"][0]["Axes"].pop(),
        lambda g: g["Grids"][0]["Axes"].append(copy.deepcopy(g["Grids"][0]["Axes"][0])),
        lambda g: g["Grids"].clear(),
        lambda g: g.pop("Grids"),
    ],
)
def test_a_grid_this_reader_does_not_know_is_sap_error(mangle):
    payload = fixture("run_ok.json")
    mangle(payload)
    error = unreadable(payload, ["0AMOUNT", "0QUANTITY"], ["0CALMONTH"])
    assert error.code == "sap_error"


def test_a_grid_of_more_than_max_cells_is_result_too_large_never_cut():
    measures = [f"M{i}" for i in range(3)]
    rows = [[f"R{i}"] for i in range(MAX_CELLS // 3 + 1)]
    cells = [{"Value": i} for i in range(len(rows) * 3)]
    payload = grid(["D0"], rows, measures, cells)
    with pytest.raises(InaError) as info:
        read_grid(payload, plan(measures=measures, top=MAX_TOP))
    assert info.value.code == "result_too_large"


def test_a_grid_of_exactly_max_cells_is_read():
    measures = [f"M{i}" for i in range(5)]
    rows = [[f"R{i}"] for i in range(MAX_CELLS // 5)]
    cells = [{"Value": i} for i in range(MAX_CELLS)]
    payload = grid(["D0"], rows, measures, cells)
    out = read_grid(payload, plan(measures=measures, top=MAX_TOP))
    assert out["row_count"] == MAX_CELLS // 5 and out["truncated"] is False


def test_the_row_beyond_top_says_truncated_and_is_not_returned():
    out = read_grid(
        fixture("run_ok.json"), run_plan(["0AMOUNT", "0QUANTITY"], ["0CALMONTH"], top=2)
    )
    assert out["truncated"] is True and out["row_count"] == 2
    assert [row[0] for row in out["rows"]] == ["202601", "202602"]


def test_exactly_top_rows_is_not_truncated():
    out = read_grid(
        fixture("run_ok.json"), run_plan(["0AMOUNT", "0QUANTITY"], ["0CALMONTH"], top=3)
    )
    assert out["truncated"] is False and out["row_count"] == 3


def test_the_cell_cap_counts_what_is_returned_not_the_extra_row():
    measures = [f"M{i}" for i in range(5)]
    top = MAX_CELLS // 5
    rows = [[f"R{i}"] for i in range(top + 1)]
    cells = [{"Value": i} for i in range(len(rows) * 5)]
    payload = grid(["D0"], rows, measures, cells)
    out = read_grid(payload, plan(measures=measures, top=top))
    assert out["row_count"] == top and out["truncated"] is True


def test_more_rows_than_top_plus_one_is_result_too_large():
    error = unreadable(fixture("run_ok.json"), ["0AMOUNT", "0QUANTITY"], ["0CALMONTH"], top=1)
    assert error.code == "result_too_large"
