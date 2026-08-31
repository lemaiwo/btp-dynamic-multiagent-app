# Agent Workflows and Peer Delegation — Design

**Date:** 2026-08-31
**Status:** Approved for planning
**First use case:** an email triage workflow — a small-model reader agent
classifies unhandled mail into work items, each item is dispatched to the ABAP
or Fiori specialist named by its own route, and a draft-writer agent produces
the reply. Runs unattended on a BTP schedule.

## Goal

Two capabilities that today's single-orchestrator, single-agent-per-run design
cannot express:

1. **Peer delegation** — an agent may consult another agent directly, so the
   chat topology is configurable rather than always a star through the one
   orchestrator.
2. **Workflows** — a declared, ordered sequence of agents executed as one
   background job, fanning out over the work items the first step discovers.

Plus the enabler both need: a **per-agent model**, so a cheap reader and an
expensive specialist can coexist in one chain.

Today `gmail-sap-arc1-assistant` is a single agent carrying `builtin:gmail`,
ARC-1 and SAP docs plus two skills, doing read → research → draft in one run.
The goal is to make that decomposable into cooperating agents without
special-casing the email scenario: a second workflow should mean rows in the
admin UI, not new code.

## Context and constraints

Properties of the existing system this design must respect. Established by
reading the code, not assumed.

**There is one orchestrator and it is the only delegator.**
`build_orchestrator()` in `agents/registry.py` constructs a single orchestrator
and calls `_attach_delegation_tool(orchestrator, specialist, row)` once per
`expose_chat` agent. Specialists are plain `Agent` instances with MCP toolsets
and skills; nothing gives one specialist a handle on another.

**`_attach_delegation_tool` already generalizes.** Its first parameter is the
parent agent. Nothing in its body assumes that parent is the orchestrator, so
attaching the same tool to a specialist requires no change to its logic — only
to who calls it and when.

**The model is global.** `get_active_model_name()` returns one name, resolved
once in `build_orchestrator` and passed to every `Agent`. There is no per-agent
override, so "a smaller LLM for the reader" does not exist yet.

**Background runs are single-agent.** `job_runner.start_run()` takes one
`AgentConfig`, verifies `run_as_principal` and credentials, runs one specialist
with `run_prompt` under `run_as()`, and writes one `JobRun` row. The row doubles
as the overlap lock (`active_job_run`).

**A run has no interactive user.** `run_as()` sets `current_principal` and
`current_base_url` but deliberately not `current_jwt`, so a run reaches only
`auth_mode` `oauth2` (per-user token store) and `none` servers. Each agent in a
chain may need a different principal's stored token.

**`registry.reload()` protects in-flight runs by inspecting
`job_runner._tasks`.** If that set is non-empty it keeps the previous build's
MCP clients open rather than closing transports out from under a running job.
Workflow runs must join that check or a mid-run reload kills them with an
opaque closed-client error.

**Job Scheduling Service constraints** (unchanged from the scheduled-runs
design): synchronous action timeout 15s, so the trigger must acknowledge with
202 and report later; async timeout defaults to 30 minutes; an endpoint
unreachable 10+ days auto-deactivates the schedule.

**Schema changes** use the existing `_ensure_column` helper in `agents/db.py`
for new columns on existing tables; new tables come from `create_all`.

## Architecture

```
CHAT (peer delegation)              BACKGROUND (workflow)

user                                 BTP Job Scheduler
 |                                    |  POST /api/workflows/{slug}/run
 v                                    v
orchestrator                       agents/workflow_runner.py
 |  delegate_abap_specialist          |
 v                                    |  step 1 (fan_out)
abap-specialist  ------.              v
 |  delegate_fiori...  |          email-reader (small model)
 v                     |              |  -> [item A, item B, item C]
fiori-specialist       |              |
                       |              +-- item A: route=abap
config: peers_json ----'              |     step 2 -> abap-specialist
                                      |     step 3 -> draft-writer
                                      +-- item B: route=fiori
                                      |     ...
                                      +-- item C: FAILED, others continue
                                      |
                                      v
                            workflow_runs
                              +- workflow_item_runs
                                   +- workflow_step_runs
```

