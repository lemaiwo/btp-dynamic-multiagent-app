# Scheduled Agent Runs — Design

**Date:** 2026-08-19
**Status:** Approved for planning
**First use case:** daily SAP system health check (ST22, SLG1, SM21, SM37) with
SAP Help research, delivered as an emailed link to a stored report.

## Goal

Let an agent configured in the admin UI be invoked non-interactively over an
API, on a schedule owned by the BTP Job Scheduling Service, and have every run
produce a durable, reviewable report that Alert Notification Service announces
by email.

The daily SAP check is the first scenario, but it must not be special-cased:
adding a second scenario should mean writing a skill and pointing a new BTP
schedule at an agent endpoint — no new code.

## Background and constraints

These are properties of the existing system that the design has to respect.
They were established by reading the code and probing the live landscape, not
assumed.

**There is no unattended identity path today.** Every MCP connection derives
from a live user. `auth_mode="jwt"` forwards `current_jwt` (the caller's XSUAA
token); `auth_mode="oauth2"` needs `current_principal` plus that user's stored
token from `mcp_oauth_tokens`. Even `/a2a` requires a real bearer token via
`require_user`. A scheduled run has neither unless we bind one explicitly.

**ARC-1 does not support `client_credentials`.** Verified against the live
server: its metadata advertises `grant_types_supported: ["authorization_code",
"refresh_token"]`, and a real token request returns
`{"error":"unsupported_grant_type"}`. `/register` echoes back whatever grants
you ask for — the client_id is a stateless signed blob — so registration
success is not evidence of support. Beyond the grant type, ARC-1 propagates the
caller's identity into SAP, so a subject-less token would have no SAP user to
run as even if the grant existed.

**Consequence:** the run acts as a **dedicated technical user** whose OAuth2
refresh token is already in the token store. That token is a single point of
failure and its renewal cannot be scripted — authorization_code requires a
human at a browser. The design makes its loss loud rather than silent.

**Job Scheduling Service constraints** (from the service documentation):
synchronous action timeout is 15s, so runs must use the asynchronous contract;
async timeout defaults to 30 minutes and is configurable; schedules use SAP's
7-field cron (`Year Month Day DayOfWeek Hour Minute Second`), UTC only; run
logs are retained 15 days; an action endpoint unreachable for 10+ days
auto-deactivates the schedule.

**Schema changes** use the existing `_ensure_column` lightweight-migration
helper in `agents/db.py`. `create_all` does not alter existing tables.

## Architecture

```
BTP Job Scheduler ──POST /api/agents/{slug}/run──▶ FastAPI
  (cron, UTC)         (202 + scheduler headers)      │
        ▲                                            ▼
        │                                    agents/job_runner.py
        │                                            │
        │                              run_as(technical user)
        │                                            │
        │                                    Registry specialist
        │                                    (output_type=RunReport)
        │                                            │
        │                                     MCP: ARC-1 (oauth2)
        │                                          SAP docs (none)
        │                                            │
        │                                       JobRun row
        │                                            │
        └──Update Run Log──────────────┬─────────────┤
                                       ▼             ▼
                                 agents/ans.py   /runs/{id}
                                       │          (approuter,
                                       ▼           user scope)
                                 ANS subscription
                                   → email
```

Admin UI adds a **Job Runs** overview (list + detail) and a **Run now** button.

## Components

### 1. Agent exposure (`agents/db.py`, `agents/admin.py`, `templates/admin.html`)

New columns on `agent_configs`, all added via `_ensure_column`:

| column | type | purpose |
|---|---|---|
| `expose_chat` | bool, default true | agent appears in the orchestrator's delegation list |
| `expose_api` | bool, default false | agent has a run endpoint |
| `api_slug` | varchar, unique when set | URL segment; derived from name, editable |
| `run_as_principal` | varchar | identity API-triggered runs bind |
| `run_prompt` | text, nullable | user prompt handed to `Agent.run()`; falls back to a fixed default. The *work* is described by instructions and attached skills — this only starts it |
| `run_timeout_seconds` | int, default 1800 | wall-clock ceiling for one run; must stay below the BTP async timeout (see below) |
| `expected_sections_json` | text | list of `source_key`s a complete report must contain |

`build_orchestrator` changes so specialists are built for **every** enabled
agent, but only `expose_chat` agents are listed in the orchestrator
instructions and given a delegation tool. This is what makes run-only agents
possible.

Admin UI: exposure fields on the agent form; `api_slug` shown with its full
endpoint URL so it can be pasted into the BTP cockpit.

### 2. Run-as identity (`agents/auth.py`)

```python
@asynccontextmanager
async def run_as(principal: str) -> AsyncIterator[None]:
    """Bind a non-interactive identity for a scheduled/API run."""
```

Mirrors what `JWTBindingMiddleware` does per request: sets `current_principal`
to the technical user and `current_base_url` from `PUBLIC_BASE_URL`. It
deliberately does **not** set `current_jwt`.

Consequences, both intentional:

