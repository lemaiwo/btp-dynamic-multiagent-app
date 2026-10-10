"""A2A runs: what a failed run tells the caller, and how a run is stopped.

* A failed orchestrator run answers the exception class plus a fixed
  sentence, never the exception's text (an httpx error names the request URL
  as sent, query string included).
* A ``message/stream`` whose client goes away (the generator is cancelled or
  closed, or ``request.is_disconnected()`` turns true) cancels the run and
  leaves the task ``canceled``, not ``working`` until the TTL.
* ``tasks/cancel`` on a running task cancels the run itself, for
  ``message/stream`` and ``message/send`` alike.

The orchestrator is a fake set on ``a2a.registry``; nothing reaches a model.

Run:  .venv/bin/python -m pytest tests/test_a2a_runs.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import a2a  # noqa: E402

SECRET_URL = "https://mcp.example.test/mcp?access_token=SECRET-VALUE"


class _Result:
    output = "done"

    def all_messages(self):
        return []


class _Orchestrator:
    """A run that fails, finishes, or blocks until it is cancelled."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, text, **kwargs):  # noqa: ARG002
        self.started.set()
        if self.mode == "fail":
            raise httpx.ConnectError(f"connection refused for {SECRET_URL}")
        if self.mode == "block":
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return _Result()


class _Registry:
    def __init__(self, orchestrator: _Orchestrator) -> None:
        self.orchestrator = orchestrator


class _Request:
    def __init__(self) -> None:
        self.gone = False

    async def is_disconnected(self) -> bool:
        return self.gone


def _params(text: str = "hello") -> dict:
    return {"message": {
        "role": "user", "parts": [{"kind": "text", "text": text}],
        "messageId": f"m-{uuid.uuid4().hex}",
        "contextId": f"ctx-{uuid.uuid4().hex}",
    }}


def _events(frames: list[str]) -> list[dict]:
    return [json.loads(f[len("data: "):]) for f in frames]


async def _stored_state(task_id: str) -> str:
    task = await a2a.store.get_task(a2a._caller(), task_id)
    assert task is not None
    return task["status"]["state"]


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(a2a, "_DISCONNECT_POLL_SECONDS", 0.01)


# -- B-a2a-1: no exception text to the caller --------------------------------
async def test_message_send_failure_names_the_class_not_the_text(monkeypatch):
    monkeypatch.setattr(a2a, "registry", _Registry(_Orchestrator("fail")))
    out = await a2a._handle_message_send("r1", _params())
    task = out["result"]
    assert task["status"]["state"] == "failed"
    text = task["status"]["message"]["parts"][0]["text"]
    assert "SECRET" not in text and "https://" not in text
    assert "ConnectError" in text
    assert text.startswith("Orchestrator error: ")


async def test_message_stream_failure_names_the_class_not_the_text(monkeypatch):
    monkeypatch.setattr(a2a, "registry", _Registry(_Orchestrator("fail")))
    frames = [f async for f in a2a._stream_message("r1", _params(), _Request())]
    events = _events(frames)
    last = events[-1]["result"]
    assert last["final"] is True and last["status"]["state"] == "failed"
    text = last["status"]["message"]["parts"][0]["text"]
    assert "SECRET" not in text and "https://" not in text
    assert "ConnectError" in text
    assert await _stored_state(last["taskId"]) == "failed"


async def test_failure_log_holds_no_exception_text(monkeypatch, caplog):
    monkeypatch.setattr(a2a, "registry", _Registry(_Orchestrator("fail")))
    caplog.set_level("DEBUG", logger="agents.a2a")
    await a2a._handle_message_send("r1", _params())
    assert "ConnectError" in caplog.text
    assert "SECRET" not in caplog.text


