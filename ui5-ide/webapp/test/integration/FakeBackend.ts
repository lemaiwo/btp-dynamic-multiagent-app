/**
 * An in-memory stand-in for /ide/api (plan §1.2), installed over
 * `window.fetch`.
 *
 * Deliberately at the network boundary, the pattern of ui5-admin's
 * FakeBackend: the real service class, its error mapping and every controller
 * run unchanged, so a journey is evidence of the wiring rather than of mocks.
 * Only `backend/*` URLs are intercepted; everything else (UI5's own resource
 * loading, Fragment.load included) goes through to the real fetch().
 *
 * The shapes below mirror the REST contract. They are local to the fake on
 * purpose until the app's own service types exist; a later task can switch
 * them to `com/agent/ide/service/types` without changing behaviour. The
 * phase 1c diagnose shapes (findings, approvals) are taken from there
 * already, type-only.
 *
 * Diagnose sessions (plan 1c): a target is diagnose-capable when its
 * conventions carry `non_production: true` (`allowDiagnose()`); such a
 * session stays in stage `investigate`, `report` streams a `report` artifact,
 * `handover` opens a change session seeded with it, and `scriptRun()` makes
 * the next run of a diagnose session emit `finding` and `approval_required`
 * frames. A diagnose session whose target lost the flag (`target_non_production`
 * false in the JSON, computed from the conventions as they are now) is refused with 409 `target_not_non_production` on runs,
 * reports, finding details and reads from SAP (`DIAGNOSE_BLOCKED`).
 *
 * Review contract (comments, revisions, pins; plan §1.1-1.3): every session
 * JSON also carries `target_non_production`, `pins`, `waiting`,
 * `open_comments` and `unresolved_comments`, computed as the server does.
 * Comments follow the server's state machine (`open` -> `sent` by a
 * request-changes start -> `addressed` by the run; whatever the run did not
 * resolve is `open` again when it ends, so `sent` exists only in flight; the
 * user dismisses or reopens). Every proposal a run writes is a numbered file revision with its
 * own syntax result (`scriptSyntax`: `ok`, `errors` with lines, or
 * `unavailable`, never shown as ok). `approve` pins the document version or
 * the file revisions it approves, refuses `open_comments` while a comment is
 * open or sent, and `version_changed` for a stale `version` or, in propose,
 * `revisions` that are not the latest of every proposed path. There is no `revise`
 * route (the server replaced it by `request-changes`) and no object search or
 * open (objects enter a session through the agent or a finding).
 *
 * Approvals are stored rows, as on the server (variant B: nothing waits for
 * a decision). What the decide route answers follows from the stored state:
 * a row past its `expiresAt` answers 410 and becomes `expired`, an approve
 * on a target that lost `non_production` answers 403 (a deny still
 * succeeds; the handover answers 409), `failArming` makes an approve
 * answer 200 with `status: "failed"`, `armingWithoutResult` an `approved`
 * with `result: null`. As on the server, a decision writes no chat message.
 */

import type {
    Approval, ApprovalAction, ApprovalStatus, ArtifactKind, BaseStatus, Comment, CommentState, DiagnoseFinding,
    Pins, SyntaxItem, SyntaxStatus, TraceParams, WaitingReason
} from "com/agent/ide/service/types";

export type Stage = "chat" | "design" | "plan" | "propose" | "review" | "done" | "investigate";
export type SessionType = "change" | "diagnose";
export type FileState = "read" | "modified" | "new";

export interface Session {
    id: string;
    title: string;
    target: string;
    type: SessionType;
    stage: Stage;
    status: "idle" | "running";
    owner: string;
    created_at: string;
    updated_at: string;
    /**
     * Test knob, never served: treats a diagnose session as if its target lost
     * the `non_production` flag (runs and live reads refused).
     */
    flagLost?: boolean;
    /** Model requests this session used (`SessionOut.requests_used`); every run adds one. Default 0. */
    requests_used?: number;
    /** The cap the session JSON reports (`SessionOut.request_cap`). Default 200. */
    request_cap?: number;
}

export interface Artifact {
    id: string;
    /** The stage that wrote it (`ArtifactSummaryOut.stage`); served from the kind when a journey seeds none. */
    stage?: Stage;
    kind: "design" | "plan" | "note" | "review" | "report";
    version: number;
    content: string;
    created_at: string;
    /** The pins the document was written against; `null`/absent when none apply. */
    based_on?: Record<string, number> | null;
}

/** One stored proposal of a file (`IdeFileRevision`), oldest first in `WorkspaceFile.revisions`. */
export interface FileRevisionRow {
    revision: number;
    proposed_source: string;
    run_id: string | null;
    created_at: string;
    syntax_status: SyntaxStatus | null;
    syntax: SyntaxItem[];
    checked_at: string | null;
}

export interface WorkspaceFile {
    path: string;
    state: FileState;
    object_type: string | null;
    object_name: string | null;
    origin_source: string;
    proposed_source: string;
    /** What `POST file/lint` answers (and `GET file` echoes once linted). */
    lint?: LintFinding[];
    linted?: boolean;
    /** The proposal's revisions, oldest first; none for a file that was only read. */
    revisions?: FileRevisionRow[];
    /** Overrides the computed base status (object with a source: `sap`, without: `absent`, scratch: `null`). */
    base_status?: BaseStatus | null;
    origin_version?: string | null;
}

export interface LintFinding { line: number; column: number; severity: string; message: string; rule: string }

export interface Message {
    id: string;
    role: "user" | "assistant" | "system";
    content: string;
    stage: Stage;
    created_at: string;
    activity?: { events: Record<string, unknown>[]; plan: { content: string; status: string }[] };
}

export interface Conventions {
    target: string;
    [key: string]: unknown;
}

/**
 * What `FakeBackend#failNext` accepts: the next call to `path` answers with
 * `body`/`status` instead; with `skip`, that many matching calls pass first.
 */
export interface FailNext { path: string; status: number; body: unknown; skip?: number }

/** The error bodies the real routes send when the user token or ARC-1 fails (plan §1.2, Task 12). */
export const USER_TOKEN_REQUIRED = {
    status: 424, body: { detail: "No user token was forwarded.", code: "user_token_required" }
};
/** The read-only guard's refusal (FIX-10): a 403 with its own code, not a missing role. */
export const READONLY_REFUSED = {
    status: 403, body: { detail: "Tool SAPWrite is not allowed in the read-only IDE.", code: "readonly_refused" }
};
/** Approving a trace on a target that lost its `non_production` flag (plan 1c §1.2); a deny is never refused for it. */
export const TARGET_NOT_NON_PRODUCTION = {
    status: 403, body: { detail: "The target is not flagged non-production.", code: "target_not_non_production" }
};
/**
 * A diagnose session whose target lost its `non_production` flag (session JSON
 * `target_non_production: false`): message and report runs, a finding's detail (the stored
 * text and a live read alike) and its source, and the file routes that read
 * SAP answer 409, as agents/ide/routes.py `_refuse_lost_flag` and the
 * runner's gate do. The session, its messages, artifacts, files and the
 * finding list, the approval list, a deny and delete keep working.
 */
export const DIAGNOSE_BLOCKED = {
    status: 409, body: { detail: "The target is no longer flagged non-production.", code: "target_not_non_production" }
};
/** Deciding an approval after its window closed. */
export const APPROVAL_EXPIRED = {
    status: 410, body: { detail: "The approval has expired.", code: "approval_expired" }
};

/** A stored approval to seed: the row's fields, plus when it stops being decidable. */
export interface ApprovalSeed extends Partial<Omit<Approval, "id">> {
    action?: ApprovalAction;
    status?: ApprovalStatus;
    /** ISO time; a pending row decided after it answers 410 and becomes `expired`. */
    expiresAt?: string;
}

/**
 * What `scriptRun` accepts: the next run of a diagnose session emits these
 * as `finding` / `approval_required` frames; `proposalRefused` adds the
 * `tool` event of a trace proposal the server did not store, with that code.
 * A change session's run emits none of them.
 */
export interface RunScript {
    findings?: (Pick<DiagnoseFinding, "kind" | "ref_id" | "title"> & Partial<DiagnoseFinding>)[];
    approval?: ApprovalSeed;
    proposalRefused?: string;
}

export const SAP_AUTH_FAILED = {
    status: 502, body: { detail: "SAP logon failed for the mapped user.", code: "sap_authentication_failed" }
};

interface SessionData {
    session: Session;
    messages: Message[];
    artifacts: Artifact[];
    files: WorkspaceFile[];
    /** Newest first, as `GET findings` answers. */
    findings: DiagnoseFinding[];
    /** Newest first, as `GET approvals` answers. */
    approvals: Approval[];
    /** Oldest first, as `GET comments` answers. */
    comments: Comment[];
    pins: Pins;
}

/** The document kind a stage writes (`stages.document_kind` of a change session). */
const DOC_KIND: Partial<Record<Stage, ArtifactKind>> = { design: "design", plan: "plan", propose: "note", review: "review" };
/** The stage whose document a kind is (the inverse of DOC_KIND, plus the diagnose report). */
const KIND_STAGE: Record<ArtifactKind, Stage> = {
    design: "design", plan: "plan", note: "propose", review: "review", report: "investigate"
};
/** The stages a request-changes run may rework (`stages.REVISABLE`). */
const REVISABLE: Stage[] = ["design", "plan", "propose", "review"];
const ARTIFACT_KINDS: ArtifactKind[] = ["design", "plan", "note", "review", "report"];
const COMMENT_STATES: CommentState[] = ["open", "sent", "addressed", "dismissed"];
/** The user's own comment transitions (contract §1.1); the others belong to runs. */
const USER_TRANSITIONS: Record<CommentState, CommentState[]> = {
    open: ["dismissed"], sent: [], addressed: ["open", "dismissed"], dismissed: ["open"]
};
const CLEARABLE = ["label", "destination", "namespace", "package", "atc_variant", "free_text"];
/** `schemas.ConventionsBody` max_length per text field (characters). */
const CONVENTIONS_MAX: Record<string, number> = {
    label: 120, destination: 200, namespace: 30, package: 30, atc_variant: 30, free_text: 20000
};
/** The server's `schemas.TARGET_PATTERN` (Task B7). */
const TARGET_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

