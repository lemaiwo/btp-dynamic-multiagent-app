"""The InA query record and its save-time gate (`agents/ina/models.py`).

Addendum (k), part 2, item 1 of the web chat design: what an admin may store
as an analytical query, and that a refusal names the field and the rule
without ever echoing a value back.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.ina import models as ina_models  # noqa: E402
from agents.ina.models import (  # noqa: E402
    InaDefinition,
    InaQueryPayload,
    validate_ina_query,
)

SECRET = "hunter2-pasted-by-mistake"
# The rule of a member's name, label and unit, and of the query name: one
# line, and no invisible format character either.
MEMBER_RULE = "must be one line of text without control or format characters"

GOOD: dict[str, Any] = {
    "name": "sales-by-region",
    "title": "Sales by region",
    "purpose": "Net sales per region and month",
    "not_for": "Order level detail; use the sales orders service",
    "destination": "S4_INA_USER",
    "user_context": True,
    "query": "2CZQ_SALES_BY_REGION",
    "package": "ZANALYTICS",
    "definition": {
        "dimensions": [
            {"name": "0CALMONTH", "label": "Calendar month", "key_field": "0CALMONTH.KEY"},
            {"name": "ZREGION", "label": "Region"},
        ],
        "measures": [
            {"name": "ZNETSALES", "label": "Net sales", "unit": "EUR"},
            {"name": "ZQUANTITY", "label": "Quantity"},
        ],
        "variables": [
            {"name": "ZV_FYEAR", "label": "Fiscal year", "mandatory": True, "type": "single"},
            {"name": "ZV_MONTHS", "label": "Months", "type": "interval"},
        ],
    },
}


def good() -> dict[str, Any]:
    return copy.deepcopy(GOOD)


def refusal(data: Any) -> str:
    """The gate's refusal text; fails the test when the data passes."""
    with pytest.raises(ValueError) as err:
        validate_ina_query(data)
    # A plain ValueError: the ValidationError's repr carries the input.
    assert not isinstance(err.value, ValidationError)
    return str(err.value)


def with_definition(**lists: Any) -> dict[str, Any]:
    data = good()
    data["definition"].update(lists)
    return data


# -- the record ---------------------------------------------------------------


def test_a_good_payload_passes_and_round_trips():
    p = validate_ina_query(GOOD)
    assert isinstance(p, InaQueryPayload)
    assert p.definition is not None
    assert p.definition.dimensions[0].key_field == "0CALMONTH.KEY"
    assert p.definition.measures[0].unit == "EUR"
    assert p.definition.variables[0].mandatory is True
    assert validate_ina_query(p.model_dump(mode="json")) == p


def test_payload_has_exactly_the_contract_fields():
    assert sorted(InaQueryPayload.model_fields) == [
        "definition",
        "destination",
        "enabled",
        "metadata_fetched_at",
        "name",
        "not_for",
        "package",
        "purpose",
        "query",
        "title",
        "user_context",
    ]


def test_defaults():
    data = {k: GOOD[k] for k in ("name", "title", "purpose", "destination", "query")}
    p = validate_ina_query(data)
    assert p.user_context is False and p.enabled is True
    assert p.not_for == "" and p.package == ""
    # None = not described yet: the record exists before its first Describe.
    assert p.definition is None and p.metadata_fetched_at is None
    p = validate_ina_query({**data, "definition": {}})
    assert p.definition == InaDefinition()
    p = validate_ina_query(
        {
            **data,
            "definition": {
                "dimensions": [{"name": "D"}],
                "measures": [{"name": "M"}],
                "variables": [{"name": "V"}],
            },
        }
    )
    assert p.definition is not None
    dim, mea, var = p.definition.dimensions[0], p.definition.measures[0], p.definition.variables[0]
    assert (dim.label, dim.key_field) == ("", None)
    assert (mea.label, mea.unit) == ("", None)
    assert (var.label, var.mandatory, var.type) == ("", False, "single")


@pytest.mark.parametrize(
    "name", ["sales-by-region", "q1", "0abc", "a", "a" * 64, "9-9-9", "a-b"]
)
def test_name_accepts_a_slug(name):
    assert validate_ina_query({**good(), "name": name}).name == name


@pytest.mark.parametrize(
    "name",
    [
        "", "-sales", "sales-", "a-", "Sales", "a" * 65, "sales_region", "sales region",
        "sales\n", "sälès", "a/b",
    ],
)
def test_name_refuses_anything_else(name):
    text = refusal({**good(), "name": name})
    assert text.startswith("name: ")


@pytest.mark.parametrize("query", ["Q", "2CZQ_SALES", "system-local.bw/ZQ_X", "Q" * 128])
def test_query_accepts_1_to_128_characters(query):
    assert validate_ina_query({**good(), "query": query}).query == query


