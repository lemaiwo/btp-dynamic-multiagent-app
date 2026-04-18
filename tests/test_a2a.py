"""Smoke tests for the A2A (Agent-to-Agent) router.

Boots the real FastAPI app (with stubbed SAP AI Core + MCP) and
exercises the A2A endpoints over an ASGI transport:

- GET  /.well-known/agent-card.json
- GET  /.well-known/agent.json (legacy)
- POST /a2a  message/send
- POST /a2a  tasks/get, tasks/cancel
- POST /a2a  unknown method returns JSON-RPC error

Run:  python tests/test_a2a.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = ROOT / "tests" / "_test_a2a.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

# Stub SAP AI Core + MCP before importing the app
import agents.shared as shared  # noqa: E402


class _FakeModel:
    model_name = "fake"


shared.get_model = lambda: _FakeModel()  # type: ignore[assignment]


class _FakeMCP:
    def __init__(self, name, base_url):
        self.name = name
        self.base_url = base_url


shared.create_mcp_server = lambda name, base_url: _FakeMCP(name, base_url)  # type: ignore[assignment]

# Patch pydantic_ai.Agent so its __init__ accepts the fake model and toolsets
# and .run / .to_web don't hit any external service.
import pydantic_ai  # noqa: E402

_orig_init = pydantic_ai.Agent.__init__


def _patched_init(self, model=None, **kwargs):
    kwargs.pop("toolsets", None)
    _orig_init(self, model="test", **kwargs)


pydantic_ai.Agent.__init__ = _patched_init  # type: ignore[method-assign]


class _FakeRunResult:
    def __init__(self, text: str):
        self.output = text

    def all_messages(self):
        return []


async def _fake_run(self, prompt, message_history=None, **kwargs):  # noqa: ARG001
    return _FakeRunResult(f"echo: {prompt}")


pydantic_ai.Agent.run = _fake_run  # type: ignore[method-assign]


def _fake_to_web(self, html_source=None):  # noqa: ARG001
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"fake-chat"})
    return app


pydantic_ai.Agent.to_web = _fake_to_web  # type: ignore[method-assign]


import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app import app  # noqa: E402


PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


async def main():
    # Run lifespan manually (init DB, seed, build orchestrator).
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://localhost",
        ) as client:
            await _run_tests(client)


async def _run_tests(c):
    print("Agent Card")
    r = await c.get("/.well-known/agent-card.json")
    check(r.status_code == 200, f"agent-card returns 200 (got {r.status_code})")
    card = r.json()
    check(card.get("protocolVersion") == "0.3.0", "protocolVersion is 0.3.0")
    check("url" in card and card["url"].endswith("/a2a"), "card.url ends with /a2a")
    check(isinstance(card.get("skills"), list) and len(card["skills"]) >= 1, "skills non-empty")
    check(card["capabilities"]["streaming"] is True, "capabilities.streaming=true")
    check("orchestrate" in {s["id"] for s in card["skills"]}, "has orchestrate skill")
    check("securitySchemes" in card, "securitySchemes present")

    r = await c.get("/.well-known/agent.json")
    check(r.status_code == 200, "legacy /.well-known/agent.json also works")

    print("\nmessage/send")
    payload = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "hello"}],
                "messageId": "m-1",
            }
        },
    }
    r = await c.post("/a2a", json=payload)
    check(r.status_code == 200, f"message/send returns 200 (got {r.status_code})")
    body = r.json()
    check(body.get("id") == "req-1", "response id echoes request id")
    check("result" in body, "response has result")
    task = body["result"]
    check(task.get("kind") == "task", "result kind is task")
    check(task["status"]["state"] == "completed", "task state completed")
    task_id = task["id"]
    context_id = task["contextId"]
    agent_msg = task["status"]["message"]
    agent_text = agent_msg["parts"][0]["text"]
    check(agent_text.startswith("echo: hello"), f"agent text is 'echo: hello' (got {agent_text!r})")

    print("\ntasks/get")
    r = await c.post("/a2a", json={
        "jsonrpc": "2.0", "id": "g-1", "method": "tasks/get",
        "params": {"id": task_id},
    })
    check(r.status_code == 200, "tasks/get 200")
    check(r.json()["result"]["id"] == task_id, "tasks/get returns the task")

    print("\ntasks/cancel on completed is a no-op terminal")
    r = await c.post("/a2a", json={
        "jsonrpc": "2.0", "id": "c-1", "method": "tasks/cancel",
        "params": {"id": task_id},
    })
    check(r.status_code == 200, "tasks/cancel 200")
    check(r.json()["result"]["status"]["state"] == "completed", "completed task not re-canceled")

    print("\nMulti-turn: contextId preserved")
    r = await c.post("/a2a", json={
        "jsonrpc": "2.0", "id": "req-2", "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "follow-up"}],
            "messageId": "m-2",
            "contextId": context_id,
        }},
    })
    check(r.status_code == 200, "follow-up 200")
    check(r.json()["result"]["contextId"] == context_id, "same contextId returned")

    print("\nUnknown method")
    r = await c.post("/a2a", json={
        "jsonrpc": "2.0", "id": "u-1", "method": "no/such",
        "params": {},
    })
    check(r.status_code == 200, "unknown method 200")
    body = r.json()
    check(body["error"]["code"] == -32601, "unknown method returns -32601")

    print("\nInvalid JSON-RPC envelope")
    r = await c.post("/a2a", json={"jsonrpc": "1.0", "id": 1, "method": "message/send"})
    check(r.status_code == 200, "invalid version returns 200 with JSON-RPC error")
    check(r.json()["error"]["code"] == -32600, "invalid version code -32600")

    print("\nEmpty text parts")
    r = await c.post("/a2a", json={
        "jsonrpc": "2.0", "id": "e-1", "method": "message/send",
        "params": {"message": {"role": "user", "parts": []}},
    })
    check(r.json()["error"]["code"] == -32602, "empty message returns -32602")

    print("\nmessage/stream SSE")
    async with c.stream("POST", "/a2a", json={
        "jsonrpc": "2.0", "id": "s-1", "method": "message/stream",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "stream me"}],
            "messageId": "m-3",
        }},
    }) as r:
        check(r.status_code == 200, "stream 200")
        check(r.headers.get("content-type", "").startswith("text/event-stream"), "content-type SSE")
        events = []
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        check(len(events) >= 3, f"emits >=3 SSE events (got {len(events)})")
        check(events[0]["result"]["kind"] == "task", "first event is task")
        check(events[-1]["result"].get("final") is True, "last event has final=true")
        check(events[-1]["result"]["status"]["state"] == "completed", "final state completed")

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
