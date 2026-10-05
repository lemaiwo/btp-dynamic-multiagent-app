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


# ------------------------------------------------------- review round 1 (DL1)


def _child(code: str) -> str:
    """Run ``code`` in a fresh interpreter and return its last output line.

    A separate process with a timeout: a regular expression that goes
    quadratic cannot be interrupted from inside, so a regression fails here
    after 30 seconds instead of hanging the suite.
    """
    import subprocess

    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("field", ["Name", "Description"])
def test_a_huge_property_of_scheme_characters_is_reduced_fast(field):
    """2 MB of characters a URL scheme may consist of, and no ``://``: the
    URL mask must see a cut text, never the whole property."""
    elapsed = _child(
        "import time\n"
        "from agents.odata import destinations as d\n"
        "big = 'a' * (2 * 1024 * 1024)\n"
        "entry = {'Name': 'ok', 'Description': 'fine', 'Type': 'HTTP'}\n"
        f"entry[{field!r}] = big\n"
        "started = time.perf_counter()\n"
        "stored, item = d._reduce(entry, 'instance')\n"
        "elapsed = time.perf_counter() - started\n"
        "assert len(item['name']) <= d.MAX_NAME_CHARS\n"
        "assert len(item['description']) <= d.MAX_DESCRIPTION_CHARS\n"
        f"assert set(item[{field.lower()!r}]) == {{'a'}}\n"
        "print(elapsed)\n"
    )
    assert float(elapsed) < 1.0


def test_plain_cuts_before_it_cleans_and_still_masks():
    text = "one\ntwo " + S_URL + "?q=1 " + "t" * 5000
    out = destinations._plain(text, 40)
    assert out == ("one two <url> " + "t" * 40)[:40]
    # A URL that starts inside the part that is looked at is masked whole,
    # also when the cut falls in the middle of it.
    out = destinations._plain("x" * 30 + " " + S_URL * 50, 300)
    assert out == "x" * 30 + " <url>"
    assert destinations._plain(None, 10) == "" and destinations._plain(7, 10) == ""


async def test_a_protocol_error_text_is_never_logged(client, service, caplog):
    """h11 quotes bytes of the answer in its error texts: a listing's raw
    configuration (or the token) would reach the WARNING line."""
    caplog.set_level(logging.DEBUG)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            f"illegal chunk header: {S_PASSWORD} marker-zz91", request=request
        )

    service.instance = broken
    service.subaccount = [dest("sub")]
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(w["level"], w["reason"]) for w in body["warnings"]] == [("instance", "unreachable")]
    assert "RemoteProtocolError" in own(caplog)
    assert "illegal chunk header" not in caplog.text and "marker-zz91" not in caplog.text
    assert "marker-zz91" not in r.text
    clean(r, caplog)

    service.subaccount = broken
    r = await client.get(URL)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "list_failed"
    assert "marker-zz91" not in caplog.text and "marker-zz91" not in r.text
    clean(r, caplog)


async def test_a_protocol_error_on_the_token_request_is_never_logged(client, service, caplog):
    """The same for the token answer, whose bytes are the service token."""
    caplog.set_level(logging.DEBUG)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            f"Illegal header value b'{SERVICE_TOKEN} marker-qq17'", request=request
        )

    service.token = broken
    r = await client.get(URL)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "token_failed"
    assert "RemoteProtocolError" in own(caplog)
    assert "marker-qq17" not in caplog.text and "Illegal header" not in caplog.text
    clean(r, caplog)


