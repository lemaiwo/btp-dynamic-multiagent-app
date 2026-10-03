import { newRun, reduceRun, activeTool, isCancelled, isRunNote, toChatItem } from "com/agent/ide/model/chatRun";
import type { SseEvent } from "com/agent/ide/service/types";

const tool = (id: string, status: string, toolName = "SAPRead"): SseEvent => ({
    type: "tool",
    data: { ts: "t", agent: "abap", kind: "tool", id, tool: toolName, detail: "", status, output: null }
});

QUnit.module("chatRun reducer (SSE §1.3)");

QUnit.test("a new run is empty and running", function (assert) {
    const run = newRun();
    assert.strictEqual(run.text, "");
    assert.strictEqual(run.finished, false);
    assert.strictEqual(run.error, null);
    assert.deepEqual(run.tools, []);
});

QUnit.test("run, then text deltas append in order; the input state is not mutated", function (assert) {
    const start = newRun();
    let run = reduceRun(start, { type: "run", data: { run_id: "r-1", stage: "design", message_id: "m-1" } });
    run = reduceRun(run, { type: "text", data: { delta: "Hello" } });
    run = reduceRun(run, { type: "text", data: { delta: ", world" } });
    assert.strictEqual(run.runId, "r-1");
    assert.strictEqual(run.stage, "design");
    assert.strictEqual(run.text, "Hello, world");
    assert.strictEqual(start.text, "", "pure: the old state is unchanged");
});

QUnit.test("tool events upsert by id; the active tool is the latest still running", function (assert) {
    let run = reduceRun(newRun(), tool("c1", "running"));
    run = reduceRun(run, tool("c2", "running", "SAPSearch"));
    assert.strictEqual(run.tools.length, 2);
    assert.strictEqual(activeTool(run)?.tool, "SAPSearch");
    run = reduceRun(run, tool("c2", "ok", "SAPSearch"));
    assert.strictEqual(run.tools.length, 2, "the end event replaced the start event");
    assert.strictEqual(run.tools[1].status, "ok");
    assert.strictEqual(activeTool(run)?.tool, "SAPRead", "back to the one still running");
    run = reduceRun(run, tool("c1", "error"));
    assert.strictEqual(activeTool(run), undefined);
});

QUnit.test("plan, usage, artifact and file events are kept", function (assert) {
    let run = reduceRun(newRun(), { type: "plan", data: { todos: [{ content: "read", status: "pending" }] } });
    run = reduceRun(run, { type: "usage", data: { requests_used: 3, request_cap: 200 } });
    run = reduceRun(run, { type: "artifact", data: { id: "a-1", kind: "design", version: 2 } });
    run = reduceRun(run, { type: "file", data: { path: "src/CLAS/zcl_x.clas.abap", state: "modified" } });
    run = reduceRun(run, { type: "file", data: { path: "src/CLAS/zcl_x.clas.abap", state: "modified" } });
    assert.deepEqual(run.todos, [{ content: "read", status: "pending" }]);
    assert.deepEqual(run.usage, { requests_used: 3, request_cap: 200 });
    assert.deepEqual(run.artifacts, [{ id: "a-1", kind: "design", version: 2 }]);
    assert.deepEqual(run.files, [{ path: "src/CLAS/zcl_x.clas.abap", state: "modified" }], "one entry per path");
});

QUnit.test("error is kept; done finishes the run with its stage and status", function (assert) {
    let run = reduceRun(newRun(), { type: "error", data: { message: "boom", code: "run_failed" } });
    assert.deepEqual(run.error, { message: "boom", code: "run_failed" });
    assert.strictEqual(run.finished, false, "an error alone does not finish the run");
    run = reduceRun(run, { type: "done", data: { message_id: "m-2", stage: "plan", status: "idle" } });
    assert.strictEqual(run.finished, true);
    assert.deepEqual(run.done, { message_id: "m-2", stage: "plan", status: "idle" });
    assert.deepEqual(run.error, { message: "boom", code: "run_failed" }, "the error stays visible after done");
});

QUnit.test("an unknown event type leaves the state as it is", function (assert) {
    const run = newRun();
    assert.strictEqual(reduceRun(run, { type: "mystery", data: {} } as unknown as SseEvent), run);
});

QUnit.module("chatRun messages");

QUnit.test("a cancelled answer is recognised by its '(cancelled)' tail", function (assert) {
    assert.ok(isCancelled("Partial answer\n\n(cancelled)"));
    assert.ok(isCancelled("(cancelled)  "));
    assert.notOk(isCancelled("I cancelled the transport (as asked)."));
    assert.notOk(isCancelled(""));
});

QUnit.test("toChatItem renders assistant markdown and keeps user text plain", function (assert) {
    const render = (md: string): string => `<div>${md.toUpperCase()}</div>`;
    const user = toChatItem({ id: "m-1", role: "user", stage: "chat", content: "<b>hi</b>", created_at: null }, render);
    assert.strictEqual(user.html, "", "a user message is never rendered as HTML");
    assert.strictEqual(user.content, "<b>hi</b>");
    const bot = toChatItem({ id: "m-2", role: "assistant", stage: "chat", content: "ok\n(cancelled)", created_at: null }, render);
    assert.strictEqual(bot.html, "<div>OK\n(CANCELLED)</div>");
    assert.strictEqual(bot.cancelled, true);
    assert.strictEqual(bot.isUser, false);
    assert.strictEqual(user.isUser, true);
});

QUnit.test("a non-fatal note (no_diagnose_server, conventions_unavailable) is kept apart from the run's error", function (assert) {
    let run = reduceRun(newRun(), { type: "error", data: { code: "no_diagnose_server", message: "no server" } });
    run = reduceRun(run, { type: "error", data: { code: "conventions_unavailable", message: "no conventions" } });
    assert.strictEqual(run.error, null, "the run did not fail");
    assert.deepEqual(run.notes.map((n) => n.code), ["no_diagnose_server", "conventions_unavailable"]);
    run = reduceRun(run, { type: "error", data: { code: "run_failed", message: "boom" } });
    assert.strictEqual(run.error?.code, "run_failed", "a real error still is one");
    assert.strictEqual(isRunNote("no_diagnose_server"), true);
    assert.strictEqual(isRunNote("run_failed"), false);
    assert.strictEqual(isRunNote(undefined), false);
});
