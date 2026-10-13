import ActivityState from "./activity";
import { newRun, reduceRun, type RunState } from "./chatRun";
import RunWatch, { isAuthFailure } from "./runWatch";
import type { SessionDetail, SseEvent } from "../service/types";

/** What a run is: a chat message, a request-changes round or a diagnose report. */
export type RunKind = "message" | "requestChanges" | "report";

type SseHandler = (event: SseEvent) => void;

/**
 * The slice of IdeService a run needs. IdeService satisfies it as it is;
 * `streamRequestChanges` is optional until the service has it (a
 * request-changes run without it is refused like any refusal before the
 * stream).
 */
export interface RunService {
    streamMessage(sid: string, text: string, onEvent: SseHandler, signal?: AbortSignal): Promise<void>;
    streamReport(sid: string, onEvent: SseHandler, signal?: AbortSignal): Promise<void>;
    streamRequestChanges?(sid: string, note: string, onEvent: SseHandler, signal?: AbortSignal): Promise<void>;
    cancel(sid: string): Promise<unknown>;
    getSession(sid: string): Promise<SessionDetail>;
}

/**
 * What the page does with a run. Every callback of a run comes only while
 * that run still belongs to the page: a {@link RunController.detach} (a
 * session switch) silences the old run's callbacks, so the page can read its
 * own current session in them.
 */
export interface RunCallbacks {
    /** Every frame, in order, after the run state and the activity folded it. */
    onEvent(e: SseEvent): void;
    /** The streamed answer to re-render: throttled (animation frame, >= 100 ms apart); `done` renders at once. */
    onRender(state: RunState): void;
    /** The paths of `file` frames, reloaded together; one call at a time. A rejection is ignored (show it yourself). */
    onFilesChanged(paths: string[]): Promise<void>;
    /** Refused before the stream started (no `run` frame): no onFinished follows, unless a Stop was pending. */
    onRefused(e: unknown, kind: RunKind, text: string): void;
    /** The stream broke after the run started; onFinished follows. */
    onStreamBroken(e: unknown): void;
    /**
     * After a run (done, stopped, broken, or a watched run that ended):
     * reload what it changed. A rejection means the reload failed: the
     * session is watched so the next poll calls this again.
     */
    onFinished(sid: string): Promise<void>;
    /**
     * The reload after a run failed for good: an auth failure (at once) or
     * {@link RunWatch.maxFailures} failed polls in a row. Nothing retries it;
     * the page stops showing the run as live and tells the user.
     */
    onWatchFailed?(sid: string, e: unknown): void;
    /** Awaited inside the run before the stream opens (e.g. a lazy library); a failure counts as a refusal. */
    onBeforeStream?(): Promise<void>;
}

/** At most one re-parse of the streamed markdown per this many ms (it re-parses the whole answer). */
const RENDER_INTERVAL_MS = 100;

/**
 * The run lifecycle of one page, without any UI: starts a run and reads its
 * SSE stream, folds the frames into a {@link RunState} and the
 * {@link ActivityState}, throttles the re-render, reloads changed files in
 * one loop, stops a run (cancel route + abort), watches a run this page does
 * not stream (model/runWatch) and cleans up after a run.
 *
 * Race guards: `generation` is bumped by {@link detach} (the page's session
 * switch), and a run, a file loop, a poll or a clean-up answers only for its
 * own generation. `stopping` is the cancel of a Stop; the clean-up waits for
 * it.
 */
export default class RunController {

    /** Bumped by every detach(): what started before belongs to another session. */
    private generation = 0;
    /** Aborts reading the current run's stream (Stop, or a session switch). */
    private runAbort?: AbortController;
    /** A run this page started is streaming. */
    private localRunning = false;
    private run: RunState = newRun();
    /** The activity panel's data: tool timeline and plan of the last run. */
    private activityState = new ActivityState();
    /** The pending animation frame of the throttled stream render. */
    private frame?: number;
    /** The pending wait before that frame, when the last render was too recent. */
    private renderTimer?: ReturnType<typeof setTimeout>;
    /** When the streamed answer was last rendered (performance.now()). */
    private lastRender = 0;
    /** The cancel call of a Stop; the run's clean-up waits for it. */
    private stopCall?: Promise<void>;
    /** Paths named by `file` events, reloaded together. */
    private pendingFiles = new Set<string>();
    /** The loop that reloads `pendingFiles` until none is left (one at a time). */
    private filesRefresh?: Promise<void>;
    /** Polls a session that is running without a stream of this page. */
    private readonly runWatch: RunWatch;

    public constructor(private readonly service: RunService, private readonly callbacks: RunCallbacks) {
        this.runWatch = new RunWatch((sid) => this.pollRun(sid), RunWatch.intervalMs, (sid, e) => {
            this.callbacks.onWatchFailed?.(sid, e);
        });
    }

