"""The OData V4 read dialect (Task V42): what reaches SAP, and what comes back.

SAP is an ``httpx.MockTransport`` behind the real ``destination_http_client``
(``tests/odata_helpers.py``), so every assertion on a request is an assertion
on what the back end would have received. The V4 service is reached through
``ODataClient`` directly: the same gate (``check_read``) and the same row
filter as V2, with ``V4Dialect`` supplying literals, options and payloads.

The payload shapes are synthetic (the OData 4.0 JSON format as RAP services
use it); no network, no database.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import httpx
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

from agents.odata.client import (  # noqa: E402
    MAX_NEXT_HOPS,
    RAW_ETAG_FIELD,
    ODataClient,
    ODataError,
    ReadQuery,
)
from agents.odata.models import EntitySetDef, ServiceDefinition  # noqa: E402
from agents.odata.session import CsrfSessionStore  # noqa: E402
from agents.odata.v2 import V2Dialect  # noqa: E402
from agents.odata.v4 import V4Dialect  # noqa: E402
from tests.odata_helpers import Sap, sap_v2, service_payload  # noqa: E402

GUID = "01234567-89ab-cdef-0123-456789abcdef"
V4_PATH = "/sap/opu/odata4/sap/api_purchasereq/srvd_a2x/sap/purchaserequisition/0001"
HOST = "s4.internal"
HEADER_PATH = V4_PATH + "/PurchaseRequisition"
ITEM_PATH = V4_PATH + "/PurchaseRequisitionItem"
CONTEXT = f"https://{HOST}:44300{V4_PATH}/$metadata#PurchaseRequisition"


def _f(name, type="Edm.String", *, selectable=True, filterable=False, **extra):
    return {
        "name": name,
        "type": type,
        "selectable": selectable,
        "filterable": filterable,
        **extra,
    }


DEFINITION = {
    "entity_sets": [
        {
            "name": "PurchaseRequisition",
            "keys": [{"name": "PurchaseRequisition"}],
            "operations": ["list", "get", "create", "update", "delete"],
            "fields": [
                _f("PurchaseRequisition", filterable=True, writable=True),
                _f("Description", filterable=True, writable=True),
                _f("CreatedByUser", selectable=False, personal_data=True),
                _f("CreationDate", "Edm.Date", filterable=True),
                _f("LastChangedAt", "Edm.DateTimeOffset", filterable=True),
                _f("Uuid", "Edm.Guid", filterable=True),
                _f("Quantity", "Edm.Decimal", filterable=True),
                _f("IsReleased", "Edm.Boolean", filterable=True),
                _f(
                    "Status",
                    "SRV.Status",
                    filterable=True,
                    values=[
                        {"value": "Open", "meaning": "Open"},
                        {"value": "Closed", "meaning": "Closed"},
                    ],
                ),
                _f("Address", "SRV.Address", filterable=True),
                _f("Tags", "Collection(Edm.String)", filterable=True),
                _f("Picture", "Edm.Stream", filterable=True),
                _f("Internal", "SRV.Internal", selectable=False),
            ],
            "navigations": [
                {"name": "_Item", "target": "PurchaseRequisitionItem", "collection": True},
                {"name": "_Hidden", "target": "Hidden", "collection": True},
            ],
        },
        {
            "name": "PurchaseRequisitionItem",
            "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
            "operations": ["list", "get"],
            "fields": [
                _f("PurchaseRequisition", filterable=True),
                _f("PurchaseRequisitionItem", filterable=True),
                _f("Plant", filterable=True),
                _f("Material"),
                _f("CreatedByUser", selectable=False, personal_data=True),
            ],
            "navigations": [
                {"name": "_Header", "target": "PurchaseRequisition", "collection": False},
            ],
        },
        {
            "name": "Hidden",
            "keys": [{"name": "Id"}],
            "operations": [],
            "fields": [_f("Id")],
        },
    ],
    "operations": [],
}
SERVICE = service_payload(odata_version="v4", service_path=V4_PATH, definition=DEFINITION)
PARSED = ServiceDefinition.model_validate(SERVICE["definition"])
ES_HEADER = PARSED.entity_set("PurchaseRequisition")
ES_ITEM = PARSED.entity_set("PurchaseRequisitionItem")
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}

ITEM_ROW = {
    "@odata.id": "PurchaseRequisitionItem(PurchaseRequisition='10000001',"
    "PurchaseRequisitionItem='00010')",
    "@odata.editLink": "PurchaseRequisitionItem(PurchaseRequisition='10000001',"
    "PurchaseRequisitionItem='00010')",
    "@odata.etag": 'W/"ITEM-TAG"',
    "PurchaseRequisition": "10000001",
    "PurchaseRequisitionItem": "00010",
    "Plant": "1000",
    "Material": "TG11",
    "CreatedByUser": "ALICE",
    "CreatedByUser@odata.type": "#String",
    "_Header@odata.navigationLink": "PurchaseRequisitionItem(...)/_Header",
    "SAP__Messages": [{"code": "M/1", "message": "about ALICE"}],
}
HEADER_ROW = {
    "@odata.context": CONTEXT + "/$entity",
    "@odata.etag": 'W/"HEAD-TAG"',
    "@odata.id": "PurchaseRequisition('10000001')",
    "PurchaseRequisition": "10000001",
    "Description": "Pumps",
    "CreatedByUser": "ALICE",
    "CreationDate": "2026-10-05",
    "LastChangedAt": "2026-10-05T10:00:00Z",
    "Uuid": GUID,
    "Quantity": 5,
    "IsReleased": True,
    "Status": "Open",
    "Address": {
        "@odata.type": "#SRV.Address",
        "City": "Brussels",
        "City@odata.type": "#String",
        "Geo": {"@odata.type": "#SRV.Geo", "Lat": 50.8},
    },
    "Tags": ["a", "b"],
    "Tags@odata.type": "#Collection(String)",
    "Internal": {"Note": "INTERNAL-NOTE"},
    "_Item@odata.navigationLink": "PurchaseRequisition('10000001')/_Item",
    "SAP__Messages": [],
}


def client(sap, service=None, **kwargs) -> ODataClient:
    return ODataClient(sap_v2(sap), service or SERVICE, V4Dialect(), **kwargs)


def query(**overrides) -> ReadQuery:
    values = {"select": [], "filter": None, "expand": [], "orderby": [], "top": 50, "skip": 0}
    values.update(overrides)
    return ReadQuery(**values)


def page(rows, **extra):
    return {"@odata.context": CONTEXT, "value": rows, **extra}


def v4_error(status: int, code: str, text: str, **extra) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": text, **extra}})


async def refused(awaitable) -> ODataError:
    with pytest.raises(ODataError) as excinfo:
        await awaitable
    return excinfo.value


def keyed(types: dict[str, str]) -> EntitySetDef:
    return EntitySetDef.model_validate(
        {
            "name": "Typed",
            "keys": [{"name": name, "type": edm} for name, edm in types.items()],
            "operations": ["get"],
            "fields": [_f(name, edm) for name, edm in types.items()],
        }
    )


# -- the dialect: literals and the key predicate ------------------------------


def test_key_segment_uses_v4_literals():
    d = V4Dialect()
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": "10000001"}) == "('10000001')"
    assert (
        d.key_segment(ES_ITEM, KEY)
        == "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
    )
    assert d.literal("Edm.String", "O'Neil") == "'O''Neil'"
    assert d.literal("Edm.String", 4500000001) == "'4500000001'"
    # No V2 prefixes and no V2 type suffixes: these travel bare.
    assert d.literal("Edm.Guid", GUID) == GUID
    assert d.literal("Edm.Date", "2026-10-05") == "2026-10-05"
    assert d.literal("Edm.DateTimeOffset", "2026-10-05T10:00:00Z") == "2026-10-05T10:00:00Z"
    assert d.literal("Edm.DateTimeOffset", "2026-10-05T10:00:00.123+02:00").endswith("+02:00")
    assert d.literal("Edm.TimeOfDay", "10:00:00") == "10:00:00"
    assert d.literal("Edm.Int32", 5) == "5" and d.literal("Edm.Int64", "-7") == "-7"
    assert d.literal("Edm.Int64", 2**40) == str(2**40)
    assert d.literal("Edm.Decimal", "10.50") == "10.50" and d.literal("Edm.Decimal", 3) == "3"
    assert d.literal("Edm.Double", 1.5) == "1.5"
    assert d.literal("Edm.Boolean", True) == "true" and d.literal("Edm.Boolean", "false") == "false"
    assert d.literal("Edm.Duration", "PT12H") == "duration'PT12H'"

    typed = keyed({"Id": "Edm.Guid", "Day": "Edm.Date", "No": "Edm.Int32", "Ok": "Edm.Boolean"})
    assert (
        d.key_segment(typed, {"Id": GUID, "Day": "2026-10-05", "No": 7, "Ok": False})
        == f"(Id={GUID},Day=2026-10-05,No=7,Ok=false)"
    )
    assert d.key_segment(keyed({"Id": "Edm.Guid"}), {"Id": GUID}) == f"({GUID})"
    # A quote in a key stays inside its literal, percent-encoded as one segment.
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": "O'Neil x"}) == "('O''Neil%20x')"


@pytest.mark.parametrize(
    "edm_type, value",
    [
        ("Edm.Guid", f"guid'{GUID}'"),  # the V2 form
        ("Edm.Guid", GUID + "\n"),
        ("Edm.Guid", "not-a-guid"),
        ("Edm.Guid", 5),
        ("Edm.Date", "2026-02-30"),
        ("Edm.Date", "2026-10-05T00:00:00"),
        ("Edm.Date", "datetime'2026-10-05T00:00:00'"),
        ("Edm.Date", "２０２６-10-05"),  # digits, but not ASCII ones
        ("Edm.DateTimeOffset", "2026-10-05T10:00:00"),  # no offset
        ("Edm.DateTimeOffset", "2026-10-05T25:00:00Z"),
        ("Edm.DateTimeOffset", "2026-13-05T10:00:00Z"),
        ("Edm.DateTimeOffset", "datetimeoffset'2026-10-05T10:00:00Z'"),
        ("Edm.TimeOfDay", "24:00:00"),
        ("Edm.TimeOfDay", "PT10H"),  # the V2 Edm.Time form
        ("Edm.Int32", "5L"),
        ("Edm.Int32", 2**31),
        ("Edm.Int32", True),
        ("Edm.Int64", "1 or 1"),
        ("Edm.Byte", -1),
        ("Edm.Decimal", "10.5M"),
        ("Edm.Decimal", "1,5"),
        ("Edm.Double", "1.5d"),
        ("Edm.Double", float("inf")),
        ("Edm.Boolean", "TRUE"),
        ("Edm.Boolean", 1),
        ("Edm.Duration", "P"),
        ("Edm.Duration", "12 hours"),
        ("Edm.String", None),
        ("Edm.String", ["a"]),
        ("Edm.String", "a\nb"),
        # Types that are not positively recognised: refused, never guessed.
        ("Edm.DateTime", "2026-10-05T00:00:00"),  # does not exist in V4
        ("Edm.Time", "PT10H"),
        ("Edm.Binary", "AAEC"),
        ("Edm.Stream", "x"),
        ("SRV.Status", "Open"),  # an enum, as far as anyone can tell from the name
        ("SRV.Address", "x"),
        ("Collection(Edm.String)", "x"),
        ("", "x"),
        ("edm.string", "x"),
    ],
)
def test_a_value_without_the_form_of_its_type_is_refused(edm_type, value):
    d = V4Dialect()
    with pytest.raises(ODataError) as excinfo:
        d.literal(edm_type, value)
    assert excinfo.value.code == "invalid_argument"
    if isinstance(value, str) and len(value) > 3:
        assert value not in excinfo.value.message
    with pytest.raises(ODataError) as excinfo:
        d.key_segment(keyed({"K": edm_type or "Edm.Nothing"}), {"K": value})
    assert excinfo.value.code == "invalid_key"


@pytest.mark.parametrize(
    "key",
    [
        {"PurchaseRequisition": "1"},
        {"PurchaseRequisition": "1", "PurchaseRequisitionItem": "1", "X": "1"},
        {"PurchaseRequisition": "1')/Other('", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "..", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "a%2Fb", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "a?$expand=_Header", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "a#b", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "a\\b", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "", "PurchaseRequisitionItem": "1"},
        {"PurchaseRequisition": "x" * 300, "PurchaseRequisitionItem": "1"},
        {},
        None,
        "10000001",
        ["10000001", "00010"],
    ],
)
def test_a_key_that_could_leave_its_segment_is_refused_before_any_request(key):
    sap = Sap(page([]))
    c = client(sap)
    with pytest.raises(ODataError) as excinfo:
        c.check_read(ES_ITEM, "get", key=key)
    assert excinfo.value.code == "invalid_key"
    assert "Other" not in excinfo.value.message and "expand" not in excinfo.value.message
    assert sap.requests == []


def test_read_params_are_the_v4_options():
    d = V4Dialect()
    params = d.read_params(
        ReadQuery(
            select=["Plant", "Material"],
            filter="  Plant eq '1000' ",
            expand=["_Header($select=PurchaseRequisition,Description)"],
            orderby=["Plant desc", "Material"],
            top=20,
            skip=40,
            count=True,
        )
    )
    assert params == {
        "$select": "Plant,Material",
        "$filter": "Plant eq '1000'",
        "$expand": "_Header($select=PurchaseRequisition,Description)",
        "$orderby": "Plant desc,Material",
        "$top": "20",
        "$skip": "40",
        "$count": "true",
    }
    bare = d.read_params(
        ReadQuery(select=["Plant"], filter="  ", expand=[], orderby=[], top=0, skip=0, count=False)
    )
    assert bare == {"$select": "Plant"}
    for params in (params, bare):
        assert "$format" not in params and "$inlinecount" not in params


def test_parse_list_and_entity():
    d = V4Dialect()
    rows, count, link = d.parse_list(
        {"value": [{"A": 1}], "@odata.count": 37, "@odata.nextLink": "Set?$skiptoken=50"}
    )
    assert (rows, count, link) == ([{"A": 1}], 37, "Set?$skiptoken=50")
    # IEEE754Compatible services send the count as a string; 4.01 drops "odata.".
    assert d.parse_list({"value": [], "@odata.count": "12"})[1] == 12
    assert d.parse_list({"value": [], "@count": 3, "@nextLink": "Set?x=1"})[1:] == (3, "Set?x=1")
    assert d.parse_list({"value": [], "@odata.count": True})[1] is None
    assert d.parse_list({"value": [], "@odata.count": -1})[1] is None
    row, etag = d.parse_entity(HEADER_ROW)
    assert row is HEADER_ROW and etag == 'W/"HEAD-TAG"'
    assert d.parse_entity({"PurchaseRequisition": "1"}) == ({"PurchaseRequisition": "1"}, None)
    for bad in (
        None,
        [],
        "x",
        {"d": {"results": []}},  # a V2 answer: the version is never detected from an answer
        {"value": "x"},
        {"value": {"A": 1}},
        {"results": []},
        {"error": {"code": "A", "message": "b"}, "value": []},
    ):
        with pytest.raises(ODataError) as excinfo:
            d.parse_list(bad)
        assert excinfo.value.code == "sap_error"
    for bad in (
        None,
        [],
        "x",
        {},
        {"error": {"code": "A", "message": "b"}},
        {"@odata.context": CONTEXT, "error": {"code": "A", "message": "b"}},
        {"@odata.context": CONTEXT, "value": [{"A": 1}]},  # a collection, not an entity
        {"@odata.context": CONTEXT},  # nothing but bookkeeping
    ):
        with pytest.raises(ODataError) as excinfo:
            d.parse_entity(bad)
        assert excinfo.value.code == "sap_error"


def test_parse_error_reads_only_the_v4_envelope():
    d = V4Dialect()
    assert d.parse_error(v4_error(400, "X/123", "Invalid filter")) == ("X/123", "Invalid filter")
    detailed = v4_error(
        400,
        "X/123",
        "Invalid filter",
        details=[{"code": "D/1", "message": "detail about ALICE"}],
        innererror={"transactionid": "ABC", "ErrorDetails": {"host": HOST}},
    )
    assert d.parse_error(detailed) == ("X/123", "Invalid filter")
    # The V2 message object is not the V4 envelope.
    v2_shaped = httpx.Response(400, json={"error": {"code": "A", "message": {"value": "b"}}})
    assert d.parse_error(v2_shaped) == ("A", "")
    for not_an_envelope in (
        httpx.Response(500, headers={"content-type": "text/html"}, text="<html>dump</html>"),
        httpx.Response(500, json={"message": "x"}),
        httpx.Response(500, json=["error"]),
        httpx.Response(500, json={"error": "text"}),
        httpx.Response(500, text="plain"),
        httpx.Response(500),
    ):
        assert d.parse_error(not_an_envelope) == ("", "")


# -- list ---------------------------------------------------------------------


async def test_list_sends_v4_options_and_returns_only_selectable_fields():
    sap = Sap(page([ITEM_ROW], **{"@odata.count": 37}))
    result = await client(sap).list(
        ES_ITEM, query(filter="Plant eq '1000'", orderby=["Plant desc"], top=10, skip=20)
    )
    request = sap.requests[0]
    assert request.method == "GET" and request.url.host == HOST
    assert request.url.path == ITEM_PATH
    params = dict(request.url.params)
    assert params == {
        "$select": "PurchaseRequisition,PurchaseRequisitionItem,Plant,Material",
        "$filter": "Plant eq '1000'",
        "$orderby": "Plant desc",
        "$top": "10",
        "$skip": "20",
        "$count": "true",
    }
    assert request.headers["Accept"] == "application/json"
    assert "cookie" not in request.headers and "x-csrf-token" not in request.headers
    assert result == {
        "items": [
            {
                "PurchaseRequisition": "10000001",
                "PurchaseRequisitionItem": "00010",
                "Plant": "1000",
                "Material": "TG11",
            }
        ],
        "count": 37,
        "next_skip": 21,
        "truncated": False,
    }


async def test_a_hidden_value_reaches_no_result_through_any_annotation():
    row = dict(HEADER_ROW)
    row["_Item"] = [ITEM_ROW]
    row["_Item@odata.count"] = 1
    row["_Item@odata.nextLink"] = "PurchaseRequisition('10000001')/_Item?$skiptoken=1"
    sap = Sap(page([row], **{"@odata.count": 1}))
    result = await client(sap).list(ES_HEADER, query(expand=["_Item"]))
    text = json.dumps(result)
    for hidden in ("ALICE", "INTERNAL-NOTE", "@odata", "SAP__Messages", "HEAD-TAG", "ITEM-TAG"):
        assert hidden not in text, hidden
    item = result["items"][0]
    assert RAW_ETAG_FIELD not in item and RAW_ETAG_FIELD not in result
    assert "CreatedByUser" not in item and "Internal" not in item
    assert item["_Item"] == [
        {
            "PurchaseRequisition": "10000001",
            "PurchaseRequisitionItem": "00010",
            "Plant": "1000",
            "Material": "TG11",
        }
    ]
    # A complex and a collection-valued field come back as plain JSON.
    assert item["Address"] == {"City": "Brussels", "Geo": {"Lat": 50.8}}
    assert item["Tags"] == ["a", "b"]
    assert item["Status"] == "Open" and item["Uuid"] == GUID and item["IsReleased"] is True


async def test_key_fields_are_visible_only_when_selectable():
    definition = json.loads(json.dumps(DEFINITION))
    fields = definition["entity_sets"][1]["fields"]
    fields[1] = _f("PurchaseRequisitionItem", selectable=False)
    service = service_payload(odata_version="v4", service_path=V4_PATH, definition=definition)
    es = ServiceDefinition.model_validate(service["definition"]).entity_set(ES_ITEM.name)
    sap = Sap(page([ITEM_ROW]), ITEM_ROW)
    c = client(sap, service)
    listed = await c.list(es, query())
    assert "00010" not in json.dumps(listed)
    assert "PurchaseRequisitionItem" not in sap.requests[0].url.params["$select"].split(",")
    got = await c.get(es, KEY, select=[], expand=[])
    assert "PurchaseRequisitionItem" not in got["item"] and "00010" not in json.dumps(got["item"])
    # The key the caller sent is in the path, and its name is known to the caller.
    assert sap.requests[1].url.path.endswith(
        "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
    )


async def test_expand_carries_a_nested_select_of_the_targets_selectable_fields():
    row = dict(HEADER_ROW, _Item=[ITEM_ROW, dict(ITEM_ROW, Plant="2000")])
    sap = Sap(page([row]))
    result = await client(sap).list(ES_HEADER, query(select=["Description"], expand=["_Item"]))
    params = dict(sap.requests[0].url.params)
    assert params["$select"] == "Description"  # no V2 `Nav/Field` path
    assert (
        params["$expand"]
        == "_Item($select=PurchaseRequisition,PurchaseRequisitionItem,Plant,Material)"
    )
    assert result["items"][0]["Description"] == "Pumps"
    assert [r["Plant"] for r in result["items"][0]["_Item"]] == ["1000", "2000"]
    assert "ALICE" not in json.dumps(result)

    # A single-valued navigation, and one that leads nowhere.
    sap = Sap(page([dict(ITEM_ROW, _Header=HEADER_ROW), dict(ITEM_ROW, _Header=None)]))
    result = await client(sap).list(ES_ITEM, query(select=["Plant"], expand=["_Header"]))
    first, second = result["items"]
    assert first["_Header"]["Description"] == "Pumps" and "CreatedByUser" not in first["_Header"]
    assert second["_Header"] is None
    assert sap.requests[0].url.params["$expand"].startswith("_Header($select=PurchaseRequisition,")


@pytest.mark.parametrize(
    "expand, code",
    [
        (["_Nope"], "unknown_target"),
        (["_Item($select=CreatedByUser)"], "unknown_target"),
        (["_Item/_Header"], "unknown_target"),
        (["_Hidden"], "operation_disabled"),  # the target has no `list`
        ("_Item", "invalid_argument"),
        ([5], "unknown_target"),
    ],
)
def test_expand_refusals(expand, code):
    c = client(Sap(page([])))
    with pytest.raises(ODataError) as excinfo:
        c.check_read(ES_HEADER, "list", expand=expand, top=5)
    assert excinfo.value.code == code
    assert "CreatedByUser" not in excinfo.value.message


async def test_a_navigation_is_read_through_the_parent_with_the_targets_rules():
    sap = Sap(page([ITEM_ROW]), HEADER_ROW)
    c = client(sap)
    result = await c.list(
        ES_HEADER,
        query(filter="Plant eq '1000'"),
        key={"PurchaseRequisition": "10000001"},
        navigation="_Item",
    )
    assert sap.requests[0].url.path == HEADER_PATH + "('10000001')/_Item"
    assert sap.requests[0].url.params["$select"].startswith("PurchaseRequisition,")
    assert result["items"][0]["Material"] == "TG11"
    result = await c.get(ES_ITEM, KEY, select=["Description"], expand=[], navigation="_Header")
    assert sap.requests[1].url.path.endswith(
        "/PurchaseRequisitionItem(PurchaseRequisition='10000001',"
        "PurchaseRequisitionItem='00010')/_Header"
    )
    assert result["item"] == {"Description": "Pumps"}

    # `get` on the parent and the matching read on the target, as in V2.
    definition = json.loads(json.dumps(DEFINITION))
    definition["entity_sets"][0]["operations"] = ["list"]
    service = service_payload(odata_version="v4", service_path=V4_PATH, definition=definition)
    es = ServiceDefinition.model_validate(service["definition"]).entity_set("PurchaseRequisition")
    with pytest.raises(ODataError) as excinfo:
        client(sap, service).check_read(
            es, "list", key={"PurchaseRequisition": "1"}, navigation="_Item", top=5
        )
    assert excinfo.value.code == "operation_disabled"
    with pytest.raises(ODataError) as excinfo:
        c.check_read(ES_HEADER, "list", key={"PurchaseRequisition": "1"}, navigation="_Hidden")
    assert excinfo.value.code == "operation_disabled"
    assert len(sap.requests) == 2


# -- paging -------------------------------------------------------------------


async def test_a_relative_next_link_is_followed_on_the_same_resource_only():
    other = dict(ITEM_ROW, PurchaseRequisitionItem="00020")
    sap = Sap(
        page([ITEM_ROW], **{"@odata.nextLink": "PurchaseRequisitionItem?$skiptoken=1"}),
        page([other]),
    )
    result = await client(sap).list(ES_ITEM, query(top=5))
    assert [i["PurchaseRequisitionItem"] for i in result["items"]] == ["00010", "00020"]
    assert sap.requests[1].url.path == ITEM_PATH
    assert sap.requests[1].url.params["$skiptoken"] == "1"
    assert "next_skip" not in result

    # The link's host is dropped: the follow-up goes through the destination.
    absolute = f"https://other.example:8443{ITEM_PATH}?$skiptoken=1"
    sap = Sap(page([ITEM_ROW], **{"@odata.nextLink": absolute}), page([other]))
    await client(sap).list(ES_ITEM, query(top=5))
    assert [r.url.host for r in sap.requests] == [HOST, HOST]
    assert sap.requests[1].url.path == ITEM_PATH


@pytest.mark.parametrize(
    "link",
    [
        "PurchaseRequisition?$skiptoken=1",  # another entity set
        "PurchaseRequisitionItem/_Header?$skiptoken=1",
        "../other/PurchaseRequisitionItem?$skiptoken=1",
        "/sap/opu/odata4/other/PurchaseRequisitionItem?$skiptoken=1",
        "//other.example/PurchaseRequisitionItem",
        "PurchaseRequisitionItem%2F..%2F..?$skiptoken=1",
        "$batch",
        "ftp://other.example" + ITEM_PATH,
        {"href": "PurchaseRequisitionItem"},
        5,
    ],
)
async def test_a_next_link_elsewhere_is_not_followed(link):
    sap = Sap(page([ITEM_ROW], **{"@odata.nextLink": link}), page([ITEM_ROW]))
    result = await client(sap).list(ES_ITEM, query(top=5))
    assert len(sap.requests) == 1 and len(result["items"]) == 1


async def test_next_links_are_followed_a_bounded_number_of_times():
    sap = Sap(page([ITEM_ROW], **{"@odata.nextLink": "PurchaseRequisitionItem?$skiptoken=1"}))
    result = await client(sap).list(ES_ITEM, query(top=50))
    assert len(sap.requests) == 1 + MAX_NEXT_HOPS
    assert len(result["items"]) == 1 + MAX_NEXT_HOPS and result["next_skip"] == 1 + MAX_NEXT_HOPS


async def test_the_result_is_capped_at_top_whatever_sap_sends():
    sap = Sap(page([ITEM_ROW] * 9))
    result = await client(sap).list(ES_ITEM, query(top=3))
    assert len(result["items"]) == 3 and result["next_skip"] == 3


@pytest.mark.parametrize("top, skip", [(0, 0), (-1, 0), (True, 0), ("5", 0), (5, -1), (5, "0")])
def test_paging_values_are_checked_before_any_request(top, skip):
    sap = Sap(page([]))
    with pytest.raises(ODataError) as excinfo:
        client(sap).check_read(ES_ITEM, "list", top=top, skip=skip)
    assert excinfo.value.code == "invalid_argument" and sap.requests == []


# -- get ----------------------------------------------------------------------


async def test_get_keeps_the_raw_etag_apart_from_the_item():
    sap = Sap(HEADER_ROW)
    result = await client(sap).get(
        ES_HEADER, {"PurchaseRequisition": "10000001"}, select=[], expand=[]
    )
    request = sap.requests[0]
    assert request.url.path == HEADER_PATH + "('10000001')"
    params = dict(request.url.params)
    assert set(params) == {"$select"} and "CreatedByUser" not in params["$select"]
    assert result[RAW_ETAG_FIELD] == 'W/"HEAD-TAG"'
    assert "HEAD-TAG" not in json.dumps(result["item"]) and "ALICE" not in json.dumps(result)
    assert result["item"]["Description"] == "Pumps" and result["truncated"] is False

    # Without `@odata.etag` the header is used; anything that is no entity tag is not.
    plain = {k: v for k, v in HEADER_ROW.items() if k != "@odata.etag"}
    sap = Sap(httpx.Response(200, json=plain, headers={"ETag": 'W/"HDR"'}))
    got = await client(sap).get(ES_HEADER, {"PurchaseRequisition": "1"}, select=[], expand=[])
    assert got[RAW_ETAG_FIELD] == 'W/"HDR"'
    sap = Sap(dict(plain, **{"@odata.etag": "*"}))
    got = await client(sap).get(ES_HEADER, {"PurchaseRequisition": "1"}, select=[], expand=[])
    assert RAW_ETAG_FIELD not in got


async def test_typed_keys_reach_sap_as_one_bare_segment():
    typed = keyed({"Id": "Edm.Guid", "At": "Edm.DateTimeOffset"})
    definition = {"entity_sets": [typed.model_dump(mode="json")], "operations": []}
    service = service_payload(odata_version="v4", service_path=V4_PATH, definition=definition)
    sap = Sap({"Id": GUID, "At": "2026-10-05T10:00:00+02:00"})
    c = client(sap, service)
    result = await c.get(
        typed, {"Id": GUID, "At": "2026-10-05T10:00:00+02:00"}, select=[], expand=[]
    )
    assert sap.requests[0].url.path == (f"{V4_PATH}/Typed(Id={GUID},At=2026-10-05T10:00:00+02:00)")
    assert "guid'" not in str(sap.requests[0].url) and "datetime" not in str(sap.requests[0].url)
    assert result["item"]["Id"] == GUID


async def test_a_single_navigation_that_leads_nowhere_is_no_item():
    for answer in (httpx.Response(204), {"@odata.context": CONTEXT, "@odata.null": True}):
        result = await client(Sap(answer)).get(
            ES_ITEM, KEY, select=[], expand=[], navigation="_Header"
        )
        assert result == {"item": None, "truncated": False}
    # A field called `d` that is null is a field, not the V2 envelope.
    typed = keyed({"d": "Edm.String"})
    definition = {"entity_sets": [typed.model_dump(mode="json")], "operations": []}
    service = service_payload(odata_version="v4", service_path=V4_PATH, definition=definition)
    sap = Sap({"@odata.context": CONTEXT, "d": None, "@odata.etag": 'W/"1"'})
    result = await client(sap, service).get(typed, {"d": "x"}, select=[], expand=[])
    assert result["item"] == {"d": None}


@pytest.mark.parametrize(
    "extra",
    [
        {"orderby": ["Plant"]},
        {"filter": "Plant eq '1'"},
        {"top": 5},
        {"skip": 5},
    ],
)
def test_get_takes_no_list_options(extra):
    with pytest.raises(ODataError) as excinfo:
        client(Sap(page([]))).check_read(ES_ITEM, "get", key=KEY, **extra)
    assert excinfo.value.code == "invalid_argument"


# -- filter and orderby -------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "PurchaseRequisition eq '10000001'",
        "contains(Description,'pump') and startswith(Description,'P')",
        "endswith(tolower(Description),'s') or not (Quantity gt 10.5)",
        "PurchaseRequisition in ('1','2', '3')",
        f"Uuid eq {GUID}",
        "CreationDate ge 2026-01-01 and CreationDate lt 2026-02-01",
        "LastChangedAt ge 2026-10-05T10:00:00Z and LastChangedAt lt 2026-10-05T10:00:00.5+02:00",
        "year(CreationDate) eq 2026 and Quantity le -3",
        "IsReleased eq true and Description ne null",
        "Status eq SRV.Status'Open' or Status has SRV.Status'Open,Closed'",
        "Description eq 'a&$top=1 /?#% any(d:d/x eq 1) $it'",
        "Description eq 'O''Neil'",
    ],
)
async def test_a_v4_filter_travels_as_one_parameter_value(expr):
    sap = Sap(page([]))
    await client(sap).list(ES_HEADER, query(filter=expr))
    request = sap.requests[0]
    assert request.url.params["$filter"] == expr
    assert request.url.path == HEADER_PATH
    assert set(request.url.params) == {"$select", "$filter", "$top", "$count"}


@pytest.mark.parametrize(
    "expr, code",
    [
        ("CreatedByUser eq 'ALICE'", "field_not_filterable"),
        ("contains(CreatedByUser,'A')", "field_not_filterable"),
        ("Description eq CreatedByUser", "field_not_filterable"),
        ("Nope eq 1", "unknown_field"),
        ("description eq 'x'", "unknown_field"),
        ("Description EQ 'x'", "unknown_field"),  # V4 operators are lower-case
        # V2 syntax is not V4 syntax.
        (f"Uuid eq guid'{GUID}'", "invalid_argument"),
        ("CreationDate ge datetime'2026-01-01T00:00:00'", "invalid_argument"),
        ("substringof('x',Description)", "invalid_argument"),
        ("Quantity gt 10.5M", "invalid_argument"),
        ("Quantity gt 5L", "invalid_argument"),
        # Lambda operators and path segments stay refused.
        ("_Item/any(d:d/Plant eq '1000')", "invalid_argument"),
        ("_Item/all(d:d/Plant eq '1000')", "invalid_argument"),
        ("any(Description)", "invalid_argument"),
        ("all(Description)", "invalid_argument"),
        ("_Item/$count gt 1", "invalid_argument"),
        ("$it/Description eq 'x'", "invalid_argument"),
        ("$root/PurchaseRequisition('1')/Description eq Description", "invalid_argument"),
        ("_Item/Plant eq '1000'", "invalid_argument"),
        ("Address/City eq 'Brussels'", "invalid_argument"),
        ("cast(Quantity,Edm.String) eq '1'", "invalid_argument"),
        ("isof(Description,Edm.String)", "invalid_argument"),
        ("Description eq '1'&$expand=_Item", "invalid_argument"),
        ("Description eq '1';$top=1", "invalid_argument"),
        ("Description eq '1", "invalid_argument"),
        ("Description eq @p1", "invalid_argument"),  # no parameter aliases
        ("Quantity add 1 eq 2", "unknown_field"),
        ("Quantity eq 1 - 2", "invalid_argument"),
        # A type that is not positively recognised is no filter target.
        ("Address eq 'x'", "field_not_filterable"),
        ("Tags eq 'a'", "field_not_filterable"),
        ("Picture eq 'x'", "field_not_filterable"),
        ("Status eq 'Open'", "field_not_filterable"),  # an enum needs its typed literal
        ("Status eq SRV.Other'Open'", "invalid_argument"),
        ("Address eq SRV.Address'x'", "field_not_filterable"),
        ("Address eq SRV.Status'Open' and Status eq SRV.Status'Open'", "invalid_argument"),
        ("Description eq Edm.String'x'", "invalid_argument"),
    ],
)
def test_v4_filter_refusals(expr, code):
    sap = Sap(page([]))
    with pytest.raises(ODataError) as excinfo:
        client(sap).check_read(ES_HEADER, "list", filter=expr, top=5)
    assert excinfo.value.code == code, excinfo.value.message
    assert "ALICE" not in excinfo.value.message and "Brussels" not in excinfo.value.message
    assert sap.requests == []


@pytest.mark.parametrize(
    "orderby, code",
    [
        (["CreatedByUser"], "field_not_selectable"),
        (["Nope"], "unknown_field"),
        (["Address"], "invalid_argument"),
        (["Tags desc"], "invalid_argument"),
        (["Status"], "invalid_argument"),
        (["Picture"], "invalid_argument"),
        (["Address/City"], "invalid_argument"),
        (["Description desc, CreatedByUser"], "invalid_argument"),
        (["_Item/$count"], "invalid_argument"),
    ],
)
def test_orderby_refusals(orderby, code):
    with pytest.raises(ODataError) as excinfo:
        client(Sap(page([]))).check_read(ES_HEADER, "list", orderby=orderby, top=5)
    assert excinfo.value.code == code


def test_the_gate_keeps_its_order_for_v4():
    c = client(Sap(page([])))
    bad_key = {"PurchaseRequisition": "1/2"}
    cases = [
        # (kwargs, expected code): each call breaks this rule and every later one.
        (dict(operation="read"), "invalid_argument"),
        (dict(key=bad_key, navigation="_Nope", select=["CreatedByUser"]), "invalid_key"),
        (dict(key={"PurchaseRequisition": "1"}, navigation="_Nope"), "unknown_target"),
        (dict(select=["CreatedByUser"], expand=["_Nope"]), "field_not_selectable"),
        (dict(expand=["_Nope"], orderby=["CreatedByUser"]), "unknown_target"),
        (dict(orderby=["CreatedByUser"], filter="CreatedByUser eq 'x'"), "field_not_selectable"),
        (dict(filter="CreatedByUser eq 'x'", top=0), "field_not_filterable"),
        (dict(top=0), "invalid_argument"),
    ]
    for kwargs, code in cases:
        operation = kwargs.pop("operation", "list")
        kwargs.setdefault("top", 5)
        with pytest.raises(ODataError) as excinfo:
            c.check_read(ES_HEADER, operation, **kwargs)
        assert excinfo.value.code == code, kwargs
    plan = c.check_read(ES_HEADER, "list", select=["Description"], expand=["_Item"], top=5)
    assert plan.path == HEADER_PATH and plan.names == ["Description"]
    assert plan.query.select == ["Description"]
    assert plan.query.expand == [
        "_Item($select=PurchaseRequisition,PurchaseRequisitionItem,Plant,Material)"
    ]


def test_the_v2_plan_is_unchanged_by_the_shared_seam():
    """V2 still names an expanded entity's fields in ``$select`` (``Nav/Field``)."""
    service = service_payload()
    definition = ServiceDefinition.model_validate(service["definition"])
    es = definition.entity_set("A_PurchaseRequisitionHeader")
    plan = ODataClient(None, service, V2Dialect()).check_read(
        es, "list", select=["PurReqnDescription"], expand=["to_PurchaseReqnItem"], top=5
    )
    assert plan.query.expand == ["to_PurchaseReqnItem"]
    assert plan.query.select[0] == "PurReqnDescription"
    assert "to_PurchaseReqnItem/Plant" in plan.query.select


