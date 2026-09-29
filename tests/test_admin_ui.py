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
        check("workflows tbody present",
              find(coll, "tbody", id="workflows-tbody") is not None)
        check("workflow runs tbody present",
              find(coll, "tbody", id="workflow-runs-tbody") is not None)
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

        # --- workflow step cards + large text editor ---
        # The step instructions used to be a two-row textarea squeezed into
        # one flex line with six other controls. Each step is now a card with
        # a full-width instructions field, and every long-text field has an
        # Expand button that opens it in a large modal editor.
        check("text editor modal present",
              find(coll, "div", id="text-editor-modal") is not None)
        check("text editor modal reuses the page's modal styling",
              (find(coll, "div", id="text-editor-modal") or ("", {}, ""))[1]
              .get("class") == "modal-backdrop")
        check("text editor textarea present",
              find(coll, "textarea", id="text-editor-textarea") is not None)
        check("text editor title present",
              find(coll, "h3", id="text-editor-title") is not None)
        check("text editor Done button applies",
              (find(coll, "button", id="text-editor-done") or ("", {}, ""))[1]
              .get("onclick") == "closeTextEditor(true)")
        check("text editor Cancel button discards",
              (find(coll, "button", id="text-editor-cancel") or ("", {}, ""))[1]
              .get("onclick") == "closeTextEditor(false)")
        check("clicking the backdrop cancels the text editor",
              "closeTextEditor(false)" in
              (find(coll, "div", id="text-editor-modal") or ("", {}, ""))[1]
              .get("onclick", ""))
        check("text editor counter present",
              find(coll, "span", id="text-editor-counter") is not None)
        css = html[html.find("<style>"): html.find("</style>")]
        check("step card style defined", ".wf-step {" in css)
        check("step instructions get a readable min-height and line-height",
              re.search(r"textarea\.wf-step-instructions\s*\{[^}]*min-height:\s*10rem", css)
              is not None
              and re.search(r"textarea\.wf-step-instructions\s*\{[^}]*line-height:\s*1\.5", css)
              is not None
              and re.search(r"textarea\.wf-step-instructions\s*\{[^}]*font-size:\s*14px", css)
              is not None)
        check("text editor textarea fills the modal",
              re.search(r"#text-editor-textarea\s*\{[^}]*height:\s*calc\(94vh", css) is not None)
        check("text editor stacks above the workflow modal",
              re.search(r"#text-editor-modal\s*\{[^}]*z-index:\s*20", css) is not None)
        check("per-kind config editors use a two-column grid",
              re.search(r"\.wf-cfg-grid\s*\{[^}]*grid-template-columns:\s*1fr 1fr", css)
              is not None)

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
        check("workflow steps are collected on save", "collectWorkflowSteps()" in js)
        check("workflow branches are collected on save",
              "collectWorkflowBranches()" in js)
        check("run-now posts to the workflow run endpoint",
              "/workflows/${id}/run" in js or "/workflows/' + id + '/run" in js)

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

            # --------------------------------------------------------------
            # renderAgentModelOptions() behavioral check: an unrepresented
            # stored override (fetch failed, or the model was undeployed
            # since the config was saved) must stay selected rather than
            # silently reverting to "" ("use the active model"), since a
            # save at that point would delete the override. This actually
            # *executes* the shipped function under real <select>.value
            # semantics, rather than just asserting the source text exists.
            auto_invoke = re.search(
                r"^loadSkills\(\);\nloadAgents\(\);\nloadWhoami\(\);"
                r"\nloadOrchestrator\(\);\nloadModel\(\);\s*$",
                js,
                re.MULTILINE,
            )
            check("found the trailing auto-invoke block to strip", auto_invoke is not None)
            js_no_autoinvoke = js[: auto_invoke.start()] if auto_invoke else js

            harness = r"""
'use strict';
const assert = require('node:assert');

// A real <select>: .value only "sticks" if a matching <option value="...">
// is present in .innerHTML, otherwise the browser resets it to "" -- that
// reset-to-blank is exactly the failure mode under test, so it must be
// modeled faithfully rather than stubbed as a plain property.
function makeSelectStub() {
    let html = '';
    let val = '';
    const optionValues = () => [...html.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);
    return {
        get innerHTML() { return html; },
        set innerHTML(h) {
            html = h;
            if (!optionValues().includes(val)) val = '';
        },
        get value() { return val; },
        set value(v) { val = optionValues().includes(v) ? v : ''; },
    };
}

const modelSelect = makeSelectStub();
const toastEl = { textContent: '', className: '' };
global.document = {
    getElementById(id) {
        if (id === 'agent-model-name') return modelSelect;
        if (id === 'toast') return toastEl;
        throw new Error('unstubbed getElementById: ' + id);
    },
};

""" + js_no_autoinvoke + r"""

async function main() {
    // Scenario 1: GET /admin/api/model fails outright.
    global.fetch = async () => ({ ok: false, json: async () => ({}) });
    await renderAgentModelOptions('gpt-4o-mini');
    assert.strictEqual(modelSelect.value, 'gpt-4o-mini',
        'scenario 1: override must survive a failed model fetch');
    assert.ok(modelSelect.innerHTML.includes('not currently available'),
        'scenario 1: the unavailable state must be shown, not hidden');
    assert.ok(toastEl.className.includes('error'),
        'scenario 1: a failed fetch must surface via toast');

    // Scenario 2: fetch succeeds, but the stored model isn't in the list
    // (e.g. its deployment was removed after the agent was configured).
    toastEl.className = '';
    global.fetch = async () => ({
        ok: true,
        json: async () => ({ available: ['other-model'], model_name: null, default: null }),
    });
    await renderAgentModelOptions('gpt-4o-mini');
    assert.strictEqual(modelSelect.value, 'gpt-4o-mini',
        'scenario 2: override must survive an undeployed model');
    assert.ok(modelSelect.innerHTML.includes('not currently available'),
        'scenario 2: the unavailable state must be shown, not hidden');
    assert.strictEqual(toastEl.className, '',
        'scenario 2: a successful fetch must not toast an error');

    // Scenario 3 (regression guard): a model that IS available selects
    // normally and gets no synthetic "(not currently available)" option.
    global.fetch = async () => ({
        ok: true,
        json: async () => ({ available: ['gpt-4o-mini', 'other-model'], model_name: null, default: null }),
    });
    await renderAgentModelOptions('gpt-4o-mini');
    assert.strictEqual(modelSelect.value, 'gpt-4o-mini');
    assert.ok(!modelSelect.innerHTML.includes('not currently available'),
        'scenario 3: a deployed model must not be flagged unavailable');

    // Scenario 4 (regression guard): blank override stays blank and means
    // "use the active model" -- no sentinel value is introduced.
    global.fetch = async () => ({
        ok: true,
        json: async () => ({ available: ['gpt-4o-mini'], model_name: null, default: null }),
    });
    await renderAgentModelOptions('');
    assert.strictEqual(modelSelect.value, '');

    console.log('all renderAgentModelOptions scenarios passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""

            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(harness)
                harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", harness_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                check(
                    "renderAgentModelOptions preserves an unrepresented "
                    "override instead of silently blanking it",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:500],
                )
            finally:
                os.unlink(harness_path)

            # --------------------------------------------------------------
            # showWorkflowRun() must actually render what a step produced.
            # WorkflowStepRun carries `output`/`error`, and the API returns
            # them, but nothing forces the renderer to read them -- this
            # proves the rendered HTML contains the (escaped) step output,
            # not just that the function exists.
            wf_run_harness = r"""
'use strict';
const assert = require('node:assert');

const detailEl = { innerHTML: '' };
global.document = {
    getElementById(id) {
        if (id === 'workflow-run-detail') return detailEl;
        throw new Error('unstubbed getElementById: ' + id);
    },
};

""" + js_no_autoinvoke + r"""

