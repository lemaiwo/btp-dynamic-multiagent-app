"""Every entry point logs one logRequest / logResponse pair per model request.

The register is on with a recording transport (``register_on`` + ``Recorder``);
each entry point of ``tests/nexus_entry_drivers.py`` runs once on scripted
models wrapped as the app wraps them. What is checked is what the register
RECEIVES, in the request bodies: as many ``logRequest`` as ``logResponse`` as
model requests; each pair shares one ``correlationId``, ``agent`` and
``runId``; the agent names are the ones the run's agents should be logged
under (a delegated specialist and a deep sub-agent included); one run id for
the whole run, the one the entry point names when it names one; and a
personal agent's own name in no request body at all.
"""
from __future__ import annotations

from collections import Counter

import pytest

from tests.testdb import use_test_database

use_test_database()

from agents.nexus import logged_model, register, sender, state  # noqa: E402
from agents.nexus.context import PERSONAL_AGENT  # noqa: E402
from agents.nexus.logged_model import wrap  # noqa: E402
from tests.nexus_entry_drivers import (  # noqa: E402, F401
    DRIVERS,
    PERSONAL_NAME,
    Ctx,
    entry_env,
)
from tests.nexus_helpers import Recorder, register_on  # noqa: E402


@pytest.fixture
async def rec(monkeypatch, entry_env):  # noqa: F811
    r = Recorder()
    await register_on(monkeypatch, r)
    yield r
    await sender.stop(flush_seconds=0.1)
    state.reset_for_tests()
    sender.reset_for_tests()
    register.reset_for_tests()
    logged_model.reset_for_tests()


def _wrapped(model):
    return wrap(model, "gpt-4o")


async def _run(monkeypatch, rec, name: str, *, payload: bool = False):
    if payload:
        state.set_send_payload(True)      # as the lifecycle sets it from NEXUS_SEND_PAYLOAD
    outcome = await DRIVERS[name](Ctx(monkeypatch, _wrapped))
    await sender.drain()
    return outcome


def _check_pairs(rec, outcome) -> list[dict]:
    """Every pair complete and consistent; the bodies of the logRequests."""
    requests = [r["body"] for r in rec.of("logRequest")]
    responses = [r["body"] for r in rec.of("logResponse")]
    assert outcome.calls > 0
    assert len(requests) == len(responses) == outcome.calls
    by_id = {b["correlationId"]: b for b in requests}
    assert len(by_id) == len(requests)                      # one id per model request
    for answer in responses:
        asked = by_id.pop(answer["correlationId"])          # its logRequest exists
        assert answer.get("agent") == asked.get("agent")
        assert answer.get("runId") == asked.get("runId")
    assert by_id == {}
    return requests


@pytest.mark.parametrize("name", sorted(DRIVERS))
async def test_one_pair_per_model_request_with_run_id_and_agent(monkeypatch, rec, name):
    outcome = await _run(monkeypatch, rec, name)
    requests = _check_pairs(rec, outcome)
    assert Counter(b["agent"] for b in requests) == outcome.agents
    run_ids = {b.get("runId") for b in requests}
    assert len(run_ids) == 1 and None not in run_ids and "" not in run_ids
    if outcome.run_id is not None:
        assert run_ids == {outcome.run_id[:64]}
    assert sender.pending() == []


@pytest.mark.parametrize("payload", [False, True], ids=["payload-off", "payload-on"])
async def test_a_personal_agents_name_is_in_no_request_body(monkeypatch, rec, payload):
    outcome = await _run(monkeypatch, rec, "webchat_personal_agent", payload=payload)
    _check_pairs(rec, outcome)
    assert {r["body"]["agent"] for r in rec.of("logRequest")} == {PERSONAL_AGENT}
    for r in rec.requests:
        text = str(r["body"])
        assert PERSONAL_NAME not in text and PERSONAL_NAME.casefold() not in text.casefold()


@pytest.mark.parametrize("name", ["ide_diagnose", "ide_report"])
async def test_ide_diagnose_and_report_turns_are_logged_as_the_diagnose_agent(
        monkeypatch, rec, name):
    from agents.ide import runner
    outcome = await _run(monkeypatch, rec, name)
    requests = _check_pairs(rec, outcome)
    diagnose_agent = runner.diagnose_agent_name()
    assert diagnose_agent != runner._agent_name("change")
    assert {b["agent"] for b in requests} == {diagnose_agent}
    assert {b["runId"] for b in requests} == {outcome.run_id}
    assert [e for e in outcome.output["errors"] if e.get("code") != "no_diagnose_server"] == []
