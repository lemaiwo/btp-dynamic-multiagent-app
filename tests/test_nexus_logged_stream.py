"""The logging model wrapper of the agent register: streamed calls.

A ``StreamedResponse`` has no completion flag, so the wrapper tracks it
itself: a stream read to its end is COMPLETED with its own usage; one that
was cancelled or left before its end is FAILED with ``cancelled`` and the
tokens counted so far.
"""
import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from agents.nexus import logged_model, register, sender, state
from agents.nexus.context import nexus_run
from agents.nexus.logged_model import wrap
from tests.nexus_helpers import Recorder, register_on

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


@pytest.fixture
async def rec(monkeypatch):
    monkeypatch.delenv("NEXUS_SEND_PAYLOAD", raising=False)
    monkeypatch.delenv("NEXUS_FALLBACK_USE_CASE", raising=False)
    r = Recorder()
    await register_on(monkeypatch, r)
    yield r
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


def _streaming(*chunks, gate=None, fail=None):
    async def stream(messages, info):
        for i, chunk in enumerate(chunks):
            if i and gate is not None:
                await gate.wait()
            yield chunk
        if fail is not None:
            raise fail
    return wrap(FunctionModel(stream_function=stream, model_name="scripted"), "gpt-4o")


async def test_streamed_run_logs_one_pair_with_final_usage(rec):
    with nexus_run("run-s"):
        async with Agent(_streaming("o", "k")).run_stream("hi") as run:
            assert await run.get_output() == "ok"
            expected = run.usage()
    await sender.drain()
    req, resp = rec.of("logRequest"), rec.of("logResponse")
    assert len(req) == len(resp) == 1
    body = resp[0]["body"]
    assert body["correlationId"] == req[0]["body"]["correlationId"]
    assert body["status"] == "COMPLETED" and body["runId"] == "run-s"
    assert body["inputTokens"] == expected.input_tokens
    assert body["outputTokens"] == expected.output_tokens
    assert expected.output_tokens > 0
    assert "modelVersion" not in body and "errorCode" not in body


async def test_streamed_payload_only_with_switch(rec, monkeypatch):
    state.set_send_payload(True)          # as the lifecycle sets it from NEXUS_SEND_PAYLOAD
    with nexus_run("r"):
        async with Agent(_streaming("o", "k")).run_stream("hello") as run:
            await run.get_output()
    await sender.drain()
    assert rec.of("logRequest")[0]["body"]["prompt"] == "hello"
    assert rec.of("logResponse")[0]["body"]["response"] == "ok"


async def test_cancelled_stream_is_failed_cancelled(rec):
    gate = asyncio.Event()
    model = _streaming("o", "k", gate=gate)

    async def consume():
        with nexus_run("r"):
            async with Agent(model).run_stream("hi") as run:
                async for _ in run.stream_text():
                    pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)  # a TimeoutError here would mean the run hung
    await sender.drain()
    resp = rec.of("logResponse")
    assert len(resp) == 1
    body = resp[0]["body"]
    assert (body["status"], body["errorCode"]) == ("FAILED", "cancelled")
    assert body["outputTokens"] >= 0 and "errorMessage" in body


async def test_abandoned_stream_is_failed_cancelled(rec):
    """A stream left before its end (no error, no cancel) is not COMPLETED."""
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.models import ModelRequestParameters

    model = _streaming("o", "k", "!")
    with nexus_run("r"):
        async with model.request_stream([ModelRequest.user_text_prompt("hi")], None,
                                        ModelRequestParameters()) as streamed:
            async for _ in streamed:
                break
    await sender.drain()
    resp = rec.of("logResponse")
    assert len(resp) == 1
    assert (resp[0]["body"]["status"], resp[0]["body"]["errorCode"]) == ("FAILED", "cancelled")


async def test_stream_read_to_its_end_directly_is_completed(rec):
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.models import ModelRequestParameters

    model = _streaming("o", "k")
    with nexus_run("r"):
        async with model.request_stream([ModelRequest.user_text_prompt("hi")], None,
                                        ModelRequestParameters()) as streamed:
            async for _ in streamed:
                pass
            usage = streamed.usage()
    await sender.drain()
    body = rec.of("logResponse")[0]["body"]
    assert body["status"] == "COMPLETED"
    assert (body["inputTokens"], body["outputTokens"]) == (usage.input_tokens, usage.output_tokens)


