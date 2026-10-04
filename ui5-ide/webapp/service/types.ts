/**
 * Shapes of the /ide/api REST contract (plan §1.2) and of the SSE stream
 * that `POST .../messages`, `POST .../request-changes` and `POST .../report`
 * answer with (plan §1.3), including the phase 1c diagnose additions and the
 * review additions: comments, file revisions, pins and syntax results.
 *
 * Types only: nothing here runs, so the module is erased by the transpiler.
 */

export type Stage = "chat" | "design" | "plan" | "propose" | "review" | "done" | "investigate";
/** `change` walks the stage gates; `diagnose` stays in `investigate` (plan 1c §1.1). */
export type SessionType = "change" | "diagnose";
export type SessionStatus = "idle" | "running";
export type ArtifactKind = "design" | "plan" | "note" | "review" | "report";
export type FileState = "read" | "modified" | "new";
export type MessageRole = "user" | "assistant" | "system";
/** Where the base of a workspace file came from: read from SAP, known to be new, or the check failed. */
export type BaseStatus = "sap" | "absent" | "unknown";
/** A syntax dry run's verdict; `unavailable` is never shown as OK (assumption A5). */
export type SyntaxStatus = "ok" | "errors" | "unavailable";
/**
 * What a session waits for, first match wins (server-computed): a pending
 * trace approval, addressed comments, proposals not yet pinned, a document
 * version not yet approved.
 */
export type WaitingReason = "approval" | "comments" | "changes" | "document";

/**
 * The versions approved so far: a document kind's version, and per path the
 * file revision approved in `propose`.
 */
export interface Pins {
    design?: number;
    plan?: number;
    review?: number;
    report?: number;
    files?: Record<string, number>;
}

/** `GET /me` */
export interface Me {
    principal: string;
    is_admin: boolean;
    /** The conventions keys the caller may open a session against. */
    targets: string[];
    /** The subset of `targets` flagged `non_production`: the only ones a diagnose session may use. */
    diagnose_targets: string[];
    /** Days a diagnose session is kept from its creation; 0 = kept until it is deleted. */
    diagnose_retention_days: number;
}

/** One row of `GET /sessions`. */
export interface SessionSummary {
    id: string;
    owner: string;
    title: string;
    target: string;
    type: SessionType;
    stage: Stage;
    status: SessionStatus;
    created_at: string | null;
    updated_at: string | null;
    /**
     * The target's conventions carry `non_production: true` now. A diagnose
     * session is only created on a flagged target, so `false` on one means the
     * flag was removed since: its runs, reports, finding details and reads
     * from SAP answer 409 `target_not_non_production`; the stored data stays
     * readable.
     */
    target_non_production: boolean;
    pins: Pins;
    waiting: WaitingReason | null;
    /** Comments in state `open` (the "Request changes (n)" badge). */
    open_comments: number;
    /** Comments in state `open` or `sent`: more than 0 blocks approve (409 `open_comments`). */
    unresolved_comments: number;
    /** Model requests used so far and the session's cap (`usage` events update them during a run). */
    requests_used: number;
    request_cap: number;
    /** Up to five object names of the session's files (B10); optional until every server sends them. */
    objects?: string[];
    objects_total?: number;
    /** Objects with a proposed change. */
    changed_objects?: number;
    /** Findings of a diagnose session; `null` for a change session. */
    findings_count?: number | null;
}

/** What `POST /sessions`, `PATCH`, `approve` and `cancel` return. */
export type Session = SessionSummary;

/** An artifact listed inside `GET /sessions/{sid}` (no content). */
export interface ArtifactSummary {
    id: string;
    stage: Stage;
    kind: ArtifactKind;
    version: number;
    created_at: string | null;
    /** The pins the document was written against (`{"design": 2}` on a plan); `null` when none apply. */
    based_on: Record<string, number> | null;
}

/** `GET /sessions/{sid}/artifacts[/{aid}]` */
export interface Artifact extends ArtifactSummary {
    content: string;
}

