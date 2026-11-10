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
    assert probe.classify_privileges([("SELECT", "SYS"), ("SELECT", "_SYS_STATISTICS")]) == "ok"
    assert probe.classify_privileges([("SELECT", "PUBLIC")]) == "ok"
    assert probe.classify_privileges([("DELETE", "ZAPP")]) == "too_wide"
    # The user's own schema holds nothing of the system's data.
    assert probe.classify_privileges([("SELECT", "PROBEUSER")], own_schema="PROBEUSER") == "ok"
    assert probe.classify_privileges([]) == "unknown"


@pytest.mark.parametrize("schema", ["_SYS_BIC", "_SYS_BI", "_SYS_REPO", "_SYS_OTHER"])
def test_non_exempt_sys_schemas_need_review(schema):
    # _SYS_BIC (activated calculation views) and _SYS_BI can hold business data,
    # but default grants through PUBLIC / MONITORING may land there too: a human
    # judges them at the gate. No _SYS_* schema is exempt by its prefix alone.
    assert probe.classify_privileges([("SELECT", schema)]) == "review"


def test_broad_system_privileges_are_too_wide():
    assert probe.classify_privileges([("DATA ADMIN", None)]) == "too_wide"
    assert probe.classify_privileges([("data admin", None), ("MONITORING", None)]) == "too_wide"
    assert probe.classify_privileges([("CATALOG READ", None)]) == "ok"
    # too_wide wins over review
    assert (
        probe.classify_privileges([("SELECT", "_SYS_BI"), ("SELECT", "SAPHANADB")]) == "too_wide"
    )


def test_any_object_privilege_on_a_business_schema_is_too_wide():
    assert probe.classify_privileges([("CREATE ANY", "ZAPP")]) == "too_wide"
    assert probe.classify_privileges([("REFERENCES", "ZAPP", "OBJ")]) == "too_wide"


# Rows as EFFECTIVE_PRIVILEGES might carry them; the third column (an object
# name) must never reach the output.
MONITORING_ROWS = [
    ("MONITORING", None, None),
    ("CATALOG READ", None, None),
    ("SELECT", "SYS", "M_DATABASE"),
    ("SELECT", "_SYS_BI", "OBJ_NAME_MUST_NOT_PRINT_1"),
    ("SELECT", "_SYS_BI", "OBJ_NAME_MUST_NOT_PRINT_2"),
    ("EXECUTE", "_SYS_REPO", "OBJ_NAME_MUST_NOT_PRINT_3"),
    ("SELECT", "PROBEUSER", "OWN_TABLE"),
]


def test_monitoring_and_public_grants_on_sys_schemas_are_review(capsys):
    code, _, _ = _run(HTTP_DESTINATION, privileges=MONITORING_ROWS)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS privileges: review (rows=7, data_grants=3, broad_system=0)" in out
    assert "schema=_SYS_BI privileges=SELECT:2" in out
    assert "schema=_SYS_REPO privileges=EXECUTE:1" in out
    assert "PRIVILEGES review" in out
    assert "OBJ_NAME_MUST_NOT_PRINT" not in out
    assert "PROBEUSER" not in out
    assert "OWN_TABLE" not in out
    assert "schema=SYS " not in out


def test_a_business_schema_is_too_wide_and_named(capsys):
    rows = [*MONITORING_ROWS, ("SELECT", "SAPHANADB", "OBJ_NAME_MUST_NOT_PRINT_4")]
    rows.append(("INSERT", "SAPHANADB", "OBJ_NAME_MUST_NOT_PRINT_5"))
    code, _, _ = _run(HTTP_DESTINATION, privileges=rows)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL privileges: too_wide (rows=9, data_grants=5, broad_system=0)" in out
    assert "schema=SAPHANADB privileges=INSERT:1,SELECT:1" in out
    assert "OBJ_NAME_MUST_NOT_PRINT" not in out


def test_data_admin_is_too_wide_and_counted(capsys):
    code, _, _ = _run(HTTP_DESTINATION, privileges=[("DATA ADMIN", None, None)])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL privileges: too_wide (rows=1, data_grants=0, broad_system=1)" in out


