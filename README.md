# SAP BTP Dynamic Multi-Agent

Multi-agent Pydantic AI application that manages SAP BTP. **Agents are
defined dynamically at runtime**: an administrator can create, edit, and
delete specialist agents through a web UI, point each one at a BTP-hosted
MCP server, and reload the orchestrator without redeploying. The chat UI
and admin UI are both protected by XSUAA.

> **About `docs/`:** the connector setup guides, the workflow design spec
> and the UI5/AI Core notes referenced from this README, `CLAUDE.md` and
> the module docstrings live in a `docs/` folder that is **intentionally
> not in this public repository**. They name real landscapes (CF orgs and
> spaces, approuter hosts, destination names, OAuth client ids), so
> `docs/` is gitignored and kept in the operator's local working copy next
> to the `.mtaext` files. If you are setting the app up elsewhere, ask the
> operator for that copy; the module docstrings under `agents/` carry the
> parts that are safe to publish.

## Architecture

```
User  -->  Approuter (XSUAA)  -->  FastAPI app
                                       |
                                       +-- /chat  --> Dynamic Orchestrator Agent
                                       |                |
                                       |                +-- Specialist A --> MCP server A
                                       |                +-- Specialist B --> MCP server B
                                       |                +-- ...
                                       |
                                       +-- /admin --> Agent CRUD + reload/restart
                                                       (scope: admin)
                                                       |
                                                       +-- PostgreSQL (config)
```

- **Dynamic registry**: Agent configs live in PostgreSQL. On startup and
  on every reload, the orchestrator and its delegation tools are rebuilt
  from the database.
- **JWT forwarding**: The approuter forwards the user's XSUAA JWT to the
  backend. A middleware binds the token to a contextvar which the MCP
  httpx client auth reads for every outgoing call — the MCP server sees
  the caller's own identity.
- **XSUAA-secured admin**: The `/admin` routes require the
  `$XSAPPNAME.admin` scope (role collection *Agent Administrator*, see
  `xs-security.json`).
- **Import / export**: Full configuration can be dumped as JSON and
  re-imported, either merging or fully replacing the current set.

## Prerequisites

- Python 3.11+ (`runtime.txt` deploys 3.13 on Cloud Foundry; the suites
  run on both)
- SAP AI Core service instance with access to Generative AI Hub
- Optional: a local PostgreSQL instance. Without one the app falls back
  to a SQLite file (`agents_registry.db`) in the project root.
- Node.js 20+ and npm: `mbt build` runs `npm ci && npm run build:ui5` for
  the UI5 admin app, and the JavaScript test suites need it too. Only the
  Python backend alone runs without it.

## Local development

### 1. Clone and set up a virtual environment

```bash
git clone <this-repo>
cd btp-multiagent-app
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your SAP AI Core credentials. You can use
either individual variables or a single service-key JSON blob — see
`.env.example` for both options.

Relevant variables:

| Variable             | Purpose                                                                                         |
|----------------------|-------------------------------------------------------------------------------------------------|
| `AICORE_*`           | SAP AI Core Generative AI Hub credentials (required).                                           |
| `DATABASE_URL`       | Optional. Async SQLAlchemy URL, e.g. `postgresql+asyncpg://user:pw@localhost:5432/agents`. Defaults to a local SQLite file. |
| `MCP_URL_ALLOWLIST`  | Optional. Comma-separated URL prefixes; restricts which MCP servers admins can register.        |
| `CALLBACK_PORT`      | Optional. Local OAuth2 callback port for interactive MCP login (default `3000`).                |
| `PORT`               | Optional. HTTP port (default `7932`).                                                           |

### 3. Run the app

```bash
python app.py
```

Open:

- **Chat:** <http://127.0.0.1:7932/chat>
- **Admin:** <http://127.0.0.1:7932/admin>
- **Health check:** <http://127.0.0.1:7932/healthz>

### UI5 admin (preview)

A SAPUI5 rebuild of the admin UI is available at `/ui5admin`. It is **not** a
replacement yet — `/admin` remains the supported admin interface. See
`docs/UI5_ADMIN.md` (local, not in repo) and `ui5-admin/` in the tree below.

