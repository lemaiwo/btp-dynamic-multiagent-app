"""The OData V2 write client (Task W2): create, update, delete.

SAP is an ``httpx.MockTransport`` behind the real ``destination_http_client``
(``tests/odata_helpers.py``). The double here knows which CSRF token and
which session cookie it handed to which caller (the caller being whoever the
destination's ``Authorization`` header names) and accepts a modifying request
only with that caller's own pair, so a token or cookie that travels with the
wrong identity shows up as a crossing.

No network, no database.
"""

from __future__ import annotations

import asyncio
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

from agents import auth  # noqa: E402
from agents.destination_auth import DestinationUserRequired  # noqa: E402
from agents.odata.client import (  # noqa: E402
    RAW_ETAG_FIELD,
    ODataClient,
    ODataError,
    WritePlan,
)
from agents.odata.models import ServiceDefinition  # noqa: E402
from agents.odata.session import CsrfSessionStore, NoCookieJar  # noqa: E402
from agents.odata.v2 import V2Dialect  # noqa: E402
from tests.odata_helpers import (  # noqa: E402
    SERVICE_PATH,
    FakeResolver,
    sap_v2,
    service_payload,
    v2_error,
)

ALICE = "alice@example.com"
BOB = "bob@example.com"
TECH = "dest-token"  # what FakeResolver's destination sends for the technical user
COOKIE_NAME = "SAP_SESSIONID_XXX_100"
ITEM_PATH = SERVICE_PATH + "/A_PurchaseRequisitionItem"
KEY = {"PurchaseRequisition": "10000001", "PurchaseRequisitionItem": "00010"}
KEYED = ITEM_PATH + "(PurchaseRequisition='10000001',PurchaseRequisitionItem='00010')"
ETAG = 'W/"20261005"'


def _f(name, *, selectable=True, writable=False, **extra):
    return {"name": name, "selectable": selectable, "writable": writable, **extra}


DEFINITION = {
    "entity_sets": [
        {
            "name": "A_PurchaseRequisitionItem",
            "keys": [{"name": "PurchaseRequisition"}, {"name": "PurchaseRequisitionItem"}],
            "operations": ["list", "get", "create", "update", "delete"],
            "fields": [
                _f("PurchaseRequisition", writable=True),
                _f("PurchaseRequisitionItem", writable=True),
                _f("Plant", writable=True),
                _f("Material"),
                _f("RequestedQuantity", writable=True, type="Edm.Decimal"),
                _f("ItemCount", writable=True, type="Edm.Int32"),
                _f("BigNumber", writable=True, type="Edm.Int64"),
                _f("IsClosed", writable=True, type="Edm.Boolean"),
                _f("DeliveryDate", writable=True, type="Edm.DateTime"),
                _f("ChangedAt", writable=True, type="Edm.DateTimeOffset"),
                _f("Attachment", writable=True, type="Edm.Binary"),
                _f("InternalNote", selectable=False, writable=True),
                _f("CreatedByUser", selectable=False, personal_data=True),
            ],
        },
        {
            "name": "A_ReadOnly",
            "keys": [{"name": "Id"}],
            "operations": ["list", "get"],
            "fields": [_f("Id"), _f("Text", writable=True)],
        },
    ],
    "operations": [],
}
SERVICE = service_payload(definition=DEFINITION)
USER_SERVICE = service_payload(definition=DEFINITION, user_context=True)
ES = ServiceDefinition.model_validate(SERVICE["definition"]).entity_set("A_PurchaseRequisitionItem")
ES_RO = ServiceDefinition.model_validate(SERVICE["definition"]).entity_set("A_ReadOnly")

CREATED = {
    "d": {
        "__metadata": {"uri": "https://s4.internal:44300" + KEYED, "etag": 'W/"created-1"'},
        "PurchaseRequisition": "10000001",
        "PurchaseRequisitionItem": "00010",
        "Plant": "1000",
        "InternalNote": "SECRET-NOTE",
        "CreatedByUser": "ALICE",
        "to_PurchaseReqn": {"__deferred": {"uri": "x"}},
    }
}