- API runs work only against `auth_mode="oauth2"` and `auth_mode="none"` MCP
  servers. `auth_mode="jwt"` needs a forwarded user token that does not exist
  in a scheduled run. Current config (ARC-1 `oauth2`, docs servers `none`) is
  compatible; the admin UI should warn when an API-exposed agent binds a `jwt`
  server.
- `PUBLIC_BASE_URL` stops being optional. There is no request to derive a host
  from, and `PerUserOAuth2Auth` needs it to resolve the DCR client.

Restores contextvars on exit, including on exception.

### 3. Run endpoints (`agents/api_runs.py`)

**`POST /api/agents/{slug}/run`** — for the scheduler.

- Protected by `require_jobscheduler` (new dependency, scope
  `$XSAPPNAME.JOBSCHEDULER`).
- Captures `x-sap-job-id`, `x-sap-job-schedule-id`, `x-sap-job-run-id`,
  `x-sap-scheduler-host`.
- Creates the `JobRun`, starts a tracked background task, returns **202** with
  the run id.
- Not routed in `approuter/xs-app.json`; the scheduler calls the app's direct
  URL, which is how the approuter already reaches it. The scope check is the
  protection.

**`POST /admin/api/agents/{id}/run`** — the Run-now button.

- `require_admin`, `trigger="manual"`, no scheduler callback.
- Same runner, so testing a scenario does not require waiting for a schedule.

### 4. Runner (`agents/job_runner.py`)

1. Resolve agent by slug; reject if `expose_api` is false.
2. Insert `JobRun` with status `running`. **This row is the overlap lock.**
3. `async with run_as(agent.run_as_principal)`:
   a. Pre-flight `has_usable_token` for each of the agent's oauth2 servers.
      Missing → fail immediately with "service account needs re-authorization"
      rather than dying inside an MCP call.
   b. `registry.current.specialists[agent.name]`, run with a run-time
      `output_type=RunReport` override and `run_timeout_seconds`.
      *Verify the per-run `output_type` override against the pinned
      pydantic-ai 1.72.0 during implementation; if unsupported, build a
      separate report-typed agent instance instead.*
   c. No progress sink is bound, so the existing A2A behaviour applies: no
      auto-continue, no waiting on a sign-in that cannot happen.
4. Post-condition: every `expected_sections` key has a section with
   `checked=true` → `success`, otherwise `degraded` with the missing or
   unchecked keys named.

   **A section with zero findings is a success, not a degradation** — a quiet
   night is the expected outcome most days. The signal is whether the source
   was *checked*, never whether it produced findings. `checked=false` with a
   `note` is how the agent reports "I could not query SM21", which is exactly
   the case that must surface.
5. Persist the report, emit the ANS event, and for scheduler-triggered runs
   make the single Update Run Log call.

**Overlap guard.** Refuse to start when a `running` row exists for that agent.
Rows older than `run_timeout_seconds` are swept to `interrupted` so a crashed
run cannot wedge the agent permanently. App shutdown and startup both sweep,
so a CF redeploy mid-run leaves no ghost.

### 5. Report model (`agents/reports.py`)

```python
class Finding(BaseModel):
    title: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    count: int
    affected: list[str]          # programs, users, objects
    detail: str
    analysis: str | None
    recommendation: str | None
    references: list[str]        # SAP Help URLs

class ReportSection(BaseModel):
    source_key: str              # "st22", "slg1", "sm21", "sm37"
    title: str
    checked: bool
    findings: list[Finding]
    note: str | None

class RunReport(BaseModel):
    summary: str                 # one line, used in the ANS event
    overall_severity: Literal["critical", "high", "medium", "low", "info"]
    sections: list[ReportSection]
```

Stored as JSON and rendered on view, so the detail page, the ANS summary and
any future trending all read one structure.

### 6. `JobRun` (`agents/db.py`)

| column | notes |
|---|---|
| `id` | uuid, used in the report URL |
| `agent_name`, `agent_id` | agent that ran |
| `trigger` | `schedule` \| `manual` |
| `status` | `running` \| `success` \| `degraded` \| `failed` \| `interrupted` |
| `started_at`, `finished_at` | run window |
| `report_json` | serialized `RunReport` |
| `summary` | denormalized for the list view and the ANS event |
| `error` | populated on failure |
| `ans_delivered` | false when the ANS post failed |
| `scheduler_job_id`, `scheduler_schedule_id`, `scheduler_run_id`, `scheduler_host` | for the callback |
| `created_by` | principal, for manual runs |

Retention sweep at startup, default 90 days, configurable.

### 7. ANS (`agents/ans.py`)

One resource-event per completed run, via the `alert-notification` binding's
OAuth client_credentials:

| field | value |
|---|---|
| `eventType` | `AgentRunCompleted` |
| `resource` | agent name / type `agent-run` |
| `severity` | `INFO` success, `WARNING` degraded, `ERROR` failed |
| `category` | `NOTIFICATION` (success/degraded), `ALERT` (failed) |
| `subject` | agent name + status |
| `body` | `RunReport.summary` + link to `{PUBLIC_BASE_URL}/runs/{id}` |

