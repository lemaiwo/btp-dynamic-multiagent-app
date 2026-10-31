"""The web chat API: ``/chat/api/...`` (``agents/webchat``).

The routers are mounted on a bare app behind a small ASGI wrapper that binds
``current_principal`` / ``current_jwt`` from a test header (the real app's
middleware binds them from the validated JWT). One test drives the real app,
so the include lines in ``app.py`` are covered too.

The orchestrator is a real pydantic-ai ``Agent`` on a streaming
``FunctionModel`` set on ``run.registry``: it sees the prompt, the history
and the per-run document tools exactly as on AI Core.

Run:  .venv/bin/python -m pytest tests/test_webchat_api.py -q
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from pydantic_ai import Agent, RunContext  # noqa: E402
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart  # noqa: E402
from pydantic_ai.models.function import DeltaToolCall, FunctionModel  # noqa: E402

from agents import auth  # noqa: E402
from agents.auth import current_jwt, current_principal  # noqa: E402
from agents.db import (  # noqa: E402
    ChatConversation,
    ChatFile,
    ChatMessage,
    SessionLocal,
    init_db,
)
from agents.progress import (  # noqa: E402
    current_delegation,
    report_delegation_end,
    report_delegation_start,
    report_tool_end,
    report_tool_start,
)
from agents.validation_errors import install_validation_handler  # noqa: E402
from agents.webchat import routes, run, store, uploads  # noqa: E402

ALICE = "alice@example.com"
BOB = "bob@example.com"
API = "/chat/api"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"PNG-CONTENT" + b"\0" * 32
SECRET = "S3cr3t-value-xyz"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def bound_app() -> Any:
    inner = FastAPI()
    inner.include_router(routes.page_router)
    inner.include_router(routes.router)
    install_validation_handler(inner)

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope["headers"])
        user = headers.get(b"x-test-user", ALICE.encode()).decode()
        p = current_principal.set(user)
        j = current_jwt.set(f"jwt-of-{user}")
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


class Script:
    """A streaming FunctionModel script: records what the model was sent."""

    def __init__(self) -> None:
        self.seen: list[list[Any]] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def workbook_then_text(self, messages, info):
        self.seen.append(list(messages))
        last = messages[-1]
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if returns:
            url = returns[0].content["download_url"]
            yield "Here is "
            yield f"[the workbook]({url})."
            return
        yield {0: DeltaToolCall(
            name="create_excel_workbook",
            json_args=json.dumps({"filename": "../report", "sheets": [
                {"name": "Data", "columns": ["a", "b"], "rows": [["=1+1", 2]]}]}),
            tool_call_id="call-1",
        )}

    async def text(self, messages, info):
        self.seen.append(list(messages))
        yield "Answer "
        yield f"{len(self.seen)}"

    async def blocks(self, messages, info):
        self.seen.append(list(messages))
        yield "partial "
        self.started.set()
        await self.release.wait()
        yield "rest"

    async def fails(self, messages, info):
        self.seen.append(list(messages))
        raise RuntimeError(f"backend refused https://user:{SECRET}@host.example/x?token={SECRET}")
        yield ""  # pragma: no cover

    async def delegates(self, messages, info):
        self.seen.append(list(messages))
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            yield "Three systems."
            return
        yield {0: DeltaToolCall(name="delegate_btp_admin",
                                json_args=json.dumps({"query": f"list systems token={SECRET}"}),
                                tool_call_id="deleg-1")}

    async def specialist_tool(self, messages, info):
        self.seen.append(list(messages))
        report_tool_start("btp-admin", "raw-1", "SAPQuery", {"q": "x"})
        report_tool_end("btp-admin", "raw-1", ok=True, output="3 rows")
        yield "done"


SPEC_USAGE = {"input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 7,
              "requests": 2, "model": "spec-model"}


async def delegate_btp_admin(ctx: RunContext, query: str) -> str:
    """A delegation as ``agents.registry`` reports it: start with the query,
    ``current_delegation`` bound around the specialist's work, end with ok,
    output preview and the specialist's own usage."""
    report_delegation_start("btp-admin", "Consulting the btp-admin specialist…",
                            delegation_id=ctx.tool_call_id, query=query)
    token = current_delegation.set(ctx.tool_call_id)
    try:
        report_tool_start("btp-admin", "raw-1", "SAPQuery", {"q": "x"})
        report_tool_end("btp-admin", "raw-1", ok=True, output="3 rows")
    finally:
        current_delegation.reset(token)
    report_delegation_end("btp-admin", delegation_id=ctx.tool_call_id, ok=True,
                          output="3 systems", usage=dict(SPEC_USAGE), duration_ms=5)
    return "3 systems"


