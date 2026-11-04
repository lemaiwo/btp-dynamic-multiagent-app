import gitEntry from "com/agent/admin/model/gitEntry";
import oauthConfig from "com/agent/admin/model/oauthConfig";
import validators from "com/agent/admin/model/validators";
import { authModesFor, findBuiltin } from "com/agent/admin/model/builtins";
import type { McpServer } from "com/agent/admin/service/types";

const URL = "builtin:git";
/** A dialog model holding fields of every other toolset as well. */
function form(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return Object.assign({
        dcr: false, client_id: "cid", client_secret: "sec", mailbox: "x@example.com",
        allow_send: true, lookback: "2d", project: "ABC", status: "Open", api_base: "/api/2",
        labels: "l1", user_context: true, has_client_secret: true, workspace: "ws", branch: "main",
        allow_comment: true, allow_approve: true, require_green_builds: false,
        destination: "GITHUB", provider: "github", repositories: ["acme/web", "acme/api"],
        base_branch: "develop", branch_prefix: "bot/", allow_push: true, allow_pull_request: true
    }, overrides);
}

function entry(oauth: Record<string, unknown>): McpServer {
    return { url: URL, auth_mode: "destination", oauth: oauth as McpServer["oauth"] };
}

QUnit.module("gitEntry");

QUnit.test("git is a destination-only builtin", function (assert) {
    assert.strictEqual(findBuiltin("Builtin:Git/")?.url, URL);
    assert.strictEqual(findBuiltin(URL)?.titleKey, "builtinGit");
    assert.deepEqual(authModesFor(URL), ["destination"]);
    assert.strictEqual(validators.validateServerUrl(URL, "destination"), "");
    assert.ok(gitEntry.isGitUrl("BUILTIN:GIT/"));
    assert.notOk(gitEntry.isGitUrl("builtin:github"));
    assert.notOk(oauthConfig.supportsUserContext(URL), "no signed-in-user switch");
    assert.strictEqual(oauthConfig.keepsAllowSend(URL, "destination"), false);
});

QUnit.test("cleanOAuth sends exactly the keys the server stores", function (assert) {
    const out = oauthConfig.cleanOAuth(form(), "destination", URL) as Record<string, unknown>;
    assert.deepEqual(out, {
        destination: "GITHUB", provider: "github", repositories: ["acme/web", "acme/api"],
        base_branch: "develop", branch_prefix: "bot/", allow_push: true, allow_pull_request: true
    });
    assert.strictEqual(gitEntry.validate(out, "destination"), "");
});

QUnit.test("switches only as true; a blank base branch and prefix are the server's defaults", function (assert) {
    const out = gitEntry.clean(form({
        base_branch: "", branch_prefix: "", allow_push: false, allow_pull_request: "true"
    }));
    assert.deepEqual(out, { destination: "GITHUB", provider: "github", repositories: ["acme/web", "acme/api"] });
    assert.strictEqual(gitEntry.validate(out, "destination"), "", "the minimal entry is valid");
});

QUnit.test("values are sent as typed, edge whitespace included, and refused before the save", function (assert) {
    const out = gitEntry.clean(form({ destination: " GITHUB", base_branch: "main ", branch_prefix: " agent/" }));
    assert.strictEqual(out.destination, " GITHUB");
    assert.strictEqual(out.base_branch, "main ");
    assert.strictEqual(out.branch_prefix, " agent/");
    assert.ok(gitEntry.validate(out, "destination"));
    assert.ok(gitEntry.validate({ ...out, destination: "GITHUB", branch_prefix: "agent/" }, "destination"),
        "the base branch alone is refused");
    assert.ok(gitEntry.validate({ ...out, destination: "GITHUB", base_branch: "main" }, "destination"),
        "the prefix alone is refused");
});

QUnit.test("the repositories field splits on commas and drops empties", function (assert) {
    assert.deepEqual(gitEntry.parseRepositories(" acme/web, ,acme/api ,"), ["acme/web", "acme/api"]);
    assert.deepEqual(gitEntry.parseRepositories(" , "), []);
    assert.deepEqual(gitEntry.parseRepositories(undefined), []);
    assert.strictEqual(gitEntry.formatRepositories(["acme/web", "acme/api"]), "acme/web, acme/api");
    assert.strictEqual(gitEntry.formatRepositories("acme/web"), "", "not a list: shown blank");
});

