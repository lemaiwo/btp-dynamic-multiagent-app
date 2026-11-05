"""The InA wire format (``agents/ina/protocol.py``) without HTTP.

The gate of a run and the requests, as pure functions: what a run's
arguments may be (``plan_run``: every cap and rule, refused with a code of
the closed list and a text that names the argument, never the value), what
``read_description`` makes of an odd description (cleaned, cut, skipped,
capped: always something the save-time gate takes), the three requests of a
run in the form SIA answered (VariableSubmit, read, Close) and the text
attributes a submit answer names. The grid reader is
``tests/test_ina_grid.py``; the client suite (``tests/test_ina_client.py``)
runs the same paths through the real HTTP client.

Run:  python -m pytest tests/test_ina_protocol.py
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ina.errors import InaError  # noqa: E402
from agents.ina.models import InaDefinition  # noqa: E402
from agents.ina.protocol import (  # noqa: E402
    MAX_DIMENSIONS,
    MAX_FILTERS,
    MAX_MEASURES,
    MAX_TOP,
    RunPlan,
    plan_run,
    read_description,
    sap_message_error,
    text_attributes,
)
from tests.ina_helpers import definition, fixture  # noqa: E402

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


ONE_MEASURE = "measures: one measure per run until the column order is verified"


@pytest.mark.parametrize(
    "measures",
    [["0AMOUNT", "M1"], ["M1", "0AMOUNT"], [f"M{i}" for i in range(MAX_MEASURES)],
     ["0AMOUNT", "0AMOUNT"], ["0AMOUNT", "0UNKNOWN"]],
)
def test_more_than_one_measure_is_refused_until_the_column_order_is_verified(measures):
    """Every grid SIA answered held ONE measure; with several, nothing in the
    answer names the measure of a column, so a wrong guess would be wrong
    numbers under the right names. Refused until a probe verifies the order;
    the text is fixed and names no measure."""
    error = refused(measures=measures)
    assert (error.code, error.message) == ("invalid_filter", ONE_MEASURE)
    assert error.hint == "run the query once per measure"
    assert "0UNKNOWN" not in str(error.to_dict())


def test_one_measure_is_planned():
    assert plan(measures=["0AMOUNT"]).measures == ["0AMOUNT"]


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


def test_a_dimension_without_a_key_field_is_filtered_by_the_key_the_read_asks_for():
    """One key per dimension (``protocol.key_of``): the filter names the
    attribute the read lists as the key, ``<dimension>.KEY`` without a
    described key field (the system's ``KeyAttribute`` for each of its
    dimensions)."""
    out = plan(filters=one("=", "x")["filters"])
    assert out.selections[0]["SetOperand"]["FieldName"] == "D0.KEY"
    assert out.keys["D0"] == "D0.KEY"


def test_key_of_is_the_described_key_field_else_the_dimension_key():
    from agents.ina.models import InaDimension
    from agents.ina.protocol import key_of

    assert key_of(InaDimension(name="D1", key_field="[D1].[KEY]")) == "[D1].[KEY]"
    assert key_of(InaDimension(name="D2")) == "D2.KEY"


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
        ("", False),
        ("false", False),
        (" FALSE ", False),
    ],
)
def test_the_spellings_of_mandatory(value, mandatory, caplog):
    cube = fixture("describe_ok.json")["Cube"]
    cube["Variables"][0]["Mandatory"] = value
    with caplog.at_level("WARNING", logger="agents.ina.description"):
        assert read_description(cube).definition.variables[0].mandatory is mandatory
    assert caplog.records == []


@pytest.mark.parametrize("value", ["yes", 2, None, -1, 1.0, ["X"], {"X": True}, "no"])
def test_a_mandatory_value_that_cannot_be_read_is_mandatory_and_logged(value, caplog):
    """Fail closed (review M1): a ``Mandatory`` that is neither a known yes
    nor a known no is no "optional", whatever ``InputType`` says."""
    cube = fixture("describe_ok.json")["Cube"]
    cube["Variables"][0]["Mandatory"] = value
    with caplog.at_level("WARNING", logger="agents.ina.description"):
        assert read_description(cube).definition.variables[0].mandatory is True
    assert [r.getMessage() for r in caplog.records] == [
        "ina: variable 1 of the description carries no mandatory marker this "
        "client reads; read as mandatory"
    ]


def test_an_unreadable_mandatory_value_is_not_made_optional_by_input_type(caplog):
    cube = sia_cube()
    cube["Variables"][2]["Mandatory"] = None  # InputType of variable 3 says Optional
    with caplog.at_level("WARNING", logger="agents.ina.description"):
        assert read_description(cube).definition.variables[2].mandatory is True
    assert len(caplog.records) == 1 and "variable 3" in caplog.records[0].getMessage()


# -------------------------------------------------- the describe answer of SIA
# ``describe_sia.json`` is the describe answer SIA (S/4HANA 2022, SAP_BW 757)
# gave on 2026-11-04 for the CDS query 2CCMMPURORDQUERY, session ids replaced:
# SIA marks a mandatory variable with ``InputType`` (``Mandatory``,
# ``MandatoryNotInitial`` or ``Optional``), never with ``Mandatory``.

SIA_MANDATORY = ["!CDS_P_P_DISPLAYCURRENCY", "!CDS_P_P_DATEFUNCTION"]
SIA_OPTIONAL = ["!CDS_P_P_STARTDATE", "!CDS_P_P_ENDDATE", "!CDS_S_SYSTEM_LANGUAGE"]


def sia_cube() -> dict[str, Any]:
    return fixture("describe_sia.json")["Cube"]


def test_the_sia_describe_marks_the_variables_sap_demands_as_mandatory():
    described = read_description(sia_cube())
    d = described.definition
    assert [(v.name, v.mandatory) for v in d.variables] == [
        *((name, True) for name in SIA_MANDATORY),
        *((name, False) for name in SIA_OPTIONAL),
    ]
    assert [v.type for v in d.variables] == ["single"] * 5
    assert d.variables[0].label == "Devise d'affichage"
    assert len(d.dimensions) == 34 and described.skipped == [] and not described.truncated
    assert all(x.key_field == f"{x.name}.KEY" for x in d.dimensions)
    assert [(m.name, m.label, m.unit) for m in d.measures] == [
        ("ELTUIDCMP1", "Mnt net cde achat", None)
    ]
    InaDefinition.model_validate(d.model_dump())


def test_the_sia_definition_refuses_a_run_without_the_mandatory_values():
    d = read_description(sia_cube()).definition
    with pytest.raises(InaError) as info:
        plan_run(
            d, measures=["ELTUIDCMP1"], group_by=[], filters=None, top=1, order=None,
            variables=None,
        )
    assert info.value.code == "variable_required"
    assert SIA_MANDATORY[0] in info.value.message


@pytest.mark.parametrize(
    ("value", "mandatory"),
    [
        ("Mandatory", True),
        ("MandatoryNotInitial", True),
        ("mandatory", True),
        (" Optional ", False),
        ("Optional", False),
    ],
)
def test_the_spellings_of_input_type(value, mandatory, caplog):
    cube = sia_cube()
    cube["Variables"][0]["InputType"] = value
    with caplog.at_level("WARNING", logger="agents.ina.description"):
        assert read_description(cube).definition.variables[0].mandatory is mandatory
    assert caplog.records == []


@pytest.mark.parametrize("value", ["Hidden", "", None, 1, True, ["Mandatory"]])
def test_an_input_type_that_cannot_be_read_is_mandatory_and_logged(value, caplog):
    cube = sia_cube()
    cube["Variables"][2]["InputType"] = value  # an optional one: not silently kept so
    with caplog.at_level("WARNING", logger="agents.ina.description"):
        variables = read_description(cube).definition.variables
    assert variables[2].mandatory is True
    assert [r.getMessage() for r in caplog.records] == [
        "ina: variable 3 of the description carries no mandatory marker this "
        "client reads; read as mandatory"
    ]


def test_a_variable_without_any_marker_is_mandatory_and_named_by_position_only(caplog):
    cube = sia_cube()
    del cube["Variables"][4]["InputType"]
    with caplog.at_level("WARNING", logger="agents.ina.description"):
        variables = read_description(cube).definition.variables
    assert variables[4].mandatory is True
    (record,) = caplog.records
    assert "variable 5" in record.getMessage()
    assert "SYSTEM_LANGUAGE" not in record.getMessage()


@pytest.mark.parametrize(
    ("flag", "input_type"),
    [(False, "Mandatory"), (True, "Optional"), ("X", "Optional")],
)
def test_any_marker_that_says_mandatory_wins(flag, input_type):
    cube = sia_cube()
    cube["Variables"][2].update({"Mandatory": flag, "InputType": input_type})
    assert read_description(cube).definition.variables[2].mandatory is True


def test_two_markers_that_say_optional_are_optional():
    cube = sia_cube()
    cube["Variables"][0].update({"Mandatory": False, "InputType": "Optional"})
    assert read_description(cube).definition.variables[0].mandatory is False


@pytest.mark.parametrize(
    ("patch", "kind"),
    [
        ({}, "single"),
        ({"FilterCapability": {"ComparisonGroup": "Interval"}}, "interval"),
        ({"FilterCapability": {"ComparisonGroup": "Range"}}, "interval"),
        ({"MultipleValues": True}, "multiple"),
        ({"MultipleValues": "true"}, "single"),
        ({"FilterCapability": "Interval"}, "single"),
        # What kind of variable it is says nothing about its values.
        ({"VariableType": "MultipleIntervalVariable"}, "single"),
    ],
)
def test_the_selection_type_of_a_sia_variable(patch, kind):
    cube = sia_cube()
    cube["Variables"][0].update(patch)
    assert read_description(cube).definition.variables[0].type == kind


def test_the_sia_refusal_of_a_run_without_its_variables_is_sap_error_with_its_text():
    error = sap_message_error(fixture("run_sia_variables_missing.json"))
    assert error is not None and error.code == "sap_error"
    assert error.message.startswith("Query kann nur dann ausgeführt werden")


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


# ------------------------------------------------ the run SIA answered (run 5)
# Recorded 2026-11-04: SIA refuses a read whose variables travel in its own
# ``Definition`` (no ``DirectVariableTransfer``). It answered with a grid only
# when a ``VariableSubmit`` (a copy of the read with
# ``ProcessingDirectives.ProcessingStep``) came first, as a request of its own,
# on the ``InstanceId`` the read then used, with each value named by the key
# of its variable's dimension and the measure structure by its real name.

from dataclasses import replace  # noqa: E402

from agents.ina.protocol import (  # noqa: E402
    analytics_request,
    close_request,
    data_source,
    variable_submit_request,
)

SIA_VALUES = {"!CDS_P_P_DISPLAYCURRENCY": "EUR", "!CDS_P_P_DATEFUNCTION": "YEARTODATE"}


def test_the_sia_describe_names_the_measure_structure_and_each_variables_key():
    d = read_description(sia_cube()).definition
    assert d.measure_structure == "ELTUIDSTRCMMPURORDQUERY"
    assert [(v.name, v.key_field) for v in d.variables] == [
        ("!CDS_P_P_DISPLAYCURRENCY", "2CH0Y9FATWMXU07PABNUMVE2HHX.KEY"),
        ("!CDS_P_P_DATEFUNCTION", "2CCGCALDFVH.KEY"),
        ("!CDS_P_P_STARTDATE", "2CQVA05CB3RQ8IWG16R9ADB9KSB.KEY"),
        ("!CDS_P_P_ENDDATE", "2CHZY5N72NNDJ2R09JG98XXDF7M.KEY"),
        ("!CDS_S_SYSTEM_LANGUAGE", "1TEXT.KEY"),
    ]


def test_a_variable_without_a_usable_dimension_reference_has_no_key_field():
    cube = sia_cube()
    cube["Variables"][0]["DimensionReference"] = {"Name": "A\nB"}
    del cube["Variables"][1]["DimensionReference"]
    d = read_description(cube).definition
    assert d.variables[0].key_field is None and d.variables[1].key_field is None


def test_the_synthetic_describe_keeps_the_measures_structure():
    assert read_description(fixture("describe_ok.json")["Cube"]).definition.measure_structure == (
        "Measures"
    )


def sia_plan(**args: Any) -> RunPlan:
    call = {"measures": ["ELTUIDCMP1"], "group_by": [], "filters": None, "top": 1,
            "order": None, "variables": dict(SIA_VALUES), **args}
    return plan_run(read_description(sia_cube()).definition, **call)


def test_the_run_names_values_by_the_dimension_key_and_the_real_structure():
    source = data_source("2CCMMPURORDQUERY", "", instance_id="ina0123456789abcdef")
    body = analytics_request(["C"], source, sia_plan())
    analytics = body["Analytics"]
    assert analytics["DataSource"] == {"ObjectName": "2CCMMPURORDQUERY", "Type": "Query",
                                       "InstanceId": "ina0123456789abcdef"}
    assert "ProcessingDirectives" not in analytics
    definition = analytics["Definition"]
    assert definition["Dimensions"] == [{
        "Name": "ELTUIDSTRCMMPURORDQUERY", "Axis": "Columns",
        "Members": [{"Name": "ELTUIDCMP1", "Visibility": "Visible"}],
    }]
    assert definition["Variables"] == [
        {"Name": "!CDS_P_P_DISPLAYCURRENCY", "Values": {"Selection": {"SetOperand": {
            "FieldName": "2CH0Y9FATWMXU07PABNUMVE2HHX.KEY",
            "Elements": [{"Comparison": "=", "Low": "EUR", "IsExcluding": False}]}}}},
        {"Name": "!CDS_P_P_DATEFUNCTION", "Values": {"Selection": {"SetOperand": {
            "FieldName": "2CCGCALDFVH.KEY",
            "Elements": [{"Comparison": "=", "Low": "YEARTODATE", "IsExcluding": False}]}}}},
    ]


SIA_ROWS = ["2CIMMPURGORG", "2CI_SUPPLIER_CDS"]
HIDE_TOTALS = [
    {"Result": "Members", "Visibility": "Visible"},
    {"Result": "Total", "Visibility": "Hidden"},
]
STRUCTURE = {
    "Name": "ELTUIDSTRCMMPURORDQUERY",
    "Axis": "Columns",
    "Members": [{"Name": "ELTUIDCMP1", "Visibility": "Visible"}],
}


def sia_texts(group_by: list[str]) -> dict[str, str]:
    return text_attributes(fixture("submit_sia.json")["Cube"], group_by)


def test_the_read_asks_each_row_dimension_for_its_key_and_text_and_hides_totals():
    """Run 8, the read SIA answered with keys and texts: each Rows entry
    lists its key and text attribute and hides the totals; no
    ``MaxResultRecords``, ``RowTo`` is top + 1."""
    plan = replace(sia_plan(group_by=SIA_ROWS, top=3), texts=sia_texts(SIA_ROWS))
    source = data_source("2CCMMPURORDQUERY", "", instance_id="ina0123456789abcdef")
    definition = analytics_request(["C"], source, plan)["Analytics"]["Definition"]
    assert definition["Dimensions"] == [
        {
            "Name": "2CIMMPURGORG",
            "Axis": "Rows",
            "Attributes": [
                {"Name": "2CIMMPURGORG.KEY", "Obtainability": "Always"},
                {"Name": "2CIMMPURGORG.LONG_TEXT", "Obtainability": "Always"},
            ],
            "ResultStructure": HIDE_TOTALS,
        },
        {
            "Name": "2CI_SUPPLIER_CDS",
            "Axis": "Rows",
            "Attributes": [
                {"Name": "2CI_SUPPLIER_CDS.KEY", "Obtainability": "Always"},
                {"Name": "2CI_SUPPLIER_CDS.LONG_TEXT", "Obtainability": "Always"},
            ],
            "ResultStructure": HIDE_TOTALS,
        },
        STRUCTURE,
    ]
    assert definition["ResultSetFeatureRequest"] == {
        "ResultFormat": "Version2",
        "ResultEncoding": "None",
        "SubSetDescription": {"RowFrom": 0, "RowTo": 4, "ColumnFrom": 0, "ColumnTo": -1},
        "UseDefaultAttributeKey": False,
    }


def test_a_row_dimension_without_a_known_text_asks_for_its_key_alone():
    plan = sia_plan(group_by=["2CCMMPURORDSPND-COMPANYCODE"])
    plan = replace(plan, texts=sia_texts(plan.group_by))
    (row, _) = analytics_request(["C"], data_source("Q", ""), plan)["Analytics"]["Definition"][
        "Dimensions"]
    assert row["Attributes"] == [
        {"Name": "2CCMMPURORDSPND-COMPANYCODE.KEY", "Obtainability": "Always"}]


def test_the_key_attribute_is_the_described_key_field_else_the_dimension_key():
    d = InaDefinition.model_validate({
        "dimensions": [{"name": "D1", "key_field": "[D1].[KEY]"}, {"name": "D2"}],
        "measures": [{"name": "M"}],
    })
    plan = plan_run(d, measures=["M"], group_by=["D1", "D2"], filters=None, top=1, order=None,
                    variables=None)
    rows = analytics_request(["C"], data_source("Q", ""), plan)["Analytics"]["Definition"][
        "Dimensions"][:2]
    assert [r["Attributes"] for r in rows] == [
        [{"Name": "[D1].[KEY]", "Obtainability": "Always"}],
        [{"Name": "D2.KEY", "Obtainability": "Always"}],
    ]


def test_the_submit_is_sent_in_the_form_sia_accepted():
    """Runs 5 to 8: the VariableSubmit was the read of the earlier form (Rows
    without attributes, ``MaxResultRecords``) with the processing step, on
    the read's ``InstanceId``, the same variables."""
    source = data_source("2CCMMPURORDQUERY", "", instance_id="ina0123456789abcdef")
    plan = sia_plan(group_by=SIA_ROWS, top=3)
    submit = variable_submit_request(["C"], source, plan)["Analytics"]
    read = analytics_request(["C"], source, plan)["Analytics"]
    assert submit["ProcessingDirectives"] == {"ProcessingStep": "VariableSubmit"}
    assert submit["DataSource"] == read["DataSource"]
    assert submit["Definition"]["Variables"] == read["Definition"]["Variables"]
    assert submit["Definition"]["Dimensions"] == [
        {"Name": "2CIMMPURGORG", "Axis": "Rows"},
        {"Name": "2CI_SUPPLIER_CDS", "Axis": "Rows"},
        STRUCTURE,
    ]
    assert submit["Definition"]["ResultSetFeatureRequest"] == {
        "ResultFormat": "Version2",
        "ResultEncoding": "None",
        "MaxResultRecords": 4,
        "SubSetDescription": {"RowFrom": 0, "RowTo": 4, "ColumnFrom": 0, "ColumnTo": -1},
        "UseDefaultAttributeKey": False,
    }


