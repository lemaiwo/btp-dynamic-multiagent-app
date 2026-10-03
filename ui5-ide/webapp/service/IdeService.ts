import SseParser from "./SseParser";
import type {
    AdminSessionRow, Approval, ApprovalDecision, Artifact, ArtifactKind, Conventions, DiagnoseFinding,
    FileDetail, FileSummary, FindingDetail, FindingOpen, LintFinding, Me, Message, ObjectHit, Session,
    SessionDetail, SessionSummary, SessionType, SseEvent
} from "./types";

/**
 * A non-2xx response from the IDE API, or a stream that broke mid-way.
 *
 * `code` is the stage-gate code the server sends next to `detail` on 409/429
 * (`run_in_progress`, `missing_artifact`, `stage_changed`, ...). The list is
 * open: show `detail` for a code you do not know. `fieldErrors` flattens a
 * FastAPI 422 `detail` array into dotted keys, as AdminError does.
 * `status` is 0 when the network failed rather than the server answering.
 *
 * `isAuth` is true for 401, 403 and `session_expired`, the last being what an
 * expired approuter session looks like from here: a redirect to the identity
 * provider or its HTML login page instead of JSON. The UI shows a "reload to
 * sign in again" hint for it.
 */
export class IdeError extends Error {
    public readonly status: number;
    public readonly detail: string;
    public readonly code?: string;
    public readonly fieldErrors: Record<string, string>;

    public constructor(status: number, detail: string, code?: string, fieldErrors: Record<string, string> = {}) {
        super(detail || `Request failed with status ${status}`);
        this.name = "IdeError";
        this.status = status;
        this.detail = detail;
        this.code = code;
        this.fieldErrors = fieldErrors;
    }

    public get isAuth(): boolean {
        return this.status === 401 || this.status === 403 || this.code === "session_expired";
    }
}

interface ValidationItem { loc?: (string | number)[]; msg?: string }

/** Called once per SSE frame, in arrival order; `done` is always the last one. */
export type SseHandler = (event: SseEvent) => void;

/**
 * The only class in the application that performs HTTP.
 *
 * Every path is *relative* (`backend/...`, never `/backend/...`) so the same
 * build runs behind the standalone approuter and behind a Work Zone site.
 *
 * The run routes answer with `text/event-stream` to a POST, which
 * `EventSource` cannot send; they are read with `fetch` and the body's
 * `ReadableStream` reader instead, through {@link SseParser}.
 */
export default class IdeService {

    private static readonly PREFIX = "backend/";

    private async request<T>(path: string, init?: RequestInit): Promise<T> {
        const response = await IdeService.send(path, {
            ...init,
            headers: {
                Accept: "application/json",
                ...(init?.body ? { "Content-Type": "application/json" } : {}),
                ...(init?.headers ?? {})
            }
        });
        if (response.status === 204) {
            return undefined as T;
        }
        if (IdeService.isHtml(response)) {
            throw IdeService.sessionExpired(response.status);
        }
        try {
            return await response.json() as T;
        } catch {
            throw new IdeError(response.status, "The server answered with something that is not JSON.", "invalid_response");
        }
    }

