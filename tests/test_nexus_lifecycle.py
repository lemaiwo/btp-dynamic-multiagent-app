"""The lifecycle of the agent register logging: the switch at start-up and at
Reload, the re-push timer and shutdown.

No real HTTP: the destination service is a ``FakeResolver`` per destination
name, the register a recording ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from agents.destination import DestinationError, DestinationNotFound, DestinationServiceConfig
from agents.nexus import lifecycle, logged_model, register, sender, state
from tests.nexus_helpers import FakeResolver, Recorder

TECH = "NEXUS_AI_PLATFORM"
USER = "NEXUS_AI_PLATFORM_USER"
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
def env(monkeypatch):
    """A bound destination service, both destinations present, the register recorded."""
    _reset()
    for name in ("NEXUS_DESTINATION", "NEXUS_FALLBACK_USE_CASE", "NEXUS_REPUSH_MINUTES",
                 "NEXUS_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_USER_DESTINATION", USER)
    fakes = {TECH: FakeResolver(TECH), USER: FakeResolver(USER)}
    built: list[tuple[str, object]] = []

    def resolver(name, cfg):
        built.append((name, cfg))
        return fakes[name]

    recorder = Recorder()
    monkeypatch.setattr(lifecycle, "config_from_environment", lambda environ: CFG)
    monkeypatch.setattr(lifecycle, "DestinationResolver", resolver)
    monkeypatch.setattr(lifecycle, "_transport", recorder.transport())

    async def no_rows():
        return []
    monkeypatch.setattr(register, "_load_rows", no_rows)
    yield type("Env", (), {"fakes": fakes, "recorder": recorder, "built": built})
    _reset()


def _info(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and r.name == "agents.nexus.lifecycle"]


async def test_no_destination_service_is_off_without_timer(env, monkeypatch, caplog):
    monkeypatch.setattr(lifecycle, "config_from_environment", lambda environ: None)
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        assert await lifecycle.start() == "off (no destination service)"
    assert not state.enabled() and lifecycle.timer_running() is False
    assert not sender.running() and lifecycle.client() is None
    assert _info(caplog) == ["agent register integration: off (no destination service)"]
    assert env.built == [] and env.recorder.requests == []
    await lifecycle.stop()


async def test_not_found_is_off_with_timer_that_only_asks_the_destination_service(
        env, monkeypatch, caplog):
    env.fakes[TECH].fail = DestinationNotFound("404")
    monkeypatch.setattr(lifecycle, "_period_seconds", lambda: 0.01)
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        assert await lifecycle.start() == f"off (destination {TECH} not found)"
        assert lifecycle.timer_running() and not sender.running()
        async with asyncio.timeout(2):
            while len(env.fakes[TECH].property_calls) < 4:
                await asyncio.sleep(0.005)
    await lifecycle.stop()
    assert env.recorder.requests == []           # never the register
    assert env.fakes[USER].property_calls == []  # off: the user destination is not asked
    assert all(env.fakes[TECH].property_calls)   # always forced: a fresh answer
    assert _info(caplog) == [f"agent register integration: off (destination {TECH} not found)"]


async def test_found_is_on_and_pushes(env, caplog):
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        assert await lifecycle.start() == f"on (destination {TECH})"
    assert state.enabled() and sender.running() and lifecycle.timer_running()
    await register.settle()
    async with asyncio.timeout(2):
        while not env.recorder.of("registerAgents"):
            await asyncio.sleep(0.005)
    assert len(env.recorder.of("registerAgents")) == 1
    assert _info(caplog) == [f"agent register integration: on (destination {TECH})",
                             f"agent register integration: user forwarding on (destination {USER})"]
    assert "tech-token" not in caplog.text
    await lifecycle.stop()


async def test_unresolvable_is_on(env):
    env.fakes[TECH].fail = DestinationError("down")
    assert await lifecycle.start() == f"on (destination {TECH})"
    assert state.enabled() and sender.running()
    await lifecycle.stop()


async def test_a_hanging_destination_service_is_bounded_by_the_timeout(env, monkeypatch):
    monkeypatch.setenv("NEXUS_TIMEOUT_SECONDS", "0.2")
    env.fakes[TECH].hang = True
    env.fakes[USER].hang = True
    began = time.monotonic()
    assert await lifecycle.start() == f"on (destination {TECH})"
    assert time.monotonic() - began < 1.0      # one deadline for both destinations
    await lifecycle.stop()


async def test_user_resolver_is_passed_and_forwarding_set(env):
    await lifecycle.start()
    assert lifecycle.client()._user is env.fakes[USER]
    assert lifecycle.client()._technical is env.fakes[TECH]
    assert state.user_forwarding() is True
    await lifecycle.stop()


async def test_missing_user_destination_logs_and_uses_technical(env, caplog):
    env.fakes[USER].fail = DestinationNotFound("404")
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        assert await lifecycle.start() == f"on (destination {TECH})"
    assert lifecycle.client()._user is None and state.user_forwarding() is False
    assert (f"agent register integration: user destination {USER} not found; calls are "
            "logged with the technical token") in _info(caplog)
    await lifecycle.stop()


async def test_no_user_destination_setting_builds_no_user_resolver(env, monkeypatch, caplog):
    monkeypatch.setenv("NEXUS_USER_DESTINATION", "")
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        await lifecycle.start()
    assert [n for n, _ in env.built] == [TECH]
    assert lifecycle.client()._user is None and state.user_forwarding() is False
    assert any("user forwarding off" in m for m in _info(caplog))
    await lifecycle.stop()


async def test_refresh_turns_on_then_off_and_back(env, caplog):
    with caplog.at_level(logging.INFO, logger="agents.nexus"):
        await lifecycle.start()
        first = lifecycle.client()
        assert await lifecycle.refresh_switch() == f"on (destination {TECH})"  # no change
        env.fakes[TECH].fail = DestinationNotFound("404")
        assert await lifecycle.refresh_switch() == f"off (destination {TECH} not found)"
        assert not state.enabled() and not sender.running()
        assert state.user_forwarding() is False and lifecycle.client() is None
        assert first._http.is_closed
        assert lifecycle.timer_running()          # the check goes on
        env.recorder.requests.clear()
        env.fakes[TECH].fail = None
        assert await lifecycle.refresh_switch() == f"on (destination {TECH})"
        assert sender.running() and state.user_forwarding() is True
        async with asyncio.timeout(2):
            while not env.recorder.of("registerAgents"):
                await asyncio.sleep(0.005)
    assert [m.split(": ", 1)[1] for m in _info(caplog)] == [
        f"on (destination {TECH})", f"user forwarding on (destination {USER})",
        f"off (destination {TECH} not found)",
        f"on (destination {TECH})", f"user forwarding on (destination {USER})",
    ]
    await lifecycle.stop()


async def test_reload_reconfigures_forwarding_and_fallback(env, monkeypatch):
    await lifecycle.start()
    first = lifecycle.client()
    assert register.use_case_for("invoice-assistant") is None
    monkeypatch.setenv("NEXUS_FALLBACK_USE_CASE", "INVOICE_QA")
    env.fakes[USER].fail = DestinationNotFound("404")
    assert await lifecycle.reevaluate() == f"on (destination {TECH})"
    assert register.use_case_for("invoice-assistant") == "INVOICE_QA"  # configure() again
    assert state.user_forwarding() is False and lifecycle.client()._user is None
    assert sender._state.client is lifecycle.client() is not first
    await lifecycle.stop()
    assert first._http.is_closed and lifecycle.client() is None  # a replaced client is closed too


async def test_timer_repushes(env, monkeypatch):
    monkeypatch.setattr(lifecycle, "_period_seconds", lambda: 0.02)
    await lifecycle.start()
    async with asyncio.timeout(2):
        while len(env.recorder.of("registerAgents")) < 3:
            await asyncio.sleep(0.005)
    await lifecycle.stop()
    assert not lifecycle.timer_running()


async def test_no_timer_when_repush_minutes_is_zero(env, monkeypatch):
    monkeypatch.setenv("NEXUS_REPUSH_MINUTES", "0")
    await lifecycle.start()
    assert state.enabled() and not lifecycle.timer_running()
    await lifecycle.stop()


async def test_stop_flushes_within_limit(env):
    await lifecycle.start()
    await register.settle()
    sender.enqueue("logRequest", {"correlationId": "c-1"})
    await lifecycle.stop(flush_seconds=1.0)
    assert len(env.recorder.of("logRequest")) == 1   # flushed before the close
    assert not state.enabled() and not sender.running() and not lifecycle.timer_running()


async def test_stop_against_a_hanging_register_is_bounded(env):
    await lifecycle.start()
    await register.settle()
    env.recorder.hang = True
    client = lifecycle.client()
    sender.enqueue("logRequest", {"correlationId": "c-1"})
    began = time.monotonic()
    await lifecycle.stop(flush_seconds=0.2)
    assert time.monotonic() - began < 1.5
    assert client._http.is_closed and sender.counters()["dropped"] >= 1


async def test_evaluation_never_raises(env, monkeypatch, caplog):
    def broken(environ):
        raise ValueError("binding text")
    monkeypatch.setattr(lifecycle, "config_from_environment", broken)
    assert await lifecycle.start() == "off (check failed)"
    assert not state.enabled() and "binding text" not in caplog.text
    await lifecycle.stop()


async def test_stop_never_raises(env, monkeypatch):
    await lifecycle.start()

    async def bad_close():
        raise RuntimeError("close")
    monkeypatch.setattr(lifecycle.client(), "aclose", bad_close)

    async def bad_stop():
        raise RuntimeError("pusher")
    monkeypatch.setattr(register, "stop_pusher", bad_stop)
    await lifecycle.stop()
    assert not state.enabled()


async def test_stop_without_start_is_harmless():
    await lifecycle.stop()
    assert not state.enabled()
