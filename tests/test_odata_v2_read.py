"""The OData V2 read client (Task O2): what reaches SAP, and what comes back.

SAP is an ``httpx.MockTransport`` behind the real ``destination_http_client``
(``tests/odata_helpers.py``), so every assertion on a request is an assertion
on what the back end would have received.
"""

from __future__ import annotations

import json
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

from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination_auth import DestinationUserRequired  # noqa: E402
from agents.odata import client as client_module  # noqa: E402
from agents.odata.client import (  # noqa: E402
    MAX_NEXT_HOPS,
    ODataClient,
    ODataError,
    ReadQuery,
    clip_result,
)
from agents.odata.models import EntitySetDef, ServiceDefinition  # noqa: E402
from agents.odata.v2 import V2Dialect  # noqa: E402
from tests.odata_helpers import (  # noqa: E402
    SERVICE_PATH,
    FakeResolver,
    Sap,
    sap_v2,
    service_payload,
    v2_error,
)

GUID = "01234567-89ab-cdef-0123-456789abcdef"
SERVICE = service_payload()
DEFINITION = ServiceDefinition.model_validate(SERVICE["definition"])
ES_HEADER = DEFINITION.entity_set("A_PurchaseRequisitionHeader")
ES_ITEM = DEFINITION.entity_set("A_PurchaseRequisitionItem")
ITEM_PATH = SERVICE_PATH + "/A_PurchaseRequisitionItem"
ITEM_URL = "http://s4.internal:44300" + ITEM_PATH

ROW = {
    "__metadata": {
        "uri": ITEM_URL + "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')",
        "type": "SRV.A_PurchaseRequisitionItemType",
        "etag": "W/\"datetime'2026-01-01'\"",
    },
    "PurchaseRequisition": "10000001",
    "PurchaseRequisitionItem": "00010",
    "Plant": "1000",
    "Material": "TG11",
    "CreatedByUser": "ALICE",
    "to_PurchaseReqn": {"__deferred": {"uri": ITEM_URL + "(...)/to_PurchaseReqn"}},
}
HEADER_ROW = {
    "__metadata": {"uri": "x", "etag": 'W/"1"'},
    "PurchaseRequisition": "10000001",
    "PurReqnDescription": "Pumps",
    "CreatedByUser": "ALICE",
    "to_PurchaseReqnItem": {"__deferred": {"uri": "x"}},
}
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}


def client(sap, service=None, **kwargs) -> ODataClient:
    return ODataClient(sap_v2(sap, **kwargs), service or SERVICE, V2Dialect())


def query(**overrides) -> ReadQuery:
    values = {"select": [], "filter": None, "expand": [], "orderby": [], "top": 50, "skip": 0}
    values.update(overrides)
    return ReadQuery(**values)


def page(rows, **extra):
    return {"d": {"results": rows, **extra}}


# -- the dialect -------------------------------------------------------------


def test_key_literals():
    d = V2Dialect()
    assert (
        d.key_segment(
            ES_ITEM, {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}
        )
        == "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
    )
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": "10000001"}) == "('10000001')"
    assert (
        d.literal("Edm.String", "O'Neil") == "'O''Neil'"
        and d.literal("Edm.Guid", GUID) == f"guid'{GUID}'"
    )
    assert d.literal("Edm.Int32", 5) == "5" and d.literal("Edm.Boolean", True) == "true"
    for bad in (
        {"PurchaseRequisition": "1"},
        {"PurchaseRequisition": "1", "PurchaseRequisitionItem": "1", "X": "1"},
        {"PurchaseRequisition": "1')/Other('", "PurchaseRequisitionItem": "1"},
        {},
        None,
        "10000001",
        ["10000001", "00010"],
    ):
        with pytest.raises(ODataError) as excinfo:
            d.key_segment(ES_ITEM, bad)
        assert excinfo.value.code == "invalid_key"
    # A quote cannot end the literal: it is doubled and stays inside it.
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": "1')Other('"}) == "('1''%29Other%28''')"


@pytest.mark.parametrize(
    "value",
    [
        "a/b",
        "a\\b",
        "a%2Fb",
        "50%",
        "a?b",
        "a#b",
        "a\nb",
        "a\x00b",
        "..",
        "a..b",
        "x" * 300,
        None,
        True,
        1.5,
        ["1"],
        {"a": 1},
    ],
)
def test_key_values_that_cannot_stay_one_path_segment_are_refused(value):
    with pytest.raises(ODataError) as excinfo:
        V2Dialect().key_segment(ES_HEADER, {"PurchaseRequisition": value})
    assert excinfo.value.code == "invalid_key"
    if isinstance(value, str) and len(value) > 3:
        assert value not in excinfo.value.message


