# SAP BTP Dynamic Multi-Agent

## Project overview
Multi-agent Pydantic AI application where specialist agents are defined
**dynamically at runtime** via an XSUAA-secured admin UI. An orchestrator
delegates to specialist agents that connect to BTP-hosted MCP servers
over OAuth 2.1. The user's XSUAA JWT is forwarded to each MCP server.
SAP AI Core's Generative AI Hub is the LLM provider.

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
  `app_only`, `destination`. Skills are reusable instruction blocks attached
  to agents by name (`AgentConfig.skills_json`). Six more tables back the
  workflow engine: `Workflow`, `WorkflowBranch`, `WorkflowStep` (the
  definition), and `WorkflowRun`, `WorkflowItemRun`, `WorkflowStepRun` (what
  happened on a run). `validate_workflow_parts` is the save-time gate that
  rejects a definition that cannot run
- `agents/auth.py` — `current_jwt`/`current_principal`/`current_base_url`
  contextvars, `principal_from_token`, `XsuaaValidator`,
  `require_user`/`require_admin` FastAPI dependencies
- `agents/shared.py` — `JWTForwardAuth`, `create_mcp_server` (JWT forward
  on CF / browser OAuth locally / per-user `oauth2`), `SAPAICoreModel`
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
  from a self-registered OAuth client; see `docs/GMAIL_SETUP.md`. Auth reuses
  `PerUserOAuth2Auth`, so sign-in and refresh are unchanged
- `agents/outlook_tools.py` — the same idea over Microsoft Graph
  (`builtin:outlook`), with an Inbox subfolder as the queue instead of a
  label. Built and unit-tested but **never run against a real mailbox**:
  `docs/OUTLOOK_SETUP.md` and `scripts/probe_outlook.py` cover the tenant
  gates that have to clear first
- `agents/destination.py` — resolves a BTP destination (URL + ready
  `Authorization` header) from the destination service, cached until its
  token nears expiry. Stores no credential for the target: the destination
  holds it. Binding comes from `VCAP_SERVICES` or `DESTINATION_*` env vars
- `agents/jira_tools.py` — in-process Jira tools over REST v2
  (`builtin:jira`), reached through a destination. JQL is built server-side
  from pinned `project`/`status` and a `lookback` ceiling; issues this
  account already commented on are skipped, so runs are repeatable.
  `add_comment` is registered only when `allow_comment` is set.
  See `docs/JIRA_SETUP.md`
- `agents/lookback.py` — the shared `parse_lookback` window parser
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
- `agents/chat_app.py` — `DynamicChatApp` ASGI wrapper that forwards to
  the current `Agent.to_web()` and is rebuilt on reload
- `agents/workflow_runner.py` — runs a workflow: the declared main line, a
  fan-out step returning `WorkItem`s, and per-item branches the fan-out step
  selects. Mirrors `job_runner.py` (task set, start lock, never-raising
  `_finalize`, shutdown cancel). Steps hand plain text to each other; the join
  step sees one `## From` block per branch taken. See
  `docs/superpowers/specs/2026-08-31-agent-workflows-design.md`
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
  `docs/UI5_ADMIN.md`
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
- `pydantic-ai[mcp,web,openai]`, `sap-ai-sdk-gen[all]`, `mcp`, `httpx`,
  `uvicorn`, `python-dotenv`
- `fastapi`, `jinja2`, `python-multipart` — admin UI
- `sqlalchemy[asyncio]`, `asyncpg` — dynamic agent storage
- `pyjwt[crypto]` — XSUAA JWT validation
- `@ui5/cli`, `ui5-tooling-transpile`, `@sapui5/types`, `karma-ui5`,
  `@playwright/test` — UI5 admin app (dev-only; not in `requirements.txt`)
