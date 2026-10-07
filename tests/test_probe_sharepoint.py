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


def _handler(download_url: str = DOWNLOAD, site_status: int = 200, auth_tokens=None):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "ds-token"})
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
            return httpx.Response(200, json={
                "name": "Planning.xlsx", "size": 1234, "eTag": "x", "file": {},
                "lastModifiedDateTime": "2026-01-02T08:00:00Z",
                "@microsoft.graph.downloadUrl": download_url})
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
    return code, capsys.readouterr().out, seen


def test_every_step_passes_and_nothing_secret_is_printed(monkeypatch, capsys):
    code, out, seen = _run(monkeypatch, capsys)
    assert code == 0, out
    assert out.count("PASS") == 6 and "FAIL" not in out
    for secret in ("graph-t0ken", "ds-token", "dest-s3cret", "tempauth", "t0ken-in-url", "PK"):
        assert secret not in out
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
    assert code == 1 and "returned no token" in out


def test_no_binding_is_a_clear_message():
    with pytest.raises(probe.ProbeError, match="no destination service binding"):
        probe.read_binding({})
    assert "dest-s3cret" not in repr(probe.read_binding({"VCAP_SERVICES": VCAP}))