class Sap:
    """SAP as far as writes go: one session (token + cookie) per caller.

    ``answers`` script the answers to modifying requests that pass the CSRF
    check, in order (the last repeats); the default is a 201 for a POST
    without a method override and a 204 otherwise.
    """

    def __init__(self, *answers) -> None:
        self.issued: dict[str, tuple[str, str]] = {}
        self.generation: dict[str, int] = {}
        self.requests: list[httpx.Request] = []
        self.crossed: list[tuple[str, str, str]] = []
        self.answers = list(answers)
        self.accepted = 0
        self.fetch_answer = None  # a callable(request) overriding the token answer

    # what the tests look at
    @property
    def fetches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.headers.get("X-CSRF-Token") == "Fetch"]

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def expire(self, caller: str) -> None:
        self.issued.pop(caller, None)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        caller = request.headers.get("Authorization", "").removeprefix("Bearer ")
        await asyncio.sleep(0)  # let other runs in, as a real round trip would
        if request.headers.get("X-CSRF-Token") == "Fetch":
            if self.fetch_answer is not None:
                return self.fetch_answer(request)
            n = self.generation[caller] = self.generation.get(caller, 0) + 1
            token, cookie = f"T-{caller}-{n}", f"S-{caller}-{n}"
            self.issued[caller] = (token, cookie)
            return httpx.Response(
                200,
                headers=[
                    ("X-CSRF-Token", token),
                    ("Set-Cookie", f"{COOKIE_NAME}={cookie}; path=/; secure; HttpOnly"),
                    ("Set-Cookie", "sap-usercontext=sap-client=100; path=/"),
                ],
                json={"d": {"EntitySets": ["A_PurchaseRequisitionItem"]}},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"d": {"results": []}})
        token = request.headers.get("X-CSRF-Token", "")
        cookie = request.headers.get("Cookie", "")
        expected = self.issued.get(caller)
        if not (expected and token == expected[0] and f"{COOKIE_NAME}={expected[1]}" in cookie):
            if any(
                other != caller and (token == t or c in cookie)
                for other, (t, c) in self.issued.items()
            ):
                self.crossed.append((caller, token, cookie))
            return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="CSRF")
        self.accepted += 1
        if self.answers:
            answer = self.answers[min(self.accepted, len(self.answers)) - 1]
            if callable(answer):
                answer = answer(request)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(201, json=answer)
        if request.method == "POST" and "X-HTTP-Method" not in request.headers:
            return httpx.Response(201, json=CREATED)
        return httpx.Response(204)


class as_user:
    """Bind a signed-in user for the current task, as the JWT middleware does."""

    def __init__(self, principal: str) -> None:
        self.principal = principal

    def __enter__(self) -> None:
        self._jwt = auth.current_jwt.set(f"jwt-of-{self.principal}")
        self._principal = auth.current_principal.set(self.principal)

    def __exit__(self, *exc: object) -> None:
        auth.current_principal.reset(self._principal)
        auth.current_jwt.reset(self._jwt)


def client(sap: Sap, service=None, *, store=None, user_context=None) -> ODataClient:
    service = service or SERVICE
    if user_context is None:
        user_context = service["user_context"]
    http = sap_v2(sap.handler, resolver=FakeResolver(), user_context=user_context)
    return ODataClient(
        http, service, V2Dialect(), sessions=store if store is not None else CsrfSessionStore()
    )


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


async def refused(awaitable) -> ODataError:
    with pytest.raises(ODataError) as excinfo:
        await awaitable
    return excinfo.value


# -- CSRF --------------------------------------------------------------------


async def test_first_write_fetches_a_token_and_reuses_it():
    sap = Sap()
    c = client(sap)
    await c.create(ES, {"PurchaseRequisition": "10000001", "Plant": "1000"})
    fetch, post = sap.requests
    assert fetch.method == "GET" and fetch.url.path == SERVICE_PATH + "/"
    assert fetch.headers["X-CSRF-Token"] == "Fetch" and "Cookie" not in fetch.headers
    assert post.method == "POST" and post.url.path == ITEM_PATH and not post.url.query
    assert post.headers["X-CSRF-Token"] == f"T-{TECH}-1"
    assert post.headers["Cookie"] == f"{COOKIE_NAME}=S-{TECH}-1; sap-usercontext=sap-client=100"
    assert post.headers["Content-Type"] == "application/json"
    assert body_of(post) == {"PurchaseRequisition": "10000001", "Plant": "1000"}
    await c.create(ES, {"Plant": "2000"})
    assert len(sap.fetches) == 1 and len(sap.writes) == 2
    assert sap.last.headers["X-CSRF-Token"] == f"T-{TECH}-1"


