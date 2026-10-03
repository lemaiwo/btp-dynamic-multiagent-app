"""IDE stage runs over SSE (contract §1.2/§1.3): messages, revise, cancel.

The routes check ownership and the stage gates *before* the stream opens, so
404/409/429 come back as plain JSON. The run itself is
``agents.ide.runner.run_stage`` in its own task, created inside the request
context: ``current_jwt`` / ``current_principal`` reach the tools, and a
client disconnect does not stop the run.

A real pydantic-ai agent runs on a scripted, streamed ``FunctionModel``
behind a fake registry build (as in ``tests/test_ide_runner.py``).

Run:  python -m pytest tests/test_ide_sse.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{ROOT / 'tests' / '_test_ide_sse.db'}"
)
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel  # noqa: E402
from pydantic_ai.toolsets import FunctionToolset  # noqa: E402
from sqlalchemy import select  # noqa: E402

from agents.auth import (  # noqa: E402
    current_jwt,
    current_principal,
    require_admin,
    require_developer,
)
from agents.db import SessionLocal, init_db  # noqa: E402
from agents.deep import DeepConfig, deep_toolset  # noqa: E402
from agents.ide import routes, runner, sse  # noqa: E402
from agents.ide.models import (  # noqa: E402
    IdeArtifact,
    IdeConventions,
    IdeMessage,
    IdeSession,
    IdeWorkspaceFile,
)
from agents.ide.store import upsert_conventions  # noqa: E402
from agents.registry import BuildResult, registry  # noqa: E402

NAME = "abap-orchestrator"

USERS = {
    "alice": {"user_name": "alice", "scope": ["developer"]},
    "bob": {"user_name": "bob", "scope": ["developer"]},
}


@pytest.fixture(autouse=True)
def _real_agent_run(monkeypatch):
    if "run" in vars(Agent):
        monkeypatch.delattr(Agent, "run")


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.delenv("IDE_SESSION_REQUEST_CAP", raising=False)
    monkeypatch.delenv("IDE_ORCHESTRATOR_AGENT", raising=False)
    await init_db()
    async with SessionLocal() as db:
        for model in (IdeWorkspaceFile, IdeArtifact, IdeMessage, IdeSession,
                      IdeConventions):
            await db.execute(model.__table__.delete())
        await db.commit()
        await upsert_conventions(db, "T1", label="Target one", namespace="Z")
    saved = registry._build
    yield
    registry._build = saved
    await runner.cancel_all()


# --- scripted model ----------------------------------------------------------


class Script:
    """Turns: a final text (str), a list of ``(tool, args)`` calls, or
    ``("wait", event, turn)`` / ``("sleep", seconds, turn)`` before a turn."""

    def __init__(self, turns: list):
        self.turns = turns
        self.started = asyncio.Event()

    def _turn(self, messages: list[ModelMessage]):
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        return self.turns[min(n, len(self.turns) - 1)]

    async def _resolve(self, turn):
        self.started.set()
        while isinstance(turn, tuple) and turn and turn[0] in ("wait", "sleep"):
            if turn[0] == "wait":
                await turn[1].wait()
            else:
                await asyncio.sleep(turn[1])
            turn = turn[2]
        return turn

    async def fn(self, messages, info: AgentInfo):
        turn = await self._resolve(self._turn(messages))
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=[ToolCallPart(t, a) for t, a in turn])

    async def stream(self, messages, info: AgentInfo):
        turn = await self._resolve(self._turn(messages))
        if isinstance(turn, str):
            half = max(1, len(turn) // 2)
            yield turn[:half]
            if turn[half:]:
                yield turn[half:]
            return
        yield {
            i: DeltaToolCall(name=t, json_args=json.dumps(a), tool_call_id=f"c-{i}")
            for i, (t, a) in enumerate(turn)
        }

    def model(self) -> FunctionModel:
        return FunctionModel(self.fn, stream_function=self.stream)


SEEN: list[dict] = []


def _identity_toolset() -> FunctionToolset:
    ts = FunctionToolset()

    @ts.tool_plain
    def whoami() -> str:
        """Report the identity bound to this run."""
        SEEN.append({"jwt": current_jwt.get(), "principal": current_principal.get()})
        return "ok"

    return ts


class _Specialist:
    def __init__(self, script: Script):
        self.script = script

    async def run(self, prompt, **kwargs):
        model = self.script.model()
        ts = deep_toolset(
            DeepConfig(enabled=True, subagent_max_depth=1),
            parent_toolsets=[], model=model, agent_name=NAME,
        )
        agent = Agent(instructions="base", retries=1)
        return await agent.run(
            prompt, model=model, toolsets=[ts, _identity_toolset()], **kwargs
        )


def _install(script: Script) -> None:
    registry._build = BuildResult(
        orchestrator=None, specialists={NAME: _Specialist(script)},
        mcp_clients=[], configs=[],
    )


# --- app ---------------------------------------------------------------------


def _fake_developer(request: Request) -> dict:
    user = request.headers.get("x-test-user", "")
    if user not in USERS:
        raise HTTPException(status_code=403, detail="Developer scope required")
    return USERS[user]


class _FakeJWTMiddleware:
    """Binds ``current_jwt`` / ``current_principal`` the way app.py's
    middleware does, for the whole request (stream included)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or [])
        jwt = headers.get(b"x-test-jwt")
        user = headers.get(b"x-test-user")
        t1 = current_jwt.set(jwt.decode() if jwt else None)
        t2 = current_principal.set(user.decode() if user else None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(t2)
            current_jwt.reset(t1)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_developer] = _fake_developer
    app.dependency_overrides[require_admin] = _fake_developer
    app.add_middleware(_FakeJWTMiddleware)
    return app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()),
                           base_url="http://test", timeout=30) as c:
        yield c