async def test_failure_inside_stream(rec):
    exc = RuntimeError("stream broke https://secret.example.test/?token=abc")
    with pytest.raises(RuntimeError) as raised, nexus_run("r"):
        async with Agent(_streaming("o", "k", fail=exc)).run_stream("hi") as run:
            await run.get_output()
    assert raised.value is exc
    await sender.drain()
    body = rec.of("logResponse")[0]["body"]
    assert (body["status"], body["errorCode"]) == ("FAILED", "RuntimeError")
    assert "secret.example.test" not in body["errorMessage"]
    assert "abc" not in body["errorMessage"]


async def test_off_stream_is_straight_through():
    state.reset_for_tests()
    sender.reset_for_tests()
    async with Agent(_streaming("o", "k")).run_stream("hi") as run:
        assert await run.get_output() == "ok"
    assert sender.pending() == []


async def test_logging_failure_in_a_stream_keeps_the_output(rec, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("no")
    monkeypatch.setattr(sender, "enqueue", broken)
    with nexus_run("r"):
        async with Agent(_streaming("o", "k")).run_stream("hi") as run:
            assert await run.get_output() == "ok"
    assert logged_model.counters()["build_failed"] == 1


async def test_run_with_event_handler_and_a_tool_logs_completed_pairs(rec):
    """The web chat's shape: ``run(event_stream_handler=...)`` streams every request."""
    from pydantic_ai.models.function import DeltaToolCall

    calls = iter([1, 2])

    async def stream(messages, info):
        if next(calls) == 1:
            yield {0: DeltaToolCall(name="noop", json_args="{}", tool_call_id="c1")}
        else:
            yield "o"
            yield "k"

    agent = Agent(wrap(FunctionModel(stream_function=stream, model_name="scripted"), "gpt-4o"))

    @agent.tool_plain
    def noop() -> str:
        return "done"

    async def handler(ctx, events):
        async for _ in events:
            pass

    with nexus_run("r"):
        result = await agent.run("hi", event_stream_handler=handler)
    assert result.output == "ok"
    await sender.drain()
    resp = rec.of("logResponse")
    assert [b["body"]["status"] for b in resp] == ["COMPLETED", "COMPLETED"]
    assert sum(b["body"]["outputTokens"] for b in resp) == result.usage().output_tokens
    assert sum(b["body"]["inputTokens"] for b in resp) == result.usage().input_tokens


def test_streamed_response_still_has_the_event_iterator():
    """The tracking relies on a private attribute: a pydantic-ai bump that drops it fails here."""
    import dataclasses

    from pydantic_ai.models import StreamedResponse

    assert "_event_iterator" in {f.name for f in dataclasses.fields(StreamedResponse)}


async def test_tracking_verified_against_aiter(rec, monkeypatch, caplog):
    """A version whose ``__aiter__`` does not hand out ``_event_iterator``: tracking off,
    one WARNING per process, counted, and a normal exit is COMPLETED (never a false FAILED)."""
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.models import ModelRequestParameters, StreamedResponse

    original = StreamedResponse.__aiter__

    def detached(self):
        inner = original(self)

        async def passthrough():
            async for event in inner:
                yield event
        return passthrough()

    monkeypatch.setattr(StreamedResponse, "__aiter__", detached)
    caplog.set_level("WARNING", logger="agents.nexus.logged_model")
    for _ in range(2):
        with nexus_run("r"):
            async with _streaming("o", "k", "!").request_stream(
                    [ModelRequest.user_text_prompt("hi")], None,
                    ModelRequestParameters()) as streamed:
                async for _ in streamed:
                    break
    await sender.drain()
    assert [b["body"]["status"] for b in rec.of("logResponse")] == ["COMPLETED", "COMPLETED"]
    assert logged_model.counters()["untracked_streams"] == 2
    warnings = [r for r in caplog.records if "not tracked" in r.getMessage()]
    assert len(warnings) == 1


def test_stream_without_the_attribute_is_untracked(caplog):
    from types import SimpleNamespace

    logged_model.reset_for_tests()
    tracker = logged_model._Tracker()
    logged_model._track(SimpleNamespace(), tracker)
    assert tracker.tracking is False
    assert logged_model.counters()["untracked_streams"] == 1
    logged_model.reset_for_tests()
