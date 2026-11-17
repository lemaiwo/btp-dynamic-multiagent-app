"""Every entry point of the app, driven once with scripted models.

Shared by ``tests/test_nexus_entry_points.py`` (the register on, every request
body checked) and ``tests/test_nexus_off_path.py`` (the register off: nothing
leaves, results unchanged). A driver builds its agents on ``Script`` models,
runs the real entry point (the HTTP route, the runner, the A2A handler) and
answers an ``Outcome``: a normalised result (no ids, so two runs compare
equal), the number of model requests per agent name the register should see
them under, and the run id when the entry point says which it is.

``ctx.wrap`` turns a scripted model into what the app would run: the logging
wrapper (``agents.nexus.logged_model.wrap``) or, for a run "without the
package", the model itself. Nothing here makes a real HTTP or model call.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
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
from sqlalchemy import delete, select

from agents import a2a, auth, invoke, job_runner, workflow_runner
from agents.auth import current_jwt, current_principal
from agents.chat_app import dynamic_chat_app
from agents.db import (
    AgentConfig,
    ChatConversation,
    ChatFile,
    ChatMessage,
    ChatUserAgent,
    JobRun,
    SessionLocal,
    Workflow,
    WorkflowItemRun,
    WorkflowRun,
    WorkflowStepRun,
    create_job_run,
    delegation_tool_name,
    delete_workflow,
    get_workflow_by_name,
    init_db,
    upsert_agent,
    upsert_workflow,
)
from agents.deep import DeepConfig, deep_toolset
from agents.nexus.context import ORCHESTRATOR, PERSONAL_AGENT
from agents.registry import BuildResult, _attach_delegation_tool, registry
from agents.validation_errors import install_validation_handler
from agents.webchat import my_routes as chat_my_routes
from agents.webchat import routes as chat_routes
from agents.webchat import run as chat_run

ALICE = "alice@example.com"
SERVERS = [{"url": "https://mcp.example.test/mcp", "auth_mode": "none"}]
ADMIN_AGENT = "invoice-assistant"
JOB_AGENT = "daily-check"
INVOKE_AGENT = "expense"
STEP_AGENTS = ("first", "second")
AGENT_NAMES = (ADMIN_AGENT, JOB_AGENT, INVOKE_AGENT, *STEP_AGENTS)
WORKFLOW_PREFIX = "nexus-entry-wf-"
PERSONAL_NAME = "Zorblax Personal Helper"
SUB_TASK = "check the sub-task"
INVOKE_SCHEMA = {
    "type": "object",
    "properties": {"expense_id": {"type": "string"},
                   "status": {"type": "string", "enum": ["draft"]}},
    "required": ["expense_id", "status"],
    "additionalProperties": False,
}
OUTPUT_ARGS = {
    "final_result": {"summary": "all good", "body_md": "# Report"},
    "submit_result": {"result": {"expense_id": "4711", "status": "draft"}},
}

Decision = tuple[str, dict[str, Any]] | None


def first_prompt(messages) -> str:
    for m in messages:
        for p in m.parts:
            if isinstance(p, UserPromptPart):
                return p.content if isinstance(p.content, str) else str(p.content)
    return ""


def answered(messages) -> bool:
    """A tool result is in the history: the agent has what it asked for."""
    return any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)


def finish(info) -> Decision:
    """The output tool when the run has one (job, invoke), else plain text."""
    if info.output_tools:
        name = info.output_tools[0].name
        return name, OUTPUT_ARGS.get(name, {})
    return None


def just_answer(messages, info) -> Decision:
    return finish(info)


class Script:
    """A ``FunctionModel`` (plain and streamed) that counts its requests.

    ``decide(messages, info)`` answers ``None`` for the text ``ok`` (or the
    output tool, see ``finish``) or ``(tool, args)`` for one tool call.
    ``usages`` are the (input, output) tokens of the plain requests, in turn
    (the last one repeats); a streamed request's usage is pydantic-ai's own
    estimate.
    """

    def __init__(self, decide: Callable[[Any, Any], Decision] = just_answer,
                 usages: tuple[tuple[int, int], ...] = ((10, 2),)) -> None:
        self.decide, self.usages, self.calls = decide, list(usages), 0

    def _usage(self) -> RequestUsage:
        i, o = self.usages.pop(0) if len(self.usages) > 1 else self.usages[0]
        return RequestUsage(input_tokens=i, output_tokens=o)

    def model(self) -> FunctionModel:
        def answer(messages, info):
            self.calls += 1
            choice = self.decide(messages, info)
            if choice is None:
                return ModelResponse(parts=[TextPart("ok")], usage=self._usage())
            tool, args = choice
            return ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id=f"c{self.calls}")],
                                 usage=self._usage())

        async def stream(messages, info):
            self.calls += 1
            choice = self.decide(messages, info)
            if choice is None:
                yield "ok"
            else:
                tool, args = choice
                yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args),
                                        tool_call_id=f"c{self.calls}")}

        return FunctionModel(answer, stream_function=stream, model_name="scripted")


@dataclass
class Outcome:
    output: Any                       # normalised: equal for equal runs
    agents: Counter                   # model requests per agent name
    run_id: str | None = None         # None: the entry point does not tell it

    @property
    def calls(self) -> int:
        return sum(self.agents.values())


@dataclass
class Ctx:
    monkeypatch: pytest.MonkeyPatch
    wrap: Callable[[Any], Any]
    extra: dict[str, Any] = field(default_factory=dict)

    def agent(self, script: Script, **kw: Any) -> Agent:
        return Agent(self.wrap(script.model()), **kw)


# --- environment --------------------------------------------------------------


async def forget_rows() -> None:
    """Remove every row a driver wrote (they share the session database)."""
    from agents.ide.models import IdeArtifact, IdeMessage, IdeSession, IdeWorkspaceFile
    async with SessionLocal() as s:
        for model in (ChatFile, ChatMessage, ChatConversation):
            await s.execute(delete(model))
        await s.execute(delete(ChatUserAgent).where(ChatUserAgent.principal == ALICE))
        ids = (await s.execute(select(Workflow.id).where(
            Workflow.name.like(f"{WORKFLOW_PREFIX}%")))).scalars().all()
        if ids:
            runs = (await s.execute(select(WorkflowRun.id).where(
                WorkflowRun.workflow_id.in_(ids)))).scalars().all()
            if runs:
                for model in (WorkflowStepRun, WorkflowItemRun):
                    await s.execute(delete(model).where(model.workflow_run_id.in_(runs)))
                await s.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(runs)))
            for wf_id in ids:
                await delete_workflow(s, wf_id, commit=False)
        await s.execute(delete(JobRun).where(JobRun.agent_name.in_(AGENT_NAMES)))
        await s.execute(delete(AgentConfig).where(AgentConfig.name.in_(AGENT_NAMES)))
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession):
            await s.execute(model.__table__.delete())
        await s.commit()


async def settle_tasks() -> None:
    """Let every run task the drivers started end (module-level task sets)."""
    await invoke.cancel_all_invoke_runs()
    await chat_run.cancel_all_chat_runs()
    tasks = [t for group in (job_runner._tasks, workflow_runner._tasks, invoke._tasks)
             for t in group if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    chat_run._turns.clear()


@pytest.fixture
async def entry_env(monkeypatch, real_agents_and_mcp):
    """The patches every driver needs; everything written is removed after."""
    for name in ("NEXUS_SEND_PAYLOAD", "NEXUS_FALLBACK_USE_CASE", "VCAP_SERVICES",
                 "VCAP_APPLICATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "model-a,model-b")
    monkeypatch.setattr(auth, "_validator", None)
    monkeypatch.setattr(auth, "_validator_checked", True)
    monkeypatch.setattr(chat_run, "catalog", chat_run.ModelCatalog())
    monkeypatch.setattr(chat_run, "HEARTBEAT_QUIET_SECONDS", 3600.0)
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))

    async def usable(agent, principal=None):
        return True
    monkeypatch.setattr(job_runner, "_has_usable_credentials", usable)
    await init_db()
    await forget_rows()
    yield
    await settle_tasks()
    await forget_rows()


def bound(inner: FastAPI) -> Callable:
    """``inner`` behind a wrapper that binds Alice and a JWT per request."""
    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt-of-alice")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)
    return asgi


def _build(**specialists: Any) -> BuildResult:
    return BuildResult(orchestrator=None, specialists=specialists, mcp_clients=[], configs=[])


# --- classic chat ---------------------------------------------------------------


async def drive_classic_chat(ctx: Ctx) -> Outcome:
    script = Script()
    agent = ctx.agent(script)
    ctx.monkeypatch.setattr(registry, "_build", BuildResult(
        orchestrator=agent, specialists={}, mcp_clients=[], configs=[]))
    ctx.monkeypatch.setattr(dynamic_chat_app, "_extra_models", [])
    ctx.monkeypatch.setattr(dynamic_chat_app, "_app", agent.to_web())
    body = {"trigger": "submit-message", "id": "c1", "messages": [
        {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}]}
    async with AsyncClient(transport=ASGITransport(app=dynamic_chat_app),
                           base_url="http://test") as c:
        r = await c.post("/api/chat", json=body)
    text = ""
    for line in r.text.split("\n"):
        if line.startswith("data: {"):
            event = json.loads(line[len("data: "):])
            if event.get("type") == "text-delta":
                text += event.get("delta", "")
    return Outcome({"status": r.status_code, "text": text}, Counter({ORCHESTRATOR: script.calls}))


# --- web chat ---------------------------------------------------------------------


def _chat_app() -> Callable:
    inner = FastAPI()
    inner.include_router(chat_routes.router)
    inner.include_router(chat_my_routes.router)
    install_validation_handler(inner)
    return bound(inner)


async def _chat_turn(conversation: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One turn in a new conversation; (assistant message id, stored outcome)."""
    async with AsyncClient(transport=ASGITransport(app=_chat_app()),
                           base_url="http://test") as c:
        r = await c.post("/chat/api/conversations", json=conversation)
        assert r.status_code in (200, 201), r.text
        cid = r.json()["id"]
        r = await c.post(f"/chat/api/conversations/{cid}/messages",
                         files=[("text", (None, "hello"))])
        assert r.status_code == 200, r.text
    mid = None
    for line in r.text.split("\n"):
        if line.startswith("data: ") and "assistant_message_id" in line:
            mid = json.loads(line[len("data: "):])["assistant_message_id"]
    assert mid is not None, r.text
    async with SessionLocal() as s:
        row = await s.get(ChatMessage, mid)
        stored = {"status": row.status, "content": row.content}
    return mid, stored


