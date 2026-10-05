import { HtmlCache, activityCounts, interleave, timeOf } from "com/agent/ide/model/conversation";
import type { Approval, Message } from "com/agent/ide/service/types";

function msg(id: string, created_at: string | null, role: Message["role"] = "assistant"): Message {
    return { id, role, stage: "chat", content: id, created_at, has_activity: false };
}

function appr(id: string, created_at: string | null): Approval {
    return {
        id, action: "trace_start", params: {} as Approval["params"], status: "pending", created_at,
        decided_at: null, result: null, error_code: null
    } as Approval;
}

const ids = (list: ReturnType<typeof interleave>): string[] =>
    list.map((e) => (e.type === "message" ? `m:${e.message.id}` : `a:${e.approval.id}`));

QUnit.module("conversation");

QUnit.test("timeOf reads API times as UTC with or without a zone; anything else is null", function (assert) {
    assert.strictEqual(timeOf("2026-10-03T10:00:00"), Date.UTC(2026, 9, 3, 10, 0, 0), "no zone: UTC");
    assert.strictEqual(timeOf("2026-10-03T10:00:00Z"), Date.UTC(2026, 9, 3, 10, 0, 0));
    assert.strictEqual(timeOf("2026-10-03T12:00:00+02:00"), Date.UTC(2026, 9, 3, 10, 0, 0), "an offset is honoured");
    assert.strictEqual(timeOf(null), null);
    assert.strictEqual(timeOf("not a time"), null);
});

QUnit.test("approval cards go where they happened between the messages", function (assert) {
    const messages = [msg("q", "2026-10-03T09:00:00", "user"), msg("a1", "2026-10-03T09:01:00"), msg("a2", "2026-10-03T09:10:00")];
    const approvals = [appr("late", "2026-10-03T09:20:00Z"), appr("mid", "2026-10-03T09:05:00Z")];   // newest first, as listed
    assert.deepEqual(ids(interleave(messages, approvals)), ["m:q", "m:a1", "a:mid", "m:a2", "a:late"]);
});

QUnit.test("on the same time a message comes first; equal entries keep their order; no time goes last", function (assert) {
    const t = "2026-10-03T10:00:00";
    assert.deepEqual(ids(interleave([msg("x", t), msg("y", t)], [appr("p", t)])), ["m:x", "m:y", "a:p"]);
    assert.deepEqual(ids(interleave([msg("x", null), msg("y", t)], [appr("p", null)])), ["m:y", "m:x", "a:p"],
        "rows without a readable time follow the dated ones, messages first");
    assert.deepEqual(ids(interleave([], [])), []);
});

QUnit.test("interleave does not change its input", function (assert) {
    const messages = [msg("b", "2026-10-03T10:00:00"), msg("a", "2026-10-03T09:00:00")];
    const approvals = [appr("p", "2026-10-03T09:30:00")];
    interleave(messages, approvals);
    assert.deepEqual(messages.map((m) => m.id), ["b", "a"]);
    assert.strictEqual(approvals.length, 1);
});

QUnit.test("activityCounts: tool calls (not notes) and plan steps", function (assert) {
    assert.deepEqual(activityCounts({
        events: [
            { kind: "tool", id: "1", tool: "SAPRead", detail: "", status: "ok" },
            { kind: "tool", id: "2", tool: "check_sap_base", detail: "", status: "ok" },
            { kind: "note", detail: "thinking" }
        ],
        plan: [{ content: "a", status: "completed" }, { content: "b", status: "pending" }],
        dropped: 3
    }), { tools: 5, steps: 2 }, "dropped events were tool calls too");
    assert.deepEqual(activityCounts({ events: [], plan: [], dropped: 0 }), { tools: 0, steps: 0 });
});

QUnit.module("HtmlCache");

QUnit.test("renders a message once per id and content; a changed content renders again", function (assert) {
    const cache = new HtmlCache();
    let calls = 0;
    const render = (md: string): string => { calls++; return `<p>${md}</p>`; };
    assert.strictEqual(cache.get("m1", "a", render), "<p>a</p>");
    assert.strictEqual(cache.get("m1", "a", render), "<p>a</p>");
    assert.strictEqual(calls, 1, "the second read is cached");
    assert.strictEqual(cache.get("m1", "b", render), "<p>b</p>", "new content of the same id");
    assert.strictEqual(calls, 2);
    cache.get("m2", "a", render);
    assert.strictEqual(calls, 3, "another id renders on its own");
    cache.clear();
    cache.get("m1", "b", render);
    assert.strictEqual(calls, 4, "clear forgets everything");
});
