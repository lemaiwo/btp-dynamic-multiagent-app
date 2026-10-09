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
- **Storage**: SQLAlchemy async on one of two databases, chosen per
  landscape at deploy time: PostgreSQL (BTP `postgresql-db` service,
  `asyncpg`) or SAP HANA Cloud through an HDI container (`hana` service,
  plan `hdi-shared`, `hana+aiohdbcli`). SQLite is the local and test
  default. The models are the schema on all three
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
  the save-time gate that rejects a definition that cannot run. Two more
  tables back `builtin:odata`: `ODataService` (`odata_services`, the
  catalogue: identity columns plus `definition_json`, one `updated_at` per
  row stamped by `update_odata_service`) and `ODataAuditLog`
  (`odata_audit_log`, the write audit). `validate_odata_service` (re-exported
  from `agents/odata/models.py`) is the save-time gate of a catalogue
  service. The `builtin:odata` entry of an agent is exactly
  `{"url": "builtin:odata", "auth_mode": "destination", "oauth": {"services":
  [...], "allow_write": true}}` (`_clean_odata_entry`): 1 to 50 distinct
  service slugs, `allow_write` stored only for the JSON boolean `true`, no
  destination and no `user_context` (those belong to each catalogue service),
  every other key dropped; an agent has at most one such entry, and
  `check_odata_services` refuses an unknown service name at every agent
  write, under a row lock. A `builtin:bitbucket` entry has its own cleaner
  too (`_clean_bitbucket_entry`, which is `bitbucket_config.clean_entry`
  after the mode check; no fallback to the row being replaced: the block
  holds no secret, so a switch an edit leaves out is gone), is stored under
  the canonical URL, and `prepare_servers` refuses a second one in any
  spelling (`BITBUCKET_SINGLE_ENTRY_MESSAGE`).
  **Which database** (`_resolve_database`, read once at import): a bound
  service wins over `DATABASE_URL`. One kind bound (a postgres label, or
  `hana`; of several `hana` bindings the HDI container: plan `hdi-shared` or
  credentials with an `hdi_user`) is used; with both bound the import fails
  with `DatabaseConfigError` unless `DB_KIND` (`postgres` | `hana`) names
  one, and a `DB_KIND` naming a kind that is not bound fails too (never
  answered with the other one; `DB_KIND` is ignored when nothing is bound).
  **On Cloud Foundry (`VCAP_APPLICATION` set) the app never ends on
  SQLite**: unparseable `VCAP_SERVICES`, a database binding whose
  `credentials` is not an object, no database binding and no `DATABASE_URL`,
  or a SQLite `DATABASE_URL` are a `DatabaseConfigError` that names no value;
  locally the same environment still falls back to the SQLite file. Locally
  `DATABASE_URL` may be a `hana://` / `hana+aiohdbcli://` URL (the container
  schema as `currentSchema`), with the design-time user in `HANA_HDI_USER` /
  `HANA_HDI_PASSWORD`. A HANA connection is always encrypted and its
  certificate validated: every spelling of `encrypt`,
  `sslValidateCertificate` and `sslHostNameInCertificate` in a URL is dropped
  (the driver reads its options in any case) before the fixed values are
  set, with one WARNING when the URL asked for less; there is no insecure
  switch as `PG_SSL_INSECURE` is for Postgres; the binding's `certificate` is
  the trust store, passed in memory. `_target_from_url` and `bound_target`
  build a target outside the app's own choice (for
  `scripts/copy_registry_config.py`).
  `init_db` has two schema steps: `_create_and_migrate` (Postgres, SQLite:
  `create_all` plus the additive `_ensure_*` chain and the two IDE data
  repairs, unchanged) and `_deploy_hana_schema` (HANA: `agents/hana_hdi.py`
  instead of all of that; the repairs are for rows of versions that never ran
  on HANA and are not run). The orchestrator row is ensured on all three.
  **On HANA the models are the only DDL: a new NOT NULL column needs a
  `server_default`** (HDI adds it to a table that holds rows;
  `tests/test_hana_hdi.py` pins the NOT NULL columns without one that exist
  today and fails for a new one), and every change of a model needs a new
  schema generation (below).
  **Statements must run on all three databases.** HANA has no `UPDATE` /
  `DELETE ... RETURNING` and cannot compare, order, group or `DISTINCT` a
  `Text` column (it is `NCLOB`; `IS NULL` and `length()` are fine), and it
  has no `SELECT` without `FROM` (`app.HEARTBEAT` is a `select()`, not
  `text("SELECT 1")`). The helpers: `has_row_locks(session)` (false on
  SQLite only: every `FOR UPDATE` that used to be "Postgres only" applies on
  HANA too), `compares_lobs(session)` (false on HANA only) and
  `text_unchanged(session, column, seen, *row)`, the WHERE term of a
  compare-and-set on a `Text` column: `column == seen` on Postgres and
  SQLite (the value typed `ComparedText`, which is `Text` with a name), on
  HANA a `FOR UPDATE` read compared in Python, the lock held to the end of
  the caller's transaction (session pins, approve, seed refresh).
  Instead of `RETURNING`, `agents/ide/store.py` selects the rows `FOR UPDATE`
  in id order, writes under the same conditions and reads which of them the
  write reached. **The whole suite checks this**: `tests/conftest.py` compiles
  every statement a test executes on the SQLite engine for the HANA dialect
  as well (`tests/hana_sql.py`) and fails the test that executed one HANA
  would refuse (about 9% of the run time; `HANA_SQL_CHECK=0` switches it
  off). Exempt are exactly the `ComparedText` compare and the one statement
  of `_resync_ide_revisions`, neither of which is ever sent to HANA.
  HANA hands timestamps back naive (UTC), as SQLite does
