/**
 * Shapes of the /ide/api REST contract (plan §1.2) and of the SSE stream
 * that `POST .../messages`, `POST .../revise` and `POST .../report` answer
 * with (plan §1.3), including the phase 1c diagnose additions.
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

/** `GET /me` */
export interface Me {
    principal: string;
    is_admin: boolean;
    /** The conventions keys the caller may open a session against. */
    targets: string[];
    /** The subset of `targets` flagged `non_production`: the only ones a diagnose session may use. */
    diagnose_targets: string[];
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
     * `true` when the target's conventions are not flagged `non_production`
     * now. A diagnose session is only created on a flagged target, so `true`
     * on one means the flag was removed since: its runs, reports, finding
     * details and reads from SAP answer 409 `target_not_non_production`. The
     * session, its messages, documents, files and finding list are still
     * served. Absent or `false`: dumps and traces are sent to the model as
     * they are and kept with the session.
     */
    masked?: boolean;
}

/** What `POST /sessions`, `PATCH`, `approve` and `cancel` return. */
export type Session = SessionSummary;

/** An artifact listed inside `GET /sessions/{sid}` (no content). */
export interface ArtifactSummary {
    id: string;
    kind: ArtifactKind;
    version: number;
    created_at: string | null;
}

/** `GET /sessions/{sid}/artifacts[/{aid}]` */
export interface Artifact extends ArtifactSummary {
    content: string;
    stage?: Stage;
}

/** `GET /sessions/{sid}/files`, `POST /sessions/{sid}/open` */
export interface FileSummary {
    path: string;
    state: FileState;
    /** `null` for a free scratch file such as `notes/impact.md`. */
    object_type: string | null;
    object_name: string | null;
}

/** One SAPLint finding. */
export interface LintFinding {
    line: number;
    column: number;
    severity: string;
    message: string;
    rule: string;
}

/** `GET /sessions/{sid}/file?path=` and `POST .../file/refresh?path=` */
export interface FileDetail {
    path: string;
    state: FileState;
    /** The diff base: the source as ARC-1 last returned it. */
    origin_source: string | null;
    proposed_source: string | null;
    lint: LintFinding[];
}

/**
 * @deprecated Use {@link LintFinding}. The phase 1a name of the SAPLint
 * finding; plan 1c's `Finding` is the diagnose finding, here
 * {@link DiagnoseFinding}, so the bare name says nothing any more. Nothing in
 * the app imports it.
 */
export type Finding = LintFinding;

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
    /** Only on an assistant message: the run's tool timeline and plan (`RunActivity.to_dict`). */
    activity?: { events?: ToolEventData[]; plan?: Todo[]; dropped?: number };
}

/** `GET /objects/search?target=&q=` */
export interface ObjectHit {
    type: string;
    name: string;
    package: string;
    description: string;
}

/** `GET|PUT /conventions/{target}` */
export interface Conventions {
    target: string;
    label?: string;
    destination?: string;
    namespace?: string;
    package?: string;
    atc_variant?: string;
    clean_core_level?: string;
    free_text?: string;
    /** Only a non-production target accepts diagnose sessions and trace approvals. */
    non_production?: boolean;
    updated_at?: string | null;
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
export type AdminSessionRow = SessionSummary;

// --- SSE stream (plan §1.3) ------------------------------------------------

export interface RunEventData { run_id: string; stage: Stage; message_id: string }
export interface TextEventData { delta: string }
/** One RunActivity event; start and end share `id`, so the UI upserts by it. */
export interface ToolEventData {
    ts: string;
    agent: string;
    kind: string;
    id: string;
    tool: string;
    detail: string;
    status: string;
    output?: string | null;
    /**
     * Why a call was refused (status `error`): `readonly_refused` by the
     * read-only guard, or a trace proposal that was not stored
     * (`too_many_pending`, `unknown_trace_request`, `target_not_non_production`,
     * `not_diagnose`, `invalid_request`, `proposal_failed`).
     */
    code?: string;
}
export interface Todo { content: string; status: string }
export interface PlanEventData { todos: Todo[] }
export interface FileEventData { path: string; state: FileState }
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
    | { type: "done"; data: DoneEventData };

export type SseEventType = SseEvent["type"];
