"""The AI Core routes send the per-call observability headers, and only then.

``agents.model_call_headers.current_call_headers`` is set by the agent
register's wrapper around an AI Core model call while that integration is on.
Both AI Core routes of ``agents/shared.py`` put those headers on the request:
the OpenAI-style route through pydantic-ai's ``extra_headers`` setting, the
Bedrock route through a botocore ``before-call`` handler. With the variable
unset (no register, the app standalone) every request is exactly as before,
and nothing here may ever fail a model call.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
import openai
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agents import shared
from agents.model_call_headers import current_call_headers, sending
from agents.shared import SAPAICoreModel

ROOT = Path(__file__).resolve().parent.parent
OURS = {
    "ai-inference-observability-persistence-mode": "metadata",
    "ai-inference-observability-labels": "ext.ai.sap.com/correlation=abc-123",
}


def _completion() -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 1, "model": "gpt-4o",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _sse() -> bytes:
    chunks = [
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"},
                      "finish_reason": None}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4o",
         "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                  "total_tokens": 4}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def _openai_model(seen: list[httpx.Request], cls=SAPAICoreModel, name="gpt-4o"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=_sse(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_completion())
    client = openai.AsyncOpenAI(
        api_key="x", base_url="https://aicore.invalid/v2/inference/deployments/d1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    return cls(name, provider=OpenAIProvider(openai_client=client))


async def _run(model, *, stream=False, settings=None):
    agent = Agent(model, model_settings=settings)
    if stream:
        async with agent.run_stream("hello") as run:
            return await run.get_output()
    return (await agent.run("hello")).output


def _ai_headers(request: httpx.Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.startswith("ai-")}


@pytest.fixture(autouse=True)
def _fresh_warning(monkeypatch, real_agents_and_mcp):
    # Script-style suites stub ``Agent`` at import; these tests need the real one.
    monkeypatch.setattr(shared, "_call_headers_warned", False)


# --- the OpenAI-style route ------------------------------------------------

@pytest.mark.parametrize("stream", [False, True])
async def test_openai_route_sends_the_headers(stream):
    seen: list[httpx.Request] = []
    with sending(OURS):
        assert await _run(_openai_model(seen), stream=stream) == "hi"
    assert len(seen) == 1
    assert _ai_headers(seen[0]) == OURS


@pytest.mark.parametrize("stream", [False, True])
async def test_openai_route_keeps_the_callers_headers(stream):
    seen: list[httpx.Request] = []
    settings = {"extra_headers": {"x-caller": "mine",
                                  "ai-inference-observability-persistence-mode": "full"}}
    with sending(OURS):
        await _run(_openai_model(seen), stream=stream, settings=settings)
    assert seen[0].headers["x-caller"] == "mine"
    assert _ai_headers(seen[0]) == OURS          # ours win
    assert settings["extra_headers"] == {        # the caller's dict is not touched
        "x-caller": "mine", "ai-inference-observability-persistence-mode": "full"}


@pytest.mark.parametrize("stream", [False, True])
async def test_openai_route_unchanged_without_the_headers(stream):
    ours: list[httpx.Request] = []
    parent: list[httpx.Request] = []
    await _run(_openai_model(ours), stream=stream)
    await _run(_openai_model(parent, cls=OpenAIChatModel), stream=stream)
    assert _ai_headers(ours[0]) == {}
    assert set(ours[0].headers.keys()) == set(parent[0].headers.keys())


def test_unset_hands_on_the_very_same_settings():
    settings = {"temperature": 0.1}
    assert current_call_headers.get() is None
    assert shared._with_call_headers(settings) is settings
    assert settings == {"temperature": 0.1}


def test_set_hands_on_a_copy():
    settings = {"temperature": 0.1, "extra_headers": {"x-a": "1"}}
    token = current_call_headers.set(dict(OURS))
    try:
        merged = shared._with_call_headers(settings)
    finally:
        current_call_headers.reset(token)
    assert merged is not settings
    assert merged["extra_headers"] == {"x-a": "1", **OURS}
    assert settings == {"temperature": 0.1, "extra_headers": {"x-a": "1"}}


@pytest.mark.parametrize("stream", [False, True])
async def test_openai_route_never_fails_on_a_broken_value(stream, caplog):
    seen: list[httpx.Request] = []
    token = current_call_headers.set(object())   # type: ignore[arg-type]
    try:
        assert await _run(_openai_model(seen), stream=stream) == "hi"
        assert await _run(_openai_model(seen), stream=stream) == "hi"
    finally:
        current_call_headers.reset(token)
    assert len(seen) == 2 and all(_ai_headers(r) == {} for r in seen)
    warnings = [r for r in caplog.records if r.name == "agents.shared"
                and r.levelname == "WARNING"]
    assert len(warnings) == 1                    # once per process
    assert warnings[0].getMessage().endswith("(TypeError)")


# --- the Bedrock route -----------------------------------------------------

class _Raw:
    def __init__(self, body: bytes):
        self._body = body

    def stream(self, **_):
        yield self._body


def _bedrock_client(captured: list[dict]):
    import boto3
    from botocore import UNSIGNED
    from botocore.awsrequest import AWSResponse
    from botocore.config import Config

    client = boto3.client("bedrock-runtime", region_name="us-east-1",
                          config=Config(signature_version=UNSIGNED,
                                        retries={"max_attempts": 1}))

    def capture(request, **_):
        captured.append(dict(request.headers))
        body = json.dumps({
            "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "metrics": {"latencyMs": 1},
        }).encode()
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, _Raw(body))

    client.meta.events.register("before-send.bedrock-runtime", capture)
    shared._register_call_headers(client)
    return client


async def _converse(client):
    def call():
        try:
            return client.converse(
                modelId="anthropic.claude", messages=[
                    {"role": "user", "content": [{"text": "hello"}]}])
        except Exception:  # noqa: BLE001 - the captured request is what counts
            return None
    return await anyio.to_thread.run_sync(call)


def _ai(headers: dict) -> dict[str, str]:
    return {str(k).lower(): (v.decode() if isinstance(v, bytes) else v)
            for k, v in headers.items() if str(k).lower().startswith("ai-")}


async def test_bedrock_route_sends_the_headers():
    captured: list[dict] = []
    client = _bedrock_client(captured)
    with sending(OURS):
        await _converse(client)
    assert len(captured) == 1 and _ai(captured[0]) == OURS


async def test_bedrock_route_unchanged_without_the_headers():
    captured: list[dict] = []
    plain: list[dict] = []
    client = _bedrock_client(captured)
    await _converse(client)
    assert len(captured) == 1 and _ai(captured[0]) == {}

    import boto3
    from botocore import UNSIGNED
    from botocore.awsrequest import AWSResponse
    from botocore.config import Config
    other = boto3.client("bedrock-runtime", region_name="us-east-1",
                         config=Config(signature_version=UNSIGNED,
                                       retries={"max_attempts": 1}))

    def capture(request, **_):
        plain.append(dict(request.headers))
        return AWSResponse(request.url, 500, {}, _Raw(b"{}"))
    other.meta.events.register("before-send.bedrock-runtime", capture)
    await _converse(other)
    assert {k.lower() for k in captured[0]} == {k.lower() for k in plain[0]}


async def test_bedrock_route_never_fails_on_a_broken_value(caplog):
    captured: list[dict] = []
    client = _bedrock_client(captured)
    token = current_call_headers.set(object())   # type: ignore[arg-type]
    try:
        await _converse(client)
        await _converse(client)
    finally:
        current_call_headers.reset(token)
    assert len(captured) == 2 and all(_ai(h) == {} for h in captured)
    warnings = [r for r in caplog.records if r.name == "agents.shared"
                and r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_bedrock_handler_returns_nothing():
    # A before-call handler that returns a value short-circuits the call.
    params = {"headers": {}}
    with sending(OURS):
        assert shared._add_call_headers(params) is None
    assert params["headers"] == OURS
    params = {"headers": {}}
    assert shared._add_call_headers(params) is None and params["headers"] == {}


def test_registering_never_fails(caplog):
    shared._register_call_headers(object())       # no ``meta``: logged, not raised
    assert [r.levelname for r in caplog.records if r.name == "agents.shared"] == ["WARNING"]


# --- standalone ------------------------------------------------------------

def test_shared_loads_no_register_module():
    code = ("import sys; import agents.shared; "
            "bad = sorted(m for m in sys.modules if m.startswith('agents.nexus')); "
            "assert not bad, bad")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


# --- end to end through the register's wrapper -----------------------------

@pytest.fixture
async def register(monkeypatch):
    from agents.nexus import logged_model, register, sender, state
    from tests.nexus_helpers import Recorder, register_on
    for name in ("NEXUS_SEND_PAYLOAD", "NEXUS_FALLBACK_USE_CASE", "NEXUS_LANDSCAPE",
                 "AICORE_RESOURCE_GROUP"):
        monkeypatch.delenv(name, raising=False)
    rec = Recorder()
    await register_on(monkeypatch, rec)
    yield rec
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


@pytest.mark.parametrize("stream", [False, True])
async def test_wrapper_labels_the_request_with_its_correlation(register, stream):
    from agents.nexus import sender
    from agents.nexus.context import nexus_run
    from agents.nexus.logged_model import wrap
    seen: list[httpx.Request] = []
    with nexus_run("r1"):
        await _run(wrap(_openai_model(seen), "gpt-4o"), stream=stream)
    await sender.drain()
    correlation = register.of("logRequest")[0]["body"]["correlationId"]
    assert seen[0].headers["ai-inference-observability-persistence-mode"] == "metadata"
    labels = seen[0].headers["ai-inference-observability-labels"].split(",")
    assert f"ext.ai.sap.com/correlation={correlation}" in labels
    assert current_call_headers.get() is None


async def test_wrapper_off_sends_no_header():
    from agents.nexus import state
    from agents.nexus.logged_model import wrap
    state.reset_for_tests()
    seen: list[httpx.Request] = []
    await _run(wrap(_openai_model(seen), "gpt-4o"))
    await _run(wrap(_openai_model(seen), "gpt-4o"), stream=True)
    assert len(seen) == 2 and all(_ai_headers(r) == {} for r in seen)


async def test_wrapper_direct_provider_sends_no_header(register):
    from agents.nexus import sender
    from agents.nexus.logged_model import wrap
    seen: list[httpx.Request] = []
    await _run(wrap(_openai_model(seen), "openai:gpt-4o"))
    await sender.drain()
    assert len(register.of("logRequest")) == 1
    assert _ai_headers(seen[0]) == {}
