# Joule A2A — remaining work

Branch: `claude/joule-a2a-integration-Le2nl` · CF space: `<your-cf-org> / <your-space>`

## Done
- [x] A2A code merged on branch (`agents/a2a.py`, `xs-security.json`, `approuter/xs-app.json`, `mta.yaml` 2.1.0)
- [x] MTA 2.1.0 deployed (`cf mta pydantic-agent` → Version 2.1.0)
- [x] `A2A_PUBLIC_URL` set to `https://your-approuter-host.cfapps.eu20-001.hana.ondemand.com` + restaged
- [x] Agent card verified via approuter — `url` points to the approuter `/a2a`, skills list populated, `securitySchemes.xsuaa` advertised

## To do

### 1. Create Joule XSUAA service key
```bash
cf create-service-key uaa-service joule-a2a-key
cf service-key uaa-service joule-a2a-key   # capture clientid / clientsecret / url
```

### 2. End-to-end curl smoke test (JOULE_A2A.md §2.4)
```bash
TOKEN=$(curl -s -X POST "<uaa-url>/oauth/token" \
    -d "grant_type=client_credentials" \
    -u "<clientid>:<clientsecret>" | jq -r .access_token)

curl -s -X POST "https://your-approuter-host.cfapps.eu20-001.hana.ondemand.com/a2a" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"List my dev-space apps."}],"messageId":"m-1"}}}' | jq .
```
Expect `result.status.state == "completed"` with the agent reply in `result.status.message.parts[0].text`.

### 3. Register in Joule Agent Hub (JOULE_A2A.md §3)
Pro-code flow only (Joule Studio Code Editor). Fields:
- Agent card URL: `https://your-approuter-host.cfapps.eu20-001.hana.ondemand.com/.well-known/agent-card.json`
- Auth: OAuth 2.0 Client Credentials
- Token URL: `<url from service key>/oauth/token`
- Client ID / Secret: from service key
- Scope: leave empty (XSUAA adds `a2a` scope automatically)

### 4. MCP trust for Joule-originated calls (JOULE_A2A.md §6)
When Joule invokes with `client_credentials`, the resulting token's `client_id` is the Joule service key's clientid. Each MCP server (`btp-agent-*-mcp`) must accept it — add the Joule clientid to each MCP xsuaa `oauth2-configuration.allowed-clients`, or introduce a token-exchange step. Without this, MCP calls from Joule-initiated turns will 401.

### 5. Housekeeping
- [ ] Update `README.md` line ~151 — the `cf deploy` example still references `pydantic-agent_2.0.0.mtar`; bump to 2.1.0
- [ ] Open PR for `claude/joule-a2a-integration-Le2nl` → `main`

## Useful URLs
- Approuter: https://your-approuter-host.cfapps.eu20-001.hana.ondemand.com
- Backend: https://your-backend-host.cfapps.eu20-001.hana.ondemand.com
- Agent card (anonymous): `<approuter>/.well-known/agent-card.json`
- XSUAA tenant: https://your-tenant.authentication.eu20.hana.ondemand.com
