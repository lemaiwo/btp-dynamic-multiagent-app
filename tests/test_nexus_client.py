"""The agent-register HTTP client: technical identity, fail-closed on errors."""
import logging

import httpx

from agents.destination import DestinationError
from agents.nexus.client import RegisterClient
from tests.nexus_helpers import REGISTER_URL, FakeResolver, Recorder


async def test_post_goes_to_logging_action_with_technical_token():
    rec = Recorder()
    tech = FakeResolver()
    out = await RegisterClient(tech, timeout=5, transport=rec.transport()).post(
        "logRequest", {"correlationId": "c"})
    assert out.ok and out.status == 200 and out.failure is None
    assert rec.requests[0]["path"] == "/logging/logRequest"
    assert rec.requests[0]["auth"] == "Bearer tech-token"
    assert rec.requests[0]["body"] == {"correlationId": "c"}


async def test_url_already_ending_in_logging_is_not_doubled():
    rec = Recorder()
    tech = FakeResolver(url=REGISTER_URL + "/logging/")
    out = await RegisterClient(tech, timeout=5, transport=rec.transport()).post("logRequest", {})
    assert out.ok
    assert rec.requests[0]["path"] == "/logging/logRequest"


async def test_401_invalidates_once_for_the_next_send_and_does_not_retry():
    rec = Recorder(statuses={"logRequest": 401})
    tech = FakeResolver()
    out = await RegisterClient(tech, timeout=5, transport=rec.transport()).post("logRequest", {})
    assert not out.ok and out.failure == "http_401" and out.status == 401
    assert tech.invalidated == [None]
    assert len(rec.requests) == 1


async def test_redirect_is_a_failure_not_followed():
    seen: list[str] = []

    async def handler(request: httpx.Request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"Location": REGISTER_URL + "/elsewhere"})

    out = await RegisterClient(FakeResolver(), timeout=5,
                               transport=httpx.MockTransport(handler)).post("logRequest", {})
    assert not out.ok and out.failure == "http_302"
    assert seen == ["/logging/logRequest"]


async def test_error_body_is_never_read_and_nothing_secret_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    read: list[bool] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(True)
            yield b'{"error": "leak-me"}'

    async def handler(request: httpx.Request):
        return httpx.Response(500, stream=Body())

    out = await RegisterClient(FakeResolver(token="tech-token"), timeout=5,
                               transport=httpx.MockTransport(handler)).post("logRequest", {})
    assert not out.ok and out.failure == "http_500"
    assert read == []
    assert "leak-me" not in caplog.text and "tech-token" not in caplog.text
    assert "register.example.test" not in caplog.text


async def test_destination_failure_is_an_outcome_not_an_exception(caplog):
    caplog.set_level(logging.DEBUG)
    out = await RegisterClient(FakeResolver(fail=DestinationError("secret-text")), timeout=5,
                               transport=Recorder().transport()).post("logRequest", {})
    assert not out.ok and out.failure == "DestinationError" and out.status is None
    assert "secret-text" not in caplog.text


async def test_register_returns_value_list():
    rec = Recorder(answers={"registerAgents": {"value": [
        {"name": "a", "useCase": "U", "resourceGroup": None, "status": "ASSIGNED"}]}})
    value = await RegisterClient(FakeResolver(), timeout=5, transport=rec.transport()).register(
        [{"name": "a"}])
    assert value[0]["useCase"] == "U"
    assert rec.of("registerAgents")[0]["body"] == {"agents": [{"name": "a"}]}


async def test_register_answers_none_for_an_unusable_answer():
    for answer, status in (({"value": "x"}, 200), ({"value": [1]}, 200), ([], 200),
                           ({"value": []}, 400)):
        rec = Recorder(statuses={"registerAgents": status}, answers={"registerAgents": answer})
        client = RegisterClient(FakeResolver(), timeout=5, transport=rec.transport())
        assert await client.register([{"name": "a"}]) is None


async def test_timeout_is_failure_class():
    rec = Recorder(hang=True)
    out = await RegisterClient(FakeResolver(), timeout=0.05, transport=rec.transport()).post(
        "logRequest", {})
    assert not out.ok and "Timeout" in out.failure


# --- Task 9: header filter, no cookie jar, the user destination ------------------------------

class _HeaderResolver(FakeResolver):
    """A destination whose stored headers try to steer the request."""

    async def resolve(self, *, force=False, user_token=None, principal=None):
        dest = await super().resolve(force=force, user_token=user_token, principal=principal)
        dest.headers.update({"Host": "elsewhere.example.test", "content-length": "1",
                             "Content-Type": "text/plain", "Transfer-Encoding": "chunked",
                             "COOKIE": "sid=stolen", "X-Tenant": "kept"})
        return dest


async def test_stored_transport_headers_are_not_sent():
    rec = Recorder()
    client = RegisterClient(_HeaderResolver(), timeout=5, transport=rec.transport())
    out = await client.post("logRequest", {"correlationId": "c"})
    assert out.ok
    headers = {k.lower(): v for k, v in rec.requests[0]["headers"].items()}
    assert headers["host"] == "register.example.test"
    assert headers["content-type"] == "application/json"
    assert "cookie" not in headers and headers.get("transfer-encoding") != "chunked"
    assert headers["content-length"] != "1"
    assert headers["x-tenant"] == "kept" and headers["authorization"] == "Bearer tech-token"
    assert rec.requests[0]["body"] == {"correlationId": "c"}


