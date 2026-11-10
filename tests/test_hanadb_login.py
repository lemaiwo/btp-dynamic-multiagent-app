"""The login protection of ``builtin:hana`` on a fake hdbcli.

Every tool call is one database login, the database user may be a person's,
and HANA locks a user after a few invalid connect attempts. These tests count
the connects the fake database sees: per destination only ONE login is in
flight until one succeeded, the breaker is shared by every system object of
the process that names the destination, and a credential that failed is not
tried again until it changes or the registry reloads.

The interleavings all run on ONE event loop: (a) parallel callers, nothing
confirmed; (b) the lock holder fails with a non-login error; (c) the lock
holder passes its deadline while its thread still connects; (d) the lock
holder is cancelled; (e) a confirmed credential goes stale.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading

import pytest

from agents.hana import connection
from agents.hana.connection import (
    LOGIN_BREAKER_SECONDS,
    MAX_CONCURRENT,
    HanaSystem,
    reset_login_state,
    run_query,
)
from agents.hana.errors import HanaError
from tests import hanadb_helpers as h

SQL = "SELECT 1 FROM SYS.DUMMY"
OTHER_PASSWORD = "pw-other-2b8e41"


def _system(name: str = "erp_tenant", destination: str = "hana-test",
            resolver: h.FakeResolver | None = None) -> HanaSystem:
    return HanaSystem(name, destination, resolver=resolver or h.FakeResolver(),
                      tokens=h.FakeTokens())


async def _until(predicate, tries: int = 300) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _codes(results: list) -> list:
    return [r.code if isinstance(r, HanaError) else ("ok" if isinstance(r, tuple) else r)
            for r in results]


def _no_secret_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    digest = hashlib.sha256(f"{h.USER}\0{h.PASSWORD}".encode()).hexdigest()
    for record in caplog.records:
        text = record.getMessage()
        for secret in (h.MARKER, h.HOST, h.USER, h.PASSWORD, h.TOKEN, digest, digest[:16]):
            assert secret not in text
        assert record.exc_info is None


# ---- (a) parallel callers, nothing confirmed --------------------------------------------


async def test_six_parallel_calls_with_a_wrong_password_make_one_login(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate, refuse_passwords={h.PASSWORD}))
    system = _system()
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(6)]
    await _until(lambda: db.inside == 1)
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 1  # one login in flight, the others wait
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 6
    assert len(db.connect_kwargs) == 1
    _no_secret_in_logs(caplog)


async def test_parallel_calls_wait_for_the_first_login_then_run_in_parallel(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    system = _system()
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(6)]
    await _until(lambda: db.inside == 1)
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 1
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["ok"] * 6
    assert len(db.connect_kwargs) == 6
    # confirmed: the next calls no longer wait for one another
    gate.clear()
    more = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(MAX_CONCURRENT)]
    await _until(lambda: db.inside == MAX_CONCURRENT)
    gate.set()
    await asyncio.gather(*more)


async def test_systems_of_two_agents_on_one_destination_share_one_login(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate, refuse_passwords={h.PASSWORD}))
    first, second = _system("erp_tenant"), _system("erp_tenant")  # two agents' entries
    third = _system("erp_other_name")  # another system name, same destination
    tasks = [asyncio.create_task(run_query(s, SQL, [], 1))
             for s in (first, second, third, first, second, third)]
    await _until(lambda: db.inside == 1)
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 6
    assert len(db.connect_kwargs) == 1
    with pytest.raises(HanaError) as info:
        await run_query(_system("erp_tenant"), SQL, [], 1)  # a new object: same breaker
    assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 1


async def test_a_login_on_one_destination_confirms_nothing_on_another(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={OTHER_PASSWORD}))
    good = _system("erp_a", "hana-a")
    bad = _system("erp_b", "hana-b", resolver=h.FakeResolver(h.http_props(
        Password=OTHER_PASSWORD)))
    await run_query(good, SQL, [], 1)
    db.gate = gate
    tasks = [asyncio.create_task(run_query(bad, SQL, [], 1)) for _ in range(3)]
    await _until(lambda: db.inside == 1)
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 2
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 3
    assert len(db.connect_kwargs) == 2
    await run_query(good, SQL, [], 1)  # the breaker of hana-b leaves hana-a alone
    assert len(db.connect_kwargs) == 3


# ---- (b) the lock holder fails with a non-login error -------------------------------------


@pytest.mark.parametrize("failure,code", [
    (h.driver_error(-10709, "Connection failed (proxy handshake)"), "unreachable"),
    (ConnectionRefusedError(111, "refused"), "proxy_refused"),
    (TimeoutError("connect"), "timeout"),
])
async def test_a_non_login_failure_neither_opens_the_breaker_nor_keeps_the_lock(
        monkeypatch, failure, code):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate, fail_at={"connect": failure}))
    system = _system()
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(3)]
    await _until(lambda: db.inside == 1)
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 1  # still one at a time: nothing confirmed
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == [code] * 3
    assert len(db.connect_kwargs) == 3  # each waiter made its own attempt
    assert db.peak == 1
    del db.fail_at["connect"]
    await run_query(system, SQL, [], 1)  # no breaker
    assert len(db.connect_kwargs) == 4


async def test_a_failure_after_the_login_confirms_the_login(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(fail_at={"execute": h.driver_error(339, "x")}))
    system = _system()
    with pytest.raises(HanaError) as info:
        await run_query(system, SQL, [], 1)
    assert info.value.code == "sql_error"
    db.gate = gate
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(3)]
    await _until(lambda: db.inside == 3)  # confirmed: parallel
    gate.set()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- (c) the lock holder passes its deadline -----------------------------------------------


@pytest.mark.parametrize("outcome", ["ok", "login_failed"])
async def test_the_lock_is_held_until_the_thread_of_a_timed_out_holder_ends(
        monkeypatch, caplog, outcome):
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    if outcome == "login_failed":
        db.refuse_passwords.add(h.PASSWORD)
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 0.2)
    system = _system()
    holder = asyncio.create_task(run_query(system, SQL, [], 1))
    await _until(lambda: db.inside == 1)
    waiter = asyncio.create_task(run_query(system, SQL, [], 1))
    results = await asyncio.gather(holder, waiter, return_exceptions=True)
    assert _codes(results) == ["timeout", "timeout"]  # the waiter by its own deadline
    assert len(db.connect_kwargs) == 1
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 5)
    late = asyncio.create_task(run_query(system, SQL, [], 1))
    await asyncio.sleep(0.1)
    assert len(db.connect_kwargs) == 1  # the login of the timed-out holder is in flight
    assert not late.done()
    gate.set()
    if outcome == "ok":
        await late
        assert len(db.connect_kwargs) == 2
    else:
        # the thread failed the login after its caller left: the breaker opened
        with pytest.raises(HanaError) as info:
            await late
        assert info.value.code == "login_failed"
        assert len(db.connect_kwargs) == 1
    slot = connection._semaphore()
    assert slot._value == MAX_CONCURRENT
    _no_secret_in_logs(caplog)


# ---- (d) the lock holder or a waiter is cancelled ------------------------------------------


async def test_a_cancelled_holder_keeps_the_lock_until_its_thread_ends(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    system = _system()
    holder = asyncio.create_task(run_query(system, SQL, [], 1))
    await _until(lambda: db.inside == 1)
    waiter = asyncio.create_task(run_query(system, SQL, [], 1))
    await asyncio.sleep(0.05)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 1 and not waiter.done()
    gate.set()
    await waiter
    assert len(db.connect_kwargs) == 2


async def test_a_waiter_cancelled_at_the_lock_leaves_lock_and_slots_intact(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    system = _system()
    holder = asyncio.create_task(run_query(system, SQL, [], 1))
    await _until(lambda: db.inside == 1)
    waiters = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(3)]
    await asyncio.sleep(0.05)
    waiters[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiters[1]
    gate.set()
    await holder
    results = await asyncio.gather(waiters[0], waiters[2], return_exceptions=True)
    assert _codes(results) == ["ok", "ok"]
    assert len(db.connect_kwargs) == 3
    assert connection._semaphore()._value == MAX_CONCURRENT
    assert not connection._login_lock("hana-test").locked()


# ---- (e) a confirmed credential goes stale -------------------------------------------------


async def test_a_stale_credential_fails_at_most_once_per_slot_then_the_gate_closes(
        monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch)
    system = _system()
    await run_query(system, SQL, [], 1)  # confirmed
    db.refuse_passwords.add(h.PASSWORD)  # changed in the database, not in the destination
    db.gate = gate
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(10)]
    await _until(lambda: db.inside == MAX_CONCURRENT)
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 10
    # the calls that had passed the gate (one per slot) each failed once; the
    # queued ones saw the breaker when they got a slot
    assert len(db.connect_kwargs) == 1 + MAX_CONCURRENT


# ---- the breaker window and the credential digest ------------------------------------------


async def test_after_the_window_the_same_credential_is_not_tried(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    now[0] += LOGIN_BREAKER_SECONDS + 1
    for _ in range(3):
        with pytest.raises(HanaError) as info:
            await run_query(system, SQL, [], 1)
        assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 1
    assert resolver.forced == [False, True]  # read fresh once, then the window again
    now[0] += LOGIN_BREAKER_SECONDS + 1
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 1
    assert resolver.forced == [False, True, True]
    _no_secret_in_logs(caplog)


async def test_a_changed_credential_is_tried_once_and_then_runs_in_parallel(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    resolver.props = h.http_props(Password=OTHER_PASSWORD)  # corrected in the destination
    with pytest.raises(HanaError):  # the window is still open: nothing read
        await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 1
    now[0] += LOGIN_BREAKER_SECONDS + 1
    await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 2
    assert db.connect_kwargs[-1]["password"] == OTHER_PASSWORD
    gate = threading.Event()
    db.gate = gate
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(MAX_CONCURRENT)]
    await _until(lambda: db.inside == MAX_CONCURRENT)
    gate.set()
    await asyncio.gather(*tasks)


async def test_a_changed_but_wrong_credential_is_tried_once(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD, OTHER_PASSWORD}))
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    resolver.props = h.http_props(Password=OTHER_PASSWORD)
    now[0] += LOGIN_BREAKER_SECONDS + 1
    gate = threading.Event()
    db.gate = gate
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(5)]
    await _until(lambda: db.inside == 1)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 5
    assert len(db.connect_kwargs) == 2


async def test_reset_login_state_clears_breaker_and_confirmation(monkeypatch):
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    system = _system()
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    db.refuse_passwords.clear()
    reset_login_state()
    await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 2
    assert connection._logins["hana-test"].confirmed is not None
    reset_login_state()
    assert connection._logins == {}


def test_the_state_holds_a_digest_never_the_credential(monkeypatch):
    h.fresh_login_state(monkeypatch)
    digest = connection.credential_digest(h.USER, h.PASSWORD)
    assert digest == hashlib.sha256(f"{h.USER}\0{h.PASSWORD}".encode()).hexdigest()
    state = connection._login("hana-test")
    state.failed = digest
    text = repr(state) + str(state)
    assert h.PASSWORD not in text and h.USER not in text and digest not in text


async def test_the_registry_reload_resets_the_login_state(monkeypatch):
    from agents import registry

    h.fresh_login_state(monkeypatch)
    connection._login("hana-test").failed = connection.credential_digest(h.USER, h.PASSWORD)

    class _Build:
        specialists: dict = {}
        configs: list = []

    async def build():
        return _Build()

    monkeypatch.setattr(registry, "build_orchestrator", build)
    reg = registry.Registry()

    async def nothing_to_close() -> None:
        return None

    monkeypatch.setattr(reg, "_close_idle_retired", nothing_to_close)
    await reg.reload()
    assert connection._logins == {}
