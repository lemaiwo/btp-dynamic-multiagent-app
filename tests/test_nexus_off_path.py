"""With the agent register off, nothing leaves and every result is unchanged.

The register is switched off the two ways the lifecycle knows: no destination
service bound, and a destination service that says the destination does not
exist. Any contact with the register fails the test twice over: the
lifecycle's transport fails the test on any request, and the register client
module cannot build an HTTP client at all. Then every entry point of
``tests/nexus_entry_drivers.py`` runs twice: once on the bare scripted models
(a run "without the package") and once wrapped as the app wraps them. The
results must be equal, nothing may be queued, and the only log line of
``agents.nexus`` is the one start-up INFO line.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from tests.testdb import use_test_database

use_test_database()

from agents.destination import DestinationNotFound, DestinationServiceConfig  # noqa: E402
from agents.model_call_headers import current_call_headers  # noqa: E402
from agents.nexus import client as nexus_client  # noqa: E402
from agents.nexus import lifecycle, logged_model, register, sender, state  # noqa: E402
from agents.nexus.logged_model import wrap  # noqa: E402
from tests.nexus_entry_drivers import DRIVERS, Ctx, entry_env  # noqa: E402, F401
from tests.nexus_helpers import FakeResolver  # noqa: E402

TECH = "NEXUS_AI_PLATFORM"
CFG = DestinationServiceConfig(client_id="cid", client_secret="not-a-secret",
                               token_url="https://uaa.example.test/oauth/token",
                               api_url="https://destination.example.test")
STARTUP = {
    "no_service": "agent register integration: off (no destination service)",
    "not_found": f"agent register integration: off (destination {TECH} not found)",
}


def _reset() -> None:
    lifecycle.reset_for_tests()
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


def _no_client(*args, **kwargs):
    pytest.fail("the register client built an HTTP client")


@pytest.fixture(params=sorted(STARTUP))
async def off(request, monkeypatch, entry_env, caplog):  # noqa: F811
    """The register off (two ways), every way to it closed; the start-up log line."""
    _reset()
    monkeypatch.setattr(lifecycle, "_transport", httpx.MockTransport(
        lambda r: pytest.fail(f"register contacted: {r.url.path}")))
    # Not the global httpx: the tests' own ASGI clients must keep working.
    monkeypatch.setattr(nexus_client, "httpx", SimpleNamespace(
        **{**vars(httpx), "AsyncClient": _no_client}))
    if request.param == "no_service":
        monkeypatch.setattr(lifecycle, "config_from_environment", lambda environ: None)
    else:
        monkeypatch.setattr(lifecycle, "config_from_environment", lambda environ: CFG)
        monkeypatch.setattr(lifecycle, "DestinationResolver", lambda name, cfg: FakeResolver(
            name, fail=DestinationNotFound("404")))
    caplog.set_level(logging.DEBUG, logger="agents.nexus")
    assert await lifecycle.start() == STARTUP[request.param].split(": ", 1)[1]
    yield STARTUP[request.param]
    await lifecycle.stop(flush_seconds=0.1)
    _reset()


@pytest.mark.parametrize("name", sorted(DRIVERS))
async def test_off_sends_nothing_and_changes_nothing(monkeypatch, off, caplog, name):
    without = await DRIVERS[name](Ctx(monkeypatch, lambda model: model))
    # Off: no correlation id is made and no call headers are published.
    monkeypatch.setattr(logged_model, "uuid", SimpleNamespace(
        uuid4=lambda: pytest.fail("a correlation id was made while off")))
    published = []
    real_sending = logged_model.sending

    def recording(headers):
        published.append(headers)
        return real_sending(headers)
    monkeypatch.setattr(logged_model, "sending", recording)
    wrapped = await DRIVERS[name](Ctx(monkeypatch, lambda model: wrap(model, "gpt-4o")))
    assert published == [] and current_call_headers.get() is None
    assert wrapped.output == without.output
    assert wrapped.agents == without.agents and wrapped.calls > 0
    assert not state.enabled() and lifecycle.client() is None and not sender.running()
    assert sender.pending() == []
    records = caplog.get_records("setup") + caplog.get_records("call")
    lines = [(r.name, r.levelno, r.getMessage()) for r in records
             if r.name.startswith("agents.nexus")]
    assert lines == [("agents.nexus.lifecycle", logging.INFO, off)]