async def test_403_required_refetches_once_and_retries_once():
    sap = Sap()
    c = client(sap)
    await c.delete(ES, KEY)
    sap.expire(TECH)  # the session timed out on the back end
    result = await c.delete(ES, KEY)
    assert result == {"ok": True, "status": 204}
    methods = [(r.method, r.headers.get("X-CSRF-Token")) for r in sap.requests]
    assert methods == [
        ("GET", "Fetch"),
        ("DELETE", f"T-{TECH}-1"),
        ("DELETE", f"T-{TECH}-1"),  # refused: 403 Required
        ("GET", "Fetch"),
        ("DELETE", f"T-{TECH}-2"),
    ]

    # A back end that keeps saying Required: one retry, then the error.
    class Stubborn(Sap):
        async def handler(self, request):
            answer = await super().handler(request)
            if request.method != "GET":
                return httpx.Response(403, headers={"X-CSRF-Token": "REQUIRED"})
            return answer

    sap = Stubborn()
    store = CsrfSessionStore()
    error = await refused(client(sap, store=store).delete(ES, KEY))
    assert error.code == "sap_error" and error.status == 403
    assert len(sap.writes) == 2 and len(sap.fetches) == 2
    assert len(store) == 0  # the refused session is not kept for the next call


async def test_403_without_the_required_header_is_not_retried():
    sap = Sap(v2_error(403, "SY/530", "No authorization"))
    store = CsrfSessionStore()
    error = await refused(client(sap, store=store).create(ES, {"Plant": "1000"}))
    assert error.code == "sap_error" and error.status == 403
    assert error.message == "SY/530: No authorization"
    assert len(sap.writes) == 1 and len(sap.fetches) == 1
    assert len(store) == 1  # the session is fine; the user is not authorised


async def test_a_token_request_that_fails_changes_nothing():
    sap = Sap()
    sap.fetch_answer = lambda request: v2_error(403, "X", "Forbidden")
    error = await refused(client(sap).create(ES, {"Plant": "1000"}))
    assert error.code == "sap_error" and error.status == 403 and not sap.writes

    sap = Sap()
    sap.fetch_answer = lambda request: httpx.Response(200, json={})  # no token header
    error = await refused(client(sap).create(ES, {"Plant": "1000"}))
    assert error.code == "sap_error" and "CSRF token" in error.message and not sap.writes

    def unreachable(request):
        raise httpx.ConnectError("no route to https://s4.internal:44300")

    sap = Sap()
    sap.fetch_answer = unreachable
    error = await refused(client(sap).create(ES, {"Plant": "1000"}))
    assert error.code == "destination_error" and "s4.internal" not in error.message


async def test_a_rotated_session_cookie_makes_the_next_write_fetch_again():
    rotate = httpx.Response(204, headers={"Set-Cookie": f"{COOKIE_NAME}=S-rotated; path=/"})
    same = httpx.Response(204, headers={"Set-Cookie": "sap-usercontext=sap-client=100; path=/"})
    sap = Sap(same, rotate, httpx.Response(204))
    store = CsrfSessionStore()
    c = client(sap, store=store)
    await c.delete(ES, KEY)
    await c.delete(ES, KEY)
    assert len(sap.fetches) == 1 and len(store) == 0
    await c.delete(ES, KEY)
    assert len(sap.fetches) == 2
    assert "S-rotated" not in sap.last.headers["Cookie"]  # only what a fetch issued


# -- identities --------------------------------------------------------------


async def test_two_users_writing_concurrently_each_send_their_own_token_and_cookie():
    sap = Sap()
    store = CsrfSessionStore()
    c = client(sap, USER_SERVICE, store=store)  # ONE client object for everybody

    async def run(principal: str, rounds: int) -> None:
        with as_user(principal):
            for n in range(rounds):
                await c.update(ES, KEY, {"Plant": f"{n:04d}"})
                await asyncio.sleep(0)

    await asyncio.gather(run(ALICE, 4), run(BOB, 4), run(ALICE, 3), run(BOB, 3))
    assert not sap.crossed and len(sap.writes) == 14
    for request in sap.writes:
        caller = request.headers["Authorization"].removeprefix("Bearer user-token-of-")
        assert caller in (ALICE, BOB)
        assert request.headers["X-CSRF-Token"] == f"T-user-token-of-{caller}-1"
        assert request.headers["Cookie"].startswith(f"{COOKIE_NAME}=S-user-token-of-{caller}-1")
    assert len(sap.fetches) == 2 and len(store) == 2
    assert len(c._http.cookies.jar) == 0


