# UI5 Admin Application — Design

**Date:** 2026-08-24
**Status:** Awaiting review

## Goal

Rebuild the admin UI as a SAPUI5 (TypeScript) application deployed to the SAP
BTP HTML5 Application Repository, served both by the existing standalone
approuter and — later, with no rebuild — by SAP Build Work Zone.

The existing `templates/admin.html` stays in place, unchanged, for the entire
duration of this work. The UI5 app is reached at a separate path. Cutover is a
later, separate decision (see "Explicitly out of scope").

## Background and constraints

Established by reading the code and the SAP BTP documentation, not assumed.

**The admin backend is plain REST-JSON, not OData.** `agents/admin.py` exposes
~25 endpoints under `/admin/api/*`, all gated on `Depends(require_admin)`
(`$XSAPPNAME.admin` scope). There is no OData service and none is planned, so
Fiori Elements and `sap.ui.core.util.MockServer` are both inapplicable. The app
is freestyle UI5 over `fetch`.

**The current admin is one 1130-line vanilla-JS page.** It covers two tabs —
Configuration (Agents, Skills, LLM model, Orchestrator instructions) and Job
Runs (list, detail, markdown report). Run reports are rendered client-side with
`marked`, `DOMPurify` and `mermaid`, already vendored in `static/vendor/`.

**The approuter is pure destination routing.** `approuter/xs-app.json` has no
local `resources` folder and serves no static content of its own; every route
targets the `pydantic-agent-backend` destination with `forwardAuthToken: true`.
This matters because of the constraint below.

