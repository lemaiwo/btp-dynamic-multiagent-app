"""AI Core observability headers labelled with the register's correlation id.

While the agent register is on, every model call to an AI Core deployment
runs with ``agents.model_call_headers.current_call_headers`` holding the two
headers that make AI Core keep a metadata record labelled with the SAME
``correlationId`` as the call's ``logRequest`` / ``logResponse``. A direct
provider (``openai:`` / ``anthropic:``) gets no header. Nothing here may fail
a model call, and while the register is off nothing is set.
"""
import asyncio
import contextvars

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.model_call_headers import current_call_headers, sending
from agents.nexus import logged_model, observability, register, sender, state
from agents.nexus.context import calling_agent, nexus_run
from agents.nexus.logged_model import wrap
from agents.nexus.register import Assignment
from tests.nexus_helpers import Recorder, register_on

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

MODE = "ai-inference-observability-persistence-mode"
LABELS = "ai-inference-observability-labels"


# --- label values ----------------------------------------------------------

@pytest.mark.parametrize("text", ["", "a" * 65, "a b", "a_b", "café", "١", None, 5])
def test_label_value_refused(text):
    assert observability.label_value(text) is None


@pytest.mark.parametrize("text", ["a" * 64, "a-b.c/d", "X", "0.9"])
def test_label_value_accepted_unchanged(text):
    assert observability.label_value(text) == text


# --- header text -----------------------------------------------------------

def test_headers_with_correlation_only():
    assert observability.headers_for("c-1", None, None) == {
        MODE: "metadata",
        LABELS: "ext.ai.sap.com/correlation=c-1",
    }


def test_headers_with_landscape_and_use_case():
    assert observability.headers_for("c-1", "UC-7", "acc")[LABELS] == (
        "ext.ai.sap.com/correlation=c-1,ext.ai.sap.com/landscape=acc,"
        "ext.ai.sap.com/use-case=UC-7")


def test_headers_leave_out_a_refused_label():
    labels = observability.headers_for("c-1", "use case", "a_b")[LABELS]
    assert labels == "ext.ai.sap.com/correlation=c-1"


def test_headers_of_a_refused_correlation_id_are_empty():
    assert observability.headers_for("bad id", "UC", "acc") == {}


# --- the contextvar --------------------------------------------------------

def test_sending_sets_and_resets():
    assert current_call_headers.get() is None
    with sending({"a": "b"}):
        assert current_call_headers.get() == {"a": "b"}
    assert current_call_headers.get() is None


@pytest.mark.parametrize("headers", [None, {}])
def test_sending_nothing_sets_nothing(headers):
    seen = []
    with sending(headers):
        seen.append(current_call_headers.get())
    assert seen == [None]


@pytest.mark.parametrize("headers", [None, {}])
def test_sending_nothing_hides_an_outer_binding(headers):
    # A direct provider's call made while an AI Core call is still open must
    # not go out with that call's headers.
    with sending({"a": "b"}):
        with sending(headers):
            assert current_call_headers.get() is None
        assert current_call_headers.get() == {"a": "b"}
    assert current_call_headers.get() is None


def test_sending_closed_in_another_context_does_not_raise():
    cm = sending({"a": "b"})
    cm.__enter__()
    other = contextvars.copy_context()
    other.run(cm.__exit__, None, None, None)   # a token of another Context: ValueError inside
    assert other.run(current_call_headers.get) is None
    current_call_headers.set(None)             # this test's own context


# --- the wrapper -----------------------------------------------------------

@pytest.fixture
async def rec(monkeypatch):
    monkeypatch.delenv("NEXUS_SEND_PAYLOAD", raising=False)
    monkeypatch.delenv("NEXUS_FALLBACK_USE_CASE", raising=False)
    monkeypatch.delenv("NEXUS_LANDSCAPE", raising=False)
    monkeypatch.delenv("AICORE_RESOURCE_GROUP", raising=False)
    r = Recorder()
    await register_on(monkeypatch, r)
    yield r
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


def _seeing(seen, *, fail=None):
    def answer(messages, info):
        seen.append(current_call_headers.get())
        if fail is not None:
            raise fail
        return ModelResponse(parts=[TextPart("ok")],
                             usage=RequestUsage(input_tokens=1, output_tokens=1))
    return FunctionModel(answer, model_name="scripted")


def _streaming_seeing(seen, *chunks, gate=None, fail=None):
    async def stream(messages, info):
        seen.append(current_call_headers.get())
        for i, chunk in enumerate(chunks):
            if i and gate is not None:
                await gate.wait()
            yield chunk
        if fail is not None:
            raise fail
    return FunctionModel(stream_function=stream, model_name="scripted")


def _correlation(headers):
    labels = dict(part.split("=", 1) for part in headers[LABELS].split(","))
    return labels["ext.ai.sap.com/correlation"]


