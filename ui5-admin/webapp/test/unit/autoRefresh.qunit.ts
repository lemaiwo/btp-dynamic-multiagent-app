import Poller, {
    clockText, isLiveStatus, nextDelay, statusLine
} from "com/agent/admin/model/autoRefresh";

const INTERVALS = { intervalMs: 3000, slowIntervalMs: 10000 };

QUnit.module("autoRefresh: which statuses are still moving");

QUnit.test("running and the not-yet-started statuses are live", function (assert) {
    assert.ok(isLiveStatus("running"));
    assert.ok(isLiveStatus("pending"));
    assert.ok(isLiveStatus("queued"));
    assert.ok(isLiveStatus("RUNNING"), "case does not matter");
});

QUnit.test("every terminal status stops the polling", function (assert) {
    ["success", "failed", "interrupted", "degraded", "partial", "cancelled", "skipped"].forEach((s) => {
        assert.notOk(isLiveStatus(s), `${s} is terminal`);
    });
});

QUnit.test("no status yet counts as live, so the first poll happens", function (assert) {
    assert.ok(isLiveStatus(""));
    assert.ok(isLiveStatus(undefined));
    assert.ok(isLiveStatus(null));
});

QUnit.module("autoRefresh: the next delay");

QUnit.test("a live run polls at the normal interval, a failed poll backs off, a finished run stops", function (assert) {
    assert.strictEqual(nextDelay("live", INTERVALS), 3000);
    assert.strictEqual(nextDelay("failed", INTERVALS), 10000);
    assert.strictEqual(nextDelay("done", INTERVALS), null);
});

QUnit.module("autoRefresh: the status line");

QUnit.test("names the interval while polling", function (assert) {
    assert.strictEqual(
        statusLine({ enabled: true, live: true, failed: false, lastRefreshed: "12:01:07", intervals: INTERVALS }),
        "Auto-refreshing every 3 s"
    );
    assert.strictEqual(
        statusLine({ enabled: true, live: true, failed: false, lastRefreshed: "", intervals: { intervalMs: 1500, slowIntervalMs: 10000 } }),
        "Auto-refreshing every 1.5 s"
    );
});

QUnit.test("says it stopped once the run finished, with the last refresh time", function (assert) {
    assert.strictEqual(
        statusLine({ enabled: true, live: false, failed: false, lastRefreshed: "12:01:07", intervals: INTERVALS }),
        "Finished, auto-refresh stopped · Last refreshed 12:01:07"
    );
    assert.strictEqual(
        statusLine({ enabled: true, live: false, failed: false, lastRefreshed: "", intervals: INTERVALS }),
        "Finished, auto-refresh stopped"
    );
});

QUnit.test("says it is off when the toggle is off, whatever the run is doing", function (assert) {
    assert.strictEqual(
        statusLine({ enabled: false, live: true, failed: false, lastRefreshed: "12:01:07", intervals: INTERVALS }),
        "Auto-refresh off · Last refreshed 12:01:07"
    );
});

QUnit.test("names the slower retry interval after a failed poll", function (assert) {
    assert.strictEqual(
        statusLine({ enabled: true, live: true, failed: true, lastRefreshed: "12:01:07", intervals: INTERVALS }),
        "Last refresh failed, retrying every 10 s · Last refreshed 12:01:07"
    );
});

QUnit.test("clockText pads to HH:MM:SS", function (assert) {
    assert.strictEqual(clockText(new Date(2026, 0, 1, 9, 5, 3)), "09:05:03");
    assert.strictEqual(clockText(new Date(2026, 0, 1, 23, 59, 59)), "23:59:59");
});

type PollerFixture = { saved: { intervalMs: number; slowIntervalMs: number } };

QUnit.module("autoRefresh: Poller", {
    beforeEach: function (this: PollerFixture) {
        this.saved = { intervalMs: Poller.intervalMs, slowIntervalMs: Poller.slowIntervalMs };
        Poller.intervalMs = 10;
        Poller.slowIntervalMs = 20;
    },
    afterEach: function (this: PollerFixture) {
        Poller.intervalMs = this.saved.intervalMs;
        Poller.slowIntervalMs = this.saved.slowIntervalMs;
    }
});

QUnit.test("ticks until the tick reports done, then is inactive", function (assert) {
    const done = assert.async();
    let ticks = 0;
    const poller = new Poller(async () => {
        ticks++;
        return ticks < 3 ? "live" : "done";
    });
    poller.start();
    assert.ok(poller.isActive(), "active right after start");
    setTimeout(() => {
        assert.strictEqual(ticks, 3, "polled until the run finished, then stopped");
        assert.notOk(poller.isActive(), "inactive once done");
        done();
    }, 120);
});

QUnit.test("never overlaps ticks and stop() arms no further timer", function (assert) {
    const done = assert.async();
    let inFlight = 0;
    let overlapped = false;
    let ticks = 0;
    const poller = new Poller(() => new Promise((resolve) => {
        ticks++;
        inFlight++;
        if (inFlight > 1) {
            overlapped = true;
        }
        setTimeout(() => {
            inFlight--;
            resolve("live");
        }, 25);
    }));
    poller.start();
    setTimeout(() => {
        poller.stop();
        const ticksAtStop = ticks;
        setTimeout(() => {
            assert.notOk(overlapped, "a tick never started while another was in flight");
            assert.ok(ticksAtStop >= 1, "it did poll before stop()");
            assert.strictEqual(ticks, ticksAtStop, "no tick after stop()");
            done();
        }, 80);
    }, 60);
});

QUnit.test("a tick that throws counts as failed and the poller keeps going", function (assert) {
    const done = assert.async();
    let ticks = 0;
    const poller = new Poller(async () => {
        ticks++;
        if (ticks === 1) {
            throw new Error("boom");
        }
        return "done";
    });
    poller.start();
    setTimeout(() => {
        assert.strictEqual(ticks, 2, "polled again after the failure, at the slow interval");
        done();
    }, 80);
});

QUnit.test("starting with a done outcome polls nothing", function (assert) {
    const done = assert.async();
    let ticks = 0;
    const poller = new Poller(async () => { ticks++; return "done"; });
    poller.start("done");
    assert.notOk(poller.isActive());
    setTimeout(() => {
        assert.strictEqual(ticks, 0);
        done();
    }, 40);
});