/** What `scriptSyntax` sets up for a path: the verdict of every later check of it. */
interface SyntaxScript { status: SyntaxStatus; items: SyntaxItem[] }

/** The `trace_start` parameters a scripted approval asks for unless told otherwise. */
export const DEFAULT_TRACE_PARAMS: TraceParams = {
    processType: "http", objectType: "url", maxExecutions: 1, expiresHours: 1,
    sqlTrace: false, aggregate: true, description: "Trace the slow order call"
};

const NEXT_STAGE: Record<Stage, Stage | null> = {
    chat: "design", design: "plan", plan: "propose", propose: "review", review: "done", done: null,
    investigate: null
};

const NOW = "2026-10-03T10:00:00";

export default class FakeBackend {

    /**
     * Every route the fake serves, in the backend's `x-routes` key format
     * ("METHOD /path/{param}", below /ide/api). The dispatcher answers 404
     * for a call that matches none of them, so this table is exactly what
     * the fake serves; the contract test checks it against `x-routes`.
     */
    public static readonly ROUTES: readonly string[] = [
        "GET /me",
        "GET /sessions", "POST /sessions",
        "GET /conventions", "POST /conventions", "GET /conventions/{target}", "PUT /conventions/{target}",
        "GET /admin/sessions",
        "GET /sessions/{sid}", "PATCH /sessions/{sid}", "DELETE /sessions/{sid}",
        "GET /sessions/{sid}/messages", "POST /sessions/{sid}/messages", "GET /sessions/{sid}/messages/{mid}/activity",
        "POST /sessions/{sid}/request-changes", "POST /sessions/{sid}/report",
        "POST /sessions/{sid}/approve", "POST /sessions/{sid}/cancel", "POST /sessions/{sid}/handover",
        "GET /sessions/{sid}/artifacts", "GET /sessions/{sid}/artifacts/{aid}",
        "GET /sessions/{sid}/files", "GET /sessions/{sid}/file", "POST /sessions/{sid}/file/refresh",
        "POST /sessions/{sid}/file/lint", "GET /sessions/{sid}/file/revisions", "POST /sessions/{sid}/file/syntax",
        "GET /sessions/{sid}/findings", "GET /sessions/{sid}/findings/{fid}", "POST /sessions/{sid}/findings/{fid}/open",
        "GET /sessions/{sid}/approvals", "POST /sessions/{sid}/approvals/{aid}",
        "GET /sessions/{sid}/comments", "POST /sessions/{sid}/comments",
        "PATCH /sessions/{sid}/comments/{cid}", "DELETE /sessions/{sid}/comments/{cid}"
    ];

    /** The ROUTES entry a call ("GET", "sessions/s-1") matches, if any. */
    public static routeOf(method: string, path: string): string | undefined {
        return FakeBackend.ROUTES.find((route) => {
            const [m, template] = route.split(" ");
            const re = new RegExp(`^${template.slice(1).replace(/\{[^}]+\}/g, "[^/]+")}$`);
            return m === method && re.test(path);
        });
    }

    public principal = "developer@example.com";
    public isAdmin = false;
    /** `MeOut.diagnose_retention_days`: days a diagnose session is kept from creation; 0 = until deleted. */
    public diagnoseRetentionDays = 14;
    /** `ApprovalOut.ttl_min` (the server's `approval_ttl_min()`), unless a seeded row sets its own. */
    public approvalTtlMin = 60;
    public conventions: Conventions[] = [];
    public sessions: SessionData[] = [];
    /** Set to force the next matching call to fail. */
    public failNext?: FailNext;
    /** Every intercepted call as "METHOD path" (query string dropped), in order. */
    public requests: string[] = [];
    /** Every call answered so far, same format; a held call appears once it is released. */
    public responses: string[] = [];
    /** The JSON body of every intercepted call that sent one, in order. */
    public bodies: { key: string; body: unknown }[] = [];

    /** Run streams leave out the final `done` frame (the `stream_incomplete` case). */
    public omitDone = false;
    /** A run also streams a plan and two tool calls, and stores them as the answer's activity. */
    public withActivity = false;
    /** A run streams this `error` frame before its end (run_timeout, run_failed, ...). */
    public errorFrame?: { message: string; code?: string };
    /** The answer of the next message runs (default: one short sentence naming the stage). */
    public answer?: string;
    /** Run streams whose last frame was handed to the stream (or dropped by a cancelled reader). */
    public streamsFlushed = 0;
    /** An approve fails to arm: the decide route answers 200 with `status: "failed"` and this `error_code`. */
    public failArming?: string;
    /** An approve is stored as `approved` with `result: null` (the outcome is unknown). */
    public armingWithoutResult = false;
    /**
     * A request-changes run sends the comments but resolves none of them: at
     * its end they return to `open`, as on the server (`sent` exists only
     * while the run is in flight).
     */
    public resolveNoComments = false;
    /** The session used its model requests: request-changes answers 429 `usage_exhausted`. */
    public exhaustUsage = false;
    /**
     * The server's send cap (`store.MAX_SENT_COMMENTS`): a request-changes
     * run sends at most this many open comments (oldest first); the rest
     * stay open and the `sent` frame says how many in `left`. 0 = no cap.
     */
    public sendCap = 0;
    /**
     * Simulates the approuter's `csrfProtection`: a GET with `X-CSRF-Token:
     * Fetch` gets the token in the response header, and a non-GET without
     * it answers 403 `X-CSRF-Token: Required`. Off by default, as locally.
     */
    public csrf = false;
    /** Token fetches answered while `csrf` is on. */
    public csrfFetches = 0;
    /** The token the simulated approuter accepts; empty until fetched or after expireCsrfToken(). */
    private csrfToken = "";
    private csrfIssued = 0;
    /** refuseCsrf(): changes still to answer 403 Required whatever token they carry. */
    private csrfRefusals = 0;
    /** Scripted syntax verdicts by path; a path without one checks `ok`. */
    private syntaxScripts = new Map<string, SyntaxScript>();
    /** When each stored approval stops being decidable (ISO time), by approval id. */
    private approvalExpiry = new Map<string, string>();

    private holds: { key: string; gate: Promise<void> }[] = [];
    /** Set by holdResponse(): answered at once, delivered only when released. */
    private lateHolds: { key: string; gate: Promise<void> }[] = [];
    /** Set by pauseStream(): the next run stream stops after its first text delta until released. */
    private streamPause?: Promise<void>;
    /** Set by splitFiles(): a propose run proposes every file and stops after the first `file` frame. */
    private filesGap?: Promise<void>;
    /** The run that is paused right now, so `cancel` can stop it. */
    private pausedRun?: { data: SessionData; reply: Message; firstChunk: string; cancel: () => void };
    /** Set by scriptRun(): what the next message run emits besides its answer. */
    private runScript?: RunScript;

    private originalFetch?: typeof fetch;
    private nextId = 1;
    /** Seconds after NOW of the last stamp(): rows a run writes get increasing times. */
    private clock = 0;

    public install(): void {
        this.originalFetch = window.fetch;
        const original = this.originalFetch;
        window.fetch = ((input: string, init?: RequestInit) => {
            if (/^backend\//.test(String(input))) {
                return FakeBackend.abortable(this.handle(String(input), init), init?.signal);
            }
            return original(input, init);
        }) as unknown as typeof fetch;
    }

    /** As a real fetch: an abort rejects the pending call with an AbortError. */
    private static abortable(response: Promise<Response>, signal?: AbortSignal | null): Promise<Response> {
        if (!signal) {
            return response;
        }
        const abortError = (): DOMException => new DOMException("The operation was aborted.", "AbortError");
        if (signal.aborted) {
            return Promise.reject(abortError());
        }
        return new Promise<Response>((resolve, reject) => {
            signal.addEventListener("abort", () => reject(abortError()), { once: true });
            response.then(resolve, reject);
        });
    }

    /**
     * Pauses the next run stream after its first text delta, with the
     * session `running`, until the returned function runs (or the run is
     * cancelled). For Stop and in-flight journeys.
     */
    public pauseStream(): () => void {
        let release!: () => void;
        this.streamPause = new Promise<void>((resolve) => { release = resolve; });
        return release;
    }

    /**
     * The next propose run writes a proposal into every workspace file and
     * sends one `file` frame per file, pausing after the first until the
     * returned function runs (a later `file` event during the tree reload).
     */
    public splitFiles(): () => void {
        let release!: () => void;
        this.filesGap = new Promise<void>((resolve) => { release = resolve; });
        return release;
    }

    public restore(): void {
        if (this.originalFetch) {
            window.fetch = this.originalFetch;
        }
    }

    /**
     * Holds the next "METHOD path" call until the returned function runs,
     * so a journey can act while a request is in flight (race tests).
     */
    public hold(key: string): () => void {
        let release!: () => void;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        this.holds.push({ key, gate });
        return release;
    }

    /**
     * The next "METHOD path" call is answered from the state at request time,
     * but the answer reaches the app only when the returned function runs
     * (a slow network: a read that left before a local change arrives after it).
     */
    public holdResponse(key: string): () => void {
        let release!: () => void;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        this.lateHolds.push({ key, gate });
        return release;
    }

