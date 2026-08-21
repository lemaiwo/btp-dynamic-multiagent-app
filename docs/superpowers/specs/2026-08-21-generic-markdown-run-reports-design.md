# Generic Markdown Run Reports — Design

**Date:** 2026-08-21
**Status:** Awaiting review
**Supersedes:** the report model in
`2026-08-19-scheduled-agent-runs-design.md` §5 ("Report model")

## Goal

Let an agent's run report be whatever its prompt asks for, rendered richly.

Today a run is graded against a fixed checklist: the agent must emit sections
whose `source_key` values match the agent's `expected_sections`, or the run is
marked `degraded`. That made sense for the daily-health-check scenario, where
the point is "did it actually look at ST22", but it fights every other
scenario. A "what's new in SAP" agent has no fixed set of sources to check,
so it invents section keys and is marked degraded for doing exactly what its
prompt asked.

After this change the report is one markdown document plus a one-line summary.
The prompt decides the content. The admin UI renders tables and Mermaid
diagrams, and the report can be downloaded as a `.md` file.

## Background and constraints

Properties of the existing system this design respects, established by reading
the code and by exercising a live run (`1a00a4b7`, 2026-08-21), not assumed.

**The structured report has almost no consumers.** `RunReport` is declared as
the shared structure that "the run detail page, the notification summary and
any later trending all read". In practice, as of today:

- The run detail renderer in `templates/admin.html:1028-1056` is the only code
  that reads `sections` and `findings`.
- `overall_severity` is rendered there and nowhere else.
- There is no notification module. `JobRun.notified` (`agents/db.py:402`) is
  written as `0` and never read. `agents/ans.py` from the earlier design does
  not exist yet.
- The scheduler-facing endpoint (`agents/api_runs.py`) returns `202` with a
  run id. It never returns the report body.

So the blast radius of changing the report shape is the admin UI plus the
runner, not a web of dependents.

**`summary` is load-bearing and must survive.** It is the runs-table column,
and ANS email (still to be built, per `docs/ANS_SETUP.md`) needs a short
subject line. A markdown body cannot serve that purpose — ANS gets the summary
plus a link, never the body.

**The admin UI escapes everything today.** Every insertion path goes through
`esc()`. Rendering model-authored markdown deliberately relaxes that, so
sanitization is a requirement of this design, not a nicety.

**`/admin` is admin-scoped.** `approuter/xs-app.json` gates it behind
`$XSAPPNAME.admin`. Content rendered there executes with an admin session in
scope, and part of that content originates from MCP servers outside our trust
boundary.

**Reports are model-generated.** Invalid Mermaid syntax is common enough that
a failed diagram must be a normal, expected outcome rather than an error.

## Architecture

```
specialist.run(prompt, output_type=RunReport)
        |
        v
RunReport{ summary, body_md }        <- pydantic-ai validates + retries
        |
        v
JobRun.report_json  (unchanged column, new payload shape)
        |
        +--> GET /admin/api/runs/{id}          -> JSON, for the UI
        +--> GET /admin/api/runs/{id}/report.md -> text/markdown, download
                       |
                       v
        marked -> DOMPurify -> DOM -> mermaid.render (per fence) -> sanitized SVG
```

Nothing about storage changes: the markdown is a string inside the existing
`report_json` TEXT column. No migration, and the shape is identical on
Postgres and local SQLite.

## Components

### 1. Report model (`agents/reports.py`)

The file collapses to one model. `Finding`, `ReportSection`, `Severity` and
`missing_sections()` are deleted.