# -- errors and odd answers ---------------------------------------------------


async def test_a_sap_error_is_reduced_to_code_and_message(caplog):
    caplog.set_level(logging.DEBUG)
    answers = (
        v4_error(
            400,
            "X/123",
            "Invalid filter",
            details=[{"code": "D/1", "message": f"see https://{HOST}/dump for ALICE"}],
            innererror={"host": HOST, "application": {"service_id": "ZSRV"}},
        ),
        httpx.Response(
            500, headers={"content-type": "text/html"}, text=f"<html>{HOST} dump ALICE</html>"
        ),
        v4_error(404, "/IWBEP/CM_V4_RUNTIME/021", "x" * 2000),
        httpx.Response(403, json={"error": {"code": 5, "message": ["no"]}}),
        httpx.Response(302, headers={"location": f"https://{HOST}/sap/public/logon"}),
    )
    sap = Sap(*answers)
    c = client(sap)
    errors = [await refused(c.list(ES_ITEM, query(filter="Plant eq 'SECRET-VAL'")))] + [
        await refused(c.list(ES_ITEM, query())) for _ in answers[1:]
    ]
    assert [e.code for e in errors] == ["sap_error"] * 5
    assert errors[0].message == "X/123: Invalid filter" and errors[0].status == 400
    assert errors[1].message == "HTTP 500 from the OData service"
    assert len(errors[2].message) <= 500 and "not exist" in errors[2].hint
    assert errors[3].message == "HTTP 403 from the OData service"
    assert errors[4].status == 302 and len(sap.requests) == 5  # a redirect is not followed
    text = "".join(f"{e!r}{e}{e.to_dict()}{e.hint}" for e in errors) + caplog.text + repr(c)
    own = "".join(r.getMessage() for r in caplog.records if r.name.startswith("agents."))
    for secret in (HOST, "ALICE", "dest-token", "D/1", "ZSRV"):
        assert secret not in "".join(f"{e!r}{e}{e.to_dict()}{e.hint}" for e in errors), secret
    for secret in (HOST, "SECRET-VAL", "dest-token"):
        assert secret not in own + repr(c), secret
    assert text  # something was looked at


