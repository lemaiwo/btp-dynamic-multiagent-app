/**
 * TypeScript mirrors of the Pydantic payloads in `agents/admin.py`.
 *
 * Response types (`Agent`, `Skill`) carry the extra server-side fields that
 * `to_dict()` adds; input types (`AgentInput`, `SkillInput`) carry only what the
 * API accepts. Keeping them separate is what stops `id` or `created_at` being
 * POSTed back and rejected.
 */

/** MCP transport auth. Mirrors VALID_AUTH_MODES in agents/db.py. */
export type AuthMode = "jwt" | "none" | "oauth2" | "app_only" | "destination" | "session";

/**
 * OAuth2 client config for an `auth_mode: "oauth2"` server.
 *
 * Two mutually exclusive shapes, which is exactly what the current vanilla-JS
 * admin gets wrong at runtime:
 *  - `dcr: true` — discover the authorization server and register dynamically
 *  - manual — `client_id` plus either `uaa_url` or both endpoint URLs
 */
export type OAuthClient =
    | { dcr: true; scope?: string }
    | {
          dcr?: false;
          /**
           * Required for `oauth2`/`app_only`; validated at runtime rather than
           * by the type, because `destination` mode carries none at all.
           */
          client_id?: string;
          /** Blank on edit means "keep the stored secret". */
          client_secret?: string;
          uaa_url?: string;
          authorize_url?: string;
          token_url?: string;
          scope?: string;
          /** Read-only echo from the server; ignored on input. */
          has_client_secret?: boolean;
          /**
           * `app_only` only. An app-only token identifies no user,
           * so the target mailbox cannot be inferred and must be named.
           */
          mailbox?: string;
          /**
           * `app_only` only. Whether the agent gets a send tool.
           * Deliberately separate from what the token permits: a tenant that
           * granted Mail.Send must not thereby hand every agent the ability to
           * send mail. See agents/outlook_tools.py.
           */
          allow_send?: boolean;
          /**
           * How far back a mail listing may reach: "90m", "5h", "2d", "1w", or
           * a bare number of hours. A ceiling the agent can narrow but not
           * widen, so a busy Inbox does not hand it years of backlog.
           */
          lookback?: string;
          /**
           * `destination` only. Names the BTP destination that holds the
           * target's URL and credential. This mode stores no credential at
           * all: rotation happens in the destination, not here.
           */
          destination?: string;
          /** `destination` only. Jira project key the listing is pinned to. */
          project?: string;
          /** `destination` only. Issue status the listing is pinned to. */
          status?: string;
          /**
           * `destination` only. REST prefix appended to the destination's URL,
           * defaulting to Jira's own `/rest/api/2`. Configurable because a
           * destination fronted by an API proxy may contribute part of that
           * path itself, where the default would double the `/rest` segment.
           * A path, never a URL — the host comes from the destination.
           */
          api_base?: string;
          /**
           * `destination` only. Comma-separated Jira labels, combined with
           * AND — an issue must carry every one of them. That is the opposite
           * of how `status` combines, because an issue has many labels but
           * only one status. Sent as a string; the server parses it.
           */
          labels?: string;
          /**
           * `destination` only. Whether the agent gets a comment tool.
           * Separate from what the destination's credential permits, for the
           * same reason `allow_send` is.
           */
          allow_comment?: boolean;
          /**
           * `builtin:sapnotes` only, on `auth_mode: "none"`. The CVSS floor a
           * note must reach to be reported; 9.0 is what SAP calls HotNews.
           * Sent as a string like every other field here; the server parses it.
           *
           * The CVE source is deliberately NOT configurable — it is pinned to
           * SAP's own CNA id in `agents/sapnotes_tools.py`, because a
           * different value does not narrow the toolset, it breaks it.
           */
          min_score?: string;
          /**
           * `builtin:outlook` only. Comma-separated fixed audience for mail
           * the agent originates. Not a tool argument by design: every other
           * mail tool acts on a message that already exists, so the audience
           * is whoever wrote in — originating mail has no such anchor, and
           * the choice must not be one an injected instruction can make.
           */
          recipients?: string;
          /**
           * `builtin:smtp` only. Sender address overriding the MAIL
           * destination's `mail.smtp.from`. Blank uses the destination's.
           */
          from?: string;
          /**
           * `builtin:smtp` and `builtin:outlook` only. The look of originated
           * mail: `band`, `band_text`, `band_sub`, `accent`, `link`,
           * `heading`, `head_cell`, `zebra`, `shell` (hex colours), `font`,
           * `logo_url` (https), `org_name`, `footer`. Validated server-side
           * by `MailTheme.from_config` in agents/mail_render.py.
           */
          theme?: Record<string, string>;
          /**
           * `builtin:teams` only. The id of the one team the toolset may read
           * (and, with `allow_send` on oauth2, post to). Pinned, never a tool
           * argument. See agents/teams_tools.py.
           */
          team?: string;
          /**
           * `builtin:teams` only. Comma-separated channel names or ids that
           * narrow `team` further; blank means every channel of the team.
           */
          channels?: string;
          // --- destinations ---
          /**
           * `destination` only. On, the destination is resolved with the
           * signed-in user's JWT (sent to the destination service as
           * X-user-token) and the tools act as that user; off, the
           * destination's own app-level credential is used and the app-only
           * rules apply (mailbox required, Teams read-only). Only sent as
           * `true`. See agents/destination_auth.py.
           */
          user_context?: boolean;
          // --- odata ---
          /**
           * `builtin:odata` only. The catalogue services (their slugs, 1-50
           * distinct) this agent may call. The entry carries no `destination`
           * and no `user_context`: both belong to each catalogue service.
           */
          services?: string[];
          /**
           * `builtin:odata` only. Opens the write operations the catalogue
           * enables for the attached services. Only sent as `true`.
           */
          allow_write?: boolean;
      };