async function main() {
    global.fetch = async () => ({
        ok: true,
        json: async () => ({
            run: {
                id: 'run-1', workflow_name: 'mail-triage', status: 'success',
                trigger: 'manual', started_at: null, finished_at: null,
                summary: null, error: null,
            },
            items: [],
            steps: [
                {
                    id: 'step-1', item_run_id: null, branch_key: null,
                    position: 1, agent_name: 'reader', status: 'success',
                    output: 'found 3 <urgent> mails', error: null,
                },
            ],
        }),
    });
    await showWorkflowRun('run-1');
    assert.ok(
        detailEl.innerHTML.includes('found 3 &lt;urgent&gt; mails'),
        'a step\'s output must reach the rendered run detail, HTML-escaped',
    );
    assert.ok(
        !detailEl.innerHTML.includes('found 3 <urgent> mails'),
        'step output must be escaped, not injected raw',
    );

    console.log('showWorkflowRun output-rendering scenario passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""

            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(wf_run_harness)
                wf_run_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", wf_run_harness_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                check(
                    "showWorkflowRun() renders a step's output, escaped",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:500],
                )
            finally:
                os.unlink(wf_run_harness_path)

            # --------------------------------------------------------------
            # The workflow editor round trip. Opening a saved workflow for
            # edit and saving it unchanged must produce the same definition:
            # branches, and every step's agent, branch and position.
            #
            # The regression this pins down: addWorkflowStepRow marked an
            # <option> selected only on an exact name match, so a step whose
            # agent no longer exists matched nothing and the browser selected
            # the FIRST option. collectWorkflowSteps submitted that
            # substitute, validation passed (a real, enabled agent) and the
            # save succeeded with a success toast -- leaving the step running
            # a different agent, unattended, under a different principal.
            #
            # Run under jsdom, not a hand-rolled stub: <select> selectedness
            # with a selected-but-disabled option is exactly the browser
            # behaviour the fix relies on, so it must not be modelled by hand.
            editor_harness = r"""
'use strict';
const assert = require('node:assert');
const { JSDOM } = require('jsdom');

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="toast"></div>
  <div id="workflow-modal"></div>
  <span id="workflow-modal-title"></span>
  <input id="workflow-id"><input id="workflow-name">
  <input id="workflow-description"><input id="workflow-api-slug">
  <input id="workflow-run-as"><input id="workflow-timeout">
  <input id="workflow-max-parallel">
  <select id="workflow-on-unknown-branch">
    <option value="fail">fail</option><option value="skip">skip</option>
  </select>
  <input type="checkbox" id="workflow-skip-seen">
  <input type="checkbox" id="workflow-enabled">
  <div id="workflow-branches"></div>
  <div id="workflow-steps"></div>
</body></html>`);
global.window = dom.window;
global.document = dom.window.document;

""" + js_no_autoinvoke + r"""

const DEF = {
    id: 7, name: 'mail-triage', description: 'triage', api_slug: 'mt',
    run_as_principal: 'svc@example.com', run_timeout_seconds: 1200,
    skip_seen_items: false, max_parallel_items: 2,
    on_unknown_branch: 'skip', enabled: true,
    branches: [
        {key: 'abap', description: 'ABAP dumps', position: 1},
        {key: 'fiori', description: 'UI issues', position: 2},
    ],
    steps: [
        {branch_key: null, position: 1, agent_name: 'reader',
         instructions: 'triage', fan_out: true, step_timeout_seconds: 300},
        {branch_key: null, position: 2, agent_name: 'drafter',
         instructions: 'draft', fan_out: false, step_timeout_seconds: 400},
        {branch_key: 'abap', position: 1, agent_name: 'abap',
         instructions: 'read the dump', fan_out: false, step_timeout_seconds: 500},
        {branch_key: 'abap', position: 2, agent_name: 'drafter',
         instructions: 'summarize', fan_out: false, step_timeout_seconds: 600},
        {branch_key: 'fiori', position: 1, agent_name: 'reader',
         instructions: 'check the UI', fan_out: false, step_timeout_seconds: 700},
    ],
};

let saved = null;
global.fetch = async (url, opts = {}) => {
    if ((opts.method || 'GET') === 'GET') {
        return { ok: true, json: async () => DEF };
    }
    saved = JSON.parse(opts.body);
    return { ok: true, json: async () => ({}) };
};
// The save's trailing refresh would re-fetch the list; nothing here needs it.
loadWorkflows = async () => {};

const shape = steps => steps.map(s => [
    s.branch_key, s.position, s.agent_name, s.instructions,
    s.fan_out, s.step_timeout_seconds,
]);

async function main() {
    // 1. Every agent still exists: an unchanged save must be a no-op.
    allAgents = [{name: 'reader'}, {name: 'abap'}, {name: 'drafter'}];
    saved = null;
    await editWorkflow(7);
    await saveWorkflow();
    assert.ok(saved, 'an unchanged save must post a body');
    assert.deepStrictEqual(
        saved.branches.map(b => [b.key, b.description, b.position]),
        DEF.branches.map(b => [b.key, b.description, b.position]),
        'branches must survive the edit round trip',
    );
    assert.deepStrictEqual(
        shape(saved.steps), shape(DEF.steps),
        'every step must survive with its agent, branch and position',
    );

    // 2. 'reader' and 'abap' have been deleted since the workflow was saved.
    //    Their steps must submit the missing names, so the server rejects the
    //    save -- never a silent substitution.
    allAgents = [{name: 'drafter'}, {name: 'zzz-other'}];
    saved = null;
    await editWorkflow(7);
    await saveWorkflow();
    assert.deepStrictEqual(
        saved.steps.map(s => s.agent_name),
        DEF.steps.map(s => s.agent_name),
        'a step whose agent no longer exists must keep naming it',
    );
    assert.ok(
        !saved.steps.some(s => s.agent_name === 'zzz-other'),
        'no step may be retargeted at whatever happens to be listed first',
    );

    console.log('workflow editor round-trip scenarios passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""

            # dir=ROOT so `require('jsdom')` resolves against the repo's
            # node_modules; a system temp dir has none.
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, dir=str(ROOT)
            ) as f:
                f.write(editor_harness)
                editor_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", editor_harness_path],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                check(
                    "the workflow editor round-trips steps, and never "
                    "silently retargets a deleted agent",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:600],
                )
            finally:
                os.unlink(editor_harness_path)

            # --- step kinds ---
            # Non-agent steps (condition/transform/http/python) carry a kind
            # and a config instead of an agent. The editor must round-trip
            # each kind's config, build a config for a row whose kind was
            # switched in the UI, and refuse to submit a malformed headers
            # JSON rather than silently sending nothing.
            check("step kind selector is rendered per row", "wf-step-kind" in js)
            check("step kinds mirror the server's set",
                  "['agent', 'condition', 'transform', 'http', 'python']" in js)
            kinds_harness = r"""
'use strict';
const assert = require('node:assert');
const { JSDOM } = require('jsdom');

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="toast"></div>
  <div id="workflow-modal"></div>
  <span id="workflow-modal-title"></span>
  <input id="workflow-id"><input id="workflow-name">
  <input id="workflow-description"><input id="workflow-api-slug">
  <input id="workflow-run-as"><input id="workflow-timeout">
  <input id="workflow-max-parallel">
  <select id="workflow-on-unknown-branch">
    <option value="fail">fail</option><option value="skip">skip</option>
  </select>
  <input type="checkbox" id="workflow-skip-seen">
  <input type="checkbox" id="workflow-enabled">
  <div id="workflow-branches"></div>
  <div id="workflow-steps"></div>
