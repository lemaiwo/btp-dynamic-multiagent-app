"""Tests for ``scripts/probe_hana_connectivity.py`` (the phase 0 probe of builtin:hana).

No SAP system, BTP service or network is reached: the destination service and
the token endpoints are one ``httpx.MockTransport``, the SOCKS5 proxy is the
other end of a ``socket.socketpair`` and ``hdbcli`` is a fake ``connect``.
Every test also checks that no secret of the fixtures reaches the output.
"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest
from hdbcli import dbapi

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import probe_hana_connectivity as probe  # noqa: E402

SECRETS = (
    "s3cr3t-destination",
    "s3cr3t-connectivity",
    "dest-client",
    "conn-client",
    "connectivityproxy.internal",
    "tenant.authentication.example.com",
    "DB-PASSWORD-MUST-NOT-PRINT",
    "TECHUSER",
    "JWT-MUST-NOT-PRINT",
    "vhost",
    "LOC1",
    "TENANTDB",
)

CONNECTIVITY = {
    "clientid": "conn-client",
    "clientsecret": "s3cr3t-connectivity",
    "token_service_url": "https://tenant.authentication.example.com",
    "onpremise_proxy_host": "connectivityproxy.internal",
    "onpremise_proxy_http_port": "20003",
    "onpremise_socks5_proxy_port": "20004",
}

VCAP = {
    "destination": [
        {
            "credentials": {
                "clientid": "dest-client",
                "clientsecret": "s3cr3t-destination",
                "url": "https://tenant.authentication.example.com",
                "uri": "https://destination-configuration.example.com",
            }
        }
    ],
    "connectivity": [{"credentials": CONNECTIVITY}],
}

HTTP_DESTINATION = {
    "Name": "hana-dest",
    "Type": "HTTP",
    "URL": "http://vhost:30013",
    "ProxyType": "OnPremise",
    "Authentication": "BasicAuthentication",
    "User": "TECHUSER",
    "Password": "DB-PASSWORD-MUST-NOT-PRINT",
    "CloudConnectorLocationId": "LOC1",
    "hana.database": "TENANTDB",
}


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, secret


# ---- S1 bindings ---------------------------------------------------------------


def test_bindings_are_read_from_vcap_and_never_printed(capsys):
    cfg = probe.read_bindings(json.dumps(VCAP))
    assert cfg.connectivity.proxy_host == "connectivityproxy.internal"
    assert cfg.connectivity.socks5_port == 20004
    assert cfg.destination.uri == "https://destination-configuration.example.com"
    probe.describe_bindings(cfg)
    out = capsys.readouterr().out
    assert "PASS bindings" in out
    _assert_clean(out)
    _assert_clean(repr(cfg))


def test_a_binding_without_the_socks5_port_is_reported(capsys):
    conn = {k: v for k, v in CONNECTIVITY.items() if k != "onpremise_socks5_proxy_port"}
    vcap = dict(VCAP, connectivity=[{"credentials": conn}])
    with pytest.raises(probe.ProbeError) as raised:
        probe.read_bindings(json.dumps(vcap))
    assert str(raised.value) == "socks5_port: not in the connectivity binding"

    args = probe.build_parser().parse_args(["--destination", "d"])
    code = probe.run(args, {"VCAP_SERVICES": json.dumps(vcap)})
    out = capsys.readouterr().out
    assert code == 2
    assert "FAIL socks5_port: not in the connectivity binding" in out
    _assert_clean(out)


def test_missing_vcap_is_named():
    with pytest.raises(probe.ProbeError, match="VCAP_SERVICES"):
        probe.read_bindings("")
    partial = {k: v for k, v in VCAP.items() if k != "destination"}
    with pytest.raises(probe.ProbeError, match="destination"):
        probe.read_bindings(json.dumps(partial))


# ---- destination shape -----------------------------------------------------------


def test_destination_shape():
    assert probe.destination_shape({"Type": "TCP", "Address": "vhost:30013"}) == "tcp"
    assert probe.destination_shape({"Type": "HTTP", "URL": "http://h:30015"}) == "http"
    assert probe.destination_shape({"Type": "HTTP", "URL": "https://h:30015"}) == "unknown"
    assert probe.destination_shape({"Type": "HTTP", "URL": "http://h"}) == "unknown"
    assert probe.destination_shape({"Type": "TCP"}) == "unknown"
    assert probe.destination_shape({"Type": "RFC"}) == "unknown"
    assert probe.destination_shape({}) == "unknown"


def test_split_address():
    assert probe.split_address("vhost:30013") == ("vhost", 30013)
    for bad in ("vhost:0", "vhost:65536", "vhost", ":30013", "vhost:x", "vhost:-1"):
        with pytest.raises(probe.ProbeError):
            probe.split_address(bad)


def test_target_of_both_shapes():
    assert probe.target_of(HTTP_DESTINATION, "http") == ("vhost", 30013)
    assert probe.target_of({"Type": "TCP", "Address": "vhost:30015"}, "tcp") == (
        "vhost",
        30015,
    )


# ---- SOCKS5 ----------------------------------------------------------------------


def test_socks5_auth_packet_bytes():
    packet = probe.socks5_auth_packet("abc", "LOC1")
    loc = base64.b64encode(b"LOC1")
    assert packet == b"\x01" + (3).to_bytes(4, "big") + b"abc" + bytes([len(loc)]) + loc
    assert probe.socks5_auth_packet("abc", "") == b"\x01\x00\x00\x00\x03abc\x00"


def test_socks5_greeting_and_connect_request():
    assert probe.SOCKS5_GREETING == b"\x05\x01\x80"
    assert probe.socks5_connect_request("vhost", 30013) == (
        b"\x05\x01\x00\x03\x05vhost" + (30013).to_bytes(2, "big")
    )
    ipv4 = b"\x05\x01\x00\x01\x0a\x00\x00\x01\x00\x01"
    assert probe.socks5_connect_request("10.0.0.1", 1) == ipv4


def _fake_proxy(sock: socket.socket, seen: list[bytes], reply_code: int) -> None:
    def read(n: int) -> bytes:
        data = b""
        while len(data) < n:
            data += sock.recv(n - len(data))
        return data

    seen.append(read(3))
    sock.sendall(b"\x05\x80")
    head = read(5)
    token = read(int.from_bytes(head[1:], "big"))
    loc_len = read(1)
    loc = read(loc_len[0])
    seen.append(head + token + loc_len + loc)
    sock.sendall(b"\x01\x00")
    req = read(5)
    rest = read(req[4] + 2)
    seen.append(req + rest)
    sock.sendall(bytes([5, reply_code, 0, 1, 0, 0, 0, 0, 0, 0]))
    sock.close()


@pytest.mark.parametrize("reply_code", [0, 2])
def test_socks5_handshake_against_a_fake_proxy(reply_code):
    client, server = socket.socketpair()
    seen: list[bytes] = []
    thread = threading.Thread(target=_fake_proxy, args=(server, seen, reply_code))
    thread.start()
    try:
        code = probe.socks5_handshake(client, "JWT-MUST-NOT-PRINT", "LOC1", "vhost", 30013)
    finally:
        client.close()
        thread.join(5)
    assert code == reply_code
    assert seen[0] == b"\x05\x01\x80"
    assert seen[1] == probe.socks5_auth_packet("JWT-MUST-NOT-PRINT", "LOC1")
    assert seen[2] == probe.socks5_connect_request("vhost", 30013)


# ---- hdbcli keyword sets -----------------------------------------------------------


def _kwargs(variant: str, location: str = "LOC1", encrypt: bool = True) -> dict:
    return probe.connect_kwargs(
        variant,
        proxy_host="connectivityproxy.internal",
        proxy_port=20004,
        token="JWT-MUST-NOT-PRINT",
        location_id=location,
        host="vhost",
        port=30013,
        user="TECHUSER",
        password="DB-PASSWORD-MUST-NOT-PRINT",
        encrypt=encrypt,
        hana_props={"hana.database": "TENANTDB"},
        timeout=15.0,
    )


def test_connect_kwargs_props_and_props_account():
    props = _kwargs("props")
    assert props["address"] == "vhost"
    assert props["port"] == 30013
    assert props["user"] == "TECHUSER"
    assert props["password"] == "DB-PASSWORD-MUST-NOT-PRINT"
    assert props["proxyHostname"] == "connectivityproxy.internal"
    assert props["proxyPort"] == "20004"
    assert props["proxyUserName"] == "JWT-MUST-NOT-PRINT"
    assert props["proxyPassword"] == "1." + base64.b64encode(b"LOC1").decode()
    assert "proxyScpAccount" not in props
    assert props["encrypt"] == "true"
    assert props["sslValidateCertificate"] == "true"
    assert props["connectTimeout"] == "15000"
    assert props["databaseName"] == "TENANTDB"

    account = _kwargs("props-account")
    assert account["proxyScpAccount"] == "LOC1"
    assert "proxyPassword" not in account
    assert account["proxyUserName"] == "JWT-MUST-NOT-PRINT"

    plain = _kwargs("props", location="", encrypt=False)
    assert "proxyPassword" not in plain
    assert plain["encrypt"] == "false"
    assert "sslValidateCertificate" not in plain
    with pytest.raises(probe.ProbeError):
        _kwargs("props-account", location="")
    with pytest.raises(ValueError):
        _kwargs("other")


def test_printable_kwargs_masks_every_value():
    printable = probe.printable_kwargs(_kwargs("props-account"))
    assert set(printable) == set(_kwargs("props-account"))
    assert set(printable.values()) == {"***"}
    _assert_clean(json.dumps(printable))


# ---- privileges ----------------------------------------------------------------------


def test_privileges_are_classified():
    assert probe.classify_privileges([("SELECT", "SAPHANADB"), ("SELECT", "SYS")]) == "too_wide"
    assert probe.classify_privileges([("CATALOG READ", None), ("MONITORING", None)]) == "ok"
    assert (
        probe.classify_privileges(
            [("SELECT", "SYS"), ("SELECT", "_SYS_STATISTICS"), ("EXECUTE", "_SYS_REPO")]
        )
        == "ok"
    )
    assert probe.classify_privileges([("SELECT", "PUBLIC")]) == "ok"
    assert probe.classify_privileges([("DELETE", "ZAPP")]) == "too_wide"
    # The user's own schema holds nothing of the system's data.
    assert probe.classify_privileges([("SELECT", "PROBEUSER")], own_schema="PROBEUSER") == "ok"
    assert probe.classify_privileges([]) == "unknown"


# ---- checks file ---------------------------------------------------------------------


class FakeCursor:
    def __init__(self, fail_on: str | None = None) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fail_on = fail_on

    def execute(self, sql, binds=()):
        self.executed.append((sql, tuple(binds)))
        if self.fail_on and self.fail_on in sql:
            raise dbapi.Error(258, "insufficient privilege: secret text")
        return True

    def fetchall(self):
        return [("ROW-VALUE-MUST-NOT-PRINT",)]

    def fetchone(self):
        return ("ROW-VALUE-MUST-NOT-PRINT",)


def test_checks_file_is_validated(tmp_path):
    good = tmp_path / "checks.json"
    good.write_text(json.dumps([{"name": "ok_check", "sql": "SELECT 1 FROM DUMMY"}]))
    assert probe.load_checks(str(good)) == [
        {"name": "ok_check", "sql": "SELECT 1 FROM DUMMY", "binds": []}
    ]
    for bad in (
        {"not": "a list"},
        [{"name": "x"}],
        [{"name": "bad name!", "sql": "SELECT 1 FROM DUMMY"}],
        [{"name": "x", "sql": "SELECT 1", "binds": [{"a": 1}]}],
        [{"name": "x", "sql": "SELECT 1", "extra": 1}],
    ):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(probe.ProbeError):
            probe.load_checks(str(path))


def test_checks_run_wrapped_and_print_only_codes(capsys):
    cursor = FakeCursor(fail_on="M_SECRET")
    checks = [
        {"name": "alerts", "sql": "SELECT * FROM M_ALERTS WHERE X = ?", "binds": ["a"]},
        {"name": "secret", "sql": "SELECT * FROM M_SECRET", "binds": []},
    ]
    passed, failed = probe.run_checks(cursor, checks)
    out = capsys.readouterr().out
    assert (passed, failed) == (1, 1)
    assert cursor.executed[0] == (
        "SELECT TOP 1 * FROM (SELECT * FROM M_ALERTS WHERE X = ?)",
        ("a",),
    )
    assert "PASS alerts" in out
    assert "FAIL secret sql_code=258" in out
    assert "secret text" not in out
    assert "ROW-VALUE" not in out


def test_sql_code_of_other_exceptions_is_the_class():
    assert probe.sql_code_of(dbapi.Error(258, "x")) == "sql_code=258"
    assert probe.sql_code_of(RuntimeError("leak")) == "error=RuntimeError"


# ---- arguments -----------------------------------------------------------------------


def test_password_is_never_an_argument():
    with pytest.raises(SystemExit) as raised:
        probe.build_parser().parse_args(["--password", "x"])
    assert raised.value.code == 2
    with pytest.raises(SystemExit):
        probe.build_parser().parse_args(["--destination", "d", "--password", "x"])


def test_parser_defaults():
    args = probe.build_parser().parse_args(["--destination", "d"])
    assert args.variant == "both"
    assert args.encrypt == "both"
    assert args.timeout == 15.0
    assert args.no_version is False


def test_module_imports_nothing_from_agents():
    text = (ROOT / "scripts" / "probe_hana_connectivity.py").read_text()
    assert "from agents" not in text
    assert "import agents" not in text


# ---- report ----------------------------------------------------------------------------


def test_report_block_and_exit_code():
    facts = probe.Facts(
        dest_shape="http",
        proxy_variant="props",
        tls="on",
        token_seconds=3599,
        read_only=True,
        query_timeout=True,
        explain_plan=False,
        privileges="ok",
        version_ok=True,
    )
    assert probe.report_block(facts).splitlines() == [
        "DEST_SHAPE http",
        "PROXY_VARIANT props",
        "TLS on",
        "TOKEN_SECONDS 3599",
        "READ_ONLY yes",
        "QUERY_TIMEOUT yes",
        "EXPLAIN_PLAN no",
        "PRIVILEGES ok",
    ]
    assert probe.exit_code(facts) == 0
    facts.privileges = "too_wide"
    assert probe.exit_code(facts) == 1
    facts.privileges = "ok"
    facts.version_ok = False
    assert probe.exit_code(facts) == 1


def test_tls_fact():
    assert probe.tls_fact({("props", True): True}) == "on"
    assert probe.tls_fact({("props", True): False, ("props", False): True}) == "fails"
    assert probe.tls_fact({("props", False): True}) == "off"
    assert probe.tls_fact({}) == "off"


# ---- end to end on fakes ---------------------------------------------------------------


def _mock_transport(destination: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200, json={"access_token": "JWT-MUST-NOT-PRINT", "expires_in": 3599}
            )
        if "/destination-configuration/v1/destinations/" in request.url.path:
            return httpx.Response(200, json={"destinationConfiguration": destination})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class FakeConnection:
    def __init__(self, privileges, explain_fails=False):
        self.privileges = privileges
        self.explain_fails = explain_fails
        self.closed = False
        self.rolled_back = False
        self.statements: list[str] = []

    def setautocommit(self, value):
        self.autocommit = value

    def cursor(self):
        conn = self

        class Cursor:
            def __init__(self):
                self.result = []

            def setquerytimeout(self, seconds):
                conn.timeout = seconds

            def execute(self, sql, binds=()):
                conn.statements.append(sql)
                if "M_DATABASE" in sql:
                    self.result = [("TENANTDB", "2.00.079.00.1234567890")]
                elif "EFFECTIVE_PRIVILEGES" in sql:
                    self.result = list(conn.privileges)
                elif sql.startswith("EXPLAIN PLAN") and conn.explain_fails:
                    raise dbapi.Error(258, "insufficient privilege: secret text")
                elif "EXPLAIN_PLAN_TABLE" in sql:
                    self.result = [("ROW",)]
                elif "CURRENT_USER" in sql and "DUMMY" in sql:
                    self.result = [("PROBEUSER",)]
                else:
                    self.result = [(1,)]
                return True

            def fetchall(self):
                return self.result

            def fetchone(self):
                return self.result[0] if self.result else None

            def close(self):
                pass

        return Cursor()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeSocket:
    def close(self):
        pass


def _run(
    destination: dict, *, privileges, extra: list[str] | None = None, connect=None, capsys=None
):
    calls: list[dict] = []
    conn = FakeConnection(privileges)

    def fake_connect(**kwargs):
        calls.append(kwargs)
        if connect is not None:
            return connect(kwargs, conn)
        return conn

    args = probe.build_parser().parse_args(["--destination", "hana-dest", *(extra or [])])
    code = probe.run(
        args,
        {"VCAP_SERVICES": json.dumps(VCAP)},
        transport=_mock_transport(destination),
        open_socket=lambda host, port, timeout: FakeSocket(),
        handshake=lambda sock, token, loc, host, port: 0,
        connect=fake_connect,
    )
    return code, calls, conn


def test_full_run_on_the_http_shape(capsys):
    code, calls, conn = _run(
        HTTP_DESTINATION, privileges=[("CATALOG READ", None), ("MONITORING", None)]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    _assert_clean(out)
    assert "DEST_SHAPE http" in out
    assert "PROXY_VARIANT props" in out
    assert "TLS on" in out
    assert "TOKEN_SECONDS 3599" in out
    assert "PRIVILEGES ok" in out
    assert "READ_ONLY yes" in out
    assert "EXPLAIN_PLAN yes" in out
    assert "2.00.079.00.1234567890" in out
    assert "PASS socks5_handshake: reply=0" in out
    assert "User present: yes" in out
    assert "Password present: yes" in out
    assert "hana.database" in out
    # The DB password reached hdbcli and nowhere else.
    assert calls[0]["password"] == "DB-PASSWORD-MUST-NOT-PRINT"
    assert calls[0]["address"] == "vhost"
    assert calls[0]["port"] == 30013
    assert conn.rolled_back and conn.closed
    assert not any(s.upper().startswith("DELETE") for s in conn.statements)


def test_full_run_without_version_and_with_too_wide_privileges(capsys):
    code, _, _ = _run(
        {**HTTP_DESTINATION, "Type": "TCP", "Address": "vhost:30015", "URL": None},
        privileges=[("SELECT", "SAPHANADB")],
        extra=["--no-version", "--variant", "props-account", "--encrypt", "no"],
    )
    out = capsys.readouterr().out
    assert code == 1
    _assert_clean(out)
    assert "DEST_SHAPE tcp" in out
    assert "PRIVILEGES too_wide" in out
    assert "PROXY_VARIANT props-account" in out
    assert "TLS off" in out
    assert "2.00.079" not in out


def test_full_run_where_no_login_works_hides_the_driver_text(capsys):
    def refuse(kwargs, conn):
        raise dbapi.Error(10, "authentication failed: secret text")

    code, calls, _ = _run(HTTP_DESTINATION, privileges=[], connect=refuse)
    out = capsys.readouterr().out
    assert code == 1
    _assert_clean(out)
    assert "secret text" not in out
    assert "sql_code=10" in out
    assert "PROXY_VARIANT none" in out
    assert "TLS fails" in out
    assert "PRIVILEGES unknown" in out
    # props and props-account, each with encrypt yes and no
    assert len(calls) == 4


def test_a_destination_without_password_stops_before_an_attempt(capsys):
    dest = {k: v for k, v in HTTP_DESTINATION.items() if k != "Password"}
    code, calls, _ = _run(dest, privileges=[])
    out = capsys.readouterr().out
    assert code == 2
    assert calls == []
    assert "Password present: no" in out
    _assert_clean(out)


def test_a_cloud_destination_is_refused(capsys):
    code, calls, _ = _run({**HTTP_DESTINATION, "ProxyType": "Internet"}, privileges=[])
    assert code == 2
    assert calls == []
    _assert_clean(capsys.readouterr().out)


def test_namespace_type():
    assert isinstance(probe.build_parser().parse_args(["--destination", "d"]), argparse.Namespace)