async def test_overlapping_users_with_expiring_sessions_do_not_cross():
    sap = Sap()
    c = client(sap, USER_SERVICE)

    async def run(principal: str) -> None:
        with as_user(principal):
            for _ in range(5):
                await c.delete(ES, KEY)
                sap.expire(f"user-token-of-{principal}")

    await asyncio.gather(run(ALICE), run(BOB))
    assert not sap.crossed


async def test_technical_and_user_context_service_on_one_destination_never_share_a_session():
    sap = Sap()
    store = CsrfSessionStore()
    technical = client(sap, SERVICE, store=store)
    personal = client(sap, USER_SERVICE, store=store)
    assert SERVICE["destination"] == USER_SERVICE["destination"]
    await technical.delete(ES, KEY)
    with as_user(ALICE):
        await personal.delete(ES, KEY)
        await technical.delete(ES, KEY)
    await technical.delete(ES, KEY)
    assert not sap.crossed and len(store) == 2 and len(sap.fetches) == 2
    by_caller = {r.headers["Authorization"]: r.headers["X-CSRF-Token"] for r in sap.writes}
    assert by_caller == {
        f"Bearer {TECH}": f"T-{TECH}-1",
        f"Bearer user-token-of-{ALICE}": f"T-user-token-of-{ALICE}-1",
    }
    # A user-context service without a signed-in user: refused, never technical.
    with pytest.raises(DestinationUserRequired):
        await personal.delete(ES, KEY)
    assert len(sap.writes) == 4


async def test_a_client_whose_connection_does_not_match_the_service_refuses_to_write():
    sap = Sap()
    # The service says "as the user" but the connection would send the technical credential.
    mismatch = client(sap, USER_SERVICE, user_context=False)
    with as_user(ALICE):
        error = await refused(mismatch.delete(ES, KEY))
    assert error.code == "destination_error" and not sap.requests
    other = client(sap, service_payload(definition=DEFINITION, destination="S4_ODATA_TECH"))
    assert (await refused(other.delete(ES, KEY))).code == "destination_error"
    no_store = ODataClient(sap_v2(sap.handler), SERVICE, V2Dialect())
    assert (await refused(no_store.delete(ES, KEY))).code == "destination_error"
    assert not sap.requests


async def test_a_writing_client_never_keeps_cookies_in_its_http_client():
    sap = Sap()
    http = sap_v2(sap.handler)  # comes with httpx's default, storing jar
    c = ODataClient(http, SERVICE, V2Dialect(), sessions=CsrfSessionStore())
    assert isinstance(http.cookies.jar, NoCookieJar)
    await c.delete(ES, KEY)
    assert len(http.cookies.jar) == 0


async def test_reads_send_no_cookie_and_no_csrf_token():
    sap = Sap()
    c = client(sap)
    await c.delete(ES, KEY)  # a session now exists for this identity
    await c.get(ES, KEY, select=[], expand=[])
    read = sap.last
    assert read.method == "GET"
    assert "Cookie" not in read.headers and "X-CSRF-Token" not in read.headers
    assert len(sap.fetches) == 1


# -- update, delete, ETag ----------------------------------------------------


async def test_update_sends_merge_and_if_match():
    sap = Sap(httpx.Response(204, headers={"ETag": 'W/"20261006"'}))
    c = client(sap)
    result = await c.update(ES, KEY, {"RequestedQuantity": "5"}, etag=ETAG)
    r = sap.last
    assert r.method == "POST" and r.headers["X-HTTP-Method"] == "MERGE"
    assert r.headers["If-Match"] == ETAG
    assert r.url.path == KEYED and body_of(r) == {"RequestedQuantity": "5"}
    assert result == {"ok": True, "status": 204, RAW_ETAG_FIELD: 'W/"20261006"'}
    assert V2Dialect().update_request(KEYED, {}) == ("POST", {"X-HTTP-Method": "MERGE"})


