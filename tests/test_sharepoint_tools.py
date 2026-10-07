"""The ``builtin:sharepoint`` toolset: two tools, both auth modes, refusals.

No network, no tenant required.

Run:  python -m pytest tests/test_sharepoint_tools.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
for _var in ("DESTINATION_CLIENT_ID", "DESTINATION_CLIENT_SECRET",
             "DESTINATION_URI", "DESTINATION_TOKEN_URL", "DESTINATION_UAA_URL"):
    os.environ.pop(_var, None)

import httpx  # noqa: E402
import pytest  # noqa: E402

import agents.sharepoint_tools as tools  # noqa: E402
from agents.destination import Destination, DestinationError  # noqa: E402
from agents.destination_auth import destination_http_client  # noqa: E402
from agents.sharepoint_tools import BUILTIN_SHAREPOINT_URL, sharepoint_toolset  # noqa: E402
from tests.sharepoint_helpers import (  # noqa: E402
    DOWNLOAD_URL,
    PINS,
    VIEWS,
    FakeGraph,
    build_workbook,
)

WEEK = {
    ("Ann Example", "Presence"): {"2026-01-05": "I", "2026-01-06": "T"},
    ("Ann Example", "Guard"): {"2026-01-05": "GDI", "2026-01-06": "GDI"},
}
WORKBOOK = build_workbook({2026: WEEK})
CONFIG = {**PINS, "views": VIEWS, "destination": "GRAPH"}
# Planted in remote text, cell text and exception text: must come out nowhere.
MARKER = "zz-planted-marker-zz"
SECRETS = (MARKER, "graph-text", "tempauth", "t0k", DOWNLOAD_URL, "graph-token")


def _toolset(graph: FakeGraph, config: dict | None = None, **kw):
    return sharepoint_toolset(config or CONFIG, http=graph.client(),
                              download_transport=graph.download_transport(), **kw)


async def _call(toolset, name: str, **args):
    return await toolset.tools[name].function(**args)


def _loud(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "agents.sharepoint_tools"):
        caplog.set_level(logging.DEBUG, logger=name)


def _said(out) -> str:
    return json.dumps(out, ensure_ascii=False, default=str) + repr(out)


def _clean(out, caplog) -> None:
    for secret in SECRETS:
        assert secret not in _said(out), secret
        assert secret not in caplog.text, secret
    for record in caplog.records:
        assert record.exc_info is None and not record.stack_info


def test_the_toolset_has_exactly_two_tools_with_view_and_dates_only():
    toolset = _toolset(FakeGraph(WORKBOOK))
    assert set(toolset.tools) == {"read_table", "read_calendar"}
    assert list(inspect.signature(toolset.tools["read_table"].function).parameters) == ["view"]
    assert list(inspect.signature(toolset.tools["read_calendar"].function).parameters) == [
        "view", "date_from", "date_to"]
    assert toolset.http_client is not None


async def test_read_table_returns_pinned_columns_and_the_modified_time():
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_table", view="team")
    assert out["view"] == "team" and out["row_count"] == 3
    assert out["columns"] == ["Name", "Team", "ID"]
    assert out["rows"][0] == {"Name": "Ann Example", "Team": "Basis", "ID": 1001}
    assert out["last_modified"] == "2026-01-02T08:00:00Z"


async def test_read_calendar_returns_runs_conflicts_and_counts():
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_calendar", view="planning",
                      date_from="2026-01-05", date_to="2026-01-11")
    assert out["from"] == "2026-01-05" and out["to"] == "2026-01-11"
    assert out["view"] == "planning" and out["sheets"] == ["2026"]
    guard = [r for r in out["runs"] if r["kind"] == "Guard"]
    # ``ID`` comes from the lookup's table view: the toolset always passes it.
    assert guard == [{"member": "Ann Example", "team": "Basis", "kind": "Guard",
                      "status": "GDI", "from": "2026-01-05", "to": "2026-01-06", "ID": 1001}]
    assert out["conflicts"] == [{"member": "Ann Example", "from": "2026-01-05",
                                 "to": "2026-01-05", "status": "GDI",
                                 "against": "unavailable"}]
    assert (out["skipped_rows"], out["unmapped"], out["lookup_misses"]) == (0, 0, 0)
    assert out["last_modified"] == "2026-01-02T08:00:00Z"


async def test_two_tool_calls_of_one_run_download_once():
    graph = FakeGraph(WORKBOOK)
    toolset = _toolset(graph)
    await _call(toolset, "read_table", view="team")
    await _call(toolset, "read_calendar", view="planning",
                date_from="2026-01-05", date_to="2026-01-11")
    assert len(graph.downloads) == 1


@pytest.mark.parametrize("tool, args, code", [
    ("read_table", {"view": "nope"}, "unknown_view"),
    ("read_table", {"view": "planning"}, "unknown_view"),
    ("read_table", {"view": ["team"]}, "unknown_view"),
    ("read_calendar", {"view": "team", "date_from": "2026-01-05", "date_to": "2026-01-06"},
     "unknown_view"),
    ("read_calendar", {"view": "planning", "date_from": "2026-01-05", "date_to": "2026-06-30"},
     "window_too_long"),
    ("read_calendar", {"view": "planning", "date_from": "tomorrow", "date_to": "2026-01-06"},
     "invalid_dates"),
    ("read_calendar", {"view": "planning", "date_from": "2026-01-06", "date_to": "2026-01-05"},
     "invalid_dates"),
    ("read_calendar", {"view": "planning", "date_from": None, "date_to": 20260106},
     "invalid_dates"),
    ("read_calendar", {"view": "planning", "date_from": "2027-01-05", "date_to": "2027-01-06"},
     "sheet_not_found"),
])
async def test_every_refusal_is_an_error_object(tool, args, code):
    out = await _call(_toolset(FakeGraph(WORKBOOK)), tool, **args)
    assert set(out) == {"error"} and out["error"]["code"] == code
    assert isinstance(out["error"]["message"], str)
    assert set(out["error"]) <= {"code", "message", "hint"}


async def test_an_unknown_view_is_answered_with_the_names_of_that_kind():
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_table", view=MARKER)
    assert out["error"]["hint"] == "views: team"
    assert MARKER not in _said(out)
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_calendar", view="x",
                      date_from="2026-01-05", date_to="2026-01-06")
    assert out["error"]["hint"] == "views: planning"


@pytest.mark.parametrize("args", [
    {"view": "planning", "date_from": "2026-01-05", "date_to": "2026-12-31"},
    {"view": "nope", "date_from": "2026-01-05", "date_to": "2026-01-06"},
    {"view": "planning", "date_from": "x", "date_to": "2026-01-06"},
])
async def test_a_refusal_is_decided_before_anything_is_fetched(args):
    graph = FakeGraph(WORKBOOK)
    out = await _call(_toolset(graph), "read_calendar", **args)
    assert "error" in out
    assert graph.requests == [] and graph.downloads == []


async def test_graph_and_workbook_failures_are_error_objects_too(monkeypatch):
    out = await _call(_toolset(FakeGraph(WORKBOOK, status={"site": 403})), "read_table",
                      view="team")
    assert out["error"]["code"] == "graph_forbidden" and "graph-text" not in repr(out)
    cold = build_workbook({2026: WEEK}, date_formulas=True)
    out = await _call(_toolset(FakeGraph(cold)), "read_calendar", view="planning",
                      date_from="2026-01-05", date_to="2026-01-06")
    assert out["error"]["code"] == "no_cached_values"

    def broken(*a, **k):
        raise RuntimeError("cell text: Ann Example is ill")

    monkeypatch.setattr(tools, "read_table_view", broken)
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_table", view="team")
    assert out == {"error": {"code": "read_failed", "message": "the workbook could not be read"}}


# --- nothing remote, nothing typed into a cell, no exception text ----------

@pytest.mark.parametrize("status, code", [
    ({"site": 401}, "graph_unauthorized"),
    ({"site": 404}, "site_not_found"),
    ({"drives": 429}, "graph_throttled"),
    ({"item": 500}, "graph_error"),
    ({"item": 404}, "file_not_found"),
    ({"download": 302}, "download_redirect"),
    ({"download": 500}, "download_failed"),
])
async def test_a_remote_failure_says_nothing_remote(status, code, caplog):
    _loud(caplog)
    for tool, args in (("read_table", {"view": "team"}),
                       ("read_calendar", {"view": "planning", "date_from": "2026-01-05",
                                          "date_to": "2026-01-06"})):
        out = await _call(_toolset(FakeGraph(WORKBOOK, status=status)), tool, **args)
        assert set(out) == {"error"} and out["error"]["code"] == code
        _clean(out, caplog)


async def test_the_download_url_is_in_no_answer_and_no_log_line(caplog):
    _loud(caplog)
    toolset = _toolset(FakeGraph(WORKBOOK))
    table = await _call(toolset, "read_table", view="team")
    runs = await _call(toolset, "read_calendar", view="planning",
                       date_from="2026-01-05", date_to="2026-01-11")
    assert table["row_count"] == 3 and runs["runs"]
    # The Graph requests are logged (the filter is not what keeps this quiet).
    assert "graph.microsoft.com/v1.0/sites" in caplog.text
    _clean(table, caplog)
    _clean(runs, caplog)


async def test_text_graph_sends_as_the_modified_time_is_not_passed_on(caplog):
    _loud(caplog)
    for value in (MARKER, f"2026-01-02T08:00:00Z {MARKER}", "2026-01-02T08:00:00Z\n", 7, None,
                  {"x": MARKER}):
        graph = FakeGraph(WORKBOOK, item={"lastModifiedDateTime": value})
        out = await _call(_toolset(graph), "read_table", view="team")
        assert out["row_count"] == 3 and out["last_modified"] is None
        _clean(out, caplog)
    graph = FakeGraph(WORKBOOK, item={"lastModifiedDateTime": "2026-01-02T08:00:00.1234567Z"})
    out = await _call(_toolset(graph), "read_table", view="team")
    assert out["last_modified"] == "2026-01-02T08:00:00.1234567Z"


async def test_cell_text_outside_the_view_is_in_no_answer_and_no_log_line(caplog):
    _loud(caplog)
    week = {
        ("Ann Example", "Presence"): {"2026-01-05": f"{MARKER} code", "2026-01-06": "T"},
        ("Ann Example", "Guard"): {"2026-01-05": "GDI"},
    }
    book = build_workbook(
        {2026: week},
        team=[("Ann Example", "Basis", 1001, f"{MARKER} unpinned column")],
        extra_rows={2026: [
            ("Bob Sample", "Basis", f"{MARKER} kind"),
            (f"{MARKER} " * 20, "Basis", "Presence"),
            ("Cy Placeholder", f"{MARKER}\nteam", "Presence"),
        ]},
        raw={2026: {"A1": f"{MARKER} title", "ZZ3": MARKER}},
    )
    toolset = _toolset(FakeGraph(book))
    out = await _call(toolset, "read_calendar", view="planning",
                      date_from="2026-01-05", date_to="2026-01-11")
    assert out["skipped_rows"] == 3 and out["unmapped"] == 1
    assert {r["member"] for r in out["runs"]} == {"Ann Example"}
    _clean(out, caplog)
    table = await _call(toolset, "read_table", view="team")
    assert table["rows"] == [{"Name": "Ann Example", "Team": "Basis", "ID": 1001}]
    _clean(table, caplog)


@pytest.mark.parametrize("where", ["fetch", "table", "calendar", "size"])
async def test_an_unexpected_error_is_logged_by_class_only(where, monkeypatch, caplog):
    _loud(caplog)

    class Odd(Exception):
        pass

    def broken(*a, **k):
        raise Odd(f"{MARKER} {DOWNLOAD_URL}")

    async def broken_fetch(self):
        broken()

    if where == "fetch":
        monkeypatch.setattr(tools.SharePointFile, "fetch", broken_fetch)
    elif where == "table":
        monkeypatch.setattr(tools, "read_table_view", broken)
    elif where == "calendar":
        monkeypatch.setattr(tools, "read_calendar_view", broken)
    else:
        monkeypatch.setattr(tools, "_result_chars", broken)
    toolset = _toolset(FakeGraph(WORKBOOK))
    tool, args = ("read_calendar", {"view": "planning", "date_from": "2026-01-05",
                                    "date_to": "2026-01-06"}) if where == "calendar" \
        else ("read_table", {"view": "team"})
    out = await _call(toolset, tool, **args)
    assert out == {"error": {"code": "read_failed", "message": "the workbook could not be read"}}
    mine = [r for r in caplog.records if r.name == "agents.sharepoint_tools"]
    assert [r.getMessage() for r in mine] == ["builtin:sharepoint: read failed (Odd)"]
    _clean(out, caplog)


async def test_a_cancellation_is_not_answered_as_a_refusal(monkeypatch):
    async def cancelled(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(tools.SharePointFile, "fetch", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _call(_toolset(FakeGraph(WORKBOOK)), "read_table", view="team")


async def test_a_result_that_does_not_fit_is_refused_not_cut_off(monkeypatch):
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 200)
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_calendar", view="planning",
                      date_from="2026-01-05", date_to="2026-01-11")
    assert set(out) == {"error"} and out["error"]["code"] == "result_too_large"
    assert out["error"]["hint"] == "ask for a shorter period"
    out = await _call(_toolset(FakeGraph(WORKBOOK)), "read_table", view="team")
    assert set(out) == {"error"} and out["error"]["code"] == "result_too_large"
    assert "period" not in out["error"]["hint"]


async def test_a_result_at_the_cap_passes_and_one_character_more_does_not(monkeypatch):
    toolset = _toolset(FakeGraph(WORKBOOK))
    out = await _call(toolset, "read_table", view="team")
    size = len(json.dumps(out, ensure_ascii=False, default=str))
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", size)
    assert "rows" in await _call(toolset, "read_table", view="team")
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", size - 1)
    assert (await _call(toolset, "read_table", view="team"))["error"]["code"] == \
        "result_too_large"


async def test_parsing_runs_off_the_event_loop(monkeypatch):
    seen: list[int] = []
    real_table, real_calendar = tools.read_table_view, tools.read_calendar_view

    def spy_table(data, view):
        seen.append(threading.get_ident())
        return real_table(data, view)

    def spy_calendar(*args):
        seen.append(threading.get_ident())
        return real_calendar(*args)

    monkeypatch.setattr(tools, "read_table_view", spy_table)
    monkeypatch.setattr(tools, "read_calendar_view", spy_calendar)
    toolset = _toolset(FakeGraph(WORKBOOK))
    await _call(toolset, "read_table", view="team")
    await _call(toolset, "read_calendar", view="planning",
                date_from="2026-01-05", date_to="2026-01-06")
    assert len(seen) == 2 and threading.get_ident() not in seen


def test_misconfiguration_is_refused_at_build_time():
    graph = FakeGraph(WORKBOOK)
    with pytest.raises(ValueError, match="supports auth_mode"):
        _toolset(graph, auth_mode="oauth2")
    with pytest.raises(ValueError, match="no per-user mode"):
        _toolset(graph, {**CONFIG, "user_context": True}, auth_mode="destination")
    with pytest.raises(ValueError, match="no per-user mode"):
        _toolset(graph, {**CONFIG, "user_context": True}, auth_mode="app_only")
    with pytest.raises(ValueError, match="oauth.site"):
        _toolset(graph, {**CONFIG, "site": "evil.test:/sites/x"})
    with pytest.raises(ValueError, match="oauth.views"):
        _toolset(graph, {**PINS, "destination": "GRAPH"})


class _Resolver:
    name = "GRAPH"

    def __init__(self) -> None:
        self.calls: list = []

    async def resolve(self, *, force=False, user_token=None, principal=None) -> Destination:
        self.calls.append(user_token)
        return Destination(url="https://graph.microsoft.com",
                           headers={"Authorization": "Bearer dest-token"},
                           expires_at=time.monotonic() + 60,
                           auth_type="OAuth2ClientCredentials", per_user=False)

    def invalidate(self, principal=None) -> None:
        pass


async def test_destination_mode_sends_the_destinations_token_to_graph_only(caplog):
    _loud(caplog)
    graph = FakeGraph(WORKBOOK)
    resolver = _Resolver()
    http = destination_http_client(
        resolver, user_context=False, expected_hosts=("graph.microsoft.com",),
        server_key=BUILTIN_SHAREPOINT_URL, transport=httpx.MockTransport(graph._graph))
    toolset = sharepoint_toolset(CONFIG, http=http, auth_mode="destination",
                                 download_transport=graph.download_transport())
    out = await _call(toolset, "read_table", view="team")
    assert out["row_count"] == 3
    assert {r.headers["Authorization"] for r in graph.requests} == {"Bearer dest-token"}
    assert resolver.calls and set(resolver.calls) == {None}
    assert "authorization" not in graph.downloads[0].headers
    assert "dest-token" not in _said(out) and "dest-token" not in caplog.text


async def test_a_destination_that_cannot_be_resolved_is_an_error_object(caplog):
    _loud(caplog)

    class Failing(_Resolver):
        async def resolve(self, **kw):
            raise DestinationError("destination GRAPH: HTTP 404 secret-detail")

    http = destination_http_client(Failing(), server_key=BUILTIN_SHAREPOINT_URL,
                                   transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    out = await _call(sharepoint_toolset(CONFIG, http=http, auth_mode="destination"),
                      "read_table", view="team")
    assert out["error"]["code"] == "destination_error" and "secret-detail" not in repr(out)
    assert "secret-detail" not in caplog.text


def test_app_only_builds_a_client_credentials_graph_client():
    app_only = {**PINS, "views": VIEWS, "client_id": "cid", "client_secret": "s",
                "token_url": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
                "scope": "https://graph.microsoft.com/.default"}
    toolset = sharepoint_toolset(app_only, auth_mode="app_only")
    from agents.client_credentials import ClientCredentialsAuth

    assert isinstance(toolset.http_client.auth, ClientCredentialsAuth)
    assert str(toolset.http_client.base_url).startswith("https://graph.microsoft.com")
