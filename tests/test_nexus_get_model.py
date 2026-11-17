"""Every model that comes out of ``agents.shared.get_model`` is behind the
logging wrapper of the agent register.

``tests/test_agent_model.py`` replaces ``shared.get_model`` for the whole
session at import, so these tests call ``shared._get_model_unpatched``, the
real function. With the register off (the default) the wrapper is a pass
through: same answer, same attributes, one cached object per name.
"""
from __future__ import annotations

import pytest
from pydantic_ai import Agent

from agents import shared
from agents.nexus import sender, state
from agents.nexus.logged_model import NexusLoggedModel
from tests.nexus_helpers import scripted_model

pytestmark = pytest.mark.usefixtures("real_agents_and_mcp")


@pytest.fixture(autouse=True)
def _off():
    state.reset_for_tests()
    sender.reset_for_tests()
    yield
    state.reset_for_tests()
    sender.reset_for_tests()


def test_get_model_wraps_and_caches(monkeypatch):
    built = []
    monkeypatch.setattr(shared, "_build_openai_model",
                        lambda n: built.append(n) or scripted_model(name=n))
    monkeypatch.setattr(shared, "_models", {})
    first = shared._get_model_unpatched("gpt-4o")
    assert isinstance(first, NexusLoggedModel) and first.model_name == "gpt-4o"
    assert shared._get_model_unpatched("gpt-4o") is first and built == ["gpt-4o"]
    assert shared._get_model_unpatched("  gpt-4o  ") is first


def test_bedrock_route_is_wrapped(monkeypatch):
    monkeypatch.setattr(shared, "_build_bedrock_model", lambda n: scripted_model(name=n))
    monkeypatch.setattr(shared, "_models", {})
    assert isinstance(shared._get_model_unpatched("anthropic--claude-x"), NexusLoggedModel)


def test_a_failing_build_raises_as_before_and_caches_nothing(monkeypatch):
    def broken(name):
        raise RuntimeError("deployment not found")
    monkeypatch.setattr(shared, "_build_openai_model", broken)
    monkeypatch.setattr(shared, "_models", {})
    with pytest.raises(RuntimeError, match="deployment not found"):
        shared._get_model_unpatched("gpt-4o")
    assert shared._models == {}


def test_real_openai_model_attributes_pass_through(monkeypatch):
    """The wrapped SAP AI Core model keeps its client, profile, system and settings."""
    import gen_ai_hub.proxy

    class _ProxyClient:
        request_header: dict[str, str] = {}

    monkeypatch.setattr(gen_ai_hub.proxy, "get_proxy_client", lambda *a, **k: _ProxyClient())
    monkeypatch.delenv("AICORE_MAX_RETRIES", raising=False)
    monkeypatch.setattr(shared, "_models", {})
    model = shared._get_model_unpatched("gpt-4o")
    assert isinstance(model, NexusLoggedModel)
    inner = model.wrapped
    assert isinstance(inner, shared.SAPAICoreModel)
    assert model.model_name == "gpt-4o" and model.system == inner.system
    assert model.client is inner.client and model.client.max_retries == 8
    assert model.profile is inner.profile and model.settings is inner.settings


async def test_off_path_answers_as_before(monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def answer(messages, info):
        return ModelResponse(parts=[TextPart("plain")])

    async def stream(messages, info):
        yield "stre"
        yield "amed"

    monkeypatch.setattr(shared, "_build_openai_model",
                        lambda n: FunctionModel(answer, stream_function=stream, model_name=n))
    monkeypatch.setattr(shared, "_models", {})
    model = shared._get_model_unpatched("gpt-4o")
    assert (await Agent(model).run("hi")).output == "plain"
    async with Agent(model).run_stream("hi") as run:
        assert await run.get_output() == "streamed"
    assert sender.pending() == []


async def test_off_path_keeps_the_model_exception(monkeypatch):
    exc = RuntimeError("model broke")
    monkeypatch.setattr(shared, "_build_openai_model", lambda n: scripted_model(name=n, fail=exc))
    monkeypatch.setattr(shared, "_models", {})
    with pytest.raises(RuntimeError) as raised:
        await Agent(shared._get_model_unpatched("gpt-4o")).run("hi")
    assert raised.value is exc and sender.pending() == []


def test_unpatched_is_the_real_function():
    assert shared._get_model_unpatched.__module__ == "agents.shared"
    assert shared._get_model_unpatched.__name__ == "get_model"
