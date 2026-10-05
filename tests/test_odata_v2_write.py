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

# The application's logging configuration, which the platform-log test below
# looks at. Imported here, before the environment is cleared: `app` loads the
# local `.env`, and what that sets must not survive into the tests.
import app  # noqa: E402, F401

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
    ReadQuery,
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
        # Answers given to the next modifying requests BEFORE any CSRF check
        # (a 401 from the ICF, a 403 Required), one each, in order.
        self.pre: list[httpx.Response] = []

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
        if self.pre:
            return self.pre.pop(0)
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


async def test_get_hands_out_only_an_etag_a_write_would_accept():
    row = {"__metadata": {"etag": "*"}, "Plant": "1000"}
    for headers in ({}, {"ETag": "not-a-tag"}, {"ETag": 'W/"a", "b"'}):
        sap = Sap()
        sap.handler = lambda request, h=headers: httpx.Response(200, json={"d": row}, headers=h)
        result = await client(sap).get(ES, KEY, select=[], expand=[])
        assert RAW_ETAG_FIELD not in result and result["item"] == {"Plant": "1000"}
    sap = Sap()
    sap.handler = lambda request: httpx.Response(200, json={"d": row}, headers={"ETag": ETAG})
    c = client(sap)
    result = await c.get(ES, KEY, select=[], expand=[])
    assert result[RAW_ETAG_FIELD] == ETAG
    c.check_write(ES, "update", key=KEY, body={"Plant": "1"}, etag=result[RAW_ETAG_FIELD])


async def test_a_rotated_session_cookie_is_used_for_the_next_write_without_a_new_fetch():
    sap = Sap()

    def rotate(request):
        token, _ = sap.issued[TECH]
        sap.issued[TECH] = (token, "S-rotated")  # SAP moved the session on
        return httpx.Response(204, headers={"Set-Cookie": f"{COOKIE_NAME}=S-rotated; path=/"})

    sap.answers = [httpx.Response(204), rotate, httpx.Response(204)]
    store = CsrfSessionStore()
    c = client(sap, store=store)
    await c.delete(ES, KEY)
    await c.delete(ES, KEY)
    await c.delete(ES, KEY)
    assert len(sap.fetches) == 1 and len(store) == 1 and len(sap.writes) == 3
    assert sap.last.headers["Cookie"] == (
        f"{COOKIE_NAME}=S-rotated; sap-usercontext=sap-client=100"
    )
    assert sap.last.headers["X-CSRF-Token"] == f"T-{TECH}-1"


# -- the destination's own 401 retry -----------------------------------------
# `DestinationAuth` re-sends a request once after a 401 (its cached credential
# aged out), also a POST. A 401 is a refusal before processing, so the repeat
# cannot apply a change twice; these tests pin how far the repeats can go.


async def test_a_401_is_resent_once_by_the_destination_auth_and_creates_once():
    sap = Sap()
    sap.pre = [httpx.Response(401)]
    result = await client(sap).create(ES, {"Plant": "1000"})
    assert result["status"] == 201 and result["item"]["Plant"] == "1000"
    assert len(sap.writes) == 2 and sap.accepted == 1 and len(sap.fetches) == 1


async def test_401_and_csrf_retries_together_are_bounded_at_four_sends():
    required = httpx.Response(403, headers={"X-CSRF-Token": "Required"})
    sap = Sap()
    sap.pre = [httpx.Response(401), required, httpx.Response(401)]
    result = await client(sap).create(ES, {"Plant": "1000"})
    assert result["status"] == 201
    assert len(sap.writes) == 4 and sap.accepted == 1 and len(sap.fetches) == 2

    # And it stops there: refusals all the way down are an error, not a loop.
    sap = Sap()
    sap.pre = [httpx.Response(401), required, httpx.Response(401), httpx.Response(401)]
    error = await refused(client(sap).create(ES, {"Plant": "1000"}))
    assert error.code == "sap_error" and error.status == 401
    assert len(sap.writes) == 4 and sap.accepted == 0 and len(sap.fetches) == 2


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
    sap = Sap(httpx.Response(201))
    assert await client(sap).create(ES, {"Plant": "1000"}) == {"item": {}, "status": 201}


