import RunController, { type RunCallbacks, type RunKind, type RunService } from "com/agent/ide/model/RunController";
import RunWatch from "com/agent/ide/model/runWatch";
import type { RunState } from "com/agent/ide/model/chatRun";
import type { SessionDetail, SseEvent } from "com/agent/ide/service/types";

/** The slice of sinon-4's fake timers these tests use. */
interface Clock { tick(ms: number): void; restore(): void }
interface SinonLike { useFakeTimers(): Clock }

let sinon: SinonLike;

/** One scripted stream: the test pushes frames, then ends or breaks it. */
interface Stream {
    kind: string;
    sid: string;
    text: string;
    signal?: AbortSignal;
    emit(event: SseEvent): void;
    end(): void;
    fail(e: unknown): void;
}

/** A fake IdeService: every stream call is handed to the test as a {@link Stream}. */
class FakeService implements RunService {
    public streams: Stream[] = [];
    public cancels: string[] = [];
    public sessions: string[] = [];
    public status: "running" | "idle" = "running";
    public cancelAnswer: () => Promise<unknown> = () => Promise.resolve({});

    public streamMessage(sid: string, text: string, onEvent: (e: SseEvent) => void, signal?: AbortSignal): Promise<void> {
        return this.open("message", sid, text, onEvent, signal);
    }

    public streamReport(sid: string, onEvent: (e: SseEvent) => void, signal?: AbortSignal): Promise<void> {
        return this.open("report", sid, "", onEvent, signal);
    }

    public streamRequestChanges(sid: string, note: string, onEvent: (e: SseEvent) => void, signal?: AbortSignal): Promise<void> {
        return this.open("requestChanges", sid, note, onEvent, signal);
    }

    public cancel(sid: string): Promise<unknown> {
        this.cancels.push(sid);
        return this.cancelAnswer();
    }

    public getSession(sid: string): Promise<SessionDetail> {
        this.sessions.push(sid);
        return Promise.resolve({ id: sid, status: this.status } as SessionDetail);
    }

    private open(kind: string, sid: string, text: string, onEvent: (e: SseEvent) => void, signal?: AbortSignal): Promise<void> {
        return new Promise<void>((resolve, reject) => {
            // Like the real service: an abort ends the read quietly and no frame follows it.
            signal?.addEventListener("abort", () => resolve());
            this.streams.push({
                kind, sid, text, signal,
                emit: (event) => { if (!signal?.aborted) { onEvent(event); } },
                end: resolve,
                fail: reject
            });
        });
    }
}

/** Records every callback, in order, as a short string. */
class Recorder implements RunCallbacks {
    public log: string[] = [];
    public renders: RunState[] = [];
    public files: string[][] = [];
    public finishAnswer: () => Promise<void> = () => Promise.resolve();
    public filesAnswer: () => Promise<void> = () => Promise.resolve();

    public onEvent(e: SseEvent): void {
        this.log.push(`event:${e.type}`);
    }

    public onRender(state: RunState): void {
        this.renders.push(state);
        this.log.push("render");
    }

    public onFilesChanged(paths: string[]): Promise<void> {
        this.files.push(paths);
        this.log.push(`files:${paths.join(",")}`);
        return this.filesAnswer();
    }

    public onRefused(e: unknown, kind: RunKind, text: string): void {
        this.log.push(`refused:${kind}:${text}:${(e as Error).message}`);
    }

    public onStreamBroken(e: unknown): void {
        this.log.push(`broken:${(e as Error).message}`);
    }

    public onFinished(sid: string): Promise<void> {
        this.log.push(`finished:${sid}`);
        return this.finishAnswer();
    }

    public onWatchFailed(sid: string, e: unknown): void {
        this.log.push(`gaveUp:${sid}:${(e as Error).message}`);
    }
}

/** Lets promise chains settle (fake timers do not touch microtasks). */
async function settle(rounds = 10): Promise<void> {
    for (let i = 0; i < rounds; i++) {
        await Promise.resolve();
    }
}

const run = (id = "r-1"): SseEvent => ({ type: "run", data: { run_id: id, stage: "design", message_id: "m-1" } });
const text = (delta: string): SseEvent => ({ type: "text", data: { delta } });
const done = (): SseEvent => ({ type: "done", data: { message_id: "m-1", stage: "design", status: "idle" } });
const file = (path: string): SseEvent => ({ type: "file", data: { path, state: "modified" } });

interface Ctx { clock: Clock; service: FakeService; calls: Recorder; runs: RunController }

