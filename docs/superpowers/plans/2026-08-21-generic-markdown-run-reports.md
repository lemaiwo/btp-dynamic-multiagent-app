# Generic Markdown Run Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed sections/findings run report with a single markdown document plus a one-line summary, rendered in the admin UI with tables and Mermaid diagrams, and downloadable as `.md`.

**Architecture:** `RunReport` collapses to `{summary, body_md}`. The markdown stays a string in the existing `report_json` column — no migration. The admin UI renders it through `marked` → `DOMPurify` → DOM, then replaces each `mermaid` fence with a sanitized SVG. Mermaid (3.5MB) is injected lazily, only when a fence is present. The `expected_sections` / `missing_sections` / `degraded` checklist is removed entirely.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy async, pydantic-ai 1.72.0, vanilla JS admin UI, vendored marked@15 / DOMPurify@3 / mermaid@11, Node 24 for JS-level tests.

**Spec:** `docs/superpowers/specs/2026-08-21-generic-markdown-run-reports-design.md`

## Global Constraints

- **No pytest.** Tests are standalone scripts with a local `check(label, cond, detail="")` helper, run as `python tests/test_x.py`. Follow the existing style exactly.
- **No CDN at runtime.** All three JS libraries are vendored into `static/vendor/` and served by the existing `StaticFiles` mount (`app.py:216`).
- **Sanitize before insertion.** Model-authored markdown must pass through `DOMPurify.sanitize` before it reaches the DOM. Mermaid's SVG output is sanitized too. Mermaid runs with `securityLevel: 'strict'`.
- **Keep the dead columns.** `agent_configs.expected_sections_json` and `job_runs.missing_sections_json` stay in the schema; only the code that reads/writes them is removed. No destructive migration against the acc Postgres.
- **Terminal run statuses after this change:** `success`, `failed`, `interrupted`. `degraded` is removed.
- **No browser in the test harness.** `tests/test_admin_ui.py` tests via HTML parsing, `node --check`, and ASGI replay. Do not write tests that assert on a rendered DOM.
- **Local run command:** `./.venv/Scripts/python.exe` on this Windows checkout.
- **All line numbers are from the pre-change state** (HEAD at 2026-08-21). They
  shift as earlier tasks delete code. Locate code by the quoted snippet, not by
  the line number, if the two disagree.

---

### Task 1: Collapse the report model

**Files:**
- Modify: `agents/reports.py` (whole file)
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Consumes: nothing
- Produces: `RunReport(summary: str, body_md: str)` from `agents.reports`. `Finding`, `ReportSection`, `Severity`, `missing_sections` no longer exist.

- [ ] **Step 1: Write the failing test**

Add near the top of `main()` in `tests/test_job_runs.py`, after `await init_db()`:

```python
    print("\n== report model ==")
    from agents.reports import RunReport
    import agents.reports as reports_mod

    r = RunReport(summary="one line", body_md="# Title\n\n| a |\n| --- |\n| 1 |\n")
    check("summary field", r.summary == "one line")
    check("body_md field", r.body_md.startswith("# Title"))
    check("model_dump has exactly two keys",
          set(r.model_dump().keys()) == {"summary", "body_md"},
          str(set(r.model_dump().keys())))
    check("Finding removed", not hasattr(reports_mod, "Finding"))
    check("ReportSection removed", not hasattr(reports_mod, "ReportSection"))
    check("missing_sections removed", not hasattr(reports_mod, "missing_sections"))
    schema = RunReport.model_json_schema()
    check("body_md description mentions mermaid",
          "mermaid" in schema["properties"]["body_md"]["description"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_job_runs.py`
Expected: FAIL — the file still imports `ReportSection`, so it errors on import, or the `removed` checks fail.

- [ ] **Step 3: Replace `agents/reports.py` entirely**