**A standalone approuter bound to `html5-apps-repo` retrieves both static
content *and* each app's `xs-app.json` from the repository**
([SAP Help: Integration with HTML5 Application Repository](https://help.sap.com/docs/BTP/65de2977205c403bbc107264b8eccf4b/1e0424b4e1d8441ebb245c4d1e6bb0e5.html)).
Two consequences follow, and the whole design rests on them:

1. The app's own `xs-app.json` governs its routes in **both** hosting models —
   standalone approuter and Work Zone's managed approuter. One file, no
   per-host branching in the application.
2. The same document states: *"A mixed scenario of modeling part of the static
   content in a local resources folder and also retrieving static content from
   HTML5 Application Repository is not supported."* The current approuter
   serves no local static content, so this costs nothing today — but it
   permanently forecloses that option for this approuter.

**Some backend URLs are not under the app path.** The credentials endpoint
returns `/oauth/login?agent=…&server=…` links, and the OAuth callback is
`PUBLIC_BASE_URL/oauth/callback`. These live on the backend approuter host. A
relative link to them breaks as soon as the app is served from a Work Zone
site.

## Decisions

Recorded with the reasoning, since several were live options.

| Decision | Choice | Why |
|---|---|---|
| Hosting | Both standalone approuter *and* Work Zone-ready | User requirement. Costs almost nothing given the shared `xs-app.json`. |
| Packaging | New modules in the **existing** MTA | One artifact, one version, one deploy. No version skew between UI and API. |
| Language | TypeScript | The ~25 REST payload shapes — especially the `oauth` discriminated union — become compile-time checks. |
| Layout | `sap.tnt.ToolPage` side navigation | Scales beyond today's two tabs, gives deep links, and its header can be suppressed inside the Work Zone shell. |
| Scope | Full parity, built incrementally | Old admin remains the fallback throughout. |
| Testing | QUnit + OPA5 in CI, plus Playwright E2E, plus manual smoke | User requirement. |

Rejected: a **separate MTA** for the UI (independent lifecycle, but a second
`mta.yaml`, a second XSUAA reference, a subaccount destination from day one,
and version skew). Rejected: **serving UI5 as FastAPI static files** (not the
HTML5 repository, which was the explicit requirement) — though this is
effectively what `ui5 serve` gives us for local development anyway.

## 1. Deployment topology

`mta.yaml` goes to version 2.3.0 and gains:

**Module `ui5-admin`** — `type: html5`, `path: ui5-admin`, custom builder
running `npm ci` then `npm run build:ui5`, `supported-platforms: []`. Produces
`ui5-admin/dist`.

**Module `ui5-admin-deployer`** — `type: com.sap.application.content`,
requiring `ui5-admin-repo-host` with `artifacts: [ui5-admin-content.zip]`.
Uploads the built app into the repository.

**Resources** — `ui5-admin-repo-host` (`html5-apps-repo`, plan `app-host`) and
`ui5-admin-repo-runtime` (`html5-apps-repo`, plan `app-runtime`). The latter is
added to `pydantic-agent-approuter`'s `requires`.

The Python module's build ignore list gains `ui5-admin/`, so the backend
droplet does not carry UI sources.

## 2. Routing

**Approuter `xs-app.json`** gains one route, inserted **above** the existing
catch-all `^(.*)$` — the catch-all would otherwise swallow it:

```json
{ "source": "^/ui5admin/(.*)$", "target": "/cominfrabelagentadmin/$1",
  "service": "html5-apps-repo-rt", "authenticationType": "xsuaa" }
```

The rewrite is not cosmetic. An app's name inside the HTML5 repository is
derived from its `manifest.json` `sap.app.id` with the dots removed, so
`com.infrabel.agentadmin` is stored as `cominfrabelagentadmin` and that is the
path segment `html5-apps-repo-rt` resolves against. Serving it at the friendlier
`/ui5admin` therefore requires the approuter to rewrite the prefix rather than
merely strip it.

The rewrite composes correctly with the relative backend calls: a page loaded
from `/ui5admin/index.html` issuing `backend/agents` requests
`/ui5admin/backend/agents`, which the approuter rewrites to
`/cominfrabelagentadmin/backend/agents`; the repository resolves the app, and
the app's own `xs-app.json` matches the remaining `/backend/agents` against its
destination route. Routes inside an app's `xs-app.json` are relative to that
app's root, so no further adjustment is needed.

A second route redirects the bare `/ui5admin` (no trailing slash) to
`/ui5admin/`, otherwise the relative-path resolution above silently targets the
wrong prefix.

**To verify during implementation:** the exact repository app-name derivation
(dot-stripping versus a `sap.cloud.service` prefix) is behaviour we are reading
from documentation, not from a deployed instance. The first deploy must confirm
the stored app name and, if it differs, the rewrite target changes to match.
Nothing else in the design depends on the specific value.

No `scope` on this route deliberately: scope enforcement lives in the app's own
`xs-app.json`, so Work Zone inherits identical protection without a second
place to keep in sync.

**The UI5 app's `xs-app.json`**, bundled into the build and read from the
repository by whichever approuter serves it:

```json
{
  "welcomeFile": "/index.html",
  "authenticationMethod": "route",
  "routes": [
    { "source": "^/backend/(.*)$", "target": "/admin/api/$1",
      "destination": "pydantic-agent-backend",
      "authenticationType": "xsuaa", "scope": "$XSAPPNAME.admin",
      "csrfProtection": false },
    { "source": "^(.*)$", "target": "$1",
      "service": "html5-apps-repo-rt", "authenticationType": "xsuaa",
      "scope": "$XSAPPNAME.admin" }
  ]
}
```

Every backend call from the app is a **relative** `backend/…`, resolving to
`/ui5admin/backend/…` standalone and `/<sap.cloud.service>.<appId>/backend/…`
in Work Zone. Both land on `/admin/api/…`. The application code never knows
which host it is running in.

`csrfProtection: false` matches the existing `/admin` route, whose API is
already exempt.

**Destination** — `pydantic-agent-backend` already exists as an MTA env
destination on the approuter with `forwardAuthToken: true`, so the standalone
case needs no new configuration. Work Zone will later require a subaccount
destination of the *same name*, plus a `sap.cloud.service` entry in the
manifest. That is the complete Work Zone delta — with one caveat: adding
`sap.cloud.service` may change how the app is addressed, so the approuter
rewrite target above must be re-verified at that point. It is a one-line change
if it moves, and it is deferred work regardless (see "Explicitly out of scope").

**`xs-security.json`** — unchanged. Same `$XSAPPNAME.admin` scope, same
`AgentAdmin` role template, no new role assignments for anyone.

## 3. Application architecture

```
ui5-admin/
  package.json           ui5-tooling, @sapui5/types, typescript, karma, playwright
  ui5.yaml               framework SAPUI5; libs sap.m, sap.tnt, sap.f, sap.ui.core
  ui5-local.yaml         dev proxy: /backend → http://127.0.0.1:7932/admin/api
  tsconfig.json
  xs-app.json
  webapp/
    manifest.json        sap.app.id: com.infrabel.agentadmin; routing config
    Component.ts
    index.html
    controller/          BaseController, App, Agents, AgentDetail, Skills,
                         SkillDetail, Runs, RunDetail, Settings
    view/                one .view.xml per controller
    fragment/            McpServerDialog, ImportDialog, ConfirmDialog
    service/             AdminService.ts, types.ts, ErrorHandler.ts
    model/               models.ts, formatter.ts
    vendor/              marked, DOMPurify, mermaid (copied from static/vendor)
    test/unit/           QUnit
    test/integration/    OPA5
  e2e/                   Playwright specs
```

**Navigation.** `sap.tnt.ToolPage` with side navigation (Agents / Skills / Job
Runs / Settings) over a routed content area. Routes: `#/agents`,
`#/agents/{id}`, `#/skills`, `#/skills/{name}`, `#/runs`, `#/runs/{runId}`,
`#/settings`. Deep links and browser back both work. The `ToolHeader` is hidden
when `sap.ushell` is present, so the Work Zone shell supplies the chrome
instead of stacking two headers.

**`AdminService.ts` is the only code that performs HTTP.** A single private
`#request<T>()` prefixes every path with the relative `backend/`, sets
`Accept: application/json`, and maps non-2xx responses onto a typed
`AdminError` carrying FastAPI's `detail`. Public methods are thin and typed:
`listAgents`, `getAgent`, `upsertAgent`, `deleteAgent`, `agentCredentials`,
`runNow`, `listRuns`, `getRun`, `runReportUrl`, `listSkills`, `getSkill`,
`upsertSkill`, `deleteSkill`, `getOrchestrator`, `setOrchestrator`, `getModel`,
`setModel`, `reload`, `restart`, `exportConfig`, `importConfig`, `whoami`,
`getConfig`. Controllers never call `fetch`. This is the seam QUnit mocks and
the single place any future base-path change would touch.

**`types.ts` mirrors the Pydantic payloads exactly** — `Agent`, `AgentInput`,
`McpServer`, `OAuthClient`, `Skill`, `JobRun`, `CredentialStatus`,
`ImportPayload`. `auth_mode` is the union `'jwt' | 'none' | 'oauth2'`. The
oauth configuration is a discriminated union — `{ dcr: true; scope?: string }`
versus the manual credential shape — which is exactly the distinction the
current JS gets wrong at runtime.

**Client-side validation mirrors the server; the server remains
authoritative.** The agent form pre-checks the rules encoded in
`McpServerPayload`: `https` required unless `auth_mode` is `none`, `builtin:`
URLs restricted to the known set, no duplicate URLs within an agent, and
`oauth2` requiring `client_id` plus either `uaa_url` or both `authorize_url`
and `token_url` (unless `dcr` is set). Admins get inline field errors instead
of a 422 toast. `MCP_URL_ALLOWLIST` is **not** duplicated client-side — it is
environment-dependent and belongs to the server alone; its rejections surface
through the normal 422 path.

**Three details requiring deliberate handling:**

1. **Run reports.** `RunDetail` reuses the vendored `marked` + `DOMPurify` +
   `mermaid`, bundled into the app rather than loaded from a CDN: Work Zone's
   CSP would block a CDN, and self-hosting preserves the exact sanitization
   behaviour already covered by `tests/test_report_render.mjs` and
   `tests/test_report_sanitize.mjs`. Rendered into an `sap.ui.core.HTML`
   control. The `.md` download links to the backend route directly.

2. **OAuth sign-in links.** `/oauth/login?…` and the callback host are outside
   the app path, so the credentials panel opens them as **absolute** URLs in a
   new tab, with the host obtained from a new endpoint (§4). A relative link
   would break under Work Zone.

3. **Run-as principal.** `GET /admin/api/whoami` exists because the run-as
   principal is an opaque XSUAA UUID nobody can recall or type. The agent form
   keeps that affordance — a "use my principal" action — alongside the live
   per-server credential status from `GET /admin/api/agents/{id}/credentials`,
   which is the check that prevents scheduled runs failing silently hours
   later.

**Error handling.** One `ErrorHandler` registered at Component level.
401/403 → a non-dismissable dialog stating the session expired, with a reload
action (the approuter re-authenticates on reload). 409 from `runNow` → an
informational message, not an error, since it means a run is already in
progress. Everything else → `MessageBox.error` carrying the server's `detail`.
No failure is swallowed.

## 4. Backend changes

Exactly one, and it is additive:

**`GET /admin/api/config`** → `{"public_base_url": "<string>"}`, gated on
`require_admin` like every sibling. Resolves `PUBLIC_BASE_URL`, falling back to
`A2A_PUBLIC_URL`, then to the request's forwarded host headers — the same
precedence `agents/oauth2.py` already applies when building `redirect_uri`.
The UI5 app uses it to construct absolute OAuth sign-in links.

`templates/admin.html` never calls it and is unaffected. No other backend file
changes.

## 5. Testing

**QUnit (unit).** `AdminService` against a stubbed `fetch`: correct paths and
verbs, 422 → typed `AdminError` with field detail, 409 from `runNow` classified
as informational. Formatters (run status → `sap.ui.core.ValueState`, durations,
timestamps). Client-side validators, table-driven from the Pydantic rules
above. Headless via `karma-ui5`.

**OPA5 (integration).** Journeys against a stubbed service layer — not a mock
OData server, which buys nothing for a REST-JSON backend:

- create an agent with two MCP servers and save it
- edit a skill and see it reflected on an agent that has it attached
- open a run and see the rendered markdown report
- trigger reload and observe the success state
- import a configuration and see the lists refresh
- submit an invalid agent and see inline field errors

These journeys are the parity criterion for the UI5 admin.

**Playwright (E2E).** The real stack, no stubs: FastAPI on `127.0.0.1:7932`
against SQLite with XSUAA absent (the existing local open-access mode), and
`ui5 serve` with `ui5-local.yaml` proxying `/backend` → `/admin/api`. Each spec
seeds via `POST /admin/api/import` and tears down afterwards, so specs are
independent and order-free. This layer catches what the other two structurally
cannot: the actual wire contract between the TypeScript types and the Pydantic
models.

**CI wiring.** `ui5-admin/package.json` gets `test` (karma: QUnit + OPA5) and
`test:e2e` (Playwright). The root `npm test` — today only the two report-render
Node tests — chains `ui5-admin`'s unit and integration run, so one command
covers both. Playwright stays a separate `npm run test:e2e` because it boots
servers. The Python suite runs unchanged; `GET /admin/api/config` is covered by
new `check()` assertions inside `tests/test_admin_api.py`, following this
repository's convention of standalone scripts run as `python tests/<name>.py`
(there is no pytest configuration here).

**Manual smoke** on the deployed BTP app before any cutover is proposed.

## 6. Coexistence

`templates/admin.html`, its `GET /admin` route, and the approuter's
`^/admin(/.*)?$` rule stay **byte-identical**. The old admin remains the path
to use in production for the duration of this work; keeping the file untouched
is what guarantees a clean fallback.

The UI5 app lives at `/ui5admin`, behind the same `$XSAPPNAME.admin` scope,
calling the same `/admin/api/*` endpoints against the same database. Both can
be open simultaneously in two tabs; there is no shared client state to
conflict.

No "try the new admin" link is added to `admin.html`, deliberately. The new app
is reached by typing `/ui5admin`.

## 7. Build hygiene

`.gitignore` and `mta.yaml`'s ignore lists gain `ui5-admin/node_modules/` and
`ui5-admin/dist/`. The Python module's ignore list gains `ui5-admin/`.

## Risks

**`mbt build` gains a Node dependency.** The build machine must be able to run
`npm ci` in `ui5-admin/`. This is normal for any UI5 MTA, but it is new here,
and a broken UI5 build will fail the **entire** MTA — including the backend
module, which currently cannot fail for front-end reasons. The escape hatch, if
this coupling proves painful, is the rejected separate-MTA approach, at the
cost of a second deploy and a subaccount destination.

**HTML5 repository entitlement.** The subaccount needs `html5-apps-repo`
entitlement with both `app-host` and `app-runtime` plans. To be verified before
implementation starts; if absent it is a request to the BTP administrator and a
hard blocker for deployment (though not for local development).

**Approuter loses the local-static option permanently.** Documented above; free
today, but irreversible for this approuter.

## Explicitly out of scope

Deferred by explicit decision, to be revisited only once the UI5 admin is
finished, tested and smoke-tested on BTP:

- Repointing `/admin` at the UI5 app
- Retiring `templates/admin.html`
- SAP Build Work Zone site configuration and the launchpad tile

The design leaves all three cheap: the first is an approuter route change, the
third is a manifest field plus a subaccount destination.