def test_key_segment_is_percent_encoded_for_the_path():
    d = V2Dialect()
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": "A B"}) == "('A%20B')"
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": "café&x=1"}) == "('caf%C3%A9%26x%3D1')"
    assert d.key_segment(ES_HEADER, {"PurchaseRequisition": 4500000001}) == "('4500000001')"


def test_typed_literals():
    d = V2Dialect()
    assert d.literal("Edm.Int64", 5) == "5L" and d.literal("Edm.Int16", "-7") == "-7"
    assert d.literal("Edm.Decimal", "12.50") == "12.50M" and d.literal("Edm.Double", 1.5) == "1.5d"
    assert d.literal("Edm.Boolean", False) == "false" and d.literal("Edm.Boolean", "true") == "true"
    assert d.literal("Edm.DateTime", "2026-01-31T10:00:00") == "datetime'2026-01-31T10:00:00'"
    assert (
        d.literal("Edm.DateTimeOffset", "2026-01-31T10:00:00Z")
        == "datetimeoffset'2026-01-31T10:00:00Z'"
    )
    assert d.literal("Edm.Time", "PT10H30M") == "time'PT10H30M'"
    for edm_type, bad in (
        ("Edm.Guid", "1' or '1' eq '1"),
        ("Edm.Int32", "5 or 1 eq 1"),
        ("Edm.Int32", True),
        ("Edm.Int32", 1.5),
        ("Edm.Decimal", "1e400x"),
        ("Edm.Boolean", "yes"),
        ("Edm.DateTime", "2026-01-31'"),
        ("Edm.Time", "10:30"),
        ("Edm.Binary", "00"),
        ("Edm.Unknown", "x"),
        ("Edm.String", "a\nb"),
        ("Edm.String", None),
        ("Edm.Double", float("nan")),
        ("Edm.Double", float("inf")),
    ):
        with pytest.raises(ODataError) as excinfo:
            d.literal(edm_type, bad)
        assert excinfo.value.code == "invalid_argument"


def test_read_params_and_parsers():
    d = V2Dialect()
    assert d.read_params(
        query(
            select=["A", "B"],
            filter="A eq 1",
            expand=["N"],
            orderby=["A desc", "B"],
            top=5,
            skip=10,
        )
    ) == {
        "$format": "json",
        "$select": "A,B",
        "$filter": "A eq 1",
        "$expand": "N",
        "$orderby": "A desc,B",
        "$top": "5",
        "$skip": "10",
        "$inlinecount": "allpages",
    }
    assert d.read_params(query(top=0, count=False)) == {"$format": "json"}
    assert d.parse_list({"d": [{"A": 1}]}) == ([{"A": 1}], None, None)
    assert d.parse_list({"d": {"results": [], "__count": "0"}}) == ([], 0, None)
    assert d.parse_entity({"d": {"A": 1, "__metadata": {"etag": 'W/"1"'}}})[1] == 'W/"1"'
    for bad in ({}, {"d": "x"}, {"d": {"results": "x"}}, [], None, {"d": {"A": 1}}):
        with pytest.raises(ODataError):
            d.parse_list(bad)
    for bad in ({}, {"d": []}, None, "x"):
        with pytest.raises(ODataError):
            d.parse_entity(bad)
    assert d.parse_error(v2_error(400, "SY/530", "Invalid filter")) == ("SY/530", "Invalid filter")
    assert d.parse_error(httpx.Response(500, html="<html><body><h1>500</h1></body></html>")) == (
        "",
        "",
    )
    assert d.parse_error(httpx.Response(400, json={"error": "x"})) == ("", "")
    assert d.parse_error(httpx.Response(400, json=["x"])) == ("", "")
    xml = (
        '<?xml version="1.0"?><error xmlns="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
        "<code>/IWFND/CM_BEC/026</code>"
        '<message xml:lang="en">No service found &amp; more</message></error>'
    )
    assert d.parse_error(
        httpx.Response(404, content=xml, headers={"content-type": "application/xml"})
    ) == ("/IWFND/CM_BEC/026", "No service found & more")


# -- list --------------------------------------------------------------------