@pytest.mark.parametrize(
    "query", ["", "Q" * 129, "ZQ\n", "Z\x00Q", "Z\tQ", "ZQ\x7f", "Z\x85Q", "Z\u2028Q", "Z\x1bQ"]
)
def test_query_refuses_empty_long_or_control_characters(query):
    assert refusal({**good(), "query": query}).startswith("query: ")


def test_query_is_required():
    data = good()
    del data["query"]
    assert refusal(data) == "query: Field required"


def test_package_may_be_blank_and_is_at_most_64_characters():
    assert validate_ina_query({**good(), "package": ""}).package == ""
    assert validate_ina_query({**good(), "package": "P" * 64}).package == "P" * 64
    assert refusal({**good(), "package": "P" * 65}).startswith("package: ")
    assert refusal({**good(), "package": "Z\nP"}) == (
        "package: Value error, package must be one line of text without control characters"
    )


@pytest.mark.parametrize("field,limit", [("title", 120), ("purpose", 200), ("not_for", 200)])
def test_texts_have_their_length_cap(field, limit):
    assert getattr(validate_ina_query({**good(), field: "x" * limit}), field) == "x" * limit
    assert refusal({**good(), field: "x" * (limit + 1)}) == (
        f"{field}: String should have at most {limit} characters"
    )


@pytest.mark.parametrize("field", ["title", "purpose", "not_for"])
@pytest.mark.parametrize(
    "char", ["\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"]
)
def test_texts_are_one_line(field, char):
    text = refusal({**good(), field: f"{SECRET}{char}second line"})
    assert text == (
        f"{field}: Value error, {field} must be one line of text without control characters"
    )


def test_title_and_purpose_are_stripped_and_required():
    p = validate_ina_query({**good(), "title": " Sales \n", "purpose": "\tNet sales\n"})
    assert (p.title, p.purpose) == ("Sales", "Net sales")
    for field in ("title", "purpose"):
        assert refusal({**good(), field: "   "}).startswith(f"{field}: ")
        data = good()
        del data[field]
        assert refusal(data) == f"{field}: Field required"


@pytest.mark.parametrize("destination", ["S", "S4_INA-USER.1", "D" * 200])
def test_destination_accepts_a_destination_name(destination):
    assert validate_ina_query({**good(), "destination": destination}).destination == destination


@pytest.mark.parametrize("destination", ["", "D" * 201, "S4 INA", "S4\n", "https://x/y"])
def test_destination_refuses_anything_else(destination):
    assert refusal({**good(), "destination": destination}).startswith("destination: ")


@pytest.mark.parametrize("field", ["user_context", "enabled"])
@pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes", None])
def test_flags_take_only_a_json_boolean(field, value):
    # user_context decides whose identity reaches SAP: "false" or 0 must not
    # be read as a decision.
    assert refusal({**good(), field: value}) == f"{field}: Input should be a valid boolean"


def test_flags_accept_real_booleans():
    for value in (True, False):
        p = validate_ina_query({**good(), "user_context": value, "enabled": value})
        assert p.user_context is value and p.enabled is value


@pytest.mark.parametrize("field", ["name", "title", "purpose", "query", "package", "destination"])
@pytest.mark.parametrize("value", [123, 1.5, ["x"], {"x": 1}, b"bytes"])
def test_texts_take_only_a_string(field, value):
    assert refusal({**good(), field: value}) == f"{field}: Input should be a valid string"


def test_metadata_fetched_at_is_a_timestamp():
    p = validate_ina_query({**good(), "metadata_fetched_at": "2026-11-03T10:00:00Z"})
    assert p.metadata_fetched_at is not None and p.metadata_fetched_at.year == 2026
    assert refusal({**good(), "metadata_fetched_at": SECRET}).startswith("metadata_fetched_at: ")


# -- the definition -----------------------------------------------------------


@pytest.mark.parametrize(
    "where", ["dimensions", "measures", "variables"]
)
def test_member_names_are_1_to_128_characters_without_control_characters(where):
    for name in ("", "N" * 129, "N\n", "N\x00", "N\u2028"):
        data = good()
        data["definition"][where][0]["name"] = name
        assert refusal(data).startswith(f"definition.{where}.0.name: ")
    data = good()
    data["definition"][where][0]["name"] = "N" * 128
    assert validate_ina_query(data)


def test_key_field_is_a_name_too():
    data = good()
    data["definition"]["dimensions"][0]["key_field"] = "K\nx"
    assert refusal(data) == (
        f"definition.dimensions.0.key_field: Value error, key_field {MEMBER_RULE}"
    )
    data["definition"]["dimensions"][0]["key_field"] = ""
    assert refusal(data).startswith("definition.dimensions.0.key_field: ")