- `agents/hana_hdi.py` — the schema in an HDI container. The app's runtime
  user may only read and write rows (any DDL fails), so tables exist only as
  design-time artifacts made by the container's design-time user
  (`hdi_user`). `artifacts(metadata)` generates them from the models
  (nothing is committed as `.hdbtable`, there is no Node deployer module):
  one `.hdbtable` per table (`COLUMN TABLE`, columns and primary key only,
  `DATETIME` written `TIMESTAMP`), one `.hdbindex` per index and per unique
  constraint (the partial `api_slug` indexes are plain unique ones: HANA
  allows several NULLs), one `.hdbconstraint` per foreign key, plus
  `.hdiconfig` and `.hdinamespace`, all under `src/`; deterministic, and a
  model construct without an artifact (unnamed unique constraint, CHECK) is an
  `HdiError` in the unit tests rather than at deploy.
  `deploy(credentials, metadata)` runs `deploy_files` in a thread (sync
  `hdbcli`), under HDI's container lock (`#DI.LOCK`). Measured on a
  container: while one connection holds the lock, a second one's `LOCK`
  times out (driver error 131) before and after the holder's `WRITE`,
  `DELETE` and `MAKE`, and gets the lock when the holder commits; so two
  instances that start together run one after the other, and the second
  finds nothing to do. That is as far as the serialisation goes: it covers
  deployers that take this lock, not another tool writing to the container.
  The wait is 30 s (`LOCK_WAIT_MS`), chosen to fit inside Cloud Foundry's
  default 60 s start timeout (a first deploy of the whole schema measured
  3 s, a redeploy 3-5 s, the check of a current container about 1 s); an
  instance that waits longer fails its start with a clear error. Nothing is
  retried. Under the lock: read the container's record; `LIST_DEPLOYED`;
  when paths and SHA-256 already match nothing is made; otherwise `WRITE`,
  `DELETE` and `MAKE` (deploy the generated set, undeploy what is deployed
  and no longer generated). A message row of severity `ERROR` is the failure
  (HDI's procedures do not raise).
  **What a transaction does there (measured):** the lock lives in the
  client's transaction, so the connection is switched off autocommit
  (hdbcli's default is on, and the lock would be gone at once); but HDI
  commits each of its calls itself: a rollback takes back neither a `WRITE`
  or `DELETE` in the design-time file system nor a successful `MAKE`. A
  `MAKE` is atomic in itself, the file system is not. So a deploy never
  relies on rollback: it deletes exactly the files that are in the file
  system and not generated (HDI refuses to delete a file that is not there)
  and undeploys exactly what is deployed and not generated, and so succeeds
  from whatever a failed deploy left behind.
  **Two guards against an older app version** (a rollback, an old instance
  during a rolling deploy), which would otherwise drop the newer tables and
  columns with their rows where Postgres just runs old code on a superset
  schema: (1) `HANA_SCHEMA_GENERATION`, a counter in the module;
  `agents/hana_schema_history.json` pins the digest of each generation's
  artifact set and a unit test fails when the models change without a new
  generation (raise the counter by one, run
  `scripts/hana_schema_history.py`, commit both). The container keeps a
  record, `meta/generation.json` in its design-time file system, read and
  written under the lock: `{generation, made, digest}`. A deploy writes it
  as "not made" before its make and as "made" after the make succeeded
  (HDI commits every call itself, so no transaction could keep record and
  schema in step); `digest` identifies the artifact files and is compared
  with what `LIST_DEPLOYED` reports. A record of a HIGHER generation that
  is made and whose digest is what is deployed: the container is not
  touched at all (no write, no make, no undeploy, one WARNING naming both
  generations) and the app starts on the newer schema. A higher generation
  that is NOT made (its make failed or was interrupted), or whose digest is
  not what is deployed: `HdiError`, nothing changed and the app does not
  start; an older version neither deploys over it nor runs on it, and the
  version of that generation finishes it. Same generation: the unchanged
  shortcut when the files match (the record is brought to "made"), else a
  deploy; that is also how a failed first make recovers. **The guard fails
  closed**: a record that is there and cannot be read, or a `LIST` that
  answers any error other than HDI's file-not-found codes, is an `HdiError`,
  never "no record"; and a record is never replaced by a lower generation.
  (2) Whatever the generations say, a deploy that would undeploy an
  `.hdbtable` is refused before anything is written unless
  `HANA_HDI_ALLOW_DROP` is exactly `true`; indexes and constraints are
  undeployed without it. Errors carry HDI's messages (cut, credential values
  and the container schema name replaced) or the driver's error code, never
  the driver's text, a password, the certificate or a URL. No `schema` plan
  (no design-time user)
- `agents/auth.py` — `current_jwt`/`current_principal`/`current_base_url`
  contextvars, `principal_from_token`, `XsuaaValidator`,
  `require_user`/`require_admin`/`require_developer` FastAPI dependencies
  (`require_developer` = the `$XSAPPNAME.developer` scope, for the ABAP Assistant)
- `agents/validation_errors.py` — the answer to a refused request
  (`RequestValidationError`) on every route of the FastAPI app: 422 with
  `detail[]` of `loc`/`msg`/`type` only, never `input`/`ctx`/`url`
  (`install_validation_handler`, re-exported by `agents.ide.routes`, called
  by `app.py`). Not involved: the mounted chat sub-application (its own
  handlers, never raises this error), routes that read the body themselves
  (A2A; the session-cookie route answers 400) and routes that validate in
  the handler and answer a string `detail` (OData catalogue, workflow
  gate). `msg` is replaced by fixed text for pydantic types that embed the
  input (`union_tag_invalid`); a `loc` part that does not look like a field
  name becomes `<unknown field>`. Save-time validators name the field or
  position, never the value (`tests/test_admin_validation_errors.py`)
- `agents/shared.py` — `JWTForwardAuth`, `create_mcp_server` (JWT forward
  on CF / browser OAuth locally / per-user `oauth2`), `SAPAICoreModel`.
  `create_mcp_server` returns a `PerRunMCPServer`: the registry shares one
  server object across all users, so each agent run must open its own MCP
  session, or overlapping runs send requests with whichever user opened the
  shared session (`tests/test_mcp_user_isolation.py`). The OpenAI-family
  model client retries a refused request (429, 5xx, connection errors)
  `AICORE_MAX_RETRIES` times (default 8, about 40 s of backoff, so a
  per-minute rate limit can clear; `0` switches retries off; read when a
  model is built, and models are cached per name, so a change needs a
  restart; `tests/test_model_retries.py`). A remote MCP URL can be
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
  is rejected at admin validation. `builtin:odata` is the one whose entry
  names no destination and the one factory that also receives the catalogue
  snapshot the registry loaded; it is always built with the storing audit
  recorder (`stored_recorder()`), so no caller can build it with writes that
  are not recorded. `builtin:sharepoint` is the one that is application-level
  in both of its modes (`app_only`, `destination`): `user_context` is refused.
  `builtin:bitbucket` has one mode, `destination`, as the destination's
  technical user (the table line `app` under `destination` only):
  `user_context` is refused
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
- `agents/sharepoint_tools.py` — ONE Excel workbook in a SharePoint document
  library, read through Microsoft Graph (`builtin:sharepoint`), read-only.
  Scope is config, never a tool argument: `site`
  (`<tenant>.sharepoint.com:/sites/<site>`), `library` and `path` pin the
  file, `views` names what may be read from it; no site, path, sheet, range,
  URL or destination ever comes from the model. Two tools:
  `read_table(view)` answers `{view, columns, rows, skipped_rows, row_count,
  last_modified}`, `read_calendar(view, date_from, date_to)` answers `{view,
  from, to, runs, conflicts, skipped_rows, unmapped, lookup_misses, sheets,
  last_modified}` (a run is member, team, kind, status, `from`, `to` plus the
  lookup's columns; consecutive days of one status are one run). No tool
  lists the views: an agent's instructions or a skill must name them (an
  unknown name is refused as `unknown_view`, the name the model sent is not
  echoed, the hint holds the configured names of that kind).
  **Application-level only**: `destination` (the destination holds the
  credential, via `DestinationAuth`) or `app_only` (client credentials stored
  with the entry); `user_context` is refused in both modes, at the gate, in
  storage and at build, never silently ignored. `SharePointFile` resolves the
  file with three Graph reads (site, the site's libraries matched by name,
  the item), cached: the library id 900 s, the bytes per eTag 300 s (no eTag,
  no cache), so the calls of one run download once. The cached bytes are the
  whole workbook, whatever the views pin, so they are dropped when the 300 s
  are over (a timer set for that entry, not the next call), when a newer
  version replaces them and when the registry closes the toolset's
  `http_client` (`SharePointFile.forget`). **One parse at a time per
  process** (`_parse`): the two reader calls run in a worker thread and take
  turns across all toolsets, and a turn is given back when the thread has
  ended, not when a cancelled tool call stops waiting (a thread cannot be
  cancelled): one parse at a time per app instance. **No result of the two
  tools is stored**: for these two tools the
  preview a run's activity keeps (`registry._short_tool_output`, shown in the
  chat's tool card and stored in `job_runs.activity_json`) is the fixed-form
  line of `activity_summary`: `read_table: n rows, s skipped`,
  `read_calendar: n runs, c conflicts, s skipped, u unmapped`, `error:
  <code>` for a refusal, decided by the tool's name alone, plain or prefixed
  (`sharepoint_read_table`, `sharepoint_0_read_table`), so another server's
  tool of exactly these names gets the same line and no preview; every other
  tool's preview is unchanged (`tests/test_sharepoint_activity.py`). What
  the model itself writes from the data is stored as for any agent: the run's
  report, and the short argument preview run activity keeps of every later
  tool call (`run_activity._detail`: the head of a mail body, todo items, a
  scratchpad write, OData call arguments). **The download URL is a
  credential**: the content comes from the item's pre-authenticated URL
  through a separate client that sends no `Authorization`, follows no
  redirect, has `trust_env=False`, and is used only for an `https` URL on the
  host the `site` pin names (no userinfo, no other port). `httpx` logs every
  request URL at INFO, so a filter on the `httpx` logger drops the records
  made in the downloading task (a contextvar, set and reset in the download
  coroutine) and any other record whose text holds a URL or query string in
  flight; it is installed at import and again before every download (a
  logging setup may have replaced the filters). A download has a total
  deadline (`DOWNLOAD_DEADLINE_SECONDS`, 120 s, on top of the 60 s
  per-operation timeout) because `fetch` holds the file's lock. Every failure
  is `{"error": {code, message, hint?}}` with a fixed text, never an
  exception and never Graph's text: `graph_unauthorized`, `graph_forbidden`,
  `graph_throttled`, `graph_error`, `graph_unreachable`, `site_not_found`,
  `file_not_found`, `library_not_found`, `not_a_file`, `destination_error`,
  `token_failed`, `download_refused`, `download_redirect`, `download_failed`,
  `too_large`, `not_a_workbook`, `result_too_large`, `unknown_view`,
  `invalid_dates`, `window_too_long`, plus the reader's codes below; anything
  unexpected is `read_failed` and logged by exception class name only (a
  traceback frame would hold the URL). `last_modified` is Graph's value only
  when it has the form of a UTC timestamp, else `null`. A result over
  `MAX_RESULT_CHARS` (60,000, measured with pydantic-ai's own tool-return
  serialiser, or plain JSON when that is longer or the serialiser cannot be
  imported: it is imported inside the measure, so a rename in pydantic-ai
  does not stop the app at import) is refused, not cut: a list
  that ends early would read as complete. The window is checked before
  anything is fetched and again inside the reader. Misconfiguration is a
  `ValueError` at registry build. Denied in ABAP Assistant sessions (the tools
  are not in `READONLY_POLICY`). Unit-tested only
  (`tests/test_sharepoint_graph.py`, `tests/test_sharepoint_tools.py`):
  never run against a real tenant. Setup
  notes (app registration with `Sites.Selected`, the destination) are in the
  module docstring and in `docs/SHAREPOINT_SETUP.md` (local, not in repo)
- `agents/sharepoint_views.py` — the one reading of a `builtin:sharepoint`
  config, standard library only: the save-time gate, storage and the toolset
  all call `check_pins` and `parse_views` / `clean_views`, so none accepts
  what another refuses. 1 to 10 views, named `[a-z][a-z0-9_]{0,31}`, of two
  kinds: `table` (an Excel table by name and the `columns`, at most 50, that
  may be returned) and `calendar` (one column per day: `sheet`, which may
  hold `{year}`, `date_row`, `first_date_column`, `first_row`, `labels` with
  `member` required and `team` / `kind` optional, `codes` mapping a cell
  value to a status (at most 100; `unmapped` is reserved), optional
  `stop_at`, `lookup` (pinned columns of a table view of the same entry, by
  member; `add` may not name a run key) and `conflict` (two kinds of row of
  one member)). **A calendar view with a `kind` label must pin its `kinds`**
  (1 to 10 distinct names; `conflict.kind` and `conflict.against` must be
  among them), a view without one must not have `kinds`. Unknown keys are
  refused, not dropped. **A pin is stored exactly as checked**: `check_pins`
  returns nothing and refuses edge whitespace, control / separator characters
  and a value that is not NFC instead of repairing them; `path` is an `.xlsx`
  below the library root without `\ % ? # : * " < > |` or `.` / `..` parts.
  A refusal (`ViewConfigError`) names the field and the rule, never a value.
  The gate is `_validate_sharepoint` in `agents/admin.py`. Storage has its
  own cleaner, `_clean_sharepoint_entry` in `agents/db.py`, because the
  generic key tables trim and `str()` what they keep: it is the last gate
  (reached without the admin payload by `scripts/import_bundle.py` and direct
  `upsert_agent` callers), checks pins and views again, refuses
  `user_context: true`, keeps only the destination name or the client
  credential, the three pins and the cleaned views, and the entry's URL is
  stored in the canonical spelling `builtin:sharepoint`.
  `tests/test_sharepoint_views.py`, `tests/test_sharepoint_registration.py`
- `agents/sharepoint_workbook.py` — workbook bytes to view data
  (`read_table`, `read_calendar`), pure and CPU work on a file somebody else
  wrote: call it in a worker thread (the toolset's `_parse`). `openpyxl` in
  read-only mode (with `defusedxml` and without `lxml`: a part with an entity
  declaration is `not_a_workbook`, in whatever part it stands). Text in the
  workbook is data, never an
  instruction, and **what leaves is default-deny**: a table view returns its
  pinned columns only, and a row with a text cell of a pinned column over 255
  characters or with a control, format or line-separator character is
  skipped and counted; a calendar cell reaches the model only as the status
  its code maps to (any other value, a number or an error value included, is
  counted as `unmapped`, never passed on); a calendar row is skipped and
  counted when it has no member, an error value in a label cell, a kind that
  is not pinned, a member or team cell over 120 characters or with such a
  character, or is a second row for the same member and kind (`skipped_rows`
  on both results; the text of a skipped row goes nowhere). A lookup name
  that is in the table twice is no key (`lookup_misses`). An Excel table is
  found in the package parts (a read-only worksheet of openpyxl does not
  load its tables). **No cached values is a refusal**: formula results are
  read as the saving application stored them and nothing is calculated;
  `no_cached_values` when no formula of a view (date row and body judged
  separately) has a result, or when a requested day is missing while the
  date row has a formula without a result among its dates. Caps, each a
  refusal: 20 MB, 5,000 archive members, 64 MB declared uncompressed size in
  total and 16 MB (`MAX_MEMBER_BYTES`) for every member that is not a sheet
  under `xl/worksheets/` (`check_archive`, from the declared sizes, before
  anything is inflated: only the rows of a sheet are streamed, every other
  part is parsed whole into a tree several times its size, on each of the 2
  opens of a table read and the 2 per year sheet, plus 2 for a lookup, of a
  calendar read). What holds: a part read whole is at most 16 MB, the archive
  at most 64 MB uncompressed, one parse at a time per app instance. That is
  no bound on memory: a hand-crafted worksheet or shared-strings part is
  bounded by the 64 MB total only, and openpyxl's structures per row and per
  string can cost several times that. Byte caps cannot close it; only parsing
  in a child process with a memory limit would (as
  `agents/_python_step_runner.py` does for the python step). Not built. **The declared sizes are
  held to**: openpyxl is given the reader's own archive (`_Archive`, through
  `ExcelReader`, which is why the openpyxl version is pinned by a test), in
  which no member is inflated beyond the size it declares (`ZipFile.read`
  alone inflates the whole stream first: 64 MB behind a declared 100 bytes)
  and a part that is read whole is capped at 16 MB wherever the package puts
  it (a sheet part the workbook calls a chart sheet is read whole); a
  workbook that openpyxl did not open on that archive is refused as
  `read_failed`, never read unguarded. A window
  of 120 days, given as exactly `YYYY-MM-DD` (a week date is refused;
  `check_window`), 5,000 rows and 200 columns of a table, 2,000 calendar
  rows (`stop_at` or 50 blank rows end them), 400 day columns. Its own codes:
  `table_not_found`, `column_not_found`, `sheet_not_found`,
  `dates_not_found` (a requested day missing or twice in the date row),
  `no_cached_values`, `too_large`, `not_a_workbook`.
  `tests/test_sharepoint_workbook.py`, `tests/test_sharepoint_calendar.py`
  (workbooks generated by `tests/sharepoint_helpers.py`)
- `agents/bitbucket_tools.py` — pull request review on Bitbucket Cloud (REST
  2.0) as an in-process toolset (`builtin:bitbucket`), as a technical user.
  **Application-level only**, through a BTP destination that points straight
  at the Bitbucket API host (with or without `/2.0`, `BitbucketAuth` sends
  the root once): Bitbucket's paging links and the diff redirect name its own
  host, so a destination that is a proxy on another host breaks both. Scope
  is config, never a tool argument: the workspace, the repositories, the
  target branch and the write switches are pinned in the entry; the model
  names only a repository slug, a pull request number and a file path.
  Every per-pull-request tool first runs one guard
  (`BitbucketClient.pull_request`): the repository is one the entry allows,
  the pull request is open and targets the pinned branch; the listing checks
  the same on every item it keeps.
  **The tools.** Always: `list_pull_requests()` (below);
  `get_pull_request(repository, id)` answers title, description, author,
  `head_commit`, `draft`, `builds` (`green` only with at least one build
  status, every one exactly `SUCCESSFUL` and all of them read; `none`;
  `not_green`) and at most 100 comments (`own` marks this account's,
  `comments_truncated`); `get_diff(repository, id, path="")` the diff, whole
  or of one file: over `MAX_DIFF_CHARS` (60,000) it is refused as
  `result_too_large` with the `diffstat` (at most 300 changed files), never
  cut, because a diff that ends early would read as complete;
  `get_file(repository, id, path)` one text file at the head commit (binary,
  LFS, over 200,000 bytes or 60,000 characters: refused; the name is
  `get_file` because `read_file` is the scratchpad tool of `agents/deep.py`).
  Only with `allow_comment` exactly `true`: `add_inline_comment(repository,
  id, path, line, text, side="new")` and `submit_review(repository, id,
  verdict, summary)`; with `allow_approve` as well:
  `complete_approval(repository, id)`. Without the switch the tool is not
  registered at all. The model's text is refused when empty or over its cap
  (4,000 / 8,000 characters), never cut or rewritten.
  **The review marker.** `submit_review` posts ONE top-level comment whose
  line 1 the code writes: `Automated review of commit <hash> - verdict:
  approve` (or `comment`; `review_marker`). It is how a repeated run knows a
  commit was reviewed, so it is read as strictly as it is written (`_marks`):
  only in a comment of the account behind the destination (`GET /user`, the
  uuid; a failure is not cached), only a top-level one (no inline comment, no
  reply), not deleted, only as the whole first line, only for the current
  head (both hashes of at least 12 characters, compared on the first 12).
  The same text typed by anybody else, quoted or further down is no marker: a
  pull request author could otherwise make the agent skip their pull request.
  A summary or inline comment whose own first line looks like the marker is
  refused. The marker says `approve` only when the entry's `allow_approve` is
  `true` at that moment: a review by an entry that may not approve is marked
  `comment` whatever the model asked, so turning the switch on later approves
  no older review.
  **The listing** answers `{pull_requests, reviewed_filter, already_reviewed,
  pull_requests_unchecked, repositories, repositories_failed, more,
  beyond_reach, note?}` plus `approval_pending` for an entry with
  `allow_approve`; an item is `repository, id, title, author, head_commit,
  draft, updated_on`. A pull request this account reviewed at its current
  head is left out and counted; "not reviewed" is said only when every
  comment was read: the marker is searched in the first 300 comments
  (`COMMENT_WINDOW`, in the order Bitbucket returns them), and no marker in
  sight while more exist is `review_state_unknown` (counted in
  `pull_requests_unchecked`, not listed), never "not reviewed".
  `approval_pending` holds the pull requests reviewed with verdict `approve`
  whose approval was not sent (this account's entry in `participants` does
  not say approved). An account that cannot be identified gives the
  unfiltered list and says so (`reviewed_filter: unavailable`). Caps per
  call: 20 listed, 40 checked, 50 repositories (one page of the workspace,
  or the pinned list), 50 open pull requests per repository (one page). A
  call that reaches a cap sets `more` and remembers where it stopped; the
  next goes on from there, round and round (the cursor is in memory, per
  toolset: gone at a registry reload, not shared between app instances).
  What lies behind the one page of repositories or of a repository's pull
  requests is never reached by any run: `beyond_reach`.
  **Inline comments are not repeated**: nothing on a head that has its review
  (`already_reviewed`), nothing on a path, side and line that already has a
  comment of this account (`already_commented`), nothing once fewer than 20
  of the 300 comments that are read remain (`comment_window_full`: the
  summary with the marker must still land inside what a later run reads).
  One complete read per pull request and head is remembered for 10 minutes
  (`_inline_state`, at most 64 entries, in memory; what another app instance
  posts meanwhile is not seen until then); `submit_review` and
  `complete_approval` never use that memo. No write is made while the
  account is unknown (`account_unknown`); a second review of the same head
  is `already_reviewed` (a new commit allows a new review).
  **The approval gate** (`_approve_behind_the_gate`, the one place that sends
  an approval). All of: the `verdict` argument is exactly `approve`; the
  entry's `allow_approve` is exactly `true`; the summary comment of this very
  call was posted and its answer read; unless `require_green_builds` is
  exactly `false`, the builds are `green` with the statuses read strictly (a
  list that holds anything but objects, more statuses than are read, or a
  failed read: no approval). Nothing a pull request says is part of it. Two
  deliberate decisions: **the approval is NOT bound to the commit the model
  read** (a commit pushed between the read and `submit_review` is approved
  with it), and **a draft pull request is NOT held back in code** (`draft` is
  reported; the seed skill tells the agent to give a draft the verdict
  `comment`). When the gate holds the approval back or Bitbucket refuses it,
  the comment stands: `commented: true`, `approved: false` and an `error`
  (`approve_not_allowed`, `builds_not_green`). `complete_approval` sends the
  approval later and posts nothing: this account's marker for the CURRENT
  head must say `approve` (`not_reviewed`), the account must not have
  approved yet (`already_approved`; unreadable: `review_state_unknown`), then
  the same switch, builds check and approve call.
  **A write is sent once.** Only a 429 is repeated (and a 401 once by the
  destination auth). A timeout or lost connection after the request may have
  left, an answer over its cap, a 2xx that is not a JSON object, any 5xx: the
  outcome is unknown and said as such (`comment_outcome_unknown`,
  `approval_outcome_unknown` with `approved: null`), never as a success or
  as "nothing happened", and nothing is sent again.
  **The HTTP core.** The credential goes only to the destination's own
  `https` host and port (no expected host is configured; an absolute URL on
  another scheme or port is refused before the auth adds a header). httpx
  follows no redirect: ONE redirect is followed by hand, and a paging `next`
  link likewise, only on the same host and port over `https`, with the
  `Location` sent as given (never decoded and rebuilt; a file path goes out
  segment by segment, raw). Every answer is streamed up to a cap (2 MB JSON,
  240,000 bytes of diff, 200,000 of a file), the body of an error status is
  never read. The calls of one toolset are sequential; a 429 is repeated
  after 2, 4 and 8 s (`BACKOFF_SECONDS`). A filter on the `httpx` logger
  drops the request records made in the sending task (a contextvar, set and
  reset in `_send`; installed at import and again before every request).
  Every failure is `{"error": {code, message, hint?}}` with a fixed text,
  never an exception and never Bitbucket's text, a header value, an
  exception's text or a URL (a log line names the step and the exception
  class or the status); the codes are the closed list `ERROR_CODES`.
  Pull request text (title, description, author and commenter names, comment
  text) is filtered by character before the model gets it (`_shown_text`: no
  control or format characters, lone surrogates, line / paragraph
  separators; a title or name keeps no line break) and then cut (300 / 4,000
  / 200 / 1,000 characters); diffs and file contents are not filtered.
  **No result is stored**: for all seven tools the preview a run's activity
  keeps (`registry._short_tool_output`) is the fixed-form line of
  `activity_summary` (counts, sizes, fixed words and a refusal's code; no
  title, name, path, commit or id), decided by the tool's name alone, plain
  or prefixed (`bitbucket_get_diff`, `bitbucket_0_get_diff`). Still stored as
  for any agent: the run's report and the argument preview of each call
  (`run_activity._detail`: the head of an inline comment or summary).
  Misconfiguration is a `ValueError` at registry build. Denied in ABAP
  Assistant sessions (the tools are not in `READONLY_POLICY`). Unit-tested
  only (`tests/test_bitbucket_http.py`, `tests/test_bitbucket_tools.py`,
  `tests/test_bitbucket_write.py`, `tests/test_bitbucket_activity.py`,
  `tests/test_bitbucket_registry.py`): **never run against a real
  workspace**. To verify there, beyond what `scripts/probe_bitbucket.py`
  answers: how Bitbucket answers an inline anchor outside the diff (the code
  reports `anchored` and maps a 400 to `invalid_line`), the order in which
  comments are returned (none is asked for or pinned), the shape of
  `participants`, and the two write calls. Setup in brief (module docstring):
  an HTTP destination with `BasicAuthentication` (the technical user's e-mail
  address and an API token with the scopes `read:repository:bitbucket`,
  `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket`,
  `read:user:bitbucket`)
