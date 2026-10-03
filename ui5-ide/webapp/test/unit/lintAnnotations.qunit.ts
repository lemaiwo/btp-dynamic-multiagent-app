import { toAceAnnotations } from "com/agent/ide/model/lintAnnotations";
import type { LintFinding } from "com/agent/ide/service/types";

function finding(line: number, severity: string, message = "m", rule = "r", column = 3): LintFinding {
    return { line, column, severity, message, rule };
}

QUnit.module("lintAnnotations");

QUnit.test("a finding becomes an Ace annotation on the zero-based row", function (assert) {
    assert.deepEqual(toAceAnnotations([finding(4, "error", "Keyword not upper case", "keyword_case", 7)]), [
        { row: 3, column: 7, type: "error", text: "Keyword not upper case (keyword_case)" }
    ]);
});

QUnit.test("severities map to error, warning and info", function (assert) {
    const types = toAceAnnotations([
        finding(1, "error"), finding(1, "Error"), finding(1, "E"),
        finding(1, "warning"), finding(1, "W"),
        finding(1, "info"), finding(1, "information"), finding(1, "hint"), finding(1, "")
    ]).map((a) => a.type);
    assert.deepEqual(types, ["error", "error", "error", "warning", "warning", "info", "info", "info", "info"]);
});

QUnit.test("line 0 or a missing column clamps to the first row and column", function (assert) {
    const [a] = toAceAnnotations([{ line: 0, column: Number.NaN, severity: "error", message: "x", rule: "" }]);
    assert.strictEqual(a.row, 0);
    assert.strictEqual(a.column, 0);
    assert.strictEqual(a.text, "x", "no empty rule suffix");
});

QUnit.test("no findings, no annotations", function (assert) {
    assert.deepEqual(toAceAnnotations([]), []);
    assert.deepEqual(toAceAnnotations(null), []);
});
