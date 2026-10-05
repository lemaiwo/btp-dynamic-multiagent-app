"""OnPremise destination properties and the connectivity service's tokens.

Covered: ``ProxyType``, ``CloudConnectorLocationId`` and ``URL.queries.*`` are
read off a destination and default to empty for every existing (Internet)
payload; a ``PrincipalPropagation`` OnPremise destination, which carries no
``authTokens``, resolves for a signed-in user and is refused without one; the
connectivity binding is read from ``VCAP_SERVICES`` or ``CONNECTIVITY_*``;
``ConnectivityTokens`` caches the app token, keeps user tokens per principal
(bounded, never in the app slot, never an app-token fall-back) and reports a
token failure without the client secret or the user's JWT.

No network: the destination service and the connectivity service's XSUAA are
MockTransports.

Run:  python -m pytest tests/test_odata_connectivity.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs

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

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents import destination as dest_mod  # noqa: E402
from agents.destination import (  # noqa: E402
    ConnectivityConfig,
    ConnectivityTokens,
    Destination,
    DestinationError,
    DestinationResolver,
    DestinationServiceConfig,
    connectivity_config_from_environment,
    connectivity_from_environment,
)

CONFIG = DestinationServiceConfig(
    client_id="sb-dest",
    client_secret="shh",
    token_url="https://uaa.example/oauth/token",
    api_url="https://destination.example",
)

SECRET = "conn-s3cr3t-value"
CCONFIG = ConnectivityConfig(
    client_id="sb-conn",
    client_secret=SECRET,
    token_url="https://conn-uaa.example/oauth/token",
    proxy_host="proxy.internal",
    proxy_port=20003,
)

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

PP_PAYLOAD = {
    "destinationConfiguration": {
        "Name": "S4_ODATA_USER",
        "URL": "http://s4.internal:44300",
        "ProxyType": "OnPremise",
        "Authentication": "PrincipalPropagation",
        "URL.queries.sap-client": "100",
    },
}

BASIC_TOKEN = {
    "type": "Basic",
    "value": "dXNlcjpwdw==",
    "http_header": {"key": "Authorization", "value": "Basic dXNlcjpwdw=="},
}


def service(payload):
    """A destination service that answers every find-destination with ``payload``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 3600})
        return httpx.Response(200, json=payload)

    return handler


def _resolver(payload, **kw):
    return DestinationResolver(
        "S4_ODATA_USER", CONFIG, transport=httpx.MockTransport(service(payload)), **kw
    )


class Uaa:
    """The connectivity service's XSUAA: one distinct token per grant and caller."""

    def __init__(self, *, expires_in: int = 3600):
        self.forms: list[dict[str, str]] = []
        self.expires_in = expires_in
        self.fail_with: httpx.Response | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if self.fail_with is not None:
            return self.fail_with
        if form.get("grant_type") == "client_credentials":
            token = f"app-token-{self.count('client_credentials')}"
        else:
            token = f"user-token-for-{form.get('assertion')}-{len(self.forms)}"
        return httpx.Response(200, json={"access_token": token, "expires_in": self.expires_in})

    def count(self, grant: str) -> int:
        return sum(1 for f in self.forms if f.get("grant_type") == grant)

    def last(self, grant: str) -> dict[str, str]:
        return [f for f in self.forms if f.get("grant_type") == grant][-1]


def _tokens(uaa: Uaa) -> ConnectivityTokens:
    return ConnectivityTokens(CCONFIG, transport=httpx.MockTransport(uaa.handler))


# --- destination properties --------------------------------------------------


async def test_onpremise_properties_are_read():
    d = await _resolver(
        {
            "destinationConfiguration": {
                "URL": "http://s4.internal:44300",
                "ProxyType": "OnPremise",
                "Authentication": "BasicAuthentication",
                "CloudConnectorLocationId": "LOC1",
                "URL.queries.sap-client": "100",
                "URL.headers.X-Custom": "1",
            },
            "authTokens": [BASIC_TOKEN],
        }
    ).resolve()
    assert (d.proxy_type, d.location_id, d.queries) == ("OnPremise", "LOC1", {"sap-client": "100"})
    assert d.headers == {"X-Custom": "1", "Authorization": "Basic dXNlcjpwdw=="}
    assert not d.per_user


