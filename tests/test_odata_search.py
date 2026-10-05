"""`search_operations`: what a model can find in the attached catalogue.

The catalogue is the only description of SAP the model gets, so these tests
pin two things: the ranking finds a target by the words an admin wrote *and*
by its technical name, and nothing the catalogue (or a missing `allow_write`)
switches off is ever shown.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

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
from agents.odata.models import validate_odata_service  # noqa: E402
from agents.odata.search import (  # noqa: E402
    MAX_FULL_TARGETS,
    MAX_SUMMARY_MATCHES,
)
from agents.odata.search import search_catalogue as _search_as_shipped  # noqa: E402
from agents.odata.tools import odata_toolset  # noqa: E402

# The ranking and shape tests below describe the catalogue search with
# operations listed (`allow_call`), as the tool will run it once operations
# can be called. The shipped default, and what the tool passes today, is
# off: `test_no_operation_is_listed_unless_the_caller_can_call_them`.
search_catalogue = functools.partial(_search_as_shipped, allow_call=True)

ITEM = "A_PurchaseRequisitionItem"


def _field(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, **kw}


def _service(**kw: Any) -> dict[str, Any]:
    """A service the way the registry snapshot holds it (`to_dict()`)."""
    clean = validate_odata_service(kw)
    return {**clean, "id": 1, "counts": {}, "has_write": True, "used_by": []}


PURCHASE_REQUISITIONS = _service(
    name="purchase-requisitions",
    title="Purchase requisitions",
    purpose="Read purchase requisitions and their items",
    not_for="Purchase orders",
    destination="S4_ODATA_USER",
    user_context=True,
    odata_version="v2",
    service_path="/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
    definition={
        "entity_sets": [
            {
                "name": "A_PurchaseRequisitionHeader",
                "title": "Purchase requisition",
                "description": "One document per request.",
                "keys": [{"name": "PurchaseRequisition"}],
                "operations": ["list", "get"],
                "fields": [
                    _field("PurchaseRequisition", label="Number", selectable=True),
                    _field("PurReqnDescription", label="Text", selectable=True),
                ],
                "navigations": [
                    {"name": "to_PurchaseReqnItem", "target": ITEM, "collection": True},
                    {"name": "to_Hidden", "target": "A_Hidden", "collection": True},
                ],
            },
            {
                "name": ITEM,
                "title": "Purchase requisition item",
                "description": "Items of a requisition, including those waiting for approval.",
                "keys": [
                    {"name": "PurchaseRequisition"},
                    {"name": "PurchaseRequisitionItem"},
                ],
                "operations": ["list", "get", "update"],
                "fields": [
                    _field("PurchaseRequisition", label="Number", selectable=True, filterable=True),
                    _field("PurchaseRequisitionItem", label="Item", selectable=True),
                    _field(
                        "PurReqnReleaseStatus",
                        label="Release status",
                        selectable=True,
                        filterable=True,
                        hint="Filter on B for the open ones",
                        values=[
                            {"value": "B", "meaning": "awaiting release"},
                            {"value": "R", "meaning": "released"},
                        ],
                    ),
                    _field("RequestedQuantity", label="Quantity", writable=True),
                    _field(
                        "CreatedByUser",
                        label="Created by",
                        personal_data=True,
                    ),
                ],
                "examples": [
                    {
                        "description": "Items awaiting release",
                        "filter": "PurReqnReleaseStatus eq 'B'",
                        "select": ["PurchaseRequisition", "PurchaseRequisitionItem"],
                        "top": 20,
                    },
                    # The next four name a field no agent may see.
                    {
                        "description": "Hidden field in select",
                        "select": ["PurchaseRequisition", "CreatedByUser", "RequestedQuantity"],
                    },
                    {
                        "description": "Hidden field in filter",
                        "filter": "createdbyuser eq 'X' and PurReqnReleaseStatus eq 'B'",
                    },
                    {
                        "description": "Hidden field in orderby",
                        "orderby": "CreatedByUser desc",
                    },
                    {"description": "Sorted by CreatedByUser", "top": 5},
                    {
                        "description": "Quantity filter",
                        "filter": "RequestedQuantity gt 10",
                    },
                ],
            },
            {
                # In the catalogue, but with nothing switched on.
                "name": "A_Hidden",
                "title": "Hidden requisition archive",
                "keys": [{"name": "Id"}],
                "operations": [],
                "fields": [_field("Id", selectable=True)],
            },
            {
                # Only a write: exists for the model only with allow_write.
                "name": "A_WriteOnly",
                "title": "Requisition note",
                "keys": [{"name": "Id"}],
                "operations": ["create"],
                "fields": [_field("Id"), _field("Note", writable=True)],
            },
        ],
        "operations": [
            {
                "name": "Release",
                "title": "Release an item",
                "kind": "function_import",
                "http_method": "POST",
                "bound_to": ITEM,
                "parameters": [{"name": "PurchaseRequisition"}, {"name": "ReleaseCode"}],
                "enabled": True,
                "changes_data": True,
            },
            {
                "name": "GetReleaseStrategy",
                "title": "Release strategy lookup",
                "kind": "function_import",
                "http_method": "GET",
                "parameters": [{"name": "PurchaseRequisition"}],
                "enabled": False,
                "changes_data": False,
            },
            {
                "name": "CountOpen",
                "title": "Count open documents",
                "kind": "function_import",
                "http_method": "GET",
                "enabled": True,
                "changes_data": False,
            },
        ],
    },
)

SALES_ORDERS = _service(
    name="sales-orders",
    title="Sales orders",
    purpose="Read sales orders",
    destination="S4_ODATA_TECH",
    odata_version="v2",
    service_path="/sap/opu/odata/sap/API_SALES_ORDER_SRV",
    definition={
        "entity_sets": [
            {
                "name": "A_SalesOrder",
                "title": "Sales order",
                "keys": [{"name": "SalesOrder"}],
                "operations": ["list"],
                "fields": [_field("SalesOrder", label="Order", selectable=True)],
            }
        ]
    },
)

SERVICES = [PURCHASE_REQUISITIONS, SALES_ORDERS]
SNAPSHOT = {s["name"]: s for s in SERVICES}


def _many(count: int) -> dict[str, Any]:
    return _service(
        name="many-things",
        title="Many things",
        purpose="A wide service",
        destination="S4_ODATA_TECH",
        odata_version="v2",
        service_path="/sap/opu/odata/sap/Z_MANY_SRV",
        definition={
            "entity_sets": [
                {
                    "name": f"Thing{i:02d}",
                    "title": f"Thing {i:02d}",
                    "keys": [{"name": "Id"}],
                    "operations": ["list"],
                    "fields": [_field("Id", selectable=True)],
                }
                for i in range(count)
            ]
        },
    )


def fake(_destination: str) -> Any:
    """A resolver factory the factory must not call while it only builds."""
    raise AssertionError("no destination is resolved before a tool runs")


def _targets(out: dict[str, Any]) -> list[str]:
    return [m["target"] for m in out["matches"]]


# -- ranking -----------------------------------------------------------------


def test_summary_matches_on_title_description_label_and_technical_name():
    for q in (
        "requisition",
        "release status",
        "A_PurchaseRequisitionItem",
        "PurReqnReleaseStatus",
        "approval",
    ):
        assert search_catalogue(SERVICES, q)["matches"][0]["target"] == ITEM, q


def test_summary_match_shape():
    out = search_catalogue(SERVICES, "approval")
    assert out["matches"][0] == {
        "service": "purchase-requisitions",
        "service_title": "Purchase requisitions",
        "target": ITEM,
        "kind": "entity_set",
        "title": "Purchase requisition item",
        "description": "Items of a requisition, including those waiting for approval.",
        "operations": ["list", "get"],
    }
    assert out["total"] == len(out["matches"])


def test_a_service_level_hit_ranks_below_a_target_hit_and_ties_sort_by_name():
    out = search_catalogue(SERVICES, "orders")
    # "Purchase orders" is only in not_for, which is not searched.
    assert _targets(out) == ["A_SalesOrder"]
    out = search_catalogue(SERVICES, "purchase")
    assert set(_targets(out)[:2]) == {"A_PurchaseRequisitionHeader", ITEM}
    assert _targets(out)[2:] == ["CountOpen"]  # service title and purpose only
    # Equal scores: by target name.
    assert _targets(search_catalogue([_many(4)], "thing")) == [f"Thing{i:02d}" for i in range(4)]


def test_no_match_returns_an_empty_list_with_a_hint():
    out = search_catalogue(SERVICES, "zzz-nothing")
    assert out["matches"] == [] and out["total"] == 0 and out["hint"]


def test_a_hidden_field_does_not_make_its_entity_set_match():
    assert search_catalogue(SERVICES, "CreatedByUser")["matches"] == []
    assert search_catalogue(SERVICES, "RequestedQuantity")["matches"] == []
    assert _targets(search_catalogue(SERVICES, "RequestedQuantity", allow_write=True)) == [ITEM]


# -- visibility --------------------------------------------------------------


def test_only_enabled_operations_are_listed_and_writes_need_allow_write():
    m = search_catalogue(SERVICES, "requisition item")["matches"][0]
    assert m["operations"] == ["list", "get"]
    m = search_catalogue(SERVICES, "requisition item", allow_write=True)["matches"][0]
    assert m["operations"] == ["list", "get", "update"]
    # changes_data op hidden
    assert not [
        x
        for x in search_catalogue(SERVICES, "release")["matches"]
        if x["kind"] == "operation"
    ]
    ops = [
        x
        for x in search_catalogue(SERVICES, "release", allow_write=True, allow_call=True)[
            "matches"
        ]
        if x["kind"] == "operation"
    ]
    assert [x["target"] for x in ops] == ["Release"]  # the disabled one stays hidden
    assert ops[0]["operations"] == ["call"]


def test_a_read_only_operation_is_visible_without_allow_write():
    assert "CountOpen" in _targets(search_catalogue(SERVICES, "count open", allow_call=True))


def test_no_operation_is_listed_unless_the_caller_can_call_them():
    """`allow_call` is off by default: what execute cannot run is not offered."""
    for allow_write in (False, True):
        for query in ("", "release", "count open", "Release", "CountOpen"):
            out = _search_as_shipped(SERVICES, query, allow_write=allow_write)
            assert not [m for m in out["matches"] if m["kind"] == "operation"], query
            full = _search_as_shipped(SERVICES, query, detail="full", allow_write=allow_write)
            assert not [m for m in full["matches"] if m["kind"] == "operation"], query


def test_writes_are_listed_only_for_a_version_that_can_be_written():
    v4 = {**PURCHASE_REQUISITIONS, "odata_version": "v4"}
    for versions, expected in (
        (None, True), (("v2", "v4"), True), (("v2",), False), ((), False),
    ):
        out = search_catalogue(
            [v4], ITEM, detail="full", allow_write=True, write_versions=versions
        )
        item = next(m for m in out["matches"] if m["target"] == ITEM)
        assert ("update" in item["operations"]) is expected, versions
        assert any(f["writable"] for f in item["fields"]) is expected, versions
        # A field that is only writable does not exist for a caller that cannot write.
        hit = search_catalogue([v4], "RequestedQuantity", allow_write=True, write_versions=versions)
        assert bool(hit["matches"]) is expected, versions
    assert "A_WriteOnly" not in _targets(
        search_catalogue([v4], "", allow_write=True, write_versions=("v2",))
    )


def test_entity_set_without_any_enabled_operation_is_invisible():
    for allow_write in (False, True):
        for q in ("hidden", "A_Hidden", "archive", ""):
            out = search_catalogue(SERVICES, q, allow_write=allow_write)
            assert "A_Hidden" not in _targets(out), (q, allow_write)
    # A write-only entity set is invisible until the entry allows writes.
    assert "A_WriteOnly" not in _targets(search_catalogue(SERVICES, "note"))
    assert "A_WriteOnly" in _targets(search_catalogue(SERVICES, "note", allow_write=True))


def test_a_disabled_service_is_invisible():
    disabled = {**SALES_ORDERS, "enabled": False}
    assert search_catalogue([PURCHASE_REQUISITIONS, disabled], "sales")["matches"] == []
    out = search_catalogue([disabled], "", service="sales-orders")
    assert out["error"]["code"] == "service_disabled"


# -- full detail -------------------------------------------------------------


def test_full_detail_lists_selectable_fields_value_meanings_and_examples_and_is_capped():
    out = search_catalogue(SERVICES, "requisition", detail="full")
    assert len(out["matches"]) <= 5
    first = out["matches"][0]
    names = [f["name"] for f in first["fields"]]
    assert "CreatedByUser" not in names and "RequestedQuantity" not in names
    status = next(f for f in first["fields"] if f["name"] == "PurReqnReleaseStatus")
    assert {"value": "B", "meaning": "awaiting release"} in status["values"]
    assert status == {
        "name": "PurReqnReleaseStatus",
        "type": "Edm.String",
        "label": "Release status",
        "filterable": True,
        "writable": False,
        "hint": "Filter on B for the open ones",
        "values": [
            {"value": "B", "meaning": "awaiting release"},
            {"value": "R", "meaning": "released"},
        ],
    }
    assert first["keys"] == [
        {"name": "PurchaseRequisition", "type": "Edm.String"},
        {"name": "PurchaseRequisitionItem", "type": "Edm.String"},
    ]
    assert first["examples"][0]["filter"] == "PurReqnReleaseStatus eq 'B'"
    assert first["purpose"] == "Read purchase requisitions and their items"
    assert first["not_for"] == "Purchase orders"
    assert first["runs_as"] == "signed-in user"

    wide = search_catalogue([_many(12)], "thing", detail="full")
    assert len(wide["matches"]) == MAX_FULL_TARGETS and wide["total"] == 12
    assert wide["hint"]
    assert wide["matches"][0]["runs_as"] == "technical user"


def test_full_detail_shows_writable_fields_only_with_allow_write():
    out = search_catalogue(SERVICES, ITEM, detail="full", allow_write=True)
    quantity = next(f for f in out["matches"][0]["fields"] if f["name"] == "RequestedQuantity")
    assert quantity["writable"] is True
    assert "CreatedByUser" not in [f["name"] for f in out["matches"][0]["fields"]]


def test_a_navigation_is_listed_only_when_the_parent_has_get():
    """Following a navigation reads through one parent entity, so the execute
    tool asks for 'get' on the parent; search must not offer what it refuses."""
    definition = json.loads(json.dumps(PURCHASE_REQUISITIONS["definition"]))
    header = definition["entity_sets"][0]
    assert header["name"] == "A_PurchaseRequisitionHeader"
    for operations, expected in ((["list"], []), (["get"], ["to_PurchaseReqnItem"])):
        header["operations"] = operations
        service = {**PURCHASE_REQUISITIONS, "definition": definition}
        out = search_catalogue([service], "A_PurchaseRequisitionHeader", detail="full")
        found = next(m for m in out["matches"] if m["target"] == "A_PurchaseRequisitionHeader")
        assert [n["name"] for n in found["navigations"]] == expected, operations


def test_full_detail_lists_only_navigations_the_model_can_follow():
    out = search_catalogue(SERVICES, "A_PurchaseRequisitionHeader", detail="full")
    header = out["matches"][0]
    assert header["target"] == "A_PurchaseRequisitionHeader"
    assert header["navigations"] == [
        {"name": "to_PurchaseReqnItem", "target": ITEM, "collection": True, "description": ""}
    ]


def test_full_detail_of_an_operation_lists_its_parameters():
    out = search_catalogue(SERVICES, "Release an item", detail="full", allow_write=True)
    op = next(m for m in out["matches"] if m["kind"] == "operation")
    assert op["bound_to"] == ITEM and op["changes_data"] is True
    assert op["parameters"] == [
        {"name": "PurchaseRequisition", "type": "Edm.String", "required": True},
        {"name": "ReleaseCode", "type": "Edm.String", "required": True},
    ]


def test_an_example_never_names_a_hidden_field():
    out = search_catalogue(SERVICES, ITEM, detail="full")
    examples = out["matches"][0]["examples"]
    assert "CreatedByUser".casefold() not in json.dumps(out).casefold()
    assert "RequestedQuantity" not in json.dumps(out)  # writable only: hidden without writes
    assert [e["description"] for e in examples] == [
        "Items awaiting release",
        "Hidden field in select",
    ]
    # A select keeps the names the agent may select; the example survives.
    assert examples[1] == {
        "description": "Hidden field in select",
        "select": ["PurchaseRequisition"],
    }

    out = search_catalogue(SERVICES, ITEM, detail="full", allow_write=True)
    examples = out["matches"][0]["examples"]
    assert "CreatedByUser".casefold() not in json.dumps(out).casefold()
    # With writes, the writable field is visible, so its example is too --
    # but it is still not selectable, so it stays out of a `select`.
    assert [e["description"] for e in examples] == [
        "Items awaiting release",
        "Hidden field in select",
        "Quantity filter",
    ]
    assert examples[1]["select"] == ["PurchaseRequisition"]


def test_a_non_selectable_key_is_named_in_keys_but_not_in_fields():
    # Key names are always visible: the agent needs them to address one
    # entity. Key *values* come back only for a selectable key field.
    out = search_catalogue(SERVICES, "A_WriteOnly", detail="full", allow_write=True)
    match = out["matches"][0]
    assert match["target"] == "A_WriteOnly"
    assert match["keys"] == [{"name": "Id", "type": "Edm.String"}]
    assert [f["name"] for f in match["fields"]] == ["Note"]


def _bound_service() -> dict[str, Any]:
    def entity_set(name: str, operations: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "keys": [{"name": "Id"}],
            "operations": operations,
            "fields": [_field("Id", selectable=True), _field("Note", writable=True)],
        }

    def operation(name: str, bound_to: str | None) -> dict[str, Any]:
        return {
            "name": name,
            "kind": "function_import",
            "http_method": "GET",
            "bound_to": bound_to,
            "enabled": True,
            "changes_data": False,
        }

    return _service(
        name="bound",
        title="Bound",
        purpose="Bound operations",
        destination="S4_ODATA_TECH",
        odata_version="v2",
        service_path="/sap/opu/odata/sap/Z_BOUND_SRV",
        definition={
            "entity_sets": [
                entity_set("Open", ["list"]),
                entity_set("Closed", []),
                entity_set("WriteOnly", ["update"]),
            ],
            "operations": [
                operation("OnOpen", "Open"),
                operation("OnClosed", "Closed"),
                operation("OnWriteOnly", "WriteOnly"),
                operation("Unbound", None),
            ],
        },
    )


def test_bound_to_names_only_an_entity_set_the_agent_can_see():
    def bound(allow_write: bool) -> dict[str, Any]:
        out = search_catalogue(
            [_bound_service()], "", detail="summary", allow_write=allow_write
        )
        names = [m["target"] for m in out["matches"] if m["kind"] == "operation"]
        found = {}
        for name in names:
            full = search_catalogue(
                [_bound_service()], name, detail="full", allow_write=allow_write
            )
            found[name] = next(m for m in full["matches"] if m["target"] == name)["bound_to"]
        return found

    assert bound(False) == {
        "OnOpen": "Open",
        "OnClosed": None,
        "OnWriteOnly": None,
        "Unbound": None,
    }
    assert bound(True)["OnWriteOnly"] == "WriteOnly"
    assert "Closed" not in json.dumps(
        search_catalogue([_bound_service()], "OnClosed", detail="full", allow_write=True)
    ).replace("OnClosed", "")


FULL_KEYS = {
    "service", "service_title", "target", "kind", "title", "description", "operations",
    "keys", "fields", "navigations", "parameters", "examples", "bound_to", "changes_data",
    "purpose", "not_for", "runs_as",
}  # fmt: skip


def test_every_full_match_has_the_same_shape():
    out = search_catalogue(
        SERVICES, "", detail="full", allow_write=True, service="purchase-requisitions"
    )
    kinds = {m["kind"] for m in out["matches"]}
    assert kinds == {"entity_set", "operation"}
    for match in out["matches"]:
        assert set(match) == FULL_KEYS, match["target"]
        for key in ("keys", "fields", "navigations", "parameters", "examples"):
            assert isinstance(match[key], list), (match["target"], key)
        if match["kind"] == "entity_set":
            assert match["parameters"] == []
            assert match["bound_to"] is None and match["changes_data"] is None
        else:
            assert match["keys"] == match["fields"] == match["navigations"] == []
            assert match["examples"] == [] and isinstance(match["changes_data"], bool)
    for match in search_catalogue(SERVICES, "", allow_write=True)["matches"]:
        assert set(match) == FULL_KEYS - {
            "keys", "fields", "navigations", "parameters", "examples", "bound_to",
            "changes_data", "purpose", "not_for", "runs_as",
        }  # fmt: skip


def test_detail_is_built_only_for_the_matches_that_are_returned(monkeypatch):
    from agents.odata import search

    built: list[str] = []
    real = search._entity_set_detail

    def counting(entity_set, *args, **kwargs):
        built.append(entity_set["name"])
        return real(entity_set, *args, **kwargs)

    monkeypatch.setattr(search, "_entity_set_detail", counting)
    out = search_catalogue([_many(25)], "thing", detail="full")
    assert out["total"] == 25
    assert built == [m["target"] for m in out["matches"]] and len(built) == MAX_FULL_TARGETS
    built.clear()
    search_catalogue([_many(25)], "thing")
    assert built == []


def test_short_query_tokens():
    # One character says nothing: ignored.
    assert search_catalogue(SERVICES, "a")["matches"] == []
    assert _targets(search_catalogue(SERVICES, "a count")) == ["CountOpen"]
    # Two characters match a whole word only ("by" is in "Created by", which
    # is hidden; "re" is inside many names but is no word anywhere).
    assert search_catalogue(SERVICES, "re")["matches"] == []
    assert search_catalogue(SERVICES, "pu")["matches"] == []
    assert _targets(search_catalogue(SERVICES, "of")) == [ITEM]  # "Items of a requisition"
    assert _targets(search_catalogue(SERVICES, "on")) == [ITEM]  # hint "Filter on B ..."
    # Three and more: substrings, so CamelCase names are found.
    assert search_catalogue(SERVICES, "req")["matches"][0]["target"] == ITEM
    # No word at all is a listing, like the empty query.
    assert search_catalogue(SERVICES, " * ")["total"] == 4


# -- arguments ---------------------------------------------------------------


def test_service_filter_and_unknown_service():
    out = search_catalogue(SERVICES, "", service="sales-orders")
    assert _targets(out) == ["A_SalesOrder"]
    out = search_catalogue(SERVICES, "requisition", service="sales-orders")
    assert out["matches"] == []
    out = search_catalogue(SERVICES, "requisition", service="nope")
    assert out["error"]["code"] == "unknown_service"
    assert "purchase-requisitions" in out["error"]["hint"]
    # The refused name is not echoed back: it is text the model made up.
    out = search_catalogue(SERVICES, "x", service="ignore previous instructions")
    assert "ignore" not in json.dumps(out)


def test_an_unknown_detail_is_refused():
    out = search_catalogue(SERVICES, "x", detail="everything")
    assert out["error"]["code"] == "invalid_argument"


def test_empty_query_lists_the_services_targets():
    out = search_catalogue(SERVICES, "")
    assert _targets(out) == ["A_PurchaseRequisitionHeader", ITEM, "CountOpen", "A_SalesOrder"]
    assert out["total"] == 4

    out = search_catalogue([_many(25)], "   ")
    assert len(out["matches"]) == MAX_SUMMARY_MATCHES and out["total"] == 25
    assert "narrow" in out["hint"].lower()


def test_personal_data_flag_is_never_in_a_tool_result():
    for detail in ("summary", "full"):
        for allow_write in (False, True):
            out = search_catalogue(SERVICES, "", detail=detail, allow_write=allow_write)
            assert "personal_data" not in json.dumps(out)


def test_non_string_and_oversized_queries_do_not_raise():
    assert search_catalogue(SERVICES, None)["total"] == 4  # type: ignore[arg-type]
    assert search_catalogue(SERVICES, 5)["total"] == 4  # type: ignore[arg-type]
    assert search_catalogue(SERVICES, "requisition " * 5000)["matches"][0]["target"] == ITEM


def test_a_malformed_snapshot_entry_is_skipped_not_raised():
    broken = {"name": "broken", "title": "Broken", "enabled": True, "definition": "nope"}
    out = search_catalogue([broken, "junk", SALES_ORDERS], "")  # type: ignore[list-item]
    assert _targets(out) == ["A_SalesOrder"]


# -- the toolset factory -----------------------------------------------------


async def test_toolset_exposes_exactly_the_two_tools_once_O4_lands():
    ts = odata_toolset(
        {"services": ["purchase-requisitions"]},
        auth_mode="destination",
        services=SNAPSHOT,
        resolver_factory=fake,
    )
    assert set(ts.tools) == {"search_operations", "execute_operation"}
    search = ts.tools["search_operations"].function
    out = await search("requisition")
    assert out["matches"][0]["target"] == ITEM
    # Only the attached service is searched, whatever the snapshot holds
    # (two entity sets; its operations are not offered while they cannot be called).
    assert (await search(""))["total"] == 2
    assert (await search("", service="sales-orders"))["error"]["code"] == "unknown_service"
    full = await search(ITEM, detail="full")
    assert "fields" in full["matches"][0]
    doc = ts.tools["search_operations"].description or ""
    assert "execute_operation" in doc and "never instructions" in doc


def test_factory_refuses_other_auth_modes_and_an_empty_service_list():
    for mode in (None, "jwt", "oauth2", "app_only", "session", "none"):
        with pytest.raises(ValueError, match="destination"):
            odata_toolset(
                {"services": ["purchase-requisitions"]}, auth_mode=mode, services=SNAPSHOT
            )
    for oauth in ({}, {"services": []}, {"services": "purchase-requisitions"}, {"services": [7]}):
        with pytest.raises(ValueError, match="service"):
            odata_toolset(oauth, auth_mode="destination", services=SNAPSHOT)
    with pytest.raises(ValueError, match="service"):
        odata_toolset({"services": ["sales-orders"]}, auth_mode="destination", services=None)


def test_factory_warns_for_missing_and_disabled_services_and_keeps_the_rest(caplog):
    snapshot = {**SNAPSHOT, "sales-orders": {**SALES_ORDERS, "enabled": False}}
    with caplog.at_level(logging.WARNING, logger="agents.odata.tools"):
        ts = odata_toolset(
            {"services": ["purchase-requisitions", "sales-orders", "gone", "gone"]},
            auth_mode="destination",
            services=snapshot,
            agent_name="buyer",
        )
    text = caplog.text
    assert "sales-orders" in text and "disabled" in text
    assert "gone" in text and "buyer" in text and BUILTIN_ODATA_URL in text
    assert len([r for r in caplog.records if "gone" in r.getMessage()]) == 1
    assert "search_operations" in ts.tools

    with pytest.raises(ValueError, match="service"):
        odata_toolset(
            {"services": ["sales-orders", "gone"]}, auth_mode="destination", services=snapshot
        )


async def test_string_false_does_not_open_writes():
    for value in ("false", "true", 1, "yes", None, False):
        ts = odata_toolset(
            {"services": ["purchase-requisitions"], "allow_write": value},
            auth_mode="destination",
            services=SNAPSHOT,
        )
        out = await ts.tools["search_operations"].function("requisition item")
        assert out["matches"][0]["operations"] == ["list", "get"], value
        assert "Release" not in _targets(await ts.tools["search_operations"].function("")), value
    ts = odata_toolset(
        {"services": ["purchase-requisitions"], "allow_write": True},
        auth_mode="destination",
        services=SNAPSHOT,
    )
    out = await ts.tools["search_operations"].function("requisition item")
    assert out["matches"][0]["operations"] == ["list", "get", "update"]


async def test_the_toolset_keeps_its_own_copy_of_the_snapshot():
    snapshot = json.loads(json.dumps(SNAPSHOT))
    ts = odata_toolset(
        {"services": ["purchase-requisitions"]}, auth_mode="destination", services=snapshot
    )
    snapshot["purchase-requisitions"]["definition"]["entity_sets"].clear()
    snapshot.clear()
    assert (await ts.tools["search_operations"].function(""))["total"] == 2
