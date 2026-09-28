"""Deep agents: config validation, the per-run scratchpad and plan, and the
``task`` sub-agent tool (shared state, depth bound, concurrency bound).

Run:  pytest tests/test_deep_agents.py

Models and toolsets are passed at ``Agent.run()`` time rather than to
``Agent(...)``: several script-style suites in this directory monkeypatch
``pydantic_ai.Agent.__init__`` at import to drop ``toolsets`` and force a
``TestModel``, and pytest imports every module here before any test runs.
``agents.deep`` builds its sub-agents the same way, so the behaviour under
test is the production one.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_deep_agents.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402

from agents import deep  # noqa: E402
from agents.deep import (  # noqa: E402
    DEFAULT_SUBAGENT_INSTRUCTIONS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    DeepConfig,
    DeepState,
    TodoItem,
    deep_instructions,
    deep_toolset,
    dump_deep_config,
    parse_deep_config,
)


@pytest.fixture(autouse=True)
def _real_agent_run(monkeypatch):
    """Run agents for real in this module.

    ``tests/test_a2a.py`` replaces ``pydantic_ai.Agent.run`` with an echo fake
    at import time, and pytest imports it (alphabetically) before this module
    runs. pydantic-ai defines ``run`` on ``AbstractAgent``, so the fake is a
    shadowing attribute on the subclass; removing it for the duration of a
    test restores the inherited real method, and monkeypatch puts the fake
    back afterwards for the modules that rely on it.
    """
    if "run" in vars(Agent):
        monkeypatch.delattr(Agent, "run")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
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
    """Tool results the model is answering to, i.e. those in the last request."""
    if not messages:
        return []
    return [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]


def _is_subagent(info: AgentInfo) -> bool:
    return bool(info.instructions and DEFAULT_SUBAGENT_INSTRUCTIONS[:40] in info.instructions)


async def _run(model, toolset, prompt="go", **kwargs):
    agent = Agent(instructions="parent agent", retries=1)
    return await agent.run(prompt, model=model, toolsets=[toolset], **kwargs)


# ---------------------------------------------------------------------------
# DeepConfig
# ---------------------------------------------------------------------------
def test_deep_config_defaults_are_off():
    cfg = DeepConfig()
    assert cfg.enabled is False
    assert (cfg.planning, cfg.scratchpad, cfg.subagents) == (True, True, True)
    assert cfg.max_subagents == 5
    assert cfg.subagent_max_depth == 1
    assert cfg.subagent_instructions == ""


@pytest.mark.parametrize(
    "bad",
    [
        {"max_subagents": 0},
        {"max_subagents": 21},
        {"subagent_max_depth": 0},
        {"subagent_max_depth": 4},
        {"max_subagent": 3},  # typo: unknown keys are refused
        {"enabled": "yes please"},
    ],
)
def test_deep_config_rejects_out_of_range_and_unknown(bad):
    with pytest.raises(ValidationError):
        DeepConfig.model_validate(bad)


def test_parse_and_dump_round_trip_and_degrade():
    cfg = DeepConfig(enabled=True, max_subagents=3, subagent_instructions="Be brief.")
    raw = dump_deep_config(cfg)
    assert raw and parse_deep_config(raw) == cfg
    # Defaults store as null so untouched agents keep a null column.
    assert dump_deep_config(DeepConfig()) is None
    assert dump_deep_config(None) is None
    # Malformed storage degrades to "off" instead of failing a build.
    assert parse_deep_config(None) == DeepConfig()
    assert parse_deep_config("{not json") == DeepConfig()
    assert parse_deep_config("[1, 2]") == DeepConfig()
    assert parse_deep_config('{"max_subagents": 999}') == DeepConfig()


# ---------------------------------------------------------------------------
# per-run state
# ---------------------------------------------------------------------------
def test_state_is_keyed_by_run_id_and_shared_via_deps():
    a = deep.state_for(_Ctx("run-a"))
    b = deep.state_for(_Ctx("run-b"))
    assert a is not b
    assert deep.state_for(_Ctx("run-a")) is a
    # A child run with the parent's state as deps sees the parent's state,
    # whatever its own run id is.
    assert deep.state_for(_Ctx("run-child", deps=a)) is a
    deep.forget_state("run-a")
    deep.forget_state("run-b")
    assert deep.state_for(_Ctx("run-a")) is not a


def test_state_ttl_sweep(monkeypatch):
    state = deep.state_for(_Ctx("run-old"))
    state.last_used -= deep.STATE_TTL_SECONDS + 1
    # Force the throttled sweep to run on the next access.
    monkeypatch.setattr(deep, "_last_sweep", 0.0)
    fresh = deep.state_for(_Ctx("run-new"))
    assert "run-old" not in deep._states
    assert deep._states["run-new"] is fresh
    deep.forget_state("run-new")


# ---------------------------------------------------------------------------
# tools: which exist
# ---------------------------------------------------------------------------
def _names(cfg: DeepConfig, depth: int = 0) -> set[str]:
    ts = deep_toolset(cfg, parent_toolsets=[], model=TestModel(), agent_name="a", depth=depth)
    return set(ts.tools)


def test_tool_groups_follow_the_switches():
    assert _names(DeepConfig(enabled=True)) == {
        "write_todos", "read_todos", "ls", "read_file", "write_file", "edit_file", "task",
    }
    assert _names(DeepConfig(enabled=True, planning=False)) == {
        "ls", "read_file", "write_file", "edit_file", "task",
    }
    assert _names(DeepConfig(enabled=True, scratchpad=False)) == {
        "write_todos", "read_todos", "task",
    }
    assert _names(DeepConfig(enabled=True, subagents=False)) == {
        "write_todos", "read_todos", "ls", "read_file", "write_file", "edit_file",
    }


def test_task_tool_is_omitted_at_max_depth():
    one = DeepConfig(enabled=True, subagent_max_depth=1)
    assert "task" in _names(one, depth=0)
    assert "task" not in _names(one, depth=1)
    three = DeepConfig(enabled=True, subagent_max_depth=3)
    assert "task" in _names(three, depth=2)
    assert "task" not in _names(three, depth=3)


def test_instructions_describe_only_what_is_enabled():
    cfg = DeepConfig(enabled=True, max_subagents=7)
    text = deep_instructions(cfg)
    assert "write_todos" in text and "scratchpad" in text and "`task`" in text
    assert "At most 7 sub-agents" in text
    assert "`task`" not in deep_instructions(cfg, depth=1)
    assert "write_todos" not in deep_instructions(DeepConfig(enabled=True, planning=False))
    assert deep_instructions(
        DeepConfig(enabled=True, planning=False, scratchpad=False, subagents=False)
    ) == ""


# ---------------------------------------------------------------------------
# tools: planning and scratchpad behaviour
# ---------------------------------------------------------------------------
async def _call(toolset: FunctionToolset, name: str, state: DeepState, **args):
    """Call one deep tool directly with a stand-in context."""
    tool = toolset.tools[name]
    return await tool.function(_Ctx("direct", deps=state), **args)


@pytest.fixture
def ts():
    return deep_toolset(
        DeepConfig(enabled=True), parent_toolsets=[], model=TestModel(), agent_name="a"
    )


async def test_todos_replace_and_render(ts):
    state = DeepState(run_id="t")
    out = await _call(ts, "write_todos", state, todos=[
        TodoItem(content="read the notes"),
        TodoItem(content="draft the reply", status="in_progress"),
    ])
    assert "1. [ ] read the notes" in out and "2. [~] draft the reply" in out
    assert "(0/2 completed)" in out
    out = await _call(ts, "write_todos", state, todos=[
        TodoItem(content="read the notes", status="completed"),
    ])
    assert len(state.todos) == 1 and "[x] read the notes" in out
    assert await _call(ts, "read_todos", state) == out
    too_many = [TodoItem(content=f"step {i}") for i in range(deep.MAX_TODOS + 1)]
    assert (await _call(ts, "write_todos", state, todos=too_many)).startswith("Error")


async def test_scratchpad_read_write_edit(ts):
    state = DeepState(run_id="s")
    assert await _call(ts, "ls", state) == []
    assert (await _call(ts, "read_file", state, path="missing.md")).startswith("Error")
    msg = await _call(ts, "write_file", state, path="notes/a.md", content="one\ntwo\nthree")
    assert msg.startswith("Wrote 13 bytes to notes/a.md")
    assert await _call(ts, "ls", state) == ["notes/a.md"]
    listing = await _call(ts, "read_file", state, path="notes/a.md")
    assert listing.splitlines() == ["     1\tone", "     2\ttwo", "     3\tthree"]
    paged = await _call(ts, "read_file", state, path="notes/a.md", offset=1, limit=1)
    assert paged.splitlines()[0] == "     2\ttwo" and "1 more line" in paged
    assert (await _call(ts, "read_file", state, path="notes/a.md", offset=9)).startswith("Error")

    msg = await _call(ts, "edit_file", state, path="notes/a.md", old_string="two", new_string="2")
    assert msg == "Replaced 1 occurrence(s) in notes/a.md."
    assert state.files["notes/a.md"] == "one\n2\nthree"
    assert (await _call(ts, "edit_file", state, path="notes/a.md",
                        old_string="nope", new_string="x")).startswith("Error")
    assert (await _call(ts, "edit_file", state, path="notes/a.md",
                        old_string="", new_string="x")).startswith("Error")


async def test_edit_is_refused_when_ambiguous_unless_replace_all(ts):
    state = DeepState(run_id="e")
    await _call(ts, "write_file", state, path="f", content="ab ab ab")
    msg = await _call(ts, "edit_file", state, path="f", old_string="ab", new_string="x")
    assert msg.startswith("Error") and "3 times" in msg
    assert state.files["f"] == "ab ab ab"
    msg = await _call(ts, "edit_file", state, path="f", old_string="ab",
                      new_string="x", replace_all=True)
    assert msg == "Replaced 3 occurrence(s) in f."
    assert state.files["f"] == "x x x"


async def test_scratchpad_caps(ts):
    state = DeepState(run_id="c")
    big = "x" * (MAX_FILE_BYTES + 1)
    assert (await _call(ts, "write_file", state, path="big", content=big)).startswith("Error")
    assert "big" not in state.files
    # Exactly at the per-file cap is fine.
    assert (await _call(ts, "write_file", state, path="max", content="x" * MAX_FILE_BYTES)
            ).startswith("Wrote")
    # Total cap: 4 MB / 256 KB = 16 full files; the 17th tips it over.
    for i in range(MAX_TOTAL_BYTES // MAX_FILE_BYTES - 1):
        assert (await _call(ts, "write_file", state, path=f"fill{i}",
                            content="y" * MAX_FILE_BYTES)).startswith("Wrote")
    assert state.total_bytes == MAX_TOTAL_BYTES
    assert (await _call(ts, "write_file", state, path="over", content="z")).startswith("Error")
    # Overwriting a file with something smaller frees room again.
    assert (await _call(ts, "write_file", state, path="max", content="small")).startswith("Wrote")
    assert (await _call(ts, "write_file", state, path="over", content="z")).startswith("Wrote")
    # An edit that would push a file past its cap is refused and leaves it intact.
    await _call(ts, "write_file", state, path="edge", content="k")
    grow = await _call(ts, "edit_file", state, path="edge", old_string="k",
                       new_string="k" * (MAX_FILE_BYTES + 1))
    assert grow.startswith("Error") and state.files["edge"] == "k"

    # File-count cap, on a fresh state so the byte cap does not interfere.
    many = DeepState(run_id="m")
    for i in range(MAX_FILES):
        many.put(f"f{i}", "a")
    assert (await _call(ts, "write_file", many, path="one-more", content="a")).startswith("Error")
    assert (await _call(ts, "write_file", many, path="f0", content="bb")).startswith("Wrote")
    for bad in ("", "  ", "a\nb", "p" * 201):
        assert (await _call(ts, "write_file", many, path=bad, content="a")).startswith("Error")


# ---------------------------------------------------------------------------
# sub-agents
# ---------------------------------------------------------------------------
def _scripted_model(script: dict[str, list]):
    """A FunctionModel following per-role scripts.

    ``script["parent"]`` / ``script["child"]`` are lists of turns; each turn
    is a list of ``(tool_name, args)`` calls, or a string for a final text
    answer. The role is read from the instructions (sub-agents carry the
    default sub-agent prompt).
    """
    counters = {"parent": 0, "child": 0}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        role = "child" if _is_subagent(info) else "parent"
        turns = script[role]
        i = counters[role]
        counters[role] = i + 1
        turn = turns[min(i, len(turns) - 1)]
        if isinstance(turn, str):
            # Let scripts echo the last tool result: "{last}".
            last = _last_tool_returns(messages)
            text = turn.replace("{last}", str(last[-1].content) if last else "")
            return ModelResponse(parts=[TextPart(text)])
        return ModelResponse(parts=[ToolCallPart(name, args) for name, args in turn])

    return FunctionModel(fn)


async def test_task_runs_a_child_that_shares_the_scratchpad():
    cfg = DeepConfig(enabled=True, subagent_max_depth=1)
    model = _scripted_model({
        "parent": [
            [("write_file", {"path": "brief.md", "content": "find the answer"})],
            [("task", {"description": "read brief.md and write the answer to answer.md"})],
            [("read_file", {"path": "answer.md"})],
            "parent saw: {last}",
        ],
        "child": [
            [("read_file", {"path": "brief.md"})],
            [("write_file", {"path": "answer.md", "content": "42"})],
            "child done",
        ],
    })
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    result = await _run(model, ts)
    assert result.output == "parent saw:      1\t42"
    returns = _tool_returns(result.all_messages())
    assert returns["task"] == ["child done"]


async def test_task_child_has_parent_toolsets_and_no_task_at_max_depth():
    seen: dict[str, list[str]] = {}
    parent_tools = FunctionToolset()

    @parent_tools.tool_plain
    def lookup(key: str) -> str:
        """Look something up."""
        return f"value-of-{key}"

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        role = "child" if _is_subagent(info) else "parent"
        seen[role] = sorted(t.name for t in info.function_tools)
        last = _last_tool_returns(messages)
        if role == "parent":
            if not last:
                return ModelResponse(parts=[ToolCallPart("task", {"description": "look up x"})])
            return ModelResponse(parts=[TextPart(str(last[-1].content))])
        if not last:
            return ModelResponse(parts=[ToolCallPart("lookup", {"key": "x"})])
        return ModelResponse(parts=[TextPart(f"child got {last[-1].content}")])

    model = FunctionModel(fn)
    cfg = DeepConfig(enabled=True, subagent_max_depth=1, subagent_instructions="")
    ts = deep_toolset(cfg, parent_toolsets=[parent_tools], model=model, agent_name="p")
    result = await _run(model, ts)
    assert result.output == "child got value-of-x"
    assert "task" in seen["parent"] and "lookup" not in seen["parent"]
    # The child has the parent's own toolsets plus the deep tools, minus task.
    assert "lookup" in seen["child"]
    assert {"write_todos", "ls", "write_file"} <= set(seen["child"])
    assert "task" not in seen["child"]


async def test_nested_task_is_available_below_max_depth():
    depth_tools: dict[str, set[str]] = {}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = {t.name for t in info.function_tools}
        last = _last_tool_returns(messages)
        if not _is_subagent(info):
            depth_tools["0"] = tools
            if not last:
                return ModelResponse(parts=[ToolCallPart("task", {"description": "level 1"})])
            return ModelResponse(parts=[TextPart(str(last[-1].content))])
        # A sub-agent: recurse once if it still can, else answer.
        if "task" in tools and not last:
            depth_tools["1"] = tools
            return ModelResponse(parts=[ToolCallPart("task", {"description": "level 2"})])
        if "task" not in tools:
            depth_tools["2"] = tools
            return ModelResponse(parts=[TextPart("bottom")])
        return ModelResponse(parts=[TextPart(f"level1<{last[-1].content}>")])

    model = FunctionModel(fn)
    cfg = DeepConfig(enabled=True, subagent_max_depth=2)
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    result = await _run(model, ts)
    assert result.output == "level1<bottom>"
    assert "task" in depth_tools["0"] and "task" in depth_tools["1"]
    assert "task" not in depth_tools["2"]


async def test_task_extra_instructions_and_custom_prompt_reach_the_child():
    captured: list[str] = []

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.instructions and "PIRATE" in info.instructions:
            captured.append(info.instructions)
            return ModelResponse(parts=[TextPart("arr")])
        last = _last_tool_returns(messages)
        if not last:
            return ModelResponse(parts=[ToolCallPart(
                "task", {"description": "d", "instructions": "Answer tersely."}
            )])
        return ModelResponse(parts=[TextPart(str(last[-1].content))])

    model = FunctionModel(fn)
    cfg = DeepConfig(enabled=True, subagent_instructions="You are a PIRATE.")
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    result = await _run(model, ts)
    assert result.output == "arr"
    assert len(captured) == 1
    assert "You are a PIRATE." in captured[0]
    assert "Answer tersely." in captured[0]
    assert DEFAULT_SUBAGENT_INSTRUCTIONS not in captured[0]
    # The child is told about its own deep tools, but not about task (depth 1 of 1).
    assert "write_todos" in captured[0] and "`task`" not in captured[0]


async def test_task_concurrency_is_bounded_by_max_subagents():
    max_subagents = 2
    in_flight = 0
    peak = 0
    started = 0
    release = asyncio.Event()

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal in_flight, peak, started
        if not _is_subagent(info):
            last = _last_tool_returns(messages)
            if not last:
                return ModelResponse(parts=[
                    ToolCallPart("task", {"description": f"job {i}"}) for i in range(5)
                ])
            return ModelResponse(parts=[TextPart(",".join(str(p.content) for p in last))])
        in_flight += 1
        started += 1
        peak = max(peak, in_flight)
        try:
            # Hold the slot until every child that *can* start has started,
            # so the peak really measures the semaphore, not scheduling luck.
            if started >= max_subagents:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1
        return ModelResponse(parts=[TextPart("ok")])

    model = FunctionModel(fn)
    cfg = DeepConfig(enabled=True, max_subagents=max_subagents)
    ts = deep_toolset(cfg, parent_toolsets=[], model=model, agent_name="p")
    result = await _run(model, ts)
    assert result.output == "ok,ok,ok,ok,ok"
    assert started == 5
    assert peak == max_subagents, f"peak concurrency {peak}"


async def test_task_failure_is_returned_as_text_and_long_output_truncated():
    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = _last_tool_returns(messages)
        if _is_subagent(info):
            if "boom" in str(messages[0].parts[-1].content):
                raise RuntimeError("kaboom")
            return ModelResponse(parts=[TextPart("x" * (deep.SUBAGENT_OUTPUT_LIMIT + 500))])
        if not last:
            return ModelResponse(parts=[
                ToolCallPart("task", {"description": "boom"}),
                ToolCallPart("task", {"description": "long"}),
            ])
        return ModelResponse(parts=[TextPart("done")])

    model = FunctionModel(fn)
    ts = deep_toolset(DeepConfig(enabled=True), parent_toolsets=[], model=model, agent_name="p")
    result = await _run(model, ts)
    assert result.output == "done"
    returns = _tool_returns(result.all_messages())["task"]
    failed = next(r for r in returns if "failed" in r)
    assert failed.startswith("Sub-agent #") and "RuntimeError: kaboom" in failed
    long = next(r for r in returns if r.startswith("xxx"))
    assert len(long) < deep.SUBAGENT_OUTPUT_LIMIT + 400
    assert "truncated at 20,000 characters" in long


async def test_task_usage_is_shared_with_the_parent_run():
    model = _scripted_model({
        "parent": [[("task", {"description": "d"})], "end"],
        "child": ["child"],
    })
    ts = deep_toolset(DeepConfig(enabled=True), parent_toolsets=[], model=model, agent_name="p")
    result = await _run(model, ts)
    # Parent: 2 requests; child: 1. Shared usage counts all three.
    assert result.usage().requests == 3
