"""Spike: can a diagnose run ask for a ``trace_start`` approval?

Two designs were probed on pydantic-ai 1.72 with a scripted ``FunctionModel``
and a fake ``SAPDiagnose`` tool behind a ``WrapperToolset`` guard:

* **Variant A (pause and resume)** -- the guard raises ``CallDeferred``; the
  top-level run ends with ``DeferredToolRequests``, its message history is
  stored, and the approval route resumes it with ``DeferredToolResults``.
* **Variant B (request and continue)** -- the guard returns a text saying an
  approval was requested; the run ends normally and the user continues with
  a new message once the approval route has armed the trace.

Outcome on 1.72.0: A1, A2, A3 and A5 pass, so variant A is technically
viable for the top-level agent. A4 shows its limit: a deferral inside a
delegate (whose output type is plain ``str``) cannot pause the outer run --
pydantic-ai raises ``UserError`` and the delegation turns it into an error
text. Variant B works the same at every depth and stores no message history
(open question Q3 has no consent to store paused runs), so B was chosen. The
A probes are skipped unless ``IDE_SPIKE_ALL=1``; A4 stays active because it
pins the behaviour a delegate sees. The decision note is local (``docs/`` is
gitignored).

Run:  python -m pytest tests/test_ide_deferred_spike.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import (  # noqa: E402
    Agent,
    CallDeferred,
    DeferredToolRequests,
    DeferredToolResults,
    FunctionToolset,
    RunContext,
    WrapperToolset,
)
from pydantic_ai.exceptions import UserError  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)

from agents.ide import runner  # noqa: E402
from agents.progress import current_progress  # noqa: E402

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")

# IDE_SPIKE_ALL=1 re-runs the variant A probes (e.g. after a pydantic-ai bump).
SKIP_A = pytest.mark.skipif(
    not os.environ.get("IDE_SPIKE_ALL"),
    reason="variant A not chosen, see spike note "
    "(docs/designs/2026-10-03-abap-ide-diagnose-1c.spike.md, local)"
)

APPROVAL_TEXT = (
    "Approval requested (id a1): the user must approve trace_start in the "
    "IDE. Nothing was armed yet. Tell the user and stop."
)


# --- fakes -------------------------------------------------------------------


class _Script:
    """Scripted model: turn ``n`` answers the ``n``-th model request of a run
    (counted by the responses already in the history). A turn is a str
    (final text) or a list of ``(tool, args)`` calls. Records every tool
    return the model was shown."""

    def __init__(self, turns: list):
        self.turns = turns
        self.seen_returns: list[str] = []

    def _turn(self, messages: list[ModelMessage]):
        for m in messages:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart):
                        self.seen_returns.append(str(p.content))
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        return self.turns[min(n, len(self.turns) - 1)]

    async def fn(self, messages, info: AgentInfo):
        turn = self._turn(messages)
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(
            parts=[ToolCallPart(t, a, tool_call_id=f"call-{i}") for i, (t, a) in enumerate(turn)]
        )

    async def stream(self, messages, info: AgentInfo):
        turn = self._turn(messages)
        if isinstance(turn, str):
            half = max(1, len(turn) // 2)
            yield turn[:half]
            if turn[half:]:
                yield turn[half:]
            return
        yield {
            i: DeltaToolCall(name=t, json_args=json.dumps(a), tool_call_id=f"call-{i}")
            for i, (t, a) in enumerate(turn)
        }

    def model(self) -> FunctionModel:
        return FunctionModel(self.fn, stream_function=self.stream)


def _diagnose_toolset(executed: list[dict]) -> FunctionToolset:
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str, **kw: Any) -> str:  # noqa: N802 -- ARC-1 name
        """Fake ARC-1 SAPDiagnose."""
        executed.append({"action": action, **kw})
        return json.dumps({"action": action, "ok": True})

    return ts


class _DeferGuard(WrapperToolset):
    """Variant A guard: ``trace_start`` is never executed, it is deferred."""

    async def call_tool(self, name, tool_args, ctx, tool):
        if name == "SAPDiagnose" and tool_args.get("action") == "trace_start":
            raise CallDeferred(metadata={"approval_id": "a1"})
        return await super().call_tool(name, tool_args, ctx, tool)


class _RequestGuard(WrapperToolset):
    """Variant B guard: ``trace_start`` records a request and returns text.
    The real guard stores an ``IdeApproval`` row and emits
    ``approval_required``; the ARC-1 call happens only in the approval route."""

    def __init__(self, wrapped, requests: list[dict]):
        super().__init__(wrapped)
        self.requests = requests

    async def call_tool(self, name, tool_args, ctx, tool):
        if name == "SAPDiagnose" and tool_args.get("action") == "trace_start":
            self.requests.append(dict(tool_args))
            return APPROVAL_TEXT
        return await super().call_tool(name, tool_args, ctx, tool)


TRACE_CALL = ("SAPDiagnose", {"action": "trace_start", "processType": "http"})


def _delegate_toolset(specialist: Agent, script: _Script, toolsets: list) -> FunctionToolset:
    """Mirror of ``agents.registry``'s delegation tool: run the specialist
    with the default ``str`` output and turn any failure into an error text
    (registry catches ``BaseException`` and returns ``Error from <name>``)."""
    ts = FunctionToolset()

    @ts.tool
    async def ask_specialist(ctx: RunContext, query: str) -> str:
        """Delegate to the specialist."""
        try:
            result = await specialist.run(
                query, model=script.model(), toolsets=toolsets, usage=ctx.usage
            )
        except BaseException as e:  # noqa: BLE001 -- mirrors registry
            return f"Error from specialist: {type(e).__name__}: {e}"
        return str(result.output)

    return ts


async def _paused_run(executed: list[dict], script: _Script):
    agent = Agent(output_type=[str, DeferredToolRequests])
    ts = _DeferGuard(_diagnose_toolset(executed))
    r1 = await agent.run("arm a trace", model=script.model(), toolsets=[ts])
    return agent, ts, r1


# --- variant A probes (not chosen) ---------------------------------------------


@SKIP_A
async def test_A1_top_level_run_pauses_with_deferred_requests():
    executed: list[dict] = []
    script = _Script([[TRACE_CALL], "done"])
    _, _, r1 = await _paused_run(executed, script)
    assert isinstance(r1.output, DeferredToolRequests)
    assert [c.tool_name for c in r1.output.calls] == ["SAPDiagnose"]
    call = r1.output.calls[0]
    assert call.args_as_dict()["action"] == "trace_start"
    assert r1.output.metadata[call.tool_call_id] == {"approval_id": "a1"}
    assert executed == []  # the guard never executed it


@SKIP_A
async def test_A2_resume_with_external_result():
    executed: list[dict] = []
    script = _Script([[TRACE_CALL], "trace T1 armed"])
    agent, ts, r1 = await _paused_run(executed, script)
    call_id = r1.output.calls[0].tool_call_id
    r2 = await agent.run(
        message_history=r1.all_messages(),
        deferred_tool_results=DeferredToolResults(
            calls={call_id: '{"traceRequestId":"T1"}'}
        ),
        model=script.model(),
        toolsets=[ts],
    )
    assert r2.output == "trace T1 armed"
    assert '{"traceRequestId":"T1"}' in script.seen_returns
    assert executed == []


@SKIP_A
async def test_A3_history_round_trips_through_json():
    executed: list[dict] = []
    script = _Script([[TRACE_CALL], "resumed"])
    agent, ts, r1 = await _paused_run(executed, script)
    stored = ModelMessagesTypeAdapter.dump_json(r1.all_messages())
    history = ModelMessagesTypeAdapter.validate_json(stored)
    call_id = r1.output.calls[0].tool_call_id
    r2 = await agent.run(
        message_history=history,
        deferred_tool_results=DeferredToolResults(calls={call_id: "T1"}),
        model=script.model(),
        toolsets=[ts],
    )
    assert r2.output == "resumed"


async def test_A4_deferred_inside_delegated_specialist_is_an_error():
    """Kept active: it is the reason variant A was rejected. A delegate built
    the registry way (``str`` output) cannot pause: pydantic-ai raises
    ``UserError`` and the delegation hands the orchestrator an error text,
    while the orchestrator's own run ends normally -- nothing pauses."""
    executed: list[dict] = []
    spec_script = _Script([[TRACE_CALL], "unreachable"])
    specialist = Agent()  # default str output, as registry.build_orchestrator
    deleg = _delegate_toolset(
        specialist, spec_script, [_DeferGuard(_diagnose_toolset(executed))]
    )
    orch_script = _Script([[("ask_specialist", {"query": "arm"})], "orchestrator done"])
    orch = Agent(output_type=[str, DeferredToolRequests])
    result = await orch.run("diagnose", model=orch_script.model(), toolsets=[deleg])

    assert result.output == "orchestrator done"  # not DeferredToolRequests
    [err] = orch_script.seen_returns
    assert err.startswith("Error from specialist: UserError")
    assert "DeferredToolRequests" in err
    assert executed == []

    # The same deferral raised directly, for the record.
    with pytest.raises(UserError, match="DeferredToolRequests"):
        await Agent().run(
            "arm",
            model=_Script([[TRACE_CALL], "x"]).model(),
            toolsets=[_DeferGuard(_diagnose_toolset([]))],
        )


