"""``POST /admin/api/odata/services/{name}/test``: the catalogue test call.

The route is driven through the real app; SAP is an ``httpx.MockTransport``
and the destination service the ``FakeResolver`` double, both put in through
the two seams of ``agents.odata.testcall`` (``_resolver``, ``_transport``).
No network. The scope test mounts the router on a bare app with a stub
validator: the suite's app runs without an XSUAA binding, where every scope
check passes.

Every row the mock SAP answers carries the values in ``VALUES``; none of
them may show up in an answer or a log line.
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
from sqlalchemy import delete, select, update

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
from agents.odata import preview, testcall  # noqa: E402
from tests.odata_helpers import (  # noqa: E402
    SERVICE_PATH,
    FakeConnectivity,
    FakeResolver,
    OnPremiseResolver,
    Sap,
    service_payload,
    v2_error,
)

SERVICES = "/admin/api/odata/services"
NAME = "purchase-requisitions"
URL = f"{SERVICES}/{NAME}/test"
SAP_HOST = "s4.internal"
SECRET = "SECRET"
HEADER = "A_PurchaseRequisitionHeader"
ITEM = "A_PurchaseRequisitionItem"
# Business data of the mock's rows: a key, a description, a plant, a user.
VALUES = ("10099001", "Zeppelin spare parts", "ZZ77", "MAT-4242", "JDOE")
ROW = {
    "__metadata": {"uri": f"https://{SAP_HOST}/x('10099001')", "type": "T"},
    "PurchaseRequisition": "10099001",
    "PurchaseRequisitionItem": "00010",
    "PurReqnDescription": "Zeppelin spare parts",
    "Plant": "ZZ77",
    "Material": "MAT-4242",
    "CreatedByUser": "JDOE",
}
V2_ROWS = {"d": {"results": [ROW]}}
V4_ROWS = {
    "@odata.context": "$metadata#X",
    "@odata.count": 4711,
    "value": [{k: v for k, v in ROW.items() if k[0] != "_"}],
}
ANSWER_KEYS = {
    "ok",
    "code",
    "status",
    "duration_ms",
    "service",
    "enabled",
    "read",
    "target",
    "rows",
    "identity",
    "per_user",
    "destination",
    "auth_type",
    "proxy_type",
    "message",
    "warnings",
}


def unread(handler):
    """``handler`` with its answers as a real transport hands them over: a
    body that is still to be read (the reachability check reads the raw
    stream, which a response built with ``content=`` no longer has)."""

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
        self.sap = Sap(V2_ROWS)
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

    monkeypatch.setattr(testcall, "_resolver", resolver)
    monkeypatch.setattr(
        testcall, "_transport", lambda: httpx.MockTransport(unread(lambda r: state.sap.handler(r)))
    )
    return state


@pytest.fixture(autouse=True)
def _every_slot_is_given_back():
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
    token = jwt.encode({"user_uuid": user, "scope": ["app.admin"]}, "k" * 32, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


async def seed(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    overrides.setdefault("destination", "S4_ODATA_TECH")
    r = await client.post(SERVICES, json=service_payload(**overrides))
    assert r.status_code == 201, r.text
    return r.json()


def definition(**operations: list[str]) -> dict[str, Any]:
    """The helper's definition with the operations of the named sets replaced."""
    data = copy.deepcopy(service_payload()["definition"])
    for entity_set in data["entity_sets"]:
        if entity_set["name"] in operations:
            entity_set["operations"] = operations[entity_set["name"]]
    return data


def lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "odata test call" in r.getMessage()]


def no_data(*texts: str) -> None:
    for text in texts:
        for value in VALUES:
            assert value not in text, value


# ---------------------------------------------------------------- green path