def use_model(monkeypatch, real_agents_and_mcp, stream) -> Agent:
    agent = Agent(FunctionModel(stream_function=stream, model_name="orchestrator-model"))
    monkeypatch.setattr(run, "registry", SimpleNamespace(orchestrator=agent, loaded=True))
    return agent


def frames(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def form(text: str | None = "Make me a workbook", files=(), model: str | None = None,
         **extra) -> dict[str, Any]:
    data = {}
    if text is not None:
        data["text"] = text
    if model is not None:
        data["model"] = model
    data.update(extra)
    # Text fields as (None, value) parts: always multipart, also without files.
    return {"files": [(k, (None, v)) for k, v in data.items()]
            + [("files", f) for f in files]}


async def new_conversation(client, user: str = ALICE, **body) -> str:
    r = await client.post(f"{API}/conversations", json=body, headers={"x-test-user": user})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def conversation_row(cid: str) -> ChatConversation:
    async with SessionLocal() as s:
        return await s.get(ChatConversation, cid)


# ---------------------------------------------------------------------------
# /me and conversations
# ---------------------------------------------------------------------------
async def test_me_names_the_caller_models_and_limits(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().text)
    out = (await client.get(f"{API}/me")).json()
    assert out["principal"] == ALICE
    assert out["models"] == [
        {"id": "orchestrator-model", "label": "orchestrator-model", "default": True},
        {"id": "model-a", "label": "model-a", "default": False},
        {"id": "model-b", "label": "model-b", "default": False},
    ]
    assert out["limits"]["max_files"] == 5
    assert out["limits"]["max_file_bytes"] == 10 * 1024 * 1024
    assert "image/png" in out["limits"]["media_types"]
    assert out["app_name"]


async def test_crud_and_newest_first(client):
    first = await new_conversation(client)
    second = await new_conversation(client, title="Mine\nsecond line", model="model-a")
    listed = (await client.get(f"{API}/conversations")).json()
    assert [c["id"] for c in listed] == [second, first]
    assert set(listed[0]) == {"id", "title", "updated_at", "created_at", "status",
                              "agent_ref", "agent_name"}
    assert (listed[0]["agent_ref"], listed[0]["agent_name"]) == (None, "Orchestrator")
    assert listed[0]["title"] == "Mine second line"
    assert listed[1]["title"] == store.DEFAULT_TITLE

    r = await client.patch(f"{API}/conversations/{first}", json={"title": "Renamed"})
    assert r.status_code == 200 and r.json()["title"] == "Renamed"
    assert (await client.get(f"{API}/conversations")).json()[0]["id"] == second

    detail = (await client.get(f"{API}/conversations/{second}")).json()
    assert detail["model"] == "model-a" and detail["messages"] == []
    r = await client.patch(f"{API}/conversations/{second}", json={"model": None})
    assert r.json()["model"] is None

    assert (await client.delete(f"{API}/conversations/{first}")).status_code == 204
    assert (await client.get(f"{API}/conversations/{first}")).status_code == 404


async def test_search_and_paging(client):
    a = await new_conversation(client, title="Budget 100% plan")
    b = await new_conversation(client, title="Holiday")
    await new_conversation(client, user=BOB, title="Budget of bob")
    found = (await client.get(f"{API}/conversations", params={"q": "budget"})).json()
    assert [c["id"] for c in found] == [a]
    assert (await client.get(f"{API}/conversations", params={"q": "100%"})).json()[0]["id"] == a
    assert (await client.get(f"{API}/conversations", params={"q": "_"})).json() == []
    newest = (await client.get(f"{API}/conversations")).json()[0]
    assert newest["id"] == b
    older = (await client.get(f"{API}/conversations",
                              params={"before": newest["updated_at"]})).json()
    assert [c["id"] for c in older] == [a]
    r = await client.get(f"{API}/conversations", params={"before": "yesterday"})
    assert r.status_code == 422 and r.json() == {"detail": routes.BAD_BEFORE}
    assert (await client.get(f"{API}/conversations", params={"limit": 0})).status_code == 422


async def test_an_unknown_model_is_a_fixed_422(client):
    r = await client.post(f"{API}/conversations", json={"model": SECRET})
    assert r.status_code == 422 and r.json() == {"detail": "Unknown model"}
    cid = await new_conversation(client)
    r = await client.patch(f"{API}/conversations/{cid}", json={"model": SECRET})
    assert r.status_code == 422 and SECRET not in r.text
    r = await client.post(f"{API}/conversations/{cid}/messages", **form(model=SECRET))
    assert r.status_code == 422 and r.json() == {"detail": "Unknown model"}
    assert (await conversation_row(cid)).status == "idle"


async def test_a_refused_body_does_not_echo_it(client):
    r = await client.post(f"{API}/conversations", json={"title": 1, "x": SECRET})
    assert r.status_code == 422 and SECRET not in r.text


async def test_another_users_conversation_and_file_are_404(client, monkeypatch,
                                                           real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().text)
    cid = await new_conversation(client)
    body = (await client.post(f"{API}/conversations/{cid}/messages",
                              **form(files=[("p.png", PNG_BYTES, "image/png")]))).text
    file_id = frames(body)[0][1]["files"][0]["id"]
    bob = {"x-test-user": BOB}
    for method, path, kw in (
        ("GET", f"/conversations/{cid}", {}),
        ("PATCH", f"/conversations/{cid}", {"json": {"title": "x"}}),
        ("DELETE", f"/conversations/{cid}", {}),
        ("POST", f"/conversations/{cid}/messages", form()),
        ("POST", f"/conversations/{cid}/cancel", {}),
        ("GET", f"/conversations/{cid}/stream", {}),
        ("GET", f"/files/{file_id}", {}),
    ):
        r = await client.request(method, API + path, headers=bob, **kw)
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await client.get(f"{API}/conversations", headers=bob)).json() == []
    assert (await conversation_row(cid)).title != "x"


