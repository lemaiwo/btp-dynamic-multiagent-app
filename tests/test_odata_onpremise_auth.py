"""OnPremise destinations through the connectivity proxy, for both identities.

Covered (``agents/destination_auth.py``): a technical-user request goes to the
proxy transport with the application's connectivity token and the
destination's own credential; a signed-in user's request carries that user's
identity (``exchange`` or ``header`` mode) and never a credential stored in
the destination; two users on one client never share a proxy token, also when
the principal is the same and the JWT is not; no user means a refusal before
any token or proxy call; ``http://`` is allowed only for an OnPremise
destination that really goes through the proxy; the proxy token and the user
headers never leave on the direct path; ``URL.queries.*`` are added to every
request and a caller's own parameter wins; a 407 drops only the token that
was used and retries once.

No network: the destination service is a fake resolver, the connectivity
tokens a fake, SAP and the proxy are two recording ``MockTransport``s.

Run:  python -m pytest tests/test_odata_onpremise_auth.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

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

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents import destination_auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.destination import ConnectivityConfig, Destination, DestinationError  # noqa: E402
from agents.destination_auth import (  # noqa: E402
    DestinationAuth,
    DestinationUserRequired,
    destination_http_client,
)

VIRTUAL = "http://s4.internal:44300"
PATH = "/sap/opu/odata/sap/SRV/Set"
BASIC = "Basic dXNlcjpwdw=="
PROXY_AUTH = "Proxy-Authorization"
PP_HEADER = "SAP-Connectivity-Authentication"
LOCATION = "SAP-Connectivity-SCC-Location_ID"


class Resolver:
    """The destination service: one OnPremise destination by default."""

    def __init__(self, **overrides: Any) -> None:
        self.name = overrides.pop("name", "S4_ODATA_TECH")
        self.fields: dict[str, Any] = {
            "url": VIRTUAL,
            "headers": {"Authorization": BASIC},
            "auth_type": "BasicAuthentication",
            "proxy_type": "OnPremise",
            "location_id": "LOC1",
            "queries": {"sap-client": "100"},
        }
        self.fields.update(overrides)
        self.calls: list[tuple[str | None, str | None, bool]] = []
        self.invalidated: list[str | None] = []

    async def resolve(self, *, force: bool = False, user_token=None, principal=None) -> Destination:
        self.calls.append((user_token, principal, force))
        return Destination(
            expires_at=time.monotonic() + 60,
            per_user=bool(user_token),
            **{**self.fields, "headers": dict(self.fields["headers"])},
        )

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidated.append(principal)


def pp_resolver(**overrides: Any) -> Resolver:
    """A PrincipalPropagation destination. It carries a stored header on
    purpose: a user run must not send it."""
    fields: dict[str, Any] = {
        "name": "S4_ODATA_USER",
        "auth_type": "PrincipalPropagation",
        "headers": {"Authorization": BASIC, "X-Stored": "stored-value"},
    }
    fields.update(overrides)
    return Resolver(**fields)


class FakeTokens:
    """``ConnectivityTokens`` without a token endpoint, with its cache rule:
    a token stays the same until ``invalidate`` drops it."""

    config = ConnectivityConfig(
        client_id="cid",
        client_secret="s3cret",
        token_url="https://uaa.example.com/oauth/token",
        proxy_host="proxy.internal",
        proxy_port=20003,
    )

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.invalidated: list[str | None] = []
        self.generation = 0

    def _suffix(self) -> str:
        return f"#{self.generation}" if self.generation else ""

    async def app_token(self, *, force: bool = False) -> str:
        self.calls.append(("app", force))
        return "APP" + self._suffix()

    async def user_token(self, user_jwt: str, principal: str | None, *, force: bool = False) -> str:
        self.calls.append(("user", user_jwt, principal, force))
        # Bound to the JWT as the real cache is (principal AND token digest).
        return f"UX-{principal}-{user_jwt}" + self._suffix()

    @staticmethod
    def user_key(principal: str | None, user_jwt: str) -> str:
        return principal or f"token:{user_jwt}"

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidated.append(principal)
        self.generation += 1


class Wire:
    """One side of the network: records requests, answers from a queue."""

    def __init__(self, *statuses: int) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses)
        self.transport = httpx.MockTransport(self._handle)
        self.closed = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        # A copy: a retry re-shapes the very same request object.
        self.requests.append(
            httpx.Request(request.method, request.url, headers=request.headers)
        )
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(status, json={"ok": True})


class World:
    def __init__(self, resolver: Resolver | None = None, *, proxy: Wire | None = None) -> None:
        self.resolver = resolver or Resolver()
        self.tokens = FakeTokens()
        self.direct = Wire()
        self.proxy = proxy or Wire()

    def client(self, *, connectivity: Any = "default", **kw: Any) -> httpx.AsyncClient:
        return destination_http_client(
            self.resolver,
            server_key="builtin:odata/svc",
            transport=self.direct.transport,
            connectivity=self.tokens if connectivity == "default" else connectivity,
            proxy_transport=self.proxy.transport,
            **kw,
        )


class as_user:
    """Bind a JWT and a principal the way the request middleware does."""

    def __init__(self, jwt: str, principal: str) -> None:
        self.jwt, self.principal = jwt, principal

    def __enter__(self) -> None:
        self._j = current_jwt.set(self.jwt)
        self._p = current_principal.set(self.principal)

    def __exit__(self, *exc: object) -> None:
        current_principal.reset(self._p)
        current_jwt.reset(self._j)


ALICE = ("jwt-a", "alice@example.com")
BOB = ("jwt-b", "bob@example.com")


# ------------------------------------------------------------ technical user


async def test_technical_user_goes_through_the_proxy_with_the_app_token():
    w = World()
    async with w.client(user_context=False) as http:
        response = await http.get(PATH, params={"$top": "1"})
    assert response.status_code == 200
    assert not w.direct.requests
    (r,) = w.proxy.requests
    assert str(r.url) == f"{VIRTUAL}{PATH}?%24top=1&sap-client=100"
    assert r.headers["Host"] == "s4.internal:44300"
    assert r.headers[PROXY_AUTH] == "Bearer APP"
    assert r.headers["Authorization"] == BASIC
    assert r.headers[LOCATION] == "LOC1"
    assert PP_HEADER not in r.headers
    assert w.tokens.calls == [("app", False)]


async def test_no_location_header_without_a_location_id():
    w = World(Resolver(location_id=""))
    async with w.client() as http:
        await http.get(PATH)
    assert LOCATION not in w.proxy.requests[0].headers


async def test_technical_run_with_a_bound_user_still_uses_the_app_token():
    """``user_context`` off is the technical user, whoever is signed in."""
    w = World()
    with as_user(*ALICE):
        async with w.client(user_context=False) as http:
            await http.get(PATH)
    (r,) = w.proxy.requests
    assert r.headers[PROXY_AUTH] == "Bearer APP" and PP_HEADER not in r.headers
    assert w.resolver.calls == [(None, None, False)]


async def test_technical_run_on_a_principal_propagation_destination_is_refused():
    """The real resolver refuses this already; the auth does not rely on it."""
    w = World(pp_resolver())
    async with w.client(user_context=False) as http:
        with pytest.raises(DestinationError, match="signed-in user"):
            await http.get(PATH)
    assert w.tokens.calls == [] and not w.proxy.requests and not w.direct.requests


# ------------------------------------------------------------ signed-in user


async def test_signed_in_user_exchange_mode():
    w = World(pp_resolver())
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="exchange") as http:
            await http.get(PATH)
    assert not w.direct.requests
    (r,) = w.proxy.requests
    assert r.headers[PROXY_AUTH] == "Bearer UX-alice@example.com-jwt-a"
    assert PP_HEADER not in r.headers
    assert r.headers[LOCATION] == "LOC1"
    # Nothing stored in the destination travels next to the user's identity.
    assert "Authorization" not in r.headers and "X-Stored" not in r.headers
    assert w.tokens.calls == [("user", "jwt-a", "alice@example.com", False)]
    assert w.resolver.calls == [("jwt-a", "alice@example.com", False)]
    assert str(r.url) == f"{VIRTUAL}{PATH}?sap-client=100"


async def test_signed_in_user_header_mode():
    w = World(pp_resolver())
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="header") as http:
            await http.get(PATH)
    (r,) = w.proxy.requests
    assert r.headers[PROXY_AUTH] == "Bearer APP"
    assert r.headers[PP_HEADER] == "Bearer jwt-a"
    assert "Authorization" not in r.headers and "X-Stored" not in r.headers
    assert w.tokens.calls == [("app", False)]


async def test_exchange_is_the_default_mode_and_the_environment_can_switch_it(monkeypatch):
    assert destination_auth.DEFAULT_PP_MODE == "exchange"
    assert destination_auth.pp_mode_from_environment({}) == "exchange"
    switched = destination_auth.pp_mode_from_environment({"CONNECTIVITY_PP_MODE": " Header "})
    assert switched == "header"
    w = World(pp_resolver())
    # Read when a user run needs it, not when the client is built.
    http = w.client(user_context=True)
    monkeypatch.setenv("CONNECTIVITY_PP_MODE", "header")
    with as_user(*ALICE):
        async with http:
            await http.get(PATH)
    assert w.proxy.requests[0].headers[PP_HEADER] == "Bearer jwt-a"


def test_an_unknown_mode_argument_is_refused():
    with pytest.raises(ValueError, match="pp_mode"):
        destination_http_client(Resolver(), connectivity=FakeTokens(), pp_mode="basic")


async def test_an_unknown_mode_in_the_environment_fails_closed(monkeypatch, caplog):
    """No fall-back: a typo must not pick a mechanism nobody chose."""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DestinationError) as info:
        destination_auth.pp_mode_from_environment({"CONNECTIVITY_PP_MODE": "s3cret-typo"})
    assert str(info.value) == "CONNECTIVITY_PP_MODE must be exchange or header"

    monkeypatch.setenv("CONNECTIVITY_PP_MODE", "s3cret-typo")
    w = World(pp_resolver())
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(DestinationError, match="CONNECTIVITY_PP_MODE must be") as info:
                await http.get(PATH)
    assert "s3cret-typo" not in str(info.value) and "s3cret-typo" not in caplog.text
    # Refused before any token call, and nothing left.
    assert w.tokens.calls == [] and not w.proxy.requests and not w.direct.requests


async def test_the_mode_is_never_read_on_a_technical_or_an_internet_path(monkeypatch, caplog):
    """Gmail, Jira, MCP ... and a technical OnPremise run do not care what
    the variable holds, and say nothing about it."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CONNECTIVITY_PP_MODE", "s3cret-typo")
    w = World()
    async with w.client() as http:
        assert (await http.get(PATH)).status_code == 200
    w = World(Resolver(url="https://api.example.com", proxy_type="Internet"))
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            assert (await http.get(PATH)).status_code == 200
        async with destination_http_client(
            w.resolver, user_context=True, transport=w.direct.transport
        ) as http:
            assert (await http.get(PATH)).status_code == 200
    assert "CONNECTIVITY_PP_MODE" not in caplog.text


