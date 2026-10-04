import ActivityState, { ACTIVITY_ROW_LIMIT, MAX_EVENTS, eventRows, isLongOutput, todoView, visibleRows } from "com/agent/ide/model/activity";
import type { SseEvent } from "com/agent/ide/service/types";

const tool = (id: string, status: string, output = ""): SseEvent => ({
    type: "tool",
    data: { ts: "t", agent: "abap", kind: "tool", id, tool: "SAPRead", detail: `d-${id}`, status, output }
});

QUnit.module("ActivityState (RunActivity shape)");

QUnit.test("start then end updates one row", function (assert) {
    const state = new ActivityState();
    state.apply(tool("c1", "running"));
    assert.strictEqual(state.events.length, 1);
    state.apply(tool("c1", "ok", "result"));
    assert.strictEqual(state.events.length, 1, "same id: upsert, no second row");
    assert.strictEqual(state.events[0].status, "ok");
    assert.strictEqual(state.events[0].output, "result");
});

QUnit.test("events without an id are appended; other events are ignored", function (assert) {
    const state = new ActivityState();
    state.apply({ type: "tool", data: { ts: "t", agent: "a", kind: "note", id: "", tool: "", detail: "x", status: "" } });
    state.apply({ type: "tool", data: { ts: "t", agent: "a", kind: "note", id: "", tool: "", detail: "y", status: "" } });
    state.apply({ type: "text", data: { delta: "hi" } });
    assert.strictEqual(state.events.length, 2);
});

QUnit.test("plan replaces the todos", function (assert) {
    const state = new ActivityState();
    state.apply({ type: "plan", data: { todos: [{ content: "a", status: "pending" }, { content: "b", status: "pending" }] } });
    state.apply({ type: "plan", data: { todos: [{ content: "a", status: "completed" }] } });
    assert.deepEqual(state.todos, [{ content: "a", status: "completed" }]);
});

QUnit.test("the list is capped at 500, dropping the oldest", function (assert) {
    const state = new ActivityState();
    for (let i = 0; i < MAX_EVENTS + 5; i++) {
        state.apply(tool(`c${i}`, "ok"));
    }
    assert.strictEqual(state.events.length, MAX_EVENTS);
    assert.strictEqual(state.events[0].id, "c5");
    state.apply(tool("c0", "ok"));
    assert.strictEqual(state.events.length, MAX_EVENTS + 0, "an end for a dropped call is not re-added as a duplicate of another");
});

QUnit.test("load replaces everything from a stored activity; reset clears", function (assert) {
    const state = new ActivityState();
    state.apply(tool("old", "ok"));
    state.load({ events: [{ ts: "t", agent: "a", kind: "tool", id: "n", tool: "T", detail: "", status: "ok" }], plan: [{ content: "p", status: "in_progress" }] });
    assert.deepEqual(state.events.map((e) => e.id), ["n"]);
    assert.strictEqual(state.todos.length, 1);
    state.load(undefined);
    assert.strictEqual(state.events.length, 0);
    assert.strictEqual(state.todos.length, 0);
});

QUnit.test("view rows: todo icons and tool status icons", function (assert) {
    assert.deepEqual(
        todoView([{ content: "a", status: "pending" }, { content: "b", status: "in_progress" }, { content: "c", status: "completed" }, { content: "d", status: "weird" }]).map((t) => t.mark),
        ["[ ]", "[~]", "[x]", "[ ]"]);
    const rows = eventRows([
        { ts: "t", agent: "a", kind: "tool", id: "1", tool: "SAPRead", detail: "D", status: "running" },
        { ts: "t", agent: "a", kind: "tool", id: "2", tool: "SAPRead", detail: "D", status: "ok", output: "out" },
        { ts: "t", agent: "a", kind: "tool", id: "3", tool: "SAPRead", detail: "D", status: "error", output: "bad" },
        { ts: "t", agent: "a", kind: "note", id: "", tool: "", detail: "thinking", status: "" }
    ]);
    assert.deepEqual(rows.map((r) => r.state), ["Information", "Success", "Error", "None"]);
    assert.deepEqual(rows.map((r) => r.hasOutput), [false, true, true, false]);
    assert.strictEqual(rows[3].title, "thinking", "an event without a tool is titled by its detail");
});

QUnit.test("a tool refused by the read-only guard is its own row state, not a failing tool", function (assert) {
    const rows = eventRows([
        { ts: "t", agent: "a", kind: "tool", id: "1", tool: "SAPWrite", detail: "D", status: "error",
            output: "Refused by the read-only IDE", code: "readonly_refused" },
        { ts: "t", agent: "a", kind: "tool", id: "2", tool: "SAPRead", detail: "D", status: "error", output: "bad" },
        { ts: "t", agent: "a", kind: "tool", id: "3", tool: "SAPRead", detail: "D", status: "ok", output: "out" }
    ]);
    assert.deepEqual(rows.map((r) => r.refused), [true, false, false]);
    assert.strictEqual(rows[0].state, "Warning", "refused reads as a warning, not an error");
    assert.strictEqual(rows[0].icon, "sap-icon://locked");
    assert.strictEqual(rows[1].state, "Error", "a failing tool stays an error");
});

