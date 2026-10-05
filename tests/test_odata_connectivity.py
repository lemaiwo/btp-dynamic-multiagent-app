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
    key = "alice@example.com"
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