```python
class RunReport(BaseModel):
    """A run's report: one line for the list, one markdown document for the page."""

    summary: str = Field(
        description="One sentence, plain text, no markdown. Shown in the runs "
                    "table and used as the notification subject."
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

Format guidance lives in the field descriptions because pydantic-ai sends the
JSON schema — descriptions included — to the model as the output contract.
That puts the instruction where the shape is already enforced, instead of
requiring every agent's `run_prompt` to repeat it.

### 2. Runner (`agents/job_runner.py`)

`missing_sections` import and call go. The finalize call becomes:

```python
report: RunReport = result.output
await _finalize(
    run_id,
    status="success",
    summary=report.summary,
    report=report.model_dump(mode="json"),
)
```

`_finalize` forwards `**kwargs` to `finish_job_run` (`agents/db.py:1135`), so
the `missing` keyword-only parameter and the `missing_sections_json` write at
`agents/db.py:1152` are removed there. Terminal statuses become `success`,
`failed` and `interrupted`; `degraded` is gone because its only trigger is.

### 3. Export endpoint (`agents/admin.py`)

```
GET /admin/api/runs/{run_id}/report.md    (require_admin)
```

Returns `PlainTextResponse(body_md, media_type="text/markdown")` with
`Content-Disposition: attachment; filename="run-<id>.md"`. A run with no
report, or an old-format report with no `body_md`, returns 404.

### 4. Vendored assets (`static/vendor/`)

Served by the existing `StaticFiles` mount (`app.py:216`). No CDN: nothing to
whitelist, and it works offline and behind the approuter.

| File | Purpose | Loaded |
| --- | --- | --- |
| `marked.min.js` | markdown → HTML | with the page |
| `purify.min.js` | HTML sanitization | with the page |
| `mermaid.min.js` | diagram rendering (**3.5MB**) | on demand |

Mermaid is injected as a classic `<script>` only when the body matches
`/^```mermaid/m`, so a report without diagrams never pays its 3.5MB cost. The
UMD bundle ends with `globalThis["mermaid"] = ...`, so a plain script tag is
enough — no module loader needed. (Verified against mermaid@11 on 2026-08-21.)

Adding these tags breaks `tests/test_admin_ui.py:266`, which asserts
`exactly one <script>`; that assertion must be narrowed to inline scripts.

### 5. Run detail rendering (`templates/admin.html`)

`renderRun()` delegates the report body to a new helper,
`renderReportBody(container, report, runId)`, so the pipeline lives in one
named function the tests can locate. It:

1. If `run.report?.body_md` is absent, render the raw report JSON in a `<pre>`
   and stop. This is the agreed treatment for pre-change runs; there is no
   legacy renderer.
2. `marked.parse(body_md)` with `{gfm: true}` for table support.
3. `DOMPurify.sanitize(html)` — strips `<script>`, event handlers and
   `javascript:` URLs.
4. Insert, then for each `pre > code.language-mermaid`, call
   `mermaid.render`, sanitize the returned SVG, and replace the `<pre>`.
   Each diagram is wrapped in its own `try/catch`: on failure the code block
   stays put with a muted "diagram could not be rendered" note beneath it.
5. Add a Download `.md` link pointing at the export endpoint.

Mermaid is initialized once with
`{startOnLoad: false, securityLevel: 'strict', theme: 'dark'}` to match the
admin palette.

Markdown tables inherit the panel's existing `table` styling. New CSS is
limited to constraining images and giving `pre` a horizontal scroll so a wide
code block cannot stretch the panel.

### 6. Removing the checklist

| Site | Change |
| --- | --- |
| `agents/reports.py` | delete `missing_sections`, `ReportSection`, `Finding`, `Severity` |
| `agents/job_runner.py` | drop the import, the call, the `degraded` branch, `_finalize(missing=...)` |
| `agents/admin.py` | drop `expected_sections` from the request model (:223) and its 4 call sites (:349, :412, :764, :860) |
| `agents/db.py` | drop the `expected_sections` property (:242) and both `to_dict` keys (:272, :290); drop the `missing_sections` property (:421) and its `to_dict` key (:441); drop `expected_sections` from the create/update helpers (:777, :814, :835); drop `missing` from `finish_job_run` (:1143, :1152) |
| `templates/admin.html` | delete the "Expected report sections" input, `collectExpectedSections()`, its load/reset lines, and the "Not checked:" warning |

