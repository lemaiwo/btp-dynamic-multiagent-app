"""``scripts/ensure_aicore_setup.py`` (the ``before-start`` deploy hook) and
the time the deploy gives it.

The hook's output lands in the deploy log: an exception's text from the AI
Core SDK may hold a URL or a response body, so only its class is printed.
A malformed ``AICORE_ENSURE_TIMEOUT`` falls back to the default with a
warning instead of failing the deploy with a traceback.

No network: the SDK client is a stub.

Run:  python -m pytest tests/test_ensure_aicore_setup.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SECRET = "https://tenant-s3cret.example.test/v2?token=abc"


def _load():
    """The script, without its ``load_dotenv`` (a local ``.env`` must not
    leak into the environment of the rest of the suite)."""
    spec = importlib.util.spec_from_file_location(
        "ensure_aicore_setup_script", ROOT / "scripts" / "ensure_aicore_setup.py")
    module = importlib.util.module_from_spec(spec)
    quiet = types.ModuleType("dotenv")
    quiet.load_dotenv = lambda *args, **kwargs: False
    real = sys.modules.get("dotenv")
    sys.modules["dotenv"] = quiet
    try:
        spec.loader.exec_module(module)
    finally:
        if real is None:
            sys.modules.pop("dotenv", None)
        else:
            sys.modules["dotenv"] = real
    return module


script = _load()


class _Client:
    base_url = "https://ai.example.test/v2"


def _sdk(monkeypatch, build=None) -> None:
    """A stub of ``ai_core_sdk.ai_core_v2_client``."""
    def from_env(**kwargs):
        if build is not None:
            raise build
        return _Client()

    sdk = types.ModuleType("ai_core_sdk")
    v2 = types.ModuleType("ai_core_sdk.ai_core_v2_client")
    v2.AICoreV2Client = types.SimpleNamespace(from_env=from_env)
    sdk.ai_core_v2_client = v2
    monkeypatch.setitem(sys.modules, "ai_core_sdk", sdk)
    monkeypatch.setitem(sys.modules, "ai_core_sdk.ai_core_v2_client", v2)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AICORE_RESOURCE_GROUP", "billing")
    monkeypatch.delenv("AICORE_ENSURE_STRICT", raising=False)
    monkeypatch.delenv("AICORE_ENSURE_TIMEOUT", raising=False)
    return monkeypatch


@pytest.mark.parametrize("raw", ["five minutes", "nan", "inf", "-1"])
def test_a_malformed_timeout_falls_back_with_a_warning(env, capsys, raw):
    env.setenv("AICORE_ENSURE_TIMEOUT", raw)
    _sdk(env)
    seen: dict = {}

    def ensure(client, group_id, timeout_s):
        seen["timeout"] = timeout_s
        return True

    env.setattr(script, "ensure_resource_group", ensure)
    assert script.main() == 0
    assert seen["timeout"] == 300.0
    out = capsys.readouterr().out
    assert "AICORE_ENSURE_TIMEOUT" in out and "300" in out
    assert raw not in out


def test_a_blank_timeout_is_the_default_without_a_warning(env, capsys):
    env.setenv("AICORE_ENSURE_TIMEOUT", "  ")
    _sdk(env)
    seen: dict = {}

    def ensure(client, group_id, timeout_s):
        seen["timeout"] = timeout_s
        return True

    env.setattr(script, "ensure_resource_group", ensure)
    assert script.main() == 0
    assert seen["timeout"] == 300.0
    assert "AICORE_ENSURE_TIMEOUT" not in capsys.readouterr().out


def test_a_valid_timeout_is_used_and_nothing_is_warned(env, capsys):
    env.setenv("AICORE_ENSURE_TIMEOUT", "120")
    _sdk(env)
    seen: dict = {}

    def ensure(client, group_id, timeout_s):
        seen["timeout"] = timeout_s
        return True

    env.setattr(script, "ensure_resource_group", ensure)
    assert script.main() == 0
    assert seen["timeout"] == 120.0
    assert "AICORE_ENSURE_TIMEOUT" not in capsys.readouterr().out


def test_a_failed_ensure_prints_the_exception_class_only(env, capsys):
    _sdk(env)

    def ensure(client, group_id, timeout_s):
        raise PermissionError(f"403 from {SECRET}: body")

    env.setattr(script, "ensure_resource_group", ensure)
    assert script.main() == 1
    out = capsys.readouterr().out
    assert "Could not ensure resource group 'billing' (PermissionError)" in out
    assert "s3cret" not in out and "token=" not in out and "body" not in out
    # The operator's hint is unchanged.
    assert "AICORE_ENSURE_STRICT=false" in out


def test_a_client_that_cannot_be_built_prints_the_exception_class_only(env, capsys):
    _sdk(env, build=ValueError(f"bad credentials for {SECRET}"))
    assert script.main() == 1
    out = capsys.readouterr().out
    assert "Could not build an AI Core client (ValueError)" in out
    assert "s3cret" not in out and "token=" not in out
    env.setenv("AICORE_ENSURE_STRICT", "false")
    assert script.main() == 0


def _app_module() -> dict:
    mta = yaml.safe_load((ROOT / "mta.yaml").read_text())
    return next(m for m in mta["modules"]
                if any(h.get("name") == "ensure-aicore-resource-group"
                       for h in m.get("hooks") or []))


def test_the_hook_has_a_task_timeout_above_the_scripts_wait():
    """A hook is a CF task: its timeout is the module's
    ``task-execution-timeout`` in seconds (SAP default 12 h). It must leave
    the script's 300 s wait plus the task's start, and end a hung hook long
    before 12 h."""
    module = _app_module()
    timeout = module["parameters"].get("task-execution-timeout")
    assert type(timeout) is int and 300 + 60 <= timeout <= 3600
    (hook,) = module["hooks"]
    assert hook["parameters"]["command"] == "python scripts/ensure_aicore_setup.py"
    assert hook["phases"] == ["deploy.application.before-start",
                              "blue-green.application.before-start.idle"]