QUnit.test("validate: each rule with a fixed text that never quotes a value", function (assert) {
    const ok = gitEntry.clean(form());
    const secret = "SECRETVALUE";
    const cases: [string, Record<string, unknown>, string][] = [
        ["no destination", { ...ok, destination: "" }, "destination name"],
        ["a destination with a space", { ...ok, destination: `${secret} X` }, "destination name"],
        ["no provider", { ...ok, provider: "" }, "provider"],
        ["an unknown provider", { ...ok, provider: secret }, "provider"],
        ["no repository", { ...ok, repositories: [] }, "1 to 50"],
        ["51 repositories", { ...ok, repositories: Array.from({ length: 51 }, (_, i) => `acme/r${i}`) }, "1 to 50"],
        ["a GitHub path without owner", { ...ok, repositories: [secret] }, "owner/name"],
        ["a GitHub path with a subgroup", { ...ok, repositories: [`acme/${secret}/x`] }, "owner/name"],
        ["a Bitbucket path without workspace", { ...ok, provider: "bitbucket", repositories: [secret] }, "workspace/slug"],
        ["a GitLab path without group", { ...ok, provider: "gitlab", repositories: [secret] }, "GitLab project path"],
        ["a .. segment", { ...ok, repositories: ["acme/.."] }, "owner/name"],
        ["a space in a path", { ...ok, repositories: [`acme/${secret} x`] }, "owner/name"],
        ["a repeated path", { ...ok, repositories: ["acme/web", "acme/web"] }, "twice"],
        ["a base branch with ..", { ...ok, base_branch: `a..${secret}` }, "base branch"],
        ["a prefix without /", { ...ok, branch_prefix: secret }, "branch prefix"],
        ["a prefix with two segments", { ...ok, branch_prefix: "a/b/" }, "branch prefix"],
        ["a Bitbucket path in upper case", { ...ok, provider: "bitbucket", repositories: ["WS/Repo"] }, "workspace/slug"],
        ["a GitLab path ending in .git", { ...ok, provider: "gitlab", repositories: ["grp/proj.git"] }, "GitLab project path"],
        ["a base branch that carries the prefix", { ...ok, base_branch: "bot/main" }, "must not carry"],
        ["a base branch under the default prefix", { ...ok, base_branch: "agent/x", branch_prefix: undefined }, "must not carry"],
        ["a switch as a string", { ...ok, allow_push: "true" }, "switch"],
        ["user_context", { ...ok, user_context: true }, "signed-in user"]
    ];
    cases.forEach(([what, cfg, expected]) => {
        const message = gitEntry.validate(cfg, "destination");
        assert.ok(message.indexOf(expected) > -1, `${what}: ${message}`);
        assert.strictEqual(message.indexOf(secret), -1, `${what}: the value is not quoted`);
    });
    assert.ok(gitEntry.validate(ok, "jwt").indexOf("destination") > -1, "only the destination mode");
});

QUnit.test("GitLab takes subgroups; GitHub and Bitbucket take exactly two segments", function (assert) {
    const ok = gitEntry.clean(form());
    assert.strictEqual(gitEntry.validate({ ...ok, provider: "gitlab", repositories: ["grp/sub/proj", "grp/proj"] }, "destination"), "");
    assert.strictEqual(gitEntry.validate({ ...ok, provider: "bitbucket", repositories: ["ws/repo-a"] }, "destination"), "");
});

QUnit.test("validateServers refuses a second Git entry, in any spelling", function (assert) {
    const oauth = gitEntry.clean(form());
    const errors = validators.validateServers([entry(oauth), { ...entry(oauth), url: "Builtin:Git/" }]);
    assert.strictEqual(errors[1], "An agent has one Git entry; add the repositories to the existing one.");
    assert.notOk(0 in errors, "the first one is fine");
    assert.deepEqual(validators.validateServers([entry(oauth)]), {}, "one entry is fine");
});
