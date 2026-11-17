"""The logging model wrapper of the agent register: plain (non-streamed) calls.

Every model request made through the wrapper while the register is on gives
exactly one ``logRequest`` and one ``logResponse`` with the same
``correlationId``. The wrapper never changes the model's answer and never
raises anything of its own.
"""
import logging

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError

from agents.nexus import logged_model, register, sender, state
from agents.nexus.context import calling_agent, nexus_run
from agents.nexus.logged_model import NexusLoggedModel, wrap
from agents.nexus.register import Assignment
from tests.nexus_helpers import Recorder, register_on, scripted_model

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


@pytest.fixture
async def rec(monkeypatch):
    monkeypatch.delenv("NEXUS_SEND_PAYLOAD", raising=False)
    monkeypatch.delenv("NEXUS_FALLBACK_USE_CASE", raising=False)
    monkeypatch.delenv("AICORE_RESOURCE_GROUP", raising=False)
    r = Recorder()
    await register_on(monkeypatch, r)
    yield r
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


async def test_one_pair_per_model_request(rec):
    with nexus_run("run-1"), calling_agent("invoice-assistant"):
        await Agent(wrap(scripted_model([(10, 2)]), "gpt-4o")).run("hi")
    await sender.drain()
    req, resp = rec.of("logRequest"), rec.of("logResponse")
    assert len(req) == len(resp) == 1
    assert req[0]["body"]["correlationId"] == resp[0]["body"]["correlationId"]
    assert req[0]["body"]["agent"] == resp[0]["body"]["agent"] == "invoice-assistant"
    assert req[0]["body"]["runId"] == resp[0]["body"]["runId"] == "run-1"
    assert req[0]["body"]["modelName"] == "gpt-4o"
    assert req[0]["body"]["resourceGroup"] == "default" and "useCase" not in req[0]["body"]
    assert req[0]["body"]["requestedAt"].endswith("Z")
    assert resp[0]["body"]["respondedAt"].endswith("Z")
    assert (resp[0]["body"]["status"], resp[0]["body"]["inputTokens"],
            resp[0]["body"]["outputTokens"]) == ("COMPLETED", 10, 2)
    assert "prompt" not in req[0]["body"] and "systemPrompt" not in req[0]["body"]
    assert "response" not in resp[0]["body"]
    # Lead decision 1: modelVersion is sent on neither call.
    assert "modelVersion" not in req[0]["body"] and "modelVersion" not in resp[0]["body"]


async def test_tokens_are_per_request_not_a_running_total(rec):
    calls = iter([1, 2])

    def answer(messages, info):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        from pydantic_ai.usage import RequestUsage
        n = next(calls)
        if n == 1:
            return ModelResponse(parts=[ToolCallPart("noop", {})],
                                 usage=RequestUsage(input_tokens=10, output_tokens=2))
        return ModelResponse(parts=[TextPart("ok")],
                             usage=RequestUsage(input_tokens=30, output_tokens=5))

    from pydantic_ai.models.function import FunctionModel
    agent = Agent(wrap(FunctionModel(answer, model_name="scripted"), "gpt-4o"))

    @agent.tool_plain
    def noop() -> str:
        return "done"

    with nexus_run("run-2"):
        result = await agent.run("hi")
    await sender.drain()
    resp = rec.of("logResponse")
    tokens = [(b["body"]["inputTokens"], b["body"]["outputTokens"]) for b in resp]
    assert tokens == [(10, 2), (30, 5)]
    assert (result.usage().input_tokens, result.usage().output_tokens) == (40, 7)
    assert len({b["body"]["correlationId"] for b in resp}) == 2


async def test_off_is_straight_through(monkeypatch):
    state.reset_for_tests()
    sender.reset_for_tests()
    result = await Agent(wrap(scripted_model(), "gpt-4o")).run("hi")
    assert result.output == "ok" and sender.pending() == []