</body></html>`);
global.window = dom.window;
global.document = dom.window.document;

""" + js_no_autoinvoke + r"""

const COND = {rules: [{when: {source: 'json', field: 'issue.priority', op: 'equals',
                              value: 'High', case_sensitive: false},
                       then: {action: 'stop', output: 'Escalated {{item.id}}'}}],
              else: {action: 'continue', output: ''}};
const TF = {extract_json: 'issue.summary', regex: {pattern: 'a', replace: 'b', flags: 'i'},
            template: 'S: {{text}}', truncate: 200};
const HTTP = {destination: 'jira', method: 'POST', path: '/rest/api/2/issue/{{item.id}}/comment',
              query: {notify: 'false'}, headers: {'X-Trace': '1'}, body: '{"body": "{{text}}"}',
              content_type: 'application/json', timeout_seconds: 20, expect_status: [200, 201]};
const PY = {code: "output = text.upper()", timeout_seconds: 7};
const DEF = {
    id: 9, name: 'kinds', description: '', api_slug: '', run_as_principal: '',
    run_timeout_seconds: 1200, skip_seen_items: true, max_parallel_items: 1,
    on_unknown_branch: 'fail', enabled: true, branches: [],
    steps: [
        {branch_key: null, position: 1, kind: 'agent', agent_name: 'reader',
         instructions: 'read', fan_out: false, step_timeout_seconds: 300, config: {}},
        {branch_key: null, position: 2, kind: 'condition', agent_name: '',
         instructions: '', fan_out: false, step_timeout_seconds: 60, config: COND},
        {branch_key: null, position: 3, kind: 'transform', agent_name: '',
         instructions: '', fan_out: false, step_timeout_seconds: 60, config: TF},
        {branch_key: null, position: 4, kind: 'http', agent_name: '',
         instructions: '', fan_out: false, step_timeout_seconds: 60, config: HTTP},
        {branch_key: null, position: 5, kind: 'python', agent_name: '',
         instructions: '', fan_out: false, step_timeout_seconds: 60, config: PY},
    ],
};

let saved = null;
global.fetch = async (url, opts = {}) => {
    if ((opts.method || 'GET') === 'GET') {
        return { ok: true, json: async () => DEF };
    }
    saved = JSON.parse(opts.body);
    return { ok: true, json: async () => ({}) };
};
loadWorkflows = async () => {};

async function main() {
    allAgents = [{name: 'reader'}];

    // 1. Every kind's config survives open -> save unchanged.
    saved = null;
    await editWorkflow(9);
    await saveWorkflow();
    assert.ok(saved, 'save must post a body');
    assert.deepStrictEqual(saved.steps.map(s => s.kind),
        ['agent', 'condition', 'transform', 'http', 'python'], 'kinds round-trip');
    assert.deepStrictEqual(saved.steps[0].config, {}, 'an agent step sends an empty config');
    assert.strictEqual(saved.steps[0].agent_name, 'reader');
    assert.strictEqual(saved.steps[1].agent_name, '', 'a non-agent step sends no agent');
    assert.deepStrictEqual(saved.steps[1].config, COND, 'condition config round-trips');
    assert.deepStrictEqual(saved.steps[2].config, TF, 'transform config round-trips');
    assert.deepStrictEqual(saved.steps[3].config, HTTP, 'http config round-trips');
    assert.deepStrictEqual(saved.steps[4].config, PY, 'python config round-trips');
    assert.ok(saved.steps.every(s => s.position === saved.steps.indexOf(s) + 1),
        'positions are contiguous across kinds');

    // 2. The agent controls are hidden for a non-agent row, and shown for an agent row.
    const rows = Array.from(document.querySelectorAll('#workflow-steps .wf-step-row'));
    assert.strictEqual(rows[1].querySelector('.wf-step-agent').style.display, 'none',
        'the agent dropdown is hidden for a condition step');
    assert.strictEqual(rows[1].querySelector('.wf-step-config').style.display, '',
        'the config editor is shown for a condition step');
    assert.strictEqual(rows[0].querySelector('.wf-step-config').style.display, 'none',
        'the config editor is hidden for an agent step');

    // 3. Switching a fresh row to `python` builds its editor and collects its config.
    addWorkflowStepRow();
    const fresh = document.querySelectorAll('#workflow-steps .wf-step-row')[5];
    const kindSelect = fresh.querySelector('.wf-step-kind');
    kindSelect.value = 'python';
    onWorkflowStepKindChange(kindSelect);
    assert.ok(fresh.querySelector('.wf-py-code'), 'the python editor appears on kind change');
    assert.strictEqual(fresh.querySelector('.wf-step-agent').style.display, 'none');
    fresh.querySelector('.wf-py-code').value = 'output = len(text)';
    fresh.querySelector('.wf-py-timeout').value = '3';
    saved = null;
    await saveWorkflow();
    assert.deepStrictEqual(saved.steps[5],
        {branch_key: null, position: 6, kind: 'python', agent_name: '', instructions: '',
         fan_out: false, step_timeout_seconds: 600,
         config: {code: 'output = len(text)', timeout_seconds: 3}},
        'a row switched to python submits its code and timeout');

    // 4. Switching back to agent hides the editor and sends an agent step.
    kindSelect.value = 'agent';
    onWorkflowStepKindChange(kindSelect);
    assert.strictEqual(fresh.querySelector('.wf-step-config').style.display, 'none');
    saved = null;
    await saveWorkflow();
    assert.strictEqual(saved.steps[5].kind, 'agent');
    assert.deepStrictEqual(saved.steps[5].config, {});

    // 5. Malformed headers JSON on an http step is refused before the request.
    rows[3].querySelector('.wf-http-headers').value = '{not json';
    saved = null;
    let threw = false;
    try { await saveWorkflow(); } catch (e) { threw = true; }
    assert.ok(threw, 'a malformed headers JSON aborts the save');
    assert.strictEqual(saved, null, 'nothing was posted');
    assert.ok(document.getElementById('toast').textContent.includes('Step 4'),
        'the toast names the step');

    console.log('workflow step kinds editor scenarios passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, dir=str(ROOT)
            ) as f:
                f.write(kinds_harness)
                kinds_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", kinds_harness_path],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                check(
                    "the workflow editor round-trips every step kind's config, "
                    "builds one on kind change, and refuses malformed JSON",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:800],
                )
            finally:
                os.unlink(kinds_harness_path)

            # --- workflow step cards + text editor ---
            # Source-level contract first: the pieces the harness below
            # exercises must be the shipped ones, not lookalikes.
            step_row_src = re.search(
                r"function addWorkflowStepRow\(step\)\s*\{(.*?)\n\}", js, re.DOTALL)
            step_row_body = step_row_src.group(1) if step_row_src else ""
            check("step rows are cards", "'wf-step-row wf-step'" in step_row_body)
            check("step card has a header line and a body",
                  'class="wf-step-head"' in step_row_body
                  and 'class="wf-step-body"' in step_row_body)
            check("instructions are a full-width 8-row field",
                  re.search(r"textFieldHtml\('Instructions',\s*'wf-step-instructions'[^)]*rows:\s*8",
                            step_row_body, re.DOTALL) is not None)
            check("python code is a 12-row field with Expand",
                  re.search(r"textFieldHtml\('Code',\s*'wf-py-code'[^)]*rows:\s*12", js, re.DOTALL)
                  is not None)
            check("transform template is a 4-row field with Expand",
                  re.search(r"textFieldHtml\('Template',\s*'wf-tf-template'[^)]*rows:\s*4", js,
                            re.DOTALL) is not None)
            check("http body is a 4-row field with Expand",
                  re.search(r"textFieldHtml\('Body template',\s*'wf-http-body'[^)]*rows:\s*4", js,
                            re.DOTALL) is not None)
            check("autoGrow helper defined", "function autoGrow(textarea)" in js)
            check("openTextEditor is generic over its source textarea",
                  "function openTextEditor(sourceTextarea, title)" in js)
            check("Expand button opens the text editor",
                  'class="secondary small wf-expand-btn"' in js
                  and 'onclick="openTextEditorFor(this)"' in js)
            check("Esc cancels the text editor",
                  "event.key === 'Escape'" in js and "closeTextEditor(false)" in js)
            check("text editor wires and unwires its key handler",
                  "document.addEventListener('keydown', onTextEditorKeydown)" in js
                  and "document.removeEventListener('keydown', onTextEditorKeydown)" in js)
            check("editWorkflow re-measures textareas once the modal is visible",
                  re.search(r"function editWorkflow\(id\).*?classList\.add\('open'\);\s*(//[^\n]*\n\s*)*autoGrowAll\(",
                            js, re.DOTALL) is not None)
            check("step rows can be reordered",
                  "moveWorkflowStep(this, -1)" in step_row_body
                  and "moveWorkflowStep(this, 1)" in step_row_body)

            # Behavioural check under jsdom: the counter and autoGrow follow
            # the textarea, the Expand -> Done round trip writes back to the
            # source textarea and re-runs its listeners, Cancel and Esc do
            # not, and the reorder buttons change what collectWorkflowSteps
            # submits. jsdom does no layout, so scrollHeight is stubbed on
            # the element under test.
            cards_harness = r"""
'use strict';
const assert = require('node:assert');
const { JSDOM } = require('jsdom');

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="toast"></div>
  <div id="workflow-modal"></div>
  <span id="workflow-modal-title"></span>
  <input id="workflow-id"><input id="workflow-name">
  <input id="workflow-description"><input id="workflow-api-slug">
  <input id="workflow-run-as"><input id="workflow-timeout">
  <input id="workflow-max-parallel">
  <select id="workflow-on-unknown-branch">
    <option value="fail">fail</option><option value="skip">skip</option>
  </select>
  <input type="checkbox" id="workflow-skip-seen">
  <input type="checkbox" id="workflow-enabled">
  <div id="workflow-branches"></div>
  <div id="workflow-steps"></div>
  <div id="text-editor-modal" class="modal-backdrop">
    <h3 id="text-editor-title"></h3>
    <textarea id="text-editor-textarea"></textarea>
    <span id="text-editor-counter"></span>
  </div>
</body></html>`);
global.window = dom.window;
global.document = dom.window.document;

""" + js_no_autoinvoke + r"""

const DEF = {
    id: 3, name: 'cards', description: '', api_slug: '', run_as_principal: '',
    run_timeout_seconds: 1200, skip_seen_items: true, max_parallel_items: 1,
    on_unknown_branch: 'fail', enabled: true, branches: [],
    steps: [
        {branch_key: null, position: 1, kind: 'agent', agent_name: 'reader',
         instructions: 'line one\nline two\nline three', fan_out: true,
         step_timeout_seconds: 300, config: {}},
        {branch_key: null, position: 2, kind: 'agent', agent_name: 'drafter',
         instructions: 'draft it', fan_out: false, step_timeout_seconds: 400, config: {}},
        {branch_key: null, position: 3, kind: 'python', agent_name: '',
         instructions: '', fan_out: false, step_timeout_seconds: 60,
         config: {code: 'output = text', timeout_seconds: 5}},
    ],
};
let saved = null;
global.fetch = async (url, opts = {}) => {
    if ((opts.method || 'GET') === 'GET') return { ok: true, json: async () => DEF };
    saved = JSON.parse(opts.body);
    return { ok: true, json: async () => ({}) };
};
loadWorkflows = async () => {};

const modal = document.getElementById('text-editor-modal');
const modalTa = document.getElementById('text-editor-textarea');
const stubHeight = (el, h) => Object.defineProperty(el, 'scrollHeight', {configurable: true, get: () => h});
const fire = (el, type) => el.dispatchEvent(new window.Event(type, {bubbles: true}));
const key = (k, extra = {}) => document.dispatchEvent(
    new window.KeyboardEvent('keydown', Object.assign({key: k, bubbles: true}, extra)));
// jsdom runs no inline onclick= handlers, so the buttons are checked for
// the right wiring and their handlers are then invoked directly.
const expand = scope => {
    const btn = scope.querySelector('.wf-expand-btn');
    assert.strictEqual(btn.getAttribute('onclick'), 'openTextEditorFor(this)');
    openTextEditorFor(btn);
};
const press = (row, cls, fn) => {
    const btn = row.querySelector('.' + cls);
    assert.ok(btn.getAttribute('onclick').startsWith(fn + '('), cls + ' calls ' + fn);
    return btn;
};

