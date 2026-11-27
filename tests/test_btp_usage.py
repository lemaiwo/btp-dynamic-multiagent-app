"""``agents/btp_usage.py``: the actual AI Core cost of this subaccount from
the Usage Data Management API, on the recorded answer
(``tests/fixtures/btp_usage/monthly_subaccounts_cost.json``).

The API answers the WHOLE global account: rows of another subaccount, of the
global account itself (``subaccountId: ""``) and of other services must never
reach any value the module answers or logs."""

from __future__ import annotations

import ast
import copy
import inspect
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import btp_usage  # noqa: E402
from agents.destination import (  # noqa: E402
    Destination,
    DestinationError,
    DestinationNotFound,
)

FIXTURE = json.loads(
    (ROOT / "tests/fixtures/btp_usage/monthly_subaccounts_cost.json").read_text())
OURS = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
HOST = "uas-reporting.example.com"
TOKEN = "Bearer secret-token-value"
FIRST, LAST = date(2026, 9, 1), date(2026, 11, 1)
# Marks of foreign rows; none may appear in an answer.
FOREIGN_MARKS = ("subaccount-b", OTHER, "999.25", "ga-foreign", "4242.5")


class FakeResolver:
    def __init__(self, url=f"https://{HOST}", fail=None):
        self.url, self.fail = url, fail
        self.calls = 0
        self.invalidated = 0

    async def resolve(self, *, force=False, user_token=None, principal=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return Destination(url=self.url, headers={"Authorization": TOKEN, "Cookie": "x=1"},
                           expires_at=time.monotonic() + 600)

    def invalidate(self, principal=None):
        self.invalidated += 1


class Api:
    """A scripted Reporting API; records each request."""

    def __init__(self, payload=None, status=200, body=None, raise_exc=None, headers=None):
        self.payload = FIXTURE if payload is None else payload
        self.status, self.body, self.raise_exc = status, body, raise_exc
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.body is not None:
            content = self.body
        else:
            content = json.dumps(self.payload).encode()
        if self.status >= 300:
            return httpx.Response(self.status, headers=self.headers,
                                  stream=ExplodingStream())
        return httpx.Response(self.status, headers=self.headers, content=content)


class ExplodingStream(httpx.AsyncByteStream):
    """The body of an error status: reading it fails the test."""

    async def __aiter__(self):
        raise AssertionError("the body of an error status was read")
        yield b""  # pragma: no cover

    async def aclose(self):
        return None


@pytest.fixture
def env(monkeypatch):
    """A destination service is bound, this subaccount is OURS."""
    btp_usage.reset()
    monkeypatch.setattr(btp_usage, "config_from_environment", lambda environ: object())
    monkeypatch.setenv("USAGE_SUBACCOUNT_ID", OURS)
    monkeypatch.delenv("USAGE_DESTINATION", raising=False)
    resolver = FakeResolver()
    monkeypatch.setattr(btp_usage, "_resolver", lambda name, cfg: resolver)
    clock = [1000.0]
    monkeypatch.setattr(btp_usage, "_now", lambda: clock[0])
    yield {"resolver": resolver, "clock": clock}
    btp_usage.reset()


def use(monkeypatch, api: Api) -> Api:
    monkeypatch.setattr(btp_usage, "_transport", httpx.MockTransport(api))
    return api


def with_foreign_rows() -> dict:
    """A copy of the fixture with more foreign rows (never written to the file)."""
    data = copy.deepcopy(FIXTURE)
    template = next(r for r in data["content"] if r["subaccountId"] == OURS
                    and r["serviceId"] == "ai-core" and r["reportYearMonth"] == 202611)
    foreign = dict(template, subaccountId=OTHER, subaccountName="subaccount-b",
                   cost=999.25, usage=4242.5, globalAccountId="ga-foreign")
    # A foreign row with odd fields must not break the read either.
    odd = dict(template, subaccountId=OTHER, cost="n/a", reportYearMonth="x")
    ga_level = dict(template, subaccountId="", subaccountName="", cost=4242.5)
    data["content"] += [foreign, odd, ga_level]
    return data


# --- parsing -------------------------------------------------------------------

async def test_only_ai_core_rows_of_this_subaccount_are_kept(env, monkeypatch):
    use(monkeypatch, Api(payload=with_foreign_rows()))
    answer = await btp_usage.monthly_cost(FIRST, LAST)
    assert answer.state == "ok"
    assert answer.months == {
        "2026-10": {"amount": 7.5, "currency": "EUR", "capacity_units": 5.0,
                    "estimated": False,
                    "metrics": [{"name": "capacity_units", "usage": 5.0, "cost": 7.5}]},
        "2026-11": {"amount": 12.0, "currency": "EUR", "capacity_units": 8.0,
                    "estimated": True,
                    "metrics": [{"name": "capacity_units", "usage": 8.0, "cost": 12.0}]},
    }
    text = json.dumps(answer.months)
    for mark in FOREIGN_MARKS + ("cloud-logging", "linux-container", "customdomain"):
        assert mark not in text


async def test_another_subaccount_sees_only_its_own_row(env, monkeypatch):
    monkeypatch.setenv("USAGE_SUBACCOUNT_ID", OTHER)
    use(monkeypatch, Api())
    answer = await btp_usage.monthly_cost(FIRST, LAST)
    assert answer.state == "ok"
    assert answer.months == {"2026-11": {
        "amount": 1.5, "currency": "EUR", "capacity_units": 1.0, "estimated": True,
        "metrics": [{"name": "capacity_units", "usage": 1.0, "cost": 1.5}]}}


async def test_months_outside_the_window_are_left_out(env, monkeypatch):
    use(monkeypatch, Api())
    answer = await btp_usage.monthly_cost(date(2026, 11, 1), date(2026, 11, 1))
    assert list(answer.months) == ["2026-11"]


def test_a_global_account_row_never_matches_an_empty_subaccount():
    # Defence in depth: an empty id is no id, so "" rows can never be "ours".
    assert btp_usage.subaccount_id({"USAGE_SUBACCOUNT_ID": "  "}) is None
    with pytest.raises(btp_usage._Refused):
        btp_usage.parse({"content": "x"}, OURS, FIRST, LAST)


def test_subaccount_from_the_xsuaa_binding():
    vcap = json.dumps({"xsuaa": [{"credentials": {"subaccountid": OURS}}]})
    assert btp_usage.subaccount_id({"VCAP_SERVICES": vcap}) == OURS
    assert btp_usage.subaccount_id(
        {"VCAP_SERVICES": vcap, "USAGE_SUBACCOUNT_ID": OTHER}) == OTHER
    assert btp_usage.subaccount_id({"VCAP_SERVICES": "{not json"}) is None
    assert btp_usage.subaccount_id({"VCAP_SERVICES": json.dumps({"xsuaa": []})}) is None
    assert btp_usage.subaccount_id({"VCAP_SERVICES": json.dumps(
        {"xsuaa": [{"credentials": {"subaccountid": "a b/c"}}]})}) is None
    assert btp_usage.subaccount_id({}) is None


def test_destination_name_setting():
    assert btp_usage.settings({}) == "BTP_USAGE"
    assert btp_usage.settings({"USAGE_DESTINATION": " MY_USAGE "}) == "MY_USAGE"


# --- the request ----------------------------------------------------------------

async def test_the_request_goes_to_the_destination_host_with_the_month_params(env, monkeypatch):
    api = use(monkeypatch, Api())
    await btp_usage.monthly_cost(FIRST, LAST)
    [request] = api.requests
    assert request.url.scheme == "https" and request.url.host == HOST
    assert request.url.path == "/reports/v1/monthlySubaccountsCost"
    assert dict(request.url.params) == {"fromDate": "202609", "toDate": "202611"}
    assert request.headers["authorization"] == TOKEN
    assert "cookie" not in request.headers


async def test_a_non_https_destination_is_refused_before_sending(env, monkeypatch):
    env["resolver"].url = f"http://{HOST}"
    api = use(monkeypatch, Api())
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "destination_error"
    assert api.requests == []


async def test_no_redirect_is_followed(env, monkeypatch):
    api = use(monkeypatch, Api(status=302, headers={"location": "https://evil.example.com/"}))
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "usage_unavailable"
    assert len(api.requests) == 1


# --- off and the codes ------------------------------------------------------------

async def test_off_without_a_destination_service(env, monkeypatch):
    monkeypatch.setattr(btp_usage, "config_from_environment", lambda environ: None)
    api = use(monkeypatch, Api())
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "off"
    assert api.requests == [] and env["resolver"].calls == 0


async def test_off_when_the_destination_does_not_exist(env, monkeypatch):
    env["resolver"].fail = DestinationNotFound("no such destination https://x?y=1")
    api = use(monkeypatch, Api())
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "off"
    assert api.requests == []


async def test_no_subaccount_sends_nothing(env, monkeypatch):
    monkeypatch.delenv("USAGE_SUBACCOUNT_ID")
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    api = use(monkeypatch, Api())
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "no_subaccount"
    assert api.requests == [] and env["resolver"].calls == 0


SERVICE_TEXT = "secret-service-text https://uas.example.com/x?token=abc"


@pytest.mark.parametrize("api_kwargs, code", [
    ({"status": 401}, "unauthorized"),
    ({"status": 403}, "unauthorized"),
    ({"status": 500}, "usage_unavailable"),
    ({"status": 404}, "usage_unavailable"),
    ({"raise_exc": httpx.ConnectError(SERVICE_TEXT)}, "usage_unavailable"),
    ({"raise_exc": httpx.ReadTimeout(SERVICE_TEXT)}, "usage_unavailable"),
    ({"body": b"<html>" + SERVICE_TEXT.encode()}, "not_json"),
    ({"payload": {"rows": [SERVICE_TEXT]}}, "unreadable"),
    ({"payload": {"content": [dict(FIXTURE["content"][1], cost=SERVICE_TEXT)]}},
     "unreadable"),
    ({"body": b"[" + b" " * (4 * 1024 * 1024) + b"]"}, "too_large"),
])
async def test_failures_are_a_code_without_service_text(env, monkeypatch, caplog,
                                                        api_kwargs, code):
    use(monkeypatch, Api(**api_kwargs))
    caplog.set_level(logging.DEBUG)
    answer = await btp_usage.monthly_cost(FIRST, LAST)
    assert answer.state == code and answer.months == {}
    for record in caplog.records:
        message = record.getMessage()
        assert "secret" not in message and "token" not in message
        assert HOST not in message and "https://" not in message
        assert record.exc_info is None


async def test_unauthorized_invalidates_the_destination(env, monkeypatch):
    use(monkeypatch, Api(status=401))
    await btp_usage.monthly_cost(FIRST, LAST)
    assert env["resolver"].invalidated == 1


async def test_a_resolve_failure_is_destination_error(env, monkeypatch, caplog):
    env["resolver"].fail = DestinationError(SERVICE_TEXT)
    api = use(monkeypatch, Api())
    caplog.set_level(logging.DEBUG)
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "destination_error"
    assert api.requests == []
    assert all("secret" not in r.getMessage() for r in caplog.records)


async def test_a_declared_length_over_the_cap_is_too_large_unread(env, monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-length": str(5 * 1024 * 1024)},
                              stream=ExplodingStream())
    monkeypatch.setattr(btp_usage, "_transport", httpx.MockTransport(handler))
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "too_large"


async def test_the_deadline_is_usage_unavailable(env, monkeypatch):
    import asyncio

    monkeypatch.setattr(btp_usage, "TIMEOUT_SECONDS", 0.05)

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(env["resolver"], "resolve", slow)
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "usage_unavailable"


async def test_httpx_request_line_is_not_logged(env, monkeypatch, caplog):
    use(monkeypatch, Api())
    caplog.set_level(logging.DEBUG, logger="httpx")
    await btp_usage.monthly_cost(FIRST, LAST)
    assert not [r for r in caplog.records if r.name == "httpx" and HOST in r.getMessage()]


# --- caching ---------------------------------------------------------------------

async def test_an_answer_is_cached_six_hours(env, monkeypatch):
    api = use(monkeypatch, Api())
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "ok"
    env["clock"][0] += 6 * 3600 - 1
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "ok"
    assert len(api.requests) == 1
    env["clock"][0] += 2
    await btp_usage.monthly_cost(FIRST, LAST)
    assert len(api.requests) == 2


async def test_a_failure_is_cached_five_minutes_only(env, monkeypatch):
    api = use(monkeypatch, Api(status=500))
    await btp_usage.monthly_cost(FIRST, LAST)
    env["clock"][0] += 299
    await btp_usage.monthly_cost(FIRST, LAST)
    assert len(api.requests) == 1
    env["clock"][0] += 2
    api.status = 200
    assert (await btp_usage.monthly_cost(FIRST, LAST)).state == "ok"
    assert len(api.requests) == 2


# --- imports ---------------------------------------------------------------------

def test_the_module_imports_only_the_destination_layer_and_the_log_filter():
    tree = ast.parse(inspect.getsource(btp_usage))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom)}
    assert {n for n in imported if n.startswith("agents")} == {
        "agents.destination", "agents.bitbucket_tools"}
    assert "sqlalchemy" not in imported
