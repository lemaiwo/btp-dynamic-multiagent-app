"""IDE run executor (``agents.ide.runner``): one streamed stage run.

Runs a real pydantic-ai agent on a scripted ``FunctionModel`` (streamed, so
the runner's text deltas are exercised) with the deep toolset, behind a fake
registry build holding one specialist named ``abap-orchestrator``.

Models and toolsets are passed at ``run()`` time, not to ``Agent(...)``,
because other suites patch ``Agent.__init__`` at import (see
``tests/test_deep_agents.py``); ``_Specialist`` wraps that.

Run:  python -m pytest tests/test_ide_runner.py -q
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_runner.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import (  # noqa: E402
    AgentInfo,
    DeltaToolCall,
    FunctionModel,
)
from sqlalchemy import select, update  # noqa: E402

from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import (  # noqa: E402
    DeepConfig,
    current_workspace,
    deep_toolset,
    scoped_deep_instructions,
)
from agents.ide import runner  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.stages import StageGateError  # noqa: E402
from agents.ide.store import create_session  # noqa: E402
from agents.progress import current_progress  # noqa: E402
from agents.registry import BuildResult, registry  # noqa: E402
from agents.shared import AGENT_REQUEST_LIMIT, run_usage_limits  # noqa: E402

NAME = "abap-orchestrator"


@pytest.fixture(autouse=True)
def _real_agent_run(monkeypatch):
    if "run" in vars(Agent):
        monkeypatch.delattr(Agent, "run")


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.delenv("IDE_SESSION_REQUEST_CAP", raising=False)
    monkeypatch.delenv("IDE_ORCHESTRATOR_AGENT", raising=False)
    monkeypatch.delenv("IDE_DIAGNOSE_AGENT", raising=False)
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession,
                      IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
    saved = registry._build
    yield
    registry._build = saved


# --- scripted model ----------------------------------------------------------


class Script:
    """Each turn is a final text (str), a list of ``(tool, args)`` calls, an
    exception to raise, or an ``asyncio.Event`` to wait on before answering
    the next turn's text."""

    def __init__(self, turns: list):
        self.turns = turns
        self.instructions: list[str] = []
        self.prompts: list[str] = []
        self.requests = 0

    def _turn(self, messages: list[ModelMessage], info: AgentInfo):
        self.requests += 1
        self.instructions.append(info.instructions or "")
        self.prompts.append("\n".join(
            str(p.content) for m in messages for p in getattr(m, "parts", ())
            if isinstance(p, UserPromptPart)
        ))
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        return self.turns[min(n, len(self.turns) - 1)]

    async def _resolve(self, turn):
        while isinstance(turn, tuple) and turn and turn[0] == "wait":
            await turn[1].wait()
            turn = turn[2]
        if isinstance(turn, BaseException):
            raise turn
        return turn

    async def fn(self, messages, info):
        turn = await self._resolve(self._turn(messages, info))
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

    async def stream(self, messages, info):
        turn = await self._resolve(self._turn(messages, info))
        if isinstance(turn, str):
            half = max(1, len(turn) // 2)
            yield turn[:half]
            if turn[half:]:
                yield turn[half:]
            return
        yield {
            i: DeltaToolCall(name=t, json_args=json.dumps(a),
                             tool_call_id=f"call-{self.requests}-{i}")
            for i, (t, a) in enumerate(turn)
        }

    def model(self) -> FunctionModel:
        return FunctionModel(self.fn, stream_function=self.stream)


class _Specialist:
    """Stands in for the registry's Agent; injects model and deep toolset at
    run time."""

    def __init__(self, script: Script, cfg: DeepConfig | None = None, toolsets=()):
        self.script = script
        self.cfg = cfg or DeepConfig(enabled=True, subagent_max_depth=1)
        self.toolsets = list(toolsets)

    async def run(self, prompt, **kwargs):
        model = self.script.model()
        ts = deep_toolset(
            self.cfg, parent_toolsets=[], model=model, agent_name=NAME,
        )
        cfg = self.cfg
        # As registry.build_orchestrator builds it: the deep section is
        # resolved per run from the bound session.
        agent = Agent(
            instructions=["base orchestrator instructions",
                          lambda: scoped_deep_instructions(cfg)],
            retries=1,
        )
        return await agent.run(prompt, model=model, toolsets=[*self.toolsets, ts], **kwargs)


def _install(script: Script | None, cfg: DeepConfig | None = None, toolsets=()) -> None:
    specialists = (
        {NAME: _Specialist(script, cfg, toolsets)} if script is not None else {}
    )
    registry._build = BuildResult(
        orchestrator=None, specialists=specialists, mcp_clients=[], configs=[]
    )


async def _session(stage: str = "chat", **values) -> str:
    async with SessionLocal() as db:
        s = await create_session(db, owner="alice", title="t", target="T1")
        s.stage = stage
        for key, value in values.items():
            setattr(s, key, value)
        await db.commit()
        return s.id


async def _row(sid: str) -> IdeSession:
    async with SessionLocal() as db:
        return await db.get(IdeSession, sid)


class Events(list):
    def __call__(self, kind: str, data: dict) -> None:
        self.append((kind, data))

    def kinds(self) -> list[str]:
        return [k for k, _ in self]

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self if k == kind]


async def _run(sid, text="go", *, feedback=None) -> Events:
    ev = Events()
    await runner.run_stage(sid, "alice", text, feedback=feedback, emit=ev)
    return ev


# --- usage limits ------------------------------------------------------------


def test_run_usage_limits_unbound_is_global_limit():
    assert current_workspace.get() is None
    assert run_usage_limits().request_limit == AGENT_REQUEST_LIMIT


# --- design / artifacts --------------------------------------------------------


