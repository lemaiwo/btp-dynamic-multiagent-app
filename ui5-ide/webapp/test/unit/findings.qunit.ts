import { upsertFinding } from "com/agent/ide/model/findings";
import type { DiagnoseFinding } from "com/agent/ide/service/types";

function finding(id: string, ref: string, patch: Partial<DiagnoseFinding> = {}): DiagnoseFinding {
    return {
        id, kind: "dump", ref_id: ref, title: `Dump ${ref}`, program: null, include: null, line: null,
        occurred_at: null, created_at: null, ...patch
    };
}

QUnit.module("findings: upsertFinding");

QUnit.test("a new finding goes to the front (newest first) and the old list is not mutated", function (assert) {
    const list = [finding("f-1", "A")];
    const next = upsertFinding(list, finding("f-2", "B"));
    assert.deepEqual(next.map((f) => f.id), ["f-2", "f-1"]);
    assert.deepEqual(list.map((f) => f.id), ["f-1"], "the input is untouched");
});

QUnit.test("a finding with a known id replaces its row in place", function (assert) {
    const list = [finding("f-2", "B"), finding("f-1", "A")];
    const next = upsertFinding(list, finding("f-1", "A", { title: "Dump A, again", line: 12 }));
    assert.deepEqual(next.map((f) => f.id), ["f-2", "f-1"]);
    assert.strictEqual(next[1].title, "Dump A, again");
    assert.strictEqual(next[1].line, 12);
});

QUnit.test("the same kind and ref_id under another id is the same finding (the server's unique key)", function (assert) {
    const list = [finding("f-1", "A")];
    const next = upsertFinding(list, finding("f-9", "A", { title: "Updated" }));
    assert.strictEqual(next.length, 1);
    assert.strictEqual(next[0].id, "f-9");
    assert.strictEqual(next[0].title, "Updated");
    const other = upsertFinding(list, finding("f-3", "A", { kind: "trace" }));
    assert.strictEqual(other.length, 2, "another kind with the same ref is another finding");
});

QUnit.test("a frame without an id or a kind is ignored", function (assert) {
    const list = [finding("f-1", "A")];
    assert.strictEqual(upsertFinding(list, null as unknown as DiagnoseFinding), list);
    assert.strictEqual(upsertFinding(list, { title: "x" } as DiagnoseFinding), list);
});
