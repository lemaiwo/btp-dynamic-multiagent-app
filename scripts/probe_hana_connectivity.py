"""Connectivity probe for an on-premise SAP HANA through the Cloud Connector (phase 0
of ``builtin:hana``).

Settles, before the toolset is built on it, what only a landscape can answer:
does ``hdbcli`` reach the HANA SQL port of a Cloud Connector virtual host
through the connectivity service's SOCKS5 proxy, as the technical database user
held in a BTP destination, and what may that user do once it is in?

Standalone on purpose: stdlib, ``httpx`` and ``hdbcli`` only, nothing imported
from this app, so the file can be copied into (or piped to) a running app
container of any release. It reads the bindings from ``VCAP_SERVICES``, resolves
the destination with the application's own token and runs read statements only.
It writes nothing anywhere (``EXPLAIN PLAN`` fills the session's own plan table
inside a read-only transaction that is rolled back; that is the A5 question).

Usage (inside the app container, with the app's Python):

    python probe_hana_connectivity.py --destination <name> \
        [--variant props|props-account|both] [--encrypt yes|no|both] \
        [--checks-file f.json] [--timeout 15] [--no-version]

The database password is read from the resolved destination (``User`` /
``Password``), never from a prompt or an argument. Two destination shapes are
read:

  * ``http``: ``Type HTTP``, ``URL http://<virtual host>:<port>``, ``ProxyType
    OnPremise``, ``BasicAuthentication`` with ``User`` / ``Password``; the URL is
    a carrier for host and port only (nothing is sent over HTTP);
  * ``tcp``: ``Type TCP``, ``Address <virtual host>:<port>``, ``ProxyType
    OnPremise``, ``User`` / ``Password`` as additional properties.

Optional destination properties: ``hana.database`` (tenant ``databaseName``),
``hana.sslHostNameInCertificate`` and ``hana.sslTrustStore`` (PEM text).

Steps, one line each, ``PASS|FAIL|SKIP <step>: <fixed words>``:

  S1 bindings (names only)          S7  SELECT ... FROM M_DATABASE (rows, version)
  S2 destination (facts only)       S8  SET TRANSACTION READ ONLY
  S3 connectivity token (lifetime)  S9  setquerytimeout(20) + SYS.DUMMY
  S4 TCP connect to the SOCKS5 port S10 EFFECTIVE_PRIVILEGES of CURRENT_USER
  S5 raw SOCKS5 handshake (0x80)    S11 EXPLAIN PLAN in the read-only transaction
  S6 hdbcli login per variant/TLS   S12 the statements of --checks-file

Variants (``hdbcli`` connection properties, SAP HANA Client SQLDBC properties):
``props`` = ``proxyHostname``, ``proxyPort``, ``proxyUserName`` (the
connectivity token) and, with a location id, ``proxyPassword`` =
``"1." + base64(location id)``; ``props-account`` = the same with the location id
in ``proxyScpAccount`` (plain text) instead of ``proxyPassword`` (skipped when
the destination has no ``CloudConnectorLocationId``).

What it prints: step results, yes/no facts, property NAMES, HANA's version
string (``--no-version`` hides it), row counts, SOCKS5 reply codes and SQL error
codes. Never a credential, a token, a host from a binding or a destination, a
row, or the text of a driver error (an error is reported by its SQL code or its
exception class).

The final block holds one fixed value per key: ``DEST_SHAPE``,
``PROXY_VARIANT``, ``TLS``, ``TOKEN_SECONDS``, ``READ_ONLY``, ``QUERY_TIMEOUT``,
``EXPLAIN_PLAN``, ``PRIVILEGES``. ``QUERY_TIMEOUT yes`` says the driver
accepted the timeout and ran a statement under it, not that it fired.

Exit code: 0 when S7 passed and ``PRIVILEGES`` is ``ok``, 1 otherwise, 2 when
the probe could not get as far as an attempt.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

try:
    import httpx
except ImportError:  # pragma: no cover - the script may run outside the venv
    sys.exit("httpx is required: run this with the app's Python")

VARIANTS = ("props", "props-account")
SOCKS5_GREETING = b"\x05\x01\x80"  # version 5, one method, 0x80 (Cloud Connector JWT)
CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z._ ()-]{1,64}$")
MAX_CHECKS = 200
QUERY_TIMEOUT_SECONDS = 20
# Object privileges that read or change data: on a non-system schema they make
# the technical user too wide (assumption A7).
DATA_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "EXECUTE", "ALTER", "DROP", "CREATE ANY",
     "INDEX", "TRIGGER", "DEBUG", "UNMASKED"}
)
# Exempt by exact name only: no _SYS_* prefix rule, because _SYS_BIC (activated
# calculation views) and _SYS_BI can hold business data, and _SYS_REPO's
# procedures grant roles.
SYSTEM_SCHEMAS = frozenset({"SYS", "PUBLIC", "_SYS_STATISTICS"})
# System privileges (no schema) that reach business data of every schema.
# DATA ADMIN authorises reading all data in the system views and any DDL.
BROAD_SYSTEM_PRIVILEGES = frozenset({"DATA ADMIN"})


class ProbeError(Exception):
    """A failure the operator can act on; its text never holds a secret."""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    token_url: str = field(repr=False)


@dataclass(frozen=True)
class DestinationBinding:
    oauth: OAuthClient
    uri: str = field(repr=False)


@dataclass(frozen=True)
class ConnectivityBinding:
    oauth: OAuthClient
    proxy_host: str = field(repr=False)
    socks5_port: int


@dataclass(frozen=True)
class Bindings:
    destination: DestinationBinding
    connectivity: ConnectivityBinding


@dataclass
class Facts:
    """The final block; filled step by step."""

    dest_shape: str = "unknown"
    proxy_variant: str = "none"
    tls: str = "off"
    token_seconds: int = 0
    read_only: bool = False
    query_timeout: bool = False
    explain_plan: bool = False
    privileges: str = "unknown"
    version_ok: bool = False


# ---- output -------------------------------------------------------------------


def say(result: str, step: str, words: str) -> None:
    print(f"{result} {step}: {words}")


def _yes(value: object) -> str:
    return "yes" if value else "no"


def sql_code_of(exc: BaseException) -> str:
    """``sql_code=<n>`` for a driver error, else the class name. Never the text."""
    code = getattr(exc, "errorcode", None)
    if not isinstance(code, int) and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    if isinstance(code, int) and not isinstance(code, bool):
        return f"sql_code={code}"
    return f"error={type(exc).__name__}"


# ---- S1 bindings ----------------------------------------------------------------


def _token_url(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/oauth/token") else base + "/oauth/token"


def _credentials(services: Mapping[str, object], label: str) -> dict:
    instances = services.get(label)
    if not isinstance(instances, list) or not instances:
        raise ProbeError(f"{label}: no binding in VCAP_SERVICES")
    creds = instances[0].get("credentials") if isinstance(instances[0], dict) else None
    if not isinstance(creds, dict):
        raise ProbeError(f"{label}: the binding has no credentials")
    return creds


def _oauth(creds: dict, label: str, *url_keys: str) -> OAuthClient:
    base = next((creds[k] for k in url_keys if creds.get(k)), "")
    if not (creds.get("clientid") and creds.get("clientsecret") and base):
        raise ProbeError(f"{label}: no clientid/clientsecret/url (x509 is not supported)")
    return OAuthClient(str(creds["clientid"]), str(creds["clientsecret"]), _token_url(str(base)))


def _port(value: object) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return 0
    return number if 1 <= number <= 65535 else 0


def read_bindings(vcap_services: str) -> Bindings:
    """Parse ``VCAP_SERVICES`` into the destination and connectivity bindings."""
    if not (vcap_services or "").strip():
        raise ProbeError("bindings: VCAP_SERVICES is empty, run inside the app container")
    try:
        services = json.loads(vcap_services)
    except ValueError:
        raise ProbeError("bindings: VCAP_SERVICES is not JSON") from None
    if not isinstance(services, dict):
        raise ProbeError("bindings: VCAP_SERVICES is not a JSON object")

    dest = _credentials(services, "destination")
    if not dest.get("uri"):
        raise ProbeError("destination: the binding has no uri")
    conn = _credentials(services, "connectivity")
    if not conn.get("onpremise_proxy_host"):
        raise ProbeError("proxy_host: not in the connectivity binding")
    socks5 = _port(conn.get("onpremise_socks5_proxy_port"))
    if not socks5:
        raise ProbeError("socks5_port: not in the connectivity binding")
    return Bindings(
        destination=DestinationBinding(
            _oauth(dest, "destination", "url"), str(dest["uri"]).rstrip("/")
        ),
        connectivity=ConnectivityBinding(
            _oauth(conn, "connectivity", "token_service_url", "url"),
            str(conn["onpremise_proxy_host"]),
            socks5,
        ),
    )


def describe_bindings(cfg: Bindings) -> None:
    """Names only: no id, secret, host or port."""
    say("PASS", "bindings", "destination found, connectivity found with a SOCKS5 port")


# ---- S2 destination ------------------------------------------------------------------


def split_address(address: str) -> tuple[str, int]:
    """``host:port`` with a port in 1..65535; refused otherwise (value not echoed)."""
    host, sep, port = str(address or "").rpartition(":")
    number = _port(port) if port.isdigit() else 0
    if not sep or not host or not number:
        raise ProbeError("address: not <host>:<port> with a port in 1..65535")
    return host, number


def destination_shape(props: Mapping[str, object]) -> str:
    """``tcp`` (Type TCP + Address), ``http`` (Type HTTP + URL http://h:p), else ``unknown``."""
    kind = str(props.get("Type") or "").upper()
    if kind == "TCP" and props.get("Address"):
        try:
            split_address(str(props["Address"]))
        except ProbeError:
            return "unknown"
        return "tcp"
    if kind == "HTTP" and props.get("URL"):
        try:
            parts = urllib.parse.urlsplit(str(props["URL"]))
            port = parts.port
        except ValueError:
            return "unknown"
        if parts.scheme.lower() == "http" and parts.hostname and port:
            return "http"
    return "unknown"


def target_of(props: Mapping[str, object], shape: str) -> tuple[str, int]:
    """The virtual host and SQL port the destination names."""
    if shape == "tcp":
        return split_address(str(props["Address"]))
    if shape == "http":
        parts = urllib.parse.urlsplit(str(props["URL"]))
        if parts.hostname and parts.port:
            return parts.hostname, parts.port
    raise ProbeError("destination: no Type TCP + Address and no Type HTTP + URL http://<host>:<port>")


def _token(client: httpx.Client, oauth: OAuthClient, what: str) -> tuple[str, int]:
    try:
        response = client.post(
            oauth.token_url,
            data={"grant_type": "client_credentials"},
            auth=(oauth.client_id, oauth.client_secret),
        )
    except httpx.HTTPError as exc:
        raise ProbeError(f"{what}: {type(exc).__name__}") from None
    token, expires, code = None, 0, ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            token = payload.get("access_token")
            expires = payload.get("expires_in") or 0
            code = str(payload.get("error") or "")[:40]
    except ValueError:
        pass
    if response.status_code != 200 or not isinstance(token, str) or not token:
        detail = f"HTTP {response.status_code}" + (f" {code}" if code.isidentifier() else "")
        raise ProbeError(f"{what}: {detail}")
    try:
        seconds = int(expires)
    except (TypeError, ValueError):
        seconds = 0
    return token, seconds


def find_destination(client: httpx.Client, binding: DestinationBinding, name: str) -> dict:
    token, _ = _token(client, binding.oauth, "destination: service token")
    url = (
        f"{binding.uri}/destination-configuration/v1/destinations/"
        f"{urllib.parse.quote(name, '')}"
    )
    try:
        response = client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ProbeError(f"destination: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise ProbeError(f"destination: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise ProbeError("destination: the answer is not JSON") from None
    cfg = payload.get("destinationConfiguration") if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        raise ProbeError("destination: no destinationConfiguration in the answer")
    return cfg


def hana_properties(props: Mapping[str, object]) -> dict[str, str]:
    return {
        k: str(v) for k, v in props.items() if k.startswith("hana.") and isinstance(v, str) and v
    }


def _word(value: object) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9_]{1,40}", text) else "-"


def describe_destination(props: Mapping[str, object], shape: str) -> None:
    """Facts only: Type and ProxyType as values, everything else as yes/no or names."""
    say(
        "PASS",
        "destination",
        f"Type {_word(props.get('Type'))}, ProxyType {_word(props.get('ProxyType'))}, "
        f"shape {shape}",
    )
    print(f"DESTINATION User present: {_yes(props.get('User'))}")
    print(f"DESTINATION Password present: {_yes(props.get('Password'))}")
    location = props.get("CloudConnectorLocationId")
    print(f"DESTINATION CloudConnectorLocationId present: {_yes(location)}")
    print(f"DESTINATION Address present: {_yes(props.get('Address'))}")
    print(f"DESTINATION URL present: {_yes(props.get('URL'))}")
    names = sorted(k for k in props if k.startswith("hana.") and re.fullmatch(r"[\w.]{1,64}", k))
    print(f"DESTINATION hana.* properties: {', '.join(names) or '-'}")


# ---- S4/S5 SOCKS5 ----------------------------------------------------------------------


def socks5_auth_packet(token: str, location_id: str) -> bytes:
    """The Cloud Connector authentication request (method 0x80).

    ``[version 1][4-byte JWT length][JWT][1-byte location id length][location
    id, base64]`` (BTP Connectivity, "Using the TCP Protocol for Cloud
    Applications"); no location id is a length of 0 and nothing after it.
    """
    jwt = token.encode()
    loc = base64.b64encode(location_id.encode()) if location_id else b""
    if len(loc) > 255:
        raise ProbeError("socks5: the location id is too long")
    return b"\x01" + len(jwt).to_bytes(4, "big") + jwt + bytes([len(loc)]) + loc


def socks5_connect_request(host: str, port: int) -> bytes:
    """RFC 1928 CONNECT: an IPv4 literal as type 1, any other name as type 3."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv4Address):
        target = b"\x01" + address.packed
    elif address is not None or ":" in host:
        raise ProbeError("socks5: the host is not a domain name or IPv4 address")
    else:
        # The proxy takes DOMAIN and IPv4 only; an IPv6 literal or a label over
        # 63 characters does not encode, and must not end the run.
        try:
            name = host.encode("idna")
        except UnicodeError:
            raise ProbeError("socks5: the host is not a domain name or IPv4 address") from None
        if len(name) > 255:
            raise ProbeError("socks5: the host name is too long")
        target = b"\x03" + bytes([len(name)]) + name
    return b"\x05\x01\x00" + target + port.to_bytes(2, "big")


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ProbeError("socks5: the proxy closed the connection")
        data += chunk
    return data


def socks5_handshake(
    sock: socket.socket, token: str, location_id: str, host: str, port: int
) -> int:
    """Greeting, 0x80 authentication and CONNECT; answers the CONNECT reply code."""
    sock.sendall(SOCKS5_GREETING)
    version, method = _recv_exact(sock, 2)
    if version != 5 or method != 0x80:
        raise ProbeError(f"socks5: method not accepted (reply {version}/{method})")
    sock.sendall(socks5_auth_packet(token, location_id))
    auth_version, status = _recv_exact(sock, 2)
    if auth_version != 1 or status != 0:
        raise ProbeError(f"socks5: authentication refused (status {status})")
    sock.sendall(socks5_connect_request(host, port))
    head = _recv_exact(sock, 4)
    if head[1] == 0:  # read the bound address so the stream is clean
        if head[3] == 1:
            _recv_exact(sock, 6)
        elif head[3] == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
        elif head[3] == 4:
            _recv_exact(sock, 18)
    return head[1]


def open_tcp(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


# ---- S6 hdbcli --------------------------------------------------------------------------


def connect_kwargs(
    variant: str,
    *,
    proxy_host: str,
    proxy_port: int,
    token: str,
    location_id: str,
    host: str,
    port: int,
    user: str,
    password: str,
    encrypt: bool,
    hana_props: Mapping[str, str],
    timeout: float,
) -> dict:
    """The keyword set for ``hdbcli.dbapi.connect``. Never printed: see printable_kwargs."""
    if variant not in VARIANTS:
        raise ValueError("unknown variant")
    kwargs: dict = {
        "address": host,
        "port": port,
        "user": user,
        "password": password,
        "proxyHostname": proxy_host,
        "proxyPort": str(proxy_port),
        "proxyUserName": token,
        "connectTimeout": str(int(timeout * 1000)),
        "encrypt": "true" if encrypt else "false",
    }
    if variant == "props-account":
        if not location_id:
            raise ProbeError("props-account: the destination has no location id")
        kwargs["proxyScpAccount"] = location_id
    elif location_id:
        kwargs["proxyPassword"] = "1." + base64.b64encode(location_id.encode()).decode()
    if encrypt:
        kwargs["sslValidateCertificate"] = "true"
        if hana_props.get("hana.sslHostNameInCertificate"):
            kwargs["sslHostNameInCertificate"] = hana_props["hana.sslHostNameInCertificate"]
        if hana_props.get("hana.sslTrustStore"):
            kwargs["sslTrustStore"] = hana_props["hana.sslTrustStore"]
    if hana_props.get("hana.database"):
        kwargs["databaseName"] = hana_props["hana.database"]
    return kwargs


def printable_kwargs(kwargs: Mapping[str, object]) -> dict[str, str]:
    return {key: "***" for key in kwargs}


def tls_fact(logins: Mapping[tuple[str, bool], bool]) -> str:
    """``on`` when an encrypted login worked, ``fails`` when one was tried and none worked."""
    tried = [ok for (_, enc), ok in logins.items() if enc]
    if any(tried):
        return "on"
    if tried:
        return "fails"
    return "off"


# ---- S10 privileges -------------------------------------------------------------------------


def classify_privileges(
    rows: Iterable[tuple[object, object]], own_schema: str | None = None
) -> str:
    """``too_wide`` when a data privilege names a schema outside ``SYSTEM_SCHEMAS``
    or a system privilege of ``BROAD_SYSTEM_PRIVILEGES`` is held; ``ok`` else.

    The user's own schema is exempt: it holds nothing of the system's data. No
    rows at all is ``unknown``. Only these fixed words leave this function.
    """
    rows = list(rows)
    if not rows:
        return "unknown"
    for privilege, schema in rows:
        name = str(privilege or "").upper()
        if schema is None:
            if name in BROAD_SYSTEM_PRIVILEGES:
                return "too_wide"
            continue
        if name not in DATA_PRIVILEGES:
            continue
        schema = str(schema)
        if schema in SYSTEM_SCHEMAS or (own_schema and schema == own_schema):
            continue
        return "too_wide"
    return "ok"


# ---- S12 checks --------------------------------------------------------------------------


def load_checks(path: str) -> list[dict]:
    """``[{"name", "sql", "binds"?}]``; refused with fixed words, never the content."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise ProbeError("checks_file: not readable as JSON") from None
    if not isinstance(data, list) or not 1 <= len(data) <= MAX_CHECKS:
        raise ProbeError(f"checks_file: not a list of 1..{MAX_CHECKS} checks")
    checks = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or set(item) - {"name", "sql", "binds"}:
            raise ProbeError(f"checks_file: check {i} has unknown keys")
        name, sql, binds = item.get("name"), item.get("sql"), item.get("binds", [])
        if not isinstance(name, str) or not CHECK_NAME_RE.fullmatch(name):
            raise ProbeError(f"checks_file: check {i} name is not [A-Za-z0-9_.-]{{1,64}}")
        if not isinstance(sql, str) or not sql.strip():
            raise ProbeError(f"checks_file: check {i} has no sql")
        if not isinstance(binds, list) or not all(
            isinstance(b, (str, int, float)) and not isinstance(b, bool) for b in binds
        ):
            raise ProbeError(f"checks_file: check {i} binds are not a list of scalars")
        checks.append({"name": name, "sql": sql, "binds": list(binds)})
    return checks


def wrap_check(sql: str) -> str:
    return f"SELECT TOP 1 * FROM ({sql})"


def run_checks(cursor, checks: list[dict]) -> tuple[int, int]:
    passed = failed = 0
    for check in checks:
        try:
            cursor.execute(wrap_check(check["sql"]), tuple(check["binds"]))
            cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 - code or class only
            failed += 1
            print(f"FAIL {check['name']} {sql_code_of(exc)}")
        else:
            passed += 1
            print(f"PASS {check['name']}")
    return passed, failed


# ---- report -------------------------------------------------------------------------------


def report_block(facts: Facts) -> str:
    return "\n".join(
        [
            f"DEST_SHAPE {facts.dest_shape}",
            f"PROXY_VARIANT {facts.proxy_variant}",
            f"TLS {facts.tls}",
            f"TOKEN_SECONDS {facts.token_seconds}",
            f"READ_ONLY {_yes(facts.read_only)}",
            f"QUERY_TIMEOUT {_yes(facts.query_timeout)}",
            f"EXPLAIN_PLAN {_yes(facts.explain_plan)}",
            f"PRIVILEGES {facts.privileges}",
        ]
    )


def exit_code(facts: Facts) -> int:
    return 0 if facts.version_ok and facts.privileges == "ok" else 1


class QuietParser(argparse.ArgumentParser):
    """argparse echoes the offending argument (``unrecognized arguments:
    --password hunter2``); this parser answers a fixed text and exits 2."""

    def error(self, message: str):  # noqa: ARG002 - the message holds the values
        self.exit(2, f"{self.prog}: invalid arguments (no value is shown); see --help\n")


def build_parser() -> argparse.ArgumentParser:
    parser = QuietParser(
        prog="probe_hana_connectivity.py",
        description="Probe an on-premise SAP HANA through the Cloud Connector SOCKS5 proxy. "
        "Prints step results and yes/no facts, never a credential, token, host or row.",
    )
    parser.add_argument("--destination", required=True, help="name of the BTP destination")
    parser.add_argument("--variant", choices=(*VARIANTS, "both"), default="both")
    parser.add_argument("--encrypt", choices=("yes", "no", "both"), default="both")
    parser.add_argument("--checks-file", help="JSON list of {name, sql, binds} to run")
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds to connect")
    parser.add_argument("--no-version", action="store_true", help="do not print HANA's version")
    return parser


# ---- the run --------------------------------------------------------------------------------


def load_hdbcli_connect() -> Callable[..., object]:
    """``hdbcli.dbapi.connect``, imported once before the attempts."""
    try:
        from hdbcli import dbapi
    except ImportError:
        raise ProbeError("hdbcli: not importable") from None
    return dbapi.connect


def run(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
    open_socket: Callable[[str, int, float], socket.socket] = open_tcp,
    handshake: Callable[..., int] = socks5_handshake,
    connect: Callable[..., object] | None = None,
) -> int:
    """Run the probe. The keyword arguments replace the network in tests."""
    facts = Facts()
    try:
        _run(args, environ, facts, transport, open_socket, handshake, connect)
    except ProbeError as exc:
        print(f"FAIL {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - the text may hold a host or a token
        print(f"FAIL probe: unexpected {type(exc).__name__}")
        return 2
    print()
    print(report_block(facts))
    return exit_code(facts)


def _run(args, environ, facts, transport, open_socket, handshake, connect) -> None:
    checks = load_checks(args.checks_file) if args.checks_file else []

    # S1
    cfg = read_bindings(environ.get("VCAP_SERVICES", ""))
    describe_bindings(cfg)

    with httpx.Client(transport=transport, timeout=args.timeout, trust_env=False) as client:
        # S2
        props = find_destination(client, cfg.destination, args.destination)
        facts.dest_shape = destination_shape(props)
        describe_destination(props, facts.dest_shape)
        if props.get("ProxyType") != "OnPremise":
            raise ProbeError("destination: ProxyType is not OnPremise")
        if not (props.get("User") and props.get("Password")):
            raise ProbeError("destination: User and Password are needed")
        host, port = target_of(props, facts.dest_shape)
        location = str(props.get("CloudConnectorLocationId") or "")
        hana_props = hana_properties(props)

        # S3
        token, facts.token_seconds = _token(client, cfg.connectivity.oauth, "connectivity_token")
        say("PASS", "connectivity_token", f"expires_in {facts.token_seconds}")

    # S4 + S5
    proxy_host, proxy_port = cfg.connectivity.proxy_host, cfg.connectivity.socks5_port
    try:
        sock = open_socket(proxy_host, proxy_port, args.timeout)
    except OSError as exc:
        say("FAIL", "socks5_tcp", f"error={type(exc).__name__}")
        sock = None
    if sock is not None:
        say("PASS", "socks5_tcp", "connected")
        try:
            reply = handshake(sock, token, location, host, port)
            say("PASS" if reply == 0 else "FAIL", "socks5_handshake", f"reply={reply}")
        except ProbeError as exc:
            say("FAIL", "socks5_handshake", str(exc).removeprefix("socks5: "))
        except OSError as exc:
            say("FAIL", "socks5_handshake", f"error={type(exc).__name__}")
        finally:
            sock.close()
    else:
        say("SKIP", "socks5_handshake", "no TCP connection")

    # S6
    if connect is None:
        connect = load_hdbcli_connect()
    variants = VARIANTS if args.variant == "both" else (args.variant,)
    modes = {"yes": (True,), "no": (False,), "both": (True, False)}[args.encrypt]
    logins: dict[tuple[str, bool], bool] = {}
    conn = None
    for variant in variants:
        for encrypt in modes:
            label = f"hdbcli variant={variant} encrypt={_yes(encrypt)}"
            if variant == "props-account" and not location:
                say("SKIP", label, "no location id in the destination")
                continue
            kwargs = connect_kwargs(
                variant,
                proxy_host=proxy_host,
                proxy_port=proxy_port,
                token=token,
                location_id=location,
                host=host,
                port=port,
                user=str(props["User"]),
                password=str(props["Password"]),
                encrypt=encrypt,
                hana_props=hana_props,
                timeout=args.timeout,
            )
            try:
                attempt = connect(**kwargs)
            except Exception as exc:  # noqa: BLE001 - code or class only
                logins[(variant, encrypt)] = False
                say("FAIL", label, sql_code_of(exc))
                continue
            finally:
                del kwargs
            logins[(variant, encrypt)] = True
            say("PASS", label, "logged in")
            if conn is None:
                conn = attempt
                facts.proxy_variant = variant
            else:
                _close(attempt)
    facts.tls = tls_fact(logins)
    if conn is None:
        for step in ("database", "read_only", "query_timeout", "privileges", "explain_plan"):
            say("SKIP", step, "no login")
        return
    try:
        _sql_steps(conn, args, facts, checks)
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001,S110 - best effort, nothing was written
            pass
        _close(conn)


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001,S110 - best effort
        pass


def _sql_steps(conn, args, facts: Facts, checks: list[dict]) -> None:
    conn.setautocommit(False)
    cursor = conn.cursor()

    # S7
    try:
        cursor.execute("SELECT DATABASE_NAME, VERSION FROM M_DATABASE")
        rows = cursor.fetchall()
        facts.version_ok = len(rows) == 1
        version = str(rows[0][1]) if rows else ""
        hidden = args.no_version or not VERSION_RE.fullmatch(version)
        shown = "" if hidden else f", version {version}"
        say("PASS" if facts.version_ok else "FAIL", "database", f"rows={len(rows)}{shown}")
    except Exception as exc:  # noqa: BLE001
        say("FAIL", "database", sql_code_of(exc))

    # S8: S7 opened a transaction (autocommit is off). End it, so SET
    # TRANSACTION READ ONLY is the first statement of the transaction S9-S12
    # then run in; mid-transaction it could be refused, and A5 would be judged
    # on a transaction that never was read-only.
    try:
        conn.rollback()
        cursor.execute("SET TRANSACTION READ ONLY")
        facts.read_only = True
        say("PASS", "read_only", "accepted")
    except Exception as exc:  # noqa: BLE001
        say("FAIL", "read_only", sql_code_of(exc))

    # S9
    try:
        cursor.setquerytimeout(QUERY_TIMEOUT_SECONDS)
        cursor.execute("SELECT 1 FROM SYS.DUMMY")
        cursor.fetchall()
        facts.query_timeout = True
        say("PASS", "query_timeout", f"setquerytimeout({QUERY_TIMEOUT_SECONDS}) accepted")
    except Exception as exc:  # noqa: BLE001
        say("FAIL", "query_timeout", sql_code_of(exc))

    # S10
    try:
        cursor.execute("SELECT CURRENT_USER FROM SYS.DUMMY")
        row = cursor.fetchone()
        own = str(row[0]) if row else None
        cursor.execute(
            "SELECT PRIVILEGE, SCHEMA_NAME FROM EFFECTIVE_PRIVILEGES "
            "WHERE USER_NAME = CURRENT_USER"
        )
        rows = [(r[0], r[1]) for r in cursor.fetchall()]
        facts.privileges = classify_privileges(rows, own_schema=own)
        say(
            "PASS" if facts.privileges == "ok" else "FAIL",
            "privileges",
            f"{facts.privileges} (rows={len(rows)})",
        )
    except Exception as exc:  # noqa: BLE001
        say("FAIL", "privileges", sql_code_of(exc))

    # S11: the name is generated here, so it is a literal, not a bind.
    name = "PROBE_" + secrets.token_hex(6).upper()
    try:
        cursor.execute(f"EXPLAIN PLAN SET STATEMENT_NAME = '{name}' FOR SELECT * FROM SYS.DUMMY")
        cursor.execute(
            "SELECT OPERATOR_NAME FROM EXPLAIN_PLAN_TABLE WHERE STATEMENT_NAME = ?", (name,)
        )
        rows = cursor.fetchall()
        facts.explain_plan = bool(rows)
        say("PASS" if rows else "FAIL", "explain_plan", f"rows={len(rows)}")
    except Exception as exc:  # noqa: BLE001
        say("FAIL", "explain_plan", sql_code_of(exc))

    # S12
    if checks and not facts.read_only:
        say("SKIP", "checks", "not read-only")
    elif checks:
        passed, failed = run_checks(cursor, checks)
        say("PASS" if not failed else "FAIL", "checks", f"passed={passed} failed={failed}")
    else:
        say("SKIP", "checks", "no --checks-file")
    try:
        cursor.close()
    except Exception:  # noqa: BLE001,S110
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    return run(args, os.environ)


if __name__ == "__main__":
    sys.exit(main())
