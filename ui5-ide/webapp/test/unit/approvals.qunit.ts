import { reduceApprovals, pendingCount, paramsText, approvalRow, approvalErrorKey, approvalErrorText } from "com/agent/ide/model/approvals";
import type { Approval, SseEvent, TraceParams } from "com/agent/ide/service/types";

const params: TraceParams = {
    processType: "http", objectType: "any", maxExecutions: 1, expiresHours: 1,
    sqlTrace: true, aggregate: false, description: "demo"
};

function approval(id: string, status: Approval["status"] = "pending", created = "2026-10-03T10:00:00Z"): Approval {
    return { id, action: "trace_start", params, status, created_at: created, decided_at: null, result: null, error_code: null };
}

// Plain English stand-in for the i18n bundle: key plus its arguments.
const t = (key: string, args?: (string | number)[]): string => [key, ...(args ?? [])].join(":");

QUnit.module("approvals: reduceApprovals");

QUnit.test("approval_required adds a pending approval at the top", function (assert) {
    const one = reduceApprovals([], { type: "approval_required", data: approval("a1") });
    const two = reduceApprovals(one, { type: "approval_required", data: approval("a2", "pending", "2026-10-03T11:00:00Z") });
    assert.deepEqual(two.map((a) => a.id), ["a2", "a1"], "newest first");
    assert.strictEqual(two[0].status, "pending");
});

QUnit.test("an event for a known id upserts instead of duplicating", function (assert) {
    const list = reduceApprovals([approval("a1")], { type: "approval_required", data: approval("a1") });
    assert.strictEqual(list.length, 1);
});

QUnit.test("approval and decided replace the pending row", function (assert) {
    const start = [approval("a1"), approval("a0", "denied", "2026-10-03T09:00:00Z")];
    const approved = reduceApprovals(start, { type: "approval", data: approval("a1", "approved") });
    assert.deepEqual(approved.map((a) => `${a.id}:${a.status}`), ["a1:approved", "a0:denied"]);
    const denied = reduceApprovals(start, { type: "decided", data: approval("a1", "denied") });
    assert.strictEqual(denied[0].status, "denied");
    assert.strictEqual(start[0].status, "pending", "the input list is not mutated");
});

QUnit.test("ordering is newest created first, whatever the arrival order", function (assert) {
    let list: Approval[] = [];
    list = reduceApprovals(list, { type: "approval_required", data: approval("old", "pending", "2026-10-01T00:00:00Z") });
    list = reduceApprovals(list, { type: "approval_required", data: approval("new", "pending", "2026-10-03T00:00:00Z") });
    list = reduceApprovals(list, { type: "approval", data: approval("mid", "approved", "2026-10-02T00:00:00Z") });
    assert.deepEqual(list.map((a) => a.id), ["new", "mid", "old"]);
});

QUnit.test("other events leave the list untouched", function (assert) {
    const list = [approval("a1")];
    const ignored = reduceApprovals(list, { type: "text", data: { delta: "x" } });
    assert.deepEqual(ignored, list);
    const unknown = reduceApprovals(list, { type: "mystery", data: {} } as unknown as SseEvent);
    assert.deepEqual(unknown, list);
});

QUnit.module("approvals: pendingCount and paramsText");

QUnit.test("pendingCount counts only pending rows", function (assert) {
    assert.strictEqual(pendingCount([approval("a"), approval("b", "approved"), approval("c")]), 2);
    assert.strictEqual(pendingCount([]), 0);
});

QUnit.test("paramsText summarises a trace_start", function (assert) {
    assert.strictEqual(paramsText(params, t),
        "approvalProcessHttp · approvalParamMaxExec:1 · approvalParamExpires:1 · approvalParamSqlOn");
    assert.strictEqual(paramsText({ ...params, processType: "rfc", maxExecutions: 3, expiresHours: 8, sqlTrace: false }, t),
        "approvalProcessRfc · approvalParamMaxExec:3 · approvalParamExpires:8");
});

QUnit.test("paramsText names the request of a trace_cancel", function (assert) {
    assert.strictEqual(paramsText({ id: "TR-1" }, t), "approvalParamRequest:TR-1");
});

QUnit.test("paramsText shows an unknown process type as it is, never as another one", function (assert) {
    const odd = { ...params, processType: "update" as never };
    assert.ok(paramsText(odd, t).startsWith("update · "), "the raw value, not HTTP");
});

QUnit.module("approvals: approvalRow (what the card shows)");

const time = (iso: string): string => `at(${iso})`;