**The `expected_sections_json` and `missing_sections_json` columns stay.** The
code stops reading and writing them; the columns remain in place. Dropping
them means a destructive migration against the acc Postgres for no functional
gain. They are inert once nothing references them.

## Security

The threat is that `body_md` is model-authored, partly derived from MCP
servers outside our trust boundary, and rendered into an admin-scoped page.

- All generated HTML passes through DOMPurify before insertion. Markdown's
  raw-HTML passthrough is therefore neutralized rather than trusted.
- Mermaid runs with `securityLevel: 'strict'`, which disables click handlers
  and HTML labels in diagrams.
- Mermaid's SVG output is sanitized too, not just the markdown HTML — it can
  contain `foreignObject`.
- No `eval`, no `innerHTML` of unsanitized strings, no model-authored
  `<script>`.

## Error handling

| Failure | Behaviour |
| --- | --- |
| Model returns invalid `RunReport` | pydantic-ai retries, as today |
| `body_md` is empty | Renders an empty report body; run is still `success`. An empty string is a valid document, not an error |
| Invalid Mermaid syntax | That fence stays a code block with a muted note; the rest of the report renders |
| Mermaid asset fails to load | All fences stay as code blocks; the report is still readable |
| Old run with no `body_md` | Raw JSON in a `<pre>` |
| `report.md` for such a run | 404 |

## Testing

**Harness constraint:** `tests/test_admin_ui.py` states it outright — there is
no browser available. The suite tests UI by parsing the rendered HTML, running
`node --check` over the inline JS, and replaying request sequences against the
live ASGI app. So rendered-DOM assertions (`<table>` appeared, `<svg>`
appeared) are not available, and the plan below does not pretend otherwise.
Everything that can be proven without a DOM is automated; the one thing that
cannot is called out as a manual step.

Tests are plain scripts with a local `check()` helper, run directly
(`python tests/test_job_runs.py`). No pytest.

### 1. `tests/test_job_runs.py` — the contract

Replace the `good`/`partial` `RunReport` fixtures (currently at ~:237) with a
single markdown fixture, and delete every `degraded` / missing-sections
assertion and the `expected_sections=` upsert arguments.

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
    check("no legacy keys", "sections" not in r.report and "overall_severity" not in r.report)
    check("no missing_sections recorded", not r.missing_sections)
```

The `no legacy keys` assertion is the one that matters: it fails if the old
schema is left half-removed.

### 2. `tests/test_admin_api.py` — the export endpoint

Delete the `expected_sections` round-trip assertions. Add, after the existing
`/admin/api/runs/{id}` block (~:739):

```python
print("\n== GET /admin/api/runs/{id}/report.md ==")
r = await client.get(f"/admin/api/runs/{run_id}/report.md")
check("200", r.status_code == 200, f"got {r.status_code}")
check("markdown content type",
      r.headers["content-type"].startswith("text/markdown"),
      r.headers.get("content-type"))
