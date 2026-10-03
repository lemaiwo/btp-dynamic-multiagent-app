/**
 * Shapes of the /ide/api REST contract (plan §1.2) and of the SSE stream
 * that `POST .../messages` and `POST .../revise` answer with (plan §1.3).
 *
 * Types only: nothing here runs, so the module is erased by the transpiler.
 */

export type Stage = "chat" | "design" | "plan" | "propose" | "review" | "done";
export type SessionStatus = "idle" | "running";
export type ArtifactKind = "design" | "plan" | "note" | "review";
export type FileState = "read" | "modified" | "new";
export type MessageRole = "user" | "assistant" | "system";

/** `GET /me` */
export interface Me {
    principal: string;
    is_admin: boolean;
    /** The conventions keys the caller may open a session against. */
    targets: string[];
}

/** One row of `GET /sessions`. */
export interface SessionSummary {
    id: string;
    owner: string;
    title: string;
    target: string;
    stage: Stage;
    status: SessionStatus;
    created_at: string | null;
    updated_at: string | null;
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
export interface Finding {
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
    lint: Finding[];
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
    updated_at?: string | null;
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
    /** `readonly_refused` when the read-only guard refused the call (status `error`). */
    code?: string;
}
export interface Todo { content: string; status: string }
export interface PlanEventData { todos: Todo[] }
export interface FileEventData { path: string; state: FileState }
export interface ArtifactEventData { id: string; kind: ArtifactKind; version: number }
export interface UsageEventData { requests_used: number; request_cap: number }
export interface ErrorEventData { message: string; code?: string }
export interface DoneEventData { message_id: string; stage: Stage; status: SessionStatus }

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
    | { type: "done"; data: DoneEventData };

export type SseEventType = SseEvent["type"];
