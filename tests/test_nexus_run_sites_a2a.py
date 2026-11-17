"""Run id at the classic chat and the A2A server.

A2A: the run id is the task id the caller is answered with. Classic chat: one
id per turn (a fresh one, ``uuid4().hex``), shared by every model call of it.
The models record the bound id; nothing leaves the process.

Run:  .venv/bin/python -m pytest tests/test_nexus_run_sites_a2a.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

from agents import a2a  # noqa: E402
from agents.chat_app import dynamic_chat_app  # noqa: E402
from agents.nexus.context import current_nexus_run_id  # noqa: E402
from agents.registry import BuildResult, registry  # noqa: E402


def _params() -> dict:
    return {"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}],
                        "messageId": f"m-{uuid.uuid4().hex}",
                        "contextId": f"ctx-{uuid.uuid4().hex}"}}


class _Result:
    output = "done"

    def all_messages(self):
        return []


class _Orchestrator:
    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def run(self, text, **kwargs):  # noqa: ARG002
        self.seen.append(current_nexus_run_id.get())
        return _Result()


class _Registry:
    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator


async def test_a2a_send_uses_the_task_id_as_run_id(monkeypatch):
    orch = _Orchestrator()
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    out = await a2a._handle_message_send("r1", _params())
    assert orch.seen == [out["result"]["id"]]


async def test_a2a_stream_uses_the_task_id_as_run_id(monkeypatch):
    class _Req:
        async def is_disconnected(self) -> bool:
            return False
    import json
    orch = _Orchestrator()
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    frames = [f async for f in a2a._stream_message("r1", _params(), _Req())]
    ids = {json.loads(f[len("data: "):])["result"].get("taskId") for f in frames}
    ids.discard(None)
    assert len(ids) == 1 and orch.seen == [next(iter(ids))]


async def test_classic_chat_turn_has_one_fresh_run_id(monkeypatch, real_agents_and_mcp):
    seen: list[str | None] = []

    def answer(messages, info):
        seen.append(current_nexus_run_id.get())
        return ModelResponse(parts=[TextPart("ok")])

    async def stream(messages, info):
        seen.append(current_nexus_run_id.get())
        yield "ok"

    agent = Agent(FunctionModel(answer, stream_function=stream, model_name="m"))
    monkeypatch.setattr(registry, "_build", BuildResult(
        orchestrator=agent, specialists={}, mcp_clients=[], configs=[]))
    monkeypatch.setattr(dynamic_chat_app, "_extra_models", [])
    # No refresh(): it would build real models from AI Core.
    monkeypatch.setattr(dynamic_chat_app, "_app", agent.to_web())
    body = {"trigger": "submit-message", "id": "c1", "messages": [
        {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hi"}]}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dynamic_chat_app),
                                 base_url="http://test") as client:
        for _ in range(2):
            r = await client.post("/api/chat", json=body)
            assert r.status_code == 200, r.text
    assert len(seen) == 2 and all(seen) and seen[0] != seen[1]
    assert all(len(s) <= 64 for s in seen)