QUnit.test("a pending trace_start lists every parameter that will be armed, and for whom", function (assert) {
    const row = approvalRow(approval("a1"), "DEMO", t, time);
    assert.strictEqual(row.pending, true);
    assert.strictEqual(row.title, "approvalTitle:DEMO");
    assert.deepEqual(row.fields.map((f) => `${f.label}=${f.value}`), [
        "approvalFieldProcess=approvalProcessHttp",
        "approvalFieldObject=approvalObjectAny",
        "approvalFieldMaxExec=1",
        "approvalFieldExpires=approvalHours:1",
        "approvalFieldSql=approvalOn",
        "approvalFieldAggregate=approvalOff",
        "approvalFieldDescription=demo",
        "approvalFieldUser=approvalOwnUser"
    ]);
    assert.strictEqual(row.statusText, "", "no status line while pending");
    assert.strictEqual(row.decideBy, "", "no decide-by hint without a ttl");
});

QUnit.test("unknown enum values are shown raw; an empty description is left out", function (assert) {
    const row = approvalRow({
        ...approval("a1"),
        params: { ...params, processType: "update" as never, objectType: "odd" as never, description: "" }
    }, "DEMO", t, time);
    assert.strictEqual(row.fields[0].value, "update");
    assert.strictEqual(row.fields[1].value, "odd");
    assert.notOk(row.fields.some((f) => f.label === "approvalFieldDescription"));
});

QUnit.test("a pending trace_cancel names the request it cancels", function (assert) {
    const row = approvalRow({ ...approval("a1"), action: "trace_cancel", params: { id: "TR-1" } }, "DEMO", t, time);
    assert.strictEqual(row.title, "approvalCancelTitle:DEMO");
    assert.deepEqual(row.fields.map((f) => `${f.label}=${f.value}`), ["approvalFieldRequest=TR-1"]);
});

QUnit.test("a ttl gives a decide-by hint from created_at", function (assert) {
    const row = approvalRow({ ...approval("a1"), ttl_min: 15 }, "DEMO", t, time);
    assert.strictEqual(row.decideBy, "approvalDecideBy:at(2026-10-03T10:15:00.000Z)");
});

QUnit.test("decided approvals are one read-only status line", function (assert) {
    const armed = approvalRow({
        ...approval("a1", "approved"), result: { trace_request_id: "TRC-9", expires_at: "2026-10-03T11:00:00Z" }
    }, "DEMO", t, time);
    assert.strictEqual(armed.pending, false);
    assert.strictEqual(armed.statusText, "approvalArmed:TRC-9:at(2026-10-03T11:00:00Z)");
    assert.strictEqual(armed.statusState, "Success");
    assert.strictEqual(approvalRow({ ...approval("a1", "approved"), result: { trace_request_id: "TRC-9" } }, "DEMO", t, time).statusText,
        "approvalArmedNoExpiry:TRC-9");
    const denied = approvalRow(approval("a1", "denied"), "DEMO", t, time);
    assert.strictEqual(denied.statusText, "approvalLineFor:approvalDenied:approvalIdentityStart:demo");
    assert.strictEqual(denied.statusState, "None");
    const expired = approvalRow(approval("a1", "expired"), "DEMO", t, time);
    assert.strictEqual(expired.statusText, "approvalLineFor:approvalExpired:approvalIdentityStart:demo");
    assert.strictEqual(expired.statusState, "Warning");
    const cancelled = approvalRow({
        ...approval("a1", "approved"), action: "trace_cancel", params: { id: "TR-1" }, result: {}
    }, "DEMO", t, time);
    assert.strictEqual(cancelled.statusText, "approvalCancelled:TR-1");
});

QUnit.test("approved without a result is 'outcome unknown', never 'armed'", function (assert) {
    const unknown = approvalRow(approval("a1", "approved"), "DEMO", t, time);
    assert.strictEqual(unknown.statusText, "approvalLineFor:approvalOutcomeUnknown:approvalIdentityStart:demo");
    assert.strictEqual(unknown.statusState, "Warning");
    const noId = approvalRow({ ...approval("a1", "approved"), result: { note: "n" } }, "DEMO", t, time);
    assert.ok(noId.statusText.startsWith("approvalLineFor:approvalOutcomeUnknown:"), "a trace_start result without a request id too");
});

QUnit.test("a failed arming says why, by error code, with the server's note when it has one", function (assert) {
    const timeout = approvalRow({
        ...approval("a1", "failed"), error_code: "arc1_timeout_unknown", result: { note: "may have been armed; check trace_requests" }
    }, "DEMO", t, time);
    assert.strictEqual(timeout.statusText, "approvalLineFor:approvalFailed:approvalErrArc1TimeoutUnknown:approvalIdentityStart:demo");
    assert.strictEqual(timeout.statusState, "Error");
    const audit = approvalRow({ ...approval("a1", "failed"), error_code: "audit_unavailable" }, "DEMO", t, time);
    assert.ok(audit.statusText.startsWith("approvalLineFor:approvalFailed:approvalErrAuditUnavailable:"));
    const odd = approvalRow({ ...approval("a1", "failed"), error_code: "boom", result: { note: "see log" } }, "DEMO", t, time);
    assert.ok(odd.statusText.startsWith("approvalLineFor:approvalFailed:approvalErrOther:boom — see log:"));
    const noCode = approvalRow({ ...approval("a1", "failed"), error_code: null }, "DEMO", t, time);
    assert.ok(noCode.statusText.startsWith("approvalLineFor:approvalFailed:approvalErrNoCode:"),
        `a failure without a code does not read "error ": ${noCode.statusText}`);
    assert.strictEqual(approvalErrorText(undefined, "see log", t), "approvalErrNoCode — see log");
});