def test_the_text_attributes_the_submit_answer_names():
    """Only the submit answer's ``Cube`` names a text attribute for most
    dimensions (11 of 35 on SIA; the describe answer names 1)."""
    assert sia_texts([*SIA_ROWS, "2CCMMPURORDSPND-COMPANYCODE"]) == {
        "2CIMMPURGORG": "2CIMMPURGORG.LONG_TEXT",
        "2CI_SUPPLIER_CDS": "2CI_SUPPLIER_CDS.LONG_TEXT",
    }
    assert sia_texts([]) == {}


@pytest.mark.parametrize(
    "text",
    [
        "2CIPLANT.LONG_TEXT",
        "2CIMMPURGORG_LONG_TEXT",
        "2CIMMPURGORG.LONG\nTEXT",
        "2CIMMPURGORG.‮TXET",
        "2CIMMPURGORG." + "X" * 130,
        "2CIMMPURGORG.KEY",
        5,
        None,
        ["2CIMMPURGORG.LONG_TEXT"],
    ],
)
def test_a_text_attribute_that_is_not_the_dimensions_own_is_not_asked_for(text):
    cube = fixture("submit_sia.json")["Cube"]
    for dimension in cube["Dimensions"]:
        if dimension["Name"] == "2CIMMPURGORG":
            dimension["TextAttribute"] = text
    assert text_attributes(cube, ["2CIMMPURGORG"]) == {}