async def test_blank_query_properties_are_skipped():
    d = await _resolver(
        {
            "destinationConfiguration": {
                "URL": "http://s4.internal:44300",
                "ProxyType": "OnPremise",
                "URL.queries.": "x",
                "URL.queries.sap-client": " ",
                "URL.queries.sap-language": " EN ",
            },
            "authTokens": [BASIC_TOKEN],
        }
    ).resolve()
    assert d.queries == {"sap-language": "EN"}


async def test_principal_propagation_without_auth_tokens_resolves_for_a_user():
    d = await _resolver(PP_PAYLOAD).resolve(user_token="jwt-a", principal="alice@example.com")
    assert d.auth_type == "PrincipalPropagation" and d.per_user and "Authorization" not in d.headers
    assert (d.proxy_type, d.queries) == ("OnPremise", {"sap-client": "100"})


async def test_principal_propagation_without_a_user_is_refused():
    with pytest.raises(DestinationError, match="PrincipalPropagation.*signed-in user"):
        await _resolver(PP_PAYLOAD).resolve()


async def test_principal_propagation_without_a_user_is_refused_even_for_a_public_target():
    # require_credential=False accepts a bare URL; it must not turn a
    # per-user destination into one that resolves as the application.
    with pytest.raises(DestinationError, match="PrincipalPropagation.*signed-in user"):
        await _resolver(PP_PAYLOAD, require_credential=False).resolve()


async def test_principal_propagation_result_is_not_served_to_the_app_slot():
    resolver = _resolver(PP_PAYLOAD)
    await resolver.resolve(user_token="jwt-a", principal="alice@example.com")
    assert resolver.cached_principals == ["alice@example.com"]
    with pytest.raises(DestinationError, match="signed-in user"):
        await resolver.resolve()


async def test_principal_propagation_on_an_internet_destination_keeps_the_old_refusal():
    payload = {
        "destinationConfiguration": {
            "URL": "https://api.example",
            "Authentication": "PrincipalPropagation",
        }
    }
    with pytest.raises(DestinationError, match="returned no authentication token"):
        await _resolver(payload).resolve(user_token="jwt-a", principal="alice@example.com")


async def test_principal_propagation_keeps_a_header_the_service_returned():
    payload = dict(
        PP_PAYLOAD,
        authTokens=[
            {
                "type": "Bearer",
                "value": "pp-tok",
                "expires_in": "600",
                "http_header": {"key": "Authorization", "value": "Bearer pp-tok"},
            }
        ],
    )
    d = await _resolver(payload).resolve(user_token="jwt-a", principal="alice@example.com")
    assert d.headers == {"Authorization": "Bearer pp-tok"} and d.proxy_type == "OnPremise"


async def test_internet_destination_defaults():
    d = await _resolver(
        {
            "destinationConfiguration": {
                "URL": "https://graph.example",
                "Authentication": "OAuth2ClientCredentials",
                "ProxyType": "Internet",
            },
            "authTokens": [
                {
                    "type": "Bearer",
                    "value": "t",
                    "expires_in": "3600",
                    "http_header": {"key": "Authorization", "value": "Bearer t"},
                }
            ],
        }
    ).resolve()
    assert (d.proxy_type, d.location_id, d.queries) == ("Internet", "", {})
    bare = await _resolver(
        {
            "destinationConfiguration": {
                "URL": "https://graph.example",
                "URL.headers.Authorization": "Bearer static",
            },
        }
    ).resolve()
    assert (bare.proxy_type, bare.location_id, bare.queries) == ("", "", {})


def test_existing_constructor_still_works():
    d = Destination(url="https://x.example", headers={}, expires_at=1.0)
    assert (d.proxy_type, d.location_id, d.queries) == ("", "", {})


# --- connectivity binding ----------------------------------------------------


def test_connectivity_config_from_vcap_and_env():
    cfg = connectivity_config_from_environment(
        {
            "VCAP_SERVICES": json.dumps(
                {
                    "connectivity": [
                        {
                            "credentials": {
                                "clientid": "c",
                                "clientsecret": SECRET,
                                "token_service_url": "https://uaa.example",
                                "onpremise_proxy_host": "proxy.internal",
                                "onpremise_proxy_http_port": "20003",
                            }
                        }
                    ]
                }
            )
        }
    )
    assert (cfg.token_url, cfg.proxy_host, cfg.proxy_port) == (
        "https://uaa.example/oauth/token",
        "proxy.internal",
        20003,
    )
    assert cfg.client_secret == SECRET and SECRET not in repr(cfg)  # secret not in repr
    assert connectivity_config_from_environment({}) is None
    assert (
        connectivity_config_from_environment(
            {
                "CONNECTIVITY_CLIENT_ID": "c",
                "CONNECTIVITY_CLIENT_SECRET": "s",
                "CONNECTIVITY_UAA_URL": "https://uaa.example",
                "CONNECTIVITY_PROXY_HOST": "h",
                "CONNECTIVITY_PROXY_PORT": "20003",
            }
        ).proxy_port
        == 20003
    )