QUnit.test("approvalErrorKey maps the known codes", function (assert) {
    assert.deepEqual(
        ["arc1_timeout_unknown", "unknown_trace_request", "too_many_pending", "audit_unavailable",
            "target_not_non_production", "approval_expired", "approval_not_pending", "interrupted", "nope", null]
            .map((c) => approvalErrorKey(c)),
        ["approvalErrArc1TimeoutUnknown", "approvalErrUnknownTraceRequest", "approvalErrTooManyPending",
            "approvalErrAuditUnavailable", "approvalNotNonProd", "approvalExpired", "approvalErrNotPending",
            "approvalErrInterrupted", undefined, undefined]);
});

QUnit.test("an action or status the UI does not know is a read-only line, never a card", function (assert) {
    const odd = approvalRow({ ...approval("a1"), action: "set_sql_trace_state" as never }, "DEMO", t, time);
    assert.strictEqual(odd.pending, false, "a pending row of an unknown action has no Approve button");
    assert.strictEqual(odd.statusText, "set_sql_trace_state: pending");
    const status = approvalRow(approval("a1", "weird" as never), "DEMO", t, time);
    assert.strictEqual(status.pending, false);
    assert.strictEqual(status.statusText, "weird");
});

QUnit.module("approvals: robustness of the card (final review S6)");

QUnit.test("a decided line says which request it is about: the description, the trace request, or the approval id", function (assert) {
    const line = (a: Approval): string => approvalRow(a, "DEMO", t, time).statusText;
    assert.strictEqual(line({ ...approval("a1", "denied"), params: { ...params, description: "" } }),
        "approvalLineFor:approvalDenied:approvalIdentityRequest:a1", "no description: the approval id");
    assert.strictEqual(line({ ...approval("a2", "expired"), action: "trace_cancel", params: { id: "TR-1" } }),
        "approvalLineFor:approvalExpired:approvalIdentityCancel:TR-1", "a cancel: the trace request");
    assert.strictEqual(line({ ...approval("a3", "failed"), params: null as never, error_code: "boom" }),
        "approvalLineFor:approvalFailed:approvalErrOther:boom:approvalIdentityRequest:a3", "unreadable params: the approval id");
});

QUnit.test("params that are not a valid object never throw and never offer Approve", function (assert) {
    const bad: unknown[] = [null, undefined, "x", 5, [], {}, { processType: "http" }, { objectType: "url" },
        { processType: 1, objectType: "url" }];
    for (const value of bad) {
        const row = approvalRow({ ...approval("a1"), params: value as never }, "DEMO", t, time);
        const shown = JSON.stringify(value);
        assert.strictEqual(row.pending, true, `${shown}: still a card, so it can be rejected`);
        assert.strictEqual(row.canApprove, false, `${shown}: no Approve`);
        assert.strictEqual(row.invalidText, "approvalInvalid", `${shown}: said to be invalid`);
        assert.deepEqual(row.fields, [], `${shown}: no half-empty parameter list to consent to`);
        assert.strictEqual(paramsText(value as never, t), "approvalInvalid", `${shown}: the summary says so too`);
    }
    const cancel = approvalRow({ ...approval("a1"), action: "trace_cancel", params: {} as never }, "DEMO", t, time);
    assert.strictEqual(cancel.canApprove, false, "a cancel without a request id has no Approve either");
    const mixed = approvalRow({ ...approval("a1"), action: "trace_cancel", params }, "DEMO", t, time);
    assert.strictEqual(mixed.canApprove, false, "nor a cancel that carries trace_start params");
    const noBounds = approvalRow({ ...approval("a1"), params: { ...params, maxExecutions: undefined as never } }, "DEMO", t, time);
    assert.strictEqual(noBounds.canApprove, false, "nor a trace_start without its numbers");
});

QUnit.test("valid params offer Approve; a decided row never does", function (assert) {
    const ok = approvalRow(approval("a1"), "DEMO", t, time);
    assert.strictEqual(ok.canApprove, true);
    assert.strictEqual(ok.invalidText, "");
    assert.strictEqual(approvalRow({ ...approval("a1"), action: "trace_cancel", params: { id: "TR-1" } }, "DEMO", t, time).canApprove, true);
    assert.strictEqual(approvalRow(approval("a1", "denied"), "DEMO", t, time).canApprove, false);
});