def _chat_registry(ctx: Ctx, orchestrator: Agent, **specialists: Any) -> None:
    ctx.monkeypatch.setattr(chat_run, "registry", SimpleNamespace(
        orchestrator=orchestrator, loaded=True,
        build=SimpleNamespace(specialists=specialists, shared_toolsets={}, in_flight=None)))


async def drive_webchat_orchestrator(ctx: Ctx) -> Outcome:
    script = Script()
    _chat_registry(ctx, ctx.agent(script))
    mid, stored = await _chat_turn({})
    return Outcome(stored, Counter({ORCHESTRATOR: script.calls}), f"chat-{mid}")


async def drive_webchat_admin_agent(ctx: Ctx) -> Outcome:
    async with SessionLocal() as s:
        await upsert_agent(s, name=ADMIN_AGENT, description="d", instructions="i",
                           mcp_servers=SERVERS, expose_chat=True)
    orch, spec = Script(), Script()
    _chat_registry(ctx, ctx.agent(orch), **{ADMIN_AGENT: ctx.agent(spec)})
    mid, stored = await _chat_turn({"agent_ref": f"admin:{ADMIN_AGENT}"})
    return Outcome(stored, Counter({ADMIN_AGENT: spec.calls, ORCHESTRATOR: orch.calls}),
                   f"chat-{mid}")


