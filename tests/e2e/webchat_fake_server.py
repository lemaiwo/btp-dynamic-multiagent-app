"""Fake backend for the web chat client (``static/chat/``), standard library only.

TEST-ONLY. Nothing under ``agents/`` or ``app.py`` imports this module. It
implements the contract of ``docs/designs/2026-10-31-webchat.md`` (the
``/chat/api`` routes and the SSE events) with in-memory state, so the front
end can be developed and screenshotted without the real app, a database or
a model.

Start it::

    .venv/bin/python tests/e2e/webchat_fake_server.py            # port 7935
    .venv/bin/python tests/e2e/webchat_fake_server.py --port 7940
    .venv/bin/python tests/e2e/webchat_fake_server.py --fast     # no sleeps
    .venv/bin/python tests/e2e/webchat_fake_server.py --slow 4   # 4x longer turn
    .venv/bin/python tests/e2e/webchat_fake_server.py --basic-user  # no developer role
    .venv/bin/python tests/e2e/webchat_fake_server.py --admin       # the admin scope (instructions visible)
    CHAT_CONTEXT_WINDOWS='{"gpt-5.6-terra": 24000}' .venv/bin/python tests/e2e/webchat_fake_server.py

then open http://127.0.0.1:7935/chat . ``Ctrl-C`` stops it.

What it serves:

- ``GET /chat``            -> ``templates/webchat.html``
- ``GET /static/...``      -> the ``static/`` folder
- ``GET /``                -> a one-line stand-in for the classic chat
- ``GET  /chat/api/me``
- ``GET  /chat/api/conversations?q=``  ``POST /chat/api/conversations``
- ``GET|PATCH|DELETE /chat/api/conversations/{id}``
- ``POST /chat/api/conversations/{id}/messages`` -> a scripted SSE turn
  (start, note, an ``agent`` delegation to ``btp-admin`` with two ``tool``
  calls under it, a nested peer delegation (``parent`` = the first one) with
  one tool, the closing ``agent`` frames with ``usage``, text deltas, two
  ``file`` events, ``title`` on the first turn, ``usage``, ``done`` with
  ``duration_ms``; addendum (b) of the design), with small sleeps so
  streaming is visible; 409 while a turn runs
- ``POST /chat/api/conversations/{id}/cancel`` -> the running turn ends with
  ``error canceled`` + ``done canceled``
- ``GET  /chat/api/conversations/{id}/stream`` -> 409 ``not_running``
- ``GET  /chat/api/files/{id}[?inline=1]``   -> dummy bytes, the real headers
- addendum (c): ``GET /chat/api/catalog`` (two admin agents, two admin
  skills, the orchestrator), ``GET|POST /chat/api/my/agents``,
  ``GET|PUT|DELETE /chat/api/my/agents/{id}``, the same under ``/my/skills``
  (the limits of the addendum, 409 on a duplicate name, FastAPI-shaped 422s
  that name the field and never the value), ``GET /chat/api/my/export``
  (the ``webchat-bundle/1`` file, ``Content-Disposition: attachment``),
  ``POST /chat/api/my/import`` (multipart ``file`` + ``on_conflict``, or a
  JSON body; per item, dropped references and errors reported); a
  conversation carries ``agent_ref`` / ``agent_name`` (``PATCH`` validates
  the ref, 422 when it names nothing the user may use; a deleted personal
  agent falls back to the orchestrator). Seeded: one personal agent with a
  skill and a consult, one personal skill
- addendum (d): ``GET /me`` carries ``developer`` (true unless
  ``--basic-user``), ``access_default`` and ``tiers``; ``GET /catalog?access=``
  lists the two advanced admin agents (``abap-developer-deep``,
  ``ui5-fiori-developer-deep``, ``tier: advanced``) only for
  ``access=advanced`` AND a developer, every agent with its ``tier``; a
  conversation carries ``access`` (``POST``/``PATCH`` take it; ``advanced``
  by a non-developer is 403 ``Advanced access needs the developer role``; an
  ``agent_ref`` naming an advanced agent in a basic conversation is 422
  ``Unknown agent``); ``/my/agents`` takes ``deep`` (422 at the field from a
  non-developer) and answers ``tier`` (advanced when ``deep`` or an advanced
  consult); an import by a non-developer drops ``deep`` and advanced consults
  (``dropped`` with ``field: "deep"`` / ``"consults"``). A developer run seeds
  one advanced conversation and one deep personal agent on top
- addendum (g): ``GET /catalog`` answers ``tool_sources`` (three admin agents
  that share their tools, ``platform-tickets`` not a chat agent,
  ``abap-developer-deep`` advanced and listed only for ``access=advanced`` AND
  a developer), each ``{name, description, tier, connectors}``; ``/my/agents``
  takes ``tools`` (at most 5 distinct names of tool sources; an unknown name or
  a 6th is a 422 at ``tools``, an advanced source from a non-developer too;
  an import drops them into ``dropped`` with ``field: "tools"``) and answers
  ``tools`` (an advanced source makes the agent advanced). The seeded personal
  agent uses two sources
- addendum (e): ``GET /chat/api/conversations/{id}/context`` built from the
  stored data (who answers, the model's window from a code table matched by
  substring and overridable by ``CHAT_CONTEXT_WINDOWS`` as on the server,
  instructions visible only with ``--admin`` for the orchestrator and an admin
  agent, the estimate = characters / 4 plus a fixed figure per binary, the
  measured figure a fixed number once a turn has usage, the uploads with their
  ``excluded`` flag, the tools of the turn, the usage totals);
  ``POST .../context/attachments/{file_id}`` ``{"excluded": bool}`` toggles the
  flag on the stored upload (409 while a turn runs, 404 for a generated file or
  a file of another conversation, 422 for a body that is no boolean)

Three conversations are seeded (today, yesterday, older) so the sidebar
groups show; the first one holds a stored turn with agent, tool, usage and
file parts, and the detail answers ``usage`` summed over the usage parts.
A request without ``X-Requested-With: XMLHttpRequest`` on ``/chat/api`` is
answered 400, so a missing header is noticed during development.
"""
# ruff: noqa: E501  -- canned markdown and seed strings are long on purpose
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import sys
import threading
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "webchat.html"
STATIC = ROOT / "static"

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 5
MEDIA_TYPES = [
    "image/png", "image/jpeg", "application/pdf", "text/plain", "text/markdown",
    "text/csv", "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]
MODELS = [
    {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra", "default": True},
    {"id": "anthropic--claude-4.5-sonnet", "label": "Claude 4.5 Sonnet", "default": False},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "default": False},
]
PRINCIPAL = "wouter@example.org"
APP_NAME = "SAP BTP Multi-Agent"

# ---- addendum (c): the admin catalogue (name + description only) and the limits ----
ADMIN_AGENTS = [
    {"name": "btp-admin", "description": "Reads and changes the BTP account model: subaccounts, entitlements, service instances, role collections.", "tier": "basic"},
    {"name": "entitlement-analyst", "description": "Ranks entitlements by unused quota and flags what nobody used in 90 days.", "tier": "basic"},
    # addendum (d): the advanced (developer) agents, deep agents on their own MCP servers
    {"name": "abap-developer-deep", "description": "ABAP developer on the dev system through ARC-1: reads, analyses, proposes and tests changes with a plan and a scratchpad.", "tier": "advanced"},
    {"name": "ui5-fiori-developer-deep", "description": "UI5 / Fiori developer: views, controllers, annotations and tests of the Fiori apps, with sub-agents per task.", "tier": "advanced"},
]
TIERS = ("basic", "advanced")
ACCESS_DENIED = "Advanced access needs the developer role"
DEVELOPER = True  # --basic-user turns it off
ADVANCED_ADMIN = {a["name"] for a in ADMIN_AGENTS if a["tier"] == "advanced"}
# addendum (g): admin agents that share their MCP servers and built-ins with
# personal agents; a source need not be a chat agent (platform-tickets is not)
TOOL_SOURCES = [
    {"name": "btp-admin", "description": "The BTP account model and the SAP security notes that apply to it.", "tier": "basic",
     "connectors": ["btp-mcp.cfapps.eu10.hana.ondemand.com", "builtin:sapnotes"]},
    {"name": "platform-tickets", "description": "Jira tickets and Confluence runbooks of the platform team.", "tier": "basic",
     "connectors": ["builtin:jira", "builtin:confluence"]},
    {"name": "abap-developer-deep", "description": "The ABAP dev system through ARC-1, as the signed-in user.", "tier": "advanced",
     "connectors": ["arc1.example.hana.ondemand.com"]},
]
ADVANCED_TOOLS = {t["name"] for t in TOOL_SOURCES if t["tier"] == "advanced"}
ADMIN_SKILLS = [
    {"name": "btp-naming-conventions", "description": "How subaccounts, spaces, instances and destinations are named in this landscape."},
    {"name": "report-layout", "description": "The table and heading layout of a platform review document."},
]
ORCHESTRATOR = {"ref": None, "name": "Orchestrator", "description": "Hands your question to the specialist that can answer it."}

# ---- addendum (e): the context of a conversation ----
ADMIN = False  # --admin: the admin scope sees the orchestrator's and the admin agents' instructions
# the window table of the design, matched by substring; CHAT_CONTEXT_WINDOWS (JSON {id or prefix: tokens}) overrides
CONTEXT_WINDOWS = [("gpt-4o", 128_000), ("gpt-5", 400_000), ("claude", 200_000), ("gemini", 1_000_000)]
MEASURED_FIXED = 2_950  # the measured context of a stored turn (the fake has no model request to read)
DOCUMENT_TOOLS = ["create_word_document", "create_pdf_document", "create_excel_workbook"]
DEEP_TOOLS = ["write_todos", "read_todos", "ls", "read_file", "write_file", "edit_file", "task"]
ORCHESTRATOR_INSTRUCTIONS = (
    "You are the orchestrator of the SAP BTP Multi-Agent app. Read the question, pick the specialist that can answer it "
    "and delegate with a precise prompt. Combine the answers into one reply. Never invent a subaccount, a service plan or a quota: "
    "quote what the specialist returned. When the user asks for a document, call the document tool after the facts are in."
)
ADMIN_INSTRUCTIONS = {
    "btp-admin": "You administer the BTP account model through the btp and cf MCP servers. Read before you change; name the subaccount, "
                 "the service and the plan in every answer. Destructive changes (delete, unsubscribe) need an explicit confirmation.",
    "entitlement-analyst": "You rank entitlements by unused quota. Ask btp-admin for the figures; report assigned, used and spare per plan, "
                           "largest spare first; flag what nobody used in 90 days.",
    "abap-developer-deep": "You are the ABAP developer on the dev system through ARC-1. Plan with write_todos, keep findings in the scratchpad, "
                           "read before you propose, and run the syntax check on every proposal. Never activate.",
    "ui5-fiori-developer-deep": "You are the UI5 / Fiori developer. Work per task with sub-agents, keep the views, controllers and annotations "
                                "consistent with the app's conventions and run the linter on every change.",
}
ADMIN_AGENT_SKILLS = {"btp-admin": ["btp-naming-conventions"], "entitlement-analyst": ["report-layout"]}
ADMIN_SKILL_TEXT = {
    "btp-naming-conventions": "# Naming\n\nSubaccounts: <b>-<area>-<env> (b-fdn-dev). Spaces: dev | test | prod. Service instances: <app>-<service> "
                              "(agent-registry-db). Destinations: UPPER_SNAKE (S4H_DEV, JIRA).",
    "report-layout": "# Platform review layout\n\n1. Summary (five lines at most).\n2. One table per subaccount: service, plan, assigned, used, spare.\n"
                     "3. Actions, ordered by money saved.",
}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,63}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
LIM = {"agents": 50, "skills": 100, "description": 300, "instructions": 20_000, "content": 50_000,
       "agent_skills": 20, "consults": 10, "tools": 5, "bundle": 1024 * 1024}