/** `GET /sessions/{sid}/files`, `POST /sessions/{sid}/open` */
export interface FileSummary {
    path: string;
    state: FileState;
    /** `null` for a free scratch file such as `notes/impact.md`. */
    object_type: string | null;
    object_name: string | null;
    /** The latest revision of the proposal; 0 = none yet. */
    revision: number;
    /** `null` = never checked (a legacy row or a scratch file). */
    base_status: BaseStatus | null;
    /** Of the latest revision; `null` = not checked yet. */
    syntax_status: SyntaxStatus | null;
}

/** One message of a syntax dry run. */
export interface SyntaxItem {
    line: number | null;
    message: string;
    severity: "error" | "warning";
}

/** `POST /sessions/{sid}/file/syntax?path=&revision=` */
export interface SyntaxResult {
    path: string;
    revision: number;
    status: SyntaxStatus;
    items: SyntaxItem[];
    checked_at: string | null;
}

/** One row of `GET /sessions/{sid}/file/revisions?path=`, newest first. */
export interface FileRevision {
    revision: number;
    run_id: string | null;
    created_at: string | null;
    /** Length of the revision's proposed source. */
    chars: number;
    syntax_status: SyntaxStatus | null;
}

/** One SAPLint finding. */
export interface LintFinding {
    /** `null` when the linter could not place the finding. */
    line: number | null;
    column: number | null;
    severity: string;
    message: string;
    rule: string;
}

/**
 * `GET /sessions/{sid}/file?path=&revision=` and `POST .../file/refresh?path=`.
 * With `revision`, `proposed_source` and `syntax` are that revision's.
 */
export interface FileDetail extends FileSummary {
    /** The diff base: the source as ARC-1 last returned it. */
    origin_source: string | null;
    proposed_source: string | null;
    /** SAP's version marker of `origin_source`; `null` = version unknown. */
    origin_version: string | null;
    lint: LintFinding[];
    /** The syntax messages of the revision served. */
    syntax: SyntaxItem[];
}

/** `GET /sessions/{sid}`: the session plus its artifact and file lists. */
export interface SessionDetail extends Session {
    artifacts: ArtifactSummary[];
    files: FileSummary[];
}

/** `GET /sessions/{sid}/messages` */
export interface Message {
    id: string;
    role: MessageRole;
    stage: Stage;
    content: string;
    created_at: string | null;
    /** The run's tool timeline and plan are served by `GET .../messages/{mid}/activity`. */
    has_activity: boolean;
}

/** `GET /sessions/{sid}/messages/{mid}/activity`; 404 `no_activity`. */
export interface Activity {
    events: ToolEventData[];
    plan: Todo[];
    dropped: number;
}

// --- Review comments (contract §1.1-1.2) -----------------------------------

/**
 * `open` (written, editable) → `sent` (a request-changes run took it) →
 * `addressed` (the agent answered it); `dismissed` by the user. The user may
 * reopen an addressed or dismissed comment. `sent` exists only while the run
 * is in flight: when it ends (done, failed or cancelled) every comment it did
 * not address is `open` again.
 */
export type CommentState = "open" | "sent" | "addressed" | "dismissed";

/** A comment on lines of one revision of a workspace file (1-based, inclusive). */
export interface FileCommentCreate {
    anchor: "file";
    path: string;
    revision: number;
    line_start: number;
    line_end: number;
    /** 1..4000 characters, plain text. */
    body: string;
    /** The first selected line, trimmed to 200 characters (plain text). */
    quote?: string;
}

/** A comment on a paragraph (0-based block index) of one version of a document. */
export interface DocumentCommentCreate {
    anchor: "document";
    kind: ArtifactKind;
    version: number;
    paragraph: number;
    body: string;
    /** The start of the commented block, trimmed to 200 characters (plain text). */
    quote?: string;
}

/** `POST /sessions/{sid}/comments`; 422 `invalid_anchor`. */
export type CommentCreate = FileCommentCreate | DocumentCommentCreate;

/** `GET|POST|PATCH /sessions/{sid}/comments[/{cid}]`: both anchors' fields, the unused ones `null`. */
export interface Comment {
    id: string;
    anchor: "file" | "document";
    path: string | null;
    revision: number | null;
    line_start: number | null;
    line_end: number | null;
    kind: ArtifactKind | null;
    version: number | null;
    paragraph: number | null;
    body: string;
    state: CommentState;
    /** The agent's one-line answer once `addressed`. */
    answer: string | null;
    /** What the anchor pointed at when the comment was written (plain text); absent from older servers. */
    quote?: string | null;
    created_at: string | null;
    updated_at: string | null;
}