def _as(user: str, jwt: str | None = None) -> dict:
    h = {"x-test-user": user}
    if jwt:
        h["x-test-jwt"] = jwt
    return h


async def _session(client, stage: str = "chat", **values) -> str:
    r = await client.post("/ide/api/sessions", json={"title": "t", "target": "T1"},
                          headers=_as("alice"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    if stage != "chat" or values:
        async with SessionLocal() as db:
            row = await db.get(IdeSession, sid)
            row.stage = stage
            for k, v in values.items():
                setattr(row, k, v)
            await db.commit()
    return sid


async def _row(sid: str) -> IdeSession:
    async with SessionLocal() as db:
        return await db.get(IdeSession, sid)


def parse(body: str) -> list[tuple[str, object]]:
    """SSE frames as ``(event, data)``; a comment frame is ``(":", text)``."""
    out: list[tuple[str, object]] = []
    for frame in body.split("\n\n"):
        if not frame:
            continue
        if frame.startswith(":"):
            out.append((":", frame[1:].strip()))
            continue
        lines = dict(line.split(": ", 1) for line in frame.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def kinds(frames) -> list[str]:
    return [k for k, _ in frames if k != ":"]


def of(frames, kind) -> list:
    return [d for k, d in frames if k == kind]


async def _wait_until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


# --- format_event ------------------------------------------------------------


def test_format_event_escapes_multiline_delta():
    frame = sse.format_event("text", {"delta": "line 1\nline 2\r\nline 3"})
    assert frame.endswith(b"\n\n")
    head, data, *rest = frame.decode("utf-8").split("\n")
    assert head == "event: text"
    assert data.startswith("data: ")
    assert rest == ["", ""]  # exactly one data line, then the blank line
    assert json.loads(data[len("data: "):]) == {"delta": "line 1\nline 2\r\nline 3"}


def test_format_event_keeps_non_ascii_and_rejects_bad_names():
    frame = sse.format_event("text", {"delta": "café ✓"})
    assert "café ✓".encode() in frame
    with pytest.raises(ValueError):
        sse.format_event("bad\nname", {})
    with pytest.raises(ValueError):
        sse.format_event("", {})


# --- message stream ------------------------------------------------------------


async def test_message_stream_order_headers_and_persisted_messages(client):
    _install(Script(["Hello\nworld"]))
    sid = await _session(client)
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json={"text": "hi"},
                          headers=_as("alice"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    frames = parse(r.text)
    ks = kinds(frames)
    assert ks[0] == "run" and ks[-1] == "done"
    assert ks.index("text") < ks.index("usage") < ks.index("done")
    assert "".join(d["delta"] for d in of(frames, "text")) == "Hello\nworld"
    run, done = of(frames, "run")[0], of(frames, "done")[0]
    assert run["stage"] == "chat" and run["message_id"]
    assert done == {"message_id": done["message_id"], "stage": "chat",
                    "status": "idle"}

    r = await client.get(f"/ide/api/sessions/{sid}/messages", headers=_as("alice"))
    assert r.status_code == 200
    msgs = r.json()
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "hi"), ("assistant", "Hello\nworld")]
    assert msgs[0]["id"] == run["message_id"]
    assert msgs[1]["id"] == done["message_id"]
    assert set(msgs[0]) >= {"id", "stage", "role", "content", "created_at"}
    assert "text" not in msgs[0]
    assert msgs[1]["activity"]["events"] == []
    assert "activity" not in msgs[0]
    assert (await _row(sid)).status == "idle"


async def test_identity_propagates_into_tools(client):
    SEEN.clear()
    _install(Script([[("whoami", {})], "done"]))
    sid = await _session(client)
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json={"text": "who"},
                          headers=_as("alice", jwt="jwt-of-alice"))
    assert r.status_code == 200
    frames = parse(r.text)
    assert kinds(frames)[-1] == "done"
    assert SEEN == [{"jwt": "jwt-of-alice", "principal": "alice"}]
    tool_events = [d for d in of(frames, "tool") if d.get("tool") == "whoami"]
    assert tool_events, frames


async def test_revise_streams_in_design_stage(client):
    _install(Script(["# Design v2"]))
    sid = await _session(client, "design")
    r = await client.post(f"/ide/api/sessions/{sid}/revise",
                          json={"feedback": "more detail"}, headers=_as("alice"))
    assert r.status_code == 200
    frames = parse(r.text)
    assert kinds(frames)[0] == "run" and kinds(frames)[-1] == "done"
    assert of(frames, "artifact")[0]["kind"] == "design"


# --- refusals as JSON, before the stream -------------------------------------


async def test_revise_in_chat_is_409_json(client):
    _install(Script(["x"]))
    sid = await _session(client)
    r = await client.post(f"/ide/api/sessions/{sid}/revise",
                          json={"feedback": "f"}, headers=_as("alice"))
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["code"] == "revise_not_allowed" and r.json()["detail"]


async def test_message_while_running_is_409_json(client):
    _install(Script(["x"]))
    sid = await _session(client, status="running")
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json={"text": "hi"},
                          headers=_as("alice"))
    assert r.status_code == 409
    assert r.json()["code"] == "run_in_progress"
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeMessage))).first() is None


