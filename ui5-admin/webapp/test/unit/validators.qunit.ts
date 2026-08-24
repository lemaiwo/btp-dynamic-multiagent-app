import validators from "com/infrabel/agentadmin/model/validators";

QUnit.module("validators.validateServerUrl");

QUnit.test("an authenticated server must use https", function (assert) {
    assert.notStrictEqual(validators.validateServerUrl("http://x.hana.ondemand.com/mcp", "jwt"), "");
    assert.strictEqual(validators.validateServerUrl("https://x.hana.ondemand.com/mcp", "jwt"), "");
});

QUnit.test("a public server may use http", function (assert) {
    assert.strictEqual(validators.validateServerUrl("http://x.example.com/mcp", "none"), "");
});

QUnit.test("a known builtin is accepted regardless of auth mode", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:gmail", "none"), "");
    assert.strictEqual(validators.validateServerUrl("BUILTIN:GMAIL", "none"), "");
});

QUnit.test("an unknown builtin is rejected as a typo", function (assert) {
    const msg = validators.validateServerUrl("builtin:teams", "none");
    assert.ok(msg.length > 0, "returns a message");
    assert.ok(msg.indexOf("builtin:gmail") > -1, "lists the known builtins");
});

QUnit.test("a blank url is rejected", function (assert) {
    assert.notStrictEqual(validators.validateServerUrl("   ", "jwt"), "");
});

QUnit.module("validators.validateOAuth");

QUnit.test("dcr needs no manual credentials", function (assert) {
    assert.strictEqual(validators.validateOAuth({ dcr: true }, "oauth2"), "");
});

QUnit.test("manual oauth2 requires a client_id", function (assert) {
    assert.notStrictEqual(
        validators.validateOAuth({ client_id: "", uaa_url: "https://u" }, "oauth2"),
        ""
    );
});

QUnit.test("manual oauth2 requires uaa_url or both endpoint urls", function (assert) {
    assert.notStrictEqual(validators.validateOAuth({ client_id: "c" }, "oauth2"), "");
    assert.strictEqual(
        validators.validateOAuth({ client_id: "c", uaa_url: "https://u" }, "oauth2"),
        ""
    );
    assert.notStrictEqual(
        validators.validateOAuth({ client_id: "c", authorize_url: "https://a" }, "oauth2"),
        "",
        "authorize_url alone is not enough"
    );
    assert.strictEqual(
        validators.validateOAuth(
            { client_id: "c", authorize_url: "https://a", token_url: "https://t" },
            "oauth2"
        ),
        ""
    );
});

QUnit.test("oauth config on a non-oauth2 server is rejected", function (assert) {
    assert.notStrictEqual(validators.validateOAuth({ client_id: "c" }, "jwt"), "");
});

QUnit.test("no oauth config on a non-oauth2 server is fine", function (assert) {
    assert.strictEqual(validators.validateOAuth(undefined, "jwt"), "");
});

QUnit.module("validators.validateServers");

QUnit.test("duplicate urls are reported on the later entry", function (assert) {
    const errors = validators.validateServers([
        { url: "https://a.hana.ondemand.com/mcp", auth_mode: "jwt" },
        { url: "https://a.hana.ondemand.com/mcp", auth_mode: "jwt" }
    ]);
    assert.notOk(errors[0], "first entry is clean");
    assert.ok(errors[1].indexOf("duplicate") > -1, "second entry flagged as duplicate");
});

QUnit.test("an empty server list is reported against index -1", function (assert) {
    const errors = validators.validateServers([]);
    assert.ok(errors[-1], "at least one server is required");
});