check("attachment filename",
      f'filename="run-{run_id}.md"' in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition"))
check("body is the markdown verbatim", r.text.startswith("# What's new"))

# A pre-change run: report present, but no body_md.
async with SessionLocal() as s:
    await finish_job_run(s, legacy_id, status="success", summary="old",
                         report={"summary": "old", "sections": []})
r = await client.get(f"/admin/api/runs/{legacy_id}/report.md")
check("legacy run 404s", r.status_code == 404, f"got {r.status_code}")

r = await client.get("/admin/api/runs/does-not-exist/report.md")
check("unknown run 404s", r.status_code == 404)
```

### 3. `tests/test_report_render.mjs` — new, the markdown pipeline

Node is available (v24) and `marked` is a pure string→string transform, so the
markdown half is genuinely testable without a DOM. Run with
`node tests/test_report_render.mjs`.

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
// proving sanitization works. Behaviour is covered by the manual step below.
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

### 4. `tests/test_admin_ui.py` — assets and template wiring

Structural checks in the existing style; `node --check` over the inline JS is
already part of this suite and covers the new code for free.

```python
print("\n== report rendering assets ==")
for name in ("marked.min.js", "purify.min.js", "mermaid.min.js"):
    check(f"{name} vendored", (ROOT / "static" / "vendor" / name).exists())

check("template loads marked", "/static/vendor/marked.min.js" in html)
check("template loads purify", "/static/vendor/purify.min.js" in html)
check("mermaid is NOT eagerly loaded",
      '<script src="/static/vendor/mermaid.min.js"' not in html)
check("expected-sections input removed", 'id="agent-expected-sections"' not in html)
check("download link present", "report.md" in html)
```

The `mermaid is NOT eagerly loaded` check is what keeps the lazy-load promise
honest — without it, a later edit could quietly add 1.1MB to every page load.

### 5. Manual verification (once, recorded in the commit)

The only thing no automated test here can prove is that sanitization and
Mermaid rendering actually behave in a browser. Load a run whose `body_md`
contains, deliberately, all four cases:

1. a markdown table → renders as a styled `<table>`
2. a valid `mermaid` fence → renders as an `<svg>`
3. a deliberately malformed `mermaid` fence → stays a code block with the
   muted "could not be rendered" note, and does not break case 2
4. `<script>window.__pwned = 1</script>` and an `<img onerror=...>` → neither
   executes; `window.__pwned` is undefined afterwards

### Known gap

Sanitization has no automated behavioural test, because DOMPurify requires a
DOM and the harness has none. Closing it means adding `jsdom` as a dev
dependency, which means a root `package.json` and `node_modules` in a project
that is otherwise pure Python plus a separate approuter. Not worth it for this
change; revisit if more model-authored content reaches the admin UI.

## Known trade-offs

**Mermaid is 3.5MB.** Larger than first estimated, which is why the lazy load
is a requirement rather than an optimisation, and why a test asserts it is not
eagerly loaded.

**`xychart-beta` is limited.** No stacked or grouped bars, coarse axis
control. Accepted because charts here are illustrative, and because the escape
hatch is cheap: since every chart is a fenced block inside one markdown
string, adding a `vega-lite` fence later touches only the renderer — not the
schema, the DB, or the agent's output type.

**Old runs become raw JSON.** Chosen deliberately over keeping a legacy
renderer. The acc run history stays retrievable but is no longer pretty.

**Dead columns remain** in `agent_configs` and `job_runs`, as above.

**Nothing enforces that a source was checked.** That was the point of
`expected_sections`, and this removes it. If a scenario later needs that
guarantee, it belongs in the agent's prompt and its skill, not in a schema
the model has to satisfy for every unrelated scenario.

**Some Mermaid diagram types can still lose a label.** `securityLevel: 'strict'`
and `htmlLabels: false` cover flowchart and class diagrams, but other diagram
types (mindmap, sequence diagrams with wrapped labels) may still emit
`foreignObject` internally, which the SVG sanitize profile strips whole. The
failure mode is an unlabeled shape, not a broken page or a sanitizer bypass —
accepted because it degrades to "read the fence" rather than anything unsafe.

**Sanitized markdown still permits `<img src="https://…">`.** DOMPurify's
default profile allows `img`, so a report derived from an untrusted MCP
server can cause the admin's browser to make an outbound request when the
image renders — a tracking pixel or read-receipt beacon. Accepted because the
spec deliberately renders images (screenshots, diagrams pasted as links are
common in these reports); the lever if that trust changes is adding `img` to
`FORBID_TAGS` in `renderReportBody`.

## Sequencing

1. Report model + runner (`reports.py`, `job_runner.py`) with tests — the
   contract change, independently verifiable.
2. Remove the checklist from API, DB and admin form, with tests.
3. Vendor assets; render markdown + tables in run detail.
4. Mermaid: lazy load, per-fence rendering, failure fallback.
5. `report.md` endpoint and Download link.
6. Markdown-pipeline and asset-wiring tests; the one-off manual
   browser verification of sanitization and Mermaid.