def test_connectivity_config_fallback_keys_and_bad_shapes():
    creds = {
        "clientid": "c",
        "clientsecret": "s",
        "url": "https://uaa.example/oauth/token",
        "onpremise_proxy_host": "proxy.internal",
        "onpremise_proxy_port": 20003,
    }
    cfg = connectivity_config_from_environment(
        {"VCAP_SERVICES": json.dumps({"connectivity": [{"credentials": creds}]})}
    )
    assert (cfg.token_url, cfg.proxy_port) == ("https://uaa.example/oauth/token", 20003)
    env = {
        "CONNECTIVITY_CLIENT_ID": "c",
        "CONNECTIVITY_CLIENT_SECRET": "s",
        "CONNECTIVITY_TOKEN_URL": "https://t.example/oauth/token",
        "CONNECTIVITY_PROXY_HOST": "h",
        "CONNECTIVITY_PROXY_PORT": "20003",
    }
    # A malformed or wrongly shaped VCAP_SERVICES falls through to the env.
    for raw in ("{not json", json.dumps({"connectivity": "oops"}), json.dumps([1])):
        assert (
            connectivity_config_from_environment({"VCAP_SERVICES": raw, **env}).token_url
            == "https://t.example/oauth/token"
        )
    # A port that is no port is no binding.
    assert connectivity_config_from_environment({**env, "CONNECTIVITY_PROXY_PORT": "abc"}) is None
    assert connectivity_config_from_environment({**env, "CONNECTIVITY_PROXY_PORT": "0"}) is None
    bad = dict(creds, onpremise_proxy_port="x")
    assert (
        connectivity_config_from_environment(
            {"VCAP_SERVICES": json.dumps({"connectivity": [{"credentials": bad}]})}
        )
        is None
    )


def test_connectivity_from_environment(monkeypatch):
    assert connectivity_from_environment() is None
    monkeypatch.setenv("CONNECTIVITY_CLIENT_ID", "c")
    monkeypatch.setenv("CONNECTIVITY_CLIENT_SECRET", "s")
    monkeypatch.setenv("CONNECTIVITY_UAA_URL", "https://uaa.example")
    monkeypatch.setenv("CONNECTIVITY_PROXY_HOST", "h")
    monkeypatch.setenv("CONNECTIVITY_PROXY_PORT", "20003")
    tokens = connectivity_from_environment()
    assert isinstance(tokens, ConnectivityTokens) and tokens.config.proxy_host == "h"


# --- connectivity tokens -----------------------------------------------------


async def test_app_token_is_cached_and_user_tokens_are_per_principal():
    uaa = Uaa()
    tokens = _tokens(uaa)
    assert (
        await tokens.app_token() == await tokens.app_token()
        and uaa.count("client_credentials") == 1
    )
    a = await tokens.user_token("jwt-a", "alice@example.com")
    b = await tokens.user_token("jwt-b", "bob@example.com")
    assert a != b and a != await tokens.app_token()
    form = uaa.last(JWT_BEARER)
    assert form["assertion"] == "jwt-b"
    assert (form["token_format"], form["response_type"]) == ("jwt", "token")
    assert (form["client_id"], form["client_secret"]) == ("sb-conn", SECRET)
    # Cached per principal: each asks again and gets their own.
    assert await tokens.user_token("jwt-a", "alice@example.com") == a
    assert await tokens.user_token("jwt-b", "bob@example.com") == b
    assert uaa.count(JWT_BEARER) == 2
    tokens.invalidate("alice@example.com")
    assert await tokens.user_token("jwt-b", "bob@example.com") == b  # bob untouched
    await tokens.user_token("jwt-a", "alice@example.com")
    assert uaa.count(JWT_BEARER) == 3
    assert uaa.count("client_credentials") == 1  # the app slot was never touched


