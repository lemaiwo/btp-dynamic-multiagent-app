import {
    changedText, counts, hasFilters, matches, objectLine, sortSessions, statusView, waitingText
} from "com/agent/ide/model/worklist";
import type { SessionSummary } from "com/agent/ide/service/types";

const t = (key: string, args?: (string | number)[]): string => (args?.length ? `${key}(${args.join(",")})` : key);

function session(id: string, extra: Partial<SessionSummary> = {}): SessionSummary {
    return {
        id, owner: "DEVUSER01", title: `Session ${id}`, target: "DEMO", type: "change", stage: "design",
        status: "idle", created_at: "2026-10-01T08:00:00Z", updated_at: "2026-10-01T08:00:00Z",
        target_non_production: false, pins: {}, waiting: null,
        open_comments: 0, unresolved_comments: 0, requests_used: 0, request_cap: 200, ...extra
    };
}

QUnit.module("worklist");

QUnit.test("waitingText words each reason; null is empty", function (assert) {
    assert.strictEqual(waitingText("approval", t), "worklistWaitingApproval");
    assert.strictEqual(waitingText("comments", t), "worklistWaitingComments");
    assert.strictEqual(waitingText("changes", t), "worklistWaitingChanges");
    assert.strictEqual(waitingText("document", t), "worklistWaitingDocument");
    assert.strictEqual(waitingText(null, t), "");
    assert.strictEqual(waitingText(undefined, t), "");
    assert.strictEqual(waitingText("other" as never, t), "", "an unknown reason shows nothing");
});

QUnit.test("search matches title and target, case-insensitive, trimmed", function (assert) {
    const s = session("1", { title: "Fix rounding in ZCL_PRICE", target: "QAS-1" });
    assert.ok(matches(s, "", {}));
    assert.ok(matches(s, "   ", {}));
    assert.ok(matches(s, "rounding", {}));
    assert.ok(matches(s, "zcl_price", {}));
    assert.ok(matches(s, "  qas-1 ", {}));
    assert.notOk(matches(s, "DEVUSER01", {}), "owner is not searched");
    assert.notOk(matches(s, "nothing", {}));
});

QUnit.test("type, stage and waiting filters combine", function (assert) {
    const a = session("a", { type: "change", stage: "plan", waiting: "document" });
    const b = session("b", { type: "diagnose", stage: "investigate", waiting: "approval" });
    const c = session("c", { type: "change", stage: "propose", waiting: null });
    const pick = (filters: Parameters<typeof matches>[2]): string[] =>
        [a, b, c].filter((s) => matches(s, "", filters)).map((s) => s.id);
    assert.deepEqual(pick({}), ["a", "b", "c"]);
    assert.deepEqual(pick({ type: "change" }), ["a", "c"]);
    assert.deepEqual(pick({ type: "diagnose" }), ["b"]);
    assert.deepEqual(pick({ stage: "propose" }), ["c"]);
    assert.deepEqual(pick({ waiting: "any" }), ["a", "b"]);
    assert.deepEqual(pick({ waiting: "none" }), ["c"]);
    assert.deepEqual(pick({ waiting: "approval" }), ["b"]);
    assert.deepEqual(pick({ type: "change", waiting: "any" }), ["a"]);
    assert.deepEqual(pick({ type: null, stage: null, waiting: null }), ["a", "b", "c"], "null means no filter");
    assert.deepEqual(pick({ type: "change", stage: "investigate" }), []);
});

QUnit.test("search and filters together", function (assert) {
    const s = session("x", { title: "Dump analysis", type: "diagnose", stage: "investigate" });
    assert.ok(matches(s, "dump", { type: "diagnose" }));
    assert.notOk(matches(s, "dump", { type: "change" }));
});

QUnit.test("sortSessions: updated_at descending, without mutating the input", function (assert) {
    const list = [
        session("old", { updated_at: "2026-09-01T00:00:00Z" }),
        session("new", { updated_at: "2026-10-02T00:00:00Z" }),
        session("none", { updated_at: null }),
        session("mid", { updated_at: "2026-09-15T12:00:00+02:00" }),
        session("bad", { updated_at: "not a date" })
    ];
    const snapshot = JSON.stringify(list);
    const sorted = sortSessions(list);
    assert.deepEqual(sorted.map((s) => s.id).slice(0, 3), ["new", "mid", "old"]);
    assert.deepEqual(sorted.map((s) => s.id).slice(3).sort(), ["bad", "none"], "missing or bad dates last");
    assert.strictEqual(JSON.stringify(list), snapshot, "input order untouched");
    assert.notStrictEqual(sorted, list, "a new array");
});

QUnit.test("sortSessions is deterministic on ties (created_at desc, then id)", function (assert) {
    const same = "2026-10-01T08:00:00Z";
    const list = [
        session("b", { updated_at: same, created_at: "2026-09-01T00:00:00Z" }),
        session("a", { updated_at: same, created_at: "2026-09-01T00:00:00Z" }),
        session("c", { updated_at: same, created_at: "2026-09-30T00:00:00Z" })
    ];
    assert.deepEqual(sortSessions(list).map((s) => s.id), ["c", "a", "b"]);
    assert.deepEqual(sortSessions(list.slice().reverse()).map((s) => s.id), ["c", "a", "b"]);
});

