"""The agent register in the app lifespan: off path unchanged, on path started and stopped."""
from __future__ import annotations

import ast
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.testdb import use_test_database  # noqa: E402

use_test_database()

import pytest  # noqa: E402

# Imported without the Cloud Foundry variables (agents.db reads them once, at
# import); whatever the environment held is put back right after.
_VCAP = ("VCAP_SERVICES", "VCAP_APPLICATION")
_saved = {name: os.environ.pop(name) for name in _VCAP if name in os.environ}
try:
    import app as app_module  # noqa: E402
finally:
    os.environ.update(_saved)
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
    for name in ("NEXUS_DESTINATION", "NEXUS_USER_DESTINATION", "NEXUS_FALLBACK_USE_CASE",
                 *_VCAP):
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

    monkeypatch.setattr(lifecycle, "start", boom)
    with caplog.at_level(logging.WARNING):
        async with app_module.lifespan(app_module.app):
            pass
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert "secret-detail" not in caplog.text


def test_app_does_not_import_the_lifecycle_at_module_level():
    """Imported inside the lifespan's guard: an import failure cannot stop the app."""
    tree = ast.parse((ROOT / "app.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            names = {a.name for a in node.names}
            assert not (node.module == "agents.nexus" and "lifecycle" in names)
            assert node.module != "agents.nexus.lifecycle"
        if isinstance(node, ast.Import):
            assert all(not a.name.startswith("agents.nexus") for a in node.names)


async def test_a_lifecycle_that_cannot_be_imported_does_not_stop_the_app(monkeypatch, caplog):
    def broken():
        raise ImportError("secret-detail")

    monkeypatch.setattr(app_module, "_nexus_lifecycle", broken)
    with caplog.at_level(logging.WARNING):
        async with app_module.lifespan(app_module.app):
            pass
    assert any("ImportError" in r.getMessage() for r in caplog.records)
    assert "secret-detail" not in caplog.text


async def test_a_raising_stop_does_not_skip_the_other_shutdown_drains(monkeypatch, caplog):
    drained = []

    async def stop(*_a, **_k):
        raise RuntimeError("secret-detail")

    async def drain_deploys(*_a, **_k):
        drained.append("deploys")

    async def audit_drain(*_a, **_k):
        drained.append("audit")
        return 0

    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(app_module, "drain_embedding_deploys", drain_deploys)
    monkeypatch.setattr(app_module.odata_audit, "drain", audit_drain)
    with caplog.at_level(logging.WARNING):
        async with app_module.lifespan(app_module.app):
            pass
    assert drained == ["deploys", "audit"]
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert "secret-detail" not in caplog.text