@pytest.mark.parametrize(
    "answer",
    [
        {"d": {"results": [ITEM_ROW]}},  # a V2 answer from a service declared as V4
        {"value": "x"},
        ["x"],
        {"error": {"code": "A", "message": "b"}, "value": [ITEM_ROW]},
        httpx.Response(200, headers={"content-type": "text/html"}, text="<html>Logon</html>"),
    ],
)
async def test_an_answer_in_another_shape_is_an_error_not_data(answer):
    error = await refused(client(Sap(answer)).list(ES_ITEM, query()))
    assert error.code == "sap_error" and "Logon" not in error.message
    assert "TG11" not in error.message


async def test_the_version_comes_from_the_catalogue_never_from_an_answer():
    # The V2 client reading a V4-shaped answer refuses it as well.
    v2_service = service_payload()
    es = ServiceDefinition.model_validate(v2_service["definition"]).entity_set(
        "A_PurchaseRequisitionItem"
    )
    sap = Sap(page([ITEM_ROW]))
    v2_client = ODataClient(sap_v2(sap), v2_service, V2Dialect())
    error = await refused(
        v2_client.list(es, ReadQuery(select=[], filter=None, expand=[], orderby=[], top=5, skip=0))
    )
    assert error.code == "sap_error"
    assert sap.requests[0].url.params["$format"] == "json"