async def test_invalidate_without_principal_drops_only_the_app_token():
    uaa = Uaa()
    tokens = _tokens(uaa)
    first = await tokens.app_token()
    a = await tokens.user_token("jwt-a", "alice@example.com")
    tokens.invalidate()
    assert await tokens.app_token() != first and uaa.count("client_credentials") == 2
    assert await tokens.user_token("jwt-a", "alice@example.com") == a and uaa.count(JWT_BEARER) == 1


async def test_force_refetches():
    uaa = Uaa()
    tokens = _tokens(uaa)
    await tokens.app_token()
    await tokens.app_token(force=True)
    await tokens.user_token("jwt-a", "alice@example.com")
    await tokens.user_token("jwt-a", "alice@example.com", force=True)
    assert (uaa.count("client_credentials"), uaa.count(JWT_BEARER)) == (2, 2)


async def test_tokens_expire_with_the_skew():
    uaa = Uaa(expires_in=30)  # below the skew: usable for one second only
    tokens = _tokens(uaa)
    await tokens.app_token()
    await tokens.user_token("jwt-a", "alice@example.com")
    tokens._app = (tokens._app[0], 0.0)
    (key,) = tokens._per_user
    tokens._per_user[key] = (tokens._per_user[key][0], 0.0)
    await tokens.app_token()
    await tokens.user_token("jwt-a", "alice@example.com")
    assert (uaa.count("client_credentials"), uaa.count(JWT_BEARER)) == (2, 2)


async def test_user_token_without_a_principal_is_keyed_by_the_token_digest():
    uaa = Uaa()
    tokens = _tokens(uaa)
    a = await tokens.user_token("jwt-a", None)
    b = await tokens.user_token("jwt-b", None)
    assert a != b and await tokens.user_token("jwt-a", None) == a and uaa.count(JWT_BEARER) == 2
    assert all(k.startswith("token:") and "jwt-" not in k for k in tokens.cached_principals)


async def test_user_token_without_a_jwt_is_refused_and_never_the_app_token():
    uaa = Uaa()
    tokens = _tokens(uaa)
    await tokens.app_token()
    for missing in ("", None):
        with pytest.raises(DestinationError, match="signed-in user"):
            await tokens.user_token(missing, "alice@example.com")
    assert uaa.count(JWT_BEARER) == 0 and tokens.cached_principals == []


async def test_user_token_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(dest_mod, "PER_USER_CACHE_MAX", 2)
    uaa = Uaa()
    tokens = _tokens(uaa)
    for who in ("alice", "bob", "carol"):
        await tokens.user_token(f"jwt-{who}", f"{who}@example.com")
    assert tokens.cached_principals == ["bob@example.com", "carol@example.com"]
    # A hit moves the entry to the fresh end, so the other one goes next.
    await tokens.user_token("jwt-bob", "bob@example.com")
    await tokens.user_token("jwt-dave", "dave@example.com")
    assert tokens.cached_principals == ["bob@example.com", "dave@example.com"]


async def test_token_error_is_a_destination_error_without_the_secret():
    uaa = Uaa()
    tokens = _tokens(uaa)
    # An endpoint that echoes what it was sent must not get it into the message.
    uaa.fail_with = httpx.Response(
        401,
        json={
            "error": "invalid_client",
            "error_description": f"Bad credentials {SECRET} for jwt-a",
        },
    )
    with pytest.raises(DestinationError) as app_err:
        await tokens.app_token()
    with pytest.raises(DestinationError) as user_err:
        await tokens.user_token("jwt-a", "alice@example.com")
    for err in (app_err, user_err):
        assert "401" in str(err.value) and "invalid_client" in str(err.value)
        assert SECRET not in str(err.value)
    # The app call never sent a JWT; the user call did, and must not echo it.
    assert "jwt-a" not in str(user_err.value)
    assert tokens.cached_principals == []

    uaa.fail_with = httpx.Response(200, json={"token_type": "bearer"})
    with pytest.raises(DestinationError, match="no access_token"):
        await tokens.app_token()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {SECRET}", request=request)

    down = ConnectivityTokens(CCONFIG, transport=httpx.MockTransport(boom))
    with pytest.raises(DestinationError, match="could not reach") as net_err:
        await down.app_token()
    assert SECRET not in str(net_err.value)


# --- a cached credential belongs to principal AND token ----------------------
#
# A job run started with "Run now" carries the trigger's JWT while `run_as`
# sets the principal to the agent's run-as user, so the two can name different
# people. A cache keyed by principal alone would then hand the trigger's
# credential to the run-as user's own later request.