/** `GET|PUT /conventions/{target}`; `PUT` takes `clear` as well and no longer creates (404 `unknown_target`). */
export interface Conventions {
    target: string;
    label?: string | null;
    destination?: string | null;
    namespace?: string | null;
    package?: string | null;
    atc_variant?: string | null;
    clean_core_level?: string | null;
    free_text?: string | null;
    /** Only a non-production target accepts diagnose sessions and trace approvals. */
    non_production?: boolean;
    updated_at?: string | null;
}

/** A conventions field `PUT /conventions/{target}` can empty through `clear`. */
export type ConventionsClearable = "label" | "destination" | "namespace" | "package" | "atc_variant" | "free_text";

/**
 * The body of `PUT /conventions/{target}` (without `clear`, which
 * IdeService adds): never the target itself, which the server refuses as an
 * unknown key. Omitted fields keep their stored value.
 */
export type ConventionsChange = Omit<ConventionsCreate, "target">;

/** `POST /conventions` (admin): 201, 409 `target_exists`, 422 for a bad target name. */
export interface ConventionsCreate {
    target: string;
    label?: string;
    destination?: string;
    namespace?: string;
    package?: string;
    atc_variant?: string;
    clean_core_level?: string;
    free_text?: string;
    /** Strictly a boolean: the server refuses `"true"`. */
    non_production?: boolean;
}

// --- Diagnose sessions (plan 1c §1.2) --------------------------------------

export type FindingKind = "dump" | "trace" | "gateway_error" | "auth_check" | "odata_call";

/** One row of `GET /sessions/{sid}/findings`: the metadata; the dump or trace text is {@link FindingDetail}. */
export interface DiagnoseFinding {
    id: string;
    kind: FindingKind;
    ref_id: string;
    title: string;
    program: string | null;
    include: string | null;
    line: number | null;
    occurred_at: string | null;
    created_at: string | null;
}

/**
 * `GET /sessions/{sid}/findings/{fid}`: `detail` is the text kept with the
 * finding, as SAP sent it: a diagnose session only exists on a target flagged
 * non-production. `?refresh=true` reads it again from SAP. Once the target
 * lost its flag the route answers 409 `target_not_non_production`, for the
 * stored text as for a refresh.
 */
export interface FindingDetail {
    finding: DiagnoseFinding;
    detail: string;
}

/** `POST /sessions/{sid}/findings/{fid}/open`; 422 `no_source` when there is no program. */
export interface FindingOpen {
    file: FileSummary;
    line: number | null;
    hint: string | null;
}

export type TraceProcessType = "http" | "dialog" | "batch" | "rfc";
export type TraceObjectType = "any" | "url" | "transaction" | "report" | "functionModule";

/** What a `trace_start` approval would arm; the bounds are enforced server-side. */
export interface TraceParams {
    processType: TraceProcessType;
    objectType: TraceObjectType;
    /** 1..IDE_TRACE_MAX_EXECUTIONS */
    maxExecutions: number;
    /** 1..IDE_TRACE_MAX_HOURS */
    expiresHours: number;
    sqlTrace: boolean;
    aggregate: boolean;
    /** At most 60 characters. */
    description: string;
}

export type ApprovalAction = "trace_start" | "trace_cancel";
export type ApprovalStatus = "pending" | "approved" | "denied" | "failed" | "expired";
export type ApprovalDecision = "approve" | "deny";

/**
 * `GET /sessions/{sid}/approvals`, `POST .../approvals/{aid}`.
 *
 * An approval is a stored row (plan 1c spike: variant B). The agent proposes
 * a trace, the run goes on and ends; nothing waits for the decision. The UI
 * lists the rows and decides them through the route, and takes every state
 * change from the route's answer or from the list:
 *
 * - `pending` stays `pending` in the list even past its time to live; the
 *   decide route then answers 410 `approval_expired` and the row is `expired`.
 * - `approved` with `result: null` (or, for a `trace_start`, without a
 *   `trace_request_id`) means the outcome is unknown, not that a trace is armed.
 * - `failed` comes back from the decide route as a 200: the decision was
 *   taken, arming did not work (`error_code`, for example
 *   `arc1_timeout_unknown`, `interrupted`, `audit_unavailable`).
 */