BUNDLE_FORMAT = "webchat-bundle/1"

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
}

ABAP_ANSWER_MD = """The read on `mt_rules` in `VALIDATE` lost its `sy-subrc` check in transport **DEVK912345**.
This is the corrected method; it raises the class's own exception instead of dumping:

```abap
* file: zcl_order_check.clas.abap
METHOD validate.
  DATA ls_rule TYPE ty_rule.

  LOOP AT it_items INTO DATA(ls_item).
    READ TABLE mt_rules INTO ls_rule
         WITH KEY rule_id = ls_item-rule_id.
    IF sy-subrc <> 0.
      RAISE EXCEPTION NEW zcx_order_check(
        textid  = zcx_order_check=>rule_missing
        rule_id = ls_item-rule_id ).
    ENDIF.
    IF ls_item-quantity > ls_rule-max_quantity.
      APPEND VALUE #( item = ls_item-item_no msg = 'Quantity above limit' ) TO rt_findings.
    ENDIF.
  ENDLOOP.
ENDMETHOD.
```

Run the ABAP Unit tests of `ZCL_ORDER_CHECK` after the change; the test `missing_rule_raises` covers the new branch.
"""


def letters(n: int) -> list[str]:
    """Spreadsheet column letters A, B, ... AA, AB for the first n columns."""
    out = []
    for i in range(n):
        s, k = "", i + 1
        while k:
            k, r = divmod(k - 1, 26)
            s = chr(65 + r) + s
        out.append(s)
    return out


def preview_of(f: dict) -> tuple[int, dict]:
    """The scripted GET /files/{id}/preview answer (addendum f) by the dummy file's type."""
    mt, name = f["media_type"], f["name"]
    if mt in ("image/png", "image/jpeg", "application/pdf"):
        return 200, {"kind": "inline", "name": name, "media_type": mt, "url": f"/chat/api/files/{f['id']}?inline=1"}
    if mt == MEDIA_TYPES[8]:
        ent = [["Subaccount", "Service", "Plan", "Assigned", "Used", "Spare"],
               ["b-fdn-dev", "hana-cloud", "hana", 2, 1, 1],
               ["b-fdn-dev", "xsuaa", "application", 20, 11, 9],
               ["b-fdn-dev", "destination", "lite", 20, 8, 12],
               ["b-fdn-dev", "jobscheduler", "standard", 5, 3, 2],
               ["b-fdn-dev", "connectivity", "lite", 1, 1, 0],
               ["b-int-dev", "integrationsuite", "enterprise_agreement", 1, 0, 1],
               ["b-int-dev", "destination", "lite", 10, 4, 6],
               ["b-int-dev", "xsuaa", "application", 10, 3, 7],
               ["b-ui-dev", "html5-apps-repo", "app-runtime", 3, 3, 0],
               ["b-ui-dev", "html5-apps-repo", "app-host", 2, 2, 0],
               ["b-ui-dev", "workzone", "standard", 1, 1, 0],
               ["b-ui-dev", "xsuaa", "application", 10, 6, 4],
               [None, None, None, None, None, None],
               ["Total", None, None, 85, 43, 42]]
        summary = [["Checked on", "2026-10-31"], ["Subaccounts", 3], ["Services with spare quota", 8],
                   ["Billable spare quota", "hana-cloud (1)"]]
        return 200, {"kind": "sheets", "name": name, "truncated": False, "sheets": [
            {"name": "Entitlements", "columns": letters(6), "rows": ent, "row_count": len(ent), "truncated": False},
            {"name": "Summary", "columns": letters(2), "rows": summary, "row_count": len(summary), "truncated": False}]}
    if mt == MEDIA_TYPES[7]:
        return 200, {"kind": "document", "name": name, "truncated": False, "blocks": [
            {"type": "heading", "level": 1, "text": "Platform review: dev subaccounts"},
            {"type": "paragraph", "text": "This review covers the three subaccounts of the Infrabel-dev directory as of 31 October 2026. It lists the entitlements nobody uses and what to do with them."},
            {"type": "heading", "level": 2, "text": "Findings"},
            {"type": "list", "ordered": False, "items": [
                "b-fdn-dev holds one spare hana-cloud unit, which bills every month.",
                "integrationsuite on b-int-dev has no tenant since July.",
                "xsuaa headroom on all three is free and can stay."]},
            {"type": "heading", "level": 2, "text": "Entitlements with spare quota"},
            {"type": "table", "rows": [["Subaccount", "Service", "Plan", "Spare"],
                                       ["b-fdn-dev", "hana-cloud", "hana", "1"],
                                       ["b-fdn-dev", "destination", "lite", "12"],
                                       ["b-int-dev", "integrationsuite", "enterprise_agreement", "1"]]},
            {"type": "heading", "level": 2, "text": "Actions"},
            {"type": "list", "ordered": True, "items": [
                "Release the spare hana-cloud unit on b-fdn-dev.",
                "Ask the integration team whether the integrationsuite entitlement is still planned.",
                "Repeat this check monthly from the pipeline."]},
            {"type": "paragraph", "text": "Prepared by btp-admin and entitlement-analyst."}]}
    lang = {"text/markdown": "markdown", "text/csv": "csv", "application/json": "json", "text/plain": "text"}.get(mt)
    if lang:
        text = f["data"].decode("utf-8", "replace")[:200000]
        return 200, {"kind": "text", "name": name, "language": lang, "text": text, "truncated": False}
    return 415, {"detail": "No preview for this file type"}


ANSWER_MD = """## Unused entitlements in the dev subaccount

I checked the three subaccounts of the **Infrabel-dev** directory. Two of them
hold entitlements that no service instance uses.

| Subaccount | Service | Plan | Assigned | Used |
| --- | --- | --- | --- | --- |
| b-fdn-dev | hana-cloud | hana | 2 | 1 |
| b-fdn-dev | xsuaa | application | 20 | 11 |
| b-int-dev | integrationsuite | enterprise_agreement | 1 | 0 |
| b-ui-dev | html5-apps-repo | app-runtime | 3 | 3 |

What I would do, in this order:

1. Release the spare `hana-cloud` quota on **b-fdn-dev** (it bills).
2. Leave `xsuaa` alone: the plan is free and the headroom is useful.
3. Ask the integration team whether `integrationsuite` on **b-int-dev** is still planned.

The same check as a script, if you want to repeat it from a pipeline:

```python
import subprocess, json

def entitlements(subaccount: str) -> list[dict]:
    out = subprocess.run(
        ["btp", "--format", "json", "list", "accounts/entitlement",
         "--subaccount", subaccount],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)["entitledServices"]

for sa in ("b-fdn-dev", "b-int-dev", "b-ui-dev"):
    for svc in entitlements(sa):
        for plan in svc["servicePlans"]:
            if plan["amount"] > plan.get("usage", 0):
                print(sa, svc["name"], plan["name"], plan["amount"] - plan.get("usage", 0))
```

How the pieces relate:

```mermaid
flowchart LR
    D[Directory Infrabel-dev] --> A[b-fdn-dev]
    D --> B[b-int-dev]
    D --> C[b-ui-dev]
    A --> H[(hana-cloud x2)]
    A --> X[xsuaa x20]
    B --> I[integrationsuite x1]
    C --> R[html5-apps-repo x3]
```

The workbook below lists every entitlement with its usage; the Word document
is a one-page summary for the platform review.
"""


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def now() -> datetime:
    return datetime.now(timezone.utc)