# -- writes are the next task -------------------------------------------------


@pytest.mark.parametrize(
    "operation, kwargs",
    [
        ("create", {"body": {"Description": "x"}}),
        ("update", {"key": {"PurchaseRequisition": "1"}, "body": {"Description": "x"}}),
        ("delete", {"key": {"PurchaseRequisition": "1"}}),
    ],
)
async def test_a_v4_write_is_refused_before_anything_is_sent(operation, kwargs):
    sap = Sap(page([]))
    c = client(sap, sessions=CsrfSessionStore())
    with pytest.raises(ODataError) as excinfo:
        c.check_write(ES_HEADER, operation, **kwargs)
    assert excinfo.value.code == "operation_disabled"
    assert "V4" in excinfo.value.message
    call = {
        "create": lambda: c.create(ES_HEADER, kwargs["body"]),
        "update": lambda: c.update(ES_HEADER, kwargs.get("key"), kwargs.get("body")),
        "delete": lambda: c.delete(ES_HEADER, kwargs.get("key")),
    }[operation]
    error = await refused(call())
    assert error.code == "operation_disabled"
    assert sap.requests == []  # not even a CSRF token request
    d = V4Dialect()
    for refuse in (
        lambda: d.encode_body(ES_HEADER, {"Description": "x"}),
        lambda: d.update_request("/x", {}),
    ):
        with pytest.raises(ODataError) as excinfo:
            refuse()
        assert excinfo.value.code == "operation_disabled"


