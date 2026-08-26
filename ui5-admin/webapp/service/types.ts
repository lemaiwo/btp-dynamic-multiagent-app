/**
 * TypeScript mirrors of the Pydantic payloads in `agents/admin.py`.
 *
 * Response types (`Agent`, `Skill`) carry the extra server-side fields that
 * `to_dict()` adds; input types (`AgentInput`, `SkillInput`) carry only what the
 * API accepts. Keeping them separate is what stops `id` or `created_at` being
 * POSTed back and rejected.
 */

/** MCP transport auth. Mirrors VALID_AUTH_MODES in agents/db.py. */
export type AuthMode = "jwt" | "none" | "oauth2" | "app_only" | "destination";

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
           * `destination` only. Whether the agent gets a comment tool.
           * Separate from what the destination's credential permits, for the
           * same reason `allow_send` is.
           */
          allow_comment?: boolean;
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
export interface CredentialStatus {
    url: string;
    auth_mode: AuthMode;
    needs_token: boolean;
    has_token: boolean;
    /** Relative to the backend host, e.g. "/oauth/login?agent=...&server=...". */
    login_url: string;
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
