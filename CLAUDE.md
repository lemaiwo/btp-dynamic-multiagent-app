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
  write, under a row lock
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
  is rejected at admin validation. `builtin:odata` is the one whose entry
  names no destination and the one factory that also receives the catalogue
  snapshot the registry loaded; it is always built with the storing audit
  recorder (`stored_recorder()`), so no caller can build it with writes that
  are not recorded
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
  refuses that request. No JWT bound is `DestinationUserRequired` before any
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
  argument checks, and for such a client the caller's `If-Match`,
  `If-None-Match`, `X-CSRF-Token`, `Cookie`, `X-HTTP-Method` and
  `X-HTTP-Method-Override` win over a `URL.headers.*` property of that name).
  A client built for the connectivity route (the OData callers) that acts as
  the signed-in user on an **Internet** destination requires a destination
  that signs in as that user: resolved for the user, a user-propagating
  `Authentication`, and an `Authorization` the destination service minted
  (`Destination.static_headers` tells stored `URL.headers.*` from it; a
  stored `Authorization` or `Cookie` is never sent on a user run). Otherwise
  `NotUserPropagating` (a `DestinationRefused`, like `OnPremiseRefused`, with
  a fixed `admin_text`) before anything is sent: the destination service
  ignores the user's token for a destination with a stored credential. The
  other destination users (Gmail, Outlook, Teams, Slack, Jira, SAP notes, MCP
  over a destination, the workflow http step) are not held to that rule. A
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
    refused") only when it carries an OData error envelope; every other 5xx
    (an HTML error page, a bare 503, a gateway's 502/504) is
    `write_outcome_unknown`. A decimal given as a JSON number is sent only
    with at most 15 significant digits, in a V2 or V4 body and URL literal
    alike (`common.plain_float`, the one rule). A refused navigation names
    the navigation, never its target entity set. A page of which not even
    one row fits a tool result is the refusal `result_too_large`, not an
    empty page. Known limits: V4 decimals below 0.0001 cannot be written
    (no `IEEE754Compatible`); `@odata.context` is not yet required to
    confirm a V4 write (undecided until the pilot; each confirmed V4 write
    logs status and whether `@odata.context` / `@odata.etag` were present);
    V4 enum members are not read from `$metadata`
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
    and answered as `reload_failed: true`, never as a 500. A service that
    acts as the signed-in user on a destination that does not sign in as the
    user is refused by the preview and the test call with `destination_error`
    and a fixed text. `POST /metadata`,
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
  when it created, changed or removed a catalogue service that is in use
  (answer keys `reloaded`, `reload_failed`, as for the catalogue routes).
  `GET /admin/api/credential-health` lists a
  `builtin:odata` entry once per attached service (`service`,
  `service_enabled`, `destination`, `user_context`, `state`: `resolvable` |
  `error` | `unbound` | `missing`); its `error` for a failed resolve is a
  fixed text ending in a code (`agents.odata.destinations.destination_failure`),
  the resolver's own text goes to the log
- `agents/a2a.py` — A2A (Agent-to-Agent) protocol server: agent card at
  `/.well-known/agent-card.json`, JSON-RPC at `/a2a` (`message/send`,
  `message/stream`, `tasks/get`, `tasks/cancel`). Used by SAP Joule.
- `agents/cf_api.py` — CF v3 API restart helper (optional, password grant)
- `templates/admin.html` — Admin UI (single-page, vanilla JS); attaches
  catalogue services to an agent (checkbox list and Allow writes), the
  catalogue itself is edited only in `ui5-admin/`
- `ui5-admin/` — SAPUI5 (TypeScript) rebuild of the admin UI, deployed to the
  BTP HTML5 Application Repository and served at `/ui5admin`. Runs **alongside**
  `templates/admin.html`, which is unchanged and still the supported admin at
  `/admin`. All HTTP goes through `webapp/service/AdminService.ts`; see
  `docs/UI5_ADMIN.md`. The server dialog's toolset dropdown comes from
  `webapp/model/builtins.ts`, which mirrors `agents/builtins.py` and lists the
  auth modes the server accepts per built-in. The **OData services** area
  (nav entry between Skills and Runs; `view/ODataServices` = list with Used by,
  Write tag, import of a configuration; `view/ODataServiceDetail` = identity
  ("Runs as" signed-in or technical user, destination field), purpose / not
  for, entity sets, operations, used by, test call) keeps its logic in
  `model/odataCatalog.ts` and `model/odataDestinations.ts` and its dialogs in
  `controller/odata/` (entity set, operation, `$metadata` import, duplicate).
  Rules the UI enforces: nothing is ticked by an import; every path that
  stores or enables a write (a ticked create/update/delete, an enabled
  data-changing operation) or widens access (unticking Changes data) lists it
  and asks on Save; a save is a GET plus a `PUT` with `expected_updated_at`;
  the destination field is an `sap.m.Input` with suggestions, `autocomplete`
  off and a value help (own picker on a phone), so the stored value is what was
  typed or explicitly picked; it relies on public API only
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
  `init_db` at start; 2.20.0 adds the `agent-connectivity` resource
  (`connectivity`, plan `lite`, bound to the app) and `ODATA_AUDIT_RETENTION_DAYS`
  (365); `CONNECTIVITY_PP_MODE` is not in the descriptor (set per landscape in
  an `.mtaext`; default `exchange`; an unknown value is one WARNING at
  startup and a refusal of each affected call)
- `scripts/probe_odata_connectivity.py` — standalone probe (stdlib + `httpx`,
  nothing imported from the app) run inside an app container that has the
  connectivity binding: proves HTTP forward mode on an `http://` virtual host,
  the technical-user path and which principal-propagation mode the proxy
  accepts; prints names, statuses and the SAP user header, never a credential
  or a body; a user run takes a one-time passcode from the terminal
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

Local falls back to SQLite if no `DATABASE_URL` is set.

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