# ---------------------------------------------------------------------------
# A turn
# ---------------------------------------------------------------------------
async def test_a_turn_streams_its_events_and_is_stored(client, monkeypatch,
                                                      real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.workbook_then_text)
    cid = await new_conversation(client)
    r = await client.post(f"{API}/conversations/{cid}/messages",
                          **form(text="Quarterly numbers\nas a workbook please",
                                 files=[("chart.png", PNG_BYTES, "image/png"),
                                        ("notes.csv", b"a,b\n1,2\n", "text/csv")]))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache, no-transform"
    assert r.headers["x-accel-buffering"] == "no"
    events = frames(r.text)
    names = [e for e, _ in events]
    assert names == ["start", "tool", "tool", "file", "text", "text", "usage", "title", "done"]
    start = events[0][1]
    assert set(start) == {"user_message_id", "assistant_message_id", "files"}
    assert [f["name"] for f in start["files"]] == ["chart.png", "notes.csv"]
    running, done = events[1][1], events[2][1]
    assert running["status"] == "running" and running["name"] == "create_excel_workbook"
    assert running["agent"] == "orchestrator" and "filename=../report" in running["args"]
    assert running["agent_id"] is None  # the orchestrator's own tool
    assert done["status"] == "done" and done["id"] == running["id"]
    assert done["agent_id"] is None and isinstance(done["duration_ms"], int)
    file_event = events[3][1]
    assert file_event["name"] == "report.xlsx"
    assert file_event["media_type"] == uploads.XLSX
    text = "".join(d["delta"] for e, d in events if e == "text")
    assert text == f"Here is [the workbook](/chat/api/files/{file_event['file_id']})."
    usage = events[6][1]
    assert usage["model"] == "orchestrator-model" and usage["agents"] == []
    assert usage["requests"] == 2 and usage["tool_calls"] == 1
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert events[7][1] == {"title": "Quarterly numbers"}
    done_event = events[8][1]
    assert done_event == {"message_id": start["assistant_message_id"], "status": "done",
                          "duration_ms": done_event["duration_ms"]}
    assert isinstance(done_event["duration_ms"], int) and done_event["duration_ms"] >= 0

    # The model got the image as bytes and the CSV as a delimited block.
    first_request = script.seen[0][-1]
    prompt = next(p for p in first_request.parts if isinstance(p, UserPromptPart)).content
    assert prompt[0].media_type == "image/png"
    assert prompt[1].startswith('<attachment name="notes.csv" type="text/csv">\na,b')
    assert prompt[-1].startswith("Quarterly numbers")

    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert detail["title"] == "Quarterly numbers" and detail["status"] == "idle"
    user, assistant = detail["messages"]
    assert (user["role"], user["position"], assistant["position"]) == ("user", 1, 2)
    assert [f["kind"] for f in user["files"]] == ["upload", "upload"]
    assert assistant["status"] == "done" and assistant["content"] == text
    assert [p["type"] for p in assistant["parts"]] == ["tool", "file", "text", "usage"]
    assert detail["usage"] == {"input_tokens": usage["input_tokens"],
                               "output_tokens": usage["output_tokens"],
                               "total_tokens": usage["total_tokens"],
                               "requests": 2, "turns": 1}
    assert assistant["files"][0]["kind"] == "generated"

    dl = await client.get(f"{API}/files/{file_event['file_id']}")
    assert dl.status_code == 200
    assert dl.headers["content-disposition"] == "attachment; filename*=UTF-8''report.xlsx"
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert dl.headers["cache-control"] == "private, no-store"
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(dl.content))
    assert book["Data"]["A2"].value == "=1+1" and book["Data"]["A2"].data_type == "s"

    async with SessionLocal() as s:
        row = await s.get(ChatMessage, assistant["id"])
    assert row.model_json and json.loads(row.model_json)


