import ResourceBundle from "sap/base/i18n/ResourceBundle";
import bitbucketEntry from "com/agent/admin/model/bitbucketEntry";
import oauthConfig from "com/agent/admin/model/oauthConfig";
import validators from "com/agent/admin/model/validators";
import { authModesFor, findBuiltin } from "com/agent/admin/model/builtins";
import type { BitbucketApprovalAsk } from "com/agent/admin/model/bitbucketEntry";
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

// --- the Save question: what a save lets the agent approve ------------------

function stored(oauth: Record<string, unknown>, url = URL): McpServer {
    return { url, auth_mode: "destination", oauth: Object.assign({ destination: "B" }, oauth) } as McpServer;
}
const ON = { workspace: "acme-ws", allow_comment: true, allow_approve: true };
function on(more: Record<string, unknown> = {}): McpServer {
    return stored(Object.assign({}, ON, more));
}
/** An ask as it is expected: what the test names, and nothing widened besides. */
function expected(named: Partial<BitbucketApprovalAsk>): BitbucketApprovalAsk {
    return Object.assign(
        { branchBefore: "", repositoriesAdded: [], wholeWorkspace: false, buildsDropped: false },
        named) as BitbucketApprovalAsk;
}

QUnit.test("a save that newly approves is asked about, with what it opens", function (assert) {
    assert.deepEqual(bitbucketEntry.approvalsToAsk([], [on()]), [expected({
        reason: "approve", workspace: "acme-ws", branch: "main", repositories: [], requireGreenBuilds: true
    })], "a new entry: the server's default branch, the whole workspace, builds required");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [stored({ workspace: "acme-ws", allow_comment: true })],
        [stored(Object.assign({}, ON, {
            branch: "release/2.x", repositories: ["svc-a", "svc-b"], require_green_builds: false
        }), "Builtin:Bitbucket/")]
    ), [expected({
        reason: "approve", workspace: "acme-ws", branch: "release/2.x",
        repositories: ["svc-a", "svc-b"], requireGreenBuilds: false
    })], "an entry that had it off, in another spelling");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(undefined, [on()]).map((a) => a.reason), ["approve"]);
    assert.deepEqual(bitbucketEntry.approvalsToAsk([on()], [on({ workspace: "other-ws" })]), [expected({
        reason: "approve", workspace: "other-ws", branch: "main", repositories: [], requireGreenBuilds: true
    })], "another workspace is newly approved, and said in full");
});

QUnit.test("a save that changes nothing about approving, or narrows it, asks nothing", function (assert) {
    assert.deepEqual(bitbucketEntry.approvalsToAsk([on()], [on()]), []);
    assert.deepEqual(bitbucketEntry.approvalsToAsk([on()], [on({ branch: "main" })]), [],
        "the default branch written out is the same branch");
    assert.deepEqual(bitbucketEntry.approvalsToAsk([on()], []), [], "removed");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [on()], [stored({ workspace: "acme-ws", allow_comment: true })]), [], "approve switched off");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [on({ branch: "develop", repositories: ["svc-a"] })],
        [stored({ workspace: "acme-ws", allow_comment: true, branch: "main", require_green_builds: false })]), [],
    "nothing widens on an entry that does not approve after the save");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [], [stored({ workspace: "acme-ws", allow_comment: true, allow_approve: "true" })]), [],
    "only exactly true approves");
    assert.deepEqual(bitbucketEntry.approvalsToAsk([on({ require_green_builds: false })], [on()]), [],
        "the builds requirement switched back on");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [on({ repositories: ["svc-a", "svc-b"] })], [on({ repositories: ["svc-b"] })]), [],
    "a repository removed");
    assert.deepEqual(bitbucketEntry.approvalsToAsk([on()], [on({ repositories: ["svc-a"] })]), [],
        "the whole workspace cut down to a list");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [on({ repositories: ["svc-a", "svc-b"] })], [on({ repositories: ["svc-b", "svc-a"] })]), [],
    "the same repositories in another order");
    assert.deepEqual(bitbucketEntry.approvalsToAsk(
        [], [{ url: "builtin:jira", auth_mode: "destination", oauth: ON } as McpServer]), [],
    "another toolset's block is not read");
});

