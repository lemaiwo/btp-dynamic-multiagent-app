"""The connection layer of ``builtin:hana`` on a fake hdbcli.

No landscape and no real connection: ``tests/hanadb_helpers.py`` injects a
fake ``hdbcli`` package. Every error text of the fake holds the database
host, the user and a unique marker; none of them may reach an answer or a
log record.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading

import pytest

from agents.destination import DestinationError
from agents.hana import connection
from agents.hana.connection import (
    COMMUNICATION_TIMEOUT_MS,
    CONNECT_TIMEOUT_MS,
    DEFAULT_ENCRYPT,
    LOB_MAX_CHARS,
    LOGIN_BREAKER_SECONDS,
    MAX_CONCURRENT,
    QUERY_TIMEOUT_SECONDS,
    HanaSystem,
    HanaTarget,
    Socks5Proxy,
    connect_kwargs,
    map_driver_error,
    run_query,
    settings_from_properties,
)
from agents.hana.errors import ERROR_CODES, MESSAGES, HanaError
from tests import hanadb_helpers as h

SECRETS = (h.MARKER, h.HOST, h.USER, h.PASSWORD, h.TOKEN)


def _no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def _logs_clean(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        _no_secret(record.getMessage())
        assert record.exc_info is None


def _system(**kw) -> HanaSystem:
    return HanaSystem(
        "erp_tenant", "hana-test",
        resolver=kw.pop("resolver", h.FakeResolver()),
        tokens=kw.pop("tokens", h.FakeTokens()),
    )


# ---- errors ---------------------------------------------------------------------------


def test_error_codes_are_the_closed_list():
    assert ERROR_CODES == frozenset({
        "destination_error", "proxy_refused", "unreachable", "login_failed",
        "not_authorized", "timeout", "unknown_system", "unknown_check",
        "invalid_argument", "select_refused", "result_too_large", "sql_error",
        "call_failed"})
    assert set(MESSAGES) == ERROR_CODES


def test_hana_error_has_fixed_message_and_dict():
    err = HanaError("timeout")
    assert err.code == "timeout"
    assert err.message == MESSAGES["timeout"]
    assert err.to_dict() == {"code": "timeout", "message": MESSAGES["timeout"]}
    assert str(err) == MESSAGES["timeout"]


def test_hana_error_sql_code_only_as_integer():
    assert HanaError("sql_error", sql_code=339).to_dict()["sql_code"] == 339
    assert "sql_code" not in HanaError("sql_error", sql_code="339").to_dict()
    assert "sql_code" not in HanaError("sql_error", sql_code=True).to_dict()


def test_hana_error_refuses_an_unknown_code():
    with pytest.raises(ValueError):
        HanaError("whatever")


# ---- settings_from_properties ------------------------------------------------------------


def test_http_shape_gives_target():
    target = settings_from_properties(h.http_props(), "erp")
    assert isinstance(target, HanaTarget)
    assert (target.host, target.port) == (h.HOST, 30015)
    assert (target.user, target.password) == (h.USER, h.PASSWORD)
    assert target.location_id is None
    assert target.encrypt is DEFAULT_ENCRYPT
    assert target.ssl_host is None


def test_tcp_shape_gives_target():
    target = settings_from_properties(
        h.tcp_props(CloudConnectorLocationId="loc1"), "erp")
    assert (target.host, target.port) == (h.HOST, 30013)
    assert target.location_id == "loc1"


def test_default_encrypt_is_on():
    assert DEFAULT_ENCRYPT is True


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True)])
def test_hana_encrypt_property(value, expected):
    target = settings_from_properties(h.http_props(**{"hana.encrypt": value}), "erp")
    assert target.encrypt is expected


@pytest.mark.parametrize("value", ["False", "no", "0", "", " false", "TRUE"])
def test_hana_encrypt_other_value_refused(value):
    with pytest.raises(HanaError) as info:
        settings_from_properties(h.http_props(**{"hana.encrypt": value}), "erp")
    assert info.value.code == "destination_error"


def test_ssl_host_property_read():
    target = settings_from_properties(
        h.http_props(**{"hana.sslHostNameInCertificate": "db.example.invalid"}), "erp")
    assert target.ssl_host == "db.example.invalid"


@pytest.mark.parametrize("props", [
    h.http_props(ProxyType="Internet"),
    h.http_props(ProxyType=None),
    h.http_props(User=None),
    h.http_props(Password=None),
    h.http_props(User=""),
    h.http_props(URL=f"https://{h.HOST}:30015"),
    h.http_props(URL=f"http://{h.HOST}"),
    h.http_props(URL=f"http://u:p@{h.HOST}:30015"),
    h.http_props(URL=f"http://{h.HOST}:30015/x?y=1"),
    h.http_props(URL=f"http://{h.HOST}:99999"),
    h.http_props(Authentication="NoAuthentication"),
    h.http_props(Type="RFC"),
    h.tcp_props(Address=h.HOST),
    h.tcp_props(Address=f"{h.HOST}:0"),
    h.tcp_props(Address=f"{h.HOST}:abc"),
    h.http_props(**{"hana.sslHostNameInCertificate": "bad host/x"}),
    h.http_props(**{"hana.sslTrustStore": "x"}),
    h.http_props(**{"hana.Encrypt": "false"}),
], ids=lambda p: ",".join(sorted(p)) if isinstance(p, dict) else str(p))
def test_bad_destination_refused(props, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HanaError) as info:
        settings_from_properties(props, "erp")
    assert info.value.code == "destination_error"
    _no_secret(str(info.value.to_dict()))
    _logs_clean(caplog)


def test_target_repr_shows_no_credential_or_host():
    target = settings_from_properties(
        h.http_props(CloudConnectorLocationId="loc1"), "erp")
    text = repr(target) + str(target)
    _no_secret(text)
    assert "loc1" not in text


# ---- connect_kwargs ----------------------------------------------------------------------

PROXY = Socks5Proxy("connectivity-proxy.internal", 20004)


def test_connect_kwargs_props_variant_unencrypted():
    target = settings_from_properties(h.http_props(**{"hana.encrypt": "false"}), "erp")
    assert connect_kwargs(target, h.TOKEN, PROXY) == {
        "address": h.HOST,
        "port": 30015,
        "user": h.USER,
        "password": h.PASSWORD,
        "proxyHostname": "connectivity-proxy.internal",
        "proxyPort": "20004",
        "proxyUserName": h.TOKEN,
        "connectTimeout": str(CONNECT_TIMEOUT_MS),
        "communicationTimeout": str(COMMUNICATION_TIMEOUT_MS),
        "encrypt": "false",
    }
    assert CONNECT_TIMEOUT_MS == 15000
    assert COMMUNICATION_TIMEOUT_MS == 30000


def test_connect_kwargs_encrypted_validates_always():
    target = settings_from_properties(h.http_props(), "erp")
    kwargs = connect_kwargs(target, h.TOKEN, PROXY)
    assert kwargs["encrypt"] == "true"
    assert kwargs["sslValidateCertificate"] == "true"
    assert "sslHostNameInCertificate" not in kwargs
    target = settings_from_properties(
        h.http_props(**{"hana.sslHostNameInCertificate": "db.example.invalid"}), "erp")
    kwargs = connect_kwargs(target, h.TOKEN, PROXY)
    assert kwargs["sslHostNameInCertificate"] == "db.example.invalid"


def test_connect_kwargs_no_ssl_keys_without_encrypt():
    target = settings_from_properties(
        h.http_props(**{"hana.encrypt": "false",
                        "hana.sslHostNameInCertificate": "db.example.invalid"}), "erp")
    kwargs = connect_kwargs(target, h.TOKEN, PROXY)
    assert "sslValidateCertificate" not in kwargs
    assert "sslHostNameInCertificate" not in kwargs


def test_connect_kwargs_location_id_goes_into_proxy_password():
    target = settings_from_properties(
        h.http_props(CloudConnectorLocationId="loc1", **{"hana.encrypt": "false"}), "erp")
    kwargs = connect_kwargs(target, h.TOKEN, PROXY)
    assert kwargs["proxyPassword"] == "1." + base64.b64encode(b"loc1").decode()
    assert "proxyScpAccount" not in kwargs


def test_connect_kwargs_without_location_has_no_proxy_password():
    target = settings_from_properties(h.http_props(), "erp")
    assert "proxyPassword" not in connect_kwargs(target, h.TOKEN, PROXY)


# ---- run_query ---------------------------------------------------------------------------


async def test_run_query_sequence_and_result(monkeypatch):
    db = h.install(monkeypatch, h.FakeDbapi(rows=[(1, "x"), (2, "y")]))
    columns, rows, truncated = await run_query(
        _system(), "SELECT A, B FROM M_X WHERE A = ?", ["1"], 5)
    assert columns == ["A", "B"]
    assert rows == [(1, "x"), (2, "y")]
    assert truncated is False
    assert db.calls == [
        ("connect",),
        ("setautocommit", False),
        ("cursor",),
        ("set_schema",),
        ("read_only",),
        ("setquerytimeout", QUERY_TIMEOUT_SECONDS),
        ("execute", "SELECT A, B FROM M_X WHERE A = ?", ("1",)),
        ("fetchmany", 6),
        ("rollback",),
        ("cursor_close",),
        ("close",),
    ]
    assert QUERY_TIMEOUT_SECONDS == 20
    assert "commit" not in db.steps()
    assert db.connect_kwargs[0]["proxyUserName"] == h.TOKEN
    assert db.connect_kwargs[0]["proxyPort"] == "20004"


async def test_run_query_without_binds(monkeypatch):
    db = h.install(monkeypatch)
    await run_query(_system(), "SELECT 1 FROM DUMMY", [], 1)
    assert ("execute", "SELECT 1 FROM DUMMY") in db.calls


async def test_run_query_truncates(monkeypatch):
    h.install(monkeypatch, h.FakeDbapi(rows=[(i, i) for i in range(10)]))
    columns, rows, truncated = await run_query(_system(), "SELECT A, B FROM M_X", [], 3)
    assert rows == [(0, 0), (1, 1), (2, 2)]
    assert truncated is True


async def test_run_query_runs_in_a_thread(monkeypatch):
    db = h.install(monkeypatch)
    seen = []
    original = db.connect

    def connect(**kw):
        seen.append(threading.current_thread() is threading.main_thread())
        return original(**kw)

    monkeypatch.setattr(db, "connect", connect)
    import sys
    monkeypatch.setattr(sys.modules["hdbcli.dbapi"], "connect", connect)
    await run_query(_system(), "SELECT 1 FROM DUMMY", [], 1)
    assert seen == [False]


@pytest.mark.parametrize("step", [
    "setautocommit", "cursor", "set_schema", "read_only", "setquerytimeout", "execute",
    "fetchmany"])
async def test_rollback_and_close_on_every_failure(monkeypatch, step, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch, h.FakeDbapi(fail_at={step: h.driver_error(339, "boom")}))
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT A FROM M_X", [], 1)
    assert info.value.code == "sql_error"
    steps = db.steps()
    assert "rollback" in steps and steps[-1] == "close"
    assert "commit" not in steps
    if step in ("setautocommit", "cursor", "read_only", "set_schema"):
        assert "execute" not in steps
    _no_secret(str(info.value.to_dict()))
    assert info.value.__cause__ is None
    _logs_clean(caplog)


async def test_rollback_failure_still_closes(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch, h.FakeDbapi(
        rows=[(1, 2)], fail_at={"rollback": h.driver_error(-10807, "lost")}))
    _, rows, _ = await run_query(_system(), "SELECT A FROM M_X", [], 1)
    assert rows == [(1, 2)]
    assert db.steps()[-1] == "close"
    _logs_clean(caplog)


async def test_connect_failure_closes_nothing_and_maps(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch, h.FakeDbapi(
        fail_at={"connect": h.driver_error(10, "authentication failed")}))
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT A FROM M_X", [], 1)
    assert info.value.code == "login_failed"
    assert db.steps() == ["connect"]
    _logs_clean(caplog)
    assert any("erp_tenant" in r.getMessage() and "connect" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("max_rows", [0, -1, True, "5", 1.5])
async def test_run_query_refuses_bad_max_rows(monkeypatch, max_rows):
    db = h.install(monkeypatch)
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT 1 FROM DUMMY", [], max_rows)
    assert info.value.code == "invalid_argument"
    assert db.calls == []


async def test_at_most_four_at_a_time(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    assert MAX_CONCURRENT == 4
    tasks = [asyncio.create_task(run_query(_system(), "SELECT 1 FROM DUMMY", [], 1))
             for _ in range(5)]
    for _ in range(200):
        if db.inside == 4:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert db.inside == 4
    assert len(db.connect_kwargs) == 4
    gate.set()
    await asyncio.gather(*tasks)
    assert db.peak == 4
    assert len(db.connect_kwargs) == 5


async def test_cancelled_caller_keeps_slot_until_thread_ends(monkeypatch):
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    tasks = [asyncio.create_task(run_query(_system(), "SELECT 1 FROM DUMMY", [], 1))
             for _ in range(4)]
    for _ in range(200):
        if db.inside == 4:
            break
        await asyncio.sleep(0.01)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    fifth = asyncio.create_task(run_query(_system(), "SELECT 1 FROM DUMMY", [], 1))
    await asyncio.sleep(0.05)
    assert len(db.connect_kwargs) == 4  # the fifth still waits for a slot
    gate.set()
    await fifth
    assert len(db.connect_kwargs) == 5
    assert db.peak <= 4


# ---- error mapping -----------------------------------------------------------------------


@pytest.mark.parametrize("exc,code", [
    (h.driver_error(10, "authentication failed"), "login_failed"),
    (h.driver_error(414, "user is forced to change password"), "login_failed"),
    (h.driver_error(663, "user is locked"), "login_failed"),
    (h.driver_error(258, "insufficient privilege"), "not_authorized"),
    (h.driver_error(1024, "timeout"), "timeout"),
    (h.driver_error(613, "execution aborted by timeout"), "timeout"),
    (h.driver_error(339, "invalid number"), "sql_error"),
    (h.driver_error(-10709, "Connection failed (RTE:[89006] System call 'connect' "
                            "failed, rc=111:Connection refused)"), "proxy_refused"),
    (h.driver_error(-10709, "Connection failed (proxy server refused)"), "proxy_refused"),
    (h.driver_error(-10709, "Connection failed (proxy handshake)"), "unreachable"),
    (h.driver_error(-10709, "Connection failed (SSL handshake failed)"), "unreachable"),
    (h.driver_error(-10709, "Connection failed (RTE:[89008] Socket timed out)"), "timeout"),
    (h.driver_error(-10709, "Connection failed (RTE:[89001] Cannot resolve host)"),
     "unreachable"),
    (h.driver_error(-10807, "Connection down"), "sql_error"),
    (ConnectionRefusedError(f"{h.HOST} {h.MARKER}"), "proxy_refused"),
    (TimeoutError(f"{h.HOST} {h.MARKER}"), "timeout"),
    (OSError(f"{h.HOST} {h.MARKER}"), "unreachable"),
])
def test_map_driver_error(monkeypatch, exc, code, caplog):
    caplog.set_level(logging.DEBUG)
    h.install(monkeypatch)
    err = map_driver_error(exc)
    assert isinstance(err, HanaError)
    assert err.code == code
    _no_secret(str(err.to_dict()))
    _no_secret(str(err))
    _logs_clean(caplog)


def test_sql_error_carries_integer_code_only(monkeypatch):
    h.install(monkeypatch)
    err = map_driver_error(h.driver_error(339, "invalid number"))
    assert err.to_dict() == {"code": "sql_error", "message": MESSAGES["sql_error"],
                             "sql_code": 339}
    err = map_driver_error(h.driver_error("339", "odd code"))
    assert "sql_code" not in err.to_dict()


def test_unexpected_exception_is_sql_error_without_text(monkeypatch):
    h.install(monkeypatch)
    err = map_driver_error(RuntimeError(h.MARKER))
    assert err.code == "sql_error"
    _no_secret(str(err.to_dict()))


# ---- destination, binding, token ---------------------------------------------------------


async def test_destination_service_failure(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch)
    resolver = h.FakeResolver(error=DestinationError(f"404 for {h.HOST} {h.MARKER}"))
    with pytest.raises(HanaError) as info:
        await run_query(_system(resolver=resolver), "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "destination_error"
    assert db.calls == []
    _logs_clean(caplog)


async def test_bad_destination_properties(monkeypatch):
    db = h.install(monkeypatch)
    resolver = h.FakeResolver(h.http_props(ProxyType="Internet"))
    with pytest.raises(HanaError) as info:
        await run_query(_system(resolver=resolver), "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "destination_error"
    assert db.calls == []


async def test_no_socks5_port(monkeypatch):
    db = h.install(monkeypatch)
    with pytest.raises(HanaError) as info:
        await run_query(_system(tokens=h.FakeTokens(socks5_port=None)),
                        "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "destination_error"
    assert info.value.message == MESSAGES["destination_error"]
    assert db.calls == []


async def test_no_connectivity_binding(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch)
    import agents.destination as destination

    monkeypatch.setattr(destination, "connectivity_from_environment", lambda: None)
    system = HanaSystem("erp_tenant", "hana-test", resolver=h.FakeResolver())
    with pytest.raises(HanaError) as info:
        await run_query(system, "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "destination_error"
    assert db.calls == []
    assert any("connectivity" in r.getMessage() for r in caplog.records)


async def test_no_destination_binding(monkeypatch):
    db = h.install(monkeypatch)
    import agents.destination as destination

    def refuse(*a, **kw):
        raise DestinationError("no binding")

    monkeypatch.setattr(destination, "resolver_from_environment", refuse)
    system = HanaSystem("erp_tenant", "hana-test", tokens=h.FakeTokens())
    with pytest.raises(HanaError) as info:
        await run_query(system, "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "destination_error"
    assert db.calls == []


async def test_token_failure_is_proxy_refused(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch)
    tokens = h.FakeTokens(error=DestinationError(f"returned 401 {h.MARKER}"))
    with pytest.raises(HanaError) as info:
        await run_query(_system(tokens=tokens), "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "proxy_refused"
    assert db.calls == []
    _logs_clean(caplog)


def test_system_repr_names_no_destination_internals():
    system = _system()
    assert "erp_tenant" in repr(system)


def test_hdbcli_not_imported_at_module_load():
    import inspect

    source = inspect.getsource(connection)
    head = source.split("\ndef ", 1)[0]
    assert "import hdbcli" not in head and "from hdbcli" not in head


def test_log_lines_name_no_exception_text():
    import inspect
    import re

    source = inspect.getsource(connection)
    calls = re.findall(r"logger\.\w+\((.*?)\)\n", source, flags=re.S)
    assert calls
    for call in calls:
        assert "str(exc" not in call and "exc_info" not in call
        assert ".host" not in call and ".user" not in call


# ---- fix round 1 -------------------------------------------------------------------------


def test_schema_statement_is_fixed():
    assert connection.SCHEMA_STATEMENT == "SET SCHEMA SYS"


def test_hana_database_property_accepted_and_ignored():
    target = settings_from_properties(h.http_props(**{"hana.database": "ERP"}), "erp")
    kwargs = connect_kwargs(target, h.TOKEN, PROXY)
    assert "databaseName" not in kwargs
    assert "ERP" not in repr(target)


def test_unknown_hana_property_named_by_rule_not_value(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HanaError) as info:
        settings_from_properties(h.http_props(**{"hana.sslTrustStore": h.MARKER}), "erp")
    assert info.value.code == "destination_error"
    _logs_clean(caplog)
    assert any("hana." in r.getMessage() for r in caplog.records)


def test_connect_kwargs_encrypt_off_is_the_landscape_set():
    target = settings_from_properties(h.http_props(**{"hana.encrypt": "false"}), "erp")
    kwargs = connect_kwargs(target, h.TOKEN, PROXY)
    assert kwargs["encrypt"] == "false"
    assert not [k for k in kwargs if k.startswith("ssl")]


def test_10807_keeps_its_sql_code(monkeypatch):
    h.install(monkeypatch)
    err = map_driver_error(h.driver_error(-10807, "Connection down"))
    assert err.to_dict()["sql_code"] == -10807


def test_os_errno_is_not_logged_as_sql_code(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch, h.FakeDbapi(
        fail_at={"connect": ConnectionRefusedError(111, h.MARKER)}))
    with pytest.raises(HanaError) as info:
        await_ = run_query(_system(), "SELECT 1 FROM DUMMY", [], 1)
        asyncio.run(await_)
    assert info.value.code == "proxy_refused"
    assert db.steps() == ["connect"]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "sql_code" not in text and "ConnectionRefusedError" in text


async def test_call_deadline_raises_timeout_and_keeps_slot(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    assert connection.CALL_DEADLINE_SECONDS == 45
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 0.1)
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "timeout"
    assert db.inside == 1  # the thread still runs
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 5)
    more = [asyncio.create_task(run_query(_system(), "SELECT 1 FROM DUMMY", [], 1))
            for _ in range(4)]
    await asyncio.sleep(0.1)
    assert len(db.connect_kwargs) == 4  # 1 timed-out thread + 3: the slot is still taken
    gate.set()
    await asyncio.gather(*more)
    assert len(db.connect_kwargs) == 5
    assert db.peak <= MAX_CONCURRENT
    _logs_clean(caplog)


async def _until(predicate, tries: int = 300) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.mark.parametrize("outcome", ["failed", "rows"])
async def test_a_thread_that_ends_as_the_deadline_passes_is_answered_by_its_outcome(
        monkeypatch, caplog, outcome):
    """The thread ends while the loop is busy and the deadline passes in the
    same stretch: the caller gets the thread's outcome (a failure mapped like
    any other), never the deadline's bare TimeoutError."""
    import time

    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    fail_at = {"connect": h.driver_error(339, "boom")} if outcome == "failed" else {}
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate, rows=[(1,)], fail_at=fail_at))
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 0.3)
    task = asyncio.create_task(run_query(_system(), "SELECT 1 FROM DUMMY", [], 1))
    await _until(lambda: db.inside == 1)
    gate.set()
    time.sleep(0.6)  # blocks the loop: the thread ends and the deadline passes
    if outcome == "failed":
        with pytest.raises(HanaError) as info:
            await task
        assert info.value.code == "sql_error"
    else:
        _, rows, _ = await task
        assert rows == [(1,)]
    _logs_clean(caplog)