async def test_the_second_turn_gets_the_first_as_history(client, monkeypatch,
                                                        real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.text)
    cid = await new_conversation(client, title="Kept title")
    first = frames((await client.post(f"{API}/conversations/{cid}/messages",
                                      **form(text="first question"))).text)
    assert "title" not in [e for e, _ in first]  # a title the user gave stays
    second = frames((await client.post(f"{API}/conversations/{cid}/messages",
                                       **form(text="second question"))).text)
    assert second[-1][1]["status"] == "done"
    sent = script.seen[1]
    prompts = [p.content for m in sent if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, UserPromptPart)]
    assert prompts[0] == ["first question"] and prompts[-1] == ["second question"]
    assert (await conversation_row(cid)).title == "Kept title"


async def test_specialist_tools_become_cards(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().specialist_tool)
    cid = await new_conversation(client)
    events = frames((await client.post(f"{API}/conversations/{cid}/messages",
                                       **form())).text)
    tools = [d for e, d in events if e == "tool"]
    assert [t["status"] for t in tools] == ["running", "done"]
    assert tools[0]["agent"] == "btp-admin" and tools[0]["name"] == "SAPQuery"
    assert tools[0]["id"].startswith("spec:") and tools[1]["output"] == "3 rows"


async def test_a_delegation_streams_agent_tool_and_usage(client, monkeypatch,
                                                          real_agents_and_mcp):
    agent = use_model(monkeypatch, real_agents_and_mcp, Script().delegates)
    agent.tool(name="delegate_btp_admin")(delegate_btp_admin)
    cid = await new_conversation(client)
    events = frames((await client.post(f"{API}/conversations/{cid}/messages",
                                       **form(text="Which systems?"))).text)
    flow = [(e, d.get("status")) for e, d in events
            if e in ("agent", "tool", "usage", "done")]
    assert flow == [("agent", "running"), ("tool", "running"), ("tool", "done"),
                    ("agent", "done"), ("usage", None), ("done", "done")]
    agent_run = next(d for e, d in events if e == "agent")
    assert agent_run == {"id": "deleg-1", "agent": "btp-admin", "parent": None,
                         "status": "running", "prompt": agent_run["prompt"]}
    assert agent_run["prompt"].startswith("list systems") and SECRET not in agent_run["prompt"]
    tools = [d for e, d in events if e == "tool"]
    assert all(t["agent_id"] == "deleg-1" for t in tools)
    assert tools[1]["output"] == "3 rows" and isinstance(tools[1]["duration_ms"], int)
    agent_done = [d for e, d in events if e == "agent"][1]
    assert set(agent_done) == {"id", "agent", "parent", "status", "output", "duration_ms",
                               "usage"}
    assert agent_done["output"] == "3 systems" and agent_done["usage"] == SPEC_USAGE
    assert isinstance(agent_done["duration_ms"], int)
    usage = next(d for e, d in events if e == "usage")
    assert usage["agents"] == [{"id": "deleg-1", "agent": "btp-admin", "input_tokens": 120,
                                "output_tokens": 30, "requests": 2, "model": "spec-model"}]
    assert usage["model"] == "orchestrator-model" and usage["tool_calls"] == 1
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert isinstance(events[-1][1]["duration_ms"], int)

    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    parts = detail["messages"][1]["parts"]
    assert [p["type"] for p in parts] == ["agent", "tool", "text", "usage"]
    stored_agent, stored_tool, _, stored_usage = parts
    assert stored_agent == {"type": "agent", "id": "deleg-1", "agent": "btp-admin",
                            "parent": None, "status": "done", "prompt": agent_run["prompt"],
                            "output": "3 systems", "duration_ms": agent_done["duration_ms"],
                            "usage": SPEC_USAGE}
    assert stored_tool["agent_id"] == "deleg-1" and stored_tool["status"] == "done"
    assert stored_usage == {"type": "usage", **usage}
    assert detail["usage"] == {"input_tokens": usage["input_tokens"],
                               "output_tokens": usage["output_tokens"],
                               "total_tokens": usage["total_tokens"],
                               "requests": usage["requests"], "turns": 1}


