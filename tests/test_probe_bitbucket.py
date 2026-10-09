"""Tests for ``scripts/probe_bitbucket.py`` against mocked endpoints.

The important properties: every step is reported on one line, only GET
requests reach Bitbucket, a redirect of the diff endpoint is followed only on
the same host and exactly as given, and neither the credential, a response
body, the ``Location`` nor anything a person wrote is ever printed or logged.

No network, no workspace.

Run:  python -m pytest tests/test_probe_bitbucket.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import probe_bitbucket as probe  # noqa: E402
import pytest  # noqa: E402

HOST = "api.bitbucket.example.test"
VCAP = json.dumps({"destination": [{"credentials": {
    "clientid": "dest-client", "clientsecret": "dest-s3cret",
    "url": "https://auth.example.test", "uri": "https://destination.example.test"}}]})
# ``%0D`` is what Bitbucket puts between the two commits of a diff spec.
LOCATION_TAIL = ("/2.0/repositories/acme/shop/diff/acme/shop:abc123%0Ddef456"
                 "?from_pullrequest_id=7&topic=true")
LOCATION = f"https://{HOST}{LOCATION_TAIL}"
DIFF = "diff --git a/x.py b/x.py\n+PLANTED-DIFF-LINE\n"

SECRETS = ("dGVjaDpQTEFOVEVE", "ds-token", "dest-s3cret", "dest-client", "PLANTED-TITLE",
           "PLANTED-DIFF-LINE", "Ann Author", "{11111111", "PLANTED-ERROR-BODY",
           "from_pullrequest_id")


def _handler(url: str = f"https://{HOST}", user_status: int = 200, missing: tuple = (),
             location: str = LOCATION, network_error: bool = False):
    seen: list[httpx.Request] = []
    refused = {"type": "error", "error": {"message": "PLANTED-ERROR-BODY"}}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "auth.example.test":
            return httpx.Response(200, json={"access_token": "ds-token"})
        if host == "destination.example.test":
            return httpx.Response(200, json={
                "destinationConfiguration": {"Name": "BITBUCKET", "URL": url,
                                             "Authentication": "BasicAuthentication",
                                             "User": "tech", "Password": "PLANTED-ERROR-BODY"},
                "authTokens": [{"type": "Basic", "value": "dGVjaDpQTEFOVEVE"}]})
        if host != HOST:
            return httpx.Response(200, text="PLANTED-DIFF-LINE")
        if network_error:
            raise httpx.ConnectError("boom dGVjaDpQTEFOVEVE")
        if request.headers.get("authorization") != "Basic dGVjaDpQTEFOVEVE":
            return httpx.Response(401, json=refused)
        if path == "/2.0/user":
            if user_status != 200:
                return httpx.Response(user_status, json=refused)
            return httpx.Response(200, json={"uuid": "{11111111-2222-3333-4444-555555555555}",
                                             "display_name": "Ann Author"})
        if path == "/2.0/repositories/acme":
            return httpx.Response(200, json={
                "values": [{"slug": "shop", "name": "PLANTED-TITLE"}, {"slug": "web"}],
                "next": f"https://{HOST}/2.0/repositories/acme?page=2"})
        if path.endswith("/pullrequests/7/diff"):
            return httpx.Response(302, headers={"location": location})
        if path.endswith("/pullrequests"):
            return httpx.Response(200, json={"values": [{
                "id": 7, "title": "PLANTED-TITLE", "author": {"display_name": "Ann Author"}}]})
        if "/diff/" in path:
            return httpx.Response(200, text=DIFF)
        if path.startswith("/2.0/repositories/acme/"):
            slug = path.rsplit("/", 1)[1]
            if slug in missing:
                return httpx.Response(404, json=refused)
            return httpx.Response(200, json={"slug": slug, "name": "PLANTED-TITLE"})
        return httpx.Response(404, json=refused)

    return handle, seen


ARGS = ["--destination", "BITBUCKET", "--workspace", "acme"]
FULL = [*ARGS, "--repository", "shop", "--pull-request", "7"]


def _run(monkeypatch, capsys, argv=None, **kw) -> tuple[int, list[str], list[httpx.Request]]:
    handle, seen = _handler(**kw)
    monkeypatch.setenv("VCAP_SERVICES", VCAP)
    code = probe.main(FULL if argv is None else argv, transport=httpx.MockTransport(handle))
    captured = capsys.readouterr()
    # The leak rule holds on every path: nothing secret on stdout or stderr.
    for secret in SECRETS:
        assert secret not in captured.out + captured.err, secret
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert all(line.startswith(("PASS  ", "FAIL  ", "SKIP  ")) for line in lines), lines
    return code, lines, seen


def _bitbucket(seen: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in seen if r.url.host == HOST]


def test_every_step_passes_and_nothing_secret_is_printed(monkeypatch, capsys):
    code, lines, seen = _run(monkeypatch, capsys)
    assert code == 0, lines
    assert len(lines) == 6 and all(line.startswith("PASS  ") for line in lines), lines
    assert ("Authentication=BasicAuthentication" in lines[0] and f"host={HOST}" in lines[0]
            and "https: yes" in lines[0] and "api root in URL: no" in lines[0])
    assert "HTTP 200" in lines[1] and "account identified: yes" in lines[1]
    assert "repositories on the first page: 2" in lines[2] and "more: yes" in lines[2]
    assert lines[3].startswith("PASS  repository shop: HTTP 200")
    assert "open pull requests on the first page: 1" in lines[4]
    assert "HTTP 302" in lines[5] and "redirect stays on the host: yes" in lines[5]
    assert f"{len(DIFF)} characters" in lines[5]
    listing = [r for r in seen if r.url.path.endswith("/pullrequests")]
    assert len(listing) == 1 and dict(listing[0].url.params) == {
        "q": 'destination.branch.name="main" AND state="OPEN"', "pagelen": "1"}


def test_only_get_requests_reach_bitbucket(monkeypatch, capsys):
    _, _, seen = _run(monkeypatch, capsys)
    assert len(_bitbucket(seen)) == 6
    assert {r.method for r in _bitbucket(seen)} == {"GET"}


@pytest.mark.parametrize("url", [f"https://{HOST}/2.0", f"https://{HOST}/2.0/"])
def test_the_api_root_in_the_destination_url_gives_the_same_requests(monkeypatch, capsys, url):
    _, plain_lines, plain = _run(monkeypatch, capsys)
    code, lines, seen = _run(monkeypatch, capsys, url=url)
    assert code == 0 and "api root in URL: yes" in lines[0]
    assert lines[1:] == plain_lines[1:]
    assert [r.url.raw_path for r in _bitbucket(seen)] == [r.url.raw_path
                                                          for r in _bitbucket(plain)]
    assert [r.url.path for r in _bitbucket(seen)].count("/2.0/user") == 1


@pytest.mark.parametrize("location", [
    "https://evil.example.test/x?from_pullrequest_id=7",
    f"http://{HOST}{LOCATION_TAIL}",
    f"https://{HOST}:8443{LOCATION_TAIL}",
    f"https://u:p@{HOST}{LOCATION_TAIL}",
    LOCATION_TAIL,
])
def test_a_redirect_off_the_host_is_a_fail_and_is_not_requested(monkeypatch, capsys, location):
    code, lines, seen = _run(monkeypatch, capsys, location=location)
    assert code == 1 and len(lines) == 6
    assert lines[5].startswith("FAIL  ") and "redirect stays on the host: no" in lines[5]
    assert all(line.startswith("PASS  ") for line in lines[:5])
    assert not [r for r in seen if r.url.host == "evil.example.test"]
    assert len(_bitbucket(seen)) == 5  # the Location was never requested


def test_the_location_is_requested_exactly_as_given(monkeypatch, capsys):
    _, _, seen = _run(monkeypatch, capsys)
    assert _bitbucket(seen)[-1].url.raw_path == LOCATION_TAIL.encode()


def test_a_refused_account_is_a_fail_and_the_later_steps_are_still_reported(monkeypatch, capsys):
    code, lines, _ = _run(monkeypatch, capsys, user_status=401)
    assert code == 1 and len(lines) == 6
    assert lines[1].startswith("FAIL  ") and "HTTP 401" in lines[1]
    assert "account identified: no" in lines[1]
    assert all(line.startswith("PASS  ") for line in lines[2:])


def test_a_missing_repository_fails_for_that_slug_only(monkeypatch, capsys):
    code, lines, _ = _run(monkeypatch, capsys, missing=("gone",),
                          argv=[*ARGS, "--repository", "shop", "--repository", "gone"])
    assert code == 1
    assert "PASS  repository shop: HTTP 200" in lines
    assert "FAIL  repository gone: HTTP 404" in lines
    assert [line[:4] for line in lines].count("FAIL") == 1


def test_without_a_pull_request_the_diff_step_is_skipped(monkeypatch, capsys):
    code, lines, seen = _run(monkeypatch, capsys, argv=[*ARGS, "--repository", "shop"])
    assert code == 0 and len(lines) == 6 and lines[5].startswith("SKIP  ")
    assert not [r for r in seen if "diff" in r.url.path]


def test_without_a_repository_the_first_of_the_workspace_is_listed(monkeypatch, capsys):
    code, lines, seen = _run(monkeypatch, capsys, argv=[*ARGS, "--pull-request", "7"])
    assert code == 0 and len(lines) == 6
    assert lines[3].startswith("SKIP  ") and lines[4].startswith("PASS  ")
    assert lines[5].startswith("SKIP  ")
    assert [r.url.path for r in seen if r.url.path.endswith("/pullrequests")] == [
        "/2.0/repositories/acme/shop/pullrequests"]


def test_a_network_error_is_reported_by_class(monkeypatch, capsys):
    code, lines, _ = _run(monkeypatch, capsys, network_error=True)
    assert code == 1 and len(lines) == 6
    assert lines[0].startswith("PASS  ")
    assert lines[1] == "FAIL  account: ConnectError"
    assert all(line.startswith("FAIL  ") and line.endswith(": ConnectError")
               for line in lines[1:])


def test_a_destination_that_is_not_https_gets_no_request(monkeypatch, capsys):
    code, lines, seen = _run(monkeypatch, capsys, url=f"http://{HOST}")
    assert code == 1 and len(lines) == 6
    assert lines[0].startswith("FAIL  ") and "https: no" in lines[0]
    assert all(line.startswith("SKIP  ") for line in lines[1:])
    assert not _bitbucket(seen)


def test_no_binding_is_a_clear_message(monkeypatch, capsys):
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    code = probe.main(FULL, transport=httpx.MockTransport(_handler()[0]))
    out = capsys.readouterr().out
    assert code == 1 and "no destination service binding" in out
    with pytest.raises(probe.ProbeError, match="no destination service binding"):
        probe.read_binding({})
    shown = repr(probe.read_binding({"VCAP_SERVICES": VCAP}))
    assert "dest-s3cret" not in shown and "dest-client" not in shown


@pytest.mark.parametrize("argv", [
    ["--destination", "D", "--workspace", "Bad Name/x"],
    [*ARGS, "--repository", "Bad Name/x"],
    [*ARGS, "--branch", "Bad Name/x\""],
    [*ARGS, "--pull-request", "0"],
    [*ARGS, "--pull-request", "Bad Name/x"],
])
def test_a_bad_argument_is_refused_without_echoing_it(capsys, argv):
    with pytest.raises(SystemExit) as stop:
        probe.main(argv, transport=httpx.MockTransport(_handler()[0]))
    captured = capsys.readouterr()
    assert stop.value.code == 2
    assert "Bad Name" not in captured.out + captured.err
    assert "'0'" not in captured.err


def test_httpx_request_logging_is_silenced(monkeypatch, capsys, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    code, _, seen = _run(monkeypatch, capsys)
    assert code == 0 and len(_bitbucket(seen)) == 6
    for secret in SECRETS:
        assert secret not in caplog.text, secret
    assert HOST not in caplog.text
    # The level is the caller's again afterwards (other suites read httpx's log).
    assert logging.getLogger("httpx").level == logging.INFO


@pytest.mark.parametrize("url, location", [
    (f"https://{HOST}", f"https://{HOST}:443{LOCATION_TAIL}"),
    (f"https://{HOST}:443", LOCATION),
    (f"https://{HOST}:443", f"https://{HOST}:443{LOCATION_TAIL}"),
])
def test_an_explicit_default_port_is_the_same_place(monkeypatch, capsys, url, location):
    """Final review n10: `:443` on https is no other port than none at all."""
    code, lines, seen = _run(monkeypatch, capsys, url=url, location=location)
    assert code == 0, lines
    assert "redirect stays on the host: yes" in lines[5]
    assert _bitbucket(seen)[-1].url.raw_path == LOCATION_TAIL.encode()


def test_the_port_rule_applies_the_default_itself():
    """Not left to how an httpx version normalises a URL."""
    assert probe._port(httpx.URL(f"https://{HOST}")) == 443
    assert probe._port(httpx.URL(f"https://{HOST}:443")) == 443
    assert probe._port(httpx.URL(f"https://{HOST}:8443")) == 8443
