import formatter from "com/infrabel/agentadmin/model/formatter";

QUnit.module("formatter");

QUnit.test("run status maps to a ValueState", function (assert) {
    assert.strictEqual(formatter.runStatusState("success"), "Success");
    assert.strictEqual(formatter.runStatusState("failed"), "Error");
    assert.strictEqual(formatter.runStatusState("interrupted"), "Warning");
    assert.strictEqual(formatter.runStatusState("running"), "Information");
});

QUnit.test("the legacy 'degraded' status still gets a colour", function (assert) {
    // No current code writes this, but stored rows carry it — half the runs in
    // the deployed acceptance database do. Without this it rendered neutral,
    // which is indistinguishable from a status the app does not understand.
    assert.strictEqual(formatter.runStatusState("degraded"), "Warning");
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

QUnit.test("a server needing no token is neutral, whatever its token state", function (assert) {
    // app_only and destination servers are connected by configuration. Colouring
    // them would suggest an action nobody can take from this screen.
    assert.strictEqual(formatter.credentialState(false, "none"), "None");
    assert.strictEqual(formatter.credentialState(false, "valid"), "None");
});

QUnit.test("a live token reads as success, an expired one as an error", function (assert) {
    assert.strictEqual(formatter.credentialState(true, "valid"), "Success");
    assert.strictEqual(formatter.credentialState(true, "expired"), "Error");
});

QUnit.test("a refreshable token is success, not a warning", function (assert) {
    // The access token has expired but PerUserOAuth2Auth renews it silently, so
    // the connection works and nobody needs to do anything. Flagging it would
    // train admins to ignore the column.
    assert.strictEqual(formatter.credentialState(true, "refreshable"), "Success");
});

QUnit.test("never signing in is a warning, not an error", function (assert) {
    // Nothing is broken yet — the agent has simply not been connected.
    assert.strictEqual(formatter.credentialState(true, "none"), "Warning");
});

QUnit.test("an unknown token state degrades to a warning", function (assert) {
    assert.strictEqual(formatter.credentialState(true, "bogus" as never), "Warning");
});