Both paths reuse the same specialist `Agent` objects from the registry build.

## Component 1 — peer delegation

### Storage

One new column on `agent_configs`, via `_ensure_column`:

| column | type | purpose |
|---|---|---|
| `peers_json` | text, nullable | JSON list of agent names this agent may consult |

Exposed as an `AgentConfig.peers` property mirroring the existing `skills`
property: tolerant of malformed JSON (log and return `[]`), filtering blanks.
Included in `to_dict` and `to_export`, so bundles stay portable.

Peers are referenced **by name**, like skills, for the same reason: exports and
seed files must survive differing row ids.

### Build

`build_orchestrator()` becomes two-pass:

1. **Pass 1** — unchanged: build every enabled agent's specialist, register the
   orchestrator delegation tool for `expose_chat` rows.
2. **Pass 2** — for each row with peers, look up each peer's built specialist in
   the `specialists` dict and call
   `_attach_delegation_tool(specialists[row.name], peer_specialist, peer_row)`.

Two passes are required because a peer may be built after the agent consuming
it, and the build order follows `list_agents()`, not the dependency graph.

A peer that names an unknown, disabled, or unbuildable agent is skipped with a
warning — the same tolerance `skills` already has. An agent naming itself is
skipped.

### Loop safety

Mutual peers (A lists B, B lists A) are legitimate — two specialists that can
each ask the other a question — so cycles are **not** rejected at save time.
They are bounded at runtime instead, in `_delegate`:

- A `ContextVar` holds the delegation stack (a tuple of agent names) for the
  current run. `_delegate` pushes its agent name on entry and resets on exit.
- **Depth cap**: `AGENT_DELEGATION_MAX_DEPTH`, default 3. Exceeding it returns
  an explanatory string to the calling model rather than raising, so the model
  can answer with what it has.
- **Re-entry guard**: if the target agent is already on the stack, return a
  string saying so. This is what actually stops A→B→A, and it is cheaper and
  more precise than a depth cap alone.

Both are returns, not exceptions: a delegation tool that raises would surface to
the user as a failed turn, whereas a message lets the model recover.

### Progress and usage

No changes needed. The progress sink is a `ContextVar` installed per request in
`agents/chat_app.py` and inherited by nested asyncio tasks, so a peer's tool
cards already reach the chat stream. `_delegate` already forwards
`usage=ctx.usage`, so nested token usage aggregates into the parent run.

## Component 2 — per-agent model

One new column on `agent_configs`, via `_ensure_column`:

| column | type | purpose |
|---|---|---|
| `model_name` | varchar(128), nullable | overrides the global active model for this agent |

`build_orchestrator` resolves the model per row: `row.model_name` if set, else
the already-resolved global model. Resolution failure for a row's override logs
a warning and falls back to the global model — the same shape as the existing
global fallback, and for the same reason: one bad value must not take the whole
registry down.

The orchestrator itself keeps using the global model.

## Component 3 — workflow storage

### `workflows`

| column | type | purpose |
|---|---|---|
| `id` | int pk | |
| `name` | varchar(64), unique | |
| `description` | text | shown in the admin list |
| `api_slug` | varchar(64), nullable, unique when set | `POST /api/workflows/{api_slug}/run` |
| `run_as_principal` | varchar(255), nullable | fallback identity for steps whose agent has none |
| `run_timeout_seconds` | int, default 1800 | ceiling for the whole run; must stay under the BTP async timeout |
| `skip_seen_items` | int, default 1 | skip items already completed by a previous run |
| `max_parallel_items` | int, default 1 | items processed concurrently |
| `enabled` | int, default 1 | |
| `created_at` / `updated_at` | timestamptz | |

Uniqueness of `name` and `api_slug` is enforced in the upsert helper, matching
how `agent_configs.api_slug` is handled today.

### `workflow_steps`

