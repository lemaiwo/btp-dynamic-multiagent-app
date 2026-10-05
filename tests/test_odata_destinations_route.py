"""``GET /admin/api/odata/destinations``: the destinations a dropdown may offer.

The route is driven through the real app; the destination service (its token
endpoint and its two listing paths) is an ``httpx.MockTransport`` put in
through the two seams of ``agents.odata.destinations`` (``_config``,
``_transport``). No network.

Every destination the mock lists carries the values in ``SECRETS`` in its
credential-bearing properties; none of them, and neither the service token nor
the client secret, may show up in an answer, a header or a log line.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

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
):
    os.environ.pop(_v, None)

import app as app_module  # noqa: E402
from agents.destination import DestinationServiceConfig  # noqa: E402
from agents.odata import destinations  # noqa: E402

URL = "/admin/api/odata/destinations"
API = "https://dest-api.example.test"
TOKEN_URL = "https://auth.example.test/oauth/token"
INSTANCE = "/destination-configuration/v1/instanceDestinations"
SUBACCOUNT = "/destination-configuration/v1/subaccountDestinations"
SERVICE_TOKEN = "svc-token-7f3a9c1e"
CLIENT_SECRET = "client-secret-55d0b2"

S_URL = "https://backend-9911.internal.example.test:44300/sap"
S_USER = "TECHUSER_ZQ81"
S_PASSWORD = "pw-Xk29-moonlight"
S_CLIENT_SECRET = "dest-client-secret-ab41"
S_AUTH_HEADER = "Basic dGVjaC11c2VyOnhrMjk="
S_TOKEN_PASSWORD = "tsp-Lm77-harbour"
S_TOKEN_USER = "token-user-qq12"
S_TOKEN_URL = "https://idp-3344.example.test/oauth/token"
S_QUERY = "sap-client-value-812"
S_CLIENT_ID = "dest-client-id-c0ffee"
SECRETS = (
    S_URL,
    "backend-9911",
    S_USER,
    S_PASSWORD,
    S_CLIENT_SECRET,
    S_AUTH_HEADER,
    S_TOKEN_PASSWORD,
    S_TOKEN_USER,
    S_TOKEN_URL,
    "idp-3344",
    S_QUERY,
    S_CLIENT_ID,
    SERVICE_TOKEN,
    CLIENT_SECRET,
)

ITEM_KEYS = {
    "name",
    "description",
    "type",
    "proxy_type",
    "authentication",
    "level",
    "user_propagating",
    "usable",
    "reason",
    "notes",
    "shadows_subaccount",
}


def dest(name: Any, **overrides: Any) -> dict[str, Any]:
    """A destination configuration as the service lists it, credentials and all."""
    data: dict[str, Any] = {
        "Name": name,
        "Type": "HTTP",
        "URL": S_URL,
        "ProxyType": "Internet",
        "Authentication": "BasicAuthentication",
        "Description": "Purchasing backend",
        "User": S_USER,
        "Password": S_PASSWORD,
        "clientId": S_CLIENT_ID,
        "clientSecret": S_CLIENT_SECRET,
        "tokenServiceURL": S_TOKEN_URL,
        "tokenServiceUser": S_TOKEN_USER,
        "tokenServicePassword": S_TOKEN_PASSWORD,
        "URL.headers.Authorization": S_AUTH_HEADER,
        "URL.queries.sap-client": S_QUERY,
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


class Service:
    """The mock destination service: what each level answers, what was asked."""

    def __init__(self) -> None:
        self.instance: Any = []
        self.subaccount: Any = []
        self.token: Any = {"access_token": SERVICE_TOKEN, "expires_in": 3600}
        self.requests: list[httpx.Request] = []

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)

    async def _answer(self, value: Any, request: httpx.Request) -> httpx.Response:
        if callable(value):
            value = value(request)
            if asyncio.iscoroutine(value):
                value = await value
        if not isinstance(value, httpx.Response):
            value = httpx.Response(200, json=value)
        if not value.is_stream_consumed and not value.is_closed:
            return value
        # As a real transport hands it over: a body that is still to be read
        # (the listing reads the raw stream, which `content=` no longer has).
        return httpx.Response(
            value.status_code, headers=value.headers, stream=httpx.ByteStream(value.content)
        )

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            assert request.method == "POST"
            return await self._answer(self.token, request)
        assert request.url.host == "dest-api.example.test", request.url
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {SERVICE_TOKEN}"
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == INSTANCE:
            return await self._answer(self.instance, request)
        if request.url.path == SUBACCOUNT:
            return await self._answer(self.subaccount, request)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")


@pytest.fixture
def service(monkeypatch) -> Service:
    state = Service()
    config = DestinationServiceConfig(
        client_id="cid", client_secret=CLIENT_SECRET, token_url=TOKEN_URL, api_url=API
    )
    monkeypatch.setattr(destinations, "_config", lambda: config)
    monkeypatch.setattr(destinations, "_transport", lambda: httpx.MockTransport(state.handler))
    return state


@pytest.fixture
async def client():
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def clean(r: httpx.Response, caplog) -> None:
    """No credential-bearing value in the answer, its headers or the log."""
    haystacks = {"body": r.text, "headers": str(r.headers), "log": caplog.text}
    for where, text in haystacks.items():
        for secret in SECRETS:
            assert secret not in text, (where, secret)


def own(caplog) -> str:
    """This module's log lines (httpx logs each request URL itself, at INFO)."""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name == destinations.logger.name
    )