export interface McpServer {
    url: string;
    auth_mode: AuthMode;
    oauth?: OAuthClient;
}

/** What POST/PUT /admin/api/agents accepts. */
export interface AgentInput {
    name: string;
    description: string;
    instructions: string;
    mcp_servers: McpServer[];
    skills: string[];
    enabled: boolean;
    expose_chat: boolean;
    expose_api: boolean;
    api_slug: string;
    run_as_principal: string;
    run_prompt: string;
    run_timeout_seconds: number;
    /** Names of other agents this one may consult directly; each becomes a
     * `delegate_<peer>` tool on it. */
    peers: string[];
    /** Overrides the globally-active LLM for this agent only. Blank means
     * "use the active model" (see `ModelInfo`, `GET /admin/api/model`). */
    model_name: string;
    /** Deep-agent tools (planning, scratchpad, sub-agents). Optional on the
     * wire: an absent key keeps what is stored, like `peers`. See the
     * `--- deep agents ---` section at the end of this file. */
    deep?: DeepConfig;
}

/** What GET /admin/api/agents returns. Servers are redacted. */
export interface Agent extends AgentInput {
    id: number;
    /** Legacy single-server fields; the DB columns are NOT NULL and
     * to_dict() always emits them. */
    mcp_url: string;
    auth_mode: AuthMode;
    created_at: string | null;
    updated_at: string | null;
}

export interface SkillInput {
    name: string;
    description: string;
    content: string;
}

export interface Skill extends SkillInput {
    id: number;
    created_at: string | null;
    updated_at: string | null;
}

/**
 * Run statuses.
 *
 * The first four are what `agents/job_runner.py` and `agents/db.py` assign
 * today. `"degraded"` is a LEGACY value: it is written by no current code
 * path, but rows carrying it still exist — half the runs in the deployed
 * acceptance database have it, left over from the superseded checklist-report
 * design. Grepping the writer is not enough; superseding the writer does not
 * rewrite history. Do not remove it without checking stored data.
 */
export type RunStatus =
    | "running"
    | "success"
    | "failed"
    | "interrupted"
    | "degraded";

export interface JobRun {
    id: string;
    agent_id: number;
    agent_name: string;
    trigger: string;
    status: RunStatus;
    started_at: string | null;
    finished_at: string | null;
    summary: string | null;
    error: string | null;
    notified: boolean;
    created_by: string | null;
}

/** One step of what a job run did; mirrors `agents/run_activity.py`.
 * `kind` is `tool` for a tool call, otherwise a free-text phase note. */
export interface RunActivityEvent {
    ts: string;
    agent: string;
    kind: "tool" | "note" | "message" | "delegation_start";
    tool?: string;
    detail?: string;
    status?: "running" | "ok" | "error";
    output?: string;
}

/** A deep agent's plan as its last `write_todos` call left it. */
export interface RunPlanItem {
    content: string;
    status: "pending" | "in_progress" | "completed";
}

export interface RunActivity {
    events: RunActivityEvent[];
    plan: RunPlanItem[];
    /** Oldest events dropped to keep the list bounded. */
    dropped: number;
}