async def test_design_run_streams_and_versions_the_artifact():
    script = Script(["# Design\nGoal: X"])
    _install(script)
    sid = await _session("design")

    ev = await _run(sid, "design it")
    kinds = ev.kinds()
    assert kinds[0] == "run" and kinds[-1] == "done"
    assert "text" in kinds
    assert kinds.index("run") < kinds.index("text") < kinds.index("artifact") \
        < kinds.index("done")
    assert "".join(d["delta"] for d in ev.of("text")) == "# Design\nGoal: X"
    run = ev.of("run")[0]
    assert run["stage"] == "design" and run["run_id"] and run["message_id"]
    art = ev.of("artifact")[0]
    assert (art["kind"], art["version"]) == ("design", 1)
    assert ev.of("usage") == [{"requests_used": 1, "request_cap": 1000}]
    done = ev.of("done")[0]
    assert done["stage"] == "design" and done["status"] == "idle"

    row = await _row(sid)
    assert (row.status, row.run_id, row.requests_used) == ("idle", None, 1)
    async with SessionLocal() as db:
        msgs = list((await db.execute(
            select(IdeMessage).where(IdeMessage.session_id == sid)
            .order_by(IdeMessage.created_at))).scalars())
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "design it"), ("assistant", "# Design\nGoal: X")]
    assert done["message_id"] == msgs[1].id
    assert json.loads(msgs[1].activity_json)["events"] == []

    # Run-time instructions: stage text plus the workspace deep section.
    assert "Produce a design in markdown" in script.instructions[0]
    assert "session workspace" in script.instructions[0]
    assert "base orchestrator instructions" in script.instructions[0]

    _install(Script(["# Design v2"]))
    ev2 = await _run(sid, "again")
    assert ev2.of("artifact")[0]["version"] == 2
    assert (await _row(sid)).requests_used == 2


async def test_chat_run_has_no_artifact():
    _install(Script(["an answer"]))
    sid = await _session("chat")
    ev = await _run(sid, "what is X?")
    assert "artifact" not in ev.kinds()
    assert ev.kinds()[-1] == "done"


# --- workspace -------------------------------------------------------------------


async def test_propose_write_file_emits_file_and_persists():
    path = "src/CLAS/zcl_new.clas.abap"
    script = Script([
        [("write_file", {"path": path, "content": "CLASS zcl_new DEFINITION."})],
        "Proposed zcl_new.",
    ])
    _install(script)
    sid = await _session("propose")
    ev = await _run(sid, "propose")
    assert ev.of("file") == [{"path": path, "state": "new"}]
    assert ev.of("artifact")[0]["kind"] == "note"
    kinds = ev.kinds()
    assert kinds.index("artifact") < kinds.index("file") < kinds.index("usage") \
        < kinds.index("done")
    tools = ev.of("tool")
    assert [t["status"] for t in tools if t["tool"] == "write_file"] == ["running", "ok"]
    async with SessionLocal() as db:
        rows = list((await db.execute(select(IdeWorkspaceFile).where(
            IdeWorkspaceFile.session_id == sid))).scalars())
    assert [(r.path, r.state, r.object_name) for r in rows] == [
        (path, "new", "ZCL_NEW")]
    # Sub-agents are off in propose: the instructions say so.
    assert "No sub-agents in this stage" in script.instructions[0]


async def test_workspace_is_shared_across_runs():
    _install(Script([
        [("write_file", {"path": "notes/a.md", "content": "FROM-RUN-A"})], "ok"]))
    sid = await _session("chat")
    await _run(sid, "write")

    seen = []

    class Reader(Script):
        async def fn(self, messages, info):
            for m in messages:
                for p in m.parts:
                    if getattr(p, "tool_name", None) == "read_file" and \
                            getattr(p, "part_kind", "") == "tool-return":
                        seen.append(p.content)
            return await super().fn(messages, info)

        async def stream(self, messages, info):
            for m in messages:
                for p in m.parts:
                    if getattr(p, "part_kind", "") == "tool-return" and \
                            p.tool_name == "read_file":
                        seen.append(p.content)
            async for chunk in super().stream(messages, info):
                yield chunk

    _install(Reader([[("read_file", {"path": "notes/a.md"})], "read it"]))
    ev = await _run(sid, "read")
    assert seen and "FROM-RUN-A" in seen[0]
    assert ev.of("file") == []  # nothing changed in run B


async def test_write_todos_emits_plan_and_persists_todos():
    _install(Script([
        [("write_todos", {"todos": [{"content": "step one", "status": "in_progress"}]})],
        "planned",
    ]))
    sid = await _session("chat")
    ev = await _run(sid, "plan it")
    assert ev.of("plan") == [{"todos": [{"content": "step one", "status": "in_progress"}]}]
    assert json.loads((await _row(sid)).todos_json)[0]["content"] == "step one"


# --- failure paths ---------------------------------------------------------


async def test_model_error_emits_error_and_resets_status():
    _install(Script([RuntimeError("model down")]))
    sid = await _session("design")
    ev = await _run(sid, "x")
    assert ev.kinds()[-2:] == ["error", "done"]
    err = ev.of("error")[0]
    assert err["code"] == "run_failed"
    assert "artifact" not in ev.kinds()
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)
    assert current_workspace.get() is None and current_progress.get() is None


async def test_agent_missing():
    _install(None)
    sid = await _session("chat")
    ev = await _run(sid, "x")
    assert ev.kinds() == ["run", "error", "done"]
    assert ev.of("error")[0]["code"] == "agent_missing"
    assert ev.of("done")[0]["message_id"] == ev.of("run")[0]["message_id"]
    assert (await _row(sid)).status == "idle"


async def test_agent_name_from_env(monkeypatch):
    monkeypatch.setenv("IDE_ORCHESTRATOR_AGENT", "other-orchestrator")
    _install(Script(["x"]))
    sid = await _session("chat")
    ev = await _run(sid, "x")
    assert ev.of("error")[0]["code"] == "agent_missing"


