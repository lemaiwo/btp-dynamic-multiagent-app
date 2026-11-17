"""The agent register in the app lifespan: off path unchanged, on path started and stopped."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import pytest  # noqa: E402

import app as app_module  # noqa: E402
from agents.db import init_db  # noqa: E402
from agents.destination import DestinationServiceConfig  # noqa: E402
from agents.nexus import lifecycle, logged_model, register, sender, state  # noqa: E402
from tests.nexus_helpers import FakeResolver, Recorder  # noqa: E402

CFG = DestinationServiceConfig(client_id="cid", client_secret="not-a-secret",
                               token_url="https://uaa.example.test/oauth/token",
                               api_url="https://destination.example.test")


def _reset():
    lifecycle.reset_for_tests()
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


@pytest.fixture(autouse=True)
async def _env(monkeypatch):
    await init_db()
    _reset()
    for name in ("NEXUS_DESTINATION", "NEXUS_USER_DESTINATION", "NEXUS_FALLBACK_USE_CASE"):
        monkeypatch.delenv(name, raising=False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(app_module.registry, "reload", noop)
    monkeypatch.setattr(app_module.dynamic_chat_app, "refresh", lambda: None)
    monkeypatch.setattr(app_module, "seed_from_file_if_empty", noop)
    monkeypatch.setattr(app_module, "ensure_ide_seed", noop)
    monkeypatch.setattr(app_module, "DB_HEARTBEAT_SECONDS", 0)
    monkeypatch.setattr(app_module, "TOKEN_KEEPWARM_SECONDS", 0)

    async def no_rows():
        return []

    monkeypatch.setattr(register, "_load_rows", no_rows)
    yield
    _reset()


def _lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "agents.nexus.lifecycle" and r.levelno == logging.INFO]


async def test_off_path_logs_one_line_and_makes_no_request(monkeypatch, caplog):
    recorder = Recorder()
    monkeypatch.setattr(lifecycle, "_transport", recorder.transport())
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        async with app_module.lifespan(app_module.app):
            assert not state.enabled() and not sender.running()
            assert not lifecycle.timer_running()
    assert _lines(caplog) == ["agent register integration: off (no destination service)"]
    assert recorder.requests == []


async def test_on_path_starts_and_shutdown_stops_the_worker(monkeypatch, caplog):
    recorder = Recorder()
    monkeypatch.setattr(lifecycle, "config_from_environment", lambda environ: CFG)
    monkeypatch.setattr(lifecycle, "DestinationResolver",
                        lambda name, cfg: FakeResolver(name))
    monkeypatch.setattr(lifecycle, "_transport", recorder.transport())
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        async with app_module.lifespan(app_module.app):
            assert state.enabled() and sender.running()
        assert not sender.running() and lifecycle.client() is None
    assert any("on (destination NEXUS_AI_PLATFORM)" in line for line in _lines(caplog))


async def test_unexpected_start_failure_does_not_stop_the_app(monkeypatch, caplog):
    async def boom():
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(app_module.nexus_lifecycle, "start", boom)
    with caplog.at_level(logging.WARNING):
        async with app_module.lifespan(app_module.app):
            pass
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert "secret-detail" not in caplog.text