async def drive_webchat_personal_agent(ctx: Ctx) -> Outcome:
    async with SessionLocal() as s:      # a second run makes the agent anew
        await s.execute(delete(ChatUserAgent).where(ChatUserAgent.principal == ALICE))
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=_chat_app()),
                           base_url="http://test") as c:
        r = await c.post("/chat/api/my/agents", json={
            "name": PERSONAL_NAME, "description": "helps with invoices",
            "instructions": "Answer briefly."})
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
    script = Script()
    # A personal agent runs on the orchestrator's model when it names none.
    _chat_registry(ctx, ctx.agent(script))
    mid, stored = await _chat_turn({"agent_ref": f"mine:{aid}"})
    return Outcome(stored, Counter({PERSONAL_AGENT: script.calls}), f"chat-{mid}")


def _delegating(tool: str) -> Callable[[Any, Any], Decision]:
    def decide(messages, info):
        if answered(messages):
            return finish(info)
        return tool, {"query": "q"}
    return decide


async def drive_webchat_delegation(ctx: Ctx) -> Outcome:
    """The orchestrator delegates to a specialist (the registry's delegation tool)."""
    orch, spec = Script(_delegating(delegation_tool_name(ADMIN_AGENT))), Script()
    orchestrator, specialist = ctx.agent(orch), ctx.agent(spec)
    _attach_delegation_tool(orchestrator, specialist, SimpleNamespace(
        name=ADMIN_AGENT, description="d", mcp_servers=[]))
    _chat_registry(ctx, orchestrator)
    mid, stored = await _chat_turn({})
    return Outcome(stored, Counter({ORCHESTRATOR: orch.calls, ADMIN_AGENT: spec.calls}),
                   f"chat-{mid}")


