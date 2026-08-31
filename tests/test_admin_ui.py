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
# A developer .env may set this. Set it EMPTY rather than popping it: app.py
# calls load_dotenv(), which fills in vars that are absent but never overrides
# ones already present. Empty means "no allowlist", i.e. the default rule.
os.environ["MCP_URL_ALLOWLIST"] = ""

# ---------------------------------------------------------------------------
# Same stubs as the API test so the app can boot without AICORE/MCP
# ---------------------------------------------------------------------------
import agents.shared as shared  # noqa: E402


class _FakeModel:
    model_name = "fake"


shared.get_model = lambda name=None: _FakeModel()  # type: ignore[assignment]
shared.create_mcp_server = lambda name, base_url, *a, **k: object()  # type: ignore[assignment]

import pydantic_ai  # noqa: E402

_orig_init = pydantic_ai.Agent.__init__


def _patched_init(self, model=None, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.pop("toolsets", None)
    _orig_init(self, model="test", **kwargs)


pydantic_ai.Agent.__init__ = _patched_init  # type: ignore[method-assign]


def _fake_to_web(self, *args, **kwargs):  # type: ignore[no-untyped-def]
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
            "openSkillModal()",
            "reloadRegistry()",
            "restartApp()",
            "exportConfig()",
            "saveAgent()",
            "closeAgentModal()",
            "saveSkill()",
            "closeSkillModal()",
            "saveOrchestrator()",
        ]:
            check(
                f"button onclick={required}",
                required in onclicks,
                f"present: {sorted(o for o in onclicks if o)}",
            )

        # Table structure
        check("agents tbody present", find(coll, "tbody", id="agents-tbody") is not None)
        check("skills tbody present", find(coll, "tbody", id="skills-tbody") is not None)
        check("orchestrator textarea", find(coll, "textarea", id="orch-instructions") is not None)
        # Run prompts carry multi-line content (e.g. mermaid syntax templates the
        # model must copy). A single-line <input> silently strips the newlines on
        # paste, so the tag itself is the requirement, not just the id.
        check("run prompt is a textarea, not an input",
              find(coll, "textarea", id="agent-run-prompt") is not None)

        # Modal form fields
        for fid in ("agent-id", "agent-name", "agent-description",
                    "agent-instructions", "agent-enabled", "agent-run-prompt",
                    "agent-model-name",
                    "skill-id", "skill-name", "skill-description", "skill-content"):
            found = any(
                e[1].get("id") == fid for e in coll.elements
                if e[0] in ("input", "textarea", "select")
            )
            check(f"form field #{fid}", found)

        # Container divs the JS renders into
        for did in ("agent-mcp-servers", "agent-skills", "agent-peers"):
            check(
                f"container #{did}",
                any(e[0] == "div" and e[1].get("id") == did for e in coll.elements),
            )

        check(
            "model override is a select, not a free-text input",
            any(e[0] == "select" and e[1].get("id") == "agent-model-name"
                for e in coll.elements),
        )

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

        # Modal backdrops
        check("agent modal", find(coll, "div", id="agent-modal") is not None)
        check("skill modal", find(coll, "div", id="skill-modal") is not None)

        # Back-to-chat link
        links = [e for e in coll.elements if e[0] == "a" and e[1].get("href") == "/"]
        check("back-to-chat link", len(links) > 0)

        # ------------------------------------------------------------------
        print("\n== report rendering assets ==")
        for name in ("marked.min.js", "purify.min.js", "mermaid.min.js"):
            check(f"{name} vendored", (ROOT / "static" / "vendor" / name).exists())

        # ------------------------------------------------------------------
        print("\n== 2. JavaScript validity ==")
        inline = [s for s in coll.scripts if s.strip()]
        check("exactly one inline <script>", len(inline) == 1, f"got {len(inline)}")
        js = inline[0]

        check("peers are collected on save", "collectAgentPeers()" in js)
        check("peer checkboxes are rendered", "renderAgentPeerChecks" in js)
        check(
            "an agent is not offered itself as a peer",
            "a.name !== currentName" in js,
        )
        check("model options are rendered", "renderAgentModelOptions" in js)

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

        # Pre-create one skill so the /skills/{id} endpoints return 200
        r = await client.post(
            "/admin/api/skills",
            json={
                "name": "uiskill",
                "description": "UI-flow test skill.",
                "content": "Follow the UI test procedure.",
            },
        )
        assert r.status_code == 201, r.text
        uiskill_id = r.json()["id"]

        # We need the fixture agent to survive until the DELETE call, so
        # sort with DELETE last.
        def _order(item: tuple[str, str]) -> tuple[int, str, str]:
            method, path = item
            return (1 if method == "DELETE" else 0, method, path)

        for method, path in sorted(discovered, key=_order):
            test_path = path.replace("/agents/1", f"/agents/{uitest_id}")
            test_path = test_path.replace("/skills/1", f"/skills/{uiskill_id}")
            body = None
            if method == "POST" and test_path.endswith("/skills"):
                body = {
                    "name": "flow-skill",
                    "description": "Created via UI flow test.",
                    "content": "Flow skill content.",
                }
            elif method == "PUT" and "/skills/" in test_path:
                body = {
                    "name": "uiskill",
                    "description": "Edited via UI flow test.",
                    "content": "Edited flow skill content.",
                }
            elif method == "PUT" and test_path.endswith("/model"):
                mr = await client.get("/admin/api/model")
                body = {"model_name": mr.json()["available"][0]}
            elif method == "POST" and test_path.endswith("/agents"):
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

        # 4i. Skill flows: create -> attach to agent -> reload -> delete detaches
        r = await client.post("/admin/api/skills", json={
            "name": "ui-skill-flow",
            "description": "Skill created by the UI flow test.",
            "content": "Follow the UI skill flow procedure.",
        })
        check("flow: skill created", r.status_code == 201, f"got {r.status_code}: {r.text}")
        flow_skill_id = r.json()["id"]

        r = await client.post("/admin/api/agents", json={
            "name": "ui_skill_agent",
            "description": "Agent with a skill.",
            "instructions": "You have a skill.",
            "mcp_url": "https://uiskill.cfapps.eu20-001.hana.ondemand.com",
            "skills": ["ui-skill-flow"],
            "enabled": True,
        })
        check("flow: agent with skill created", r.status_code == 201, f"got {r.status_code}: {r.text}")
        skill_agent_id = r.json()["id"]
        check("flow: agent lists skill", r.json()["skills"] == ["ui-skill-flow"])

        r = await client.post("/admin/api/reload")
        check("flow: reload with skill attached", r.status_code == 200)

        r = await client.delete(f"/admin/api/skills/{flow_skill_id}")
        check("flow: skill delete 204", r.status_code == 204)
        r = await client.get(f"/admin/api/agents/{skill_agent_id}")
        check("flow: skill detached from agent", r.json()["skills"] == [])
        r = await client.delete(f"/admin/api/agents/{skill_agent_id}")
        check("flow: skill agent cleanup", r.status_code == 204)

        # 4j. Validation — UI must surface backend validation as toast errors.
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

        # ------------------------------------------------------------------
        print("\n== job runs UI ==")
        check("runs tab present", 'data-tab="runs"' in html)
        check("runs list container", 'id="runs-list"' in html)
        check("run-now button", "runAgentNow" in html)

        # run-as is an opaque XSUAA principal, so the UI must hand it over and
        # show whether that identity actually holds a token per OAuth2 server.
        check("whoami loaded on boot", "loadWhoami()" in html)
        check("use-my-identity button", 'id="agent-use-my-identity"' in html)
        check("whoami api used", "/admin/api/whoami" in html)
        check("credentials api used", "/credentials?principal=" in html)
        check("credentials panel", 'id="agent-credentials"' in html)
        check("connect button opens login", "function connectServer" in html)
        # Only ever open our own same-origin sign-in URL in a popup.
        check("connect guards the login url", "/^\\/oauth\\/login\\?/" in html)
        # The popup authorizes whoever is logged in — say so, or an admin
        # connects their own identity while believing they connected the
        # service account, which is the exact confusion this panel exists for.
        check("connect warns whose identity is used", "not as the id above" in html)
        check("runs api used", "/admin/api/runs" in html)
        check("exposure field expose_api", 'id="agent-expose-api"' in html)
        check("exposure field api_slug", 'id="agent-api-slug"' in html)
        check("run-as field", 'id="agent-run-as"' in html)
        check("expected-sections input removed", 'id="agent-expected-sections"' not in html)
        check("endpoint URL hint shown", "/api/agents/" in html)

        check("template loads marked", "/static/vendor/marked.min.js" in html)
        check("template loads purify", "/static/vendor/purify.min.js" in html)
        check("mermaid is NOT eagerly loaded",
              '<script src="/static/vendor/mermaid.min.js"' not in html)

        # An API-triggered run binds a technical principal and deliberately
        # never binds a user JWT, so an agent that is expose_api AND binds an
        # auth_mode="jwt" MCP server can only ever fail. The operator has no
        # way to know that from the form, so the hint must say it.
        hint = re.search(r"function updateEndpointHint\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("updateEndpointHint() found in JS", hint is not None)
        hint_body = hint.group(1) if hint else ""
        check(
            "hint inspects the auth modes",
            "mcp-auth-mode" in hint_body and "'jwt'" in hint_body,
            hint_body,
        )
        check(
            "hint gates the warning on expose_api",
            "agent-expose-api" in hint_body,
            hint_body,
        )
        check("hint warns about JWT forward", "Warning" in hint_body, hint_body)
        check(
            "auth mode change refreshes the hint",
            "toggleOauthFields(this); updateEndpointHint()" in html,
        )

        # ------------------------------------------------------------------
        # saveAgent() must actually SEND all six exposure fields, not just
        # have form elements for them. AgentPayload defaults + whole-object
        # PUT semantics mean a field saveAgent() forgets to include gets
        # silently reset on every save (expose_api -> False, expose_chat ->
        # True, etc.) -- deleting one key here is exactly the regression
        # this check exists to catch, and the earlier ID-presence checks
        # above would not catch it.
        print("\n== saveAgent() sends all six exposure fields ==")
        m = re.search(r"async function saveAgent\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("saveAgent() function found in JS", m is not None)
        save_agent_body = m.group(1) if m else ""
        for field in (
            "expose_chat",
            "expose_api",
            "api_slug",
            "run_as_principal",
            "run_prompt",
            "run_timeout_seconds",
        ):
            check(
                f"saveAgent() sends {field}",
                re.search(rf"\b{field}\s*:", save_agent_body) is not None,
                f"{field}: not found as an object key in saveAgent()",
            )

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