async def test_if_match_star_is_never_sent():
    sap = Sap()
    c = client(sap)
    for bad in ("*", " * ", 'W/"a", *', '"a", "b"', "", "20261005", 'W/"a"\r\nX: y', 5, ["*"]):
        for call in (c.update(ES, KEY, {"Plant": "1"}, etag=bad), c.delete(ES, KEY, etag=bad)):
            assert (await refused(call)).code == "invalid_argument"
    assert not sap.requests
    await c.update(ES, KEY, {"Plant": "1"})
    await c.delete(ES, KEY)
    assert len(sap.writes) == 2
    assert all("If-Match" not in r.headers for r in sap.writes)
    await c.delete(ES, KEY, etag=ETAG)
    assert sap.last.method == "DELETE" and sap.last.headers["If-Match"] == ETAG


async def test_428_becomes_etag_required_with_a_read_first_hint():
    sap = Sap(v2_error(428, "/IWBEP/CM_MGW_RT/175", "Precondition required"))
    error = await refused(client(sap).update(ES, KEY, {"Plant": "1"}))
    assert error.code == "etag_required" and error.status == 428
    assert "read the entity" in error.hint and "etag" in error.hint
    assert len(sap.writes) == 1


async def test_412_is_reported_as_changed_since_read():
    sap = Sap(v2_error(412, "/IWBEP/CM_MGW_RT/022", "Precondition failed"))
    error = await refused(client(sap).delete(ES, KEY, etag=ETAG))
    assert error.code == "sap_error" and error.status == 412
    assert error.message == "/IWBEP/CM_MGW_RT/022: Precondition failed"
    assert error.hint == "read the entity again and retry with its etag"
    assert len(sap.writes) == 1


async def test_delete_returns_ok_and_status():
    sap = Sap()
    assert await client(sap).delete(ES, KEY) == {"ok": True, "status": 204}
    r = sap.last
    assert r.method == "DELETE" and r.url.path == KEYED and not r.content
    assert "Content-Type" not in r.headers


# -- create ------------------------------------------------------------------


async def test_create_returns_the_created_item_and_etag_filtered_to_selectable_fields():
    sap = Sap()
    result = await client(sap).create(
        ES, {"PurchaseRequisition": "10000001", "Plant": "1000", "InternalNote": "n"}
    )
    assert result == {
        "item": {
            "PurchaseRequisition": "10000001",
            "PurchaseRequisitionItem": "00010",
            "Plant": "1000",
        },
        "status": 201,
        RAW_ETAG_FIELD: 'W/"created-1"',
    }
    # Written, but not released for reading: neither value comes back.
    assert "SECRET-NOTE" not in json.dumps(result) and "ALICE" not in json.dumps(result)
    assert "__metadata" not in result["item"]


async def test_create_without_an_echo_still_succeeds():
    for answer in (httpx.Response(204), httpx.Response(201, text="<html>ok</html>")):
        sap = Sap(answer)
        result = await client(sap).create(ES, {"Plant": "1000"})
        assert result == {"item": {}, "status": answer.status_code}


async def test_body_values_are_encoded_by_edm_type():
    sap = Sap()
    await client(sap).create(
        ES,
        {
            "Plant": 1000,
            "RequestedQuantity": 5.5,
            "ItemCount": "7",
            "BigNumber": 9007199254740993,
            "IsClosed": "true",
            "DeliveryDate": "2026-10-05T00:00:00",
            "ChangedAt": "2026-10-05T12:00:00+02:00",
            "InternalNote": None,
        },
    )
    assert body_of(sap.last) == {
        "Plant": "1000",
        "RequestedQuantity": "5.5",
        "ItemCount": 7,
        "BigNumber": "9007199254740993",
        "IsClosed": True,
        "DeliveryDate": "/Date(1791158400000)/",
        "ChangedAt": "/Date(1791194400000+0120)/",
        "InternalNote": None,
    }
    # The form a read returned is handed back unchanged.
    await client(sap).create(ES, {"DeliveryDate": "/Date(1791158400000)/"})
    assert body_of(sap.last) == {"DeliveryDate": "/Date(1791158400000)/"}