async def test_concurrent_second_run_is_refused():
    gate = asyncio.Event()
    started = asyncio.Event()

    class Blocking(Script):
        def _turn(self, messages, info):
            started.set()
            return super()._turn(messages, info)

    _install(Blocking([("wait", gate, "first done")]))
    sid = await _session("chat")
    ev = Events()
    first = asyncio.create_task(
        runner.run_stage(sid, "alice", "one", feedback=None, emit=ev))
    await started.wait()
    with pytest.raises(StageGateError) as exc:
        await runner.run_stage(sid, "alice", "two", feedback=None, emit=Events())
    assert exc.value.code == "run_in_progress"
    gate.set()
    await first
    assert ev.kinds()[-1] == "done"
    assert (await _row(sid)).status == "idle"
    # The refused run left no user message behind.
    async with SessionLocal() as db:
        contents = [m.content for m in (await db.execute(
            select(IdeMessage).where(IdeMessage.session_id == sid))).scalars()]
    assert "two" not in contents


async def test_other_owner_cannot_run():
    _install(Script(["x"]))
    sid = await _session("chat")
    with pytest.raises(runner.SessionNotFound):
        await runner.run_stage(sid, "mallory", "x", feedback=None, emit=Events())


async def test_gate_refusal_from_locked_row():
    _install(Script(["x"]))
    sid = await _session("done")
    with pytest.raises(StageGateError) as exc:
        await _run(sid, "x")
    assert exc.value.code == "stage_done"


async def test_usage_cap_exhausted(monkeypatch):
    monkeypatch.setenv("IDE_SESSION_REQUEST_CAP", "2")
    _install(Script([[("ls", {})], [("ls", {})], "three"]))
    sid = await _session("chat")
    ev = await _run(sid, "x")
    assert ev.of("error")[0]["code"] == "usage_exhausted"
    assert ev.kinds()[-1] == "done"
    row = await _row(sid)
    assert (row.status, row.requests_used) == ("idle", 2)
    assert ev.of("usage") == [{"requests_used": 2, "request_cap": 2}]
    # The session is now out of requests: the next run is refused at the gate.
    with pytest.raises(StageGateError) as exc:
        await _run(sid, "y")
    assert exc.value.code == "usage_exhausted"


async def test_cap_counts_previous_runs(monkeypatch):
    monkeypatch.setenv("IDE_SESSION_REQUEST_CAP", "3")
    _install(Script([[("ls", {})], "two"]))
    sid = await _session("chat", requests_used=2)
    ev = await _run(sid, "x")
    assert ev.of("error")[0]["code"] == "usage_exhausted"
    assert (await _row(sid)).requests_used == 3


async def test_revise_saves_feedback_once_and_reruns_stage():
    script = Script(["# Design v2"])
    _install(script)
    sid = await _session("design")
    async with SessionLocal() as db:
        db.add(IdeArtifact(session_id=sid, stage="design", kind="design",
                           content="v1", version=1))
        await db.commit()
    ev = await _run(sid, None, feedback="SPLIT-IT")
    assert ev.of("artifact")[0]["version"] == 2
    # The feedback is the run's request: in the prompt once, not in the
    # instructions; the previous version rides along as data.
    assert "SPLIT-IT" not in script.instructions[0]
    assert script.prompts[0].count("SPLIT-IT") == 1
    assert "## Previous design (version 1)\nv1" in script.prompts[0]
    async with SessionLocal() as db:
        users = [m.content for m in (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "user"))).scalars()]
    assert users == ["SPLIT-IT"]


# --- cancellation ------------------------------------------------------------


async def test_cancel_marks_message_and_resets():
    gate = asyncio.Event()
    started = asyncio.Event()

    class Blocking(Script):
        def _turn(self, messages, info):
            started.set()
            return super()._turn(messages, info)

    _install(Blocking([
        [("write_file", {"path": "notes/partial.md", "content": "P"})],
        ("wait", gate, "never"),
    ]))
    sid = await _session("design")
    ev = Events()
    task = asyncio.create_task(
        runner.run_stage(sid, "alice", "x", feedback=None, emit=ev))
    await started.wait()
    while not any(k == "tool" and d["status"] == "ok" for k, d in ev):
        await asyncio.sleep(0.01)
    assert await runner.cancel(sid) is True
    await asyncio.wait_for(task, timeout=5)  # cancel(sid) ends the run cleanly
    assert ev.kinds()[-1] == "done"
    assert "artifact" not in ev.kinds()
    assert ev.of("file") == [{"path": "notes/partial.md", "state": "new"}]
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)
    async with SessionLocal() as db:
        last = (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "assistant"))).scalar_one()
    assert last.content.endswith("(cancelled)")
    assert sid not in runner._tasks
    assert await runner.cancel(sid) is False


async def test_caller_cancel_propagates_after_cleanup():
    gate = asyncio.Event()
    started = asyncio.Event()

    class Blocking(Script):
        def _turn(self, messages, info):
            started.set()
            return super()._turn(messages, info)

    _install(Blocking([("wait", gate, "never")]))
    sid = await _session("chat")
    ev = Events()
    task = asyncio.create_task(
        runner.run_stage(sid, "alice", "x", feedback=None, emit=ev))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ev.kinds()[-1] == "done"
    assert (await _row(sid)).status == "idle"


async def test_cancel_all():
    gate = asyncio.Event()
    started = asyncio.Event()

    class Blocking(Script):
        def _turn(self, messages, info):
            started.set()
            return super()._turn(messages, info)

    _install(Blocking([("wait", gate, "never")]))
    sid = await _session("chat")
    task = asyncio.create_task(
        runner.run_stage(sid, "alice", "x", feedback=None, emit=Events()))
    await started.wait()
    await runner.cancel_all()
    await asyncio.wait_for(task, timeout=5)
    assert (await _row(sid)).status == "idle"


# --- fix round 1 -----------------------------------------------------------


def _blocking(gate: asyncio.Event, started: asyncio.Event, turns: list) -> Script:
    class Blocking(Script):
        def _turn(self, messages, info):
            started.set()
            return super()._turn(messages, info)

    return Blocking(turns)


