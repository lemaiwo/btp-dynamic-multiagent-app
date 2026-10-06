"""Final review, identity: what a destination may add to an OData request.

* A service that runs as the signed-in user on an Internet destination is
  refused unless the destination really signs in as that user (resolved for
  the user AND a user-propagating authentication type); a stored
  ``Authorization`` or ``Cookie`` never travels on a user run.
* A destination's ``URL.queries.*`` cannot add an OData system query option,
  with or without the ``$``; the caller's concurrency and session headers win
  over ``URL.headers.*`` of the same name.
* A retry of the auth layer that fails after a request left says so
  (``request_left``).

All of it for auths built by ``routed_auth`` (the OData callers) only: every
other destination user behaves as before, which the last tests pin.

No network: the destination service and SAP are MockTransports.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agents.destination import (  # noqa: E402
    Destination,
    DestinationError,
    DestinationResolver,
    DestinationServiceConfig,
)
from agents.destination_auth import (  # noqa: E402
    NotUserPropagating,
    destination_http_client,
    request_left,
)
from tests.test_odata_onpremise_auth import (  # the same doubles
    ALICE,
    BASIC,
    PATH,
    Resolver,
    World,
    as_user,
)

URL = "https://api.example.com/base"
USER_BEARER = "Bearer minted-for-the-user"


def internet(**overrides: Any) -> Resolver:
    fields: dict[str, Any] = {
        "url": URL,
        "proxy_type": "Internet",
        "location_id": "",
        "queries": {},
        "auth_type": "OAuth2JWTBearer",
        "headers": {"Authorization": USER_BEARER},
    }
    fields.update(overrides)
    return Resolver(**fields)


def plain_client(world: World, **kw: Any) -> httpx.AsyncClient:
    """The client of every OTHER built-in: no ``connectivity`` argument."""
    return destination_http_client(
        world.resolver, server_key="builtin:other", transport=world.direct.transport, **kw
    )


# ------------------------------------------------------------------------ A1


@pytest.mark.parametrize(
    "auth_type, headers",
    [
        ("BasicAuthentication", {"Authorization": BASIC}),
        ("NoAuthentication", {"Authorization": "Bearer stored"}),
        ("NoAuthentication", {"Cookie": "SAP_SESSIONID=stored"}),
        ("OAuth2ClientCredentials", {"Authorization": "Bearer app"}),
        ("", {"Authorization": BASIC}),
        # Not in the fixed text, so not admitted: the allowlist and the text
        # an admin reads are the same three types.
        ("SAMLAssertion", {"Authorization": "SAML2.0 stored"}),
        ("PrincipalPropagation", {"Authorization": "Bearer stored"}),
    ],
)
async def test_a_user_service_on_a_technical_internet_destination_is_refused(auth_type, headers):
    w = World(internet(auth_type=auth_type, headers=headers, name="S4_ODATA_TECH"))
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(NotUserPropagating) as err:
                await http.get(PATH)
    assert w.direct.requests == [] and w.proxy.requests == []
    assert isinstance(err.value, DestinationError)
    assert err.value.admin_text == (
        "destination 'S4_ODATA_TECH' does not sign in as the user: a service that runs as "
        "the signed-in user needs a user-propagating destination (OAuth2JWTBearer, "
        "OAuth2UserTokenExchange, OAuth2SAMLBearerAssertion) or, on-premise, "
        "PrincipalPropagation"
    )
    for secret in (BASIC, "stored", "Bearer app"):
        assert secret not in str(err.value) and secret not in err.value.admin_text


async def test_a_destination_not_resolved_for_the_user_is_refused():
    """The type alone is not enough: the answer must be the one the
    destination service gave for this user's token."""

    class AppLevel(Resolver):
        async def resolve(self, **kwargs: Any) -> Destination:
            kwargs["user_token"] = None
            return await super().resolve(**kwargs)

    w = World(AppLevel(**internet().fields))
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(NotUserPropagating):
                await http.get(PATH)
    assert w.direct.requests == []


async def test_a_user_propagating_destination_is_used_and_stored_credentials_stay_behind():
    w = World(
        internet(
            headers={
                "Authorization": USER_BEARER,
                "Cookie": "SAP_SESSIONID=stored",
                "X-Language": "EN",
            },
            static_headers=frozenset({"cookie", "x-language"}),
        )
    )
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.get(PATH)
    (sent,) = w.direct.requests
    assert sent.headers["authorization"] == USER_BEARER
    assert "cookie" not in sent.headers
    assert sent.headers["x-language"] == "EN"


