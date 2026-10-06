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
    """``ConnectivityTokens`` without a token endpoint."""

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
        if force:
            self.generation += 1
        return "APP" + self._suffix()

    async def user_token(self, user_jwt: str, principal: str | None, *, force: bool = False) -> str:
        self.calls.append(("user", user_jwt, principal, force))
        if force:
            self.generation += 1
        # Bound to the JWT as the real cache is (principal AND token digest).
        return f"UX-{principal}-{user_jwt}" + self._suffix()

    @staticmethod
    def user_key(principal: str | None, user_jwt: str) -> str:
        return principal or f"token:{user_jwt}"

    def invalidate(self, principal: str | None = None) -> None:
        self.invalidated.append(principal)


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
    monkeypatch.setenv("CONNECTIVITY_PP_MODE", "header")
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.get(PATH)
    assert w.proxy.requests[0].headers[PP_HEADER] == "Bearer jwt-a"


def test_an_unknown_mode_is_refused_or_falls_back_to_the_default(caplog):
    with pytest.raises(ValueError, match="pp_mode"):
        DestinationAuth(Resolver(), connectivity=FakeTokens(), pp_mode="basic")
    caplog.set_level(logging.WARNING)
    # Both modes are user modes, so the default is a safe answer to a typo.
    assert destination_auth.pp_mode_from_environment({"CONNECTIVITY_PP_MODE": "s3cret-typo"}) == (
        "exchange"
    )
    assert "CONNECTIVITY_PP_MODE" in caplog.text and "s3cret-typo" not in caplog.text


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
    w = World()
    async with w.client(connectivity=None) as http:
        with pytest.raises(DestinationError, match="no connectivity service binding") as info:
            await http.get(PATH)
    assert "S4_ODATA_TECH" in str(info.value)
    assert not w.proxy.requests and not w.direct.requests


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
    assert [c[-1] for c in w.tokens.calls] == [False, True]
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
    assert w.tokens.calls == [("app", False), ("app", True)]


async def test_407_in_header_mode_drops_the_app_token():
    w = World(pp_resolver(), proxy=Wire(407, 200))
    with as_user(*ALICE):
        async with w.client(user_context=True, pp_mode="header") as http:
            await http.get(PATH)
    assert w.tokens.invalidated == [None]
    assert w.proxy.requests[1].headers[PP_HEADER] == "Bearer jwt-a"


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
    auth = DestinationAuth(w.resolver, connectivity=w.tokens, retry_on_401=False)
    router = destination_auth.OnPremiseRouter(direct=w.direct.transport, proxied=w.proxy.transport)
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