On first startup the database is empty, so the app imports
[`agents.seed.json`](./agents.seed.json) which contains the Cloud Foundry,
BTP platform, and audit log agents from the previous hard-coded setup.
You can also re-import this file at any time via the admin UI's
**Import config** button.

> **Local auth:** XSUAA validation is skipped when no `VCAP_SERVICES`
> binding is present, so everyone is treated as an admin. Do not
> expose the local server publicly. If you want to simulate an
> XSUAA-protected admin, run behind a local approuter or put a
> reverse proxy in front that injects a Bearer token.

> **MCP login locally:** Without a `VCAP_APPLICATION` binding the MCP
> clients use the interactive **authorization_code** flow. On first
> use, each MCP server opens a browser window pointing at
> <http://localhost:3000/callback>; authorize in the browser and
> return to the chat. Tokens are cached in `.tokens-<agent>.json` in
> the project root.

### 4. First steps in the UI

1. Open <http://127.0.0.1:7932/admin>.
2. You should see the seeded agent(s).
3. Click **+ New agent**, fill in name / description / instructions /
   MCP URL, save.
4. Optionally define reusable **skills** in the Skills panel (name,
   description, content) and attach them to agents from the agent's
   edit dialog. Attached skills are listed in the specialist's system
   prompt and the full content is loaded on demand through a
   `load_skill` tool, so large instructions don't inflate every request.
5. Click **Reload agents** — the orchestrator is rebuilt in place and
   the new specialist is available in the chat without restarting.
   **Saving an agent, skill or workflow never rebuilds anything on its
   own**: after every edit that should reach the chat or the next
   scheduled run (a changed instruction, a toolset's send switch, a new
   peer) press **Reload** (in the UI5 admin: Settings → Reload, or
   `POST /admin/api/reload`).
6. Use **Export config** to download a JSON snapshot, or **Import
   config** to load a saved configuration (merge or replace). Skills
   are included in exports and imported before agents.

### 5. Running the tests

None of the suites needs an external service: the Python ones boot the
real FastAPI app over an ASGI transport with SAP AI Core and MCP stubbed,
and store state in throwaway SQLite files. `.github/workflows/ci.yml`
runs everything below except the Playwright scripts.

```bash
# Python unit + API tests: registry, auth, OAuth2, connectors (Gmail,
# Outlook, Teams, Slack, Jira, SAP notes), workflows, A2A, admin API.
# pytest.ini sets asyncio_mode=auto and testpaths=tests, so no flags.
pip install pytest pytest-asyncio
pytest

# Admin template contract test for templates/admin.html (HTML structure,
# `node --check` on the inline JS, every fetch() replayed against the app).
python tests/test_admin_ui.py

# Run-report rendering + DOMPurify sanitising, on jsdom (root package.json).
npm ci
node tests/test_report_render.mjs
node tests/test_report_sanitize.mjs

# UI5 admin: TypeScript check, then QUnit units + OPA5 journeys in
# headless Chrome via karma (ui5-admin/karma.conf.js).
npm --prefix ui5-admin ci
npm --prefix ui5-admin test

# UI5 admin end-to-end against a real backend on an isolated database
# (Playwright; see ui5-admin/playwright.config.ts).
npm --prefix ui5-admin run test:e2e

# Chat UI scripts (progress panel, heartbeat, OAuth auto-continue). Not in
# CI: templates/chat.html loads the pydantic-ai chat bundle from
# cdn.jsdelivr.net, so they need a browser with CDN access and a running
# local server.
python tests/test_chat_progress_ui.py
python tests/test_chat_heartbeat_ui.py
python tests/test_chat_oauth_autocontinue_ui.py
```

`ruff check` (`ruff.toml`: E, F, I at 100 columns) is advisory for now;
CI runs it without failing the build until the pre-existing findings
are worked off.

### 6. Resetting state

- Delete `agents_registry.db` to wipe the SQLite registry and trigger
  a fresh seed on next start.
- Delete `.tokens-*.json` files to force a fresh OAuth handshake with
  the MCP servers.