@pytest.mark.parametrize("mode", ["exchange", "header"])
async def test_two_users_never_share_a_proxy_token(mode):
    w = World(pp_resolver())
    async with w.client(user_context=True, pp_mode=mode) as http:
        for jwt, principal in (ALICE, BOB, ALICE, BOB):
            with as_user(jwt, principal):
                await http.get(PATH)
    seen = [(r.headers[PROXY_AUTH], r.headers.get(PP_HEADER)) for r in w.proxy.requests]
    if mode == "exchange":
        a = ("Bearer UX-alice@example.com-jwt-a", None)
        b = ("Bearer UX-bob@example.com-jwt-b", None)
    else:
        a, b = ("Bearer APP", "Bearer jwt-a"), ("Bearer APP", "Bearer jwt-b")
    assert seen == [a, b, a, b]
    assert not w.direct.requests


async def test_a_run_as_principal_with_the_triggers_jwt_gets_the_token_of_that_jwt():
    """ "Run now": the trigger's JWT with the run-as user's principal. The
    token asked for is the one of the JWT presented, never one filed under
    the principal alone."""
    w = World(pp_resolver())
    async with w.client(user_context=True, pp_mode="exchange") as http:
        with as_user("jwt-a", "bob@example.com"):
            await http.get(PATH)
        with as_user(*BOB):
            await http.get(PATH)
    assert [r.headers[PROXY_AUTH] for r in w.proxy.requests] == [
        "Bearer UX-bob@example.com-jwt-a",
        "Bearer UX-bob@example.com-jwt-b",
    ]
    assert [c[:3] for c in w.tokens.calls] == [
        ("user", "jwt-a", "bob@example.com"),
        ("user", "jwt-b", "bob@example.com"),
    ]