async def test_error_detail_stays_on_the_server(caplog):
    import logging

    _install(Script([RuntimeError("SECRET-HOST db.internal:5432 password=x")]))
    sid = await _session("design")
    caplog.set_level(logging.ERROR, logger="agents.ide.runner")
    ev = await _run(sid, "x")
    err = ev.of("error")[0]
    assert err["code"] == "run_failed"
    assert "SECRET" not in err["message"] and "RuntimeError" not in err["message"]
    async with SessionLocal() as db:
        stored = (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "assistant"))).scalar_one()
    assert "SECRET" not in stored.content and "RuntimeError" not in stored.content
    assert "SECRET-HOST" in caplog.text  # logged with the traceback


async def test_cancelled_run_stores_no_running_calls():
    """A call still open when the run is cancelled (a delegated specialist's
    SAPRead that never returned) is stored closed, not ``running``."""
    from agents.progress import report_tool_start

    gate, started = asyncio.Event(), asyncio.Event()

    class OpenCall(Script):
        def _turn(self, messages, info):
            # Runs inside the run's context, so this reaches the run's sink.
            report_tool_start("abap-developer", "open-1", "SAPRead", {"name": "X"})
            started.set()
            return super()._turn(messages, info)

    _install(OpenCall([("wait", gate, "never")]))
    sid = await _session("chat")
    ev = Events()
    task = asyncio.create_task(
        runner.run_stage(sid, "alice", "x", feedback=None, emit=ev))
    await started.wait()
    assert await runner.cancel(sid) is True
    await asyncio.wait_for(task, timeout=5)
    assert any(d["id"] == "open-1" and d["status"] == "running" for d in ev.of("tool"))
    async with SessionLocal() as db:
        stored = (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "assistant"))).scalar_one()
    events = json.loads(stored.activity_json)["events"]
    opened = [e for e in events if e.get("id") == "open-1"]
    assert opened and opened[0]["status"] == "error"
    assert opened[0]["output"] == "(interrupted)"


async def test_release_is_retried_once(monkeypatch):
    real = runner._release
    calls = []

    async def flaky(start, usage):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db blip")
        return await real(start, usage)

    monkeypatch.setattr(runner, "_release", flaky)
    _install(Script(["ok"]))
    sid = await _session("chat")
    ev = await _run(sid, "x")
    assert len(calls) == 2
    assert (await _row(sid)).status == "idle"
    assert ev.kinds()[-1] == "done"


async def test_failed_release_is_reclaimed_after_the_stale_age(monkeypatch):
    async def broken(start, usage):
        raise RuntimeError("db down")

    monkeypatch.setattr(runner, "_release", broken)
    _install(Script(["ok"]))
    sid = await _session("chat")
    ev = await _run(sid, "x")
    assert ev.kinds()[-1] == "done"
    assert (await _row(sid)).status == "running"  # the release never landed
    monkeypatch.undo()
    _install(Script(["second"]))
    monkeypatch.setenv("IDE_RUN_HEARTBEAT_S", "0.01")

    # Fresh: still refused (another instance could own it).
    with pytest.raises(StageGateError) as exc:
        await _run(sid, "y")
    assert exc.value.code == "run_in_progress"
    assert await runner.release_stale(sid) is False

    monkeypatch.setenv("IDE_RUN_STALE_S", "0.05")
    await asyncio.sleep(0.1)
    ev2 = await _run(sid, "y")
    assert ev2.kinds()[0] == "run" and ev2.kinds()[-1] == "done"
    assert (await _row(sid)).status == "idle"


async def test_release_stale_for_the_cancel_route(monkeypatch):
    monkeypatch.setenv("IDE_RUN_HEARTBEAT_S", "0.01")
    sid = await _session("chat", status="running", run_id="ghost")
    assert await runner.release_stale(sid) is False  # fresh row
    monkeypatch.setenv("IDE_RUN_STALE_S", "0.05")
    await asyncio.sleep(0.1)
    assert await runner.release_stale(sid) is True
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)
    assert await runner.release_stale(sid) is False  # already idle
    assert await runner.release_stale("no-such-session") is False


async def test_live_long_run_with_heartbeat_is_not_reclaimed(monkeypatch):
    monkeypatch.setenv("IDE_RUN_HEARTBEAT_S", "0.02")
    monkeypatch.setenv("IDE_RUN_STALE_S", "0.2")
    gate, started = asyncio.Event(), asyncio.Event()
    _install(_blocking(gate, started, [("wait", gate, "done at last")]))
    sid = await _session("chat")
    ev = Events()
    task = asyncio.create_task(
        runner.run_stage(sid, "alice", "x", feedback=None, emit=ev))
    await started.wait()
    await asyncio.sleep(0.5)  # well past the stale age
    # Seen from another instance: no local task, only the row's age counts.
    live = runner._tasks.pop(sid)
    try:
        with pytest.raises(StageGateError) as exc:
            await runner.run_stage(sid, "alice", "y", feedback=None, emit=Events())
        assert exc.value.code == "run_in_progress"
        assert await runner.release_stale(sid) is False
    finally:
        runner._tasks[sid] = live
    gate.set()
    await asyncio.wait_for(task, timeout=5)
    assert ev.kinds()[-1] == "done"
    assert (await _row(sid)).status == "idle"


async def test_local_live_task_is_never_reclaimed(monkeypatch):
    async def no_heartbeat(sid, run_id):
        await asyncio.Event().wait()

    # No heartbeat, so the row really ages past the stale age.
    monkeypatch.setattr(runner, "_heartbeat", no_heartbeat)
    monkeypatch.setenv("IDE_RUN_HEARTBEAT_S", "0.01")
    monkeypatch.setenv("IDE_RUN_STALE_S", "0.05")
    gate, started = asyncio.Event(), asyncio.Event()
    _install(_blocking(gate, started, [("wait", gate, "ok")]))
    sid = await _session("chat")
    task = asyncio.create_task(
        runner.run_stage(sid, "alice", "x", feedback=None, emit=Events()))
    await started.wait()
    await asyncio.sleep(0.1)  # stale by age, but this process runs it
    assert await runner.release_stale(sid) is False
    with pytest.raises(StageGateError):
        await runner.run_stage(sid, "alice", "y", feedback=None, emit=Events())
    gate.set()
    await asyncio.wait_for(task, timeout=5)