    /** Adds a session owned by the caller; it becomes the newest one. */
    public addSession(
        title: string, files: WorkspaceFile[] = [], artifacts: Artifact[] = [], type: SessionType = "change"
    ): Session {
        const session: Session = {
            id: this.id("s"), title, target: "dev-system", type,
            stage: type === "diagnose" ? "investigate" : "chat", status: "idle",
            owner: this.principal, created_at: NOW, updated_at: NOW
        };
        this.sessions.push(FakeBackend.newData(session, artifacts, files));
        return session;
    }

    private static newData(session: Session, artifacts: Artifact[] = [], files: WorkspaceFile[] = []): SessionData {
        return { session, messages: [], artifacts, files, findings: [], approvals: [], comments: [], pins: {} };
    }

    /**
     * Stores the next version of `kind` for `sid`, newest first, with
     * `based_on` from the session's pins as `submit_document` sets it.
     */
    public addArtifact(sid: string, kind: ArtifactKind, content?: string): Artifact {
        const data = this.mustData(sid);
        return this.storeArtifact(data, kind, content);
    }

    /**
     * Stores `source` as the next revision of the workspace file `path`
     * (created as a new object file when missing) and answers its number.
     */
    public addRevision(sid: string, path: string, source: string): number {
        const data = this.mustData(sid);
        let file = data.files.find((f) => f.path === path);
        if (!file) {
            const m = /^src\/([A-Z]+)\/([^.]+)\./.exec(path);
            file = {
                path, state: "new", object_type: m ? m[1] : null, object_name: m ? m[2].toUpperCase() : null,
                origin_source: "", proposed_source: ""
            };
            data.files.push(file);
        }
        return this.propose(file, source, null).revision;
    }

    /** Sets a comment's state directly, as a run does (`sent`, `addressed`). */
    public setCommentState(sid: string, cid: string, state: CommentState, answer?: string): void {
        const comment = this.mustData(sid).comments.find((c) => c.id === cid);
        if (!comment) {
            throw new Error(`No comment ${cid}`);
        }
        comment.state = state;
        comment.answer = state === "addressed" ? (answer ?? comment.answer ?? "Addressed by the fake.") : comment.answer;
        comment.updated_at = NOW;
    }

    /** Every later syntax check of `path` (route and run end) answers this verdict. */
    public scriptSyntax(path: string, status: SyntaxStatus, items: SyntaxItem[] = []): void {
        this.syntaxScripts.set(path, { status, items });
    }

    private mustData(sid: string): SessionData {
        const data = this.dataOf(sid);
        if (!data) {
            throw new Error(`No session ${sid}`);
        }
        return data;
    }

    private storeArtifact(data: SessionData, kind: ArtifactKind, content?: string): Artifact {
        const version = data.artifacts.filter((a) => a.kind === kind).length + 1;
        const basedOnKind = ({ plan: "design", review: "plan", note: "plan" } as Partial<Record<ArtifactKind, "design" | "plan">>)[kind];
        const pin = basedOnKind ? data.pins[basedOnKind] : undefined;
        const artifact: Artifact = {
            id: this.id("a"), stage: KIND_STAGE[kind], kind, version, created_at: NOW,
            content: content ?? `# ${kind} v${version}\n\nWritten by the fake.`,
            based_on: basedOnKind && pin !== undefined ? { [basedOnKind]: pin } : null
        };
        data.artifacts.unshift(artifact);
        return artifact;
    }

    /** A new revision of `file` holding `source`, unchecked. */
    private propose(file: WorkspaceFile, source: string, runId: string | null): FileRevisionRow {
        file.revisions ??= [];
        const row: FileRevisionRow = {
            revision: file.revisions.length + 1, proposed_source: source, run_id: runId, created_at: NOW,
            syntax_status: null, syntax: [], checked_at: null
        };
        file.revisions.push(row);
        file.proposed_source = source;
        file.state = file.origin_source ? "modified" : "new";
        return row;
    }

    /** The syntax dry run on `row`: the scripted verdict (default ok), stored on the revision. */
    private checkSyntax(file: WorkspaceFile, row: FileRevisionRow): void {
        const script = this.syntaxScripts.get(file.path) ?? { status: "ok", items: [] };
        row.syntax_status = script.status;
        // An unavailable check never carries messages: there is nothing it could vouch for.
        row.syntax = script.status === "unavailable" ? [] : script.items.map((i) => ({ ...i }));
        row.checked_at = NOW;
    }

    private static latest(file: WorkspaceFile): FileRevisionRow | undefined {
        return file.revisions?.[file.revisions.length - 1];
    }

    private static baseStatus(file: WorkspaceFile): BaseStatus | null {
        if (file.base_status !== undefined) {
            return file.base_status;
        }
        if (!file.object_type) {
            return null;
        }
        return file.origin_source ? "sap" : "absent";
    }

    /** Flags `target`'s conventions `non_production`, so `/me` offers it for diagnose sessions. */
    public allowDiagnose(target = "dev-system"): void {
        const conv = this.conventions.find((c) => c.target === target);
        if (conv) {
            conv.non_production = true;
        } else {
            this.conventions.push({ target, non_production: true });
        }
    }

    /**
     * Stores an approval for `sid` (pending unless the seed says otherwise),
     * newest first by `created_at` as the list route answers.
     */
    public addApproval(sid: string, seed: ApprovalSeed = {}): Approval {
        const data = this.dataOf(sid);
        if (!data) {
            throw new Error(`No session ${sid}`);
        }
        return this.storeApproval(data, seed);
    }

    private storeApproval(data: SessionData, seed: ApprovalSeed): Approval {
        const { expiresAt, ...fields } = seed;
        const action = fields.action ?? "trace_start";
        const approval: Approval = {
            status: "pending", created_at: NOW, decided_at: null, result: null, error_code: null,
            ...fields,
            id: this.id("ap"), action,
            params: fields.params ?? (action === "trace_start" ? { ...DEFAULT_TRACE_PARAMS } : { id: "TRC-1" })
        };
        if (expiresAt) {
            this.approvalExpiry.set(approval.id, expiresAt);
        }
        data.approvals.push(approval);
        data.approvals.sort((a, b) => (a.created_at === b.created_at ? 0 : (a.created_at ?? "") < (b.created_at ?? "") ? 1 : -1));
        return approval;
    }

    /**
     * The next message run also emits one `finding` frame per entry of
     * `findings` (upserted by kind + ref_id, as the server does) and, with
     * `approval`, one `approval_required` frame for a new pending approval.
     */
    public scriptRun(script: RunScript): void {
        this.runScript = script;
    }

    /** The approuter session lost its token: the next change answers 403 Required until a new fetch. */
    public expireCsrfToken(): void {
        this.csrfToken = "";
    }

    /** The approuter refuses the next `count` changes (403 Required) even with a fresh token: the csrf_failed case. */
    public refuseCsrf(count: number): void {
        this.csrfRefusals = count;
    }

    /** Findings and approvals of a session, for journeys to inspect or seed. */
    public dataOf(sid: string): SessionData | undefined {
        return this.sessions.find((d) => d.session.id === sid);
    }

    public reset(): void {
        this.clock = 0;
        this.nextId = 1;
        this.requests = [];
        this.responses = [];
        this.bodies = [];
        this.holds = [];
        this.lateHolds = [];
        this.failNext = undefined;
        this.isAdmin = false;
        this.diagnoseRetentionDays = 14;
        this.approvalTtlMin = 60;
        this.omitDone = false;
        this.withActivity = false;
        this.errorFrame = undefined;
        this.answer = undefined;
        this.streamsFlushed = 0;
        this.filesGap = undefined;
        this.streamPause = undefined;
        this.pausedRun = undefined;
        this.runScript = undefined;
        this.failArming = undefined;
        this.armingWithoutResult = false;
        this.resolveNoComments = false;
        this.exhaustUsage = false;
        this.sendCap = 0;
        this.csrf = false;
        this.csrfFetches = 0;
        this.csrfToken = "";
        this.csrfRefusals = 0;
        this.syntaxScripts.clear();
        this.approvalExpiry.clear();
        this.conventions = [
            { target: "dev-system", naming: { prefix: "Z" }, package: "ZLOCAL" }
        ];
        const sid = this.id("s");
        this.sessions = [{
            session: {
                id: sid, title: "Explain the order class", target: "dev-system", type: "change",
                stage: "chat", status: "idle", owner: this.principal,
                created_at: NOW, updated_at: NOW
            },
            messages: [],
            artifacts: [],
            files: [{
                path: "src/CLAS/zcl_demo.clas.abap", state: "read",
                object_type: "CLAS", object_name: "ZCL_DEMO",
                origin_source: "CLASS zcl_demo DEFINITION PUBLIC.\nENDCLASS.\nCLASS zcl_demo IMPLEMENTATION.\nENDCLASS.",
                proposed_source: ""
            }],
            findings: [],
            approvals: [],
            comments: [],
            pins: {}
        }];
    }

    /**
     * A time after every earlier stamp, in the API's format (UTC, no zone):
     * a run's question, the approvals it proposes and its answer are stored in
     * that order, as on the server (the answer is written when the run ends).
     */
    private stamp(): string {
        this.clock++;
        return new Date(Date.parse(`${NOW}Z`) + this.clock * 1000).toISOString().replace(/\.\d{3}Z$/, "");
    }

    private id(prefix: string): string {
        return `${prefix}-${this.nextId++}`;
    }

    private json(status: number, body?: unknown): Response {
        return new Response(
            status === 204 || body === undefined ? null : JSON.stringify(body),
            { status, headers: { "Content-Type": "application/json" } }
        );
    }

