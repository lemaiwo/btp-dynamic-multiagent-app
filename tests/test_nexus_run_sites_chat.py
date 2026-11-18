"""Run id and agent name at the web chat and the IDE run sites.

The model records what the agent register would be told (the bound run id and
agent name) on every model call; nothing leaves the process.

Run:  .venv/bin/python -m pytest tests/test_nexus_run_sites_chat.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import ChatConversation, ChatFile, ChatMessage, SessionLocal, init_db  # noqa: E402
from agents.nexus.context import (  # noqa: E402
    ORCHESTRATOR,
    PERSONAL_AGENT,
    current_agent_name,
    current_nexus_run_id,
)
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import routes, run, user_agents  # noqa: E402

API = "/chat/api"
ALICE = "alice@example.com"


class Seen:
    """What the bound ContextVars said at every model call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    def model(self) -> FunctionModel:
        def answer(messages, info):
            self.calls.append((current_nexus_run_id.get(), current_agent_name.get()))
            return ModelResponse(parts=[TextPart("ok")])
        async def stream(messages, info):
            self.calls.append((current_nexus_run_id.get(), current_agent_name.get()))
            yield "ok"
        return FunctionModel(answer, stream_function=stream, model_name="m")


@pytest.fixture
async def client():
    inner = FastAPI()
    inner.include_router(routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        p = current_principal.set(ALICE)
        j = current_jwt.set("jwt")
        try:
            await inner(scope, receive, send)
        finally:
            current_principal.reset(p)
            current_jwt.reset(j)

    async with AsyncClient(transport=ASGITransport(app=asgi), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    saved = (auth._validator, auth._validator_checked)
    auth._validator, auth._validator_checked = None, True
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "model-a,model-b")
    monkeypatch.setattr(run, "catalog", run.ModelCatalog())
    monkeypatch.setattr(run, "HEARTBEAT_QUIET_SECONDS", 3600.0)
    await init_db()
    async with SessionLocal() as s:
        for model in (ChatFile, ChatMessage, ChatConversation):
            await s.execute(delete(model))
        await s.commit()
    yield
    await run.cancel_all_chat_runs()
    run._turns.clear()
    auth._validator, auth._validator_checked = saved


async def _turn(client) -> str:
    r = await client.post(f"{API}/conversations", json={})
    cid = r.json()["id"]
    r = await client.post(f"{API}/conversations/{cid}/messages",
                          files=[("text", (None, "hello"))])
    assert r.status_code == 200
    for line in r.text.split("\n"):
        if line.startswith("data: ") and "assistant_message_id" in line:
            import json
            return json.loads(line[6:])["assistant_message_id"]
    raise AssertionError(r.text)


async def test_orchestrator_turn_logs_chat_id_and_orchestrator(client, monkeypatch,
                                                              real_agents_and_mcp):
    seen = Seen()
    agent = Agent(seen.model())
    monkeypatch.setattr(run, "registry", SimpleNamespace(orchestrator=agent, loaded=True))
    mid = await _turn(client)
    assert seen.calls == [(f"chat-{mid}", ORCHESTRATOR)]


async def test_admin_agent_run_straight_logs_its_own_name(client, monkeypatch,
                                                         real_agents_and_mcp):
    seen = Seen()
    specialist = Agent(seen.model())

    async def fake_runner(data):
        return run.Runner(specialist, "invoice-assistant", None, "m",
                          nexus_agent="invoice-assistant")
    monkeypatch.setattr(run, "resolve_runner", fake_runner)
    mid = await _turn(client)
    assert seen.calls == [(f"chat-{mid}", "invoice-assistant")]


async def test_resolve_runner_names_the_agent_per_kind(monkeypatch):
    orch = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])))
    spec = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("x")])))
    build = SimpleNamespace(specialists={"invoice-assistant": spec})
    monkeypatch.setattr(run, "registry",
                        SimpleNamespace(orchestrator=orch, loaded=True, build=build))
    data: Any = SimpleNamespace(principal=ALICE, agent_ref=None, access="basic",
                                model_name=None)

    def target(kind, name):
        async def resolve(principal, ref, access):
            return SimpleNamespace(kind=kind, name=name,
                                   user_row=SimpleNamespace(model_name=None))
        return resolve

    async def personal(row, *, model, build):
        return orch, None
    monkeypatch.setattr(user_agents, "build_personal_agent", personal)
    monkeypatch.setattr(user_agents, "resolve_agent_ref", target("orchestrator", None))
    assert (await run.resolve_runner(data)).nexus_agent == ORCHESTRATOR
    monkeypatch.setattr(user_agents, "resolve_agent_ref",
                        target("admin", "invoice-assistant"))
    assert (await run.resolve_runner(data)).nexus_agent == "invoice-assistant"
    monkeypatch.setattr(user_agents, "resolve_agent_ref", target("mine", "My Secret Name"))
    r = await run.resolve_runner(data)
    assert r.nexus_agent == PERSONAL_AGENT and r.label == "My Secret Name"


# --- IDE ---------------------------------------------------------------------


class _IdeSpecialist:
    """Stands in for the registry's Agent: records the bound ids, answers text."""

    def __init__(self) -> None:
        self.seen: list[tuple[str | None, str]] = []

    async def run(self, prompt, **kwargs):
        self.seen.append((current_nexus_run_id.get(), current_agent_name.get()))
        return SimpleNamespace(output="done", usage=lambda: None,
                               new_messages=lambda: [])


async def test_ide_run_logs_its_run_id_and_agent_name():
    from agents.ide import runner
    from agents.ide.models import IdeArtifact, IdeMessage, IdeSession, IdeWorkspaceFile
    from agents.ide.store import create_session
    from agents.registry import BuildResult, registry

    async with SessionLocal() as db:
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession):
            await db.execute(model.__table__.delete())
        await db.commit()
    stub = _IdeSpecialist()
    saved = registry._build
    registry._build = BuildResult(orchestrator=None, specialists={
        runner._agent_name("change"): stub}, mcp_clients=[], configs=[])
    try:
        async with SessionLocal() as db:
            s = await create_session(db, owner="alice", title="t", target="T1")
            s.stage = "chat"
            await db.commit()
            sid = s.id
        events: list[tuple[str, dict]] = []
        await runner.run_stage(sid, "alice", "go", request_changes=None,
                               emit=lambda k, d: events.append((k, d)))
    finally:
        registry._build = saved
    run_id = next(d["run_id"] for k, d in events if k == "run")
    assert stub.seen == [(run_id, runner._agent_name("change"))]