class PerUserService:
    """A destination service that answers with a token naming the X-user-token."""

    def __init__(self):
        self.user_tokens: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 3600})
        user = request.headers.get("X-user-token")
        self.user_tokens.append(user)
        return httpx.Response(
            200,
            json={
                "destinationConfiguration": {
                    "URL": "https://api.example",
                    "Authentication": "OAuth2UserTokenExchange",
                },
                "authTokens": [
                    {
                        "type": "Bearer",
                        "value": f"cred-of-{user}",
                        "expires_in": "3600",
                        "http_header": {"key": "Authorization", "value": f"Bearer cred-of-{user}"},
                    }
                ],
            },
        )

    def resolver(self) -> DestinationResolver:
        return DestinationResolver(
            "S4_ODATA_USER", CONFIG, transport=httpx.MockTransport(self.handler)
        )


def _cred(d: Destination) -> str:
    return d.headers["Authorization"]


async def test_resolver_never_serves_a_credential_obtained_with_another_users_token():
    svc = PerUserService()
    resolver = svc.resolver()
    # Alice's token travels with Bob's principal (a run-as job she triggered).
    mixed = await resolver.resolve(user_token="jwt-alice", principal="bob@example.com")
    assert _cred(mixed) == "Bearer cred-of-jwt-alice"
    # Bob's own request must not get Alice's credential ...
    bob = await resolver.resolve(user_token="jwt-bob", principal="bob@example.com")
    assert _cred(bob) == "Bearer cred-of-jwt-bob"
    # ... and neither is Alice's own request served the entry filed under Bob.
    alice = await resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    assert alice is not mixed
    assert svc.user_tokens == ["jwt-alice", "jwt-bob", "jwt-alice"]
    # Each of the three is a cache hit for exactly its own pair.
    assert await resolver.resolve(user_token="jwt-alice", principal="bob@example.com") is mixed
    assert await resolver.resolve(user_token="jwt-bob", principal="bob@example.com") is bob
    assert await resolver.resolve(user_token="jwt-alice", principal="alice@example.com") is alice
    assert len(svc.user_tokens) == 3
    assert sorted(resolver.cached_principals) == ["alice@example.com", "bob@example.com"]


async def test_resolver_invalidate_drops_every_entry_of_the_principal():
    svc = PerUserService()
    resolver = svc.resolver()
    await resolver.resolve(user_token="jwt-alice", principal="bob@example.com")
    await resolver.resolve(user_token="jwt-bob", principal="bob@example.com")
    alice = await resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    resolver.invalidate("bob@example.com")
    assert resolver.cached_principals == ["alice@example.com"]
    assert await resolver.resolve(user_token="jwt-alice", principal="alice@example.com") is alice
    await resolver.resolve(user_token="jwt-bob", principal="bob@example.com")
    await resolver.resolve(user_token="jwt-alice", principal="bob@example.com")
    assert len(svc.user_tokens) == 5
    resolver.invalidate_all()
    assert resolver.cached_principals == []


async def test_resolver_keys_never_hold_the_raw_token():
    resolver = PerUserService().resolver()
    await resolver.resolve(user_token="jwt-alice", principal="bob@example.com")
    await resolver.resolve(user_token="jwt-carol")
    assert "jwt-" not in repr(list(resolver._per_user)) + repr(resolver.cached_principals)


async def test_resolver_lru_bound_holds_with_several_tokens_per_principal(monkeypatch):
    monkeypatch.setattr(dest_mod, "PER_USER_CACHE_MAX", 2)
    svc = PerUserService()
    resolver = svc.resolver()
    for n in range(4):
        await resolver.resolve(user_token=f"jwt-{n}", principal="bob@example.com")
    assert len(resolver._per_user) == 2 and resolver.cached_principals == ["bob@example.com"]
    # The two newest survived; the oldest was evicted and is fetched again.
    await resolver.resolve(user_token="jwt-3", principal="bob@example.com")
    await resolver.resolve(user_token="jwt-2", principal="bob@example.com")
    assert len(svc.user_tokens) == 4
    await resolver.resolve(user_token="jwt-0", principal="bob@example.com")
    assert len(svc.user_tokens) == 5 and len(resolver._per_user) == 2