@pytest.mark.parametrize("mode", ["exchange", "header"])
async def test_no_user_is_refused_before_any_token_call(mode):
    w = World(pp_resolver())
    async with w.client(user_context=True, pp_mode=mode) as http:
        with pytest.raises(DestinationUserRequired):
            await http.get(PATH)
    assert w.tokens.calls == [] and w.resolver.calls == []
    assert w.proxy.requests == [] and w.direct.requests == []


@pytest.mark.parametrize(
    "auth_type", ["BasicAuthentication", "NoAuthentication", "OAuth2SAMLBearerAssertion", ""]
)
async def test_a_user_run_on_a_destination_that_is_not_principal_propagation_is_refused(auth_type):
    """A stored credential next to a user token would let SAP answer as the
    technical user and pass for principal propagation."""
    w = World(Resolver(auth_type=auth_type))
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(DestinationError, match="PrincipalPropagation"):
                await http.get(PATH)
    assert w.tokens.calls == [] and not w.proxy.requests and not w.direct.requests


# ------------------------------------------------------------- scheme, route


async def test_http_is_allowed_only_for_onpremise():
    w = World(Resolver(proxy_type="Internet"))
    async with w.client() as http:
        with pytest.raises(DestinationError, match="must use https://"):
            await http.get(PATH)
    assert not w.proxy.requests and not w.direct.requests and w.tokens.calls == []

    w = World(Resolver(proxy_type=""))
    async with w.client() as http:
        with pytest.raises(DestinationError, match="must use https://"):
            await http.get(PATH)

    w = World()
    async with w.client() as http:
        assert (await http.get(PATH)).status_code == 200
    assert len(w.proxy.requests) == 1


async def test_onpremise_https_is_refused():
    """An https target would be a CONNECT tunnel: the request's own
    ``Proxy-Authorization`` would travel inside it, to the target."""
    w = World(Resolver(url="https://s4.internal:44300"))
    async with w.client() as http:
        with pytest.raises(DestinationError, match="http://"):
            await http.get(PATH)
    assert not w.proxy.requests and not w.direct.requests and w.tokens.calls == []


async def test_onpremise_without_binding_names_the_binding():
    """The OData path (``connectivity`` passed, and it is None)."""
    w = World()
    async with w.client(connectivity=None) as http:
        with pytest.raises(destination_auth.OnPremiseRefused) as info:
            await http.get(PATH)
    assert "no connectivity service binding" in str(info.value)
    assert "S4_ODATA_TECH" in str(info.value)
    assert info.value.admin_text == (
        "destination 'S4_ODATA_TECH' is an OnPremise destination, but this app has no "
        "connectivity service binding"
    )
    assert not w.proxy.requests and not w.direct.requests