export interface Approval {
    id: string;
    action: ApprovalAction;
    /**
     * `TraceParams` for `trace_start`, `{id}` (the trace request) for
     * `trace_cancel`. It is stored JSON: model/approvals checks its shape
     * before a card offers Approve.
     */
    params: TraceParams | { id: string };
    status: ApprovalStatus;
    created_at: string | null;
    decided_at: string | null;
    /** `note`: the server's remark on an unclear outcome (`arc1_timeout_unknown`, `interrupted`). */
    result: { trace_request_id?: string; expires_at?: string; note?: string } | null;
    error_code: string | null;
    /**
     * Minutes a pending approval can be decided, counted from `created_at`
     * (the server's `approval_ttl_min()`, sent with every approval). Optional
     * for an answer of a server that does not send it yet: no decide-by hint then.
     */
    ttl_min?: number;
}

/** `GET /admin/sessions`: metadata only, never content. */
export interface AdminSessionRow {
    id: string;
    owner: string;
    title: string;
    target: string;
    type: SessionType;
    stage: Stage;
    status: SessionStatus;
    created_at: string | null;
    updated_at: string | null;
}

// --- SSE stream (plan §1.3) ------------------------------------------------

export interface RunEventData { run_id: string; stage: Stage; message_id: string }
export interface TextEventData { delta: string }
/** One RunActivity event; start and end share `id`, so the UI upserts by it. */
export interface ToolEventData {
    ts?: string | null;
    agent?: string | null;
    /** `tool` for a call (with `id`/`tool`/`status`); a note, message or delegation carries none of those. */
    kind: string;
    id?: string | null;
    tool?: string | null;
    detail: string;
    status?: string | null;
    ended?: string | null;
    output?: string | null;
    /**
     * Why a call was refused (status `error`): `readonly_refused` by the
     * read-only guard, or a trace proposal that was not stored
     * (`too_many_pending`, `unknown_trace_request`, `target_not_non_production`,
     * `not_diagnose`, `invalid_request`, `proposal_failed`).
     */
    code?: string | null;
}
export interface Todo { content: string; status: string }
export interface PlanEventData { todos: Todo[] }
export interface FileEventData {
    path: string;
    state: FileState;
    revision?: number;
    base_status?: BaseStatus | null;
    syntax_status?: SyntaxStatus | null;
}
/**
 * After a request-changes start (`sent`, with `left`: open comments not sent
 * this round because of the server's cap; 0 = none), per `resolve_comments`
 * call (`addressed`), and at the end of the run for comments still `sent`
 * (`open` again).
 */
export interface CommentsEventData { ids: string[]; state: "sent" | "addressed" | "open"; left?: number }
export interface ArtifactEventData { id: string; kind: ArtifactKind; version: number }
export interface UsageEventData { requests_used: number; request_cap: number }
export interface ErrorEventData { message: string; code?: string }
export interface DoneEventData { message_id: string; stage: Stage; status: SessionStatus }
/** After a diagnose run, once per new or updated finding. */
export type FindingEventData = DiagnoseFinding;
/**
 * `approval_required`: a pending approval the run just stored. The run does
 * not wait for it. The `approval` frame type is kept in the union for the
 * parser, but no stream sends it: there is no resumed stream, a decision is
 * the answer of `POST .../approvals/{aid}`.
 */
export type ApprovalEventData = Approval;

/** One parsed SSE frame, discriminated on `type` (the frame's `event:` line). */
export type SseEvent =
    | { type: "run"; data: RunEventData }
    | { type: "text"; data: TextEventData }
    | { type: "tool"; data: ToolEventData }
    | { type: "plan"; data: PlanEventData }
    | { type: "file"; data: FileEventData }
    | { type: "artifact"; data: ArtifactEventData }
    | { type: "usage"; data: UsageEventData }
    | { type: "error"; data: ErrorEventData }
    | { type: "finding"; data: FindingEventData }
    | { type: "approval_required"; data: ApprovalEventData }
    | { type: "approval"; data: ApprovalEventData }
    | { type: "comments"; data: CommentsEventData }
    | { type: "done"; data: DoneEventData };

export type SseEventType = SseEvent["type"];
