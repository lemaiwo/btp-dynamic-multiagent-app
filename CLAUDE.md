# SAP BTP Dynamic Multi-Agent

## Project overview
Multi-agent Pydantic AI application where specialist agents are defined
**dynamically at runtime** via an XSUAA-secured admin UI. An orchestrator
delegates to specialist agents that connect to BTP-hosted MCP servers
over OAuth 2.1. The user's XSUAA JWT is forwarded to each MCP server.
SAP AI Core's Generative AI Hub is the LLM provider.

> **`docs/` is not in this repository.** Every `docs/...` path mentioned
> below (connector setup guides, the workflow design spec, `UI5_ADMIN.md`,
> `AICORE_RESOURCE_GROUP.md`) is landscape-specific: it names real CF
> orgs and spaces, approuter hosts, destination names and OAuth client
> ids, so `docs/` is gitignored and lives only in the operator's local
> working copy (the same place as the `.mtaext` files). Do not create
> `docs/` files in the repo to satisfy a reference; ask for the local
> copy instead. Module docstrings carry the parts that are safe to
> publish.

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
- `agents/db.py` — SQLAlchemy models (`AgentConfig`, `SkillConfig`,
  `OrchestratorConfig`), `init_db`, CRUD helpers, VCAP/ENV postgres URL
  resolver. `AgentConfig.auth_mode` is one of `jwt`, `none`, `oauth2`,
  `app_only`, `destination`, `session`. Skills are reusable instruction
  blocks attached to agents by name (`AgentConfig.skills_json`). Six more
  tables back the workflow engine: `Workflow`, `WorkflowBranch`,
  `WorkflowStep` (the definition), and `WorkflowRun`, `WorkflowItemRun`,
  `WorkflowStepRun` (what happened on a run). `validate_workflow_parts` is
  the save-time gate that rejects a definition that cannot run
- `agents/auth.py` — `current_jwt`/`current_principal`/`current_base_url`
  contextvars, `principal_from_token`, `XsuaaValidator`,
  `require_user`/`require_admin`/`require_developer` FastAPI dependencies
  (`require_developer` = the `$XSAPPNAME.developer` scope, for the ABAP Assistant)
- `agents/validation_errors.py` — the app-wide answer to a refused request
  (`RequestValidationError`): 422 with `detail[]` of `loc`/`msg`/`type` only,
  never `input`/`ctx`/`url`, on every route (`install_validation_handler`,
  re-exported by `agents.ide.routes`, called by `app.py`). Save-time
  validators name the field instead of quoting the value; routes that
  validate in the handler keep their string `detail`
  (`tests/test_admin_validation_errors.py`)
- `agents/shared.py` — `JWTForwardAuth`, `create_mcp_server` (JWT forward
  on CF / browser OAuth locally / per-user `oauth2`), `SAPAICoreModel`.
  `create_mcp_server` returns a `PerRunMCPServer`: the registry shares one
  server object across all users, so each agent run must open its own MCP
  session, or overlapping runs send requests with whichever user opened the
  shared session (`tests/test_mcp_user_isolation.py`). A remote MCP URL can be
  reached through a BTP destination (`auth_mode="destination"`,
  `_destination_mcp_server`): the destination names the host and holds the
  credential, as the signed-in user when `user_context` is true; no JWT bound
  means a refusal, never an app-level fallback