async def test_v2_one_list_read_of_one_row_and_nothing_of_it_in_answer_or_log(
    client, remote, caplog
):
    caplog.set_level(logging.DEBUG)
    await seed(client)
    r = await client.post(URL, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == ANSWER_KEYS
    assert body["duration_ms"] >= 0
    del body["duration_ms"]
    assert body == {
        "ok": True,
        "code": None,
        "status": 200,
        "service": NAME,
        "enabled": True,
        "read": "list",
        "target": HEADER,
        "rows": 1,
        "identity": "technical",
        "per_user": False,
        "destination": "S4_ODATA_TECH",
        "auth_type": "OAuth2ClientCredentials",
        "proxy_type": "",
        "message": testcall._ROW_TEXT,
        "warnings": [],
    }
    # Exactly one request, in the shape the agent's `list` sends.
    (sent,) = remote.requests
    assert sent.method == "GET"
    assert sent.url.host == SAP_HOST and sent.url.path == f"{SERVICE_PATH}/{HEADER}"
    # `$inlinecount` too: the tools always ask for the count, and a service
    # that refuses the option must not test green.
    assert dict(sent.url.params) == {
        "$format": "json",
        "$select": "PurchaseRequisition,PurReqnDescription",
        "$top": "1",
        "$inlinecount": "allpages",
    }
    assert sent.headers["accept"] == "application/json"
    assert sent.headers["authorization"] == "Bearer dest-token"
    assert "cookie" not in sent.headers and "x-csrf-token" not in sent.headers
    assert remote.asked_for == ["S4_ODATA_TECH"]
    assert remote.resolver.calls == [(None, None)]
    no_data(r.text, caplog.text)
    assert SAP_HOST not in r.text and SAP_HOST not in caplog.text
    assert "dest-token" not in r.text and "dest-token" not in caplog.text
    (line,) = lines(caplog)
    for part in (
        f"service={NAME}",
        "destination=S4_ODATA_TECH",
        f"target={HEADER}",
        "read=list",
        "by=",
        "user_context=False",
        "auth_type=OAuth2ClientCredentials",
        "per_user=False",
        "status=200",
        "rows=1",
    ):
        assert part in line, part


async def test_v4_one_list_read(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client, odata_version="v4")
    remote.answer(V4_ROWS)
    r = await client.post(URL, json={})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True, r.text
    assert body["rows"] == 1 and body["target"] == HEADER and body["read"] == "list"
    (sent,) = remote.requests
    assert sent.url.path == f"{SERVICE_PATH}/{HEADER}"
    assert dict(sent.url.params) == {
        "$select": "PurchaseRequisition,PurReqnDescription",
        "$top": "1",
        "$count": "true",
    }
    assert "4711" not in r.text  # the count is dropped with the rows
    no_data(r.text, caplog.text)


async def test_no_row_is_still_a_working_service(client, remote):
    await seed(client)
    remote.answer({"d": {"results": []}})
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is True and body["rows"] == 0 and body["message"] == testcall._NO_ROW_TEXT


async def test_more_rows_than_asked_count_as_one(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client)
    remote.answer({"d": {"results": [ROW, ROW, ROW]}})
    r = await client.post(URL, json={})
    assert r.json()["rows"] == 1
    no_data(r.text, caplog.text)


async def test_a_paging_link_is_not_followed(client, remote):
    await seed(client)
    remote.answer(
        {"d": {"results": [], "__next": f"https://{SAP_HOST}{SERVICE_PATH}/{HEADER}?$skiptoken=1"}}
    )
    body = (await client.post(URL, json={})).json()
    assert len(remote.requests) == 1
    assert body["ok"] is True and body["rows"] == 0
    assert [w["code"] for w in body["warnings"]] == ["paging_not_followed"]


async def test_an_empty_body_is_the_default_test(client, remote):
    await seed(client)
    r = await client.post(URL)
    assert r.status_code == 200 and r.json()["ok"] is True


# ------------------------------------------------------- which entity set


async def test_a_named_entity_set_of_the_service_is_tried(client, remote):
    await seed(client)
    body = (await client.post(URL, json={"entity_set": ITEM})).json()
    assert body["ok"] is True and body["target"] == ITEM
    (sent,) = remote.requests
    assert sent.url.path == f"{SERVICE_PATH}/{ITEM}"
    assert sent.url.params["$select"] == (
        "PurchaseRequisition,PurchaseRequisitionItem,Plant,Material"
    )
    assert sent.url.params["$top"] == "1"


async def test_the_default_is_the_first_entity_set_with_list(client, remote):
    await seed(client, definition=definition(**{HEADER: ["get"]}))
    body = (await client.post(URL, json={})).json()
    assert body["target"] == ITEM and body["read"] == "list"


async def test_an_entity_set_of_another_service_is_refused_without_a_request(client, remote):
    await seed(client, definition=definition(**{ITEM: ["get"]}))
    other = service_payload(name="other-service", destination="S4_ODATA_TECH")
    other["definition"]["entity_sets"][0]["name"] = "A_Other"
    other["definition"]["entity_sets"][1]["navigations"] = []
    other["definition"]["entity_sets"][0]["navigations"] = []
    assert (await client.post(SERVICES, json=other)).status_code == 201

    r = await client.post(URL, json={"entity_set": "A_Other"})
    assert r.status_code == 422 and r.headers["x-odata-error"] == "unknown_target"
    assert "A_Other" not in r.text
    # Enabled for `get` only: a list of it is not what the catalogue allows.
    r = await client.post(URL, json={"entity_set": ITEM})
    assert r.status_code == 422 and r.headers["x-odata-error"] == "operation_disabled"
    assert remote.requests == [] and remote.asked_for == []


@pytest.mark.parametrize(
    "body",
    [
        {"destination": f"https://u:{SECRET}@evil.example"},
        {"user_context": True},
        {"service_path": f"/sap/{SECRET}"},
        {"odata_version": "v4"},
        {"top": 100},
        {"filter": f"Plant eq '{SECRET}'"},
        {"select": [SECRET]},
        {"definition": {}},
        {f"https://{SECRET}.example/?x": 1},
        {"entity_set": f"{HEADER}?$filter=Plant eq '{SECRET}'"},
        {"entity_set": f"../{SECRET}"},
        {"entity_set": 7},
        {"entity_set": [HEADER]},
        [HEADER],
        f"{SECRET}",
    ],
)
async def test_the_body_can_change_nothing_and_is_never_echoed(client, remote, body):
    await seed(client)
    r = await client.post(URL, json=body)
    assert r.status_code == 422, r.text
    assert SECRET not in r.text and "evil" not in r.text
    assert remote.requests == [] and remote.asked_for == []


async def test_an_oversized_body_is_refused(client, remote):
    await seed(client)
    r = await client.post(URL, content=b'{"entity_set": "' + b"A" * 20_000 + b'"}')
    assert r.status_code == 413 and remote.requests == []


# ------------------------------------------------------------------ identity


async def test_a_user_context_service_is_read_as_the_admin_who_calls(client, remote, caplog):
    caplog.set_level(logging.INFO, logger=testcall.logger.name)
    await seed(client, user_context=True, destination="S4_ODATA_USER")
    headers = bearer("alice")
    token = headers["Authorization"].removeprefix("Bearer ")
    r = await client.post(URL, json={}, headers=headers)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True, r.text
    assert body["identity"] == "user" and body["per_user"] is True
    assert body["auth_type"] == "OAuth2UserTokenExchange" and body["warnings"] == []
    assert remote.asked_for == ["S4_ODATA_USER"]
    ((sent_token, principal),) = remote.resolver.calls
    assert sent_token == token and principal
    (sent,) = remote.requests
    assert sent.headers["authorization"] == f"Bearer user-token-of-{principal}"
    # The admin's own JWT goes to the destination service only: no header of
    # the request to the target carries it, nor its URL.
    assert all(token not in value for value in sent.headers.values())
    assert token not in str(sent.url)
    (line,) = lines(caplog)
    assert f"by={principal}" in line and "user_context=True" in line and "per_user=True" in line
    assert token not in caplog.text and token not in r.text

    # Another admin: their own identity, nothing kept from the first call.
    other = bearer("bob")
    await client.post(URL, json={}, headers=other)
    assert remote.resolver.calls[-1][0] == other["Authorization"].removeprefix("Bearer ")
    assert remote.requests[-1].headers["authorization"] != sent.headers["authorization"]


async def test_a_user_context_service_without_a_signed_in_user_is_424(client, remote, caplog):
    caplog.set_level(logging.INFO, logger=testcall.logger.name)
    await seed(client, user_context=True)
    r = await client.post(URL, json={})
    assert r.status_code == 424 and r.headers["x-odata-error"] == "user_token_required"
    assert r.json() == {"detail": testcall._USER_REQUIRED_TEXT}
    # Nothing resolved, nothing sent -- and never the destination's own credential.
    assert remote.asked_for == [] and remote.resolver.calls == [] and remote.requests == []
    (line,) = lines(caplog)
    assert "code=user_token_required" in line


class Technical(FakeResolver):
    """A destination whose authentication type propagates no user."""

    async def resolve(self, **kwargs: Any) -> Destination:
        resolved = await super().resolve(**kwargs)
        return Destination(
            url=self.url,
            headers={"Authorization": "Basic dGVjaDp4"},
            expires_at=resolved.expires_at,
            auth_type="BasicAuthentication",
            per_user=resolved.per_user,
            proxy_type="Internet",
            queries={"sap-client": "100", "sap-language": "EN"},
        )


async def test_user_context_on_a_technical_destination_is_said(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client, user_context=True)
    remote.resolver = Technical(name="S4_ODATA_TECH")
    body = (await client.post(URL, json={}, headers=bearer("alice"))).json()
    assert body["ok"] is True and body["identity"] == "technical" and body["per_user"] is True
    assert body["auth_type"] == "BasicAuthentication" and body["proxy_type"] == "Internet"
    codes = [w["code"] for w in body["warnings"]]
    assert codes == ["technical_credential"]
    assert body["warnings"][0]["message"] == testcall._WARNINGS["technical_credential"]
    assert "the test ran" in body["warnings"][0]["message"]
    assert "dGVjaDp4" not in caplog.text


async def test_warnings_do_not_claim_a_run_when_nothing_was_sent(client, remote):
    await seed(client, user_context=True)
    # An http:// destination is refused after it was resolved, before sending.
    remote.resolver = Technical(url="http://s4.internal:8000", name="S4_ODATA_TECH")
    body = (await client.post(URL, json={}, headers=bearer("alice"))).json()
    assert body["code"] == "destination_error" and remote.requests == []
    assert body["identity"] == "technical" and body["auth_type"] == "BasicAuthentication"
    codes = [w["code"] for w in body["warnings"]]
    assert codes == ["technical_credential"]
    for warning in body["warnings"]:
        assert "ran" not in warning["message"], warning
        assert "requests through this destination" in warning["message"]


async def test_destination_query_properties_are_applied_and_no_longer_warned_about(client, remote):
    await seed(client)
    remote.resolver = Technical(name="S4_ODATA_TECH")
    r = await client.post(URL, json={})
    assert r.json()["ok"] is True and r.json()["warnings"] == []
    params = remote.requests[0].url.params
    assert params["sap-client"] == "100" and params["sap-language"] == "EN"
    # The read's own options are still there, as the client wrote them.
    assert params["$top"] == "1"


class Proxy:
    """The connectivity side of one test: the tokens and the proxy's wire."""

    def __init__(self) -> None:
        self.tokens = FakeConnectivity()
        self.sap = Sap(V2_ROWS)

    @property
    def requests(self) -> list[httpx.Request]:
        return self.sap.requests


@pytest.fixture
def proxy(monkeypatch) -> Proxy:
    state = Proxy()
    monkeypatch.setattr(testcall, "_connectivity", lambda: state.tokens)
    monkeypatch.setattr(
        testcall,
        "_proxy_transport",
        lambda: httpx.MockTransport(unread(lambda r: state.sap.handler(r))),
    )
    return state


@pytest.mark.parametrize("operations", [["list", "get"], ["get"]])
async def test_an_on_premise_destination_is_read_through_the_connectivity_proxy(
    client, remote, proxy, operations
):
    await seed(client, definition=definition(**{HEADER: operations, ITEM: operations}))
    remote.resolver = OnPremiseResolver()
    if "list" not in operations:
        proxy.sap = Sap(httpx.Response(200, content=b"<edmx:Edmx/>"))
    r = await client.post(URL, json={})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["ok"] is True and body["status"] == 200, body
    assert body["identity"] == "technical" and body["proxy_type"] == "OnPremise"
    assert body["warnings"] == [] or [w["code"] for w in body["warnings"]] == [
        "no_list_entity_set"
    ]
    assert remote.requests == []
    (sent,) = proxy.requests
    assert sent.url.scheme == "http" and sent.url.host == "s4.internal"
    assert sent.url.params["sap-client"] == "100"
    assert sent.headers["Proxy-Authorization"] == "Bearer APP"
    assert sent.headers["Authorization"] == "Basic dGVjaDp4"
    assert sent.headers["SAP-Connectivity-SCC-Location_ID"] == "LOC1"
    assert proxy.tokens.calls == [("app",)]


async def test_an_on_premise_test_as_the_signed_in_user_carries_only_that_user(
    client, remote, proxy, caplog
):
    caplog.set_level(logging.DEBUG)
    await seed(client, user_context=True, destination="S4_ODATA_USER")
    remote.resolver = OnPremiseResolver(name="S4_ODATA_USER", auth_type="PrincipalPropagation")
    headers = bearer("alice")
    body = (await client.post(URL, json={}, headers=headers)).json()
    assert body["ok"] is True and body["identity"] == "user" and body["per_user"] is True
    assert body["auth_type"] == "PrincipalPropagation" and body["warnings"] == []
    assert remote.requests == []
    (sent,) = proxy.requests
    assert sent.headers["Proxy-Authorization"] == "Bearer UX-alice"
    assert "Authorization" not in sent.headers
    assert "SAP-Connectivity-Authentication" not in sent.headers
    jwt_sent = headers["Authorization"].removeprefix("Bearer ")
    assert proxy.tokens.calls == [("user", jwt_sent, "alice")]
    assert jwt_sent not in caplog.text and "UX-alice" not in caplog.text


async def test_an_on_premise_user_test_on_a_technical_destination_sends_nothing(
    client, remote, proxy
):
    """Never the destination's stored credential next to a user's identity."""
    await seed(client, user_context=True)
    remote.resolver = OnPremiseResolver()
    body = (await client.post(URL, json={}, headers=bearer("alice"))).json()
    assert body["ok"] is False and body["code"] == "destination_error"
    assert body["message"] == (
        "OnPremise destination 'S4_ODATA_TECH' cannot act as the signed-in user: "
        "its Authentication must be PrincipalPropagation"
    )
    assert remote.requests == [] and proxy.requests == [] and proxy.tokens.calls == []


async def test_an_https_on_premise_destination_sends_nothing(client, remote, proxy):
    await seed(client)
    remote.resolver = OnPremiseResolver(url="https://s4.internal:44300")
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "destination_error"
    assert body["message"] == (
        "OnPremise destination 'S4_ODATA_TECH' must use an http:// address "
        "(virtual host and port): the Cloud Connector tunnel is what encrypts it"
    )
    assert body["status"] is None and body["proxy_type"] == "OnPremise"
    assert remote.requests == [] and proxy.requests == [] and proxy.tokens.calls == []


async def test_an_on_premise_destination_without_a_connectivity_binding_sends_nothing(
    client, remote
):
    """The real connectivity seam, in an environment without a binding."""
    await seed(client)
    remote.resolver = OnPremiseResolver()
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "destination_error"
    assert body["message"] == (
        "destination 'S4_ODATA_TECH' is an OnPremise destination, but this app has no "
        "connectivity service binding"
    )
    assert remote.requests == []


@pytest.mark.parametrize("operations", [["list", "get"], ["get"]])
@pytest.mark.parametrize("as_user", [False, True])
async def test_a_407_of_the_proxy_has_its_own_code_and_says_what_to_check(
    client, remote, proxy, as_user, operations
):
    """For the list and for the reachability check alike."""
    extra: dict[str, Any] = {"definition": definition(**{HEADER: operations, ITEM: operations})}
    headers: dict[str, str] = {}
    remote.resolver = OnPremiseResolver()
    if as_user:
        extra.update(user_context=True, destination="S4_ODATA_USER")
        remote.resolver = OnPremiseResolver(name="S4_ODATA_USER", auth_type="PrincipalPropagation")
        headers = bearer("alice")
    await seed(client, **extra)
    proxy.sap = Sap(
        httpx.Response(407, json={"error": {"code": "X/1", "message": {"value": "tenant-zone-9"}}})
    )
    body = (await client.post(URL, json={}, headers=headers)).json()
    assert body["ok"] is False and body["status"] == 407 and body["code"] == "proxy_refused"
    message = body["message"]
    assert message.startswith("HTTP 407 from the connectivity proxy: ")
    assert "connectivity service binding" in message and "CloudConnectorLocationId" in message
    assert ("CONNECTIVITY_PP_MODE" in message) is as_user
    assert "tenant-zone-9" not in message and "OData service" not in message
    assert len(proxy.requests) == 1 and proxy.tokens.invalidated == []


# ------------------------------------------------------------------ failures


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_a_sap_refusal_is_a_failed_test_never_the_routes_own_status(
    client, remote, caplog, status
):
    caplog.set_level(logging.DEBUG)
    await seed(client)
    remote.answer(
        v2_error(
            status,
            "/IWFND/CM_BEC/026",
            f"No authorisation, see https://{SAP_HOST}/help?token={SECRET} for details",
        )
    )
    r = await client.post(URL, json={})
    assert r.status_code == 200, r.text
    assert "x-odata-error" not in r.headers
    body = r.json()
    assert body["ok"] is False and body["code"] == "sap_error" and body["status"] == status
    assert body["rows"] == 0 and body["target"] == HEADER
    assert body["message"] == (
        f"HTTP {status} from the OData service: /IWFND/CM_BEC/026: "
        "No authorisation, see <url> for details"
    )
    assert len(remote.requests) == 1  # no second attempt, also after a 401
    assert len(remote.resolver.calls) == 1
    assert SECRET not in r.text and SECRET not in caplog.text and SAP_HOST not in caplog.text
    (line,) = lines(caplog)
    assert "code=sap_error" in line and f"status={status}" in line
    assert "authorisation" not in line  # SAP's text is for the admin, not the log


async def test_a_sap_error_without_an_envelope_gets_the_status_hint(client, remote):
    await seed(client)
    remote.answer(
        httpx.Response(
            403, text=f"<html>{SECRET} dump</html>", headers={"content-type": "text/html"}
        )
    )
    r = await client.post(URL, json={})
    body = r.json()
    assert body["code"] == "sap_error" and body["status"] == 403
    assert body["message"].startswith("HTTP 403 from the OData service: ")
    assert SECRET not in r.text


async def test_a_long_sap_message_is_capped(client, remote):
    await seed(client)
    remote.answer(v2_error(400, "X", "y" * 5000))
    body = (await client.post(URL, json={})).json()
    assert len(body["message"]) <= testcall.MAX_MESSAGE_CHARS


async def test_a_sign_in_page_at_200_is_a_failed_test(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client)
    remote.answer(
        httpx.Response(
            200,
            text=f"<html><form action='https://idp.{SECRET}.example'>{VALUES[0]}</form></html>",
            headers={"content-type": "text/html", "set-cookie": f"SAP_SESSIONID={SECRET}"},
        )
    )
    r = await client.post(URL, json={})
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False and body["code"] == "unexpected_answer" and body["status"] == 200
    assert body["rows"] == 0
    assert SECRET not in r.text and SECRET not in caplog.text
    no_data(r.text, caplog.text)
    assert len(remote.requests) == 1


async def test_json_of_another_shape_is_a_failed_test(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client)
    remote.answer({"rows": [ROW]})
    r = await client.post(URL, json={})
    assert r.json()["ok"] is False and r.json()["code"] == "unexpected_answer"
    no_data(r.text, caplog.text)


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_a_redirect_is_not_followed(client, remote, caplog, status):
    caplog.set_level(logging.DEBUG)
    await seed(client)
    remote.answer(
        httpx.Response(status, headers={"location": f"https://idp.{SECRET}.example/login"})
    )
    r = await client.post(URL, json={})
    body = r.json()
    assert body["ok"] is False and body["code"] == "redirect" and body["status"] == status
    assert len(remote.requests) == 1
    assert SECRET not in r.text and SECRET not in caplog.text


async def test_the_test_has_a_total_timeout(client, remote, monkeypatch):
    monkeypatch.setattr(preview, "PREVIEW_BUDGET_SECONDS", 0.05)
    await seed(client)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=V2_ROWS)

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(slow))
    started = time.monotonic()
    r = await client.post(URL, json={})
    assert time.monotonic() - started < 3
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False and body["code"] == "timeout" and body["status"] is None