async function main() {
    allAgents = [{id: 1, name: 'reader'}, {id: 2, name: 'drafter'}];
    await editWorkflow(3);
    const rows = () => Array.from(document.querySelectorAll('#workflow-steps .wf-step-row'));
    assert.strictEqual(rows().length, 3);

    // 1. Card layout: header + body, instructions in the body, counter filled.
    const card = rows()[0];
    assert.ok(card.classList.contains('wf-step'), 'row is a card');
    const head = card.querySelector('.wf-step-head');
    assert.ok(head.querySelector('.wf-step-agent'), 'agent select is in the header');
    assert.ok(head.querySelector('.wf-step-open-agent'), 'open-agent link is in the header');
    assert.ok(head.querySelector('.wf-step-timeout'), 'timeout is in the header');
    assert.strictEqual(head.querySelector('.wf-step-num').textContent, 'Step 1');
    assert.strictEqual(rows()[2].querySelector('.wf-step-num').textContent, 'Step 3');
    const ta = card.querySelector('.wf-step-body .wf-field textarea.wf-step-instructions');
    assert.ok(ta, 'instructions textarea lives in the card body as a text field');
    assert.strictEqual(ta.getAttribute('rows'), '8');
    assert.strictEqual(ta.value, 'line one\nline two\nline three');
    const counter = card.querySelector('.wf-field .wf-text-counter');
    assert.strictEqual(counter.textContent, '3 lines · 28 chars', 'counter reflects the loaded text');
    assert.ok(card.querySelector('.wf-field .wf-expand-btn'), 'Expand button next to the instructions');

    // 2. autoGrow follows the content on input, capped at 60vh (jsdom: 768px).
    stubHeight(ta, 300);
    ta.value = 'x';
    fire(ta, 'input');
    assert.strictEqual(ta.style.height, '302px', 'grows to scrollHeight');
    assert.strictEqual(counter.textContent, '1 line · 1 char', 'counter follows input');
    stubHeight(ta, 5000);
    fire(ta, 'input');
    assert.strictEqual(ta.style.height, Math.floor(768 * 0.6) + 'px', 'capped near 60vh');
    assert.strictEqual(ta.style.overflowY, 'auto', 'scrolls once capped');
    stubHeight(ta, 0);
    ta.value = 'kept';
    fire(ta, 'input');
    assert.strictEqual(ta.style.height, 'auto', 'no scrollHeight (hidden): left to CSS');

    // 3. Expand -> edit -> Done writes back and re-runs the source listeners.
    expand(card.querySelector('.wf-field'));
    assert.ok(modal.classList.contains('open'), 'text editor opens');
    assert.strictEqual(modalTa.value, 'kept', 'editor starts from the source text');
    assert.ok(document.getElementById('text-editor-title').textContent.includes('Instructions'));
    assert.ok(document.getElementById('text-editor-title').textContent.includes('Step 1'));
    modalTa.value = 'new\ntext';
    fire(modalTa, 'input');
    assert.strictEqual(document.getElementById('text-editor-counter').textContent, '2 lines · 8 chars');
    closeTextEditor(true);
    assert.ok(!modal.classList.contains('open'), 'Done closes the editor');
    assert.strictEqual(ta.value, 'new\ntext', 'Done writes back to the source textarea');
    assert.strictEqual(counter.textContent, '2 lines · 8 chars', 'source counter re-run');

    // 4. Expand -> edit -> Esc leaves the source untouched.
    expand(card.querySelector('.wf-field'));
    modalTa.value = 'discarded';
    key('Escape');
    assert.ok(!modal.classList.contains('open'), 'Esc closes the editor');
    assert.strictEqual(ta.value, 'new\ntext', 'Esc does not write back');
    // The key handler is gone once closed: Esc again must be a no-op.
    ta.value = 'after';
    key('Escape');
    assert.strictEqual(ta.value, 'after', 'no stale key handler');

    // 5. Expand -> edit -> Cancel leaves the source untouched too.
    expand(card.querySelector('.wf-field'));
    modalTa.value = 'also discarded';
    closeTextEditor(false);
    assert.strictEqual(ta.value, 'after', 'Cancel does not write back');
    // Ctrl+Enter applies.
    expand(card.querySelector('.wf-field'));
    modalTa.value = 'applied by key';
    key('Enter', {ctrlKey: true});
    assert.ok(!modal.classList.contains('open'));
    assert.strictEqual(ta.value, 'applied by key');

    // 6. Non-agent kinds: code/template/body are text fields with Expand,
    //    the agent body is hidden, and the Expand round trip reaches them.
    const py = rows()[2];
    assert.strictEqual(py.querySelector('.wf-step-agent-body').style.display, 'none');
    const code = py.querySelector('.wf-step-config .wf-field textarea.wf-py-code');
    assert.ok(code, 'python code is a text field');
    assert.strictEqual(code.getAttribute('rows'), '12');
    expand(py.querySelector('.wf-step-config'));
    assert.strictEqual(modalTa.value, 'output = text');
    modalTa.value = 'output = text.upper()';
    closeTextEditor(true);
    assert.strictEqual(code.value, 'output = text.upper()');
    const kindSelect = rows()[1].querySelector('.wf-step-kind');
    kindSelect.value = 'transform';
    onWorkflowStepKindChange(kindSelect);
    assert.strictEqual(rows()[1].querySelector('.wf-step-agent-body').style.display, 'none',
        'switching to a non-agent kind hides the instructions block');
    const tmpl = rows()[1].querySelector('.wf-field textarea.wf-tf-template');
    assert.ok(tmpl && tmpl.getAttribute('rows') === '4', 'transform template is a 4-row text field');
    assert.ok(tmpl.dataset.wired, 'a rebuilt editor is wired for autoGrow/counter');
    kindSelect.value = 'http';
    onWorkflowStepKindChange(kindSelect);
    const body = rows()[1].querySelector('.wf-field textarea.wf-http-body');
    assert.ok(body && body.getAttribute('rows') === '4', 'http body is a 4-row text field');
    assert.ok(rows()[1].querySelector('.wf-field textarea.wf-http-headers'), 'headers is a text field');
    kindSelect.value = 'agent';
    onWorkflowStepKindChange(kindSelect);
    assert.strictEqual(rows()[1].querySelector('.wf-step-agent-body').style.display, '',
        'switching back to agent shows the instructions block');

    // 7. Reordering: move step 3 up, then save; the submitted order follows.
    moveWorkflowStep(press(rows()[2], 'wf-step-up', 'moveWorkflowStep'), -1);
    assert.deepStrictEqual(rows().map(r => r.querySelector('.wf-step-num').textContent),
        ['Step 1', 'Step 2', 'Step 3'], 'renumbered after a move');
    assert.strictEqual(rows()[1].querySelector('.wf-step-kind').value, 'python');
    moveWorkflowStep(press(rows()[0], 'wf-step-up', 'moveWorkflowStep'), -1);  // no-op at the top
    assert.strictEqual(rows()[0].querySelector('.wf-step-agent').value, 'reader');
    saved = null;
    await saveWorkflow();
    assert.deepStrictEqual(saved.steps.map(s => [s.kind, s.position]),
        [['agent', 1], ['python', 2], ['agent', 3]], 'save reflects the new order');
    assert.strictEqual(saved.steps[0].instructions, 'applied by key',
        'the edited instructions are what gets saved');
    assert.strictEqual(saved.steps[1].config.code, 'output = text.upper()');
    removeWorkflowStep(press(rows()[1], 'wf-step-remove', 'removeWorkflowStep'));
    assert.deepStrictEqual(rows().map(r => r.querySelector('.wf-step-num').textContent),
        ['Step 1', 'Step 2'], 'renumbered after a remove');

    console.log('workflow step card / text editor scenarios passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, dir=str(ROOT)
            ) as f:
                f.write(cards_harness)
                cards_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", cards_harness_path],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                check(
                    "step cards: counter and autoGrow follow the textarea, "
                    "Expand/Done writes back, Cancel/Esc do not, reorder is saved",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:1200],
                )
            finally:
                os.unlink(cards_harness_path)

            # --------------------------------------------------------------
            # A name containing a quote and a parenthesis must round-trip
            # safely through the Delete buttons on all three tables. Before
            # this fix, the name was interpolated straight into an inline
            # onclick="...('...')" attribute: escapeHtml encodes `'` as
            # `&#39;`, but the browser HTML-decodes the attribute BEFORE
            # compiling it as JS, turning `&#39;` back into `'` -- so a
            # workflow/agent/skill named e.g. `x'); alert(1); //` broke out
            # of the string literal and ran arbitrary JS in anyone's admin
            # session who clicked Delete. This proves the real DOM wiring
            # (data-id/data-name read via addEventListener, not an inline
            # handler string) carries the name through intact and inert.
            xss_harness = r"""
'use strict';
const assert = require('node:assert');

// A minimal tbody stub: real enough to parse the <button ...> markup the
// shipped code writes via innerHTML and to let addEventListener/click work,
// without needing a full DOM. Entity decoding covers exactly the five
// entities escapeHtml() can produce, mirroring the browser's own
// attribute-value decoding that made the inline-onclick version exploitable.
function makeTbodyStub() {
    let buttons = [];
    function reparse(html) {
        buttons = [];
        const btnRe = /<button\b([^>]*)>/g;
        let m;
        while ((m = btnRe.exec(html))) {
            const attrs = m[1];
            const cls = (attrs.match(/class="([^"]*)"/) || [, ''])[1].split(/\s+/);
            const dataset = {};
            const dataRe = /data-([\w-]+)="([^"]*)"/g;
            let dm;
            while ((dm = dataRe.exec(attrs))) {
                const decoded = dm[2]
                    .replace(/&amp;/g, '&')
                    .replace(/&lt;/g, '<')
                    .replace(/&gt;/g, '>')
                    .replace(/&quot;/g, '"')
                    .replace(/&#39;/g, "'");
                const key = dm[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                dataset[key] = decoded;
            }
            const listeners = {};
            buttons.push({
                classes: cls,
                dataset,
                addEventListener(type, cb) { listeners[type] = cb; },
                click() { if (listeners.click) listeners.click(); },
            });
        }
    }
    let html = '';
    return {
        get innerHTML() { return html; },
        set innerHTML(h) { html = h; reparse(h); },
        querySelectorAll(selector) {
            const cls = selector.replace('.', '');
            return buttons.filter(b => b.classes.includes(cls));
        },
    };
}

const wfTbody = makeTbodyStub();
const skillTbody = makeTbodyStub();
const agentTbody = makeTbodyStub();
global.document = {
    getElementById(id) {
        if (id === 'workflows-tbody') return wfTbody;
        if (id === 'skills-tbody') return skillTbody;
        if (id === 'agents-tbody') return agentTbody;
        throw new Error('unstubbed getElementById: ' + id);
    },
};

""" + js_no_autoinvoke + r"""

async function main() {
    const evilName = `o'Brien"'); alert(1); //`;
    let capturedMsg = null;
    global.confirm = (msg) => { capturedMsg = msg; return false; };

    // Workflows
    allWorkflows = [{id: 7, name: evilName, enabled: true, api_slug: '', _stepCount: 1}];
    renderWorkflows();
    const wfBtn = wfTbody.querySelectorAll('.delete-workflow-btn')[0];
    assert.strictEqual(wfBtn.dataset.name, evilName,
        'workflow name must round-trip through data-name intact');
    wfBtn.click();
    assert.ok(capturedMsg && capturedMsg.includes(evilName),
        'deleteWorkflow must receive the real name via dataset, not a mangled one');

    // Skills
    capturedMsg = null;
    allSkills = [{id: 3, name: evilName, description: 'x'}];
    renderSkillsTable();
    const skillBtn = skillTbody.querySelectorAll('.delete-skill-btn')[0];
    assert.strictEqual(skillBtn.dataset.name, evilName);
    skillBtn.click();
    assert.ok(capturedMsg && capturedMsg.includes(evilName));

    // Agents
    capturedMsg = null;
    global.fetch = async () => ({
        ok: true,
        json: async () => ([{
            id: 9, name: evilName, description: 'x',
            mcp_servers: [{url: 'https://x.example.com', auth_mode: 'jwt'}],
            skills: [], enabled: true, expose_api: false,
        }]),
    });
    await loadAgents();
    const agentBtn = agentTbody.querySelectorAll('.delete-agent-btn')[0];
    assert.strictEqual(agentBtn.dataset.name, evilName);
    agentBtn.click();
    assert.ok(capturedMsg && capturedMsg.includes(evilName));

    console.log('delete-button name round-trip scenario passed (no inline-onclick breakout)');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""

            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(xss_harness)
                xss_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", xss_harness_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                check(
                    "delete buttons carry a quote-and-paren name safely "
                    "(no inline-onclick breakout)",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:500],
                )
            finally:
                os.unlink(xss_harness_path)

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

        # Pre-create one workflow (referencing the fixture agent above) and
        # trigger a run, so the id=1 endpoints -- including the run detail
        # view -- return 200 rather than 404.
        r = await client.post(
            "/admin/api/workflows",
            json={
                "name": "uiworkflow",
                "description": "UI-flow test workflow.",
                "api_slug": "uiworkflow",
                "steps": [
                    {"branch_key": None, "position": 1, "agent_name": "uitest",
                     "instructions": "do it", "fan_out": False,
                     "step_timeout_seconds": 600},
                ],
            },
        )
        assert r.status_code == 201, r.text
        uiworkflow_id = r.json()["id"]

        r = await client.post(f"/admin/api/workflows/{uiworkflow_id}/run")
        assert r.status_code in (200, 202), r.text
        uiworkflow_run_id = r.json()["run_id"]

        import agents.workflow_runner as _wr  # noqa: PLC0415

        await asyncio.gather(*[t for t in _wr._tasks if not t.done()],
                             return_exceptions=True)

        # We need the fixture agent to survive until the DELETE call, so
        # sort with DELETE last -- and the agent DELETE after the workflow
        # DELETE, because an agent a workflow step names cannot be deleted.
        def _order(item: tuple[str, str]) -> tuple[int, str, str]:
            method, path = item
            rank = 0
            if method == "DELETE":
                rank = 2 if "/agents/" in path else 1
            return (rank, method, path)

        for method, path in sorted(discovered, key=_order):
            test_path = path.replace("/agents/1", f"/agents/{uitest_id}")
            test_path = test_path.replace("/skills/1", f"/skills/{uiskill_id}")
            test_path = test_path.replace("/workflows/1", f"/workflows/{uiworkflow_id}")
            test_path = test_path.replace(
                "/workflow-runs/1", f"/workflow-runs/{uiworkflow_run_id}"
            )
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
            elif method == "POST" and test_path.endswith("/workflows"):
                body = {
                    "name": "flow-workflow",
                    "description": "Created via UI flow test.",
                    "api_slug": "flow-workflow",
                    "steps": [
                        {"branch_key": None, "position": 1, "agent_name": "uitest",
                         "instructions": "do it", "fan_out": False,
                         "step_timeout_seconds": 600},
                    ],
                }
            elif method == "PUT" and "/workflows/" in test_path:
                body = {
                    "name": "uiworkflow",
                    "description": "Edited via UI flow test.",
                    "api_slug": "uiworkflow",
                    "steps": [
                        {"branch_key": None, "position": 1, "agent_name": "uitest",
                         "instructions": "edited", "fan_out": False,
                         "step_timeout_seconds": 600},
                    ],
                }
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

            if method == "DELETE" and "/agents/" in test_path:
                # Deleting an agent a workflow step names is refused (409)
                # by design, so the fixture workflows go first -- the same
                # order an operator has to follow.
                wr = await client.get("/admin/api/workflows")
                for w in wr.json():
                    await client.delete(f"/admin/api/workflows/{w['id']}")

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

        # --- destinations ---------------------------------------------------
        # Every built-in can run through a BTP destination. The server row
        # must offer the mode, carry the destination sub-form, and round-trip
        # each built-in's config block the way agents/db.py stores it.
        print("\n== destination servers ==")
        check("auth mode offers a BTP destination", '<option value="destination">' in html)
        check("destination name field", 'class="dest-destination"' in html)
        check("act-as-user switch", 'class="dest-user_context"' in html)
        check("destination config collected on save", "function collectDestinationConfig" in html)
        check("destination fields follow the built-in", "function syncDestinationFields" in html)
        check("collectMcpServers handles destination mode",
              "mode === 'destination'" in html and "collectDestinationConfig(r)" in html)

        if shutil.which("node") is not None:
            dest_harness = r"""
