"""Runs are unaffected when the agent register is down, slow or failing.

The register is ON with a recording ``httpx.MockTransport`` that fails in one
of five ways: connection refused (``down``), a call that never answers
(``slow``, cut by the client's own deadline), or 500 / 401 / 403 to every
action. Checked here:

* an agent run (an orchestrator chat run with a delegation, a job run, a
  streamed A2A run) gives the same result as the same run on the bare models,
  and is not slower by more than 0.5 s: with a register that hangs for 10 s
  the run is over while the first log call is still waiting for an answer;
* a failed registration push leaves the assignment cache as it was, is not
  retried by itself, and the next trigger pushes again;
* a full queue drops the OLDEST entries and counts them;
* the failure WARNING is rate limited (one per interval, counts and the
  failure's class only); no log record holds the register's URL, a token, an
  exception's text or a body;
* after 5 failures in a row sending pauses, and a dead register costs ONE
  probe per pause;
* shutdown with a dead register returns within the flush bound.

No real HTTP, no real model call, no long sleep: the sender's clock is moved
by hand and the client's deadline is shrunk where a call has to time out.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from types import SimpleNamespace

import pytest

from tests.testdb import use_test_database

use_test_database()

from pydantic_ai import Agent  # noqa: E402

from agents.destination import DestinationServiceConfig  # noqa: E402
from agents.nexus import lifecycle, logged_model, register, sender, state  # noqa: E402
from agents.nexus.logged_model import wrap  # noqa: E402
from tests.nexus_entry_drivers import DRIVERS, Ctx, entry_env  # noqa: E402, F401
from tests.nexus_helpers import (  # noqa: E402
    REGISTER_URL,
    FakeResolver,
    Recorder,
    register_on,
    scripted_model,
)

TECH = "NEXUS_AI_PLATFORM"
TOKEN = "tech-token"                       # FakeResolver's technical token
SECRET = "SECRET-TEXT-0e4c"                # in exception texts, error bodies, prompts
CFG = DestinationServiceConfig(client_id="cid", client_secret="not-a-secret",
                               token_url="https://uaa.example.test/oauth/token",
                               api_url="https://destination.example.test")
ACTIONS = ("logRequest", "logResponse", "registerAgents")
FAULTS = ("down", "http_401", "http_403", "http_500", "slow")
# What the sender's WARNING names as the last failure, per fault.
FAILURE_CLASS = {"down": "ConnectError", "slow": "TimeoutError", "http_401": "http_401",
                 "http_403": "http_403", "http_500": "http_500"}
# One orchestrator chat run with a delegation, one job run, one streamed run.
RUN_DRIVERS = ("webchat_delegation", "job", "a2a_stream")
HANG_TIMEOUT = 10.0                        # the client's deadline against a hanging register
SHORT_TIMEOUT = 0.05                       # where a hanging call must time out quickly
WARNING_LINE = re.compile(r"^agent register: sent=\d+ failed=(\d+) dropped=(\d+) "
                          r"user_fallback=\d+ last=(\S+)$")


def faulty(fault: str) -> Recorder:
    """A register that fails every call the given way."""
    rec = Recorder(answers={a: {"error": SECRET} for a in ACTIONS})
    if fault == "down":
        rec.refuse = True
        rec.refuse_text = f"refused {REGISTER_URL}/logging Bearer {TOKEN} {SECRET}"
    elif fault == "slow":
        rec.hang = True
    else:
        rec.status = int(fault.removeprefix("http_"))
    return rec


def heal(rec: Recorder) -> None:
    rec.refuse, rec.hang, rec.status = False, False, None


def _reset() -> None:
    lifecycle.reset_for_tests()
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


@pytest.fixture
async def clean():
    """Nothing of the register is left behind, whatever a test did."""
    _reset()
    yield
    await sender.stop(flush_seconds=0)
    await register.stop_pusher()
    _reset()


@pytest.fixture
def clock(monkeypatch):
    """The sender's clock, moved by hand."""
    now = [1000.0]
    monkeypatch.setattr(sender, "_now", lambda: now[0])
    return now


def _records(caplog) -> list[logging.LogRecord]:
    return caplog.get_records("setup") + caplog.get_records("call")


def _assert_no_secret_in_logs(caplog) -> None:
    """No record names the register's URL, a token, an exception's text or a body."""
    fmt = logging.Formatter("%(name)s %(message)s")
    for record in _records(caplog):
        text = fmt.format(record)
        for forbidden in ("register.example.test", TOKEN, "Bearer", SECRET):
            assert forbidden not in text, (record.name, forbidden)


def _sender_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in _records(caplog)
            if r.name == "agents.nexus.sender" and r.levelno == logging.WARNING
            and WARNING_LINE.match(r.getMessage())]


def _entry(n: int) -> dict:
    return {"correlationId": f"c-{n:02d}", "useCase": "INVOICE_QA", "prompt": SECRET}