class Stalls(httpx.AsyncByteStream):
    """A body that begins and then never goes on."""

    async def __aiter__(self):
        yield b'{"d": {"results": ['
        await asyncio.sleep(5)


class Breaks(httpx.AsyncByteStream):
    """A body whose connection is lost after its beginning."""

    async def __aiter__(self):
        yield b'{"d": {"results": ['
        raise httpx.ReadError(f"connection to {SAP_HOST} lost")


LIST_AND_GET = {HEADER: ["list", "get"], ITEM: ["list", "get"]}
GET_ONLY = {HEADER: ["get"], ITEM: ["get"]}


@pytest.mark.parametrize("operations", [LIST_AND_GET, GET_ONLY])
async def test_a_timeout_after_the_headers_arrived_keeps_the_status(
    client, remote, monkeypatch, operations
):
    monkeypatch.setattr(preview, "PREVIEW_BUDGET_SECONDS", 0.05)
    await seed(client, definition=definition(**operations))

    async def stalls(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/xml"}, stream=Stalls())

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(stalls))
    started = time.monotonic()
    body = (await client.post(URL, json={})).json()
    assert time.monotonic() - started < 3
    assert body["ok"] is False and body["code"] == "timeout" and body["status"] == 200


@pytest.mark.parametrize("operations", [LIST_AND_GET, GET_ONLY])
async def test_a_connection_lost_mid_body_is_unreachable_with_the_status(
    client, remote, monkeypatch, caplog, operations
):
    caplog.set_level(logging.DEBUG)
    await seed(client, definition=definition(**operations))

    async def breaks(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/xml"}, stream=Breaks())

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(breaks))
    r = await client.post(URL, json={})
    body = r.json()
    assert body["ok"] is False and body["code"] == "unreachable" and body["status"] == 200
    assert body["message"] == preview.UNREACHABLE_TEXT
    assert SAP_HOST not in r.text and SAP_HOST not in caplog.text