async def test_an_open_delegation_is_closed_and_a_failed_turn_has_no_usage(
        client, monkeypatch, real_agents_and_mcp):
    async def starts_then_fails(messages, info):
        report_delegation_start("btp-admin", "Consulting…", delegation_id="d-open",
                                query="q")
        token = current_delegation.set("d-open")
        try:
            report_tool_start("btp-admin", "raw-9", "SAPRead", {})
        finally:
            current_delegation.reset(token)
        raise RuntimeError("boom")
        yield ""  # pragma: no cover

    use_model(monkeypatch, real_agents_and_mcp, starts_then_fails)
    cid = await new_conversation(client)
    events = frames((await client.post(f"{API}/conversations/{cid}/messages",
                                       **form())).text)
    assert "usage" not in [e for e, _ in events]
    agent_frames = [d for e, d in events if e == "agent"]
    assert [a["status"] for a in agent_frames] == ["running", "error"]
    assert agent_frames[1]["output"] == "not finished" and agent_frames[1]["usage"] is None
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    parts = detail["messages"][1]["parts"]
    assert [(p["type"], p["status"]) for p in parts] == [("agent", "error"), ("tool", "error")]
    assert parts[1]["agent_id"] == "d-open"
    assert detail["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                               "requests": 0, "turns": 0}


async def test_a_failed_run_is_stored_masked(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().fails)
    cid = await new_conversation(client)
    events = frames((await client.post(f"{API}/conversations/{cid}/messages",
                                       **form())).text)
    assert [e for e, _ in events][-3:] == ["error", "title", "done"]
    error = events[-3][1]
    assert error["code"] == "run_failed" and SECRET not in json.dumps(error)
    assert events[-1][1]["status"] == "failed"
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assistant = detail["messages"][1]
    assert assistant["status"] == "failed" and "RuntimeError" in assistant["error"]
    assert SECRET not in assistant["error"] and detail["status"] == "idle"


async def test_409_while_running_then_cancel(client, monkeypatch, real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.blocks)
    cid = await new_conversation(client)
    post = asyncio.create_task(client.post(f"{API}/conversations/{cid}/messages", **form()))
    await asyncio.wait_for(script.started.wait(), 5)
    r = await client.post(f"{API}/conversations/{cid}/messages", **form())
    assert r.status_code == 409 and r.json() == {"detail": "a run is in progress"}
    assert (await client.delete(f"{API}/conversations/{cid}")).status_code == 409
    assert (await client.post(f"{API}/conversations/{cid}/cancel")).json() == {"canceled": True}
    events = frames((await asyncio.wait_for(post, 5)).text)
    assert [e for e, _ in events][-3:] == ["error", "title", "done"]
    assert events[-3][1]["code"] == "canceled"
    assert events[-1][1]["status"] == "canceled"
    assistant = (await client.get(f"{API}/conversations/{cid}")).json()["messages"][1]
    assert assistant["status"] == "canceled" and assistant["content"] == "partial "
    async with SessionLocal() as s:
        assert (await s.get(ChatMessage, assistant["id"])).model_json is None
    assert (await conversation_row(cid)).status == "idle"
    assert (await client.post(f"{API}/conversations/{cid}/cancel")).json() == {"canceled": False}


async def test_reattach_replays_a_running_turn(client, monkeypatch, real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.blocks)
    cid = await new_conversation(client)
    r = await client.get(f"{API}/conversations/{cid}/stream")
    assert r.status_code == 409 and r.json() == {"detail": "not_running"}
    post = asyncio.create_task(client.post(f"{API}/conversations/{cid}/messages", **form()))
    await asyncio.wait_for(script.started.wait(), 5)
    again = asyncio.create_task(client.get(f"{API}/conversations/{cid}/stream"))
    await asyncio.sleep(0.05)
    script.release.set()
    first, second = await asyncio.wait_for(asyncio.gather(post, again), 5)
    assert frames(first.text) == frames(second.text)
    assert frames(second.text)[0][0] == "start"


async def test_a_client_that_goes_away_does_not_stop_the_run(monkeypatch,
                                                             real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.blocks)
    async with AsyncClient(transport=ASGITransport(app=bound_app()),
                           base_url="http://test") as c:
        cid = await new_conversation(c)
        post = asyncio.create_task(c.post(f"{API}/conversations/{cid}/messages", **form()))
        await asyncio.wait_for(script.started.wait(), 5)
        post.cancel()
        with pytest.raises(asyncio.CancelledError):
            await post
    script.release.set()
    for _ in range(100):
        if (await conversation_row(cid)).status == "idle":
            break
        await asyncio.sleep(0.02)
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChatMessage).where(
            ChatMessage.conversation_id == cid, ChatMessage.role == "assistant"))).scalars().all()
    assert rows[0].status == "done" and rows[0].content == "partial rest"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