async def test_list_sends_select_and_parses_d_results():
    sap = Sap({"d": {"results": [ROW], "__count": "37", "__next": ITEM_URL + "?$skiptoken=1"}})
    out = await client(sap).list(
        ES_ITEM,
        ReadQuery(
            select=["PurchaseRequisition"],
            filter="Plant eq '1000'",
            expand=[],
            orderby=["Plant desc"],
            top=1,
            skip=0,
        ),
    )
    q = sap.requests[0].url.params
    assert (
        q["$select"] == "PurchaseRequisition"
        and q["$filter"] == "Plant eq '1000'"
        and q["$top"] == "1"
        and q["$inlinecount"] == "allpages"
        and q["$format"] == "json"
    )
    assert q["$orderby"] == "Plant desc"
    assert sap.requests[0].url.path == "/sap/opu/odata/sap/SRV/A_PurchaseRequisitionItem"
    assert out == {
        "items": [{"PurchaseRequisition": "10000001"}],
        "count": 37,
        "next_skip": 1,
        "truncated": False,
    }
    assert len(sap.requests) == 1


async def test_the_request_goes_to_the_destination_with_its_credential():
    sap = Sap(page([ROW]))
    await client(sap).list(ES_ITEM, query())
    request = sap.requests[0]
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.port) == (
        "https",
        "s4.internal",
        44300,
    )
    assert request.headers["Authorization"] == "Bearer dest-token"
    assert request.headers["Accept"] == "application/json"


async def test_a_user_context_service_without_a_user_is_refused_before_any_call():
    sap = Sap(page([ROW]))
    with pytest.raises(DestinationUserRequired):
        await client(sap, user_context=True).list(ES_ITEM, query())
    assert sap.requests == []


async def test_a_user_context_service_calls_as_the_signed_in_user():
    sap = Sap(page([ROW]))
    resolver = FakeResolver()
    j, p = current_jwt.set("jwt-alice"), current_principal.set("alice@example.com")
    try:
        await client(sap, user_context=True, resolver=resolver).list(ES_ITEM, query())
    finally:
        current_jwt.reset(j)
        current_principal.reset(p)
    assert sap.requests[0].headers["Authorization"] == "Bearer user-token-of-alice@example.com"
    assert resolver.calls == [("jwt-alice", "alice@example.com")]


async def test_rows_carry_only_selectable_fields_and_no_metadata():
    sap = Sap(page([dict(ROW, Unlisted="x", Complex={"__metadata": {"type": "T"}, "A": 1})]))
    out = await client(sap).list(ES_ITEM, query())
    # No select given: every selectable field is asked for, never "all".
    assert (
        sap.requests[0].url.params["$select"]
        == "PurchaseRequisition,PurchaseRequisitionItem,Plant,Material"
    )
    assert out["items"] == [
        {
            "PurchaseRequisition": "10000001",
            "PurchaseRequisitionItem": "00010",
            "Plant": "1000",
            "Material": "TG11",
        }
    ]
    assert "ALICE" not in json.dumps(out) and "__" not in json.dumps(out)


async def test_a_row_is_cut_to_the_requested_select_even_when_sap_ignores_it():
    sap = Sap(page([ROW]))
    out = await client(sap).list(ES_ITEM, query(select=["Plant", "Plant"]))
    assert sap.requests[0].url.params["$select"] == "Plant"
    assert out["items"] == [{"Plant": "1000"}]


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"select": ["CreatedByUser"]}, "field_not_selectable"),
        ({"select": ["Nope"]}, "unknown_field"),
        ({"select": ["Plant,CreatedByUser"]}, "unknown_field"),
        ({"select": ["*"]}, "unknown_field"),
        ({"select": [5]}, "unknown_field"),
        ({"select": "Plant"}, "invalid_argument"),
        ({"filter": "CreatedByUser eq 'ALICE'"}, "field_not_filterable"),
        ({"filter": "Nope eq 1"}, "unknown_field"),
        ({"filter": "Plant eq '1'&$top=9"}, "invalid_argument"),
        ({"orderby": ["CreatedByUser"]}, "field_not_selectable"),
        ({"orderby": ["Nope desc"]}, "unknown_field"),
        ({"orderby": ["Plant desc,CreatedByUser"]}, "invalid_argument"),
        ({"orderby": ["Plant sideways"]}, "invalid_argument"),
        ({"orderby": "Plant"}, "invalid_argument"),
        ({"expand": ["to_Nowhere"]}, "unknown_target"),
        ({"expand": ["to_PurchaseReqn/to_PurchaseReqnItem"]}, "unknown_target"),
        ({"top": 0}, "invalid_argument"),
        ({"top": -1}, "invalid_argument"),
        ({"top": 100_000}, "invalid_argument"),
        ({"top": True}, "invalid_argument"),
        ({"top": "5"}, "invalid_argument"),
        ({"skip": -1}, "invalid_argument"),
    ],
)
async def test_what_the_catalogue_does_not_allow_never_reaches_sap(overrides, code):
    sap = Sap(page([ROW]))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query(**overrides))
    assert excinfo.value.code == code
    assert sap.requests == []