| column | type | purpose |
|---|---|---|
| `id` | int pk | |
| `workflow_id` | int | |
| `position` | int | execution order, unique within a workflow |
| `agent_name` | varchar(64), nullable | fixed agent for this step |
| `route_map_json` | text, nullable | JSON object mapping a route value to an agent name |
| `instructions` | text | the step's task text, appended to the prompt |
| `fan_out` | int, default 0 | this step produces the work items |
| `on_missing_route` | varchar(16), default `fail` | `fail` or `skip` when an item's route matches no key |
| `step_timeout_seconds` | int, default 600 | ceiling for one step of one item |

Exactly one of `agent_name` / `route_map_json` must be set — validated on save.

**Validation at save time**, because a workflow that cannot run must not wait
until 03:00 to say so:

- At most one step may have `fan_out`.
- Every `agent_name` and every value in `route_map_json` must name an existing,
  enabled agent.
- Positions must be unique and contiguous from 1.
- A workflow with no steps cannot be enabled.

### Run records

Three tables, mirroring the fan-out shape:

- **`workflow_runs`** — `id` (uuid str), `workflow_id`, `workflow_name`,
  `trigger` (`schedule` | `manual`), `status`, `started_at`, `finished_at`,
  `items_total`, `items_succeeded`, `items_failed`, `items_skipped`,
  `summary`, `error`, `created_by`, and the four `scheduler_*` columns
  `JobRun` already carries.
- **`workflow_item_runs`** — `id`, `workflow_run_id`, `item_key` (the reader's
  `id`), `title`, `route_json`, `status`, `error`, timestamps.
- **`workflow_step_runs`** — `id`, `workflow_run_id`, `item_run_id` (null for
  steps that run before the fan-out), `position`, `route` (nullable; which
  route value produced this row, for multi-route dispatch), `agent_name`,
  `status`, `output` (text), `error`, timestamps.

`status` values match `JobRun`'s vocabulary: `running`, `success`, `failed`,
`interrupted`, plus `skipped` on item runs and `partial` on workflow runs.

## Component 4 — the runner (`agents/workflow_runner.py`)

Mirrors `agents/job_runner.py` deliberately: same task-set pattern, same
`_finalize` never-raises discipline, same per-entity start lock, same
`cancel_all_runs` shutdown hook. That module's docstrings explain why each of
those exists; the reasons apply identically here.

### The work item

```python
class WorkItem(BaseModel):
    id: str            # stable dedup key (e.g. the Gmail message id)
    route: list[str]   # dispatch keys; empty means the step's fixed agent
    text: str          # everything the next step should read
```

This is the *only* structure the engine understands. `id` exists because
fan-out needs item identity for reporting and repeat-run safety; `route` exists
because dispatch is data, not an LLM decision. Everything else the reader wants
to convey goes in `text`, unparsed.

`route` is a list because one item can legitimately need two specialists — an
email that is half an ABAP dump and half a Fiori rendering question. A dispatch
step therefore runs **once per route value, in list order**, chaining each
agent's output into the next as an ordinary handoff. A fixed (`agent_name`)
step ignores `route` entirely and runs once.

This is why `workflow_step_runs` carries a `route` column: two rows can share
one `position` within a single item run, distinguished by which route value
produced them.

### Execution

An agent's **effective principal** is its own `run_as_principal` when set,
otherwise the workflow's. A step whose agent has neither fails the run at
preflight, naming the agent.

1. **Preflight.** Resolve the workflow and its steps. Collect every agent the
   workflow can reach (fixed `agent_name`s plus all `route_map` values). For
   each, check it is built in `registry.build.specialists` and that its
   effective principal has usable credentials, reusing
   `job_runner._has_usable_credentials`. Any failure fails the run before a
   single model call, naming the agent and the fix — the same
   "never-configured vs. configured-but-stale" distinction `job_runner` draws.
2. **Pre-fan-out steps** run once, in position order, their outputs chained as
   text. Their step runs carry `item_run_id = NULL`.
3. **The fan-out step** runs its agent with `output_type=list[WorkItem]`. An
   empty list is a successful run with `items_total = 0`, not an error — "no
   mail today" is a normal outcome. A workflow with no `fan_out` step at all is
   valid: it is a linear chain that runs once, with no item runs.
