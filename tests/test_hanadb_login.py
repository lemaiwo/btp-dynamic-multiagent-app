"""The login protection of ``builtin:hana`` on a fake hdbcli.

Every tool call is one database login, the database user may be a person's,
and HANA locks a user after a few invalid connect attempts. These tests count
the connects the fake database sees: per database account only ONE login is
in flight until one succeeded, the breaker is shared by every system object
and destination of the process that reach the account, and a credential that
failed is not tried again until it changes or an admin presses Reload (other
registry reloads keep the failure).

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
OTHER_USER = "DBUSER_OTHER_2B8E41"


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


def _state(destination: str = "hana-test") -> connection._Login:
    return connection._logins[connection._accounts[destination]]


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


async def test_a_login_on_one_account_confirms_nothing_on_another(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={OTHER_PASSWORD}))
    good = _system("erp_a", "hana-a")
    bad = _system("erp_b", "hana-b", resolver=h.FakeResolver(h.http_props(
        User=OTHER_USER, Password=OTHER_PASSWORD)))
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
    assert not connection._login_lock(connection._accounts["hana-test"]).locked()


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
    # read fresh once per window, without opening the window again
    assert resolver.forced == [False, True, False, False]
    now[0] += LOGIN_BREAKER_SECONDS + 1
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 1
    assert resolver.forced == [False, True, False, False, True]
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
    assert _state("hana-test").confirmed is not None
    reset_login_state()
    assert connection._logins == {} and connection._accounts == {}


def test_the_state_holds_a_digest_never_the_credential(monkeypatch):
    h.fresh_login_state(monkeypatch)
    digest = connection.credential_digest(h.USER, h.PASSWORD)
    assert digest == hashlib.sha256(f"{h.USER}\0{h.PASSWORD}".encode()).hexdigest()
    state = connection._login("hana-test")
    state.failed.append(digest)
    text = repr(state) + str(state)
    assert h.PASSWORD not in text and h.USER not in text and digest not in text


async def test_the_registry_reload_is_a_soft_reset(monkeypatch):
    from agents import registry

    h.fresh_login_state(monkeypatch)
    state = connection._login("acct")
    digest = connection.credential_digest(h.USER, h.PASSWORD)
    state.failed.append(digest)
    state.confirmed = digest
    state.blocked_until = connection._clock() + LOGIN_BREAKER_SECONDS

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
    # the failure is kept, the window and the confirmation are gone, and the
    # next call reads the destination fresh
    assert connection._logins == {"acct": state}
    assert state.failed == [digest] and state.confirmed is None
    assert state.blocked_until is None and state.epoch == 1


# ---- soft reset (every registry reload) and full reset (Reload / Restart) ----------------


async def _fail_once(monkeypatch, system: HanaSystem | None = None) -> h.FakeDbapi:
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    with pytest.raises(HanaError):
        await run_query(system or _system(), SQL, [], 1)
    assert len(db.connect_kwargs) == 1
    return db


async def test_after_a_soft_reset_the_same_credential_costs_no_login(monkeypatch):
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    db = await _fail_once(monkeypatch, system)
    for _ in range(3):  # unrelated admin saves, each reloading the registry
        connection.soft_reset_login_state()
        with pytest.raises(HanaError) as info:
            await run_query(system, SQL, [], 1)
        assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 1
    assert resolver.forced[-1] is True  # read fresh after the reset


async def test_after_a_soft_reset_a_changed_credential_is_tried_at_once(monkeypatch):
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    db = await _fail_once(monkeypatch, system)
    resolver.props = h.http_props(Password=OTHER_PASSWORD)  # corrected in the destination
    connection.soft_reset_login_state()
    await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 2
    assert db.connect_kwargs[-1]["password"] == OTHER_PASSWORD


async def test_after_a_full_reset_the_same_credential_is_tried_once(monkeypatch):
    system = _system()
    db = await _fail_once(monkeypatch, system)
    db.refuse_passwords.clear()  # unlocked in the database, password unchanged
    reset_login_state()
    await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 2


async def test_a_soft_reset_during_a_login_leaves_lock_and_slots_intact(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate, refuse_passwords={h.PASSWORD}))
    system = _system()
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(4)]
    await _until(lambda: db.inside == 1)
    lock = connection._login_lock(connection._accounts["hana-test"])
    assert lock.locked()
    connection.soft_reset_login_state()
    await asyncio.sleep(0.05)
    assert lock.locked() and len(db.connect_kwargs) == 1  # no second login beside it
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 4
    assert len(db.connect_kwargs) == 1
    assert not lock.locked()
    assert connection._semaphore()._value == MAX_CONCURRENT


# ---- the state belongs to the database account, not to the destination name -----------


async def test_two_destinations_of_one_account_share_one_login(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate, refuse_passwords={h.PASSWORD}))
    first = _system("erp_tenant", "hana-a")
    second = _system("erp_tenant_too", "hana-b")  # same host, port and user
    tasks = [asyncio.create_task(run_query(s, SQL, [], 1)) for s in (first, second) * 3]
    await _until(lambda: db.inside == 1)
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 1
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 6
    assert len(db.connect_kwargs) == 1
    assert connection._accounts["hana-a"] == connection._accounts["hana-b"]
    assert len(connection._logins) == 1


async def test_one_host_and_port_with_two_users_are_two_accounts(monkeypatch):
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    bad = _system("erp_a", "hana-a")
    good = _system("erp_b", "hana-b", resolver=h.FakeResolver(h.http_props(
        User=OTHER_USER, Password=OTHER_PASSWORD)))
    with pytest.raises(HanaError):
        await run_query(bad, SQL, [], 1)
    await run_query(good, SQL, [], 1)
    assert len(db.connect_kwargs) == 2
    assert connection._accounts["hana-a"] != connection._accounts["hana-b"]


async def test_a_destination_whose_user_changes_moves_to_that_accounts_state(monkeypatch):
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    first_account = connection._accounts["hana-test"]
    resolver.props = h.http_props(User=OTHER_USER, Password=OTHER_PASSWORD)
    connection.soft_reset_login_state()  # the destination edit came with a reload
    await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 2
    assert connection._accounts["hana-test"] != first_account
    assert _state().confirmed is not None
    assert connection._logins[first_account].failed  # the old account keeps it


def test_the_account_key_is_a_digest_of_location_host_port_and_user():
    target = connection.settings_from_properties(h.http_props(), "erp_tenant")
    key = connection.account_key(target)
    assert len(key) == 64 and h.HOST not in key and h.USER not in key
    moved = connection.settings_from_properties(h.http_props(
        CloudConnectorLocationId="loc1"), "erp_tenant")
    assert connection.account_key(moved) != key
    other_password = connection.settings_from_properties(h.http_props(
        Password=OTHER_PASSWORD), "erp_tenant")
    assert connection.account_key(other_password) == key


# ---- the gate is looked at again right before the thread starts --------------------------


async def test_a_failure_recorded_before_the_thread_starts_stops_it(monkeypatch):
    db = h.install(monkeypatch)
    system = _system()
    await run_query(system, SQL, [], 1)  # confirmed
    real_spawn = connection._spawn

    def spawn_after_a_failure(coro):
        # A _settle of another thread lands between the slot check and the
        # first step of this call's task.
        connection._note_login_failed(connection._accounts["hana-test"],
                                      connection.credential_digest(h.USER, h.PASSWORD),
                                      "erp_tenant")
        return real_spawn(coro)

    monkeypatch.setattr(connection, "_spawn", spawn_after_a_failure)
    with pytest.raises(HanaError) as info:
        await run_query(system, SQL, [], 1)
    assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 1  # no connect
    await asyncio.sleep(0)
    assert connection._semaphore()._value == MAX_CONCURRENT
    assert not connection._login_lock(connection._accounts["hana-test"]).locked()


# ---- an account remembers every failed credential, not only the last -------------------


def _two_destinations(password_a: str, password_b: str):
    first = _system("erp_a", "hana-a", resolver=h.FakeResolver(h.http_props(
        Password=password_a)))
    second = _system("erp_b", "hana-b", resolver=h.FakeResolver(h.http_props(
        Password=password_b)))
    return first, second


async def test_two_wrong_passwords_of_one_account_cost_one_login_each(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD, OTHER_PASSWORD}))
    first, second = _two_destinations(h.PASSWORD, OTHER_PASSWORD)
    with pytest.raises(HanaError):
        await run_query(first, SQL, [], 1)
    now[0] += LOGIN_BREAKER_SECONDS + 1
    with pytest.raises(HanaError):
        await run_query(second, SQL, [], 1)
    assert len(db.connect_kwargs) == 2
    for _ in range(3):
        now[0] += LOGIN_BREAKER_SECONDS + 1
        connection.soft_reset_login_state()
        for system in (first, second, first, second):
            with pytest.raises(HanaError) as info:
                await run_query(system, SQL, [], 1)
            assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 2


async def test_a_late_failure_of_an_old_credential_keeps_the_newer_one_out(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch)
    first, second = _two_destinations(h.PASSWORD, OTHER_PASSWORD)
    await run_query(first, SQL, [], 1)  # A confirmed
    db.refuse_passwords.update({h.PASSWORD, OTHER_PASSWORD})
    db.gate = gate
    old = asyncio.create_task(run_query(first, SQL, [], 1))  # confirmed: no lock
    new = asyncio.create_task(run_query(second, SQL, [], 1))  # B: the lock
    await _until(lambda: db.inside == 2)
    gate.set()
    results = await asyncio.gather(old, new, return_exceptions=True)
    assert _codes(results) == ["login_failed"] * 2
    assert len(db.connect_kwargs) == 3
    for _ in range(3):
        connection.soft_reset_login_state()
        for system in (first, second):
            with pytest.raises(HanaError):
                await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 3


def test_the_failed_credentials_are_the_last_eight(monkeypatch):
    h.fresh_login_state(monkeypatch)
    digests = [connection.credential_digest(h.USER, f"pw-{n}") for n in range(9)]
    for digest in digests:
        connection._note_login_failed("acct", digest, "erp_tenant")
    state = connection._logins["acct"]
    assert connection.FAILED_CREDENTIALS_KEPT == 8
    assert state.failed == digests[1:]  # the oldest is evicted, in order
    assert not connection._gate_closed("acct", digests[0]) or connection._blocked(state)
    connection._note_login_failed("acct", digests[3], "erp_tenant")  # again: moved last
    assert state.failed[-1] == digests[3] and len(state.failed) == 8


async def test_a_success_keeps_the_other_failed_credentials(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    stale, good = _two_destinations(h.PASSWORD, OTHER_PASSWORD)
    with pytest.raises(HanaError):
        await run_query(stale, SQL, [], 1)
    now[0] += LOGIN_BREAKER_SECONDS + 1
    for _ in range(3):
        # the stale destination is answered without a login and does NOT
        # open the shared window again ...
        with pytest.raises(HanaError) as info:
            await run_query(stale, SQL, [], 1)
        assert info.value.code == "login_failed"
        # ... so the destination with the correct password connects
        await run_query(good, SQL, [], 1)
    assert len(db.connect_kwargs) == 4
    state = _state("hana-a")
    assert state.confirmed is not None and len(state.failed) == 1
    assert state.blocked_until < now[0]


# ---- at most MAX_FAILED_LOGINS failed logins per account until a full reset -------------
# HANA counts invalid connects per USER, not per password (default lock limit 6).


def _with_password(system: HanaSystem, password: str) -> None:
    system._resolver.props = h.http_props(Password=password)


async def test_a_stale_burst_then_new_wrong_passwords_cost_nothing_more(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    gate = threading.Event()
    db = h.install(monkeypatch)
    system = _system(resolver=h.FakeResolver())
    await run_query(system, SQL, [], 1)  # A confirmed
    db.refuse_passwords.update({h.PASSWORD, OTHER_PASSWORD, "pw-third-2b8e41"})
    db.gate = gate
    tasks = [asyncio.create_task(run_query(system, SQL, [], 1)) for _ in range(MAX_CONCURRENT)]
    await _until(lambda: db.inside == MAX_CONCURRENT)
    gate.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert len(db.connect_kwargs) == 1 + MAX_CONCURRENT  # the burst: 4 failed
    for password in (OTHER_PASSWORD, "pw-third-2b8e41"):
        _with_password(system, password)
        for _ in range(2):
            now[0] += LOGIN_BREAKER_SECONDS + 1
            connection.soft_reset_login_state()
            with pytest.raises(HanaError) as info:
                await run_query(system, SQL, [], 1)
            assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 1 + MAX_CONCURRENT
    reset_login_state()  # the admin's Reload
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 2 + MAX_CONCURRENT
    assert db.connect_kwargs[-1]["password"] == "pw-third-2b8e41"


async def test_three_wrong_passwords_and_the_fourth_is_not_tried(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    passwords = [f"pw-wrong-{n}-2b8e41" for n in range(4)]
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords=set(passwords)))
    system = _system(resolver=h.FakeResolver())
    assert connection.MAX_FAILED_LOGINS == 3
    for password in passwords:
        _with_password(system, password)
        now[0] += LOGIN_BREAKER_SECONDS + 1
        with pytest.raises(HanaError) as info:
            await run_query(system, SQL, [], 1)
        assert info.value.code == "login_failed"
    assert len(db.connect_kwargs) == 3
    connection.soft_reset_login_state()  # the count survives a soft reset
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    assert len(db.connect_kwargs) == 3
    message = HanaError("login_failed").message
    assert "Reload" in message and "locked" in message and "destination" in message


async def test_a_successful_login_resets_the_count(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    wrong = [f"pw-wrong-{n}-2b8e41" for n in range(4)]
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords=set(wrong)))
    system = _system(resolver=h.FakeResolver())
    for password in (wrong[0], wrong[1], h.PASSWORD, wrong[2], wrong[3]):
        _with_password(system, password)
        now[0] += LOGIN_BREAKER_SECONDS + 1
        try:
            await run_query(system, SQL, [], 1)
        except HanaError:
            pass
    assert len(db.connect_kwargs) == 5  # 2 failed, 1 success (count 0), 2 failed
    assert _state().failures == 2


async def test_a_confirmed_login_clears_the_recheck(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = h.install(monkeypatch, h.FakeDbapi(refuse_passwords={h.PASSWORD}))
    system = _system(resolver=h.FakeResolver())
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    assert _state().recheck_at is not None
    _with_password(system, OTHER_PASSWORD)
    now[0] += LOGIN_BREAKER_SECONDS + 1
    await run_query(system, SQL, [], 1)
    assert _state().recheck_at is None and _state().failures == 0
    assert len(db.connect_kwargs) == 2


async def test_the_refusal_warnings_are_logged_once_per_window(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    passwords = [f"pw-wrong-{n}-2b8e41" for n in range(3)]
    h.install(monkeypatch, h.FakeDbapi(refuse_passwords=set(passwords)))
    system = _system(resolver=h.FakeResolver(h.http_props(Password=passwords[0])))
    with pytest.raises(HanaError):
        await run_query(system, SQL, [], 1)
    now[0] += LOGIN_BREAKER_SECONDS + 1
    for _ in range(4):
        with pytest.raises(HanaError):
            await run_query(system, SQL, [], 1)
    stale = [r for r in caplog.records if "still holds" in r.getMessage()]
    assert len(stale) == 1
    for password in passwords[1:]:
        _with_password(system, password)
        now[0] += LOGIN_BREAKER_SECONDS + 1
        with pytest.raises(HanaError):
            await run_query(system, SQL, [], 1)
    crossed = [r for r in caplog.records if "failed logins" in r.getMessage()]
    assert len(crossed) == 1  # logged when the cap is reached
    caplog.clear()
    now[0] += LOGIN_BREAKER_SECONDS + 1
    for _ in range(4):
        with pytest.raises(HanaError):
            await run_query(system, SQL, [], 1)
    capped = [r for r in caplog.records if "failed logins" in r.getMessage()]
    assert len(capped) == 1
    _no_secret_in_logs(caplog)