def by_name(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in body["items"]}


# ------------------------------------------------------------- the happy path


async def test_both_levels_are_merged_and_the_instance_wins(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = [
        dest("S4_ODATA", Description="instance copy"),
        dest("alpha_dest", Authentication="OAuth2UserTokenExchange"),
    ]
    service.subaccount = [
        dest("S4_ODATA", Description="subaccount copy", Authentication="NoAuthentication"),
        dest("Zulu", Authentication="PrincipalPropagation", ProxyType="OnPremise"),
        dest("beta"),
    ]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"items", "truncated", "skipped", "warnings"}
    assert body["truncated"] is False and body["skipped"] == 0 and body["warnings"] == []
    # By name, case-insensitive.
    assert [i["name"] for i in body["items"]] == ["alpha_dest", "beta", "S4_ODATA", "Zulu"]
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
    items = by_name(body)
    assert items["S4_ODATA"] == {
        "name": "S4_ODATA",
        "description": "instance copy",
        "type": "HTTP",
        "proxy_type": "Internet",
        "authentication": "BasicAuthentication",
        "level": "instance",
        "user_propagating": False,
        "usable": True,
        "reason": None,
        "notes": [],
        "shadows_subaccount": True,
    }
    assert items["alpha_dest"]["user_propagating"] is True
    assert items["alpha_dest"]["shadows_subaccount"] is False
    assert items["beta"]["level"] == "subaccount"
    assert items["Zulu"]["user_propagating"] is True
    assert r.headers["cache-control"] == "no-store"
    clean(r, caplog)
    # One token, one request per level, nothing else.
    assert service.count("/oauth/token") == 1
    assert service.count(INSTANCE) == 1 and service.count(SUBACCOUNT) == 1
    assert len(service.requests) == 3


