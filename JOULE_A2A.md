# Integrating the orchestrator into SAP Joule (A2A)

This guide describes how to expose the dynamic multi-agent orchestrator
to **SAP Joule** using the [Agent-to-Agent (A2A)](https://a2a-protocol.org)
protocol, and how to configure BTP and the Joule Agent Hub so Joule can
discover and invoke the orchestrator as a remote code-based agent.

Reference:
[Joule A2A: Connect Code Based Agents into Joule](https://community.sap.com/t5/technology-blog-posts-by-sap/joule-a2a-connect-code-based-agents-into-joule/ba-p/14329279)
on the SAP Community blog.

---

## 1. What this integration adds

| Endpoint                              | Purpose                                             | Auth                                 |
|---------------------------------------|-----------------------------------------------------|--------------------------------------|
| `GET  /.well-known/agent-card.json`   | A2A Agent Card (discovery document)                 | Anonymous                            |
| `GET  /.well-known/agent.json`        | Legacy alias for the agent card                     | Anonymous                            |
| `POST /a2a`                           | JSON-RPC 2.0 entry point for A2A clients            | XSUAA — `<xsappname>.a2a` scope      |

Supported JSON-RPC methods (A2A v0.3):

- `message/send` — synchronous single-turn invocation, returns a `Task`
- `message/stream` — streamed invocation (Server-Sent Events)
- `tasks/get`, `tasks/cancel`

`contextId` is honoured across turns so Joule can keep a conversation
thread alive. The orchestrator's message history is preserved in memory
per context for one hour by default (`A2A_CONTEXT_TTL`).

The agent card is built dynamically from the registry: every **enabled**
specialist becomes an A2A `skill` (in addition to the top-level
`orchestrate` skill), so when an admin adds/removes agents, the card
reflects the change after a reload.

---

## 2. Configure SAP BTP (Cloud Foundry)

### 2.1 Deploy (or re-deploy) the app

Bump to the new MTA version and redeploy:

```bash
mbt build
cf deploy mta_archives/pydantic-agent_2.1.0.mtar
```

This update:

- Adds the **`$XSAPPNAME.a2a`** scope, the **`AgentA2AClient`** role
  template, and the **`Agent A2A Client`** role collection
  (`xs-security.json`).
- Publishes routes for `/.well-known/agent-card.json` (anonymous) and
  `/a2a` (XSUAA, `a2a` scope) in the approuter (`approuter/xs-app.json`).
- Registers `client_credentials` and `urn:ietf:params:oauth:grant-type:jwt-bearer`
  as allowed grant types so Joule can obtain a token.

After deploy, set the public approuter URL so the agent card advertises
the correct `url`:

```bash
cf set-env pydantic-agent A2A_PUBLIC_URL \
    https://<org>-<space>-pydantic-agent-approuter.cfapps.eu20-001.hana.ondemand.com
cf restage pydantic-agent
```

Verify:

```bash
curl -s https://<approuter-url>/.well-known/agent-card.json | jq .
```

You should see a card containing `"protocolVersion": "0.3.0"`, the
correct `url`, and one `skill` per enabled agent.

### 2.2 Create a service key for Joule

Joule's Agent Hub authenticates to BTP as an OAuth2 client. Create a
dedicated XSUAA service instance / key for Joule to consume:

```bash
# Option A — reuse the existing uaa-service and create a key
cf create-service-key uaa-service joule-a2a-key

# Fetch the credentials (clientid, clientsecret, url)
cf service-key uaa-service joule-a2a-key
```

Copy the `clientid`, `clientsecret`, and `url` values — you will hand
them to Joule in section 3.2.

### 2.3 Grant the `a2a` scope to Joule

The XSUAA instance is already configured with a
`grant-as-authority-to-apps` entry so the scope can be granted to
provisioning apps. For an external Joule Agent Hub tenant, assign the
**`Agent A2A Client`** role collection to the Joule technical user or
service instance:

1. BTP Cockpit → your subaccount → **Security → Role Collections**.
2. Open **`Agent A2A Client`**.
3. Click **Edit** → **Add Users** (or **Add Service Instance**).
4. Enter the Joule technical user / service principal.
5. Save.

> If Joule calls you with `client_credentials` using the service key
> from 2.2, the scope is automatically included in the token because the
> key is bound to the same XSUAA app that declares `$XSAPPNAME.a2a`.
> No role-collection assignment is needed in that case.

### 2.4 Verify end-to-end with `curl`

```bash
# 1. Fetch a client-credentials token
TOKEN=$(curl -s -X POST "${XSUAA_URL}/oauth/token" \
    -d "grant_type=client_credentials" \
    -u "${CLIENT_ID}:${CLIENT_SECRET}" | jq -r .access_token)

# 2. Invoke the orchestrator
curl -s -X POST "https://<approuter-url>/a2a" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "List my dev-space apps."}],
        "messageId": "m-1"
      }
    }
  }' | jq .
```

A successful response is a JSON-RPC envelope with
`result.status.state == "completed"` and the agent reply inside
`result.status.message.parts[0].text`.

---

## 3. Register the agent in Joule

> As of this writing, A2A integration is only available for agents
> built with the **Joule Studio Code Editor (pro-code)** flow. The
> Joule Studio Agent Builder does not yet support direct A2A
> registration. Follow the SAP community blog for the current UI
> details — the field names below may shift slightly between releases.

### 3.1 Open the Joule Agent Hub

1. Open **SAP Build** → **Joule Studio** → **Agent Hub**
   (or **Joule Agent Hub** in your tenant's administration area).
2. Choose **Register remote agent** → **A2A**.

### 3.2 Fill in the agent connection

| Field                     | Value                                                                     |
|---------------------------|---------------------------------------------------------------------------|
| **Agent name**            | SAP BTP Multi-Agent Orchestrator                                          |
| **Agent card URL**        | `https://<approuter-url>/.well-known/agent-card.json`                     |
| **Authentication type**   | OAuth 2.0 Client Credentials                                              |
| **Token URL**             | `<uaa-url>/oauth/token` (from the service key `url` field + `/oauth/token`) |
| **Client ID**             | `clientid` from the service key                                           |
| **Client Secret**         | `clientsecret` from the service key                                       |
| **Scope**                 | *(leave empty — XSUAA adds the `a2a` scope automatically)*                |

Joule fetches the agent card from the URL above, reads the
`securitySchemes` and `skills`, and prepares the agent for invocation.

### 3.3 Map capabilities to Joule scenarios

Once the agent is registered you can:

1. Create a **Joule capability** referencing this remote agent.
2. For each business scenario, set the `inputMode` / `outputMode` to
   `text/plain` (the orchestrator returns text).
3. Bind example prompts — the agent card already exposes examples
   under `skills[].examples`; Joule uses them to train intent
   classification.

### 3.4 Test from Joule

In Joule Studio's preview chat, send a prompt that matches one of the
example utterances (e.g. *"List my dev-space apps"*). Joule routes the
prompt through the Agent Hub → A2A JSON-RPC → your orchestrator → MCP
specialists → AI Core and streams the answer back.

---

## 4. Local development

The A2A endpoints are also available on the local server:

```bash
python app.py
# Agent Card: http://127.0.0.1:7932/.well-known/agent-card.json
# A2A RPC:    http://127.0.0.1:7932/a2a
```

Without XSUAA, the backend runs in "dev mode" and accepts any request
on `/a2a` (no bearer token required), so you can exercise it with
`curl` as shown above minus the token step.

The included test suite covers agent-card generation, JSON-RPC
dispatch, multi-turn `contextId`, and SSE streaming:

```bash
python tests/test_a2a.py
```

---

## 5. Environment variables

| Variable                 | Default                                            | Purpose                                                              |
|--------------------------|----------------------------------------------------|----------------------------------------------------------------------|
| `A2A_PUBLIC_URL`         | *(derived from request)*                           | Public base URL advertised in the agent card. **Set on CF.**        |
| `A2A_AGENT_NAME`         | `SAP BTP Multi-Agent Orchestrator`                 | `name` field in the agent card.                                      |
| `A2A_AGENT_DESCRIPTION`  | *(see `mta.yaml`)*                                 | `description` field in the agent card.                               |
| `A2A_AGENT_VERSION`      | `2.1.0`                                            | `version` field in the agent card.                                   |
| `A2A_PROVIDER_ORG`       | `SAP BTP Dynamic Multi-Agent`                      | `provider.organization` field.                                       |
| `A2A_PROVIDER_URL`       | SAP community blog URL                             | `provider.url` field.                                                |
| `A2A_CONTEXT_TTL`        | `3600`                                             | Seconds a conversation's message history is kept in memory.         |
| `A2A_TASK_TTL`           | `900`                                              | Seconds a completed task is retrievable via `tasks/get`.            |

---

## 6. Troubleshooting

**`401 Missing bearer token`** when calling `/a2a` on the approuter URL
Your Joule side isn't presenting a token, or the token targets a
different XSUAA tenant. Re-check the service-key `url` vs. the
app's XSUAA `url` binding.

**`403` on `/a2a`**
The caller authenticated but lacks the `<xsappname>.a2a` scope. If
you are using `client_credentials` with the service key: make sure
the key was created on the **same** XSUAA instance that binds this
app. If you are using a jwt-bearer flow from Joule: assign the
**Agent A2A Client** role collection to the Joule principal.

**Agent card shows no skills**
The registry is empty or all agents are disabled. Open
`/admin`, enable at least one specialist, click **Reload agents**.
The card is rebuilt from the live registry on every request.

**Joule shows "Agent unreachable"**
Verify `/.well-known/agent-card.json` is reachable anonymously
(`curl -s https://<approuter-url>/.well-known/agent-card.json`).
The approuter route for this path is configured with
`authenticationType: none`.

**MCP calls fail with 401 when invoked from Joule**
The A2A endpoint forwards the **caller's** token to MCP. When Joule
calls with client-credentials, that token may not be valid for your
MCP server. Configure the specialist's MCP server to accept the
same XSUAA app, or introduce a token-exchange step. For the
provided `infrabel-app-dev-cf-ai-btp-agent-*` MCP servers, add the
Joule client-id to their XSUAA `oauth2-configuration.allowed-clients`.

---

## 7. Reference — agent card example

```json
{
  "protocolVersion": "0.3.0",
  "name": "SAP BTP Multi-Agent Orchestrator",
  "description": "Dynamic multi-agent orchestrator for SAP BTP.",
  "version": "2.1.0",
  "url": "https://<approuter>/a2a",
  "preferredTransport": "JSONRPC",
  "provider": {
    "organization": "SAP BTP Dynamic Multi-Agent",
    "url": "https://community.sap.com/..."
  },
  "capabilities": {
    "streaming": true,
    "pushNotifications": false,
    "stateTransitionHistory": false
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {
      "id": "orchestrate",
      "name": "SAP BTP Orchestrator",
      "description": "Route an SAP BTP management request ...",
      "tags": ["sap", "btp", "orchestrator"],
      "examples": ["List the running applications in my dev space."]
    },
    {
      "id": "specialist.cloudfoundry",
      "name": "cloudfoundry",
      "description": "Cloud Foundry operations ...",
      "tags": ["sap", "btp", "cloudfoundry"]
    }
  ],
  "securitySchemes": {
    "xsuaa": {
      "type": "oauth2",
      "flows": {
        "clientCredentials": {
          "tokenUrl": "https://<subdomain>.authentication.eu20.hana.ondemand.com/oauth/token",
          "scopes": {}
        }
      }
    }
  },
  "security": [{ "xsuaa": [] }]
}
```