async def _refused(client, cid: str, status: int, **kw) -> dict:
    r = await client.post(f"{API}/conversations/{cid}/messages", **kw)
    assert r.status_code == status, r.text
    assert (await conversation_row(cid)).status == "idle"
    return r.json()


async def test_limits_are_refused_with_fixed_texts(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().text)
    cid = await new_conversation(client)
    out = await _refused(client, cid, 415, json={"text": "hi"})
    assert out == {"detail": uploads.MSG_NOT_MULTIPART}
    out = await _refused(client, cid, 422, **form(text=None, model=""))
    assert out == {"detail": uploads.MSG_TEXT_MISSING}
    out = await _refused(client, cid, 413, **form(text="x" * (64 * 1024 + 1)))
    assert out == {"detail": uploads.MSG_TEXT_TOO_LARGE}
    out = await _refused(client, cid, 422, **form(files=[("a.png", PNG_BYTES, "image/png")] * 6))
    assert out == {"detail": uploads.MSG_TOO_MANY_FILES}
    out = await _refused(client, cid, 415, **form(files=[(f"{SECRET}.exe", b"MZ\0\0", "")]))
    assert out == {"detail": uploads.MSG_TYPE_NOT_ALLOWED}
    out = await _refused(client, cid, 415, **form(files=[("a.png", PNG_BYTES, "image/jpeg")]))
    assert out == {"detail": uploads.MSG_TYPE_MISMATCH}
    out = await _refused(client, cid, 422, **form(other="x"))
    assert out == {"detail": uploads.MSG_UNEXPECTED_FIELD}
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 16)
    out = await _refused(client, cid, 413, **form(files=[("a.png", PNG_BYTES, "image/png")]))
    assert out == {"detail": uploads.MSG_FILE_TOO_LARGE}
    async with SessionLocal() as s:
        assert (await s.execute(select(ChatMessage))).scalars().all() == []


