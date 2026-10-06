"""``POST /admin/api/odata/metadata``: the ``$metadata`` import preview.

The route is driven through the real app; SAP is an ``httpx.MockTransport``
and the destination service the ``FakeResolver`` double, both put in through
the two seams of ``agents.odata.preview`` (``_resolver``, ``_transport``).
No network. The scope test mounts the router on a bare app with a stub
validator: the suite's app runs without an XSUAA binding, where every scope
check passes.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
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

import app as app_module  # noqa: E402
from agents import auth  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ODataAuditLog,
    ODataService,
    SessionLocal,
    init_db,
)
from agents.destination import Destination, DestinationError  # noqa: E402
from agents.odata import preview  # noqa: E402
from agents.odata.metadata import parse_metadata  # noqa: E402
from agents.odata.models import MAX_ENTITY_SETS, MAX_FIELDS, MAX_OPERATIONS  # noqa: E402
from tests.odata_helpers import (  # noqa: E402
    SAP_URL,
    FakeConnectivity,
    FakeResolver,
    OnPremiseResolver,
    Sap,
)

URL = "/admin/api/odata/metadata"
SERVICES = "/admin/api/odata/services"
FIXTURES = ROOT / "tests" / "fixtures" / "odata"
V2 = (FIXTURES / "v2_purchasereq_reduced.xml").read_bytes()
V4 = (FIXTURES / "v4_reduced.xml").read_bytes()
PATH = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV"
SAP_HOST = "s4.internal"
# Never part of an answer or a log line.
SECRET = "SECRET"

REQUEST: dict[str, Any] = {
    "destination": "S4_ODATA_TECH",
    "service_path": PATH,
    "odata_version": "v2",
}

ENTITY_SET_KEYS = {
    "name",
    "path",
    "entity_type",
    "label",
    "keys",
    "keys_total",
    "fields",
    "fields_total",
    "navigations",
    "navigations_total",
    "declared",
    "status",
    "new_fields",
    "new_fields_total",
    "removed_fields",
    "changed_keys",
    "changed_types",
    "truncated",
}
FIELD_KEYS = {"name", "type", "label", "declared"}
# The names of the catalogue's switches: never a key of a preview field,
# entity set or operation, so that spreading one into a definition cannot
# switch anything on.
SWITCHES = {"filterable", "selectable", "writable", "enabled", "operations", "changes_data"}
OPERATION_KEYS = {
    "name",
    "qualified_name",
    "kind",
    "http_method",
    "bound_to",
    "parameters",
    "parameters_total",
    "label",
    "status",
    "suggested",
    "truncated",
}
TOP_KEYS = {
    "fetched_at",
    "entity_sets",
    "operations",
    "skipped",
    "removed_entity_sets",
    "removed_operations",
    "removed_complete",
    "skipped_stored_entity_sets",
    "summary",
    "truncated",
    "totals",
    "warnings",
}


def xml(body: bytes, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, content=body, headers={"content-type": "application/xml", **headers}
    )


def request(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(REQUEST)
    data.update(patch)
    return data


def unread(handler):
    """``handler`` with its answers as a real transport hands them over: a
    body that is still to be read. (A response built with ``content=`` is
    already read, and the fetch reads the raw stream.)"""

    def respond(req: httpx.Request) -> httpx.Response:
        response = handler(req)
        if not response.is_stream_consumed and not response.is_closed:
            return response
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(response.content),
        )

    return respond


class Remote:
    """The destination service and SAP of one test."""

    def __init__(self) -> None:
        self.sap = Sap(xml(V2))
        self.resolver: Any = FakeResolver(name="S4_ODATA_TECH")
        self.asked_for: list[str] = []

    def answer(self, *answers: Any) -> None:
        self.sap = Sap(*answers)

    @property
    def requests(self) -> list[httpx.Request]:
        return self.sap.requests


@pytest.fixture
def remote(monkeypatch) -> Remote:
    state = Remote()

    def resolver(name: str) -> Any:
        state.asked_for.append(name)
        return state.resolver

    monkeypatch.setattr(preview, "_resolver", resolver)
    monkeypatch.setattr(
        preview, "_transport", lambda: httpx.MockTransport(unread(state.sap.handler))
    )
    return state


@pytest.fixture(autouse=True)
def _every_slot_is_given_back():
    """Whatever a test did, no preview slot stays taken."""
    assert preview._active == 0
    yield
    assert preview._active == 0


@pytest.fixture(autouse=True)
async def _clean_tables():
    await init_db()
    async with SessionLocal() as s:
        for model in (ODataService, ODataAuditLog, AgentConfig):
            await s.execute(delete(model))
        await s.commit()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def bearer(user: str) -> dict[str, str]:
    """An unsigned-for-our-purposes token: the suite's app has no XSUAA
    binding, so the middleware reads the claims as they are."""
    token = jwt.encode({"user_uuid": user, "scope": ["app.admin"]}, "k" * 32, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def walk(value: Any):
    """Every ``(key, value)`` of a JSON answer, at any depth."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key, inner
            yield from walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from walk(inner)


# ------------------------------------------------------------- the happy path