- `agents/oauth2.py` — per-user OAuth2 authorization_code for
  `auth_mode="oauth2"`: `PerUserOAuth2Auth` (httpx auth that attaches/
  refreshes the user's token, raises `OAuthAuthorizationRequired`),
  `begin_authorization`/`complete_authorization` (PKCE + state),
  `resolve_config`/`_discover_and_register` (RFC 8414/9728/7591 discovery +
  Dynamic Client Registration when the server `oauth` is `{dcr: true}`).
  Tokens in `mcp_oauth_tokens`, flow state in `mcp_oauth_states`, registered
  DCR clients in `mcp_oauth_clients`
- `agents/oauth_routes.py` — `GET /oauth/callback` completes the flow
- `agents/builtins.py` — registry of `builtin:` pseudo-URLs and the factory
  that turns one into a toolset. The set is closed; an unknown `builtin:` URL
  is rejected at admin validation
- `agents/gmail_tools.py` — in-process Gmail tools over the REST API,
  attached when an agent lists the pseudo-URL `builtin:gmail` instead of an
  MCP endpoint. Google's hosted Gmail MCP server refuses every `tools/call`
  from a self-registered OAuth client; see `docs/GMAIL_SETUP.md` (local,
  not in repo). Auth reuses
  `PerUserOAuth2Auth`, so sign-in and refresh are unchanged. On a
  `destination`, `GmailClient(mailbox=)` switches `users/me` to
  `users/{mailbox}` for the app-level case. Drafts only by default;
  `allow_send: true` on an oauth2 Gmail server adds `send_reply`, which
  sends the same threaded reply `create_draft` would save
- `agents/outlook_tools.py` — the same idea over Microsoft Graph
  (`builtin:outlook`), with an Inbox subfolder as the queue instead of a
  label. `build_http_client` picks per-user, app-only or destination auth;
  `teams_tools` reuses it. Built and unit-tested but **never run against a
  real mailbox**: `docs/OUTLOOK_SETUP.md` and `scripts/probe_outlook.py`
  cover the tenant gates that have to clear first
- `agents/mail_render.py` — markdown subset → Outlook-safe HTML for the mail
  this app *originates* (`send_mail`, `create_mail_draft`). Nested tables and
  inline styles only: Outlook on Windows lays mail out with Word's engine.
  The subject's ` -- ` tail becomes the header subline, the opening paragraph
  becomes the verdict callout, and the tool's `status` (`ok`/`attention`)
  tints it. Replies stay plain text — a report shell on an answer to a person
  would read as a newsletter. `MailTheme` (validated by `from_config`) restyles
  band/accent/links/headings/tables/font, adds a logo or org name and replaces
  the footer, from an optional `theme` object in a `builtin:smtp`/`builtin:outlook`
  server's config; status tints stay fixed, and no theme renders byte-identically
- `agents/teams_tools.py` — Teams channels over Microsoft Graph
  (`builtin:teams`). One `team` (and optionally `channels`) is pinned in
  config, never a tool argument. `oauth2` reads and, with `allow_send`,
  posts as the signed-in user; `app_only` is read-only because Graph refuses
  application posts; `destination` follows `user_context` (posting only as
  the user). Unit-tested only; setup notes are in the module docstring
- `agents/slack_tools.py` — Slack over the Web API (`builtin:slack`), as a
  bot. Slack has no client-credentials grant, so the `xoxb-` token lives in a
  BTP destination (NoAuthentication + `URL.headers.Authorization`, which
  `agents/destination.py` sends as a static header). Scope is the channels the
  bot is in, or a pinned `channels` list; posting needs `allow_send`, and
  posted text is escaped so it cannot `<!channel>` or mention anyone.
  Unit-tested only; Slack + BTP setup guide in `SLACK_SETUP.md`
- `agents/smtp_tools.py` — report mail over SMTP (`builtin:smtp`), with host
  and credential from a BTP destination of Type `MAIL` (`destination` mode
  only, Internet proxy only). One tool, `send_mail`, with outlook's contract:
  `allow_send` must be `true`, the audience is the pinned `recipients`, an
  optional `from` overrides `mail.smtp.from`, and the body goes through
  `agents/mail_render.py`. STARTTLS or implicit TLS with the certificate
  always verified; a login is never sent unencrypted. Stdlib `smtplib` in a
  thread. Unit-tested only; setup notes in the module docstring
- `agents/destination.py` — resolves a BTP destination (URL + ready
  `Authorization` header) from the destination service, cached until its
  token nears expiry. `resolve_properties()` returns the raw
  `destinationConfiguration` instead (for `MAIL` destinations, which have no
  URL), same cache rules, with a `repr` that masks credential values. Stores no credential for the target: the destination
  holds it. Binding comes from `VCAP_SERVICES` or `DESTINATION_*` env vars.
  `resolve(user_token=, principal=)` sends the user's JWT as `X-user-token`
  so a user-propagating destination (OAuth2UserTokenExchange, OAuth2JWTBearer,
  OAuth2SAMLBearerAssertion, PrincipalPropagation) returns that user's token;
  per-user results live in a bounded LRU keyed by principal, apart from the
  app-level entry. `Destination.auth_type` echoes the `Authentication` type
  for diagnostics; `require_credential=False` accepts a bare-URL destination
  (public targets)
- `agents/destination_auth.py` — `DestinationAuth` (httpx auth) and
  `destination_http_client`: a built-in issues requests against
  `PLACEHOLDER_BASE` (`https://destination.invalid`), the auth resolves the
  destination per request (as the signed-in user when `user_context`),
  rewrites the URL onto the destination's, sets its headers, retries once on
  401, and refuses to send the credential to a host the destination did not
  name. `DestinationUserRequired` (a `DestinationError`, deliberately not
  `OAuthAuthorizationRequired`) is raised when `user_context` is on and no
  JWT is bound -- scheduled runs. Every built-in accepts
  `auth_mode="destination"` with `{destination, user_context}` plus its pinned
  keys; `user_context=false` keeps the app-only rules (mailbox required,
  Teams read-only). Storage keys per built-in are `_DEST_KEYS_BY_URL` in
  `agents/db.py`; save-time rules are `_validate_destination_config` in
  `agents/admin.py`; `GET /admin/api/credential-health` reports
  destination servers under `destinations`
- `agents/jira_tools.py` — in-process Jira tools over REST v2
  (`builtin:jira`), reached through a destination. JQL is built server-side
  from pinned `project`/`status` and a `lookback` ceiling; issues this
  account already commented on are skipped, so runs are repeatable.
  `add_comment` is registered only when `allow_comment` is set.
  See `docs/JIRA_SETUP.md`
- `agents/lookback.py` — the shared `parse_lookback` window parser
- `agents/sapnotes_tools.py` — SAP security notes discovered through the
  public NVD API (`builtin:sapnotes`). `sourceIdentifier=cna@sap.com` is
  pinned in code; the CVSS floor is the one configurable knob. Returns the
  whole backlog by default, because SAP re-releases notes without NVD
  re-publishing the CVE. An optional `destination` supplies a proxy URL and
  a `URL.headers.apiKey` header in place of `NVD_API_KEY`. See
  `docs/SAP_SECURITY_NOTES.md`
- `agents/sapnotedetail_tools.py` — SAP note detail from the private
  `me.sap.com` backend (`builtin:sapnotedetail`), giving the support-package
  level that fixes each note. Authenticates with a browser session cookie
  under `auth_mode="session"`, stored in `mcp_oauth_tokens` and refreshed by
  hand with `scripts/sap_session.py`; Playwright stays off the platform.
  Under `auth_mode="destination"` the cookie moves into the destination's
  `URL.headers.Cookie` property and is refreshed there instead. See
  `docs/SAP_NOTE_DETAIL.md`
- `agents/registry.py` — `build_orchestrator` dynamically constructs the
  orchestrator + delegation tools + specialists from the DB; `Registry`
  singleton with `reload()` for atomic swaps. Attached skills are listed
  (name + description) in the specialist's system prompt; full content is
  served on demand via a per-specialist `load_skill` tool. Agents may list
  `peers` (other agents' names); a second build pass attaches a delegation
  tool per peer to that agent, so a chain can run specialist to specialist
  instead of through the orchestrator. Recursion is bounded by
  `AGENT_DELEGATION_MAX_DEPTH` and a re-entry guard. `AgentConfig.model_name`
  overrides the globally active model per agent
- `agents/deep.py` — opt-in "deep agent" tools per specialist
  (`AgentConfig.deep_json`, parsed by `DeepConfig`): `write_todos`/
  `read_todos`, an in-memory per-run scratchpad (`ls`/`read_file`/
  `write_file`/`edit_file`, capped at 200 files / 256 KB / 4 MB) and a
  `task` tool that runs an ephemeral sub-agent with the parent's toolsets.
  State is one `DeepState` per `RunContext.run_id` in a TTL table; a
  sub-agent gets the parent's state as `deps`, so the two share a plan and
  scratchpad while a peer reached by delegation does not. `task` is omitted
  once `depth >= subagent_max_depth`; concurrency is a semaphore per state
  and depth. `registry.build_orchestrator` adds the `deep_toolset` and, as a
  zero-argument instructions callable, `scoped_deep_instructions` when the
  row's config is enabled, so the text is resolved per run from that agent's
  own config: `task` is described only if the agent was built with it and
  (in an IDE session) the stage allows it. Sub-agents are never registered
  as specialists or peers
  An IDE session binds a `WorkspaceScope` (session id, the session's own
  `DeepState`, `allow_subagents`, `request_limit`, `changed_files`) to the
  `current_workspace` contextvar: while bound, the scratchpad and plan belong
  to the session instead of the run and are shared by every run and agent in it;
  the instructions then describe that shared workspace for the top-level agent
  and every delegate, and `agents/ide/runner.py` adds no deep section of its own
- `agents/run_activity.py` — what an API-triggered run is doing while it
  runs: `job_runner.execute_run` installs a `RunActivity` as the
  `agents.progress` sink, so every tool call (the agent's own, a peer's, a
  deep sub-agent's) and every `write_todos` plan lands in it. Kept in memory
  while live, served by `GET /admin/api/runs/{id}` as `activity`, stored in
  `job_runs.activity_json` when the run ends. The sink sets
  `interactive = False` (`progress.is_interactive`), so a run that needs
  sign-in still fails fast instead of waiting for a click. Every run site
  passes `shared.run_usage_limits()` (`AGENT_REQUEST_LIMIT`, default 200,
  instead of pydantic-ai's 50 that deep sub-agents share with their parent)
- `agents/ide/` — the backend of the **ABAP Assistant** (the UI's name; the
  code, `/ide/api`, the `developer` scope and the role collection
  `ABAP IDE Developer` keep the old naming): a staged chat that analyses,
  designs, plans and **proposes** ABAP changes in a per-session workspace,
  with review comments on documents and diffs.
  **It is read-only for source: nothing is ever written to or activated in
  the ABAP system** (SAP reads and the syntax dry run go through the
  read-only check; the one runtime setting a developer can approve is
  arming a trace, see `approvals.py`). A session has a type:
  `change` (the staged chat above, default) or `diagnose` (stage
  `investigate` only, it never moves; request-changes answers 409
  `revise_not_allowed`, approve 409 `approve_not_allowed`). A diagnose run
  uses the agent `IDE_DIAGNOSE_AGENT` (`abap-diagnostics`),
  `POST .../report` runs the turn that submits a `report` and `POST .../handover`
  opens a new `change` session carrying that report. Setup guide: `docs/IDE_SETUP.md` (local, not in repo)
  - `models.py` — `IdeSession` (owner, target, stage, status, todos,
    `pins_json`: the approved document versions and file revisions, e.g.
    `{"design": 2, "files": {path: revision}}`), `IdeMessage`, `IdeArtifact`
    (`based_on_json`: the pins a document was written from; versions are
    unique per session and kind, index `uq_ide_artifacts_version`, added by
    `init_db` when it can be), `IdeWorkspaceFile` (`revision`,
    `origin_version`, `base_status`, `base_checked_at`), `IdeConventions`
    (per-target conventions, optional ARC-1 destination, `non_production`
    flag), `IdeFinding`, `IdeApproval`, `IdeFileRevision` (every proposed
    source a run wrote, numbered per path, with its syntax-check result),
    `IdeComment` (anchor `file` = path, revision, 1-based line range, or
    `document` = kind, version, 0-based block; `body`, the selected text as
    `quote` (one line, at most 200 chars), `answer`; states `open`,
    `sent` (only while the request-changes run that sent it is in flight,
    `sent_run_id`), `addressed`, `dismissed`; the user dismisses and
    reopens, body edit and delete only while `open`) and `IdeAuditLog`
    (`ide_audit_log`, not a session child: it survives a session purge).
    New columns and tables are added by `init_db` (`_ensure_column`); it
    also gives a pre-revision proposal its revision 1 and, after a rollback
    to 2.18.0 that edited proposals, a new revision for every proposal whose
    text is not its path's latest revision (`_resync_ide_revisions`,
    additive, idempotent)
  - `store.py` — owner-scoped persistence; a session that is not the
    caller's answers 404; `list_all_sessions_meta` is metadata only
  - `paths.py` — abapGit-style paths `src/<TYPE>/<name>.<ext>`; any other
    path is a scratch note
  - `workspace.py` — loads the session's files and plan into a `DeepState`
    around a run and saves them back (`new`/`modified`/`read` states)
  - `basecheck.py` — the SAP base of an object file: `apply_base` sets
    `origin_source`, `origin_version` (the SAP version marker),
    `base_status="sap"`, `base_checked_at` (used by `open_object`, `POST
    .../open`, finding open and refresh); `check_bases` runs at the end of a
    successful change run and reads every object file still without a base
    from SAP as the signed-in user (at most `BASE_CHECK_MAX` = 20 per run):
    found -> `modified` (`read` when equal), `arc1.is_not_found` -> `absent`
    (stays `new`), other ARC-1 errors -> `unknown`; a call that never reached
    SAP (no user token, no configuration) leaves the row unchecked for the
    next run; `absent`/`unknown` rows are asked again once a revision newer
    than `base_checked_at` exists, or, for a row without revision rows, once
    the row was written well after the check (unchecked rows first). A read
    that is no source (`usable_source`: empty, an HTML error page, or a
    one-line not-found text) is
    `unknown`, and the open/refresh routes answer 502 `no_source`
    (finding open uses the same code with 422 when the finding names no
    program that maps to an object: nothing is read; the UI tells the two
    apart by status).
    Conditional UPDATEs; the result is merged into the run's `file` events,
    and the runner shows the wait as a `tool` event `check_sap_base`
    ("Checking SAP base of n objects"; the syntax check likewise as
    `check_syntax`), also stored in the message's activity
  - `syntaxcheck.py` — the ARC-1 syntax dry run of a proposal (`SAPDiagnose`
    `syntax` with the proposed `source`; writes nothing), as the signed-in
    user through `arc1.get_arc1_client(..., policy="change")`, stored per
    `IdeFileRevision` via `store.set_syntax_result`. `check_syntax` runs after
    the base check of a successful change run on every object file whose
    latest revision is unchecked (at most `SYNTAX_CHECK_MAX` = 20, 120 s in
    total; merged into the run's `file` events, never fails the run);
    `check_one` serves `POST /ide/api/sessions/{sid}/file/syntax?path=&revision=`
    (re-checks and overwrites; 422 `not_an_object`/`no_proposal`, 404
    `unknown_revision`, 409 `run_in_progress`, 409 `syntax_check_running`
    while one is in flight for the session). `parse_syntax` fails safe by
    positive recognition: a bare list, or an object with only the known keys
    (list keys, all merged; ok flags, counted only when exactly `true`; a few
    descriptive keys) -- any other key, e.g. `error`, `truncated`, `status`,
    makes it `unavailable`; error messages -> `errors`, warnings alone ->
    `ok` with items; plain text, ARC-1 errors, a parser error (that file
    only), an empty proposal (no call), a missing token or a timeout ->
    `unavailable`, never `ok`. The real payload shape is unverified
    (fixtures in `tests/fixtures/arc1_syntax/` are synthetic)
  - `readonly.py` — `READONLY_POLICY` allowlist and `ReadOnlyGuard`, which
    `registry.build_orchestrator` wraps around every MCP server and built-in
    of a specialist (not the session tools) and which acts while a
    workspace is bound: default-deny by tool name, then argument checks (no
    data preview, SQL, traces, transport mutations). Enforced in code, not
    by prompt. The policy follows the session type: `diagnose` adds the
    `SAPDiagnose` data actions (dumps, traces, gateway errors, OData
    performance, authorization trace) with `user`/`traceUser` filters
    refused; `trace_start`/`trace_cancel` are never forwarded, the guard
    turns them into a stored proposal; `set_sql_trace_state` stays refused.
    Diagnose rights apply to the session target's ARC-1 server only
  - `diagnose.py` — `is_non_production(conventions)`, the ONE switch for
    diagnose: only a target whose conventions row holds `non_production`
    exactly `True` may be diagnosed (missing or unreadable conventions, or
    any other value, mean no; it fails towards production). Diagnose data
    (tool results to the model, run activity, finding detail) is used and
    stored as ARC-1 sent it, which is why it exists only for such a target.
    `DiagnoseRun` is bound on the `current_diagnose` contextvar by the run
    task (set and reset there); a diagnose call without a binding for that
    session is refused; `is_target_server` limits diagnose rights to the
    session target's ARC-1 server
  - `shapes.py` — recognises `SAPDiagnose` result shapes (by tool,
    `action`, `id` and the keys a payload carries) for `findings.py`
  - `findings.py` — turns data results into `IdeFinding` metadata (kind,
    ref id, program, include, line, time; a value is kept only when it has
    the form of an identifier); a dump/gateway-error detail read also
    yields `detail` text, stored with the finding and served only while the
    target is still flagged `non_production`
  - `approvals.py` — variant B: the agent only proposes (a pending
    `IdeApproval` + `approval_required` event); arming happens solely in
    `POST /ide/api/sessions/{id}/approvals/{aid}` with server-built args
    (allowlisted keys, bounds `IDE_TRACE_MAX_EXECUTIONS`/`IDE_TRACE_MAX_HOURS`,
    `traceUser` dropped so ARC-1 traces the signed-in user). Checks run at
    decision time (owner, diagnose, pending, TTL, `non_production` re-read
    -- 403 `target_not_non_production` when gone --, user JWT); a
    conditional UPDATE makes arming exactly-once; an intent
    audit row is written before the ARC-1 call; a timeout ends as
    `arc1_timeout_unknown`; `mark_interrupted` closes rows left by a crash.
    Audit rows go to `ide_audit_log` and the `agents.ide.audit` logger; the
    proposal's description is stored without control characters and with
    e-mail addresses replaced by `[EMAIL]`
  - `stages.py` — `chat -> design -> plan -> propose -> review -> done`, one
    `approve` per step, gates raise `StageGateError` (409, 429 for
    `usage_exhausted`); `IdeSession.status` is the run lock. `approve` pins
    the version it approved (`approve {version}` refuses a stale one with
    `version_changed`) in one conditional UPDATE, and is refused while a
    comment is `open` or `sent` (`open_comments`); later stages read the
    pinned documents. In `propose`, `ApproveBody.revisions` (`path ->
    revision` of the proposals the developer saw) must equal the current
    revisions of every proposed object file, else 409 `version_changed`;
    sent in any other stage it is 409 `stage_changed`. The approve holds the
    session row lock (`store.lock_session_row`) and re-reads the revisions
    after its UPDATE; open, refresh and a finding's open (the only writers of
    a file's `state` outside a run) take the same lock after their ARC-1 read
    and answer 409 `run_in_progress` if a run started meanwhile. A
    request-changes run is allowed in `design`, `plan`, `propose` and
    `review` (`REVISABLE`). The worklist `waiting` reason
    (`store.waiting_for`: `approval`, `comments`, `changes`, `document`,
    first match wins) is always `null` in stage `done`. `build_prompt` puts
    model- and user-written text in the user prompt as delimited data
    (`<session-documents>`, and for a request-changes run `<review-comments>`
    with each comment's anchor -- `<path> revision r lines a-b` or `block n
    of <kind> vN`, the 1-based block the developer saw -- and its `quote`),
    never in the instructions
  - `session_tools.py` — the agent tools of an IDE run:
    `ide_session_toolset()`, one `FunctionToolset` behind `.filtered(...)`,
    which `registry.build_orchestrator` attaches to every specialist (the
    app's own tools: not wrapped in `ReadOnlyGuard`; deep sub-agents do not
    get it). A tool is listed only while the session workspace
    (`current_workspace`) and `current_ide_run` are bound for the same
    session, so chat, A2A, jobs and workflows see none; each tool re-checks
    on call and takes the session from the binding, never from an argument.
    `submit_document(kind, content)` is the only way a stage document comes
    to exist (a final answer is never captured), listed when the run's stage
    takes one (`stages.document_kind`: change `design`/`plan`/`note` in
    propose/`review`; diagnose `report` in a report run only), emits
    `artifact` at once; `open_object(type, name, include?)` (change sessions)
    reads an object from SAP through `Arc1Client` as the
    signed-in user into the workspace with its base and version marker,
    keeping a proposal already there (at most `MAX_SESSION_OBJECTS` = 50
    object files per session, `too_many_objects`; an empty or one-line
    not-found answer is `no_source` and not stored); `resolve_comments([{id, answer}])`,
    listed only in a request-changes run of a change session, moves the
    comments that run sent to `addressed` with a one-line answer (at most
    500 chars) and emits `comments`
  - `runner.py` — `run_stage` runs one message, request-changes or report
    turn in its own task, saves the workspace, emits events; stale-run
    reaper. A request-changes run marks the session's open comments `sent`
    in the transaction that takes the lock (`nothing_to_send` with no
    comment and no note; at most `store.MAX_SENT_COMMENTS` comments /
    `MAX_SENT_CHARS` characters, oldest first, the rest stay `open` and are
    counted as `left`) and, however it ends, returns the comments *it* sent
    and left `sent` to `open` when it releases the lock (scoped by
    `sent_run_id`, as are a reclaim and `resolve_comments`). Comment bodies,
    notes and answers are stored without control or Unicode format characters
  - `sse.py` — frames the runner's events (`run`, `text`, `tool`, `plan`,
    `file` (with `revision`, `base_status`, `syntax_status`), `artifact`
    (when `submit_document` stores it, mid-run), `comments` (`{ids, state}`:
    `sent` at a request-changes start, `addressed` per `resolve_comments`,
    `open` for what the run hands back at its end), `finding`,
    `approval_required`, `usage`, `error`, `done`) with a `: ping`
    heartbeat
  - `review_routes.py` — the review side of `/ide/api`: comments (list by
    `state`, create (422 `invalid_anchor`), `PATCH` body xor `state`
    `open`/`dismissed` (409 `comment_not_editable` / `invalid_transition`),
    delete while open), file revisions, a message's run activity (`GET
    .../messages/{mid}/activity`; the message list carries only
    `has_activity`), and `POST /sessions/{id}/request-changes` (optional
    `note`; SSE; replaces the removed `/revise`)
  - `schemas.py` — the typed request/response models of `/ide/api`; every
    JSON route declares its `response_model` (`SessionOut` carries
    `target_non_production`, `pins`, `waiting`, `open_comments`,
    `unresolved_comments` and the worklist fields `objects` (at most 5
    names), `objects_total`, `changed_objects` and, for diagnose,
    `findings_count`, computed in grouped queries, never one per session).
    `scripts/export_ide_schema.py` writes `$defs` plus an `x-routes` map
    (`"<METHOD> <path>"` -> request/response model, list and stream flags,
    built from the real routers) to
    `ui5-ide/webapp/test/contract/ide-api.schema.json`; the committed file
    must equal the script output (`tests/test_ide_schemas.py`), so
    regenerate after every model or route change. Stored enum values
    outside the contract are coerced with a WARNING, never a 500
  - `routes.py` — REST under `/ide/api`, all behind `require_developer`;
    every `/sessions/{sid}` route loads the session owner-scoped first (404
    for another user's) and child rows by id and session. Sessions (list,
    create, detail with artifact and file summaries, rename via `PATCH`,
    delete, `cancel`), messages (SSE), `approve` (body `version` and, in
    propose, `revisions`), artifacts, files (`GET .../file?path=&revision=`,
    refresh, lint, `POST .../file/syntax`), `GET /me` (principal, `is_admin` hint,
    `targets`, `diagnose_targets`, `diagnose_retention_days`). Conventions:
    `POST /conventions` creates a target (409
    `target_exists`), `PUT /conventions/{target}` only updates (404
    `unknown_target`) and takes a `clear` list of fields to empty. The
    conventions writes, `GET /admin/sessions` and `POST /admin/seed/refresh`
    need the developer AND the admin scope (`require_admin` on the route on
    top of the router's `require_developer`; the approuter's `/ide/api`
    route already demands `developer`). Setting or changing the
    `non_production` flag is audited (`conventions_flag`), and so is setting,
    changing or clearing the `destination` of a flagged target
    (`conventions_destination`), in the write's own transaction. A refused
    request body is a 422 `detail[]` of `loc`/`msg`/`type` only, never the
    input (`agents/validation_errors.py`; a lone
    surrogate echoed back made it a 500); session titles are stored as
    one-line plain text.
    `POST .../open` and `GET /objects/search` stay but the UI no longer
    calls them. Diagnose adds `POST
    /sessions/{id}/report`, `/handover`, `GET .../findings[/{fid}]`
    (stored detail, live refresh), `POST .../findings/{fid}/open` (finding
    to source) and `GET .../approvals`, `POST .../approvals/{aid}`; `type:
    diagnose` is accepted only for a `non_production` target (422). A
    diagnose session whose target lost the flag afterwards (or its
    conventions row) answers 409 `target_not_non_production` for message
    and report runs (`stages.assert_can_run`, again in `runner._start`),
    handover, finding detail and open, file refresh/lint/syntax and `POST
    .../open` (approving a trace: 403); stored
    messages, artifacts, the finding list, the approval list, a denial and
    delete keep working. Change sessions never look at the flag.
    Retention: change sessions `IDE_SESSION_RETENTION_DAYS` from last
    update, diagnose `IDE_DIAGNOSE_RETENTION_DAYS` (14) from creation, audit
    rows `IDE_AUDIT_RETENTION_DAYS`; pending approvals expire after
    `IDE_APPROVAL_TTL_MIN`. These are read at import through
    `store.env_int` (a non-integer falls back to the default with a
    warning); `IDE_DIAGNOSE_RETENTION_DAYS=0` keeps raw diagnose text
    forever and is logged as a WARNING at startup (`GET /me` serves the
    effective value as `diagnose_retention_days`); `IDE_TRACE_ARM_TIMEOUT_S`
    is floored at 5
  - `arc1.py` — direct ARC-1 calls (open, refresh, lint, search, the
    syntax dry run, `open_object`, the base check) as the
    signed-in user through the target's destination; runs the read-only
    check first; no user token means 424; `arm_trace`/`cancel_trace` are
    the only way `trace_start`/`trace_cancel` reach ARC-1. `read_version`
    reads the version marker (`SAPRead type=VERSIONS`, newest revision's
    `id`/`uri`/`date`, `None` for any other answer) and `is_not_found`
    decides "object does not exist" (only an object-shaped message --
    "<object word> <NAME> does not exist / not found", nothing after it,
    behind at most three `prefix: ` segments plus an optional `ADT HTTP <n>:`,
    with a name that looks like an object name rather than an English word --
    that names no user/authorisation/destination/target/tool/token, whatever
    the status or code; or a 404 with no message at all; L1 replaces the
    text rule with the real payload). Without a destination the URL
    comes from `IDE_ARC1_URL_<TARGET>`
  - `seed.py` + `seed.ide.json` — `ensure_ide_seed` inserts the IDE agents
    and skills whose name does not exist yet and never touches existing
    rows; `IDE_SEED=false` disables it (the code default is on; `mta.yaml`
    sets it `false`, a landscape turns it on in its `.mtaext`). Seeds the
    `abap-diagnostics` agent and the skills `abap-dump-analysis`,
    `abap-performance-trace`, `abap-authorization-analysis`.
    `refresh_ide_seed` (`POST /ide/api/admin/seed/refresh`, developer AND
    admin scope,
    ignores `IDE_SEED`) brings an already-seeded database up to the shipped
    texts: missing names are added, a stored `instructions`/`content` whose
    SHA-256 is listed in `seed_history.json` (every text ever shipped, per
    name) is replaced (conditional UPDATE, nothing else on the row changes),
    any other text is an admin edit and is reported as `skipped_edited`;
    the registry and chat app are reloaded when something changed. A reload
    that fails after the rows were committed is logged and answered as the
    normal result with `reload_failed: true` (default `false`), not a 500. After
    editing `seed.ide.json` run `scripts/ide_seed_history.py` (a test fails
    until the new hashes are listed). Seed version 2 names the session
    tools (`submit_document`, `resolve_comments` in request-changes runs,
    `open_object`) and the syntax dry run (`SAPDiagnose action=syntax` with
    `source`; "unavailable" is not checked); the review-stage prompt lists
    each pinned file as `path (revision n): syntax <status>` with its stored
    messages
- `ui5-ide/` — the **ABAP Assistant**, a freestyle SAPUI5 1.120 (TypeScript)
  app built like `ui5-admin/`, deployed to the HTML5 Application Repository
  and served at `/ui5ide`; its backend is `/ide/api` (approuter
  `/ui5ide/backend/...`). Needs the `developer` scope. Two routes:
  - **Worklist** (`#/`, `#/sessions`; `view/Worklist.view.xml`): the
    caller's sessions with type, system, stage, status (the server's
    `waiting` reason worded "Trace request pending" / "Comments answered" /
    "Changes to review" / "Document to approve"), listed objects or finding
    count; search, type and stage filters, "Waiting for me"; new session
    (the dialog offers diagnose only for `diagnose_targets` from `GET
    /me`), rename, delete, conventions.
  - **Session page** (`#/sessions/{id}?view=document|changes|source|findings`,
    plus `path`/`line`/`kind`/`version`; `view/Session.view.xml` and the
    `fragment/SessionHeader|Conversation|DocumentView|ChangesView|SourceView|
    Findings|CommentPopover|RequestChangesDialog` fragments): the
    conversation (streamed answer, per-message run activity, plan) beside
    the artifact column. The document view renders design/plan/review/
    report markdown (vendored marked + DOMPurify, `model/markdown.ts`) by
    version with "based on" links and block comments; the changes view
    shows each proposed object as a diff against its SAP base (vendored
    jsdiff) by revision, with line-range comments, the base status ("New
    in SAP", "Changed vs SAP version …", "Base not checked"), the syntax
    status of the revision ("No syntax messages", warnings/errors with
    their lines, "Syntax not checked" for `unavailable` -- never "OK"),
    "Check syntax", and "Open in ADT" (`model/adtLink.ts`: an `adt://`
    link whose project is the target name; none for FUNC or unknown types).
    The source view is a read-only line table; diagnose sessions show a
    banner, the findings list (opens the source at the line, detail
    dialog) and Report / Handover. "Request changes (n)" sends the open
    comments and an optional note; Approve sends the version or revisions
    shown (changes are approved only from the changes view) and explains
    an `open_comments` refusal. A proposed trace (`approval_required`, or a
    pending row from `GET .../approvals`) appears as an approval card that
    lists every server-built value the decision covers, with separate
    Approve and Reject buttons and no default action; a decided approval is
    one read-only status line.
  - Conventions dialog: admins create a target (`POST /conventions`), edit
    fields and clear them explicitly (`clear`), and set or remove the
    `non_production` flag behind a confirmation that names what diagnose
    sends and how long it is kept (`diagnose_retention_days`); others read.
  `model/RunController.ts` owns a run's lifecycle (start, stream, stop,
  watch a run the page does not stream; typed callbacks, no `sap.m`).
  All HTTP goes through `service/IdeService.ts`, which sends the
  approuter's CSRF token on every non-GET call (fetched with `X-CSRF-Token:
  Fetch` on `GET me`, one retry on a `Required` 403, then `csrf_failed`;
  nothing is sent locally). Tests: `npm test` (tsc + karma QUnit/OPA5 on
  `test/integration/FakeBackend.ts`); `test/unit/contract.qunit.ts` drives
  the real `IdeService` against the fake and validates every request and
  answer against `test/contract/ide-api.schema.json`, and the fake's route
  table must equal its `x-routes` (minus the routes the UI never calls).
  `npm run test:e2e` runs the Playwright specs in `e2e/` against the real
  local backend (`app.py` on a throw-away `_e2e_registry.db`, rows seeded
  with `sqlite3`, only model/SAP calls answered by `page.route`);
  `npm run test:e2e:stream` (`playwright.stream.config.ts`, `ui5-stream.yaml`)
  runs `e2e/real-stream.spec.ts` with no mocked routes against
  `tests/e2e/ide_stream_server.py` (see Dependencies)
- `agents/chat_app.py` — `DynamicChatApp` ASGI wrapper that forwards to
  the current `Agent.to_web()` and is rebuilt on reload
- `agents/workflow_runner.py` — runs a workflow: the declared main line, a
  fan-out step returning `WorkItem`s, and per-item branches the fan-out step
  selects. Mirrors `job_runner.py` (task set, start lock, never-raising
  `_finalize`, shutdown cancel). Steps hand plain text to each other; the join
  step sees one `## From` block per branch taken. See
  `docs/superpowers/specs/2026-08-31-agent-workflows-design.md`
- `agents/step_kinds.py` — the deterministic step kinds a `WorkflowStep.kind`
  can name besides `agent`: `condition` (first matching rule wins; `stop`
  ends the main line successfully, skips the rest of a branch for one item,
  or skips the rest of the join), `transform` (extract_json → regex →
  template → truncate), `http` (a BTP destination via `agents.destination`,
  confined relative path) and `python` (admin-authored code in a
  `python -I -S -E` subprocess started by `agents/_python_step_runner.py`:
  import allowlist, no `open`, empty environment, temp cwd, RLIMIT_AS/CPU,
  killed on timeout — a guard against mistakes, not against a hostile
  admin). Pydantic config models double as the save-time gate
  (`validate_step_config`, called from `validate_workflow_parts`); the
  runner calls `execute_step` and hands the output on as `<kind>#<position>`.
  Templates know `{{text}}`, `{{item.x}}`, `{{json.x}}`, `{{source.NAME}}`
  and are substituted, never evaluated. Mirrored in the UI by
  `ui5-admin/webapp/model/stepKinds.ts` and the step editors in both admins
- `agents/admin.py` — FastAPI `/admin` router: agent + skill + workflow CRUD,
  reload, restart, import/export, seed-on-startup
- `agents/a2a.py` — A2A (Agent-to-Agent) protocol server: agent card at
  `/.well-known/agent-card.json`, JSON-RPC at `/a2a` (`message/send`,
  `message/stream`, `tasks/get`, `tasks/cancel`). Used by SAP Joule.
- `agents/cf_api.py` — CF v3 API restart helper (optional, password grant)
- `templates/admin.html` — Admin UI (single-page, vanilla JS)
- `ui5-admin/` — SAPUI5 (TypeScript) rebuild of the admin UI, deployed to the
  BTP HTML5 Application Repository and served at `/ui5admin`. Runs **alongside**
  `templates/admin.html`, which is unchanged and still the supported admin at
  `/admin`. All HTTP goes through `webapp/service/AdminService.ts`; see
  `docs/UI5_ADMIN.md`. The server dialog's toolset dropdown comes from
  `webapp/model/builtins.ts`, which mirrors `agents/builtins.py` and lists the
  auth modes the server accepts per built-in
- `agents.seed.json` — Initial config imported when DB is empty
- `mta.yaml` — adds `postgresql-db` resource; version 2.1.0 adds
  A2A env vars (`A2A_PUBLIC_URL`, `A2A_AGENT_NAME`, …); 2.7.0 makes the
  AI Core resource group the `aicore-resource-group` parameter (override
  per landscape in an `.mtaext`) and adds a `before-start` hook running
  `scripts/ensure_aicore_setup.py`. Needs `_schema-version: "3.2"` for
  `hooks`; 2.17.0 adds the `ui5-ide` HTML5 module and the IDE env vars
  `IDE_SESSION_RETENTION_DAYS` (0 disables cleanup), `IDE_SESSION_REQUEST_CAP`,
  `IDE_ORCHESTRATOR_AGENT` and `IDE_SEED`. Not in the descriptor (set per
  landscape in an `.mtaext`): `IDE_RUN_TIMEOUT_S`, `IDE_RUN_STALE_S`,
  `IDE_RUN_HEARTBEAT_S`, `IDE_SAVE_TIMEOUT_S`, `IDE_ARC1_URL_<TARGET>`;
  2.18.0 adds `IDE_DIAGNOSE_AGENT`, `IDE_DIAGNOSE_RETENTION_DAYS`,
  `IDE_AUDIT_RETENTION_DAYS`, `IDE_APPROVAL_TTL_MIN`, `IDE_TRACE_MAX_EXECUTIONS`,
  `IDE_TRACE_MAX_HOURS`, `IDE_TRACE_ARM_TIMEOUT_S`; 2.19.0 is the ABAP
  Assistant release (review comments, revisions, pins, the reworked
  `ui5-ide`): no new env vars, the new IDE columns and tables are added by
  `init_db` at start
- `scripts/ensure_aicore_setup.py` — creates the `AICORE_RESOURCE_GROUP`
  resource group if missing, idempotent, no-op for `default`. Creates the
  group only; model deployments stay in `scripts/deploy_claude.py` because
  they bill and take minutes. Deployments are per group, so a fresh group
  has no models until that script runs against it. See
  `docs/AICORE_RESOURCE_GROUP.md`
- `xs-security.json` — `admin`, `user`, `a2a` and `developer` scopes with
  matching role templates and role collections (the `developer` one is
  still named `ABAP IDE Developer`; its descriptions say ABAP Assistant)
- `approuter/xs-app.json` — `/admin` requires admin scope, `/a2a`
  requires `a2a` scope, `/ui5ide` and `/ide/api` require `developer`,
  `/.well-known/agent-card.json` is anonymous. The two IDE backend routes
  (`/ui5ide/backend/...` and `/ide/api/...`, also in `ui5-ide/xs-app.json`)
  have `csrfProtection: true`; every other route has it off
- `JOULE_A2A.md` — configuration guide for BTP + Joule Agent Hub

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
4. `POST /api/workflows/{slug}/run` is the second scheduler entry point
   alongside `POST /api/agents/{slug}/run`: both acknowledge within the BTP
   Job Scheduling Service's 15s synchronous budget and run in the background
5. `POST /ide/api/sessions/{id}/messages` (and `/request-changes`, `/report`)
   answers with an SSE stream: the route takes the session's run lock (a
   request-changes start also marks the open comments `sent`), `run_stage`
   binds the `WorkspaceScope` and the IDE run, runs the orchestrator with
   read-only-guarded MCP toolsets plus the session tools, and relays events
   until `done`; a change run that did not fail ends with the SAP base
   check and the syntax dry run, and however a run ends, the comments it
   left `sent` go back to `open`.
   `approve` moves a change session one stage on and pins what it approved:
   refused while a comment is `open` or `sent` (409 `open_comments`), or
   when the `version` (documents) or `revisions` (propose) the UI sends are
   no longer the latest (409 `version_changed`)
6. A diagnose run may end with `approval_required` (a proposed trace): the
   developer answers with `POST /ide/api/sessions/{id}/approvals/{aid}`
   (`approve`/`deny`); only that route arms the trace

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
All pinned in `requirements.txt` to the versions the suites last ran on;
bump a pin, rerun the suites, then deploy.
- `pydantic-ai[mcp,web,openai,bedrock]`, `sap-ai-sdk-gen[all]`, `mcp`,
  `httpx`, `uvicorn`, `python-dotenv` (pip warns that the `pydantic-ai`
  meta package declares none of those extras; the imports work because it
  pulls them in anyway)
- `fastapi`, `jinja2`, `python-multipart` — admin UI
- `sqlalchemy[asyncio]`, `asyncpg` (Postgres on CF), `aiosqlite` (the
  local SQLite fallback) — dynamic agent storage
- `pyjwt[crypto]` — XSUAA JWT validation
- Test-only: `pytest`, `pytest-asyncio` (`pytest.ini` sets
  `asyncio_mode = auto`), `jsdom` via the root `package.json`, `ruff`
  (`ruff.toml`, advisory in CI). Script-style suites patch
  `create_mcp_server` and `Agent.__init__` at import; `tests/conftest.py`'s
  `real_agents_and_mcp` fixture restores the real ones for tests that build
  real agents. After a pydantic-ai bump rerun the deferred-tool spike with
  `IDE_SPIKE_ALL=1 .venv/bin/python -m pytest tests/test_ide_deferred_spike.py`
- Real-stream e2e backend (test-only, never imported by `agents/` or
  `app.py`): `.venv/bin/python tests/e2e/ide_stream_server.py` serves the
  real app on `127.0.0.1:7933` with a throw-away `_e2e_stream.db`, target
  `DEMO`, a scripted stage-aware `FunctionModel` in place of the IDE agents
  and an in-memory ARC-1 (`FakeSap`); `tests/test_ide_stream_server.py`
  proves the script through `runner.run_stage`; it backs
  `ui5-ide/e2e/real-stream.spec.ts` (`npm run test:e2e:stream`)
- `@ui5/cli`, `ui5-tooling-transpile`, `@sapui5/types`, `karma-ui5`,
  `@playwright/test` — UI5 apps (dev-only; not in `requirements.txt`).
  `ui5-ide` uses no `sap.ui.codeeditor` (sources are plain HTML tables) and
  loads vendored, CDN-free copies of marked, DOMPurify and jsdiff from
  `webapp/vendor/` (versions and licences in its `README.md`; `diff` is a
  pinned devDependency copied by `npm run vendor:diff`)