async def test_a_stored_authorization_is_never_the_users_credential():
    """OAuth2JWTBearer on paper, but the only ``Authorization`` is a stored
    ``URL.headers.Authorization``: nothing was minted for the user."""
    w = World(
        internet(
            headers={"Authorization": "Bearer stored"},
            static_headers=frozenset({"authorization"}),
        )
    )
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(NotUserPropagating):
                await http.get(PATH)
    assert w.direct.requests == []


async def test_a_technical_service_keeps_the_stored_headers():
    w = World(
        internet(
            auth_type="NoAuthentication",
            headers={"Authorization": "Bearer stored", "Cookie": "a=b"},
            static_headers=frozenset({"authorization", "cookie"}),
        )
    )
    async with w.client() as http:
        await http.get(PATH)
    (sent,) = w.direct.requests
    assert sent.headers["authorization"] == "Bearer stored" and sent.headers["cookie"] == "a=b"


async def test_other_built_ins_keep_todays_behaviour_for_a_user_on_a_technical_destination():
    """Gmail, Outlook, Teams, Slack, Jira, MCP, the workflow http step: not
    changed by this rule (their own save-time checks apply)."""
    w = World(
        internet(
            auth_type="BasicAuthentication",
            headers={"Authorization": BASIC, "Cookie": "a=b"},
            static_headers=frozenset({"cookie"}),
        )
    )
    with as_user(*ALICE):
        async with plain_client(w, user_context=True) as http:
            await http.get(PATH)
    (sent,) = w.direct.requests
    assert sent.headers["authorization"] == BASIC and sent.headers["cookie"] == "a=b"


CONFIG = DestinationServiceConfig(
    client_id="sb-dest",
    client_secret="shh",
    token_url="https://uaa.example/oauth/token",
    api_url="https://destination.example",
)


def test_the_resolver_tells_stored_headers_from_the_minted_one():
    resolver = DestinationResolver("S4_ODATA_USER", CONFIG)
    payload = {
        "destinationConfiguration": {
            "URL": URL,
            "Authentication": "OAuth2JWTBearer",
            "URL.headers.authorization": "Bearer stored",
            "URL.headers.Cookie": "a=b",
            "URL.headers.X-Language": "EN",
        },
        "authTokens": [
            {
                "type": "Bearer",
                "value": "u",
                "http_header": {"key": "Authorization", "value": "Bearer u"},
            }
        ],
    }
    minted = resolver._destination_from(payload, per_user=True)
    assert minted.static_headers == frozenset({"cookie", "x-language"})
    # One Authorization only, the minted one, whatever case the property had.
    assert {k: v for k, v in minted.headers.items() if k.lower() == "authorization"} == {
        "Authorization": "Bearer u"
    }
    stored = resolver._destination_from({**payload, "authTokens": []}, per_user=True)
    assert stored.static_headers == frozenset({"authorization", "cookie", "x-language"})


# ------------------------------------------------------- N1: SystemUser


