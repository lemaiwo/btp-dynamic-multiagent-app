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
 * frames. Every session JSON carries `masked`, computed from the conventions
 * as they are now; a diagnose session that reads `masked: true` (its target
 * lost the flag) is refused with 409 `target_not_non_production` on runs,
 * reports, finding details and reads from SAP (`DIAGNOSE_BLOCKED`).
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
    Approval, ApprovalAction, ApprovalStatus, DiagnoseFinding, TraceParams
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
     * Forces `masked: true` in the session JSON. Without it the JSON says what
     * the server computes on every read: masked unless the target's
     * conventions are flagged `non_production` now.
     */
    masked?: boolean;
}

export interface Artifact {
    id: string;
    kind: "design" | "plan" | "note" | "review" | "report";
    version: number;
    content: string;
    created_at: string;
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
 * `masked: true`): message and report runs, a finding's detail (the stored
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
}

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

    public principal = "developer@example.com";
    public isAdmin = false;
    public conventions: Conventions[] = [];
    public sessions: SessionData[] = [];
    /** Set to force the next matching call to fail. */
    public failNext?: FailNext;
    /** Every intercepted call as "METHOD path" (query string dropped), in order. */
    public requests: string[] = [];
    /** Every call answered so far, same format; a held call appears once it is released. */
    public responses: string[] = [];

    /** Run streams leave out the final `done` frame (the `stream_incomplete` case). */
    public omitDone = false;
    /** A run also streams a plan and two tool calls, and stores them as the answer's activity. */
    public withActivity = false;
    /** A run streams this `error` frame before its end (run_timeout, run_failed, ...). */
    public errorFrame?: { message: string; code?: string };
    /** Run streams whose last frame was handed to the stream (or dropped by a cancelled reader). */
    public streamsFlushed = 0;
    /** An approve fails to arm: the decide route answers 200 with `status: "failed"` and this `error_code`. */
    public failArming?: string;
    /** An approve is stored as `approved` with `result: null` (the outcome is unknown). */
    public armingWithoutResult = false;
    /** When each stored approval stops being decidable (ISO time), by approval id. */
    private approvalExpiry = new Map<string, string>();

    private holds: { key: string; gate: Promise<void> }[] = [];
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

    /** Adds a session owned by the caller; it becomes the newest one. */
    public addSession(
        title: string, files: WorkspaceFile[] = [], artifacts: Artifact[] = [], type: SessionType = "change"
    ): Session {
        const session: Session = {
            id: this.id("s"), title, target: "dev-system", type,
            stage: type === "diagnose" ? "investigate" : "chat", status: "idle",
            owner: this.principal, created_at: NOW, updated_at: NOW
        };
        this.sessions.push({ session, messages: [], artifacts, files, findings: [], approvals: [] });
        return session;
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

    /** Findings and approvals of a session, for journeys to inspect or seed. */
    public dataOf(sid: string): SessionData | undefined {
        return this.sessions.find((d) => d.session.id === sid);
    }

    public reset(): void {
        this.nextId = 1;
        this.requests = [];
        this.responses = [];
        this.holds = [];
        this.failNext = undefined;
        this.isAdmin = false;
        this.omitDone = false;
        this.withActivity = false;
        this.errorFrame = undefined;
        this.streamsFlushed = 0;
        this.filesGap = undefined;
        this.streamPause = undefined;
        this.pausedRun = undefined;
        this.runScript = undefined;
        this.failArming = undefined;
        this.armingWithoutResult = false;
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
            approvals: []
        }];
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

    private fileSummary(f: WorkspaceFile): Pick<WorkspaceFile, "path" | "state" | "object_type" | "object_name"> {
        return { path: f.path, state: f.state, object_type: f.object_type, object_name: f.object_name };
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
        const held = this.holds.findIndex((h) => h.key === key);
        if (held >= 0) {
            const { gate } = this.holds.splice(held, 1)[0];
            await gate;
        }
        const response = await this.route(method, path, params, init);
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

        if (method === "GET" && path === "me") {
            return this.json(200, {
                principal: this.principal, is_admin: this.isAdmin,
                targets: this.conventions.map((c) => c.target),
                diagnose_targets: this.conventions.filter((c) => c.non_production === true).map((c) => c.target)
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
                this.sessions.push({ session, messages: [], artifacts: [], files: [], findings: [], approvals: [] });
                return this.json(201, this.sessionJson(session));
            }
        }
        if (method === "GET" && path === "objects/search") {
            const q = (params.get("q") || "").toUpperCase();
            return this.json(200, [
                { type: "CLAS", name: "ZCL_DEMO", package: "ZLOCAL", description: "Demo class" },
                { type: "DDLS", name: "ZI_DEMO", package: "ZLOCAL", description: "Demo view" }
            ].filter((o) => o.name.includes(q.replace(/\*/g, ""))));
        }
        if (path === "conventions" && method === "GET") {
            return this.json(200, this.conventions);
        }
        const conv = /^conventions\/([^/]+)$/.exec(path);
        if (conv) {
            const target = decodeURIComponent(conv[1]);
            if (method === "GET") {
                const found = this.conventions.find((c) => c.target === target);
                return found ? this.json(200, found) : this.json(404, { detail: "Not found" });
            }
            if (method === "PUT") {
                if (!this.isAdmin) {
                    return this.json(403, { detail: "Admin scope required" });
                }
                const next = { ...body, target } as Conventions;
                this.conventions = this.conventions.filter((c) => c.target !== target).concat(next);
                return this.json(200, next);
            }
        }
        if (method === "GET" && path === "admin/sessions") {
            if (!this.isAdmin) {
                return this.json(403, { detail: "Admin scope required" });
            }
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
                    artifacts: data.artifacts.map(({ id, kind, version, created_at }) => ({ id, kind, version, created_at })),
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
            return this.json(200, data.messages);
        }
        if ((sub === "messages" || sub === "revise") && method === "POST") {
            if (sub === "messages" && this.blocked(s)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (s.status === "running") {
                return this.gateError("run_in_progress", "A run is already in progress.");
            }
            if (sub === "revise" && (s.stage === "chat" || s.stage === "done" || s.type === "diagnose")) {
                return this.gateError("revise_not_allowed", `Revise is not allowed in stage ${s.stage}.`);
            }
            if (s.stage === "done") {
                return this.gateError("stage_done", "The session is done.");
            }
            const userMsg: Message = {
                id: this.id("m"), role: "user", stage: s.stage, created_at: NOW,
                content: String(sub === "revise" ? body.feedback : body.text)
            };
            const answer = `Fake answer in stage **${s.stage}**.`;
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
            const frames: [string, unknown][] = [
                ["run", { run_id: this.id("r"), stage: s.stage, message_id: userMsg.id }],
                ...(this.withActivity ? [
                    ["plan", { todos: plan }] as [string, unknown],
                    ["tool", { ...read, status: "running", output: "" }] as [string, unknown],
                    ["tool", read] as [string, unknown]
                ] : []),
                ...chunks.map((delta): [string, unknown] => ["text", { delta }]),
                ...this.scriptedFrames(data)
            ];
            const kind = ({ design: "design", plan: "plan", propose: "note", review: "review" } as const)[
                s.stage as "design" | "plan" | "propose" | "review"
            ];
            if (kind) {
                const version = data.artifacts.filter((a) => a.kind === kind).length + 1;
                const artifact: Artifact = {
                    id: this.id("a"), kind, version, content: `# ${kind} v${version}\n\nWritten by the fake.`, created_at: NOW
                };
                data.artifacts.unshift(artifact);
                frames.push(["artifact", { id: artifact.id, kind, version }]);
            }
            const firstFile = frames.length;
            if (s.stage === "propose" && data.files.length) {
                // A proposal for the first workspace file (every file after
                // splitFiles()), as a propose run writes them.
                for (const file of this.filesGap ? data.files : data.files.slice(0, 1)) {
                    file.proposed_source = `${file.origin_source}\n* proposed by the fake`;
                    file.state = "modified";
                    frames.push(["file", { path: file.path, state: file.state }]);
                }
            }
            if (this.errorFrame) {
                frames.push(["error", this.errorFrame]);
            }
            frames.push(["usage", { requests_used: 1, request_cap: 200 }]);
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
            if (s.status === "running") {
                return this.gateError("run_in_progress", "A run is already in progress.");
            }
            if (s.type === "diagnose") {
                return this.gateError("approve_not_allowed", "A diagnose session has no stages to approve.");
            }
            const next = NEXT_STAGE[s.stage];
            if (!next) {
                return this.gateError("stage_done", "The session is done.");
            }
            const need = ({ design: "design", plan: "plan", review: "review" } as Partial<Record<Stage, string>>)[s.stage];
            if (need && !data.artifacts.some((a) => a.kind === need)) {
                return this.gateError("missing_artifact", `Approve needs a ${need} artifact.`);
            }
            if (s.stage === "propose" && !data.files.some((f) => f.state === "modified" || f.state === "new")) {
                return this.gateError("no_proposals", "Approve needs at least one proposed file.");
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
            return this.json(200, data.artifacts.filter((a) => !kind || a.kind === kind));
        }
        const art = /^artifacts\/([^/]+)$/.exec(sub);
        if (art && method === "GET") {
            const found = data.artifacts.find((a) => a.id === art[1]);
            return found ? this.json(200, found) : this.json(404, { detail: "Artifact not found" });
        }
        if (sub === "files" && method === "GET") {
            return this.json(200, data.files.map((f) => this.fileSummary(f)));
        }
        if (sub === "file" || sub === "file/refresh" || sub === "file/lint") {
            const file = data.files.find((f) => f.path === params.get("path"));
            if (!file) {
                return this.json(404, { detail: "File not found" });
            }
            if (sub !== "file" && this.blocked(s)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            if (sub === "file/lint" && method === "POST") {
                file.linted = true;
                return this.json(200, file.lint ?? []);
            }
            return this.json(200, {
                path: file.path, state: file.state, origin_source: file.origin_source,
                proposed_source: file.proposed_source, lint: file.linted ? file.lint ?? [] : []
            });
        }
        if (sub === "open" && method === "POST") {
            if (this.blocked(s)) {
                return this.json(DIAGNOSE_BLOCKED.status, DIAGNOSE_BLOCKED.body);
            }
            const type = String(body.type || "").toUpperCase();
            const name = String(body.name || "");
            const ext: Record<string, string> = {
                CLAS: "clas.abap", INTF: "intf.abap", PROG: "prog.abap", DDLS: "ddls.asddls",
                BDEF: "bdef.asbdef", DCLS: "dcls.asdcls", DDLX: "ddlx.asddlxs", SRVD: "srvd.srvdsrv", FUNC: "func.abap"
            };
            if (!ext[type]) {
                return this.json(422, { detail: `Unsupported object type '${type}'` });
            }
            const filePath = `src/${type}/${name.toLowerCase()}.${ext[type]}`;
            let file = data.files.find((f) => f.path === filePath);
            if (!file) {
                file = {
                    path: filePath, state: "read", object_type: type, object_name: name.toUpperCase(),
                    origin_source: `* ${name.toUpperCase()}`, proposed_source: ""
                };
                data.files.push(file);
            }
            return this.json(200, this.fileSummary(file));
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
            const approval = this.storeApproval(data, script.approval);
            // The frame of a new proposal is always pending, whatever becomes of the stored row.
            frames.push(["approval_required", { ...approval, status: "pending" }]);
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
                id: this.id("a"), kind: "report", version, created_at: NOW,
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
                ["usage", { requests_used: 1, request_cap: 200 }],
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
            this.sessions.push({
                session, messages: [], files: [], findings: [], approvals: [],
                artifacts: [{ id: this.id("a"), kind: "report", version: 1, content: report.content, created_at: NOW }]
            });
            return this.json(201, session);
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
            return this.json(200, data.approvals);
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
                return this.json(200, approval);
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
                return this.json(200, approval);
            }
            approval.status = "approved";
            if (this.armingWithoutResult) {
                approval.result = null;
            } else if (approval.action === "trace_start") {
                approval.result = { trace_request_id: this.id("TRC"), expires_at: "2026-10-03T11:00:00" };
            } else {
                approval.result = { trace_request_id: (approval.params as { id: string }).id };
            }
            return this.json(200, approval);
        }
        return undefined;
    }

    /** `masked` as the server computes it on every read: true unless the target is non-production now. */
    private isMasked(s: Session): boolean {
        return s.masked === true || !this.nonProduction(s.target);
    }

    /** A diagnose session on a target that lost its flag: nothing more is read from SAP or sent to the model. */
    private blocked(s: Session): boolean {
        return s.type === "diagnose" && this.isMasked(s);
    }

    private sessionJson(s: Session): Session {
        return { ...s, masked: this.isMasked(s) };
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
