"""Tests for ``scripts/probe_odata_connectivity.py`` (the connectivity spike probe).

No SAP and no BTP service is reached: the XSUAA of the app, the destination
service and the XSUAA of the connectivity instance are one
``httpx.MockTransport``; the connectivity proxy is a real HTTP forward proxy on
``127.0.0.1`` (``asyncio.start_server``), because the question the probe has to
answer first is what ``httpx`` puts on the wire for an ``http://`` target.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import probe_odata_connectivity as probe  # noqa: E402

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
    "connectivity": [
        {
            "credentials": {
                "clientid": "conn-client",
                "clientsecret": "s3cr3t-connectivity",
                "token_service_url": "https://tenant.authentication.example.com",
                "onpremise_proxy_host": "connectivityproxy.internal",
                "onpremise_proxy_http_port": "20003",
            }
        }
    ],
    "xsuaa": [
        {
            "credentials": {
                "clientid": "app-client",
                "clientsecret": "s3cr3t-xsuaa",
                "url": "https://tenant.authentication.example.com",
            }
        }
    ],
}


@dataclass
class Recorded:
    """What the fake forward proxy saw, one entry per request."""

    request_lines: list[str] = field(default_factory=list)
    header_sets: list[dict[str, str]] = field(default_factory=list)

    @property
    def request_line(self) -> str:
        return self.request_lines[-1]

    @property
    def headers(self) -> dict[str, str]:
        return self.header_sets[-1]


async def start_forward_proxy(
    recorded: Recorded, *, status_for=lambda headers: 200
) -> tuple[asyncio.AbstractServer, int]:
    """A forward proxy that records the request head and answers itself."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        recorded.request_lines.append(lines[0])
        recorded.header_sets.append(headers)
        status = status_for(headers)
        body = b'{"d":{"secret-row":"BODY-MUST-NOT-BE-PRINTED"}}'
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nsap-authenticated-user: ALICE\r\n"
            "Connection: close\r\n\r\n".encode() + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_bindings_are_read_from_vcap_and_never_printed(capsys):
    cfg = probe.read_bindings(json.dumps(VCAP))
    assert cfg.connectivity.proxy_host == "connectivityproxy.internal"
    assert cfg.connectivity.proxy_port == 20003
    probe.describe_bindings(cfg)
    out = capsys.readouterr().out
    assert "s3cr3t" not in out
    assert "s3cr3t" not in repr(cfg)
    assert "connectivity" in out


def test_a_missing_binding_is_named():
    partial = {k: v for k, v in VCAP.items() if k != "connectivity"}
    with pytest.raises(probe.ProbeError, match="connectivity"):
        probe.read_bindings(json.dumps(partial))
    with pytest.raises(probe.ProbeError, match="VCAP_SERVICES"):
        probe.read_bindings("")


def test_headers_per_mode():
    assert probe.proxy_headers(
        "technical", app_token="A", user_jwt=None, exchanged=None, location_id="LOC"
    ) == {"Proxy-Authorization": "Bearer A", "SAP-Connectivity-SCC-Location_ID": "LOC"}
    assert probe.proxy_headers(
        "exchange", app_token="A", user_jwt="U", exchanged="X", location_id=""
    ) == {"Proxy-Authorization": "Bearer X"}
    assert probe.proxy_headers(
        "header", app_token="A", user_jwt="U", exchanged=None, location_id=""
    ) == {"Proxy-Authorization": "Bearer A", "SAP-Connectivity-Authentication": "Bearer U"}


def test_a_user_mode_never_falls_back_to_the_app_token():
    with pytest.raises(probe.ProbeError):
        probe.proxy_headers("exchange", app_token="A", user_jwt="U", exchanged=None, location_id="")
    with pytest.raises(probe.ProbeError):
        probe.proxy_headers("header", app_token="A", user_jwt=None, exchanged=None, location_id="")


async def test_per_request_proxy_authorization_reaches_a_forward_proxy():
    recorded = Recorded()
    server, port = await start_forward_proxy(recorded)
    async with server:
        seen = await probe.send_through_proxy(
            f"http://127.0.0.1:{port}",
            "http://s4.internal:44300/sap/opu/odata/x/",
            headers={"Proxy-Authorization": "Bearer X"},
            timeout=5,
        )
    # absolute-form request target = forward mode (no CONNECT tunnel)
    assert recorded.request_line.startswith("GET http://s4.internal:44300/sap/opu/odata/x/")
    assert recorded.headers["proxy-authorization"] == "Bearer X"
    assert seen.status == 200
    assert seen.sap_user == "ALICE"
    assert seen.length == len(b'{"d":{"secret-row":"BODY-MUST-NOT-BE-PRINTED"}}')