# -- review follow-up ---------------------------------------------------------


def _one_set(*fields, keys=("Id",)):
    es = EntitySetDef.model_validate(
        {
            "name": "Things",
            "keys": [{"name": k} for k in keys],
            "operations": ["list", "get"],
            "fields": list(fields),
        }
    )
    definition = {"entity_sets": [es.model_dump(mode="json")], "operations": []}
    return es, service_payload(odata_version="v4", service_path=V4_PATH, definition=definition)


async def test_an_entity_with_a_property_called_error_can_be_read():
    es, service = _one_set(_f("Id"), _f("error"), _f("detail", "SRV.Detail"))
    for row in (
        {"@odata.context": CONTEXT, "Id": "1", "error": "E1"},
        {"Id": "1", "error": {"code": "E1", "message": "a field, not an envelope"}},
        {"error": "E1"},  # not an object: not an envelope
    ):
        got = await client(Sap(row), service).get(es, {"Id": "1"}, select=[], expand=[])
        assert got["item"]["error"] == row["error"]
    listed = await client(Sap(page([{"Id": "1", "error": "E1"}])), service).list(es, query())
    assert listed["items"] == [{"Id": "1", "error": "E1"}]
    # The envelope itself -- an object under `error` and no other property -- in a 200.
    for envelope in (
        {"error": {"code": "A", "message": "b"}},
        {"@odata.context": CONTEXT, "error": {"code": "A", "message": "b"}},
        {"error": {}},
    ):
        error = await refused(
            client(Sap(envelope), service).get(es, {"Id": "1"}, select=[], expand=[])
        )
        assert error.code == "sap_error"
    # A write stays strict: any top-level `error` is not a confirmation.
    rules = ODataClient(None, service, V4Dialect())
    assert rules._is_entity({"Id": "1"}) is True
    assert rules._is_entity({"Id": "1", "error": "E1"}) is False
    assert rules._is_entity({"Id": "1", "error": {"code": "A"}}) is False