# --- job, invoke, workflow ------------------------------------------------------------


def _deep_decide(messages, info) -> Decision:
    """The specialist runs one deep sub-agent (``task``), then hands in its report."""
    if SUB_TASK in first_prompt(messages):     # the sub-agent: plain text
        return None
    if answered(messages):
        return finish(info)
    return "task", {"description": SUB_TASK}


async def _job(ctx: Ctx, specialist: Agent) -> tuple[str, dict[str, Any]]:
    async with SessionLocal() as s:
        job = await upsert_agent(
            s, name=JOB_AGENT, description="d", instructions="i", mcp_servers=SERVERS,
            api_slug=JOB_AGENT, run_as_principal="svc@example.test")
        run = await create_job_run(s, agent=job, trigger="manual")
        run_id, job_id = run.id, job.id
    ctx.monkeypatch.setattr(registry, "_build", _build(**{JOB_AGENT: specialist}))
    await job_runner.execute_run(run_id, job_id)
    async with SessionLocal() as s:
        row = await s.get(JobRun, run_id)
        report = json.loads(row.report_json) if row.report_json else None
        return run_id, {"status": row.status, "summary": (report or {}).get("summary")}


async def drive_job(ctx: Ctx) -> Outcome:
    script = Script()
    run_id, stored = await _job(ctx, ctx.agent(script))
    return Outcome(stored, Counter({JOB_AGENT: script.calls}), run_id)


async def drive_job_deep_subagent(ctx: Ctx) -> Outcome:
    script = Script(_deep_decide)
    model = ctx.wrap(script.model())
    specialist = Agent(model, toolsets=[deep_toolset(
        DeepConfig(enabled=True), parent_toolsets=[], model=model,
        agent_name=JOB_AGENT, depth=0)])
    run_id, stored = await _job(ctx, specialist)
    return Outcome(stored, Counter({JOB_AGENT: script.calls}), run_id)


async def drive_invoke(ctx: Ctx) -> Outcome:
    from tests.test_invoke_api import bound_app, form, wait_done
    async with SessionLocal() as s:
        await upsert_agent(
            s, name=INVOKE_AGENT, description="d", instructions="i", mcp_servers=SERVERS,
            expose_chat=False, api_slug=INVOKE_AGENT, expose_invoke=True,
            result_schema=INVOKE_SCHEMA)
    script = Script()
    ctx.monkeypatch.setattr(invoke, "registry", SimpleNamespace(
        build=SimpleNamespace(specialists={INVOKE_AGENT: ctx.agent(script)})))
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        r = await c.post(f"/api/invoke/{INVOKE_AGENT}", **form())
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        answer = await wait_done(c, run_id)
    return Outcome({"status": answer["status"], "result": answer["result"]},
                   Counter({INVOKE_AGENT: script.calls}), run_id)