async def test_an_unreachable_service_names_no_host(client, remote, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(down))
    r = await client.post(URL, json={})
    body = r.json()
    assert body["ok"] is False and body["code"] == "unreachable" and body["status"] is None
    assert SAP_HOST not in r.text and SAP_HOST not in caplog.text


async def test_a_destination_that_cannot_be_resolved(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client)

    class Broken(FakeResolver):
        async def resolve(self, **_kwargs: Any) -> Destination:
            raise DestinationError(
                f"destination service returned 500 for 'S4_ODATA_TECH': "
                f"see https://dest.{SECRET}.example/x"
            )

    remote.resolver = Broken()
    r = await client.post(URL, json={})
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False and body["code"] == "destination_error"
    assert body["message"] == preview.DESTINATION_TEXT
    assert body["auth_type"] == "" and body["per_user"] is False
    # Nothing was resolved: as whom it would have run is not known.
    assert body["identity"] == "unknown" and body["warnings"] == []
    assert SECRET not in r.text and SECRET not in caplog.text
    assert remote.requests == []


async def test_identity_is_unknown_when_a_user_context_destination_was_not_resolved(
    client, remote
):
    await seed(client, user_context=True)

    class Broken(FakeResolver):
        async def resolve(self, **_kwargs: Any) -> Destination:
            raise DestinationError("no such destination")

    remote.resolver = Broken()
    body = (await client.post(URL, json={}, headers=bearer("alice"))).json()
    assert body["code"] == "destination_error"
    assert body["identity"] == "unknown" and body["per_user"] is False


