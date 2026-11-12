"""scripts/probe_ariba.py against a scripted destination service and Ariba."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "probe_ariba", Path(__file__).resolve().parents[1] / "scripts" / "probe_ariba.py")
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["probe_ariba"] = probe
_SPEC.loader.exec_module(probe)

SECRETS = ("tok-SECRET", "key-SECRET", "realm-SECRET", "user-SECRET", "ACME Rail", "offer.pdf")
VCAP = json.dumps({"destination": [{"credentials": {
    "url": "https://auth.example", "uri": "https://dest.example",
    "clientid": "cid", "clientsecret": "binding-SECRET"}}]})


def _config(**over):
    config = {"Name": "ARIBA", "URL": "https://eu.openapi.ariba.com/api/sourcing-event/v2/prod",
              "URL.headers.apiKey": "key-SECRET", "URL.queries.realm": "realm-SECRET",
              "URL.queries.user": "user-SECRET", "URL.queries.passwordAdapter": "PasswordAdapter1"}
    config.update(over)
    return config


def _transport(config=None, seen=None):
    bids = [{"invitationId": "inv1", "itemId": "it1", "bidStatus": "Accepted",
             "item": {"itemId": "it1", "title": "ACME Rail", "terms": [
                 {"fieldId": "RQATTACH", "value": {"attachmentValue": {
                     "id": "att1", "fileName": "offer.pdf", "contentType": "application/pdf"}}}]}}]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "auth.example":
            return httpx.Response(200, json={"access_token": "dest-token"})
        if host == "dest.example":
            return httpx.Response(200, json={
                "destinationConfiguration": config or _config(),
                "authTokens": [{"type": "Bearer", "value": "tok-SECRET"}]})
        assert request.method == "GET"
        assert request.headers["apiKey"] == "key-SECRET"
        assert request.url.params["realm"] == "realm-SECRET"
        if path.endswith("/content"):
            assert request.url.params["itemId"] == "it1"
            assert request.url.params["fieldId"] == "RQATTACH"
            return httpx.Response(200, content=b"%PDF-1.7 body")
        if path.endswith("/supplierInvitations"):
            return httpx.Response(200, json={"payload": [
                {"invitationId": "inv1", "hasBid": True, "organization": {"name": "ACME Rail"}},
                {"invitationId": "inv2", "hasBid": False}]})
        if path.endswith("/supplierBids"):
            return httpx.Response(200, json={"payload": bids})
        if path.endswith("/events/Doc1"):
            return httpx.Response(200, json={"payload": {
                "eventStateName": "Open", "eventTypeName": "RFP", "title": "ACME Rail",
                "items": [{}, {}]}})
        if path.endswith("/events/identifiers"):
            assert "createDateFrom gt" in request.url.params["$filter"]
            return httpx.Response(200, json={"payload": [
                {"internalId": "Doc77", "projectId": "WS1"}, {"internalId": "bad id/x"}]})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "ACME Rail"})

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _binding(monkeypatch):
    monkeypatch.setenv("VCAP_SERVICES", VCAP)


def test_full_run_prints_counts_and_no_data(capsys, tmp_path):
    code = probe.main(["--destination", "ARIBA", "--event", "Doc1", "--attachments"],
                      transport=_transport())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS  event: state=Open type=RFP items=2" in out
    assert "2 invited, 1 with a bid" in out
    assert "1 bid rows, 1 attachments from 1 suppliers" in out
    assert "by content: pdf" in out
    for secret in SECRETS + ("binding-SECRET", "dest-token"):
        assert secret not in out


def test_placeholders_stop_before_ariba_is_called(capsys):
    seen: list[httpx.Request] = []
    code = probe.main(["--destination", "ARIBA", "--event", "Doc1"], transport=_transport(
        _config(**{"URL.headers.apiKey": "REPLACE_WITH_APPLICATION_KEY"}), seen))
    out = capsys.readouterr().out
    assert code == 1
    assert "placeholders left: 1" in out
    assert all(r.url.host != "eu.openapi.ariba.com" for r in seen)


def test_without_event_only_counts_events(capsys):
    code = probe.main(["--destination", "ARIBA"], transport=_transport())
    out = capsys.readouterr().out
    assert code == 0
    assert "2 events created in the last 90 days; ids: Doc77" in out
    assert "bad id" not in out


def test_error_status_names_no_text(capsys):
    code = probe.main(["--destination", "ARIBA", "--event", "Nope"], transport=_transport())
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL  event: event, HTTP 404" in out
    assert "ACME Rail" not in out


def test_dump_outside_temp_is_refused(capsys):
    seen: list[httpx.Request] = []
    code = probe.main(["--destination", "ARIBA", "--dump", str(Path.home() / "x")],
                      transport=_transport(seen=seen))
    assert code == 1
    assert probe.DUMP_REFUSED in capsys.readouterr().out
    assert seen == []


def test_bad_event_id_is_refused_without_echo(capsys):
    with pytest.raises(SystemExit) as exit_info:
        probe.main(["--destination", "ARIBA", "--event", "a/b?x"])
    assert exit_info.value.code == 2