QUnit.module("RunController", {
    before: function () {
        return new Promise<void>((resolve) => {
            sap.ui.require(["sap/ui/thirdparty/sinon-4"], function (lib: SinonLike) {
                sinon = lib;
                resolve();
            });
        });
    },
    beforeEach: function (this: Ctx) {
        this.clock = sinon.useFakeTimers();
        this.service = new FakeService();
        this.calls = new Recorder();
        this.runs = new RunController(this.service, this.calls);
    },
    afterEach: function (this: Ctx) {
        this.runs.dispose();
        this.clock.restore();
    }
});

QUnit.test("frames reach onEvent in order; the end of the stream calls onFinished once", async function (this: Ctx, assert) {
    const started = this.runs.start("s-1", "message", "hi");
    assert.ok(this.runs.running, "running from the start call on");
    await settle();
    const stream = this.service.streams[0];
    assert.strictEqual(stream.kind, "message");
    assert.strictEqual(stream.text, "hi");
    stream.emit(run());
    stream.emit(text("Hello"));
    stream.emit(done());
    assert.strictEqual(this.runs.state.text, "Hello", "the run state folds the frames");
    stream.end();
    await started;
    assert.deepEqual(this.calls.log,
        ["event:run", "event:text", "event:done", "render", "finished:s-1"],
        "every frame in order, done renders at once, then the clean-up");
    assert.notOk(this.runs.running, "no longer running");
});

QUnit.test("each kind streams through its own service call", async function (this: Ctx, assert) {
    for (const kind of ["message", "report", "requestChanges"] as RunKind[]) {
        const started = this.runs.start("s-1", kind, "note");
        await settle();
        const stream = this.service.streams[this.service.streams.length - 1];
        assert.strictEqual(stream.kind, kind, `${kind} uses its own stream`);
        stream.end();
        await started;
    }
});

QUnit.test("a second start while a run of ours is in flight is ignored (one stream)", async function (this: Ctx, assert) {
    const first = this.runs.start("s-1", "requestChanges", "note");
    const second = await this.runs.start("s-1", "requestChanges", "note");
    await settle();
    assert.strictEqual(this.service.streams.length, 1, "one stream only");
    assert.strictEqual(second, false, "the second start did nothing");
    this.service.streams[0].end();
    await first;
});

QUnit.test("a refusal before the stream calls onRefused and never onFinished", async function (this: Ctx, assert) {
    const started = this.runs.start("s-1", "requestChanges", "do it again");
    await settle();
    this.service.streams[0].fail(new Error("run_in_progress"));
    await started;
    assert.deepEqual(this.calls.log, ["refused:requestChanges:do it again:run_in_progress"]);
    assert.notOk(this.runs.running, "not running after the refusal");
});

QUnit.test("a stream that breaks after the run started calls onStreamBroken, then onFinished", async function (this: Ctx, assert) {
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    this.service.streams[0].fail(new Error("network"));
    await started;
    assert.deepEqual(this.calls.log, ["event:run", "broken:network", "finished:s-1"]);
});

QUnit.test("stop calls cancel once, aborts the read and calls onFinished once, after the cancel", async function (this: Ctx, assert) {
    let answer!: () => void;
    this.service.cancelAnswer = () => new Promise((resolve) => { answer = () => resolve({}); });
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    const stream = this.service.streams[0];
    stream.emit(run());
    const stopped = this.runs.stop("s-1");
    assert.ok(this.runs.stopping, "stopping until the cancel answered");
    assert.ok(stream.signal?.aborted, "the read was aborted");
    void this.runs.stop("s-1");
    assert.deepEqual(this.service.cancels, ["s-1"], "a second stop sends nothing");
    await settle();
    assert.notOk(this.calls.log.includes("finished:s-1"), "the clean-up waits for the cancel");
    answer();
    await stopped;
    await started;
    assert.deepEqual(this.calls.log.filter((l) => l === "finished:s-1"), ["finished:s-1"], "onFinished once");
    assert.notOk(this.runs.stopping, "the stop is over");
});

QUnit.test("stop without a stream of ours (a watched run) cleans up once the cancel answered", async function (this: Ctx, assert) {
    await this.runs.stop("s-2");
    await settle();
    assert.deepEqual(this.service.cancels, ["s-2"]);
    assert.deepEqual(this.calls.log, ["finished:s-2"]);
});

QUnit.test("a failed cancel rejects stop() but the clean-up still runs", async function (this: Ctx, assert) {
    this.service.cancelAnswer = () => Promise.reject(new Error("run_on_other_instance"));
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    await this.runs.stop("s-1").then(() => assert.ok(false, "should reject"), (e: Error) => {
        assert.strictEqual(e.message, "run_on_other_instance", "the caller gets the refusal");
    });
    await started;
    assert.deepEqual(this.calls.log, ["event:run", "finished:s-1"]);
});

