import NullableKey, { MAIN_LINE_KEY } from "com/agent/admin/model/NullableKey";

QUnit.module("NullableKey: null in the model, a sentinel key in the Select");

QUnit.test("the main-line sentinel is a real, non-empty key", function (assert) {
    // sap.m.Select shows nothing for selectedKey "", so the "Main line"
    // option cannot use the empty string.
    assert.ok(MAIN_LINE_KEY.length > 0, "non-empty");
});

QUnit.test("formatValue maps null, undefined and '' to the sentinel", function (assert) {
    const type = new NullableKey();
    assert.strictEqual(type.formatValue(null), MAIN_LINE_KEY, "null selects 'Main line'");
    assert.strictEqual(type.formatValue(undefined), MAIN_LINE_KEY, "undefined too");
    assert.strictEqual(type.formatValue(""), MAIN_LINE_KEY, "and a stray empty string");
    assert.strictEqual(type.formatValue("abap"), "abap", "a real key passes through");
});

QUnit.test("parseValue maps the sentinel and '' back to null", function (assert) {
    const type = new NullableKey();
    assert.strictEqual(type.parseValue(MAIN_LINE_KEY), null, "choosing 'Main line' stores null");
    assert.strictEqual(type.parseValue(""), null, "an empty key is null too");
    assert.strictEqual(type.parseValue("  "), null, "whitespace is no key either");
    assert.strictEqual(type.parseValue("abap"), "abap", "a branch key is kept");
    assert.strictEqual(type.parseValue(" abap "), "abap", "and trimmed");
});

QUnit.test("validateValue accepts anything", function (assert) {
    const type = new NullableKey();
    type.validateValue();
    assert.ok(true, "no exception");
});