4. **Per item**, in position order, for every step after the fan-out:
   - Resolve the agent(s). A fixed step uses `agent_name` once. A dispatch step
     maps each of the item's route values through `route_map_json`, in list
     order; a value with no matching key follows `on_missing_route`.
   - For each resolved agent in turn: build the prompt (below), run it under
     `run_as` for that agent's **effective principal**, bounded by
     `step_timeout_seconds`, and chain its output into the next.
   - Record a `workflow_step_run` per agent run, tagged with its route value.
5. **Aggregate.** All items succeeded → `success`. Some failed → `partial`.
   The fan-out step itself failed, or preflight failed → `failed`.

Items are processed with a semaphore of `max_parallel_items`, default 1.
Sequential is the default because concurrent items would multiply load on ARC-1
and the model quota; the column exists so the ceiling can be raised without a
schema change.

### Prompt construction

Step N's prompt is assembled as plain text — no structured record, by design:

```
## From <previous agent name>
<previous step's output text>

## Your task
<this step's instructions>
```

For the first step after the fan-out, the "From" block carries `item.text`. For
pre-fan-out step 1 there is no "From" block at all. The handoff is therefore
steered by writing each agent's instructions and each step's task text, not by
configuration.

Only the immediately preceding output is included, not the whole transcript.
A three-step chain over many items would otherwise grow the prompt without
bound, and each agent's job is defined by its own instructions anyway.

### Repeat-run safety

When `skip_seen_items` is set, an item whose `item_key` already has a `success`
item run for this workflow is recorded `skipped` and its steps are not run.

This is the generic form of the ad-hoc idempotency each toolset invented
separately — Gmail's label queue, Jira's already-commented filter. It makes a
retry after a crash resume rather than re-draft, which matters most for the
steps that write to the outside world.

It is per workflow, not global: the same email may legitimately be processed by
two different workflows.

### Interaction with `registry.reload()`

`Registry.reload()` currently checks `job_runner._tasks` before closing the
previous build's MCP clients. It must also check `workflow_runner._tasks`, or
an admin pressing Reload mid-workflow tears the transports out from under it.
The existing deferred-import comment explains the circularity; the same
approach applies.

## Component 5 — trigger endpoint

`agents/api_runs.py` gains:

```
POST /api/workflows/{slug}/run   -> 202 {"run_id": ...}
```

Identical contract to the existing agent route: `require_jobscheduler`, the
same `x-sap-job-*` header capture, 404 for unknown or disabled slug, 409 when a
run of that workflow is already in progress. Like the agent route it is
deliberately absent from `approuter/xs-app.json` — the scheduler reaches the app
directly and the scope check is the protection.

## Error handling

- **Step timeout** → that step fails; the item fails; other items continue.
- **Workflow timeout** → the run is cancelled and recorded `failed`; in-flight
  item runs are recorded `interrupted`.
- **Shutdown** (`CancelledError`) → run recorded `interrupted`, then re-raised,
  exactly as `job_runner.execute_run` does.
- **Finalization failure** → logged and swallowed via a `_finalize` twin, since
  the run row is the overlap lock and an escaping exception would wedge it.
- **A step's agent needs interactive sign-in** → there is no user and no popup;
  the step fails with the sign-in message. This is why preflight checks
  credentials up front.
- **Peer delegation depth or re-entry exceeded** → a message returned to the
  calling model, not an exception.

## Admin UI

`templates/admin.html` (the supported admin per `CLAUDE.md`) gains:

- **Agent form**: a "Can consult" multi-select bound to `peers_json`, and a
  model override field bound to `model_name` (blank = use the global model).
- **Workflows section**: list, create/edit with an ordered step editor
  (position, agent or route map, instructions, fan-out flag), and a "Run now"
  button.
- **Workflow runs**: a list, and a detail view rendering the run → item → step
  tree with each step's output.

`ui5-admin/` is a **follow-up increment**, not part of this design. Building the
same CRUD twice in one pass doubles the UI work for a feature whose shape will
move once it meets a real workflow. The REST API is shared, so the UI5 version
is additive when it comes.