async def test_request_headers_carry_the_logged_correlation_id(rec):
    seen = []
    with nexus_run("r"):
        await Agent(wrap(_seeing(seen), "gpt-4o")).run("hi")
    await sender.drain()
    assert len(seen) == 1 and seen[0][MODE] == "metadata"
    cid = _correlation(seen[0])
    assert rec.of("logRequest")[0]["body"]["correlationId"] == cid
    assert rec.of("logResponse")[0]["body"]["correlationId"] == cid
    assert current_call_headers.get() is None


async def test_stream_headers_carry_the_logged_correlation_id(rec):
    seen = []
    model = wrap(_streaming_seeing(seen, "o", "k"), "gpt-4o")
    with nexus_run("r"):
        async with Agent(model).run_stream("hi") as run:
            assert await run.get_output() == "ok"
    await sender.drain()
    cid = _correlation(seen[0])
    assert rec.of("logRequest")[0]["body"]["correlationId"] == cid
    assert rec.of("logResponse")[0]["body"]["correlationId"] == cid
    assert current_call_headers.get() is None


async def test_landscape_and_use_case_in_the_labels(rec, monkeypatch):
    state.set_landscape("acc")
    monkeypatch.setattr(register._state, "cache",
                        {"billing": Assignment("UC-1", "rg", "ASSIGNED")})
    seen = []
    with nexus_run("r"), calling_agent("billing"):
        await Agent(wrap(_seeing(seen), "gpt-4o")).run("hi")
    assert seen[0][LABELS].endswith(
        ",ext.ai.sap.com/landscape=acc,ext.ai.sap.com/use-case=UC-1")


async def test_none_after_a_model_error(rec):
    seen = []
    with pytest.raises(RuntimeError), nexus_run("r"):
        await Agent(wrap(_seeing(seen, fail=RuntimeError("x")), "gpt-4o")).run("hi")
    assert seen[0] is not None
    assert current_call_headers.get() is None


async def test_none_after_a_failing_stream(rec):
    seen = []
    model = wrap(_streaming_seeing(seen, "o", "k", fail=RuntimeError("x")), "gpt-4o")
    with pytest.raises(RuntimeError), nexus_run("r"):
        async with Agent(model).run_stream("hi") as run:
            await run.get_output()
    assert seen[0] is not None
    assert current_call_headers.get() is None


async def test_none_after_an_abandoned_stream(rec):
    seen = []
    model = wrap(_streaming_seeing(seen, "o", "k", "!"), "gpt-4o")
    with nexus_run("r"):
        async with model.request_stream([ModelRequest.user_text_prompt("hi")], None,
                                        ModelRequestParameters()) as streamed:
            async for _ in streamed:
                break
    assert seen[0] is not None
    assert current_call_headers.get() is None


async def test_none_after_a_cancelled_stream(rec):
    seen, after = [], []
    gate = asyncio.Event()
    model = wrap(_streaming_seeing(seen, "o", "k", gate=gate), "gpt-4o")

    async def consume():
        try:
            with nexus_run("r"):
                async with Agent(model).run_stream("hi") as run:
                    async for _ in run.stream_text():
                        pass
        finally:
            after.append(current_call_headers.get())

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert seen[0] is not None and after == [None]
    assert current_call_headers.get() is None


@pytest.mark.parametrize("name", ["openai:gpt-4o", "anthropic:x"])
async def test_direct_provider_gets_no_header_and_no_resource_group(rec, name):
    seen = []
    with nexus_run("r"):
        await Agent(wrap(_seeing(seen), name)).run("hi")
    await sender.drain()
    assert seen == [None]
    body = rec.of("logRequest")[0]["body"]
    assert "resourceGroup" not in body and body["modelName"] == name


async def test_ai_core_name_keeps_the_resource_group(rec):
    with nexus_run("r"):
        await Agent(wrap(_seeing([]), "gpt-4o")).run("hi")
    await sender.drain()
    assert rec.of("logRequest")[0]["body"]["resourceGroup"] == "default"


async def test_headers_sent_when_the_log_request_failed(rec, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("no")
    monkeypatch.setattr(sender, "enqueue", broken)
    seen = []
    with nexus_run("r"):
        result = await Agent(wrap(_seeing(seen), "gpt-4o")).run("hi")
    assert result.output == "ok"
    assert seen[0] is not None and seen[0][MODE] == "metadata"


async def test_headers_for_raising_never_fails_the_call(rec, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no")
    monkeypatch.setattr(observability, "headers_for", broken)
    seen = []
    with nexus_run("r"):
        result = await Agent(wrap(_seeing(seen), "gpt-4o")).run("hi")
    await sender.drain()
    assert result.output == "ok" and seen == [None]
    assert len(rec.of("logResponse")) == 1
    assert logged_model.counters()["build_failed"] == 1


async def test_off_sets_nothing():
    state.reset_for_tests()
    sender.reset_for_tests()
    seen = []
    await Agent(wrap(_seeing(seen), "gpt-4o")).run("hi")
    async with Agent(wrap(_streaming_seeing(seen, "o", "k"), "gpt-4o")).run_stream("hi") as run:
        await run.get_output()
    assert seen == [None, None]
    assert sender.pending() == []
