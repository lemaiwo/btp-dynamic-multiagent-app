"""Delegated model calls are logged under the specialist's name.

The registry's delegation tool binds the specialist's name around its run, so
every model call of the specialist (and of its deep sub-agents, which inherit
the context) carries it, next to the run id the caller bound.
"""
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.deep import DeepConfig, deep_toolset
from agents.nexus import logged_model, register, sender, state
from agents.nexus.context import nexus_run
from agents.nexus.logged_model import wrap
from agents.registry import _attach_delegation_tool
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


def calling_model(tool_for, args):
    """Calls ``tool_for(info)`` once when the prompt is 'go' / the query, then text.

    Streams too, because the delegation tool runs the specialist with an event
    stream handler.
    """
    def wanted(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return None
        first = next(p for m in messages for p in m.parts if isinstance(p, UserPromptPart))
        return tool_for(info, str(first.content))

    def respond(messages, info):
        tool = wanted(messages, info)
        usage = RequestUsage(input_tokens=5, output_tokens=1)
        if tool is None:
            return ModelResponse(parts=[TextPart("done")], usage=usage)
        return ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id="c1")], usage=usage)

    async def stream(messages, info):
        tool = wanted(messages, info)
        if tool is None:
            yield "done"
        else:
            import json
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id="c1")}

    return FunctionModel(respond, stream_function=stream, model_name="m")


def pairs(rec):
    return {(r["body"]["agent"], r["body"]["runId"]) for r in rec.of("logRequest")}


async def test_specialist_calls_carry_its_name_and_the_shared_run_id(rec):
    spec = Agent(wrap(calling_model(lambda i, p: None, {}), "gpt-4o"))
    orch = Agent(wrap(calling_model(
        lambda i, p: i.function_tools[0].name if p == "hi" else None,
        {"query": "q"}), "gpt-4o"))
    _attach_delegation_tool(orch, spec, SimpleNamespace(
        name="invoice-assistant", description="d", mcp_servers=[]))
    with nexus_run("run-d"):
        await orch.run("hi")
    await sender.drain()
    assert pairs(rec) == {("invoice-assistant", "run-d"), ("orchestrator", "run-d")}
    assert len(rec.of("logRequest")) == 3  # orch x2, specialist x1


async def test_deep_subagent_keeps_parent_name(rec):
    model = wrap(calling_model(
        lambda i, p: "task" if p == "q" else None, {"description": "sub"}), "gpt-4o")
    spec = Agent(model, toolsets=[deep_toolset(
        DeepConfig(enabled=True), parent_toolsets=[], model=model,
        agent_name="invoice-assistant", depth=0)])
    orch = Agent(wrap(calling_model(
        lambda i, p: i.function_tools[0].name if p == "hi" else None,
        {"query": "q"}), "gpt-4o"))
    _attach_delegation_tool(orch, spec, SimpleNamespace(
        name="invoice-assistant", description="d", mcp_servers=[]))
    with nexus_run("run-e"):
        await orch.run("hi")
    await sender.drain()
    names = [r["body"]["agent"] for r in rec.of("logRequest")]
    assert names.count("invoice-assistant") >= 3 and "orchestrator" in names
    assert pairs(rec) <= {("invoice-assistant", "run-e"), ("orchestrator", "run-e")}