def test_odd_schema_names_and_kinds_are_not_printed(capsys):
    rows = [("SELECT", "zsecret", "o"), ("SELECT", "Z-ODD", "o"), ("evil\nkind", "ZAPP", "o")]
    code, _, _ = _run(HTTP_DESTINATION, privileges=rows)
    out = capsys.readouterr().out
    assert code == 1
    assert "zsecret" not in out
    assert "Z-ODD" not in out
    assert "evil" not in out
    assert out.count("schema=<unprintable> privileges=SELECT:1") == 2
    assert "schema=ZAPP privileges=<unprintable>:1" in out


def test_schema_lines_are_capped_at_ten(capsys):
    rows = [("SELECT", f"_SYS_S{i:02d}", "o") for i in range(13)]
    code, _, _ = _run(HTTP_DESTINATION, privileges=rows)
    out = capsys.readouterr().out
    assert code == 0
    lines = [line for line in out.splitlines() if line.startswith("schema=")]
    assert [line.split()[0] for line in lines] == [f"schema=_SYS_S{i:02d}" for i in range(10)]
    assert "and 3 more" in out


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


def test_password_is_never_an_argument(capsys):
    with pytest.raises(SystemExit) as raised:
        probe.build_parser().parse_args(["--password", "x"])
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        probe.build_parser().parse_args(["--destination", "d", "--password", "hunter2"])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "hunter2" not in captured.err + captured.out
    assert "invalid arguments" in captured.err


def test_argument_errors_never_echo_values(capsys):
    for argv in (
        ["--destination", "d", "--variant", "s3cr3t-variant"],
        ["--destination", "d", "--timeout", "s3cr3t-timeout"],
        ["s3cr3t-positional"],
    ):
        with pytest.raises(SystemExit) as raised:
            probe.build_parser().parse_args(argv)
        assert raised.value.code == 2
        captured = capsys.readouterr()
        assert "s3cr3t" not in captured.err + captured.out


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
        explain_plan="no",
        privileges="ok",
        version_ok=True,
        schema_pin=True,
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
        "WRITE_REFUSED untested",
        "SCHEMA_PIN yes",
    ]
    assert probe.exit_code(facts) == 0
    facts.privileges = "review"
    assert probe.exit_code(facts) == 0
    facts.privileges = "too_wide"
    assert probe.exit_code(facts) == 1
    facts.privileges = "unknown"
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
    def __init__(self, privileges, explain_fails=False, read_only_fails=False,
                 write_refused=False, schema_fails=False, write_error=None,
                 insert_refused=None, insert_error=None, setup_fails=False):
        self.privileges = privileges
        self.explain_fails = explain_fails
        self.read_only_fails = read_only_fails
        self.write_refused = write_refused
        self.schema_fails = schema_fails
        # what the refused write raises (default: SQL code 7)
        self.write_error = write_error
        # the DML attempt: refused like the DDL one unless said otherwise
        self.insert_refused = write_refused if insert_refused is None else insert_refused
        self.insert_error = insert_error or write_error
        self.setup_fails = setup_fails
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
                elif sql.startswith("SET TRANSACTION") and conn.read_only_fails:
                    raise dbapi.Error(7, "feature not supported: secret text")
                elif sql.startswith("SET SCHEMA") and conn.schema_fails:
                    raise dbapi.Error(362, "invalid schema name: secret text")
                elif "CURRENT_SCHEMA" in sql:
                    self.result = [("PROBEUSER",)]
                elif sql.startswith("CREATE LOCAL TEMPORARY") and "AGENT_PROBE_RW" in sql:
                    if conn.setup_fails:
                        raise dbapi.Error(258, "insufficient privilege: secret text")
                elif sql.startswith("CREATE LOCAL TEMPORARY") and conn.write_refused:
                    raise conn.write_error or dbapi.Error(
                        7, "transaction is read-only: secret text")
                elif sql.startswith("INSERT") and conn.insert_refused:
                    raise conn.insert_error or dbapi.Error(
                        7, "transaction is read-only: secret text")
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
        self.statements.append("ROLLBACK")

    def close(self):
        self.closed = True


