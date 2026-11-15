"""Direct API providers next to SAP AI Core (``agents/shared.py``): a model
name prefixed ``anthropic:`` / ``openai:`` is served by that provider's own
API, everything else stays an AI Core deployment."""

from __future__ import annotations

import pytest

from agents import shared

# ``shared._build_model`` is what ``get_model`` routes through; the tests call
# it because other suites replace ``shared.get_model`` for the whole run.

_VARIABLES = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_MODELS", "OPENAI_MODELS",
    "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "AICORE_AVAILABLE_MODELS", "AICORE_MODEL",
    "DEFAULT_MODEL", "AICORE_CLIENT_ID", "AICORE_SERVICE_KEY", "AICORE_AUTH_URL",
)


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AICORE_HOME", str(tmp_path))  # no SDK config file
    monkeypatch.setattr(shared, "_discover_deployed_models", lambda refresh=False: [])


def test_prefix_decides_the_provider():
    assert shared.direct_provider_of("anthropic:claude-opus-5-5") == "anthropic"
    assert shared.direct_provider_of("OpenAI:gpt-4o") == "openai"
    # AI Core deployment names are not direct, whatever they contain.
    for name in ("anthropic--claude-4.6-opus", "gpt-4o", "claude-3", "anthropic:", "other:x", ""):
        assert shared.direct_provider_of(name) is None


def test_without_keys_nothing_changes():
    assert shared.direct_models() == []
    assert shared.default_model_name() == "gpt-4o"
    assert shared.available_models() == [
        m.strip() for m in shared.DEFAULT_AVAILABLE_MODELS.split(",")
    ]


def test_direct_models_only_when_no_aicore(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_MODELS", "gpt-x, openai:gpt-y ,gpt-x")
    models = shared.available_models()
    assert models == [
        "anthropic:claude-opus-5-5", "anthropic:claude-sonnet-5-5",
        "anthropic:claude-haiku-5-5", "openai:gpt-x", "openai:gpt-y",
    ]
    assert shared.default_model_name() == "anthropic:claude-opus-5-5"
    monkeypatch.setenv("DEFAULT_MODEL", "openai:gpt-y")
    assert shared.default_model_name() == "openai:gpt-y"


def test_direct_models_follow_the_aicore_deployments(monkeypatch):
    monkeypatch.setenv("AICORE_CLIENT_ID", "bound")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MODELS", "claude-opus-5-5")
    monkeypatch.setattr(shared, "_discover_deployed_models", lambda refresh=False: ["gpt-4o"])
    assert shared.available_models() == ["gpt-4o", "anthropic:claude-opus-5-5"]
    assert shared.default_model_name() == "gpt-4o"
    monkeypatch.setenv("AICORE_AVAILABLE_MODELS", "a, openai:b")
    assert shared.available_models() == ["a", "openai:b"]


def test_direct_anthropic_model_is_built_with_the_key(monkeypatch):
    from pydantic_ai.models.anthropic import AnthropicModel

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-anthropic")
    model = shared._build_model("anthropic:claude-opus-5-5")
    assert isinstance(model, AnthropicModel)
    assert model.model_name == "claude-opus-5-5"
    assert model.client.api_key == "test-key-anthropic"


def test_direct_openai_model_uses_the_shared_model_class(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.test/v1")
    model = shared._build_model("openai:gpt-4o")
    assert isinstance(model, shared.SAPAICoreModel)
    assert model.model_name == "gpt-4o"
    assert model.client.api_key == "test-key-openai"
    assert str(model.client.base_url).startswith("https://llm.example.test/v1")


def test_missing_key_names_the_variable_only(monkeypatch):
    with pytest.raises(shared.DirectModelError, match="ANTHROPIC_API_KEY"):
        shared._build_model("anthropic:claude-opus-5-5")
    with pytest.raises(shared.DirectModelError, match="OPENAI_API_KEY"):
        shared._build_model("openai:gpt-4o")


def test_unprefixed_names_still_go_to_aicore(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    built = []
    monkeypatch.setattr(shared, "_build_bedrock_model", lambda name: built.append(("bedrock", name)) or name)
    monkeypatch.setattr(shared, "_build_openai_model", lambda name: built.append(("openai", name)) or name)
    shared._build_model("anthropic--claude-4.6-opus")
    shared._build_model("gpt-4o")
    assert built == [("bedrock", "anthropic--claude-4.6-opus"), ("openai", "gpt-4o")]
