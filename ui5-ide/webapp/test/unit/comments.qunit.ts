import {
    allowedTransitions, anchorKey, anchorText, endRun, groupByAnchor, isEditable, openCount, stateText
} from "com/agent/ide/model/comments";
import type { Comment, CommentState } from "com/agent/ide/service/types";

const t = (key: string, args?: (string | number)[]): string => (args?.length ? `${key}(${args.join("|")})` : key);

function fileComment(id: string, state: CommentState, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "file", path: "src/CLAS/zcl_demo.clas.abap", revision: 3, line_start: 12, line_end: 14,
        kind: null, version: null, paragraph: null, body: "b", state, answer: null,
        created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z", ...extra
    };
}

function docComment(id: string, state: CommentState, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 2, paragraph: 4, body: "b", state, answer: null,
        created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z", ...extra
    };
}

QUnit.module("comments");

QUnit.test("user transitions follow the state machine; sent has none (lead decision)", function (assert) {
    assert.deepEqual(allowedTransitions("open"), ["dismissed"], "open -> dismissed (sent is a run's, not the user's)");
    assert.deepEqual(allowedTransitions("sent"), [], "no user transition out of sent");
    assert.deepEqual(allowedTransitions("addressed"), ["open", "dismissed"], "reopen or dismiss");
    assert.deepEqual(allowedTransitions("dismissed"), ["open"], "reopen");
    assert.deepEqual(allowedTransitions("bogus" as CommentState), [], "unknown state: nothing");
});

QUnit.test("allowedTransitions returns a fresh array each time", function (assert) {
    const a = allowedTransitions("addressed");
    a.push("sent");
    assert.deepEqual(allowedTransitions("addressed"), ["open", "dismissed"]);
});

QUnit.test("body edit and delete only while open", function (assert) {
    assert.ok(isEditable("open"));
    (["sent", "addressed", "dismissed"] as CommentState[]).forEach((s) => assert.notOk(isEditable(s), s));
});

QUnit.test("endRun: every still-sent comment is open again, without mutating the input", function (assert) {
    const list = [fileComment("c1", "sent"), fileComment("c2", "addressed"), docComment("c3", "sent"), docComment("c4", "dismissed")];
    const snapshot = JSON.stringify(list);
    const out = endRun(list);
    assert.deepEqual(out.map((c) => c.state), ["open", "addressed", "open", "dismissed"]);
    assert.strictEqual(JSON.stringify(list), snapshot, "input untouched");
    assert.notStrictEqual(out[0], list[0], "a changed comment is a copy");
    assert.strictEqual(out[1], list[1], "an unchanged comment is the same object");
});

QUnit.test("anchorText: file range, single line, document paragraph (1-based for people)", function (assert) {
    assert.strictEqual(anchorText(fileComment("c", "open"), t),
        "commentAnchorFileRange(zcl_demo.clas.abap|3|12|14)");
    assert.strictEqual(anchorText(fileComment("c", "open", { line_end: 12 }), t),
        "commentAnchorFileLine(zcl_demo.clas.abap|3|12)");
    assert.strictEqual(anchorText(docComment("c", "open"), t), "commentAnchorDocument(docKindDesign|2|5)");
    assert.strictEqual(anchorText(docComment("c", "open", { kind: "plan", paragraph: 0 }), t),
        "commentAnchorDocument(docKindPlan|2|1)");
});

QUnit.test("anchorText never builds markup from the comment", function (assert) {
    const c = fileComment("c", "open", { path: "notes/<img src=x onerror=alert(1)>.md" });
    const text = anchorText(c, (key, args) => `${key}:${(args ?? []).join(",")}`);
    assert.ok(text.includes("<img"), "returned as plain text; the caller renders it as text");
});

QUnit.test("groupByAnchor groups by anchor key in first-seen order and keeps each group's order", function (assert) {
    const list = [
        fileComment("c1", "open"),
        docComment("c2", "open"),
        fileComment("c3", "addressed"),
        fileComment("c4", "open", { line_start: 20, line_end: 20 }),
        docComment("c5", "open", { paragraph: 1 }),
        docComment("c6", "open", { version: 3 })
    ];
    const snapshot = JSON.stringify(list);
    const groups = groupByAnchor(list);
    assert.deepEqual(Array.from(groups.keys()), [
        anchorKey(list[0]), anchorKey(list[1]), anchorKey(list[3]), anchorKey(list[4]), anchorKey(list[5])
    ]);
    assert.deepEqual(groups.get(anchorKey(list[0]))?.map((c) => c.id), ["c1", "c3"]);
    assert.strictEqual(JSON.stringify(list), snapshot, "input untouched");
    assert.notStrictEqual(anchorKey(list[1]), anchorKey(list[5]), "another document version is another anchor");
    assert.notStrictEqual(anchorKey(list[0]), anchorKey(fileComment("x", "open", { revision: 4 })), "another revision too");
});

QUnit.test("openCount counts only open comments", function (assert) {
    assert.strictEqual(openCount([]), 0);
    assert.strictEqual(openCount([fileComment("a", "open"), fileComment("b", "sent"), docComment("c", "open"),
        docComment("d", "addressed"), docComment("e", "dismissed")]), 2);
});

QUnit.test("stateText maps each state to its i18n key; unknown is shown as sent by the server", function (assert) {
    assert.strictEqual(stateText("open", t), "commentStateOpen");
    assert.strictEqual(stateText("sent", t), "commentStateSent");
    assert.strictEqual(stateText("addressed", t), "commentStateAddressed");
    assert.strictEqual(stateText("dismissed", t), "commentStateDismissed");
    assert.strictEqual(stateText("weird" as CommentState, t), "weird");
});
