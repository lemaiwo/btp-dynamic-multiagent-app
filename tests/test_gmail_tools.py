"""Focused tests for the built-in Gmail toolset (``builtin:gmail``).

Google's hosted Gmail MCP server refuses every ``tools/call`` from a
self-registered OAuth client (see docs/GMAIL_SETUP.md). The same token works
against the Gmail REST API, so the tools are served in-process instead. These
tests exercise that toolset against a mocked httpx transport.

Covered: request shaping (query, caps), plain-text extraction and truncation,
reply threading headers on drafts, label name -> id resolution, the absence of
any send capability, and the ``builtin:`` URL rule in admin validation.

No network, no live mailbox, no SAP AI Core.

Run:  python tests/test_gmail_tools.py
"""

from __future__ import annotations

import asyncio
import base64
import email
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

from agents.builtins import is_builtin_url  # noqa: E402
from agents.gmail_tools import (  # noqa: E402
    BUILTIN_GMAIL_URL,
    GmailClient,
    gmail_toolset,
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


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


LABELS = {
    "labels": [
        {"id": "INBOX", "name": "INBOX"},
        {"id": "Label_8458", "name": "agent"},
        {"id": "Label_9001", "name": "agent-drafted"},
        {"id": "Label_5781", "name": "Partners/Amista"},
    ]
}

THREAD_FULL = {
    "id": "t1",
    "messages": [
        {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX", "Label_8458"],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "Subject", "value": "question workzone"},
                    {"name": "From", "value": "Wouter <wouter@lemaire.tech>"},
                    {"name": "Date", "value": "Thu, 21 Aug 2026 22:25:14 +0000"},
                    {"name": "Message-ID", "value": "<abc@mail.gmail.com>"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _b64("how do I configure btp workzone?")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>should be ignored</p>")},
                    },
                ],
            },
        }
    ],
}