## Increments

The three components are independently useful and should land in this order,
each green and deployable on its own:

1. **Per-agent model** (component 2) — one column, one resolution change. Also
   the smallest way to prove the `_ensure_column` migration path for this work.
2. **Peer delegation** (component 1) — the two-pass build, the loop guards, and
   the agent-form fields. Delivers the configurable chat topology on its own,
   with no workflow tables in sight.
3. **Workflows** (components 3–5) — storage, runner, endpoint, and the
   workflow admin UI. The largest by far; it depends on 1 for the small-model
   reader but not on 2.

`ui5-admin` parity is a fourth increment, outside this design.

## Out of scope

Deliberately excluded; each is a later increment if a real workflow demands it:

- Parallel *steps* within an item (steps are strictly ordered).
- Conditional steps, skips, and branching beyond `route_map` dispatch.
- Retry-with-backoff of a failed step or item.
- Workflow versioning or run-time pinning of a definition.
- A visual workflow editor.
- Per-step model override (the agent already carries one).
- Cross-instance run locking. Like `job_runner`, the lock closes the in-process
  race only; the app runs a single CF instance.
- Letting an agent replace the orchestrator as the chat root.

## Testing

New suites `tests/test_peer_delegation.py`, `tests/test_workflow_runner.py`,
`tests/test_workflow_admin_api.py`, following the existing pattern of fake
agents and an on-disk SQLite test DB.

**Peer delegation:**

- Pass 2 attaches a `delegate_*` tool to a specialist that declares peers, and
  none to one that does not.
- A peer built after its consumer is still attached (the two-pass requirement).
- Unknown, disabled, and self-referencing peer names are skipped with a warning
  and do not break the build.
- Depth cap: a chain deeper than the limit returns the cap message rather than
  recursing.
- Re-entry guard: A→B→A returns the re-entry message; the stack contextvar is
  reset on exit so a later independent call to A still works.
- Nested usage aggregates into the parent run's `ctx.usage`.

**Per-agent model:**

- A row with `model_name` builds against that model; a row without uses the
  global one.
- An unresolvable override falls back to the global model and logs, rather than
  failing the build.

**Workflow runner:**

- Declared order is followed exactly, including when a step's agent is slower
  than the next one's.
- Fan-out produces one item run per `WorkItem`; an empty list yields a
  successful run with zero items.
- Route dispatch picks the agent named by the item's route;
  `on_missing_route` `fail` and `skip` both behave as declared.
- An item with two route values runs the dispatch step twice, in list order,
  with the second agent receiving the first's output; both step runs are
  recorded and tagged with their route value.
- A fixed (`agent_name`) step runs exactly once even when the item carries
  routes.
- A workflow with no fan-out step runs as a linear chain with no item runs.
- The prompt handed to step N contains step N−1's output and the step's
  instructions, and does *not* contain step N−2's output.
- One item failing leaves the others `success` and the run `partial`.
- Step timeout fails only that item.
- `skip_seen_items`: an item key with a prior successful run is skipped; the
  same key under a different workflow is not.
- Preflight fails the run before any model call when an agent is unbuilt or has
  no usable credentials, and the error names the agent.
- Each step binds its own agent's principal, falling back to the workflow's.
- Cancellation records `interrupted` and re-raises.
- `_finalize` swallows a DB failure rather than leaving the row `running`.
- `registry.reload()` keeps old MCP clients open while a workflow task is in
  flight.

**Admin API:**

- Save-time validation: more than one fan-out step, unknown agent in
  `agent_name` or a `route_map` value, both or neither of
  `agent_name`/`route_map_json` set, duplicate or non-contiguous positions,
  enabling a workflow with no steps, duplicate `name` or `api_slug`.
- `POST /api/workflows/{slug}/run` returns 202 with a run id, 404 for unknown
  or disabled, 409 on overlap.
- Export/import round-trips `peers_json` and `model_name` on agents and the
  workflow definitions.

The existing suites must stay green — in particular `tests/test_job_runs.py`
and `tests/test_admin_api.py`, both of which touch code this design changes.