    /** A run this page started is streaming. */
    public get running(): boolean {
        return this.localRunning;
    }

    /** What the current (or last) run has delivered so far. */
    public get state(): RunState {
        return this.run;
    }

    /** The plan and tool timeline of the current run, or the stored one the page loaded into it. */
    public get activity(): ActivityState {
        return this.activityState;
    }

    /** A Stop's cancel call is on its way. */
    public get stopping(): boolean {
        return !!this.stopCall;
    }

    /** Resolves when the reload loop of the `file` frames ran dry (at once when none runs). */
    public settleFiles(): Promise<void> {
        return this.filesRefresh ?? Promise.resolve();
    }

    /**
     * Starts a run and follows its stream to the end. The caller checks the
     * gates first; from this call on {@link running} is true. Resolves when
     * the run and its clean-up are over: true when the clean-up ran for it
     * (not for a plain refusal, nor after a detach).
     */
    public async start(sid: string, kind: RunKind, text: string): Promise<boolean> {
        if (this.localRunning) {
            // A run of ours is in flight (a double press): one run, one stream.
            return false;
        }
        this.runWatch.stop();
        const generation = this.generation;
        this.run = newRun();
        this.activityState = new ActivityState();
        this.localRunning = true;
        const abort = new AbortController();
        this.runAbort = abort;
        let refused = false;
        let refusal: unknown;
        let finished = false;
        try {
            await this.callbacks.onBeforeStream?.();
            const onEvent = (event: SseEvent): void => this.onRunEvent(generation, event);
            await this.stream(sid, kind, text, onEvent, abort.signal);
        } catch (e) {
            if (this.run.runId) {
                // The run had started; the stream broke.
                if (generation === this.generation) {
                    this.callbacks.onStreamBroken(e);
                }
            } else {
                // Refused before the stream started (409/429/404/auth).
                refused = true;
                refusal = e;
            }
        } finally {
            if (this.runAbort === abort) {
                this.runAbort = undefined;
            }
            if (generation === this.generation) {
                this.localRunning = false;
                if (refused) {
                    this.callbacks.onRefused(refusal, kind, text);
                }
                if (!refused || this.stopCall) {
                    await this.finish(sid, generation);
                    finished = true;
                }
            }
        }
        return finished;
    }

    /**
     * Stops the run: stops reading the stream, then asks the server to
     * cancel it (aborting alone leaves the run going). A run without a
     * stream of ours (started elsewhere) is cleaned up once the cancel
     * answered. The promise is the cancel's answer, for the page's message;
     * the clean-up runs either way. Does nothing while a Stop is on its way.
     */
    public stop(sid: string): Promise<void> {
        if (this.stopCall) {
            return this.stopCall;
        }
        const generation = this.generation;
        const hadStream = !!this.runAbort;
        const cancel = this.service.cancel(sid).then(() => undefined);
        this.stopCall = cancel.then(() => undefined, () => undefined);
        this.runAbort?.abort();
        if (!hadStream) {
            void this.stopCall.then(() => this.finish(sid, generation));
        }
        return cancel;
    }

    /**
     * Watches the session while the server says it is running and no stream
     * of this page follows it (left by a session switch, started before a
     * reload or in another tab); stops otherwise.
     */
    public watch(detail: SessionDetail | null): void {
        if (detail?.status === "running" && !this.localRunning && !this.runAbort) {
            this.runWatch.watch(detail.id);
        } else {
            this.runWatch.stop();
        }
    }

    /**
     * A session switch: stops reading the old session's stream (its run goes
     * on server-side), stops the watch, resets the run state and silences
     * every callback of what started before.
     */
    public detach(): void {
        this.generation++;
        this.runAbort?.abort();
        this.runAbort = undefined;
        this.localRunning = false;
        this.runWatch.stop();
        this.run = newRun();
        this.activityState = new ActivityState();
        this.pendingFiles.clear();
        // The old session's reload loop checks the generation and ends by
        // itself; the new session's first file event starts its own.
        this.filesRefresh = undefined;
        // A Stop of the old session is not this session's: without this, a
        // Stop here would answer with the old, still pending cancel. The old
        // run's finish() checks the generation and calls nobody back.
        this.stopCall = undefined;
        this.cancelRender();
    }

    /**
     * The page goes away: stop the poll, the stream read and the render
     * timers, and silence everything still on its way (a late finish of a
     * Stop must not call into a destroyed view).
     */
    public dispose(): void {
        this.generation++;
        this.runWatch.stop();
        this.runAbort?.abort();
        this.runAbort = undefined;
        this.localRunning = false;
        this.stopCall = undefined;
        this.pendingFiles.clear();
        this.filesRefresh = undefined;
        this.cancelRender();
    }

