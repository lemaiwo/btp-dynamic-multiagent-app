# SAP BTP Management Agent

Multi-agent Pydantic AI application that manages SAP BTP through specialized agents. An orchestrator delegates to Cloud Foundry and BTP platform specialists, each connected to a dedicated MCP server via OAuth 2.1 over Streamable HTTP. Uses SAP AI Core Generative AI Hub as the LLM provider.

## Architecture

```
User  -->  Web Chat UI  -->  Orchestrator Agent
                                 |
                    +------------+------------+
                    |                         |
            Cloud Foundry Agent          BTP Agent
                    |                         |
              CF MCP Server            BTP MCP Server
```

## Prerequisites

- Python 3.13+
- SAP AI Core service instance with access to Generative AI Hub
- Access to the MCP servers (audit log, Cloud Foundry, BTP)

## Local Development

### 1. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
.venv\Scripts\activate      # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your SAP AI Core credentials. You can use either individual variables or a single service key JSON:

**Option A — Individual variables:**

```
AICORE_AUTH_URL=https://<instance>.authentication.<region>.hana.ondemand.com/oauth/token
AICORE_CLIENT_ID=your-client-id
AICORE_CLIENT_SECRET=your-client-secret
AICORE_BASE_URL=https://api.ai.<region>.cfapps.sap.hana.ondemand.com/v2
AICORE_RESOURCE_GROUP=default
```

**Option B — Service key JSON:**

```
AICORE_SERVICE_KEY={"clientid":"...","clientsecret":"...","url":"...","serviceurls":{"AI_API_URL":"..."}}
```

### 4. Run the application

```bash
python app.py
```

The web chat opens at **http://127.0.0.1:7932**. On first use, each MCP server triggers an OAuth2 browser flow — authorize in the browser and return to the chat.

## Deploy to SAP BTP Cloud Foundry

### Prerequisites

- [Cloud MTA Build Tool (mbt)](https://sap.github.io/cloud-mta-build-tool/) installed
- [CF CLI](https://docs.cloudfoundry.org/cf-cli/install-go-cli.html) with the [MTA plugin](https://github.com/cloudfoundry/multiapps-cli-plugin)
- Logged in to your CF space: `cf login`

### 1. Build the MTA archive

```bash
mbt build
```

This produces `mta_archives/pydantic-agent_1.0.0.mtar`.

### 2. Deploy

```bash
cf deploy mta_archives/pydantic-agent_1.0.0.mtar
```

This will:
- Create/bind an `aicore` service instance (extended plan)
- Push the Python application with the `python_buildpack`
- Set MCP server URLs as environment variables

### Environment variables on CF

The deployment sets these via `mta.yaml` properties:

| Variable | Description |
|---|---|
| `MCP_AUDITLOG_URL` | Audit log MCP server base URL |
| `MCP_CLOUDFOUNDRY_URL` | Cloud Foundry MCP server base URL |
| `MCP_BTP_URL` | BTP platform MCP server base URL |

SAP AI Core credentials are resolved automatically from the bound `aicore` service instance via `VCAP_SERVICES`.

### OAuth2 note

The local OAuth2 flow uses a browser redirect to `localhost:3000/callback`, which does not work on Cloud Foundry. For production deployment, the MCP server authentication must be adapted to a non-interactive mechanism (e.g., client credentials grant or pre-provisioned tokens).

## Project Structure

```
.
├── app.py                  # Entry point — web chat server
├── agents/
│   ├── shared.py           # OAuth2, MCP factory, SAP AI Core model
│   ├── orchestrator.py     # Orchestrator with delegation tools
│   ├── cloudfoundry.py     # Cloud Foundry specialist
│   ├── btp.py              # BTP platform specialist
│   └── auditlog.py         # Audit log specialist (currently disabled)
├── mta.yaml                # MTA deployment descriptor
├── runtime.txt             # Python version for CF buildpack
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
└── agent.py                # Legacy single-agent app (reference)
```