async def test_settings_are_sent(rec):
    with nexus_run("r"):
        await Agent(wrap(scripted_model(), "gpt-4o"),
                    model_settings={"temperature": 0.2, "top_p": 0.9, "max_tokens": 50}).run("hi")
    await sender.drain()
    body = rec.of("logRequest")[0]["body"]
    assert (body["temperature"], body["topP"], body["maxTokens"]) == (0.2, 0.9, 50)


async def test_settings_absent_or_not_numbers_are_left_out(rec):
    with nexus_run("r"):
        await Agent(wrap(scripted_model(), "gpt-4o"),
                    model_settings={"temperature": "hot", "max_tokens": True}).run("hi")
    await sender.drain()
    body = rec.of("logRequest")[0]["body"]
    assert not {"temperature", "topP", "maxTokens"} & set(body)


async def test_payload_only_with_switch(rec, monkeypatch):
    monkeypatch.setenv("NEXUS_SEND_PAYLOAD", "true")
    with nexus_run("r"):
        await Agent(wrap(scripted_model(), "gpt-4o"), system_prompt="be brief").run("hello there")
    await sender.drain()
    req, resp = rec.of("logRequest")[0]["body"], rec.of("logResponse")[0]["body"]
    assert req["systemPrompt"] == "be brief"
    assert req["prompt"] == "hello there"
    assert resp["response"] == "ok"


async def test_filtered_finish_reason(rec):
    with nexus_run("r"):
        await Agent(wrap(scripted_model(finish_reason="content_filter"), "gpt-4o")).run("hi")
    await sender.drain()
    assert rec.of("logResponse")[0]["body"]["status"] == "FILTERED"


async def test_failed_call_is_masked_and_reraised(rec):
    exc = RuntimeError("boom https://secret.example.test/x?token=abc")
    with pytest.raises(RuntimeError) as raised, nexus_run("r"):
        await Agent(wrap(scripted_model(fail=exc), "gpt-4o")).run("hi")
    assert raised.value is exc
    await sender.drain()
    body = rec.of("logResponse")[0]["body"]
    assert body["status"] == "FAILED" and body["errorCode"] == "RuntimeError"
    assert "secret.example.test" not in body["errorMessage"] and "abc" not in body["errorMessage"]
    assert len(body["errorMessage"]) <= 200
    assert (body["inputTokens"], body["outputTokens"]) == (0, 0)


async def test_timeout_class(rec):
    with pytest.raises(TimeoutError), nexus_run("r"):
        await Agent(wrap(scripted_model(fail=TimeoutError()), "gpt-4o")).run("hi")
    await sender.drain()
    body = rec.of("logResponse")[0]["body"]
    assert (body["status"], body["errorCode"]) == ("TIMEOUT", "TimeoutError")


async def test_http_status_code_and_body_never_sent(rec):
    exc = ModelHTTPError(429, "gpt-4o", {"error": {"message": "secret prompt echo"}})
    with pytest.raises(ModelHTTPError), nexus_run("r"):
        await Agent(wrap(scripted_model(fail=exc), "gpt-4o")).run("hi")
    await sender.drain()
    body = rec.of("logResponse")[0]["body"]
    assert (body["status"], body["errorCode"]) == ("FAILED", "http_429")
    assert "secret prompt echo" not in body["errorMessage"]


async def test_provider_content_filter_error_is_filtered(rec):
    exc = ModelHTTPError(400, "gpt-4o", {"error": {"code": "content_filter"}})
    with pytest.raises(ModelHTTPError), nexus_run("r"):
        await Agent(wrap(scripted_model(fail=exc), "gpt-4o")).run("hi")
    await sender.drain()
    body = rec.of("logResponse")[0]["body"]
    assert (body["status"], body["errorCode"]) == ("FILTERED", "http_400")


async def test_no_run_id_warns_once_per_agent(rec, caplog):
    caplog.set_level(logging.WARNING, logger="agents.nexus.logged_model")
    for _ in range(2):
        await Agent(wrap(scripted_model(), "gpt-4o")).run("hi")
    await sender.drain()
    ids = {r["body"]["runId"] for r in rec.of("logRequest")}
    assert len(ids) == 2
    warnings = [r for r in caplog.records if "without a run id" in r.getMessage()]
    assert len(warnings) == 1


