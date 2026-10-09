import bitbucketEntry from "com/agent/admin/model/bitbucketEntry";
import oauthConfig from "com/agent/admin/model/oauthConfig";
import validators from "com/agent/admin/model/validators";
import { authModesFor, findBuiltin } from "com/agent/admin/model/builtins";
import type { McpServer } from "com/agent/admin/service/types";

const URL = "builtin:bitbucket";
/** A dialog model holding fields of every other toolset as well. */
function form(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return Object.assign({
        dcr: false, client_id: "cid", client_secret: "sec", mailbox: "x@example.com",
        allow_send: true, lookback: "2d", project: "ABC", status: "Open", api_base: "/api/2",
        labels: "l1", user_context: true, has_client_secret: true,
        destination: " BITBUCKET ", workspace: "acme-ws", repositories: ["svc-a", "svc-b"],
        branch: "release/2.x", allow_comment: true, allow_approve: true, require_green_builds: false
    }, overrides);
}

QUnit.module("bitbucketEntry");

QUnit.test("bitbucket is a destination-only builtin", function (assert) {
    assert.strictEqual(findBuiltin("Builtin:Bitbucket/")?.url, URL);
    assert.deepEqual(authModesFor(URL), ["destination"]);
    assert.strictEqual(validators.validateServerUrl(URL, "destination"), "");
    assert.ok(bitbucketEntry.isBitbucketUrl("BUILTIN:BITBUCKET/"));
    assert.notOk(bitbucketEntry.isBitbucketUrl("builtin:jira"));
    assert.notOk(oauthConfig.supportsUserContext(URL), "no signed-in-user switch");
});

QUnit.test("cleanOAuth sends exactly the keys the server stores", function (assert) {
    const out = oauthConfig.cleanOAuth(form(), "destination", URL) as Record<string, unknown>;
    assert.deepEqual(out, {
        destination: "BITBUCKET", workspace: "acme-ws", repositories: ["svc-a", "svc-b"],
        branch: "release/2.x", allow_comment: true, allow_approve: true, require_green_builds: false
    });
});

QUnit.test("defaults and blanks are left out, nothing of another toolset is kept", function (assert) {
    const out = oauthConfig.cleanOAuth(form({
        repositories: [], branch: "", allow_comment: false, allow_approve: false,
        require_green_builds: true
    }), "destination", URL) as Record<string, unknown>;
    assert.deepEqual(out, { destination: "BITBUCKET", workspace: "acme-ws" });
});

QUnit.test("a switch is sent only for exactly the boolean", function (assert) {
    const out = oauthConfig.cleanOAuth(form({
        allow_comment: "true", allow_approve: 1, require_green_builds: "false"
    }), "destination", URL) as Record<string, unknown>;
    assert.notOk("allow_comment" in out);
    assert.notOk("allow_approve" in out);
    assert.notOk("require_green_builds" in out);
});

QUnit.test("the repositories field is a list, typed comma-separated", function (assert) {
    assert.deepEqual(bitbucketEntry.parseRepositories(" svc-a, ,svc-b ,"), ["svc-a", "svc-b"]);
    assert.deepEqual(bitbucketEntry.parseRepositories(""), []);
    assert.strictEqual(bitbucketEntry.formatRepositories(["svc-a", "svc-b"]), "svc-a, svc-b");
    assert.strictEqual(bitbucketEntry.formatRepositories(undefined), "");
    assert.strictEqual(bitbucketEntry.formatRepositories("svc-a"), "", "only a stored list is shown");
});

QUnit.test("the validator names the field, mirrors the server and never quotes the value", function (assert) {
    const ok = { destination: "BITBUCKET", workspace: "acme-ws" };
    const check = (o: Record<string, unknown>, mode = "destination"): string =>
        validators.validateOAuth(o as McpServer["oauth"], mode as McpServer["auth_mode"], URL);
    assert.strictEqual(check(ok), "");
    assert.strictEqual(check(Object.assign({}, ok, {
        repositories: ["svc-a", "svc_b.c"], branch: "release/2.x", allow_comment: true,
        allow_approve: true, require_green_builds: false
    })), "");
    assert.ok(check(ok, "oauth2").indexOf("destination") > -1, "another mode is refused");
    assert.ok(check({ destination: "BITBUCKET" }).indexOf("workspace") > -1);
    assert.ok(check({ workspace: "acme-ws" }).indexOf("destination") > -1);
    const bad: Array<[Record<string, unknown>, string]> = [
        [{ workspace: "Acme S3cret" }, "workspace"],
        [{ repositories: ["svc-a", "S3cret/x"] }, "Repositories"],
        [{ repositories: ["svc-a", "svc-a"] }, "twice"],
        [{ repositories: new Array(51).fill(0).map((_, i) => "r" + i) }, "50"],
        [{ branch: "a..S3cret" }, "branch"],
        [{ branch: "x.lock" }, "branch"],
        [{ allow_approve: true }, "commenting"]
    ];
    bad.forEach(([change, word]) => {
        const message = check(Object.assign({}, ok, change));
        assert.ok(message.toLowerCase().indexOf(word.toLowerCase()) > -1, `${word}: ${message}`);
        assert.strictEqual(message.indexOf("S3cret"), -1, "the value is not quoted");
    });
});