# --- final fix round: liveness (FIX-5) ----------------------------------------


def test_stale_age_is_at_least_three_heartbeats(monkeypatch):
    monkeypatch.setenv("IDE_RUN_HEARTBEAT_S", "30")
    monkeypatch.setenv("IDE_RUN_STALE_S", "10")
    assert runner.stale_after() == 90
    monkeypatch.setenv("IDE_RUN_STALE_S", "500")
    assert runner.stale_after() == 500
    monkeypatch.delenv("IDE_RUN_STALE_S")
    # Default: the startup ghost age, not the run timeout, so a crashed
    # instance's lock frees within minutes. The run timeout stays separate.
    assert runner.stale_after() == runner.ghost_age() == 90
    assert runner.run_timeout() == runner.DEFAULT_RUN_TIMEOUT_S


async def test_tool_timeout_error_is_run_failed_not_run_timeout(caplog):
    import logging

    # A tool's own TimeoutError (an HTTP read timeout, say) is the same class
    # as the run deadline's; only the deadline is run_timeout.
    _install(Script([TimeoutError("ARC-1 read timed out")]))
    sid = await _session("design")
    caplog.set_level(logging.ERROR, logger="agents.ide.runner")
    ev = await _run(sid, "x")
    assert ev.of("error")[0]["code"] == "run_failed"
    assert "ARC-1 read timed out" in caplog.text  # logged with the traceback
    assert (await _row(sid)).status == "idle"


async def test_run_deadline_is_run_timeout(monkeypatch):
    monkeypatch.setenv("IDE_RUN_TIMEOUT_S", "0.1")
    gate = asyncio.Event()
    _install(Script([("wait", gate, "never")]))
    sid = await _session("design")
    ev = await _run(sid, "x")
    assert ev.of("error")[0]["code"] == "run_timeout"
    assert ev.kinds()[-1] == "done"
    assert (await _row(sid)).status == "idle"


async def test_reclaim_skips_a_row_touched_since_it_was_seen():
    from datetime import timedelta

    from agents.ide.models import utcnow

    sid = await _session("chat", status="running", run_id="r1")
    async with SessionLocal() as db:
        row = await db.get(IdeSession, sid)
        row.updated_at = utcnow() - timedelta(hours=1)
        await db.commit()
    async with SessionLocal() as seen_db:
        seen = await seen_db.get(IdeSession, sid)
        # A heartbeat of the same run lands between the read and the reclaim.
        async with SessionLocal() as other:
            await other.execute(
                update(IdeSession).where(IdeSession.id == sid)
                .values(updated_at=utcnow())
            )
            await other.commit()
        assert await runner._reclaim(seen_db, seen) is False
        await seen_db.commit()
    row = await _row(sid)
    assert (row.status, row.run_id) == ("running", "r1")


# --- final fix round: deep instructions (FIX-6) --------------------------------


async def test_run_never_advertises_task_the_orchestrator_lacks():
    # The seeded orchestrator is built with subagents=false; chat allows
    # sub-agents, but this agent has no `task` tool to offer.
    script = Script(["ok"])
    _install(script, DeepConfig(enabled=True, subagents=False))
    sid = await _session("chat")
    await _run(sid, "x")
    text = script.instructions[0]
    assert "Delegate isolated sub-tasks with `task`" not in text
    assert "session workspace" in text
    assert text.count("## Working method (deep agent)") == 1


# --- final fix round: error codes (FIX-10) --------------------------------------


@pytest.mark.parametrize("wrap", [False, True])
async def test_destination_user_required_in_a_run_is_user_token_required(wrap):
    from agents.destination_auth import DestinationUserRequired

    exc = DestinationUserRequired("ide-T1", "arc1-abap-readonly")
    if wrap:  # as the MCP client's task group raises it
        exc = ExceptionGroup("unhandled errors in a TaskGroup", [exc])
    _install(Script([exc]))
    sid = await _session("chat")
    ev = await _run(sid, "x")
    err = ev.of("error")[0]
    assert err["code"] == "user_token_required"
    assert "arc1-abap-readonly" not in err["message"]
    assert ev.kinds()[-1] == "done"
    assert (await _row(sid)).status == "idle"


async def test_guard_refusal_tool_event_carries_readonly_refused():
    from pydantic_ai.toolsets import FunctionToolset

    from agents.ide.readonly import ReadOnlyGuard

    arc1 = FunctionToolset()

    @arc1.tool_plain
    def SAPRead(type: str, name: str) -> str:  # noqa: N802 -- ARC-1's name
        return "source"

    _install(
        Script([[("SAPRead", {"type": "TABLE_CONTENTS", "name": "MARA"})], "done"]),
        toolsets=[ReadOnlyGuard(arc1)],
    )
    sid = await _session("chat")
    ev = await _run(sid, "x")
    ended = [t for t in ev.of("tool") if t["tool"] == "SAPRead" and t["status"] != "running"]
    assert ended and ended[-1]["status"] == "error"
    assert ended[-1]["code"] == "readonly_refused"
    ok = [t for t in ev.of("tool") if t["status"] == "ok"]
    assert all("code" not in t for t in ok)
    # The stored activity (what a reload shows) carries the code too.
    async with SessionLocal() as db:
        stored = (await db.execute(select(IdeMessage).where(
            IdeMessage.session_id == sid, IdeMessage.role == "assistant"))).scalar_one()
    events = json.loads(stored.activity_json)["events"]
    refused = [e for e in events if e.get("tool") == "SAPRead"]
    assert refused and refused[-1]["code"] == "readonly_refused"


# --- diagnose sessions (phase 1c) ---------------------------------------------

from pydantic_ai.toolsets import FunctionToolset  # noqa: E402

from agents.ide import diagnose  # noqa: E402
from agents.ide.stages import (  # noqa: E402
    DIAGNOSE_RULE,
    READ_ONLY_RULE,
    REPORT_REQUEST,
    STAGE_INSTRUCTIONS,
    Stage,
)
from agents.ide.store import upsert_conventions  # noqa: E402

DIAG = "abap-diagnostics"
DEST = "arc1-abap-readonly"