async def test_the_deadline_covers_the_wait_for_a_free_slot(monkeypatch, caplog):
    """Four threads hold every slot; a fifth caller whose deadline passes
    while it still waits gets ``timeout`` and never takes or leaks a slot."""
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    db = h.install(monkeypatch, h.FakeDbapi(gate=gate))
    held = [asyncio.create_task(run_query(_system(), "SELECT 1 FROM DUMMY", [], 1))
            for _ in range(MAX_CONCURRENT)]
    await _until(lambda: db.inside == MAX_CONCURRENT)
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 0.1)
    with pytest.raises(HanaError) as info:
        await run_query(_system(), "SELECT 1 FROM DUMMY", [], 1)
    assert info.value.code == "timeout"
    assert len(db.connect_kwargs) == MAX_CONCURRENT  # the fifth never connected
    monkeypatch.setattr(connection, "CALL_DEADLINE_SECONDS", 5)
    gate.set()
    await asyncio.gather(*held)
    slot = connection._semaphore()
    assert slot._value == MAX_CONCURRENT  # nothing leaked
    await run_query(_system(), "SELECT 1 FROM DUMMY", [], 1)
    assert slot._value == MAX_CONCURRENT
    _logs_clean(caplog)
    assert any("erp_tenant" in r.getMessage() for r in caplog.records)


