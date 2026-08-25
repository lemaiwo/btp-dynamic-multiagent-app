import ErrorHandler from "com/infrabel/agentadmin/service/ErrorHandler";
import { AdminError } from "com/infrabel/agentadmin/service/AdminService";

QUnit.module("ErrorHandler.classify");

QUnit.test("401 and 403 are session problems", function (assert) {
    assert.strictEqual(ErrorHandler.classify(new AdminError(401, "no token")), "session");
    assert.strictEqual(ErrorHandler.classify(new AdminError(403, "forbidden")), "session");
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