LOGON_PAGE = {"content-type": "text/html; charset=utf-8"}


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, headers=LOGON_PAGE, text="<html><form>Logon</form></html>"),
        httpx.Response(201, headers=LOGON_PAGE, text="<html><form>Logon</form></html>"),
        httpx.Response(204, headers=LOGON_PAGE, text="<html>odd</html>"),
        httpx.Response(202),
        httpx.Response(200, text="OK"),
    ],
)
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_a_2xx_that_is_not_the_answer_of_a_write_is_not_reported_as_success(
    operation, answer
):
    """A sign-in page arrives as ``200 text/html``: that is not a write."""
    sap = Sap(answer)
    c = client(sap)
    call = {
        "create": lambda: c.create(ES, {"Plant": "1000"}),
        "update": lambda: c.update(ES, KEY, {"Plant": "1000"}),
        "delete": lambda: c.delete(ES, KEY),
    }[operation]
    error = await refused(call())
    assert error.code == "write_outcome_unknown" and error.status == answer.status_code
    # A create has no key to read back, so its hint is its own.
    expected = "list by the values you sent" if operation == "create" else "read the entity first"
    assert expected in error.hint and "Logon" not in error.message
    assert ("read the entity first" in error.hint) is (operation != "create")
    assert len(sap.writes) == 1


async def test_what_counts_as_a_successful_write():
    # create: 201, or 200 with the entity; nothing else.
    for answer in (httpx.Response(200, json=CREATED), httpx.Response(201, json=CREATED)):
        result = await client(Sap(answer)).create(ES, {"Plant": "1000"})
        assert result["item"]["Plant"] == "1000" and result["status"] == answer.status_code
    for answer in (
        httpx.Response(200),
        httpx.Response(204),
        httpx.Response(201, json={"unexpected": 1}),
    ):
        error = await refused(client(Sap(answer)).create(ES, {"Plant": "1000"}))
        assert error.code == "write_outcome_unknown"
    # update / delete: 204, or 200 with nothing or with the entity.
    for answer in (httpx.Response(204), httpx.Response(200), httpx.Response(200, json={"d": {}})):
        c = client(Sap(answer))
        assert (await c.update(ES, KEY, {"Plant": "1"}))["ok"] is True
        assert await c.delete(ES, KEY) == {"ok": True, "status": answer.status_code}
    error = await refused(client(Sap(httpx.Response(201))).delete(ES, KEY))
    assert error.code == "write_outcome_unknown"


ERROR_IN_A_200 = {"error": {"code": "ZX/001", "message": {"value": "Not changed"}}}


@pytest.mark.parametrize(
    "body",
    [
        ERROR_IN_A_200,
        {"d": CREATED["d"], "error": {"code": "ZX/001"}},  # an entity AND an error
        {"error": None},
        {"ok": True},
        {"results": []},
        ["d"],
        "done",
        0,
        b"null",
    ],
)
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_a_200_with_a_body_that_is_no_entity_is_not_a_completed_change(operation, body):
    """JSON alone proves nothing: a proxy can wrap SAP's error in a 200."""
    as_content = {"content": body} if isinstance(body, bytes) else {"json": body}
    sap = Sap(httpx.Response(200, **as_content))
    c = client(sap)
    call = c.update(ES, KEY, {"Plant": "1"}) if operation == "update" else c.delete(ES, KEY)
    error = await refused(call)
    assert error.code == "write_outcome_unknown" and error.status == 200
    assert "read the entity first" in error.hint
    assert "ZX/001" not in error.message and len(sap.writes) == 1
    # The same for a 204 that carries one.
    error = await refused(client(Sap(httpx.Response(204, **as_content))).delete(ES, KEY))
    assert error.code == "write_outcome_unknown"


async def test_an_error_body_is_not_a_created_entity():
    for answer in (
        httpx.Response(201, json=ERROR_IN_A_200),
        httpx.Response(200, json={"d": CREATED["d"], "error": {"code": "ZX/001"}}),
    ):
        error = await refused(client(Sap(answer)).create(ES, {"Plant": "1000"}))
        assert error.code == "write_outcome_unknown"
        assert "list by the values you sent" in error.hint


async def test_an_unknown_create_says_to_list_first_wherever_it_comes_from():
    def lost(request):
        raise httpx.ReadTimeout("timed out")

    for answer in (lost, httpx.Response(504, text="x"), httpx.Response(200, text="OK")):
        error = await refused(client(Sap(answer)).create(ES, {"Plant": "1000"}))
        assert error.code == "write_outcome_unknown"
        assert "list by the values you sent before trying again" in error.hint
        assert "read the entity first" not in error.hint
        error = await refused(client(Sap(answer)).update(ES, KEY, {"Plant": "1000"}))
        assert error.code == "write_outcome_unknown" and "read the entity first" in error.hint