/** GET /admin/api/runs/{id} adds the report body to the list shape. */
export interface JobRunDetail extends JobRun {
    /** Inner fields stay optional: runs predating markdown reports have no
     * body_md (see api_get_run_markdown's 404 path). */
    report?: { body_md?: string; summary?: string } | null;
    /** Live while the run is running, stored once it ends; null for runs
     * recorded before activity existed. */
    activity?: RunActivity | null;
}

/** Per-MCP-server credential status for a principal. */
/**
 * How usable a stored credential is. Mirrors `token_status` in
 * `agents/oauth2.py`.
 *
 * `refreshable` means the access token has expired but a refresh token is
 * stored, so the connection renews itself without an interactive sign-in —
 * working, not broken. `expired` has nothing left to refresh with.
 */
export type TokenState = "none" | "valid" | "refreshable" | "expired";

export interface CredentialStatus {
    url: string;
    auth_mode: AuthMode;
    needs_token: boolean;
    has_token: boolean;
    /** Relative to the backend host, e.g. "/oauth/login?agent=...&server=...". */
    login_url: string;
    token_state: TokenState;
    /** ISO expiry of the access token, or null when the server issued no
     * `expires_in` (the token does not expire on its own). */
    expires_at: string | null;
    /** True for `app_only` and `destination` servers: connected by
     * configuration, not by anyone signing in. For those the server reports
     * `has_token: true` and `token_state: "valid"` so the panel does not show
     * "not connected" with no way to fix it. */
    no_user_token: boolean;
}

/** One agent/server whose scheduled runs will fail for want of a credential. */
export interface CredentialProblem {
    agent: string;
    server_key: string;
    principal: string;
    token_state: TokenState | "unknown";
    expires_at: string | null;
}

/**
 * Health of the credentials scheduled runs depend on.
 *
 * `refreshable` counts as healthy: it only means the access token has lapsed
 * and the stored refresh token will renew it on the next call, which is the
 * normal state between nightly runs. Only `expired`/`none` reach `problems`.
 */
export interface CredentialHealth {
    checked: number;
    healthy: number;
    problems: CredentialProblem[];
}

export interface WhoAmI {
    principal: string;
    label: string;
}

export interface AdminConfig {
    public_base_url: string;
}

/** GET /admin/api/model. `default` is the built-in fallback model name,
 * distinct from `model_name` (the currently active one). */
export interface ModelInfo {
    model_name: string;
    available: string[];
    default: string;
}

export interface OrchestratorInfo {
    instructions: string;
}

export interface ImportPayload {
    orchestrator_instructions?: string | null;
    skills: SkillInput[];
    agents: AgentInput[];
    /** Optional: an export made before workflows existed carries none, and
     * `replace` only removes workflows when the bundle has this section. */
    workflows?: WorkflowInput[];
    /** Optional: an export made before the OData catalogue existed carries
     * none. Imported before the agents, which refer to services by name. */
    odata_services?: ODataServiceInput[];
    /** If true, delete agents/skills/workflows absent from the import. */
    replace: boolean;
}

/** POST /admin/api/reload. `agents` is the total; `enabled` the subset the
 * orchestrator can delegate to. Reload rebuilds all of them, not just the
 * orchestrator. */
export interface ReloadResult {
    status: string;
    agents: number;
    enabled: number;
}

// --- Workflows -------------------------------------------------------------

/**
 * A named sub-sequence of steps a work item may or may not enter.
 *
 * `key` is what the fan-out step emits to select this branch; `description`
 * is shown to it in the branch catalogue so it chooses from a list it can
 * see rather than guessing label strings.
 */
export interface WorkflowBranch {
    key: string;
    description: string;
    position: number;
}

/**
 * One agent invocation in a workflow.
 *
 * `branch_key` is `null` for a main-line step; otherwise the step belongs to
 * that branch. `fan_out` marks the single step (main line only) that returns
 * work items for every later main-line step to run once per item.
 */
export interface WorkflowStep {
    branch_key: string | null;
    position: number;
    agent_name: string;
    instructions: string;
    fan_out: boolean;
    step_timeout_seconds: number;
    // --- step kinds ---
    /** "agent" (the default when absent) or a deterministic kind; mirrors
     * `WorkflowStep.kind` in agents/db.py. */
    kind?: StepKind;
    /** The kind's settings, validated server-side by agents/step_kinds.py;
     * `{}` for an agent step. */
    config?: StepConfig;
}

// --- step kinds ---
export type StepKind = "agent" | "condition" | "transform" | "http" | "python";