async def test_a_built_in_that_never_passes_connectivity_says_what_it_cannot_do():
    """Gmail, Jira, MCP ...: a binding would not help them."""
    w = World()
    http = destination_http_client(
        w.resolver, server_key="builtin:jira", transport=w.direct.transport
    )
    async with http:
        with pytest.raises(DestinationError) as info:
            await http.get(PATH)
    text = str(info.value)
    assert "builtin:jira cannot reach OnPremise destinations" in text
    assert "only OData services go through the connectivity proxy" in text
    assert "binding" not in text
    assert not w.direct.requests


async def test_the_three_refusals_carry_a_fixed_text_for_the_admin():
    w = World(Resolver(url="https://s4.internal:44300"))
    async with w.client() as http:
        with pytest.raises(destination_auth.OnPremiseRefused) as info:
            await http.get(PATH)
    assert info.value.admin_text == (
        "OnPremise destination 'S4_ODATA_TECH' must use an http:// address "
        "(virtual host and port): the Cloud Connector tunnel is what encrypts it"
    )
    w = World(Resolver())
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(destination_auth.OnPremiseRefused) as info:
                await http.get(PATH)
    assert info.value.admin_text == (
        "OnPremise destination 'S4_ODATA_TECH' cannot act as the signed-in user: its "
        "Authentication must be PrincipalPropagation"
    )


async def test_without_connectivity_the_client_is_the_one_of_today():
    """No router, no new transport: an Internet destination user that never
    passes ``connectivity`` gets what it got before."""
    w = World(Resolver(url="https://api.example.com", proxy_type="Internet", queries={}))
    http = destination_http_client(w.resolver, transport=w.direct.transport)
    assert http._transport is w.direct.transport
    async with http:
        await http.get("/v1/things")
    (r,) = w.direct.requests
    assert str(r.url) == "https://api.example.com/v1/things"
    assert r.headers["Authorization"] == BASIC


async def test_internet_destination_never_gets_proxy_headers():
    w = World(
        Resolver(
            url="https://api.example.com/base",
            proxy_type="Internet",
            location_id="LOC1",
            headers={
                "Authorization": BASIC,
                # A destination cannot choose the identity the proxy sees.
                "Proxy-Authorization": "Bearer from-destination",
                "SAP-Connectivity-Authentication": "Bearer from-destination",
            },
        )
    )
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.get(PATH, headers={PROXY_AUTH: "Bearer from-caller"})
    assert not w.proxy.requests and w.tokens.calls == []
    (r,) = w.direct.requests
    assert PROXY_AUTH not in r.headers and PP_HEADER not in r.headers and LOCATION not in r.headers
    assert r.headers["Authorization"] == BASIC
    # Queries still appended.
    assert str(r.url) == f"https://api.example.com/base{PATH}?sap-client=100"


async def test_onpremise_destination_headers_cannot_set_the_proxy_identity():
    w = World(
        Resolver(
            headers={
                "Authorization": BASIC,
                "proxy-authorization": "Bearer from-destination",
                "SAP-Connectivity-Authentication": "Bearer someone-else",
                "SAP-Connectivity-SCC-Location_ID": "OTHER",
            }
        )
    )
    async with w.client() as http:
        await http.get(PATH)
    (r,) = w.proxy.requests
    assert r.headers.get_list(PROXY_AUTH) == ["Bearer APP"]
    assert PP_HEADER not in r.headers
    assert r.headers.get_list(LOCATION) == ["LOC1"]


async def test_an_absolute_url_to_another_host_is_refused_for_onpremise():
    w = World()
    async with w.client() as http:
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get("http://other.internal:44300/sap/x")
    assert not w.proxy.requests and not w.direct.requests


async def test_router_refuses_a_proxy_token_on_the_direct_path():
    direct, proxy = Wire(), Wire()
    router = destination_auth.OnPremiseRouter(direct=direct.transport, proxied=proxy.transport)
    # Hand-built: not shaped by DestinationAuth, so not marked for the proxy.
    for url in ("https://evil.example.com/x", "http://s4.internal:44300/x"):
        request = httpx.Request("GET", url, headers={PROXY_AUTH: "Bearer APP"})
        with pytest.raises(DestinationError, match="refusing") as info:
            await router.handle_async_request(request)
        assert "APP" not in str(info.value)
    request = httpx.Request(
        "GET", "https://evil.example.com/x", headers={PP_HEADER: "Bearer jwt-a"}
    )
    with pytest.raises(DestinationError, match="refusing"):
        await router.handle_async_request(request)
    # Plain http never leaves on the direct path either.
    with pytest.raises(DestinationError, match="refusing"):
        await router.handle_async_request(httpx.Request("GET", "http://s4.internal:44300/x"))
    assert not direct.requests and not proxy.requests

    # The mark alone is not enough: the proxy path is http with a proxy token.
    marked = httpx.Request(
        "GET", "https://s4.internal/x", headers={PROXY_AUTH: "Bearer APP"}
    )
    marked.extensions[destination_auth.PROXY_ROUTE_EXTENSION] = True
    with pytest.raises(DestinationError, match="refusing"):
        await router.handle_async_request(marked)
    bare = httpx.Request("GET", "http://s4.internal:44300/x")
    bare.extensions[destination_auth.PROXY_ROUTE_EXTENSION] = True
    with pytest.raises(DestinationError, match="refusing"):
        await router.handle_async_request(bare)
    assert not direct.requests and not proxy.requests

    ok = await router.handle_async_request(httpx.Request("GET", "https://api.example.com/x"))
    assert ok.status_code == 200 and len(direct.requests) == 1 and not proxy.requests