QUnit.test("newlyApproves names the workspaces whose approving is switched on by this save", function (assert) {
    const entry = (workspace: string, approve: boolean): McpServer => ({
        url: URL, auth_mode: "destination",
        oauth: Object.assign({ destination: "B", workspace, allow_comment: true },
            approve ? { allow_approve: true } : {})
    } as McpServer);
    const jira = { url: "builtin:jira", auth_mode: "destination",
        oauth: { destination: "J", allow_approve: true } } as unknown as McpServer;
    assert.deepEqual(bitbucketEntry.newlyApproves([], [entry("ws-a", true)]), ["ws-a"]);
    assert.deepEqual(bitbucketEntry.newlyApproves([entry("ws-a", false)], [entry("ws-a", true)]), ["ws-a"]);
    assert.deepEqual(bitbucketEntry.newlyApproves([entry("ws-a", true)], [entry("ws-a", true)]), []);
    assert.deepEqual(bitbucketEntry.newlyApproves([entry("ws-a", true)], [entry("ws-b", true)]), ["ws-b"]);
    assert.deepEqual(bitbucketEntry.newlyApproves([entry("ws-a", true)], [entry("ws-a", false)]), []);
    assert.deepEqual(bitbucketEntry.newlyApproves([], [jira]), [], "only a bitbucket entry counts");
    assert.deepEqual(bitbucketEntry.newlyApproves(undefined, undefined), []);
    const truthy = entry("ws-a", false);
    (truthy.oauth as Record<string, unknown>).allow_approve = "true";
    assert.deepEqual(bitbucketEntry.newlyApproves([], [truthy]), [], "only the boolean true approves");
});

QUnit.test("the pins are sent as typed, never repaired", function (assert) {
    const out = oauthConfig.cleanOAuth(form({
        workspace: " acme-ws", repositories: ["svc-a "], branch: "main "
    }), "destination", URL) as Record<string, unknown>;
    assert.strictEqual(out.workspace, " acme-ws");
    assert.deepEqual(out.repositories, ["svc-a "]);
    assert.strictEqual(out.branch, "main ");
    const message = validators.validateOAuth(out as McpServer["oauth"], "destination", URL);
    assert.ok(message.indexOf("workspace") > -1, "and the validator refuses what the server would");
    assert.ok(validators.validateOAuth(
        { destination: "B", workspace: "acme-ws", branch: "main " } as McpServer["oauth"], "destination", URL
    ).indexOf("branch") > -1);
    assert.ok(validators.validateOAuth(
        { destination: "B", workspace: "acme-ws", repositories: [] } as McpServer["oauth"], "destination", URL
    ).indexOf("Repositories") > -1, "an empty list is refused, not read as the whole workspace");
});

QUnit.test("a switch that is not a boolean is refused", function (assert) {
    const check = (o: Record<string, unknown>): string => validators.validateOAuth(
        Object.assign({ destination: "B", workspace: "acme-ws" }, o) as McpServer["oauth"], "destination", URL);
    assert.ok(check({ allow_comment: "true" }).indexOf("switch") > -1);
    assert.ok(check({ require_green_builds: "false" }).indexOf("switch") > -1);
    assert.strictEqual(check({ allow_comment: false, allow_approve: false, require_green_builds: true }), "");
});

QUnit.test("an agent holds one bitbucket entry, in whatever spelling", function (assert) {
    const entry = (url: string): McpServer => ({
        url, auth_mode: "destination", oauth: { destination: "B", workspace: "acme-ws" }
    } as McpServer);
    assert.deepEqual(validators.validateServers([entry(URL)]), {});
    assert.deepEqual(validators.validateServers([entry(URL), entry("Builtin:Bitbucket/")]),
        { 1: "An agent has one Bitbucket entry; change the existing one." });
});
