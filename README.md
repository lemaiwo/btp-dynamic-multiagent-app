# SAP BTP Dynamic Multi-Agent

Multi-agent Pydantic AI application that manages SAP BTP. **Agents are
defined dynamically at runtime**: an administrator can create, edit, and
delete specialist agents through a web UI, point each one at a BTP-hosted
MCP server, and reload the orchestrator without redeploying. The chat UI
and admin UI are both protected by XSUAA.

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
  `$XSAPPNAME.admin` scope (role collection *Pydantic Agent Administrator*).
- **Import / export**: Full configuration can be dumped as JSON and
  re-imported, either merging or fully replacing the current set.

## Prerequisites

- Python 3.13+ (3.11 also works)
- SAP AI Core service instance with access to Generative AI Hub
- Optional: a local PostgreSQL instance. Without one the app falls back
  to a SQLite file (`agents_registry.db`) in the project root.
- Optional: Node.js (only required to run the UI test suite)

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
2. You should see the three seeded agents.
3. Click **+ New agent**, fill in name / description / instructions /
   MCP URL, save.
4. Click **Reload agents** — the orchestrator is rebuilt in place and
   the new specialist is available in the chat without restarting.
5. Use **Export config** to download a JSON snapshot, or **Import
   config** to load a saved configuration (merge or replace).

### 5. Running the tests

The repo ships with two self-contained test suites that boot the real
FastAPI app (stubbing SAP AI Core + MCP) over an ASGI transport — no
external services needed.

```bash
# Backend HTTP API tests (45 checks)
python tests/test_admin_api.py

# Admin UI tests (50 checks: HTML structure, JS syntax, fetch contract, flows)
python tests/test_admin_ui.py
```

The UI test uses `node --check` to syntax-check the inline JavaScript,
so Node.js must be on the `PATH` for that step (everything else
degrades gracefully).

### 6. Resetting state

- Delete `agents_registry.db` to wipe the SQLite registry and trigger
  a fresh seed on next start.
- Delete `.tokens-*.json` files to force a fresh OAuth handshake with
  the MCP servers.

## Deploy to SAP BTP Cloud Foundry

### Build

```bash
mbt build
cf deploy mta_archives/pydantic-agent_2.0.0.mtar
```

This creates/binds:

| Resource            | Service            | Purpose                     |
|---------------------|--------------------|-----------------------------|
| `aicore-service`    | `aicore`           | LLM via Generative AI Hub   |
| `uaa-service`       | `xsuaa`            | Auth for chat + admin       |
| `agent-registry-db` | `postgresql-db`    | Dynamic agent configuration |

After deploy, assign the **Pydantic Agent Administrator** role collection
to your user in the BTP cockpit, then open `/admin` on the approuter URL.

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

## Project structure

```
.
├── app.py                      # FastAPI entry point, middleware, lifespan
├── agents/
│   ├── db.py                   # SQLAlchemy models, VCAP postgres resolver
│   ├── auth.py                 # XSUAA JWT validation + JWT forward contextvar
│   ├── shared.py               # MCP factory, SAP AI Core model, JWTForwardAuth
│   ├── registry.py             # Dynamic orchestrator builder + reload
│   ├── chat_app.py             # Dynamic ASGI wrapper around Agent.to_web()
│   ├── admin.py                # FastAPI admin router (CRUD / reload / I/O)
│   └── cf_api.py               # CF API restart helper
├── templates/
│   └── admin.html              # Admin UI
├── approuter/                  # XSUAA-protected approuter
├── agents.seed.json            # Initial agents to import on first startup
├── mta.yaml                    # MTA deployment descriptor
├── xs-security.json            # XSUAA scopes + role collections
└── requirements.txt
```

## Import / export format

```json
{
  "version": 1,
  "orchestrator_instructions": "You are an SAP BTP ... orchestrator. ...",
  "agents": [
    {
      "name": "cloudfoundry",
      "description": "Cloud Foundry operations ...",
      "instructions": "You are an SAP BTP Cloud Foundry specialist. ...",
      "mcp_url": "https://...hana.ondemand.com",
      "enabled": true
    }
  ]
}
```

`POST /admin/api/import` accepts an additional top-level `"replace":
true` field to delete agents not present in the payload (otherwise
entries are upserted).