async def test_router_closes_both_transports():
    closed: list[str] = []

    class Closing(httpx.AsyncBaseTransport):
        def __init__(self, name: str) -> None:
            self.name = name

        async def handle_async_request(self, request):  # pragma: no cover - unused
            raise AssertionError

        async def aclose(self) -> None:
            closed.append(self.name)

    await destination_auth.OnPremiseRouter(direct=Closing("d"), proxied=Closing("p")).aclose()
    assert sorted(closed) == ["d", "p"]


def test_the_real_proxy_transport_is_built_from_the_binding_only():
    """Without ``proxy_transport`` the proxied side is an httpx transport
    pointed at the binding's proxy; nothing is sent to build it."""
    http = destination_http_client(Resolver(), connectivity=FakeTokens())
    router = http._transport
    assert isinstance(router, destination_auth.OnPremiseRouter)
    assert isinstance(router._proxied, httpx.AsyncHTTPTransport)
    assert isinstance(router._direct, httpx.AsyncHTTPTransport)
    assert destination_auth.proxy_url_of(FakeTokens.config) == "http://proxy.internal:20003"


def test_environment_proxies_do_not_apply_to_a_client_with_connectivity(monkeypatch):
    """Accepted rule: the router is the client's transport, so httpx mounts
    nothing from ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY`` -- nothing in the
    environment can change where a request with a proxy token goes."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "s4.internal")
    http = destination_http_client(Resolver(), connectivity=FakeTokens())
    assert http._mounts == {}
    # The client every other destination user gets is untouched by this.
    assert destination_http_client(Resolver())._mounts != {}


def test_only_the_factory_builds_a_connectivity_aware_auth():
    """An auth that knows the connectivity tokens on a client without the
    router would send a proxy token directly; so the two are built together,
    in one place."""
    tokens = FakeTokens()
    with pytest.raises(TypeError):
        DestinationAuth(Resolver(), connectivity=tokens)
    with pytest.raises(TypeError):
        DestinationAuth(Resolver(), _route=tokens)
    auth, transport = destination_auth.routed_auth(Resolver(), tokens)
    assert isinstance(transport, destination_auth.OnPremiseRouter)
    assert auth._connectivity is tokens
    # Without a binding: the same auth class, the caller's own transport.
    auth, transport = destination_auth.routed_auth(Resolver(), None, direct=None)
    assert transport is None and auth._connectivity is None

    import re as _re

    pattern = _re.compile(r"\b_Route\(|\bOnPremiseRouter\(|_route=")
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted((ROOT / "agents").rglob("*.py"))
        if path.name != "destination_auth.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


# ------------------------------------------------------------------- queries


async def test_a_caller_query_parameter_wins_over_url_queries():
    w = World(Resolver(queries={"sap-client": "100", "sap-language": "EN"}))
    async with w.client() as http:
        await http.get(PATH, params={"SAP-Client": "200", "$filter": "Name eq 'a b'"})
    (r,) = w.proxy.requests
    assert r.url.params.get_list("SAP-Client") == ["200"]
    assert "sap-client" not in r.url.params
    assert r.url.params["sap-language"] == "EN"
    assert r.url.params["$filter"] == "Name eq 'a b'"


async def test_the_callers_query_is_sent_as_written_and_values_are_encoded():
    w = World(Resolver(queries={"x": "a&b=c d"}))
    async with w.client() as http:
        await http.get(f"{PATH}?$top=1&$filter=Name%20eq%20%27a%27")
    (r,) = w.proxy.requests
    assert r.url.query == b"$top=1&$filter=Name%20eq%20%27a%27&x=a%26b%3Dc%20d"


# ------------------------------------------------------------------- retries


async def test_407_invalidates_that_principals_token_and_retries_once():
    w = World(pp_resolver(), proxy=Wire(407, 200))
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="exchange") as http:
            response = await http.get(PATH)
    assert response.status_code == 200
    assert w.tokens.invalidated == ["alice@example.com"]
    assert all(c[0] == "user" and c[-1] is False for c in w.tokens.calls)
    first, second = w.proxy.requests
    assert first.headers[PROXY_AUTH] == "Bearer UX-alice@example.com-jwt-a"
    assert second.headers.get_list(PROXY_AUTH) == ["Bearer UX-alice@example.com-jwt-a#1"]
    assert str(second.url) == f"{VIRTUAL}{PATH}?sap-client=100"
    # The destination itself was not the problem.
    assert w.resolver.invalidated == []


async def test_407_for_the_technical_user_drops_the_app_token_only():
    w = World(proxy=Wire(407, 407))
    async with w.client() as http:
        response = await http.get(PATH)
    # A second 407 is a real refusal, not a loop.
    assert response.status_code == 407 and len(w.proxy.requests) == 2
    assert w.tokens.invalidated == [None]
    assert [r.headers[PROXY_AUTH] for r in w.proxy.requests] == ["Bearer APP", "Bearer APP#1"]


async def test_a_407_does_not_drop_a_token_somebody_else_already_renewed():
    """Overlapping requests refused with the same old token: the shared app
    token is fetched again once, not once per request."""
    w = World(proxy=Wire(407, 200))
    async with w.client() as http:
        request = http.build_request("GET", PATH)
        flow = http.auth.async_auth_flow(request)
        await flow.__anext__()  # shaped with the old token
        w.tokens.invalidate()  # another request renewed it meanwhile
        w.tokens.invalidated.clear()
        await flow.asend(httpx.Response(407, request=request))
    assert w.tokens.invalidated == []
    assert request.headers[PROXY_AUTH] == "Bearer APP#1"


async def test_407_in_header_mode_drops_the_app_token():
    w = World(pp_resolver(), proxy=Wire(407, 200))
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="header") as http:
            await http.get(PATH)
    assert w.tokens.invalidated == [None]
    assert w.proxy.requests[1].headers[PP_HEADER] == "Bearer jwt-a"
    assert w.proxy.requests[1].headers[PROXY_AUTH] == "Bearer APP#1"


async def test_existing_401_retry_still_works_for_onpremise():
    w = World(proxy=Wire(401, 200))
    async with w.client() as http:
        response = await http.get(PATH)
    assert response.status_code == 200 and len(w.proxy.requests) == 2
    assert w.resolver.invalidated == [None]
    assert [c[2] for c in w.resolver.calls] == [False, True]
    second = w.proxy.requests[1]
    assert second.headers[PROXY_AUTH] == "Bearer APP" and second.headers["Authorization"] == BASIC
    assert w.tokens.invalidated == []


async def test_no_retry_at_all_for_a_one_shot_auth():
    """``retry_on_401=False`` (a resolver and tokens made for one request)
    turns the 407 retry off as well."""
    w = World(proxy=Wire(407, 200))
    auth, router = destination_auth.routed_auth(
        w.resolver,
        w.tokens,
        direct=w.direct.transport,
        proxied=w.proxy.transport,
        retry_on_401=False,
    )
    async with httpx.AsyncClient(
        base_url=destination_auth.PLACEHOLDER_BASE, auth=auth, transport=router
    ) as http:
        response = await http.get(PATH)
    assert response.status_code == 407 and len(w.proxy.requests) == 1
    assert w.tokens.invalidated == []


# -------------------------------------------------------------------- secrets


async def test_a_location_id_that_is_not_a_plain_token_is_refused_without_quoting_it():
    w = World(Resolver(location_id="LOC1\r\nProxy-Authorization: Bearer x"))
    async with w.client() as http:
        with pytest.raises(DestinationError) as info:
            await http.get(PATH)
    assert "Bearer x" not in str(info.value)
    assert not w.proxy.requests


async def test_a_proxy_side_protocol_error_carries_no_header_bytes():
    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            # What h11 says about a header it refuses: it quotes the bytes.
            raise httpx.LocalProtocolError(
                f"Illegal header value {request.headers[PROXY_AUTH]!r}", request=request
            )

    w = World()
    http = destination_http_client(
        w.resolver, connectivity=w.tokens, transport=w.direct.transport, proxy_transport=Broken()
    )
    async with http:
        with pytest.raises(DestinationError) as info:
            await http.get(PATH)
    assert "APP" not in str(info.value) and "LocalProtocolError" in str(info.value)
    # Not chained either: the original error holds the header.
    assert info.value.__cause__ is None and info.value.__context__ is None


async def test_nothing_secret_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    w = World(pp_resolver(), proxy=Wire(407, 200))
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="header") as http:
            await http.get(PATH)
    text = caplog.text
    for secret in ("jwt-a", "APP", BASIC, "s3cret", "stored-value"):
        assert secret not in text


# ------------------------------------------------- fix round 1: more rules


async def test_a_callers_authorization_does_not_travel_on_a_user_run():
    w = World(pp_resolver())
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="exchange") as http:
            await http.get(PATH, headers={"Authorization": "Basic Y2FsbGVyOng="})
    (r,) = w.proxy.requests
    assert "Authorization" not in r.headers
    assert r.headers[PROXY_AUTH] == "Bearer UX-alice@example.com-jwt-a"


async def test_caller_set_identity_headers_are_dropped_on_the_proxy_path():
    w = World()
    async with w.client() as http:
        await http.get(
            PATH,
            headers={
                PP_HEADER: "Bearer someone-else",
                LOCATION: "OTHER",
                PROXY_AUTH: "Bearer from-caller",
                "SAP-Connectivity-Technical-Authentication": "Basic eDp5",
            },
        )
    (r,) = w.proxy.requests
    assert PP_HEADER not in r.headers
    assert "SAP-Connectivity-Technical-Authentication" not in r.headers
    assert r.headers.get_list(LOCATION) == ["LOC1"]
    assert r.headers.get_list(PROXY_AUTH) == ["Bearer APP"]

    w = World(Resolver(location_id=""))
    async with w.client() as http:
        await http.get(PATH, headers={LOCATION: "OTHER"})
    assert LOCATION not in w.proxy.requests[0].headers


@pytest.mark.parametrize("proxy_type", ["OnPremise", "Internet"])
async def test_a_destination_cannot_set_host_or_the_technical_authentication(proxy_type):
    """Accepted rule: reserved headers of a destination are dropped for
    every destination, not only OnPremise ones."""
    url = VIRTUAL if proxy_type == "OnPremise" else "https://api.example.com"
    w = World(
        Resolver(
            url=url,
            proxy_type=proxy_type,
            headers={
                "Authorization": BASIC,
                "Host": "evil.example.com",
                "SAP-Connectivity-Technical-Authentication": "Basic eDp5",
                "X-Custom": "kept",
            },
        )
    )
    async with w.client() as http:
        await http.get(PATH)
    (r,) = (w.proxy if proxy_type == "OnPremise" else w.direct).requests
    assert r.headers["Host"] == httpx.URL(url).netloc.decode()
    assert "SAP-Connectivity-Technical-Authentication" not in r.headers
    assert r.headers["X-Custom"] == "kept" and r.headers["Authorization"] == BASIC


@pytest.mark.parametrize(
    "link",
    [
        "http://s4.internal:44301/sap/x",  # the virtual host, another port
        "https://s4.internal:44300/sap/x",  # the virtual host, over https
        "https://s4.internal/sap/x",
    ],
)
async def test_an_absolute_link_off_the_destinations_address_is_refused(link):
    w = World()
    async with w.client() as http:
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get(link)
    assert not w.proxy.requests and not w.direct.requests


async def test_an_absolute_link_to_the_virtual_host_itself_goes_through_the_proxy():
    w = World()
    async with w.client() as http:
        await http.get(f"{VIRTUAL}{PATH}?$skiptoken=20")
    (r,) = w.proxy.requests
    assert r.url.query == b"$skiptoken=20&sap-client=100"
    assert r.headers[PROXY_AUTH] == "Bearer APP"


async def test_a_plain_http_link_on_the_direct_path_is_refused_with_connectivity():
    """An Internet destination's client that has the router: an http:// link
    to its own host must not leave (and never with the credential)."""
    w = World(Resolver(url="https://api.example.com", proxy_type="Internet"))
    async with w.client() as http:
        with pytest.raises(DestinationError, match="refusing to send"):
            await http.get("http://api.example.com/v1/next")
    assert not w.proxy.requests and not w.direct.requests


async def test_a_401_retry_on_a_user_run_still_sends_no_destination_header():
    w = World(pp_resolver(), proxy=Wire(401, 200))
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="exchange") as http:
            response = await http.get(PATH, headers={"Authorization": "Basic Y2FsbGVyOng="})
    assert response.status_code == 200 and len(w.proxy.requests) == 2
    assert [c[2] for c in w.resolver.calls] == [False, True]
    for r in w.proxy.requests:
        assert "Authorization" not in r.headers and "X-Stored" not in r.headers
        assert r.headers[PROXY_AUTH] == "Bearer UX-alice@example.com-jwt-a"
    assert w.resolver.invalidated == ["alice@example.com"]


async def test_a_redirect_answered_on_the_proxy_path_is_not_followed():
    w = World()

    def redirect(request: httpx.Request) -> httpx.Response:
        w.proxy.requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example.com/login"})

    http = destination_http_client(
        w.resolver,
        connectivity=w.tokens,
        transport=w.direct.transport,
        proxy_transport=httpx.MockTransport(redirect),
    )
    async with http:
        response = await http.get(PATH)
    assert response.status_code == 302
    assert len(w.proxy.requests) == 1 and not w.direct.requests


# -------------------------------------------- queries: the OData callers


async def test_odata_callers_skip_dollar_queries_of_a_destination(caplog):
    """A destination must not add ``$filter``, ``$expand``, ``$top`` ...
    behind the argument checks of the tools."""
    caplog.set_level(logging.WARNING, logger=destination_auth.logger.name)
    queries = {
        "$filter": "Secret eq 'x'",
        "$expand": "to_All",
        "$top": "9999",
        "$format": "xml",
        "$skiptoken": "5",
        "sap-client": "100",
    }
    w = World(Resolver(queries=queries))
    async with w.client() as http:
        await http.get(PATH, params={"$top": "1"})
        await http.get(PATH)
    first, second = w.proxy.requests
    assert first.url.query == b"%24top=1&sap-client=100"
    assert second.url.query == b"sap-client=100"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # Once per destination, the destination's name only.
    assert len(warnings) == 1 and "S4_ODATA_TECH" in warnings[0]
    assert "Secret" not in caplog.text and "9999" not in caplog.text


async def test_odata_callers_deduplicate_destination_queries_without_case():
    w = World(Resolver(queries={"sap-client": "100", "SAP-CLIENT": "200", "sap-language": "EN"}))
    async with w.client() as http:
        await http.get(PATH)
        await http.get(PATH, params={"Sap-Client": "300"})
    first, second = w.proxy.requests
    # The first spelling of the destination; then the caller's wins.
    assert first.url.query == b"sap-client=100&sap-language=EN"
    assert second.url.query == b"Sap-Client=300&sap-language=EN"


async def test_a_next_link_with_a_skiptoken_keeps_it_and_gets_no_second_client():
    w = World(Resolver(queries={"sap-client": "100", "$skiptoken": "1"}))
    async with w.client() as http:
        await http.get(f"{PATH}?$skiptoken=abc%2F20&sap-client=100")
    (r,) = w.proxy.requests
    assert r.url.query == b"$skiptoken=abc%2F20&sap-client=100"


async def test_other_destination_users_keep_append_when_absent():
    """No OData rules for Gmail, Jira, MCP ...: every query property the
    request lacks is appended, ``$`` or not."""
    w = World(
        Resolver(
            url="https://api.example.com",
            proxy_type="Internet",
            queries={"$format": "json", "key": "1", "KEY": "2"},
        )
    )
    async with destination_http_client(w.resolver, transport=w.direct.transport) as http:
        await http.get("/v1/things", params={"q": "x"})
    assert w.direct.requests[0].url.query == b"q=x&%24format=json&key=1&KEY=2"


# ------------------------------------- the real ConnectivityTokens behind it


class Uaa:
    """The connectivity service's XSUAA: a new token per call."""

    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        n = len(self.forms)
        if form["grant_type"] == "client_credentials":
            token = f"app-{n}"
        else:
            token = f"user-of-{form['assertion']}-{n}"
        return httpx.Response(200, json={"access_token": token, "expires_in": 3600})

    def grants(self, kind: str) -> list[dict[str, str]]:
        app = kind == "app"
        return [f for f in self.forms if (f["grant_type"] == "client_credentials") == app]


