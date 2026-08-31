# UI5 Admin Application

A SAPUI5 (TypeScript) rebuild of the admin UI, deployed to the SAP BTP HTML5
Application Repository and served at `/ui5admin`.

The original `templates/admin.html` at `/admin` is unchanged and remains the
supported admin UI. The UI5 app runs alongside it against the same
`/admin/api/*` endpoints and the same database. Cutover has not happened and is
a separate decision.

**The two front-ends are independent and do not share code.** Each maintains
its own copy of the `AgentInput` shape -- `templates/admin.html`'s inline JS
and `ui5-admin/webapp/service/types.ts` -- and its own form. Adding a field to
one does not add it to the other. This has already caused two bugs: an
absent field silently wiped on save from whichever admin lacked it, and the
same field simply missing from the other admin's form. When `AgentInput`
gains a field on the backend, add it to *both* admins in the same change.

## Layout

- `ui5-admin/webapp/service/` — `AdminService` (the only code that performs
  HTTP), `types.ts` (mirrors of the Pydantic payloads), `ErrorHandler`,
  `ReportRenderer`
- `ui5-admin/webapp/controller/`, `view/`, `fragment/` — one controller per view
- `ui5-admin/webapp/model/` — formatters and client-side validators
- `ui5-admin/webapp/test/` — QUnit unit tests and OPA5 journeys
- `ui5-admin/e2e/` — Playwright specs against a live backend

## Running locally

```bash
.venv/Scripts/python.exe app.py    # backend on 127.0.0.1:7932
cd ui5-admin && npm install && npm start   # UI on localhost:8080
```

`ui5-local.yaml` proxies `/backend` to `http://127.0.0.1:7932/admin/api`, which
is the same relative path the deployed app uses. Local mode has no XSUAA, so the
admin API is open.

## Testing

```bash
npm test              # repo root: Node report tests + UI5 unit + OPA5
npm run test:e2e      # Playwright; boots both servers itself
```

## How routing works in both hosts

Every call the app makes is relative (`backend/...`). The app's own
`xs-app.json`, which the approuter reads *from the HTML5 repository*, rewrites
`/backend/*` onto `/admin/api/*` through the `pydantic-agent-backend`
destination. The same file governs under the standalone approuter and under a
Work Zone managed approuter, so one build serves both.

The standalone approuter additionally rewrites `/ui5admin/*` onto
`/cominfrabelagentadmin/*` — the repository derives an app's name from
`sap.app.id` with the dots removed, so the friendlier path needs a rewrite
rather than a strip.

The `/resources` routes exist because `ui5 build` never bundles the SAPUI5
framework — it ships only the app's own code. Without a route to the
framework's CDN or repository copy, the deployed app fetches `sap-ui-core.js`,
gets a 404, and never boots past a blank page.

**Both `/resources` routes pin the same SAPUI5 version, `1.120.50`, and they
have to.** The unversioned `https://ui5.sap.com/resources/…` path is not a
stable target: it serves whatever SAPUI5 shipped most recently. Pointing the
app at it means the framework silently changes under a build that was never
tested against it — and because these files are served with a multi-day
`Cache-Control`, a returning browser can end up holding *half* of one version
and half of another. Two things follow, and both were live defects on
2026-08-26:

- Keep `resolve: false` in `ui5.yaml`. With `resolve: true` the builder inlines
  SAPUI5's own modules into `Component-preload.js` (499 of 517 modules), so the
  app runs framework JS frozen at build time against framework CSS fetched at
  serve time. sap.tnt renamed `.sapTntNavLI` to `.sapTntNL` after 1.120, so
  every SideNavigation rule missed and the menu rendered as an unstyled
  bulleted `<ul>`; sap.m Dialogs clipped their first form row.
- Keep the two files' pins equal. The approuter serves `/ui5admin/*`, but the
  HTML5 repository serves `/cominfrabelagentadmin/*` using the app's *own*
  `xs-app.json`. When only one of them was pinned, the bug reproduced on one
  URL and not the other.

`tests/test_ui5_asset_versioning.mjs` asserts all of this — run it after
touching either `xs-app.json` or the `bundles:` block.

## Known constraints

- Binding `html5-apps-repo` to the approuter means it can never also serve
  static content from a local `resources` folder.
- `mbt build` now runs `npm ci` inside `ui5-admin/`; a broken UI5 build fails
  the whole MTA, backend module included.
- OAuth sign-in links must be absolute, built from `GET /admin/api/config`,
  because `/oauth/login` lives outside the app path.
