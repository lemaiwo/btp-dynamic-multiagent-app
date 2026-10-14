"""An empty model response is not replayed to the OpenAI-compatible endpoint.

Seen on acc/AI with ``gpt-5.6-terra`` (2026-10-14): after a tool call the
model answered with no text and no tool call; pydantic-ai keeps that
``ModelResponse`` in the history and asks again, and ``OpenAIChatModel`` maps
it to an assistant message with ``content: null`` and no ``tool_calls``, which
the endpoint refuses (``Invalid value for 'content': expected a string, got
null``), so the whole run fails. ``SAPAICoreModel`` leaves such a message out
of the payload. Every other message is mapped as before (a thinking-only
response is replayed as tagged text, a string).
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

import agents.shared as shared


class _ProxyClient:
    request_header: dict[str, str] = {}


@pytest.fixture
def model(monkeypatch):
    import gen_ai_hub.proxy

    monkeypatch.setattr(gen_ai_hub.proxy, "get_proxy_client", lambda *a, **k: _ProxyClient())
    return shared._build_openai_model("gpt-4o")


def _history(middle: ModelResponse) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content="who is on call?")]),
        ModelResponse(parts=[ToolCallPart(tool_name="submit_result", args={"a": 1},
                                          tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="submit_result", content={"ok": True},
                                           tool_call_id="c1")]),
        middle,
        ModelRequest(parts=[]),
    ]


async def test_an_empty_response_is_left_out(model):
    messages = await model._map_messages(_history(ModelResponse(parts=[])),
                                         ModelRequestParameters())
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool"]
    assert messages[1]["tool_calls"][0]["id"] == "c1"


async def test_a_thinking_only_response_is_kept_as_text(model):
    # pydantic-ai replays thinking as tagged text, a string the endpoint takes.
    messages = await model._map_messages(
        _history(ModelResponse(parts=[ThinkingPart(content="hmm")])),
        ModelRequestParameters())
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert isinstance(messages[3]["content"], str) and "hmm" in messages[3]["content"]


async def test_text_and_tool_call_responses_are_kept(model):
    messages = await model._map_messages(
        _history(ModelResponse(parts=[TextPart(content="done")])),
        ModelRequestParameters())
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[3]["content"] == "done"
    for m in messages:
        if m["role"] == "assistant":
            assert m.get("content") is not None or m.get("tool_calls")
