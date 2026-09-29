# SAP BTP Dynamic Multi-Agent

## Project overview
Multi-agent Pydantic AI application where specialist agents are defined
**dynamically at runtime** via an XSUAA-secured admin UI. An orchestrator
delegates to specialist agents that connect to BTP-hosted MCP servers
over OAuth 2.1. The user's XSUAA JWT is forwarded to each MCP server.
SAP AI Core's Generative AI Hub is the LLM provider.

> **`docs/` is not in this repository.** Every `docs/...` path mentioned
> below (connector setup guides, the workflow design spec, `UI5_ADMIN.md`,
> `AICORE_RESOURCE_GROUP.md`) is landscape-specific: it names real CF
> orgs and spaces, approuter hosts, destination names and OAuth client
> ids, so `docs/` is gitignored and lives only in the operator's local
> working copy (the same place as the `.mtaext` files). Do not create
> `docs/` files in the repo to satisfy a reference; ask for the local
> copy instead. Module docstrings carry the parts that are safe to
> publish.

## Architecture
- **LLM**: SAP AI Core Generative AI Hub (`sap-ai-sdk-gen`) via
  OpenAI-compatible API
- **Storage**: PostgreSQL (BTP `postgresql-db` service) via SQLAlchemy async
- **MCP**: Streamable HTTP with JWT forwarding (`JWTForwardAuth` in
  `agents/shared.py`) reading `agents.auth.current_jwt` per request
- **Framework**: FastAPI app combining admin router + mounted
  pydantic-ai chat (`DynamicChatApp` rebuilds on reload)
- **Auth**: Approuter forwards JWT; `agents/auth.py` validates against
  XSUAA JWKS; `require_admin` dependency enforces `$XSAPPNAME.admin` scope

## Key files
- `app.py` — FastAPI entry; middleware binds JWT; lifespan initializes
  DB, seeds from `agents.seed.json`, builds the initial registry
- `agents/db.py` — SQLAlchemy models (`AgentConfig`, `SkillConfig`,
  `OrchestratorConfig`), `init_db`, CRUD helpers, VCAP/ENV postgres URL
  resolver. `AgentConfig.auth_mode` is one of `jwt`, `none`, `oauth2`,
  `app_only`, `destination`, `session`. Skills are reusable instruction
  blocks attached to agents by name (`AgentConfig.skills_json`). Six more
  tables back the workflow engine: `Workflow`, `WorkflowBranch`,
  `WorkflowStep` (the definition), and `WorkflowRun`, `WorkflowItemRun`,
  `WorkflowStepRun` (what happened on a run). `validate_workflow_parts` is
  the save-time gate that rejects a definition that cannot run
- `agents/auth.py` — `current_jwt`/`current_principal`/`current_base_url`
  contextvars, `principal_from_token`, `XsuaaValidator`,
  `require_user`/`require_admin` FastAPI dependencies
- `agents/shared.py` — `JWTForwardAuth`, `create_mcp_server` (JWT forward
  on CF / browser OAuth locally / per-user `oauth2`), `SAPAICoreModel`.
  `create_mcp_server` returns a `PerRunMCPServer`: the registry shares one
  server object across all users, so each agent run must open its own MCP
  session, or overlapping runs send requests with whichever user opened the
  shared session (`tests/test_mcp_user_isolation.py`)