## Deploy to SAP BTP Cloud Foundry

### Build

```bash
mbt build
cf deploy mta_archives/pydantic-agent_<version>.mtar   # <version> is `version:` in mta.yaml
```

`mbt build` needs Node.js: the `ui5-admin` module is built with
`npm ci && npm run build:ui5` and uploaded to the HTML5 Application
Repository. Landscape-specific values (routes, the AI Core resource
group, `PUBLIC_BASE_URL`/`A2A_PUBLIC_URL`, `A2A_AGENT_VERSION`) go in a
gitignored `<landscape>.mtaext` passed to `cf deploy -e`.

This creates/binds:

| Resource            | Service            | Purpose                     |
|---------------------|--------------------|-----------------------------|
| `aicore-service`    | `aicore`           | LLM via Generative AI Hub   |
| `uaa-service`       | `xsuaa`            | Auth for chat + admin       |
| `agent-registry-db` | `postgresql-db`    | Dynamic agent configuration |

After deploy, assign the **Agent Administrator** role collection to your
user in the BTP cockpit (chat-only users get **Agent User**; the Joule
technical user gets **Agent A2A Client**), then open `/admin` on the
approuter URL.

> **`xs-security.json` redirect URIs are landscape pins.** The
> `oauth2-configuration.redirect-uris` list names the CF domains this app
> has been deployed to; XSUAA refuses the login redirect for any other
> domain. Deploying to a new landscape means adding its
> `https://*.cfapps.<landscape>.hana.ondemand.com/**` there (or overriding
> the whole list in your `.mtaext`); the existing entries are kept because
> removing one breaks login on that landscape.

### Optional: CF API restart

The admin UI exposes a **Restart app** button that performs an in-memory
reload of the orchestrator and, if configured, also triggers a real CF
app restart via the CF API. To enable the CF API restart, bind a
user-provided service called `cf-api` with credentials
`{"username": "...", "password": "..."}` for a technical user that has
the SpaceDeveloper role on this space. If not configured, the in-memory
reload alone is sufficient for newly added agents to take effect.

### MCP URL allow-list

By default, only HTTPS URLs ending in `*.hana.ondemand.com` may be
registered as MCP servers. Set the `MCP_URL_ALLOWLIST` env var (in
`mta.yaml` or `cf set-env`) to a comma-separated list of URL prefixes
for tighter control.

### MCP authentication modes

Each MCP server or built-in toolset is registered with one of six auth
modes (`AgentConfig.auth_mode`, see `agents/db.py`):

| Mode          | When to use                                                                 | How the call authenticates                                              |
|---------------|------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| `jwt`         | BTP servers that **trust this app's XSUAA** (same subaccount / granted scope) | This app's XSUAA JWT is forwarded on every request                      |
| `oauth2`      | Servers with their **own** authorization server; `builtin:gmail`, `builtin:outlook`, `builtin:teams` as the signed-in user | Per-user OAuth2 authorization_code (sign in once; tokens stored per user) |
| `none`        | Public servers and `builtin:sapnotes` (NVD)                                  | No token is sent                                                        |
| `app_only`    | `builtin:outlook` / `builtin:teams` as the application (service mailbox, read-only Teams) | OAuth2 client_credentials with the registration's own secret; `mailbox` names the target |
| `destination` | Every built-in (`builtin:jira` and `builtin:slack` only take this mode); app-level or **as the signed-in user** | The BTP destination named in the config holds the URL and credential; nothing is stored here. With `user_context` the destination service exchanges the user's JWT for the target's token -- see [Destinations for built-in connectors](#destinations-for-built-in-connectors) |
| `session`     | `builtin:sapnotedetail` (me.sap.com)                                         | A browser session cookie stored by an operator (`scripts/sap_session.py`) |

The UI5 admin's toolset dropdown offers only the modes each built-in can
actually run with (`ui5-admin/webapp/model/builtins.ts`).

#### Destinations for built-in connectors

Every built-in toolset can reach its API through a **BTP destination**
instead of holding a credential in this app. The server's config block is
then just `{"destination": "<name>", "user_context": true|false}` plus the
built-in's usual pinned keys (`mailbox`, `team`, `channels`, `lookback`,
`recipients`, `min_score`, ...). The destination supplies the URL -- the
API host itself, or a proxy such as API Management in front of it; the
config block can never change the host -- and the credential.