async def test_a_disabled_operation_never_reaches_sap():
    definition = service_payload()["definition"]
    definition["entity_sets"][1]["operations"] = ["get"]
    service = service_payload(definition=definition)
    items = ServiceDefinition.model_validate(service["definition"]).entity_set(
        "A_PurchaseRequisitionItem"
    )
    sap = Sap(page([ROW]))
    with pytest.raises(ODataError) as excinfo:
        await client(sap, service).list(items, query())
    assert excinfo.value.code == "operation_disabled"
    header = ServiceDefinition.model_validate(service["definition"]).entity_set(
        "A_PurchaseRequisitionHeader"
    )
    for call in (
        client(sap, service).list(
            header, query(), key={"PurchaseRequisition": "1"}, navigation="to_PurchaseReqnItem"
        ),
        client(sap, service).list(header, query(expand=["to_PurchaseReqnItem"])),
    ):
        with pytest.raises(ODataError) as excinfo:
            await call
        assert excinfo.value.code == "operation_disabled"
    assert sap.requests == []


async def test_orderby_is_normalised():
    sap = Sap(page([ROW]))
    await client(sap).list(
        ES_ITEM, query(orderby=["Plant  desc", " Material ", "PurchaseRequisition asc"])
    )
    assert sap.requests[0].url.params["$orderby"] == "Plant desc,Material,PurchaseRequisition asc"


async def test_expand_adds_the_targets_selectable_fields_and_filters_them():
    row = dict(
        HEADER_ROW,
        to_PurchaseReqnItem={"results": [ROW, dict(ROW, PurchaseRequisitionItem="00020")]},
    )
    sap = Sap(page([row]))
    out = await client(sap).list(
        ES_HEADER, query(select=["PurchaseRequisition"], expand=["to_PurchaseReqnItem"])
    )
    q = sap.requests[0].url.params
    assert q["$expand"] == "to_PurchaseReqnItem"
    assert q["$select"] == (
        "PurchaseRequisition,to_PurchaseReqnItem/PurchaseRequisition,"
        "to_PurchaseReqnItem/PurchaseRequisitionItem,to_PurchaseReqnItem/Plant,"
        "to_PurchaseReqnItem/Material"
    )
    assert out["items"] == [
        {
            "PurchaseRequisition": "10000001",
            "to_PurchaseReqnItem": [
                {
                    "PurchaseRequisition": "10000001",
                    "PurchaseRequisitionItem": "00010",
                    "Plant": "1000",
                    "Material": "TG11",
                },
                {
                    "PurchaseRequisition": "10000001",
                    "PurchaseRequisitionItem": "00020",
                    "Plant": "1000",
                    "Material": "TG11",
                },
            ],
        }
    ]
    assert "ALICE" not in json.dumps(out)


async def test_an_expanded_single_navigation_and_an_unexpanded_one():
    sap = Sap(page([dict(ROW, to_PurchaseReqn=HEADER_ROW)]), page([ROW]))
    out = await client(sap).list(ES_ITEM, query(select=["Plant"], expand=["to_PurchaseReqn"]))
    assert out["items"] == [
        {
            "Plant": "1000",
            "to_PurchaseReqn": {"PurchaseRequisition": "10000001", "PurReqnDescription": "Pumps"},
        }
    ]
    # SAP left the navigation deferred: nothing is invented for it.
    out = await client(sap).list(ES_ITEM, query(select=["Plant"], expand=["to_PurchaseReqn"]))
    assert out["items"] == [{"Plant": "1000"}]