    /**
     * fetch with the checks every call shares. Redirects are not followed:
     * the API never redirects, so one is the approuter sending an expired
     * session to the identity provider (and following it cross-origin would
     * only fail as an opaque network error). Non-2xx becomes an IdeError.
     */
    private static async send(path: string, init: RequestInit): Promise<Response> {
        let response: Response;
        try {
            response = await fetch(IdeService.PREFIX + path, { ...init, redirect: "manual" });
        } catch (e) {
            if (init.signal?.aborted) {
                throw e;
            }
            throw new IdeError(0, (e as Error).message || "Network error", "network");
        }
        if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
            throw IdeService.sessionExpired(response.status);
        }
        if (!response.ok) {
            throw await IdeService.toError(response);
        }
        return response;
    }

    private static isHtml(response: Response): boolean {
        return (response.headers.get("Content-Type") ?? "").toLowerCase().includes("text/html");
    }

    private static sessionExpired(status: number): IdeError {
        return new IdeError(status, "Your session has expired. Reload the page to sign in again.", "session_expired");
    }

    private static async toError(response: Response): Promise<IdeError> {
        let detail = "";
        let code: string | undefined;
        const fieldErrors: Record<string, string> = {};
        try {
            const body = await response.json() as { detail?: string | ValidationItem[]; code?: unknown };
            if (typeof body.code === "string") {
                code = body.code;
            }
            if (typeof body.detail === "string") {
                detail = body.detail;
            } else if (Array.isArray(body.detail)) {
                body.detail.forEach((item) => {
                    const key = (item.loc ?? []).filter((p) => p !== "body").join(".");
                    if (key && item.msg) {
                        fieldErrors[key] = item.msg;
                    }
                });
                detail = body.detail.map((i) => i.msg).filter(Boolean).join("; ");
            }
        } catch {
            detail = response.statusText;
        }
        return new IdeError(response.status, detail, code, fieldErrors);
    }

    private static json(body: unknown): RequestInit {
        return { body: JSON.stringify(body) };
    }

    private static sid(sid: string): string {
        return `sessions/${encodeURIComponent(sid)}`;
    }

    private static pathQuery(path: string): string {
        return `?path=${encodeURIComponent(path)}`;
    }

    // --- Identity ------------------------------------------------------------
    public getMe(): Promise<Me> {
        return this.request<Me>("me");
    }

    // --- Sessions ------------------------------------------------------------
    public listSessions(): Promise<SessionSummary[]> {
        return this.request<SessionSummary[]>("sessions");
    }

    /** A `diagnose` session needs a `non_production` target (422 `target_not_non_production`). */
    public createSession(title: string, target: string, type: SessionType = "change"): Promise<Session> {
        return this.request<Session>("sessions", { method: "POST", ...IdeService.json({ title, target, type }) });
    }

    public getSession(sid: string): Promise<SessionDetail> {
        return this.request<SessionDetail>(IdeService.sid(sid));
    }

    public renameSession(sid: string, title: string): Promise<Session> {
        return this.request<Session>(IdeService.sid(sid), { method: "PATCH", ...IdeService.json({ title }) });
    }

    public deleteSession(sid: string): Promise<void> {
        return this.request<void>(IdeService.sid(sid), { method: "DELETE" });
    }

    public listMessages(sid: string): Promise<Message[]> {
        return this.request<Message[]>(`${IdeService.sid(sid)}/messages`);
    }

    /** Moves to the next stage; 409 `IdeError` with `code` when the gate refuses. */
    public approve(sid: string): Promise<Session> {
        return this.request<Session>(`${IdeService.sid(sid)}/approve`, { method: "POST" });
    }

    public cancel(sid: string): Promise<Session> {
        return this.request<Session>(`${IdeService.sid(sid)}/cancel`, { method: "POST" });
    }

    // --- Artifacts -----------------------------------------------------------
    /** Latest version first. */
    public listArtifacts(sid: string, kind?: ArtifactKind): Promise<Artifact[]> {
        const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
        return this.request<Artifact[]>(`${IdeService.sid(sid)}/artifacts${query}`);
    }

    public getArtifact(sid: string, aid: string): Promise<Artifact> {
        return this.request<Artifact>(`${IdeService.sid(sid)}/artifacts/${encodeURIComponent(aid)}`);
    }

    // --- Workspace files -----------------------------------------------------
    public listFiles(sid: string): Promise<FileSummary[]> {
        return this.request<FileSummary[]>(`${IdeService.sid(sid)}/files`);
    }

    public getFile(sid: string, path: string): Promise<FileDetail> {
        return this.request<FileDetail>(`${IdeService.sid(sid)}/file${IdeService.pathQuery(path)}`);
    }

    /** Re-reads `origin_source` (the diff base) from the ABAP system. */
    public refreshFile(sid: string, path: string): Promise<FileDetail> {
        return this.request<FileDetail>(`${IdeService.sid(sid)}/file/refresh${IdeService.pathQuery(path)}`, { method: "POST" });
    }

    /** Offline SAPLint on the proposed source. */
    public lintFile(sid: string, path: string): Promise<LintFinding[]> {
        return this.request<LintFinding[]>(`${IdeService.sid(sid)}/file/lint${IdeService.pathQuery(path)}`, { method: "POST" });
    }

    /** Reads an ABAP object into the workspace (state `read`). */
    public openObject(sid: string, type: string, name: string): Promise<FileSummary> {
        return this.request<FileSummary>(`${IdeService.sid(sid)}/open`, { method: "POST", ...IdeService.json({ type, name }) });
    }

    public searchObjects(target: string, q: string): Promise<ObjectHit[]> {
        const params = new URLSearchParams({ target, q });
        // URLSearchParams encodes `*` as-is and spaces as `+`; both are what
        // FastAPI's query parser expects.
        return this.request<ObjectHit[]>(`objects/search?${params.toString()}`);
    }

    // --- Diagnose: report handover, findings, approvals ------------------------
    /**
     * Opens a new **change** session on the same target with the diagnose
     * session's `report` as its first artifact. Refusals are all 409, in
     * this order: `not_diagnose`, `target_not_non_production` (the target
     * lost its flag), `run_in_progress`, `missing_artifact` (no report yet).
     */
    public handover(sid: string): Promise<Session> {
        return this.request<Session>(`${IdeService.sid(sid)}/handover`, { method: "POST" });
    }

    /** Newest first; the metadata, without the detail text. */
    public listFindings(sid: string): Promise<DiagnoseFinding[]> {
        return this.request<DiagnoseFinding[]>(`${IdeService.sid(sid)}/findings`);
    }

    /**
     * The finding plus its detail text as kept with the session; `refresh`
     * reads the text again from SAP (424/502 when ARC-1 fails, 422
     * `no_detail` when SAP has no text for it). 409
     * `target_not_non_production` once the target lost its flag, with or
     * without `refresh`.
     */
    public getFinding(sid: string, fid: string, refresh = false): Promise<FindingDetail> {
        return this.request<FindingDetail>(
            `${IdeService.sid(sid)}/findings/${encodeURIComponent(fid)}${refresh ? "?refresh=true" : ""}`);
    }

    /**
     * Reads the finding's program into the workspace; 422 `no_source` when it
     * has none, 409 `target_not_non_production` when the target lost its flag.
     */
    public openFinding(sid: string, fid: string): Promise<FindingOpen> {
        return this.request<FindingOpen>(`${IdeService.sid(sid)}/findings/${encodeURIComponent(fid)}/open`, { method: "POST" });
    }

    /** Newest first. The server may close interrupted approvals while listing: show what it answers. */
    public listApprovals(sid: string): Promise<Approval[]> {
        return this.request<Approval[]>(`${IdeService.sid(sid)}/approvals`);
    }

    /**
     * Approves or denies a pending trace request and answers the decided
     * approval: a plain JSON answer, no stream, and no run is resumed by it.
     * The caller shows the decision from this answer (or from
     * {@link listApprovals}). A 200 can carry `status: "failed"` with an
     * `error_code` (arming did not work), and `approved` without a `result`
     * (outcome unknown). Refusals carry `code`: 409 `approval_not_pending` /
     * `not_diagnose` / `unknown_trace_request`, 403
     * `target_not_non_production` (an approve only: a deny of a pending
     * approval is never refused for the flag), 410 `approval_expired`, 424
     * `user_token_required` / `arc1_not_configured` (nothing was decided).
     * The server writes no chat message for a decision.
     */
    public decideApproval(sid: string, aid: string, decision: ApprovalDecision): Promise<Approval> {
        return this.request<Approval>(`${IdeService.sid(sid)}/approvals/${encodeURIComponent(aid)}`, {
            method: "POST", ...IdeService.json({ decision })
        });
    }

    // --- Conventions ---------------------------------------------------------
    public listConventions(): Promise<Conventions[]> {
        return this.request<Conventions[]>("conventions");
    }

    public getConventions(target: string): Promise<Conventions> {
        return this.request<Conventions>(`conventions/${encodeURIComponent(target)}`);
    }

    /** Admin only (403 otherwise). */
    public putConventions(target: string, conventions: Conventions): Promise<Conventions> {
        return this.request<Conventions>(`conventions/${encodeURIComponent(target)}`, {
            method: "PUT", ...IdeService.json(conventions)
        });
    }

    // --- Admin ---------------------------------------------------------------
    /** Admin only: every user's session metadata, never content. */
    public listAllSessions(): Promise<AdminSessionRow[]> {
        return this.request<AdminSessionRow[]>("admin/sessions");
    }

    // --- Streaming runs ------------------------------------------------------
    /**
     * Sends a message and streams the run's events to `onEvent`.
     *
     * Rejects with an {@link IdeError} when the server refuses before the
     * stream starts (409 gate, 429 usage, 404) or the network fails. Resolves
     * when the stream ends or `signal` aborts; after an abort no further event
     * reaches `onEvent`. A stream that ends without a `done` frame gets a
     * synthetic `error` event with code `stream_incomplete`. A login page or
     * redirect instead of the stream rejects with `session_expired`
     * (`isAuth`), any other non-SSE answer with `invalid_response`.
     *
     * Aborting only stops *reading*: the run keeps going on the server, and
     * the session stays `running` until it ends. To stop the run, call
     * {@link cancel} after aborting.
     */
    public streamMessage(sid: string, text: string, onEvent: SseHandler, signal?: AbortSignal): Promise<void> {
        return this.stream(`${IdeService.sid(sid)}/messages`, { text }, onEvent, signal);
    }

    /**
     * As {@link streamMessage}, rerunning the current stage with `feedback`.
     * The same abort caveat applies: call {@link cancel} to stop the run.
     */
    public streamRevise(sid: string, feedback: string, onEvent: SseHandler, signal?: AbortSignal): Promise<void> {
        return this.stream(`${IdeService.sid(sid)}/revise`, { feedback }, onEvent, signal);
    }

    /**
     * Diagnose sessions only (409 `not_diagnose`): writes the `report`
     * artifact and streams the run as {@link streamMessage} does, with the
     * same abort caveat.
     */
    public streamReport(sid: string, onEvent: SseHandler, signal?: AbortSignal): Promise<void> {
        return this.stream(`${IdeService.sid(sid)}/report`, {}, onEvent, signal);
    }

    private async stream(path: string, body: unknown, onEvent: SseHandler, signal?: AbortSignal): Promise<void> {
        if (signal?.aborted) {
            return;
        }
        let response: Response;
        try {
            response = await IdeService.send(path, {
                method: "POST",
                headers: { Accept: "text/event-stream", "Content-Type": "application/json" },
                body: JSON.stringify(body),
                signal
            });
        } catch (e) {
            if (signal?.aborted && !(e instanceof IdeError)) {
                return;
            }
            throw e;
        }
        if (IdeService.isHtml(response)) {
            throw IdeService.sessionExpired(response.status);
        }
        if (!(response.headers.get("Content-Type") ?? "").toLowerCase().startsWith("text/event-stream")) {
            throw new IdeError(response.status, "The server did not answer with an event stream.", "invalid_response");
        }
        if (!response.body) {
            throw new IdeError(response.status, "The response has no body to stream.", "stream_incomplete");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const parser = new SseParser();
        let sawDone = false;
        let ended = false;
        // A stubbed or already-delivered body does not notice the signal by
        // itself; cancelling the reader is what ends a pending read().
        const onAbort = (): void => { reader.cancel().catch(() => undefined); };
        signal?.addEventListener("abort", onAbort);

        const deliver = (events: SseEvent[]): void => {
            for (const event of events) {
                if (signal?.aborted) {
                    return;
                }
                if (event.type === "done") {
                    sawDone = true;
                }
                onEvent(event);
            }
        };

        try {
            for (;;) {
                let chunk: ReadableStreamReadResult<Uint8Array>;
                try {
                    chunk = await reader.read();
                } catch (e) {
                    if (signal?.aborted) {
                        return;
                    }
                    throw new IdeError(0, (e as Error).message || "The stream broke off.", "network");
                }
                if (signal?.aborted) {
                    return;
                }
                if (chunk.done) {
                    ended = true;
                    parser.push(decoder.decode());
                    deliver(parser.flush());
                    break;
                }
                deliver(parser.push(decoder.decode(chunk.value, { stream: true })));
                if (signal?.aborted) {
                    return;
                }
            }
            if (!sawDone) {
                onEvent({
                    type: "error",
                    data: { message: "The run's stream ended before it finished.", code: "stream_incomplete" }
                });
            }
        } finally {
            signal?.removeEventListener("abort", onAbort);
            // Anything but a clean end (abort, a throwing onEvent, a broken
            // read) releases the body, so the connection is not held open.
            if (!ended) {
                reader.cancel().catch(() => undefined);
            }
        }
    }
}