async def test_a_token_error_body_is_not_logged(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.token = httpx.Response(401, text="unauthorized marker-kk05 body")
    r = await client.get(URL)
    assert r.status_code == 502 and r.headers["x-odata-error"] == "token_failed"
    assert "401" in own(caplog) and "marker-kk05" not in caplog.text


@pytest.mark.parametrize(
    "token_url",
    ["https://auth.example.test:notaport/oauth/token", "https://auth.exa mple.test\x00/x"],
)
async def test_a_malformed_token_url_is_a_token_failure(
    client, service, monkeypatch, caplog, token_url
):
    caplog.set_level(logging.DEBUG)
    config = DestinationServiceConfig(
        client_id="cid", client_secret=CLIENT_SECRET, token_url=token_url, api_url=API
    )
    monkeypatch.setattr(destinations, "_config", lambda: config)
    r = await client.get(URL)
    assert r.status_code == 502, r.text
    assert r.headers["x-odata-error"] == "token_failed"
    assert r.json() == {"detail": destinations.TOKEN_FAILED_TEXT}
    assert r.headers["cache-control"] == "no-store"
    assert service.requests == []
    assert "InvalidURL" in own(caplog)
    assert "example.test" not in own(caplog) and "notaport" not in caplog.text
    clean(r, caplog)


async def test_every_error_answer_is_no_store(client, service, monkeypatch):
    service.instance = httpx.Response(500)
    service.subaccount = httpx.Response(500)
    r = await client.get(URL)
    assert (r.status_code, r.headers["x-odata-error"]) == (502, "list_failed")
    assert r.headers["cache-control"] == "no-store"

    service.token = httpx.Response(500)
    r = await client.get(URL)
    assert (r.status_code, r.headers["x-odata-error"]) == (502, "token_failed")
    assert r.headers["cache-control"] == "no-store"

    async def slow(request: httpx.Request) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(destinations, "LIST_BUDGET_SECONDS", 0.2)
    service.token = slow
    r = await client.get(URL)
    assert (r.status_code, r.headers["x-odata-error"]) == (504, "timeout")
    assert r.headers["cache-control"] == "no-store"

    monkeypatch.setattr(destinations, "_config", lambda: None)
    r = await client.get(URL)
    assert (r.status_code, r.headers["x-odata-error"]) == (503, "no_destination_service")
    assert r.headers["cache-control"] == "no-store"


async def test_a_refused_query_parameter_is_no_store(client, service):
    r = await client.get(URL + "?level=instance")
    assert r.status_code == 422
    assert r.headers["cache-control"] == "no-store"


async def test_an_upstream_401_on_one_level_and_on_both(client, service, caplog):
    caplog.set_level(logging.DEBUG)
    service.instance = [dest("inst")]
    service.subaccount = httpx.Response(
        401, text=f"unauthorized {SERVICE_TOKEN}", headers={"www-authenticate": "Bearer"}
    )
    r = await client.get(URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == ["inst"]
    assert [(w["level"], w["reason"], w["status"]) for w in body["warnings"]] == [
        ("subaccount", "http_error", 401)
    ]
    assert service.count("/oauth/token") == 1 and service.count(SUBACCOUNT) == 1  # no retry
    clean(r, caplog)

    service.instance = httpx.Response(401, text=f"unauthorized {SERVICE_TOKEN}")
    r = await client.get(URL)
    assert (r.status_code, r.headers["x-odata-error"]) == (502, "list_failed")
    assert r.json() == {"detail": destinations.LIST_FAILED_TEXT}
    assert r.headers["cache-control"] == "no-store"
    assert service.count("/oauth/token") == 2 and service.count(INSTANCE) == 2
    clean(r, caplog)


async def test_the_time_the_token_took_is_taken_off_the_levels(client, service, monkeypatch):
    """One budget for the whole call: a level that would fit into a fresh
    budget, but not into what the token request left, times out."""
    monkeypatch.setattr(destinations, "LIST_BUDGET_SECONDS", 1.0)

    async def slow_token(request: httpx.Request) -> dict[str, Any]:
        await asyncio.sleep(0.6)
        return {"access_token": SERVICE_TOKEN}

    async def slow_level(request: httpx.Request) -> list[Any]:
        await asyncio.sleep(0.7)
        return [dest("late")]

    service.token = slow_token
    service.instance = slow_level
    service.subaccount = slow_level
    started = asyncio.get_running_loop().time()
    r = await client.get(URL)
    elapsed = asyncio.get_running_loop().time() - started
    assert (r.status_code, r.headers["x-odata-error"]) == (504, "timeout")
    assert elapsed < 1.25, elapsed


@pytest.mark.parametrize(
    "raw",
    [b'[{"Name": "a"}, {"Name": ]', b'[{"Name": "a"} x]', b"\xff\xfe[]", b"{}"],
)
def test_a_listing_that_is_not_json_keeps_no_parser_error(raw):
    """A ``JSONDecodeError`` carries the whole listing as ``.doc`` and a
    ``UnicodeDecodeError`` the raw bytes as ``.object``: neither may hang off
    the failure that is kept."""
    with pytest.raises(destinations._LevelFailed) as caught:
        destinations._reduce_listing(raw, False, "instance")
    assert caught.value.reason == "not_json"
    assert caught.value.__context__ is None and caught.value.__cause__ is None


async def test_a_failed_level_keeps_no_traceback_of_the_listing():
    """What ``_Level.failed`` holds is a fresh object: no traceback (whose
    frames hold the raw listing) and no chained error."""
    config = DestinationServiceConfig(
        client_id="cid", client_secret=CLIENT_SECRET, token_url=TOKEN_URL, api_url=API
    )
    listing = json.dumps([dest("a")]).encode() + b" trailing"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(listing))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await destinations._list_level(
            http, config, SERVICE_TOKEN, "instance", INSTANCE, asyncio.get_running_loop().time() + 5
        )
    failed = result.failed
    assert failed is not None and failed.reason == "not_json"
    assert failed.__traceback__ is None
    assert failed.__context__ is None and failed.__cause__ is None


def test_the_docstrings_do_not_overstate_the_description():
    doc = destinations.__doc__ or ""
    assert "secrets masked" not in Path(destinations.__file__).read_text().lower()
    assert "free text" in doc
    assert "five properties" in doc and "six" not in doc.lower()