SIGN_IN_COOKIE = "MYSAPSSO2=SIGN-IN-PAGE-COOKIE; path=/"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(
            200,
            headers=[("content-type", "text/html"), ("Set-Cookie", SIGN_IN_COOKIE)],
            text="<html><form>Logon</form></html>",
        ),
        httpx.Response(200, headers={"Set-Cookie": SIGN_IN_COOKIE}, json=ERROR_IN_A_200),
        httpx.Response(202, headers={"Set-Cookie": SIGN_IN_COOKIE}),
    ],
)
async def test_cookies_of_an_unconfirmed_answer_never_reach_the_stored_session(answer):
    sap = Sap(answer, httpx.Response(204))
    store = CsrfSessionStore()
    c = client(sap, store=store)
    error = await refused(c.update(ES, KEY, {"Plant": "1"}))
    assert error.code == "write_outcome_unknown"
    # The session that did not get the write through is gone, and nothing of
    # the answer was merged into it: the next write starts from a new token.
    assert len(store) == 0
    assert (await c.update(ES, KEY, {"Plant": "1"}))["ok"] is True
    assert len(sap.fetches) == 2 and len(sap.writes) == 2
    for request in sap.requests:
        assert "SIGN-IN-PAGE-COOKIE" not in request.headers.get("Cookie", "")
        assert "MYSAPSSO2" not in request.headers.get("Cookie", "")
    assert f"T-{TECH}-2" == sap.writes[1].headers["X-CSRF-Token"]


async def test_cookies_of_a_refused_change_are_not_merged_either():
    """A 4xx keeps the session as it was sent: only a confirmed write moves it on."""
    refusal = httpx.Response(
        400,
        headers={"Set-Cookie": "MYSAPSSO2=FROM-A-400; path=/"},
        json={"error": {"code": "ZX/001", "message": {"value": "Invalid value"}}},
    )
    sap = Sap(refusal, httpx.Response(204))
    store = CsrfSessionStore()
    c = client(sap, store=store)
    assert (await refused(c.update(ES, KEY, {"Plant": "1"}))).code == "sap_error"
    assert (await c.update(ES, KEY, {"Plant": "1"}))["ok"] is True
    assert len(sap.fetches) == 1  # the session itself is kept
    assert "FROM-A-400" not in sap.writes[1].headers["Cookie"]


async def test_a_gateway_timeout_on_a_write_leaves_the_outcome_open():
    for status in (502, 504):
        error = await refused(client(Sap(httpx.Response(status, text="x"))).delete(ES, KEY))
        assert error.code == "write_outcome_unknown" and error.status == status
    # An OData error envelope is SAP's own answer: the change was refused.
    error = await refused(client(Sap(v2_error(502, "A", "b"))).delete(ES, KEY))
    assert error.code == "sap_error"
    error = await refused(client(Sap(v2_error(500, "A", "b"))).delete(ES, KEY))
    assert error.code == "sap_error"


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
            "ChangedAt": "2026-10-05T12:00:00Z",
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
        "ChangedAt": "/Date(1791201600000+0000)/",
        "InternalNote": None,
    }
    # The form a read returned is handed back unchanged.
    echoed = {"DeliveryDate": "/Date(1791158400000)/", "ChangedAt": "/Date(1791194400000+0120)/"}
    await client(sap).create(ES, echoed)
    assert body_of(sap.last) == echoed
    await client(sap).create(ES, {"ChangedAt": "2026-10-05T12:00:00+00:00"})
    assert body_of(sap.last) == {"ChangedAt": "/Date(1791201600000+0000)/"}


@pytest.mark.parametrize(
    "field, value, accepted",
    [
        ("DeliveryDate", "2031-03-04", "2026-10-05T00:00:00"),
        ("DeliveryDate", "2031-03-04T00:00:00Z", "2026-10-05T00:00:00"),
        ("ChangedAt", "2031-03-04T12:00:00+02:00", "2026-10-05T12:00:00Z"),
        ("ChangedAt", "2031-03-04T12:00:00-00:30", "2026-10-05T12:00:00Z"),
        ("ChangedAt", "2031-03-04T12:00:00", "2026-10-05T12:00:00Z"),
    ],
)
def test_a_refused_date_value_says_which_form_is_accepted(field, value, accepted):
    c = ODataClient(None, SERVICE, V2Dialect())
    with pytest.raises(ODataError) as excinfo:
        c.check_write(ES, "create", body={field: value})
    error = excinfo.value
    assert error.code == "invalid_argument" and field in error.message
    assert accepted in error.hint and "/Date(" in error.hint
    assert "2031" not in error.hint + error.message  # the value is not repeated


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
    # Neither the path (key values) nor the catalogue entry, only names.
    assert "10000001" not in repr(plan) and "CreatedByUser" not in repr(plan)
    assert repr(plan) == (
        "WritePlan(operation='update', entity_set='A_PurchaseRequisitionItem', fields=('Plant',))"
    )
    plan = c.check_write(ES, "delete", key=KEY)
    assert plan.body is None and plan.fields == () and plan.etag is None