- `agents/bitbucket_config.py` — the one reading of a `builtin:bitbucket`
  entry, standard library only: the save-time gate, storage and the toolset
  all call it (`check_entry`, `clean_entry`, `pins_of`), so none accepts what
  another refuses. Keys: `destination` (a destination name), `workspace` (one
  slug), optional `repositories` (1 to 50 distinct slugs; absent = every
  repository of the workspace), `branch` (default `main`), and the switches
  `allow_comment`, `allow_approve` (requires `allow_comment`) and
  `require_green_builds` (default true), each a JSON boolean. **A value is
  stored exactly as checked**: nothing is trimmed, lower-cased or converted;
  a switch is stored only as the one boolean that changes something (`true`,
  `true`, `false`). Unknown keys and `user_context: true` are refused, not
  dropped. A refusal (`BitbucketConfigError`) names the field and the rule,
  never the value and never the name of an unknown key. `repository_allowed`
  and `confine_path` (a file path below the repository root: no `\ % ? #`,
  control character, empty, `.` or `..` segment) check the tool arguments.
  **At most one entry per agent**: refused at the gate
  (`_validate_raw_bitbucket_entry` in `agents/admin.py` checks the block as
  the client sent it, before the lax payload model; the agent payload counts
  by server key), refused in storage (`prepare_servers` in `agents/db.py`),
  and a hand-written row with two gets NO Bitbucket toolset at registry build
  (one WARNING, the agent keeps its other servers): the second could carry
  wider switches than the one somebody reviewed.
  `tests/test_bitbucket_config.py`, `tests/test_bitbucket_registration.py`
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
  per-user results live in a bounded LRU keyed by principal AND a digest of
  the token, apart from the app-level entry (a "Run now" job carries the
  trigger's JWT under the run-as principal, so a principal alone would let
  one user's credential serve another; the Outlook/Teams caches and the
  OData CSRF store follow the same rule, the former with a 900 s TTL).
  `Destination.auth_type` echoes the `Authentication` type
  for diagnostics; `require_credential=False` accepts a bare-URL destination
  (public targets). `Destination` also carries `proxy_type`, `location_id`
  (`CloudConnectorLocationId`) and `queries` (the `URL.queries.*`
  properties, e.g. `sap-client`). `ConnectivityConfig` and
  `connectivity_config_from_environment` read the connectivity binding from
  `VCAP_SERVICES` (env fallback `CONNECTIVITY_*`); `ConnectivityTokens` gets
  the application's token (client credentials) or a user's (jwt-bearer
  exchange), cached per owner and token digest, and fails with class,
  status and OAuth code only, never the endpoint's text
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
  Teams read-only); `user_context` is stored for Gmail, Outlook and Teams
  only (Jira, Slack, SMTP and the SAP-notes built-ins are app-level) and
  `builtin:odata` carries it per catalogue service. Storage keys per
  built-in are `_DEST_KEYS_BY_URL` in
  `agents/db.py`; save-time rules are `_validate_destination_config` in
  `agents/admin.py`; `GET /admin/api/credential-health` reports
  destination servers under `destinations`.
  **OnPremise destinations** (`ProxyType: OnPremise`, a virtual host behind a
  Cloud Connector) are reached only by a client built for it
  (`routed_auth` / `destination_http_client(..., connectivity=...)`, today
  the OData toolset, its `$metadata` preview and its test call; every other
  built-in refuses one): the request keeps the destination's `http://` URL
  (an `https://` OnPremise URL is refused: a CONNECT tunnel would hand the
  proxy token to the target) and goes to the connectivity proxy in HTTP
  forward mode with a per-request `Proxy-Authorization`, through
  `OnPremiseRouter`, which refuses a request carrying a proxy token that was
  not shaped for the proxy. A technical service sends the application's
  connectivity token and the destination's own `Authorization`. A
  signed-in-user service requires `Authentication: PrincipalPropagation`,
  sends no credential stored in the destination and none the caller set, and
  carries the identity by `CONNECTIVITY_PP_MODE`: `exchange` (default; a token
  exchanged from the user's JWT in `Proxy-Authorization`) or `header` (the
  application's token there plus the JWT in `SAP-Connectivity-Authentication`);
  the variable is read only when such a request needs it and any other value
  refuses that request (one WARNING at startup, `app.warn_on_unknown_pp_mode`).
  The signed-in-user path through the proxy is unit-tested only: it has not
  run against a landscape. No JWT bound is `DestinationUserRequired` before any
  token or proxy call: a user run never falls back to the application's
  identity. `SAP-Connectivity-SCC-Location_ID` is sent when the destination
  names a location; no connectivity binding is a `DestinationError`; a 407
  from the proxy drops that owner's token, retries once and then surfaces as
  the code `proxy_refused` (never blamed on SAP). For EVERY destination the
  connectivity headers and `Host` cannot be set by a `URL.headers.*` property,
  and `URL.queries.*` are sent with every request (a caller's parameter of the
  same name wins; a client built for the connectivity route skips `$`-prefixed
  names and the OData system option names without the `$` (`filter`, `top`,
  `id`, ...), so a destination cannot add `$filter` or `$expand` behind the
  argument checks; for such a client the caller's `X-CSRF-Token` and
  `Cookie` win over a `URL.headers.*` property of that name, and `If-Match`,
  `If-None-Match`, `X-HTTP-Method` and `X-HTTP-Method-Override` are never
  taken from a destination, so a stored `If-Match: *` cannot make a change
  unconditional).
  A client built for the connectivity route (the OData callers) that acts as
  the signed-in user on an **Internet** destination requires a destination
  that signs in as that user: resolved for the user, an `Authentication` of
  `OAuth2JWTBearer`, `OAuth2UserTokenExchange` or `OAuth2SAMLBearerAssertion`
  (the types the refusal names), no `SystemUser` property
  (`Destination.system_user`: the destination service would mint the token
  for that fixed user), and an `Authorization` the destination service minted
  (`Destination.static_headers` tells stored `URL.headers.*` from it; a
  stored `Authorization` or `Cookie` is never sent on a user run). Otherwise
  `NotUserPropagating` (a `DestinationRefused`, like `OnPremiseRefused`, with
  a fixed `admin_text`) before anything is sent: the destination service
  ignores the user's token for a destination with a stored credential. On
  either path such a client that acts as the signed-in user never sends a
  destination's `sap-user`, `sap-password` or `mysapsso2` header or query
  parameter. The other destination users (Gmail, Outlook, Teams, Slack, Jira, SAP notes, MCP
  over a destination, the workflow http step) are not held to these rules. A
  401 or proxy 407 whose one retry could not be prepared is marked
  (`request_left`), so a write is audited as sent. Token endpoint failures
  are reported as status plus OAuth error code only, never the body
- `agents/odata/` — OData V2/V4 services as an in-process toolset
  (`builtin:odata`). An admin curates a **catalogue** in the database (which
  services, entity sets, fields and operations exist *for agents*); an agent
  gets exactly two tools, `search_operations` and `execute_operation`, and
  never a URL, a destination name or a free path: a name the catalogue does
  not hold does not exist for the model. Identity is per catalogue service
  (`destination` + `user_context`, "Runs as" in the UI), never per agent. The
  agent's entry only lists services and says `allow_write`. Rules enforced in
  code, not by prompt: **only fields marked readable (`selectable`) reach the
  model**, whatever SAP sent (`__metadata`, `@odata.*` and the raw ETag are
  dropped); a filterable field must be selectable (else a filter could probe
  hidden values); key field NAMES are always visible, key VALUES come back only
  for a selectable key; `$select` is always sent; a `filter` is only a
  parameter value, checked field by field against the catalogue (V4 enum
  fields only with a typed literal, complex and collection fields never); a
  navigation or `$expand` needs the target set enabled in the catalogue.
  **The write rule**: a call is a read only when the operation's
  `changes_data` is false AND its method is `GET`; everything else (create,
  update, delete, and every call that is not such a read, a V2 function import
  or a V4 action always) is a write and needs the catalogue operation enabled
  AND `allow_write` exactly `true`, and is audited. A `POST` stored with
  `changes_data: false` is not refused at save: it is stored and run as an
  audited write. Tools are denied in ABAP Assistant sessions by
  `ReadOnlyGuard` (default-deny by name; they are not in `READONLY_POLICY`)
  - `models.py` — the definition models (`ODataServicePayload`,
    `ServiceDefinition`, `EntitySetDef`, `FieldDef`, `OperationDef`; all
    `extra="forbid"`) and `validate_odata_service`, the save-time gate: names
    unique, keys are fields, `list`/`get` need a selectable field, `create`/
    `update` a writable one, V2 allows only function imports, V4 only actions
    (`POST`) and functions (`GET`), `title`/`purpose`/`not_for` one line,
    `definition` required, booleans strict. Refusals name the field and the
    rule, never a value (`agents/loc_fields.py`). `OperationDef.is_write` is
    the one write rule; `returns` names the entity set an operation answers
    with. Never imports `agents.db`
  - `urls.py`, `common.py` — path confinement (`confine_service_path`,
    `join_path`: a model or admin value is only ever a path below the
    destination's host), the key predicate (one segment, written by typed
    literal; `/ \ % ? #` and `..` in a string key are refused because
    `DestinationAuth` rebuilds the URL from the decoded path), `check_filter`,
    and the error-envelope reader
  - `client.py`, `v2.py`, `v4.py` — `ODataClient` re-enforces the catalogue
    on the way out and on the way back (`check_read`, `check_write`,
    `check_call`; rows cut to the selectable fields that were asked for),
    one gate for both versions; the dialects hold literals, query option
    names and payload shapes. Nothing a back end says reaches the caller
    except the short code and message of a proper OData error envelope.
    A write takes its CSRF token and cookies from the identity of this
    request, is repeated only after a refusal that says it was not
    processed (one repeat after a 403 `Required`), never after a failure
    (`write_outcome_unknown`; `ODataError.sent` says whether the change left
    the app), and success is recognised positively (a sign-in page at 200 is
    not a success). A 5xx on a modifying request is `sap_error` ("SAP
    refused") only when it carries an OData error envelope, read strictly
    for this one decision (`common.is_error_envelope`: a string `code` plus
    the dialect's message form, or an XML `error` with `code` and
    `message`); every other 5xx (an HTML error page, a bare 503, a
    gateway's 502/504, also with a JSON or XML message of its own) is
    `write_outcome_unknown`. A decimal given as a JSON number is sent only
    with at most 15 significant digits, in a V2 or V4 body and URL literal
    alike (`common.plain_float`, the one rule). A refused navigation names
    the navigation, never its target entity set. A page of which not even
    one row fits a tool result is the refusal `result_too_large`, not an
    empty page. Known limits: V4 decimals below 0.0001 cannot be written
    (no `IEEE754Compatible`); `@odata.context` is not yet required to
    confirm a V4 write (undecided until the pilot; each confirmed V4 write
    logs status and whether `@odata.context` / `@odata.etag` were present);
    V4 enum members are not read from `$metadata`; SAP's own error text (up
    to 500 characters, URLs masked) reaches the model. A proxy 407 that came
    through the connectivity proxy is `proxy_refused`, with a hint for the
    admin (binding, location id, `CONNECTIVITY_PP_MODE`, Cloud Connector
    trust) in the preview and test call, and one for the model (nothing was
    changed, tell the user, do not retry)
  - `session.py` — `CsrfSessionStore`: token and SAP session cookies per
    destination and per user, in memory, never in the shared HTTP client; the
    key follows the credential that is *sent* (`user:<principal>:<sha256 jwt>`),
    so a user entry is served only to a request that carries the same token
  - `search.py`, `calls.py` — `search_operations` over the snapshot: only
    what the catalogue enables is listed, writes and `changes_data`
    operations only with `allow_write`, results built key by key (a UI-only
    flag cannot leak), and only what `execute_operation` can run
    (`calls.py` is the one "callable" rule, also used to list uncallable
    enabled operations in the service detail)
  - `tools.py` — `odata_toolset(...)`: the two tools, snapshot loaded by the
    registry at reload (no database read per call). A catalogue edit of a
    service that is in use reloads the registry itself, so it applies to the
    next run; see `admin_routes.py`. Every refusal is `{"error": {code, message, hint?}}`, never
    an exception. The model never holds an ETag: a `get` returns an opaque
    handle (`h-...`, tied to identity, service, entity set and key, TTL 15
    min, bounded) only where the entity set can be changed and the entry has
    `allow_write`; the real ETag stays server-side and goes out as
    `If-Match`. **Audit rule: no intent row, no write.** Every modifying call
    that passed its checks has an `intent` row committed before a client is
    built or anything is sent, and a result row exactly once afterwards;
    without a storing recorder, or when the intent cannot be written,
    nothing is sent (`audit_not_configured` / `audit_unavailable`)
  - `audit.py` — `StoredWriteRecorder`, the `odata_audit_log` rows: names and
    keys only (field NAMES, never a value, token, cookie or ETag), `sent_as`
    (the identity SAP saw, from the validated JWT that was sent) and
    `run_principal` separately; a row left `intent` means the write MAY
    have been sent. Purged by age (`ODATA_AUDIT_RETENTION_DAYS`, default 365,
    `0` keeps forever and logs a WARNING, floor 7 days) from `app.py`'s
    retention pass; `drain()` at shutdown
  - `metadata.py`, `preview.py`, `testcall.py` — `$metadata` import. The
    parser treats the document as untrusted: no DTD (two layers), caps on
    size (20 MB), depth, elements (75,000) and attributes (750,000), a work
    budget, ASCII-only names; one odd element is skipped and recorded
    (`ParsedMetadata.skipped`), not fatal. It is CPU work: call it through
    `asyncio.to_thread`. `preview.py` fetches ONE document by destination
    and service path (no redirect, no retry, a byte cap, 25 s for fetch and
    parse, two previews at a time) and answers names, types, labels and what
    SAP *declares* under `declared`/`suggested` keys, enabling nothing.
    `testcall.py` does one `top=1` read through a stored service and answers
    outcome, status, duration and names, never a row
  - `destinations.py` — the destination dropdown: reads exactly five
    properties of each destination (`Name`, `Description`, `Type`,
    `ProxyType`, `Authentication`); URL, user and credential never leave it
  - `admin_routes.py` — `/admin/api/odata/...`, every route with its own
    `require_admin`, bodies read as raw JSON and validated in the handler so a
    refusal never echoes input. Catalogue CRUD (`GET`/`POST /services`,
    `GET`/`PUT`/`DELETE /services/{name}`, `POST /services/{name}/duplicate`);
    a `PUT` takes `expected_updated_at` and answers 409 when the stored row
    moved (lock, compare, write in one transaction); `DELETE` answers 409 while
    any agent, enabled or not, attaches the service. After the commit, a
    `PUT` or `DELETE` of a service that is in use (an agent row attaches it,
    or the running build still holds a copy of it) rebuilds the registry and
    the chat app (`reload_after_catalogue_change`), so an edit that closes
    something does not wait for a manual reload; a service nobody uses
    triggers none. The `PUT` answer carries `reloaded` and `reload_failed`,
    the 204 of a `DELETE` the headers `X-OData-Reloaded` and
    `X-OData-Reload-Failed`; a rebuild that fails after the commit is logged
    and answered as `reload_failed: true`, never as a 500 (the log names
    the exception class only). The rebuild reaches only the app instance
    that served the request: run one app instance, or restart all instances
    after an edit that closes access. A rebuild also discards the ETag
    handles and CSRF sessions of the old toolset (an agent re-reads before an
    update). A service that
    acts as the signed-in user on a destination that does not sign in as the
    user is refused by the preview and the test call with `destination_error`
    and a fixed text (the picker, the preview and the save checks still treat
    `SAMLAssertion` as user-propagating and do not know `SystemUser`: such a
    destination is offered, then refused at run time). CSRF on the admin
    routes is unchanged. `POST /metadata`,
    `POST /services/{name}/test`, `GET /destinations`, `GET /audit`. A
    preview, test or destination-list failure carries a stable code in the
    `X-OData-Error` header (`busy`, `invalid_path`, `user_token_required`,
    `destination_error`, `proxy_refused`, `unreachable`, `redirect`,
    `sap_error`, `not_xml`, `too_large`, `timeout`, `invalid_metadata`,
    `unknown_target`, `operation_disabled`, `no_destination_service`,
    `token_failed`, `list_failed`); a test that ran and failed is a 200 with
    `ok: false`
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
  overrides the globally active model per agent. For `builtin:odata` it loads
  the catalogue snapshot of the services the agents list at reload and adds
  an `## OData services` block to the specialist's instructions (one line per
  attached service: name, title, purpose, "not for"; the text is stripped of
  control characters and of the delimiter tag); an entry with no usable
  service builds the agent without the OData tools; retired builds drain
  their audit recorder
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
- `agents/notifications.py` — finished-run notifications for the admin UI,
  included by `agents/admin.py` like the OData router, each route with its
  own `require_admin`. **Derived, not stored**: the runners write nothing
  for it; the list is read from `job_runs` and `workflow_runs` when asked
  (only the columns of the answer, never a summary, report, error or
  activity). `GET /admin/api/notifications` answers `{items, unread_count,
  seen_at}`: an item is `{kind` (`agent` | `workflow`)`, run_id, name,
  status, trigger, created_by, finished_at, unread}`, runs with a
  `finished_at` in the last 7 days, newest first, at most 50 of both kinds
  together (one statement per table, merged in Python: no `UNION`);
  `unread_count` covers the whole 7 days and can exceed the list. One `now`
  per request bounds list and count alike (`finished_at <= now`), so they
  cannot disagree about a run that finishes meanwhile. Both run tables have
  an index on `finished_at` for these reads (`ix_job_runs_finished_at`,
  `ix_workflow_runs_finished_at`; `init_db` adds them to an existing
  Postgres or SQLite database, `_ensure_plain_index`). The one
  new table is the read marker, `AdminNotificationState`
  (`admin_notification_state`: `principal`, `seen_at`), one row per admin
  keyed by `current_principal` (in the local app that is `local-dev`, which
  `agents/auth.py` binds; the fixed key `local` only when no principal can
  be derived; a principal over 255 characters is keyed by its digest,
  `sha256:<hex>`), never by anything of the request; `unread` is
  `finished_at > seen_at`. A caller
  without a marker gets one set to now by the first call (nothing is unread
  then; a concurrent first call reads the row the other one inserted).
  `POST /admin/api/notifications/seen` with exactly `{"up_to": <timestamp
  with a time zone>}` moves the marker to `min(up_to, now)` and only forward
  (a conditional UPDATE) and answers `{seen_at}`; any other body, a larger
  one included, is a 422 with one fixed text and nothing of the input.
  Timestamps are answered as aware UTC whatever the database hands back.
  An accepted limit: `finished_at` is stamped before the run's commit, so
  with two app instances a run stamped earlier but committed later than a
  run already marked read is shown as read. The table and the two indexes
  are HANA schema generation 2. The tests are in
  `tests/test_notifications.py`
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
  reload, restart, import/export, seed-on-startup. It includes the OData
  catalogue router (`agents/odata/admin_routes.py`). Export carries
  `odata_services`; import takes them first (up to 200, `enabled` required,
  a duplicate name refused, unchanged services not restamped) and with
  `replace` deletes only a service that is absent from a non-empty section
  and attached by no remaining agent; the answer adds
  `imported_/created_/updated_/removed_odata_services`,
  `removed_odata_service_names` and `odata_identity_changes`. The import
  does not reload the registry for agents, skills or workflows, but it does
  when it created, changed or removed a catalogue service that is in use,
  or changed what an agent's `builtin:odata` entry allows (the entry
  changed, added or gone, the agent removed by `replace`, disabled or
  enabled) (answer keys `reloaded`, `reload_failed`, as for the catalogue
  routes).
  An agent create, update or delete that changes that agent's
  `builtin:odata` entry (services, `allow_write`, the entry itself), or
  disables or enables an agent that has one, reloads
  the same way after the commit; create and update always answer `reloaded`
  and `reload_failed` (both `false` for any other save, which does not
  reload), the 204 of a delete carries the two `X-OData-*` headers.
  `GET /admin/api/credential-health` lists a
  `builtin:odata` entry once per attached service (`service`,
  `service_enabled`, `destination`, `user_context`, `state`: `resolvable` |
  `error` | `unbound` | `missing`); its `error` for a failed resolve is a
  fixed text ending in a code (`agents.odata.destinations.destination_failure`),
  the resolver's own text goes to the log. The seed loop
  (`seed_from_file_if_empty`) logs a refused entry of any type (skill, agent,
  workflow) by its name (only when it has the form of a name) and the field
  locations and error types of the refusal, or the exception class: never
  the entry or the text of the error, which hold secrets, pins and input