def real_resolver(configuration: dict[str, Any], calls: list[httpx.Request]) -> DestinationResolver:
    """The REAL resolver in front of a destination service that answers
    ``configuration`` with a minted token, as the service does for a SAML
    bearer destination -- also when ``SystemUser`` makes it mint that token
    for the fixed user instead of the caller."""

    def service(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "uaa.example":
            return httpx.Response(200, json={"access_token": "svc", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "destinationConfiguration": {"Name": "S4_ODATA_USER", "URL": URL, **configuration},
                "authTokens": [
                    {
                        "type": "Bearer",
                        "value": "minted",
                        "http_header": {"key": "Authorization", "value": "Bearer minted"},
                        "expires_in": "3600",
                    }
                ],
            },
        )

    return DestinationResolver("S4_ODATA_USER", CONFIG, transport=httpx.MockTransport(service))


@pytest.mark.parametrize("auth_type", ["OAuth2SAMLBearerAssertion", "OAuth2JWTBearer"])
async def test_a_destination_with_a_system_user_is_refused_for_a_user_service(auth_type):
    """From the destination service's JSON to the refusal: ``SystemUser``
    makes the service mint the token for that fixed user whatever user token
    is sent, so the type, ``per_user`` and a minted header all look right."""
    calls: list[httpx.Request] = []
    resolver = real_resolver(
        {"Authentication": auth_type, "ProxyType": "Internet", "SystemUser": "BATCH_USER"}, calls
    )
    w = World(resolver)  # type: ignore[arg-type]
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(NotUserPropagating) as err:
                await http.get(PATH)
    # It WAS resolved for the user, and still nothing left for the target.
    assert any(r.headers.get("x-user-token") == ALICE[0] for r in calls)
    assert w.direct.requests == [] and w.proxy.requests == []
    assert "does not sign in as the user" in err.value.admin_text
    assert "BATCH_USER" not in str(err.value) and "minted" not in str(err.value)


@pytest.mark.parametrize("system_user", [None, "", "   "])
async def test_without_a_system_user_the_same_destination_is_used(system_user):
    configuration = {"Authentication": "OAuth2SAMLBearerAssertion", "ProxyType": "Internet"}
    if system_user is not None:
        configuration["SystemUser"] = system_user
    w = World(real_resolver(configuration, []))  # type: ignore[arg-type]
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.get(PATH)
    (sent,) = w.direct.requests
    assert sent.headers["authorization"] == "Bearer minted"


def test_the_resolver_records_a_system_user_and_never_its_name():
    resolver = DestinationResolver("S4_ODATA_USER", CONFIG)
    payload = {
        "destinationConfiguration": {
            "URL": URL, "Authentication": "OAuth2SAMLBearerAssertion", "SystemUser": " BATCH_USER ",
        },
        "authTokens": [{"type": "Bearer", "value": "u"}],
    }
    resolved = resolver._destination_from(payload, per_user=True)
    assert resolved.system_user is True and "BATCH_USER" not in repr(resolved)
    del payload["destinationConfiguration"]["SystemUser"]
    assert resolver._destination_from(payload, per_user=True).system_user is False
    assert Destination(url=URL, headers={}, expires_at=0.0).system_user is False


async def test_a_technical_service_may_use_a_destination_with_a_system_user():
    w = World(real_resolver({"Authentication": "OAuth2SAMLBearerAssertion", "SystemUser": "B"}, []))  # type: ignore[arg-type]
    async with w.client() as http:
        await http.get(PATH)
    assert len(w.direct.requests) == 1


async def test_other_built_ins_are_not_held_to_the_system_user_rule():
    w = World(real_resolver({"Authentication": "OAuth2SAMLBearerAssertion", "SystemUser": "B"}, []))  # type: ignore[arg-type]
    with as_user(*ALICE):
        async with plain_client(w, user_context=True) as http:
            await http.get(PATH)
    assert len(w.direct.requests) == 1


async def test_a_401_retry_that_resolves_to_a_non_propagating_destination_sends_nothing_more():
    """The first answer signed in as the user; the admin then gave the
    destination a ``SystemUser``. The retry after the 401 must not carry
    that account's token."""

    class GetsSystemUser(Resolver):
        async def resolve(self, *, force: bool = False, **kwargs: Any) -> Destination:
            resolved = await super().resolve(force=force, **kwargs)
            if not force:
                return resolved
            return Destination(
                **{
                    **resolved.__dict__,
                    "headers": {"Authorization": "Bearer of-the-system-user"},
                    "system_user": True,
                }
            )

    w = World(GetsSystemUser(**internet().fields))
    w.direct.statuses = [401, 200]
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            with pytest.raises(NotUserPropagating) as err:
                await http.post(PATH, json={})
    (only,) = w.direct.requests
    assert only.headers["authorization"] == USER_BEARER
    assert request_left(err.value) is True


# ------------------------------------------------------------------------ A3

SYSTEM_OPTIONS = (
    "filter", "select", "expand", "orderby", "top", "skip", "count", "search", "apply",
    "format", "skiptoken", "deltatoken", "compute", "levels", "schemaversion", "index", "id",
)


async def test_odata_callers_skip_system_query_options_without_the_dollar(caplog):
    queries = {name if i % 2 else name.upper(): "x" for i, name in enumerate(SYSTEM_OPTIONS)}
    queries["Filter"] = "Secret eq 'x'"
    queries["sap-client"] = "100"
    w = World(Resolver(queries=queries))
    async with w.client() as http:
        await http.get(PATH)
    (sent,) = w.proxy.requests
    assert sent.url.query == b"sap-client=100"
    assert "Secret" not in caplog.text


async def test_other_destination_users_still_append_such_names():
    w = World(internet(auth_type="OAuth2ClientCredentials", queries={"filter": "a", "id": "7"}))
    async with plain_client(w) as http:
        await http.get(PATH)
    (sent,) = w.direct.requests
    assert sent.url.query == b"filter=a&id=7"


CALLER_WINS = (
    "If-Match", "If-None-Match", "X-CSRF-Token", "Cookie", "X-HTTP-Method",
    "X-HTTP-Method-Override",
)


# What a destination may still set when the caller sends none (N3): the
# session pair. A concurrency token or a method override never comes from a
# destination: `URL.headers.If-Match: *` would turn an update or delete
# without an etag into an unconditional one instead of 428 `etag_required`.
SESSION_HEADERS = ("X-CSRF-Token", "Cookie")
NEVER_FROM_A_DESTINATION = (
    "If-Match", "If-None-Match", "X-HTTP-Method", "X-HTTP-Method-Override",
)


async def test_the_callers_concurrency_and_session_headers_win_for_odata():
    stored = {name.upper(): "from-destination" for name in CALLER_WINS}
    w = World(Resolver(headers={"Authorization": BASIC, "X-Other": "d", **stored}, queries={}))
    async with w.client() as http:
        await http.get(PATH, headers={**{n: "from-caller" for n in CALLER_WINS}, "X-Other": "c"})
        await http.get(PATH)
    first, second = w.proxy.requests
    for name in CALLER_WINS:
        assert first.headers.get_list(name) == ["from-caller"], name
    # Without a caller value the destination's session headers still apply ...
    for name in SESSION_HEADERS:
        assert second.headers[name] == "from-destination", name
    # ... its concurrency and method headers never do.
    for name in NEVER_FROM_A_DESTINATION:
        assert name not in second.headers, name
    assert first.headers["x-other"] == "d" and second.headers["x-other"] == "d"


@pytest.mark.parametrize("method", ["PATCH", "DELETE", "POST"])
async def test_a_destination_cannot_make_a_change_unconditional(method):
    """Also on the direct path and for a signed-in user."""
    w = World(
        internet(
            headers={
                "Authorization": USER_BEARER,
                "If-Match": "*",
                "if-none-match": "*",
                "X-HTTP-Method": "DELETE",
                "x-http-method-override": "MERGE",
            },
            static_headers=frozenset(
                {"if-match", "if-none-match", "x-http-method", "x-http-method-override"}
            ),
        )
    )
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.request(method, PATH)
    (sent,) = w.direct.requests
    for name in NEVER_FROM_A_DESTINATION:
        assert name not in sent.headers, name


async def test_on_a_retry_the_destinations_own_header_is_not_taken_for_the_callers():
    """Attempt one set the destination's ``Cookie`` (the caller had none);
    the retry resolves again and must apply the NEW value."""

    class Changing(Resolver):
        async def resolve(self, **kwargs: Any) -> Destination:
            resolved = await super().resolve(**kwargs)
            headers = {**resolved.headers, "Cookie": f"v={len(self.calls)}"}
            return Destination(**{**resolved.__dict__, "headers": headers})

    w = World(Changing(queries={}))
    w.proxy.statuses = [401, 200]
    async with w.client() as http:
        await http.get(PATH)
    first, second = w.proxy.requests
    assert (first.headers["cookie"], second.headers["cookie"]) == ("v=1", "v=2")


async def test_for_other_built_ins_a_destination_still_sets_those_headers():
    w = World(
        internet(
            auth_type="OAuth2ClientCredentials",
            headers={"Authorization": BASIC, "If-Match": "*", "X-HTTP-Method": "MERGE"},
        )
    )
    async with plain_client(w) as http:
        await http.get(PATH)
    (sent,) = w.direct.requests
    assert sent.headers["if-match"] == "*" and sent.headers["x-http-method"] == "MERGE"


# ------------------------------------------------- N6: logon by HTTP fields

LOGON_FIELDS = {"sap-user": "STORED_USER", "SAP-Password": "stored-pw", "MYSAPSSO2": "stored-sso"}


def _no_logon_field(request: httpx.Request) -> None:
    assert request.url.query == b"sap-client=100"
    for name in LOGON_FIELDS:
        assert name not in request.headers, name
    for value in LOGON_FIELDS.values():
        assert value not in str(request.url) and value not in str(request.headers.raw)


async def test_a_user_run_never_sends_a_destinations_logon_fields_on_the_direct_path():
    """A target that accepts logon by ``sap-user``/``sap-password`` or an
    SSO2 ticket would run as that stored user, not the signed-in one."""
    w = World(
        internet(
            headers={"Authorization": USER_BEARER, **LOGON_FIELDS},
            static_headers=frozenset(name.lower() for name in LOGON_FIELDS),
            queries={**LOGON_FIELDS, "sap-client": "100"},
        )
    )
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.get(PATH)
    (sent,) = w.direct.requests
    _no_logon_field(sent)
    assert sent.headers["authorization"] == USER_BEARER


async def test_a_user_run_never_sends_a_destinations_logon_fields_through_the_proxy():
    w = World(
        Resolver(
            name="S4_ODATA_USER",
            auth_type="PrincipalPropagation",
            headers=dict(LOGON_FIELDS),
            queries={**LOGON_FIELDS, "sap-client": "100"},
        )
    )
    with as_user(*ALICE):
        async with w.client(user_context=True) as http:
            await http.get(PATH)
    (sent,) = w.proxy.requests
    _no_logon_field(sent)


async def test_a_technical_service_and_other_built_ins_keep_such_properties():
    """The stored user IS the identity of a technical service; the other
    destination users are not changed."""
    fields = {
        "auth_type": "NoAuthentication",
        "headers": {"Authorization": BASIC, "sap-user": "STORED_USER"},
        "queries": {"sap-user": "STORED_USER"},
    }
    w = World(internet(**fields))
    async with w.client() as http:
        await http.get(PATH)
    with as_user(*ALICE):
        async with plain_client(w, user_context=True) as http:
            await http.get(PATH)
    for sent in w.direct.requests:
        assert sent.headers["sap-user"] == "STORED_USER"
        assert sent.url.query == b"sap-user=STORED_USER"
    assert len(w.direct.requests) == 2


async def test_for_other_built_ins_the_destination_header_still_wins():
    w = World(
        internet(
            auth_type="OAuth2ClientCredentials",
            headers={"Authorization": BASIC, "Cookie": "d=1"},
        )
    )
    async with plain_client(w) as http:
        await http.get(PATH, headers={"Cookie": "c=1"})
    (sent,) = w.direct.requests
    assert sent.headers["cookie"] == "d=1"


# ------------------------------------------------------------------------ A5


class FailsOnForce(Resolver):
    async def resolve(self, *, force: bool = False, **kwargs: Any) -> Destination:
        if force:
            raise DestinationError("destination service returned 503 for 'S4_ODATA_TECH'")
        return await super().resolve(force=force, **kwargs)


async def test_a_failed_re_resolve_after_a_401_says_that_a_request_left():
    w = World(FailsOnForce())
    w.proxy.statuses = [401]
    async with w.client() as http:
        with pytest.raises(DestinationError) as err:
            await http.post(PATH, json={})
    assert len(w.proxy.requests) == 1 and request_left(err.value) is True


async def test_a_failed_token_renewal_after_a_407_says_that_a_request_left():
    w = World()
    w.proxy.statuses = [407]

    calls = {"n": 0}
    real = w.tokens.app_token

    async def app_token() -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise DestinationError("connectivity service token request returned 500")
        return await real()

    w.tokens.app_token = app_token  # type: ignore[method-assign]
    async with w.client() as http:
        with pytest.raises(DestinationError) as err:
            await http.post(PATH, json={})
    assert len(w.proxy.requests) == 1 and request_left(err.value) is True


async def test_a_refusal_before_anything_was_sent_is_not_marked():
    w = World(Resolver(url="https://s4.internal:44300"))  # OnPremise over https: refused
    async with w.client() as http:
        with pytest.raises(DestinationError) as err:
            await http.post(PATH, json={})
    assert w.proxy.requests == [] and w.direct.requests == []
    assert request_left(err.value) is False
    assert request_left(ValueError("x")) is False
