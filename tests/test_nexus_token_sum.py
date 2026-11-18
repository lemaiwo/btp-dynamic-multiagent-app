"""The tokens the register is told add up to the run's own usage.

An orchestrator delegates to a specialist (the registry's delegation tool),
and the specialist runs a deep sub-agent (``task``); each runs on its own
scripted model. The orchestrator's requests are plain, with distinct token
counts per request; the specialist and its sub-agent are streamed (the
delegation tool runs the specialist with an event stream handler), so their
counts are pydantic-ai's estimate, which differs with the text streamed.
Over all ``logResponse`` bodies of the run: the sum of ``inputTokens`` and of
``outputTokens`` equals ``result.usage()`` of the run, and there is one
``logResponse`` per request ``result.usage()`` counted. The same holds
through a real entry point, the web chat, against the usage part it stores.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage

from tests.testdb import use_test_database

use_test_database()

from agents.db import ChatMessage, SessionLocal, delegation_tool_name  # noqa: E402
from agents.deep import DeepConfig, deep_toolset  # noqa: E402
from agents.nexus import logged_model, register, sender, state  # noqa: E402
from agents.nexus.context import nexus_run  # noqa: E402
from agents.nexus.logged_model import wrap  # noqa: E402
from agents.registry import _attach_delegation_tool  # noqa: E402
from tests import nexus_entry_drivers as drivers  # noqa: E402
from tests.nexus_entry_drivers import (  # noqa: E402, F401
    SUB_TASK,
    answered,
    entry_env,
    first_prompt,
)
from tests.nexus_helpers import Recorder, register_on  # noqa: E402

SPECIALIST = "invoice-assistant"
ORCH_USAGES = [(11, 3), (13, 5), (17, 7)]


@pytest.fixture
async def rec(monkeypatch, entry_env):  # noqa: F811
    r = Recorder()
    await register_on(monkeypatch, r)
    yield r
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


def orchestrator_model() -> FunctionModel:
    """Plain requests: delegate once, then answer; distinct usage per request."""
    usages = list(ORCH_USAGES)
    tool = delegation_tool_name(SPECIALIST)

    def respond(messages, info):
        i, o = usages.pop(0)
        usage = RequestUsage(input_tokens=i, output_tokens=o)
        if answered(messages):
            return ModelResponse(parts=[TextPart("all done")], usage=usage)
        return ModelResponse(parts=[ToolCallPart(tool, {"query": "q"}, tool_call_id="d1")],
                             usage=usage)

    async def stream(messages, info):  # the web chat streams the orchestrator too
        if answered(messages):
            yield "all done"
        else:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps({"query": "q"}),
                                    tool_call_id="d1")}
    return FunctionModel(respond, stream_function=stream, model_name="orch")


def specialist_model() -> FunctionModel:
    """Runs one deep sub-agent, then answers; the sub-agent answers longer text."""
    def choose(messages):
        if SUB_TASK in first_prompt(messages):
            return "text", "the sub-task is checked " * 7
        if answered(messages):
            return "text", "specialist done"
        return "tool", None

    def respond(messages, info):
        kind, text = choose(messages)
        if kind == "text":
            return ModelResponse(parts=[TextPart(text)],
                                 usage=RequestUsage(input_tokens=23, output_tokens=9))
        return ModelResponse(parts=[ToolCallPart("task", {"description": SUB_TASK},
                                                 tool_call_id="t1")],
                             usage=RequestUsage(input_tokens=29, output_tokens=4))

    async def stream(messages, info):
        kind, text = choose(messages)
        if kind == "text":
            yield text
        else:
            yield {0: DeltaToolCall(name="task", json_args=json.dumps({"description": SUB_TASK}),
                                    tool_call_id="t1")}
    return FunctionModel(respond, stream_function=stream, model_name="spec")


def build() -> Agent:
    orch = Agent(wrap(orchestrator_model(), "gpt-4o"))
    model = wrap(specialist_model(), "gpt-4o")
    spec = Agent(model, toolsets=[deep_toolset(
        DeepConfig(enabled=True), parent_toolsets=[], model=model,
        agent_name=SPECIALIST, depth=0)])
    _attach_delegation_tool(orch, spec, SimpleNamespace(
        name=SPECIALIST, description="d", mcp_servers=[]))
    return orch


def logged(rec, run_id: str) -> tuple[list[dict], list[dict]]:
    requests = [r["body"] for r in rec.of("logRequest") if r["body"].get("runId") == run_id]
    responses = [r["body"] for r in rec.of("logResponse") if r["body"].get("runId") == run_id]
    return requests, responses


async def test_logged_tokens_add_up_to_the_runs_usage(rec):
    with nexus_run("run-sum"):
        result = await build().run("hi")
    await sender.drain()
    requests, responses = logged(rec, "run-sum")
    usage = result.usage()
    agents = [b["agent"] for b in responses]
    assert agents.count("orchestrator") == 2 and agents.count(SPECIALIST) == 3
    assert len(requests) == len(responses) == usage.requests == 5
    assert sum(b["inputTokens"] for b in responses) == usage.input_tokens
    assert sum(b["outputTokens"] for b in responses) == usage.output_tokens
    # The plain orchestrator requests carry their own counts, not an estimate.
    orch = [(b["inputTokens"], b["outputTokens"]) for b in responses
            if b["agent"] == "orchestrator"]
    assert sorted(orch) == sorted(ORCH_USAGES[:2])
    assert all(b["status"] == "COMPLETED" for b in responses)
    assert len(rec.requests) == len(requests) + len(responses)   # nothing else was sent


async def test_through_the_web_chat_the_stored_usage_matches(monkeypatch, rec):
    drivers._chat_registry(drivers.Ctx(monkeypatch, lambda m: m), build())
    mid, stored = await drivers._chat_turn({})
    assert stored["status"] == "done"
    await sender.drain()
    async with SessionLocal() as s:
        parts = json.loads((await s.get(ChatMessage, mid)).parts_json)
    (usage,) = [p for p in parts if p["type"] == "usage"]
    requests, responses = logged(rec, f"chat-{mid}")
    assert len(requests) == len(responses) == usage["requests"] == 5
    assert sum(b["inputTokens"] for b in responses) == usage["input_tokens"]
    assert sum(b["outputTokens"] for b in responses) == usage["output_tokens"]