def real_world(resolver: Resolver, proxy: Wire) -> tuple[World, Uaa]:
    from agents.destination import ConnectivityTokens

    uaa = Uaa()
    w = World(resolver, proxy=proxy)
    w.tokens = ConnectivityTokens(FakeTokens.config, transport=httpx.MockTransport(uaa.handler))
    return w, uaa


async def test_real_tokens_a_407_invalidates_and_the_next_request_uses_the_new_token():
    w, uaa = real_world(pp_resolver(), Wire(200, 407, 200, 200))
    async with w.client(user_context=True, pp_mode="exchange") as http:
        with as_user(*ALICE):
            await http.get(PATH)
            await http.get(PATH)  # 407, then retried
            await http.get(PATH)
        with as_user(*BOB):
            await http.get(PATH)
    sent = [r.headers[PROXY_AUTH] for r in w.proxy.requests]
    assert sent == [
        "Bearer user-of-jwt-a-1",
        "Bearer user-of-jwt-a-1",
        "Bearer user-of-jwt-a-2",
        "Bearer user-of-jwt-a-2",
        "Bearer user-of-jwt-b-3",
    ]
    assert [f["assertion"] for f in uaa.forms] == ["jwt-a", "jwt-a", "jwt-b"]
    assert uaa.grants("app") == []