async def test_set_cookie_is_not_replayed():
    rec = Recorder(set_cookie="JSESSIONID=abc; Path=/")
    client = RegisterClient(FakeResolver(), timeout=5, transport=rec.transport())
    assert (await client.post("logRequest", {})).ok
    assert (await client.post("logRequest", {})).ok
    assert "cookie" not in {k.lower() for k in rec.requests[1]["headers"]}
    await client.aclose()


def _identity():
    from agents.nexus.context import Identity
    return Identity(jwt="user.jwt.value", principal="alice")


async def test_user_destination_without_authorization_falls_back():
    class NoAuth(FakeResolver):
        async def resolve(self, **kw):
            dest = await super().resolve(**kw)
            dest.headers.clear()
            return dest

    rec = Recorder()
    user = NoAuth("NEXUS_AI_PLATFORM_USER")
    client = RegisterClient(FakeResolver(), user, timeout=5, transport=rec.transport())
    out = await client.post("logRequest", {"correlationId": "c"}, identity=_identity())
    assert out.ok and out.user_fallback and out.sent_as == "technical"
    assert rec.requests[0]["auth"] == "Bearer tech-token"


async def test_stored_authorization_of_the_user_destination_falls_back():
    class Stored(FakeResolver):
        async def resolve(self, **kw):
            import dataclasses
            dest = await super().resolve(**kw)
            return dataclasses.replace(dest, static_headers=frozenset({"authorization"}))

    rec = Recorder()
    client = RegisterClient(FakeResolver(), Stored("U", token="stored"), timeout=5,
                            transport=rec.transport())
    out = await client.post("logRequest", {"correlationId": "c"}, identity=_identity())
    assert out.user_fallback and rec.requests[0]["auth"] == "Bearer tech-token"


async def test_401_under_the_user_invalidates_that_user_only():
    rec = Recorder(statuses={"logRequest": 401})
    tech, user = FakeResolver(), FakeResolver("U", token="user-token")
    client = RegisterClient(tech, user, timeout=5, transport=rec.transport())
    out = await client.post("logRequest", {"correlationId": "c"}, identity=_identity())
    assert not out.ok and out.status == 401 and out.sent_as == "user"
    assert rec.requests[0]["auth"] == "Bearer user-token"
    assert user.invalidated == ["alice"] and tech.invalidated == []


async def test_register_agents_ignores_an_identity():
    rec = Recorder()
    user = FakeResolver("U", token="user-token")
    client = RegisterClient(FakeResolver(), user, timeout=5, transport=rec.transport())
    out = await client.post("registerAgents", {"agents": []}, identity=_identity())
    assert out.ok and rec.requests[0]["auth"] == "Bearer tech-token" and user.calls == []


async def _user_call(user):
    rec = Recorder()
    client = RegisterClient(FakeResolver(), user, timeout=5, transport=rec.transport())
    out = await client.post("logRequest", {"correlationId": "c"}, identity=_identity())
    return out, rec.requests[0]["auth"]


async def test_basic_user_destination_falls_back():
    out, auth = await _user_call(FakeResolver("U", token="basic",
                                              user_auth_type="BasicAuthentication"))
    assert out.user_fallback and out.sent_as == "technical" and auth == "Bearer tech-token"


async def test_client_credentials_user_destination_falls_back():
    out, auth = await _user_call(FakeResolver("U", token="cc",
                                              user_auth_type="OAuth2ClientCredentials"))
    assert out.user_fallback and auth == "Bearer tech-token"


async def test_system_user_destination_falls_back():
    out, auth = await _user_call(FakeResolver("U", token="fixed", system_user=True))
    assert out.user_fallback and out.sent_as == "technical" and auth == "Bearer tech-token"


async def test_destination_not_resolved_for_the_user_falls_back():
    out, auth = await _user_call(FakeResolver("U", token="app", marks_per_user=False))
    assert out.user_fallback and auth == "Bearer tech-token"


async def test_user_propagating_destination_goes_as_the_user():
    for kind in ("OAuth2JWTBearer", "OAuth2UserTokenExchange", "OAuth2SAMLBearerAssertion"):
        out, auth = await _user_call(FakeResolver("U", token="u", user_auth_type=kind))
        assert out.sent_as == "user" and not out.user_fallback and auth == "Bearer u"


async def test_expired_kept_destinations_are_dropped():
    rec = Recorder()
    user = FakeResolver("U", token="u", ttl=-1.0)          # already expired when kept
    client = RegisterClient(FakeResolver(), user, timeout=5, transport=rec.transport())
    for cid in ("c-1", "c-2"):
        assert (await client.post("logRequest", {"correlationId": cid},
                                  identity=_identity())).ok
    user.ttl = 600.0
    assert (await client.post("logRequest", {"correlationId": "c-3"},
                              identity=_identity())).ok
    assert list(client._pair_destinations) == ["c-3"]
