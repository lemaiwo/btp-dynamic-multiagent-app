import ErrorHandler from "com/agent/admin/service/ErrorHandler";
import { AdminError } from "com/agent/admin/service/AdminService";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";

QUnit.module("ErrorHandler.classify");

QUnit.test("401 is a session problem", function (assert) {
    assert.strictEqual(ErrorHandler.classify(new AdminError(401, "no token")), "session");
});

QUnit.test("403 is a missing role, not a lapsed session", function (assert) {
    // Reloading on 403 re-authenticates the same user to the same 403: a
    // non-admin would be stuck in a reload loop.
    assert.strictEqual(ErrorHandler.classify(new AdminError(403, "forbidden")), "forbidden");
});

QUnit.test("409 is a conflict, not a failure", function (assert) {
    assert.strictEqual(ErrorHandler.classify(new AdminError(409, "already running")), "conflict");
});

QUnit.test("everything else is a plain error", function (assert) {
    assert.strictEqual(ErrorHandler.classify(new AdminError(500, "boom")), "error");
    assert.strictEqual(ErrorHandler.classify(new AdminError(422, "bad")), "error");
});

QUnit.test("a non-AdminError is still classified, not thrown on", function (assert) {
    assert.strictEqual(ErrorHandler.classify(new Error("network down")), "error");
    assert.strictEqual(ErrorHandler.classify("something odd"), "error");
});

QUnit.module("ErrorHandler.messageFor");

QUnit.test("an AdminError's detail is used", function (assert) {
    assert.strictEqual(
        ErrorHandler.messageFor(new AdminError(500, "database unavailable"), "fallback"),
        "database unavailable"
    );
});

QUnit.test("a detail-less error falls back", function (assert) {
    assert.strictEqual(ErrorHandler.messageFor(new AdminError(500, ""), "fallback"), "fallback");
    assert.strictEqual(ErrorHandler.messageFor(undefined, "fallback"), "fallback");
});

QUnit.module("ErrorHandler.handle on a 409", {
    beforeEach: function (this: HandleCtx) {
        this.toasts = [];
        this.boxes = [];
        this.realShow = MessageToast.show;
        this.realError = MessageBox.error;
        (MessageToast as unknown as { show: unknown }).show = (text: string) => { this.toasts.push(text); };
        (MessageBox as unknown as { error: unknown }).error = (text: string) => { this.boxes.push(text); };
    },
    afterEach: function (this: HandleCtx) {
        (MessageToast as unknown as { show: unknown }).show = this.realShow;
        (MessageBox as unknown as { error: unknown }).error = this.realError;
    }
});

interface HandleCtx {
    toasts: string[];
    boxes: string[];
    realShow: typeof MessageToast.show;
    realError: typeof MessageBox.error;
}

QUnit.test("a 409 from a run trigger is a toast: a run is already in flight", function (this: HandleCtx, assert) {
    ErrorHandler.handle(new AdminError(409, "a run is already in progress", {}, "", true), "fallback");

    assert.deepEqual(this.toasts, ["a run is already in progress"], "the detail as a toast");
    assert.deepEqual(this.boxes, [], "no message box");
});

QUnit.test("a 409 from a save is a MessageBox with the server's detail", function (this: HandleCtx, assert) {
    ErrorHandler.handle(new AdminError(409, "An agent named 'a' already exists."), "Could not save the agent.");

    assert.deepEqual(this.boxes, ["An agent named 'a' already exists."], "the detail in a box that stays");
    assert.deepEqual(this.toasts, [], "no toast that vanishes after 3 seconds");
});

QUnit.test("a 409 without detail outside a run trigger falls back in a MessageBox", function (this: HandleCtx, assert) {
    ErrorHandler.handle(new AdminError(409, ""), "Could not delete the agent.");

    assert.deepEqual(this.boxes, ["Could not delete the agent."], "the caller's fallback");
    assert.deepEqual(this.toasts, [], "no toast");
});

QUnit.test("classify still answers conflict for every 409 (the OData pages branch on it)", function (assert) {
    assert.strictEqual(ErrorHandler.classify(new AdminError(409, "x")), "conflict");
    assert.strictEqual(ErrorHandler.classify(new AdminError(409, "x", {}, "", true)), "conflict");
});