# -- the gate ----------------------------------------------------------------


@pytest.mark.parametrize(
    "operation, kwargs, code, names",
    [
        ("merge", {"body": {"Plant": "1"}}, "invalid_argument", ""),
        ("create", {"key": KEY, "body": {"Plant": "1"}}, "invalid_argument", ""),
        ("create", {"body": {}}, "invalid_argument", ""),
        ("create", {"body": None}, "invalid_argument", ""),
        ("create", {"body": [{"Plant": "1"}]}, "invalid_argument", ""),
        ("create", {"body": {"Plant": "1"}, "etag": ETAG}, "invalid_argument", ""),
        ("create", {"body": {"Nope": "1"}}, "unknown_field", "Nope"),
        ("create", {"body": {"Plant": "1", 5: "x"}}, "unknown_field", ""),
        ("create", {"body": {"Material": "TG11"}}, "field_not_writable", "Material"),
        ("create", {"body": {"CreatedByUser": "X"}}, "field_not_writable", "CreatedByUser"),
        ("create", {"body": {"Plant": {"deep": 1}}}, "invalid_argument", "Plant"),
        ("create", {"body": {"Plant": ["1000"]}}, "invalid_argument", "Plant"),
        ("create", {"body": {"Plant": 1.5}}, "invalid_argument", "Plant"),
        ("create", {"body": {"Plant": "a\x00b"}}, "invalid_argument", "Plant"),
        ("create", {"body": {"ItemCount": "seven"}}, "invalid_argument", "ItemCount"),
        ("create", {"body": {"ItemCount": 2**31}}, "invalid_argument", "ItemCount"),
        ("create", {"body": {"ItemCount": True}}, "invalid_argument", "ItemCount"),
        ("create", {"body": {"IsClosed": "yes"}}, "invalid_argument", "IsClosed"),
        ("create", {"body": {"RequestedQuantity": "1e5"}}, "invalid_argument", "RequestedQuantity"),
        ("create", {"body": {"DeliveryDate": "2026-13-05T00:00:00"}}, "invalid_argument", ""),
        ("create", {"body": {"DeliveryDate": "2026-02-30T00:00:00"}}, "invalid_argument", ""),
        ("create", {"body": {"DeliveryDate": "2026-10-05T00:00:00Z"}}, "invalid_argument", ""),
        ("create", {"body": {"ChangedAt": "2026-10-05T00:00:00"}}, "invalid_argument", ""),
        ("create", {"body": {"Attachment": "QUJD"}}, "invalid_argument", "Attachment"),
        ("update", {"body": {"Plant": "1"}}, "invalid_key", ""),
        ("update", {"key": {"PurchaseRequisition": "1"}, "body": {"Plant": "1"}}, "invalid_key", ""),  # noqa: E501
        ("update", {"key": KEY, "body": {}}, "invalid_argument", ""),
        (
            "update",
            {"key": KEY, "body": {"PurchaseRequisition": "99999999", "Plant": "1"}},
            "invalid_argument",
            "PurchaseRequisition",
        ),
        ("update", {"key": KEY, "body": {"PurchaseRequisition": "10000001"}}, "invalid_argument", ""),  # noqa: E501
        ("delete", {"key": KEY, "body": {"Plant": "1"}}, "invalid_argument", ""),
        ("delete", {}, "invalid_key", ""),
        ("delete", {"key": {**KEY, "PurchaseRequisitionItem": "1/../2"}}, "invalid_key", ""),
    ],
)
def test_check_write_refuses(operation, kwargs, code, names):
    c = ODataClient(None, SERVICE, V2Dialect())  # the gate needs no connection
    with pytest.raises(ODataError) as excinfo:
        c.check_write(ES, operation, **kwargs)
    assert excinfo.value.code == code
    assert names in excinfo.value.message
    # A refusal names fields, never values.
    for value in ("99999999", "seven", "TG11", "QUJD", "2026-13", "1e5", "yes"):
        assert value not in excinfo.value.message