// --- U6: what a worklist row shows ------------------------------------------

QUnit.test("statusView: waiting wins over running and done, and carries its reason", function (assert) {
    assert.deepEqual(statusView(session("1", { waiting: "changes" }), t),
        { text: "worklistStatusWaiting", state: "Warning", inverted: false, reason: "worklistWaitingChanges", icon: "sap-icon://alert" });
    assert.deepEqual(statusView(session("2", { waiting: "approval", status: "running" }), t).reason, "worklistWaitingApproval");
    assert.deepEqual(statusView(session("3", { waiting: "document", stage: "done" }), t).text, "worklistStatusWaiting");
});

QUnit.test("statusView: running, done and idle badges without a reason", function (assert) {
    assert.deepEqual(statusView(session("1", { status: "running" }), t),
        { text: "worklistStatusRunning", state: "Information", inverted: true, reason: "", icon: "" });
    assert.deepEqual(statusView(session("2", { stage: "done" }), t),
        { text: "worklistStatusDone", state: "Success", inverted: true, reason: "", icon: "" });
    assert.deepEqual(statusView(session("3"), t),
        { text: "worklistStatusIdle", state: "None", inverted: true, reason: "", icon: "" });
    assert.strictEqual(statusView(session("4", { waiting: "bogus" as never }), t).text, "worklistStatusIdle",
        "an unknown reason is not shown as waiting");
});

QUnit.test("counts: sessions waiting for the developer and sessions running", function (assert) {
    assert.deepEqual(counts([]), { waiting: 0, running: 0 });
    assert.deepEqual(counts([
        session("a", { waiting: "changes" }),
        session("b", { waiting: "approval", status: "running" }),
        session("c", { status: "running" }),
        session("d"),
        session("e", { waiting: "bogus" as never })
    ]), { waiting: 2, running: 2 });
});

QUnit.test("objectLine: up to three names, then the rest as a count", function (assert) {
    assert.strictEqual(objectLine([], t), "");
    assert.strictEqual(objectLine(["ZCL_A"], t), "ZCL_A");
    assert.strictEqual(objectLine(["A", "B", "C"], t), "A, B, C");
    assert.strictEqual(objectLine(["A", "B", "C", "D", "E"], t), "worklistObjectsMore(A, B, C,2)");
});

QUnit.test("objectLine counts the objects the server did not list (objects_total, Task U8)", function (assert) {
    assert.strictEqual(objectLine(["A", "B", "C", "D", "E"], t, 9), "worklistObjectsMore(A, B, C,6)",
        "five names listed of nine: three shown, six more");
    assert.strictEqual(objectLine(["A", "B"], t, 2), "A, B", "a total equal to the names adds nothing");
    assert.strictEqual(objectLine(["A", "B"], t, 1), "A, B", "a total below the names is ignored");
    assert.strictEqual(objectLine([], t, 0), "");
});

QUnit.test("search also matches the session's object names (Task U8)", function (assert) {
    const s = session("1", { title: "Rounding", objects: ["ZCL_PRICE_CALC", "ZI_ORDER"] });
    assert.ok(matches(s, "price_calc", {}), "an object name matches, case-insensitive");
    assert.ok(matches(s, "zi_order", {}));
    assert.notOk(matches(s, "ZCL_OTHER", {}));
    assert.ok(matches(session("2", { objects: undefined }), "session 2", {}), "a session without objects still matches its title");
});

QUnit.test("changedText: changed objects of a change session, findings of a diagnose session (Task U8)", function (assert) {
    assert.strictEqual(changedText(session("1", { changed_objects: 2 }), t), "2");
    assert.strictEqual(changedText(session("1", { changed_objects: 0 }), t), "worklistNoValue", "none changed: a dash");
    assert.strictEqual(changedText(session("1"), t), "worklistNoValue", "an older server without the field: a dash");
    assert.strictEqual(changedText(session("2", { type: "diagnose", findings_count: 1 }), t), "worklistFindingsOne");
    assert.strictEqual(changedText(session("2", { type: "diagnose", findings_count: 3 }), t), "worklistFindings(3)");
    assert.strictEqual(changedText(session("2", { type: "diagnose", findings_count: 0 }), t), "worklistNoValue");
    assert.strictEqual(changedText(session("2", { type: "diagnose", findings_count: null }), t), "worklistNoValue");
});

QUnit.test("hasFilters: a query or any filter set", function (assert) {
    assert.notOk(hasFilters("", {}));
    assert.notOk(hasFilters("  ", { type: null, stage: null, waiting: null }));
    assert.ok(hasFilters("x", {}));
    assert.ok(hasFilters("", { type: "change" }));
    assert.ok(hasFilters("", { stage: "plan" }));
    assert.ok(hasFilters("", { waiting: "any" }));
});