async def test_navigation_read_uses_the_target_entity_sets_fields():
    sap = Sap(page([ROW], __count="1"))
    out = await client(sap).list(
        ES_HEADER,
        query(select=["Plant"], filter="Plant eq '1000'", orderby=["Plant"]),
        key={"PurchaseRequisition": "10000001"},
        navigation="to_PurchaseReqnItem",
    )
    request = sap.requests[0]
    assert (
        request.url.path
        == SERVICE_PATH + "/A_PurchaseRequisitionHeader('10000001')/to_PurchaseReqnItem"
    )
    assert request.url.params["$select"] == "Plant"
    assert out == {"items": [{"Plant": "1000"}], "count": 1, "truncated": False}
    # A field of the *source* entity set means nothing on the target.
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(
            ES_HEADER,
            query(select=["PurReqnDescription"]),
            key={"PurchaseRequisition": "10000001"},
            navigation="to_PurchaseReqnItem",
        )
    assert excinfo.value.code == "unknown_field"


@pytest.mark.parametrize(
    "key, navigation, code",
    [
        ({"PurchaseRequisition": "1"}, "to_Nowhere", "unknown_target"),
        ({"PurchaseRequisition": "1"}, "to_PurchaseReqnItem/to_PurchaseReqn", "unknown_target"),
        ({"PurchaseRequisition": "1"}, "../A_Other", "unknown_target"),
        ({"PurchaseRequisition": "1"}, 5, "unknown_target"),
        (None, "to_PurchaseReqnItem", "invalid_key"),
        ({"Wrong": "1"}, "to_PurchaseReqnItem", "invalid_key"),
        (
            {"PurchaseRequisition": "1"},
            None,
            "invalid_argument",
        ),  # a key without a navigation is a get
    ],
)
async def test_navigation_refusals(key, navigation, code):
    sap = Sap(page([ROW]))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_HEADER, query(), key=key, navigation=navigation)
    assert excinfo.value.code == code
    assert sap.requests == []


async def test_a_single_navigation_is_a_get_and_a_collection_is_a_list():
    sap = Sap({"d": HEADER_ROW})
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query(), key=KEY, navigation="to_PurchaseReqn")
    assert excinfo.value.code == "invalid_argument"
    with pytest.raises(ODataError) as excinfo:
        await client(sap).get(
            ES_HEADER,
            {"PurchaseRequisition": "1"},
            select=[],
            expand=[],
            navigation="to_PurchaseReqnItem",
        )
    assert excinfo.value.code == "invalid_argument"
    assert sap.requests == []
    out = await client(sap).get(ES_ITEM, KEY, select=[], expand=[], navigation="to_PurchaseReqn")
    assert sap.requests[0].url.path == (
        ITEM_PATH
        + "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')/to_PurchaseReqn"
    )
    assert out == {
        "item": {"PurchaseRequisition": "10000001", "PurReqnDescription": "Pumps"},
        "etag": 'W/"1"',
        "truncated": False,
    }


# -- paging ------------------------------------------------------------------


async def test_a_confined_next_link_is_followed_until_top_rows():
    rows = [dict(ROW, PurchaseRequisitionItem=f"{n:05d}") for n in range(1, 6)]
    sap = Sap(
        page(rows[:2], __count="5", __next=ITEM_URL + "?$skiptoken=2&$select=Plant"),
        page(rows[2:4], __next=ITEM_URL + "?$skiptoken=4"),
        page(rows[4:]),
    )
    out = await client(sap).list(ES_ITEM, query(select=["PurchaseRequisitionItem"], top=3, skip=0))
    assert [r["PurchaseRequisitionItem"] for r in out["items"]] == ["00001", "00002", "00003"]
    assert out["count"] == 5 and out["next_skip"] == 3
    assert len(sap.requests) == 2
    follow = sap.requests[1]
    assert follow.url.path == ITEM_PATH and follow.url.host == "s4.internal"
    assert dict(follow.url.params) == {
        "$skiptoken": "2",
        "$select": "Plant",
    }  # SAP's link, as it sent it
    assert follow.headers["Authorization"] == "Bearer dest-token"