async def test_message_in_done_stage_is_409(client):
    _install(Script(["x"]))
    sid = await _session(client, "done")
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json={"text": "hi"},
                          headers=_as("alice"))
    assert r.status_code == 409 and r.json()["code"] == "stage_done"


async def test_usage_exhausted_is_429_json(client, monkeypatch):
    monkeypatch.setenv("IDE_SESSION_REQUEST_CAP", "3")
    _install(Script(["x"]))
    sid = await _session(client, requests_used=3)
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json={"text": "hi"},
                          headers=_as("alice"))
    assert r.status_code == 429
    assert r.json()["code"] == "usage_exhausted" and r.json()["detail"]


@pytest.mark.parametrize("path,body", [
    ("messages", {"text": "hi"}),
    ("revise", {"feedback": "f"}),
    ("cancel", None),
])
async def test_other_user_gets_404_and_nothing_runs(client, path, body):
    _install(Script(["x"]))
    sid = await _session(client, "design")
    r = await client.post(f"/ide/api/sessions/{sid}/{path}", json=body,
                          headers=_as("bob"))
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    async with SessionLocal() as db:
        assert (await db.execute(select(IdeMessage))).first() is None
    assert (await _row(sid)).status == "idle"


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "x" * 20001}, {},
                                  {"text": "hi", "extra": 1}])
async def test_message_body_is_validated(client, body):
    sid = await _session(client)
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json=body,
                          headers=_as("alice"))
    assert r.status_code == 422


def test_no_route_relies_on_a_trailing_slash():
    # The client treats any 3xx as an expired session: every IDE route is
    # declared without a trailing slash, and the client calls it that way.
    paths = [r.path for r in routes.router.routes]
    assert "/ide/api/sessions/{sid}/messages" in paths
    assert "/ide/api/sessions/{sid}/revise" in paths
    assert "/ide/api/sessions/{sid}/cancel" in paths
    assert not [p for p in paths if p.endswith("/")]


# --- heartbeat -----------------------------------------------------------------


async def test_heartbeat_ping_while_the_model_is_slow(client, monkeypatch):
    monkeypatch.setattr(sse, "HEARTBEAT_INTERVAL_S", 0.01)
    _install(Script([("sleep", 0.2, "slow answer")]))
    sid = await _session(client)
    r = await client.post(f"/ide/api/sessions/{sid}/messages", json={"text": "hi"},
                          headers=_as("alice"))
    frames = parse(r.text)
    assert (":", "ping") in frames
    assert kinds(frames)[-1] == "done"
    assert r.text.endswith("\n\n")


# --- cancel --------------------------------------------------------------------


async def test_cancel_stops_the_run_and_closes_the_stream(client):
    gate = asyncio.Event()
    script = Script([("wait", gate, "never seen")])
    _install(script)
    sid = await _session(client)
    stream = asyncio.create_task(client.post(
        f"/ide/api/sessions/{sid}/messages", json={"text": "hi"},
        headers=_as("alice")))
    await asyncio.wait_for(script.started.wait(), 5)

    # Another user cannot cancel it.
    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("bob"))
    assert r.status_code == 404
    assert (await _row(sid)).status == "running"

    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("alice"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "idle" and r.json()["id"] == sid

    resp = await asyncio.wait_for(stream, 5)
    frames = parse(resp.text)
    assert kinds(frames)[0] == "run" and kinds(frames)[-1] == "done"
    assert of(frames, "done")[0]["status"] == "idle"
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)
    r = await client.get(f"/ide/api/sessions/{sid}/messages", headers=_as("alice"))
    assert r.json()[-1]["content"].endswith("(cancelled)")