async def test_a_null_value_wrapper_is_no_item():
    answer = {"@odata.context": CONTEXT, "value": None}
    result = await client(Sap(answer)).get(ES_ITEM, KEY, select=[], expand=[], navigation="_Header")
    assert result == {"item": None, "truncated": False}
    # An entity that really has a field called `value` is still an entity.
    es, service = _one_set(_f("Id"), _f("value"))
    got = await client(Sap({"Id": "1", "value": None}), service).get(
        es, {"Id": "1"}, select=[], expand=[]
    )
    assert got["item"] == {"Id": "1", "value": None}


@pytest.mark.parametrize("dialect", [V2Dialect(), V4Dialect()])
@pytest.mark.parametrize("edm_type", ["Edm.Double", "Edm.Single"])
def test_a_huge_number_as_a_float_key_is_an_invalid_key(dialect, edm_type):
    for huge in (10**400, -(10**400)):
        with pytest.raises(ODataError) as excinfo:
            dialect.literal(edm_type, huge)
        assert excinfo.value.code == "invalid_argument"
        with pytest.raises(ODataError) as excinfo:
            dialect.key_segment(keyed({"K": edm_type}), {"K": huge})
        assert excinfo.value.code == "invalid_key"
    assert dialect.literal(edm_type, 2).startswith("2.0")


