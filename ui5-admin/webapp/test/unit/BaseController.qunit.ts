import BaseController from "com/agent/admin/controller/BaseController";
import type View from "sap/ui/core/mvc/View";

/** Records what `withBusy` does to the view, without needing a real one. */
class ViewStub {
    public busy = false;
    public delay = -1;
    public busyCalls: boolean[] = [];

    public setBusy(value: boolean): this {
        this.busy = value;
        this.busyCalls.push(value);
        return this;
    }

    public setBusyIndicatorDelay(value: number): this {
        this.delay = value;
        return this;
    }
}

/** Concrete subclass: BaseController is abstract and has no view outside a
 *  running app, so the view is stubbed and `withBusy` exposed for the test. */
class TestController extends BaseController {
    public view = new ViewStub();

    public getView(): View {
        return this.view as unknown as View;
    }

    public busyRun<T>(work: () => Promise<T>): Promise<T> {
        return this.withBusy(work);
    }
}

/** A promise plus the handles to settle it, so a test can observe the
 *  in-flight state before letting the work finish. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

QUnit.module("BaseController.withBusy");

QUnit.test("the view is busy while the work is pending and free once it resolves", async function (assert) {
    const controller = new TestController("test");
    const work = deferred<string>();

    const done = controller.busyRun(() => work.promise);
    assert.strictEqual(controller.view.busy, true, "busy while pending");

    work.resolve("loaded");
    assert.strictEqual(await done, "loaded", "the work's value is passed through");
    assert.strictEqual(controller.view.busy, false, "free once resolved");
});

QUnit.test("overlapping loads keep the indicator up until the last one finishes", async function (assert) {
    const controller = new TestController("test");
    const first = deferred<void>();
    const second = deferred<void>();

    const a = controller.busyRun(() => first.promise);
    const b = controller.busyRun(() => second.promise);

    first.resolve();
    await a;
    assert.strictEqual(controller.view.busy, true, "still busy — the second load is in flight");

    second.resolve();
    await b;
    assert.strictEqual(controller.view.busy, false, "free once both finished");
    assert.deepEqual(controller.view.busyCalls, [true, false], "the indicator is not toggled in between");
});

QUnit.test("a failing load clears the indicator and still rejects", async function (assert) {
    const controller = new TestController("test");
    const boom = new Error("backend down");

    let caught: unknown;
    try {
        await controller.busyRun(() => Promise.reject(boom));
    } catch (error) {
        caught = error;
    }

    assert.strictEqual(caught, boom, "the failure reaches the caller");
    assert.strictEqual(controller.view.busy, false, "no indicator left spinning after a failure");
});

QUnit.test("a short delay is applied so fast loads do not flash the indicator", function (assert) {
    const controller = new TestController("test");

    void controller.busyRun(() => Promise.resolve());

    assert.ok(controller.view.delay > 0, "a delay is set");
    assert.ok(controller.view.delay <= 500, "but a short one, so a slow load shows up promptly");
});