async def test_v2_preview_lists_what_the_document_declares(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == TOP_KEYS
    assert datetime_is_utc_iso(body["fetched_at"])
    assert [e["name"] for e in body["entity_sets"]] == [
        "A_PurchaseRequisitionHeader",
        "A_PurchaseRequisitionItem",
    ]
    header, item = body["entity_sets"]
    assert set(header) == ENTITY_SET_KEYS
    assert header["path"] == "A_PurchaseRequisitionHeader"
    assert header["entity_type"] == "PURCHASEREQ_SRV.A_PurchaseRequisitionHeaderType"
    assert header["label"] == "Purchase requisition"
    assert header["keys"] == [{"name": "PurchaseRequisition", "type": "Edm.String"}]
    assert header["declared"] == {"creatable": True, "updatable": True, "deletable": False}
    assert item["declared"] == {"creatable": False, "updatable": True, "deletable": False}
    assert header["keys_total"] == 1 and header["navigations_total"] == 1
    assert header["truncated"] is False and header["changed_keys"] is False
    assert header["navigations"] == [
        {"name": "to_PurchaseReqnItem", "target": "A_PurchaseRequisitionItem", "collection": True}
    ]
    assert all(set(f) == FIELD_KEYS for e in body["entity_sets"] for f in e["fields"])
    assert header["fields"][0] == {
        "name": "PurchaseRequisition",
        "type": "Edm.String",
        "label": header["fields"][0]["label"],
        "declared": {"filterable": True, "creatable": False, "updatable": False},
    }
    assert header["fields_total"] == 3 and item["fields_total"] == 10
    # Without `service` everything is new.
    assert {e["status"] for e in body["entity_sets"]} == {"new"}
    assert all(e["new_fields"] == [] and e["removed_fields"] == [] for e in body["entity_sets"])
    (operation,) = body["operations"]
    assert set(operation) == OPERATION_KEYS
    assert operation["name"] == "ReleaseItem" and operation["qualified_name"] == ""
    assert operation["kind"] == "function_import" and operation["http_method"] == "POST"
    assert operation["bound_to"] == "A_PurchaseRequisitionItem"
    assert operation["label"] == "Release item" and operation["status"] == "new"
    assert operation["parameters"][-1] == {
        "name": "ReleaseCode",
        "type": "Edm.String",
        "required": False,
    }
    assert operation["parameters_total"] == 3 and operation["truncated"] is False
    assert body["skipped"] == [] and body["truncated"] is False and body["warnings"] == []
    assert body["removed_entity_sets"] == [] and body["removed_operations"] == []
    assert body["removed_complete"] is True
    assert body["summary"] == {
        "entity_sets": 2,
        "operations": 1,
        "in_service": 0,
        "changed": 0,
        "skipped": 0,
        "removed_entity_sets": 0,
        "removed_operations": 0,
        "skipped_stored_entity_sets": 0,
    }
    assert body["skipped_stored_entity_sets"] == []
    assert body["totals"] == {"entity_sets": 2, "operations": 1, "skipped": 0}

    # One GET, of exactly the confined path plus $metadata, with no query.
    (sent,) = remote.requests
    assert sent.method == "GET"
    assert str(sent.url) == f"{SAP_URL}{PATH}/$metadata"
    assert sent.headers["accept"] == "application/xml"
    assert sent.headers["authorization"] == "Bearer dest-token"
    assert "cookie" not in sent.headers
    assert remote.asked_for == ["S4_ODATA_TECH"]
    # The technical identity: the destination was resolved without a user.
    assert remote.resolver.calls == [(None, None)]
    assert SAP_HOST not in caplog.text and "dest-token" not in caplog.text


def datetime_is_utc_iso(value: str) -> bool:
    from datetime import datetime, timedelta

    moment = datetime.fromisoformat(value)
    return moment.utcoffset() == timedelta(0)


async def test_v4_preview(client, remote):
    remote.answer(xml(V4))
    r = await client.post(URL, json=request(odata_version="v4", service_path="/sap/opu/odata4/x"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["name"] for e in body["entity_sets"]] == [
        "PurchaseRequisition",
        "PurchaseRequisitionItem",
    ]
    assert body["entity_sets"][1]["label"] == ""
    assert body["entity_sets"][1]["declared"] == {
        "creatable": False,
        "updatable": False,
        "deletable": True,
    }
    release, count = body["operations"]
    assert release["kind"] == "action" and release["http_method"] == "POST"
    assert release["qualified_name"] == "com.example.pr.v0001.Release"
    assert release["bound_to"] == "PurchaseRequisition"
    assert release["parameters"] == [{"name": "Note", "type": "Edm.String", "required": False}]
    assert count["kind"] == "function" and count["bound_to"] is None
    assert str(remote.requests[0].url) == f"{SAP_URL}/sap/opu/odata4/x/$metadata"


async def test_changes_data_is_suggested_per_kind(client, remote):
    # V4: an action changes data, a function does not.
    remote.answer(xml(V4))
    r = await client.post(URL, json=request(odata_version="v4"))
    release, count = r.json()["operations"]
    assert release["suggested"] == {
        "changes_data": True,
        "known": True,
        # The fixture's action returns the entity it is called on.
        "returns": {"entity_set": "PurchaseRequisition", "collection": False, "type": ""},
    }
    assert count["suggested"]["changes_data"] is False and count["suggested"]["known"] is True
    # V2: a POST function import changes data ...
    remote.answer(xml(V2))
    (post,) = (await client.post(URL, json=REQUEST)).json()["operations"]
    item = {"entity_set": "A_PurchaseRequisitionItem", "collection": False, "type": ""}
    assert post["suggested"] == {"changes_data": True, "known": True, "returns": item}
    # ... and of a GET one nothing is known: it is treated as changing.
    as_get = V2.replace(b'm:HttpMethod="POST"', b'm:HttpMethod="GET"')
    assert as_get != V2
    remote.answer(xml(as_get))
    (get,) = (await client.post(URL, json=REQUEST)).json()["operations"]
    assert get["http_method"] == "GET"
    assert get["suggested"] == {"changes_data": True, "known": False, "returns": item}


async def test_nothing_in_the_preview_is_enabled(client, remote):
    """The preview informs; every switch of the catalogue is the admin's."""
    for document, version in ((V2, "v2"), (V4, "v4")):
        remote.answer(xml(document))
        body = (await client.post(URL, json=request(odata_version=version))).json()
        keys = {key for key, _ in walk(body)}
        assert not keys & {
            "enabled",
            "selectable",
            "writable",
            "operations_enabled",
            "hint",
            "values",
            "examples",
            "description",
            "personal_data",
            "title",
        }
        # No field, entity set or operation has a key named like a switch
        # of the catalogue: what the service declares is nested.
        items = body["entity_sets"] + body["operations"]
        items += [f for e in body["entity_sets"] for f in e["fields"]]
        assert items and not any(set(item) & SWITCHES for item in items)
        assert all(set(f) == FIELD_KEYS for e in body["entity_sets"] for f in e["fields"])


async def test_what_the_parser_skipped_is_passed_through(client, remote):
    document = V2.replace(
        b"<FunctionImport ",
        b'<FunctionImport Name="Purge" m:HttpMethod="DELETE"/><FunctionImport ',
        1,
    )
    remote.answer(xml(document))
    body = (await client.post(URL, json=REQUEST)).json()
    assert body["skipped"] == [
        {
            "kind": "operation",
            "entity_set": "",
            "position": 1,
            "reason": "unsupported_http_method",
            "entity_type": "",
        }
    ]
    assert body["summary"]["skipped"] == 1 and body["totals"]["skipped"] == 1
    assert [o["name"] for o in body["operations"]] == ["ReleaseItem"]
    assert body["truncated"] is False


async def test_nothing_is_stored_and_no_document_is_cached(client, remote):
    before = (await client.get(SERVICES)).json()
    for _ in range(2):
        assert (await client.post(URL, json=REQUEST)).status_code == 200
    assert (await client.get(SERVICES)).json() == before == []
    # One fetch per call: the second call asked SAP again.
    assert len(remote.requests) == 2


# ------------------------------------------------------------------ truncation


def big_v2(entity_sets: int, fields: int, operations: int) -> bytes:
    properties = "".join(
        f'<Property Name="F{i}" Type="Edm.String" Nullable="false"/>' for i in range(fields)
    )
    sets = "".join(f'<EntitySet Name="S{i}" EntityType="NS.T"/>' for i in range(entity_sets))
    imports = "".join(
        f'<FunctionImport Name="Op{i}" m:HttpMethod="POST"/>' for i in range(operations)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" '
        'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
        '<edmx:DataServices m:DataServiceVersion="2.0">'
        '<Schema Namespace="NS" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">'
        f'<EntityType Name="T"><Key><PropertyRef Name="F0"/></Key>{properties}</EntityType>'
        f'<EntityContainer Name="C" m:IsDefaultEntityContainer="true">{sets}{imports}'
        "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()


async def test_a_document_larger_than_the_catalogue_is_cut_and_says_so(client, remote):
    document = big_v2(MAX_ENTITY_SETS + 5, 3, MAX_OPERATIONS + 3)
    parsed = parse_metadata(document, "v2")
    assert len(parsed.entity_sets) == MAX_ENTITY_SETS + 5
    remote.answer(xml(document))
    body = (await client.post(URL, json=REQUEST)).json()
    assert body["truncated"] is True
    assert body["totals"] == {
        "entity_sets": MAX_ENTITY_SETS + 5,
        "operations": MAX_OPERATIONS + 3,
        "skipped": 0,
    }
    # The first N, in document order.
    assert [e["name"] for e in body["entity_sets"]] == [f"S{i}" for i in range(MAX_ENTITY_SETS)]
    assert [o["name"] for o in body["operations"]] == [f"Op{i}" for i in range(MAX_OPERATIONS)]
    assert body["summary"]["entity_sets"] == MAX_ENTITY_SETS
    assert body["summary"]["operations"] == MAX_OPERATIONS


async def test_more_fields_than_an_entity_set_can_store_are_cut_per_set(client, remote):
    remote.answer(xml(big_v2(2, MAX_FIELDS + 7, 0)))
    body = (await client.post(URL, json=REQUEST)).json()
    assert body["truncated"] is True
    for entity_set in body["entity_sets"]:
        assert entity_set["fields_total"] == MAX_FIELDS + 7
        assert [f["name"] for f in entity_set["fields"]] == [f"F{i}" for i in range(MAX_FIELDS)]


async def test_the_preview_has_a_field_budget_over_all_entity_sets(client, remote, monkeypatch):
    monkeypatch.setattr(preview, "MAX_PREVIEW_FIELDS", 5)
    remote.answer(xml(big_v2(3, 4, 0)))
    body = (await client.post(URL, json=REQUEST)).json()
    assert body["truncated"] is True
    # The key field (F0) costs no budget and is never cut.
    assert [[f["name"] for f in e["fields"]] for e in body["entity_sets"]] == [
        ["F0", "F1", "F2", "F3"],
        ["F0", "F1", "F2"],
        ["F0"],
    ]
    assert [e["fields_total"] for e in body["entity_sets"]] == [4, 4, 4]
    assert [e["truncated"] for e in body["entity_sets"]] == [False, True, True]


async def test_an_untruncated_document_at_the_limits_is_not_flagged(client, remote):
    remote.answer(xml(big_v2(3, MAX_FIELDS, 2)))
    body = (await client.post(URL, json=REQUEST)).json()
    assert body["truncated"] is False


# ----------------------------------------------------------------- the fetch


class Chunks(httpx.AsyncByteStream):
    """A body served chunk by chunk, counting how far it was read."""

    def __init__(self, count: int, size: int) -> None:
        self.count = count
        self.size = size
        self.served = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(self.count):
            self.served += 1
            yield b"<" * self.size

    async def aclose(self) -> None:
        self.closed = True


async def test_an_oversized_body_is_cut_off_while_reading(client, remote, monkeypatch):
    monkeypatch.setattr(preview, "MAX_FETCH_BYTES", 1000)
    stream = Chunks(count=100, size=400)
    remote.answer(
        lambda _request: httpx.Response(
            200, headers={"content-type": "application/xml"}, stream=stream
        )
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502, r.text
    assert r.headers["x-odata-error"] == "too_large"
    assert r.json() == {"detail": "the $metadata document is larger than 1000 bytes"}
    # Not read to the end: the read stopped with the chunk that crossed the cap.
    assert stream.served == 3 and stream.closed


async def test_an_oversized_error_body_is_not_read_to_the_end(client, remote):
    stream = Chunks(count=1000, size=1000)
    remote.answer(
        lambda _request: httpx.Response(
            500, headers={"content-type": "application/xml"}, stream=stream
        )
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "sap_error"
    assert stream.served < 100 and stream.closed


@pytest.mark.parametrize(
    "response",
    [
        # A sign-in page, said to be HTML.
        httpx.Response(
            200,
            content=f"<html><body>Log on {SECRET}</body></html>".encode(),
            headers={"content-type": "text/html; charset=utf-8"},
        ),
        # The same page behind an XML content type.
        xml(f"<!DOCTYPE html>\n<html><body>{SECRET}</body></html>".encode()),
        xml(f"  \n<HTML><body>{SECRET}</body></HTML>".encode()),
        httpx.Response(200, json={"d": {"EntitySets": [SECRET]}}),
        httpx.Response(200, content=f"plain {SECRET}".encode()),
        httpx.Response(200, content=b"", headers={"content-type": "application/xml"}),
        httpx.Response(204),
    ],
)
async def test_a_sign_in_page_or_other_non_xml_answer_is_refused(
    client, remote, response, caplog
):
    caplog.set_level(logging.DEBUG)
    remote.answer(response)
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502, r.text
    assert r.headers["x-odata-error"] == "not_xml"
    assert "did not answer with an XML document" in r.json()["detail"]
    assert SECRET not in r.text and SECRET not in caplog.text


async def test_xml_that_is_no_edmx_is_a_422_of_the_parser(client, remote):
    remote.answer(xml(f"<feed><entry>{SECRET}</entry></feed>".encode()))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 422
    assert r.json() == {"detail": "not an EDMX document"}
    assert r.headers["x-odata-error"] == "invalid_metadata"


async def test_a_dtd_is_refused(client, remote):
    remote.answer(xml(b'<!DOCTYPE x [<!ENTITY a "b">]>' + V2))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 422 and "DTD" in r.json()["detail"]


async def test_a_redirect_is_not_followed(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    remote.answer(
        httpx.Response(302, headers={"location": f"https://idp.{SECRET}.example/login?x=1"}),
        xml(V2),
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502, r.text
    assert r.headers["x-odata-error"] == "redirect"
    assert "redirect" in r.json()["detail"] and "302" in r.json()["detail"]
    assert len(remote.requests) == 1
    assert SECRET not in r.text and SECRET not in caplog.text


async def test_a_sap_error_is_a_short_code_and_message(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    envelope = (
        '<?xml version="1.0"?><error xmlns="http://schemas.microsoft.com/ado/2007/08/'
        'dataservices/metadata"><code>/IWFND/CM_MGW/004</code><message xml:lang="en">'
        "No authorization to access Service</message><innererror><transactionid>"
        f"{SECRET}</transactionid></innererror></error>"
    ).encode()
    remote.answer(xml(envelope, status=403))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502, r.text
    assert r.headers["x-odata-error"] == "sap_error"
    detail = r.json()["detail"]
    assert "HTTP 403" in detail
    assert "/IWFND/CM_MGW/004: No authorization to access Service" in detail
    assert len(detail) <= 500
    assert SECRET not in r.text and SAP_HOST not in r.text
    assert SECRET not in caplog.text and SAP_HOST not in caplog.text


async def test_an_html_error_page_is_never_echoed(client, remote):
    remote.answer(
        httpx.Response(
            500,
            content=f"<html>dump at https://{SAP_HOST}/x {SECRET}</html>".encode(),
            headers={"content-type": "text/html"},
        )
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and "HTTP 500" in r.json()["detail"]
    assert SECRET not in r.text and SAP_HOST not in r.text


async def test_a_url_in_a_sap_message_is_masked(client, remote):
    remote.answer(
        httpx.Response(
            404,
            json={
                "error": {
                    "code": "X/404",
                    "message": {"lang": "en", "value": f"see https://{SAP_HOST}:44300/a?b=c now"},
                }
            },
        )
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502
    assert "X/404: see <url> now" in r.json()["detail"]
    assert SAP_HOST not in r.text


async def test_a_401_is_not_sent_again(client, remote):
    """One fetch per call: the resolver is new for this call, so a second
    attempt could only repeat a failed logon."""
    remote.answer(httpx.Response(401), xml(V2))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and "HTTP 401" in r.json()["detail"]
    assert len(remote.requests) == 1


async def test_an_unreachable_service_names_no_host(client, remote, caplog):
    caplog.set_level(logging.DEBUG)

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {req.url}", request=req)

    remote.answer(refuse)
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "unreachable"
    assert r.json() == {"detail": "the OData service could not be reached"}
    assert SAP_HOST not in caplog.text


async def test_the_fetch_has_a_total_timeout(client, remote, monkeypatch):
    monkeypatch.setattr(preview, "PREVIEW_BUDGET_SECONDS", 0.05)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return xml(V2)

    remote.sap = Sap()
    monkeypatch.setattr(preview, "_transport", lambda: httpx.MockTransport(slow))
    started = time.monotonic()
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 504 and r.headers["x-odata-error"] == "timeout"
    assert time.monotonic() - started < 3


async def test_a_destination_that_cannot_be_resolved(client, remote, caplog):
    caplog.set_level(logging.DEBUG)

    class Broken(FakeResolver):
        async def resolve(self, **_kwargs: Any) -> Destination:
            raise DestinationError(
                f"destination service returned 500 for 'S4_ODATA_TECH': "
                f"see https://dest.{SECRET}.example/x"
            )

    remote.resolver = Broken()
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"
    assert r.json() == {"detail": preview.DESTINATION_TEXT}
    assert SECRET not in r.text and SECRET not in caplog.text
    assert remote.requests == []


async def test_no_destination_binding_is_a_destination_error(client):
    """The real resolver seam, in an environment without a binding."""
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"


class Proxy:
    """The connectivity side of one test: the tokens and the proxy's wire."""

    def __init__(self) -> None:
        self.tokens = FakeConnectivity()
        self.sap = Sap(xml(V2))

    @property
    def requests(self) -> list[httpx.Request]:
        return self.sap.requests


@pytest.fixture
def proxy(monkeypatch) -> Proxy:
    state = Proxy()
    monkeypatch.setattr(preview, "_connectivity", lambda: state.tokens)
    monkeypatch.setattr(
        preview,
        "_proxy_transport",
        lambda: httpx.MockTransport(unread(lambda r: state.sap.handler(r))),
    )
    return state


async def test_an_on_premise_destination_is_fetched_through_the_connectivity_proxy(
    client, remote, proxy
):
    remote.resolver = OnPremiseResolver()
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["entity_sets"] > 0
    # Nothing went out past the proxy.
    assert remote.requests == []
    (sent,) = proxy.requests
    assert str(sent.url) == f"http://s4.internal:44300{PATH}/$metadata?sap-client=100"
    assert sent.headers["Proxy-Authorization"] == "Bearer APP"
    assert sent.headers["Authorization"] == "Basic dGVjaDp4"
    assert sent.headers["SAP-Connectivity-SCC-Location_ID"] == "LOC1"
    assert "SAP-Connectivity-Authentication" not in sent.headers
    assert sent.headers["Accept"] == "application/xml"
    assert sent.headers["Accept-Encoding"] == "identity"
    assert proxy.tokens.calls == [("app", False)]


async def test_an_on_premise_fetch_as_the_signed_in_user_carries_only_that_user(
    client, remote, proxy
):
    remote.resolver = OnPremiseResolver(name="S4_ODATA_USER", auth_type="PrincipalPropagation")
    headers = bearer("alice-id")
    body = request(destination="S4_ODATA_USER", user_context=True)
    r = await client.post(URL, json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert remote.requests == []
    (sent,) = proxy.requests
    assert sent.headers["Proxy-Authorization"] == "Bearer UX-alice-id"
    # The destination's stored credential is not sent next to the user.
    assert "Authorization" not in sent.headers
    jwt_sent = headers["Authorization"].removeprefix("Bearer ")
    assert proxy.tokens.calls == [("user", jwt_sent, "alice-id", False)]


async def test_an_on_premise_user_fetch_on_a_technical_destination_sends_nothing(
    client, remote, proxy
):
    remote.resolver = OnPremiseResolver()
    r = await client.post(URL, json=request(user_context=True), headers=bearer("alice-id"))
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"
    assert remote.requests == [] and proxy.requests == [] and proxy.tokens.calls == []


async def test_an_https_on_premise_destination_is_refused_before_anything_is_sent(
    client, remote, proxy
):
    remote.resolver = OnPremiseResolver(url="https://s4.internal:44300")
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"
    assert remote.requests == [] and proxy.requests == [] and proxy.tokens.calls == []


async def test_an_on_premise_destination_without_a_connectivity_binding_says_so_in_the_log(
    client, remote, caplog
):
    """The real connectivity seam, in an environment without a binding."""
    caplog.set_level(logging.WARNING, logger=preview.logger.name)
    remote.resolver = OnPremiseResolver()
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"
    assert r.json() == {"detail": preview.DESTINATION_TEXT}
    assert "no connectivity service binding" in caplog.text
    assert remote.requests == []


async def test_a_407_of_the_proxy_is_one_request_and_no_retry(client, remote, proxy):
    remote.resolver = OnPremiseResolver()
    proxy.sap = Sap(httpx.Response(407, text="Proxy Authentication Required"))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "sap_error"
    assert "connectivity proxy" in r.json()["detail"]
    assert len(proxy.requests) == 1 and proxy.tokens.invalidated == []


# ------------------------------------------------------------------ identity


async def test_user_context_runs_as_the_admin_who_calls(client, remote):
    remote.resolver = FakeResolver(name="S4_ODATA_USER")
    body = request(destination="S4_ODATA_USER", user_context=True)
    for user in ("alice-id", "bob-id"):
        headers = bearer(user)
        r = await client.post(URL, json=body, headers=headers)
        assert r.status_code == 200, r.text
        sent = remote.requests[-1]
        # What SAP saw is this caller's credential, not the other's and not
        # the destination's own.
        assert sent.headers["authorization"] == f"Bearer user-token-of-{user}"
        token, principal = remote.resolver.calls[-1]
        assert principal == user
        assert f"Bearer {token}" == headers["Authorization"]
    assert len(remote.requests) == 2


async def test_user_context_without_a_bound_token_is_a_424(client, remote):
    r = await client.post(URL, json=request(user_context=True))
    assert r.status_code == 424, r.text
    assert r.headers["x-odata-error"] == "user_token_required"
    assert r.json() == {
        "detail": "This fetch runs in SAP as the signed-in user, but no user token "
        "reached the backend. Sign in again and retry."
    }
    # No fallback to the destination's own credential: nothing was resolved
    # and nothing was sent.
    assert remote.resolver.calls == [] and remote.requests == []


async def test_without_user_context_a_bound_token_is_not_used(client, remote):
    r = await client.post(URL, json=REQUEST, headers=bearer("alice-id"))
    assert r.status_code == 200
    assert remote.resolver.calls == [(None, None)]
    assert remote.requests[0].headers["authorization"] == "Bearer dest-token"


# ------------------------------------------------------------------ the compare

STORED: dict[str, Any] = {
    "name": "purchase-requisitions",
    "title": "Purchase requisitions",
    "purpose": "Read requisitions and their items",
    "destination": "S4_ODATA_TECH",
    "user_context": False,
    "odata_version": "v2",
    "service_path": PATH,
    "definition": {
        "entity_sets": [
            {
                "name": "A_PurchaseRequisitionHeader",
                "title": "Stored title HINT-TEXT",
                "description": "Stored description HINT-TEXT",
                "keys": [{"name": "PurchaseRequisition"}],
                "operations": ["list", "get"],
                "fields": [
                    {"name": "PurchaseRequisition", "selectable": True, "filterable": True},
                    {"name": "PurchaseRequisitionType", "selectable": True},
                    {"name": "PurReqnDescription", "hint": "HINT-TEXT of a field"},
                ],
            },
            {
                "name": "A_PurchaseRequisitionItem",
                "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
                "operations": ["list", "update"],
                "fields": [
                    {"name": "PurchaseRequisition", "selectable": True},
                    {"name": "PurchaseRequisitionItem", "selectable": True},
                    {
                        "name": "PurReqnReleaseStatus",
                        "writable": True,
                        "values": [{"value": "05", "meaning": "HINT-TEXT released"}],
                    },
                    {"name": "LegacyField", "hint": "HINT-TEXT gone"},
                ],
                "examples": [{"description": "HINT-TEXT example", "filter": "Material eq 'X'"}],
            },
        ],
        "operations": [
            {
                "name": "ReleaseItem",
                "kind": "function_import",
                "http_method": "POST",
                "bound_to": "A_PurchaseRequisitionItem",
                "description": "HINT-TEXT of an operation",
                "enabled": True,
                "parameters": [{"name": "ReleaseCode"}],
            }
        ],
    },
}


async def test_the_compare_uses_the_stored_definition(client, remote):
    created = await client.post(SERVICES, json=STORED)
    assert created.status_code == 201, created.text
    stored = (await client.get(f"{SERVICES}/purchase-requisitions")).json()

    r = await client.post(URL, json=request(service="purchase-requisitions"))
    assert r.status_code == 200, r.text
    body = r.json()
    header, item = body["entity_sets"]
    assert header["status"] == "in_service"
    assert header["new_fields"] == [] and header["removed_fields"] == []
    assert item["status"] == "changed"
    assert item["new_fields"] == [
        "Material",
        "RequestedQuantity",
        "BaseUnit",
        "ItemNetAmount",
        "PurReqnItemCurrency",
        "CreatedByUser",
        "CreationDate",
    ]
    assert item["removed_fields"] == ["LegacyField"]
    assert body["operations"][0]["status"] == "in_service"
    assert body["summary"]["in_service"] == 1 and body["summary"]["changed"] == 1
    # The stored hints and switches never travel in this answer ...
    assert "HINT-TEXT" not in r.text and "Stored title" not in r.text
    assert not {key for key, _ in walk(body)} & {"enabled", "selectable", "writable", "hint"}
    assert item["changed_keys"] is False and item["changed_types"] == []
    assert body["removed_entity_sets"] == [] and body["removed_operations"] == []
    # ... and the service is exactly as it was.
    assert (await client.get(f"{SERVICES}/purchase-requisitions")).json() == stored


async def test_an_entity_set_the_service_does_not_have_is_new(client, remote):
    data = copy.deepcopy(STORED)
    del data["definition"]["entity_sets"][0]
    data["definition"]["operations"] = []
    assert (await client.post(SERVICES, json=data)).status_code == 201
    body = (await client.post(URL, json=request(service="purchase-requisitions"))).json()
    assert [e["status"] for e in body["entity_sets"]] == ["new", "changed"]
    assert body["operations"][0]["status"] == "new"


@pytest.mark.parametrize("name", ["no-such-service", "Not A Name SECRET", "x" * 300, ""])
async def test_an_unknown_service_to_compare_with_is_a_404_before_any_fetch(
    client, remote, name
):
    r = await client.post(URL, json=request(service=name))
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Service not found"}
    assert remote.requests == [] and remote.asked_for == []


async def test_a_null_service_means_no_compare(client, remote):
    r = await client.post(URL, json=request(service=None))
    assert r.status_code == 200 and {e["status"] for e in r.json()["entity_sets"]} == {"new"}


# ---------------------------------------------------------------- the request


@pytest.mark.parametrize(
    ("patch", "names"),
    [
        ({"destination": f"bad name {SECRET}"}, "destination"),
        ({"destination": f"https://{SECRET}.example"}, "destination"),
        ({"destination": None}, "destination"),
        ({"service_path": f"/sap/../{SECRET}"}, "service_path"),
        ({"service_path": f"https://{SECRET}.example/sap"}, "service_path"),
        ({"service_path": f"/sap/x?sap-client={SECRET}"}, "service_path"),
        ({"service_path": f"/sap/x#{SECRET}"}, "service_path"),
        ({"service_path": f"/sap/x/{SECRET}/"}, "service_path"),
        ({"service_path": f"/sap/%2e%2e/{SECRET}"}, "service_path"),
        ({"service_path": f"sap/{SECRET}"}, "service_path"),
        ({"service_path": 7}, "service_path"),
        ({"odata_version": f"v3-{SECRET}"}, "odata_version"),
        ({"odata_version": 2}, "odata_version"),
        ({"user_context": "true"}, "user_context"),
        ({"user_context": 1}, "user_context"),
        ({"user_context": None}, "user_context"),
        ({"service": 12345}, "service"),
        ({"url": f"https://{SECRET}.example/$metadata"}, "url"),
        ({"query": f"sap-client={SECRET}"}, "query"),
        ({"definition": {"entity_sets": []}}, "definition"),
    ],
)
async def test_a_refused_body_names_the_field_and_never_echoes_it(client, remote, patch, names):
    r = await client.post(URL, json=request(**patch))
    assert r.status_code == 422, (patch, r.text)
    assert r.json()["detail"].startswith(names + ":"), r.text
    assert SECRET not in r.text
    assert remote.requests == [] and remote.asked_for == []


@pytest.mark.parametrize("missing", ["destination", "service_path", "odata_version"])
async def test_a_missing_field_is_named(client, remote, missing):
    body = request()
    del body[missing]
    r = await client.post(URL, json=body)
    assert r.status_code == 422 and r.json()["detail"].startswith(missing + ":")


async def test_a_body_that_is_no_object_or_too_large(client, remote):
    r = await client.post(URL, json=[REQUEST])
    assert r.status_code == 422 and r.json() == {"detail": "body: expected a JSON object"}
    r = await client.post(URL, json=request(service="x" * 70_000))
    assert r.status_code == 413
    assert remote.requests == []


async def test_a_version_mismatch_is_refused(client, remote):
    r = await client.post(URL, json=request(odata_version="v4"))
    assert r.status_code == 422
    assert r.json() == {"detail": "the document is OData V2, not V4"}
    remote.answer(xml(V4))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 422
    assert r.json() == {"detail": "the document is OData V4, not V2"}


# -------------------------------------------------------------------- the scope


class _StubValidator:
    xsappname = "app"

    def __init__(self, claims: dict[str, dict[str, Any]]) -> None:
        self._claims = claims

    def validate(self, token: str) -> dict[str, Any]:
        if token not in self._claims:
            raise HTTPException(status_code=401, detail="Invalid token")
        return self._claims[token]

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        return scope in (payload.get("scope") or [])


def test_the_route_carries_the_admin_dependency_and_is_served():
    from agents.auth import require_admin
    from agents.odata.admin_routes import router

    (route,) = [r for r in router.routes if r.path == "/api/odata/metadata"]
    assert route.methods == {"POST"}
    assert require_admin in [d.call for d in route.dependant.dependencies]
    assert "post" in app_module.app.openapi()["paths"][URL]


async def test_only_an_admin_reaches_the_fetch(monkeypatch, remote):
    validator = _StubValidator(
        {
            "usr": {"user_name": "u", "scope": ["user"]},
            "dev": {"user_name": "d", "scope": ["developer", "user", "a2a"]},
            "adm": {"user_name": "a", "scope": ["admin"]},
        }
    )
    monkeypatch.setattr(auth, "get_validator", lambda: validator)
    marker = auth.current_claims.set(None)
    try:
        from agents.odata.admin_routes import router

        bare = FastAPI()
        bare.include_router(router, prefix="/admin")
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
            r = await c.post(URL, json=REQUEST)
            assert r.status_code == 401
            for token in ("usr", "dev"):
                r = await c.post(URL, json=REQUEST, headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 403
                assert r.json() == {"detail": "Admin scope required"}
            assert remote.requests == [] and remote.asked_for == []
            r = await c.post(URL, json=REQUEST, headers={"Authorization": "Bearer adm"})
            assert r.status_code == 200
            assert len(remote.requests) == 1
    finally:
        auth.current_claims.reset(marker)


# ------------------------------------------- leftovers of the audit re-review

AUDIT_URL = "/admin/api/odata/audit"


async def test_before_id_is_held_to_what_the_id_column_holds(client):
    """A 32-bit integer on Postgres: a larger bound value is a 500 there."""
    from agents.odata.admin_routes import AUDIT_MAX_ID

    assert AUDIT_MAX_ID == 2**31 - 1
    assert ODataAuditLog.__table__.c.id.type.python_type is int
    r = await client.get(AUDIT_URL, params={"before_id": str(AUDIT_MAX_ID)})
    assert r.status_code == 200 and r.json()["items"] == []
    for value in (str(AUDIT_MAX_ID + 1), "9" * 10, "9" * 18, "02147483648"):
        r = await client.get(AUDIT_URL, params={"before_id": value})
        assert r.status_code == 422, value
        assert r.json()["detail"].startswith("before_id:") and value not in r.text


@pytest.mark.parametrize("width", [1, 8, 16, 17])
def test_fit_cuts_plainly_when_the_column_has_no_room_for_the_digest(width):
    from types import SimpleNamespace

    from agents.odata.audit import _fit

    column = SimpleNamespace(name=f"narrow_{width}", type=SimpleNamespace(length=width))
    value = "abcdefghijklmnopqrstuvwxyz" * 4
    assert _fit(value, column) == value[:width]
    assert _fit("ab"[:width], column) == "ab"[:width]


def test_fit_still_marks_a_cut_in_a_column_with_room():
    from types import SimpleNamespace

    from agents.odata.audit import _fit

    column = SimpleNamespace(name="roomy", type=SimpleNamespace(length=18))
    cut = _fit("x" * 40, column)
    assert cut is not None and len(cut) == 18 and cut.startswith("x~")


# ---------------------------------------------------------- review round 1


def test_fit_logs_a_plain_cut_once(caplog, monkeypatch):
    from types import SimpleNamespace

    from agents.odata import audit

    monkeypatch.setattr(audit, "_cut_warned", set())
    caplog.set_level(logging.WARNING, logger=audit.audit_logger.name)
    column = SimpleNamespace(name="narrow", type=SimpleNamespace(length=4))
    assert audit._fit("abcdefgh-SECRET", column) == "abcd"
    assert audit._fit("abcdefgh-SECRET", column) == "abcd"
    lines = [r.getMessage() for r in caplog.records if "narrow" in r.getMessage()]
    assert len(lines) == 1 and "cut plainly" in lines[0] and SECRET not in caplog.text


async def test_a_compressed_answer_is_refused_without_being_inflated(client, remote):
    import gzip

    stream = Chunks(count=50, size=1000)
    packed = gzip.compress(V2)
    for response in (
        httpx.Response(
            200,
            headers={"content-type": "application/xml", "content-encoding": "gzip"},
            stream=stream,
        ),
        httpx.Response(
            200,
            headers={
                "content-type": "application/xml",
                "content-encoding": "GZIP",
                "content-length": str(len(packed)),
            },
            stream=httpx.ByteStream(packed),
        ),
        httpx.Response(
            200, headers={"content-type": "application/xml", "content-encoding": "br"}, content=b"x"
        ),
    ):
        remote.answer(lambda _request, response=response: response)
        r = await client.post(URL, json=REQUEST)
        assert r.status_code == 502, r.text
        assert r.headers["x-odata-error"] == "not_xml"
    # Not one chunk of the compressed body was read.
    assert stream.served == 0 and stream.closed


async def test_identity_encoding_is_read_raw(client, remote):
    remote.answer(xml(V2, **{"content-encoding": "identity"}))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 200 and r.json()["summary"]["entity_sets"] == 2


async def test_a_destination_header_cannot_change_what_the_fetch_asks_for(client, remote):
    remote.resolver = FakeResolver(
        name="S4_ODATA_TECH",
        headers={
            "Authorization": "Bearer dest-token",
            "Accept-Encoding": "gzip, br",
            "Accept": "text/html",
            "X-Custom": "kept",
        },
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 200
    (sent,) = remote.requests
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["accept"] == "application/xml"
    assert sent.headers["x-custom"] == "kept"
    assert sent.headers.get_list("accept-encoding") == ["identity"]


async def test_a_declared_length_over_the_cap_is_refused_before_reading(
    client, remote, monkeypatch
):
    monkeypatch.setattr(preview, "MAX_FETCH_BYTES", 1000)
    stream = Chunks(count=10, size=400)
    remote.answer(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/xml", "content-length": "4000"},
            stream=stream,
        )
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "too_large"
    assert stream.served == 0 and stream.closed


async def test_a_deeply_nested_error_body_is_no_500(client, remote):
    nested = b"[" * 100_000
    remote.answer(
        httpx.Response(500, content=nested, headers={"content-type": "application/json"})
    )
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "sap_error"
    assert r.json() == {"detail": "HTTP 500 from the OData service"}


def keyed_v2(fields: int, key: str, sets: int = 1) -> bytes:
    """One type of ``fields`` properties whose key is the property ``key``."""
    return big_v2(sets, fields, 0).replace(
        b'<PropertyRef Name="F0"/>', f'<PropertyRef Name="{key}"/>'.encode()
    )


async def test_a_key_field_past_the_cut_is_kept(client, remote):
    last = f"F{MAX_FIELDS + 6}"
    remote.answer(xml(keyed_v2(MAX_FIELDS + 7, last)))
    (entity_set,) = (await client.post(URL, json=REQUEST)).json()["entity_sets"]
    names = [f["name"] for f in entity_set["fields"]]
    assert entity_set["keys"] == [{"name": last, "type": "Edm.String"}]
    # MAX_FIELDS in all, in document order, the key among them.
    assert names == [f"F{i}" for i in range(MAX_FIELDS - 1)] + [last]
    assert entity_set["truncated"] is True and entity_set["fields_total"] == MAX_FIELDS + 7


async def test_keys_survive_a_spent_field_budget(client, remote, monkeypatch):
    monkeypatch.setattr(preview, "MAX_PREVIEW_FIELDS", 0)
    remote.answer(xml(keyed_v2(6, "F4", sets=2)))
    body = (await client.post(URL, json=REQUEST)).json()
    assert [[f["name"] for f in e["fields"]] for e in body["entity_sets"]] == [["F4"], ["F4"]]
    assert body["truncated"] is True


def stored_service(fields: list[dict[str, Any]], keys: list[str], **more: Any) -> dict[str, Any]:
    data = copy.deepcopy(STORED)
    data["definition"] = {
        "entity_sets": [
            {
                "name": "S0",
                "keys": [{"name": k} for k in keys],
                "operations": [],
                "fields": fields,
            },
            *more.get("entity_sets", []),
        ],
        "operations": more.get("operations", []),
    }
    return data


async def test_a_change_past_the_cut_is_still_a_change(client, remote):
    """Status and field lists are worked out against ALL fields of the
    document, not against the fields the answer shows."""
    total = MAX_FIELDS + 7
    known = [{"name": f"F{i}"} for i in range(MAX_FIELDS)]  # everything shown is stored
    assert (await client.post(SERVICES, json=stored_service(known, ["F0"]))).status_code == 201
    remote.answer(xml(big_v2(1, total, 0)))
    body = (await client.post(URL, json=request(service="purchase-requisitions"))).json()
    (entity_set,) = body["entity_sets"]
    assert entity_set["status"] == "changed"
    assert entity_set["new_fields"] == [f"F{i}" for i in range(MAX_FIELDS, total)]
    assert entity_set["new_fields_total"] == 7 and entity_set["removed_fields"] == []


async def test_a_stored_field_past_the_cut_is_not_reported_as_removed(client, remote):
    total = MAX_FIELDS + 7
    known = [{"name": f"F{i}"} for i in range(total - 400, total)] + [{"name": "F0"}]
    assert (await client.post(SERVICES, json=stored_service(known, ["F0"]))).status_code == 201
    remote.answer(xml(big_v2(1, total, 0)))
    (entity_set,) = (
        await client.post(URL, json=request(service="purchase-requisitions"))
    ).json()["entity_sets"]
    assert entity_set["removed_fields"] == []


async def test_what_disappeared_from_the_document_is_named(client, remote):
    data = stored_service(
        [{"name": "F0"}, {"name": "F1"}, {"name": "F2"}],
        ["F0"],
        entity_sets=[
            {"name": "GoneSet", "keys": [{"name": "Id"}], "fields": [{"name": "Id"}]},
            {"name": "S1", "keys": [{"name": "F0"}], "fields": [{"name": "F0"}]},
        ],
        operations=[
            {"name": "Op0", "kind": "function_import", "http_method": "POST"},
            {"name": "GoneOp", "kind": "function_import", "http_method": "POST"},
        ],
    )
    assert (await client.post(SERVICES, json=data)).status_code == 201
    remote.answer(xml(big_v2(2, 3, 1)))
    body = (await client.post(URL, json=request(service="purchase-requisitions"))).json()
    assert body["removed_entity_sets"] == ["GoneSet"]
    assert body["removed_operations"] == ["GoneOp"]
    assert body["removed_complete"] is True
    assert body["summary"]["removed_entity_sets"] == 1
    assert body["summary"]["removed_operations"] == 1
    assert body["entity_sets"][0]["status"] == "in_service"


async def test_a_stored_name_past_the_shown_cut_is_not_removed(client, remote):
    last = f"S{MAX_ENTITY_SETS + 4}"
    data = stored_service(
        [{"name": "F0"}],
        ["F0"],
        entity_sets=[{"name": last, "keys": [{"name": "F0"}], "fields": [{"name": "F0"}]}],
        operations=[
            {"name": f"Op{MAX_OPERATIONS + 2}", "kind": "function_import", "http_method": "POST"}
        ],
    )
    assert (await client.post(SERVICES, json=data)).status_code == 201
    remote.answer(xml(big_v2(MAX_ENTITY_SETS + 5, 1, MAX_OPERATIONS + 3)))
    body = (await client.post(URL, json=request(service="purchase-requisitions"))).json()
    assert last not in [e["name"] for e in body["entity_sets"]]
    assert body["removed_entity_sets"] == [] and body["removed_operations"] == []
    assert body["removed_complete"] is True and body["truncated"] is True


async def test_past_the_parsers_cap_nothing_is_called_removed(client, remote):
    from agents.odata.metadata import MAX_PARSED_ENTITY_SETS

    data = stored_service(
        [{"name": "F0"}],
        ["F0"],
        entity_sets=[{"name": "Elsewhere", "keys": [{"name": "F0"}], "fields": [{"name": "F0"}]}],
    )
    assert (await client.post(SERVICES, json=data)).status_code == 201
    remote.answer(xml(big_v2(MAX_PARSED_ENTITY_SETS + 30, 1, 0)))
    body = (await client.post(URL, json=request(service="purchase-requisitions"))).json()
    # Unknown, not "none": the parser stopped before the end of the document.
    assert body["removed_complete"] is False
    assert body["removed_entity_sets"] is None and body["removed_operations"] is None
    assert body["summary"]["removed_entity_sets"] is None
    assert body["summary"]["removed_operations"] is None
    assert body["skipped_stored_entity_sets"] == []
    assert body["truncated"] is True
    assert body["totals"]["entity_sets"] == MAX_PARSED_ENTITY_SETS + 30
    assert len(body["entity_sets"]) == MAX_ENTITY_SETS


async def test_a_changed_key_or_field_type_is_reported_by_name(client, remote):
    known = [
        {"name": "F0", "type": "Edm.Guid"},  # the document says Edm.String
        {"name": "F1"},
        {"name": "F2"},
    ]
    created = await client.post(SERVICES, json=stored_service(known, ["F0", "F1"]))
    assert created.status_code == 201
    remote.answer(xml(big_v2(1, 3, 0)))
    (entity_set,) = (
        await client.post(URL, json=request(service="purchase-requisitions"))
    ).json()["entity_sets"]
    assert entity_set["new_fields"] == [] and entity_set["removed_fields"] == []
    assert entity_set["changed_types"] == ["F0"]
    assert entity_set["changed_keys"] is True
    assert entity_set["status"] == "changed"


async def test_keys_navigations_and_parameters_are_capped_with_totals(client, remote, monkeypatch):
    monkeypatch.setattr(preview, "MAX_PREVIEW_KEYS", 1)
    monkeypatch.setattr(preview, "MAX_PREVIEW_NAVIGATIONS", 0)
    monkeypatch.setattr(preview, "MAX_PREVIEW_PARAMETERS", 2)
    body = (await client.post(URL, json=REQUEST)).json()
    item = body["entity_sets"][1]
    assert len(item["keys"]) == 1 and item["keys_total"] == 2
    assert item["navigations"] == [] and item["navigations_total"] == 1
    assert item["truncated"] is True
    (operation,) = body["operations"]
    assert len(operation["parameters"]) == 2 and operation["parameters_total"] == 3
    assert operation["truncated"] is True and body["truncated"] is True


async def test_an_unknown_key_is_named_only_when_it_looks_like_a_field(client, remote):
    r = await client.post(URL, json=request(**{f"https://{SECRET}.example/$metadata?x=1": 1}))
    assert r.status_code == 422
    assert r.json()["detail"].startswith("<unknown field>:") and SECRET not in r.text
    r = await client.post(URL, json=request(**{"x" * 65: 1, "a\n": 2}))
    assert r.status_code == 422 and "xxx" not in r.text
    assert r.json()["detail"].count("<unknown field>:") == 2
    r = await client.post(
        "/admin/api/odata/services/purchase-requisitions/duplicate",
        json={"name": "copy", f"{SECRET} key": 1},
    )
    assert r.status_code == 422 and SECRET not in r.text


async def test_an_http_internet_destination_is_refused_with_nothing_sent(client, remote):
    remote.resolver = FakeResolver(url="http://s4.internal:44300", name="S4_ODATA_TECH")
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"
    assert remote.requests == [] and SAP_HOST not in r.text


async def test_user_context_on_a_technical_destination_is_said_in_the_answer(
    client, remote, caplog
):
    class Technical(FakeResolver):
        async def resolve(self, **kwargs: Any) -> Destination:
            resolved = await super().resolve(**kwargs)
            return Destination(
                url=self.url,
                headers={"Authorization": "Basic dGVjaDp4"},
                expires_at=resolved.expires_at,
                auth_type="BasicAuthentication",
                per_user=True,
            )

    caplog.set_level(logging.INFO, logger=preview.logger.name)
    remote.resolver = Technical(name="S4_ODATA_TECH")
    r = await client.post(URL, json=request(user_context=True), headers=bearer("alice-id"))
    assert r.status_code == 200, r.text
    (warning,) = r.json()["warnings"]
    assert warning["code"] == "technical_credential"
    assert "destination's own credential" in warning["message"]
    # The log says how it WAS fetched, and by whom.
    (line,) = [x.getMessage() for x in caplog.records if "odata metadata preview" in x.getMessage()]
    assert "auth_type=BasicAuthentication" in line and "by=alice-id" in line
    assert "user_context=True" in line and "dGVjaDp4" not in caplog.text


async def test_a_user_propagating_destination_has_no_warning(client, remote, caplog):
    caplog.set_level(logging.INFO, logger=preview.logger.name)
    r = await client.post(URL, json=request(user_context=True), headers=bearer("alice-id"))
    assert r.status_code == 200 and r.json()["warnings"] == []
    (line,) = [x.getMessage() for x in caplog.records if "odata metadata preview" in x.getMessage()]
    assert "auth_type=OAuth2UserTokenExchange" in line and "per_user=True" in line


async def test_a_refusal_is_logged_with_its_code_and_the_caller(client, remote, caplog):
    caplog.set_level(logging.INFO, logger=preview.logger.name)
    remote.answer(httpx.Response(302, headers={"location": "https://idp.example/"}))
    await client.post(URL, json=REQUEST, headers=bearer("bob-id"))
    (line,) = [x.getMessage() for x in caplog.records if "odata metadata preview" in x.getMessage()]
    assert "code=redirect" in line and "by=bob-id" in line and "idp.example" not in line


async def test_at_most_two_previews_run_at_a_time(client, remote, monkeypatch):
    gate = asyncio.Event()
    arrived = 0

    async def held(_request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        arrived += 1
        await gate.wait()
        return unread(lambda _r: xml(V2))(_request)

    monkeypatch.setattr(preview, "_transport", lambda: httpx.MockTransport(held))
    first = asyncio.ensure_future(client.post(URL, json=REQUEST))
    second = asyncio.ensure_future(client.post(URL, json=REQUEST))
    for _ in range(200):
        if arrived == 2:
            break
        await asyncio.sleep(0.01)
    assert arrived == 2 and preview._active == 2
    third = await client.post(URL, json=REQUEST)
    assert third.status_code == 429 and third.headers["x-odata-error"] == "busy"
    assert third.json() == {"detail": preview.BUSY_TEXT}
    assert arrived == 2  # the third fetched nothing
    gate.set()
    assert (await first).status_code == 200 and (await second).status_code == 200
    assert preview._active == 0
    assert (await client.post(URL, json=REQUEST)).status_code == 200


async def test_the_budget_covers_the_parse_and_the_slot_waits_for_the_thread(
    client, remote, monkeypatch
):
    import threading

    release = threading.Event()
    real = preview.parse_metadata

    def slow_parse(document, version):
        release.wait(5)
        return real(document, version)

    monkeypatch.setattr(preview, "parse_metadata", slow_parse)
    monkeypatch.setattr(preview, "PREVIEW_BUDGET_SECONDS", 0.2)
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 504 and r.headers["x-odata-error"] == "timeout"
    # Answered, but the thread still runs: its slot is still taken.
    assert preview._active == 1
    release.set()
    for _ in range(200):
        if preview._active == 0:
            break
        await asyncio.sleep(0.01)
    assert preview._active == 0


async def test_a_document_over_the_parsers_work_budget_is_a_422(client, remote, monkeypatch):
    from agents.odata import metadata

    monkeypatch.setattr(metadata, "MAX_PARSE_WORK", 50)
    remote.answer(xml(big_v2(3, 60, 0)))
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 422 and r.headers["x-odata-error"] == "invalid_metadata"
    assert "too large to read" in r.json()["detail"]


def test_the_document_is_let_go_once_parsed():
    holder = [bytearray(V2)]
    answer = preview._parse_and_build(holder, "v2", None)
    assert holder == [] and answer["summary"]["entity_sets"] == 2


# ------------------------------------------------------------- fix round 2


def skipping_v2() -> bytes:
    """`Bad` is declared but its key names no property; `T` has a property
    with a name the catalogue cannot hold."""
    return big_v2(2, 2, 0).replace(
        b"<EntityContainer ",
        b'<EntityType Name="B"><Key><PropertyRef Name="Ghost"/></Key>'
        b'<Property Name="Id" Type="Edm.String"/></EntityType><EntityContainer ',
    ).replace(
        b"</EntityType>", b'<Property Name="bad name" Type="Edm.String"/></EntityType>', 1
    ).replace(b"</EntityContainer>", b'<EntitySet Name="Bad" EntityType="NS.B"/></EntityContainer>')


async def test_a_stored_set_the_parser_skips_is_not_called_removed(client, remote):
    data = stored_service(
        [{"name": "F0"}, {"name": "F1"}],
        ["F0"],
        entity_sets=[
            {"name": "Bad", "keys": [{"name": "Id"}], "fields": [{"name": "Id"}]},
            {"name": "GoneSet", "keys": [{"name": "Id"}], "fields": [{"name": "Id"}]},
        ],
    )
    assert (await client.post(SERVICES, json=data)).status_code == 201
    remote.answer(xml(skipping_v2()))
    body = (await client.post(URL, json=request(service="purchase-requisitions"))).json()
    # Still declared, so not removed -- but not usable as it is declared now.
    assert body["removed_entity_sets"] == ["GoneSet"]
    assert body["skipped_stored_entity_sets"] == [
        {"name": "Bad", "reason": "unrepresentable_key"}
    ]
    assert body["summary"]["removed_entity_sets"] == 1
    assert body["summary"]["skipped_stored_entity_sets"] == 1
    assert body["removed_complete"] is True


async def test_a_skipped_entry_names_the_entity_type(client, remote):
    remote.answer(xml(skipping_v2()))
    body = (await client.post(URL, json=REQUEST)).json()
    # The property is listed under the first set of the type only; the type
    # is what explains the field missing from `S1` as well.
    assert body["skipped"] == [
        {"kind": "property", "entity_set": "S0", "position": 3, "reason": "invalid_name",
         "entity_type": "NS.T"},
        {"kind": "entity_set", "entity_set": "Bad", "position": 3,
         "reason": "unrepresentable_key", "entity_type": "NS.B"},
    ]
    assert [e["entity_type"] for e in body["entity_sets"]] == ["NS.T", "NS.T"]
    assert body["skipped_stored_entity_sets"] == []


def test_an_entity_type_that_is_no_edm_name_is_not_passed_on():
    document = skipping_v2().replace(b'Namespace="NS"', b'Namespace="NS &lt;b&gt;"')
    document = document.replace(b'EntityType="NS.', b'EntityType="NS &lt;b&gt;.')
    answer = preview.build_preview(parse_metadata(document, "v2"))
    assert [s["reason"] for s in answer["skipped"]] == ["invalid_name", "unrepresentable_key"]
    assert [s["entity_type"] for s in answer["skipped"]] == ["", ""]


def test_without_a_stored_service_nothing_is_removed_or_skipped_stored():
    answer = preview.build_preview(parse_metadata(skipping_v2(), "v2"))
    assert answer["removed_entity_sets"] == [] and answer["removed_operations"] == []
    assert answer["skipped_stored_entity_sets"] == [] and answer["removed_complete"] is True


def test_v4_totals_are_never_below_what_is_shown():
    from agents.odata.metadata import MAX_PARSED_OPERATIONS

    imports = "".join(
        f'<ActionImport Name="I{i}" Action="NS.Do"/>' for i in range(MAX_PARSED_OPERATIONS + 5)
    )
    document = (
        '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
        '<edmx:DataServices><Schema Namespace="NS" xmlns="http://docs.oasis-open.org/odata/ns/edm">'
        f'<Action Name="Do"/><EntityContainer Name="C">{imports}</EntityContainer>'
        "</Schema></edmx:DataServices></edmx:Edmx>"
    ).encode()
    answer = preview.build_preview(parse_metadata(document, "v4"))
    assert answer["truncated"] is True and len(answer["operations"]) == MAX_OPERATIONS
    # One `Action` element, but 400 operations were read from its imports.
    assert answer["totals"]["operations"] == MAX_PARSED_OPERATIONS
    assert answer["removed_complete"] is False and answer["removed_operations"] is None


async def test_an_unexpected_failure_of_the_parse_is_a_coded_error_and_is_logged(
    client, remote, monkeypatch, caplog
):
    def broken(document, version):
        raise RuntimeError(f"{SECRET} text of the failure")

    monkeypatch.setattr(preview, "parse_metadata", broken)
    remote.answer(xml(V2))
    with caplog.at_level(logging.INFO, logger="agents.odata.preview"):
        r = await client.post(URL, json=REQUEST)
    assert r.status_code == 500 and r.headers["x-odata-error"] == "preview_failed"
    assert r.json()["detail"] == preview._FAILED_TEXT
    assert preview._FAILED_TEXT.startswith("the $metadata preview failed unexpectedly")
    assert SECRET not in r.text and SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "refused (code=preview_failed status=500)" in caplog.text
    for _ in range(200):
        if preview._active == 0:
            break
        await asyncio.sleep(0.01)
    assert preview._active == 0


def test_plain_cuts_a_huge_text_before_the_url_mask():
    """2 MB of characters a URL scheme may consist of, and no ``://``: the
    mask is quadratic on such a run, so it must only ever see a cut text.
    In a child process with a timeout, because a regular expression that
    runs away cannot be interrupted from inside."""
    import subprocess

    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from agents.odata import preview\n"
            "big = 'a' * (2 * 1024 * 1024)\n"
            "started = time.perf_counter()\n"
            "out = preview.plain(big, 300)\n"
            "elapsed = time.perf_counter() - started\n"
            "assert out == 'a' * 300\n"
            "print(elapsed)\n",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert float(done.stdout.strip().splitlines()[-1]) < 1.0


def test_plain_still_cleans_masks_and_caps():
    text = "one\ntwo https://host-77.example.test/x?q=1 " + "t" * 5000
    assert preview.plain(text, 40) == ("one two <url> " + "t" * 40)[:40]
    assert preview.plain("x" * 30 + " " + "https://host-77.example.test/p" * 50, 300) == (
        "x" * 30 + " <url>"
    )
    assert preview.plain(None, 10) == "" and preview.plain(b"x", 10) == ""