async def test_v4_requests_say_which_version_they_speak():
    other = dict(ITEM_ROW, PurchaseRequisitionItem="00020")
    sap = Sap(
        page([ITEM_ROW], **{"@odata.nextLink": "PurchaseRequisitionItem?$skiptoken=1"}),
        page([other]),
        ITEM_ROW,
    )
    c = client(sap)
    await c.list(ES_ITEM, query(top=5))
    await c.get(ES_ITEM, KEY, select=[], expand=[])
    assert len(sap.requests) == 3
    for request in sap.requests:
        assert request.headers["OData-MaxVersion"] == "4.0"
        assert request.headers["Accept"] == "application/json"
    # V2 sends no such header.
    v2_service = service_payload()
    es = ServiceDefinition.model_validate(v2_service["definition"]).entity_set(
        "A_PurchaseRequisitionItem"
    )
    sap = Sap({"d": {"results": []}})
    await ODataClient(sap_v2(sap), v2_service, V2Dialect()).list(es, query(top=5))
    assert "odata-maxversion" not in sap.requests[0].headers


def test_the_write_refusal_has_one_source_and_was_not_sent():
    c = client(Sap(page([])), sessions=CsrfSessionStore())
    with pytest.raises(ODataError) as excinfo:
        c.check_write(ES_HEADER, "delete", key={"PurchaseRequisition": "1"})
    assert excinfo.value.message == V4Dialect.write_refusal and excinfo.value.sent is False
    with pytest.raises(ODataError) as excinfo:
        V4Dialect().encode_body(ES_HEADER, {"Description": "x"})
    assert excinfo.value.message == V4Dialect.write_refusal


async def test_a_read_error_never_says_sent():
    error = await refused(client(Sap(v4_error(400, "A", "b"))).list(ES_ITEM, query()))
    assert error.sent is False and "sent" not in error.to_dict()