async def test_real_tokens_header_mode_refetches_the_app_token_once_per_407():
    w, uaa = real_world(pp_resolver(), Wire(407, 200, 200))
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="header") as http:
            await http.get(PATH)
            await http.get(PATH)
    assert [r.headers[PROXY_AUTH] for r in w.proxy.requests] == [
        "Bearer app-1",
        "Bearer app-2",
        "Bearer app-2",
    ]
    assert len(uaa.grants("app")) == 2 and uaa.grants("user") == []


async def test_real_tokens_the_same_principal_with_another_jwt_is_no_cache_hit():
    """ "Run now": the trigger's JWT under the run-as principal must not be
    served the token exchanged from that user's own JWT, or the reverse."""
    w, uaa = real_world(pp_resolver(), Wire())
    async with w.client(user_context=True, pp_mode="exchange") as http:
        for jwt in ("jwt-b", "jwt-a", "jwt-b"):
            with as_user(jwt, "bob@example.com"):
                await http.get(PATH)
    assert [r.headers[PROXY_AUTH] for r in w.proxy.requests] == [
        "Bearer user-of-jwt-b-1",
        "Bearer user-of-jwt-a-2",
        "Bearer user-of-jwt-b-1",
    ]
    assert [f["assertion"] for f in uaa.forms] == ["jwt-b", "jwt-a"]