async def drive_workflow(ctx: Ctx) -> Outcome:
    async with SessionLocal() as s:
        for n in STEP_AGENTS:
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.test")
        name = f"{WORKFLOW_PREFIX}{uuid.uuid4().hex[:8]}"
        await upsert_workflow(
            s, name=name, description="d", enabled=True, branches=[],
            steps=[{"branch_key": None, "position": p, "agent_name": a,
                    "instructions": "go", "fan_out": False, "step_timeout_seconds": 60}
                   for p, a in enumerate(STEP_AGENTS, start=1)])
    scripts = {n: Script() for n in STEP_AGENTS}
    ctx.monkeypatch.setattr(registry, "_build", _build(
        **{n: ctx.agent(s) for n, s in scripts.items()}))
    async with SessionLocal() as s:
        wf = await get_workflow_by_name(s, name)
    run_id = await workflow_runner.start_workflow_run(wf, trigger="manual")
    await asyncio.gather(*[t for t in workflow_runner._tasks if not t.done()],
                         return_exceptions=True)
    async with SessionLocal() as s:
        row = await s.get(WorkflowRun, run_id)
        stored = {"status": row.status}
    return Outcome(stored, Counter({n: s.calls for n, s in scripts.items()}), run_id)


# --- A2A ---------------------------------------------------------------------------------


def _a2a_params() -> dict[str, Any]:
    return {"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}],
                        "messageId": f"m-{uuid.uuid4().hex}",
                        "contextId": f"ctx-{uuid.uuid4().hex}"}}


def _a2a_text(result: dict[str, Any]) -> str:
    texts = [p.get("text", "") for a in result.get("artifacts") or []
             for p in a.get("parts") or []]
    return "".join(texts)


async def drive_a2a_send(ctx: Ctx) -> Outcome:
    script = Script()
    ctx.monkeypatch.setattr(a2a, "registry", SimpleNamespace(orchestrator=ctx.agent(script)))
    token = current_principal.set(ALICE)
    try:
        out = await a2a._handle_message_send("r1", _a2a_params())
    finally:
        current_principal.reset(token)
    result = out["result"]
    return Outcome({"state": result["status"]["state"], "text": _a2a_text(result)},
                   Counter({ORCHESTRATOR: script.calls}), result["id"])


async def drive_a2a_stream(ctx: Ctx) -> Outcome:
    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    script = Script()
    ctx.monkeypatch.setattr(a2a, "registry", SimpleNamespace(orchestrator=ctx.agent(script)))
    token = current_principal.set(ALICE)
    try:
        frames = [json.loads(f[len("data: "):])["result"]
                  async for f in a2a._stream_message("r1", _a2a_params(), _Req())]
    finally:
        current_principal.reset(token)
    ids = {f.get("taskId") for f in frames} - {None}
    assert len(ids) == 1, frames
    states = [f["status"]["state"] for f in frames if "status" in f]
    text = "".join(_a2a_text({"artifacts": [f["artifact"]]}) for f in frames if "artifact" in f)
    return Outcome({"states": states, "text": text}, Counter({ORCHESTRATOR: script.calls}),
                   next(iter(ids)))


# --- IDE ------------------------------------------------------------------------------------


async def drive_ide(ctx: Ctx) -> Outcome:
    from agents.ide import runner
    from agents.ide.store import create_session
    script = Script()
    name = runner._agent_name("change")
    ctx.monkeypatch.setattr(registry, "_build", _build(**{name: ctx.agent(script)}))
    async with SessionLocal() as db:
        s = await create_session(db, owner=ALICE, title="t", target="T1")
        s.stage = "chat"
        await db.commit()
        sid = s.id
    events: list[tuple[str, dict]] = []
    await runner.run_stage(sid, ALICE, "go", request_changes=None,
                           emit=lambda k, d: events.append((k, d)))
    run_id = next(d["run_id"] for k, d in events if k == "run")
    text = "".join(d.get("text", d.get("delta", "")) for k, d in events if k == "text")
    kinds = sorted({k for k, _ in events} - {"usage"})
    return Outcome({"kinds": kinds, "text": text, "errors": [d for k, d in events if k == "error"]},
                   Counter({name: script.calls}), run_id)


DRIVERS: dict[str, Callable[[Ctx], Awaitable[Outcome]]] = {
    "classic_chat": drive_classic_chat,
    "webchat_orchestrator": drive_webchat_orchestrator,
    "webchat_admin_agent": drive_webchat_admin_agent,
    "webchat_personal_agent": drive_webchat_personal_agent,
    "webchat_delegation": drive_webchat_delegation,
    "job": drive_job,
    "job_deep_subagent": drive_job_deep_subagent,
    "invoke": drive_invoke,
    "workflow": drive_workflow,
    "a2a_send": drive_a2a_send,
    "a2a_stream": drive_a2a_stream,
    "ide": drive_ide,
}