- `agents/a2a.py` — A2A (Agent-to-Agent) protocol server: agent card at
  `/.well-known/agent-card.json`, JSON-RPC at `/a2a` (`message/send`,
  `message/stream`, `tasks/get`, `tasks/cancel`). Used by SAP Joule.
- `agents/cf_api.py` — CF v3 API restart helper (optional, password grant)
- `templates/admin.html` — Admin UI (single-page, vanilla JS); attaches
  catalogue services to an agent (checkbox list and Allow writes), the
  catalogue itself is edited only in `ui5-admin/`; it does not read
  `reloaded` / `reload_failed`, so a failed reload is not shown there. It
  offers `builtin:sharepoint` in destination mode only, with the views
  edited as JSON and the three pins sent through `DESTINATION_EXACT_KEYS`
  (as typed, never trimmed). A server stored on an auth mode this page does
  not offer for its toolset (`app_only`, for every built-in; such an entry
  is made in `ui5-admin/`) keeps that mode: it gets an option of its own,
  the loaded block is posted back as it was loaded (no secret: the server
  keeps the stored one) while that mode stays selected, and the row carries
  a fixed note saying where the server is edited; before, such an agent
  could not be saved here. It offers `builtin:bitbucket` (destination mode):
  workspace and target branch sent as typed, repositories as a
  comma-separated field (blank = the key is absent), three checkboxes (Allow
  commenting, Allow approving, which needs the first, and "Approve only when
  all builds are successful", sent only as `false`); two such rows are
  refused before the save. It asks no question when approving is switched on
- `ui5-admin/` — SAPUI5 (TypeScript) rebuild of the admin UI, deployed to the
  BTP HTML5 Application Repository and served at `/ui5admin`. Runs **alongside**
  `templates/admin.html`, which is still the supported admin at `/admin`. All HTTP goes through `webapp/service/AdminService.ts`; see
  `docs/UI5_ADMIN.md`. The server dialog's toolset dropdown comes from
  `webapp/model/builtins.ts`, which mirrors `agents/builtins.py` and lists the
  auth modes the server accepts per built-in. For `builtin:sharepoint`
  (`app_only` or `destination`) the dialog edits the views as JSON and sends
  the three pins as typed, never trimmed (the server refuses edge whitespace
  instead of repairing it). `model/bitbucketEntry.ts` mirrors
  `agents/bitbucket_config.py` for `builtin:bitbucket` and adds no rule:
  `clean` builds the block from nothing (its own keys, pins as typed, a
  switch only as the boolean that is stored), `validate` answers fixed texts
  that never quote a value, `newlyApproves` names the workspaces whose entry
  approves after a save and did not before. The **OData services** area
  (nav entry between Skills and Runs; `view/ODataServices` = list with Used by,
  Write tag, import of a service file; `view/ODataServiceDetail` = identity
  ("Runs as" signed-in or technical user, destination field), purpose / not
  for, entity sets, operations, used by, test call) keeps its logic in
  `model/odataCatalog.ts`, `model/odataDestinations.ts`, `model/odataEntry.ts`
  (the agent's entry) and `model/importBundle.ts` (what a configuration bundle
  opens), and its dialogs in `controller/odata/` (entity set, operation,
  `$metadata` import; duplicate on the page controller). One write rule
  everywhere; the UI mirrors the server's and adds none.
  - Destination field (page and Duplicate dialog): an `sap.m.Input` with
    suggestions (what each destination signs in as, a warning when it does
    not match "Runs as"), `autocomplete` off and a value-help list; on a phone
    a plain field and an own picker (`fragment/ODataDestinationPicker`),
    because the full-screen suggestion dialog completes typed text. The stored
    value is what was typed or explicitly picked; what the field shows is
    stored when Escape, Enter or leaving the field settles it. Public API only.
  - Import from `$metadata` (`ODataImportDialog`): nothing arrives ticked or
    enabled (new entity sets without List, new operations disabled, declared
    capabilities are information); a re-import lists new / changed / removed,
    never overwrites admin work (a label the admin wrote, a title, a key
    change only when ticked) and removes only what is ticked, with what agents
    lose and which bound operations block it; an answer that arrives after the
    dialog was closed or another service shown is dropped.
  - Operations: a table (Enabled, Changes data; a POST cannot be unticked) with
    the server's `uncallable_operations` reason per row; a row press opens
    the operation dialog, where only business name and description are
    editable (the rest is shown as read from SAP). The pending strip has a
    second category beside the writes: operations newly marked as only
    reading ("no longer recorded and callable without Allow writes"), also
    asked on Save, naming the agents without Allow writes that use the service.
  - Agent server dialog (`McpServerDialog`, `model/odataEntry.ts`): toolset
    "OData services" (auth mode fixed to destination), a multi-select of
    catalogue services showing "Runs as", missing / disabled services, a
    warning for a signed-in-user service on an agent with a run endpoint, and
    Allow writes with the list of what it opens (enabled writes of the
    selected services, "could not be read" when the catalogue read fails);
    at most one OData entry per agent; a stored entry in another spelling
    (`Builtin:OData`, trailing `/`, as the save gate accepts) opens with its
    controls. The agent's Save asks when it newly gives writes or adds
    services (a generic question when what it opens cannot be read; the
    question and the PUT use one snapshot) and always sends the entry as
    exactly `{services, allow_write: <boolean>}`.
  - Rules the UI enforces: every path that stores or enables a write (a
    ticked create/update/delete, an enabled data-changing operation, an
    service created from a file or by Duplicate, a bundle) or widens access (unticking
    Changes data) lists it and asks on Save / Create / Import; the Settings
    import and the list page's file import ASK before they open writes (a file
    without a boolean `enabled` is created switched off) and show the
    server's warnings, removed services and identity changes; a changed
    destination, service path or OData version of a service in use is asked
    about; a save is a GET plus a `PUT` with `expected_updated_at`.
  - A save, delete or import whose answer says `reload_failed` (JSON key, or
    the `X-OData-Reload-Failed` header of a 204) shows a warning that stays
    ("Saved, but not active yet": press Reload in Settings), through
    `BaseController.warnIfNotLive`; the two outcome keys are never sent back
    in a body
  - Notifications: a bell with the unread count (also in its tooltip, for
    screen readers) and a toast in the app shell for finished agent and
    workflow runs, logic in `model/notifications.ts`. `GET notifications` is
    polled every 15 s only while the page is visible, had pointer or key input
    in the last 10 minutes (`App.notificationIdleMs`, so an unattended screen
    does not keep the session alive) and the last poll was not answered 401 or
    403; a stopped chain has no timer and starts again with one poll on the
    next input or when the tab becomes visible. Opening the list reads it
    fresh and marks read what it shows (`POST notifications/seen` with the
    newest shown `finished_at`, unchanged); failures are silent, the first
    answered poll shows no toast, and inside a host shell that hides the
    header there is no bell, poll or toast. `e2e/notifications.spec.ts` checks
    it against the real backend.