# ---- final review: the order of the session statements ----------------------------------


async def test_schema_is_pinned_before_the_transaction_is_made_read_only(monkeypatch):
    db = h.install(monkeypatch)
    await run_query(_system(), "SELECT 1 FROM SYS.DUMMY", [], 1)
    steps = db.steps()
    assert steps.index("setautocommit") < steps.index("set_schema") \
        < steps.index("read_only") < steps.index("setquerytimeout") < steps.index("execute")
    # nothing runs between the read-only statement and the query but the timeout
    assert steps[steps.index("read_only") + 1:steps.index("execute")] == ["setquerytimeout"]


# ---- final review: the login breaker ----------------------------------------------------


def _login_failing(monkeypatch, code: int = 10) -> h.FakeDbapi:
    return h.install(monkeypatch, h.FakeDbapi(
        fail_at={"connect": h.driver_error(code, "authentication failed")}))


async def test_breaker_after_a_login_failure_connects_once(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = _login_failing(monkeypatch)
    system = _system()
    for _ in range(3):
        with pytest.raises(HanaError) as info:
            await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
        assert info.value.code == "login_failed"
    assert db.steps() == ["connect"]
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "breaker" in r.getMessage()]
    assert len(warnings) == 1 and "erp_tenant" in warnings[0]
    _logs_clean(caplog)
    assert "hana-test" not in " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("code", [10, 414, 663])