    private stream(sid: string, kind: RunKind, text: string, onEvent: SseHandler, signal: AbortSignal): Promise<void> {
        switch (kind) {
            case "message":
                return this.service.streamMessage(sid, text, onEvent, signal);
            case "report":
                return this.service.streamReport(sid, onEvent, signal);
            default:
                if (!this.service.streamRequestChanges) {
                    return Promise.reject(new Error("request-changes is not available"));
                }
                return this.service.streamRequestChanges(sid, text, onEvent, signal);
        }
    }

    /**
     * After a run (done, stopped, broken or polled to its end): waits for a
     * pending Stop, then lets the page reload. True when the reload worked;
     * when it failed, the session is watched so the next poll retries, unless
     * it failed as signed out (then the page is told at once). Called by a
     * poll (`fromPoll`), a failure is thrown to the watch, which counts it.
     */
    private async finish(sid: string, generation: number, fromPoll = false): Promise<boolean> {
        const stopCall = this.stopCall;
        if (stopCall) {
            await stopCall;
            if (this.stopCall === stopCall) {
                this.stopCall = undefined;
            }
        }
        this.cancelRender();
        if (generation !== this.generation) {
            return false;
        }
        try {
            await this.callbacks.onFinished(sid);
            return true;
        } catch (e) {
            if (generation !== this.generation) {
                return false;
            }
            if (fromPoll) {
                throw e;
            }
            if (isAuthFailure(e)) {
                this.runWatch.stop();
                this.callbacks.onWatchFailed?.(sid, e);
            } else {
                this.runWatch.watch(sid);
            }
            return false;
        }
    }

    private onRunEvent(generation: number, event: SseEvent): void {
        if (generation !== this.generation) {
            return;
        }
        this.run = reduceRun(this.run, event);
        if (event.type === "tool" || event.type === "plan") {
            this.activityState.apply(event);
        }
        this.callbacks.onEvent(event);
        switch (event.type) {
            case "tool":
            case "plan":
            case "text":
                this.scheduleRender();
                break;
            case "file":
                this.pendingFiles.add(event.data.path);
                this.filesRefresh ??= this.drainChangedFiles(generation);
                break;
            case "done":
                this.render();
                break;
            default:
                break;
        }
    }

    /**
     * Re-renders the streamed answer at most once per animation frame and
     * per RENDER_INTERVAL_MS: each render re-parses the whole answer.
     */
    private scheduleRender(): void {
        if (this.frame !== undefined || this.renderTimer !== undefined) {
            return;
        }
        const wait = this.lastRender + RENDER_INTERVAL_MS - performance.now();
        const frame = (): void => {
            this.frame = requestAnimationFrame(() => {
                this.frame = undefined;
                this.render();
            });
        };
        if (wait > 0) {
            this.renderTimer = setTimeout(() => {
                this.renderTimer = undefined;
                frame();
            }, wait);
        } else {
            frame();
        }
    }

    private cancelRender(): void {
        if (this.frame !== undefined) {
            cancelAnimationFrame(this.frame);
            this.frame = undefined;
        }
        if (this.renderTimer !== undefined) {
            clearTimeout(this.renderTimer);
            this.renderTimer = undefined;
        }
    }

    private render(): void {
        this.lastRender = performance.now();
        this.callbacks.onRender(this.run);
    }

    /**
     * Reloads the paths of `file` events until none is left: a path that
     * arrives while a reload is in flight is picked up by the next round
     * instead of being lost. A failed round is the page's to show; the loop
     * goes on with the paths that arrived meanwhile (the failed round's own
     * paths are not retried, so a lasting failure cannot spin). Clears
     * `filesRefresh` in the same turn as the last empty check, so the next
     * event always starts a new loop.
     */
    private drainChangedFiles(generation: number): Promise<void> {
        let loop: Promise<void> | undefined;
        loop = (async () => {
            // Events of one chunk arrive together: let them all land first.
            await Promise.resolve();
            try {
                while (this.pendingFiles.size && generation === this.generation) {
                    const paths = [...this.pendingFiles];
                    this.pendingFiles.clear();
                    try {
                        await this.callbacks.onFilesChanged(paths);
                    } catch {
                        // The page shows its own errors (onFilesChanged).
                    }
                }
            } finally {
                if (this.filesRefresh === loop) {
                    this.filesRefresh = undefined;
                }
            }
        })();
        return loop;
    }

    /** One poll of {@link watch}: true while the run goes on; its end cleans up like a run of ours. */
    private async pollRun(sid: string): Promise<boolean> {
        const generation = this.generation;
        const detail = await this.service.getSession(sid);
        if (generation !== this.generation || this.localRunning || this.runAbort) {
            return false;
        }
        if (detail.status === "running") {
            return true;
        }
        // A failed reload is thrown to the watch: it retries, and gives up on
        // an auth failure or after RunWatch.maxFailures failures in a row.
        await this.finish(sid, generation, true);
        return false;
    }
}