/** Wire shape of a step's config. Kept loose on purpose: the server owns the
 * schema (agents/step_kinds.py) and rejects what it does not accept; the UI
 * edits a flattened copy (see model/stepKinds.ts) and maps back. */
export type StepConfig = Record<string, unknown>;

export type ConditionSource = "text" | "item" | "json";
export type ConditionOp =
    | "contains" | "not_contains" | "equals" | "not_equals" | "matches"
    | "not_matches" | "gt" | "lt" | "is_empty" | "not_empty";
export type StepAction = "continue" | "stop";

export interface ConditionRule {
    when: { source: ConditionSource; field: string; op: ConditionOp; value: string; case_sensitive: boolean };
    then: { action: StepAction; output: string };
}
export interface ConditionConfig {
    rules: ConditionRule[];
    else: { action: StepAction; output: string };
}
export interface TransformConfig {
    extract_json: string;
    regex: { pattern: string; replace: string; flags: string } | null;
    template: string;
    truncate: number | null;
}
export interface HttpConfig {
    destination: string;
    method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
    path: string;
    query: Record<string, string>;
    headers: Record<string, string>;
    body: string;
    content_type: string;
    timeout_seconds: number;
    expect_status?: number[];
}
export interface PythonConfig {
    code: string;
    timeout_seconds: number;
}

/** Fields shared by `Workflow` and `WorkflowInput`; see each for what each
 * one adds. */
export interface WorkflowBase {
    name: string;
    description: string;
    api_slug: string;
    /** Fallback identity for steps whose agent has no run_as_principal of
     * its own. A scheduled run has no interactive user to borrow one from. */
    run_as_principal: string;
    run_timeout_seconds: number;
    /** Repeat-run safety: an item already completed by an earlier run is
     * skipped, so a retry after a crash resumes rather than re-drafting. */
    skip_seen_items: boolean;
    max_parallel_items: number;
    /** "fail" or "skip". Mirrors VALID_ON_UNKNOWN_BRANCH in agents/db.py. */
    on_unknown_branch: string;
    enabled: boolean;
}

/** What POST/PUT /admin/api/workflows accepts. */
export interface WorkflowInput extends WorkflowBase {
    branches: WorkflowBranch[];
    steps: WorkflowStep[];
}

/** GET /admin/api/workflows list entry. `Workflow.to_dict()` carries no
 * `branches`/`steps` — the detail routes add them alongside, see
 * `WorkflowDetail`. */
export interface Workflow extends WorkflowBase {
    id: number;
}

/** What GET/POST/PUT /admin/api/workflows/{id} return: the list shape plus
 * its branches and steps. */
export interface WorkflowDetail extends Workflow {
    branches: WorkflowBranch[];
    steps: WorkflowStep[];
}

/** Statuses `agents/workflow_runner.py` assigns to a `WorkflowRun`.
 * `"partial"` means at least one item succeeded or was skipped while at
 * least one other failed. */
export type WorkflowRunStatus = "running" | "success" | "failed" | "interrupted" | "partial";

export interface WorkflowRun {
    id: string;
    workflow_id: number;
    workflow_name: string;
    trigger: string;
    status: WorkflowRunStatus;
    started_at: string | null;
    finished_at: string | null;
    items_total: number;
    items_succeeded: number;
    items_failed: number;
    items_skipped: number;
    summary: string | null;
    error: string | null;
    created_by: string | null;
}

/** Statuses `agents/workflow_runner.py` assigns to a `WorkflowItemRun`.
 * `"skipped"` is `skip_seen_items` refusing an item already completed by an
 * earlier run. */
export type WorkflowItemStatus = "running" | "success" | "failed" | "interrupted" | "skipped";

/** One work item flowing through a workflow run. */
export interface WorkflowItemRun {
    id: string;
    workflow_run_id: string;
    item_key: string;
    title: string;
    /** Which branches the reader selected, for this item. */
    branches: string[];
    status: WorkflowItemStatus;
    error: string | null;
    started_at: string | null;
    finished_at: string | null;
}

export type WorkflowStepStatus = "running" | "success" | "failed" | "interrupted";

/** One agent invocation inside a workflow run. `item_run_id` is `null` for
 * steps that ran before the fan-out, i.e. once per run. */
export interface WorkflowStepRun {
    id: string;
    workflow_run_id: string;
    item_run_id: string | null;
    branch_key: string | null;
    position: number;
    agent_name: string;
    status: WorkflowStepStatus;
    output: string | null;
    error: string | null;
    started_at: string | null;
    finished_at: string | null;
}