async def test_the_last_page_has_no_next_skip():
    sap = Sap(page([ROW], __count="1"))
    out = await client(sap).list(ES_ITEM, query(skip=0))
    assert out == {
        "items": [
            {
                "PurchaseRequisition": "10000001",
                "PurchaseRequisitionItem": "00010",
                "Plant": "1000",
                "Material": "TG11",
            }
        ],
        "count": 1,
        "truncated": False,
    }
    sap = Sap(page([ROW], __count="41"))
    out = await client(sap).list(ES_ITEM, query(skip=40))
    assert "next_skip" not in out and out["count"] == 41


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/collect?x=1",
        "//evil.example" + ITEM_PATH,
        "https://s4.internal:44300/sap/opu/odata/sap/OTHER/Set?$skiptoken=1",
        "https://s4.internal:44300" + SERVICE_PATH + "/../OTHER/Set",
        "https://s4.internal:44300"
        + SERVICE_PATH
        + "/A_PurchaseRequisitionHeader?$skiptoken=1",  # another entity set
        "https://s4.internal:44300" + ITEM_PATH + "/%2e%2e/A_PurchaseRequisitionHeader",
        {"uri": ITEM_URL},
    ],
)
async def test_a_foreign_next_link_is_not_followed_and_next_skip_is_still_set(link):
    sap = Sap(page([ROW], __next=link))
    out = await client(sap).list(ES_ITEM, query(top=10, skip=20, count=False))
    assert len(sap.requests) == 1
    assert "$inlinecount" not in sap.requests[0].url.params
    if isinstance(link, str):
        assert out["next_skip"] == 21
    assert len(out["items"]) == 1 and "count" not in out


async def test_next_links_are_followed_a_bounded_number_of_times():
    sap = Sap(page([ROW], __next=ITEM_URL + "?$skiptoken=1"))  # the same page, forever
    out = await client(sap).list(ES_ITEM, query(top=100))
    assert len(sap.requests) == 1 + MAX_NEXT_HOPS
    assert len(out["items"]) == 1 + MAX_NEXT_HOPS and out["next_skip"] == 1 + MAX_NEXT_HOPS
    sap = Sap(
        page([ROW], __next=ITEM_URL + "?$skiptoken=1"), page([], __next=ITEM_URL + "?$skiptoken=1")
    )
    await client(sap).list(ES_ITEM, query(top=100))
    assert len(sap.requests) == 2  # an empty page ends it


# -- get ---------------------------------------------------------------------


async def test_get_returns_item_and_etag():
    sap = Sap({"d": ROW})
    out = await client(sap).get(ES_ITEM, KEY, select=["Plant", "Material"], expand=[])
    request = sap.requests[0]
    assert (
        request.url.path
        == ITEM_PATH + "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
    )
    assert dict(request.url.params) == {"$format": "json", "$select": "Plant,Material"}
    assert out == {
        "item": {"Plant": "1000", "Material": "TG11"},
        "etag": "W/\"datetime'2026-01-01'\"",
        "truncated": False,
    }


async def test_get_falls_back_to_the_etag_header_and_omits_a_missing_one():
    row = {k: v for k, v in ROW.items() if k != "__metadata"}
    sap = Sap(httpx.Response(200, json={"d": row}, headers={"ETag": 'W/"7"'}), {"d": row})
    assert (await client(sap).get(ES_ITEM, KEY, select=["Plant"], expand=[]))["etag"] == 'W/"7"'
    assert await client(sap).get(ES_ITEM, KEY, select=["Plant"], expand=[]) == {
        "item": {"Plant": "1000"},
        "truncated": False,
    }


async def test_get_needs_the_whole_key_and_keeps_it_inside_the_segment():
    sap = Sap({"d": HEADER_ROW})
    for bad in (
        None,
        {},
        {"PurchaseRequisition": "1"},
        {"PurchaseRequisition": "1", "PurchaseRequisitionItem": "a/b"},
    ):
        with pytest.raises(ODataError) as excinfo:
            await client(sap).get(ES_ITEM, bad, select=[], expand=[])
        assert excinfo.value.code == "invalid_key"
    assert sap.requests == []
    await client(sap).get(
        ES_HEADER,
        {"PurchaseRequisition": "1')/to_PurchaseReqnItem('"[:3] + "') or ('"},
        select=[],
        expand=[],
    )
    path = sap.requests[0].url.path
    assert path.startswith(SERVICE_PATH + "/A_PurchaseRequisitionHeader('")
    assert path.count("/") == SERVICE_PATH.count("/") + 1  # still one segment below the root
    assert "'')" in path  # the quote was doubled


async def test_a_null_single_navigation_is_an_empty_item():
    sap = Sap(httpx.Response(204), {"d": None})
    for _ in range(2):
        out = await client(sap).get(
            ES_ITEM, KEY, select=[], expand=[], navigation="to_PurchaseReqn"
        )
        assert out == {"item": None, "truncated": False}