QUnit.test("detach aborts the read without a cancel; nothing of the old run is called back", async function (this: Ctx, assert) {
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    const stream = this.service.streams[0];
    stream.emit(run());
    stream.emit(text("partial"));
    this.runs.detach();
    assert.ok(stream.signal?.aborted, "the read was aborted");
    assert.notOk(this.runs.running, "not running after a detach");
    assert.strictEqual(this.runs.state.text, "", "the run state is reset");
    stream.emit(text("late"));
    await started;
    this.clock.tick(1000);
    await settle();
    assert.deepEqual(this.service.cancels, [], "no cancel: the run goes on server-side");
    assert.deepEqual(this.calls.log, ["event:run", "event:text"], "no render, no onFinished after the detach");
});

QUnit.test("render throttling coalesces 50 text frames into at most 3 onRender calls", async function (this: Ctx, assert) {
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    const stream = this.service.streams[0];
    stream.emit(run());
    for (let i = 0; i < 50; i++) {
        stream.emit(text(`${i} `));
        this.clock.tick(2);
    }
    this.clock.tick(300);
    const renders = this.calls.renders.length;
    assert.ok(renders >= 1 && renders <= 3, `${renders} renders for 50 frames`);
    assert.ok(this.calls.renders[renders - 1].text.startsWith("0 1 2"), "the render sees the folded text");
    assert.ok(this.calls.renders[renders - 1].text.endsWith("49 "), "the last render has every frame");
    stream.end();
    await started;
});

QUnit.test("file frames are reloaded together, one loop at a time; onFinished can wait for it", async function (this: Ctx, assert) {
    let release!: () => void;
    this.calls.filesAnswer = () => new Promise((resolve) => { release = resolve; });
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    const stream = this.service.streams[0];
    stream.emit(run());
    stream.emit(file("a"));
    stream.emit(file("b"));
    await settle();
    assert.deepEqual(this.calls.files, [["a", "b"]], "one round for a chunk");
    stream.emit(file("c"));
    await settle();
    assert.strictEqual(this.calls.files.length, 1, "a path during a round waits for the next one");
    this.calls.filesAnswer = () => Promise.resolve();
    let settled = false;
    void this.runs.settleFiles().then(() => { settled = true; });
    release();
    await settle();
    assert.deepEqual(this.calls.files, [["a", "b"], ["c"]], "the next round picked it up");
    assert.ok(settled, "settleFiles resolves when the loop ran dry");
    stream.end();
    await started;
});

QUnit.test("watch polls a run started elsewhere and finishes it when it ends", async function (this: Ctx, assert) {
    this.runs.watch({ id: "s-3", status: "running" } as SessionDetail);
    this.clock.tick(RunWatch.intervalMs);
    await settle();
    assert.deepEqual(this.service.sessions, ["s-3"], "polled once");
    assert.deepEqual(this.calls.log, [], "still running");
    this.service.status = "idle";
    this.clock.tick(RunWatch.intervalMs);
    await settle();
    assert.deepEqual(this.calls.log, ["finished:s-3"], "its end reloads like a run of ours");
    this.clock.tick(RunWatch.intervalMs * 2);
    await settle();
    assert.strictEqual(this.service.sessions.length, 2, "no poll after the end");
});

QUnit.test("watch does nothing while a run of ours streams, and stops for an idle session", async function (this: Ctx, assert) {
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.runs.watch({ id: "s-1", status: "running" } as SessionDetail);
    this.clock.tick(RunWatch.intervalMs);
    await settle();
    assert.deepEqual(this.service.sessions, [], "no poll beside our own stream");
    this.service.streams[0].end();
    await started;
    this.runs.watch({ id: "s-1", status: "idle" } as SessionDetail);
    this.clock.tick(RunWatch.intervalMs);
    await settle();
    assert.deepEqual(this.service.sessions, [], "an idle session is not watched");
});

QUnit.test("a failed onFinished re-arms the watch, so the next poll retries", async function (this: Ctx, assert) {
    this.calls.finishAnswer = () => Promise.reject(new Error("reload failed"));
    this.service.status = "idle";
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    this.service.streams[0].end();
    await started;
    this.calls.finishAnswer = () => Promise.resolve();
    this.clock.tick(RunWatch.intervalMs);
    await settle();
    assert.deepEqual(this.service.sessions, ["s-1"], "the watch polled after the failure");
    assert.deepEqual(this.calls.log.filter((l) => l.startsWith("finished")), ["finished:s-1", "finished:s-1"],
        "the poll ran the clean-up again");
});

