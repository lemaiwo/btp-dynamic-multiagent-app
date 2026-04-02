# Pydantic AI Agent with SAP MCP Server

## Project Overview
A multi-agent Pydantic AI application that manages SAP BTP through specialized agents, each connected to a dedicated MCP server via OAuth 2.1 over Streamable HTTP. An orchestrator agent delegates to three specialists: audit log, Cloud Foundry, and BTP platform management. Uses SAP AI Core's Generative AI Hub as the LLM provider and exposes a web chat interface.

## Architecture
- **LLM**: SAP AI Core Generative AI Hub (`sap-ai-sdk-gen` package) via OpenAI-compatible API
- **MCP**: Streamable HTTP transport with OAuth2 flow (`mcp` SDK's `OAuthClientProvider`)
- **UI**: Pydantic AI built-in web chat (`orchestrator.to_web()`)
- **Framework**: Pydantic AI with `OpenAIModel` + `OpenAIProvider` wrapping SAP's `AsyncOpenAI` client
- **Multi-Agent**: Orchestrator delegates to specialist agents via `@agent.tool` pattern

## Key Files
- `app.py` — Entry point for the multi-agent web chat
- `agents/shared.py` — Shared infrastructure: OAuth2, token storage, MCP factory, SAP AI Core model
- `agents/auditlog.py` — Audit log specialist agent
- `agents/cloudfoundry.py` — Cloud Foundry specialist agent
- `agents/btp.py` — BTP platform specialist agent
- `agents/orchestrator.py` — Orchestrator agent with delegation tools
- `agent.py` — Legacy single-agent application (kept for reference)
- `requirements.txt` — Python dependencies
- `.env` — SAP AI Core credentials (not committed)
- `.tokens-{name}.json` — Per-server OAuth2 tokens (not committed)

## MCP Servers
- **Audit Log**: `https://infrabel-app-dev-cf-ai-btp-agent-auditlog-mcp.cfapps.eu20-001.hana.ondemand.com/`
- **Cloud Foundry**: `https://infrabel-app-dev-cf-ai-btp-agent-cf-mcp.cfapps.eu20-001.hana.ondemand.com/`
- **BTP**: `https://infrabel-app-dev-cf-ai-btp-agent-btp-mcp.cfapps.eu20-001.hana.ondemand.com/`

## Configuration
- `CALLBACK_PORT` — Local port for OAuth2 redirect callback (default: 3000)
- SAP AI Core model name is set in `agents/shared.py` `get_model()` (currently `gpt-4o`)

## Running
```bash
pip install -r requirements.txt
cp .env.example .env  # fill in AICORE_* credentials
python app.py
# Opens web chat at http://127.0.0.1:7932
```

## SAP AI Core Auth
Credentials are resolved automatically by `sap-ai-sdk-gen` from environment variables:
- Option A: Individual vars (`AICORE_AUTH_URL`, `AICORE_CLIENT_ID`, `AICORE_CLIENT_SECRET`, `AICORE_BASE_URL`, `AICORE_RESOURCE_GROUP`)
- Option B: Single JSON blob (`AICORE_SERVICE_KEY`)

## Dependencies
- `pydantic-ai[mcp,web,openai]` — Agent framework, MCP client, web UI, OpenAI model support
- `sap-ai-sdk-gen[all]` — SAP AI Core SDK (handles LLM auth and proxy)
- `mcp` — MCP protocol SDK (OAuth2 client provider)
- `httpx` — Async HTTP client
- `uvicorn` — ASGI server
- `python-dotenv` — Environment variable loading