class FakeSocket:
    def close(self):
        pass


def _run(
    destination: dict,
    *,
    privileges,
    extra: list[str] | None = None,
    connect=None,
    read_only_fails: bool = False,
    write_refused: bool = False,
    schema_fails: bool = False,
    write_error: BaseException | None = None,
    **fake,
):
    calls: list[dict] = []
    conn = FakeConnection(privileges, read_only_fails=read_only_fails,
                          write_refused=write_refused, schema_fails=schema_fails,
                          write_error=write_error, **fake)

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
    # EXPLAIN PLAN writes plan rows for the session: only with --explain-plan.
    assert "EXPLAIN_PLAN untested" in out
    assert "SKIP explain_plan: no --explain-plan" in out
    assert not any(x.startswith("EXPLAIN") for x in conn.statements)
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


# ---- fix round 1 ---------------------------------------------------------------------------


def test_read_only_starts_a_fresh_transaction(capsys):
    _, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                      extra=["--explain-plan"])
    capsys.readouterr()
    s = conn.statements
    database = next(i for i, x in enumerate(s) if "M_DATABASE" in x)
    read_only = s.index("SET TRANSACTION READ ONLY")
    # S7 opened a transaction (autocommit off): it is rolled back right before S8,
    # so S8 (schema pin, then read-only, the app's order) opens the transaction
    # S9-S11 then run in.
    assert database < read_only
    assert s[read_only - 2:read_only] == ["ROLLBACK", "SET SCHEMA SYS"]
    between = s[read_only + 1 :]
    assert any("EXPLAIN PLAN" in x for x in between)
    explain = next(i for i, x in enumerate(s) if x.startswith("EXPLAIN PLAN"))
    assert "ROLLBACK" not in s[read_only:explain]


def test_checks_are_skipped_when_the_transaction_is_not_read_only(tmp_path, capsys):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps([{"name": "alerts", "sql": "SELECT * FROM M_ALERTS"}]))
    _, _, conn = _run(
        HTTP_DESTINATION,
        privileges=[("MONITORING", None)],
        extra=["--checks-file", str(checks)],
        read_only_fails=True,
    )
    out = capsys.readouterr().out
    assert "FAIL read_only: sql_code=7" in out
    assert "SKIP checks: not read-only" in out
    assert "READ_ONLY no" in out
    assert not any("M_ALERTS" in x for x in conn.statements)
    assert "secret text" not in out


def test_checks_run_in_the_read_only_transaction(tmp_path, capsys):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps([{"name": "alerts", "sql": "SELECT * FROM M_ALERTS"}]))
    _, _, conn = _run(
        HTTP_DESTINATION, privileges=[("MONITORING", None)], extra=["--checks-file", str(checks)]
    )
    out = capsys.readouterr().out
    assert "PASS alerts" in out
    assert "PASS checks: passed=1 failed=0" in out
    assert "SELECT TOP 1 * FROM (SELECT * FROM M_ALERTS)" in conn.statements


def test_a_missing_hdbcli_stops_before_any_attempt(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "hdbcli", None)
    monkeypatch.setitem(sys.modules, "hdbcli.dbapi", None)
    args = probe.build_parser().parse_args(["--destination", "hana-dest"])
    code = probe.run(
        args,
        {"VCAP_SERVICES": json.dumps(VCAP)},
        transport=_mock_transport(HTTP_DESTINATION),
        open_socket=lambda host, port, timeout: FakeSocket(),
        handshake=lambda sock, token, loc, host, port: 0,
    )
    out = capsys.readouterr().out
    assert code == 2
    assert out.count("FAIL hdbcli: not importable") == 1
    assert "ImportError" not in out
    _assert_clean(out)


@pytest.mark.parametrize("host", ["::1", "fe80::1", "a" * 64 + ".example"])
def test_unencodable_hosts_are_a_probe_error(host):
    with pytest.raises(probe.ProbeError):
        probe.socks5_connect_request(host, 30013)