async def test_no_destination_binding_is_a_destination_error(client):
    await seed(client)
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "destination_error"


async def test_a_plain_http_destination_is_refused(client, remote):
    await seed(client)
    remote.resolver = FakeResolver(url="http://s4.internal:8000", name="S4_ODATA_TECH")
    body = (await client.post(URL, json={})).json()
    assert body["code"] == "destination_error" and remote.requests == []


async def test_a_defect_is_a_fixed_text(client, remote, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client)

    def broken(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"boom {SECRET}")

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(broken))
    r = await client.post(URL, json={})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["code"] == "test_failed"
    assert SECRET not in r.text and SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_a_stored_definition_that_is_not_valid_sends_nothing(client, remote):
    await seed(client)
    async with SessionLocal() as s:
        await s.execute(
            update(ODataService)
            .where(ODataService.name == NAME)
            .values(definition_json='{"entity_sets": "nope"}')
        )
        await s.commit()
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "invalid_definition"
    assert body["identity"] == "unknown"
    assert remote.requests == [] and remote.asked_for == []


# --------------------------------------------------- the shared two-slot limit


async def test_test_calls_and_previews_share_the_two_slots(client, remote, monkeypatch):
    await seed(client)
    gate = asyncio.Event()
    arrived = 0

    async def held(_request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        arrived += 1
        await gate.wait()
        if _request.url.path.endswith("$metadata"):
            return httpx.Response(
                200,
                headers={"content-type": "application/xml"},
                stream=httpx.ByteStream(b"<nope/>"),
            )
        return httpx.Response(200, json=V2_ROWS)

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(held))
    monkeypatch.setattr(preview, "_transport", lambda: httpx.MockTransport(held))
    monkeypatch.setattr(preview, "_resolver", lambda _name: remote.resolver)
    metadata = {
        "destination": "S4_ODATA_TECH",
        "service_path": SERVICE_PATH,
        "odata_version": "v2",
    }
    first = asyncio.ensure_future(client.post(URL, json={}))
    second = asyncio.ensure_future(client.post("/admin/api/odata/metadata", json=metadata))
    for _ in range(200):
        if arrived == 2:
            break
        await asyncio.sleep(0.01)
    assert arrived == 2 and preview._active == 2
    third = await client.post(URL, json={})
    assert third.status_code == 429 and third.headers["x-odata-error"] == "busy"
    assert third.json() == {"detail": preview.BUSY_TEXT}
    assert preview.BUSY_TEXT == "other previews or test calls are running; try again in a moment"
    fourth = await client.post("/admin/api/odata/metadata", json=metadata)
    assert fourth.status_code == 429 and fourth.json() == {"detail": preview.BUSY_TEXT}
    assert arrived == 2
    gate.set()
    assert (await first).json()["ok"] is True
    await second
    assert preview._active == 0
    assert (await client.post(URL, json={})).json()["ok"] is True


