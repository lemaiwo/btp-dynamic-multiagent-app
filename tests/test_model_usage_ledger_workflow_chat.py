"""The token ledger (``model_usage``) for workflow steps and web chat turns.

A workflow agent step binds its own collector, so a fan-out branch's step
counts only what it ran: rows ``source="workflow_step"``, ``ref_id`` = the
step run, ``parent_id`` = the workflow run. A chat turn binds the collector
of its ``Turn``: rows ``source="chat"``, ``ref_id`` = the assistant message,
``parent_id`` = the conversation. The fakes call ``model_usage.add`` as the
model wrapper does for every answered request.

The ledger is written after the outcome's own commit: a ledger failure is
logged and never loses the step's or the turn's outcome.

Run:  .venv/bin/python -m pytest tests/test_model_usage_ledger_workflow_chat.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete, select, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

import agents.db as db_module  # noqa: E402
import agents.workflow_runner as wr  # noqa: E402
from agents import auth, job_runner, model_usage  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    AgentConfig,
    ChatConversation,
    ChatFile,
    ChatMessage,
    ModelUsage,
    SessionLocal,
    Workflow,
    WorkflowBranch,
    WorkflowItemRun,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepRun,
    finish_step_run,
    get_workflow_by_name,
    init_db,
    list_step_runs,
    upsert_agent,
    upsert_workflow,
)
from agents.registry import BuildResult, registry  # noqa: E402
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import routes, run, store  # noqa: E402

SERVERS = [{"url": "https://x.example.com/mcp", "auth_mode": "none"}]
ALICE = "alice@example.com"
API = "/chat/api"


def answered(n_in: int, n_out: int, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=n_in, output_tokens=n_out, cache_read_tokens=cached)


def spend_two_models() -> None:
    """Two requests of model-a and one of model-b, as the wrapper reports them."""
    model_usage.add("model-a", answered(100, 10, 5))
    model_usage.add("model-a", answered(50, 5))
    model_usage.add("model-b", answered(7, 3))


TWO_MODELS = [("model-a", 150, 15, 5, 2), ("model-b", 7, 3, 0, 1)]


async def ledger(source: str) -> list[ModelUsage]:
    async with SessionLocal() as s:
        rows = await s.execute(select(ModelUsage).where(ModelUsage.source == source)
                               .order_by(ModelUsage.ref_id, ModelUsage.model))
        return list(rows.scalars().all())


def counts(rows: list[ModelUsage]) -> list[tuple]:
    return [(r.model, r.input_tokens, r.output_tokens, r.cache_read_tokens, r.requests)
            for r in rows]


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(job_runner, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(wr, "FINALIZE_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(job_runner, "_has_usable_credentials",
                        lambda agent, principal=None: asyncio.sleep(0, result=True),
                        raising=False)
    saved_auth = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "model-a,model-b")
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    monkeypatch.setattr(run, "HEARTBEAT_QUIET_SECONDS", 3600.0)
    saved_build = registry._build
    await init_db()
    async with SessionLocal() as s:
        for model in (ModelUsage, WorkflowStepRun, WorkflowItemRun, WorkflowRun,
                      WorkflowStep, WorkflowBranch, Workflow, AgentConfig,
                      ChatFile, ChatMessage, ChatConversation):
            await s.execute(delete(model))
        await s.commit()
        for n in ("first", "second"):
            await upsert_agent(s, name=n, description=n, instructions=n,
                               mcp_servers=SERVERS, run_as_principal="svc@example.com")
    yield
    await run.cancel_all_chat_runs()
    run._turns.clear()
    registry._build = saved_build
    auth._validator, auth._validator_checked = saved_auth


# ---------------------------------------------------------------------------
# Workflow steps
# ---------------------------------------------------------------------------
class FakeResult:
    def __init__(self, output):
        self.output = output


class SpendingAgent:
    """A built specialist that spends on two models, then answers or fails."""

    def __init__(self, fail_with: BaseException | None = None, spend: bool = True):
        self.fail_with = fail_with
        self.spend = spend

    async def run(self, prompt, **kwargs):
        if self.spend:
            spend_two_models()
            kwargs["usage"].incr(RunUsage(requests=3, input_tokens=157, output_tokens=18,
                                          cache_read_tokens=5))
        if self.fail_with is not None:
            raise self.fail_with
        return FakeResult('{"a": "b"}')


def install(mapping):
    registry._build = BuildResult(
        orchestrator=None, specialists=dict(mapping), mcp_clients=[], configs=[]
    )


def step(position, kind="agent", agent="", config=None):
    return {"branch_key": None, "position": position, "agent_name": agent,
            "instructions": f"do step {position}", "fan_out": False,
            "step_timeout_seconds": 60, "kind": kind, "config": config or {}}


async def run_workflow(name, steps) -> str:
    async with SessionLocal() as s:
        await upsert_workflow(s, name=name, description="d", enabled=True,
                              branches=[], skip_seen_items=False, steps=steps)
        wf = await get_workflow_by_name(s, name)
    run_id = await wr.start_workflow_run(wf, trigger="manual")
    await asyncio.gather(*[t for t in wr._tasks if not t.done()], return_exceptions=True)
    return run_id


async def steps_of(run_id):
    async with SessionLocal() as s:
        return {r.position: r for r in await list_step_runs(s, run_id)}


async def test_an_agent_step_stores_one_row_per_model_and_a_transform_none():
    install({"first": SpendingAgent(), "second": SpendingAgent(spend=False)})
    run_id = await run_workflow("line", [
        step(1, agent="first"),
        step(2, kind="transform", config={"extract_json": "a"}),
        step(3, agent="second"),
    ])
    steps = await steps_of(run_id)
    rows = await ledger("workflow_step")
    assert counts(rows) == TWO_MODELS
    assert {r.ref_id for r in rows} == {steps[1].id}
    assert {r.parent_id for r in rows} == {run_id}
    # The ledger agrees with the step row's own counts (one figure in the UI).
    assert steps[1].requests == sum(r.requests for r in rows)


@pytest.mark.parametrize("failure, status", [
    (RuntimeError("boom"), "failed"),
    (asyncio.TimeoutError(), "failed"),
    (asyncio.CancelledError(), "interrupted"),
])
async def test_a_step_that_ends_badly_after_the_model_keeps_its_rows(failure, status):
    install({"first": SpendingAgent(fail_with=failure)})
    run_id = await run_workflow(f"bad-{status}-{type(failure).__name__}",
                                [step(1, agent="first")])
    steps = await steps_of(run_id)
    assert steps[1].status == status
    rows = await ledger("workflow_step")
    assert counts(rows) == TWO_MODELS
    assert {(r.ref_id, r.parent_id) for r in rows} == {(steps[1].id, run_id)}


async def test_a_step_finished_twice_keeps_one_set_of_rows():
    install({"first": SpendingAgent(spend=False)})
    run_id = await run_workflow("twice", [step(1, agent="first")])
    step_id = (await steps_of(run_id))[1].id
    rows = [{"model": "model-a", "input_tokens": 1, "output_tokens": 2,
             "cache_read_tokens": 0, "requests": 1}]
    async with SessionLocal() as s:
        await finish_step_run(s, step_id, status="success", output="x", model_usage=rows)
    async with SessionLocal() as s:
        await finish_step_run(s, step_id, status="failed", error="e", model_usage=rows)
    assert counts(await ledger("workflow_step")) == [("model-a", 1, 2, 0, 1)]


async def test_a_ledger_failure_does_not_lose_the_steps_outcome(monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(db_module, "replace_model_usage", broken)
    install({"first": SpendingAgent()})
    run_id = await run_workflow("ledger-down", [step(1, agent="first")])
    steps = await steps_of(run_id)
    assert steps[1].status == "success" and steps[1].requests == 3
    assert await ledger("workflow_step") == []


async def test_the_collector_is_unbound_after_a_step():
    install({"first": SpendingAgent()})
    await run_workflow("unbound", [step(1, agent="first")])
    assert model_usage.current() is None


# ---------------------------------------------------------------------------
# Web chat turns
# ---------------------------------------------------------------------------
def bound_app() -> Any:
    inner = FastAPI()
    inner.include_router(routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt-of-alice")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    return asgi


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        yield c


class Script:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def text(self, messages, info):
        spend_two_models()
        yield "Answer"

    async def fails(self, messages, info):
        spend_two_models()
        raise RuntimeError("backend refused")
        yield ""  # pragma: no cover

    async def blocks(self, messages, info):
        spend_two_models()
        yield "partial "
        self.started.set()
        await self.release.wait()
        yield "rest"


def use_model(monkeypatch, stream) -> None:
    agent = Agent(FunctionModel(stream_function=stream, model_name="orchestrator-model"))
    monkeypatch.setattr(run, "registry", SimpleNamespace(orchestrator=agent, loaded=True))


def form(text: str = "Hello") -> dict[str, Any]:
    return {"files": [("text", (None, text))]}


async def new_conversation(client) -> str:
    r = await client.post(f"{API}/conversations", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def assistant_id(client, cid: str) -> str:
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    return detail["messages"][1]["id"]


async def test_a_done_turn_stores_one_row_per_model(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, Script().text)
    cid = await new_conversation(client)
    r = await client.post(f"{API}/conversations/{cid}/messages", **form())
    assert r.status_code == 200 and '"status": "done"' in r.text
    mid = await assistant_id(client, cid)
    rows = await ledger("chat")
    assert counts(rows) == TWO_MODELS
    assert {(r.ref_id, r.parent_id) for r in rows} == {(mid, cid)}
    assert model_usage.current() is None


async def test_a_turn_that_failed_after_the_model_keeps_its_rows(client, monkeypatch,
                                                                 real_agents_and_mcp):
    use_model(monkeypatch, Script().fails)
    cid = await new_conversation(client)
    await client.post(f"{API}/conversations/{cid}/messages", **form())
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert detail["messages"][1]["status"] == "failed"
    rows = await ledger("chat")
    assert counts(rows) == TWO_MODELS
    assert {(r.ref_id, r.parent_id) for r in rows} == {(detail["messages"][1]["id"], cid)}


async def test_a_canceled_turn_keeps_its_rows(client, monkeypatch, real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, script.blocks)
    cid = await new_conversation(client)
    post = asyncio.create_task(client.post(f"{API}/conversations/{cid}/messages", **form()))
    await asyncio.wait_for(script.started.wait(), 5)
    assert (await client.post(f"{API}/conversations/{cid}/cancel")).json() == {"canceled": True}
    await asyncio.wait_for(post, 5)
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert detail["messages"][1]["status"] == "canceled"
    rows = await ledger("chat")
    assert counts(rows) == TWO_MODELS
    assert {(r.ref_id, r.parent_id) for r in rows} == {(detail["messages"][1]["id"], cid)}


async def test_a_ledger_failure_does_not_lose_the_turn(client, monkeypatch,
                                                       real_agents_and_mcp):
    async def broken(*a, **kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(db_module, "replace_model_usage", broken)
    use_model(monkeypatch, Script().text)
    cid = await new_conversation(client)
    await client.post(f"{API}/conversations/{cid}/messages", **form())
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert detail["status"] == "idle"
    assert detail["messages"][1]["status"] == "done"
    assert detail["messages"][1]["content"] == "Answer"
    assert await ledger("chat") == []


async def test_finish_turn_replaces_the_rows_of_its_message(client):
    cid = await new_conversation(client)
    rows = [{"model": "model-a", "input_tokens": 1, "output_tokens": 1,
             "cache_read_tokens": 0, "requests": 1}]
    for _ in range(2):
        async with SessionLocal() as s:
            await store.finish_turn(s, conversation_id=cid, message_id="m-1", status="done",
                                    content="x", parts=[], model_json=None, error=None,
                                    title=None, model_usage=rows)
    assert counts(await ledger("chat")) == [("model-a", 1, 1, 0, 1)]


async def test_a_chat_purge_leaves_the_ledger(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, Script().text)
    cid = await new_conversation(client)
    await client.post(f"{API}/conversations/{cid}/messages", **form())
    async with SessionLocal() as s:
        await s.execute(update(ChatConversation).where(ChatConversation.id == cid).values(
            updated_at=datetime.now(timezone.utc) - timedelta(days=400)))
        await s.commit()
    async with SessionLocal() as s:
        assert await store.purge_older_than(s, 180) == 1
    async with SessionLocal() as s:
        assert await s.get(ChatConversation, cid) is None
    assert counts(await ledger("chat")) == TWO_MODELS
    assert json.dumps([r.parent_id for r in await ledger("chat")]) == json.dumps([cid, cid])