async def test_connectivity_never_serves_a_token_obtained_with_another_users_jwt():
    uaa = Uaa()
    tokens = _tokens(uaa)
    mixed = await tokens.user_token("jwt-alice", "bob@example.com")
    assert "jwt-alice" in mixed
    bob = await tokens.user_token("jwt-bob", "bob@example.com")
    assert "jwt-bob" in bob and bob != mixed
    assert uaa.last(JWT_BEARER)["assertion"] == "jwt-bob"
    alice = await tokens.user_token("jwt-alice", "alice@example.com")
    assert alice != mixed and uaa.count(JWT_BEARER) == 3
    assert await tokens.user_token("jwt-alice", "bob@example.com") == mixed
    assert await tokens.user_token("jwt-bob", "bob@example.com") == bob
    assert await tokens.user_token("jwt-alice", "alice@example.com") == alice
    assert uaa.count(JWT_BEARER) == 3
    assert sorted(tokens.cached_principals) == ["alice@example.com", "bob@example.com"]
    assert "jwt-" not in repr(list(tokens._per_user))


async def test_connectivity_invalidate_drops_every_entry_of_the_principal():
    uaa = Uaa()
    tokens = _tokens(uaa)
    await tokens.user_token("jwt-alice", "bob@example.com")
    await tokens.user_token("jwt-bob", "bob@example.com")
    alice = await tokens.user_token("jwt-alice", "alice@example.com")
    # What the proxy's 407 rule calls.
    tokens.invalidate(ConnectivityTokens.user_key("bob@example.com", "jwt-bob"))
    assert tokens.cached_principals == ["alice@example.com"]
    assert await tokens.user_token("jwt-alice", "alice@example.com") == alice
    await tokens.user_token("jwt-bob", "bob@example.com")
    await tokens.user_token("jwt-alice", "bob@example.com")
    assert uaa.count(JWT_BEARER) == 5
    # Without a principal, user_key names that one token's entry.
    await tokens.user_token("jwt-carol", None)
    await tokens.user_token("jwt-dave", None)
    tokens.invalidate(ConnectivityTokens.user_key(None, "jwt-carol"))
    assert [k for k in tokens.cached_principals if k.startswith("token:")] == [
        ConnectivityTokens.user_key(None, "jwt-dave")
    ]


async def test_connectivity_lru_bound_holds_with_several_tokens_per_principal(monkeypatch):
    monkeypatch.setattr(dest_mod, "PER_USER_CACHE_MAX", 2)
    uaa = Uaa()
    tokens = _tokens(uaa)
    for n in range(4):
        await tokens.user_token(f"jwt-{n}", "bob@example.com")
    assert len(tokens._per_user) == 2 and tokens.cached_principals == ["bob@example.com"]
    await tokens.user_token("jwt-3", "bob@example.com")
    await tokens.user_token("jwt-2", "bob@example.com")
    assert uaa.count(JWT_BEARER) == 4
    await tokens.user_token("jwt-0", "bob@example.com")
    assert uaa.count(JWT_BEARER) == 5 and len(tokens._per_user) == 2


# --- review follow-up: pinned edge cases, scrubbed errors, per-key locks -----


def _sha(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def test_a_principal_shaped_like_a_token_owner_does_not_reach_that_callers_entry():
    svc = PerUserService()
    resolver = svc.resolver()
    spoof = "token:" + _sha("jwt-victim")
    assert DestinationResolver.user_key(None, "jwt-victim") == spoof
    # A caller whose principal is literally the owner name of a principal-less
    # caller's entry, in both orders.
    evil = await resolver.resolve(user_token="jwt-evil", principal=spoof)
    victim = await resolver.resolve(user_token="jwt-victim")
    assert (_cred(evil), _cred(victim)) == ("Bearer cred-of-jwt-evil", "Bearer cred-of-jwt-victim")
    assert await resolver.resolve(user_token="jwt-evil", principal=spoof) is evil
    assert await resolver.resolve(user_token="jwt-victim") is victim
    assert svc.user_tokens == ["jwt-evil", "jwt-victim"]

    uaa = Uaa()
    tokens = _tokens(uaa)
    t_victim = await tokens.user_token("jwt-victim", None)
    t_evil = await tokens.user_token("jwt-evil", spoof)
    assert "jwt-victim" in t_victim and "jwt-evil" in t_evil
    assert await tokens.user_token("jwt-victim", None) == t_victim
    assert await tokens.user_token("jwt-evil", spoof) == t_evil
    assert uaa.count(JWT_BEARER) == 2


async def test_destination_auth_never_sends_a_credential_resolved_with_another_users_token():
    """The production path: ``DestinationAuth`` reads ``current_jwt`` and
    ``current_principal`` separately, and a run-as job binds the trigger's
    token with the run-as user's principal."""
    from agents.auth import current_jwt, current_principal
    from agents.destination_auth import destination_http_client

    svc = PerUserService()
    sent: list[str] = []

    def target(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"ok": True})

    client = destination_http_client(
        svc.resolver(), user_context=True, transport=httpx.MockTransport(target)
    )

    async def call(jwt: str, principal: str) -> None:
        j = current_jwt.set(jwt)
        p = current_principal.set(principal)
        try:
            (await client.get("/v1/things")).raise_for_status()
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    async with client:
        await call("jwt-alice", "bob@example.com")  # Alice triggers a job that runs as Bob
        await call("jwt-bob", "bob@example.com")  # Bob's own request
        await call("jwt-alice", "alice@example.com")  # Alice's own request
        await call("jwt-bob", "bob@example.com")  # Bob again: his own entry, from cache
    assert sent == [
        "Bearer cred-of-jwt-alice",
        "Bearer cred-of-jwt-bob",
        "Bearer cred-of-jwt-alice",
        "Bearer cred-of-jwt-bob",
    ]
    assert svc.user_tokens == ["jwt-alice", "jwt-bob", "jwt-alice"]