# ---- final review: the write test --------------------------------------------------------


def test_schema_pin_comes_before_read_only_and_the_write_test_is_off_by_default(capsys):
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS schema_pin: accepted" in out
    assert "PASS read_only: accepted" in out
    assert "SKIP write_refused: no --write-test" in out
    assert "WRITE_REFUSED untested" in out
    assert "WRITE_REFUSED no" not in out
    assert not any(x.startswith("CREATE") for x in conn.statements)
    assert not any("CURRENT_SCHEMA" in x or x.startswith("INSERT") for x in conn.statements)
    s = conn.statements
    assert s.index("SET SCHEMA SYS") < s.index("SET TRANSACTION READ ONLY")
    assert out.index("schema_pin") < out.index("read_only:") < out.index("write_refused")


def test_parser_write_test_flag():
    assert probe.build_parser().parse_args(["--destination", "d"]).write_test is False
    assert probe.build_parser().parse_args(
        ["--destination", "d", "--write-test"]).write_test is True
    assert probe.build_parser().parse_args(["--destination", "d"]).explain_plan is False
    assert probe.build_parser().parse_args(
        ["--destination", "d", "--explain-plan"]).explain_plan is True


def test_a_refused_write_passes_inside_the_read_only_transaction(tmp_path, capsys):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps([{"name": "alerts", "sql": "SELECT * FROM M_ALERTS"}]))
    code, _, conn = _run(
        HTTP_DESTINATION, privileges=[("MONITORING", None)], write_refused=True,
        extra=["--write-test", "--checks-file", str(checks)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS write_setup: accepted" in out
    assert "PASS write_refused_ddl: sql_code=7" in out
    assert "PASS write_refused_dml: sql_code=7" in out
    assert "WRITE_REFUSED yes" in out
    assert "READ_ONLY yes" in out
    assert "secret text" not in out and "PROBEUSER" not in out
    _assert_clean(out)
    s = conn.statements
    read_only = s.index("SET TRANSACTION READ ONLY")
    create = s.index("CREATE LOCAL TEMPORARY TABLE #AGENT_PROBE_RO (I INTEGER)")
    assert create == read_only + 1  # same transaction, nothing in between
    # the DML target is made before the read-only transaction, in the user's
    # own schema, and dropped once the steps are done
    setup = s.index('CREATE LOCAL TEMPORARY TABLE "PROBEUSER".#AGENT_PROBE_RW (I INTEGER)')
    assert s.index("SELECT CURRENT_SCHEMA FROM SYS.DUMMY") < setup
    assert setup < s.index("ROLLBACK") < s.index("SET SCHEMA SYS")
    assert s[create + 1] == 'INSERT INTO "PROBEUSER".#AGENT_PROBE_RW VALUES (1)'
    assert not any(x == "DROP TABLE #AGENT_PROBE_RO" for x in s)
    assert s[-3:] == ["ROLLBACK", 'DROP TABLE "PROBEUSER".#AGENT_PROBE_RW', "ROLLBACK"]
    assert out.index("PASS checks") < out.index("PASS write_cleanup: dropped")
    assert "PASS checks: passed=1 failed=0" in out


def test_an_accepted_write_fails_is_dropped_and_rolled_back(tmp_path, capsys):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps([{"name": "alerts", "sql": "SELECT * FROM M_ALERTS"}]))
    code, _, conn = _run(
        HTTP_DESTINATION, privileges=[("MONITORING", None)],
        extra=["--write-test", "--checks-file", str(checks)])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL write_refused_ddl: the write was accepted" in out
    assert "SKIP write_refused_dml: write was accepted" in out
    assert "WRITE_REFUSED no" in out
    # The transaction was not read-only after all: no later SQL step runs.
    assert "READ_ONLY no" in out
    for step in ("query_timeout", "privileges", "explain_plan", "checks"):
        assert f"SKIP {step}: write was accepted" in out
    assert "EXPLAIN_PLAN untested" in out and "QUERY_TIMEOUT no" in out
    _assert_clean(out)
    s = conn.statements
    create = s.index("CREATE LOCAL TEMPORARY TABLE #AGENT_PROBE_RO (I INTEGER)")
    assert s[create + 1:] == ["DROP TABLE #AGENT_PROBE_RO", "ROLLBACK", "ROLLBACK",
                              'DROP TABLE "PROBEUSER".#AGENT_PROBE_RW', "ROLLBACK"]
    assert not any("M_ALERTS" in x for x in s)


def test_an_accepted_write_stops_the_explain_plan_too(capsys):
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         extra=["--write-test", "--explain-plan"])
    out = capsys.readouterr().out
    assert code == 1
    assert "SKIP explain_plan: write was accepted" in out
    assert not any(x.startswith("EXPLAIN") for x in conn.statements)