`user_context` decides **whose** credential that is:

| `user_context` | Who the tools act as | Destination `Authentication` types that work | Trust needed on the identity side |
|---|---|---|---|
| `false` (default) | The application. Same rules as `app_only`: `builtin:outlook`/`builtin:gmail` need `mailbox`, `builtin:teams` is read-only. Scheduled runs need this. | `OAuth2ClientCredentials`; `NoAuthentication` with a static `URL.headers.Authorization` (Slack's bot token); `NoAuthentication` with `URL.headers.Cookie` (`builtin:sapnotedetail`) or `URL.headers.apiKey` (`builtin:sapnotes`) | None beyond the app registration the destination's client id belongs to |
| `true` | **The signed-in user**: Graph `/me`, Gmail `users/me`, Teams posts under the user's name. Chat only -- scheduled and API-triggered runs carry no user token and fail with a clear error (no sign-in loop). | `OAuth2UserTokenExchange`, `OAuth2JWTBearer`, `OAuth2SAMLBearerAssertion` (`PrincipalPropagation` for on-premise via the Cloud Connector) | The target's identity provider must trust the token the destination service presents: for Microsoft Graph, a federation or an Entra ID app that accepts the IAS/XSUAA-issued assertion (OAuth2SAMLBearerAssertion) or a JWT-bearer trust; for Google, a Workspace-side trust to IAS. In BTP, set up IAS as the subaccount's trust and configure the destination with the IdP's token service URL and the target audience; the destination's owner does this once |

**How the per-user flow works.** On every tool call the app resolves the
destination (`GET /destination-configuration/v1/destinations/{name}`) with
its own destination-service token. When `user_context` is on it also sends
the request-bound user JWT as the `X-user-token` header; the destination
service then performs the token exchange / assertion for that user and
returns an `authTokens` entry that is *that user's* token for the target.
Results are cached per principal (bounded, 256 entries, until the token
nears expiry) and separately from the app-level entry, so two users never
share a token. A 401 from the target drops that user's entry and retries
once. Implementation: `agents/destination.py` (resolver) and
`agents/destination_auth.py` (`DestinationAuth`, the httpx auth every
destination-backed built-in uses; Jira and Slack keep their own resolver
calls).

`GET /admin/api/credential-health` lists every destination-mode server
under `destinations`, resolved with the app token, as `resolvable`,
`error` (the service's message, never a header) or `unbound` (no
destination service binding), and warns when a `user_context` server sits
on an app-level `Authentication` type -- every user would share one
mailbox.

**Worked example: Outlook as the signed-in user (Graph, user token
exchange).**

1. In the subaccount, establish trust to your Identity Authentication (IAS)
   tenant and, in IAS, a corporate IdP / application trust that Entra ID
   accepts for an OAuth2 SAML bearer assertion (Entra ID application
   registered with the Graph delegated permissions `Mail.Read`,
   `Mail.ReadWrite`, `Mail.Send` as needed, admin consented).
2. Create the destination, e.g. `GRAPH_USER`: URL
   `https://graph.microsoft.com`, Authentication
   `OAuth2SAMLBearerAssertion` (or `OAuth2UserTokenExchange` when the
   target trusts XSUAA/IAS JWTs directly), Audience
   `https://graph.microsoft.com`, Token Service URL
   `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, client
   id/secret of the Entra app, and `scope` = `https://graph.microsoft.com/.default`.
3. In the admin UI add `builtin:outlook`, auth mode **BTP destination**,
   destination `GRAPH_USER`, **Act as signed-in user** on. No mailbox: the
   user's token names it. Turn **Sending** on only if replies may leave the
   mailbox.
4. Open the chat as a user: the first tool call resolves the destination
   with the user's JWT, and Graph answers for that user's Inbox.

The same server with **Act as signed-in user** off and destination
`GRAPH_APP` (`OAuth2ClientCredentials`, application permissions, an
Exchange application access policy narrowing it to the service mailbox)
plus `mailbox = service@example.com` is the unattended variant a scheduler
can run.

**Worked example: Slack as the bot (static header).** Slack has no
client-credentials grant, so the bot token lives in the destination: URL
`https://slack.com/api`, Authentication `NoAuthentication`, additional
property `URL.headers.Authorization` = `Bearer xoxb-...`. The
`builtin:slack` server names it in `destination`; nothing else is stored.
Step by step in `SLACK_SETUP.md`.

#### Per-user OAuth2 (`oauth2`)

Use this when a target system (e.g. an ABAP system) is protected by its
**own** authorization server and you don't want to couple it to this
app's XSUAA. Each end user signs in to the target once; the resulting
access/refresh tokens are stored **per user** in PostgreSQL and forwarded
on every MCP call, so the user's real identity reaches the target
(per-user authorizations, transport ownership, and audit are preserved).

There are two ways to obtain the OAuth client (admin UI → new/edit agent
→ MCP server → `OAuth2 (per-user)`):

**A. Auto-discover & register (DCR) — recommended.** Tick *"Auto-discover
& register (DCR)"* and fill in **nothing else** (scope optional). On first
use the app reads the MCP server's OAuth metadata
(`/.well-known/oauth-protected-resource` → authorization server →
`/.well-known/oauth-authorization-server`) and **registers itself**
(RFC 7591), caching the registered client for all users. The callback
redirect URI is registered automatically — no manual step on the target.
Works when the MCP server exposes its own OAuth (as most MCP servers,
including ABAP ones, do).

**B. Manual client.** Untick DCR and provide:

- **XSUAA / UAA URL** — the target's UAA `url` (authorize/token endpoints
  are derived as `<url>/oauth/authorize` and `<url>/oauth/token`), or set
  the **Authorize URL** / **Token URL** explicitly.
