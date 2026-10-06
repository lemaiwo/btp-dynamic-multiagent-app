"""Deep-agent state bound to a session scope (``WorkspaceScope``).

An IDE session binds ``agents.deep.current_workspace`` around its runs so the
plan and scratchpad survive across runs and stages of that one session. These
tests pin the isolation contract: two scopes never see each other's todos or
files, an unbound run keeps its per-run state exactly as before, a sub-agent's
``deps`` still win, and ``task`` is refused when the scope switches sub-agents
off.

Run:  python -m pytest tests/test_deep_workspace.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent, RunContext  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from agents import deep  # noqa: E402
from agents.deep import (  # noqa: E402
    DEFAULT_SUBAGENT_INSTRUCTIONS,
    DeepConfig,
    DeepState,
    WorkspaceScope,
    current_workspace,
    deep_instructions,
    deep_toolset,
    scoped_deep_instructions,
    state_for,
)


@pytest.fixture(autouse=True)
def _real_agent_run(monkeypatch):
    """Undo the import-time ``Agent.run`` fake of other suites (see
    ``tests/test_deep_agents.py``)."""
    if "run" in vars(Agent):
        monkeypatch.delattr(Agent, "run")


class _Ctx:
    """Just enough of a RunContext for the state lookup."""

    def __init__(self, run_id: str, deps=None):
        self.run_id = run_id
        self.deps = deps


def _tool_returns(messages: list[ModelMessage]) -> dict[str, list]:
    out: dict[str, list] = {}
    for m in messages:
        for part in m.parts:
            if isinstance(part, ToolReturnPart):
                out.setdefault(part.tool_name, []).append(part.content)
    return out


def _last_tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    if not messages:
        return []
    return [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]


def _is_subagent(info: AgentInfo) -> bool:
    return bool(info.instructions and DEFAULT_SUBAGENT_INSTRUCTIONS[:40] in info.instructions)


def _script_model(turns: list):
    """A FunctionModel for the top-level agent only: each turn is a list of
    ``(tool, args)`` calls or a final text. Fails loudly if a child is run."""

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if _is_subagent(info):
            raise AssertionError("a sub-agent was started")
        n = sum(1 for m in messages if any(isinstance(p, ToolReturnPart) for p in m.parts))
        turn = turns[n]
        if isinstance(turn, str):
            last = _last_tool_returns(messages)
            text = turn.format(last=last[-1].content if last else "")
            return ModelResponse(parts=[TextPart(text)])
        return ModelResponse(parts=[ToolCallPart(name, args) for name, args in turn])

    return FunctionModel(fn)


async def _run(model, toolset, prompt="go"):
    agent = Agent(instructions="parent agent", retries=1)
    return await agent.run(prompt, model=model, toolsets=[toolset])


# ---------------------------------------------------------------------------
# state_for
# ---------------------------------------------------------------------------
def test_bound_scope_is_shared_across_run_ids():
    st = DeepState(run_id="ide:s1")
    tok = current_workspace.set(WorkspaceScope("s1", st))
    try:
        assert state_for(_Ctx(run_id="ws-r1")) is st
        assert state_for(_Ctx(run_id="ws-r2")) is st
        # The session state never enters the per-run table, so the TTL sweep
        # and forget_state cannot drop or hand it to another run.
        assert "ws-r1" not in deep._states and "ws-r2" not in deep._states
        assert st not in deep._states.values()
    finally:
        current_workspace.reset(tok)


def test_unbound_is_per_run_unchanged():
    assert current_workspace.get() is None
    a = state_for(_Ctx(run_id="ws-unbound-a"))
    b = state_for(_Ctx(run_id="ws-unbound-b"))
    assert a is not b
    assert state_for(_Ctx(run_id="ws-unbound-a")) is a
    assert deep._states["ws-unbound-a"] is a


def test_unbinding_returns_to_per_run_state():
    st = DeepState(run_id="ide:s-unbind")
    tok = current_workspace.set(WorkspaceScope("s-unbind", st))
    try:
        assert state_for(_Ctx(run_id="ws-same")) is st
    finally:
        current_workspace.reset(tok)
    after = state_for(_Ctx(run_id="ws-same"))
    assert after is not st
    assert deep._states["ws-same"] is after


def test_deps_still_win_when_bound():
    st = DeepState(run_id="ide:s-deps")
    other = DeepState(run_id="sub-parent")
    tok = current_workspace.set(WorkspaceScope("s-deps", st))
    try:
        assert state_for(_Ctx(run_id="child", deps=other)) is other
    finally:
        current_workspace.reset(tok)


async def test_two_scopes_do_not_leak():
    cfg = DeepConfig(enabled=True, subagents=False)
    ts = deep_toolset(cfg, parent_toolsets=[], model=None, agent_name="p")
    write_file = ts.tools["write_file"].function
    write_todos = ts.tools["write_todos"].function
    ls = ts.tools["ls"].function
    read_todos = ts.tools["read_todos"].function

    states = {"A": DeepState(run_id="ide:A"), "B": DeepState(run_id="ide:B")}
    both_written = asyncio.Event()
    written = 0
    seen: dict[str, tuple] = {}

    async def session(name: str) -> None:
        nonlocal written
        current_workspace.set(WorkspaceScope(name, states[name]))
        # Same run id in both sessions: the scope, not the run id, decides.
        ctx = _Ctx(run_id="shared-run-id")
        await write_file(ctx, path=f"src/CLAS/zcl_{name.lower()}.clas.abap", content=name)
        await write_todos(ctx, todos=[deep.TodoItem(content=f"todo {name}")])
        written += 1
        if written == 2:
            both_written.set()
        await both_written.wait()
        seen[name] = (await ls(ctx), await read_todos(ctx))

    await asyncio.gather(session("A"), session("B"))

    assert current_workspace.get() is None  # each task bound its own copy
    assert seen["A"][0] == ["src/CLAS/zcl_a.clas.abap"]
    assert seen["B"][0] == ["src/CLAS/zcl_b.clas.abap"]
    assert "todo A" in seen["A"][1] and "todo B" not in seen["A"][1]
    assert "todo B" in seen["B"][1] and "todo A" not in seen["B"][1]
    assert set(states["A"].files) == {"src/CLAS/zcl_a.clas.abap"}
    assert set(states["B"].files) == {"src/CLAS/zcl_b.clas.abap"}
    assert "shared-run-id" not in deep._states


async def test_scope_persists_across_runs_and_isolates_from_unbound_run():
    cfg = DeepConfig(enabled=True, subagents=False)
    st = DeepState(run_id="ide:persist")

    model1 = _script_model([[("write_file", {"path": "notes/a.md", "content": "x"})], "ok"])
    tok = current_workspace.set(WorkspaceScope("persist", st))
    try:
        await _run(model1, deep_toolset(cfg, parent_toolsets=[], model=model1, agent_name="p"))
        model2 = _script_model([[("ls", {})], "{last}"])
        r2 = await _run(model2, deep_toolset(cfg, parent_toolsets=[], model=model2, agent_name="p"))
    finally:
        current_workspace.reset(tok)
    assert r2.output == "['notes/a.md']"

    # An unbound run afterwards sees an empty, per-run scratchpad.
    model3 = _script_model([[("ls", {})], "{last}"])
    r3 = await _run(model3, deep_toolset(cfg, parent_toolsets=[], model=model3, agent_name="p"))
    assert r3.output == "[]"


# ---------------------------------------------------------------------------
# task tool
# ---------------------------------------------------------------------------
async def test_task_refused_when_subagents_off():
    cfg = DeepConfig(enabled=True, subagent_max_depth=1)
    st = DeepState(run_id="ide:nosub")
    model = _script_model([[("task", {"description": "do it"})], "{last}"])
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    tok = current_workspace.set(WorkspaceScope("nosub", st, allow_subagents=False))
    try:
        result = await _run(model, ts)
    finally:
        current_workspace.reset(tok)
    returns = _tool_returns(result.all_messages())["task"]
    assert len(returns) == 1
    assert returns[0].startswith("Error: sub-agents are not available")
    assert st.subagent_count == 0


async def test_task_allowed_in_scope_shares_session_state():
    cfg = DeepConfig(enabled=True, subagent_max_depth=1)
    st = DeepState(run_id="ide:sub")

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = _last_tool_returns(messages)
        if _is_subagent(info):
            if not last:
                return ModelResponse(parts=[ToolCallPart(
                    "write_file", {"path": "notes/child.md", "content": "c"})])
            return ModelResponse(parts=[TextPart("child done")])
        if not last:
            return ModelResponse(parts=[ToolCallPart("task", {"description": "write"})])
        return ModelResponse(parts=[TextPart(str(last[-1].content))])

    model = FunctionModel(fn)
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    tok = current_workspace.set(WorkspaceScope("sub", st))
    try:
        result = await _run(model, ts)
    finally:
        current_workspace.reset(tok)
    assert result.output == "child done"
    assert st.files == {"notes/child.md": "c"}


# ---------------------------------------------------------------------------
# instructions
# ---------------------------------------------------------------------------
def test_workspace_instructions_mention_file_tree():
    cfg = DeepConfig(enabled=True)
    ws = deep_instructions(cfg, workspace=True)
    plain = deep_instructions(cfg)
    assert "session workspace" in ws and "file tree" in ws
    assert "src/CLAS/zcl_x.clas.abap" in ws and "notes/" in ws
    assert "session workspace" not in plain
    # Planning and task text are the same in both variants.
    assert "**Plan first.**" in ws and "**Plan first.**" in plain
    assert "`task`" in ws


# ---------------------------------------------------------------------------
# carry-forward (Task 8)
# ---------------------------------------------------------------------------
def test_workspace_instructions_say_shared_by_every_agent():
    ws = deep_instructions(DeepConfig(enabled=True), workspace=True)
    assert "shared by every agent" in ws
    assert "specialists you delegate to" in ws


def test_workspace_instructions_without_subagents_say_so():
    cfg = DeepConfig(enabled=True, subagents=False)
    ws = deep_instructions(cfg, workspace=True)
    assert "No sub-agents in this stage" in ws
    assert "Delegate isolated sub-tasks with `task`" not in ws
    # Outside a session nothing changes: no task paragraph, no extra line.
    assert "No sub-agents" not in deep_instructions(cfg)
    # With sub-agents on, the task paragraph is there and the line is not.
    on = deep_instructions(DeepConfig(enabled=True), workspace=True)
    assert "Delegate isolated sub-tasks with `task`" in on
    assert "No sub-agents" not in on


async def test_peer_delegated_inside_scope_sees_the_same_workspace():
    """A peer reached by delegation runs with ``deps=None`` and its own run
    id, but inherits ``current_workspace``: it reads what the delegating
    agent wrote and its own writes land in the session state."""
    cfg = DeepConfig(enabled=True, subagents=False)
    st = DeepState(run_id="ide:peer")

    peer_model = _script_model([
        [("read_file", {"path": "notes/from-parent.md"})],
        [("write_file", {"path": "notes/from-peer.md", "content": "peer"})],
        "peer saw: {last}",
    ])
    peer_seen: list[str] = []

    async def delegate(ctx: RunContext, query: str) -> str:
        peer = Agent(instructions="peer agent", retries=1)
        result = await peer.run(
            query, model=peer_model, usage=ctx.usage,
            toolsets=[deep_toolset(cfg, parent_toolsets=[], model=peer_model,
                                   agent_name="peer")],
        )
        for m in result.all_messages():
            for part in m.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "read_file":
                    peer_seen.append(str(part.content))
        return str(result.output)

    from pydantic_ai.toolsets import FunctionToolset

    delegation = FunctionToolset()
    delegation.tool(delegate)
    parent_model = _script_model([
        [("write_file", {"path": "notes/from-parent.md", "content": "PARENT-NOTE"})],
        [("delegate", {"query": "read the note"})],
        "{last}",
    ])
    parent_ts = deep_toolset(cfg, parent_toolsets=[], model=parent_model, agent_name="p")
    tok = current_workspace.set(WorkspaceScope("peer", st, allow_subagents=False))
    try:
        agent = Agent(instructions="parent agent", retries=1)
        await agent.run("go", model=parent_model, toolsets=[parent_ts, delegation])
    finally:
        current_workspace.reset(tok)
    assert peer_seen and "PARENT-NOTE" in peer_seen[0]
    assert st.files == {"notes/from-parent.md": "PARENT-NOTE",
                        "notes/from-peer.md": "peer"}


def test_run_usage_limits_capped_by_bound_scope():
    from agents.shared import AGENT_REQUEST_LIMIT, run_usage_limits

    assert run_usage_limits().request_limit == AGENT_REQUEST_LIMIT
    tok = current_workspace.set(
        WorkspaceScope("cap", DeepState(run_id="ide:cap"), request_limit=7)
    )
    try:
        assert run_usage_limits().request_limit == min(7, AGENT_REQUEST_LIMIT)
    finally:
        current_workspace.reset(tok)
    tok = current_workspace.set(
        WorkspaceScope("nocap", DeepState(run_id="ide:nocap"), request_limit=None)
    )
    try:
        assert run_usage_limits().request_limit == AGENT_REQUEST_LIMIT
    finally:
        current_workspace.reset(tok)


async def test_subagent_requests_count_against_the_session_cap():
    """``task`` children share the parent's usage and read the scope's
    limit, so a sub-agent cannot spend past the session cap."""
    from pydantic_ai.exceptions import UsageLimitExceeded

    from agents.shared import run_usage_limits

    cfg = DeepConfig(enabled=True, subagent_max_depth=1)
    st = DeepState(run_id="ide:subcap")

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = _last_tool_returns(messages)
        if _is_subagent(info):
            if len(messages) < 7:
                return ModelResponse(parts=[ToolCallPart("ls", {})])
            return ModelResponse(parts=[TextPart("child done")])
        if not last:
            return ModelResponse(parts=[ToolCallPart("task", {"description": "loop"})])
        return ModelResponse(parts=[TextPart(str(last[-1].content))])

    model = FunctionModel(fn)
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    from pydantic_ai.usage import RunUsage

    usage = RunUsage()
    tok = current_workspace.set(WorkspaceScope("subcap", st, request_limit=3))
    try:
        agent = Agent(instructions="parent agent", retries=1)
        # The child stops at the shared cap (deep.task reports that to the
        # parent as a failed sub-agent); the parent's next request is then
        # over the same cap, so the whole run tree stops.
        with pytest.raises(UsageLimitExceeded):
            await agent.run("go", model=model, toolsets=[ts], usage=usage,
                            usage_limits=run_usage_limits())
    finally:
        current_workspace.reset(tok)
    assert st.subagent_count == 1
    assert usage.requests == 3


# ---------------------------------------------------------------------------
# final fix round (FIX-6): the deep section follows the bound session
# ---------------------------------------------------------------------------
def _bind(allow: bool):
    return current_workspace.set(
        WorkspaceScope(session_id="s", state=DeepState(run_id="s"), allow_subagents=allow)
    )


def test_scoped_instructions_unbound_are_the_per_run_text():
    cfg = DeepConfig(enabled=True)
    assert scoped_deep_instructions(cfg) == deep_instructions(cfg)
    assert "private" in scoped_deep_instructions(cfg)


def test_scoped_instructions_bound_say_shared_workspace():
    tok = _bind(True)
    try:
        text = scoped_deep_instructions(DeepConfig(enabled=True))
    finally:
        current_workspace.reset(tok)
    assert "session workspace" in text and "shown to the user" not in text
    assert "private" not in text
    assert "Delegate isolated sub-tasks with `task`" in text


def test_scoped_instructions_never_advertise_a_task_tool_not_built():
    # The agent was built without sub-agents: no stage can give it `task`.
    tok = _bind(True)
    try:
        text = scoped_deep_instructions(DeepConfig(enabled=True, subagents=False))
    finally:
        current_workspace.reset(tok)
    assert "Delegate isolated sub-tasks with `task`" not in text


def test_scoped_instructions_stage_without_subagents():
    tok = _bind(False)
    try:
        text = scoped_deep_instructions(DeepConfig(enabled=True))
    finally:
        current_workspace.reset(tok)
    assert "Delegate isolated sub-tasks with `task`" not in text
    assert "No sub-agents in this stage" in text
