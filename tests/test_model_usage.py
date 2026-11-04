"""The token counts of an AI Core answer reach pydantic-ai's usage.

pydantic-ai 1.72 maps OpenAI usage through genai-prices and swallows every
error of that mapping, answering zero tokens; with the genai-prices versions
in use (0.1.9 locally, 0.2.1 on the platform) every chat answer that carries
``completion_tokens_details`` counted 0 in / 0 out (seen on acc/AI). The model
class reads the counts as the answer says them.
"""
from __future__ import annotations

from openai import omit as OMIT
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from pydantic_ai.providers.openai import OpenAIProvider

from agents import shared
from agents.shared import SAPAICoreModel, SAPAICoreStreamedResponse

USAGE = {
    "prompt_tokens": 12,
    "completion_tokens": 6,
    "total_tokens": 18,
    "prompt_tokens_details": {"audio_tokens": 0, "cache_write_tokens": 0, "cached_tokens": 4},
    "completion_tokens_details": {
        "accepted_prediction_tokens": 0, "audio_tokens": 0,
        "reasoning_tokens": 0, "rejected_prediction_tokens": 0,
    },
}


def _model(name="gpt-5.6-terra"):
    provider = OpenAIProvider(api_key="x", base_url="https://aicore.invalid/v2/inference/deployments/d1")
    return SAPAICoreModel(name, provider=provider)


def _completion(usage=USAGE):
    return ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 1, "model": "gpt-5.6-terra-2026-07-09",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": usage,
    })


def _chunk(usage):
    return ChatCompletionChunk.model_validate({
        "id": "c1", "object": "chat.completion.chunk", "created": 1,
        "model": "gpt-5.6-terra-2026-07-09", "choices": [], "usage": usage,
    })


def test_non_streamed_answer_counts_its_tokens():
    u = _model()._map_usage(_completion())
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (12, 6, 4)
    assert u.details.get("reasoning_tokens") == 0


def test_streamed_chunk_counts_its_tokens():
    model = _model()
    assert model._streamed_response_cls is SAPAICoreStreamedResponse
    resp = SAPAICoreStreamedResponse.__new__(SAPAICoreStreamedResponse)
    resp._model_name = "gpt-5.6-terra"
    resp._provider_name = "openai"
    resp._provider_url = "https://aicore.invalid/v2"
    u = resp._map_usage(_chunk(USAGE))
    assert (u.input_tokens, u.output_tokens) == (12, 6)


def test_a_chunk_without_usage_counts_nothing():
    resp = SAPAICoreStreamedResponse.__new__(SAPAICoreStreamedResponse)
    resp._model_name = "gpt-5.6-terra"
    resp._provider_name = "openai"
    resp._provider_url = "https://aicore.invalid/v2"
    u = resp._map_usage(_chunk(None))
    assert (u.input_tokens, u.output_tokens) == (0, 0)


def test_an_answer_without_counts_stays_zero():
    empty = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    u = _model()._map_usage(_completion(empty))
    assert (u.input_tokens, u.output_tokens) == (0, 0)


def test_the_stream_asks_for_its_usage_chunk(monkeypatch):
    monkeypatch.delenv(shared.AICORE_STREAM_USAGE, raising=False)
    assert _model()._get_stream_options({}) == {"include_usage": True}
    monkeypatch.setenv(shared.AICORE_STREAM_USAGE, "0")
    assert _model()._get_stream_options({}) is OMIT
