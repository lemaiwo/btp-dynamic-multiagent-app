import {
    LAST_RUNS_LIMIT, RUN_REFRESH_DELAYS_MS, canonical, isDirty, runsCountLabel
} from "com/agent/admin/model/runsPanel";

QUnit.module("runsPanel: the last-runs panel's count label");

QUnit.test("zero reads as no runs, one is singular, more are plural", function (assert) {
    assert.strictEqual(runsCountLabel(0), "No runs");
    assert.strictEqual(runsCountLabel(1), "1 run");
    assert.strictEqual(runsCountLabel(2), "2 runs");
    assert.strictEqual(runsCountLabel(9), "9 runs");
});

QUnit.test("a full page reads as 'limit+' rather than an exact count", function (assert) {
    // The page asked for at most LAST_RUNS_LIMIT rows, so getting exactly
    // that many says nothing about how many more exist.
    assert.strictEqual(runsCountLabel(LAST_RUNS_LIMIT), `${LAST_RUNS_LIMIT}+ runs`);
    assert.strictEqual(runsCountLabel(3, 3), "3+ runs", "the limit is a parameter");
    assert.strictEqual(runsCountLabel(2, 3), "2 runs", "below the limit the count is exact");
});

QUnit.test("the refresh schedule reloads once quickly and once later", function (assert) {
    assert.strictEqual(RUN_REFRESH_DELAYS_MS.length, 2);
    assert.ok(RUN_REFRESH_DELAYS_MS[0] < RUN_REFRESH_DELAYS_MS[1], "in ascending order");
    assert.ok(RUN_REFRESH_DELAYS_MS[0] <= 1500, "the first reload is prompt");
});

QUnit.module("runsPanel: the dirty check behind Refresh");

QUnit.test("key order and undefined values do not count as a change", function (assert) {
    assert.strictEqual(canonical({ b: 1, a: [{ d: 2, c: 3 }] }), canonical({ a: [{ c: 3, d: 2 }], b: 1 }));
    assert.strictEqual(canonical({ a: 1, b: undefined }), canonical({ a: 1 }));
    assert.notStrictEqual(canonical({ a: 1, b: null }), canonical({ a: 1 }), "null is a value, not an absence");
});

QUnit.test("isDirty compares the current payload against the snapshot by value", function (assert) {
    const snapshot = canonical({ name: "btp-agent", peers: ["gmail-agent"], enabled: true });
    assert.notOk(isDirty(snapshot, { enabled: true, peers: ["gmail-agent"], name: "btp-agent" }), "same data, other key order");
    assert.ok(isDirty(snapshot, { name: "renamed", peers: ["gmail-agent"], enabled: true }), "a scalar changed");
    assert.ok(isDirty(snapshot, { name: "btp-agent", peers: [], enabled: true }), "a list changed");
    assert.ok(isDirty(snapshot, { name: "btp-agent", peers: ["gmail-agent"], enabled: true, extra: 1 }), "a key added");
});

QUnit.test("with no snapshot (a new record, or one that failed to load) nothing is dirty", function (assert) {
    assert.notOk(isDirty(undefined, { name: "anything" }));
});
