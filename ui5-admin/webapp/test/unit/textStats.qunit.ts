import { counterText, textStats } from "com/agent/admin/model/textStats";

QUnit.module("textStats: the counter under long text fields");

QUnit.test("empty text is 0 characters and 0 lines", function (assert) {
    assert.deepEqual(textStats(""), { chars: 0, lines: 0 });
    assert.deepEqual(textStats(undefined), { chars: 0, lines: 0 });
    assert.deepEqual(textStats(null), { chars: 0, lines: 0 });
    assert.strictEqual(counterText(""), "0 characters · 0 lines");
});

QUnit.test("one line without a break is one line", function (assert) {
    assert.deepEqual(textStats("a"), { chars: 1, lines: 1 });
    assert.strictEqual(counterText("a"), "1 character · 1 line", "singulars");
});

QUnit.test("every line break starts another line, a trailing one included", function (assert) {
    assert.deepEqual(textStats("one\ntwo\nthree"), { chars: 13, lines: 3 });
    assert.deepEqual(textStats("one\n"), { chars: 4, lines: 2 }, "the cursor is on a second, empty line");
    assert.strictEqual(counterText("one\ntwo"), "7 characters · 2 lines");
});

QUnit.test("large counts are grouped", function (assert) {
    const text = "x".repeat(1234);
    assert.strictEqual(counterText(text), "1,234 characters · 1 line");
});
