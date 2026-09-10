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

/** GET /admin/api/runs/{id} adds the report body to the list shape. */
export interface JobRunDetail extends JobRun {
    /** Inner fields stay optional: runs predating markdown reports have no
     * body_md (see api_get_run_markdown's 404 path). */
    report?: { body_md?: string; summary?: string } | null;
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
    /** If true, delete agents/skills absent from the import. */
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