async def test_flags_for_what_an_odata_service_cannot_use(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = [
        dest("mail", Type="MAIL"),
        dest("rfc", Type="RFC", ProxyType="OnPremise"),
        dest("notype", Type=None),
        dest("has space"),
        dest("x" * 201),
        dest("onprem", ProxyType="OnPremise", Authentication="PrincipalPropagation"),
        dest("plain"),
    ]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    items = by_name(r.json())
    assert (items["mail"]["usable"], items["mail"]["reason"]) == (False, "not_http")
    assert items["mail"]["type"] == "MAIL"
    assert (items["rfc"]["usable"], items["rfc"]["reason"]) == (False, "not_http")
    assert items["rfc"]["notes"] == ["on_premise"]
    assert (items["has space"]["usable"], items["has space"]["reason"]) == (False, "invalid_name")
    # Judged on the name as stored, not on the cut form that is shown.
    long_name = "x" * 200
    assert (items[long_name]["usable"], items[long_name]["reason"]) == (False, "invalid_name")
    assert (items["notype"]["usable"], items["notype"]["reason"]) == (False, "not_http")
    assert items["notype"]["type"] == ""
    # On-premise stays selectable; the note says requests are not possible yet.
    assert items["onprem"]["usable"] is True and items["onprem"]["reason"] is None
    assert items["onprem"]["notes"] == ["on_premise"]
    assert items["onprem"]["proxy_type"] == "OnPremise"
    assert items["plain"]["usable"] is True and items["plain"]["notes"] == []
    clean(r, caplog)


async def test_every_returned_string_is_cleaned_and_capped(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = [
        dest(
            "odd",
            Description=f"line one\nline two\t{S_URL}?x=1 " + "d" * 1000,
            Type=S_PASSWORD,
            ProxyType=f"Internet {S_PASSWORD}",
            Authentication=S_AUTH_HEADER,
        ),
        dest("typed", Description={"x": S_PASSWORD}, Type=7, ProxyType=None, Authentication=[1]),
        dest("future", Authentication="OAuth2SomethingNew", Type="TCP", ProxyType="PrivateLink"),
        dest(f"evil\n{S_URL}"),
    ]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    items = by_name(r.json())
    odd = items["odd"]
    assert "\n" not in odd["description"] and "\t" not in odd["description"]
    assert odd["description"].startswith("line one line two <url> ddd")
    assert len(odd["description"]) == destinations.MAX_DESCRIPTION_CHARS
    assert (odd["type"], odd["proxy_type"], odd["authentication"]) == ("other", "other", "other")
    assert odd["usable"] is False and odd["reason"] == "not_http"
    typed = items["typed"]
    assert typed["description"] == ""
    assert (typed["type"], typed["proxy_type"], typed["authentication"]) == ("other", "", "other")
    future = items["future"]
    assert (future["type"], future["proxy_type"]) == ("TCP", "PrivateLink")
    assert future["authentication"] == "OAuth2SomethingNew"
    assert future["user_propagating"] is False
    evil = items["evil <url>"]
    assert evil["usable"] is False and evil["reason"] == "invalid_name"
    clean(r, caplog)


async def test_entries_without_a_plain_name_are_skipped_and_counted(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = [
        dest(None),
        dest(42),
        dest({"Name": S_PASSWORD}),
        dest(""),
        dest(" \n "),
        "a string",
        [dest("nested")],
        None,
        dest("kept"),
    ]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["kept"]
    assert body["skipped"] == 8
    clean(r, caplog)


async def test_the_sort_is_case_insensitive_and_stable(client, service):
    service.instance = [dest("b"), dest("A"), dest("a"), dest("C")]
    service.subaccount = [dest("B"), dest("_x"), dest("0x")]
    names = [i["name"] for i in (await client.get(URL)).json()["items"]]
    assert names == ["0x", "_x", "A", "a", "B", "b", "C"]


async def test_nothing_is_kept_between_two_calls(client, service):
    service.instance = [dest("one")]
    assert [i["name"] for i in (await client.get(URL)).json()["items"]] == ["one"]
    service.instance = [dest("two")]
    assert [i["name"] for i in (await client.get(URL)).json()["items"]] == ["two"]
    assert service.count("/oauth/token") == 2  # one per call, none reused


# -------------------------------------------------------- one level, or none


async def test_one_level_failing_returns_the_other_with_a_warning(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = [dest("S4_ODATA")]
    service.subaccount = httpx.Response(
        500, text=f"boom {S_PASSWORD} {SERVICE_TOKEN} {CLIENT_SECRET} {S_URL}"
    )
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["S4_ODATA"]
    assert body["warnings"] == [
        {
            "code": "level_unavailable",
            "level": "subaccount",
            "reason": "http_error",
            "status": 500,
            "message": "the subaccount destinations could not be listed",
        }
    ]
    clean(r, caplog)
    warned = [x for x in caplog.records if x.name == destinations.logger.name]
    assert any(x.levelno == logging.WARNING and "subaccount" in x.getMessage() for x in warned)
    assert service.count(SUBACCOUNT) == 1  # no retry

    service.instance, service.subaccount = httpx.Response(403), [dest("sub_only")]
    body = (await client.get(URL)).json()
    assert [i["name"] for i in body["items"]] == ["sub_only"]
    assert [(w["level"], w["reason"], w["status"]) for w in body["warnings"]] == [
        ("instance", "http_error", 403)
    ]


async def test_both_levels_failing_is_an_error_with_a_code(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = httpx.Response(500, text=f"x {S_PASSWORD}")
    service.subaccount = httpx.Response(404, text=f"y {S_URL}")
    r = await client.get(URL)
    assert r.status_code == 502
    assert r.headers["x-odata-error"] == "list_failed"
    assert r.json() == {"detail": destinations.LIST_FAILED_TEXT}
    clean(r, caplog)
    assert service.count(INSTANCE) == 1 and service.count(SUBACCOUNT) == 1


async def test_no_binding_is_a_fixed_error_and_asks_nothing(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a binding")

    monkeypatch.setattr(destinations, "_transport", lambda: httpx.MockTransport(never))
    r = await client.get(URL)  # the real `_config`: the environment has no binding
    assert r.status_code == 503
    assert r.headers["x-odata-error"] == "no_destination_service"
    assert r.json() == {"detail": destinations.NO_BINDING_TEXT}


@pytest.mark.parametrize(
    "answer",
    [
        {"destinations": [dest("wrapped")]},
        "text",
        7,
        None,
        httpx.Response(200, text=f"<html>{S_PASSWORD}</html>"),
        httpx.Response(200, content=b"[{\"Name\": \"cut\""),
        httpx.Response(200, content=b"[{\"Name\": \"a\"}] trailing"),
        httpx.Response(200, content=b"\xff\xfe\x00"),
        httpx.Response(200, content=b"[" * 100_000),
        httpx.Response(
            200,
            content=gzip.compress(json.dumps([dest("zipped")]).encode()),
            headers={"content-encoding": "gzip"},
        ),
    ],
)
async def test_an_answer_that_is_no_array_fails_that_level_only(client, service, caplog, answer):
    caplog.set_level(logging.DEBUG)
    service.instance = answer
    service.subaccount = [dest("sub")]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["sub"]
    assert [(w["level"], w["reason"]) for w in body["warnings"]] == [("instance", "not_json")]
    clean(r, caplog)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_is_not_followed(client, service, caplog, status):
    caplog.set_level(logging.DEBUG)
    service.instance = httpx.Response(
        status, headers={"location": f"https://elsewhere.example.test/{S_PASSWORD}"}
    )
    service.subaccount = [dest("sub")]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(w["level"], w["reason"]) for w in body["warnings"]] == [("instance", "redirect")]
    assert all(q.url.host != "elsewhere.example.test" for q in service.requests)
    assert len(service.requests) == 3
    assert "elsewhere" not in r.text and "elsewhere" not in caplog.text
    assert "example.test" not in own(caplog)
    clean(r, caplog)


async def test_an_unreachable_level_names_no_host(client, service, caplog):
    caplog.set_level(logging.DEBUG)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"cannot connect to {request.url} with {SERVICE_TOKEN}", request=request
        )

    service.instance = down
    service.subaccount = [dest("sub")]
    r = await client.get(URL)
    body = r.json()
    assert [(w["level"], w["reason"]) for w in body["warnings"]] == [("instance", "unreachable")]
    assert "example.test" not in r.text
    assert "ConnectError" in own(caplog) and "example.test" not in own(caplog)
    clean(r, caplog)


# --------------------------------------------------------------------- bounds


class Chunks(httpx.AsyncByteStream):
    """A body served in chunks that counts how many were asked for."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.served = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.served += 1
            yield chunk


async def test_an_oversized_answer_is_cut_while_reading(client, service, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(destinations, "MAX_LIST_BYTES", 4096)
    one = json.dumps(dest("PLACEHOLDER")).encode()
    parts = [b"["] + [one.replace(b"PLACEHOLDER", b"d%04d" % n) + b"," for n in range(500)]
    stream = Chunks(parts)
    service.instance = httpx.Response(200, stream=stream)
    service.subaccount = [dest("sub")]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is True and body["warnings"] == []
    names = [i["name"] for i in body["items"]]
    # Only whole entries inside the cap, in order; the rest was never read.
    whole, used = 0, 1
    while used + len(parts[whole + 1]) <= 4096:
        used += len(parts[whole + 1])
        whole += 1
    assert 0 < whole < 20
    assert names == [f"d{n:04d}" for n in range(whole)] + ["sub"]
    assert stream.served <= whole + 3
    clean(r, caplog)


async def test_the_number_of_entries_is_capped(client, service, monkeypatch):
    monkeypatch.setattr(destinations, "MAX_ENTRIES", 5)
    service.instance = [dest(f"i{n:02d}") for n in range(9)]
    service.subaccount = [dest(f"s{n:02d}") for n in range(9)]
    body = (await client.get(URL)).json()
    assert body["truncated"] is True
    assert [i["name"] for i in body["items"]] == ["i00", "i01", "i02", "i03", "i04"]

    service.instance = [dest(f"i{n:02d}") for n in range(5)]
    service.subaccount = []
    assert (await client.get(URL)).json()["truncated"] is False


async def test_a_slow_level_is_dropped_at_the_time_budget(client, service, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(destinations, "LIST_BUDGET_SECONDS", 0.2)

    async def slow(request: httpx.Request) -> list[Any]:
        await asyncio.sleep(30)
        return [dest("late")]

    service.instance = [dest("fast")]
    service.subaccount = slow
    started = asyncio.get_running_loop().time()
    r = await client.get(URL)
    assert asyncio.get_running_loop().time() - started < 5
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["fast"]
    assert [(w["level"], w["reason"]) for w in body["warnings"]] == [("subaccount", "timeout")]

    service.instance = slow
    r = await client.get(URL)
    assert r.status_code == 504
    assert r.headers["x-odata-error"] == "timeout"
    assert r.json() == {"detail": destinations.TIMEOUT_TEXT}
    clean(r, caplog)


async def test_a_slow_token_endpoint_is_a_timeout(client, service, monkeypatch):
    monkeypatch.setattr(destinations, "LIST_BUDGET_SECONDS", 0.2)

    async def slow(request: httpx.Request) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {"access_token": SERVICE_TOKEN}

    service.token = slow
    r = await client.get(URL)
    assert r.status_code == 504 and r.headers["x-odata-error"] == "timeout"
    assert service.count(INSTANCE) == 0 and service.count(SUBACCOUNT) == 0


def test_the_budget_is_well_under_the_approuter_timeout():
    assert 0 < destinations.LIST_BUDGET_SECONDS <= 20


# ---------------------------------------------------------------- the token


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, text=f"bad client {CLIENT_SECRET} at {TOKEN_URL}?secret={S_PASSWORD}"),
        httpx.Response(200, text="<html>sign in</html>"),
        httpx.Response(302, headers={"location": "https://elsewhere.example.test/"}),
        {"token_type": "bearer"},
    ],
)
async def test_a_token_failure_is_a_fixed_error_and_lists_nothing(client, service, caplog, answer):
    caplog.set_level(logging.DEBUG)
    service.token = answer
    service.instance = [dest("never")]
    r = await client.get(URL)
    assert r.status_code == 502
    assert r.headers["x-odata-error"] == "token_failed"
    assert r.json() == {"detail": destinations.TOKEN_FAILED_TEXT}
    assert len(service.requests) == 1  # the token request, once
    assert "example.test" not in own(caplog) and "elsewhere" not in caplog.text
    clean(r, caplog)
    warned = [x for x in caplog.records if x.name == destinations.logger.name]
    assert [x.levelno for x in warned] == [logging.WARNING]


async def test_an_unreachable_token_endpoint(client, service, caplog):
    caplog.set_level(logging.DEBUG)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route to {request.url} {CLIENT_SECRET}", request=request)

    service.token = down
    r = await client.get(URL)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "token_failed"
    assert "ConnectError" in own(caplog) and "example.test" not in own(caplog)
    clean(r, caplog)


# ---------------------------------------------------------------- the route


@pytest.mark.parametrize(
    "query",
    ["?level=instance", f"?name={S_PASSWORD}", f"?{S_PASSWORD}=1", "?x=1&x=2", "?limit=5"],
)
async def test_query_parameters_are_refused_without_echo(client, service, caplog, query):
    caplog.set_level(logging.DEBUG)
    service.instance = [dest("never")]
    r = await client.get(URL + query)
    assert r.status_code == 422
    assert r.json() == {"detail": "query: this route takes no parameters"}
    assert service.requests == []
    clean(r, caplog)


def test_the_route_carries_the_admin_dependency_and_is_served():
    from agents.auth import require_admin
    from agents.odata.admin_routes import router

    (route,) = [r for r in router.routes if r.path == "/api/odata/destinations"]
    assert route.methods == {"GET"}
    assert require_admin in [d.call for d in route.dependant.dependencies]
    assert list(app_module.app.openapi()["paths"][URL]) == ["get"]


class _StubValidator:
    xsappname = "app"

    def __init__(self, claims: dict[str, dict[str, Any]]) -> None:
        self._claims = claims

    def validate(self, token: str) -> dict[str, Any]:
        from fastapi import HTTPException

        if token not in self._claims:
            raise HTTPException(status_code=401, detail="Invalid token")
        return self._claims[token]

    def has_scope(self, payload: dict[str, Any], scope: str) -> bool:
        return scope in (payload.get("scope") or [])


async def test_only_an_admin_reaches_the_destination_service(monkeypatch, service):
    from fastapi import FastAPI

    from agents import auth
    from agents.odata.admin_routes import router

    service.instance = [dest("S4_ODATA")]
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
        bare = FastAPI()
        bare.include_router(router, prefix="/admin")
        async with AsyncClient(transport=ASGITransport(app=bare), base_url="http://test") as c:
            r = await c.get(URL)
            assert r.status_code == 401
            for token in ("usr", "dev"):
                r = await c.get(URL, headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 403
                assert r.json() == {"detail": "Admin scope required"}
            assert service.requests == []
            r = await c.get(URL, headers={"Authorization": "Bearer adm"})
            assert r.status_code == 200
            assert [i["name"] for i in r.json()["items"]] == ["S4_ODATA"]
    finally:
        auth.current_claims.reset(marker)


def test_the_module_keeps_no_cache_and_no_raw_configuration():
    """Structure, not behaviour: the module has no module-level container a
    listing could survive in, and never touches the per-destination resolvers."""
    source = Path(destinations.__file__).read_text()
    assert "DestinationResolver" not in source and "resolver_for" not in source
    for name, value in vars(destinations).items():
        if name.startswith("__"):
            continue
        assert not isinstance(value, (dict, list, set)), name