async def test_a_full_conversation_takes_no_more(client, monkeypatch, real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().text)
    monkeypatch.setattr(store, "MAX_MESSAGES", 2)
    cid = await new_conversation(client)
    assert (await client.post(f"{API}/conversations/{cid}/messages", **form())).status_code == 200
    out = await _refused(client, cid, 422, **form())
    assert out == {"detail": routes.FULL}


async def test_the_picked_model_is_used_and_kept(client, monkeypatch, real_agents_and_mcp):
    script = Script()
    use_model(monkeypatch, real_agents_and_mcp, script.text)
    picked = FunctionModel(stream_function=script.text, model_name="model-b")
    built: list[str] = []

    def fake_get_model(name):
        built.append(name)
        return picked

    monkeypatch.setattr(run, "get_model", fake_get_model)
    cid = await new_conversation(client)
    r = await client.post(f"{API}/conversations/{cid}/messages", **form(model="model-b"))
    assert frames(r.text)[-1][1]["status"] == "done"
    assert built == ["model-b"]
    assert (await conversation_row(cid)).model_name == "model-b"


async def test_download_inline_only_for_images_and_pdf(client, monkeypatch,
                                                       real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().text)
    cid = await new_conversation(client)
    body = (await client.post(f"{API}/conversations/{cid}/messages", **form(files=[
        ("p.png", PNG_BYTES, "image/png"), ("n.txt", b"hello", "text/plain")]))).text
    png, txt = frames(body)[0][1]["files"]
    r = await client.get(f"{API}/files/{png['id']}", params={"inline": 1})
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.content == PNG_BYTES and r.headers["content-type"] == "image/png"
    r = await client.get(f"{API}/files/{txt['id']}", params={"inline": 1})
    assert r.headers["content-disposition"].startswith("attachment;")
    assert (await client.get(f"{API}/files/nope")).status_code == 404


# ---------------------------------------------------------------------------
# Retention, startup, the real app
# ---------------------------------------------------------------------------
async def _aged(cid: str, days: int, status: str = "idle") -> None:
    async with SessionLocal() as s:
        await s.execute(update(ChatConversation).where(ChatConversation.id == cid).values(
            updated_at=datetime.now(timezone.utc) - timedelta(days=days), status=status))
        await s.commit()