    /**
     * An event-stream response. Frames are enqueued one by one; with a
     * `pauseAfter` gate the stream stops after that many frames until the
     * gate resolves, then sends the rest (`rest()` is read only then, so a
     * cancel meanwhile can change what follows).
     */
    private sse(
        frames: [string, unknown][], pause?: { after: number; gate: Promise<void>; rest: () => [string, unknown][] }
    ): Response {
        const encode = (frame: [string, unknown]): Uint8Array =>
            new TextEncoder().encode(`event: ${frame[0]}\ndata: ${JSON.stringify(frame[1])}\n\n`);
        const flushed = (): void => { this.streamsFlushed++; };
        const body = new ReadableStream<Uint8Array>({
            async start(controller) {
                const head = pause ? frames.slice(0, pause.after) : frames;
                head.forEach((f) => controller.enqueue(encode(f)));
                if (pause) {
                    await pause.gate;
                }
                try {
                    (pause ? pause.rest() : []).forEach((f) => controller.enqueue(encode(f)));
                    controller.close();
                } catch {
                    // The reader cancelled meanwhile (Stop aborts the stream).
                } finally {
                    flushed();
                }
            }
        });
        return new Response(body, {
            status: 200,
            headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" }
        });
    }

    /** `FileSummaryOut`: revision, base and syntax status of the latest revision. */
    private fileSummary(f: WorkspaceFile): Record<string, unknown> {
        const latest = FakeBackend.latest(f);
        return {
            path: f.path, state: f.state, object_type: f.object_type, object_name: f.object_name,
            revision: latest?.revision ?? 0, base_status: FakeBackend.baseStatus(f),
            syntax_status: latest?.syntax_status ?? null
        };
    }

    private gateError(code: string, message: string): Response {
        return this.json(code === "usage_exhausted" ? 429 : 409, { detail: message, code });
    }

    private async handle(url: string, init?: RequestInit): Promise<Response> {
        const method = (init?.method || "GET").toUpperCase();
        const [rawPath, query = ""] = url.replace(/^backend\//, "").split("?");
        const path = rawPath.replace(/\/$/, "");
        const params = new URLSearchParams(query);
        const key = `${method} ${path}`;
        this.requests.push(key);
        if (typeof init?.body === "string") {
            try {
                this.bodies.push({ key, body: JSON.parse(init.body) });
            } catch {
                this.bodies.push({ key, body: init.body });
            }
        }
        const headers = new Headers(init?.headers);
        const csrfHeader = headers.get("X-CSRF-Token");
        const wantsToken = this.csrf && (method === "GET" || method === "HEAD") && csrfHeader?.toLowerCase() === "fetch";
        const refused = this.csrfRefusals > 0 && method !== "GET" && method !== "HEAD";
        if (refused) {
            this.csrfRefusals--;
        }
        if (refused || (this.csrf && method !== "GET" && method !== "HEAD" && (!this.csrfToken || csrfHeader !== this.csrfToken))) {
            // The approuter refuses before the backend sees the call.
            this.responses.push(key);
            return new Response("Forbidden", { status: 403, headers: { "Content-Type": "text/plain", "X-CSRF-Token": "Required" } });
        }
        const held = this.holds.findIndex((h) => h.key === key);
        if (held >= 0) {
            const { gate } = this.holds.splice(held, 1)[0];
            await gate;
        }
        const response = await this.route(method, path, params, init);
        const late = this.lateHolds.findIndex((h) => h.key === key);
        if (late >= 0) {
            const { gate } = this.lateHolds.splice(late, 1)[0];
            await gate;
        }
        if (wantsToken) {
            if (!this.csrfToken) {
                this.csrfToken = `fake-csrf-${++this.csrfIssued}`;
            }
            this.csrfFetches++;
            response.headers.set("X-CSRF-Token", this.csrfToken);
        }
        this.responses.push(key);
        return response;
    }

    private async route(method: string, path: string, params: URLSearchParams, init?: RequestInit): Promise<Response> {
        const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};

        if (this.failNext && this.failNext.path === path && this.failNext.skip) {
            this.failNext.skip--;
        } else if (this.failNext && this.failNext.path === path) {
            const fail = this.failNext;
            this.failNext = undefined;
            return this.json(fail.status, fail.body);
        }
        if (!FakeBackend.routeOf(method, path)) {
            return this.json(404, { detail: `No fake for ${method} ${path}` });
        }

        if (method === "GET" && path === "me") {
            return this.json(200, {
                principal: this.principal, is_admin: this.isAdmin,
                targets: this.conventions.map((c) => c.target),
                diagnose_targets: this.conventions.filter((c) => c.non_production === true).map((c) => c.target),
                diagnose_retention_days: this.diagnoseRetentionDays
            });
        }
        if (path === "sessions") {
            if (method === "GET") {
                return this.json(200, this.sessions.map((d) => this.sessionJson(d.session)).slice().reverse());
            }
            if (method === "POST") {
                const target = String(body.target || "");
                const conv = this.conventions.find((c) => c.target === target);
                if (!conv) {
                    return this.json(422, { detail: `Unknown target '${target}'` });
                }
                const type = String(body.type ?? "change");
                if (type !== "change" && type !== "diagnose") {
                    return this.json(422, { detail: [{ loc: ["body", "type"], msg: "Input should be 'change' or 'diagnose'" }] });
                }
                if (type === "diagnose" && conv.non_production !== true) {
                    return this.json(422, {
                        detail: `Target '${target}' is not flagged non-production.`, code: "target_not_non_production"
                    });
                }
                const session: Session = {
                    id: this.id("s"), title: String(body.title || ""), target, type,
                    stage: type === "diagnose" ? "investigate" : "chat", status: "idle", owner: this.principal,
                    created_at: NOW, updated_at: NOW
                };
                this.sessions.push(FakeBackend.newData(session));
                return this.json(201, this.sessionJson(session));
            }
        }
        if (path === "conventions" && method === "GET") {
            return this.json(200, this.conventions.map(FakeBackend.conventionsJson));
        }
        if (path === "conventions" && method === "POST") {
            return this.createConventions(body);
        }
        const conv = /^conventions\/([^/]+)$/.exec(path);
        if (conv) {
            const target = decodeURIComponent(conv[1]);
            if (method === "GET") {
                const found = this.conventions.find((c) => c.target === target);
                return found ? this.json(200, FakeBackend.conventionsJson(found)) : this.json(404, { detail: "Not found" });
            }
            if (method === "PUT") {
                return this.putConventions(target, body);
            }
        }
        if (method === "GET" && path === "admin/sessions") {
            if (!this.isAdmin) {
                return this.json(403, { detail: "Admin scope required" });
            }
            // AdminSessionRowOut: metadata only, never content or computed state.
            return this.json(200, this.sessions.map(({ session: s }) => ({
                id: s.id, owner: s.owner, title: s.title, target: s.target, type: s.type,
                stage: s.stage, status: s.status, created_at: s.created_at, updated_at: s.updated_at
            })));
        }

        const m = /^sessions\/([^/]+)(?:\/(.+))?$/.exec(path);
        if (m) {
            const data = this.sessions.find((d) => d.session.id === m[1] && d.session.owner === this.principal);
            if (!data) {
                // Not the caller's session answers 404, never 403 (no existence leak).
                return this.json(404, { detail: "Session not found" });
            }
            return this.handleSession(method, m[2] || "", data, body, params);
        }
        return this.json(404, { detail: `No fake for ${method} ${path}` });
    }

