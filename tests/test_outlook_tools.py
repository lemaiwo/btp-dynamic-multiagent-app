"""Focused tests for the built-in Outlook toolset (``builtin:outlook``).

Microsoft's hosted Outlook MCP server is gated behind a Copilot licence and an
admin-registered enterprise app (see docs/OUTLOOK_SETUP.md), so the tools call
Microsoft Graph directly, the same way the Gmail toolset does.

The queue is an Inbox subfolder rather than a label: listing the folder is the
search, and moving a message out of it is what marks the mail handled.

Covered: folder resolution, request shaping, HTML-vs-text body handling, the
two-step reply draft, the HTML conversion a sent reply needs, move destinations,
and that both built-ins are dispatched and validated. Whether the send tool is
registered at all is `tests/test_client_credentials.py`, next to `allow_send`.

No network, no live mailbox, no tenant required.

Run:  python tests/test_outlook_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)
os.environ["MCP_URL_ALLOWLIST"] = ""

import httpx  # noqa: E402

from agents.builtins import BUILTIN_URLS, build_builtin_toolset, is_builtin_url  # noqa: E402
from agents.gmail_tools import BUILTIN_GMAIL_URL  # noqa: E402
from agents.jira_tools import BUILTIN_JIRA_URL  # noqa: E402
from agents.outlook_tools import (  # noqa: E402
    BUILTIN_OUTLOOK_URL,
    OutlookClient,
    outlook_toolset,
)

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


CHILD_FOLDERS = {
    "value": [
        {"id": "AAA-agent", "displayName": "agent"},
        {"id": "AAA-done", "displayName": "agent-done"},
        {"id": "AAA-news", "displayName": "Newsletters"},
    ]
}

MESSAGES = {
    "value": [
        {
            "id": "msg1",
            "conversationId": "conv1",
            "subject": "question workzone",
            "from": {"emailAddress": {"name": "Wouter", "address": "wouter@lemaire.tech"}},
            "receivedDateTime": "2026-08-21T22:25:14Z",
            "bodyPreview": "how do I configure btp workzone?",
            "isRead": False,
        }
    ]
}

MESSAGE_FULL = {
    "id": "msg1",
    "conversationId": "conv1",
    "subject": "question workzone",
    "from": {"emailAddress": {"name": "Wouter", "address": "wouter@lemaire.tech"}},
    "toRecipients": [{"emailAddress": {"address": "wouter@lemaire.tech"}}],
    "receivedDateTime": "2026-08-21T22:25:14Z",
    "body": {"contentType": "text", "content": "how do I configure btp workzone?"},
}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = {}
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "path": path,
            "body": body,
            "headers": dict(request.headers),
        })

        if path.endswith("/mailFolders/inbox/childFolders"):
            return httpx.Response(200, json=CHILD_FOLDERS)
        if path.endswith("/mailFolders/AAA-agent/messages"):
            return httpx.Response(200, json=MESSAGES)
        if path.endswith("/messages/msg1/createReply"):
            return httpx.Response(201, json={"id": "draft9", "conversationId": "conv1"})
        # Graph answers the reply action with 202 Accepted and an empty body.
        if path.endswith("/messages/msg1/reply"):
            return httpx.Response(202)
        if path.endswith("/messages/msg1/move"):
            return httpx.Response(201, json={"id": "msg1-moved"})
        if path.endswith("/messages/draft9"):
            return httpx.Response(200, json={"id": "draft9"})
        if path.endswith("/messages/msg1"):
            return httpx.Response(200, json=MESSAGE_FULL)
        return httpx.Response(404, json={"error": {"message": f"unmapped {path}"}})

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]

    def find(self, needle: str) -> list[dict]:
        return [c for c in self.calls if c["path"].endswith(needle)]


def _client(rec: Recorder) -> OutlookClient:
    return OutlookClient(httpx.AsyncClient(
        base_url="https://graph.microsoft.com",
        transport=httpx.MockTransport(rec.handler),
    ))


async def main() -> None:
    # --- dispatch and validation -------------------------------------------
    print("\n== builtin registry ==")
    # A subset, not an equality: the set is closed but it grows, and pinning
    # it here made adding a built-in fail a mail test for no reason. What the
    # whole set contains is `tests/test_builtins.py`.
    check("the three mail/issue built-ins are registered",
          {BUILTIN_GMAIL_URL, BUILTIN_OUTLOOK_URL, BUILTIN_JIRA_URL} <= BUILTIN_URLS,
          f"got {sorted(BUILTIN_URLS)}")
    check("recognises builtin:outlook", is_builtin_url(BUILTIN_OUTLOOK_URL))
    check("still recognises builtin:gmail", is_builtin_url(BUILTIN_GMAIL_URL))
    check("rejects unknown builtins", not is_builtin_url("builtin:teams"))
    try:
        build_builtin_toolset("builtin:teams", {})
        check("dispatch rejects unknown", False, "no exception")
    except ValueError as e:
        check("dispatch rejects unknown", "builtin:teams" in str(e))

    # --- list_pending -------------------------------------------------------
    # The folder IS the queue: no search query, so no query-syntax to get wrong.
    # The name is resolved to an opaque id first.
    print("\n== list_pending ==")
    rec = Recorder()
    rows = await _client(rec).list_pending("agent", limit=5)
    check("resolves the folder by display name",
          any("childFolders" in p for p in rec.paths()), f"{rec.paths()}")
    check("lists messages from the resolved id",
          any("/mailFolders/AAA-agent/messages" in p for p in rec.paths()), f"{rec.paths()}")
    listing = rec.find("/mailFolders/AAA-agent/messages")
    params = httpx.URL(listing[0]["url"]).params if listing else {}
    check("caps the page size", params.get("$top") == "5", f"got {params.get('$top')}")
    check("returns one row", len(rows) == 1, f"got {rows}")
    row = rows[0] if rows else {}
    check("row carries the id", row.get("message_id") == "msg1")
    check("row carries the subject", row.get("subject") == "question workzone")
    check("row flattens the sender", row.get("from") == "wouter@lemaire.tech", f"got {row}")
    check("row omits the body", "body" not in row)

    # An oversized limit must be clamped, not forwarded.
    rec = Recorder()
    await _client(rec).list_pending("agent", limit=9999)
    top = httpx.URL(rec.find("/messages")[0]["url"]).params.get("$top")
    check("clamps an oversized limit", top is not None and int(top) <= 50, f"got {top}")

    # A folder that is not there must say so rather than return an empty queue:
    # "no mail waiting" and "wrong folder name" are very different states.
    rec = Recorder()
    try:
        await _client(rec).list_pending("nope")
        check("unknown folder raises", False, "no exception")
    except Exception as e:
        check("unknown folder raises", "nope" in str(e), f"got {e}")

    # --- get_message --------------------------------------------------------
    # Graph returns HTML unless asked otherwise; the Prefer header is what makes
    # it hand back text, which is far cheaper than stripping tags here.
    print("\n== get_message ==")
    rec = Recorder()
    msg = await _client(rec).get_message("msg1")
    hdrs = rec.find("/messages/msg1")[0]["headers"]
    prefer = hdrs.get("prefer", "")
    check("asks Graph for a text body", "text" in prefer.lower(), f"prefer={prefer!r}")
    check("returns the body", "configure btp workzone" in msg.get("body", ""), f"got {msg}")
    check("returns the subject", msg.get("subject") == "question workzone")
    check("returns the sender", msg.get("from") == "wouter@lemaire.tech")

    rec = Recorder()
    short = await _client(rec).get_message("msg1", max_chars=10)
    check("truncates to max_chars", len(short["body"]) <= 30, f"len={len(short['body'])}")
    check("marks the truncation", "truncated" in short["body"].lower(), f"got {short['body']!r}")

    # --- create_reply_draft -------------------------------------------------
    # Two steps: createReply builds a correctly threaded draft, then PATCH puts
    # our text in it. That is why no MIME or In-Reply-To handling is needed.
    print("\n== create_reply_draft ==")
    rec = Recorder()
    res = await _client(rec).create_reply_draft("msg1", "Here is the answer.")
    check("calls createReply", len(rec.find("/createReply")) == 1, f"{rec.paths()}")
    patch = rec.find("/messages/draft9")
    check("patches the draft body", len(patch) == 1, f"{rec.paths()}")
    check("uses PATCH", bool(patch) and patch[0]["method"] == "PATCH",
          f"got {patch[0]['method'] if patch else None}")
    sent = patch[0]["body"] if patch else {}
    check("sends the body as text",
          (sent.get("body") or {}).get("contentType", "").lower() == "text", f"got {sent}")
    check("carries our text", "Here is the answer." in (sent.get("body") or {}).get("content", ""))
    check("returns the draft id", res.get("draft_id") == "draft9", f"got {res}")
    check("never calls send", not any("send" in p.lower() for p in rec.paths()), f"{rec.paths()}")

    # --- send_reply body formatting -----------------------------------------
    # Graph builds the reply as HTML -- the reply API's `Prefer: outlook.timezone`
    # note says the JSON path creates "the reply message in HTML ... based on the
    # request body". So a plain-text comment arrives with every newline collapsed
    # as HTML whitespace and the whole answer reads as one run-on paragraph. The
    # tool converts the breaks before sending rather than asking the model for
    # HTML, which keeps the tool contract plain text and the escaping ours.
    print("\n== send_reply body formatting ==")
    rec = Recorder()
    await _client(rec).send_reply(
        "msg1",
        "Hi Wouter,\n\nStep 1: open the cockpit.\nStep 2: click Edit.\n\nKind regards,\nFinops SAP",
    )
    rep = rec.find("/messages/msg1/reply")
    check("posts to reply", len(rep) == 1, f"{rec.paths()}")
    payload = rep[0]["body"] if rep else {}
    comment = payload.get("comment", "")
    check("keeps single newlines as line breaks", comment.count("<br>") == 2, f"got {comment!r}")
    check("keeps blank lines as paragraphs", comment.count("<p>") == 3, f"got {comment!r}")
    check("leaves no raw newline to be collapsed", "\n" not in comment, f"got {comment!r}")
    # The docs are explicit: sending both is a 400.
    check("never sends message.body alongside comment", "message" not in payload, f"got {payload}")

    rec = Recorder()
    await _client(rec).send_reply("msg1", 'Use <ABAP> & "quotes" <b>here</b>')
    esc = (rec.find("/messages/msg1/reply")[0]["body"] or {}).get("comment", "")
    check("escapes markup in the model's text",
          "<ABAP>" not in esc and "&lt;ABAP&gt;" in esc, f"got {esc!r}")
    check("escapes ampersands", "&amp;" in esc, f"got {esc!r}")
    check("does not let the model inject tags", "<b>" not in esc, f"got {esc!r}")

    rec = Recorder()
    await _client(rec).send_reply("msg1", "   ")
    blank = (rec.find("/messages/msg1/reply")[0]["body"] or {}).get("comment", None)
    check("an empty body stays an empty comment", blank == "", f"got {blank!r}")

    # --- move_message -------------------------------------------------------
    # Moving out of the queue folder is the idempotency step.
    print("\n== move_message ==")
    rec = Recorder()
    out = await _client(rec).move_message("msg1", "agent-done")
    mv = rec.find("/move")
    check("posts to move", len(mv) == 1, f"{rec.paths()}")
    check("resolves the destination name to an id",
          bool(mv) and mv[0]["body"].get("destinationId") == "AAA-done", f"got {mv[0]['body']}")
    check("reports the destination", out.get("moved_to") == "agent-done", f"got {out}")

    # Well-known names are Graph ids in their own right, so they must pass
    # through without a folder lookup -- "inbox" is not an Inbox subfolder.
    rec = Recorder()
    await _client(rec).move_message("msg1", "inbox")
    mv = rec.find("/move")
    check("passes well-known folders through",
          bool(mv) and mv[0]["body"].get("destinationId") == "inbox", f"got {mv[0]['body']}")
    check("skips the folder lookup for well-known names",
          not any("childFolders" in p for p in rec.paths()), f"{rec.paths()}")

    rec = Recorder()
    try:
        await _client(rec).move_message("msg1", "nowhere")
        check("unknown destination raises", False, "no exception")
    except Exception as e:
        check("unknown destination raises", "nowhere" in str(e), f"got {e}")

    # --- toolset surface ----------------------------------------------------
    print("\n== toolset surface ==")
    ts = outlook_toolset({"client_id": "x"}, http=httpx.AsyncClient(
        base_url="https://graph.microsoft.com",
        transport=httpx.MockTransport(Recorder().handler),
    ))
    names = set(ts.tools.keys())
    expected = {"list_pending", "get_message", "create_reply_draft", "move_message"}
    check("exposes exactly the agreed tools", names == expected, f"got {sorted(names)}")
    check("exposes no send tool", not any("send" in n for n in names))
    check("exposes its http client for reload cleanup",
          getattr(ts, "http_client", None) is not None)

    # --- admin validation ---------------------------------------------------
    print("\n== admin payload validation ==")
    from agents.admin import McpServerPayload

    ok = McpServerPayload(url=BUILTIN_OUTLOOK_URL, auth_mode="oauth2",
                          oauth={"client_id": "a", "client_secret": "b",
                                 "authorize_url": "https://a/x", "token_url": "https://a/t",
                                 "scope": "s"})
    check("accepts builtin:outlook", ok.url == BUILTIN_OUTLOOK_URL)
    try:
        McpServerPayload(url="builtin:teams", auth_mode="none")
        check("rejects builtin:teams", False, "accepted")
    except Exception:
        check("rejects builtin:teams", True)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