```python
"""The report an API-triggered agent run produces.

Two fields, because two things consume a run: the runs list wants one
scannable line, and the run page wants the whole document. The document is
markdown so the agent's prompt — not a schema — decides what a report
contains. pydantic-ai validates this as the agent's output_type and retries
the model on a mismatch, so the shape is enforced rather than hoped for.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RunReport(BaseModel):
    """A run's report: one line for the list, one markdown document for the page."""

    summary: str = Field(
        description=(
            "One sentence, plain text, no markdown. Shown in the runs table "
            "and used as the notification subject."
        )
    )
    body_md: str = Field(
        description=(
            "The full report as GitHub-flavoured markdown. Use headings and "
            "tables; put tabular data in a markdown table rather than prose. "
            "For a chart or diagram, emit a ```mermaid fenced block "
            "(pie, xychart-beta, flowchart, sequenceDiagram). Do not emit raw "
            "HTML: it is stripped before rendering."
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_job_runs.py`
Expected: the six new checks PASS. Later checks in the file still FAIL — Task 2 fixes those. Do not fix them here.

- [ ] **Step 5: Commit**

```bash
git add agents/reports.py tests/test_job_runs.py
git commit -m "refactor: collapse RunReport to summary + markdown body"
```

---

### Task 2: Runner stops grading runs

**Files:**
- Modify: `agents/job_runner.py:23` (import), `agents/job_runner.py:194-209` (run + finalize)
- Modify: `agents/db.py:1135-1155` (`finish_job_run`)
- Test: `tests/test_job_runs.py`

**Interfaces:**
- Consumes: `RunReport(summary, body_md)` from Task 1.
- Produces: completed runs persist `status="success"` and `report_json={"summary":…, "body_md":…}`. `finish_job_run` no longer accepts `missing`.

- [ ] **Step 1: Write the failing test**

In `tests/test_job_runs.py`, replace the `good` fixture (~:237) and delete the `partial` fixture with every assertion that follows it about `degraded` / missing sections:

```python
    good = RunReport(
        summary="3 ADT changes, 0 blockers",
        body_md=(
            "# What's new\n\n"
            "| Source | Findings |\n| --- | --- |\n| ADT | 3 |\n\n"
            "```mermaid\npie title Findings by source\n  \"ADT\" : 3\n```\n"
        ),
    )
    registry._build = _FakeBuild({"Daily Check": _FakeSpecialist(good)})
    os.environ["PUBLIC_BASE_URL"] = "https://approuter.example.com"
    job_runner._has_usable_credentials = lambda agent: asyncio.sleep(0, result=True)

    async with SessionLocal() as s:
        job = await get_agent_by_slug(s, "daily-check")
        agent_id, run_id = job.id, (await create_job_run(s, agent=job, trigger="manual")).id
    await job_runner.execute_run(run_id, agent_id)

    async with SessionLocal() as s:
        r = await get_job_run(s, run_id)
        check("run is success", r.status == "success", r.status)
        check("summary stored", r.summary == "3 ADT changes, 0 blockers")
        check("markdown body stored", "| Source | Findings |" in r.report["body_md"])
        check("mermaid fence survives storage", "```mermaid" in r.report["body_md"])
        check("no legacy keys",
              "sections" not in r.report and "overall_severity" not in r.report,
              str(sorted(r.report.keys())))
```

Also remove `ReportSection` from the `from agents.reports import ...` line, and remove any `expected_sections=[...]` argument from the `upsert_agent` calls in this file.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_job_runs.py`
Expected: FAIL — `job_runner` still imports `missing_sections` from `agents.reports`, which Task 1 deleted, so the module fails to import.

- [ ] **Step 3: Update the runner**

In `agents/job_runner.py:23`, change the import to:

```python
from agents.reports import RunReport
```

Replace the block at `agents/job_runner.py:199-209` with:

```python
        report: RunReport = result.output
        await _finalize(
            run_id,
            status="success",
            summary=report.summary,
            report=report.model_dump(mode="json"),
        )
```

In `agents/db.py`, delete the `missing` parameter from `finish_job_run` (`:1143`) and the line that writes it (`:1152`):