- `agents.seed.json` — Initial config imported when DB is empty. Holds the
  agent `pr-reviewer` with the skill `pull-request-review` (what to read,
  the blocking findings, how to choose the verdict; an administrator edits
  it): the agent ships **disabled and read-only** (a `builtin:bitbucket`
  entry with placeholder destination and workspace and no switch), so it
  posts nothing and, with no marker, reviews every listed pull request again
  on each run until `allow_comment` is set (`tests/test_bitbucket_seed.py`)
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
  `init_db` at start; 2.20.0 adds the `agent-connectivity` resource
  (`connectivity`, plan `lite`, bound to the app) and `ODATA_AUDIT_RETENTION_DAYS`
  (365); `CONNECTIVITY_PP_MODE` is not in the descriptor (set per landscape in
  an `.mtaext`; default `exchange`; an unknown value is one WARNING at
  startup and a refusal of each affected call); 2.21.0 adds the resource
  `agent-registry-hana` (`hana`, plan `hdi-shared`) with `active: false`,
  required by the app next to `agent-registry-db` (a requirement on an
  inactive resource is ignored). A landscape switches database in its
  `.mtaext` by setting `active` on the two resources (the snippet is a
  comment in the descriptor); with both active it must also set `DB_KIND`.
  Nothing is copied between the databases by a deploy (see
  `scripts/copy_registry_config.py`). `HANA_HDI_ALLOW_DROP` is not in the
  descriptor (an `.mtaext` sets it for the one deploy that may drop a table);
  2.22.0 is the `builtin:sharepoint` release: no new resource and no new
  environment variable; 2.23.0 adds the finished-run notifications
  (`agents/notifications.py`): no new resource or environment variable, one
  new table (`admin_notification_state`) and an index on `finished_at` of
  both run tables (together HANA schema generation 2); 2.24.0 is the
  `builtin:bitbucket` release: no new resource, no new environment variable,
  no new table or column (HANA schema generation unchanged)
