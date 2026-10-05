import RunWatch from "com/agent/ide/model/runWatch";

/** The slice of sinon-4's fake timers these tests use. */
interface Clock { tick(ms: number): void; restore(): void }
interface SinonLike { useFakeTimers(): Clock }

let sinon: SinonLike;

interface Ctx { clock: Clock }

QUnit.module("runWatch", {
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
    },
    afterEach: function (this: Ctx) {
        this.clock.restore();
    }
});

/** Lets the poll's promise chain settle (fake timers do not touch microtasks). */
async function settle(): Promise<void> {
    for (let i = 0; i < 5; i++) {
        await Promise.resolve();
    }
}

QUnit.test("polls every interval while the poll says the run goes on, then stops", async function (this: Ctx, assert) {
    const answers = [true, true, false];
    const polled: string[] = [];
    const watch = new RunWatch((sid) => {
        polled.push(sid);
        return Promise.resolve(answers.shift() ?? false);
    }, 3000);

    watch.watch("s-1");
    assert.ok(watch.isWatching("s-1"), "watching s-1");
    this.clock.tick(2999);
    assert.deepEqual(polled, [], "nothing before the first interval");
    this.clock.tick(1);
    await settle();
    assert.deepEqual(polled, ["s-1"], "first poll after one interval");
    this.clock.tick(3000);
    await settle();
    this.clock.tick(3000);
    await settle();
    assert.deepEqual(polled, ["s-1", "s-1", "s-1"], "polled until the run ended");
    assert.notOk(watch.isWatching("s-1"), "no longer watching once the poll said it ended");
    this.clock.tick(10000);
    await settle();
    assert.strictEqual(polled.length, 3, "no poll after the end");
});

QUnit.test("watch() for the session already watched does not start a second timer", async function (this: Ctx, assert) {
    let count = 0;
    const watch = new RunWatch(() => { count++; return Promise.resolve(true); }, 1000);
    watch.watch("s-1");
    watch.watch("s-1");
    watch.watch("s-1");
    this.clock.tick(1000);
    await settle();
    assert.strictEqual(count, 1, "one poll per interval");
    watch.stop();
});

QUnit.test("stop() and a switch to another session cancel the pending poll", async function (this: Ctx, assert) {
    const polled: string[] = [];
    const watch = new RunWatch((sid) => { polled.push(sid); return Promise.resolve(true); }, 1000);
    watch.watch("s-1");
    watch.stop();
    this.clock.tick(5000);
    await settle();
    assert.deepEqual(polled, [], "stopped before the first poll");

    watch.watch("s-1");
    watch.watch("s-2");
    this.clock.tick(1000);
    await settle();
    assert.deepEqual(polled, ["s-2"], "only the session now watched is polled");
    watch.stop();
});

QUnit.test("a poll in flight when stop() runs does not re-arm", async function (this: Ctx, assert) {
    let resolvePoll!: (running: boolean) => void;
    let count = 0;
    const watch = new RunWatch(() => {
        count++;
        return new Promise<boolean>((resolve) => { resolvePoll = resolve; });
    }, 1000);
    watch.watch("s-1");
    this.clock.tick(1000);
    await settle();
    assert.strictEqual(count, 1, "the poll started");
    watch.stop();
    resolvePoll(true);
    await settle();
    this.clock.tick(5000);
    await settle();
    assert.strictEqual(count, 1, "the late answer did not schedule another poll");
});

QUnit.test("a failing poll keeps watching (a network blip is not the end of the run)", async function (this: Ctx, assert) {
    let count = 0;
    const watch = new RunWatch(() => {
        count++;
        return count === 1 ? Promise.reject(new Error("offline")) : Promise.resolve(false);
    }, 1000);
    watch.watch("s-1");
    this.clock.tick(1000);
    await settle();
    this.clock.tick(1000);
    await settle();
    assert.strictEqual(count, 2, "polled again after the failure");
    assert.notOk(watch.isWatching("s-1"), "ended by the second answer");
});