def tiny_png(size: int = 96) -> bytes:
    """A small gradient PNG (so uploaded images show a thumbnail)."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            row += bytes((10 + x * 2 % 246, 110 + y % 100, 209))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.files: dict[str, dict] = {}
        self.cancel_flags: dict[str, threading.Event] = {}
        self.my_agents: dict[str, dict] = {}
        self.my_skills: dict[str, dict] = {}
        self.seed()

    # ---- seed ----
    def seed(self) -> None:
        t = now()
        png = tiny_png()
        c1 = self.new_conversation("Unused entitlements in dev", "gpt-5.6-terra", t - timedelta(hours=3))
        up = self.add_file(c1["id"], None, "upload", "subaccounts.png", "image/png", png)
        um = self.add_message(c1["id"], "user", "Which BTP subaccounts have entitlements that nobody uses? The screenshot shows the directory.", files=[up["id"]])
        up["message_id"] = um["id"]
        am = self.add_message(c1["id"], "assistant", ANSWER_MD, status="done")
        x = self.add_file(c1["id"], am["id"], "generated", "entitlements.xlsx", MEDIA_TYPES[8], b"PK\x03\x04" + b"\0" * 6400)
        d = self.add_file(c1["id"], am["id"], "generated", "platform-review.docx", MEDIA_TYPES[7], b"PK\x03\x04" + b"\0" * 18200)
        am["parts"] = [
            {"type": "note", "text": "Reading the screenshot"},
            {"type": "agent", "id": "a1", "agent": "btp-admin", "parent": None, "status": "done",
             "prompt": "Which subaccounts of the Infrabel-dev directory have entitlements with spare quota? The screenshot shows the directory.",
             "output": "3 subaccounts; b-fdn-dev has 2 services with unused quota (Destination 8 of 20, Job Scheduling 3 of 5).",
             "duration_ms": 6420, "usage": {"input_tokens": 1820, "output_tokens": 260, "requests": 3, "model": "gpt-5.6-terra"}},
            {"type": "tool", "id": "t1", "agent": "btp-admin", "agent_id": "a1", "name": "list_subaccounts", "status": "done",
             "args": "directory=Infrabel-dev", "output": "3 subaccounts", "duration_ms": 1380},
            {"type": "tool", "id": "t2", "agent": "btp-admin", "agent_id": "a1", "name": "list_entitlements", "status": "done",
             "args": "subaccount=b-fdn-dev", "output": "12 services, 2 with spare quota", "duration_ms": 2240},
            {"type": "agent", "id": "a2", "agent": "entitlement-analyst", "parent": "a1", "status": "done",
             "prompt": "Rank these 12 entitlements by unused quota and flag the ones nobody used in 90 days.",
             "output": "2 flagged: Destination (8/20, no instance since July), Job Scheduling (3/5).",
             "duration_ms": 1810, "usage": {"input_tokens": 640, "output_tokens": 95, "requests": 1, "model": "gpt-5.6-terra"}},
            {"type": "tool", "id": "t3", "agent": "entitlement-analyst", "agent_id": "a2", "name": "usage_history", "status": "done",
             "args": "subaccount=b-fdn-dev days=90", "output": "2 services without usage", "duration_ms": 910},
            {"type": "tool", "id": "t4", "agent": None, "agent_id": None, "name": "create_workbook", "status": "done",
             "args": "title=entitlements rows=12", "output": "entitlements.xlsx (6 KB)", "duration_ms": 420},
            {"type": "text", "text": ANSWER_MD},
            {"type": "file", "file_id": x["id"], "name": x["name"], "media_type": x["media_type"], "size": x["size"]},
            {"type": "file", "file_id": d["id"], "name": d["name"], "media_type": d["media_type"], "size": d["size"]},
            {"type": "usage", "input_tokens": 3410, "output_tokens": 812, "cache_read_tokens": 1024, "total_tokens": 4222,
             "requests": 6, "tool_calls": 4, "model": "gpt-5.6-terra", "duration_ms": 9870,
             "agents": [{"id": "a1", "agent": "btp-admin", "input_tokens": 1820, "output_tokens": 260, "requests": 3, "model": "gpt-5.6-terra"},
                        {"id": "a2", "agent": "entitlement-analyst", "input_tokens": 640, "output_tokens": 95, "requests": 1, "model": "gpt-5.6-terra"}]},
        ]
        c2 = self.new_conversation("Destinations of the sandbox", "gpt-5.6-terra", t - timedelta(days=1, hours=2))
        self.add_message(c2["id"], "user", "List the destinations of the sandbox subaccount.")
        a2m = self.add_message(c2["id"], "assistant", "There are **4** destinations: `S4H_DEV`, `JIRA`, `SHAREPOINT`, `BITBUCKET`.", status="done")
        a2m["parts"] = [
            {"type": "text", "text": a2m["content"]},
            {"type": "usage", "input_tokens": 980, "output_tokens": 64, "cache_read_tokens": 0, "total_tokens": 1044,
             "requests": 1, "tool_calls": 0, "model": "gpt-5.6-terra", "duration_ms": 2150, "agents": []},
        ]
        c3 = self.new_conversation("Role collections for the invoke API", "anthropic--claude-4.5-sonnet", t - timedelta(days=4))
        self.add_message(c3["id"], "user", "Who has the Agent Invoker role collection?")
        self.add_message(c3["id"], "assistant", "", status="failed", error="RuntimeError: the destination service answered 503")
        c4 = self.new_conversation("Cloud Connector trust on prod", "gpt-5.6-terra", t - timedelta(days=23))
        self.add_message(c4["id"], "user", "Is the Cloud Connector trust configured on prod?")
        self.add_message(c4["id"], "assistant", "Yes, the location id `SCC-PRD` is trusted since 2026-09-02.", status="done")
        # personal skill + agent (addendum c)
        sk = self.put_skill(None, {"name": "weekly-quota-report", "description": "The shape of the Monday quota mail: one table per subaccount, unused quota first.",
                                   "content": "# Weekly quota report\n\nOne table per subaccount. Columns: service, plan, assigned, used, spare.\nSort by spare quota, largest first. End with the three actions that free the most quota."})
        self.put_skill(None, {"name": "ticket-tone", "description": "How to word a Jira comment for the platform team.",
                              "content": "Short, factual, one action per paragraph. Name the subaccount and the service plan. No greetings."})
        ag = self.put_agent(None, {"name": "Quota watcher", "description": "Watches the dev entitlements and drafts the Monday quota report.",
                                   "instructions": "You watch the entitlements of the dev subaccounts.\n\n1. Ask btp-admin for the entitlements with spare quota.\n2. Rank them with the weekly-quota-report skill.\n3. Draft the report as a Word document.",
                                   "skills": [{"scope": "mine", "name": sk["name"]}, {"scope": "admin", "name": "btp-naming-conventions"}],
                                   "consults": ["btp-admin"], "tools": ["btp-admin", "platform-tickets"], "model": None})
        c2["agent_ref"], c2["agent_label"] = "admin:btp-admin", "btp-admin"
        c4["agent_ref"], c4["agent_label"] = f"mine:{ag['id']}", ag["name"]
        if DEVELOPER:
            # addendum (d): an advanced conversation and a deep personal agent
            self.put_agent(None, {"name": "ABAP reviewer", "description": "Reviews ABAP changes against clean core and drafts the ATC fix list.",
                                  "instructions": "Review the named objects with abap-developer-deep. Plan first, keep findings in the scratchpad, answer with a fix list.",
                                  "skills": [], "consults": ["abap-developer-deep"], "model": None, "deep": True})
            c5 = self.new_conversation("Dump in ZCL_ORDER_CHECK on dev", "gpt-5.6-terra", t - timedelta(hours=1))
            c5["access"] = "advanced"
            c5["agent_ref"], c5["agent_label"] = "admin:abap-developer-deep", "abap-developer-deep"
            self.add_message(c5["id"], "user", "There is a short dump in ZCL_ORDER_CHECK=>VALIDATE on dev since this morning. What changed?")
            m5 = self.add_message(c5["id"], "assistant", "The dump is a `CX_SY_ITAB_LINE_NOT_FOUND` in `VALIDATE`, line 142: the read on `mt_rules` has no `sy-subrc` check since transport **DEVK912345** (yesterday 17:40).", status="done")
            m5["parts"] = [{"type": "text", "text": m5["content"]},
                           {"type": "usage", "input_tokens": 2210, "output_tokens": 140, "cache_read_tokens": 0, "total_tokens": 2350,
                            "requests": 2, "tool_calls": 2, "model": "gpt-5.6-terra", "duration_ms": 5400, "agents": []}]
            # addendum (f): the fix as a fenced ABAP block, a viewer item in advanced access
            self.add_message(c5["id"], "user", "Propose the fix.")
            m6 = self.add_message(c5["id"], "assistant", ABAP_ANSWER_MD, status="done")
            m6["parts"] = [{"type": "text", "text": ABAP_ANSWER_MD},
                           {"type": "usage", "input_tokens": 2980, "output_tokens": 410, "cache_read_tokens": 0, "total_tokens": 3390,
                            "requests": 2, "tool_calls": 1, "model": "gpt-5.6-terra", "duration_ms": 6100, "agents": []}]

    # ---- addendum (c): personal agents and skills ----
    def put_agent(self, aid: str | None, body: dict) -> dict:
        t = now()
        row = self.my_agents.get(aid) if aid else None
        new = {"id": row["id"] if row else str(uuid.uuid4()), "name": body["name"], "description": body.get("description") or "",
               "instructions": body["instructions"], "skills": list(body.get("skills") or []), "consults": list(body.get("consults") or []),
               "tools": list(body.get("tools") or []),
               "model": body.get("model"), "deep": body.get("deep") is True,
               "created_at": row["created_at"] if row else t, "updated_at": t}
        self.my_agents[new["id"]] = new
        return new

    @staticmethod
    def tier_of(a: dict) -> str:
        """advanced when deep is on, any consult names an advanced admin agent (addendum d)
        or any tool source is advanced (addendum g)."""
        return "advanced" if a.get("deep") or any(c in ADVANCED_ADMIN for c in a.get("consults") or []) \
            or any(t in ADVANCED_TOOLS for t in a.get("tools") or []) else "basic"

    def put_skill(self, sid: str | None, body: dict) -> dict:
        t = now()
        row = self.my_skills.get(sid) if sid else None
        new = {"id": row["id"] if row else str(uuid.uuid4()), "name": body["name"], "description": body.get("description") or "",
               "content": body["content"], "created_at": row["created_at"] if row else t, "updated_at": t}
        self.my_skills[new["id"]] = new
        return new

    def agent_out(self, a: dict) -> dict:
        return {"id": a["id"], "ref": f"mine:{a['id']}", "name": a["name"], "description": a["description"], "instructions": a["instructions"],
                "skills": a["skills"], "consults": a["consults"], "tools": list(a.get("tools") or []),
                "model": a["model"], "deep": a.get("deep") is True,
                "tier": self.tier_of(a), "created_at": utc(a["created_at"]),
                "updated_at": utc(a["updated_at"]), "source": "mine", "readonly": False}

    def skill_out(self, s: dict) -> dict:
        return {"id": s["id"], "name": s["name"], "description": s["description"], "content": s["content"],
                "created_at": utc(s["created_at"]), "updated_at": utc(s["updated_at"]), "source": "mine", "readonly": False}

    def delete_agent(self, aid: str) -> None:
        self.my_agents.pop(aid, None)
        for c in self.conversations.values():
            if c.get("agent_ref") == f"mine:{aid}":
                c["agent_ref"], c["agent_label"] = None, None

    def delete_skill(self, sid: str) -> None:
        s = self.my_skills.pop(sid, None)
        if s is None:
            return
        for a in self.my_agents.values():
            a["skills"] = [x for x in a["skills"] if not (x["scope"] == "mine" and x["name"] == s["name"])]

    def resolve_ref(self, ref, access: str = "basic") -> tuple[bool, str | None]:
        """(usable, name): None is the orchestrator; admin:<name> and mine:<id> must exist,
        and an advanced one exists only for an advanced context (addendum d)."""
        if ref is None:
            return True, ORCHESTRATOR["name"]
        if not isinstance(ref, str):
            return False, None
        if ref.startswith("admin:"):
            name = ref[6:]
            a = next((x for x in ADMIN_AGENTS if x["name"] == name), None)
            if a is None:
                return False, name
            return (a["tier"] == "basic" or access == "advanced"), name
        if ref.startswith("mine:"):
            a = self.my_agents.get(ref[5:])
            if a is None:
                return False, None
            return (self.tier_of(a) == "basic" or access == "advanced"), a["name"]
        return False, None

    def agent_name_of(self, c: dict) -> str:
        ref = c.get("agent_ref")
        ok, name = self.resolve_ref(ref, c.get("access", "basic"))
        if ok:
            return name
        return f"{c.get('agent_label')} (deleted)" if c.get("agent_label") else "Agent (deleted)"

    def bundle(self, agent_ids: list[str] | None, skill_ids: list[str] | None) -> dict:
        everything = agent_ids is None and skill_ids is None  # no parameter at all = all of the user's
        agents = [a for a in self.my_agents.values() if everything or a["id"] in (agent_ids or [])]
        # a selection also carries the own skills the selected agents attach
        attached = {x["name"] for a in agents for x in a["skills"] if x["scope"] == "mine"}
        skills = [s for s in self.my_skills.values() if everything or s["id"] in (skill_ids or []) or s["name"] in attached]
        return {"format": BUNDLE_FORMAT, "exported_at": utc(now()),
                "agents": [{"name": a["name"], "description": a["description"], "instructions": a["instructions"],
                            "skills": a["skills"], "consults": a["consults"], "tools": list(a.get("tools") or []),
                            "model": a["model"], "deep": a.get("deep") is True} for a in agents],
                "skills": [{"name": s["name"], "description": s["description"], "content": s["content"]} for s in skills]}

    # ---- helpers ----
    def new_conversation(self, title: str, model: str | None, at: datetime | None = None) -> dict:
        at = at or now()
        conv = {"id": str(uuid.uuid4()), "principal": PRINCIPAL, "title": title[:120] or "New chat",
                "model_name": model, "created_at": at, "updated_at": at, "status": "idle",
                "agent_ref": None, "agent_label": None, "access": "basic"}
        self.conversations[conv["id"]] = conv
        self.messages[conv["id"]] = []
        self.cancel_flags[conv["id"]] = threading.Event()
        return conv

    def add_message(self, cid: str, role: str, content: str, *, status: str = "done", error: str | None = None,
                    files: list[str] | None = None, at: datetime | None = None) -> dict:
        msgs = self.messages[cid]
        msg = {"id": str(uuid.uuid4()), "position": len(msgs) + 1, "role": role, "content": content,
               "parts": [], "status": status, "error": error,
               "created_at": at or self.conversations[cid]["updated_at"], "file_ids": list(files or [])}
        msgs.append(msg)
        return msg

    def add_file(self, cid: str, mid: str | None, kind: str, name: str, media_type: str, data: bytes) -> dict:
        f = {"id": str(uuid.uuid4()), "conversation_id": cid, "message_id": mid, "kind": kind, "name": name,
             "media_type": media_type, "size": len(data), "data": data, "created_at": now()}
        self.files[f["id"]] = f
        return f

    def conv_summary(self, c: dict) -> dict:
        return {"id": c["id"], "title": c["title"], "updated_at": utc(c["updated_at"]),
                "created_at": utc(c["created_at"]), "status": c["status"], "access": c.get("access", "basic"),
                "agent_ref": c.get("agent_ref"), "agent_name": self.agent_name_of(c)}

    def conv_usage(self, cid: str) -> dict:
        total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "requests": 0, "turns": 0}
        for m in self.messages[cid]:
            for p in m["parts"]:
                if p.get("type") != "usage":
                    continue
                total["turns"] += 1
                for k in ("input_tokens", "output_tokens", "total_tokens", "requests"):
                    total[k] += int(p.get(k) or 0)
        return total

    # ---- addendum (e): the context of a conversation ----
    @staticmethod
    def context_window(model_id: str | None) -> int | None:
        mid = (model_id or "").lower()
        raw = os.environ.get("CHAT_CONTEXT_WINDOWS")
        if raw:
            try:
                table = json.loads(raw)
            except ValueError:
                table = {}
            if isinstance(table, dict):
                for key, tokens in sorted(table.items(), key=lambda kv: -len(str(kv[0]))):
                    if isinstance(tokens, int) and tokens > 0 and mid.startswith(str(key).lower()):
                        return tokens
        for key, tokens in CONTEXT_WINDOWS:
            if key in mid:
                return tokens
        return None

    def context_model(self, c: dict) -> str:
        ref = c.get("agent_ref") or ""
        if ref.startswith("admin:"):
            return "gpt-5.6-terra"  # an admin agent runs on its own model
        if ref.startswith("mine:"):
            a = self.my_agents.get(ref[5:])
            if a and a.get("model"):
                return a["model"]
        return c.get("model_name") or MODELS[0]["id"]

    def context(self, c: dict) -> dict:
        cid = c["id"]
        access = c.get("access", "basic")
        ref = c.get("agent_ref")
        ok, name = self.resolve_ref(ref, access)
        # who answers
        if not ref or not ok:
            agent = {"ref": None, "name": ORCHESTRATOR["name"], "kind": "orchestrator", "tier": "basic"}
            visible, text = ADMIN, ORCHESTRATOR_INSTRUCTIONS
            skill_names = []
            own = None
        elif ref.startswith("admin:"):
            row = next(x for x in ADMIN_AGENTS if x["name"] == name)
            agent = {"ref": ref, "name": name, "kind": "admin", "tier": row["tier"]}
            visible, text = ADMIN, ADMIN_INSTRUCTIONS.get(name, f"You are {name}.")
            skill_names = [("admin", n) for n in ADMIN_AGENT_SKILLS.get(name, [])]
            own = None
        else:
            own = self.my_agents[ref[5:]]
            agent = {"ref": ref, "name": own["name"], "kind": "mine", "tier": self.tier_of(own)}
            visible, text = True, own["instructions"]
            skill_names = [(x["scope"], x["name"]) for x in own["skills"]]
        skills = []
        for scope, sname in skill_names:
            if scope == "mine":
                row = next((x for x in self.my_skills.values() if x["name"] == sname), None)
                if row is None:
                    continue
                skills.append({"scope": "mine", "name": sname, "chars": len(row["content"]), "visible": True, "text": row["content"]})
            else:
                content = ADMIN_SKILL_TEXT.get(sname)
                if content is None:
                    continue
                skills.append({"scope": "admin", "name": sname, "chars": len(content), "visible": ADMIN, "text": content if ADMIN else None})
        # history: the finished turns
        msgs = [m for m in self.messages[cid] if m["status"] != "running"]
        if any(m["status"] == "running" for m in self.messages[cid]) and msgs and msgs[-1]["role"] == "user":
            msgs = msgs[:-1]  # the running turn's user message is not in the context yet
        chars = len(text) + sum(sk["chars"] for sk in skills)
        for m in msgs:
            chars += len(m["content"] or "")
            for part in m["parts"]:
                for key in ("text", "prompt", "output"):
                    v = part.get(key)
                    if isinstance(v, str):
                        chars += len(v)
        in_history = {fid for m in msgs for fid in m["file_ids"]}
        uploads = [f for f in self.files.values() if f["conversation_id"] == cid and f["kind"] == "upload" and f["id"] in in_history]
        binary = 0
        for f in uploads:
            if f.get("excluded"):
                continue
            if f["media_type"].startswith("image/"):
                binary += 1_000
            elif f["media_type"] == "application/pdf":
                binary += 1_500 * max(1, math.ceil(f["size"] / 102_400))
            else:
                binary += f["size"] // 4
        has_usage = any(p.get("type") == "usage" for m in msgs for p in m["parts"])
        model_id = self.context_model(c)
        tools = [{"kind": "document", "name": n} for n in DOCUMENT_TOOLS]
        if own is not None and own.get("deep"):
            tools += [{"kind": "deep", "name": n} for n in DEEP_TOOLS]
        if agent["kind"] == "orchestrator":
            targets = [a["name"] for a in ADMIN_AGENTS if a["tier"] == "basic" or access == "advanced"]
        elif agent["kind"] == "mine":
            targets = [n for n in own["consults"] if n not in ADVANCED_ADMIN or access == "advanced"]
        else:
            targets = []
        tools += [{"kind": "delegate", "name": f"delegate_to_{t.replace('-', '_')}", "agent": t} for t in targets]
        return {
            "agent": agent,
            "access": access,
            "model": {"id": model_id, "context_window": self.context_window(model_id)},
            "instructions": {"visible": visible, "text": text if visible else None, "chars": len(text), "skills": skills},
            "history": {"turns": sum(1 for m in msgs if m["role"] == "assistant"), "messages": len(msgs), "chars": chars,
                        "estimated_tokens": chars // 4 + binary, "measured_tokens": MEASURED_FIXED if has_usage else None,
                        "attachments": [{"file_id": f["id"], "name": f["name"], "media_type": f["media_type"], "size": f["size"],
                                         "excluded": bool(f.get("excluded"))} for f in uploads]},
            "tools": tools,
            "usage": self.conv_usage(cid),
        }

    def conv_detail(self, c: dict) -> dict:
        out = {"id": c["id"], "title": c["title"], "model": c["model_name"], "status": c["status"],
               "created_at": utc(c["created_at"]), "updated_at": utc(c["updated_at"]),
               "access": c.get("access", "basic"), "agent_ref": c.get("agent_ref"), "agent_name": self.agent_name_of(c),
               "usage": self.conv_usage(c["id"]), "messages": []}
        for m in self.messages[c["id"]]:
            files = [self.files[fid] for fid in m["file_ids"] if fid in self.files]
            files += [f for f in self.files.values() if f["message_id"] == m["id"] and f["id"] not in m["file_ids"]]
            out["messages"].append({
                "id": m["id"], "position": m["position"], "role": m["role"], "content": m["content"],
                "parts": m["parts"], "status": m["status"], "error": m["error"], "created_at": utc(m["created_at"]),
                "files": [{"id": f["id"], "name": f["name"], "media_type": f["media_type"], "size": f["size"], "kind": f["kind"]} for f in files],
            })
        return out


STORE = Store()
FAST = False
SLOW = 1.0


def nap(seconds: float) -> None:
    if not FAST:
        time.sleep(seconds * SLOW)


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[tuple[str, str, bytes]]]:
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = BytesParser(policy=HTTP).parsebytes(head + body)
    fields: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files.append((filename, part.get_content_type(), payload))
        elif name:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def sniff(name: str, declared: str, data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if data.startswith(b"PK\x03\x04") and ext in ("docx", "xlsx"):
        return MEDIA_TYPES[7] if ext == "docx" else MEDIA_TYPES[8]
    by_ext = {"txt": "text/plain", "md": "text/markdown", "csv": "text/csv", "json": "application/json"}
    if ext in by_ext:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return by_ext[ext]
    return None


def v_err(field, rule, index: int | None = None) -> dict:
    loc = ["body", field] + ([index] if index is not None else [])
    return {"loc": loc, "msg": rule, "type": "value_error"}


def text_ok(v, *, min_len: int, max_len: int, one_line: bool = False) -> str | None:
    if not isinstance(v, str):
        return "too_short" if min_len else "pattern"
    if len(v) < min_len:
        return "too_short"
    if len(v) > max_len:
        return "too_long"
    if CONTROL_RE.search(v):
        return "control"
    if one_line and ("\n" in v or "\r" in v):
        return "too_long"
    return None


AGENT_KEYS = {"name", "description", "instructions", "skills", "consults", "tools", "model", "deep"}
SKILL_KEYS = {"name", "description", "content"}


def check_agent(body: dict, *, my_skill_names: set[str], dropping: bool = False) -> tuple[list[dict], list[dict]]:
    """-> (field errors as (field, rule) dicts, dropped references [{field, name}]).
    Addendum (d): ``deep`` must be a boolean; ``deep: true`` and a consult of an advanced
    admin agent are accepted from a developer only (422 at the field, or dropped on import)."""
    errors: list[tuple[str, str]] = []
    dropped: list[dict] = []
    if not isinstance(body, dict):
        return [("item", "pattern")], dropped
    deep = body.get("deep", False)
    if deep is not None and not isinstance(deep, bool):
        errors.append(("deep", "pattern"))
    elif deep is True and not DEVELOPER:
        if dropping:
            dropped.append({"field": "deep", "name": None})
            body["deep"] = False
        else:
            errors.append(("deep", "developer_only"))
    for k in body:
        if k not in AGENT_KEYS:
            errors.append((k, "unknown_key"))
    name = body.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or CONTROL_RE.search(name):
        errors.append(("name", "pattern"))
    r = text_ok(body.get("description") or "", min_len=0, max_len=LIM["description"], one_line=True)
    if r:
        errors.append(("description", r))
    r = text_ok(body.get("instructions"), min_len=1, max_len=LIM["instructions"])
    if r:
        errors.append(("instructions", r))
    skills = body.get("skills") or []
    if not isinstance(skills, list) or len(skills) > LIM["agent_skills"]:
        errors.append(("skills", "too_many"))
    else:
        kept = []
        for i, sk in enumerate(skills):
            if not isinstance(sk, dict) or sk.get("scope") not in ("admin", "mine") or not isinstance(sk.get("name"), str):
                errors.append(("skills", "pattern"))
                break
            exists = (sk["name"] in {a["name"] for a in ADMIN_SKILLS}) if sk["scope"] == "admin" else (sk["name"] in my_skill_names)
            if exists:
                kept.append({"scope": sk["scope"], "name": sk["name"]})
            elif dropping:
                dropped.append({"field": "skills", "name": sk["name"]})
            else:
                errors.append(("skills", "pattern", i))
        body["skills"] = kept
    consults = body.get("consults") or []
    if not isinstance(consults, list) or len(consults) > LIM["consults"]:
        errors.append(("consults", "too_many"))
    else:
        kept = []
        for i, c in enumerate(consults):
            if not isinstance(c, str):
                errors.append(("consults", "pattern"))
                break
            known = any(a["name"] == c for a in ADMIN_AGENTS)
            if known and (DEVELOPER or c not in ADVANCED_ADMIN):
                kept.append(c)
            elif dropping:
                dropped.append({"field": "consults", "name": c})
            elif known:
                errors.append(("consults", "developer_only", i))
            else:
                errors.append(("consults", "pattern", i))
        body["consults"] = kept
    # addendum (g): at most 5 distinct names of tool sources; an advanced one from a developer only
    tools = body.get("tools") or []
    if not isinstance(tools, list) or len(tools) > LIM["tools"] or len(set(map(str, tools))) != len(tools):
        errors.append(("tools", "too_many"))
    else:
        kept = []
        for i, t in enumerate(tools):
            known = isinstance(t, str) and any(x["name"] == t for x in TOOL_SOURCES)
            if known and (DEVELOPER or t not in ADVANCED_TOOLS):
                kept.append(t)
            elif dropping:
                dropped.append({"field": "tools", "name": t if isinstance(t, str) else None})
            elif known:
                errors.append(("tools", "developer_only", i))
            else:
                errors.append(("tools", "not_a_tool_source", i))
        body["tools"] = kept
    model = body.get("model")
    if model is not None and model not in {m["id"] for m in MODELS}:
        if dropping:
            dropped.append({"field": "model", "name": None})
            body["model"] = None
        else:
            errors.append(("model", "unknown_model"))
    return errors, dropped


def check_skill(body: dict) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    if not isinstance(body, dict):
        return [("item", "pattern")]
    for k in body:
        if k not in SKILL_KEYS:
            errors.append((k, "unknown_key"))
    name = body.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or CONTROL_RE.search(name):
        errors.append(("name", "pattern"))
    r = text_ok(body.get("description") or "", min_len=0, max_len=LIM["description"], one_line=True)
    if r:
        errors.append(("description", r))
    r = text_ok(body.get("content"), min_len=1, max_len=LIM["content"])
    if r:
        errors.append(("content", r))
    return errors


RULE_TEXT = {"pattern": "is not a valid name or value", "too_long": "is too long", "too_short": "is empty",
             "too_many": "has too many entries", "control": "holds control characters", "unknown_key": "is not a key of this format",
             "unknown_model": "names a model that does not exist here", "limit": "would pass your limit",
             "developer_only": "needs the developer role", "not_a_tool_source": "is not a tool source you can use"}


def rule_text(code: str) -> str:
    return RULE_TEXT.get(code, code)


def free_name(name: str, taken: set[str]) -> str:
    if name.lower() not in taken:
        return name
    for i in range(2, 1000):
        suffix = f" {i}"
        cand = name[: 64 - len(suffix)] + suffix
        if cand.lower() not in taken:
            return cand
    return name


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "webchat-fake/1.0"

    def log_message(self, fmt, *args):  # quieter log
        sys.stderr.write("%s %s\n" % (self.command, fmt % args))

    # ---- plumbing ----
    def send_json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_bytes(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def detail(self, status: int, text: str) -> None:
        self.send_json(status, {"detail": text})

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def json_body(self) -> dict:
        raw = self.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def ajax(self) -> bool:
        if self.headers.get("X-Requested-With") == "XMLHttpRequest":
            return True
        self.detail(400, "X-Requested-With: XMLHttpRequest is required")
        return False

    # ---- routing ----
    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        if path == "/chat":
            self.send_bytes(200, TEMPLATE.read_bytes(), MIME[".html"], {"Cache-Control": "no-store"})
        elif path == "/":
            self.send_bytes(200, b"<!doctype html><title>Classic chat</title><p>Classic chat stand-in. <a href='/chat'>New chat</a>", MIME[".html"])
        elif path.startswith("/static/"):
            self.serve_static(path)
        elif path.startswith("/chat/api/"):
            self.api("GET", path[len("/chat/api"):], parse_qs(url.query))
        else:
            self.detail(404, "not found")

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path.startswith("/chat/api/"):
            self.api("POST", url.path[len("/chat/api"):], parse_qs(url.query))
        else:
            self.detail(404, "not found")

    def do_PUT(self):
        url = urlsplit(self.path)
        if url.path.startswith("/chat/api/"):
            self.api("PUT", url.path[len("/chat/api"):], parse_qs(url.query))
        else:
            self.detail(404, "not found")

    def do_PATCH(self):
        url = urlsplit(self.path)
        if url.path.startswith("/chat/api/"):
            self.api("PATCH", url.path[len("/chat/api"):], parse_qs(url.query))
        else:
            self.detail(404, "not found")

    def do_DELETE(self):
        url = urlsplit(self.path)
        if url.path.startswith("/chat/api/"):
            self.api("DELETE", url.path[len("/chat/api"):], parse_qs(url.query))
        else:
            self.detail(404, "not found")

    def serve_static(self, path: str) -> None:
        rel = unquote(path[len("/static/"):])
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            self.detail(404, "not found")
            return
        self.send_bytes(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"),
                        {"Cache-Control": "no-store"})

    # ---- the API ----
    def api(self, method: str, path: str, query: dict) -> None:
        parts = [p for p in path.split("/") if p]
        # /files/{id} is a plain navigation (a Download link): no AJAX header required there.
        if parts[:1] == ["files"] and method == "GET" and len(parts) == 2:
            return self.get_file(parts[1], query)
        if not self.ajax():
            return
        if parts[:1] == ["files"] and len(parts) == 3 and parts[2] == "preview" and method == "GET":
            with STORE.lock:
                f = STORE.files.get(parts[1])
            if f is None:
                return self.detail(404, "not found")
            status, payload = preview_of(f)
            return self.send_json(status, payload, {"Cache-Control": "private, no-store"})
        if parts == ["me"] and method == "GET":
            return self.send_json(200, {"principal": PRINCIPAL, "models": MODELS, "app_name": APP_NAME,
                                        "developer": DEVELOPER, "access_default": "basic", "tiers": list(TIERS),
                                        "limits": {"max_file_bytes": MAX_FILE_BYTES, "max_files": MAX_FILES,
                                                   "media_types": MEDIA_TYPES}})
        if parts == ["catalog"] and method == "GET":
            access = (query.get("access", ["basic"])[0] or "basic")
            if access not in TIERS:
                return self.detail(422, "access must be basic or advanced")
            advanced = access == "advanced" and DEVELOPER  # else as if basic
            return self.send_json(200, {
                "agents": [{"ref": f"admin:{a['name']}", "name": a["name"], "description": a["description"], "tier": a["tier"], "source": "admin", "readonly": True}
                           for a in ADMIN_AGENTS if advanced or a["tier"] == "basic"],
                "skills": [{"name": s["name"], "description": s["description"], "source": "admin", "readonly": True} for s in ADMIN_SKILLS],
                "orchestrator": ORCHESTRATOR,
                "tool_sources": [{"name": t["name"], "description": t["description"], "tier": t["tier"], "connectors": list(t["connectors"])}
                                 for t in TOOL_SOURCES if advanced or t["tier"] == "basic"],
            })
        if parts[:1] == ["my"]:
            return self.my(method, parts[1:], query)
        if parts == ["conversations"]:
            if method == "GET":
                q = (query.get("q", [""])[0] or "").lower()
                limit = int(query.get("limit", ["50"])[0] or 50)
                with STORE.lock:
                    convs = sorted(STORE.conversations.values(), key=lambda c: c["updated_at"], reverse=True)
                    items = [STORE.conv_summary(c) for c in convs if not q or q in c["title"].lower()][:limit]
                return self.send_json(200, items)
            if method == "POST":
                data = self.json_body()
                title = str(data.get("title") or "New chat")[:120]
                model = data.get("model")
                if model is not None and model not in {m["id"] for m in MODELS}:
                    return self.detail(422, "unknown model")
                ref = data.get("agent_ref")
                access = data.get("access", "basic")
                if access not in TIERS:
                    return self.detail(422, "access must be basic or advanced")
                if access == "advanced" and not DEVELOPER:
                    return self.detail(403, ACCESS_DENIED)
                with STORE.lock:
                    ok, name = STORE.resolve_ref(ref, access)
                    if not ok:
                        return self.detail(422, "Unknown agent")
                    conv = STORE.new_conversation(title, model)
                    conv["access"] = access
                    conv["agent_ref"], conv["agent_label"] = ref, (name if ref else None)
                    out = STORE.conv_summary(conv)
                out["model"] = model
                return self.send_json(201, out)
            return self.detail(405, "method not allowed")
        if parts[:1] == ["conversations"] and len(parts) >= 2:
            cid = parts[1]
            with STORE.lock:
                conv = STORE.conversations.get(cid)
            if conv is None:
                return self.detail(404, "not found")
            if len(parts) == 2:
                if method == "GET":
                    with STORE.lock:
                        return self.send_json(200, STORE.conv_detail(conv))
                if method == "PATCH":
                    data = self.json_body()
                    with STORE.lock:
                        if "title" in data:
                            title = str(data["title"] or "").strip()
                            if not title or len(title) > 120:
                                return self.detail(422, "title must be 1 to 120 characters")
                            conv["title"] = title
                        if "model" in data:
                            if data["model"] is not None and data["model"] not in {m["id"] for m in MODELS}:
                                return self.detail(422, "unknown model")
                            conv["model_name"] = data["model"]
                        access = conv.get("access", "basic")
                        if "access" in data:
                            if data["access"] not in TIERS:
                                return self.detail(422, "access must be basic or advanced")
                            if data["access"] == "advanced" and not DEVELOPER:
                                return self.detail(403, ACCESS_DENIED)
                            access = data["access"]
                        if "agent_ref" in data:
                            # checked against the access of this PATCH (both keys in one body are one decision)
                            ok, name = STORE.resolve_ref(data["agent_ref"], access)
                            if not ok:
                                return self.detail(422, "Unknown agent")
                            conv["agent_ref"] = data["agent_ref"]
                            conv["agent_label"] = name if data["agent_ref"] else None
                        elif "access" in data:
                            # as the real route: an agent the new access does not offer
                            # falls back to the orchestrator (like a deleted agent)
                            ok, _name = STORE.resolve_ref(conv.get("agent_ref"), access)
                            if not ok:
                                conv["agent_ref"], conv["agent_label"] = None, None
                        conv["access"] = access
                        detail = STORE.conv_detail(conv)
                    return self.send_json(200, detail)
                if method == "DELETE":
                    with STORE.lock:
                        STORE.conversations.pop(cid, None)
                        STORE.messages.pop(cid, None)
                        for fid in [f["id"] for f in STORE.files.values() if f["conversation_id"] == cid]:
                            STORE.files.pop(fid, None)
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self.detail(405, "method not allowed")
            action = parts[2]
            if action == "messages" and method == "POST":
                return self.post_message(conv)
            if action == "cancel" and method == "POST":
                with STORE.lock:
                    running = conv["status"] == "running"
                    if running:
                        STORE.cancel_flags[cid].set()
                return self.send_json(200, {"canceled": running})
            if action == "stream" and method == "GET":
                return self.detail(409, "not_running")
            if action == "context":
                if len(parts) == 3 and method == "GET":
                    with STORE.lock:
                        return self.send_json(200, STORE.context(conv))
                if len(parts) == 5 and parts[3] == "attachments" and method == "POST":
                    data = self.json_body()
                    if not isinstance(data.get("excluded"), bool):
                        return self.detail(422, "excluded must be a boolean")
                    with STORE.lock:
                        f = STORE.files.get(parts[4])
                        if f is None or f["conversation_id"] != cid or f["kind"] != "upload":
                            return self.detail(404, "not found")
                        if conv["status"] == "running":
                            return self.detail(409, "a run is in progress")
                        f["excluded"] = data["excluded"]
                        return self.send_json(200, {"file_id": f["id"], "excluded": f["excluded"]})
        return self.detail(404, "not found")

    # ---- addendum (c): /my/... ----
    def errors_422(self, errs: list[tuple[str, str]]) -> None:
        self.send_json(422, {"detail": [v_err(*e) for e in errs]})

    def my(self, method: str, parts: list[str], query: dict) -> None:
        if parts == ["export"] and method == "GET":
            def ids(key):
                raw = ",".join(query.get(key, []))
                return [x for x in raw.split(",") if x] if raw else None
            with STORE.lock:
                bundle = STORE.bundle(ids("agents"), ids("skills"))
            body = json.dumps(bundle, indent=1).encode()
            return self.send_bytes(200, body, "application/json", {
                "Content-Disposition": f'attachment; filename="webchat-bundle-{now().date().isoformat()}.json"',
                "Cache-Control": "private, no-store"})
        if parts == ["import"] and method == "POST":
            return self.import_bundle()
        if not parts or parts[0] not in ("agents", "skills"):
            return self.detail(404, "not found")
        kind = parts[0]
        table = STORE.my_agents if kind == "agents" else STORE.my_skills
        out = STORE.agent_out if kind == "agents" else STORE.skill_out
        if len(parts) == 1:
            if method == "GET":
                with STORE.lock:
                    rows = sorted(table.values(), key=lambda r: r["name"].lower())
                    return self.send_json(200, [out(r) for r in rows])
            if method == "POST":
                return self.write_my(kind, None)
            return self.detail(405, "method not allowed")
        if len(parts) == 2:
            rid = parts[1]
            with STORE.lock:
                row = table.get(rid)
            if row is None:
                return self.detail(404, "not found")
            if method == "GET":
                return self.send_json(200, out(row))
            if method == "PUT":
                return self.write_my(kind, rid)
            if method == "DELETE":
                with STORE.lock:
                    if kind == "agents":
                        STORE.delete_agent(rid)
                    else:
                        STORE.delete_skill(rid)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self.detail(405, "method not allowed")
        return self.detail(404, "not found")

    def write_my(self, kind: str, rid: str | None) -> None:
        raw = self.body()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self.errors_422([("body", "pattern")])
        if not isinstance(body, dict):
            return self.errors_422([("body", "pattern")])
        with STORE.lock:
            table = STORE.my_agents if kind == "agents" else STORE.my_skills
            if kind == "agents":
                errs, _dropped = check_agent(body, my_skill_names={s["name"] for s in STORE.my_skills.values()})
            else:
                errs = check_skill(body)
            if errs:
                return self.errors_422(errs)
            taken = {r["name"].lower() for r in table.values() if r["id"] != rid}
            if body["name"].lower() in taken:
                return self.detail(409, "an agent of this name exists" if kind == "agents" else "a skill of this name exists")
            if rid is None and len(table) >= LIM[kind]:
                return self.detail(422, f"the limit of {LIM[kind]} {kind} is reached")
            row = STORE.put_agent(rid, body) if kind == "agents" else STORE.put_skill(rid, body)
            payload = STORE.agent_out(row) if kind == "agents" else STORE.skill_out(row)
        return self.send_json(200 if rid else 201, payload)

    def import_bundle(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        rule = "rename"
        if ctype.startswith("multipart/form-data"):
            fields, uploads = parse_multipart(ctype, self.body())
            rule = fields.get("on_conflict") or "rename"
            if not uploads:
                return self.detail(422, "file is required")
            raw = uploads[0][2]
        else:
            raw = self.body()
        if len(raw) > LIM["bundle"]:
            return self.detail(413, "bundle too large")
        if rule not in ("rename", "replace", "skip"):
            return self.detail(422, "on_conflict must be rename, replace or skip")
        try:
            bundle = json.loads(raw)
        except ValueError:
            return self.detail(422, "the file is not JSON")
        if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
            return self.detail(422, "not a webchat-bundle/1 file")
        agents_in = bundle.get("agents") or []
        skills_in = bundle.get("skills") or []
        if not isinstance(agents_in, list) or not isinstance(skills_in, list) or len(agents_in) > 50 or len(skills_in) > 100:
            return self.detail(422, "the bundle holds too many items")
        result = {"agents": {"created": 0, "replaced": 0, "skipped": 0, "renamed": []},
                  "skills": {"created": 0, "replaced": 0, "skipped": 0, "renamed": []},
                  "dropped": [], "errors": []}
        with STORE.lock:
            def place(kind: str, item: dict, table: dict, put) -> None:
                label = f"{kind[:-1]}:{item.get('name') if isinstance(item.get('name'), str) else '?'}"
                by_name = {r["name"].lower(): r for r in table.values()}
                existing = by_name.get(item["name"].lower())
                if existing is not None:
                    if rule == "skip":
                        result[kind]["skipped"] += 1
                        return
                    if rule == "replace":
                        put(existing["id"], item)
                        result[kind]["replaced"] += 1
                        return
                    new_name = free_name(item["name"], set(by_name))
                    result[kind]["renamed"].append({"from": item["name"], "to": new_name})
                    item = {**item, "name": new_name}
                if len(table) >= LIM[kind]:
                    result["errors"].append({"item": label, "field": "name", "rule": rule_text("limit")})
                    return
                put(None, item)
                result[kind]["created"] += 1

            for i, item in enumerate(skills_in):
                label = f"skill:{item.get('name')}" if isinstance(item, dict) and isinstance(item.get('name'), str) else f"skill:#{i}"
                errs = check_skill(item)
                if errs:
                    result["errors"].extend({"item": label, "field": f, "rule": rule_text(e[1])} for e in errs for f in [e[0]])
                    continue
                place("skills", item, STORE.my_skills, STORE.put_skill)
            my_skill_names = {s["name"] for s in STORE.my_skills.values()}
            for i, item in enumerate(agents_in):
                label = f"agent:{item.get('name')}" if isinstance(item, dict) and isinstance(item.get('name'), str) else f"agent:#{i}"
                errs, dropped = check_agent(item, my_skill_names=my_skill_names, dropping=True)
                if errs:
                    result["errors"].extend({"item": label, "field": f, "rule": rule_text(e[1])} for e in errs for f in [e[0]])
                    continue
                result["dropped"].extend({"item": label, **d} for d in dropped)
                place("agents", item, STORE.my_agents, STORE.put_agent)
        return self.send_json(200, result)

    def get_file(self, fid: str, query: dict) -> None:
        with STORE.lock:
            f = STORE.files.get(fid)
        if f is None:
            return self.detail(404, "not found")
        inline = query.get("inline", ["0"])[0] == "1" and f["media_type"] in ("image/png", "image/jpeg", "application/pdf")
        disp = "inline" if inline else "attachment"
        self.send_bytes(200, f["data"], f["media_type"], {
            "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(f['name'])}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        })

    # ---- the scripted turn ----
    def post_message(self, conv: dict) -> None:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self.detail(415, "multipart/form-data expected")
        fields, uploads = parse_multipart(ctype, self.body())
        text = (fields.get("text") or "").strip()
        if not text:
            return self.detail(422, "text is required")
        if len(text.encode()) > 64 * 1024:
            return self.detail(422, "text is too long")
        if len(uploads) > MAX_FILES:
            return self.detail(422, "too many files")
        model = fields.get("model")
        stored = []
        for name, declared, data in uploads:
            if len(data) > MAX_FILE_BYTES:
                return self.detail(413, "file too large")
            mt = sniff(name, declared, data)
            if mt is None:
                return self.detail(415, "file type not accepted")
            stored.append((name, mt, data))
        cid = conv["id"]
        with STORE.lock:
            if conv["status"] == "running":
                return self.detail(409, "a run is in progress")
            conv["status"] = "running"
            STORE.cancel_flags[cid].clear()
            if model:
                conv["model_name"] = model
            first_turn = not STORE.messages[cid]
            file_rows = [STORE.add_file(cid, None, "upload", n, mt, d) for n, mt, d in stored]
            um = STORE.add_message(cid, "user", text, files=[f["id"] for f in file_rows], at=now())
            for f in file_rows:
                f["message_id"] = um["id"]
            am = STORE.add_message(cid, "assistant", "", status="running", at=now())
        cancel = STORE.cancel_flags[cid]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        parts: list[dict] = []
        content = []

        def emit(event: str, data: dict) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        def text_part(delta: str) -> None:
            content.append(delta)
            if parts and parts[-1]["type"] == "text":
                parts[-1]["text"] += delta
            else:
                parts.append({"type": "text", "text": delta})
            emit("text", {"delta": delta})

        def merge(kind: str, pid: str, frame: dict) -> None:
            for i, p in enumerate(parts):
                if p["type"] == kind and p["id"] == pid:
                    parts[i] = {**p, **frame}
                    break
            else:
                parts.append({"type": kind, **frame})

        def tool(tid: str, name: str, status: str, *, agent: str | None, agent_id: str | None, **extra) -> None:
            frame = {"id": tid, "agent": agent, "agent_id": agent_id, "name": name, "status": status, **extra}
            merge("tool", tid, frame)
            emit("tool", frame)

        def agent(aid: str, name: str, status: str, *, parent: str | None, **extra) -> None:
            frame = {"id": aid, "agent": name, "parent": parent, "status": status, **extra}
            merge("agent", aid, frame)
            emit("agent", frame)

        def note(t: str) -> None:
            parts.append({"type": "note", "text": t})
            emit("note", {"text": t})

        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        def finish(status: str) -> None:
            with STORE.lock:
                am["content"] = "".join(content)
                am["parts"] = parts
                am["status"] = status
                conv["status"] = "idle"
                conv["updated_at"] = now()
                if status == "canceled":
                    am["error"] = "canceled"
            emit("done", {"message_id": am["id"], "status": status, "duration_ms": elapsed_ms()})

        def canceled() -> bool:
            if cancel.is_set():
                emit("error", {"code": "canceled", "message": "The run was stopped."})
                finish("canceled")
                return True
            return False

        try:
            emit("start", {"user_message_id": um["id"], "assistant_message_id": am["id"],
                           "files": [{"id": f["id"], "name": f["name"], "media_type": f["media_type"], "size": f["size"]} for f in file_rows]})
            nap(0.4)
            with STORE.lock:
                ref_ok, _ = STORE.resolve_ref(conv.get("agent_ref"), conv.get("access", "basic"))
            if not ref_ok:
                emit("error", {"code": "agent_unavailable", "message": "The selected agent is no longer available."})
                with STORE.lock:
                    am["error"] = "agent_unavailable"
                finish("failed")
                return
            note("Reading your question" + (" and the attachments" if file_rows else ""))
            nap(0.9)
            if canceled():
                return
            a1_start = time.monotonic()
            agent("a1", "btp-admin", "running", parent=None,
                  prompt="Which subaccounts of the Infrabel-dev directory have entitlements with spare quota? " + text[:200])
            note("Asking btp-admin")
            nap(0.5)
            tool("t1", "list_subaccounts", "running", agent="btp-admin", agent_id="a1", args="directory=Infrabel-dev")
            note("btp-admin is listing the subaccounts")
            nap(1.4)
            if canceled():
                return
            tool("t1", "list_subaccounts", "done", agent="btp-admin", agent_id="a1", output="3 subaccounts", duration_ms=int(1400 * SLOW))
            tool("t2", "list_entitlements", "running", agent="btp-admin", agent_id="a1", args="subaccount=b-fdn-dev")
            note("btp-admin is reading the entitlements")
            nap(1.4)
            if canceled():
                return
            tool("t2", "list_entitlements", "done", agent="btp-admin", agent_id="a1", output="12 services, 2 with spare quota", duration_ms=int(1400 * SLOW))
            # a nested delegation: btp-admin consults a peer
            a2_start = time.monotonic()
            agent("a2", "entitlement-analyst", "running", parent="a1",
                  prompt="Rank these 12 entitlements by unused quota and flag the ones nobody used in 90 days.")
            note("btp-admin is asking entitlement-analyst")
            nap(0.4)
            tool("t3", "usage_history", "running", agent="entitlement-analyst", agent_id="a2", args="subaccount=b-fdn-dev days=90")
            nap(1.2)
            if canceled():
                return
            tool("t3", "usage_history", "done", agent="entitlement-analyst", agent_id="a2", output="2 services without usage", duration_ms=int(1200 * SLOW))
            nap(0.3)
            agent("a2", "entitlement-analyst", "done", parent="a1",
                  output="2 flagged: Destination (8/20, no instance since July), Job Scheduling (3/5).",
                  duration_ms=int((time.monotonic() - a2_start) * 1000),
                  usage={"input_tokens": 640, "output_tokens": 95, "requests": 1, "model": conv["model_name"] or "gpt-5.6-terra"})
            nap(0.4)
            if canceled():
                return
            agent("a1", "btp-admin", "done", parent=None,
                  output="3 subaccounts; b-fdn-dev has 2 services with unused quota (Destination 8 of 20, Job Scheduling 3 of 5).",
                  duration_ms=int((time.monotonic() - a1_start) * 1000),
                  usage={"input_tokens": 1820, "output_tokens": 260, "requests": 3, "model": conv["model_name"] or "gpt-5.6-terra"})
            tool("t4", "create_workbook", "running", agent=None, agent_id=None, args="title=entitlements rows=12")
            note("Composing the answer")
            nap(0.4)
            tool("t4", "create_workbook", "done", agent=None, agent_id=None, output="report.xlsx (9 KB)", duration_ms=int(400 * SLOW))
            nap(0.2)
            # text deltas: word-ish chunks so the markdown re-renders visibly
            buf = ANSWER_MD + ("\n\n" + ABAP_ANSWER_MD if conv.get("access") == "advanced" else "")
            i = 0
            n = 0
            while i < len(buf):
                step = 6 if n % 7 else 24
                delta = buf[i:i + step]
                i += step
                n += 1
                text_part(delta)
                if n % 3 == 0 and canceled():
                    return
                nap(0.012)
            nap(0.4)
            if canceled():
                return
            with STORE.lock:
                x = STORE.add_file(cid, am["id"], "generated", "report.xlsx", MEDIA_TYPES[8], b"PK\x03\x04" + b"\0" * 9100)
            parts.append({"type": "file", "file_id": x["id"], "name": x["name"], "media_type": x["media_type"], "size": x["size"]})
            emit("file", {"file_id": x["id"], "name": x["name"], "media_type": x["media_type"], "size": x["size"]})
            nap(0.5)
            with STORE.lock:
                d = STORE.add_file(cid, am["id"], "generated", "summary.docx", MEDIA_TYPES[7], b"PK\x03\x04" + b"\0" * 22600)
            parts.append({"type": "file", "file_id": d["id"], "name": d["name"], "media_type": d["media_type"], "size": d["size"]})
            emit("file", {"file_id": d["id"], "name": d["name"], "media_type": d["media_type"], "size": d["size"]})
            nap(0.3)
            if first_turn or conv["title"] == "New chat":
                title = text.splitlines()[0][:120]
                with STORE.lock:
                    conv["title"] = title
                emit("title", {"title": title})
            # an admin agent runs on its own model, whatever the conversation picked
            model = "gpt-5.6-terra" if str(conv.get("agent_ref") or "").startswith("admin:") else (conv["model_name"] or "gpt-5.6-terra")
            usage = {"input_tokens": 3410, "output_tokens": 812, "cache_read_tokens": 1024, "total_tokens": 4222,
                     "requests": 6, "tool_calls": 4, "model": model, "duration_ms": elapsed_ms(),
                     "agents": [{"id": "a1", "agent": "btp-admin", "input_tokens": 1820, "output_tokens": 260, "requests": 3, "model": model},
                                {"id": "a2", "agent": "entitlement-analyst", "input_tokens": 640, "output_tokens": 95, "requests": 1, "model": model}]}
            parts.append({"type": "usage", **usage})
            emit("usage", usage)
            finish("done")
        except (BrokenPipeError, ConnectionResetError):
            # client went away: the turn still finishes and is stored
            with STORE.lock:
                am["content"] = "".join(content) or ANSWER_MD
                am["parts"] = parts
                am["status"] = "done"
                conv["status"] = "idle"
                conv["updated_at"] = now()


def main(argv=None) -> int:
    global FAST, SLOW, DEVELOPER, ADMIN
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=7935)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--fast", action="store_true", help="no sleeps in the scripted turn")
    ap.add_argument("--slow", type=float, default=1.0, help="multiply every sleep (screenshots mid-turn)")
    ap.add_argument("--basic-user", action="store_true", help="the signed-in user has no developer role (addendum d)")
    ap.add_argument("--admin", action="store_true", help="the signed-in user has the admin scope: instructions visible in the context drawer (addendum e)")
    args = ap.parse_args(argv)
    ADMIN = args.admin
    FAST = args.fast
    SLOW = max(0.0, args.slow)
    if args.basic_user:
        DEVELOPER = False
        STORE.__init__()  # re-seed without the developer-only rows
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"webchat fake server on http://{args.host}:{args.port}/chat", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
