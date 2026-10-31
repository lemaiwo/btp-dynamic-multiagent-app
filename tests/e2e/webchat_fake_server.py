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

then open http://127.0.0.1:7935/chat . ``Ctrl-C`` stops it.

What it serves:

- ``GET /chat``            -> ``templates/webchat.html``
- ``GET /static/...``      -> the ``static/`` folder
- ``GET /``                -> a one-line stand-in for the classic chat
- ``GET  /chat/api/me``
- ``GET  /chat/api/conversations?q=``  ``POST /chat/api/conversations``
- ``GET|PATCH|DELETE /chat/api/conversations/{id}``
- ``POST /chat/api/conversations/{id}/messages`` -> a scripted SSE turn
  (start, note, text deltas, two ``tool`` events of agent ``btp-admin``,
  two ``file`` events, ``title`` on the first turn, ``done``), with small
  sleeps so streaming is visible; 409 while a turn runs
- ``POST /chat/api/conversations/{id}/cancel`` -> the running turn ends with
  ``error canceled`` + ``done canceled``
- ``GET  /chat/api/conversations/{id}/stream`` -> 409 ``not_running``
- ``GET  /chat/api/files/{id}[?inline=1]``   -> dummy bytes, the real headers

Three conversations are seeded (today, yesterday, older) so the sidebar
groups show; the first one holds a stored turn with parts and files.
A request without ``X-Requested-With: XMLHttpRequest`` on ``/chat/api`` is
answered 400, so a missing header is noticed during development.
"""
# ruff: noqa: E501  -- canned markdown and seed strings are long on purpose
from __future__ import annotations

import argparse
import json
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

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
}

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
            {"type": "tool", "id": "t1", "agent": "btp-admin", "name": "list_subaccounts", "status": "done",
             "args": "directory=Infrabel-dev", "output": "3 subaccounts"},
            {"type": "tool", "id": "t2", "agent": "btp-admin", "name": "list_entitlements", "status": "done",
             "args": "subaccount=b-fdn-dev", "output": "12 services, 2 with spare quota"},
            {"type": "text", "text": ANSWER_MD},
            {"type": "file", "file_id": x["id"], "name": x["name"], "media_type": x["media_type"], "size": x["size"]},
            {"type": "file", "file_id": d["id"], "name": d["name"], "media_type": d["media_type"], "size": d["size"]},
        ]
        c2 = self.new_conversation("Destinations of the sandbox", "gpt-5.6-terra", t - timedelta(days=1, hours=2))
        self.add_message(c2["id"], "user", "List the destinations of the sandbox subaccount.")
        self.add_message(c2["id"], "assistant", "There are **4** destinations: `S4H_DEV`, `JIRA`, `SHAREPOINT`, `BITBUCKET`.", status="done")
        c3 = self.new_conversation("Role collections for the invoke API", "anthropic--claude-4.5-sonnet", t - timedelta(days=4))
        self.add_message(c3["id"], "user", "Who has the Agent Invoker role collection?")
        self.add_message(c3["id"], "assistant", "", status="failed", error="RuntimeError: the destination service answered 503")
        c4 = self.new_conversation("Cloud Connector trust on prod", "gpt-5.6-terra", t - timedelta(days=23))
        self.add_message(c4["id"], "user", "Is the Cloud Connector trust configured on prod?")
        self.add_message(c4["id"], "assistant", "Yes, the location id `SCC-PRD` is trusted since 2026-09-02.", status="done")

    # ---- helpers ----
    def new_conversation(self, title: str, model: str | None, at: datetime | None = None) -> dict:
        at = at or now()
        conv = {"id": str(uuid.uuid4()), "principal": PRINCIPAL, "title": title[:120] or "New chat",
                "model_name": model, "created_at": at, "updated_at": at, "status": "idle"}
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
                "created_at": utc(c["created_at"]), "status": c["status"]}

    def conv_detail(self, c: dict) -> dict:
        out = {"id": c["id"], "title": c["title"], "model": c["model_name"], "status": c["status"],
               "created_at": utc(c["created_at"]), "updated_at": utc(c["updated_at"]), "messages": []}
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
        if parts == ["me"] and method == "GET":
            return self.send_json(200, {"principal": PRINCIPAL, "models": MODELS, "app_name": APP_NAME,
                                        "limits": {"max_file_bytes": MAX_FILE_BYTES, "max_files": MAX_FILES,
                                                   "media_types": MEDIA_TYPES}})
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
                with STORE.lock:
                    conv = STORE.new_conversation(title, model)
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
        return self.detail(404, "not found")

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

        def tool(tid: str, name: str, status: str, **extra) -> None:
            card = {"type": "tool", "id": tid, "agent": "btp-admin", "name": name, "status": status, **extra}
            for i, p in enumerate(parts):
                if p["type"] == "tool" and p["id"] == tid:
                    parts[i] = {**p, **card}
                    break
            else:
                parts.append(card)
            emit("tool", {"id": tid, "agent": "btp-admin", "name": name, "status": status, **extra})

        def note(t: str) -> None:
            parts.append({"type": "note", "text": t})
            emit("note", {"text": t})

        def finish(status: str) -> None:
            with STORE.lock:
                am["content"] = "".join(content)
                am["parts"] = parts
                am["status"] = status
                conv["status"] = "idle"
                conv["updated_at"] = now()
                if status == "canceled":
                    am["error"] = "canceled"
            emit("done", {"message_id": am["id"], "status": status})

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
            note("Reading your question" + (" and the attachments" if file_rows else ""))
            nap(0.9)
            if canceled():
                return
            tool("t1", "list_subaccounts", "running", args="directory=Infrabel-dev")
            note("btp-admin is listing the subaccounts")
            nap(1.4)
            if canceled():
                return
            tool("t1", "list_subaccounts", "done", output="3 subaccounts")
            tool("t2", "list_entitlements", "running", args="subaccount=b-fdn-dev")
            note("btp-admin is reading the entitlements")
            nap(1.4)
            if canceled():
                return
            tool("t2", "list_entitlements", "done", output="12 services, 2 with spare quota")
            note("Composing the answer")
            nap(0.3)
            # text deltas: word-ish chunks so the markdown re-renders visibly
            buf = ANSWER_MD
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
    global FAST, SLOW
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=7935)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--fast", action="store_true", help="no sleeps in the scripted turn")
    ap.add_argument("--slow", type=float, default=1.0, help="multiply every sleep (screenshots mid-turn)")
    args = ap.parse_args(argv)
    FAST = args.fast
    SLOW = max(0.0, args.slow)
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
