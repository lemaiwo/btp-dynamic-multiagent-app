"""Focused tests for the built-in Teams toolset (``builtin:teams``).

Covered: channel resolution inside the pinned allow-list, HTML-to-text, the
lookback window applied client-side, thread reads, that posting tools exist
only with ``allow_send``, the payload validation, and that the pinned config
survives storage under both supported auth modes.

No network, no tenant required.

Run:  python -m pytest tests/test_teams_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents.teams_tools import (  # noqa: E402
    BUILTIN_TEAMS_URL,
    TeamsClient,
    _html_to_text,
    teams_toolset,
)

TEAM = "team-guid"
CHANNELS = {
    "value": [
        {"id": "19:general@thread.tacv2", "displayName": "General", "description": ""},
        {"id": "19:support@thread.tacv2", "displayName": "Support", "description": "Questions"},
        {"id": "19:secret@thread.tacv2", "displayName": "Leadership", "description": ""},
    ]
}


def _ago(minutes: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _msg(mid: str, minutes_ago: int, text: str, **extra) -> dict:
    return {
        "id": mid,
        "messageType": "message",
        "createdDateTime": _ago(minutes_ago),
        "subject": None,
        "body": {"contentType": "html", "content": text},
        "from": {"user": {"displayName": "Ann", "id": "u1"}},
        "webUrl": f"https://teams.example/{mid}",
        **extra,
    }


class Graph:
    """A mock Graph that records requests and answers a fixed set of paths."""

    def __init__(self, messages: list[dict] | None = None, pages: list[list[dict]] | None = None):
        self.requests: list[httpx.Request] = []
        self.pages = pages or [messages or []]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/channels"):
            return httpx.Response(200, json=CHANNELS)
        if path.endswith("/replies") and request.method == "GET":
            return httpx.Response(200, json={"value": [
                _msg("r2", 5, "<p>second</p>"),
                _msg("r1", 10, "<p>first</p>"),
                _msg("sys", 7, "", messageType="systemEventMessage"),
            ]})
        if request.method == "POST":
            return httpx.Response(201, json={"id": "new", "webUrl": "https://teams.example/new"})
        if path.endswith("/messages"):
            page = int(request.url.params.get("page", "0"))
            body: dict = {"value": self.pages[page]}
            if page + 1 < len(self.pages):
                body["@odata.nextLink"] = (
                    f"https://graph.microsoft.com{path}?page={page + 1}"
                )
            return httpx.Response(200, json=body)
        if "/messages/" in path:
            return httpx.Response(200, json=_msg("m1", 20, "<p>root</p>"))
        return httpx.Response(404, json={"error": path})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://graph.microsoft.com", transport=httpx.MockTransport(self.handler)
        )


def run(coro):
    return asyncio.run(coro)


def test_html_to_text_keeps_structure_and_mentions():
    text = _html_to_text('<p>Hi <at id="0">Bob</at>,</p><p>a &amp; b<br>c</p>')
    assert text == "Hi Bob,\na & b\nc"


def test_channels_are_limited_to_the_allow_list():
    graph = Graph()
    client = TeamsClient(graph.client(), TEAM, channels=["support", "19:general@thread.tacv2"])
    names = [c["name"] for c in run(client.list_channels())]
    assert names == ["General", "Support"]
    with pytest.raises(ValueError, match="no channel 'Leadership'"):
        run(client.list_messages("Leadership"))


def test_list_messages_shapes_filters_and_orders():
    graph = Graph(messages=[
        _msg("m3", 5, "<p>newest</p>"),
        _msg("gone", 6, "<p>x</p>", deletedDateTime=_ago(1)),
        _msg("sys", 7, "", messageType="systemEventMessage"),
        _msg("m2", 30, "<p>middle</p>"),
        _msg("m1", 60 * 24 * 3, "<p>old</p>"),
    ])
    client = TeamsClient(graph.client(), TEAM, lookback_minutes=60 * 24)
    out = run(client.list_messages("Support", limit=10))
    assert [m["message_id"] for m in out] == ["m2", "m3"]
    assert out[1]["body"] == "newest"
    assert out[1]["from"] == "Ann"
    first = graph.requests[1]
    assert first.url.raw_path.split(b"?")[0] == b"/v1.0/teams/team-guid/channels/19%3Asupport%40thread.tacv2/messages"
    assert first.url.params["$top"] == "10"


def test_list_messages_window_cannot_widen_the_ceiling():
    graph = Graph(messages=[_msg("m2", 30, "a"), _msg("m1", 180, "b")])
    client = TeamsClient(graph.client(), TEAM, lookback_minutes=60)
    out = run(client.list_messages("General", lookback_minutes=60 * 24))
    assert [m["message_id"] for m in out] == ["m2"]


def test_list_messages_follows_graph_next_links_only():
    graph = Graph(pages=[[_msg("m2", 5, "a")], [_msg("m1", 10, "b")]])
    client = TeamsClient(graph.client(), TEAM)
    out = run(client.list_messages("General", limit=5))
    assert [m["message_id"] for m in out] == ["m1", "m2"]

    hostile = Graph(pages=[[_msg("m2", 5, "a")], [_msg("m1", 10, "b")]])
    original = hostile.handler

    def rewrite(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/messages"):
            body = json.loads(response.content)
            if "@odata.nextLink" in body:
                body["@odata.nextLink"] = "https://evil.example/steal"
            return httpx.Response(200, json=body)
        return response

    hostile.handler = rewrite
    client = TeamsClient(hostile.client(), TEAM)
    run(client.list_messages("General", limit=5))
    assert all(r.url.host == "graph.microsoft.com" for r in hostile.requests)


def test_get_thread_returns_root_and_ordered_replies():
    graph = Graph()
    client = TeamsClient(graph.client(), TEAM)
    thread = run(client.get_thread("Support", "m1"))
    assert thread["body"] == "root"
    assert [r["message_id"] for r in thread["replies"]] == ["r1", "r2"]


def test_reply_posts_html_to_the_thread():
    graph = Graph()
    client = TeamsClient(graph.client(), TEAM)
    out = run(client.reply_to_message("Support", "m1", "Hello\n\nWorld <b>"))
    post = graph.requests[-1]
    assert post.method == "POST"
    assert post.url.raw_path.endswith(b"/channels/19%3Asupport%40thread.tacv2/messages/m1/replies")
    assert json.loads(post.content)["body"] == {
        "contentType": "html",
        "content": "<p>Hello</p><p>World &lt;b&gt;</p>",
    }
    assert out["message_id"] == "new"


def _tool_names(toolset) -> set[str]:
    return set(toolset.tools)


def test_posting_tools_exist_only_with_allow_send():
    graph = Graph()
    read_only = teams_toolset({"team": TEAM}, http=graph.client())
    assert _tool_names(read_only) == {"list_channels", "list_messages", "get_thread"}
    posting = teams_toolset({"team": TEAM, "allow_send": True}, http=graph.client())
    assert {"post_message", "reply_to_message"} <= _tool_names(posting)


def test_toolset_refuses_misconfiguration():
    graph = Graph()
    with pytest.raises(ValueError, match="requires a 'team'"):
        teams_toolset({}, http=graph.client())
    with pytest.raises(ValueError, match="cannot post under auth_mode 'app_only'"):
        teams_toolset(
            {"team": TEAM, "allow_send": True}, http=graph.client(), auth_mode="app_only"
        )
    with pytest.raises(ValueError, match="supports auth_mode"):
        teams_toolset({"team": TEAM}, http=graph.client(), auth_mode="destination")


def test_teams_is_a_known_builtin():
    from agents.builtins import BUILTIN_URLS, is_builtin_url

    assert BUILTIN_TEAMS_URL in BUILTIN_URLS
    assert is_builtin_url("BUILTIN:TEAMS")


_OAUTH2 = {
    "client_id": "cid",
    "client_secret": "secret",
    "authorize_url": "https://login.microsoftonline.com/t/oauth2/v2.0/authorize",
    "token_url": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
    "scope": "offline_access ChannelMessage.Read.All",
    "team": TEAM,
}
_APP_ONLY = {
    "client_id": "cid",
    "client_secret": "secret",
    "token_url": "https://login.microsoftonline.com/t/oauth2/v2.0/token",
    "scope": "https://graph.microsoft.com/.default",
    "team": TEAM,
}


def test_payload_accepts_both_supported_modes():
    from agents.admin import McpServerPayload

    McpServerPayload(url="builtin:teams", auth_mode="oauth2", oauth=_OAUTH2)
    # No mailbox needed: the team is the target.
    McpServerPayload(url="builtin:teams", auth_mode="app_only", oauth=_APP_ONLY)


@pytest.mark.parametrize(
    "mode, oauth, match",
    [
        ("jwt", None, "requires auth_mode=oauth2"),
        ("oauth2", {**_OAUTH2, "team": ""}, "requires oauth.team"),
        ("oauth2", {"dcr": True}, "cannot use DCR"),
        ("app_only", {**_APP_ONLY, "allow_send": True}, "cannot post under auth_mode=app_only"),
    ],
)
def test_payload_rejects(mode, oauth, match):
    from pydantic import ValidationError

    from agents.admin import McpServerPayload

    with pytest.raises(ValidationError, match=match):
        McpServerPayload(url="builtin:teams", auth_mode=mode, oauth=oauth)


def test_pinned_config_survives_storage():
    from agents.db import _clean_oauth

    stored = _clean_oauth(
        {**_OAUTH2, "channels": ["Support", "General"], "lookback": "2d", "allow_send": True},
        "oauth2", None, url="builtin:teams",
    )
    assert stored["team"] == TEAM
    assert stored["channels"] == "Support, General"
    assert stored["lookback"] == "2d"
    assert stored["allow_send"] is True

    app = _clean_oauth({**_APP_ONLY, "channels": "Support"}, "app_only", None, url="builtin:teams")
    assert app["team"] == TEAM and app["channels"] == "Support"


def test_other_oauth2_servers_store_what_they_did_before():
    from agents.db import _clean_oauth

    stored = _clean_oauth(
        {**_OAUTH2, "lookback": "2d", "allow_send": True},
        "oauth2", None, url="https://mcp.example.com/mcp",
    )
    assert "team" not in stored and "lookback" not in stored and "allow_send" not in stored