QUnit.test("a reload after a run that fails as signed out is reported at once, not watched", async function (this: Ctx, assert) {
    const signedOut = Object.assign(new Error("expired"), { status: 401, isAuth: true });
    this.calls.finishAnswer = () => Promise.reject(signedOut);
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    this.service.streams[0].end();
    await started;
    assert.deepEqual(this.calls.log.filter((l) => !l.startsWith("event") && l !== "render"),
        ["finished:s-1", "gaveUp:s-1:expired"], "the page is told at once");
    this.clock.tick(RunWatch.intervalMs * 3);
    await settle();
    assert.deepEqual(this.service.sessions, [], "no poll: a signed-out poll would fail the same way");
});

QUnit.test("a watched run whose reload keeps failing is given up after RunWatch.maxFailures polls", async function (this: Ctx, assert) {
    this.calls.finishAnswer = () => Promise.reject(new Error("reload failed"));
    this.service.status = "idle";
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    this.service.streams[0].end();
    await started;
    for (let i = 0; i < RunWatch.maxFailures + 3; i++) {
        this.clock.tick(RunWatch.intervalMs);
        await settle();
    }
    assert.strictEqual(this.service.sessions.length, RunWatch.maxFailures, "polled maxFailures times, then stopped");
    assert.deepEqual(this.calls.log.filter((l) => l.startsWith("gaveUp")), ["gaveUp:s-1:reload failed"],
        "the page is told once");
});

QUnit.test("a watched session whose poll answers 401 is given up at once", async function (this: Ctx, assert) {
    const signedOut = Object.assign(new Error("expired"), { status: 401, isAuth: true });
    this.service.getSession = (sid: string) => { this.service.sessions.push(sid); return Promise.reject(signedOut); };
    this.runs.watch({ id: "s-3", status: "running" } as SessionDetail);
    this.clock.tick(RunWatch.intervalMs * 4);
    await settle();
    assert.deepEqual(this.service.sessions, ["s-3"], "one poll");
    assert.deepEqual(this.calls.log, ["gaveUp:s-3:expired"]);
});

// --- U3 review follow-ups (Task U7) -----------------------------------------

QUnit.test("detach clears a pending Stop: Stop works again in the next session before the old cancel answers", async function (this: Ctx, assert) {
    let answerOld!: () => void;
    this.service.cancelAnswer = () => new Promise((resolve) => { answerOld = () => resolve({}); });
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    void this.runs.stop("s-1");
    assert.ok(this.runs.stopping, "the Stop of s-1 is on its way");
    this.runs.detach();
    assert.notOk(this.runs.stopping, "a detach forgets the old session's Stop");
    this.service.cancelAnswer = () => Promise.resolve({});
    // Not awaited: before the fix this was the old, still pending Stop.
    void this.runs.stop("s-2");
    await settle(20);
    assert.deepEqual(this.service.cancels, ["s-1", "s-2"], "the Stop of s-2 is sent, not swallowed by the old one");
    assert.ok(this.calls.log.includes("finished:s-2"), "and s-2 is cleaned up");
    answerOld();
    await started;
    await settle();
    assert.notOk(this.calls.log.includes("finished:s-1"), "the old session is never called back");
});

QUnit.test("dispose silences a late finish: no callback reaches a destroyed page", async function (this: Ctx, assert) {
    let answer!: () => void;
    this.service.cancelAnswer = () => new Promise((resolve) => { answer = () => resolve({}); });
    const started = this.runs.start("s-1", "message", "hi");
    await settle();
    this.service.streams[0].emit(run());
    void this.runs.stop("s-1");
    this.runs.dispose();
    answer();
    await started;
    await settle();
    assert.deepEqual(this.calls.log, ["event:run"], "no onFinished after dispose");
});

QUnit.test("a file-drain loop of the old session stops after detach; the new session's file starts its own", async function (this: Ctx, assert) {
    const releases: (() => void)[] = [];
    this.calls.filesAnswer = () => new Promise((resolve) => { releases.push(resolve); });
    const first = this.runs.start("s-1", "message", "hi");
    await settle();
    const old = this.service.streams[0];
    old.emit(run());
    old.emit(file("src/a"));
    await settle();
    assert.deepEqual(this.calls.files, [["src/a"]], "the first round of s-1 runs");
    old.emit(file("src/b"));
    this.runs.detach();
    releases[0]();
    await settle();
    assert.deepEqual(this.calls.files, [["src/a"]], "the stale loop does not go on with s-1's next path");
    const second = this.runs.start("s-2", "message", "hi");
    await settle();
    const fresh = this.service.streams[1];
    fresh.emit(run("r-2"));
    fresh.emit(file("src/c"));
    await settle();
    assert.deepEqual(this.calls.files, [["src/a"], ["src/c"]], "s-2's file is reloaded by a loop of its own");
    releases[1]();
    fresh.end();
    old.end();
    await Promise.all([first, second]);
});