@pytest.mark.parametrize("operations", [LIST_AND_GET, GET_ONLY])
async def test_a_cancelled_request_gives_its_slot_back(remote, monkeypatch, operations):
    """A client that disconnects cancels the handler while SAP is asked."""
    gate = asyncio.Event()
    arrived = asyncio.Event()

    async def held(_request: httpx.Request) -> httpx.Response:
        arrived.set()
        await gate.wait()
        return httpx.Response(200, json=V2_ROWS)

    monkeypatch.setattr(testcall, "_transport", lambda: httpx.MockTransport(held))
    service = service_payload(destination="S4_ODATA_TECH", definition=definition(**operations))
    call = asyncio.ensure_future(testcall.run_test_call(service))
    await asyncio.wait_for(arrived.wait(), 2)
    assert preview._active == 1
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert preview._active == 0


async def test_a_failed_or_refused_test_gives_its_slot_back(client, remote):
    await seed(client, user_context=True)
    for _ in range(3):
        assert (await client.post(URL, json={})).status_code == 424
    remote.answer(v2_error(500, "X", "y"))
    for _ in range(3):
        r = await client.post(URL, json={}, headers=bearer("alice"))
        assert r.json()["code"] == "sap_error"
    assert preview._active == 0


# ------------------------------------------------- no entity set with `list`


