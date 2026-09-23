"""Focused tests for the built-in Slack toolset (``builtin:slack``).

Covered: channel resolution (member-only by default, pinned allow-list when
set), ``ok: false`` handling and the one auth retry, history shaping and the
lookback window, threads, that posting tools exist only with ``allow_send``,
that posted text cannot mention anyone, the destination's static
``URL.headers.*`` support, payload validation, and storage.

No network, no workspace required.

Run:  python -m pytest tests/test_slack_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import httpx  # noqa: E402
import pytest  # noqa: E402

from agents.destination import Destination  # noqa: E402
from agents.slack_tools import SlackClient, SlackError, _escape, slack_toolset  # noqa: E402

API = "https://slack.com/api"
CHANNELS = [
    {"id": "C1", "name": "general", "is_member": True, "purpose": {"value": "All hands"}},
    {"id": "C2", "name": "support", "is_member": True, "purpose": {"value": ""}},
    {"id": "C3", "name": "random", "is_member": False, "purpose": {"value": ""}},
]


class Resolver:
    def __init__(self) -> None:
        self.invalidated = 0

    async def resolve(self, *, force: bool = False) -> Destination:
        return Destination(
            url=API, headers={"Authorization": "Bearer xoxb-test"}, expires_at=time.monotonic() + 60
        )

    def invalidate(self) -> None:
        self.invalidated += 1


class Slack:
    """A mock Slack Web API recording every call."""

    def __init__(self, history: list[dict] | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.history = history or []
        self.fail_auth_once = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        if self.fail_auth_once:
            self.fail_auth_once = False
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        if method == "conversations.list":
            return httpx.Response(200, json={"ok": True, "channels": CHANNELS})
        if method == "conversations.history":
            return httpx.Response(200, json={"ok": True, "messages": self.history})
        if method == "conversations.replies":
            return httpx.Response(200, json={"ok": True, "messages": [
                {"ts": "100.0", "user": "U1", "text": "root"},
                {"ts": "101.0", "user": "U2", "text": "a reply"},
            ]})
        if method == "users.info":
            uid = request.url.params["user"]
            if uid == "U2":
                return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
            return httpx.Response(200, json={"ok": True, "user": {"profile": {"display_name": "Ann"}}})
        if method == "chat.postMessage":
            return httpx.Response(200, json={"ok": True, "ts": "200.0"})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def run(coro):
    return asyncio.run(coro)


def test_default_scope_is_channels_the_bot_is_in():
    slack = Slack()
    client = SlackClient(Resolver(), slack.client())
    assert [c["name"] for c in run(client.list_channels())] == ["general", "support"]
    assert slack.calls[0].headers["Authorization"] == "Bearer xoxb-test"


def test_pinned_channels_narrow_and_refuse_others():
    client = SlackClient(Resolver(), Slack().client(), channels=["#support"])
    assert [c["name"] for c in run(client.list_channels())] == ["support"]
    with pytest.raises(ValueError, match="no channel 'general'"):
        run(client.list_messages("general"))


def test_history_is_shaped_filtered_and_oldest_first():
    now = time.time()
    slack = Slack(history=[
        {"ts": f"{now - 60:.6f}", "user": "U1", "text": "a &amp; b &lt;x&gt;", "reply_count": 2},
        {"ts": f"{now - 90:.6f}", "subtype": "channel_join", "user": "U3", "text": "joined"},
        {"ts": f"{now - 120:.6f}", "user": "U2", "text": "earlier"},
    ])
    client = SlackClient(Resolver(), slack.client(), lookback_minutes=60)
    out = run(client.list_messages("support", limit=10, lookback_minutes=24 * 60))
    assert [m["text"] for m in out] == ["earlier", "a & b <x>"]
    assert out[1]["from"] == "Ann" and out[1]["reply_count"] == 2
    # users.info without the scope falls back to the id, and is asked once.
    assert out[0]["from"] == "U2"
    history = next(c for c in slack.calls if c.url.path.endswith("conversations.history"))
    assert history.url.params["channel"] == "C2"
    oldest = float(history.url.params["oldest"])
    assert now - 3600 - 5 < oldest < now - 3600 + 5, "the ceiling beats a wider request"


def test_thread_returns_root_and_replies():
    client = SlackClient(Resolver(), Slack().client())
    thread = run(client.get_thread("general", "100.0"))
    assert thread["text"] == "root"
    assert [r["text"] for r in thread["replies"]] == ["a reply"]


def test_ok_false_raises_and_auth_errors_retry_once():
    slack = Slack()
    resolver = Resolver()
    client = SlackClient(resolver, slack.client())
    slack.fail_auth_once = True
    assert run(client.list_channels())
    assert resolver.invalidated == 1

    with pytest.raises(SlackError, match="unknown_method"):
        run(client._call("nope.method"))


def test_posted_text_cannot_mention_anyone():
    assert _escape("hi <!channel> & <@U1>") == "hi &lt;!channel&gt; &amp; &lt;@U1&gt;"
    slack = Slack()
    client = SlackClient(Resolver(), slack.client())
    run(client.reply_in_thread("general", "100.0", "ping <!here>"))
    post = slack.calls[-1]
    assert post.method == "POST"
    assert json.loads(post.content) == {
        "channel": "C1", "thread_ts": "100.0", "text": "ping &lt;!here&gt;",
    }


def test_posting_tools_exist_only_with_allow_send():
    base = {"destination": "SLACK"}
    read_only = slack_toolset(base, http=Slack().client(), resolver=Resolver())
    assert set(read_only.tools) == {"list_channels", "list_messages", "get_thread"}
    # The string "true" is not True: the switch opens only on a real boolean.
    not_quite = slack_toolset({**base, "allow_send": "true"}, http=Slack().client(), resolver=Resolver())
    assert "post_message" not in not_quite.tools
    posting = slack_toolset({**base, "allow_send": True}, http=Slack().client(), resolver=Resolver())
    assert {"post_message", "reply_in_thread"} <= set(posting.tools)


def test_toolset_refuses_other_modes_and_a_missing_destination():
    with pytest.raises(ValueError, match="requires auth_mode 'destination'"):
        slack_toolset({"destination": "SLACK"}, auth_mode="oauth2")
    with pytest.raises(ValueError, match="requires a 'destination'"):
        slack_toolset({}, auth_mode="destination")


def test_destination_static_authorization_header_is_accepted():
    from agents.destination import DestinationResolver, DestinationServiceConfig

    resolver = DestinationResolver("SLACK", DestinationServiceConfig(
        client_id="c", client_secret="s", token_url="https://t", api_url="https://d",
    ))
    resolved = resolver._destination_from({
        "destinationConfiguration": {
            "URL": "https://slack.com/api/",
            "Authentication": "NoAuthentication",
            "URL.headers.Authorization": "Bearer xoxb-1",
        }
    })
    assert resolved.url == API
    assert resolved.headers == {"Authorization": "Bearer xoxb-1"}


def test_destination_without_any_credential_is_still_refused():
    from agents.destination import DestinationError, DestinationResolver, DestinationServiceConfig

    resolver = DestinationResolver("SLACK", DestinationServiceConfig(
        client_id="c", client_secret="s", token_url="https://t", api_url="https://d",
    ))
    with pytest.raises(DestinationError, match="carries neither"):
        resolver._destination_from({"destinationConfiguration": {"URL": "https://x"}})


def test_payload_requires_destination_mode():
    from pydantic import ValidationError

    from agents.admin import McpServerPayload

    McpServerPayload(url="builtin:slack", auth_mode="destination", oauth={"destination": "SLACK"})
    with pytest.raises(ValidationError, match="requires auth_mode=destination"):
        McpServerPayload(url="builtin:slack", auth_mode="oauth2", oauth={"dcr": True})


def test_storage_keeps_slack_keys_and_leaves_jira_alone():
    from agents.db import _clean_oauth

    slack = _clean_oauth(
        {"destination": "SLACK", "channels": ["support", "general"], "lookback": "1d",
         "allow_send": True, "project": "ABC"},
        "destination", None, url="builtin:slack",
    )
    assert slack == {
        "destination": "SLACK", "channels": "support, general", "lookback": "1d", "allow_send": True,
    }
    jira = _clean_oauth(
        {"destination": "JIRA", "project": "ABC", "allow_send": True},
        "destination", None, url="builtin:jira",
    )
    assert jira == {"destination": "JIRA", "project": "ABC", "allow_comment": False}