@pytest.mark.parametrize("where", ["dimensions", "measures"])
def test_member_labels_are_one_line_of_at_most_120(where):
    data = good()
    data["definition"][where][0]["label"] = "L" * 121
    assert refusal(data) == (
        f"definition.{where}.0.label: String should have at most 120 characters"
    )
    data["definition"][where][0]["label"] = "Net\nsales"
    assert refusal(data) == (
        f"definition.{where}.0.label: Value error, label {MEMBER_RULE}"
    )


def test_variable_label_is_one_line_of_at_most_120():
    data = good()
    data["definition"]["variables"][0]["label"] = "L" * 121
    assert refusal(data) == (
        "definition.variables.0.label: String should have at most 120 characters"
    )
    data["definition"]["variables"][0]["label"] = "Fiscal\u2029year"
    assert refusal(data) == (
        "definition.variables.0.label: Value error, "
        f"label {MEMBER_RULE}"
    )


@pytest.mark.parametrize("where", ["dimensions", "measures", "variables"])
@pytest.mark.parametrize("field", ["name", "label"])
@pytest.mark.parametrize("char", ["\u202e", "\u202a", "\u2066", "\u2069", "\u200b", "\ufeff"])
def test_member_names_and_labels_refuse_invisible_format_characters(where, field, char):
    # A bidi override reorders how the line reads in the admin table and in
    # the model's tool answer; a zero-width character hides a difference.
    data = good()
    data["definition"][where][0][field] = f"Fiscal{char}year"
    assert refusal(data) == (
        f"definition.{where}.0.{field}: Value error, {field} {MEMBER_RULE}"
    )


def test_units_key_fields_and_the_query_name_refuse_them_too():
    data = good()
    data["definition"]["measures"][0]["unit"] = "E\u202eUR"
    assert refusal(data) == (
        f"definition.measures.0.unit: Value error, unit {MEMBER_RULE}"
    )
    data = good()
    data["definition"]["dimensions"][0]["key_field"] = "K\u200bEY"
    assert refusal(data) == (
        f"definition.dimensions.0.key_field: Value error, key_field {MEMBER_RULE}"
    )
    assert refusal({**good(), "query": "2CZQ\u202eX"}) == f"query: Value error, query {MEMBER_RULE}"


def test_the_record_texts_keep_the_plain_one_line_rule():
    # Title, purpose and "not for" are the admin's own text, as on the OData
    # page: a format character there is not refused.
    p = validate_ina_query({**good(), "title": "Sales\u200b by region"})
    assert p.title == "Sales\u200b by region"


def test_measure_unit_is_optional_and_at_most_32():
    data = good()
    data["definition"]["measures"][0]["unit"] = "U" * 32
    assert validate_ina_query(data)
    data["definition"]["measures"][0]["unit"] = "U" * 33
    assert refusal(data) == (
        "definition.measures.0.unit: String should have at most 32 characters"
    )
    data["definition"]["measures"][0]["unit"] = "E\nUR"
    assert refusal(data).startswith("definition.measures.0.unit: ")
    data["definition"]["measures"][0]["unit"] = None
    assert validate_ina_query(data).definition.measures[0].unit is None


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_variable_mandatory_takes_only_a_json_boolean(value):
    data = good()
    data["definition"]["variables"][0]["mandatory"] = value
    assert refusal(data) == "definition.variables.0.mandatory: Input should be a valid boolean"


@pytest.mark.parametrize("kind", ["single", "interval", "multiple"])
def test_variable_type_accepts_the_three_kinds(kind):
    data = good()
    data["definition"]["variables"][0]["type"] = kind
    assert validate_ina_query(data).definition.variables[0].type == kind


@pytest.mark.parametrize("kind", ["range", "Single", "", None, 1])
def test_variable_type_refuses_anything_else(kind):
    data = good()
    data["definition"]["variables"][0]["type"] = kind
    assert refusal(data).startswith("definition.variables.0.type: ")


@pytest.mark.parametrize(
    "where,cap",
    [("dimensions", 100), ("measures", 100), ("variables", 50)],
)
def test_list_caps(where, cap):
    assert getattr(ina_models, f"MAX_{where.upper()}") == cap
    at_cap = with_definition(**{where: [{"name": f"N{i}"} for i in range(cap)]})
    assert len(getattr(validate_ina_query(at_cap).definition, where)) == cap
    over = with_definition(**{where: [{"name": f"N{i}"} for i in range(cap + 1)]})
    assert refusal(over) == (
        f"definition.{where}: List should have at most {cap} items after validation, "
        f"not {cap + 1}"
    )