METADATA = httpx.Response(
    200,
    headers={"content-type": "application/xml"},
    content=b'<?xml version="1.0"?><edmx:Edmx xmlns:edmx="x">'
    + VALUES[1].encode()
    + b"</edmx:Edmx>",
)


async def test_without_a_list_entity_set_only_reachability_is_checked(client, remote, caplog):
    caplog.set_level(logging.DEBUG)
    await seed(client, definition=definition(**{HEADER: ["get"], ITEM: ["get"]}))
    remote.answer(METADATA)
    r = await client.post(URL, json={})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True, r.text
    assert body["read"] == "metadata" and body["target"] == "$metadata" and body["rows"] == 0
    assert body["status"] == 200 and body["message"] == testcall._METADATA_TEXT
    (warning,) = body["warnings"]
    assert warning["code"] == "no_list_entity_set" and "key" in warning["message"]
    (sent,) = remote.requests
    assert sent.method == "GET" and sent.url.path == f"{SERVICE_PATH}/$metadata"
    assert sent.url.query == b""
    assert sent.headers["accept-encoding"] == "identity"
    no_data(r.text, caplog.text)
    (line,) = lines(caplog)
    assert "read=metadata" in line


async def test_the_reachability_check_fails_like_a_read(client, remote):
    await seed(client, definition=definition(**{HEADER: ["get"], ITEM: ["get"]}))
    remote.answer(v2_error(403, "AUTH", "not authorised"))
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "sap_error" and body["status"] == 403
    assert body["message"] == "HTTP 403 from the OData service: AUTH: not authorised"

    remote.answer(
        httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"})
    )
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "unexpected_answer"

    remote.answer(httpx.Response(302, headers={"location": "https://idp.example/"}))
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["code"] == "redirect"
    assert len(remote.requests) == 1