# -- errors ------------------------------------------------------------------


async def test_sap_error_envelope_becomes_a_short_message():
    sap = Sap(v2_error(400, "005056A509B11EE1B9A8FEC11C21578E", "Invalid filter"))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query())
    error = excinfo.value
    assert (error.code, error.status) == ("sap_error", 400)
    assert error.message == "005056A509B11EE1B9A8FEC11C21578E: Invalid filter"
    assert str(error) == error.message
    sap = Sap(
        v2_error(403, "/IWFND/CM_BEC/026", "No authorization\r\n\tfor <b>service</b> " + "x" * 2000)
    )
    with pytest.raises(ODataError) as excinfo:
        await client(sap).get(ES_ITEM, KEY, select=[], expand=[])
    error = excinfo.value
    assert error.status == 403 and len(error.message) <= 500
    assert error.message.startswith("/IWFND/CM_BEC/026: No authorization for <b>service</b> x")
    assert "\n" not in error.message and "\t" not in error.message
    assert error.hint


async def test_html_error_page_does_not_leak_into_the_message():
    html = (
        "<html><head><title>500 SAP Internal Server Error</title></head>"
        "<body>dump at host s4.internal</body></html>"
    )
    sap = Sap(httpx.Response(500, html=html))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query())
    error = excinfo.value
    assert (error.code, error.status, error.message) == (
        "sap_error",
        500,
        "HTTP 500 from the OData service",
    )
    assert "<" not in error.message and "s4.internal" not in (error.hint or "")


async def test_a_sign_in_page_or_a_redirect_is_an_error_not_data():
    sap = Sap(httpx.Response(200, html="<html><body>Logon</body></html>"))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query())
    assert excinfo.value.code == "sap_error" and "Logon" not in excinfo.value.message
    sap = Sap(httpx.Response(302, headers={"Location": "https://evil.example/login?next=x"}))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query())
    assert excinfo.value.status == 302 and "evil" not in excinfo.value.message + (
        excinfo.value.hint or ""
    )
    assert len(sap.requests) == 1  # a redirect is never followed


async def test_an_unexpected_shape_is_an_error():
    for answer in ({"value": []}, {"d": {"A": 1}}, ["x"], "text"):
        with pytest.raises(ODataError) as excinfo:
            await client(Sap(answer)).list(ES_ITEM, query())
        assert excinfo.value.code == "sap_error"


async def test_a_transport_failure_is_a_generic_destination_error():
    def boom(request):
        raise httpx.ConnectError("connect to https://s4.internal:44300/secret?token=abc failed")

    with pytest.raises(ODataError) as excinfo:
        await client(boom).list(ES_ITEM, query())
    error = excinfo.value
    assert error.code == "destination_error" and error.status is None
    assert "s4.internal" not in error.message and "token" not in error.message
    assert error.__cause__ is None  # the httpx text does not travel along


async def test_an_oversized_answer_is_refused(monkeypatch):
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 2000)
    sap = Sap(page([dict(ROW, Material="x" * 5000)]))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(ES_ITEM, query())
    assert excinfo.value.code == "sap_error" and excinfo.value.hint


async def test_filter_travels_only_as_a_parameter_value(monkeypatch):
    monkeypatch.setattr(
        client_module, "check_filter", lambda expr, fields: None
    )  # check_filter bypassed
    sap = Sap(page([ROW]))
    hostile = "x eq 1&$top=9999&$select=CreatedByUser#frag /../OTHER?y=1"
    await client(sap).list(ES_ITEM, query(select=["Plant"], filter=hostile, top=5))
    url = sap.requests[0].url
    assert url.params["$filter"] == hostile
    assert url.params.get_list("$top") == ["5"] and url.params.get_list("$select") == ["Plant"]
    assert url.path == ITEM_PATH and url.fragment == ""
    assert sorted(url.params.keys()) == ["$filter", "$format", "$inlinecount", "$select", "$top"]


# -- the client's own inputs -------------------------------------------------


def test_a_service_without_a_confined_path_or_a_valid_definition_is_refused():
    http = sap_v2(Sap())
    for service_path in ("/sap/opu/../x", "https://evil.example/sap", "", None):
        with pytest.raises(ODataError) as excinfo:
            ODataClient(http, dict(SERVICE, service_path=service_path), V2Dialect())
        assert excinfo.value.code == "invalid_argument"
    with pytest.raises(ODataError) as excinfo:
        ODataClient(http, dict(SERVICE, definition={"entity_sets": "s3cr3t"}), V2Dialect())
    assert "s3cr3t" not in excinfo.value.message
    ODataClient(
        http, dict(SERVICE, definition=DEFINITION), V2Dialect()
    )  # a parsed definition is fine


