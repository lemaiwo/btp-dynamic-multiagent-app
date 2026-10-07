"""Tests for ``scripts/probe_sharepoint.py`` against mocked endpoints.

The important properties: each step is reported, a download on another host
is a FAIL that is never requested, and neither a token, the client secret nor
the download URL is ever printed.

No network, no tenant.

Run:  python -m pytest tests/test_probe_sharepoint.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import probe_sharepoint as probe  # noqa: E402
import pytest  # noqa: E402

SITE = "example.sharepoint.com:/sites/planning"
VCAP = json.dumps({"destination": [{"credentials": {
    "clientid": "dest-client", "clientsecret": "dest-s3cret",
    "url": "https://auth.example.test", "uri": "https://destination.example.test"}}]})
DOWNLOAD = "https://example.sharepoint.com/_layouts/15/download.aspx?tempauth=t0ken-in-url"


SECRETS = ("graph-t0ken", "ds-token", "dest-s3cret", "tempauth", "t0ken-in-url", "PK",
           "AADSTS", "t=s3", "dest-client", "secret-in-bad-url")


def _handler(download_url: str | None = DOWNLOAD, site_status: int = 200, auth_tokens=None,
             download_status: int = 200, redirect_to: str | None = None,
             network_error: bool = False, destination_body: str | None = None,
             size="1234"):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if network_error and host == "graph.microsoft.com":
            raise httpx.ConnectError("boom graph-t0ken")
        if host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "ds-token"})
        if host == "destination.example.test" and destination_body is not None:
            return httpx.Response(200, content=destination_body.encode())
        if host == "destination.example.test":
            tokens = [{"value": "graph-t0ken"}] if auth_tokens is None else auth_tokens
            return httpx.Response(200, json={
                "destinationConfiguration": {"Authentication": "OAuth2ClientCredentials",
                                             "URL": "https://graph.microsoft.com"},
                "authTokens": tokens})
        if host == "graph.microsoft.com" and path.endswith(":/sites/planning"):
            return httpx.Response(site_status, json={"id": "h,g1,g2", "displayName": "Planning"})
        if host == "graph.microsoft.com" and path.endswith("/drives"):
            return httpx.Response(200, json={"value": [{"id": "b!d1", "name": "Documents"}]})
        if host == "graph.microsoft.com" and "/root:/" in path:
            item = {"name": "Planning.xlsx", "size": size, "eTag": "x", "file": {},
                    "lastModifiedDateTime": "2026-01-02T08:00:00Z"}
            if download_url is not None:
                item["@microsoft.graph.downloadUrl"] = download_url
            return httpx.Response(200, json=item)
        if host == "example.sharepoint.com" and redirect_to:
            return httpx.Response(302, headers={"location": redirect_to})
        if host == "example.sharepoint.com" and download_status != 200:
            return httpx.Response(download_status)
        if host == "example.sharepoint.com":
            return httpx.Response(200, content=b"PK\x03\x04rest")
        return httpx.Response(404)

    return handle, seen


def _run(monkeypatch, capsys, **kw) -> tuple[int, str, list[httpx.Request]]:
    handle, seen = _handler(**kw)
    real = httpx.Client
    monkeypatch.setattr(probe.httpx, "Client",
                        lambda **k: real(transport=httpx.MockTransport(handle)))
    monkeypatch.setenv("VCAP_SERVICES", VCAP)
    code = probe.main(["--destination", "GRAPH", "--site", SITE,
                       "--library", "documents", "--path", "Team/Planning.xlsx"])
    captured = capsys.readouterr()
    # The leak rule holds on every path: nothing secret on stdout or stderr.
    assert captured.err == ""
    for secret in SECRETS:
        assert secret not in captured.out, secret
    return code, captured.out, seen


def test_every_step_passes_and_nothing_secret_is_printed(monkeypatch, capsys):
    code, out, seen = _run(monkeypatch, capsys)
    assert code == 0, out
    assert out.count("PASS") == 6 and "FAIL" not in out
    download = [r for r in seen if r.url.host == "example.sharepoint.com"]
    assert len(download) == 1 and "authorization" not in download[0].headers


def test_a_site_that_is_not_granted_stops_the_probe(monkeypatch, capsys):
    code, out, seen = _run(monkeypatch, capsys, site_status=403)
    assert code == 1 and "FAIL  site: HTTP 403" in out and "Sites.Selected" in out
    assert not [r for r in seen if "/drives" in r.url.path]


def test_a_download_on_another_host_is_a_fail_and_is_not_requested(monkeypatch, capsys):
    code, out, seen = _run(monkeypatch, capsys, download_url="https://cdn.example.test/x?t=s3")
    assert code == 1 and "download host is 'cdn.example.test'" in out
    assert "t=s3" not in out
    assert not [r for r in seen if r.url.host == "cdn.example.test"]


def test_a_destination_without_a_token_is_reported_by_code(monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys,
                        auth_tokens=[{"error": "invalid_client: AADSTS7000215 ...", "value": ""}])
    assert code == 1 and "returned no token" in out and "AADSTS" not in out


def test_no_binding_is_a_clear_message():
    with pytest.raises(probe.ProbeError, match="no destination service binding"):
        probe.read_binding({})
    assert "dest-s3cret" not in repr(probe.read_binding({"VCAP_SERVICES": VCAP}))


def test_a_redirecting_download_is_a_fail_and_is_not_followed(monkeypatch, capsys):
    code, out, seen = _run(monkeypatch, capsys, redirect_to="https://cdn.example.test/x?t=s3")
    assert code == 1 and "FAIL  download: HTTP 302" in out
    assert not [r for r in seen if r.url.host == "cdn.example.test"]


def test_a_failing_download_is_a_fail(monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys, download_status=500)
    assert code == 1 and "FAIL  download: HTTP 500" in out


def test_a_missing_download_location_has_its_own_message(monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys, download_url=None)
    assert code == 1 and "no download location" in out and "None" not in out


def test_a_network_error_is_reported_by_class(monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys, network_error=True)
    assert code == 1 and "FAIL  network: ConnectError" in out


@pytest.mark.parametrize("kw", [
    {"destination_body": "<html>secret-in-bad-url</html>"},
    {"size": "not-a-number"},
])
def test_an_unexpected_error_is_a_class_name_only(monkeypatch, capsys, kw):
    code, out, _ = _run(monkeypatch, capsys, **kw)
    assert code == 1 and "FAIL  unexpected " in out
