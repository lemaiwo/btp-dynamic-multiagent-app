import { BUILTINS, authModesFor, findBuiltin } from "com/agent/admin/model/builtins";
import validators from "com/agent/admin/model/validators";

QUnit.module("builtins catalog");

QUnit.test("every built-in in the catalog passes the url validator", function (assert) {
    BUILTINS.forEach((b) => {
        assert.strictEqual(validators.validateServerUrl(b.url, b.defaultAuthMode), "", b.url);
    });
});

QUnit.test("each default auth mode is one the server accepts", function (assert) {
    BUILTINS.forEach((b) => {
        assert.ok(authModesFor(b.url).indexOf(b.defaultAuthMode) > -1, b.url);
    });
});

QUnit.test("findBuiltin normalises case and a trailing slash", function (assert) {
    assert.strictEqual(findBuiltin("BUILTIN:Teams/")?.url, "builtin:teams");
    assert.strictEqual(findBuiltin("https://a.hana.ondemand.com/mcp"), undefined);
});

QUnit.test("a remote server is offered neither destination nor session", function (assert) {
    const modes = authModesFor("https://a.hana.ondemand.com/mcp");
    assert.strictEqual(modes.indexOf("destination"), -1);
    assert.strictEqual(modes.indexOf("session"), -1);
    assert.ok(modes.indexOf("jwt") > -1);
});

QUnit.test("restricted built-ins offer only what the server accepts", function (assert) {
    assert.deepEqual(authModesFor("builtin:jira"), ["destination"]);
    assert.deepEqual(authModesFor("builtin:slack"), ["destination"]);
    assert.deepEqual(authModesFor("builtin:sapnotedetail"), ["session", "destination"]);
    assert.deepEqual(authModesFor("builtin:teams"), ["oauth2", "app_only", "destination"]);
    assert.deepEqual(authModesFor("builtin:gmail"), ["oauth2", "destination"]);
    assert.deepEqual(authModesFor("builtin:outlook"), ["oauth2", "app_only", "destination"]);
    assert.deepEqual(authModesFor("builtin:sapnotes"), ["none", "destination"]);
});

// --- destinations ---
QUnit.test("every built-in can run through a BTP destination", function (assert) {
    BUILTINS.forEach((b) => {
        assert.ok(authModesFor(b.url).indexOf("destination") > -1, `${b.url} offers destination`);
    });
    // The default stays what it was: a destination is an option, not the
    // first thing an admin is steered to.
    assert.strictEqual(findBuiltin("builtin:outlook")?.defaultAuthMode, "oauth2");
    assert.strictEqual(findBuiltin("builtin:sapnotes")?.defaultAuthMode, "none");
});

QUnit.test("every built-in restricts its auth modes to what its factory builds", function (assert) {
    // A mode the factory cannot build is a save that succeeds and an agent
    // that vanishes from chat at the next reload. No entry may leave the
    // dropdown open to jwt or destination by omission.
    BUILTINS.forEach((b) => {
        assert.ok(Array.isArray(b.authModes) && b.authModes.length > 0, `${b.url} lists authModes`);
    });
});
