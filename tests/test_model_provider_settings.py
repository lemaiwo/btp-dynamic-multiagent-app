"""The admin's direct model provider settings (``agents/model_providers.py``,
``agents/model_provider_routes.py``) and how ``agents/shared.py`` uses them."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

from agents import model_provider_routes, model_providers, shared  # noqa: E402
from agents.db import ModelProvider, SessionLocal, init_db  # noqa: E402

SECRET = "sk-test-SECRET-value-0123456789"
_VARIABLES = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_MODELS", "OPENAI_MODELS",
    "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "AICORE_AVAILABLE_MODELS", "AICORE_MODEL",
    "DEFAULT_MODEL", "AICORE_CLIENT_ID", "AICORE_SERVICE_KEY", "AICORE_AUTH_URL",
)


@pytest.fixture(autouse=True)
async def clean(monkeypatch, tmp_path):
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AICORE_HOME", str(tmp_path))
    monkeypatch.setattr(shared, "_discover_deployed_models", lambda refresh=False: [])
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(ModelProvider))
        await session.commit()
    shared.set_direct_provider_settings({})
    yield
    shared.set_direct_provider_settings({})


@pytest.fixture
async def client(monkeypatch):
    app = FastAPI()
    app.include_router(model_provider_routes.router, prefix="/admin")
    reloads = []

    async def fake_reload():
        reloads.append(1)

    from agents.chat_app import dynamic_chat_app
    from agents.registry import registry

    monkeypatch.setattr(registry, "reload", fake_reload)
    monkeypatch.setattr(dynamic_chat_app, "refresh", lambda: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        http.reloads = reloads  # type: ignore[attr-defined]
        yield http


# --- validation ----------------------------------------------------------------
@pytest.mark.parametrize(
    "body, field",
    [
        ({}, "enabled"),
        ({"enabled": "true"}, "enabled"),
        ({"enabled": True, "api_key": "has blank"}, "api_key"),
        ({"enabled": True, "api_key": "x" * 501}, "api_key"),
        ({"enabled": True, "api_key": 5}, "api_key"),
        ({"enabled": True, "base_url": "http://api.example.com/v1"}, "base_url"),
        ({"enabled": True, "base_url": "https://user:pw@api.example.com"}, "base_url"),
        ({"enabled": True, "base_url": "https://api.example.com/v1?x=1"}, "base_url"),
        ({"enabled": True, "models": "gpt"}, "models"),
        ({"enabled": True, "models": ["ok", "not a name!"]}, "models[1]"),
        ({"enabled": True, "models": ["m"] * 31}, "models"),
        ({"enabled": True, "surprise": 1}, "body"),
    ],
)
def test_refusals_name_the_field_never_the_value(body, field):
    with pytest.raises(ValueError) as refused:
        model_providers.validate("openai", body)
    assert str(refused.value).startswith(field + ":")
    for value in body.values():
        if isinstance(value, str) and len(value) > 4:
            assert value not in str(refused.value)


def test_validate_cleans_models_and_keeps_absent_keys():
    cleaned = model_providers.validate(
        "anthropic", {"enabled": True, "models": ["anthropic:claude-x", " claude-y ", "claude-x"]})
    assert cleaned["models"] == ["claude-x", "claude-y"]
    assert cleaned["api_key"] is model_providers._KEEP
    assert model_providers.validate("openai", {"enabled": True, "api_key": None})["api_key"] is None
    assert model_providers.validate(
        "openai", {"enabled": True, "base_url": "http://fake-model:9000/v1/"}
    )["base_url"] == "http://fake-model:9000/v1"
    with pytest.raises(ValueError):
        model_providers.validate("mistral", {"enabled": True})


# --- how shared uses a setting ---------------------------------------------------
def test_stored_setting_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.setenv("ANTHROPIC_MODELS", "env-model")
    assert shared.direct_api_key("anthropic") == "from-env"
    assert shared.direct_key_source("anthropic") == "env"

    shared.set_direct_provider_settings(
        {"anthropic": {"enabled": True, "api_key": "stored", "base_url": "", "models": ["m1"]}})
    assert shared.direct_api_key("anthropic") == "stored"
    assert shared.direct_key_source("anthropic") == "setting"
    assert shared.direct_models() == ["anthropic:m1"]

    # Stored without a key or models: each falls back to its variable.
    shared.set_direct_provider_settings(
        {"anthropic": {"enabled": True, "api_key": "", "base_url": "", "models": []}})
    assert shared.direct_api_key("anthropic") == "from-env"
    assert shared.direct_models() == ["anthropic:env-model"]

    # Switched off: off, whatever the environment holds.
    shared.set_direct_provider_settings(
        {"anthropic": {"enabled": False, "api_key": "stored", "base_url": "", "models": []}})
    assert shared.direct_api_key("anthropic") == ""
    assert shared.direct_key_source("anthropic") == "none"
    assert shared.direct_models() == []
    with pytest.raises(shared.DirectModelError):
        shared._build_model("anthropic:m1")


def test_a_new_setting_forgets_the_models_built_with_the_old_key(monkeypatch):
    monkeypatch.setattr(shared, "_models", {
        "anthropic:m": object(), "openai:m": object(), "gpt-4o": "aicore-model"})
    shared.set_direct_provider_settings({})
    assert shared._models == {"gpt-4o": "aicore-model"}


def test_the_stored_key_and_base_url_build_the_client():
    shared.set_direct_provider_settings({"openai": {
        "enabled": True, "api_key": SECRET, "base_url": "https://llm.example.test/v1",
        "models": ["m"]}})
    model = shared._build_model("openai:m")
    assert model.client.api_key == SECRET
    assert str(model.client.base_url).startswith("https://llm.example.test/v1")


# --- the routes ------------------------------------------------------------------
async def test_save_load_and_never_return_the_key(client):
    saved = await client.put("/admin/api/model-providers/anthropic", json={
        "enabled": True, "api_key": SECRET, "models": ["claude-opus-5-5"]})
    assert saved.status_code == 200 and saved.json()["reloaded"] is True
    assert client.reloads == [1]
    listed = (await client.get("/admin/api/model-providers")).json()
    assert SECRET not in json.dumps(listed) and SECRET not in saved.text
    anthropic = next(p for p in listed["providers"] if p["provider"] == "anthropic")
    assert anthropic == {
        "provider": "anthropic", "stored": True, "enabled": True, "has_key": True,
        "key_source": "setting", "has_stored_key": True, "base_url": "",
        "models": ["claude-opus-5-5"], "effective_models": ["anthropic:claude-opus-5-5"],
    }
    assert listed["available_models"] == ["anthropic:claude-opus-5-5"]
    assert shared.direct_api_key("anthropic") == SECRET

    # A save without api_key keeps the stored one; null clears it.
    await client.put("/admin/api/model-providers/anthropic", json={"enabled": True, "models": []})
    assert shared.direct_api_key("anthropic") == SECRET
    await client.put("/admin/api/model-providers/anthropic", json={"enabled": True, "api_key": None})
    assert shared.direct_api_key("anthropic") == ""

    # What is stored survives a fresh load (a restart).
    await client.put("/admin/api/model-providers/openai", json={
        "enabled": True, "api_key": SECRET, "base_url": "https://llm.example.test/v1"})
    shared.set_direct_provider_settings({})
    await model_providers.refresh()
    assert shared.direct_api_key("openai") == SECRET
    assert shared.direct_base_url("openai") == "https://llm.example.test/v1"


async def test_refused_bodies_and_unknown_provider(client):
    refused = await client.put("/admin/api/model-providers/openai", json={
        "enabled": True, "api_key": "bad key " + SECRET})
    assert refused.status_code == 422 and SECRET not in refused.text
    assert refused.json()["detail"].startswith("api_key:")
    assert client.reloads == []
    assert (await client.put("/admin/api/model-providers/mistral", json={"enabled": True})).status_code == 404
    not_json = await client.put("/admin/api/model-providers/openai", content=b"{nope")
    assert not_json.status_code == 422
    assert (await client.post("/admin/api/model-providers/mistral/test")).status_code == 404


async def test_a_failed_reload_is_reported_not_raised(client, monkeypatch):
    from agents.registry import registry

    async def broken():
        raise RuntimeError("no model")

    monkeypatch.setattr(registry, "reload", broken)
    saved = await client.put("/admin/api/model-providers/openai", json={"enabled": False})
    assert saved.status_code == 200
    assert saved.json()["reloaded"] is False and saved.json()["reload_failed"] is True


async def test_test_call_reports_class_and_status_only(client, monkeypatch):
    assert (await client.post("/admin/api/model-providers/openai/test")).json() == {
        "ok": False, "error": "no_key"}
    shared.set_direct_provider_settings({"openai": {
        "enabled": True, "api_key": SECRET, "base_url": "http://localhost:9/v1", "models": []}})
    answer = (await client.post("/admin/api/model-providers/openai/test")).json()
    assert answer["ok"] is False and answer["error"] == "APIConnectionError"
    assert SECRET not in json.dumps(answer)