def test_explain_plan_runs_only_with_its_flag(capsys):
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         extra=["--explain-plan"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS explain_plan: rows=1" in out and "EXPLAIN_PLAN yes" in out
    assert any(x.startswith("EXPLAIN PLAN") for x in conn.statements)


def test_write_refused_values_in_the_report():
    facts = probe.Facts()
    assert "WRITE_REFUSED untested" in probe.report_block(facts)
    assert "EXPLAIN_PLAN untested" in probe.report_block(facts)
    facts.write_refused = "yes"
    assert "WRITE_REFUSED yes" in probe.report_block(facts)
    facts.write_refused, facts.version_ok, facts.privileges = "no", True, "ok"
    assert probe.exit_code(facts) == 1


def test_the_write_test_is_skipped_when_read_only_was_refused(capsys):
    _, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                      read_only_fails=True, extra=["--write-test"])
    out = capsys.readouterr().out
    assert "SKIP write_refused: not read-only" in out
    assert not any(x.startswith("CREATE LOCAL TEMPORARY TABLE #AGENT_PROBE_RO")
                   or x.startswith("INSERT") for x in conn.statements)


def test_no_login_skips_the_new_steps_too(capsys):
    def refuse(kwargs, conn):
        raise dbapi.Error(10, "authentication failed: secret text")

    _run(HTTP_DESTINATION, privileges=[], connect=refuse, extra=["--write-test"])
    out = capsys.readouterr().out
    assert "SKIP schema_pin: no login" in out
    assert "SKIP write_refused: no login" in out


# ---- final review round 2: only a read-only refusal passes; the schema pin gates --------


def test_the_read_only_refusal_codes_start_as_one_candidate():
    assert probe.READ_ONLY_REFUSAL_CODES == frozenset({7})
    assert all(isinstance(c, int) for c in probe.READ_ONLY_REFUSAL_CODES)


@pytest.mark.parametrize("error,shown", [
    (dbapi.Error(257, "sql syntax error: secret text"), "sql_code=257"),
    (dbapi.Error(258, "insufficient privilege: secret text"), "sql_code=258"),
    (RuntimeError("secret text"), "error=RuntimeError"),
])
def test_another_refusal_of_the_write_is_inconclusive(tmp_path, capsys, error, shown):
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         write_refused=True, write_error=error, extra=["--write-test"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert f"INCONCLUSIVE write_refused_ddl: {shown}" in out
    assert f"INCONCLUSIVE write_refused_dml: {shown}" in out
    assert "PASS write_refused" not in out
    assert "WRITE_REFUSED inconclusive" in out
    assert "secret text" not in out
    _assert_clean(out)


def test_inconclusive_in_the_report_exits_non_zero():
    facts = probe.Facts(version_ok=True, privileges="ok", schema_pin=True)
    assert probe.exit_code(facts) == 0
    facts.write_refused = "inconclusive"
    assert "WRITE_REFUSED inconclusive" in probe.report_block(facts)
    assert probe.exit_code(facts) == 1


def test_a_failed_schema_pin_stops_the_write_test_and_the_checks(tmp_path, capsys):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps([{"name": "alerts", "sql": "SELECT * FROM M_ALERTS"}]))
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         schema_fails=True, write_refused=True,
                         extra=["--write-test", "--checks-file", str(checks)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FAIL schema_pin: sql_code=362" in out
    assert "SKIP write_refused: schema pin failed" in out
    assert "SKIP checks: schema pin failed" in out
    assert out.splitlines()[-1] == "SCHEMA_PIN no"
    assert not any(x.startswith("CREATE LOCAL TEMPORARY TABLE #AGENT_PROBE_RO")
                   or x.startswith("INSERT") for x in conn.statements)
    assert 'DROP TABLE "PROBEUSER".#AGENT_PROBE_RW' in conn.statements  # the setup is undone
    assert not any("M_ALERTS" in x for x in conn.statements)
    assert "secret text" not in out


def test_schema_pin_yes_is_the_last_key_of_a_good_run(capsys):
    code, _, _ = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.splitlines()[-1] == "SCHEMA_PIN yes"


def test_a_read_only_refused_after_a_passing_schema_pin_is_legible(capsys):
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         read_only_fails=True, extra=["--write-test"])
    out = capsys.readouterr().out
    assert out.index("PASS schema_pin: accepted") < out.index("FAIL read_only: sql_code=7")
    s = conn.statements
    assert s.index("SET SCHEMA SYS") + 1 == s.index("SET TRANSACTION READ ONLY")