# -- unknown outcome, secrets ------------------------------------------------


class Odd(Exception):
    """Not an httpx error: a custom transport's own failure."""


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("timed out reading https://s4.internal:44300" + KEYED),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.StreamError("stream consumed"),
        Odd("https://s4.internal:44300 went away"),
        TimeoutError("inner timeout"),
    ],
)
async def test_a_failure_while_sending_is_never_retried_and_says_the_outcome_is_unknown(exc):
    def lost(request):
        raise exc

    sap = Sap(lost)
    error = await refused(client(sap).update(ES, KEY, {"Plant": "1"}))
    assert error.code == "write_outcome_unknown" and error.status is None
    assert "not known" in error.message and "read the entity first" in error.hint
    assert len(sap.writes) == 1  # sent once, not again
    assert "s4.internal" not in error.message + error.hint
    assert error.__cause__ is None and error.__suppress_context__


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("no route to https://s4.internal:44300"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.PoolTimeout("no connection free"),
    ],
)
async def test_a_connection_that_was_never_made_changed_nothing(exc):
    def never(request):
        raise exc

    sap = Sap(never)
    error = await refused(client(sap).update(ES, KEY, {"Plant": "1"}))
    assert error.code == "destination_error" and "nothing was changed" in error.message
    assert len(sap.writes) == 1 and "s4.internal" not in error.message


async def test_a_destination_error_and_a_cancellation_pass_through():
    from agents.destination import DestinationError

    def refuse(request):
        raise DestinationError("the destination could not be resolved")

    with pytest.raises(DestinationError):
        await client(Sap(refuse)).delete(ES, KEY)

    started = asyncio.Event()

    class Slow(Sap):
        async def handler(self, request):
            if request.method != "GET" and not started.is_set():
                started.set()
                await asyncio.sleep(30)
            return await super().handler(request)

    sap = Slow()
    c = client(sap)
    task = asyncio.create_task(c.delete(ES, KEY))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Nothing is left half-open: the cancelled client itself serves the next
    # call, on the session it already had.
    assert c._http.is_closed is False
    assert await c.delete(ES, KEY) == {"ok": True, "status": 204}
    # One token for both; the double records a request when it answers it,
    # which the cancelled one never got to.
    assert len(sap.fetches) == 1 and len(sap.writes) == 1


async def test_the_platform_log_carries_no_host_key_or_filter_value(caplog):
    """With the app's logging set up, at INFO on the ROOT logger.

    `httpx` logs ``HTTP Request: <METHOD> <url>`` at INFO by itself, after
    the destination rewrite: host, key predicate and ``$filter`` values of
    every user. ``app.py`` turns that down; this drives a read and a write
    through the real client and looks at everything that was logged.
    """
    caplog.set_level(logging.INFO)  # the root logger, as in the platform log
    sap = Sap()
    c = client(sap)
    listing = DEFINITION["entity_sets"][0]
    service = service_payload(
        definition={
            **DEFINITION,
            "entity_sets": [
                {
                    **listing,
                    "fields": [
                        {**f, "filterable": True} if f["name"] == "Plant" else f
                        for f in listing["fields"]
                    ],
                },
                DEFINITION["entity_sets"][1],
            ],
        }
    )
    reader = client(sap, service)
    es = ServiceDefinition.model_validate(service["definition"]).entity_set(ES.name)
    await reader.list(
        es,
        ReadQuery(select=[], filter="Plant eq 'FILTER-VAL'", expand=[], orderby=[], top=5, skip=0),
    )
    await reader.get(es, KEY, select=[], expand=[])
    await c.update(ES, KEY, {"Plant": "BODY-VAL"}, etag=ETAG)
    assert "FILTER-VAL" in str(sap.requests[0].url.params) and len(sap.writes) == 1
    assert "odata: update on entity set" in caplog.text  # something was logged at all
    for secret in ("s4.internal", "10000001", "00010", "FILTER-VAL", "BODY-VAL", SERVICE_PATH):
        assert secret not in caplog.text, secret
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]


def test_the_application_turns_the_http_library_loggers_down():
    """What the test above relies on, stated on its own: `app` (imported at
    the top of this module) raises the loggers that would write request URLs."""
    assert "app" in sys.modules
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


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