async def _until(check, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.005)


# --- runs ---------------------------------------------------------------------------


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("name", RUN_DRIVERS)
async def test_a_run_is_unaffected_by_a_failing_register(
        monkeypatch, entry_env, clean, caplog, name, fault):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("NEXUS_SEND_PAYLOAD", "true")   # the bodies carry text too
    rec = faulty(fault)
    client = await register_on(monkeypatch, rec,
                               timeout=HANG_TIMEOUT if fault == "slow" else 5)

    started = time.perf_counter()
    without = await DRIVERS[name](Ctx(monkeypatch, lambda model: model))
    bare = time.perf_counter() - started
    started = time.perf_counter()
    wrapped = await DRIVERS[name](Ctx(monkeypatch, lambda model: wrap(model, "gpt-4o")))
    took = time.perf_counter() - started

    # Same result, same model requests, nothing raised into the run.
    assert wrapped.output == without.output
    assert wrapped.agents == without.agents and wrapped.calls > 0
    # Not delayed: enqueue never waits for the register.
    assert took <= bare + 0.5, (took, bare)
    assert rec.of("logRequest"), "the register was never reached"
    if fault == "slow":
        # The run is over while the register still sits on the FIRST call
        # (it would answer only after HANG_TIMEOUT): the run did not wait.
        assert took < HANG_TIMEOUT / 2
        assert sender.counters()["sent"] == sender.counters()["failed"] == 0
    else:
        await _until(lambda: sender.counters()["failed"] >= 1)
        assert sender.counters()["sent"] == 0
    if fault == "http_401":
        await _until(lambda: client._technical.invalidated)
    _assert_no_secret_in_logs(caplog)


async def test_queue_overflow_under_model_calls_drops_the_oldest(
        monkeypatch, real_agents_and_mcp, clean):
    """15 runs (30 log calls) against a register that hangs, queue of 10."""
    rec = faulty("slow")
    await register_on(monkeypatch, rec, queue_max=10, timeout=HANG_TIMEOUT)
    agent = Agent(wrap(scripted_model(), "gpt-4o"))
    for _ in range(15):
        assert (await agent.run("hello")).output == "ok"
    in_flight = 1 if sender._state.in_flight is not None else 0
    assert len(sender.pending()) == 10
    assert sender.counters()["dropped"] == 30 - 10 - in_flight
    # The newest calls are the ones kept: the last log call is the last response.
    assert sender.pending()[-1].action == "logResponse"


# --- registration -------------------------------------------------------------------


def _assigned(use_case: str) -> dict:
    return {"value": [{"name": "invoice-assistant", "useCase": use_case,
                       "resourceGroup": "rg-1", "status": "ASSIGNED"}]}


@pytest.mark.parametrize("fault", FAULTS)
async def test_a_failed_push_keeps_the_cache_and_the_next_trigger_retries(
        monkeypatch, clean, caplog, fault):
    caplog.set_level(logging.DEBUG)

    async def rows():
        return [SimpleNamespace(name="invoice-assistant", description="d",
                                model_name="gpt-4o", enabled=True)]
    monkeypatch.setattr(register, "_load_rows", rows)
    rec = Recorder(answers={"registerAgents": _assigned("INVOICE_QA")})
    client = await register_on(monkeypatch, rec, timeout=SHORT_TIMEOUT)
    register.start_pusher(client)
    register.request_push()
    await register.settle()
    assert register.use_case_for("invoice-assistant") == "INVOICE_QA"
    before = register.assignment_for("invoice-assistant")

    failing = faulty(fault)
    rec.refuse, rec.refuse_text, rec.hang, rec.status = (
        failing.refuse, failing.refuse_text, failing.hang, failing.status)
    rec.answers["registerAgents"] = {"error": SECRET}
    register.request_push()
    await register.settle()
    assert len(rec.of("registerAgents")) == 2
    assert register.assignment_for("invoice-assistant") == before
    assert register.use_case_for("invoice-assistant") == "INVOICE_QA"
    await asyncio.sleep(0.2)                          # not retried by itself
    assert len(rec.of("registerAgents")) == 2

    heal(rec)
    rec.answers["registerAgents"] = _assigned("INVOICE_QB")
    register.request_push()                           # the next trigger
    await register.settle()
    assert len(rec.of("registerAgents")) == 3
    assert rec.of("registerAgents")[2]["body"] == rec.of("registerAgents")[1]["body"]
    assert register.use_case_for("invoice-assistant") == "INVOICE_QB"
    _assert_no_secret_in_logs(caplog)


# --- the sender: overflow, warnings, pause ----------------------------------------------


