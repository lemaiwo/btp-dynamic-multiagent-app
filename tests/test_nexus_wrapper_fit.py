"""What the agent-register wrapper relies on in the pinned pydantic-ai.

A failure here means the wrapper (agents/nexus/logged_model.py) must move to
the fallback: a mixin on SAPAICoreModel and a subclass of BedrockConverseModel
with the same two overrides (see the design, "Approach").

The tests that run an ``Agent`` take ``real_agents_and_mcp``: script-style
suites replace ``Agent.__init__`` / ``Agent.run`` at import, so without the
fixture the outcome would depend on the file order.
"""
import inspect

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import RequestUsage


def test_wrapper_signatures():
    req = inspect.signature(WrapperModel.request)
    assert list(req.parameters)[:4] == [
        "self", "messages", "model_settings", "model_request_parameters"]
    stream = inspect.signature(WrapperModel.request_stream)
    assert "run_context" in stream.parameters


def _answer(messages, info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("ok")],
                         usage=RequestUsage(input_tokens=7, output_tokens=3))


async def test_agent_runs_on_a_wrapper_and_keeps_usage(real_agents_and_mcp):
    inner = FunctionModel(_answer, model_name="scripted")
    wrapped = WrapperModel(inner)
    assert wrapped.model_name == "scripted"
    assert wrapped.profile is inner.profile or wrapped.profile == inner.profile
    result = await Agent(wrapped).run("hi")
    assert result.output == "ok"
    assert (result.usage().input_tokens, result.usage().output_tokens) == (7, 3)


async def test_streamed_run_on_a_wrapper(real_agents_and_mcp):
    async def stream(messages, info):
        yield "o"
        yield "k"
    wrapped = WrapperModel(FunctionModel(stream_function=stream, model_name="scripted"))
    async with Agent(wrapped).run_stream("hi") as run:
        assert await run.get_output() == "ok"


def test_profile_of_the_app_models_passes_through():
    from openai import AsyncOpenAI
    from pydantic_ai.profiles.openai import OpenAIModelProfile
    from pydantic_ai.providers.openai import OpenAIProvider

    from agents.shared import SAPAICoreModel
    inner = SAPAICoreModel(
        "gpt-4o",
        provider=OpenAIProvider(openai_client=AsyncOpenAI(api_key="x", base_url="https://x.invalid")),
        profile=OpenAIModelProfile(openai_supports_strict_tool_definition=False),
    )
    wrapped = WrapperModel(inner)
    assert wrapped.profile.openai_supports_strict_tool_definition is False
    assert wrapped.model_name == "gpt-4o"
    assert wrapped.system == inner.system


def test_bedrock_model_passes_through():
    """Built on a botocore client without credentials: nothing is sent."""
    boto3 = pytest.importorskip("boto3")
    from pydantic_ai.models.bedrock import BedrockConverseModel
    from pydantic_ai.providers.bedrock import BedrockProvider

    client = boto3.client("bedrock-runtime", region_name="eu-central-1")
    inner = BedrockConverseModel(
        "anthropic--claude-4-sonnet", provider=BedrockProvider(bedrock_client=client))
    wrapped = WrapperModel(inner)
    assert wrapped.model_name == "anthropic--claude-4-sonnet"
    assert wrapped.system == inner.system == "bedrock"
    assert wrapped.profile is inner.profile