async def test_breaker_opens_for_every_login_code(monkeypatch, code):
    db = _login_failing(monkeypatch, code)
    system = _system()
    for _ in range(2):
        with pytest.raises(HanaError):
            await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
    assert db.steps() == ["connect"]


async def test_breaker_window_ends_and_reads_the_destination_fresh(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(connection, "_clock", lambda: now[0])
    db = _login_failing(monkeypatch)
    resolver = h.FakeResolver()
    system = _system(resolver=resolver)
    with pytest.raises(HanaError):
        await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
    now[0] += LOGIN_BREAKER_SECONDS - 1
    with pytest.raises(HanaError) as info:
        await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
    assert info.value.code == "login_failed"
    assert db.steps() == ["connect"]
    assert resolver.forced == [False]  # the blocked call read nothing
    del db.fail_at["connect"]
    now[0] += 2
    await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
    assert db.steps().count("connect") == 2
    assert resolver.forced == [False, True]  # the cached properties were dropped
    await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
    assert resolver.forced == [False, True, False]
    assert LOGIN_BREAKER_SECONDS == 900


async def test_breaker_is_per_system(monkeypatch):
    db = _login_failing(monkeypatch)
    first, second = _system(), HanaSystem(
        "erp_systemdb", "hana-other", resolver=h.FakeResolver(), tokens=h.FakeTokens())
    with pytest.raises(HanaError):
        await run_query(first, "SELECT 1 FROM SYS.DUMMY", [], 1)
    with pytest.raises(HanaError) as info:
        await run_query(second, "SELECT 1 FROM SYS.DUMMY", [], 1)
    assert info.value.code == "login_failed"
    assert db.steps() == ["connect", "connect"]


async def test_a_new_system_object_resets_the_breaker(monkeypatch):
    # A registry reload builds new HanaSystem objects: the way out after the
    # password was corrected in the destination.
    db = _login_failing(monkeypatch)
    with pytest.raises(HanaError):
        await run_query(_system(), "SELECT 1 FROM SYS.DUMMY", [], 1)
    del db.fail_at["connect"]
    await run_query(_system(), "SELECT 1 FROM SYS.DUMMY", [], 1)
    assert db.steps().count("connect") == 2


@pytest.mark.parametrize("failure", [
    h.driver_error(258, "insufficient privilege"),
    h.driver_error(-10709, "connection refused"),
    ConnectionRefusedError(111, "refused"),
])
async def test_other_failures_do_not_open_the_breaker(monkeypatch, failure):
    db = h.install(monkeypatch, h.FakeDbapi(fail_at={"connect": failure}))
    system = _system()
    for _ in range(2):
        with pytest.raises(HanaError) as info:
            await run_query(system, "SELECT 1 FROM SYS.DUMMY", [], 1)
        assert info.value.code != "login_failed"
    assert db.steps() == ["connect", "connect"]


# ---- final review: LOB values -------------------------------------------------------------


async def test_text_lob_is_read_before_the_connection_closes(monkeypatch):
    db = h.install(monkeypatch)
    lob = h.FakeLob("SELECT * FROM T WHERE X = 1", db=db)
    db.rows = [(1, lob)]
    _, rows, _ = await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 1)
    assert rows == [(1, "SELECT * FROM T WHERE X = 1")]
    steps = db.steps()
    assert steps.index("fetchmany") < steps.index("lob_read") < steps.index("rollback")


