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
- `agents/db.py` — SQLAlchemy models (`AgentConfig`, `OrchestratorConfig`),
  `init_db`, CRUD helpers, VCAP/ENV postgres URL resolver
- `agents/auth.py` — `current_jwt` contextvar, `XsuaaValidator`,
  `require_user`/`require_admin` FastAPI dependencies
- `agents/shared.py` — `JWTForwardAuth`, `create_mcp_server` (JWT forward
  on CF / browser OAuth locally), `SAPAICoreModel`
- `agents/registry.py` — `build_orchestrator` dynamically constructs the
  orchestrator + delegation tools + specialists from the DB; `Registry`
  singleton with `reload()` for atomic swaps
- `agents/chat_app.py` — `DynamicChatApp` ASGI wrapper that forwards to
  the current `Agent.to_web()` and is rebuilt on reload
- `agents/admin.py` — FastAPI `/admin` router: CRUD, reload, restart,
  import/export, seed-on-startup
- `agents/cf_api.py` — CF v3 API restart helper (optional, password grant)
- `templates/admin.html` — Admin UI (single-page, vanilla JS)
- `agents.seed.json` — Initial config imported when DB is empty
- `mta.yaml` — adds `postgresql-db` resource; bumps version to 2.0.0
- `xs-security.json` — adds `admin` scope, `AgentAdmin` role, admin role
  collection
- `approuter/xs-app.json` — routes `/admin` requires admin scope

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