Recipients and routing live in ANS subscriptions, not in this app.

**Confirm ANS subject/body size limits against the service documentation during
implementation** and truncate defensively regardless. The full report is never
in the event — that is why the report is hosted and linked.

### 8. Report view (`/runs/{id}`)

Server-rendered page behind the approuter. `/runs/**` added to
`approuter/xs-app.json` requiring the `user` scope, so emailed links work for
anyone who can log in.

### 9. Deployment (`mta.yaml`, `xs-security.json`)

- New resources: `jobscheduler` (plan `standard`) and `alert-notification`,
  both bound to the `pydantic-agent` module.
- `xs-security.json`: scope `$XSAPPNAME.JOBSCHEDULER` with
  `grant-as-authority-to-apps: ["$XSSERVICENAME(jobscheduler-service)"]`.
- `PUBLIC_BASE_URL` set to the approuter URL.
- **Prerequisite:** subaccount entitlements for both services must exist in
  `infrabel-app-acc-cf`. Check before starting — a missing entitlement blocks
  the deploy at the last step.

## The first scenario (configuration, not code)

1. A **skill** — "Daily SAP System Check" — carrying the instructions: the four
   sources and how to query each, the 24h window, deduplicate and rank by
   severity × occurrence count, research the top 5–10 in SAP Help, and the
   report structure to emit.
2. The existing ABAP Development Agent (or a run-only clone) with the skill
   attached, `expose_api=true`, `expected_sections_json =
   ["st22","slg1","sm21","sm37"]`, and `run_as_principal` set to the technical
   user.
3. A BTP job whose action is that agent's endpoint, cron at an irregular time
   such as `03:17:23` — SAP advises against the top of the hour and midnight
   UTC, which are the service's busiest moments.
4. An ANS subscription with an email action, filtered to `eventType =
   AgentRunCompleted`.

**Timeout ordering matters.** The BTP job's async timeout must be set
explicitly and must exceed `run_timeout_seconds`, or the scheduler gives up
while the run is still working and the later Update Run Log call lands on a run
it has already failed. With the 1800s default, set the BTP async timeout to
2700s. Both defaults are documented together so they cannot drift apart
silently.

A second scenario repeats steps 1–4. No code changes.

## Known trade-offs

**Instructions, not a coded pipeline.** "All four sources were checked" and
"ranked by severity × frequency" are prompt discipline, not guarantees. Two
safeguards recover most of it: the `RunReport` `output_type` makes the shape
enforceable (pydantic-ai validates and retries on mismatch), and the
`expected_sections` post-condition turns a silently incomplete report into a
visibly `degraded` run. Neither guarantees the *content* of a section is
thorough — only that the section exists and is non-empty.

**Duplication between skill and config.** The sources live in the skill's
instruction text and `expected_sections` lives on the agent config; they can
drift. Deriving one from the other means parsing prose. Mitigation: drift
surfaces as `degraded` on the first test run.

**Schedules are not in the admin UI.** Cron lives in the BTP cockpit, so
creating a scenario is a two-place operation. Accepted deliberately: BTP owns
timing, retries and run history, and the app does not reimplement them. The
`api_slug` field displays the full endpoint URL to make the handoff easy.

**Execution history is split.** BTP holds run status for 15 days; our `JobRun`
rows hold content for 90. Deliberate — we do not duplicate the cockpit.

## Error handling

| failure | behaviour |
|---|---|
| technical-user token gone | `failed` at pre-flight, `ERROR` event naming re-authorization |
| specialist timeout / MCP error | `failed`, error persisted |
| missing expected sections | `degraded`, missing keys named in report and event |
| ANS post fails | run keeps its real status, `ans_delivered=false`, visible in overview |
| scheduler callback fails | retried twice, then logged; schedule shows `ACK_NOT_RECVD` |
| app restart mid-run | swept to `interrupted` on shutdown and startup |

Principle: a delivery problem must never lose a completed run.

## Testing

Hand-rolled harness style, matching `tests/test_oauth2.py`.

- `run_as` binds and restores contextvars, including on exception
- overlap guard: second run refused while one is `running`; stale rows swept
- post-condition: complete report → `success`; missing section → `degraded`
- ANS payload construction, severity mapping, truncation (mocked transport)
- scheduler header parsing and callback URL construction
- endpoints: missing scope → 403, unknown slug → 404, `expose_api=false`
  rejected, 202 body shape
- admin UI (`test_admin_ui.py` style): Job Runs tab renders, Run-now creates a
  row, detail view renders a report

## Sequencing

Three increments, each leaving the app working and deployable:

1. **Core** — exposure fields, `JobRun`, runner, both endpoints, Run-now
   button, Job Runs overview. Verifiable entirely from the admin UI with no
   external services.
2. **ANS** — `agents/ans.py`, the service binding, event emission.
3. **Scheduler** — `xs-security.json` scope, `jobscheduler` binding, the BTP
   job, `PUBLIC_BASE_URL`, end-to-end scheduled run.

Risky external dependencies come last; the core is proven before anything
depends on a BTP entitlement.