def test_request_url_keeps_dollar_format_and_adds_destination_queries():
    url = probe.build_request_url(
        "http://s4.internal:44300/", "/sap/opu/odata/sap/SRV/", {"sap-client": "100"}
    )
    assert url == "http://s4.internal:44300/sap/opu/odata/sap/SRV/?$format=json&sap-client=100"


def test_passcode_is_not_an_argument():
    text = probe.build_parser().format_help()
    assert "--passcode" not in text
    for flag in ("--destination", "--path", "--user", "--mode", "--timeout", "--origin"):
        assert flag in text


def test_report_block_has_the_spike_record_keys():
    text = probe.report_block(
        {"technical": 200, "exchange": 200, "header": 407}, location_id="", auth_tokens=False
    )
    for key in (
        "PP_MODE: exchange",
        "ONPREM_HTTP_FORWARD: ok",
        "TECH_USER: ok",
        "LOCATION_ID_NEEDED: no",
        "FIND_DESTINATION_PP: authTokens absent",
    ):
        assert key in text
    assert "SAP_CLIENT:" in text


def test_report_block_does_not_claim_what_was_not_tested():
    text = probe.report_block({"technical": 200}, location_id="LOC", auth_tokens=True)
    assert "PP_MODE: not-tested" in text
    assert "LOCATION_ID_NEEDED: yes (LOC)" in text
    assert "FIND_DESTINATION_PP: authTokens present" in text
    text = probe.report_block(
        {"exchange": 407, "header": None}, location_id="", auth_tokens=False
    )
    assert "PP_MODE: none-works" in text
    assert "TECH_USER: not-tested" in text
    assert "ONPREM_HTTP_FORWARD: failed" in text
    assert "PP_MODE: header" in probe.report_block(
        {"exchange": 407, "header": 200}, location_id="", auth_tokens=False
    )


# ---- the whole run, with every BTP service mocked ---------------------------

APP_TOKEN = "tok-app-connectivity"
EXCHANGED = "tok-exchanged-user"
USER_JWT = "tok-user-jwt"
DEST_TOKEN = "tok-destination-service"
BASIC = base64.b64encode(b"TECH:s3cr3t-sap-password").decode()
PASSCODE = "one-time-passcode-123"