class GatedService(PerUserService):
    """Holds the find-destination call of ``slow`` open until released."""

    def __init__(self, slow: str):
        super().__init__()
        self.slow = slow
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        if request.headers.get("X-user-token") == self.slow:
            self.entered.set()
            await self.release.wait()
        return PerUserService.handler(self, request)


class GatedUaa(Uaa):
    def __init__(self, slow: str):
        super().__init__()
        self.slow = slow
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        if f"assertion={self.slow}".encode() in request.content:
            self.entered.set()
            await self.release.wait()
        return Uaa.handler(self, request)


async def test_overlapping_resolves_for_the_same_user_and_token_fetch_once():
    svc = GatedService(slow="jwt-alice")
    resolver = svc.resolver()
    first = asyncio.create_task(
        resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    )
    second = asyncio.create_task(
        resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    )
    await svc.entered.wait()
    await asyncio.sleep(0)
    svc.release.set()
    a, b = await asyncio.gather(first, second)
    assert a is b and svc.user_tokens == ["jwt-alice"]
    assert resolver._user_locks.idle

    uaa = GatedUaa(slow="jwt-alice")
    tokens = _tokens(uaa)
    t1 = asyncio.create_task(tokens.user_token("jwt-alice", "alice@example.com"))
    t2 = asyncio.create_task(tokens.user_token("jwt-alice", "alice@example.com"))
    await uaa.entered.wait()
    await asyncio.sleep(0)
    uaa.release.set()
    assert await t1 == await t2 and uaa.count(JWT_BEARER) == 1
    assert tokens._user_locks.idle


async def test_one_users_slow_resolve_does_not_hold_up_another_user():
    svc = GatedService(slow="jwt-alice")
    resolver = svc.resolver()
    slow = asyncio.create_task(
        resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    )
    await svc.entered.wait()
    # Alice's fetch is open. Bob, the same principal with another token, and
    # the app-level slot all complete meanwhile.
    bob = await asyncio.wait_for(
        resolver.resolve(user_token="jwt-bob", principal="bob@example.com"), 1
    )
    other = await asyncio.wait_for(
        resolver.resolve(user_token="jwt-alice-2", principal="alice@example.com"), 1
    )
    app = await asyncio.wait_for(resolver.resolve(), 1)
    assert _cred(bob) == "Bearer cred-of-jwt-bob" and _cred(other) == "Bearer cred-of-jwt-alice-2"
    assert _cred(app) == "Bearer cred-of-None" and not slow.done()
    svc.release.set()
    assert _cred(await slow) == "Bearer cred-of-jwt-alice"
    assert resolver._user_locks.idle


async def test_one_users_slow_token_exchange_does_not_hold_up_another_user():
    uaa = GatedUaa(slow="jwt-alice")
    tokens = _tokens(uaa)
    slow = asyncio.create_task(tokens.user_token("jwt-alice", "alice@example.com"))
    await uaa.entered.wait()
    bob = await asyncio.wait_for(tokens.user_token("jwt-bob", "bob@example.com"), 1)
    app = await asyncio.wait_for(tokens.app_token(), 1)
    assert "jwt-bob" in bob and app.startswith("app-token") and not slow.done()
    uaa.release.set()
    assert "jwt-alice" in await slow
    assert tokens._user_locks.idle