# ---------------------------------------------------- disabled, state, scope


async def test_a_disabled_service_may_be_tested_and_says_so(client, remote):
    await seed(client, enabled=False)
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is True and body["enabled"] is False
    assert [w["code"] for w in body["warnings"]] == ["service_disabled"]
    assert len(remote.requests) == 1


async def test_a_test_call_changes_and_stores_nothing(client, remote):
    before = await seed(client)
    remote.answer(V2_ROWS, v2_error(500, "X", "y"))
    for _ in range(2):
        await client.post(URL, json={})
    assert {r.method for r in remote.requests} == {"GET"}
    after = (await client.get(f"{SERVICES}/{NAME}")).json()
    assert after == before
    async with SessionLocal() as s:
        assert (await s.execute(select(ODataAuditLog))).scalars().all() == []


async def test_an_unknown_service_is_404_before_the_body_is_looked_at(client, remote):
    for name in ("nope", "Not%20A%20Slug", "a" * 80):
        r = await client.post(f"{SERVICES}/{name}/test", json={"x": 1})
        assert r.status_code == 404 and r.json() == {"detail": "Service not found"}
    assert remote.requests == [] and remote.asked_for == []


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

    (route,) = [r for r in router.routes if r.path == "/api/odata/services/{name}/test"]
    assert route.methods == {"POST"}
    assert require_admin in [d.call for d in route.dependant.dependencies]
    assert "post" in app_module.app.openapi()["paths"][f"{SERVICES}/{{name}}/test"]


async def test_only_an_admin_reaches_the_test(client, monkeypatch, remote):
    await seed(client)
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
            r = await c.post(URL, json={})
            assert r.status_code == 401
            for token in ("usr", "dev"):
                r = await c.post(URL, json={}, headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 403
                assert r.json() == {"detail": "Admin scope required"}
            assert remote.requests == [] and remote.asked_for == []
            r = await c.post(URL, json={}, headers={"Authorization": "Bearer adm"})
            assert r.status_code == 200 and r.json()["ok"] is True
            assert len(remote.requests) == 1
    finally:
        auth.current_claims.reset(marker)


@pytest.mark.parametrize("operations", [["list", "get"], ["get"]])
async def test_a_407_on_the_direct_path_is_not_the_connectivity_proxys(client, remote, operations):
    await seed(client, definition=definition(**{HEADER: operations, ITEM: operations}))
    remote.answer(
        httpx.Response(407, json={"error": {"code": "X/1", "message": {"value": "tenant-zone-9"}}})
    )
    body = (await client.post(URL, json={})).json()
    assert body["ok"] is False and body["status"] == 407 and body["code"] == "sap_error"
    assert body["message"] == "HTTP 407 from the OData service"


async def test_an_unknown_pp_mode_refuses_a_user_test_and_says_which_setting(
    client, remote, proxy, monkeypatch
):
    monkeypatch.setenv("CONNECTIVITY_PP_MODE", "s3cret-typo")
    await seed(client, user_context=True, destination="S4_ODATA_USER")
    remote.resolver = OnPremiseResolver(name="S4_ODATA_USER", auth_type="PrincipalPropagation")
    body = (await client.post(URL, json={}, headers=bearer("alice"))).json()
    assert body["ok"] is False and body["code"] == "destination_error"
    assert body["message"] == "CONNECTIVITY_PP_MODE must be exchange or header"
    assert body["status"] is None
    assert remote.requests == [] and proxy.requests == [] and proxy.tokens.calls == []