@pytest.mark.parametrize("where", ["dimensions", "measures", "variables"])
def test_duplicate_names_are_refused_by_position_never_by_name(where):
    name = f"{SECRET}-name"
    data = with_definition(**{where: [{"name": "A"}, {"name": name}, {"name": name}]})
    text = refusal(data)
    assert text == (
        f"definition.{where}: Value error, "
        "entry 2 repeats the name of an earlier entry; names must be distinct"
    )
    assert SECRET not in text


def test_names_are_compared_exactly():
    # InA names are case sensitive technical names, and a dimension and a
    # measure live in different lists of the request.
    data = with_definition(
        dimensions=[{"name": "ZREGION"}, {"name": "zregion"}],
        measures=[{"name": "ZREGION"}],
        variables=[{"name": "ZREGION"}],
    )
    assert validate_ina_query(data)


def test_definition_refuses_a_list_of_anything_but_objects():
    assert refusal(with_definition(dimensions=["ZREGION"])).startswith(
        "definition.dimensions.0: "
    )
    assert refusal({**good(), "definition": []}).startswith("definition: ")


# -- unknown keys and the refusal text ------------------------------------------


def test_an_unknown_key_is_refused_anywhere():
    for path in (
        (),
        ("definition",),
        ("definition", "dimensions", 0),
        ("definition", "measures", 0),
        ("definition", "variables", 0),
    ):
        data = good()
        target: Any = data
        for part in path:
            target = target[part]
        target["sortable"] = True
        where = ".".join(str(p) for p in (*path, "sortable"))
        assert refusal(data) == f"{where}: Extra inputs are not permitted"


def test_an_unknown_key_is_named_only_when_it_looks_like_a_field_name():
    data = good()
    data[f"https://user:{SECRET}@host/x"] = 1
    data[SECRET] = 1  # has a hyphen: no field name
    data["definition"]["measures"][1][f"{SECRET} y"] = 1
    data["definition"]["variables"][0]["Tenant_2"] = 1
    text = refusal(data)
    assert SECRET not in text and "host" not in text
    lines = text.split("; ")
    assert lines.count("<unknown field>: Extra inputs are not permitted") == 2
    assert "definition.measures.1.<unknown field>: Extra inputs are not permitted" in lines
    assert "definition.variables.0.Tenant_2: Extra inputs are not permitted" in lines


def test_a_refusal_never_echoes_an_input_value():
    data = {
        **good(),
        "name": f"bad {SECRET}",
        "title": SECRET * 10,
        "purpose": f"{SECRET}\nsecond line",
        "not_for": SECRET * 10,
        "destination": f"bad {SECRET}",
        "user_context": SECRET,
        "query": f"{SECRET}\x00",
        "package": SECRET * 5,
        "enabled": SECRET,
        "metadata_fetched_at": SECRET,
    }
    data["definition"]["dimensions"][0].update(name=f"{SECRET}\n", label=SECRET * 10)
    data["definition"]["dimensions"][1]["key_field"] = f"{SECRET}\u2028"
    data["definition"]["measures"][0].update(unit=SECRET * 2, label=f"{SECRET}\r")
    data["definition"]["variables"][0].update(type=SECRET, mandatory=SECRET)
    data["definition"]["variables"][1]["label"] = SECRET * 10
    text = refusal(data)
    assert SECRET not in text
    for loc in (
        "name: ",
        "title: ",
        "purpose: ",
        "not_for: ",
        "destination: ",
        "user_context: ",
        "query: ",
        "package: ",
        "enabled: ",
        "metadata_fetched_at: ",
        "definition.dimensions.0.name: ",
        "definition.dimensions.0.label: ",
        "definition.dimensions.1.key_field: ",
        "definition.measures.0.unit: ",
        "definition.measures.0.label: ",
        "definition.variables.0.type: ",
        "definition.variables.0.mandatory: ",
        "definition.variables.1.label: ",
    ):
        assert loc in text


def test_a_refusal_lists_at_most_20_problems():
    data = with_definition(dimensions=[{"name": ""} for _ in range(25)])
    lines = refusal(data).split("; ")
    assert len(lines) == 21 and lines[-1] == "and 5 more"


@pytest.mark.parametrize("data", [[GOOD], "x", None, 1, SECRET])
def test_anything_but_an_object_is_refused(data):
    assert refusal(data) == "ina_query: expected an object"


def test_the_gate_never_imports_the_database_module():
    # The gate is used by storage, the routes and the import script alike;
    # agents.db reads the database binding at import.
    code = (
        "import sys; import agents.ina.models; "
        "assert 'agents.db' not in sys.modules, sorted(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)