@pytest.fixture
def target_servers(monkeypatch):
    """Stub toolsets have no destination to recognise: every toolset counts
    as the target's ARC-1 server, or only the ones added to the list."""
    only: list = []

    def _is_target(toolset, run) -> bool:
        return not only or any(toolset is t for t in only)

    monkeypatch.setattr(diagnose, "is_target_server", _is_target)
    return only


def _arc1() -> FunctionToolset:
    """Stands in for the target's ARC-1 server."""
    ts = FunctionToolset()

    @ts.tool_plain
    def SAPDiagnose(action: str) -> str:  # noqa: N802
        """Diagnose."""
        return json.dumps({"dumps": []})

    return ts


def _install_agents(agents: dict, configs: list | None = None) -> None:
    """``{name: Script | _Specialist}``; a Script gets the ARC-1 stub."""
    specialists = {
        name: a if isinstance(a, _Specialist) else _Specialist(a, toolsets=[_arc1()])
        for name, a in agents.items()
    }
    registry._build = BuildResult(
        orchestrator=None, specialists=specialists, mcp_clients=[],
        configs=configs or [],
    )


async def _diagnose_session(non_production: bool | None = True, **values) -> str:
    """A diagnose session on T1; ``non_production=None`` leaves T1 without a
    conventions row."""
    async with SessionLocal() as db:
        if non_production is not None:
            await upsert_conventions(db, "T1", destination=DEST,
                                     non_production=non_production)
        s = await create_session(db, owner="alice", title="t", target="T1",
                                 session_type="diagnose")
        for key, value in values.items():
            setattr(s, key, value)
        await db.commit()
        return s.id


async def _report(sid: str) -> Events:
    ev = Events()
    await runner.run_stage(sid, "alice", None, feedback=None, emit=ev, report=True)
    return ev


async def _artifacts(sid: str) -> list[IdeArtifact]:
    async with SessionLocal() as db:
        return list((await db.execute(
            select(IdeArtifact).where(IdeArtifact.session_id == sid)
            .order_by(IdeArtifact.version))).scalars())


async def _messages(sid: str) -> list[IdeMessage]:
    async with SessionLocal() as db:
        return list((await db.execute(
            select(IdeMessage).where(IdeMessage.session_id == sid)
            .order_by(IdeMessage.created_at))).scalars())


async def test_diagnose_run_uses_diagnose_agent(target_servers, monkeypatch):
    orchestrator, diagnostics = Script(["from the orchestrator"]), Script(["found it"])
    _install_agents({NAME: orchestrator, DIAG: diagnostics})
    sid = await _diagnose_session()
    ev = await _run(sid, "why did it dump?")

    assert ev.of("error") == []
    assert ev.of("run")[0]["stage"] == "investigate"
    assert "".join(d["delta"] for d in ev.of("text")) == "found it"
    assert (orchestrator.requests, diagnostics.requests) == (0, 1)
    instructions = diagnostics.instructions[0]
    assert "- Session type: diagnose" in instructions
    assert "You cannot change code" in instructions
    assert READ_ONLY_RULE not in instructions
    assert ev.of("done")[0]["stage"] == "investigate"
    assert (await _row(sid)).stage == "investigate"

    # A change session of the same build still runs the orchestrator.
    change = await _session("chat")
    await _run(change, "hello")
    assert (orchestrator.requests, diagnostics.requests) == (1, 1)

    # IDE_DIAGNOSE_AGENT is honoured, and only for diagnose sessions.
    other = Script(["from the other one"])
    _install_agents({NAME: orchestrator, DIAG: diagnostics, "other-diag": other})
    monkeypatch.setenv("IDE_DIAGNOSE_AGENT", "other-diag")
    await _run(sid, "again")
    await _run(change, "again")
    assert (orchestrator.requests, diagnostics.requests, other.requests) == (2, 1, 1)


async def test_diagnose_agent_missing(target_servers):
    """The orchestrator is no fallback for a diagnose run."""
    orchestrator = Script(["x"])
    _install_agents({NAME: orchestrator})
    sid = await _diagnose_session()
    ev = await _run(sid, "x")
    assert ev.kinds() == ["run", "error", "done"]
    assert ev.of("error")[0]["code"] == "agent_missing"
    assert DIAG in ev.of("error")[0]["message"]
    assert orchestrator.requests == 0
    assert (await _row(sid)).status == "idle"


def test_diagnose_agent_name(monkeypatch):
    assert runner.diagnose_agent_name() == DIAG
    monkeypatch.setenv("IDE_DIAGNOSE_AGENT", "  my-diag ")
    assert runner.diagnose_agent_name() == "my-diag"
    monkeypatch.setenv("IDE_ORCHESTRATOR_AGENT", "x")
    assert runner.diagnose_agent_name() == "my-diag"


async def test_diagnose_message_run_stores_no_artifact(target_servers):
    _install_agents({DIAG: Script(["It is a division by zero."])})
    sid = await _diagnose_session()
    ev = await _run(sid, "why?")
    assert ev.of("error") == [] and "artifact" not in ev.kinds()
    assert await _artifacts(sid) == []
    user, assistant = await _messages(sid)
    assert (user.stage, user.content) == ("investigate", "why?")
    assert (assistant.stage, assistant.content) == (
        "investigate", "It is a division by zero.")


