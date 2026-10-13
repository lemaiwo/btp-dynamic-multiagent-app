# Handover: invoke API (branch `feat/invoke-api-spike`)

Context for a Claude Code session that continues this work. Written by the
cloud session that made this branch; delete this file when the feature lands.

## The use case

An expense app (another BTP subaccount) wants to call an "expense expert"
agent with an attachment (receipt as image or PDF). The agent reads that
user's expense history in SAP, creates a **draft** expense, and the expense
app then refreshes its page and needs the **id of the created expense**. It
must stay generic: today expenses, later something else. The caller wants
both polling and a short synchronous wait, and wants to see everything the
agent did and why.

## Why the existing endpoints do not fit

- `POST /api/agents/{slug}/run` (`agents/api_runs.py`): `JOBSCHEDULER` scope
  only, one run per agent at a time (`job_runner._lock_for(agent.id)`), runs
  as the agent's fixed `run_as_principal`, uses the stored `run_prompt` only,
  returns a markdown report; the result is read behind `require_admin`
  (`GET /admin/api/runs/{id}`).
- `POST /a2a` (`agents/a2a.py`): always the orchestrator (no agent by name),
  `message/send` returns no task id until finished, tasks in memory only
  (15 min TTL, per instance), no attachments, no structured result.

## Agreed design

**Endpoint** `POST /api/invoke/{slug}`, multipart: `input` (text) plus
`files[]`. Behind a NEW XSUAA scope `invoke` (`xs-security.json`, role
template + collection; approuter route). The caller's JWT is the run's
identity: `current_principal` AND `current_jwt` are the user's (no
`run_as_principal`, unlike the scheduler run). Answers `202 {run_id}`;
`?wait=<s>` (max 30) waits that long and returns the poll answer if the run
finished.

**Poll** `GET /api/invoke/runs/{run_id}`, only for the principal who started
it (404 otherwise):

```json
{"status": "completed|running|failed|canceled",
 "result": {"expense_id": "4711", "status": "draft"},
 "writes": [{"service": "expenses", "entity_set": "ExpenseReports",
             "operation": "create", "key": {"ExpenseId": "4711"},
             "outcome": "ok"}],
 "text": "the model's final explanation",
 "activity": [...], "error": null}
```

- `result`: what the agent handed in through a `submit_result(result)` tool
  validated against the agent's `result_schema` (same pattern as the IDE's
  `submit_document`, `agents/ide/session_tools.py`). A run that ends without
  calling it is `failed` with `error: no_result`, so the caller never reads
  a result the model only talked about.
- `writes`: read from `odata_audit_log` by `run_id` (`agents/odata/audit.py`,
  `created_key_json` is the key SAP returned). It is what the expense app
  relies on for the id; it cannot be made up and it is generic.
- `activity`: `RunActivity` (`agents/run_activity.py`) already records every
  tool call (own, peer, deep sub-agent) and plans; served live from memory,
  stored in `job_runs.activity_json` after. Add a record per attachment
  (name, media type, size; never content).

**Per-agent settings** (`AgentConfig`, both admin UIs): `expose_invoke`
(independent of `expose_chat` / `expose_api`), `result_schema` (JSON schema),
attachment limits (allowed media types, default `image/png`, `image/jpeg`,
`application/pdf`; max 10 MB per file, 5 files, 20 PDF pages). Invoke runs
have their own lock per (user, agent): a second call answers 409 with the
running `run_id`; a cap per instance; timeout 5 min (not the scheduler's 30).
Reuse `job_runs` rows (`trigger="invoke"`, `created_by=principal`) and the
runner pattern of `agents/job_runner.py` (`_finalize` never raises, stale
sweep, `shared.run_usage_limits()`).

**Attachments**: sent with the call, kept in memory for the run only, never
persisted (no separate upload endpoint: it would need storage, retention,
owner checks and cleanup for a receipt used once). Given to the model
natively as pydantic-ai `BinaryContent`. Claude names go through AI Core's
**Bedrock Converse** proxy (`agents/shared._build_bedrock_model`), which
takes `image` / `document` blocks; the probe below proves it through AI
Core. Fallback if the PDF step fails: rasterise pages to PNG on the server
(`pypdfium2`). Fallback if images fail too: a different model route (bigger
change, decide then).

**Identity across subaccounts**: expense backend -> destination
`OAuth2SAMLBearerAssertion` (audience: this app's XSUAA) -> this endpoint
with the user's JWT carrying `invoke` -> the catalogue OData service "Runs
as: signed-in user" resolves the S/4 destination with `user_token`
(`agents/destination.py`), again `OAuth2SAMLBearerAssertion`. Setup: the
calling subaccount's XSUAA trusted as IdP here; the user holds the role
collection with `invoke` here. The expense agent's `builtin:odata` entry has
`allow_write: true` with only the "create draft" operation enabled.

**Guard to keep**: an agent whose `builtin:bitbucket` entry approves
(`asks_to_approve`) must be refused `expose_invoke`, like chat exposure
(`check_bitbucket_approver_reach` in `agents/db.py`).

## State of the branch

- `scripts/probe_attachments.py`: the go/no-go spike. Builds the model
  through `agents.shared.get_model`, sends a generated PNG (red square) and
  a generated PDF ("Receipt code: 7391-KX"), alone and together, and checks
  the answers. Not yet run against AI Core (the cloud session had no
  credentials). Run with the app's `.env`:

      .venv/bin/python scripts/probe_attachments.py --model anthropic--claude-4.6-opus

  `RESULT PASS` -> native attachments, no extraction. `pdf FAIL` only ->
  add the pypdfium2 fallback.
- Nothing else is built yet.

## Order of work

1. Run the probe; decide the attachment path.
2. `agents/invoke.py`: routes, per-user lock, `submit_result`, attachment
   records in activity, `writes` from the audit log; `invoke` scope in
   `xs-security.json`; approuter route (`csrfProtection` off, like `/a2a`:
   server-to-server). Register in `app.py`.
3. `AgentConfig` columns (`_ensure_column` in `init_db`; HANA: a NOT NULL
   column needs a `server_default`, raise `HANA_SCHEMA_GENERATION`, run
   `scripts/hana_schema_history.py`), admin payload gate, both admin UIs.
4. Tests (`tests/`, SQLite; the conftest compiles every statement for HANA
   too), then CLAUDE.md entries for the new module, scope and settings.
5. Configuration on the landscape: expense agent + skill + catalogue
   service (landscape-specific, stays in the local `docs/`).
