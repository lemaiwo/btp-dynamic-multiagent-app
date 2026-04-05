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

- Python 3.13+
- SAP AI Core service instance with access to Generative AI Hub
- PostgreSQL (local) or a BTP `postgresql-db` service instance

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in AICORE_* + DATABASE_URL
python app.py
```

Chat: <http://127.0.0.1:7932/chat> — Admin: <http://127.0.0.1:7932/admin>

Locally, XSUAA validation is skipped (no binding present), so everyone is
treated as an admin — do not expose the local server publicly. Locally
the MCP servers use an interactive authorization_code browser flow
instead of JWT forwarding.

### Seeding the initial agents

On first startup, if the database is empty, the app imports
[`agents.seed.json`](./agents.seed.json) which contains the Cloud Foundry,
BTP platform, and audit log agents from the previous hard-coded setup.
You can also import this file at any time via the admin UI's **Import
config** button.

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
