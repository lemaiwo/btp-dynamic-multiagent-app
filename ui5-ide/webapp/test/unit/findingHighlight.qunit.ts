import { markerRange, targetLine } from "com/agent/ide/model/findingHighlight";

QUnit.module("findingHighlight");

QUnit.test("a line inside the source is kept, one-based", function (assert) {
    assert.strictEqual(targetLine(1, 30), 1, "first line");
    assert.strictEqual(targetLine(12, 30), 12, "a line in the middle");
    assert.strictEqual(targetLine(30, 30), 30, "last line");
});

QUnit.test("a line outside the source is clamped", function (assert) {
    assert.strictEqual(targetLine(31, 30), 30, "past the end: the last line");
    assert.strictEqual(targetLine(0, 30), 1, "before the start: the first line");
    assert.strictEqual(targetLine(-4, 30), 1, "negative: the first line");
    assert.strictEqual(targetLine(12.7, 30), 12, "a fraction is cut");
});

QUnit.test("no line or no source gives no target", function (assert) {
    assert.strictEqual(targetLine(null, 30), null, "the finding has no line");
    assert.strictEqual(targetLine(undefined as unknown as null, 30), null, "nor an undefined one");
    assert.strictEqual(targetLine(Number.NaN, 30), null, "not a number");
    assert.strictEqual(targetLine("12" as unknown as number, 30), null, "a string is not a line");
    assert.strictEqual(targetLine(5, 0), null, "an empty source has no line to mark");
    assert.strictEqual(targetLine(5, Number.NaN), null, "an unknown length neither");
});

QUnit.test("the marker covers the whole line, on Ace's zero-based row", function (assert) {
    assert.deepEqual(markerRange(1), [0, 0, 0, Infinity], "line 1 is row 0");
    assert.deepEqual(markerRange(42), [41, 0, 41, Infinity], "line 42 is row 41");
});