@SKIP_A
async def test_A5_event_stream_handler_sees_the_pause():
    executed: list[dict] = []
    script = _Script([[TRACE_CALL], "resumed text"])
    agent = Agent(output_type=[str, DeferredToolRequests])
    ts = _DeferGuard(_diagnose_toolset(executed))
    updates: list = []
    frames: list[tuple[str, dict]] = []
    token = current_progress.set(updates.append)
    try:
        parts: list[str] = []
        r1 = await agent.run(
            "arm", model=script.model(), toolsets=[ts],
            event_stream_handler=runner._stream_handler(
                "abap-diagnostics", lambda e, d: frames.append((e, d)), parts
            ),
        )
        assert isinstance(r1.output, DeferredToolRequests)
        assert any(u.kind == "tool_start" for u in updates)
        call_id = r1.output.calls[0].tool_call_id
        parts2: list[str] = []
        r2 = await agent.run(
            message_history=r1.all_messages(),
            deferred_tool_results=DeferredToolResults(calls={call_id: "T1"}),
            model=script.model(), toolsets=[ts],
            event_stream_handler=runner._stream_handler(
                "abap-diagnostics", lambda e, d: frames.append((e, d)), parts2
            ),
        )
    finally:
        current_progress.reset(token)
    assert r2.output == "resumed text"
    assert "".join(parts2) == "resumed text"