async def test_report_run_stores_report_artifact_v1_then_v2(target_servers):
    script = Script(["# Report one"])
    _install_agents({DIAG: script})
    sid = await _diagnose_session()
    await _run(sid, "why?")

    ev = await _report(sid)
    assert ev.of("error") == []
    first = ev.of("artifact")
    assert [(a["kind"], a["version"]) for a in first] == [("report", 1)]
    assert ev.kinds()[-1] == "done"
    assert script.prompts[-1].endswith(f"# Request\n{REPORT_REQUEST}")
    assert "Previous report" not in script.prompts[-1]
    assert STAGE_INSTRUCTIONS[Stage.investigate] not in script.instructions[-1]  # raw

    script.turns = ["# Report two"]
    ev = await _report(sid)
    assert [(a["kind"], a["version"]) for a in ev.of("artifact")] == [("report", 2)]
    assert "## Previous report (version 1)\n# Report one" in script.prompts[-1]

    arts = await _artifacts(sid)
    assert [(a.kind, a.stage, a.version, a.content) for a in arts] == [
        ("report", "investigate", 1, "# Report one"),
        ("report", "investigate", 2, "# Report two"),
    ]
    # The request is the run's user message; the session never moves.
    users = [m.content for m in await _messages(sid) if m.role == "user"]
    assert users == ["why?", REPORT_REQUEST, REPORT_REQUEST]
    row = await _row(sid)
    assert (row.stage, row.status, row.run_id) == ("investigate", "idle", None)


async def test_failed_report_run_stores_no_artifact(target_servers):
    _install_agents({DIAG: Script([RuntimeError("boom")])})
    sid = await _diagnose_session()
    ev = await _report(sid)
    assert ev.of("error")[0]["code"] == "run_failed"
    assert "artifact" not in ev.kinds() and await _artifacts(sid) == []


async def test_report_on_change_session_refused(target_servers):
    script = Script(["x"])
    _install_agents({NAME: script, DIAG: script})
    sid = await _session("design")
    ev = Events()
    with pytest.raises(StageGateError) as exc:
        await runner.run_stage(sid, "alice", None, feedback=None, emit=ev, report=True)
    assert exc.value.code == "not_diagnose"
    assert ev == [] and script.requests == 0
    assert await _messages(sid) == []
    assert (await _row(sid)).status == "idle"


async def test_diagnose_revise_refused(target_servers):
    script = Script(["x"])
    _install_agents({DIAG: script})
    sid = await _diagnose_session()
    ev = Events()
    with pytest.raises(StageGateError) as exc:
        await runner.run_stage(sid, "alice", None, feedback="redo", emit=ev)
    assert exc.value.code == "revise_not_allowed"
    assert ev == [] and script.requests == 0 and await _messages(sid) == []


@pytest.mark.parametrize("text,feedback", [("x", None), (None, "y"), ("x", "y")])
async def test_report_run_takes_no_text(text, feedback):
    """The report request is the app's own text, never the caller's."""
    sid = await _diagnose_session()
    with pytest.raises(ValueError):
        await runner.run_stage(sid, "alice", text, feedback=feedback,
                               emit=Events(), report=True)
    assert await _messages(sid) == []


@pytest.mark.parametrize("masked", [False, True])
async def test_diagnose_prompt_wording_follows_the_masking_switch(
    target_servers, masked
):
    """``masked=True`` is forced past the start: a real start on a target
    that requires masking is refused (the tests below), but what runs behind
    it still words the prompt from ``_Start.masked``."""
    script = Script(["ok"])
    _install_agents({DIAG: script})
    sid = await _diagnose_session(True)
    ev = Events()
    start = await runner._start(sid, "alice", "mail jane.doe@example.com", None)
    assert start.masked is False
    await runner._execute(dataclasses.replace(start, masked=masked), ev)
    assert ev.of("error") == []
    instructions = script.instructions[0]
    assert (DIAGNOSE_RULE in instructions) is masked
    assert (STAGE_INSTRUCTIONS[Stage.investigate] in instructions) is masked
    assert ("not masked" in instructions) is not masked


@pytest.mark.parametrize("non_production", [False, None])
@pytest.mark.parametrize("report", [False, True])
async def test_diagnose_run_is_refused_when_the_target_lost_its_flag(
    target_servers, non_production, report
):
    """Message and report runs: refused before anything is emitted, stored
    or sent to the model."""
    script = Script(["ok"])
    _install_agents({DIAG: script})
    sid = await _diagnose_session(non_production)
    ev = Events()
    with pytest.raises(StageGateError) as exc:
        await runner.run_stage(
            sid, "alice", None if report else "mail jane.doe@example.com",
            feedback=None, emit=ev, report=report,
        )
    assert exc.value.code == "target_not_non_production"
    assert list(ev) == [] and script.requests == 0
    assert await _messages(sid) == []
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "report"])
async def test_diagnose_run_never_persists_the_workspace(target_servers, outcome):
    """D2: a diagnose scratchpad may hold dump text. The runner takes the
    session type from the row, so no outcome saves files or todos."""
    gate, started = asyncio.Event(), asyncio.Event()

    class Blocking(Script):
        def _turn(self, messages, info):
            started.set()
            return super()._turn(messages, info)

    last = {"success": "done", "report": "# Report",
            "error": RuntimeError("boom"), "cancel": ("wait", gate, "never")}[outcome]
    _install_agents({DIAG: Blocking([
        [("write_file", {"path": "notes/dump.md", "content": "dump text"}),
         ("write_todos", {"todos": [{"content": "look", "status": "pending"}]})],
        last,
    ])})
    sid = await _diagnose_session()
    ev = Events()
    task = asyncio.create_task(runner.run_stage(
        sid, "alice", None if outcome == "report" else "x", feedback=None,
        emit=ev, report=outcome == "report"))
    if outcome == "cancel":
        await started.wait()
        while not any(k == "tool" and d["status"] == "ok" for k, d in ev):
            await asyncio.sleep(0.01)
        assert await runner.cancel(sid) is True
    await asyncio.wait_for(task, timeout=5)

    assert ev.kinds()[-1] == "done"
    assert any(d["tool"] == "write_file" and d["status"] == "ok" for d in ev.of("tool"))
    assert ev.of("file") == []
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeWorkspaceFile))).all() == []
    row = await _row(sid)
    assert row.todos_json in (None, "", "[]")
    assert (row.status, row.run_id) == ("idle", None)
    assert diagnose.current_diagnose.get() is None and current_workspace.get() is None


# --- carry-forward of the Task 6 review ---------------------------------------