# -- B-a2a-2: a run is stopped, not left working ------------------------------
async def test_stream_disconnect_cancels_the_run(monkeypatch):
    orch = _Orchestrator("block")
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    request = _Request()
    gen = a2a._stream_message("r1", _params(), request)
    first = _events([await gen.__anext__()])[0]["result"]
    task_id = first["id"]
    working = _events([await gen.__anext__()])[0]["result"]
    assert working["status"]["state"] == "working"
    assert await _stored_state(task_id) == "working"

    rest = asyncio.ensure_future(gen.__anext__())
    await asyncio.wait_for(orch.started.wait(), 2)
    request.gone = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(rest, 2)
    assert orch.cancelled
    assert await _stored_state(task_id) == "canceled"
    assert task_id not in a2a._running


async def test_stream_cancelled_by_server_cancels_the_run(monkeypatch):
    """Starlette cancels the response task when it sees the disconnect."""
    orch = _Orchestrator("block")
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    frames: list[str] = []

    async def consume() -> None:
        async for f in a2a._stream_message("r1", _params(), _Request()):
            frames.append(f)

    consumer = asyncio.ensure_future(consume())
    await asyncio.wait_for(orch.started.wait(), 2)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    task_id = _events(frames)[0]["result"]["id"]
    assert orch.cancelled
    assert await _stored_state(task_id) == "canceled"
    assert task_id not in a2a._running


async def test_tasks_cancel_stops_a_running_stream(monkeypatch):
    orch = _Orchestrator("block")
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    gen = a2a._stream_message("r1", _params(), _Request())
    task_id = _events([await gen.__anext__()])[0]["result"]["id"]
    await gen.__anext__()  # working
    rest = asyncio.ensure_future(gen.__anext__())
    await asyncio.wait_for(orch.started.wait(), 2)

    out = await a2a._handle_tasks_cancel("c1", {"id": task_id})
    assert out["result"]["status"]["state"] == "canceled"

    final = _events([await asyncio.wait_for(rest, 2)])[0]["result"]
    assert final["kind"] == "status-update"
    assert final["final"] is True and final["status"]["state"] == "canceled"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert orch.cancelled
    assert await _stored_state(task_id) == "canceled"


async def test_tasks_cancel_stops_a_running_send(monkeypatch):
    orch = _Orchestrator("block")
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    before = set(a2a._running)
    send = asyncio.ensure_future(a2a._handle_message_send("r1", _params()))
    await asyncio.wait_for(orch.started.wait(), 2)
    (task_id,) = set(a2a._running) - before

    out = await a2a._handle_tasks_cancel("c1", {"id": task_id})
    assert out["result"]["status"]["state"] == "canceled"

    answer = await asyncio.wait_for(send, 2)
    assert answer["result"]["status"]["state"] == "canceled"
    assert orch.cancelled
    assert await _stored_state(task_id) == "canceled"
    assert task_id not in a2a._running


async def test_completed_stream_still_completes(monkeypatch):
    monkeypatch.setattr(a2a, "registry", _Registry(_Orchestrator("ok")))
    frames = [f async for f in a2a._stream_message("r1", _params(), _Request())]
    events = [e["result"] for e in _events(frames)]
    assert [e["kind"] for e in events] == [
        "task", "status-update", "artifact-update", "status-update",
    ]
    assert events[-1]["status"]["state"] == "completed"
    assert await _stored_state(events[0]["id"]) == "completed"
    assert events[0]["id"] not in a2a._running


async def test_cancel_of_another_principals_task_does_not_stop_it(monkeypatch):
    orch = _Orchestrator("block")
    monkeypatch.setattr(a2a, "registry", _Registry(orch))
    before = set(a2a._running)
    send = asyncio.ensure_future(a2a._handle_message_send("r1", _params()))
    await asyncio.wait_for(orch.started.wait(), 2)
    (task_id,) = set(a2a._running) - before

    token = a2a.current_principal.set("mallory")
    try:
        out = await a2a._handle_tasks_cancel("c1", {"id": task_id})
    finally:
        a2a.current_principal.reset(token)
    assert out["error"]["code"] == -32001
    await asyncio.sleep(0.02)
    assert not orch.cancelled and not send.done()
    await a2a._handle_tasks_cancel("c2", {"id": task_id})
    await asyncio.wait_for(send, 2)