@pytest.mark.parametrize("cube", [None, [], "x", {"Dimensions": "x"}, {"Dimensions": [None, 5]}])
def test_a_submit_answer_without_dimensions_names_no_text(cube):
    assert text_attributes(cube, SIA_ROWS) == {}


def test_a_sort_on_a_measure_names_the_real_structure():
    plan = sia_plan(group_by=["2CIMMPURGORG"],
                    order=[{"by": "ELTUIDCMP1", "direction": "desc"}])
    assert analytics_request(["C"], data_source("Q", ""), plan)["Analytics"]["Definition"][
        "Sort"] == [{"Dimension": "ELTUIDSTRCMMPURORDQUERY", "SortType": "Measure",
                     "MeasureName": "ELTUIDCMP1", "Direction": "Desc"}]


def test_an_older_description_falls_back_to_measures_and_the_variable_name():
    """A row described before 2.37.1 holds neither name: the request keeps
    the form it had (which SIA refuses; Describe again to fix it)."""
    d = InaDefinition.model_validate({
        "measures": [{"name": "M"}],
        "variables": [{"name": "!V", "mandatory": True}],
    })
    plan = plan_run(d, measures=["M"], group_by=[], filters=None, top=1, order=None,
                    variables={"!V": "EUR"})
    definition = analytics_request(["C"], data_source("Q", ""), plan)["Analytics"]["Definition"]
    assert definition["Dimensions"][0]["Name"] == "Measures"
    assert definition["Variables"][0]["Values"]["Selection"]["SetOperand"] == {
        "FieldName": "!V", "Elements": [{"Comparison": "=", "Low": "EUR", "IsExcluding": False}]}


def test_the_close_names_the_instance_only():
    assert close_request("ina0123456789abcdef") == {
        "Analytics": {"Actions": [{"Type": "Close", "DataSources": ["ina0123456789abcdef"]}]},
        "Options": ["StatefulServer"],
    }
