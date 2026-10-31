"""Delegation detail on the progress side channel (``agents.progress``) and
the per-run token share the delegation tool reports (``agents.registry``).

The specialist and orchestrator are real pydantic-ai ``Agent``s on
``FunctionModel``s with fixed per-response usage, wired by the real
``_attach_delegation_tool``: the share must be the sum of the specialist's
own ``ModelResponse.usage``, not the cumulative ``RunUsage`` it shares with
the orchestrator through ``usage=ctx.usage``.

Run:  .venv/bin/python -m pytest tests/test_delegation_usage.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel  # noqa: E402
from pydantic_ai.usage import RequestUsage  # noqa: E402

from agents import progress  # noqa: E402
from agents.progress import (  # noqa: E402
    ProgressUpdate,
    current_delegation,
    current_progress,
    report_delegation_end,
    report_delegation_start,
    report_tool_end,
    report_tool_start,
)
from agents.registry import _attach_delegation_tool, specialist_usage  # noqa: E402

SECRET = "S3cr3t-value-xyz"


@pytest.fixture
def updates():
    seen: list[ProgressUpdate] = []
    token = current_progress.set(seen.append)
    yield seen
    current_progress.reset(token)


# ---------------------------------------------------------------------------
# agents.progress
# ---------------------------------------------------------------------------
def test_existing_callers_keep_working(updates):
    report_delegation_start("btp-admin", "Consulting…")
    report_delegation_end("btp-admin")
    start, end = updates
    assert (start.kind, start.text, start.delegation_id, start.parent, start.prompt) == (
        "delegation_start", "Consulting…", None, None, None)
    assert (end.kind, end.ok, end.output, end.usage, end.duration_ms) == (
        "delegation_end", True, None, None, None)


def test_delegation_id_comes_from_the_contextvar(updates):
    report_tool_start("btp-admin", "t-0", "SAPRead")  # outside any delegation
    token = current_delegation.set("d-1")
    try:
        report_tool_start("btp-admin", "t-1", "SAPRead", {"a": 1})
        report_tool_end("btp-admin", "t-1", ok=False, output="x")
        report_delegation_start("peer", "Consulting peer…", delegation_id="d-2", query="q")
    finally:
        current_delegation.reset(token)
    outside, start, end, nested = updates
    assert outside.delegation_id is None
    assert start.delegation_id == "d-1" and end.delegation_id == "d-1" and not end.ok
    # An explicit id wins; the parent is the delegation bound at its start.
    assert (nested.delegation_id, nested.parent, nested.prompt) == ("d-2", "d-1", "q")


def test_a_sink_that_raises_is_still_ignored():
    def boom(_update):
        raise RuntimeError("sink")

    token = current_progress.set(boom)
    try:
        report_delegation_end("a", delegation_id="d", ok=False, usage={"input_tokens": 1})
    finally:
        current_progress.reset(token)
    assert progress.current_delegation.get() is None


# ---------------------------------------------------------------------------
# agents.registry: the delegation tool
# ---------------------------------------------------------------------------
ORCH_USAGE = RequestUsage(input_tokens=1000, output_tokens=50)
SPEC_USAGES = [RequestUsage(input_tokens=100, output_tokens=10, cache_read_tokens=5),
               RequestUsage(input_tokens=200, output_tokens=20)]


def orchestrator_model() -> FunctionModel:
    def respond(messages, info):
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            return ModelResponse(parts=[TextPart("summary")], usage=ORCH_USAGE)
        tool = info.function_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool, {"query": "list systems"},
                                                 tool_call_id="call-orch-1")],
                             usage=ORCH_USAGE)
    return FunctionModel(respond, model_name="orch-model")


def specialist(fail: bool = False) -> Agent:
    """Answers with fixed usage when run plainly, and streams (estimated usage)
    when run with an event stream handler, as the delegation tool runs it."""
    def done(messages) -> bool:
        return any(isinstance(p, ToolReturnPart) for p in messages[-1].parts)

    def respond(messages, info):
        if done(messages):
            return ModelResponse(parts=[TextPart("3 systems")], usage=SPEC_USAGES[1])
        return ModelResponse(parts=[ToolCallPart("count", {}, tool_call_id="spec-call-1")],
                             usage=SPEC_USAGES[0])

    async def stream(messages, info):
        if fail:
            raise RuntimeError(f"backend https://user:{SECRET}@h.example/x?token={SECRET}")
        if done(messages):
            yield "3 systems"
            return
        yield {0: DeltaToolCall(name="count", json_args="{}", tool_call_id="spec-call-1")}

    agent = Agent(FunctionModel(respond, stream_function=stream, model_name="spec-model"))

    @agent.tool_plain
    def count() -> int:
        return 3

    return agent


def wire(spec: Agent) -> Agent:
    orch = Agent(orchestrator_model())
    _attach_delegation_tool(orch, spec, SimpleNamespace(
        name="btp-admin", description="Admin of the landscape.", mcp_servers=[]))
    return orch


async def test_specialist_usage_sums_its_own_responses(real_agents_and_mcp):
    result = await specialist().run("q")
    share = specialist_usage(result.new_messages())
    assert share == {"input_tokens": 300, "output_tokens": 30, "cache_read_tokens": 5,
                     "requests": 2, "model": "spec-model"}
    assert specialist_usage([]) == {"input_tokens": 0, "output_tokens": 0,
                                    "cache_read_tokens": 0, "requests": 0, "model": None}


async def test_delegation_reports_its_share_not_the_shared_total(updates,
                                                                 real_agents_and_mcp):
    result = await wire(specialist()).run("Which systems?")
    total = result.usage()
    # The orchestrator's own two responses have fixed usage; the tree total
    # (one RunUsage shared through usage=ctx.usage) adds the specialist's.
    own = [m for m in result.new_messages() if isinstance(m, ModelResponse)]
    assert [m.usage for m in own] == [ORCH_USAGE, ORCH_USAGE]
    spec_in, spec_out = total.input_tokens - 2000, total.output_tokens - 100
    assert spec_in > 0 and spec_out > 0
    starts = [u for u in updates if u.kind == "delegation_start"]
    ends = [u for u in updates if u.kind == "delegation_end"]
    assert len(starts) == len(ends) == 1
    start, end = starts[0], ends[0]
    assert (start.delegation_id, start.parent, start.prompt) == (
        "call-orch-1", None, "list systems")
    assert start.text == "Consulting the btp-admin specialist…"
    # ... the delegation reports only its own run.
    assert end.delegation_id == "call-orch-1" and end.ok is True
    assert end.usage == {"input_tokens": spec_in, "output_tokens": spec_out,
                         "cache_read_tokens": 0, "requests": 2, "model": "spec-model"}
    assert total.requests == 4
    assert end.output == "3 systems"
    assert isinstance(end.duration_ms, int) and end.duration_ms >= 0
    # The specialist's tool calls carry the delegation id.
    tool_updates = [u for u in updates if u.kind in ("tool_start", "tool_end")]
    assert [u.kind for u in tool_updates] == ["tool_start", "tool_end"]
    assert all(u.delegation_id == "call-orch-1" for u in tool_updates)
    assert current_delegation.get() is None


async def test_a_failed_delegation_reports_not_ok_without_usage(updates,
                                                                real_agents_and_mcp):
    await wire(specialist(fail=True)).run("Which systems?")
    end = next(u for u in updates if u.kind == "delegation_end")
    assert end.ok is False and end.usage is None
    assert end.output.startswith("Error from btp-admin: RuntimeError")
    assert SECRET not in end.output and len(end.output) <= 400
    assert isinstance(end.duration_ms, int)
    assert current_delegation.get() is None
