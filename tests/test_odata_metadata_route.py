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
from tests.odata_helpers import SAP_URL, FakeResolver, Sap  # noqa: E402

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
    "fields",
    "fields_total",
    "navigations",
    "capabilities",
    "status",
    "new_fields",
    "removed_fields",
}
FIELD_KEYS = {"name", "type", "label", "filterable", "creatable", "updatable"}
OPERATION_KEYS = {
    "name",
    "qualified_name",
    "kind",
    "http_method",
    "bound_to",
    "parameters",
    "label",
    "status",
    "changes_data",
    "changes_data_known",
}
TOP_KEYS = {
    "fetched_at",
    "entity_sets",
    "operations",
    "skipped",
    "summary",
    "truncated",
    "totals",
}


def xml(body: bytes, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, content=body, headers={"content-type": "application/xml", **headers}
    )


def request(**patch: Any) -> dict[str, Any]:
    data = copy.deepcopy(REQUEST)
    data.update(patch)
    return data


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
    monkeypatch.setattr(preview, "_transport", lambda: state.sap.transport())
    return state


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
    assert header["capabilities"] == {"creatable": True, "updatable": True, "deletable": False}
    assert item["capabilities"] == {"creatable": False, "updatable": True, "deletable": False}
    assert header["navigations"] == [
        {"name": "to_PurchaseReqnItem", "target": "A_PurchaseRequisitionItem", "collection": True}
    ]
    assert all(set(f) == FIELD_KEYS for e in body["entity_sets"] for f in e["fields"])
    assert header["fields"][0] == {
        "name": "PurchaseRequisition",
        "type": "Edm.String",
        "label": header["fields"][0]["label"],
        "filterable": True,
        "creatable": False,
        "updatable": False,
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
    assert body["skipped"] == [] and body["truncated"] is False
    assert body["summary"] == {
        "entity_sets": 2,
        "operations": 1,
        "in_service": 0,
        "changed": 0,
        "skipped": 0,
    }
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
    assert body["entity_sets"][1]["capabilities"] == {
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
    assert (release["changes_data"], release["changes_data_known"]) == (True, True)
    assert (count["changes_data"], count["changes_data_known"]) == (False, True)
    # V2: a POST function import changes data ...
    remote.answer(xml(V2))
    (post,) = (await client.post(URL, json=REQUEST)).json()["operations"]
    assert (post["changes_data"], post["changes_data_known"]) == (True, True)
    # ... and of a GET one nothing is known: it is treated as changing.
    as_get = V2.replace(b'm:HttpMethod="POST"', b'm:HttpMethod="GET"')
    assert as_get != V2
    remote.answer(xml(as_get))
    (get,) = (await client.post(URL, json=REQUEST)).json()["operations"]
    assert get["http_method"] == "GET"
    assert (get["changes_data"], get["changes_data_known"]) == (True, False)


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
        # An entity set carries no list of enabled operations.
        assert all("operations" not in e for e in body["entity_sets"])


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
    assert [len(e["fields"]) for e in body["entity_sets"]] == [4, 1, 0]
    assert [e["fields_total"] for e in body["entity_sets"]] == [4, 4, 4]


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
    monkeypatch.setattr(preview, "FETCH_TIMEOUT_SECONDS", 0.05)

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
    assert "S4_ODATA_TECH" not in r.json()["detail"] or SECRET not in r.text
    assert SECRET not in r.text and SECRET not in caplog.text
    assert remote.requests == []


async def test_no_destination_binding_is_a_destination_error(client):
    """The real resolver seam, in an environment without a binding."""
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "destination_error"


class OnPremise(FakeResolver):
    async def resolve(self, **kwargs: Any) -> Destination:
        resolved = await super().resolve(**kwargs)
        return Destination(
            url=self.url,
            headers=resolved.headers,
            expires_at=resolved.expires_at,
            auth_type="BasicAuthentication",
            proxy_type="OnPremise",
        )


@pytest.mark.parametrize("url", ["https://s4.internal:44300", "http://s4.internal:44300"])
async def test_an_on_premise_destination_is_refused_before_anything_is_sent(client, remote, url):
    remote.resolver = OnPremise(url=url)
    r = await client.post(URL, json=REQUEST)
    assert r.status_code == 502, r.text
    assert r.headers["x-odata-error"] == "on_premise_unavailable"
    assert r.json() == {
        "detail": "on-premise destinations are not available yet in this version"
    }
    # Nothing went out past the Cloud Connector.
    assert remote.requests == []


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


def test_the_audit_docstrings_name_both_updating_statements_and_the_limits():
    from agents.odata import audit
    from agents.odata.admin_routes import api_list_odata_audit

    for doc in (audit.__doc__, ODataAuditLog.__doc__):
        text_ = " ".join((doc or "").split())
        assert "``result``" in text_ and "``abandon``" in text_
        assert "No other statement updates a row" not in text_
        assert "Nothing else updates a row" not in text_
    route = " ".join((api_list_odata_audit.__doc__ or "").split())
    assert "``sent_as`` matches the STORED form" in route
    assert "committed late" in route and "read again from the top" in route
    assert "audit not confirmed" in route and "intent timeout" in route
