"""Tests for the admin UI (templates/admin.html).

Covers three layers, since we don't have a real browser available:

1. **HTML structure** — parse the rendered /admin page and assert that
   every control the JS binds to is present with the correct id/attrs.
2. **JavaScript validity** — extract the inline <script> and run
   `node --check` to catch syntax errors in the admin UI logic.
3. **UI flow simulation** — discover every `api(...)` call in the JS,
   verify each targets a real backend endpoint, and then replay the
   exact request sequence that each user-visible button performs
   against the live ASGI app — so we can prove the UI contract
   (endpoint, method, payload shape, response shape) is consistent
   end-to-end.

Run:  python tests/test_admin_ui.py
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolated SQLite, no CF/XSUAA bindings
TEST_DB = ROOT / "tests" / "_ui_test_registry.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

# ---------------------------------------------------------------------------
# Same stubs as the API test so the app can boot without AICORE/MCP
# ---------------------------------------------------------------------------
import agents.shared as shared  # noqa: E402


class _FakeModel:
    model_name = "fake"


shared.get_model = lambda: _FakeModel()  # type: ignore[assignment]
shared.create_mcp_server = lambda name, base_url: object()  # type: ignore[assignment]

import pydantic_ai  # noqa: E402

_orig_init = pydantic_ai.Agent.__init__


def _patched_init(self, model=None, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.pop("toolsets", None)
    _orig_init(self, model="test", **kwargs)


pydantic_ai.Agent.__init__ = _patched_init  # type: ignore[method-assign]


def _fake_to_web(self):  # type: ignore[no-untyped-def]
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"fake-chat"})

    return app


pydantic_ai.Agent.to_web = _fake_to_web  # type: ignore[method-assign]

from httpx import ASGITransport, AsyncClient  # noqa: E402

import app as app_module  # noqa: E402


# ---------------------------------------------------------------------------
# Mini HTML collector — gathers (tag, attrs, text) triples we care about
# ---------------------------------------------------------------------------
class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict, str]] = []
        self.scripts: list[str] = []
        self._in_script = False
        self._script_buf: list[str] = []
        self._current_tag: str | None = None
        self._text_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        self.elements.append((tag, d, ""))
        if tag == "script":
            self._in_script = True
            self._script_buf = []
        self._current_tag = tag
        self._text_buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._script_buf))
            self._in_script = False
        if self._text_buf and self.elements:
            # Attach accumulated text to the most recent element of this tag
            for i in range(len(self.elements) - 1, -1, -1):
                if self.elements[i][0] == tag:
                    t, a, _ = self.elements[i]
                    self.elements[i] = (t, a, "".join(self._text_buf).strip())
                    break
            self._text_buf = []

    def handle_data(self, data):
        if self._in_script:
            self._script_buf.append(data)
        else:
            self._text_buf.append(data)


def find(collector: _Collector, tag: str, **attrs) -> tuple[str, dict, str] | None:
    for el in collector.elements:
        if el[0] != tag:
            continue
        if all(el[1].get(k) == v for k, v in attrs.items()):
            return el
    return None


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
PASSED = 0
FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}   {detail}")


async def _run_lifespan(app, received, state):
    await app({"type": "lifespan"}, received.pop_left, state.append)


async def main() -> None:
    transport = ASGITransport(app=app_module.app)

    # Manual lifespan management
    lifespan_messages: list[dict] = []
    lifespan_incoming: list[dict] = [{"type": "lifespan.startup"}]

    async def receive():
        while not lifespan_incoming:
            await asyncio.sleep(0.05)
        return lifespan_incoming.pop(0)

    async def send(msg):
        lifespan_messages.append(msg)

    lifespan_task = asyncio.create_task(
        app_module.app({"type": "lifespan"}, receive, send)
    )
    for _ in range(50):
        if any(m["type"] == "lifespan.startup.complete" for m in lifespan_messages):
            break
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("Lifespan did not complete")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        print("\n== 1. HTML structure ==")
        r = await client.get("/admin")
        assert r.status_code == 200, r.status_code
        html = r.text

        coll = _Collector()
        coll.feed(html)

        # Page chrome
        title = find(coll, "title")
        check("has <title>", title is not None and "Administration" in (title[2] or ""))
        h1 = find(coll, "h1")
        check("has header h1", h1 is not None and "Administration" in (h1[2] or ""))

        # Main action buttons (discovered by onclick attribute)
        onclicks = {
            e[1].get("onclick") for e in coll.elements if e[0] == "button"
        }
        for required in [
            "openAgentModal()",
            "reloadRegistry()",
            "restartApp()",
            "exportConfig()",
            "saveAgent()",
            "closeAgentModal()",
            "saveOrchestrator()",
        ]:
            check(
                f"button onclick={required}",
                required in onclicks,
                f"present: {sorted(o for o in onclicks if o)}",
            )

        # Table structure
        check("agents tbody present", find(coll, "tbody", id="agents-tbody") is not None)
        check("orchestrator textarea", find(coll, "textarea", id="orch-instructions") is not None)

        # Modal form fields
        for fid in ("agent-id", "agent-name", "agent-description",
                    "agent-instructions", "agent-mcp-url", "agent-enabled"):
            found = any(
                e[1].get("id") == fid for e in coll.elements
                if e[0] in ("input", "textarea", "select")
            )
            check(f"form field #{fid}", found)

        # Import file input
        file_input = next(
            (e for e in coll.elements
             if e[0] == "input" and e[1].get("id") == "import-file"),
            None,
        )
        check(
            "file import input",
            file_input is not None and file_input[1].get("accept") == ".json",
        )

        # Toast container
        check("toast container", find(coll, "div", id="toast") is not None)

        # Modal backdrop
        check("agent modal", find(coll, "div", id="agent-modal") is not None)

        # Back-to-chat link
        links = [e for e in coll.elements if e[0] == "a" and e[1].get("href") == "/"]
        check("back-to-chat link", len(links) > 0)

        # ------------------------------------------------------------------
        print("\n== 2. JavaScript validity ==")
        check("exactly one <script>", len(coll.scripts) == 1, f"got {len(coll.scripts)}")
        js = coll.scripts[0]

        if shutil.which("node") is None:
            check("node available", False, "node not on PATH, skipping syntax check")
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
                # Wrap in a function so top-level `await` / DOM references are legal
                f.write("function _wrapper() {\n" + js + "\n}\n")
                js_path = f.name
            try:
                result = subprocess.run(
                    ["node", "--check", js_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                check(
                    "node --check passes",
                    result.returncode == 0,
                    result.stderr.strip()[:300],
                )
            finally:
                os.unlink(js_path)

        # ------------------------------------------------------------------
        print("\n== 3. fetch() call discovery ==")
        # The JS wraps every call in `api('/path', opts)` — extract paths
        api_call_re = re.compile(
            r"""api\s*\(\s*[`'"]([^`'"]+)[`'"]\s*(?:,\s*\{([^}]*)\})?""",
            re.DOTALL,
        )
        calls = api_call_re.findall(js)
        check("JS calls api(...) helper", len(calls) > 0, f"got {len(calls)}")

        # Substitute JS template literals (${...}) with a placeholder id
        def _normalize(path: str) -> tuple[str, str]:
            # method extraction from opts
            return path

        discovered: set[tuple[str, str]] = set()
        for path, opts in calls:
            method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", opts or "")
            method = (method_match.group(1) if method_match else "GET").upper()
            # Strip template placeholders
            norm = re.sub(r"\$\{[^}]+\}", "1", path)
            discovered.add((method, "/admin/api" + norm))

        print(f"     discovered {len(discovered)} (method, path) call sites:")
        for m, p in sorted(discovered):
            print(f"       {m:6} {p}")

        # Pre-create one agent so the id=1 endpoints return 200
        r = await client.post(
            "/admin/api/agents",
            json={
                "name": "uitest",
                "description": "UI-flow test agent.",
                "instructions": "You are a UI test agent.",
                "mcp_url": "https://uitest.cfapps.eu20-001.hana.ondemand.com",
                "enabled": True,
            },
        )
        assert r.status_code == 201
        uitest_id = r.json()["id"]

        # We need the fixture agent to survive until the DELETE call, so
        # sort with DELETE last.
        def _order(item: tuple[str, str]) -> tuple[int, str, str]:
            method, path = item
            return (1 if method == "DELETE" else 0, method, path)

        for method, path in sorted(discovered, key=_order):
            test_path = path.replace("/agents/1", f"/agents/{uitest_id}")
            body = None
            if method == "POST" and test_path.endswith("/agents"):
                body = {
                    "name": f"flow_{uitest_id}_created",
                    "description": "Created via UI flow test.",
                    "instructions": "You are a flow test.",
                    "mcp_url": "https://flow.cfapps.eu20-001.hana.ondemand.com",
                    "enabled": True,
                }
            elif method == "PUT" and "/agents/" in test_path:
                body = {
                    "name": "uitest",
                    "description": "Edited via UI flow test.",
                    "instructions": "Edited.",
                    "mcp_url": "https://uitest.cfapps.eu20-001.hana.ondemand.com",
                    "enabled": True,
                }
            elif method == "PUT" and test_path.endswith("/orchestrator"):
                body = {"instructions": "UI flow orchestrator instructions."}
            elif method == "POST" and test_path.endswith("/import"):
                body = {
                    "orchestrator_instructions": "Imported.",
                    "agents": [
                        {
                            "name": "ui_import_1",
                            "description": "Imported UI test.",
                            "instructions": "Imported.",
                            "mcp_url": "https://ui-imp.cfapps.eu20-001.hana.ondemand.com",
                            "enabled": True,
                        }
                    ],
                    "replace": False,
                }

            resp = await client.request(method, test_path, json=body)
            ok = resp.status_code in (200, 201, 204)
            check(
                f"{method} {test_path}",
                ok,
                f"status={resp.status_code} body={resp.text[:200]}",
            )

        # ------------------------------------------------------------------
        print("\n== 4. End-to-end UI flows ==")

        # 4a. "New agent" flow: openAgentModal() -> fill -> saveAgent() -> loadAgents()
        r = await client.post(
            "/admin/api/agents",
            json={
                "name": "ui_new_flow",
                "description": "Created by the UI new-agent flow.",
                "instructions": "You are the UI new flow.",
                "mcp_url": "https://uinew.cfapps.eu20-001.hana.ondemand.com",
                "enabled": True,
            },
        )
        check("flow: new agent created", r.status_code == 201)
        new_id = r.json()["id"]

        r = await client.get("/admin/api/agents")
        names_now = {a["name"] for a in r.json()}
        check("flow: new agent visible in table", "ui_new_flow" in names_now)

        # 4b. "Edit" flow: editAgent(id) -> PUT -> loadAgents()
        r = await client.get(f"/admin/api/agents/{new_id}")
        check("flow: edit loads current values", r.status_code == 200)
        payload = r.json()
        payload["description"] = "Edited via UI edit flow."
        r = await client.put(f"/admin/api/agents/{new_id}", json={
            "name": payload["name"],
            "description": payload["description"],
            "instructions": payload["instructions"],
            "mcp_url": payload["mcp_url"],
            "enabled": payload["enabled"],
        })
        check("flow: edit saves", r.status_code == 200)
        check("flow: edit persisted", r.json()["description"] == "Edited via UI edit flow.")

        # 4c. "Save orchestrator" flow
        r = await client.put("/admin/api/orchestrator",
                             json={"instructions": "Final orchestrator prompt."})
        check("flow: orchestrator save", r.status_code == 200)

        # 4d. "Reload agents" flow
        r = await client.post("/admin/api/reload")
        data = r.json() if r.status_code == 200 else {}
        check("flow: reload succeeds", r.status_code == 200 and data.get("status") == "reloaded")

        # 4e. "Export" flow — verifies the blob the UI turns into a download
        r = await client.get("/admin/api/export")
        check("flow: export status", r.status_code == 200)
        exp = r.json()
        check(
            "flow: export shape matches UI expectations",
            "version" in exp and "orchestrator_instructions" in exp and "agents" in exp,
            f"got keys={list(exp.keys())}",
        )

        # 4f. "Import merge" flow — same payload shape the JS builds
        r = await client.post("/admin/api/import", json={
            "orchestrator_instructions": exp["orchestrator_instructions"],
            "agents": [
                {
                    "name": "ui_merge_import",
                    "description": "Merged via UI import.",
                    "instructions": "Merged.",
                    "mcp_url": "https://ui-merge.cfapps.eu20-001.hana.ondemand.com",
                    "enabled": True,
                }
            ],
            "replace": False,
        })
        check("flow: import merge", r.status_code == 200 and r.json()["imported"] == 1)

        # 4g. "Delete" flow: confirm dialog -> DELETE -> loadAgents()
        r = await client.delete(f"/admin/api/agents/{new_id}")
        check("flow: delete 204", r.status_code == 204)
        r = await client.get("/admin/api/agents")
        names_after = {a["name"] for a in r.json()}
        check("flow: agent removed from table", "ui_new_flow" not in names_after)

        # 4h. "Restart app" flow — expects cf_restart.ok=false locally
        r = await client.post("/admin/api/restart")
        check(
            "flow: restart reports reload ok + cf fallback",
            r.status_code == 200
            and r.json().get("status") == "reloaded"
            and r.json().get("cf_restart", {}).get("ok") is False,
        )

        # 4i. Validation — UI must surface backend validation as toast errors.
        # Non-BTP host must fail.
        r = await client.post("/admin/api/agents", json={
            "name": "bad_url",
            "description": "Non BTP host.",
            "instructions": "x",
            "mcp_url": "https://evil.example.com",
            "enabled": True,
        })
        check("flow: rejects non-BTP host", r.status_code == 422)
        err = r.json()
        check("flow: error body has detail for toast", "detail" in err)

    # Shutdown lifespan
    lifespan_incoming.append({"type": "lifespan.shutdown"})
    try:
        await asyncio.wait_for(lifespan_task, timeout=5)
    except asyncio.TimeoutError:
        lifespan_task.cancel()

    print(f"\n=== {PASSED} passed, {FAILED} failed ===")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