/** GET /admin/api/workflow-runs/{run_id}: the run plus every item and step
 * that ran within it. */
export interface WorkflowRunDetail {
    run: WorkflowRun;
    items: WorkflowItemRun[];
    steps: WorkflowStepRun[];
}

// --- where used ---
/** One agent that lists the queried agent as a peer. */
export interface WhereUsedPeer {
    id: number;
    name: string;
    enabled: boolean;
}

/** One workflow step that runs the queried agent; `branch_key` null is the
 * main line. Positions count within their group, as everywhere else. */
export interface WhereUsedStep {
    position: number;
    branch_key: string | null;
}

export interface WhereUsedWorkflow {
    id: number;
    name: string;
    api_slug: string | null;
    enabled: boolean;
    steps: WhereUsedStep[];
}

/** GET /admin/api/agents/{id}/where-used. Disabled referrers are included
 * and flagged -- unlike the server's delete/disable guard, this is for an
 * operator to read, and a switched-off workflow still needs its agent. */
export interface AgentWhereUsed {
    agent: { id: number; name: string };
    peers: WhereUsedPeer[];
    workflows: WhereUsedWorkflow[];
}

// --- deep agents -------------------------------------------------------------

/** Mirrors `agents.deep.DeepConfig`. `GET` always returns it (defaults when
 * nothing is stored); `POST`/`PUT` may omit it to keep the stored value. */
export interface DeepConfig {
    enabled: boolean;
    /** `write_todos` / `read_todos` */
    planning: boolean;
    /** `ls` / `read_file` / `write_file` / `edit_file` on a per-run scratchpad */
    scratchpad: boolean;
    /** the `task` tool that runs an ephemeral sub-agent */
    subagents: boolean;
    /** 1..20 concurrent sub-agents per run */
    max_subagents: number;
    /** 1..3; 1 means sub-agents cannot spawn sub-agents of their own */
    subagent_max_depth: number;
    /** replaces the default sub-agent system prompt when non-blank */
    subagent_instructions: string;
}

export const DEEP_DEFAULTS: Readonly<DeepConfig> = Object.freeze({
    enabled: false,
    planning: true,
    scratchpad: true,
    subagents: true,
    max_subagents: 5,
    subagent_max_depth: 1,
    subagent_instructions: ""
});

// --- odata ---
// Mirrors of `agents/odata/models.py` (the definition a catalogue service
// stores) and of the answers of `/admin/api/odata/...`.

export type ODataVersion = "v2" | "v4";

/** What an entity set can offer an agent. `create`, `update` and `delete`
 * are the write operations an agent gets only with `allow_write`. */
export type ODataEntityOp = "list" | "get" | "create" | "update" | "delete";

export interface ODataKey {
    name: string;
    /** An EDM type name; the server's default is `Edm.String`. */
    type: string;
}

/** What one coded value of a field means, e.g. `05` = released. */
export interface ODataValueMeaning {
    value: string;
    meaning: string;
}

export interface ODataField {
    name: string;
    type: string;
    label: string;
    /** Returned to the agent and allowed in `$select`. */
    selectable: boolean;
    /** Allowed in `$filter` and `$orderby`. */
    filterable: boolean;
    /** Accepted in a create or update body. */
    writable: boolean;
    hint: string;
    values: ODataValueMeaning[];
    personal_data: boolean;
}

export interface ODataNavigation {
    name: string;
    /** The name of the entity set the navigation leads to. */
    target: string;
    collection: boolean;
    description: string;
}

export interface ODataExampleQuery {
    description: string;
    filter: string;
    select: string[];
    orderby: string;
    top: number | null;
}

export interface ODataEntitySet {
    name: string;
    title: string;
    /** The path segment under the service path; blank means `name`. */
    path: string;
    entity_type: string;
    description: string;
    keys: ODataKey[];
    operations: ODataEntityOp[];
    fields: ODataField[];
    navigations: ODataNavigation[];
    examples: ODataExampleQuery[];
}

export interface ODataParam {
    name: string;
    type: string;
    required: boolean;
}

/** A V2 function import, or a V4 action (POST) or function (GET). */
export interface ODataOperation {
    name: string;
    /** V4 only: the namespace-qualified name. Blank on V2. */
    qualified_name: string;
    title: string;
    kind: "function_import" | "action" | "function";
    http_method: "GET" | "POST";
    /** The name of the entity set the operation is bound to, if any. */
    bound_to: string | null;
    parameters: ODataParam[];
    description: string;
    enabled: boolean;
    /** On, the operation is a write: an agent needs `allow_write` for it. */
    changes_data: boolean;
}