async def test_a_cancelled_fetch_leaves_no_lock_behind():
    svc = GatedService(slow="jwt-alice")
    resolver = svc.resolver()
    task = asyncio.create_task(
        resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    )
    await svc.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resolver._user_locks.idle and resolver.cached_principals == []
    svc.slow = ""
    d = await asyncio.wait_for(
        resolver.resolve(user_token="jwt-alice", principal="alice@example.com"), 1
    )
    assert _cred(d) == "Bearer cred-of-jwt-alice"


async def test_insert_drops_the_same_owners_expired_entries():
    import dataclasses

    svc = PerUserService()
    resolver = svc.resolver()
    await resolver.resolve(user_token="jwt-old", principal="bob@example.com")
    await resolver.resolve(user_token="jwt-alice", principal="alice@example.com")
    for key in list(resolver._per_user):  # everything cached so far has expired
        resolver._per_user[key] = dataclasses.replace(resolver._per_user[key], expires_at=0.0)
    await resolver.resolve(user_token="jwt-new", principal="bob@example.com")
    # Bob's expired entry went; Alice's is not Bob's to clean up.
    assert [(o, d == _sha("jwt-new")) for o, d in resolver._per_user] == [
        ("alice@example.com", False),
        ("bob@example.com", True),
    ]

    uaa = Uaa()
    tokens = _tokens(uaa)
    await tokens.user_token("jwt-old", "bob@example.com")
    await tokens.user_token("jwt-alice", "alice@example.com")
    for key in list(tokens._per_user):
        tokens._per_user[key] = (tokens._per_user[key][0], 0.0)
    await tokens.user_token("jwt-new", "bob@example.com")
    assert list(tokens._per_user) == [
        ("alice@example.com", _sha("jwt-alice")),
        ("bob@example.com", _sha("jwt-new")),
    ]


async def test_resolver_errors_carry_neither_the_user_token_nor_a_secret():
    user_jwt = "eyJ.user-jwt-distinctive.sig"
    svc_secret = CONFIG.client_secret  # "shh" is too short to be distinctive
    config = DestinationServiceConfig(
        client_id="sb-dest",
        client_secret="dest-s3cr3t-distinctive",
        token_url="https://uaa.example/oauth/token",
        api_url="https://destination.example",
    )
    assert svc_secret != config.client_secret

    def resolver_with(handler) -> DestinationResolver:
        return DestinationResolver("S4_ODATA_USER", config, transport=httpx.MockTransport(handler))

    def leaks(exc: BaseException) -> bool:
        seen, texts = set(), []
        while exc is not None and id(exc) not in seen:  # the chain is logged too
            seen.add(id(exc))
            texts.append(str(exc))
            exc = exc.__cause__ or exc.__context__
        text = " ".join(texts)
        return any(s in text for s in (user_jwt, config.client_secret, "svc-token-distinctive"))

    # A destination service (or a proxy in front of it) that echoes the request.
    def echo_find(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "svc-token-distinctive"})
        return httpx.Response(500, text=f"upstream failed; headers were {dict(request.headers)}")

    with pytest.raises(DestinationError, match="returned 500") as find_err:
        await resolver_with(echo_find).resolve(user_token=user_jwt, principal="alice@example.com")
    assert "upstream failed" in str(find_err.value) and not leaks(find_err.value)

    def echo_token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad client: {request.content.decode()}")

    with pytest.raises(DestinationError, match="token request returned 401") as token_err:
        await resolver_with(echo_token).resolve(user_token=user_jwt, principal="alice@example.com")
    assert not leaks(token_err.value)

    def boom_find(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "svc-token-distinctive"})
        raise httpx.ConnectError(f"refused {dict(request.headers)}", request=request)

    with pytest.raises(
        DestinationError, match="could not reach the destination service"
    ) as net_err:
        await resolver_with(boom_find).resolve(user_token=user_jwt, principal="alice@example.com")
    assert not leaks(net_err.value)

    def boom_token(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.content.decode()}", request=request)

    with pytest.raises(DestinationError, match="token endpoint") as net_token_err:
        await resolver_with(boom_token).resolve()
    assert not leaks(net_token_err.value)
    # resolve_properties (MAIL destinations) goes through the same calls.
    with pytest.raises(DestinationError, match="returned 500") as props_err:
        await resolver_with(echo_find).resolve_properties()
    assert not leaks(props_err.value)