```python
async def finish_job_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    report: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    row = await session.get(JobRun, run_id)
    if row is None:
        return
    row.status = status
    row.summary = summary
    row.report_json = json.dumps(report) if report is not None else None
    row.error = error
    row.finished_at = datetime.now(timezone.utc)
    await session.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_job_runs.py`
Expected: PASS, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add agents/job_runner.py agents/db.py tests/test_job_runs.py
git commit -m "refactor: runs are success or failed, never degraded"
```

---

### Task 3: Remove the checklist from API, DB and admin form

**Files:**
- Modify: `agents/admin.py:223` (request model), `:349`, `:412-413`, `:764`, `:860`
- Modify: `agents/db.py:242-249` (property), `:272`, `:290` (to_dict), `:421-425` (property), `:441` (to_dict), `:777`, `:814-815`, `:835-836` (helpers)
- Modify: `templates/admin.html:354-356`, `:717`, `:809-812`, `:841`, `:866`, `:1033-1034`
- Test: `tests/test_admin_api.py`, `tests/test_admin_ui.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AgentConfig.to_dict()` and `JobRun.to_dict()` no longer contain `expected_sections` / `missing_sections`. `upsert_agent()` no longer accepts `expected_sections`.

- [ ] **Step 1: Write the failing test**

In `tests/test_admin_api.py`, delete the `expected_sections` round-trip assertions, and add to the agent-creation block:

```python
        check("expected_sections gone from agent payload",
              "expected_sections" not in created,
              str(sorted(created.keys())))
```

And in the run-detail block (~:739):

```python
        check("missing_sections gone from run payload",
              "missing_sections" not in r.json(),
              str(sorted(r.json().keys())))
```

In `tests/test_admin_ui.py`, in the HTML structure section:

```python
        check("expected-sections input removed",
              'id="agent-expected-sections"' not in html)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe tests/test_admin_api.py` then `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: FAIL on the three new checks — the keys and the input are still present.

- [ ] **Step 3: Remove every site**

`agents/admin.py`: delete the `expected_sections: list[str] = Field(default_factory=list)` field at `:223`; delete the `expected_sections=payload.expected_sections,` argument at `:349` and `:860`; delete the two-line `row.expected_sections_json = (...)` assignment at `:412-413`; delete `expected_sections=agent.expected_sections,` at `:764`.

`agents/db.py`: delete the `expected_sections` property (`:242-249`) and its `to_dict` entries (`:272`, `:290`); delete the `missing_sections` property (`:421-425`) and its `to_dict` entry (`:441`); delete the `expected_sections: list[str] | None = None` parameter (`:777`) and both assignment blocks (`:814-815`, `:835-836`). Leave `expected_sections_json` (`:184`), `missing_sections_json` (`:399`) and the migration at `:575` in place — the columns stay.

`templates/admin.html`: delete the `<label>Expected report sections…</label>` block (`:354-356`); delete the reset line (`:717`); delete `collectExpectedSections()` entirely (`:809-812`); delete the load line (`:841`); delete `expected_sections: collectExpectedSections(),` (`:866`); delete the `Not checked:` warning expression (`:1033-1034`).

- [ ] **Step 4: Run tests to verify they pass**

Run all four suites:

```bash
./.venv/Scripts/python.exe tests/test_job_runs.py
./.venv/Scripts/python.exe tests/test_admin_api.py
./.venv/Scripts/python.exe tests/test_admin_ui.py
./.venv/Scripts/python.exe tests/test_registry.py 2>/dev/null || true
```
Expected: PASS, 0 failures in the first three.

- [ ] **Step 5: Commit**

```bash
git add agents/admin.py agents/db.py templates/admin.html tests/
git commit -m "refactor: drop expected_sections checklist from API, DB and admin form"
```

---

### Task 4: Vendor the rendering assets

**Files:**
- Create: `static/vendor/marked.min.js`, `static/vendor/purify.min.js`, `static/vendor/mermaid.min.js`
- Create: `static/vendor/README.md`
- Test: `tests/test_admin_ui.py`