async def test_cached_use_case_and_fallback(rec, monkeypatch):
    monkeypatch.setattr(register._state, "cache",
                        {"invoice-assistant": Assignment("INVOICE_QA", "rg-1", "ASSIGNED")})
    monkeypatch.setenv("NEXUS_FALLBACK_USE_CASE", "GENERAL_QA")
    monkeypatch.setattr(register._state, "fallback_read", False)  # read it again
    with nexus_run("r"):
        with calling_agent("invoice-assistant"):
            await Agent(wrap(scripted_model(), "gpt-4o")).run("hi")
        with calling_agent("other-agent"):
            await Agent(wrap(scripted_model(), "gpt-4o")).run("hi")
    await sender.drain()
    req, resp = rec.of("logRequest"), rec.of("logResponse")
    assert [b["body"]["useCase"] for b in req] == ["INVOICE_QA", "GENERAL_QA"]
    assert [b["body"]["useCase"] for b in resp] == ["INVOICE_QA", "GENERAL_QA"]


async def test_long_agent_name_and_deployment_are_cut(rec):
    with nexus_run("r"), calling_agent("  " + "a" * 80 + "  "):
        await Agent(wrap(scripted_model(), "m" * 150)).run("hi")
    await sender.drain()
    body = rec.of("logRequest")[0]["body"]
    assert body["agent"] == "a" * 64 and body["modelName"] == "m" * 120


async def test_logging_failure_never_reaches_the_run(rec, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise ValueError("enqueue exploded")
    monkeypatch.setattr(sender, "enqueue", broken)
    with nexus_run("r"):
        result = await Agent(wrap(scripted_model(), "gpt-4o")).run("hi")
    assert result.output == "ok"
    # The logRequest failed, so no orphan logResponse is sent for it.
    assert logged_model.counters()["build_failed"] == 1
    assert any("could not be built (ValueError)" in r.getMessage() for r in caplog.records)
    assert not any("exploded" in r.getMessage() for r in caplog.records)


async def test_logging_failure_keeps_the_original_exception(rec, monkeypatch):
    monkeypatch.setattr(sender, "enqueue", lambda *a, **k: (_ for _ in ()).throw(ValueError()))
    exc = RuntimeError("model down")
    with pytest.raises(RuntimeError) as raised, nexus_run("r"):
        await Agent(wrap(scripted_model(fail=exc), "gpt-4o")).run("hi")
    assert raised.value is exc


def test_wrap_returns_a_logged_model_with_the_inner_name():
    inner = scripted_model(name="scripted")
    wrapped = wrap(inner, "gpt-4o")
    assert isinstance(wrapped, NexusLoggedModel)
    assert wrapped.wrapped is inner and wrapped.model_name == "scripted"
    assert wrap(wrapped, "gpt-4o") is wrapped  # never wrapped twice


async def test_response_side_failure_keeps_the_result(rec, monkeypatch):
    real = sender.enqueue

    def only_requests(action, body, identity=None):
        if action == "logResponse":
            raise ValueError("no")
        real(action, body, identity)
    monkeypatch.setattr(sender, "enqueue", only_requests)
    with nexus_run("r"):
        result = await Agent(wrap(scripted_model([(4, 1)]), "gpt-4o")).run("hi")
    assert result.output == "ok"
    assert (result.usage().input_tokens, result.usage().output_tokens) == (4, 1)
    assert logged_model.counters()["build_failed"] == 1


async def test_empty_agent_name_is_left_out(rec):
    """An empty agent name is never sent as ``agent: ""`` (review of tasks 7-8)."""
    with nexus_run("r"), calling_agent("   "):
        await Agent(wrap(scripted_model([(1, 1)]), "gpt-4o")).run("hi")
    await sender.drain()
    for body in (rec.of("logRequest")[0]["body"], rec.of("logResponse")[0]["body"]):
        assert "agent" not in body and body["runId"] == "r"