- **Client ID / Client secret** — from a service key of the target's XSUAA
  instance. The secret is stored in the DB, never returned by the API or
  included in exports (leave it blank on edit to keep the stored value).
- **Scope** — optional.

Use B when the target is fronted by **plain XSUAA**, which does not support
Dynamic Client Registration. With B you must register this app's callback
as a redirect URI on the target's XSUAA
(`oauth2-configuration.redirect-uris`):

```
https://<your-approuter-host>/oauth/callback
```

Either way, **no trust relationship between the two XSUAA instances is
needed** — the target only needs to allow the callback URL (automatically
in mode A, manually in mode B).

**Runtime:** the first time a user triggers an `oauth2` specialist, the
chat returns an **Authorize** link. The user opens it, signs in to the
target, is redirected back to `/oauth/callback` (which exchanges the code
for tokens via PKCE), and then re-sends the request. Tokens refresh
automatically; on a refresh failure the user is re-prompted.

> Exports redact OAuth client secrets. Re-importing into a fresh
> database therefore requires re-entering the secret in the admin UI.

## Project structure

```
.
├── app.py                      # FastAPI entry point, middleware, lifespan
├── agents/
│   ├── db.py                   # SQLAlchemy models (agents, skills, workflows, runs), VCAP postgres resolver
│   ├── auth.py                 # XSUAA JWT validation + JWT forward contextvar
│   ├── shared.py               # MCP factory (PerRunMCPServer), SAP AI Core model, JWTForwardAuth
│   ├── oauth2.py               # Per-user OAuth2 (PKCE, DCR discovery, token storage)
│   ├── oauth_routes.py         # GET /oauth/callback
│   ├── client_credentials.py   # app_only token client
│   ├── destination.py          # BTP destination service resolver (app-level and per-user)
│   ├── destination_auth.py     # httpx auth routing a built-in through a destination
│   ├── builtins.py             # builtin: pseudo-URL registry -> toolset factories
│   ├── gmail_tools.py          # builtin:gmail       (Gmail REST, oauth2)
│   ├── outlook_tools.py        # builtin:outlook     (Microsoft Graph, oauth2 / app_only)
│   ├── teams_tools.py          # builtin:teams       (Microsoft Graph, oauth2 / app_only)
│   ├── slack_tools.py          # builtin:slack       (Slack Web API via destination)
│   ├── jira_tools.py           # builtin:jira        (Jira REST via destination)
│   ├── sapnotes_tools.py       # builtin:sapnotes    (NVD, public)
│   ├── sapnotedetail_tools.py  # builtin:sapnotedetail (me.sap.com session cookie)
│   ├── lookback.py             # shared look-back window parser
│   ├── registry.py             # Dynamic orchestrator builder, peers, reload
│   ├── chat_app.py             # Dynamic ASGI wrapper around Agent.to_web()
│   ├── admin.py                # FastAPI /admin router (agent/skill/workflow CRUD, reload, import/export)
│   ├── api_runs.py             # Scheduler entry points: POST /api/agents|workflows/{slug}/run
│   ├── job_runner.py           # Scheduled single-agent runs + reports
│   ├── workflow_runner.py      # Workflow engine (main line, fan-out, branches, join)
│   ├── a2a.py                  # A2A server for SAP Joule (agent card + JSON-RPC)
│   └── cf_api.py               # CF API restart helper
├── templates/
│   ├── admin.html              # Admin UI (vanilla JS), served at /admin
│   └── chat.html               # Chat UI shell around the pydantic-ai chat bundle
├── ui5-admin/                  # SAPUI5 (TypeScript) admin, served at /ui5admin
│   ├── webapp/                 # Component, views, controllers, model/, service/
│   ├── webapp/test/            # QUnit units + OPA5 journeys (karma)
│   └── e2e/                    # Playwright end-to-end tests
├── approuter/                  # XSUAA-protected approuter (xs-app.json routes)
├── scripts/                    # Operator scripts: AI Core setup, model deployment, probes, sap_session.py
├── tests/                      # pytest suites, admin template test, jsdom report tests, chat UI scripts
├── .github/workflows/ci.yml    # CI: pytest, template test, jsdom tests, UI5 tsc + karma
├── agents.seed.json            # Initial agents to import on first startup
├── mta.yaml                    # MTA deployment descriptor (+ .mtaext per landscape, gitignored)
├── xs-security.json            # XSUAA scopes, role templates, role collections
├── JOULE_A2A.md                # Joule / A2A configuration guide
├── SLACK_SETUP.md              # Slack + BTP destination setup for builtin:slack
├── pytest.ini · ruff.toml      # Test and lint configuration
├── runtime.txt                 # Python 3.13 for the CF buildpack
└── requirements.txt            # Pinned Python dependencies
```

