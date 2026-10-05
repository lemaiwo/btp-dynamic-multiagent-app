"""The OData catalogue definition models and the save-time gate.

Section 1.2 of the OData services plan: what an admin may store as a
catalogue service, and that a refusal names the offending *name* without
ever echoing a value back.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _v in (
    "DESTINATION_CLIENT_ID",
    "DESTINATION_CLIENT_SECRET",
    "DESTINATION_URI",
    "DESTINATION_TOKEN_URL",
    "DESTINATION_UAA_URL",
    "CONNECTIVITY_CLIENT_ID",
    "CONNECTIVITY_CLIENT_SECRET",
    "CONNECTIVITY_TOKEN_URL",
    "CONNECTIVITY_PROXY_HOST",
    "CONNECTIVITY_PROXY_PORT",
    "CONNECTIVITY_PP_MODE",
):
    os.environ.pop(_v, None)

from agents.odata import BUILTIN_ODATA_URL  # noqa: E402
from agents.odata import models as odata_models  # noqa: E402
from agents.odata.models import (  # noqa: E402
    ODataServicePayload,
    ServiceDefinition,
    validate_odata_service,
)
from agents.odata.urls import confine_service_path  # noqa: E402

GOOD: dict[str, Any] = {
    "name": "purchase-requisitions",
    "title": "Purchase requisitions",
    "purpose": "Read requisitions and their items",
    "destination": "S4_ODATA_USER",
    "user_context": True,
    "odata_version": "v2",
    "service_path": "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
    "definition": {
        "entity_sets": [
            {
                "name": "A_PurchaseRequisitionItem",
                "title": "Requisition item",
                "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
                "operations": ["list", "get"],
                "fields": [
                    {"name": "PurchaseRequisition", "selectable": True, "filterable": True},
                    {"name": "PurchaseRequisitionItem", "selectable": True},
                    {
                        "name": "PurReqnReleaseStatus",
                        "label": "Release status",
                        "selectable": True,
                        "filterable": True,
                        "values": [{"value": "B", "meaning": "awaiting release"}],
                    },
                ],
            }
        ],
        "operations": [
            {
                "name": "ReleaseItem",
                "kind": "function_import",
                "http_method": "POST",
                "bound_to": "A_PurchaseRequisitionItem",
                "parameters": [{"name": "ReleaseCode"}],
            }
        ],
    },
}


def good() -> dict[str, Any]:
    return copy.deepcopy(GOOD)


def with_entity_set(**patch: Any) -> dict[str, Any]:
    data = good()
    data["definition"]["entity_sets"][0].update(patch)
    return data


def with_operation(**patch: Any) -> dict[str, Any]:
    data = good()
    data["definition"]["operations"][0].update(patch)
    return data


def with_bound_to(name: str) -> dict[str, Any]:
    return with_operation(bound_to=name)


def refused(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ODataServicePayload.model_validate(data)


def test_builtin_url():
    assert BUILTIN_ODATA_URL == "builtin:odata"


def test_good_payload_round_trips():
    p = ODataServicePayload.model_validate(GOOD)
    assert (
        p.definition.operations[0].enabled is False
        and p.definition.operations[0].changes_data is True
    )
    assert ODataServicePayload.model_validate(p.model_dump(mode="json")) == p


def test_payload_has_exactly_the_contract_fields():
    assert sorted(ODataServicePayload.model_fields) == [
        "definition",
        "destination",
        "enabled",
        "metadata_fetched_at",
        "name",
        "not_for",
        "odata_version",
        "purpose",
        "service_path",
        "title",
        "user_context",
    ]


def test_defaults():
    data = good()
    del data["user_context"]
    data["definition"] = {}
    p = ODataServicePayload.model_validate(data)
    assert p.enabled is True and p.user_context is False and p.not_for == ""
    assert p.definition == ServiceDefinition() and p.metadata_fetched_at is None
    field = ODataServicePayload.model_validate(GOOD).definition.entity_sets[0].fields[1]
    assert (field.type, field.filterable, field.writable, field.personal_data) == (
        "Edm.String",
        False,
        False,
        False,
    )


def test_title_and_purpose_are_stripped():
    p = ODataServicePayload.model_validate(
        {**good(), "title": "  Requisitions ", "purpose": " Read them\n"}
    )
    assert (p.title, p.purpose) == ("Requisitions", "Read them")


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"name": "Purchase Req"}, "name"),
        ({"purpose": "   "}, "purpose"),
        ({"purpose": "x" * 201}, "purpose"),
        ({"title": " "}, "title"),
        ({"title": "x" * 121}, "title"),
        ({"not_for": "x" * 201}, "not_for"),
        ({"name": "-leading"}, "name"),
        ({"name": "x" * 65}, "name"),
        ({"service_path": "https://evil.example/x"}, "service_path"),
        ({"service_path": "/sap/../etc"}, "service_path"),
        ({"service_path": "/sap/opu?x=1"}, "service_path"),
        ({"service_path": "sap/opu"}, "service_path"),
        ({"destination": "bad name"}, "destination"),
        ({"odata_version": "v3"}, "odata_version"),
        ({"secret": "x"}, "secret"),
    ],
)
def test_bad_top_level_values_are_refused(patch, message):
    refused({**good(), **patch}, message)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "sap/opu",
        "/sap/opu/",
        "/sap//opu",
        "//host/x",
        "/sap/../etc",
        "/sap/./x",
        "/sap/opu?x=1",
        "/sap/opu#frag",
        "/sap\\opu",
        "https://evil.example/x",
        "/x/http://evil.example",
        "/sap/o\x00pu",
        "/sap/o\npu",
        "/sap/o\x7fpu",
        "/sap/%2e%2e/etc",
        "/sap/o pu",
        "/" + "a" * 512,
    ],
)
def test_confine_service_path_refuses(path):
    with pytest.raises(ValueError) as err:
        confine_service_path(path)
    # A refusal never repeats what was typed (it may be a pasted secret).
    assert len(path) < 4 or path not in str(err.value)


def test_confine_service_path_accepts_real_shapes():
    for path in (
        "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
        "/sap/opu/odata/sap/ZSOME_SRV;v=0002",
        "/sap/opu/odata4/sap/api_x/srvd_a2x/sap/x/0001",
        "/" + "a" * 511,
    ):
        assert confine_service_path(path) == path
    with pytest.raises(ValueError):
        confine_service_path(None)  # type: ignore[arg-type]


def test_definition_rules():
    # rule 1: unique names
    data = good()
    data["definition"]["entity_sets"].append(copy.deepcopy(data["definition"]["entity_sets"][0]))
    refused(data, "duplicate entity set 'A_PurchaseRequisitionItem'")
    data = good()
    data["definition"]["entity_sets"][0]["fields"].append({"name": "PurReqnReleaseStatus"})
    refused(data, "duplicate field 'PurReqnReleaseStatus'")
    data = good()
    data["definition"]["operations"].append(copy.deepcopy(data["definition"]["operations"][0]))
    refused(data, "duplicate operation 'ReleaseItem'")
    nav = {"name": "to_Header", "target": "A_PurchaseRequisitionHeader", "collection": False}
    refused(with_entity_set(navigations=[nav, dict(nav)]), "duplicate navigation 'to_Header'")
    # rule 2: keys
    refused(with_entity_set(keys=[{"name": "Nope"}]), "key 'Nope'")
    for op in ("get", "update", "delete"):
        fields = [{"name": "PurchaseRequisition", "selectable": True, "writable": True}]
        refused(
            with_entity_set(keys=[], operations=[op], fields=fields),
            f"'A_PurchaseRequisitionItem'.*'{op}'.*key",
        )
    # rule 3: list/get need something to select
    for op in ("list", "get"):
        refused(
            with_entity_set(
                operations=[op],
                fields=[{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
            ),
            f"'A_PurchaseRequisitionItem'.*'{op}'.*selectable",
        )
    # rule 4: create/update need something to write
    for op in ("create", "update"):
        refused(with_entity_set(operations=[op]), f"'A_PurchaseRequisitionItem'.*'{op}'.*writable")
    # rule 5: bound_to
    refused(with_bound_to("A_Missing"), "bound_to 'A_Missing'")
    # rule 6: kinds per protocol version
    refused(with_operation(kind="action"), "'ReleaseItem'.*'action'.*v2")
    v4 = {**good(), "odata_version": "v4"}
    refused(v4, "'ReleaseItem'.*'function_import'.*v4")
    v4_op = {"name": "Release", "qualified_name": "com.example.Release", "bound_to": None}
    refused(_v4(**v4_op, kind="function", http_method="POST"), "'Release'.*GET")
    refused(_v4(**v4_op, kind="action", http_method="GET"), "'Release'.*POST")
    refused(
        _v4(**{**v4_op, "qualified_name": ""}, kind="action", http_method="POST"),
        "'Release'.*qualified_name",
    )
    assert ODataServicePayload.model_validate(_v4(**v4_op, kind="action", http_method="POST"))
    assert ODataServicePayload.model_validate(_v4(**v4_op, kind="function", http_method="GET"))


def _v4(**operation: Any) -> dict[str, Any]:
    data = good()
    data["odata_version"] = "v4"
    data["definition"]["operations"] = [operation]
    return data


def test_a_write_only_entity_set_needs_no_selectable_field():
    data = with_entity_set(
        operations=["create", "delete"],
        fields=[
            {"name": "PurchaseRequisition", "writable": True},
            {"name": "PurchaseRequisitionItem", "writable": True},
        ],
    )
    p = ODataServicePayload.model_validate(data)
    assert p.definition.entity_sets[0].has_write() and p.has_write()


def test_entity_set_path_is_one_confined_segment():
    assert (
        ODataServicePayload.model_validate(with_entity_set(path="A_Item"))
        .definition.entity_sets[0]
        .path
        == "A_Item"
    )
    for bad in ("a/b", "..", "A_Item?x=1", "/A_Item", "A Item", "A_Item#x", "a\\b"):
        refused(with_entity_set(path=bad), "path")


def test_unknown_key_anywhere_is_refused():
    data = good()
    data["definition"]["entity_sets"][0]["fields"][0]["sortable"] = True
    refused(data, "sortable")
    refused(with_entity_set(tenant="x"), "tenant")
    refused(with_operation(password="x"), "password")
    data = good()
    data["definition"]["views"] = []
    refused(data, "views")
    data = good()
    data["definition"]["entity_sets"][0]["keys"][0]["nullable"] = False
    refused(data, "nullable")
    data = good()
    data["definition"]["operations"][0]["parameters"][0]["default"] = "x"
    refused(data, "default")


def test_list_caps(monkeypatch):
    data = good()
    data["definition"]["entity_sets"][0]["fields"] = [
        {"name": f"F{i}", "selectable": True} for i in range(odata_models.MAX_FIELDS + 1)
    ]
    data["definition"]["entity_sets"][0]["keys"] = [{"name": "F0"}]
    refused(data, "fields")
    data = good()
    data["definition"]["operations"] = [
        {"name": f"Op{i}", "kind": "function_import", "http_method": "GET"}
        for i in range(odata_models.MAX_OPERATIONS + 1)
    ]
    refused(data, "operations")


def test_definition_size_cap(monkeypatch):
    assert odata_models.MAX_DEFINITION_BYTES == 2_000_000
    monkeypatch.setattr(odata_models, "MAX_DEFINITION_BYTES", 500)
    refused(good(), "500 bytes")
    small = good()
    small["definition"] = {"entity_sets": [], "operations": []}
    assert ODataServicePayload.model_validate(small)


def test_helpers():
    p = ODataServicePayload.model_validate(GOOD)
    es = p.definition.entity_set("A_PurchaseRequisitionItem")
    assert es is not None and p.definition.entity_set("Nope") is None
    assert es.field("PurReqnReleaseStatus").label == "Release status" and es.field("Nope") is None
    assert es.selectable_names() == [
        "PurchaseRequisition",
        "PurchaseRequisitionItem",
        "PurReqnReleaseStatus",
    ]
    assert es.has_write() is False
    assert (
        p.definition.operation("ReleaseItem").kind == "function_import"
        and p.definition.operation("Nope") is None
    )
    # The one operation changes data but is not enabled, so nothing writes yet.
    assert p.has_write() is False
    assert ODataServicePayload.model_validate(with_operation(enabled=True)).has_write() is True
    assert (
        ODataServicePayload.model_validate(
            with_operation(enabled=True, changes_data=False)
        ).has_write()
        is False
    )
    writable = with_entity_set(operations=["list", "update"])
    writable["definition"]["entity_sets"][0]["fields"][2]["writable"] = True
    assert ODataServicePayload.model_validate(writable).has_write() is True


def test_validate_odata_service_returns_the_clean_dict_or_raises_value_error():
    clean = validate_odata_service(GOOD)
    assert clean["definition"]["entity_sets"][0]["path"] == ""
    assert clean["metadata_fetched_at"] is None and clean["enabled"] is True
    with pytest.raises(ValueError, match="A_Missing"):
        validate_odata_service(with_bound_to("A_Missing"))
    with pytest.raises(ValueError) as err:
        validate_odata_service(with_bound_to("A_Missing"))
    assert not isinstance(err.value, ValidationError)


def test_validate_odata_service_never_echoes_an_input_value():
    secret = "hunter2-pasted-by-mistake"
    data = {
        **good(),
        "service_path": f"/sap/opu?token={secret}",
        "destination": f"bad {secret}",
        "odata_version": secret,
        "title": secret * 10,
        "metadata_fetched_at": secret,
    }
    data["definition"]["entity_sets"][0]["fields"][0]["name"] = f"{secret} x"
    data["definition"]["entity_sets"][0]["path"] = f"{secret}/x"
    with pytest.raises(ValueError) as err:
        validate_odata_service(data)
    text = str(err.value)
    assert secret not in text
    for loc in (
        "service_path: ",
        "destination: ",
        "odata_version: ",
        "title: ",
        "metadata_fetched_at: ",
        "definition.entity_sets.0.fields.0.name: ",
        "definition.entity_sets.0.path: ",
    ):
        assert loc in text
    with pytest.raises(ValueError, match="expected an object"):
        validate_odata_service([secret])  # type: ignore[arg-type]


def test_an_unknown_key_is_named_only_when_it_looks_like_a_field_name():
    """A refused key is the client's own text. A value pasted where a key
    belongs (a URL, a token) must not come back, not even its first
    characters; a mistyped field name still should, so it can be fixed."""
    secret = "hunter2-pasted-by-mistake"
    data = good()
    data["sortable"] = True
    data[f"https://user:{secret}@host/x"] = 1
    data[secret] = 1  # has a hyphen: no identifier
    data["x" * 65] = 1
    data[""] = 1
    data["definition"]["entity_sets"][0][f"{secret} y"] = 1
    data["definition"]["entity_sets"][0]["fields"][0]["Tenant_2"] = 1
    with pytest.raises(ValueError) as err:
        validate_odata_service(data)
    text = str(err.value)
    assert secret not in text and "host" not in text and "xxx" not in text
    lines = text.split("; ")
    assert "sortable: Extra inputs are not permitted" in lines
    assert lines.count("<unknown field>: Extra inputs are not permitted") == 4
    assert "definition.entity_sets.0.<unknown field>: Extra inputs are not permitted" in lines
    assert (
        "definition.entity_sets.0.fields.0.Tenant_2: Extra inputs are not permitted" in lines
    )


def test_a_trailing_newline_is_not_part_of_a_name():
    # Python's `$` also matches before a final "\n"; these values end up in
    # request URLs, so the newline must be refused, not carried along.
    refused(with_entity_set(path="A_Item\n"), "path")
    refused(with_operation(qualified_name="ns.Op\n"), "qualified_name")
    refused(with_operation(bound_to="A_PurchaseRequisitionItem\n"), "bound_to")
    refused(with_entity_set(name="A_PurchaseRequisitionItem\n"), "name")
    refused({**good(), "name": "purchase-requisitions\n"}, "name")
    refused({**good(), "destination": "S4_ODATA_USER\n"}, "destination")
    data = good()
    data["definition"]["entity_sets"][0]["keys"][0]["name"] = "PurchaseRequisition\n"
    refused(data, "keys")
    assert ODataServicePayload.model_validate(with_operation(qualified_name="ns.Op"))


def test_example_query_lengths():
    def with_example(**patch: Any) -> dict[str, Any]:
        return with_entity_set(examples=[{"description": "Open items", **patch}])

    ok = with_example(select=["PurchaseRequisition", "x" * 128], orderby="x" * 300)
    assert ODataServicePayload.model_validate(ok)
    refused(with_example(select=["x" * 129]), "select")
    refused(with_example(orderby="x" * 301), "orderby")


def test_a_filterable_field_must_be_selectable():
    # The search tool lists only selectable fields; filtering on a field the
    # agent may not read would let it probe the hidden values.
    def with_field(**flags: bool) -> dict[str, Any]:
        data = good()
        data["definition"]["entity_sets"][0]["fields"].append({"name": "CreatedByUser", **flags})
        return data

    refused(with_field(filterable=True, selectable=False), "field 'CreatedByUser' is filterable")
    refused(with_field(filterable=True), "field 'CreatedByUser' is filterable")
    assert ODataServicePayload.model_validate(with_field(filterable=True, selectable=True))
    assert ODataServicePayload.model_validate(with_field(selectable=True, filterable=False))
    assert ODataServicePayload.model_validate(with_field(writable=True))
    with pytest.raises(ValueError, match="definition.entity_sets.0.fields.3: .*'CreatedByUser'"):
        validate_odata_service(with_field(filterable=True))


def test_definition_is_required():
    # A PUT without the key must not replace the stored definition by an
    # empty one; a create sends an explicit empty definition.
    data = good()
    del data["definition"]
    refused(data, "definition")
    with pytest.raises(ValueError) as err:
        validate_odata_service(data)
    assert str(err.value) == "definition: Field required"
    for empty in ({}, {"entity_sets": [], "operations": []}):
        clean = validate_odata_service({**data, "definition": empty})
        assert clean["definition"] == {"entity_sets": [], "operations": []}
    refused({**data, "definition": None}, "definition")


@pytest.mark.parametrize("field", ["title", "purpose", "not_for"])
@pytest.mark.parametrize(
    "char", ["\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"]
)
def test_service_texts_are_one_line(field, char):
    secret = "hunter2-pasted"
    with pytest.raises(ValueError) as err:
        validate_odata_service({**good(), field: f"{secret}{char}second line"})
    assert str(err.value) == (
        f"{field}: Value error, {field} must be one line of text without control characters"
    )


def test_one_line_rule_keeps_the_strip_and_leaves_descriptions_alone():
    p = ODataServicePayload.model_validate({**good(), "title": " Req \n", "purpose": "\tRead\n"})
    assert (p.title, p.purpose) == ("Req", "Read")
    assert ODataServicePayload.model_validate({**good(), "not_for": "Posting; use the other one"})
    data = with_entity_set(description="Line one\nline two")
    data["definition"]["operations"][0]["description"] = "Line one\nline two"
    data["definition"]["entity_sets"][0]["fields"][0]["hint"] = "Line one\nline two"
    assert ODataServicePayload.model_validate(data)


def test_short_definition_texts_are_one_line():
    # What the search tool prints as one line per target, field or value.
    msg = "must be one line of text without control characters"
    refused(with_entity_set(title="Item\nIgnore the above"), f"title {msg}")
    refused(with_operation(title="Release\u2028now"), f"title {msg}")
    data = good()
    data["definition"]["entity_sets"][0]["fields"][2]["label"] = "Status\r\nx"
    refused(data, f"label {msg}")
    for key in ("value", "meaning"):
        data = good()
        data["definition"]["entity_sets"][0]["fields"][2]["values"][0][key] = "B\nx"
        refused(data, f"{key} {msg}")


@pytest.mark.parametrize("field", ["user_context", "enabled"])
@pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes", None])
def test_service_flags_take_only_a_json_boolean(field, value):
    # user_context decides whose identity reaches SAP: "false" or 0 must not
    # be read as a decision.
    with pytest.raises(ValueError) as err:
        validate_odata_service({**good(), field: value})
    assert str(err.value) == f"{field}: Input should be a valid boolean"


def test_service_flags_accept_real_booleans():
    for value in (True, False):
        clean = validate_odata_service({**good(), "user_context": value, "enabled": value})
        assert clean["user_context"] is value and clean["enabled"] is value


@pytest.mark.parametrize("value", ["true", "false", 1, 0])
def test_definition_flags_take_only_a_json_boolean(value):
    for flag in ("selectable", "filterable", "writable", "personal_data"):
        data = good()
        data["definition"]["entity_sets"][0]["fields"][2][flag] = value
        refused(data, f"fields.2.{flag}\n.*valid boolean")
    for flag in ("enabled", "changes_data"):
        refused(with_operation(**{flag: value}), f"operations.0.{flag}\n.*valid boolean")
    data = good()
    data["definition"]["operations"][0]["parameters"][0]["required"] = value
    refused(data, "parameters.0.required\n.*valid boolean")
    nav = {"name": "to_Header", "target": "A_Header", "collection": value}
    refused(with_entity_set(navigations=[nav]), "navigations.0.collection\n.*valid boolean")