class Recorder:
    """Records every request the toolset makes and serves canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = {"_raw": request.content.decode(errors="replace")}
        self.calls.append((request.method, str(request.url), body))

        if path.endswith("/labels"):
            return httpx.Response(200, json=LABELS)
        if path.endswith("/threads") and request.method == "GET":
            return httpx.Response(
                200, json={"threads": [{"id": "t1", "snippet": "how do I config..."}]}
            )
        if path.endswith("/threads/t1/modify"):
            return httpx.Response(200, json={"id": "t1"})
        if path.endswith("/threads/t1"):
            return httpx.Response(200, json=THREAD_FULL)
        if path.endswith("/drafts"):
            return httpx.Response(200, json={"id": "r-99", "message": {"id": "m9"}})
        return httpx.Response(404, json={"error": {"message": f"unmapped {path}"}})

    def paths(self) -> list[str]:
        return [httpx.URL(u).path for _, u, _ in self.calls]


def _client(rec: Recorder) -> GmailClient:
    http = httpx.AsyncClient(
        base_url="https://gmail.googleapis.com",
        transport=httpx.MockTransport(rec.handler),
    )
    return GmailClient(http)


async def main() -> None:
    # --- the builtin URL rule ---------------------------------------------
    # The registry branches on this to decide between an MCP connection and the
    # in-process toolset, so the predicate must not accept near-misses.
    print("\n== builtin URL detection ==")
    check("recognises builtin:gmail", is_builtin_url(BUILTIN_GMAIL_URL))
    check("case/space tolerant", is_builtin_url("  Builtin:Gmail "))
    check("rejects https URLs", not is_builtin_url("https://host/mcp"))
    check("rejects unknown builtins", not is_builtin_url("builtin:slack"))

    # --- search_threads ----------------------------------------------------
    # threads.list returns only id+snippet, so headers come from a per-thread
    # metadata fetch. The query must reach Gmail verbatim: the agent prompt
    # relies on `label:agent` matching by display name.
    print("\n== search_threads ==")
    rec = Recorder()
    c = _client(rec)
    out = await c.search_threads("label:agent", max_results=5)
    q = httpx.URL(rec.calls[0][1])
    check("hits threads.list", rec.calls[0][1].split("?")[0].endswith("/users/me/threads"))
    check("passes the query verbatim", q.params.get("q") == "label:agent",
          f"got {q.params.get('q')!r}")
    check("passes maxResults", q.params.get("maxResults") == "5")
    check("returns one row", len(out) == 1, f"got {out}")
    row = out[0] if out else {}
    check("row carries the subject", row.get("subject") == "question workzone", f"got {row}")
    check("row carries the sender", "wouter@lemaire.tech" in str(row.get("from", "")))
    check("row carries the id", row.get("thread_id") == "t1")
    check("row omits the body", "body" not in row and "payload" not in row)

    # A cap protects the context window: a huge maxResults must be clamped,
    # not passed through. See the context_length_exceeded row in the doc.
    rec2 = Recorder()
    await _client(rec2).search_threads("label:agent", max_results=9999)
    capped = httpx.URL(rec2.calls[0][1]).params.get("maxResults")
    check("clamps an oversized maxResults", capped is not None and int(capped) <= 50,
          f"got {capped}")

    # --- get_thread --------------------------------------------------------
    print("\n== get_thread ==")
    rec = Recorder()
    got = await _client(rec).get_thread("t1")
    text = json.dumps(got)
    check("extracts the text/plain part", "configure btp workzone" in text, f"got {text[:200]}")
    check("drops the text/html part", "should be ignored" not in text)
    check("keeps the subject", "question workzone" in text)

    # Truncation is what keeps one fat thread from blowing the run.
    rec = Recorder()
    short = await _client(rec).get_thread("t1", max_chars=10)
    body = short["messages"][0]["body"]
    check("truncates to max_chars", len(body) <= 10 + 20, f"len={len(body)}")
    check("marks the truncation", "…" in body or "truncated" in body.lower(), f"got {body!r}")

    # --- create_draft ------------------------------------------------------
    # A reply must thread: without In-Reply-To/References Gmail starts a new
    # conversation, and threadId alone does not set those headers.
    print("\n== create_draft ==")
    rec = Recorder()
    res = await _client(rec).create_draft("t1", "Here is the answer.")
    draft_call = [c for c in rec.calls if c[1].endswith("/drafts")]
    check("posts to drafts.create", len(draft_call) == 1, f"calls={rec.paths()}")
    check("returns the draft id", res.get("draft_id") == "r-99", f"got {res}")
    payload = draft_call[0][2] if draft_call else {}
    check("sets threadId", payload.get("message", {}).get("threadId") == "t1")
    raw = payload.get("message", {}).get("raw", "")
    mime = base64.urlsafe_b64decode(raw.encode()).decode() if raw else ""
    msg = email.message_from_string(mime) if mime else None
    check("sets In-Reply-To", bool(msg and msg.get("In-Reply-To") == "<abc@mail.gmail.com>"),
          f"got {msg.get('In-Reply-To') if msg else None}")
    check("sets References", bool(msg and "<abc@mail.gmail.com>" in (msg.get("References") or "")))
    check("replies to the sender", bool(msg and "wouter@lemaire.tech" in (msg.get("To") or "")))
    check("prefixes the subject with Re:",
          bool(msg and (msg.get("Subject") or "").startswith("Re:")),
          f"got {msg.get('Subject') if msg else None}")
    check("carries the body", "Here is the answer." in mime)
    check("never calls send", not any("/send" in p for p in rec.paths()), f"{rec.paths()}")

    # --- list_labels / modify_labels ---------------------------------------
    # The REST API modifies by label ID, so names must be resolved first. This
    # is the mirror image of the MCP server, whose search wants display names.
    print("\n== labels ==")
    rec = Recorder()
    labels = await _client(rec).list_labels()
    check("maps name -> id", labels.get("agent") == "Label_8458", f"got {labels}")
    check("keeps nested names intact", labels.get("Partners/Amista") == "Label_5781")

    rec = Recorder()
    await _client(rec).modify_labels("t1", add=["agent-drafted"], remove=["agent"])
    mod = [c for c in rec.calls if c[1].endswith("/modify")]
    check("posts threads.modify", len(mod) == 1, f"calls={rec.paths()}")
    sent = mod[0][2] if mod else {}
    check("resolves added names to ids", sent.get("addLabelIds") == ["Label_9001"], f"got {sent}")
    check("resolves removed names to ids", sent.get("removeLabelIds") == ["Label_8458"])

    # An unknown label must fail loudly. Silently dropping it would leave the
    # mail labelled and the next run would redo the whole thread.
    rec = Recorder()
    try:
        await _client(rec).modify_labels("t1", add=[], remove=["nope"])
        check("unknown label raises", False, "no exception")
    except Exception as e:
        check("unknown label raises", "nope" in str(e), f"got {e}")

    # Accepting ids directly keeps the tool usable when the agent already has one.
    rec = Recorder()
    await _client(rec).modify_labels("t1", add=[], remove=["Label_8458"])
    mod = [c for c in rec.calls if c[1].endswith("/modify")]
    check("passes through raw label ids", mod and mod[0][2].get("removeLabelIds") == ["Label_8458"])

    # --- the toolset surface -----------------------------------------------
    # "Never send anything" is enforced by the absence of a tool, not by prompt
    # wording, so the exposed set is part of the contract.
    print("\n== toolset surface ==")
    ts = gmail_toolset({"client_id": "x"}, http=httpx.AsyncClient(
        base_url="https://gmail.googleapis.com",
        transport=httpx.MockTransport(Recorder().handler),
    ))
    names = set(ts.tools.keys()) if hasattr(ts, "tools") else set()
    expected = {"search_threads", "get_thread", "create_draft", "list_labels", "modify_labels"}
    check("exposes exactly the agreed tools", names == expected, f"got {sorted(names)}")
    check("exposes no send tool", not any("send" in n for n in names))

    # --- admin validation ---------------------------------------------------
    # builtin: has no host, so it must bypass the https + allowlist rules that
    # every real URL still has to satisfy.
    print("\n== admin payload validation ==")
    from agents.admin import McpServerPayload

    ok = McpServerPayload(url=BUILTIN_GMAIL_URL, auth_mode="oauth2",
                          oauth={"client_id": "a", "client_secret": "b",
                                 "authorize_url": "https://a/x", "token_url": "https://a/t",
                                 "scope": "s"})
    check("accepts builtin:gmail", ok.url == BUILTIN_GMAIL_URL)

    for bad in ["builtin:slack", "builtin:", "builtin"]:
        try:
            McpServerPayload(url=bad, auth_mode="none")
            check(f"rejects {bad!r}", False, "accepted")
        except Exception:
            check(f"rejects {bad!r}", True)

    try:
        McpServerPayload(url="http://evil.example.com/mcp", auth_mode="oauth2",
                         oauth={"client_id": "a", "client_secret": "b",
                                "authorize_url": "https://a/x", "token_url": "https://a/t",
                                "scope": "s"})
        check("still rejects plain-http authenticated URLs", False, "accepted")
    except Exception:
        check("still rejects plain-http authenticated URLs", True)

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