async def test_a_failing_token_endpoint_names_nothing_of_what_it_answered(caplog):
    """The REAL ``ConnectivityTokens``: neither the endpoint's body nor an
    httpx error text reaches the error or the log."""
    from agents.destination import ConnectivityTokens

    caplog.set_level(logging.DEBUG)

    def refuses(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "error": "unauthorized",
                "error_description": "client sb-clone-12345!b999 of zone acme-prod-zone is locked",
            },
        )

    def odd_code(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Client sb-clone-12345!b999 unknown"})

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to tok-9f8e7d6c5b4a secret-looking", request=request)

    texts = []
    for handler in (refuses, odd_code, unreachable):
        w = World()
        w.tokens = ConnectivityTokens(FakeTokens.config, transport=httpx.MockTransport(handler))
        async with w.client() as http:
            with pytest.raises(DestinationError) as info:
                await http.get(PATH)
        assert info.value.__cause__ is None and info.value.__context__ is None
        texts.append(str(info.value))
        assert not w.proxy.requests
    assert "500" in texts[0] and "unauthorized" in texts[0]
    assert "401" in texts[1] and "ConnectError" in texts[2]
    for leaked in ("sb-clone", "acme-prod-zone", "locked", "unknown", "tok-9f8e7d6c5b4a", "s3cret"):
        assert all(leaked not in text for text in texts), leaked
        assert leaked not in caplog.text, leaked