QUnit.test("a stored activity keeps the refusal code", function (assert) {
    const state = new ActivityState();
    state.load({ events: [{ ts: "t", agent: "a", kind: "tool", id: "1", tool: "SAPWrite", detail: "", status: "error", code: "readonly_refused" }] });
    assert.strictEqual(eventRows(state.events)[0].refused, true);
});

QUnit.test("a trace proposal that was not stored is a refusal row with its own label", function (assert) {
    const codes = ["too_many_pending", "unknown_trace_request", "target_not_non_production", "not_diagnose", "invalid_request", "proposal_failed"];
    const rows = eventRows(codes.map((code, i) => (
        { ts: "t", agent: "a", kind: "tool", id: String(i), tool: "SAPDiagnose", detail: "trace_start", status: "error", output: "Proposal refused", code }
    )));
    assert.deepEqual(rows.map((r) => r.refused), codes.map(() => true));
    assert.deepEqual(rows.map((r) => r.state), codes.map(() => "Warning"));
    assert.deepEqual(rows.map((r) => r.refusedKey), [
        "activityProposalTooManyPending", "activityProposalUnknownRequest", "activityProposalNotNonProd",
        "activityProposalNotDiagnose", "activityProposalInvalid", "activityProposalFailed"
    ]);
    const guard = eventRows([{ ts: "t", agent: "a", kind: "tool", id: "g", tool: "SAPWrite", detail: "", status: "error", code: "readonly_refused" }]);
    assert.strictEqual(guard[0].refusedKey, "activityRefused");
    assert.strictEqual(eventRows([{ ts: "t", agent: "a", kind: "tool", id: "o", tool: "SAPRead", detail: "", status: "ok" }])[0].refusedKey, "");
});

QUnit.test("a note event (no id, tool, status, agent or ts) becomes a plain row with empty strings", function (assert) {
    const [row] = eventRows([{ kind: "note", detail: "Delegating to abap" }]);
    assert.deepEqual([row.id, row.agent, row.status, row.title, row.detail, row.icon, row.state],
        ["", "", "", "Delegating to abap", "", "", "None"]);
});

QUnit.test("the run-end checks get readable labels; other tools keep their name (Task U8)", function (assert) {
    const rows = eventRows([
        { ts: "t", agent: "ide", kind: "tool", id: "1", tool: "check_sap_base", detail: "Checking SAP base", status: "ok" },
        { ts: "t", agent: "ide", kind: "tool", id: "2", tool: "check_syntax", detail: "Checking syntax", status: "ok" },
        { ts: "t", agent: "abap", kind: "tool", id: "3", tool: "SAPRead", detail: "ZCL_A", status: "ok" }
    ]);
    assert.deepEqual(rows.map((r) => r.labelKey), ["activityToolCheckSapBase", "activityToolCheckSyntax", ""]);
    assert.strictEqual(rows[2].title, "SAPRead");
});

QUnit.module("activity rows on screen (U8 review)");

QUnit.test("visibleRows: the first 50 until all are asked for", function (assert) {
    const rows = Array.from({ length: 60 }, (_, i) => i);
    assert.deepEqual(visibleRows(rows, false), { rows: rows.slice(0, 50), hidden: 10 });
    assert.deepEqual(visibleRows(rows, true), { rows, hidden: 0 });
    assert.deepEqual(visibleRows(rows.slice(0, 50), false), { rows: rows.slice(0, 50), hidden: 0 }, "exactly 50: nothing hidden");
    assert.strictEqual(ACTIVITY_ROW_LIMIT, 50);
});

QUnit.test("isLongOutput: more than 3 lines or more than 240 characters", function (assert) {
    assert.notOk(isLongOutput(""));
    assert.notOk(isLongOutput("a\nb\nc"));
    assert.ok(isLongOutput("a\nb\nc\nd"));
    assert.ok(isLongOutput("x".repeat(241)));
    assert.notOk(isLongOutput("x".repeat(240)));
});

QUnit.test("eventRows: a status key per row, so the state can be read as text", function (assert) {
    const rows = eventRows([
        { ts: "t", agent: "a", kind: "tool", id: "1", tool: "T", detail: "", status: "ok", output: "" },
        { ts: "t", agent: "a", kind: "tool", id: "2", tool: "T", detail: "", status: "running", output: "" },
        { ts: "t", agent: "a", kind: "tool", id: "3", tool: "T", detail: "", status: "error", output: "" },
        { ts: "t", agent: "a", kind: "tool", id: "4", tool: "T", detail: "", status: "ok", output: "", code: "readonly_refused" }
    ]);
    assert.deepEqual(rows.map((r) => r.statusKey), ["activityStatusOk", "activityStatusRunning", "activityStatusError", "activityStatusRefused"]);
});