- `agents/oauth2.py` — per-user OAuth2 authorization_code for
  `auth_mode="oauth2"`: `PerUserOAuth2Auth` (httpx auth that attaches/
  refreshes the user's token, raises `OAuthAuthorizationRequired`),
  `begin_authorization`/`complete_authorization` (PKCE + state),
  `resolve_config`/`_discover_and_register` (RFC 8414/9728/7591 discovery +
  Dynamic Client Registration when the server `oauth` is `{dcr: true}`).
  Tokens in `mcp_oauth_tokens`, flow state in `mcp_oauth_states`, registered
  DCR clients in `mcp_oauth_clients`
- `agents/oauth_routes.py` — `GET /oauth/callback` completes the flow
- `agents/builtins.py` — registry of `builtin:` pseudo-URLs and the factory
  that turns one into a toolset. The set is closed; an unknown `builtin:` URL
  is rejected at admin validation
- `agents/gmail_tools.py` — in-process Gmail tools over the REST API,
  attached when an agent lists the pseudo-URL `builtin:gmail` instead of an
  MCP endpoint. Google's hosted Gmail MCP server refuses every `tools/call`
  from a self-registered OAuth client; see `docs/GMAIL_SETUP.md` (local,
  not in repo). Auth reuses
  `PerUserOAuth2Auth`, so sign-in and refresh are unchanged. On a
  `destination`, `GmailClient(mailbox=)` switches `users/me` to
  `users/{mailbox}` for the app-level case
- `agents/outlook_tools.py` — the same idea over Microsoft Graph
  (`builtin:outlook`), with an Inbox subfolder as the queue instead of a
  label. `build_http_client` picks per-user, app-only or destination auth;
  `teams_tools` reuses it. Built and unit-tested but **never run against a
  real mailbox**: `docs/OUTLOOK_SETUP.md` and `scripts/probe_outlook.py`
  cover the tenant gates that have to clear first
- `agents/mail_render.py` — markdown subset → Outlook-safe HTML for the mail
  this app *originates* (`send_mail`, `create_mail_draft`). Nested tables and
  inline styles only: Outlook on Windows lays mail out with Word's engine.
  The subject's ` -- ` tail becomes the header subline, the opening paragraph
  becomes the verdict callout, and the tool's `status` (`ok`/`attention`)
  tints it. Replies stay plain text — a report shell on an answer to a person
  would read as a newsletter
- `agents/teams_tools.py` — Teams channels over Microsoft Graph
  (`builtin:teams`). One `team` (and optionally `channels`) is pinned in
  config, never a tool argument. `oauth2` reads and, with `allow_send`,
  posts as the signed-in user; `app_only` is read-only because Graph refuses
  application posts; `destination` follows `user_context` (posting only as
  the user). Unit-tested only; setup notes are in the module docstring
- `agents/slack_tools.py` — Slack over the Web API (`builtin:slack`), as a
  bot. Slack has no client-credentials grant, so the `xoxb-` token lives in a
  BTP destination (NoAuthentication + `URL.headers.Authorization`, which
  `agents/destination.py` sends as a static header). Scope is the channels the
  bot is in, or a pinned `channels` list; posting needs `allow_send`, and
  posted text is escaped so it cannot `<!channel>` or mention anyone.
  Unit-tested only; Slack + BTP setup guide in `SLACK_SETUP.md`
- `agents/destination.py` — resolves a BTP destination (URL + ready
  `Authorization` header) from the destination service, cached until its
  token nears expiry. Stores no credential for the target: the destination
  holds it. Binding comes from `VCAP_SERVICES` or `DESTINATION_*` env vars.
  `resolve(user_token=, principal=)` sends the user's JWT as `X-user-token`
  so a user-propagating destination (OAuth2UserTokenExchange, OAuth2JWTBearer,
  OAuth2SAMLBearerAssertion, PrincipalPropagation) returns that user's token;
  per-user results live in a bounded LRU keyed by principal, apart from the
  app-level entry. `Destination.auth_type` echoes the `Authentication` type
  for diagnostics; `require_credential=False` accepts a bare-URL destination
  (public targets)
- `agents/destination_auth.py` — `DestinationAuth` (httpx auth) and
  `destination_http_client`: a built-in issues requests against
  `PLACEHOLDER_BASE` (`https://destination.invalid`), the auth resolves the
  destination per request (as the signed-in user when `user_context`),
  rewrites the URL onto the destination's, sets its headers, retries once on
  401, and refuses to send the credential to a host the destination did not
  name. `DestinationUserRequired` (a `DestinationError`, deliberately not
  `OAuthAuthorizationRequired`) is raised when `user_context` is on and no
  JWT is bound -- scheduled runs. Every built-in accepts
  `auth_mode="destination"` with `{destination, user_context}` plus its pinned
  keys; `user_context=false` keeps the app-only rules (mailbox required,
  Teams read-only). Storage keys per built-in are `_DEST_KEYS_BY_URL` in
  `agents/db.py`; save-time rules are `_validate_destination_config` in
  `agents/admin.py`; `GET /admin/api/credential-health` reports
  destination servers under `destinations`
- `agents/jira_tools.py` — in-process Jira tools over REST v2
  (`builtin:jira`), reached through a destination. JQL is built server-side
  from pinned `project`/`status` and a `lookback` ceiling; issues this
  account already commented on are skipped, so runs are repeatable.
  `add_comment` is registered only when `allow_comment` is set.
  See `docs/JIRA_SETUP.md`
- `agents/lookback.py` — the shared `parse_lookback` window parser
- `agents/sapnotes_tools.py` — SAP security notes discovered through the
  public NVD API (`builtin:sapnotes`). `sourceIdentifier=cna@sap.com` is
  pinned in code; the CVSS floor is the one configurable knob. Returns the
  whole backlog by default, because SAP re-releases notes without NVD
  re-publishing the CVE. An optional `destination` supplies a proxy URL and
  a `URL.headers.apiKey` header in place of `NVD_API_KEY`. See
  `docs/SAP_SECURITY_NOTES.md`
- `agents/sapnotedetail_tools.py` — SAP note detail from the private
  `me.sap.com` backend (`builtin:sapnotedetail`), giving the support-package
  level that fixes each note. Authenticates with a browser session cookie
  under `auth_mode="session"`, stored in `mcp_oauth_tokens` and refreshed by
  hand with `scripts/sap_session.py`; Playwright stays off the platform.
  Under `auth_mode="destination"` the cookie moves into the destination's
  `URL.headers.Cookie` property and is refreshed there instead. See
  `docs/SAP_NOTE_DETAIL.md`
- `agents/registry.py` — `build_orchestrator` dynamically constructs the
  orchestrator + delegation tools + specialists from the DB; `Registry`
  singleton with `reload()` for atomic swaps. Attached skills are listed
  (name + description) in the specialist's system prompt; full content is
  served on demand via a per-specialist `load_skill` tool. Agents may list
  `peers` (other agents' names); a second build pass attaches a delegation
  tool per peer to that agent, so a chain can run specialist to specialist
  instead of through the orchestrator. Recursion is bounded by
  `AGENT_DELEGATION_MAX_DEPTH` and a re-entry guard. `AgentConfig.model_name`
  overrides the globally active model per agent
- `agents/deep.py` — opt-in "deep agent" tools per specialist
  (`AgentConfig.deep_json`, parsed by `DeepConfig`): `write_todos`/
  `read_todos`, an in-memory per-run scratchpad (`ls`/`read_file`/
  `write_file`/`edit_file`, capped at 200 files / 256 KB / 4 MB) and a
  `task` tool that runs an ephemeral sub-agent with the parent's toolsets.
  State is one `DeepState` per `RunContext.run_id` in a TTL table; a
  sub-agent gets the parent's state as `deps`, so the two share a plan and
  scratchpad while a peer reached by delegation does not. `task` is omitted
  once `depth >= subagent_max_depth`; concurrency is a semaphore per state
  and depth. `registry.build_orchestrator` appends `deep_instructions` and
  the `deep_toolset` when the row's config is enabled; sub-agents are never
  registered as specialists or peers
- `agents/chat_app.py` — `DynamicChatApp` ASGI wrapper that forwards to
  the current `Agent.to_web()` and is rebuilt on reload
- `agents/workflow_runner.py` — runs a workflow: the declared main line, a
  fan-out step returning `WorkItem`s, and per-item branches the fan-out step
  selects. Mirrors `job_runner.py` (task set, start lock, never-raising
  `_finalize`, shutdown cancel). Steps hand plain text to each other; the join
  step sees one `## From` block per branch taken. See
  `docs/superpowers/specs/2026-08-31-agent-workflows-design.md`
- `agents/step_kinds.py` — the deterministic step kinds a `WorkflowStep.kind`
  can name besides `agent`: `condition` (first matching rule wins; `stop`
  ends the main line successfully, skips the rest of a branch for one item,
  or skips the rest of the join), `transform` (extract_json → regex →
  template → truncate), `http` (a BTP destination via `agents.destination`,
  confined relative path) and `python` (admin-authored code in a
  `python -I -S -E` subprocess started by `agents/_python_step_runner.py`:
  import allowlist, no `open`, empty environment, temp cwd, RLIMIT_AS/CPU,
  killed on timeout — a guard against mistakes, not against a hostile
  admin). Pydantic config models double as the save-time gate
  (`validate_step_config`, called from `validate_workflow_parts`); the
  runner calls `execute_step` and hands the output on as `<kind>#<position>`.
  Templates know `{{text}}`, `{{item.x}}`, `{{json.x}}`, `{{source.NAME}}`
  and are substituted, never evaluated. Mirrored in the UI by
  `ui5-admin/webapp/model/stepKinds.ts` and the step editors in both admins
- `agents/admin.py` — FastAPI `/admin` router: agent + skill + workflow CRUD,
  reload, restart, import/export, seed-on-startup
- `agents/a2a.py` — A2A (Agent-to-Agent) protocol server: agent card at
  `/.well-known/agent-card.json`, JSON-RPC at `/a2a` (`message/send`,
  `message/stream`, `tasks/get`, `tasks/cancel`). Used by SAP Joule.
- `agents/cf_api.py` — CF v3 API restart helper (optional, password grant)
- `templates/admin.html` — Admin UI (single-page, vanilla JS)
- `ui5-admin/` — SAPUI5 (TypeScript) rebuild of the admin UI, deployed to the
  BTP HTML5 Application Repository and served at `/ui5admin`. Runs **alongside**
  `templates/admin.html`, which is unchanged and still the supported admin at
  `/admin`. All HTTP goes through `webapp/service/AdminService.ts`; see
  `docs/UI5_ADMIN.md`. The server dialog's toolset dropdown comes from
  `webapp/model/builtins.ts`, which mirrors `agents/builtins.py` and lists the
  auth modes the server accepts per built-in
- `agents.seed.json` — Initial config imported when DB is empty
- `mta.yaml` — adds `postgresql-db` resource; version 2.1.0 adds
  A2A env vars (`A2A_PUBLIC_URL`, `A2A_AGENT_NAME`, …); 2.7.0 makes the
  AI Core resource group the `aicore-resource-group` parameter (override
  per landscape in an `.mtaext`) and adds a `before-start` hook running
  `scripts/ensure_aicore_setup.py`. Needs `_schema-version: "3.2"` for
  `hooks`
- `scripts/ensure_aicore_setup.py` — creates the `AICORE_RESOURCE_GROUP`
  resource group if missing, idempotent, no-op for `default`. Creates the
  group only; model deployments stay in `scripts/deploy_claude.py` because
  they bill and take minutes. Deployments are per group, so a fresh group
  has no models until that script runs against it. See
  `docs/AICORE_RESOURCE_GROUP.md`
- `xs-security.json` — `admin`, `user`, and `a2a` scopes with matching
  role templates and role collections
- `approuter/xs-app.json` — `/admin` requires admin scope, `/a2a`
  requires `a2a` scope, `/.well-known/agent-card.json` is anonymous
- `JOULE_A2A.md` — configuration guide for BTP + Joule Agent Hub

## Runtime flow
1. Lifespan: `init_db()` → `seed_from_file_if_empty(SEED_FILE)` →
   `registry.reload()` → `dynamic_chat_app.refresh()`
2. Request: `JWTBindingMiddleware` extracts bearer token → sets
   `current_jwt` contextvar → downstream code (chat → orchestrator →
   specialist → MCP httpx client) inherits the token via contextvar
   propagation across asyncio tasks
3. Admin reload: `POST /admin/api/reload` → `registry.reload()` rebuilds
   orchestrator from DB → `dynamic_chat_app.refresh()` swaps the ASGI
   inner app → next chat request gets the new agents
4. `POST /api/workflows/{slug}/run` is the second scheduler entry point
   alongside `POST /api/agents/{slug}/run`: both acknowledge within the BTP
   Job Scheduling Service's 15s synchronous budget and run in the background

## Running locally
```bash
pip install -r requirements.txt
cp .env.example .env  # AICORE_* + (optional) DATABASE_URL
python app.py
# Chat:  http://127.0.0.1:7932/chat
# Admin: http://127.0.0.1:7932/admin  (no XSUAA locally → open access)
```

Local falls back to SQLite if no `DATABASE_URL` is set.

## Dependencies
All pinned in `requirements.txt` to the versions the suites last ran on;
bump a pin, rerun the suites, then deploy.
- `pydantic-ai[mcp,web,openai,bedrock]`, `sap-ai-sdk-gen[all]`, `mcp`,
  `httpx`, `uvicorn`, `python-dotenv` (pip warns that the `pydantic-ai`
  meta package declares none of those extras; the imports work because it
  pulls them in anyway)
- `fastapi`, `jinja2`, `python-multipart` — admin UI
- `sqlalchemy[asyncio]`, `asyncpg` (Postgres on CF), `aiosqlite` (the
  local SQLite fallback) — dynamic agent storage
- `pyjwt[crypto]` — XSUAA JWT validation
- Test-only: `pytest`, `pytest-asyncio` (`pytest.ini` sets
  `asyncio_mode = auto`), `jsdom` via the root `package.json`, `ruff`
  (`ruff.toml`, advisory in CI)
- `@ui5/cli`, `ui5-tooling-transpile`, `@sapui5/types`, `karma-ui5`,
  `@playwright/test` — UI5 admin app (dev-only; not in `requirements.txt`)
