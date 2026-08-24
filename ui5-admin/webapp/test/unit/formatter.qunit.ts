import formatter from "com/infrabel/agentadmin/model/formatter";

QUnit.module("formatter");

QUnit.test("run status maps to a ValueState", function (assert) {
    assert.strictEqual(formatter.runStatusState("success"), "Success");
    assert.strictEqual(formatter.runStatusState("failed"), "Error");
    assert.strictEqual(formatter.runStatusState("interrupted"), "Warning");
    assert.strictEqual(formatter.runStatusState("running"), "Information");
});

QUnit.test("an unknown status degrades to None rather than throwing", function (assert) {
    assert.strictEqual(formatter.runStatusState("bogus" as never), "None");
});

QUnit.test("duration is rendered from the two timestamps", function (assert) {
    assert.strictEqual(
        formatter.runDuration("2026-08-24T10:00:00", "2026-08-24T10:01:30"),
        "1m 30s"
    );
    assert.strictEqual(
        formatter.runDuration("2026-08-24T10:00:00", "2026-08-24T10:00:05"),
        "5s"
    );
});

QUnit.test("a still-running job has no duration", function (assert) {
    assert.strictEqual(formatter.runDuration("2026-08-24T10:00:00", null), "");
});

QUnit.test("a null timestamp renders as an em dash, not 'null'", function (assert) {
    assert.strictEqual(formatter.timestamp(null), "—");
});

QUnit.test("server summary counts servers and names the builtins", function (assert) {
    assert.strictEqual(
        formatter.serverSummary([{ url: "builtin:gmail", auth_mode: "none" }]),
        "builtin:gmail"
    );
    assert.strictEqual(
        formatter.serverSummary([
            { url: "https://a.hana.ondemand.com/mcp", auth_mode: "jwt" },
            { url: "https://b.hana.ondemand.com/mcp", auth_mode: "jwt" }
        ]),
        "2 MCP servers"
    );
    assert.strictEqual(formatter.serverSummary([]), "—");
});