async def test_diagnose_run_without_target_server_says_so(target_servers, caplog):
    """Without the target's server every diagnose read is refused or simply
    absent, which looks like "there are no dumps": say so instead."""
    mine = _arc1()
    target_servers.append(mine)  # the only target server; no agent has it
    script = Script(["nothing found"])
    _install_agents({DIAG: script})
    sid = await _diagnose_session()
    with caplog.at_level("WARNING", logger="agents.ide.runner"):
        ev = await _run(sid, "why?")

    errors = ev.of("error")
    assert [e["code"] for e in errors] == ["no_diagnose_server"]
    assert DEST not in errors[0]["message"]  # config names stay in the log
    assert ev.kinds()[:2] == ["run", "error"] and ev.kinds()[-1] == "done"
    # A note, not a failure: the run still answers.
    assert script.requests == 1
    assert (await _messages(sid))[-1].content == "nothing found"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "no_diagnose_server" in r.getMessage()]
    assert len(warnings) == 1 and DEST in warnings[0].getMessage()


async def test_target_server_on_the_agent_or_a_peer_is_found(target_servers):
    mine = _arc1()
    target_servers.append(mine)
    # On the agent itself.
    _install_agents({DIAG: _Specialist(Script(["ok"]), toolsets=[mine])})
    sid = await _diagnose_session()
    assert (await _run(sid, "x")).of("error") == []
    # On a peer of a peer (delegation reaches it).
    _install_agents(
        {DIAG: _Specialist(Script(["ok"])),
         "abap-researcher": _Specialist(Script(["ok"])),
         "abap-reader": _Specialist(Script(["ok"]), toolsets=[mine])},
        configs=[{"name": DIAG, "peers": ["abap-researcher"]},
                 {"name": "abap-researcher", "peers": ["abap-reader", DIAG]},
                 {"name": "abap-reader", "peers": []}],
    )
    assert (await _run(sid, "x")).of("error") == []
    # On an agent the diagnose agent cannot reach: that does not count.
    _install_agents(
        {DIAG: _Specialist(Script(["ok"])),
         "abap-reader": _Specialist(Script(["ok"]), toolsets=[mine])},
        configs=[{"name": DIAG, "peers": []}, {"name": "abap-reader", "peers": []}],
    )
    ev = await _run(sid, "x")
    assert [e["code"] for e in ev.of("error")] == ["no_diagnose_server"]


async def test_change_run_never_reports_a_missing_diagnose_server(target_servers):
    target_servers.append(_arc1())
    _install(Script(["ok"]))  # no toolsets at all
    sid = await _session("chat")
    assert (await _run(sid, "x")).of("error") == []


def test_start_is_masked_unless_told_otherwise():
    start = runner._Start(sid="s", run_id="r", stage=Stage.investigate,
                          message_id="m", requests_used=0)
    assert start.masked is True


@pytest.mark.parametrize("session_type,non_production", [
    ("change", False), ("change", True), ("change", None), ("diagnose", True),
])
async def test_start_is_raw_for_change_and_flagged_diagnose(
    session_type, non_production
):
    """A change session never looks at the flag (nor needs the row)."""
    async with SessionLocal() as db:
        if non_production is not None:
            await upsert_conventions(db, "T1", destination=DEST,
                                     non_production=non_production)
        s = await create_session(db, owner="alice", title="t", target="T1",
                                 session_type=session_type)
    start = await runner._start(s.id, "alice", "x", None)
    assert start.masked is False
    assert start.session_type == session_type


@pytest.mark.parametrize("non_production", [False, None])
async def test_start_refuses_diagnose_without_the_flag(non_production):
    sid = await _diagnose_session(non_production)
    with pytest.raises(StageGateError) as exc:
        await runner._start(sid, "alice", "x", None)
    assert exc.value.code == "target_not_non_production"
    assert await _messages(sid) == []


@pytest.mark.parametrize("broken", ["flag", "destination", "load"])
async def test_unreadable_conventions_refuse_the_run(target_servers, monkeypatch, broken):
    """Loading the row and reading it are one step: whatever goes wrong
    there counts as "masking required", and such a run does not start. The
    gate (``stages.assert_can_run``) reads the stored row and lets it
    through; this is the runner's own check."""

    class Row:
        @property
        def non_production(self):
            if broken == "flag":
                raise RuntimeError("detached row")
            return True

        @property
        def destination(self):
            if broken == "destination":
                raise RuntimeError("detached row")
            return DEST

    async def get(db, target):
        if broken == "load":
            raise RuntimeError("database is gone")
        return Row()

    sid = await _diagnose_session()  # the stored row says non_production
    monkeypatch.setattr(runner, "get_conventions", get)
    with pytest.raises(StageGateError) as exc:
        await runner._start(sid, "alice", "mail jane.doe@example.com", None)
    assert exc.value.code == "target_not_non_production"
    assert await _messages(sid) == []
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)


async def test_change_flow_unchanged(target_servers, monkeypatch):
    """A change session next to the diagnose agent: orchestrator, stage
    artifact, persisted workspace, no session type in the prompt."""
    monkeypatch.setenv("IDE_DIAGNOSE_AGENT", DIAG)
    orchestrator, diagnostics = Script([
        [("write_file", {"path": "notes/n.md", "content": "N"})], "# Design",
    ]), Script(["no"])
    _install_agents({NAME: _Specialist(orchestrator), DIAG: diagnostics})
    async with SessionLocal() as db:
        await upsert_conventions(db, "T1", destination=DEST, non_production=True)
    sid = await _session("design")
    ev = await _run(sid, "mail jane.doe@example.com")
    assert ev.of("error") == [] and diagnostics.requests == 0
    assert [(a["kind"], a["version"]) for a in ev.of("artifact")] == [("design", 1)]
    assert ev.of("file") == [{"path": "notes/n.md", "state": "new"}]
    instructions = orchestrator.instructions[0]
    assert READ_ONLY_RULE in instructions and "Session type" not in instructions
    assert (await _messages(sid))[0].content == "mail jane.doe@example.com"
    ev = await _run(sid, None, feedback="shorter")
    assert [(a["kind"], a["version"]) for a in ev.of("artifact")] == [("design", 2)]
    assert (await _row(sid)).stage == "design"