@pytest.mark.parametrize("fault", ("down", "slow"))
async def test_overflow_drops_the_oldest_and_counts_them(
        monkeypatch, clean, clock, caplog, fault):
    caplog.set_level(logging.DEBUG)
    await register_on(monkeypatch, faulty(fault), queue_max=10, timeout=HANG_TIMEOUT)
    for n in range(30):                                # no await: the worker waits
        sender.enqueue("logRequest", _entry(n))
    assert [e.correlation_id for e in sender.pending()] == [f"c-{n:02d}" for n in range(20, 30)]
    assert sender.counters()["dropped"] == 20
    # Twenty drops, one WARNING: the drops are rate limited as well.
    warnings = _sender_warnings(caplog)
    assert len(warnings) == 1 and WARNING_LINE.match(warnings[0]).group(2) == "1"

    clock[0] += sender.WARN_INTERVAL_SECONDS + 1       # the next window
    sender.enqueue("logRequest", _entry(30))
    warnings = _sender_warnings(caplog)
    assert len(warnings) == 2
    assert WARNING_LINE.match(warnings[1]).group(2) == "20"   # 19 since, plus this one
    assert sender.counters()["dropped"] == 21
    _assert_no_secret_in_logs(caplog)


@pytest.mark.parametrize("fault", FAULTS)
async def test_failures_pause_sending_one_probe_per_pause_one_warning_per_interval(
        monkeypatch, clean, clock, caplog, fault):
    caplog.set_level(logging.DEBUG)
    rec = faulty(fault)
    await register_on(monkeypatch, rec, timeout=SHORT_TIMEOUT)
    for n in range(20):
        sender.enqueue("logRequest", _entry(n))

    await sender.wait_paused()
    assert len(rec.requests) == sender.FAILURES_BEFORE_PAUSE == 5
    assert len(_sender_warnings(caplog)) == 1

    for probe in range(1, 4):                          # a dead register, pause after pause
        clock[0] += sender.PAUSE_SECONDS + 1
        sender.wake()
        await sender.wait_paused()
        assert len(rec.requests) == 5 + probe          # ONE probe per pause
        assert len(_sender_warnings(caplog)) == 1      # still in the first interval

    clock[0] = 1000.0 + sender.WARN_INTERVAL_SECONDS + 1
    sender.wake()
    await sender.wait_paused()
    assert len(rec.requests) == 9
    warnings = _sender_warnings(caplog)
    assert len(warnings) == 2
    first, second = (WARNING_LINE.match(w) for w in warnings)
    assert first.group(1) == "1" and second.group(1) == "8"   # counts since the last line
    assert first.group(3) == second.group(3) == FAILURE_CLASS[fault]
    # Nothing retried, nothing dropped: the rest waits in the queue.
    assert sender.counters() == {"sent": 0, "failed": 9, "dropped": 0, "user_fallback": 0}
    assert len(sender.pending()) == 20 - 9
    _assert_no_secret_in_logs(caplog)


# --- shutdown -------------------------------------------------------------------------------


@pytest.fixture
def lifecycle_env(monkeypatch):
    for name in ("NEXUS_DESTINATION", "NEXUS_FALLBACK_USE_CASE", "NEXUS_SEND_PAYLOAD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_USER_DESTINATION", "")     # no user forwarding
    monkeypatch.setenv("NEXUS_REPUSH_MINUTES", "0")      # no timer
    monkeypatch.setenv("NEXUS_TIMEOUT_SECONDS", str(HANG_TIMEOUT))
    monkeypatch.setattr(lifecycle, "config_from_environment", lambda environ: CFG)
    monkeypatch.setattr(lifecycle, "DestinationResolver", lambda name, cfg: FakeResolver(name))

    async def no_rows():
        return []
    monkeypatch.setattr(register, "_load_rows", no_rows)


@pytest.mark.parametrize("fault", FAULTS)
async def test_shutdown_with_a_dead_register_returns_within_the_flush_bound(
        monkeypatch, clean, lifecycle_env, caplog, fault):
    caplog.set_level(logging.DEBUG)
    rec = faulty(fault)
    monkeypatch.setattr(lifecycle, "_transport", rec.transport())
    assert await lifecycle.start() == f"on (destination {TECH})"
    for n in range(10):
        sender.enqueue("logRequest", _entry(n))
    if fault == "slow":
        await _until(lambda: sender._state.in_flight is not None)
    else:
        await _until(lambda: sender.counters()["failed"] >= 5)

    started = time.perf_counter()
    await lifecycle.stop(flush_seconds=3.0)
    took = time.perf_counter() - started

    assert took < 3.5, took
    if fault != "slow":
        # Paused after 5 failures: nothing is flushed, the rest is dropped at once.
        assert took < 1.0, took
    assert not state.enabled() and not sender.running() and lifecycle.client() is None
    counts = sender.counters()
    assert counts["sent"] == 0 and counts["failed"] + counts["dropped"] == 10
    assert sender.pending() == []
    _assert_no_secret_in_logs(caplog)
