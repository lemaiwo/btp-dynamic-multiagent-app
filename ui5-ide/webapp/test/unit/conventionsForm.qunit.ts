import {
    CLEARABLE, TARGET_MAX, createBody, formOf, isValidTarget, resync, updateBody
} from "com/agent/ide/model/conventionsForm";
import type { Conventions } from "com/agent/ide/service/types";

QUnit.module("conventionsForm");

const STORED: Conventions = {
    target: "DEMO", label: "Demo system", destination: "arc1-abap-readonly", namespace: "", package: "ZDEMO",
    atc_variant: null, clean_core_level: "B", free_text: "Use released APIs.", non_production: false, updated_at: null
};

QUnit.test("the target rule is the server's: letter or digit first, then up to 63 of [A-Za-z0-9_.-]", function (assert) {
    assert.strictEqual(TARGET_MAX, 64, "64 characters at most");
    for (const ok of ["DEMO", "DEMO2", "demo-system", "a", "9.x_y-z", "A".repeat(64)]) {
        assert.ok(isValidTarget(ok), `${ok} is accepted`);
    }
    for (const bad of ["", " DEMO", "DEMO ", "-DEMO", ".DEMO", "_DEMO", "DE MO", "DEMO/1", "DEMO!", "A".repeat(65), "DÉMO", "{i18n>appTitle}"]) {
        assert.notOk(isValidTarget(bad), `"${bad}" is refused`);
    }
});

QUnit.test("formOf: every text field as a string, null as empty, no flag", function (assert) {
    const form = formOf(STORED);
    assert.deepEqual(form, {
        label: "Demo system", destination: "arc1-abap-readonly", namespace: "", package: "ZDEMO",
        atc_variant: "", clean_core_level: "B", free_text: "Use released APIs."
    });
    assert.deepEqual(formOf(undefined).package, "", "no row: empty");
});

QUnit.test("updateBody sends only the changed fields and never the flag", function (assert) {
    const form = { ...formOf(STORED), label: "Demo", free_text: "Use released APIs.\nNo SELECT *." };
    assert.deepEqual(updateBody(formOf(STORED), form), {
        body: { label: "Demo", free_text: "Use released APIs.\nNo SELECT *." }, clear: []
    });
    assert.deepEqual(updateBody(formOf(STORED), formOf(STORED)), { body: {}, clear: [] }, "nothing changed: nothing to send");
    assert.notOk("non_production" in updateBody(formOf(STORED), form).body, "the flag is never part of a Save");
});

QUnit.test("an emptied field is an explicit clear, never an empty string value", function (assert) {
    const form = { ...formOf(STORED), package: "", label: "  " };
    const { body, clear } = updateBody(formOf(STORED), form);
    assert.deepEqual(clear.slice().sort(), ["label", "package"], "both emptied fields are cleared (blank counts as empty)");
    assert.notOk("package" in body, "package is not also set (the server refuses set + clear)");
    assert.notOk("label" in body, "label is not also set");
});

QUnit.test("a field already empty is not cleared again; clean core level is never cleared", function (assert) {
    const form = { ...formOf(STORED), namespace: "", atc_variant: "", clean_core_level: "" };
    assert.deepEqual(updateBody(formOf(STORED), form), { body: {}, clear: [] });
    assert.deepEqual(CLEARABLE, ["label", "destination", "namespace", "package", "atc_variant", "free_text"]);
});

QUnit.test("createBody: the trimmed target plus the non-empty fields, never the flag", function (assert) {
    const form = { ...formOf(undefined), package: "ZNEW", free_text: "Keep it short.", label: "  " };
    const body = createBody(" DEMO2 ", form);
    assert.deepEqual(body, { target: "DEMO2", package: "ZNEW", free_text: "Keep it short." });
    assert.notOk("non_production" in body, "a new target starts as production; the flag is set afterwards, confirmed");
});

QUnit.test("values are text: binding syntax in a field is sent and compared literally", function (assert) {
    const stored: Conventions = { ...STORED, free_text: "{i18n>appTitle}" };
    const form = { ...formOf(stored), label: "{= ${x} }" };
    assert.strictEqual(formOf(stored).free_text, "{i18n>appTitle}");
    assert.deepEqual(updateBody(formOf(stored), form), { body: { label: "{= ${x} }" }, clear: [] });
});

QUnit.test("values are sent trimmed; whitespace added around a stored value is no change", function (assert) {
    const form = { ...formOf(STORED), label: "  Demo  ", package: "ZDEMO  ", free_text: "Use released APIs.\n" };
    assert.deepEqual(updateBody(formOf(STORED), form), { body: { label: "Demo" }, clear: [] });
    const created = createBody("DEMO2", { ...formOf(undefined), package: " ZNEW ", label: "\tNew\n" });
    assert.deepEqual(created, { target: "DEMO2", package: "ZNEW", label: "New" });
});

QUnit.test("a whitespace-only stored value can be cleared; left as it is, it is not touched", function (assert) {
    const stored: Conventions = { ...STORED, label: "   ", namespace: " " };
    assert.deepEqual(updateBody(formOf(stored), { ...formOf(stored), label: "" }), { body: {}, clear: ["label"] }, "emptied: cleared");
    assert.deepEqual(updateBody(formOf(stored), formOf(stored)), { body: {}, clear: [] }, "untouched: nothing sent");
});

QUnit.test("resync: untouched fields take the fresh row, edited ones keep the user's text; the fresh row is the new baseline", function (assert) {
    const base = formOf(STORED);
    const form = { ...base, label: "Mine" };
    const fresh: Conventions = { ...STORED, label: "Theirs", package: "ZOTHER", non_production: true };
    const next = resync(base, form, fresh);
    assert.strictEqual(next.form.package, "ZOTHER", "untouched: the other admin's value is shown");
    assert.strictEqual(next.form.label, "Mine", "edited: the user's text stays");
    assert.deepEqual(next.base, formOf(fresh), "the baseline is the fresh row");
    assert.deepEqual(updateBody(next.base, next.form), { body: { label: "Mine" }, clear: [] }, "a later Save sends only the user's edit");
    assert.deepEqual(form.package, "ZDEMO", "the inputs are not changed in place");
});