async def test_long_text_lob_is_cut_with_a_marker(monkeypatch):
    db = h.install(monkeypatch)
    db.rows = [(h.FakeLob("x" * (LOB_MAX_CHARS + 10), db=db),)]
    _, rows, _ = await run_query(_system(), "SELECT A FROM SYS.M_X", [], 1)
    value = rows[0][0]
    assert value == "x" * LOB_MAX_CHARS + " …[cut]"
    assert LOB_MAX_CHARS == 65536


async def test_text_lob_of_exactly_the_cap_is_not_marked(monkeypatch):
    db = h.install(monkeypatch)
    db.rows = [(h.FakeLob("y" * LOB_MAX_CHARS, db=db),)]
    _, rows, _ = await run_query(_system(), "SELECT A FROM SYS.M_X", [], 1)
    assert rows[0][0] == "y" * LOB_MAX_CHARS


async def test_binary_lob_is_bytes_cut_at_64k(monkeypatch):
    db = h.install(monkeypatch)
    db.rows = [(h.FakeLob(b"\x01" * 70000, db=db),)]
    _, rows, _ = await run_query(_system(), "SELECT A FROM SYS.M_X", [], 1)
    assert rows[0][0] == b"\x01" * 65536


async def test_unreadable_lob_is_a_fixed_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    db = h.install(monkeypatch)
    db.rows = [(h.FakeLob("x", error=h.driver_error(-10807, "lob gone"), db=db), 2)]
    _, rows, _ = await run_query(_system(), "SELECT A, B FROM SYS.M_X", [], 1)
    assert rows == [("<unreadable LOB>", 2)]
    assert db.steps()[-1] == "close" and "commit" not in db.steps()
    _logs_clean(caplog)


async def test_lobs_of_the_dropped_extra_row_are_not_read(monkeypatch):
    db = h.install(monkeypatch)
    kept, extra = h.FakeLob("a", db=db), h.FakeLob("b", db=db)
    db.rows = [(kept,), (extra,)]
    _, rows, truncated = await run_query(_system(), "SELECT A FROM SYS.M_X", [], 1)
    assert rows == [("a",)] and truncated is True
    assert extra.reads == []