# --- variant B probes (chosen) -------------------------------------------------


async def test_B1_guard_returns_text_and_run_continues():
    executed: list[dict] = []
    requests: list[dict] = []
    script = _Script([[TRACE_CALL], "I asked for your approval to arm the trace."])
    ts = _RequestGuard(_diagnose_toolset(executed), requests)
    updates: list = []
    frames: list[tuple[str, dict]] = []
    parts: list[str] = []
    token = current_progress.set(updates.append)
    try:
        # The IDE runner's call shape: default str output, streamed handler.
        result = await Agent().run(
            "arm a trace", model=script.model(), toolsets=[ts],
            event_stream_handler=runner._stream_handler(
                "abap-diagnostics", lambda e, d: frames.append((e, d)), parts
            ),
        )
    finally:
        current_progress.reset(token)

    assert result.output == "I asked for your approval to arm the trace."
    assert "".join(parts) == result.output
    assert requests == [{"action": "trace_start", "processType": "http"}]
    assert executed == []  # never forwarded to ARC-1
    assert APPROVAL_TEXT in script.seen_returns
    kinds = [u.kind for u in updates]
    assert "tool_start" in kinds and "tool_end" in kinds


async def test_B1_guard_works_inside_a_delegated_specialist():
    executed: list[dict] = []
    requests: list[dict] = []
    spec_script = _Script([[TRACE_CALL], "specialist: approval requested (a1)"])
    specialist = Agent()
    deleg = _delegate_toolset(
        specialist, spec_script, [_RequestGuard(_diagnose_toolset(executed), requests)]
    )
    orch_script = _Script([[("ask_specialist", {"query": "arm"})], "orchestrator done"])
    result = await Agent().run("diagnose", model=orch_script.model(), toolsets=[deleg])

    assert result.output == "orchestrator done"
    assert orch_script.seen_returns == ["specialist: approval requested (a1)"]
    assert APPROVAL_TEXT in spec_script.seen_returns
    assert requests == [{"action": "trace_start", "processType": "http"}]
    assert executed == []


async def test_B1_other_actions_still_execute():
    executed: list[dict] = []
    script = _Script([[("SAPDiagnose", {"action": "dumps"})], "listed"])
    ts = _RequestGuard(_diagnose_toolset(executed), [])
    result = await Agent().run("list dumps", model=script.model(), toolsets=[ts])
    assert result.output == "listed"
    assert executed == [{"action": "dumps"}]