**Interfaces:**
- Consumes: nothing.
- Produces: three files under `static/vendor/`, served at `/static/vendor/<name>`. `marked.min.js` exports `{ marked }` under CommonJS `require`; `mermaid.min.js` sets `globalThis.mermaid` when loaded as a classic script.

- [ ] **Step 1: Write the failing test**

In `tests/test_admin_ui.py`, HTML structure section:

```python
        print("\n== report rendering assets ==")
        for name in ("marked.min.js", "purify.min.js", "mermaid.min.js"):
            check(f"{name} vendored", (ROOT / "static" / "vendor" / name).exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: FAIL — three missing files.

- [ ] **Step 3: Download the assets**

```bash
mkdir -p static/vendor
curl -sL -o static/vendor/marked.min.js  https://cdn.jsdelivr.net/npm/marked@15/marked.min.js
curl -sL -o static/vendor/purify.min.js  https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js
curl -sL -o static/vendor/mermaid.min.js https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js
```

Expected sizes, as a sanity check: marked ~39KB, purify ~29KB, mermaid ~3.5MB.

Write `static/vendor/README.md`:

```markdown
# Vendored browser libraries

Downloaded, not built. The admin UI must work with no CDN access.

| File | Source | Version |
| --- | --- | --- |
| `marked.min.js` | https://cdn.jsdelivr.net/npm/marked@15/marked.min.js | 15.x |
| `purify.min.js` | https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js | 3.x |
| `mermaid.min.js` | https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js | 11.x |

To refresh, re-run the curl commands in
`docs/superpowers/plans/2026-08-21-generic-markdown-run-reports.md` Task 4
and re-run `node tests/test_report_render.mjs`.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: the three asset checks PASS.

- [ ] **Step 5: Commit**

```bash
git add static/vendor
git commit -m "chore: vendor marked, dompurify and mermaid for report rendering"
```

---

### Task 5: The markdown pipeline test

**Files:**
- Create: `tests/test_report_render.mjs`

**Interfaces:**
- Consumes: `static/vendor/marked.min.js` from Task 4.
- Produces: `node tests/test_report_render.mjs`, exit 0 on pass. Asserts `templates/admin.html` contains a `renderReportBody` function — which Task 6 creates, so this test is expected to fail its last four checks until then.

- [ ] **Step 1: Write the test**

Create `tests/test_report_render.mjs`:

```js
// Markdown rendering contract for run reports.
// Run:  node tests/test_report_render.mjs
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { marked } = require('../static/vendor/marked.min.js');

let failed = 0;
const check = (label, cond, detail = '') => {
  if (cond) { console.log(`  PASS  ${label}`); }
  else { failed++; console.log(`  FAIL  ${label}   ${detail}`); }
};

console.log('\n== markdown -> html ==');
const html = marked.parse(
  '| a | b |\n| --- | --- |\n| 1 | 2 |\n\n' +
  '```mermaid\npie title T\n  "x" : 1\n```\n',
  { gfm: true },
);
check('table renders', html.includes('<table>'), html.slice(0, 120));
check('mermaid fence keeps its language class', html.includes('language-mermaid'));
check('fence content is left verbatim', html.includes('pie title T'));

console.log('\n== sanitization is wired in ==');
// Structural, not behavioural: DOMPurify needs a DOM, so this guards against
// the realistic regression (someone drops the sanitize call) rather than
// proving sanitization works. Behaviour is covered by the manual step in the spec.
const tpl = readFileSync(new URL('../templates/admin.html', import.meta.url), 'utf8');
const i = tpl.indexOf('function renderReportBody');
check('renderReportBody exists', i !== -1);
const render = tpl.slice(i, i + 4000);
check('markdown output is sanitized', /DOMPurify\.sanitize/.test(render));
check('never inserts unsanitized markdown',
      !/innerHTML\s*=\s*marked\.parse/.test(render));
check('mermaid svg is sanitized too',
      (render.match(/DOMPurify\.sanitize/g) || []).length >= 2);

process.exit(failed ? 1 : 0);
```

- [ ] **Step 2: Run it to see the expected partial failure**