    private handleSession(
        method: string, sub: string, data: SessionData,
        body: Record<string, unknown>, params: URLSearchParams
    ): Response {
        const s = data.session;
        if (sub === "") {
            if (method === "GET") {
                return this.json(200, {
                    ...this.sessionJson(s),
                    artifacts: data.artifacts.map(({ id, stage, kind, version, created_at, based_on }) =>
                        ({ id, stage: stage ?? KIND_STAGE[kind], kind, version, created_at, based_on: based_on ?? null })),
                    files: data.files.map((f) => this.fileSummary(f))
                });
            }
            if (method === "PATCH") {
                s.title = String(body.title ?? s.title);
                return this.json(200, this.sessionJson(s));
            }
            if (method === "DELETE") {
                this.sessions = this.sessions.filter((d) => d !== data);
                return this.json(204);
            }
        }
        if (sub === "messages" && method === "GET") {
            // The activity is served per message (`.../activity`), never inline.
            return this.json(200, data.messages.map(({ activity, ...m }) => ({ ...m, has_activity: !!activity })));
        }
        const act = /^messages\/([^/]+)\/activity$/.exec(sub);
        if (act && method === "GET") {
            const message = data.messages.find((m) => m.id === decodeURIComponent(act[1]));
            if (!message) {
                return this.json(404, { detail: "Message not found" });
            }
            if (!message.activity) {
                return this.json(404, { detail: "The message has no activity.", code: "no_activity" });
            }
            return this.json(200, { events: message.activity.events, plan: message.activity.plan, dropped: 0 });
        }
        if (sub === "request-changes" && method === "POST") {
            return this.requestChanges(data, body);
        }
        const comments = this.handleComments(method, sub, data, body, params);
        if (comments) {
            return comments;
        }
        if (sub === "messages" && method === "POST") {
            if (this.blocked(s)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (s.status === "running") {
                return this.gateError("run_in_progress", "A run is already in progress.");
            }
            if (s.stage === "done") {
                return this.gateError("stage_done", "The session is done.");
            }
            const userMsg: Message = {
                id: this.id("m"), role: "user", stage: s.stage, created_at: this.stamp(),
                content: String(body.text)
            };
            const answer = this.answer ?? `Fake answer in stage **${s.stage}**.`;
            const reply: Message = {
                id: this.id("m"), role: "assistant", stage: s.stage, created_at: NOW, content: answer
            };
            data.messages.push(userMsg, reply);
            const toolBase = { ts: NOW, agent: "abap", kind: "tool", tool: "SAPRead", detail: "ZCL_DEMO" };
            const plan = [{ content: "Read the class", status: "completed" }, { content: "Write the design", status: "in_progress" }];
            const read = { ...toolBase, id: "call-1", status: "ok", output: "CLASS zcl_demo DEFINITION PUBLIC." };
            // Streamed in several deltas, as the model does.
            const chunks = answer.match(/.{1,8}/g) ?? [answer];
            if (this.withActivity) {
                reply.activity = { events: [read], plan };
            }
            const runId = this.id("r");
            const frames: [string, unknown][] = [
                ["run", { run_id: runId, stage: s.stage, message_id: userMsg.id }],
                ...(this.withActivity ? [
                    ["plan", { todos: plan }] as [string, unknown],
                    ["tool", { ...read, status: "running", output: "" }] as [string, unknown],
                    ["tool", read] as [string, unknown]
                ] : []),
                ...chunks.map((delta): [string, unknown] => ["text", { delta }]),
                ...this.scriptedFrames(data)
            ];
            reply.created_at = this.stamp();
            const kind = ({ design: "design", plan: "plan", propose: "note", review: "review" } as const)[
                s.stage as "design" | "plan" | "propose" | "review"
            ];
            if (kind) {
                const artifact = this.storeArtifact(data, kind);
                frames.push(["artifact", { id: artifact.id, kind, version: artifact.version }]);
            }
            const firstFile = frames.length;
            if (s.stage === "propose" && data.files.length) {
                // A proposal for the first workspace file (every file after
                // splitFiles()), as a propose run writes them.
                frames.push(...this.proposeFrames(this.filesGap ? data.files : data.files.slice(0, 1), runId));
            }
            if (this.errorFrame) {
                frames.push(["error", this.errorFrame]);
            }
            frames.push(["usage", this.useRequest(s)]);
            const done = (): [string, unknown] => ["done", { message_id: reply.id, stage: s.stage, status: "idle" }];
            if (this.filesGap && frames[firstFile]?.[0] === "file") {
                const gate = this.filesGap;
                this.filesGap = undefined;
                return this.sse(frames, {
                    after: firstFile + 1, gate, rest: () => [...frames.slice(firstFile + 1), done()]
                });
            }
            if (!this.streamPause) {
                if (!this.omitDone) {
                    frames.push(done());
                }
                return this.sse(frames);
            }
            // Paused: run + first delta now, the rest on release; a cancel
            // ends it with the partial answer saved as "(cancelled)".
            let resume!: () => void;
            const gate = new Promise<void>((r) => { resume = r; });
            void this.streamPause.then(() => resume());
            this.streamPause = undefined;
            s.status = "running";
            let cancelled = false;
            this.pausedRun = { data, reply, firstChunk: chunks[0], cancel: () => { cancelled = true; resume(); } };
            return this.sse(frames, {
                after: 2,
                gate,
                rest: () => {
                    s.status = "idle";
                    this.pausedRun = undefined;
                    if (cancelled) {
                        return [done()];
                    }
                    return this.omitDone ? frames.slice(2) : [...frames.slice(2), done()];
                }
            });
        }
        if (sub === "approve" && method === "POST") {
            if (body.version !== undefined && body.version !== null && !Number.isInteger(body.version)) {
                // Request validation (pydantic) comes before every gate.
                return this.json(422, { detail: [{ loc: ["body", "version"], msg: "Input should be a valid integer" }] });
            }
            // The refusals in `stages.approve`'s order: diagnose, revisions outside propose (stage_changed),
            // done, running, comments, then the stage's own rule (no_proposals / version_changed in propose,
            // missing_artifact / version_changed for a document stage).
            if (s.type === "diagnose") {
                return this.gateError("approve_not_allowed", "Diagnose sessions have no stages to approve.");
            }
            const revisions = body.revisions === null ? undefined : body.revisions;
            if (revisions !== undefined && s.stage !== "propose") {
                // `revisions` belong to propose only (backend cbd4da4).
                return this.gateError("stage_changed", "The session is no longer in the propose stage; reload and try again.");
            }
            const next = NEXT_STAGE[s.stage];
            if (!next) {
                return this.gateError("stage_done", "This session is done; start a new session.");
            }
            if (s.status === "running") {
                return this.gateError("run_in_progress", "A run is in progress for this session.");
            }
            if (data.comments.some((c) => c.state === "open" || c.state === "sent")) {
                return this.gateError("open_comments", "Resolve or dismiss the open review comments first.");
            }
            if (s.stage === "propose") {
                if (!data.files.some((f) => FakeBackend.isProposedObject(f))) {
                    return this.gateError("no_proposals", "Propose at least one new or modified workspace file first.");
                }
                const proposed = this.proposedRevisions(data);
                if (revisions !== undefined && !this.sameRevisions(data, revisions)) {
                    // What the user saw is not what would be pinned (contract fix round U7).
                    return this.gateError("version_changed", "The proposals changed since they were shown. Review them and approve again.");
                }
                data.pins.files = proposed;
            } else {
                const need = ({ design: "design", plan: "plan", review: "review" } as Partial<Record<Stage, "design" | "plan" | "review">>)[s.stage];
                if (need) {
                    const latest = this.latestVersion(data, need);
                    if (latest < 1) {
                        return this.gateError("missing_artifact", `Submit a ${need} in the ${s.stage} stage first.`);
                    }
                    if (body.version !== undefined && body.version !== null && body.version !== latest) {
                        return this.gateError("version_changed", `The ${need} changed: version ${latest} is the latest. Review it and approve again.`);
                    }
                    data.pins[need] = latest;
                }
            }
            s.stage = next;
            return this.json(200, this.sessionJson(s));
        }
        if (sub === "cancel" && method === "POST") {
            const run = this.pausedRun;
            if (run && run.data === data) {
                run.reply.content = `${run.firstChunk}\n\n(cancelled)`;
                run.cancel();
            }
            s.status = "idle";
            return this.json(200, this.sessionJson(s));
        }
        if (sub === "artifacts" && method === "GET") {
            const kind = params.get("kind");
            return this.json(200, data.artifacts.filter((a) => !kind || a.kind === kind).map(FakeBackend.artifactJson));
        }
        const art = /^artifacts\/([^/]+)$/.exec(sub);
        if (art && method === "GET") {
            const found = data.artifacts.find((a) => a.id === art[1]);
            return found ? this.json(200, FakeBackend.artifactJson(found)) : this.json(404, { detail: "Artifact not found" });
        }
        if (sub === "files" && method === "GET") {
            return this.json(200, data.files.map((f) => this.fileSummary(f)));
        }
        if (sub === "file" || sub === "file/refresh" || sub === "file/lint" || sub === "file/revisions" || sub === "file/syntax") {
            const file = data.files.find((f) => f.path === params.get("path"));
            if (!file) {
                return this.json(404, { detail: "File not found" });
            }
            if (sub !== "file" && sub !== "file/revisions" && this.blocked(s)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (sub === "file/lint" && method === "POST") {
                file.linted = true;
                return this.json(200, file.lint ?? []);
            }
            if (sub === "file/revisions" && method === "GET") {
                return this.json(200, (file.revisions ?? []).slice().reverse().map((r) => ({
                    revision: r.revision, run_id: r.run_id, created_at: r.created_at,
                    chars: r.proposed_source.length, syntax_status: r.syntax_status
                })));
            }
            const wanted = params.get("revision");
            const row = wanted === null ? FakeBackend.latest(file) : file.revisions?.find((r) => String(r.revision) === wanted);
            if (wanted !== null && !row) {
                return this.json(404, { detail: `No revision ${wanted} of ${file.path}.`, code: "unknown_revision" });
            }
            if (sub === "file/syntax" && method === "POST") {
                if (s.status === "running") {
                    return this.gateError("run_in_progress", "A run is already in progress.");
                }
                if (!file.object_type) {
                    return this.json(422, { detail: "Only an ABAP object has a syntax check.", code: "not_an_object" });
                }
                if (!row) {
                    return this.json(422, { detail: "There is no proposal to check.", code: "no_proposal" });
                }
                this.checkSyntax(file, row);
                return this.json(200, {
                    path: file.path, revision: row.revision, status: row.syntax_status, items: row.syntax, checked_at: row.checked_at
                });
            }
            return this.json(200, {
                ...this.fileSummary(file),
                revision: row?.revision ?? 0,
                syntax_status: row?.syntax_status ?? null,
                origin_source: file.origin_source,
                proposed_source: row ? row.proposed_source : file.proposed_source,
                origin_version: file.origin_version ?? null,
                lint: file.linted ? file.lint ?? [] : [],
                syntax: row?.syntax ?? []
            });
        }
        const diagnose = this.handleDiagnose(method, sub, data, body, params);
        if (diagnose) {
            return diagnose;
        }
        return this.json(404, { detail: `No fake for ${method} sessions/${s.id}/${sub}` });
    }

    /** The `finding` and `approval_required` frames scriptRun() asked for, applied to `data`. */
    private scriptedFrames(data: SessionData): [string, unknown][] {
        const script = this.runScript;
        this.runScript = undefined;
        // The server emits these for diagnose sessions only; a change session's run never proposes a trace.
        if (!script || data.session.type !== "diagnose") {
            return [];
        }
        const frames: [string, unknown][] = [];
        for (const f of script.findings ?? []) {
            let finding = data.findings.find((x) => x.kind === f.kind && x.ref_id === f.ref_id);
            if (finding) {
                Object.assign(finding, f, { id: finding.id });
            } else {
                finding = {
                    program: null, include: null, line: null, occurred_at: null, created_at: NOW,
                    ...f, id: this.id("f")
                };
                data.findings.unshift(finding);
            }
            frames.push(["finding", { ...finding }]);
        }
        if (script.approval) {
            const approval = this.storeApproval(data, { created_at: this.stamp(), ...script.approval });
            // The frame of a new proposal is always pending, whatever becomes of the stored row.
            frames.push(["approval_required", { ...this.approvalJson(approval), status: "pending" }]);
        }
        if (script.proposalRefused) {
            // A proposal that was not stored: the tool call ends in error with the refusal code.
            frames.push(["tool", {
                ts: NOW, agent: "abap-diagnostics", kind: "tool", id: this.id("call"), tool: "SAPDiagnose",
                detail: "trace_start", status: "error", code: script.proposalRefused,
                output: `Proposal refused (${script.proposalRefused}). Nothing was armed or cancelled.`
            }]);
        }
        return frames;
    }

    /** The phase 1c routes: report, handover, findings, approvals. `undefined` when none matches. */
    private handleDiagnose(
        method: string, sub: string, data: SessionData, body: Record<string, unknown>, params = new URLSearchParams()
    ): Response | undefined {
        const s = data.session;
        const notDiagnose = (): Response => this.gateError("not_diagnose", "Only a diagnose session can do this.");

        if (sub === "report" && method === "POST") {
            if (s.type !== "diagnose") {
                return notDiagnose();
            }
            if (this.blocked(s)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (s.status === "running") {
                return this.gateError("run_in_progress", "A run is already in progress.");
            }
            const version = data.artifacts.filter((a) => a.kind === "report").length + 1;
            const artifact: Artifact = {
                id: this.id("a"), stage: "investigate", kind: "report", version, created_at: NOW,
                content: `# Diagnose report v${version}\n\nRoot cause found by the fake.`
            };
            data.artifacts.unshift(artifact);
            const reply: Message = {
                id: this.id("m"), role: "assistant", stage: s.stage, created_at: NOW, content: "Report written."
            };
            data.messages.push(reply);
            return this.sse([
                ["run", { run_id: this.id("r"), stage: s.stage, message_id: reply.id }],
                ["text", { delta: reply.content }],
                ["artifact", { id: artifact.id, kind: "report", version }],
                ["usage", this.useRequest(s)],
                ["done", { message_id: reply.id, stage: s.stage, status: "idle" }]
            ]);
        }
        if (sub === "handover" && method === "POST") {
            if (s.type !== "diagnose") {
                return notDiagnose();
            }
            // The server's gate order: not_diagnose, target_not_non_production, run_in_progress, missing_artifact.
            if (!this.nonProduction(s.target)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (s.status === "running") {
                return this.gateError("run_in_progress", "A run is already in progress.");
            }
            const report = data.artifacts.find((a) => a.kind === "report");
            if (!report) {
                return this.gateError("missing_artifact", "Handover needs a report artifact.");
            }
            const session: Session = {
                id: this.id("s"), title: `Change: ${s.title}`.slice(0, 200), target: s.target, type: "change",
                stage: "chat", status: "idle", owner: s.owner, created_at: NOW, updated_at: NOW
            };
            this.sessions.push(FakeBackend.newData(session,
                [{ id: this.id("a"), kind: "report", version: 1, content: report.content, created_at: NOW, based_on: null }]));
            return this.json(201, this.sessionJson(session));
        }
        if (sub === "findings" && method === "GET") {
            return this.json(200, data.findings);
        }
        const fm = /^findings\/([^/]+)(\/open)?$/.exec(sub);
        if (fm) {
            const finding = data.findings.find((f) => f.id === decodeURIComponent(fm[1]));
            if (!finding) {
                return this.json(404, { detail: "Finding not found" });
            }
            if (this.blocked(s)) {
                // routes.py `_refuse_lost_flag`: the detail is refused whether it is the stored
                // text or a live read (the stored text is raw), and so is "open".
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (!fm[2] && method === "GET") {
                // The stored text, as SAP sent it: a diagnose session only runs on a flagged target.
                const detail = `Detail of ${finding.kind} ${finding.ref_id}: raised by DEVUSER01 at line ${finding.line ?? "?"}.`;
                return this.json(200, {
                    finding,
                    detail: params.get("refresh") === "true" ? `${detail}\n(re-read from SAP)` : detail
                });
            }
            if (fm[2] && method === "POST") {
                return this.openFindingSource(data, finding);
            }
        }
        if (sub === "approvals" && method === "GET") {
            return this.json(200, data.approvals.map((a) => this.approvalJson(a)));
        }
        const am = /^approvals\/([^/]+)$/.exec(sub);
        if (am && method === "POST") {
            if (s.type !== "diagnose") {
                return notDiagnose();
            }
            const approval = data.approvals.find((a) => a.id === decodeURIComponent(am[1]));
            if (!approval) {
                return this.json(404, { detail: "Approval not found" });
            }
            const decision = body.decision;
            if (decision !== "approve" && decision !== "deny") {
                return this.json(422, { detail: [{ loc: ["body", "decision"], msg: "Input should be 'approve' or 'deny'" }] });
            }
            // The stored state decides, in the order of agents/ide/approvals.py `decide`: pending
            // and not expired first (an expired row stays refused, a row past its expiry becomes
            // `expired`), then a deny, which always succeeds, and only an approve rechecks the
            // target's flag. A decision writes no chat message: the row is the record.
            if (approval.status === "expired") {
                return this.json(APPROVAL_EXPIRED.status, APPROVAL_EXPIRED.body);
            }
            if (approval.status !== "pending") {
                return this.gateError("approval_not_pending", "The approval was already decided.");
            }
            const expiresAt = this.approvalExpiry.get(approval.id);
            if (expiresAt && Date.parse(expiresAt) <= Date.now()) {
                approval.status = "expired";
                return this.json(APPROVAL_EXPIRED.status, APPROVAL_EXPIRED.body);
            }
            if (decision === "deny") {
                approval.status = "denied";
                approval.decided_at = NOW;
                return this.json(200, this.approvalJson(approval));
            }
            if (!this.nonProduction(s.target)) {
                // Nothing was decided: the row stays pending.
                return this.json(TARGET_NOT_NON_PRODUCTION.status, TARGET_NOT_NON_PRODUCTION.body);
            }
            approval.decided_at = NOW;
            if (this.failArming) {
                // Arming failed after the decision: a decided row, answered with 200.
                approval.status = "failed";
                approval.error_code = this.failArming;
                approval.result = this.failArming === "arc1_timeout_unknown"
                    ? { note: "may have been armed; check trace_requests" } : null;
                return this.json(200, this.approvalJson(approval));
            }
            approval.status = "approved";
            if (this.armingWithoutResult) {
                approval.result = null;
            } else if (approval.action === "trace_start") {
                approval.result = { trace_request_id: this.id("TRC"), expires_at: "2026-10-03T11:00:00" };
            } else {
                approval.result = { trace_request_id: (approval.params as { id: string }).id };
            }
            return this.json(200, this.approvalJson(approval));
        }
        return undefined;
    }

    /** A proposal of an ABAP object: state `modified`/`new` and an object path (`stages._proposed_paths`). */
    private static isProposedObject(f: WorkspaceFile): boolean {
        return !!f.object_name && (f.state === "modified" || f.state === "new");
    }

    /**
     * What a propose approve pins (`stages._proposed_revisions`): the latest
     * revision of every proposed object file; a `read` file keeps its
     * revisions but is no proposal, a note is no object, and a proposal
     * without a revision has nothing to pin.
     */
    private proposedRevisions(data: SessionData): Record<string, number> {
        const current: Record<string, number> = {};
        data.files.forEach((f) => {
            const latest = FakeBackend.latest(f);
            if (latest && FakeBackend.isProposedObject(f)) {
                current[f.path] = latest.revision;
            }
        });
        return current;
    }

    /** `revisions` of an approve equal {@link proposedRevisions}, no more, no less. */
    private sameRevisions(data: SessionData, revisions: unknown): boolean {
        if (typeof revisions !== "object" || revisions === null || Array.isArray(revisions)) {
            return false;
        }
        const sent = revisions as Record<string, unknown>;
        const current = this.proposedRevisions(data);
        const keys = Object.keys(current);
        return keys.length === Object.keys(sent).length && keys.every((k) => sent[k] === current[k]);
    }

    /** A diagnose session's target lost the flag now (or a test forced it with `Session.flagLost`). */
    private flagLost(s: Session): boolean {
        return s.flagLost === true || !this.nonProduction(s.target);
    }

    /** A diagnose session on a target that lost its flag: nothing more is read from SAP or sent to the model. */
    private blocked(s: Session): boolean {
        return s.type === "diagnose" && this.flagLost(s);
    }

    /** `SessionOut`, computed from the stored rows on every read as the server does. */
    private sessionJson(s: Session): Record<string, unknown> {
        const data = this.sessions.find((d) => d.session === s);
        const comments = data?.comments ?? [];
        const objects = data ? FakeBackend.objectNames(data) : [];
        const { flagLost: _knob, ...served } = s;
        return {
            ...served,
            requests_used: s.requests_used ?? 0,
            request_cap: s.request_cap ?? 200,
            objects: objects.slice(0, 5),
            objects_total: objects.length,
            changed_objects: data ? new Set(data.files.filter((f) => f.object_name && (f.state === "modified" || f.state === "new"))
                .map((f) => f.object_name)).size : 0,
            findings_count: s.type === "diagnose" ? data?.findings.length ?? 0 : null,
            target_non_production: this.nonProduction(s.target),
            pins: data ? FakeBackend.copyPins(data.pins) : {},
            waiting: data ? this.waiting(data) : null,
            open_comments: comments.filter((c) => c.state === "open").length,
            unresolved_comments: comments.filter((c) => c.state === "open" || c.state === "sent").length
        };
    }

    /** Every object name of the session's files, first seen first (`SessionOut.objects`, B10). */
    private static objectNames(data: SessionData): string[] {
        const names: string[] = [];
        data.files.forEach((f) => {
            if (f.object_name && !names.includes(f.object_name)) {
                names.push(f.object_name);
            }
        });
        return names;
    }

    /** A run used one model request: counted on the session, reported in the `usage` frame. */
    private useRequest(s: Session): { requests_used: number; request_cap: number } {
        s.requests_used = (s.requests_used ?? 0) + 1;
        return { requests_used: s.requests_used, request_cap: s.request_cap ?? 200 };
    }

    private static copyPins(pins: Pins): Pins {
        return { ...pins, ...(pins.files ? { files: { ...pins.files } } : {}) };
    }

    /** The worklist marker, first match wins (contract §1.2). */
    private waiting(data: SessionData): WaitingReason | null {
        const s = data.session;
        if (s.stage === "done") {
            // A finished session waits for nothing (backend e021a23).
            return null;
        }
        if (s.type === "diagnose" && data.approvals.some((a) => a.status === "pending")) {
            return "approval";
        }
        if (data.comments.some((c) => c.state === "addressed")) {
            return "comments";
        }
        if (s.type !== "change" || s.status !== "idle") {
            return null;
        }
        if (s.stage === "propose" && data.files.some((f) => {
            const latest = FakeBackend.latest(f);
            return !!latest && data.pins.files?.[f.path] !== latest.revision;
        })) {
            return "changes";
        }
        const kind = ({ design: "design", plan: "plan", review: "review" } as Partial<Record<Stage, "design" | "plan" | "review">>)[s.stage];
        if (kind) {
            const latest = this.latestVersion(data, kind);
            if (latest > 0 && latest !== data.pins[kind]) {
                return "document";
            }
        }
        return null;
    }

    private latestVersion(data: SessionData, kind: ArtifactKind): number {
        return data.artifacts.filter((a) => a.kind === kind).reduce((max, a) => Math.max(max, a.version), 0);
    }

    /**
     * One new revision per file, checked at run end (base check, then syntax),
     * and its `file` frame with revision, base and syntax status.
     */
    private proposeFrames(files: WorkspaceFile[], runId: string): [string, unknown][] {
        return files.map((file): [string, unknown] => {
            const row = this.propose(file, `${file.origin_source}\n* proposed by the fake`, runId);
            if (file.object_type) {
                this.checkSyntax(file, row);
            }
            return ["file", {
                path: file.path, state: file.state, revision: row.revision,
                base_status: FakeBackend.baseStatus(file), syntax_status: row.syntax_status
            }];
        });
    }

    // --- review comments (contract §1.1-1.2) ---------------------------------

    private handleComments(
        method: string, sub: string, data: SessionData, body: Record<string, unknown>, params: URLSearchParams
    ): Response | undefined {
        if (sub === "comments" && method === "GET") {
            const state = params.get("state");
            if (state !== null && !COMMENT_STATES.includes(state as CommentState)) {
                return this.json(422, { detail: [{ loc: ["query", "state"], msg: "Input should be 'open', 'sent', 'addressed' or 'dismissed'" }] });
            }
            return this.json(200, data.comments.filter((c) => state === null || c.state === state).map(FakeBackend.commentJson));
        }
        if (sub === "comments" && method === "POST") {
            return this.createComment(data, body);
        }
        const cm = /^comments\/([^/]+)$/.exec(sub);
        if (!cm) {
            return undefined;
        }
        const comment = data.comments.find((c) => c.id === decodeURIComponent(cm[1]));
        if (!comment) {
            return this.json(404, { detail: "Comment not found" });
        }
        const notEditable = (): Response => this.gateError("comment_not_editable", "Only an open comment can be changed or deleted.");
        if (method === "DELETE") {
            if (comment.state !== "open") {
                return notEditable();
            }
            data.comments = data.comments.filter((c) => c !== comment);
            return this.json(204);
        }
        if (method === "PATCH") {
            const hasBody = body.body !== undefined;
            const hasState = body.state !== undefined;
            const hasQuote = Object.prototype.hasOwnProperty.call(body, "quote");
            if (hasQuote && (hasState || (body.quote !== null && typeof body.quote !== "string"))) {
                return this.json(422, { detail: "A quote goes with a body edit only.", code: "invalid_quote" });
            }
            if (!hasState && !hasBody && !hasQuote) {
                return this.json(422, { detail: "Send either body or state." });
            }
            if (hasBody && hasState) {
                return this.json(422, { detail: "Send either body or state." });
            }
            if (hasQuote && !hasBody) {
                if (comment.state !== "open") {
                    return notEditable();
                }
                comment.quote = FakeBackend.cleanQuote(body.quote);
            } else if (hasBody) {
                const text = FakeBackend.commentBody(body.body);
                if (text === null) {
                    return this.json(422, { detail: [{ loc: ["body", "body"], msg: "String should have 1 to 4000 characters" }] });
                }
                if (comment.state !== "open") {
                    return notEditable();
                }
                comment.body = text;
                if (hasQuote) {
                    comment.quote = FakeBackend.cleanQuote(body.quote);
                }
            } else {
                const next = body.state as CommentState;
                if (next !== "open" && next !== "dismissed") {
                    return this.json(422, { detail: [{ loc: ["body", "state"], msg: "Input should be 'open' or 'dismissed'" }] });
                }
                if (!USER_TRANSITIONS[comment.state].includes(next)) {
                    return this.gateError("invalid_transition", `A ${comment.state} comment cannot become ${next}.`);
                }
                comment.state = next;
            }
            comment.updated_at = NOW;
            return this.json(200, FakeBackend.commentJson(comment));
        }
        return undefined;
    }

    /** `CommentOut`: `quote` is required (null when none), as the contract says (backend a0f37aa). */
    private static commentJson(c: Comment): Comment {
        return { ...c, quote: c.quote ?? null };
    }

    /**
     * As the server cleans a quote (`store._comment_quote`): control
     * characters other than tab/newline/CR and Unicode format characters
     * (Cf) removed, whitespace runs collapsed, cut to 200 code points, then
     * trimmed; nothing left is null. Callers refuse a non-string first.
     */
    private static cleanQuote(value: unknown): string | null {
        if (typeof value !== "string") {
            return null;
        }
        // eslint-disable-next-line no-control-regex
        const plain = value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "").replace(/\p{Cf}/gu, "");
        // Python's str.split() also splits on U+0085 (NEL), which JS's \s does not know.
        const line = Array.from(plain.replace(/[\s\u0085]+/g, " ").trim()).slice(0, 200).join("").trim();
        return line || null;
    }

    private static commentBody(value: unknown): string | null {
        return typeof value === "string" && value.length >= 1 && value.length <= 4000 ? value : null;
    }

    private createComment(data: SessionData, body: Record<string, unknown>): Response {
        const text = FakeBackend.commentBody(body.body);
        if (text === null) {
            return this.json(422, { detail: [{ loc: ["body", "body"], msg: "String should have 1 to 4000 characters" }] });
        }
        if (body.quote !== undefined && body.quote !== null && typeof body.quote !== "string") {
            return this.json(422, { detail: "The quote must be text", code: "invalid_quote" });
        }
        const int = (v: unknown): v is number => Number.isInteger(v);
        const invalid = (why: string): Response => this.json(422, { detail: `Invalid anchor: ${why}.`, code: "invalid_anchor" });
        const comment: Comment = {
            id: this.id("c"), anchor: "file", path: null, revision: null, line_start: null, line_end: null,
            kind: null, version: null, paragraph: null, body: text, state: "open", answer: null,
            // The backend's coming `quote` (plain text, at most 200 characters): stored and echoed.
            quote: FakeBackend.cleanQuote(body.quote),
            created_at: NOW, updated_at: NOW
        };
        if (body.anchor === "file") {
            const file = data.files.find((f) => f.path === body.path);
            if (!file) {
                return invalid("the file is not in the session");
            }
            const { revision, line_start: start, line_end: end } = body;
            if (!int(revision) || revision < 0 || revision > (FakeBackend.latest(file)?.revision ?? 0)) {
                return invalid("unknown revision");
            }
            if (!int(start) || !int(end) || start < 1 || end < start) {
                return invalid("bad line range");
            }
            Object.assign(comment, { path: file.path, revision, line_start: start, line_end: end });
        } else if (body.anchor === "document") {
            const { kind, version, paragraph } = body;
            if (!ARTIFACT_KINDS.includes(kind as ArtifactKind) || !int(version)
                || !data.artifacts.some((a) => a.kind === kind && a.version === version)) {
                return invalid("unknown document version");
            }
            if (!int(paragraph) || paragraph < 0) {
                return invalid("bad paragraph");
            }
            Object.assign(comment, { anchor: "document", kind, version, paragraph });
        } else {
            return this.json(422, { detail: [{ loc: ["body", "anchor"], msg: "Input should be 'file' or 'document'" }] });
        }
        data.comments.push(comment);
        return this.json(201, FakeBackend.commentJson(comment));
    }

    /**
     * `POST request-changes`: refusals before the stream, then `run`,
     * `comments` (sent), the text, `comments` (addressed), the reworked
     * document (or file revisions in `propose`), `usage`, `done`.
     */
    private requestChanges(data: SessionData, body: Record<string, unknown>): Response {
        const s = data.session;
        const note = body.note === undefined || body.note === null ? "" : body.note;
        if (typeof note !== "string" || note.length > 4000) {
            return this.json(422, { detail: [{ loc: ["body", "note"], msg: "String should have at most 4000 characters" }] });
        }
        // The server's gate order (stages.assert_can_run), then nothing_to_send.
        if (s.type === "diagnose") {
            return this.gateError("revise_not_allowed", "Changes cannot be requested in a diagnose session.");
        }
        if (s.stage === "done") {
            return this.gateError("stage_done", "The session is done.");
        }
        if (s.status === "running") {
            return this.gateError("run_in_progress", "A run is already in progress.");
        }
        if (!REVISABLE.includes(s.stage)) {
            return this.gateError("revise_not_allowed", `Changes cannot be requested in stage ${s.stage}.`);
        }
        if (this.exhaustUsage) {
            return this.gateError("usage_exhausted", "This session has used its model requests.");
        }
        const allOpen = data.comments.filter((c) => c.state === "open");
        const open = this.sendCap > 0 ? allOpen.slice(0, this.sendCap) : allOpen;
        const left = allOpen.length - open.length;
        if (!open.length && !note.trim()) {
            return this.gateError("nothing_to_send", "Write a comment or a note first.");
        }
        const runId = this.id("r");
        open.forEach((c) => { c.state = "sent"; c.updated_at = NOW; });
        const ids = open.map((c) => c.id);
        const userMsg: Message = {
            id: this.id("m"), role: "user", stage: s.stage, created_at: NOW,
            // As the server stores it (B9): the count, then the note.
            content: [ids.length ? `Request changes: ${ids.length} comment(s)` : "", note].filter(Boolean).join("\n\n")
        };
        const reply: Message = {
            id: this.id("m"), role: "assistant", stage: s.stage, created_at: NOW, content: `Reworked the ${s.stage}.`
        };
        data.messages.push(userMsg, reply);
        const frames: [string, unknown][] = [["run", { run_id: runId, stage: s.stage, message_id: userMsg.id }]];
        if (ids.length) {
            frames.push(["comments", { ids, state: "sent", left }]);
        }
        frames.push(["text", { delta: reply.content }]);
        // What the run does after its first text: resolve, store the rework, end. Applied only
        // when the stream gets there, so a paused stream (pauseStream) shows the comments `sent`.
        const rest = (): [string, unknown][] => {
            const tail: [string, unknown][] = [];
            if (ids.length && !this.resolveNoComments) {
                open.forEach((c) => {
                    c.state = "addressed";
                    c.answer = `Addressed: ${c.body}`.slice(0, 500);
                    c.updated_at = NOW;
                });
                tail.push(["comments", { ids, state: "addressed" }]);
            }
            if (s.stage === "propose") {
                const proposed = data.files.filter((f) => f.revisions?.length);
                tail.push(...this.proposeFrames(proposed.length ? proposed : data.files.slice(0, 1), runId));
            } else {
                const artifact = this.storeArtifact(data, DOC_KIND[s.stage] as ArtifactKind);
                tail.push(["artifact", { id: artifact.id, kind: artifact.kind, version: artifact.version }]);
            }
            tail.push(["usage", this.useRequest(s)]);
            // The run is over: whatever it did not resolve goes back to open, and the server says so (B9).
            const unresolved = open.filter((c) => c.state === "sent");
            unresolved.forEach((c) => { c.state = "open"; c.updated_at = NOW; });
            if (unresolved.length) {
                tail.push(["comments", { ids: unresolved.map((c) => c.id), state: "open" }]);
            }
            tail.push(["done", { message_id: reply.id, stage: s.stage, status: "idle" }]);
            return tail;
        };
        const gate = this.streamPause;
        if (gate) {
            this.streamPause = undefined;
            return this.sse(frames, { after: frames.length, gate, rest });
        }
        frames.push(...rest());
        return this.sse(frames);
    }

    // --- conventions (contract §1.2, D4) -------------------------------------

    private createConventions(body: Record<string, unknown>): Response {
        if (!this.isAdmin) {
            return this.json(403, { detail: "Admin scope required" });
        }
        const target = body.target;
        if (typeof target !== "string" || !TARGET_PATTERN.test(target)) {
            return this.json(422, { detail: [{ loc: ["body", "target"], msg: "String should match the target pattern" }] });
        }
        const strict = FakeBackend.strictBool(body);
        if (strict) {
            return strict;
        }
        if (this.conventions.some((c) => c.target === target)) {
            return this.json(409, { detail: `Target '${target}' exists.`, code: "target_exists" });
        }
        const created = { ...body, target } as Conventions;
        this.conventions.push(created);
        return this.json(201, FakeBackend.conventionsJson(created));
    }

    /** Updates an existing target only (404 `unknown_target`); `clear` removes fields. */
    private putConventions(target: string, body: Record<string, unknown>): Response {
        if (!this.isAdmin) {
            return this.json(403, { detail: "Admin scope required" });
        }
        const found = this.conventions.find((c) => c.target === target);
        if (!found) {
            return this.json(404, { detail: `Unknown target '${target}'.`, code: "unknown_target" });
        }
        const strict = FakeBackend.strictBool(body);
        if (strict) {
            return strict;
        }
        const invalid = FakeBackend.conventionsUpdateErrors(body);
        if (invalid.length) {
            return this.json(422, { detail: invalid });
        }
        const { clear = [], ...fields } = body;
        if (!Array.isArray(clear) || clear.some((k) => !CLEARABLE.includes(String(k)))) {
            return this.json(422, { detail: [{ loc: ["body", "clear"], msg: "Unknown field to clear" }] });
        }
        const both = (clear as string[]).filter((k) => fields[k] !== undefined && fields[k] !== null);
        if (both.length) {
            return this.json(422, { detail: `Set and cleared at once: ${both.join(", ")}.` });
        }
        const next = { ...found, ...fields, target } as Conventions;
        // The columns are NOT NULL with "" as their empty value: a cleared field is stored as "".
        (clear as string[]).forEach((k) => { next[k] = ""; });
        this.conventions = this.conventions.map((c) => (c === found ? next : c));
        return this.json(200, FakeBackend.conventionsJson(next));
    }

    /**
     * What the server's `ConventionsUpdate` (extra=forbid) refuses besides the flag's StrictBool:
     * unknown keys, a text field that is not a string or longer than its `max_length`, and a
     * `clean_core_level` outside A-D. FastAPI's 422 shape, one item per refusal.
     */
    private static conventionsUpdateErrors(body: Record<string, unknown>): { loc: string[]; msg: string }[] {
        const errors: { loc: string[]; msg: string }[] = [];
        for (const [key, value] of Object.entries(body)) {
            if (key === "clear" || key === "non_production") {
                continue;
            }
            if (!(key in CONVENTIONS_MAX) && key !== "clean_core_level") {
                errors.push({ loc: ["body", key], msg: "Extra inputs are not permitted" });
                continue;
            }
            if (value === null || value === undefined) {
                continue;
            }
            if (typeof value !== "string") {
                errors.push({ loc: ["body", key], msg: "Input should be a valid string" });
            } else if (key === "clean_core_level" && !/^[A-D]$/.test(value)) {
                errors.push({ loc: ["body", key], msg: "String should match pattern '^[A-D]$'" });
            } else if (key !== "clean_core_level" && Array.from(value).length > CONVENTIONS_MAX[key]) {
                errors.push({ loc: ["body", key], msg: `String should have at most ${CONVENTIONS_MAX[key]} characters` });
            }
        }
        return errors;
    }

    /** `ConventionsOut`: every field present (null when not set), `non_production` a boolean. */
    private static conventionsJson(c: Conventions): Record<string, unknown> {
        const fields = ["label", "destination", "namespace", "package", "atc_variant", "clean_core_level", "free_text", "updated_at"];
        const out: Record<string, unknown> = { ...c, non_production: c.non_production === true };
        fields.forEach((k) => { out[k] = (c as Record<string, unknown>)[k] ?? null; });
        return out;
    }

    /** `ApprovalOut`: with the server's `ttl_min`. */
    private approvalJson(a: Approval): Approval {
        return { ...a, ttl_min: a.ttl_min ?? this.approvalTtlMin };
    }

    /** `ArtifactOut`: every field, `stage` and `based_on` always present. */
    private static artifactJson(a: Artifact): Artifact {
        return { ...a, stage: a.stage ?? KIND_STAGE[a.kind], based_on: a.based_on ?? null };
    }

    /** `non_production` is a StrictBool on the server: "true" or 1 is 422, `null` means not sent. */
    private static strictBool(body: Record<string, unknown>): Response | undefined {
        if (body.non_production !== undefined && body.non_production !== null && typeof body.non_production !== "boolean") {
            return new Response(JSON.stringify({ detail: [{ loc: ["body", "non_production"], msg: "Input should be a valid boolean" }] }),
                { status: 422, headers: { "Content-Type": "application/json" } });
        }
        return undefined;
    }

    private nonProduction(target: string): boolean {
        return this.conventions.find((c) => c.target === target)?.non_production === true;
    }

    /** As the server: a class pool opens the class, any other program the program; no program is `no_source`. */
    private openFindingSource(data: SessionData, finding: DiagnoseFinding): Response {
        if (!finding.program) {
            return this.json(422, { detail: "The finding has no source program.", code: "no_source" });
        }
        const pool = /^(\w+?)=*CP$/.exec(finding.program);
        const type = pool ? "CLAS" : "PROG";
        const name = (pool ? pool[1] : finding.program).toUpperCase();
        const filePath = `src/${type}/${name.toLowerCase()}.${type === "CLAS" ? "clas.abap" : "prog.abap"}`;
        let file = data.files.find((f) => f.path === filePath);
        if (!file) {
            file = {
                path: filePath, state: "read", object_type: type, object_name: name,
                origin_source: Array.from({ length: 30 }, (_, i) => `* ${name} line ${i + 1}`).join("\n"),
                proposed_source: ""
            };
            data.files.push(file);
        }
        const methodInclude = !!finding.include && /CM[0-9A-Z]{3}$/.test(finding.include);
        return this.json(200, {
            file: this.fileSummary(file),
            line: methodInclude ? null : finding.line,
            hint: methodInclude ? `Method include ${finding.include ?? ""}, line ${finding.line ?? "?"}` : null
        });
    }
}