def test_check_write_order_and_plan():
    c = ODataClient(None, SERVICE, V2Dialect())
    for operation in ("create", "update", "delete"):
        with pytest.raises(ODataError) as excinfo:  # disabled wins over a bad key and body
            c.check_write(ES_RO, operation, key={"bad": 1}, body={"Nope": {}})
        assert excinfo.value.code == "operation_disabled"
    with pytest.raises(ODataError) as excinfo:  # the key is checked before the body
        c.check_write(ES, "update", key={}, body={"Nope": "1"})
    assert excinfo.value.code == "invalid_key"
    with pytest.raises(ODataError) as excinfo:  # unknown/not writable before the value
        c.check_write(ES, "create", body={"Plant": {"deep": 1}, "Material": "x"})
    assert excinfo.value.code == "field_not_writable"
    with pytest.raises(ODataError) as excinfo:
        c.check_write(ES, "create", body={"InternalNote": "x" * 1_100_000})
    assert excinfo.value.code == "invalid_argument" and "larger" in excinfo.value.message

    plan = c.check_write(
        ES, "update", key=KEY, body={"PurchaseRequisition": 10000001, "Plant": "VAL-1"}, etag=ETAG
    )
    assert isinstance(plan, WritePlan)
    # A key field equal to the key is accepted and not sent.
    assert plan.body == {"Plant": "VAL-1"} and plan.fields == ("Plant",)
    assert plan.path == KEYED and plan.etag == ETAG and plan.operation == "update"
    assert "VAL-1" not in repr(plan) and "20261005" not in repr(plan)
    plan = c.check_write(ES, "delete", key=KEY)
    assert plan.body is None and plan.fields == () and plan.etag is None


# -- unknown outcome, secrets ------------------------------------------------


async def test_a_network_error_on_a_write_is_never_retried_and_says_the_outcome_is_unknown():
    def lost(request):
        raise httpx.ReadTimeout("timed out reading https://s4.internal:44300" + KEYED)

    sap = Sap(lost)
    store = CsrfSessionStore()
    error = await refused(client(sap, store=store).update(ES, KEY, {"Plant": "1"}))
    assert error.code == "write_outcome_unknown" and error.status is None
    assert "not known" in error.message and "read the entity" in error.hint
    assert len(sap.writes) == 1  # sent once, not again
    assert "s4.internal" not in error.message + error.hint


async def test_token_and_cookie_are_never_in_an_error_message_or_log(caplog):
    caplog.set_level(logging.DEBUG)
    secret_body = {"Plant": "PLANT-VALUE-1", "InternalNote": "NOTE-VALUE-1"}

    def lost(request):
        raise httpx.ReadError(f"broken pipe to {request.url} with {request.headers}")

    answers = (
        v2_error(400, "ZX/001", "Invalid value"),
        httpx.Response(500, headers={"content-type": "text/html"}, text="<html>s4.internal</html>"),
        v2_error(428, "A", "b"),
        v2_error(412, "A", "b"),
        lost,
    )
    sap = Sap(*answers)
    store = CsrfSessionStore()
    c = client(sap, store=store)
    errors = [await refused(c.update(ES, KEY, secret_body, etag=ETAG)) for _ in answers]
    sap.expire(TECH)

    class Stubborn(Sap):
        async def handler(self, request):
            answer = await super().handler(request)
            if request.method != "GET":
                return httpx.Response(403, headers={"X-CSRF-Token": "Required"})
            return answer

    stubborn = Stubborn()
    errors.append(await refused(client(stubborn).create(ES, secret_body)))
    plan = c.check_write(ES, "update", key=KEY, body=secret_body, etag=ETAG)
    # This app's own log lines. The `httpx` library logger writes every
    # request URL at INFO by itself; that is the application's logging
    # configuration and not something this client can decide.
    own = [r.getMessage() for r in caplog.records if r.name.startswith("agents.")]
    assert len(own) >= 10
    text = "\n".join(own) + repr(c) + repr(store) + repr(plan)
    for error in errors:
        text += f"{error!r}{error}{error.to_dict()}{error.hint}{error.__cause__!r}"
    assert len(sap.writes) == 5 and len(stubborn.writes) == 2
    for secret in (
        f"T-{TECH}",
        f"S-{TECH}",
        COOKIE_NAME,
        "PLANT-VALUE-1",
        "NOTE-VALUE-1",
        "s4.internal",
        "dest-token",
        "20261005",
    ):
        assert secret not in text, secret
    assert "odata: update on entity set A_PurchaseRequisitionItem answered HTTP 400" in own