def test_no_login_reports_no_schema_pin(capsys):
    def refuse(kwargs, conn):
        raise dbapi.Error(10, "authentication failed: secret text")

    code, _, _ = _run(HTTP_DESTINATION, privileges=[], connect=refuse)
    out = capsys.readouterr().out
    assert code == 1
    assert "SCHEMA_PIN no" in out


# ---- final review round 3: a DML attempt beside the DDL one ------------------------------


def test_a_refused_ddl_with_an_accepted_insert_fails(tmp_path, capsys):
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps([{"name": "alerts", "sql": "SELECT * FROM M_ALERTS"}]))
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         write_refused=True, insert_refused=False,
                         extra=["--write-test", "--checks-file", str(checks)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "PASS write_refused_ddl: sql_code=7" in out
    assert "FAIL write_refused_dml: the write was accepted" in out
    assert "WRITE_REFUSED no" in out and "READ_ONLY no" in out
    for step in ("query_timeout", "privileges", "explain_plan", "checks"):
        assert f"SKIP {step}: write was accepted" in out
    s = conn.statements
    insert = s.index('INSERT INTO "PROBEUSER".#AGENT_PROBE_RW VALUES (1)')
    assert s[insert + 1] == "ROLLBACK"  # the row is undone at once
    assert 'DROP TABLE "PROBEUSER".#AGENT_PROBE_RW' in s[insert:]
    assert not any("M_ALERTS" in x for x in s)
    _assert_clean(out)


@pytest.mark.parametrize("ddl,dml", [(None, 258), (258, None)])
def test_one_attempt_refused_for_another_reason_is_inconclusive(capsys, ddl, dml):
    def error(code):
        return None if code is None else dbapi.Error(code, "other: secret text")

    code, _, _ = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                      write_refused=True, write_error=error(ddl),
                      insert_refused=True, insert_error=error(dml) or dbapi.Error(7, "ro"),
                      extra=["--write-test"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "WRITE_REFUSED inconclusive" in out
    assert "INCONCLUSIVE write_refused_" in out and "PASS write_refused_" in out
    assert "secret text" not in out


def test_a_failed_setup_skips_the_insert_and_is_inconclusive(capsys):
    code, _, conn = _run(HTTP_DESTINATION, privileges=[("MONITORING", None)],
                         write_refused=True, setup_fails=True, extra=["--write-test"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "FAIL write_setup: sql_code=258" in out
    assert "PASS write_refused_ddl: sql_code=7" in out
    assert "SKIP write_refused_dml: setup failed" in out
    assert "WRITE_REFUSED inconclusive" in out
    assert not any(x.startswith("INSERT") or x.startswith("DROP") for x in conn.statements)
    assert "secret text" not in out