Run: `node tests/test_report_render.mjs`
Expected: the three `markdown -> html` checks PASS; the four sanitization checks FAIL with `renderReportBody exists` first, because Task 6 has not run yet. This confirms the markdown half works against the real vendored file.

- [ ] **Step 3: Commit**

```bash
git add tests/test_report_render.mjs
git commit -m "test: markdown rendering contract for run reports"
```

---

### Task 6: Render markdown in the run detail

**Files:**
- Modify: `templates/admin.html` — add `<script src>` tags before `:393`, add CSS near `:149`, replace `showRun()` at `:1023-1057`
- Modify: `tests/test_admin_ui.py:266-267` (script-count assertion)
- Test: `tests/test_report_render.mjs`, `tests/test_admin_ui.py`

**Interfaces:**
- Consumes: vendored assets from Task 4; `RunReport.body_md` from Task 1.
- Produces: `renderReportBody(container, report, runId)` in the inline script — the anchor Task 5's structural checks look for.

- [ ] **Step 1: Fix the script-count assertion first**

`tests/test_admin_ui.py:266-267` asserts `exactly one <script>`. Adding two `<script src>` tags breaks it. Change those two lines to:

```python
        inline = [s for s in coll.scripts if s.strip()]
        check("exactly one inline <script>", len(inline) == 1, f"got {len(inline)}")
        js = inline[0]
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_admin_ui.py`, HTML structure section:

```python
        check("template loads marked", "/static/vendor/marked.min.js" in html)
        check("template loads purify", "/static/vendor/purify.min.js" in html)
        check("mermaid is NOT eagerly loaded",
              '<script src="/static/vendor/mermaid.min.js"' not in html)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py`
Expected: FAIL on `template loads marked` and `template loads purify`.

- [ ] **Step 4: Add the script tags and CSS**

Immediately before `<script>` at `templates/admin.html:393`:

```html
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script>
```

Add near the `#run-detail` rules at `:149`:

```css
        #run-detail .md img { max-width: 100%; height: auto; }
        #run-detail .md pre { overflow-x: auto; }
        #run-detail .md table { border-collapse: collapse; margin: 10px 0; }
        #run-detail .md th, #run-detail .md td {
            border: 1px solid var(--border); padding: 4px 8px; text-align: left;
        }
        #run-detail .mermaid-error { color: var(--muted); font-size: 12px; margin-top: 4px; }
```

- [ ] **Step 5: Replace `showRun()` and add `renderReportBody()`**

Replace `templates/admin.html:1023-1057` (the whole `showRun` function) with:

```js
let _mermaidLoading = null;

function loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (_mermaidLoading) return _mermaidLoading;
    _mermaidLoading = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = '/static/vendor/mermaid.min.js';
        s.onload = () => {
            window.mermaid.initialize({
                startOnLoad: false, securityLevel: 'strict', theme: 'dark',
            });
            resolve(window.mermaid);
        };
        s.onerror = () => reject(new Error('mermaid failed to load'));
        document.head.appendChild(s);
    });
    return _mermaidLoading;
}

async function renderReportBody(container, report, runId) {
    if (!report || typeof report.body_md !== 'string') {
        // Pre-change runs have no markdown body; show what is stored.
        container.innerHTML = '<h4>Report</h4><pre class="md"></pre>';
        container.querySelector('pre').textContent =
            report ? JSON.stringify(report, null, 2) : 'No report recorded.';
        return;
    }
    const body = document.createElement('div');
    body.className = 'md';
    body.innerHTML = DOMPurify.sanitize(marked.parse(report.body_md, {gfm: true}));
    container.appendChild(body);

    const link = document.createElement('p');
    link.innerHTML =
        `<a href="/admin/api/runs/${encodeURIComponent(runId)}/report.md">Download .md</a>`;
    container.appendChild(link);

    const fences = body.querySelectorAll('pre > code.language-mermaid');
    if (!fences.length) return;

    let mermaid;
    try {
        mermaid = await loadMermaid();
    } catch (e) {
        return;  // fences stay as code blocks; report is still readable
    }
    for (let i = 0; i < fences.length; i++) {
        const code = fences[i];
        try {
            const {svg} = await mermaid.render(`md-${runId}-${i}`, code.textContent);
            const holder = document.createElement('div');
            holder.innerHTML = DOMPurify.sanitize(svg, {USE_PROFILES: {svg: true, svgFilters: true}});
            code.parentElement.replaceWith(holder);
        } catch (e) {
            const note = document.createElement('p');
            note.className = 'mermaid-error';
            note.textContent = 'Diagram could not be rendered.';
            code.parentElement.after(note);
        }
    }
}

async function showRun(id) {
    const res = await fetch(`/admin/api/runs/${id}`);
    const el = document.getElementById('run-detail');
    if (!res.ok) { el.innerHTML = '<p class="error">Failed to load run.</p>'; return; }
    const run = await res.json();
    el.innerHTML = `<h3>${esc(run.agent_name)} — <span class="status-badge status-${esc(run.status)}">${esc(run.status)}</span></h3>`
        + `<p class="hint">Trigger: ${esc(run.trigger)} · Started: ${run.started_at ? esc(new Date(run.started_at).toLocaleString()) : ''}${run.finished_at ? ' · Finished: ' + esc(new Date(run.finished_at).toLocaleString()) : ''}</p>`
        + (run.summary ? `<p>${esc(run.summary)}</p>` : '')
        + (run.error ? `<p class="error">${esc(run.error)}</p>` : '');
    await renderReportBody(el, run.report, run.id);
}
```

- [ ] **Step 6: Run both test suites to verify they pass**

Run: `./.venv/Scripts/python.exe tests/test_admin_ui.py` then `node tests/test_report_render.mjs`
Expected: both PASS, 0 failures. `test_report_render.mjs` now finds `renderReportBody` and its two `DOMPurify.sanitize` calls.

- [ ] **Step 7: Commit**

```bash
git add templates/admin.html tests/test_admin_ui.py
git commit -m "feat: render run reports as markdown with tables and mermaid"
```

---

### Task 7: The `.md` export endpoint

**Files:**
- Modify: `agents/admin.py` — add route after `api_get_run` (`:542-553`)
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: `RunReport.body_md` from Task 1; the Download link added in Task 6 points here.
- Produces: `GET /admin/api/runs/{run_id}/report.md` → `text/markdown` attachment, or 404.

- [ ] **Step 1: Write the failing test**

In `tests/test_admin_api.py`, after the existing `/admin/api/runs/{id}` block (~:739):

```python
        print("\n== GET /admin/api/runs/{id}/report.md ==")
        from agents.db import finish_job_run, SessionLocal as _SL

        async with _SL() as s:
            await finish_job_run(s, sched_run_id, status="success",
                                 summary="one line",
                                 report={"summary": "one line",
                                         "body_md": "# What's new\n\n| a |\n| --- |\n"})
        r = await client.get(f"/admin/api/runs/{sched_run_id}/report.md")
        check("200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
        check("markdown content type",
              r.headers["content-type"].startswith("text/markdown"),
              r.headers.get("content-type"))
        check("attachment filename",
              f'filename="run-{sched_run_id}.md"' in r.headers.get("content-disposition", ""),
              r.headers.get("content-disposition"))
        check("body is the markdown verbatim", r.text.startswith("# What's new"))

        async with _SL() as s:
            await finish_job_run(s, sched_run_id, status="success", summary="old",
                                 report={"summary": "old", "sections": []})
        r = await client.get(f"/admin/api/runs/{sched_run_id}/report.md")
        check("legacy run 404s", r.status_code == 404, f"got {r.status_code}")

        r = await client.get("/admin/api/runs/does-not-exist/report.md")
        check("unknown run 404s", r.status_code == 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe tests/test_admin_api.py`
Expected: FAIL — `404` for the happy path, because the route does not exist.

- [ ] **Step 3: Add the route**

At the top of `agents/admin.py`, add to the imports:

```python
from fastapi.responses import PlainTextResponse
```

After `api_get_run` (`agents/admin.py:553`):

```python
@router.get("/api/runs/{run_id}/report.md", dependencies=[Depends(require_admin)])
async def api_get_run_markdown(run_id: str) -> PlainTextResponse:
    """The run's report as a downloadable .md file.

    Runs recorded before reports became markdown have no body_md; there is
    nothing to serve for those, so they 404 rather than returning an empty file.
    """
    from agents.db import get_job_run

    async with SessionLocal() as session:
        row = await get_job_run(session, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        body = (row.report or {}).get("body_md")
        if not isinstance(body, str):
            raise HTTPException(status_code=404, detail="Run has no markdown report")
        return PlainTextResponse(
            body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="run-{run_id}.md"'},
        )
```

**Route order matters:** FastAPI matches in declaration order, and `/api/runs/{run_id}` would otherwise swallow `/api/runs/{id}/report.md`. It does not here — the paths differ in segment count — but keep this route adjacent to `api_get_run` so the relationship stays obvious.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe tests/test_admin_api.py`
Expected: PASS, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add agents/admin.py tests/test_admin_api.py
git commit -m "feat: download a run report as markdown"
```

---

### Task 8: Full verification

**Files:**
- Modify: `CLAUDE.md` (key-files description of `agents/reports.py`), if it mentions the report shape
- Test: all suites

**Interfaces:**
- Consumes: everything above.
- Produces: a verified working feature.

- [ ] **Step 1: Run every suite**

```bash
./.venv/Scripts/python.exe tests/test_job_runs.py
./.venv/Scripts/python.exe tests/test_admin_api.py
./.venv/Scripts/python.exe tests/test_admin_ui.py
./.venv/Scripts/python.exe tests/test_registry.py
./.venv/Scripts/python.exe tests/test_a2a.py
./.venv/Scripts/python.exe tests/test_tool_resilience.py
node tests/test_report_render.mjs
```
Expected: 0 failures across all seven.

- [ ] **Step 2: Grep for leftovers**

```bash
grep -rn "expected_sections\|missing_sections\|degraded\|ReportSection\|overall_severity" \
  --include=*.py --include=*.html agents/ templates/ tests/ app.py
```
Expected: only `expected_sections_json` / `missing_sections_json` column definitions and the migration in `agents/db.py`. Anything else is a missed removal.

- [ ] **Step 3: Manual browser verification**

Start the app (`./.venv/Scripts/python.exe app.py`), trigger a run, and open `/admin` → Runs. Confirm each of these four, which no automated test here can prove:

1. a markdown table renders as a styled `<table>`
2. a valid `mermaid` fence renders as an `<svg>`
3. a malformed `mermaid` fence stays a code block with the muted note, and does not stop case 2 from rendering
4. with `<script>window.__pwned = 1</script>` in the body, `window.__pwned` is `undefined` in the console afterwards

For case 3 and 4, edit a stored run directly rather than hoping the model emits them:

```python
./.venv/Scripts/python.exe -c "
import asyncio, json
from agents.db import SessionLocal, finish_job_run, list_job_runs
body = '''# Test

| a | b |
| --- | --- |
| 1 | 2 |

\`\`\`mermaid
pie title Good
  \"x\" : 1
\`\`\`

\`\`\`mermaid
this is not valid mermaid
\`\`\`

<script>window.__pwned = 1</script>
'''
async def go():
    async with SessionLocal() as s:
        runs = await list_job_runs(s, limit=1)
        assert runs, 'no runs yet — trigger one from /admin first'
        await finish_job_run(s, runs[0].id, status='success',
                             summary='render test',
                             report={'summary':'render test','body_md':body})
        print('patched run', runs[0].id)
asyncio.run(go())
"
```

- [ ] **Step 4: Update `CLAUDE.md` if needed**

Check whether `CLAUDE.md` describes the report shape under "Key files". If it does, update it to say the report is `summary` plus a markdown body. If it does not mention reports, change nothing.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: record markdown report verification"
```