async def test_retention_purges_old_conversations_with_children(client, monkeypatch,
                                                                real_agents_and_mcp):
    use_model(monkeypatch, real_agents_and_mcp, Script().text)
    old = await new_conversation(client)
    await client.post(f"{API}/conversations/{old}/messages",
                      **form(files=[("p.png", PNG_BYTES, "image/png")]))
    busy = await new_conversation(client)
    fresh = await new_conversation(client)
    await _aged(old, 200)
    await _aged(busy, 200, status="running")
    await _aged(fresh, 10)
    async with SessionLocal() as s:
        assert await store.purge_older_than(s, 180) == 1
    async with SessionLocal() as s:
        left = set((await s.execute(select(ChatConversation.id))).scalars().all())
        assert left == {busy, fresh}
        assert (await s.execute(select(ChatMessage).where(
            ChatMessage.conversation_id == old))).scalars().all() == []
        assert (await s.execute(select(ChatFile.id).where(
            ChatFile.conversation_id == old))).scalars().all() == []


async def test_the_app_retention_pass_runs_the_chat_purge(client, monkeypatch):
    import app as app_module

    old = await new_conversation(client)
    await _aged(old, 400)
    monkeypatch.setattr(store, "CHAT_RETENTION_DAYS", 180)
    await app_module._purge_ide_sessions()
    assert await conversation_row(old) is None
    keep = await new_conversation(client)
    await _aged(keep, 400)
    monkeypatch.setattr(store, "CHAT_RETENTION_DAYS", 0)
    await app_module._purge_ide_sessions()
    assert await conversation_row(keep) is not None


async def test_startup_resets_ghost_runs(client):
    cid = await new_conversation(client)
    await _aged(cid, 0, status="running")
    async with SessionLocal() as s:
        s.add(ChatMessage(conversation_id=cid, position=1, role="assistant", status="running"))
        await s.commit()
    async with SessionLocal() as s:
        assert await store.reset_running(s) == 1
    detail = (await client.get(f"{API}/conversations/{cid}")).json()
    assert detail["status"] == "idle" and detail["messages"][0]["status"] == "canceled"


async def test_the_real_app_serves_the_routes():
    import app as app_module

    saved = app_module.ON_CF
    app_module.ON_CF = False
    try:
        async with AsyncClient(transport=ASGITransport(app=app_module.app),
                               base_url="http://test") as c:
            r = await c.post(f"{API}/conversations", json={})
            assert r.status_code == 201
            listed = (await c.get(f"{API}/conversations")).json()
            assert listed[0]["id"] == r.json()["id"]
            page = await c.get("/chat")
            assert page.status_code == 200 and 'id="app"' in page.text
    finally:
        app_module.ON_CF = saved


def test_generated_docx_from_the_toolset_is_a_zip():
    from agents.webchat import docgen

    data = docgen.build_docx("# x")
    assert zipfile.ZipFile(io.BytesIO(data)).testzip() is None


# ---------------------------------------------------------------------------
# A specialist's own delegate_* call is no tool card (the peer is an agent card)
# ---------------------------------------------------------------------------
def test_a_peer_delegation_of_a_specialist_is_not_a_tool_card():
    from agents.progress import ProgressUpdate
    from agents.webchat.run import Turn, TurnRecorder

    turn = Turn(conversation_id="c", assistant_message_id="a", principal="local-dev")
    rec = TurnRecorder(turn)
    rec.sink(ProgressUpdate(agent="btp-admin", kind="tool_start", tool_call_id="t1",
                            tool_name="delegate_cost_analyst", args={"query": "x"},
                            delegation_id="d1"))
    rec.sink(ProgressUpdate(agent="btp-admin", kind="tool_end", tool_call_id="t1", ok=True,
                            output="done", delegation_id="d1"))
    rec.sink(ProgressUpdate(agent="btp-admin", kind="tool_start", tool_call_id="t2",
                            tool_name="SAPQuery", args={"q": "y"}, delegation_id="d1"))
    kinds = [(p["type"], p.get("name")) for p in rec.parts]
    assert kinds == [("tool", "SAPQuery")]