def btp_services(calls: list[httpx.Request], *, proxy_type="OnPremise", auth_tokens=True):
    """XSUAA (app + connectivity), and the destination service, as one transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth/token":
            form = dict(httpx.QueryParams(request.content.decode()))
            client = base64.b64decode(request.headers["authorization"].split()[1]).decode()
            grant = form["grant_type"]
            if grant == "password":
                assert client == "app-client:s3cr3t-xsuaa"
                assert form["passcode"] == PASSCODE
                assert json.loads(form["login_hint"]) == {"origin": "sap.custom"}
                return httpx.Response(200, json={"access_token": USER_JWT})
            if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
                assert client == "conn-client:s3cr3t-connectivity"
                assert form["assertion"] == USER_JWT
                assert form["token_format"] == "jwt" and form["response_type"] == "token"
                return httpx.Response(200, json={"access_token": EXCHANGED})
            assert grant == "client_credentials"
            if client.startswith("conn-client:"):
                return httpx.Response(200, json={"access_token": APP_TOKEN})
            return httpx.Response(200, json={"access_token": DEST_TOKEN})
        assert request.url.path == "/destination-configuration/v1/destinations/S4_ODATA_TECH"
        assert request.headers["authorization"] == f"Bearer {DEST_TOKEN}"
        cfg = {
            "Name": "S4_ODATA_TECH",
            "URL": "http://s4.internal:44300",
            "ProxyType": proxy_type,
            "Authentication": "BasicAuthentication" if auth_tokens else "PrincipalPropagation",
            "CloudConnectorLocationId": "LOC",
            "URL.queries.sap-client": "100",
            "Password": "s3cr3t-sap-password",
        }
        body: dict = {"destinationConfiguration": cfg}
        if auth_tokens:
            body["authTokens"] = [
                {
                    "type": "Basic",
                    "value": BASIC,
                    "http_header": {"key": "Authorization", "value": f"Basic {BASIC}"},
                }
            ]
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def environ_for(port: int) -> dict[str, str]:
    vcap = json.loads(json.dumps(VCAP))
    creds = vcap["connectivity"][0]["credentials"]
    creds["onpremise_proxy_host"] = "127.0.0.1"
    creds["onpremise_proxy_http_port"] = str(port)
    return {"VCAP_SERVICES": json.dumps(vcap)}


def args_for(*extra: str) -> argparse.Namespace:
    return probe.build_parser().parse_args(
        ["--destination", "S4_ODATA_TECH", "--path", "/sap/opu/odata/sap/SRV/", *extra]
    )


SECRETS = (
    "s3cr3t", APP_TOKEN, EXCHANGED, USER_JWT, DEST_TOKEN, BASIC, PASSCODE, "BODY-MUST-NOT",
)


def no_passcode() -> str:
    raise AssertionError("the technical path must not ask for a passcode")


async def test_technical_run_sends_the_app_token_and_prints_no_secret(capsys):
    recorded, calls = Recorded(), []
    server, port = await start_forward_proxy(recorded)
    async with server:
        code = await probe.run(
            args_for(), environ_for(port), transport=btp_services(calls), read_passcode=no_passcode
        )
    out = capsys.readouterr().out
    assert code == 0
    assert recorded.request_line.startswith(
        "GET http://s4.internal:44300/sap/opu/odata/sap/SRV/?$format=json&sap-client=100 "
    )
    assert recorded.headers["proxy-authorization"] == f"Bearer {APP_TOKEN}"
    assert recorded.headers["authorization"] == f"Basic {BASIC}"
    assert recorded.headers["sap-connectivity-scc-location_id"] == "LOC"
    assert "sap-connectivity-authentication" not in recorded.headers
    assert "x-user-token" not in calls[1].headers
    assert "RESULT mode=technical status=200 sap_user_header=ALICE length=" in out
    assert "TECH_USER: ok" in out and "PP_MODE: not-tested" in out
    assert "SAP_CLIENT: URL.queries.sap-client honoured" in out
    for secret in SECRETS:
        assert secret not in out


async def test_user_run_tries_both_modes_with_the_right_headers(capsys):
    recorded, calls = Recorded(), []

    def only_exchange(headers: dict[str, str]) -> int:
        return 200 if headers.get("proxy-authorization") == f"Bearer {EXCHANGED}" else 407

    server, port = await start_forward_proxy(recorded, status_for=only_exchange)
    async with server:
        code = await probe.run(
            args_for("--user"),
            environ_for(port),
            transport=btp_services(calls, auth_tokens=False),
            read_passcode=lambda: PASSCODE,
        )
    out = capsys.readouterr().out
    assert code == 0
    exchange, header = recorded.header_sets
    assert exchange["proxy-authorization"] == f"Bearer {EXCHANGED}"
    assert "sap-connectivity-authentication" not in exchange
    assert header["proxy-authorization"] == f"Bearer {APP_TOKEN}"
    assert header["sap-connectivity-authentication"] == f"Bearer {USER_JWT}"
    assert "authorization" not in exchange  # PrincipalPropagation: no destination credential
    find = next(c for c in calls if c.url.path.startswith("/destination-configuration"))
    assert find.headers["x-user-token"] == USER_JWT
    assert "RESULT mode=exchange status=200" in out
    assert "RESULT mode=header status=407" in out
    assert "PP_MODE: exchange" in out and "TECH_USER: not-tested" in out
    assert "FIND_DESTINATION_PP: authTokens absent" in out
    for secret in SECRETS:
        assert secret not in out


async def test_no_2xx_answer_is_a_non_zero_exit(capsys):
    recorded, calls = Recorded(), []
    server, port = await start_forward_proxy(recorded, status_for=lambda headers: 407)
    async with server:
        code = await probe.run(
            args_for(), environ_for(port), transport=btp_services(calls), read_passcode=no_passcode
        )
    out = capsys.readouterr().out
    assert code == 1
    assert "TECH_USER: failed" in out and "ONPREM_HTTP_FORWARD: failed" in out


async def test_an_internet_destination_is_refused_before_any_proxy_call(capsys):
    recorded, calls = Recorded(), []
    server, port = await start_forward_proxy(recorded)
    async with server:
        code = await probe.run(
            args_for(),
            environ_for(port),
            transport=btp_services(calls, proxy_type="Internet"),
            read_passcode=no_passcode,
        )
    out = capsys.readouterr().out
    assert code == 2
    assert recorded.request_lines == []
    assert "ProxyType: Internet" in out
    for secret in SECRETS:
        assert secret not in out