async def test_an_entity_set_path_overrides_the_name():
    definition = service_payload()["definition"]
    definition["entity_sets"][1]["path"] = "Items.v2"
    service = service_payload(definition=definition)
    items = ServiceDefinition.model_validate(service["definition"]).entity_set(
        "A_PurchaseRequisitionItem"
    )
    sap = Sap(page([ROW]))
    await client(sap, service).list(items, query())
    assert sap.requests[0].url.path == SERVICE_PATH + "/Items.v2"


async def test_an_entity_set_whose_name_cannot_be_a_segment_is_refused():
    odd = EntitySetDef(
        name="A..B", keys=[], operations=["list"], fields=[{"name": "Plant", "selectable": True}]
    )
    sap = Sap(page([ROW]))
    with pytest.raises(ODataError) as excinfo:
        await client(sap).list(odd, query())
    assert excinfo.value.code == "invalid_argument" and sap.requests == []


def test_odata_error_shape():
    error = ODataError("sap_error", "boom", status=400, hint="try less")
    assert error.to_dict() == {"code": "sap_error", "message": "boom", "hint": "try less"}
    assert ODataError("invalid_key", "bad").to_dict() == {"code": "invalid_key", "message": "bad"}
    assert ODataError("x", "y").status is None and ODataError("x", "y").hint is None


# -- clip_result -------------------------------------------------------------


def test_clip_result_truncates_by_size():
    items = [{"n": n, "text": "x" * 100} for n in range(50)]
    result = {"items": items, "count": 500, "next_skip": 50, "truncated": False}
    assert clip_result(result, 100_000) == result
    clipped = clip_result(result, 2000)
    assert clipped["truncated"] is True and 0 < len(clipped["items"]) < 50
    assert len(json.dumps(clipped)) <= 2000
    assert clipped["items"] == items[: len(clipped["items"])]
    assert clipped["next_skip"] == len(clipped["items"])  # the page continues where it was cut
    assert clipped["count"] == 500
    assert len(result["items"]) == 50 and result["truncated"] is False  # the input is not changed
    # One more item would not have fitted.
    one_more = dict(clipped, items=items[: len(clipped["items"]) + 1])
    assert len(json.dumps(one_more)) > 2000


def test_clip_result_sets_next_skip_when_the_last_page_is_cut():
    items = [{"n": n, "text": "x" * 100} for n in range(50)]
    clipped = clip_result({"items": items, "truncated": False}, 2000, skip=100)
    assert clipped["truncated"] is True and clipped["next_skip"] == 100 + len(clipped["items"])
    clipped = clip_result({"items": items, "truncated": False}, 2000)
    assert clipped["truncated"] is True and "next_skip" not in clipped


def test_clip_result_handles_an_item_a_call_result_and_the_impossible():
    item = {
        "item": {
            "Plant": "1000",
            "Text": "y" * 5000,
            "to_Items": [{"n": n, "t": "z" * 50} for n in range(100)],
        },
        "etag": 'W/"1"',
        "truncated": False,
    }
    clipped = clip_result(item, 1500)
    assert clipped["truncated"] is True and len(json.dumps(clipped)) <= 1500
    assert clipped["item"]["Plant"] == "1000" and clipped["etag"] == 'W/"1"'
    assert len(item["item"]["to_Items"]) == 100  # the input is not changed
    call = clip_result(
        {"result": [{"n": n, "t": "z" * 50} for n in range(100)], "truncated": False}, 500
    )
    assert (
        call["truncated"] is True and 0 < len(call["result"]) < 100 and len(json.dumps(call)) <= 500
    )
    text = clip_result({"result": "v" * 5000, "truncated": False}, 500)
    assert (
        text["truncated"] is True
        and len(json.dumps(text)) <= 500
        and text["result"].startswith("vvv")
    )
    tiny = clip_result({"items": [{"a": "x" * 500}], "truncated": False}, 60)
    assert tiny == {"items": [], "truncated": True}
    assert clip_result({"ok": True, "status": 204}, 10_000) == {
        "ok": True,
        "status": 204,
        "truncated": False,
    }
