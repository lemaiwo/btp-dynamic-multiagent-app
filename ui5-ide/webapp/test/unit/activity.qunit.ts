import ActivityState, { MAX_EVENTS, todoView, eventRows } from "com/agent/ide/model/activity";
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