## Import / export format

```json
{
  "version": 1,
  "orchestrator_instructions": "You are an SAP BTP ... orchestrator. ...",
  "skills": [
    {
      "name": "cf-troubleshooting",
      "description": "How to diagnose failing Cloud Foundry apps.",
      "content": "1. Check recent logs ..."
    }
  ],
  "agents": [
    {
      "name": "cloudfoundry",
      "description": "Cloud Foundry operations ...",
      "instructions": "You are an SAP BTP Cloud Foundry specialist. ...",
      "mcp_url": "https://...hana.ondemand.com",
      "skills": ["cf-troubleshooting"],
      "enabled": true
    }
  ]
}
```

`POST /admin/api/import` accepts an additional top-level `"replace":
true` field to delete agents (and skills, when a `"skills"` section is
present) not in the payload (otherwise entries are upserted). Skills
are imported before agents so agents can reference them.

## Joule integration (A2A)

The orchestrator is also exposed as an **A2A (Agent-to-Agent)** endpoint
so it can be registered as a remote code-based agent in the **SAP Joule
Agent Hub**. The agent card is served at
`/.well-known/agent-card.json` and the JSON-RPC endpoint at `/a2a`.

See [**JOULE_A2A.md**](./JOULE_A2A.md) for the full configuration guide
covering BTP approuter routes, XSUAA scope/role-collection setup,
service-key creation, and how to register the agent in Joule.