- `scripts/copy_registry_config.py` — copies the registry's configuration
  from one database to the other (a landscape that switches from PostgreSQL
  to HANA, or back). **It is the only supported way to carry stored secrets
  over** (`/admin/api/export` redacts them on purpose, and stays as it is):
  rows go from one database to the other inside one process, never through
  a file, a bundle, an argument or a log. **It copies configuration, not
  history**: `agent_configs` (every column), `skill_configs`, the
  orchestrator row, `workflows` with branches and steps, `odata_services`,
  `ide_conventions`, `mcp_oauth_clients`, `mcp_oauth_tokens`; not job or
  workflow runs, the audit logs, IDE sessions and their children, OAuth flow
  states, the notification read markers (`admin_notification_state`). Rows are matched by natural key (integer ids are the target's own;
  branches and steps follow their workflow's name); a target row of the same
  key is replaced, other target rows are left alone; a second run changes
  nothing; one transaction on the target. The target's schema must exist
  (start the app once on it); the source gets no writing statement (READ
  ONLY on Postgres). As a CF task of an app with BOTH services bound:
  `python scripts/copy_registry_config.py --from postgres --to hana`;
  elsewhere `--from-env NAME --to-env NAME` name environment variables that
  hold the URLs (never a URL as an argument). Both go through the resolver
  of `agents/db.py` (a refused `--from` / `--to` value is not echoed).
  `DB_KIND` chooses neither side; because `agents.db` refuses to be imported
  with two bound services and no `DB_KIND`, the script sets
  `DB_KIND=postgres` for its own process when the variable is unset, which
  only decides the engine `agents.db` builds for itself and through which
  the script reads and writes nothing. Dry run by default (`--apply`
  writes): per table the counts to insert / replace / unchanged and the
  names concerned (counts only for the OAuth tables); no other column value
  is printed or logged, and a failure is reported by its class only.
  Timestamps are handed over as aware UTC instants (Postgres returns aware
  datetimes, HANA and SQLite naive UTC). Rows that exist only in the target
  are listed per table (left alone); a unique value (`api_slug`) that a
  target-only row holds and a source row of another name needs is a refusal
  by table and names before anything is written. Setting `non_production`,
  or the destination of a flagged target, writes the app's own audit rows
  (`conventions_flag`, `conventions_destination`) in the same transaction,
  as actor `script:copy_registry_config`. **Around `--apply`**: the app on
  the source should be stopped or no longer in use; the app on the target
  must be restarted (or reloaded) afterwards; user tokens are copied as
  they are at that moment, so one that either side refreshes later makes the
  other side's copy stale and a second run overwrites the target's. A
  failure after the target's commit (closing the source) is reported as
  "written", never as "nothing was written"
- `scripts/hana_schema_history.py` — pins the digest of the current HANA
  artifact set for `HANA_SCHEMA_GENERATION` in
  `agents/hana_schema_history.json`; only ever adds a generation
- `scripts/probe_odata_connectivity.py` — standalone probe (stdlib + `httpx`,
  nothing imported from the app) run inside an app container that has the
  connectivity binding: proves HTTP forward mode on an `http://` virtual host,
  the technical-user path and which principal-propagation mode the proxy
  accepts; prints names, statuses and the SAP user header, never a credential
  or a body; a user run takes a one-time passcode from the terminal
- `scripts/probe_sharepoint.py` — standalone go/no-go probe for
  `builtin:sharepoint` (nothing imported from the app, reads only), run
  inside an app container that has the destination binding: does the
  destination hand out a Graph token, does it resolve the site, is the
  library there under that name, is the file there with its download
  location on the site's host, does the download answer with a workbook.
  Prints names, sizes, statuses and host names, never a token, the download
  URL or a byte of the file (`tests/test_probe_sharepoint.py`)
- `scripts/probe_bitbucket.py` — standalone go/no-go probe for
  `builtin:bitbucket` (nothing imported from the app, GET requests only),
  run inside an app container that has the destination binding: does the
  destination resolve to an `https` host (with or without `/2.0`), does
  Bitbucket identify the account, is the workspace visible, is each named
  repository visible, can the open pull requests to a branch be listed, and
  (with `--pull-request`) does the diff answer with its redirect on the
  destination's host. One `PASS` / `FAIL` / `SKIP` line per step: names typed
  on the command line, statuses, counts, sizes and host names, never a
  token, a `Location`, a response body, an exception text, a line of a
  diff, a title or a user name (`tests/test_probe_bitbucket.py`)
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
1. Lifespan: `init_db()` (on SAP HANA: the HDI deploy of the generated
   artifacts, a no-op when the container is current or holds a newer schema
   generation) →
   `seed_from_file_if_empty(SEED_FILE)` →
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
7. An agent with a `builtin:odata` entry calls `execute_operation`: catalogue
   and write checks (service, target, fields, the two write switches) -> for a
   write, the `intent` audit row is committed -> the service's destination is
   resolved as its identity (technical, or the bound user JWT) -> a request
   to the destination's host, through the connectivity proxy for an OnPremise
   destination -> rows cut to the readable fields -> the result row is
   written. A refusal or a SAP error is returned to the model as an `error`
   object, never raised

## Running locally
```bash
pip install -r requirements.txt
cp .env.example .env  # AICORE_* + (optional) DATABASE_URL
python app.py         # listens on 127.0.0.1; HOST=0.0.0.0 to open it up
# Chat:  http://127.0.0.1:7932/chat
# Admin: http://127.0.0.1:7932/admin  (no XSUAA locally → open access)
```

Local falls back to SQLite if no `DATABASE_URL` is set. A `hana://` URL
needs `HANA_HDI_USER` / `HANA_HDI_PASSWORD` as well (see `agents/db.py`).

## Dependencies
All pinned in `requirements.txt` to the versions the suites last ran on;
bump a pin, rerun the suites, then deploy.
- `pydantic-ai[mcp,web,openai,bedrock]`, `sap-ai-sdk-gen[all]`, `mcp`,
  `httpx`, `uvicorn`, `python-dotenv` (pip warns that the `pydantic-ai`
  meta package declares none of those extras; the imports work because it
  pulls them in anyway)
- `fastapi`, `jinja2`, `python-multipart` — admin UI
- `builtin:odata` adds no dependency: `$metadata` is parsed with the standard
  library's expat
- `openpyxl` and `defusedxml` — `builtin:sharepoint` reads `.xlsx`; openpyxl
  uses defusedxml when it is installed and then refuses entity declarations
  (not a DTD as such). That holds only without `lxml`: with it openpyxl
  parses the parts it reads whole with lxml instead, so `lxml` must not be
  added to `requirements.txt` (both pins, `openpyxl.DEFUSEDXML` and
  `openpyxl.LXML is False` asserted by `tests/test_sharepoint_requirements.py`)
- `sqlalchemy[asyncio]`, `asyncpg` (Postgres on CF), `aiosqlite` (the
  local SQLite fallback), `sqlalchemy-hana` and `hdbcli` (SAP HANA Cloud;
  imported only when a hana binding or URL is in use) — dynamic agent storage
- `pyjwt[crypto]` — XSUAA JWT validation
- Database-specific suites are opt-in and SKIPPED without their variable:
  `TEST_POSTGRES_URL` (`tests/pg.py`, a schema per test:
  `tests/test_odata_postgres.py`, `tests/test_ide_postgres.py` and one case
  of `tests/test_copy_registry_config.py`) and
  `TEST_HANA_SERVICE_KEY` (`tests/hana.py`: the JSON of a service key of a
  throw-away `hdi-shared` container, in the environment only;
  `tests/test_hana_integration.py` deploys through `init_db`, empties every
  table around each test and must run serially, one process per container).
  `tests/ide_concurrency.py` holds the races of the IDE store once; the
  Postgres and the HANA suite both run them
- Test-only: `pytest`, `pytest-asyncio` (`pytest.ini` sets
  `asyncio_mode = auto`), `jsdom` via the root `package.json`, `ruff`
  (`ruff.toml`, advisory in CI). Script-style suites patch
  `create_mcp_server` and `Agent.__init__` at import; `tests/conftest.py`'s
  `real_agents_and_mcp` fixture restores the real ones for tests that build
  real agents. `tests/test_admin_ui.py` (the classic admin in jsdom) is
  script-style: run it as
  `NODE_PATH=$(npm root) .venv/bin/python tests/test_admin_ui.py`; pytest
  collects nothing from it. After a pydantic-ai bump rerun the deferred-tool
  spike with
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
