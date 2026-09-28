import {
    emptyRule,
    emptyUiConfig,
    kindOf,
    stepSummary,
    uiConfigFromWire,
    wireConfigFromUi,
    STEP_KINDS
} from "com/agent/admin/model/stepKinds";
import type { ConditionConfig, HttpConfig, TransformConfig } from "com/agent/admin/service/types";

QUnit.module("stepKinds: wire <-> editor config");

QUnit.test("the kind list mirrors the server", function (assert) {
    assert.deepEqual(STEP_KINDS, ["agent", "condition", "transform", "http", "python"]);
    assert.strictEqual(kindOf({}), "agent", "a row without a kind is an agent step");
    assert.strictEqual(kindOf({ kind: "python" }), "python");
});

QUnit.test("a condition config round-trips through the flat rule rows", function (assert) {
    const wire: ConditionConfig = {
        rules: [
            { when: { source: "json", field: "issue.priority", op: "equals", value: "High", case_sensitive: true },
              then: { action: "stop", output: "Escalated {{item.id}}" } },
            { when: { source: "text", field: "", op: "not_empty", value: "", case_sensitive: false },
              then: { action: "continue", output: "" } }
        ],
        else: { action: "stop", output: "nothing matched" }
    };
    const ui = uiConfigFromWire("condition", wire as unknown as Record<string, unknown>);
    assert.strictEqual(ui.rules.length, 2);
    assert.deepEqual(ui.rules[0], {
        source: "json", field: "issue.priority", op: "equals", value: "High", case_sensitive: true,
        action: "stop", output: "Escalated {{item.id}}"
    }, "when/then are flattened into one row");
    assert.strictEqual(ui.else_action, "stop");
    assert.deepEqual(wireConfigFromUi("condition", ui), wire as unknown as Record<string, unknown>);
});

QUnit.test("a fresh condition has no rules and continues otherwise", function (assert) {
    const cfg = emptyUiConfig("condition");
    cfg.rules.push(emptyRule());
    const wire = wireConfigFromUi("condition", cfg) as unknown as ConditionConfig;
    assert.deepEqual(wire.rules[0].when, { source: "text", field: "", op: "contains", value: "", case_sensitive: false });
    assert.deepEqual(wire.else, { action: "continue", output: "" });
});

QUnit.test("a transform config round-trips, with a null regex when the pattern is blank", function (assert) {
    const wire: TransformConfig = {
        extract_json: "issue.summary", regex: { pattern: "a", replace: "b", flags: "i" },
        template: "S: {{text}}", truncate: 200
    };
    const ui = uiConfigFromWire("transform", wire as unknown as Record<string, unknown>);
    assert.strictEqual(ui.regex_pattern, "a");
    assert.strictEqual(ui.truncate, "200", "truncate is edited as text");
    assert.deepEqual(wireConfigFromUi("transform", ui), wire as unknown as Record<string, unknown>);

    ui.regex_pattern = "";
    ui.truncate = "";
    const back = wireConfigFromUi("transform", ui) as unknown as TransformConfig;
    assert.strictEqual(back.regex, null, "no pattern means no regex block");
    assert.strictEqual(back.truncate, null, "blank truncate means none");
});

QUnit.test("an http config round-trips, query and headers as JSON text", function (assert) {
    const wire: HttpConfig = {
        destination: "jira", method: "POST", path: "/rest/api/2/issue/{{item.id}}/comment",
        query: { notify: "false" }, headers: { "X-Trace": "1" }, body: "{\"body\": \"{{text}}\"}",
        content_type: "application/json", timeout_seconds: 20, expect_status: [200, 201]
    };
    const ui = uiConfigFromWire("http", wire as unknown as Record<string, unknown>);
    assert.strictEqual(JSON.parse(ui.query_text).notify, "false");
    assert.strictEqual(ui.expect_status_text, "200,201");
    assert.deepEqual(wireConfigFromUi("http", ui), wire as unknown as Record<string, unknown>);

    const bare = wireConfigFromUi("http", emptyUiConfig("http")) as unknown as HttpConfig;
    assert.deepEqual(bare.query, {}, "blank JSON text is an empty object");
    assert.notOk("expect_status" in bare, "no expected codes means the server default (2xx)");
    assert.strictEqual(bare.path, "/");
});

QUnit.test("malformed JSON in an http field is reported by name", function (assert) {
    const ui = emptyUiConfig("http");
    ui.headers_text = "{not json";
    assert.throws(() => wireConfigFromUi("http", ui), /Headers must be a JSON object/);
    ui.headers_text = "";
    ui.query_text = "[1, 2]";
    assert.throws(() => wireConfigFromUi("http", ui), /Query must be a JSON object/);
});

QUnit.test("a python config round-trips", function (assert) {
    const ui = uiConfigFromWire("python", { code: "output = text.upper()", timeout_seconds: 7 });
    assert.strictEqual(ui.code, "output = text.upper()");
    assert.strictEqual(ui.py_timeout, 7);
    assert.deepEqual(wireConfigFromUi("python", ui), { code: "output = text.upper()", timeout_seconds: 7 });
});

QUnit.test("an agent step maps to an empty config", function (assert) {
    assert.deepEqual(wireConfigFromUi("agent", emptyUiConfig("agent")), {});
    assert.strictEqual(stepSummary({ kind: "agent", config: {} }), "");
});

QUnit.test("summaries say what a step does", function (assert) {
    assert.strictEqual(stepSummary({ kind: "condition", config: { rules: [{}, {}] } }), "2 rules");
    assert.strictEqual(stepSummary({ kind: "condition", config: {} }), "0 rules");
    assert.strictEqual(stepSummary({ kind: "transform", config: {} }), "pass-through");
    assert.strictEqual(
        stepSummary({ kind: "transform", config: { extract_json: "a.b", regex: { pattern: "x" }, truncate: 10 } }),
        "json a.b, regex, ≤10"
    );
    assert.strictEqual(stepSummary({ kind: "http", config: { destination: "jira", path: "/x" } }), "GET jira/x");
    assert.strictEqual(stepSummary({ kind: "python", config: { code: "\n  output = 1\n" } }), "output = 1");
    assert.strictEqual(stepSummary({ kind: "python", config: { code: "x".repeat(60) } }).length, 41);
});