'use strict';
const assert = require('node:assert');
const { JSDOM } = require('jsdom');

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="toast"></div>
  <div id="agent-mcp-servers"></div>
  <input id="agent-api-slug" value="">
  <input type="checkbox" id="agent-expose-api">
  <p id="agent-endpoint-hint"></p>
</body></html>`, { url: 'https://admin.example/admin' });
global.window = dom.window;
global.document = dom.window.document;
global.location = dom.window.location;

""" + js_no_autoinvoke + r"""

function addAndCollect(server) {
    document.getElementById('agent-mcp-servers').innerHTML = '';
    addMcpServerRow(server);
    const row = document.querySelector('.mcp-server-row');
    return { row, out: collectMcpServers()[0] };
}

// 1. Outlook as the signed-in user: mailbox is dropped (/me), send kept.
let { row, out } = addAndCollect({
    url: 'builtin:outlook', auth_mode: 'destination',
    oauth: { destination: 'GRAPH', user_context: true, mailbox: 'svc@example.com',
             lookback: '2d', recipients: 'a@x, b@x', allow_send: true },
});
assert.strictEqual(row.querySelector('.mcp-destination').style.display, 'block',
    'the destination sub-form is shown for the mode');
assert.strictEqual(row.querySelector('.dest-field-mailbox').style.display, 'none',
    'no mailbox to name when acting as the user');
assert.deepStrictEqual(out, {
    url: 'builtin:outlook', auth_mode: 'destination',
    oauth: { destination: 'GRAPH', lookback: '2d', recipients: 'a@x, b@x',
             user_context: true, allow_send: true },
});

// 2. Outlook app-level: the mailbox is kept and user_context is absent.
({ row, out } = addAndCollect({
    url: 'builtin:outlook', auth_mode: 'destination',
    oauth: { destination: 'GRAPH', mailbox: 'svc@example.com' },
}));
assert.strictEqual(row.querySelector('.dest-field-mailbox').style.display, '');
assert.deepStrictEqual(out.oauth, { destination: 'GRAPH', mailbox: 'svc@example.com' });

// 3. Teams without user context: allow_send is neither shown nor sent.
({ row, out } = addAndCollect({
    url: 'builtin:teams', auth_mode: 'destination',
    oauth: { destination: 'GRAPH', team: 't1', channels: 'General', allow_send: true },
}));
assert.strictEqual(row.querySelector('.dest-field-allow_send').style.display, 'none');
assert.deepStrictEqual(out.oauth, { destination: 'GRAPH', team: 't1', channels: 'General' });
row.querySelector('.dest-user_context').checked = true;
syncDestinationFields(row);
assert.strictEqual(row.querySelector('.dest-field-allow_send').style.display, '',
    'as the signed-in user Teams may post');
assert.deepStrictEqual(collectMcpServers()[0].oauth,
    { destination: 'GRAPH', team: 't1', channels: 'General', user_context: true, allow_send: true });

// 4. Jira keeps its filters and ignores the user switch it never shows.
({ row, out } = addAndCollect({
    url: 'builtin:jira', auth_mode: 'destination',
    oauth: { destination: 'JIRA', project: 'ABC', status: 'Open', api_base: '/api/2',
             allow_comment: true, user_context: true },
}));
assert.strictEqual(row.querySelector('.dest-field-user_context').style.display, 'none');
assert.deepStrictEqual(out.oauth,
    { destination: 'JIRA', project: 'ABC', status: 'Open', api_base: '/api/2', allow_comment: true });

// 5. Gmail as the user, sapnotes and sapnotedetail: only what each stores.
({ out } = addAndCollect({ url: 'builtin:gmail', auth_mode: 'destination',
    oauth: { destination: 'G', user_context: true, mailbox: 'x@y', lookback: '1d' } }));
assert.deepStrictEqual(out.oauth, { destination: 'G', user_context: true });
({ out } = addAndCollect({ url: 'builtin:sapnotes', auth_mode: 'destination',
    oauth: { destination: 'NVD', min_score: '9.0', user_context: true } }));
assert.deepStrictEqual(out.oauth, { destination: 'NVD', min_score: '9.0' });
({ row, out } = addAndCollect({ url: 'builtin:sapnotedetail', auth_mode: 'destination',
    oauth: { destination: 'MESAP' } }));
assert.deepStrictEqual(out.oauth, { destination: 'MESAP' });
assert.ok(row.querySelector('.dest-hint').textContent.includes('URL.headers.Cookie'),
    'the sapnotedetail hint says where the cookie now lives');

// 6. Editing an oauth2 server is untouched: no destination block is sent.
({ row, out } = addAndCollect({ url: 'https://x.hana.ondemand.com/mcp', auth_mode: 'oauth2',
    oauth: { dcr: true } }));
assert.strictEqual(row.querySelector('.mcp-destination').style.display, 'none');
assert.deepStrictEqual(out.oauth, { dcr: true });

console.log('destination server round-trip scenarios passed');
"""
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, dir=str(ROOT)
            ) as f:
                f.write(dest_harness)
                dest_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", dest_harness_path],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                check(
                    "destination servers round-trip through the row per built-in",
                    result.returncode == 0
                    and "destination server round-trip scenarios passed" in result.stdout,
                    (result.stderr or result.stdout)[-1500:],
                )
            finally:
                os.unlink(dest_harness_path)

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

        # The HTML admin is the only writer that carries peers/model_name, so
        # it is also the only way to CLEAR either one. The backend now treats
        # an absent key as "keep the stored value" (so the UI5 admin, which
        # sends neither, stops wiping them) -- which makes it load-bearing
        # that saveAgent() keeps sending both explicitly, including when they
        # are empty. Drop either key here and clearing silently stops working.
        print("\n== saveAgent() sends peers and model_name (clearing depends on it) ==")
        for field in ("peers", "model_name"):
            check(
                f"saveAgent() sends {field}",
                re.search(rf"\b{field}\s*:", save_agent_body) is not None,
                f"{field}: not found as an object key in saveAgent()",
            )

        # --- deep agents ---
        # Same contract for `deep`: the backend keeps the stored config when
        # the key is absent, so the HTML admin must always send the object,
        # or unticking "enable" could never reach the server.
        print("\n== deep agent section: controls and save contract ==")
        for fid in ("agent-deep-enabled", "agent-deep-planning", "agent-deep-scratchpad",
                    "agent-deep-subagents", "agent-deep-max-subagents",
                    "agent-deep-max-depth", "agent-deep-instructions"):
            found = any(
                e[1].get("id") == fid for e in coll.elements
                if e[0] in ("input", "textarea", "select")
            )
            check(f"deep form field #{fid}", found)
        check("deep section is collapsible (<details>)",
              any(e[0] == "details" and e[1].get("id") == "agent-deep-section"
                  for e in coll.elements))
        for fid, lo, hi in (("agent-deep-max-subagents", "1", "20"),
                            ("agent-deep-max-depth", "1", "3")):
            el = next((e for e in coll.elements
                       if e[0] == "input" and e[1].get("id") == fid), None)
            check(f"#{fid} is a bounded number input",
                  el is not None and el[1].get("type") == "number"
                  and el[1].get("min") == lo and el[1].get("max") == hi,
                  str(el))
        check("saveAgent() sends deep",
              re.search(r"\bdeep\s*:", save_agent_body) is not None)
        check("deep is collected from the form", "function collectDeep(" in js)
        check("editAgent() fills the deep section", "setDeepForm(a.deep" in js)
        check("openAgentModal() resets the deep section", "setDeepForm(null)" in js)
        collect_deep_body = js[js.index("function collectDeep("):]
        for key in ("enabled", "planning", "scratchpad", "subagents",
                    "max_subagents", "subagent_max_depth", "subagent_instructions"):
            check(f"collectDeep() sends {key}",
                  re.search(rf"\b{key}\s*:", collect_deep_body) is not None)

        # FastAPI returns `detail` as a plain string for the HTTPExceptions we
        # raise, but as a list of {loc, msg} objects for any pydantic body
        # validation error. Concatenating that into a toast yields
        # "Save failed: [object Object]", which tells the admin nothing.
        print("\n== error toasts render a pydantic detail array readably ==")
        check("errText helper exists", "function errText(" in js)
        check("errText handles an array detail", "Array.isArray(d)" in js)
        check("errText uses loc and msg", ".loc" in js and "e.msg" in js)
        for handler, label in (
            (r"async function saveAgent\(\)\s*\{(.*?)\n\}", "saveAgent"),
            (r"async function importConfig\(evt\)\s*\{(.*?)\n\}", "importConfig"),
        ):
            hm = re.search(handler, js, re.DOTALL)
            check(f"{label}() found in JS", hm is not None)
            body = hm.group(1) if hm else ""
            check(
                f"{label}() renders errors via errText",
                "errText(" in body,
                body[-300:],
            )
            check(
                f"{label}() no longer concatenates err.detail raw",
                "err.detail" not in body,
                body[-300:],
            )

        # ------------------------------------------------------------------
        # --- where used ---
        # Cross-links between the two editors: the agent modal says which
        # workflows and peers refer to the agent, and every workflow step row
        # links to its agent. Both editors are modals over the same page, so
        # the links are deep links opened in a new tab rather than in-place
        # switches that would drop the edits in the editor being left.
        print("\n== where used ==")
        check("where-used container in the agent modal", 'id="agent-where-used"' in html)
        check("where-used api used", "/where-used" in js)
        edit_agent = re.search(r"async function editAgent\(id\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("editAgent() found in JS", edit_agent is not None)
        check(
            "editAgent() loads where-used",
            "loadAgentWhereUsed(a.id)" in (edit_agent.group(1) if edit_agent else ""),
        )
        open_agent = re.search(r"async function openAgentModal\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check(
            "openAgentModal() clears where-used",
            "renderAgentWhereUsed(null)" in (open_agent.group(1) if open_agent else ""),
        )
        step_row = re.search(r"function addWorkflowStepRow\(step\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("addWorkflowStepRow() found in JS", step_row is not None)
        check(
            "step rows get an open-agent link",
            "attachStepAgentLink(row)" in (step_row.group(1) if step_row else ""),
        )
        check("deep links are handled on load", "function openFromHash" in js
              and re.search(r"^openFromHash\(\);\s*$", js, re.MULTILINE) is not None)
        check("editor links open in a new tab", 'target="_blank"' in js
              and "link.target = '_blank'" in js)

        if shutil.which("node") is not None:
            # renderAgentWhereUsed() under a DOM stub: names are operator
            # text and must land escaped; ids must become numeric deep links.
            wu_harness = r"""
'use strict';
const assert = require('node:assert');
const el = { innerHTML: '' };
global.document = {
    getElementById(id) {
        if (id === 'agent-where-used') return el;
        throw new Error('unstubbed getElementById: ' + id);
    },
};
""" + js_no_autoinvoke + r"""
renderAgentWhereUsed({
    agent: { id: 1, name: 'x' },
    peers: [{ id: 5, name: 'peer <b>', enabled: false }],
    workflows: [{ id: 7, name: 'wf & co', api_slug: null, enabled: true,
                  steps: [{ position: 1, branch_key: null }, { position: 2, branch_key: 'abap' }] }],
});
assert.ok(el.innerHTML.includes('href="#workflow=7"'), 'workflow deep link');
assert.ok(el.innerHTML.includes('href="#agent=5"'), 'peer deep link');
assert.ok(el.innerHTML.includes('wf &amp; co'), 'workflow name escaped');
assert.ok(el.innerHTML.includes('peer &lt;b&gt;'), 'peer name escaped');
assert.ok(!el.innerHTML.includes('peer <b>'), 'peer name not injected raw');
assert.ok(el.innerHTML.includes('main #1, abap #2'), 'positions listed per branch');
assert.ok(el.innerHTML.includes('disabled'), 'a disabled referrer is flagged');
renderAgentWhereUsed({ agent: { id: 1, name: 'x' }, peers: [], workflows: [] });
assert.ok(el.innerHTML.includes('no workflow or peer agent'), 'empty state');
renderAgentWhereUsed(null);
assert.strictEqual(el.innerHTML, '', 'cleared for a new agent');
console.log('renderAgentWhereUsed scenarios passed');
"""
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(wu_harness)
                wu_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", wu_harness_path], capture_output=True, text=True, timeout=10,
                )
                check(
                    "renderAgentWhereUsed() links, escapes and flags referrers",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:500],
                )
            finally:
                os.unlink(wu_harness_path)

        # ------------------------------------------------------------------
        # --- detail runs ---
        # The editors double as detail pages: Run now / Refresh in the modal
        # header (only for a saved row) and a "Last runs" table at the bottom
        # whose rows deep-link to the run detail views in a new tab.
        print("\n== detail runs (editor header buttons + last runs) ==")
        for el_id, tag in (
            ("agent-run-now", "button"), ("agent-refresh", "button"),
            ("agent-last-runs", "div"),
            ("workflow-run-now", "button"), ("workflow-refresh", "button"),
            ("workflow-last-runs", "div"),
        ):
            found = find(coll, tag, id=el_id)
            check(f"{tag}#{el_id} present", found is not None)
            check(f"#{el_id} starts hidden (new-row default)",
                  found is not None and "hidden" in found[1])
        for el_id, handler in (
            ("agent-run-now", "runAgentFromDetail()"),
            ("agent-refresh", "refreshAgentDetail()"),
            ("workflow-run-now", "runWorkflowFromDetail()"),
            ("workflow-refresh", "refreshWorkflowDetail()"),
        ):
            found = find(coll, "button", id=el_id)
            check(f"#{el_id} onclick={handler}",
                  found is not None and found[1].get("onclick") == handler)
        check("JS has the detail-runs header", "// --- detail runs ---" in js)
        edit_agent = re.search(r"async function editAgent\(id\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("editAgent() shows the detail-runs controls",
              "setDetailRunsMode('agent', a.id)" in (edit_agent.group(1) if edit_agent else ""))
        open_agent = re.search(r"async function openAgentModal\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("openAgentModal() hides them for a new agent",
              "setDetailRunsMode('agent', '')" in (open_agent.group(1) if open_agent else ""))
        edit_wf = re.search(r"async function editWorkflow\(id\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("editWorkflow() shows the detail-runs controls",
              "setDetailRunsMode('workflow', w.id)" in (edit_wf.group(1) if edit_wf else ""))
        open_wf = re.search(r"function openWorkflowModal\(\)\s*\{(.*?)\n\}", js, re.DOTALL)
        check("openWorkflowModal() hides them for a new workflow",
              "setDetailRunsMode('workflow', '')" in (open_wf.group(1) if open_wf else ""))
        check("openFromHash() handles run deep links",
              "/^#(run|workflow-run)=" in js and "openRunFromHash(" in js)
        check("editor Run now reuses the list's agent run helper",
              "startAgentRun(" in js and js.count("startAgentRun(") >= 3)
        check("editor Run now reuses the list's workflow run helper",
              "startWorkflowRun(" in js and js.count("startWorkflowRun(") >= 3)
        check("last-runs refresh after a run: 1 s and 5 s",
              "DETAIL_RUN_REFRESH_DELAYS = [1000, 5000]" in js)
        check("Refresh confirms before discarding unsaved edits",
              "detailFormDirty(kind)" in js
              and "confirm(`This ${cfg.noun} has unsaved changes" in js)
        # The discovery/replay section above must have seen the new api()
        # calls (and replayed them against the live app).
        for want in (
            ("GET", "/admin/api/runs?agent_id=1&limit=10"),
            ("GET", "/admin/api/workflow-runs?workflow_id=1&limit=10"),
            ("GET", "/admin/api/agents/1"),
            ("GET", "/admin/api/workflows/1"),
        ):
            check(f"api() call discovered: {want[0]} {want[1]}", want in discovered)

        # --- run detail auto-refresh ---
        print("\n== run detail auto-refresh (toolbar + poller) ==")
        for prefix in ("run-detail", "workflow-run-detail"):
            check(f"button#{prefix}-refresh present",
                  find(coll, "button", id=f"{prefix}-refresh") is not None)
            cb = find(coll, "input", id=f"{prefix}-autorefresh")
            check(f"input#{prefix}-autorefresh is a checkbox, checked by default",
                  cb is not None and cb[1].get("type") == "checkbox" and "checked" in cb[1])
            check(f"span#{prefix}-autorefresh-status present",
                  find(coll, "span", id=f"{prefix}-autorefresh-status") is not None)
            check(f"button#{prefix}-close present",
                  find(coll, "button", id=f"{prefix}-close") is not None)
        check("pure helpers exist",
              "function isLiveRunStatus(status)" in js
              and "function nextRefreshDelay(failures)" in js)
        check("one shared stopAutoRefresh()", "function stopAutoRefresh()" in js)
        check("run rows open through the poller",
              "openRunDetail('run', row.dataset.id)" in js
              and "openRunDetail('workflow-run', row.dataset.id)" in js)
        check("workflow run rows no longer interpolate the id into onclick",
              "showWorkflowRun('${r.id}')" not in js)
        check("switchTab() pauses the job-run poller",
              "_autoRefresh.kind === 'run'" in js)

        if shutil.which("node") is not None:
            # --- detail runs --- render functions under jsdom: server text is
            # escaped, ids become deep links, the empty/error states read
            # right, and the header buttons + block exist only for a saved row.
            detail_runs_harness = r"""
'use strict';
const assert = require('node:assert');
const { JSDOM } = require('jsdom');

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="toast"></div>
  <input type="hidden" id="agent-id"><input type="hidden" id="workflow-id">
  <button id="agent-run-now" hidden></button><button id="agent-refresh" hidden></button>
  <div id="agent-last-runs" hidden></div>
  <button id="workflow-run-now" hidden></button><button id="workflow-refresh" hidden></button>
  <div id="workflow-last-runs" hidden></div>
</body></html>`);
global.window = dom.window;
global.document = dom.window.document;

""" + js_no_autoinvoke + r"""

const urls = [];
global.fetch = async (url) => {
    urls.push(String(url));
    return { ok: true, json: async () => [] };
};
const tick = () => new Promise(r => setImmediate(r));
const byId = id => document.getElementById(id);

async function main() {
    // 1. Agent last runs: escaped, linked, duration formatted, summary truncated.
    const long = 'x'.repeat(200);
    renderDetailRuns('agent', [
        { id: 'r-1', status: 'success', trigger: 'manual',
          started_at: '2026-09-29T10:00:00Z', finished_at: '2026-09-29T10:01:05Z',
          summary: 'found <b>3</b> & more', error: null },
        { id: 'r-2', status: 'failed', trigger: 'scheduler',
          started_at: '2026-09-29T09:00:00Z', finished_at: '2026-09-29T09:00:07Z',
          summary: null, error: 'boom <script>' },
        { id: 'r-3', status: 'running', trigger: 'manual',
          started_at: new Date(Date.now() - 65000).toISOString(), finished_at: null,
          summary: long, error: null },
        { id: '"><img src=x onerror=1>', status: 'success', trigger: 'manual',
          started_at: null, finished_at: null, summary: '', error: null },
    ], 5);
    let html = byId('agent-last-runs').innerHTML;
    assert.ok(html.includes('href="#run=r-1"') && html.includes('href="#run=r-2"'),
        'job run deep links');
    assert.ok(html.includes('target="_blank"') && html.includes('rel="noopener"'),
        'links open a new tab');
    // Text is compared on the cell, elements via the DOM: jsdom serializes
    // `<` inside an attribute (the title holds the full summary) unescaped,
    // which is valid HTML, so a substring check on innerHTML would misfire.
    const box = byId('agent-last-runs');
    assert.ok(html.includes('>found &lt;b&gt;3&lt;/b&gt; &amp; more<'), 'summary escaped');
    assert.strictEqual(box.querySelector('b'), null, 'summary not injected raw');
    assert.ok(html.includes('>boom &lt;script&gt;<'), 'error text escaped');
    assert.strictEqual(box.querySelector('script'), null, 'error text not injected raw');
    assert.strictEqual(box.querySelector('img'), null, 'a hostile id cannot inject markup');
    assert.ok(html.includes('status-badge status-success')
        && html.includes('status-badge status-failed'), 'status badges reuse the Job Runs styling');
    assert.ok(html.includes('1m 05s') && html.includes('7s'), 'durations formatted');
    assert.ok(html.includes('1m 0') && html.includes('…'),
        'a live run counts up and is marked open-ended');
    const longCell = [...box.querySelectorAll('td.dr-summary')]
        .find(c => c.getAttribute('title') === long);
    assert.ok(longCell, 'full summary kept in the title');
    assert.ok(longCell.textContent.length < long.length && longCell.textContent.endsWith('…'),
        'long summary truncated in the cell');
    assert.ok(html.includes('<th>Status</th><th>Trigger</th><th>Started</th><th>Duration</th><th>Summary</th>'),
        'agent columns');
    assert.ok(html.includes('detail-runs-refresh'), 'block has its own Refresh link');

    // 2. Empty and error states.
    renderDetailRuns('agent', [], 5);
    assert.ok(byId('agent-last-runs').innerHTML.includes('No runs yet'), 'empty state');
    renderDetailRuns('agent', null, 5);
    assert.ok(byId('agent-last-runs').innerHTML.includes('Could not load the runs'), 'error state');

    // 3. Workflow last runs: item counts and the workflow-run deep link.
    renderDetailRuns('workflow', [
        { id: 'w-1', status: 'running', trigger: 'scheduler', items_total: 3, items_succeeded: 1,
          items_failed: 1, items_skipped: 0, started_at: '2026-09-29T10:00:00Z', finished_at: null,
          summary: null, error: 'x <i>' },
    ], 9);
    html = byId('workflow-last-runs').innerHTML;
    assert.ok(html.includes('href="#workflow-run=w-1"'), 'workflow run deep link');
    assert.ok(html.includes('3 · 1 ok · 1 failed · 0 skipped'), 'item counts');
    assert.ok(html.includes('>x &lt;i&gt;<'), 'workflow error escaped');
    assert.strictEqual(byId('workflow-last-runs').querySelector('i'), null,
        'workflow error not injected raw');
    assert.ok(html.includes('<th>Status</th><th>Trigger</th><th>Items</th><th>Started</th><th>Finished</th><th>Summary</th>'),
        'workflow columns');

    // 4. Hidden for a new row, shown (and loaded) for a saved one.
    setDetailRunsMode('agent', '');
    assert.ok(byId('agent-run-now').hidden && byId('agent-refresh').hidden,
        'buttons hidden for a new agent');
    assert.ok(byId('agent-last-runs').hidden && byId('agent-last-runs').innerHTML === '',
        'block hidden and empty');
    assert.strictEqual(urls.length, 0, 'nothing fetched for a new agent');

    byId('agent-id').value = '5';
    setDetailRunsMode('agent', 5);
    await tick();
    assert.ok(!byId('agent-run-now').hidden && !byId('agent-refresh').hidden,
        'buttons shown for a saved agent');
    assert.ok(!byId('agent-last-runs').hidden, 'block shown for a saved agent');
    assert.deepStrictEqual(urls, ['/admin/api/runs?agent_id=5&limit=10'],
        'agent runs fetched with id and limit');
    assert.ok(byId('agent-last-runs').innerHTML.includes('No runs yet'),
        'rendered once the answer is in');

    byId('workflow-id').value = '9';
    setDetailRunsMode('workflow', 9);
    await tick();
    assert.ok(!byId('workflow-run-now').hidden && !byId('workflow-refresh').hidden,
        'workflow buttons shown');
    assert.strictEqual(urls[1], '/admin/api/workflow-runs?workflow_id=9&limit=10',
        'workflow runs fetched');
    setDetailRunsMode('workflow', '');
    assert.ok(byId('workflow-run-now').hidden && byId('workflow-last-runs').hidden,
        'workflow controls hidden again');

    // 5. A late answer for a row no longer in the form is dropped.
    byId('agent-last-runs').innerHTML = 'KEEP';
    await loadDetailRuns('agent', 4);
    assert.strictEqual(byId('agent-last-runs').innerHTML, 'KEEP', 'stale answer ignored');

    console.log('detail runs scenarios passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, dir=ROOT) as f:
                f.write(detail_runs_harness)
                detail_runs_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", detail_runs_harness_path], capture_output=True, text=True, timeout=30,
                )
                check(
                    "renderDetailRuns()/setDetailRunsMode() escape, link and hide for a new row",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:800],
                )
            finally:
                os.unlink(detail_runs_harness_path)

            # --- run detail auto-refresh --- the poller under jsdom with hand
            # driven timers: starts for a live run, stops on a terminal
            # payload, on an unticked box and on close; Refresh re-fetches;
            # a failed poll toasts once and backs off.
            autorefresh_harness = r"""
