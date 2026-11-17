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