QUnit.test("widening an entry that already approves is asked about, naming exactly what widens", function (assert) {
    const widened = (before: McpServer, after: McpServer) => bitbucketEntry.approvalsToAsk([before], [after]);
    assert.deepEqual(widened(on(), on({ require_green_builds: false })), [expected({
        reason: "widened", workspace: "acme-ws", branch: "main", repositories: [], requireGreenBuilds: false,
        buildsDropped: true
    })], "the build requirement dropped");
    assert.deepEqual(widened(on({ require_green_builds: true }), on({ require_green_builds: false }))
        .map((a) => a.buildsDropped), [true]);
    assert.deepEqual(widened(on({ require_green_builds: false }), on({ require_green_builds: false })), [],
        "it was off already");

    assert.deepEqual(widened(on({ repositories: ["svc-a"] }), on()), [expected({
        reason: "widened", workspace: "acme-ws", branch: "main", repositories: [], requireGreenBuilds: true,
        wholeWorkspace: true
    })], "a list replaced by the whole workspace");
    assert.deepEqual(widened(on({ repositories: ["svc-a"] }), on({ repositories: [] })).map((a) => a.wholeWorkspace),
        [true], "an empty list is the whole workspace too");
    assert.deepEqual(
        widened(on({ repositories: ["svc-a", "svc-b"] }), on({ repositories: ["svc-c", "svc-a", "svc-d"] })),
        [expected({
            reason: "widened", workspace: "acme-ws", branch: "main",
            repositories: ["svc-c", "svc-a", "svc-d"], requireGreenBuilds: true,
            repositoriesAdded: ["svc-c", "svc-d"]
        })], "repositories added, also when another one was removed");

    assert.deepEqual(widened(on({ branch: "develop" }), on()), [expected({
        reason: "widened", workspace: "acme-ws", branch: "main", repositories: [], requireGreenBuilds: true,
        branchBefore: "develop"
    })], "another branch");
    assert.deepEqual(widened(on(), on({ branch: "release/2.x" })).map((a) => a.branchBefore), ["main"]);

    assert.deepEqual(
        widened(on({ repositories: ["svc-a"] }),
            on({ branch: "develop", repositories: ["svc-a", "svc-b"], require_green_builds: false })),
        [{
            reason: "widened", workspace: "acme-ws", branch: "develop", repositories: ["svc-a", "svc-b"],
            requireGreenBuilds: false, branchBefore: "main", repositoriesAdded: ["svc-b"],
            wholeWorkspace: false, buildsDropped: true
        }], "all three at once, in one ask");
    assert.deepEqual(
        widened(on({ repositories: ["svc-a", "svc-b"], require_green_builds: false }),
            on({ repositories: ["svc-a", "svc-c"] })).map((a) => [a.repositoriesAdded, a.buildsDropped]),
        [[["svc-c"], false]], "a narrowing beside a widening is not listed");
});

// --- the texts ---------------------------------------------------------------

/** The floor: the texts the dialog and the Save question are known to use.
 *  The test below also reads the keys out of the fragment and the controller,
 *  so a key added there without a text fails as well. */
const BITBUCKET_I18N_KEYS = [
    "builtinBitbucket", "builtinBitbucketDesc", "bitbucketWorkspaceLabel", "bitbucketWorkspaceHint",
    "bitbucketRepositoriesLabel", "bitbucketRepositoriesPlaceholder", "bitbucketRepositoriesHint",
    "bitbucketBranchLabel", "bitbucketBranchHint", "bitbucketAllowCommentLabel", "bitbucketAllowCommentHint",
    "bitbucketAllowApproveLabel", "bitbucketAllowApproveHint", "bitbucketGreenBuildsLabel",
    "bitbucketGreenBuildsHint", "bitbucketApproveSaveTitle", "bitbucketApproveSaveQuestion",
    "bitbucketApproveSaveQuestionNoBuilds", "bitbucketAllRepositories", "bitbucketSomeRepositories",
    "bitbucketWidenSaveTitle", "bitbucketWidenSaveQuestion", "bitbucketWidenBranch",
    "bitbucketWidenRepositoriesAll", "bitbucketWidenRepositoriesAdded", "bitbucketWidenBuilds"
];

async function source(resource: string): Promise<string> {
    const response = await fetch(sap.ui.require.toUrl(resource));
    return response.ok ? response.text() : "";
}

function keysIn(text: string, pattern: RegExp): string[] {
    const found: string[] = [];
    let match = pattern.exec(text);
    while (match) {
        if (found.indexOf(match[1]) === -1) {
            found.push(match[1]);
        }
        match = pattern.exec(text);
    }
    return found;
}

QUnit.test("every text of the bitbucket entry exists", async function (assert) {
    const bundle = await ResourceBundle.create({
        url: sap.ui.require.toUrl("com/agent/admin/i18n/i18n.properties"), async: true
    }) as ResourceBundle;
    const fragment = keysIn(
        await source("com/agent/admin/fragment/McpServerDialog.fragment.xml"), /i18n>(bitbucket\w+)/g);
    // Every string literal of the controller that has the form of a bitbucket text key.
    const controller = keysIn(
        await source("com/agent/admin/controller/AgentDetail.controller.js"), /["'`](bitbucket[A-Z]\w*)["'`]/g);
    assert.ok(fragment.length >= 12, `the fragment's keys were read (${fragment.length})`);
    assert.ok(controller.length >= 7, `the controller's keys were read (${controller.length})`);
    BITBUCKET_I18N_KEYS.concat(fragment, controller).forEach((key) => {
        assert.ok(bundle.hasText(key), key);
    });
    const args = ["agent-x", "release/2.x", "ws-a", "all of them"];
    const named = (key: string, given: string[]): void => {
        const text = bundle.getText(key, given) || "";
        given.forEach((arg) => {
            assert.ok(text.indexOf(arg) > -1, `${key} names ${arg}`);
        });
        assert.strictEqual(text.indexOf("{"), -1, `${key} has no open placeholder`);
    };
    named("bitbucketApproveSaveQuestion", args);
    named("bitbucketApproveSaveQuestionNoBuilds", args);
    named("bitbucketWidenSaveQuestion", ["agent-x", "ws-a"]);
    named("bitbucketWidenBranch", ["release/2.x", "develop"]);
    named("bitbucketWidenRepositoriesAdded", ["\"svc-a\""]);
    named("bitbucketSomeRepositories", ["\"svc-a\""]);
});