'use strict';
const assert = require('node:assert');
const { JSDOM } = require('jsdom');

const dom = new JSDOM(`<!doctype html><html><body>
  <div id="toast"></div>
  <div class="toolbar" id="run-detail-toolbar" hidden>
    <button id="run-detail-refresh"></button>
    <input type="checkbox" id="run-detail-autorefresh" checked>
    <span id="run-detail-autorefresh-status"></span>
  </div>
  <div id="run-detail"></div>
  <div id="workflow-run-detail-toolbar" hidden>
    <button id="workflow-run-detail-refresh"></button>
    <input type="checkbox" id="workflow-run-detail-autorefresh" checked>
    <span id="workflow-run-detail-autorefresh-status"></span>
  </div>
  <div id="workflow-run-detail"></div>
</body></html>`);
global.window = dom.window;
global.document = dom.window.document;

// Timers are driven by hand: every setTimeout the page code asks for lands
// here, and fire() runs the oldest pending one.
const timers = [];
global.setTimeout = (fn, ms) => {
    timers.push({ fn, ms, cancelled: false, fired: false });
    return timers.length;
};
global.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].cancelled = true; };

""" + js_no_autoinvoke + r"""

const toasts = [];
toast = (msg) => { toasts.push(msg); };   // no toast timers in the queue
let status = 'running', okResp = true, fetchCount = 0;
global.fetch = async (url) => {
    fetchCount += 1;
    if (!okResp) return { ok: false, json: async () => ({ detail: 'boom' }) };
    if (String(url).includes('/workflow-runs/')) {
        return { ok: true, json: async () => ({
            run: { id: 'w1', workflow_name: 'wf', status, trigger: 'manual',
                   started_at: null, finished_at: null, summary: null, error: null },
            items: [], steps: [],
        }) };
    }
    return { ok: true, json: async () => ({
        id: 'r1', agent_name: 'a', status, trigger: 'manual',
        started_at: '2026-09-29T10:00:00Z', finished_at: null,
        summary: 'sum <b>', error: null, report: null,
    }) };
};
const live = () => timers.filter(t => !t.cancelled && !t.fired);
async function fire() {
    const t = live()[0];
    assert.ok(t, 'a timer is pending');
    t.fired = true;
    await t.fn();
}
const byId = id => document.getElementById(id);