export interface ODataDefinition {
    entity_sets: ODataEntitySet[];
    operations: ODataOperation[];
}

/** What POST/PUT /admin/api/odata/services accepts (`ODataServicePayload`). */
export interface ODataServiceInput {
    /** The slug agents and server entries use. Immutable once created. */
    name: string;
    title: string;
    purpose: string;
    not_for: string;
    destination: string;
    /** On, calls run as the signed-in user; off, as the destination's
     * technical user. */
    user_context: boolean;
    odata_version: ODataVersion;
    service_path: string;
    enabled: boolean;
    definition: ODataDefinition;
    /** ISO timestamp of the last metadata import, or null. */
    metadata_fetched_at: string | null;
}

/** One agent that lists the service in a `builtin:odata` server entry. */
export interface ODataUsedBy {
    agent_id: number;
    agent: string;
    enabled: boolean;
    expose_api: boolean;
    api_slug: string;
    allow_write: boolean;
}

/** One row of GET /admin/api/odata/services: everything but the definition. */
export interface ODataServiceSummary extends Omit<ODataServiceInput, "definition"> {
    id: number;
    created_at: string | null;
    updated_at: string | null;
    counts: { entity_sets: number; operations: number };
    /** An entity set with a write operation, or an enabled operation that
     * changes data. */
    has_write: boolean;
    used_by: ODataUsedBy[];
}

/** GET /admin/api/odata/services/{name}. */
export interface ODataService extends ODataServiceSummary {
    definition: ODataDefinition;
}

/** POST /admin/api/odata/metadata. Nothing is stored. */
export interface ODataMetadataRequest {
    destination: string;
    service_path: string;
    odata_version: ODataVersion;
    user_context?: boolean;
    /** The stored service to compare against; without it everything is new. */
    service?: string;
}

export type ODataPreviewStatus = "new" | "in_service" | "changed";

export interface ODataPreviewField {
    name: string;
    type: string;
    label: string;
    filterable: boolean;
    creatable: boolean;
    updatable: boolean;
}

export interface ODataPreviewEntitySet {
    name: string;
    entity_type: string;
    label: string;
    keys: ODataKey[];
    fields: ODataPreviewField[];
    navigations: { name: string; target: string; collection: boolean }[];
    capabilities: { creatable: boolean; updatable: boolean; deletable: boolean };
    status: ODataPreviewStatus;
    new_fields: string[];
    removed_fields: string[];
}

export interface ODataPreviewOperation {
    name: string;
    qualified_name: string;
    kind: ODataOperation["kind"];
    http_method: ODataOperation["http_method"];
    bound_to: string | null;
    parameters: ODataParam[];
    label: string;
    status: ODataPreviewStatus;
}

/**
 * Something the $metadata declares that the parser left out
 * (`SkippedElement` in agents/odata/metadata.py). Never the element's own
 * text: `entity_set` is the owning set (for kind `entity_set` its own name,
 * blank when that name was the problem) and `position` is its 1-based place
 * in the document, which is how an admin finds it.
 */
export interface ODataSkippedElement {
    kind: "entity_set" | "property" | "navigation" | "operation";
    entity_set: string;
    position: number;
    /** A reason code, e.g. `invalid_name`, `invalid_type`, `duplicate_name`,
     * `unrepresentable_key`, `unresolved_target`. Open-ended on purpose. */
    reason: string;
}

/** What the service's $metadata offers: names and labels only, no data. */
export interface ODataMetadataPreview {
    fetched_at: string;
    entity_sets: ODataPreviewEntitySet[];
    operations: ODataPreviewOperation[];
    /** What the parser left out; empty when it took everything. */
    skipped: ODataSkippedElement[];
    summary: { entity_sets: number; operations: number; in_service: number; changed: number };
}

/** POST /admin/api/odata/services/{name}/test. Never a data value. */
export interface ODataTestResult {
    ok: boolean;
    status: number | null;
    duration_ms: number;
    target: string;
    rows: number;
    identity: "user" | "technical";
    destination: string;
    auth_type: string;
    proxy_type: string;
    message: string;
}

/** POST /admin/api/odata/services/{name}/duplicate. What is left out is
 * taken from the source. */
export interface ODataDuplicateRequest {
    name: string;
    title?: string;
    destination?: string;
    user_context?: boolean;
}