async def test_cancel_releases_a_stale_running_row(client, monkeypatch):
    from datetime import timedelta

    from agents.ide.models import utcnow

    monkeypatch.setenv("IDE_RUN_STALE_S", "60")
    sid = await _session(client, status="running", run_id="lost-run")
    async with SessionLocal() as db:
        row = await db.get(IdeSession, sid)
        row.updated_at = utcnow() - timedelta(seconds=600)
        await db.commit()
    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json()["status"] == "idle"
    row = await _row(sid)
    assert (row.status, row.run_id) == ("idle", None)


async def test_cancel_keeps_a_fresh_running_row_without_local_task(client,
                                                                  monkeypatch):
    # Could be a live run on another instance: not ours to release.
    monkeypatch.setenv("IDE_RUN_STALE_S", "600")
    monkeypatch.setattr(routes, "CANCEL_START_WAIT_S", 0.05)
    sid = await _session(client, status="running", run_id="elsewhere")
    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("alice"))
    # Nothing was cancelled: the caller is told so instead of a 200 that
    # reads like success.
    assert r.status_code == 409
    assert r.json()["code"] == "run_on_other_instance"
    row = await _row(sid)
    assert (row.status, row.run_id) == ("running", "elsewhere")


async def test_cancel_on_idle_session_is_a_noop(client):
    sid = await _session(client)
    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("alice"))
    assert r.status_code == 200 and r.json()["status"] == "idle"


# --- client disconnect -----------------------------------------------------------


async def test_client_disconnect_does_not_kill_the_run():
    gate = asyncio.Event()
    script = Script([("wait", gate, "finished anyway")])
    _install(script)
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        sid = await _session(c)

    body = json.dumps({"text": "hi"}).encode()
    disconnect = asyncio.Event()
    sent: list[dict] = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and b"event: run" in message.get(
                "body", b""):
            disconnect.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": f"/ide/api/sessions/{sid}/messages",
        "raw_path": f"/ide/api/sessions/{sid}/messages".encode(), "query_string": b"",
        "root_path": "", "server": ("test", 80), "client": ("127.0.0.1", 1),
        "headers": [(b"content-type", b"application/json"),
                    (b"x-test-user", b"alice"),
                    (b"content-length", str(len(body)).encode())],
    }
    await asyncio.wait_for(app(scope, receive, send), 5)
    assert sent[0]["status"] == 200
    assert (await _row(sid)).status == "running"  # the client left, the run did not

    gate.set()

    async def finished():
        return (await _row(sid)).status == "idle"

    await _wait_until(finished)
    async with SessionLocal() as db:
        msgs = list((await db.execute(
            select(IdeMessage).where(IdeMessage.session_id == sid)
            .order_by(IdeMessage.created_at))).scalars())
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"), ("assistant", "finished anyway")]



async def test_cancel_when_the_run_ends_meanwhile_returns_the_idle_session(
        client, monkeypatch):
    # The other instance's run finishes between the read and the answer:
    # nothing to cancel any more, so no 409.
    monkeypatch.setenv("IDE_RUN_STALE_S", "600")
    monkeypatch.setattr(routes, "CANCEL_START_WAIT_S", 0.05)
    sid = await _session(client, status="running", run_id="elsewhere")

    async def finished_meanwhile(s):
        async with SessionLocal() as db:
            row = await db.get(IdeSession, s)
            row.status, row.run_id = "idle", None
            await db.commit()
        return False

    monkeypatch.setattr(routes.runner, "release_stale", finished_meanwhile)
    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("alice"))
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


async def test_cancel_when_another_run_started_meanwhile_is_not_409(client, monkeypatch):
    monkeypatch.setenv("IDE_RUN_STALE_S", "600")
    monkeypatch.setattr(routes, "CANCEL_START_WAIT_S", 0.05)
    sid = await _session(client, status="running", run_id="first")

    async def replaced(s):
        async with SessionLocal() as db:
            row = await db.get(IdeSession, s)
            row.run_id = "second"
            await db.commit()
        return False

    monkeypatch.setattr(routes.runner, "release_stale", replaced)
    r = await client.post(f"/ide/api/sessions/{sid}/cancel", headers=_as("alice"))
    # The run the caller saw is gone; the session is returned as it is now.
    assert r.status_code == 200
    assert r.json()["status"] == "running"