async function main() {
    // Pure helpers.
    for (const s of ['running', 'pending', 'Queued']) assert.ok(isLiveRunStatus(s), s + ' is live');
    for (const s of ['success', 'failed', 'interrupted', '', null, undefined]) {
        assert.ok(!isLiveRunStatus(s), s + ' is terminal');
    }
    assert.strictEqual(nextRefreshDelay(0), 3000);
    assert.strictEqual(nextRefreshDelay(1), 10000);
    assert.strictEqual(nextRefreshDelay(4), 10000);

    // 1. A running run: rendered, toolbar shown, one 3 s timer queued.
    await openRunDetail('run', 'r1');
    const statusEl = byId('run-detail-autorefresh-status');
    assert.strictEqual(fetchCount, 1);
    assert.ok(!byId('run-detail-toolbar').hidden, 'toolbar shown');
    assert.ok(byId('run-detail').innerHTML.includes('sum &lt;b&gt;'), 'run rendered, escaped');
    assert.strictEqual(live().length, 1, 'exactly one poll scheduled');
    assert.strictEqual(live()[0].ms, 3000);
    assert.ok(statusEl.textContent.includes('Auto-refreshing every 3 s'), statusEl.textContent);
    assert.ok(statusEl.textContent.includes('Last refreshed'), statusEl.textContent);

    // 2. Still running: the chain continues with a single timer.
    await fire();
    assert.strictEqual(fetchCount, 2);
    assert.strictEqual(live().length, 1, 'never more than one pending poll');

    // 3. Finished payload: polling stops.
    status = 'success';
    await fire();
    assert.strictEqual(fetchCount, 3);
    assert.strictEqual(live().length, 0, 'no poll after a terminal status');
    assert.strictEqual(_autoRefresh, null);
    assert.ok(statusEl.textContent.includes('Finished, auto-refresh stopped'),
        statusEl.textContent);

    // 4. Unticking stops it; ticking again polls at once and resumes.
    status = 'running';
    await openRunDetail('run', 'r1');
    assert.strictEqual(live().length, 1);
    const box = byId('run-detail-autorefresh');
    box.checked = false;
    await toggleAutoRefresh('run');
    assert.strictEqual(live().length, 0, 'unticked: timer cleared');
    assert.ok(statusEl.textContent.includes('Auto-refresh off'), statusEl.textContent);
    let before = fetchCount;
    box.checked = true;
    await toggleAutoRefresh('run');
    assert.strictEqual(fetchCount, before + 1, 're-ticked: polled at once');
    assert.strictEqual(live().length, 1, 're-ticked: chain resumed');
    // Unticked while a run finishes: no timer, and nothing to resume later.
    box.checked = false;
    await toggleAutoRefresh('run');
    assert.strictEqual(live().length, 0);
    box.checked = true;

    // 5. Closing the view clears the timer and resets the view.
    await openRunDetail('run', 'r1');
    assert.strictEqual(live().length, 1);
    closeRunDetail('run');
    assert.strictEqual(live().length, 0, 'closed: timer cleared');
    assert.strictEqual(_autoRefresh, null);
    assert.ok(byId('run-detail-toolbar').hidden, 'closed: toolbar hidden');
    assert.ok(byId('run-detail').innerHTML.includes('Select a run'), 'closed: placeholder back');
    assert.strictEqual(statusEl.textContent, '');
    await refreshRunDetail('run');
    assert.strictEqual(live().length, 0, 'Refresh with nothing open is a no-op');

    // 6. The Refresh button re-fetches and restarts the countdown.
    await openRunDetail('run', 'r1');
    before = fetchCount;
    const oldTimer = live()[0];
    await refreshRunDetail('run');
    assert.strictEqual(fetchCount, before + 1, 'Refresh re-fetched');
    assert.ok(oldTimer.cancelled, 'Refresh replaced the pending poll');
    assert.strictEqual(live().length, 1);

    // 7. A failed poll: one toast, 10 s back-off, last good render kept.
    okResp = false;
    toasts.length = 0;
    await fire();
    assert.strictEqual(toasts.length, 1, 'one toast on the first failure');
    assert.strictEqual(live()[0].ms, 10000, 'backed off to 10 s');
    assert.ok(byId('run-detail').innerHTML.includes('sum &lt;b&gt;'), 'last good render kept');
    assert.ok(statusEl.textContent.includes('retrying in 10 s'), statusEl.textContent);
    await fire();
    assert.strictEqual(toasts.length, 1, 'no toast on repeated failures');
    okResp = true;
    await fire();
    assert.strictEqual(live()[0].ms, 3000, 'back to 3 s once a poll succeeds');

    // 8. Opening another run supersedes the poll: one shared poller.
    await openRunDetail('workflow-run', 'w1');
    assert.strictEqual(live().length, 1, 'the job-run poll was replaced, not added to');
    assert.strictEqual(_autoRefresh.kind, 'workflow-run');
    assert.ok(!byId('workflow-run-detail-toolbar').hidden);
    assert.ok(byId('workflow-run-detail').innerHTML.includes('status-running'),
        'workflow run rendered');
    status = 'failed';
    await fire();
    assert.strictEqual(live().length, 0);
    assert.ok(byId('workflow-run-detail-autorefresh-status').textContent.includes('Finished'),
        'workflow poll stopped');

    // 9. A run that never loads (404) is not polled.
    okResp = false;
    await openRunDetail('run', 'missing');
    assert.strictEqual(live().length, 0, 'no poll for a run that never loaded');
    assert.ok(byId('run-detail').innerHTML.includes('Failed to load run'),
        'first-load failure shown inline');
    okResp = true;

    // 10. Leaving the Job Runs tab pauses the job-run poll.
    status = 'running';
    await openRunDetail('run', 'r1');
    assert.strictEqual(live().length, 1);
    switchTab('config');
    assert.strictEqual(live().length, 0, 'tab hidden: timer cleared');
    assert.strictEqual(_autoRefresh, null);
    assert.ok(statusEl.textContent.includes('paused'), statusEl.textContent);

    console.log('run detail auto-refresh scenarios passed');
}

main().catch(err => { console.error(err); process.exitCode = 1; });
"""
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, dir=ROOT) as f:
                f.write(autorefresh_harness)
                autorefresh_harness_path = f.name
            try:
                result = subprocess.run(
                    ["node", autorefresh_harness_path], capture_output=True, text=True, timeout=30,
                )
                check(
                    "run detail poller starts, stops, backs off, refreshes and closes",
                    result.returncode == 0,
                    (result.stdout + result.stderr).strip()[:800],
                )
            finally:
                os.unlink(autorefresh_harness_path)

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
