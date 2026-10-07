import oauthConfig from "com/agent/admin/model/oauthConfig";
import formatter from "com/agent/admin/model/formatter";
import { isRemoteUrl } from "com/agent/admin/model/remoteUrl";
import validators from "com/agent/admin/model/validators";
import type { AuthMode } from "com/agent/admin/service/types";

/** A dialog model holding every field the fragment can show, all filled in. */
function fullForm(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return Object.assign({
        dcr: false,
        client_id: "cid",
        client_secret: "sec",
        uaa_url: "https://uaa.example.com",
        authorize_url: "https://a/authorize",
        token_url: "https://a/token",
        scope: "Mail.Read",
        mailbox: "svc@example.com",
        allow_send: true,
        lookback: "2d",
        recipients: "team@example.com",
        team: "team-id",
        channels: "general",
        destination: "DEST",
        project: "ABC",
        status: "Open",
        api_base: "/rest/api/2",
        labels: "l1",
        allow_comment: true,
        min_score: "9.0",
        has_client_secret: true
    }, overrides);
}

QUnit.module("oauthConfig.cleanOAuth");

QUnit.test("an oauth2 Outlook server keeps its send config across a re-save", function (assert) {
    // The regression this module exists for: allow_send/recipients/lookback
    // were dropped for every oauth2 url but teams, so reopening an Outlook
    // server and pressing OK silently removed its send tool.
    const out = oauthConfig.cleanOAuth(fullForm(), "oauth2", "builtin:outlook") as Record<string, unknown>;
    assert.strictEqual(out.allow_send, true, "allow_send kept");
    assert.strictEqual(out.recipients, "team@example.com", "recipients kept");
    assert.strictEqual(out.lookback, "2d", "lookback kept");
    assert.strictEqual(out.client_id, "cid");
    assert.strictEqual(out.authorize_url, "https://a/authorize", "manual client fields kept");
    assert.notOk("mailbox" in out, "mailbox is app-only: a delegated token has /me");
    assert.notOk("team" in out, "teams keys are not outlook keys");
    assert.notOk("has_client_secret" in out, "read-only echo is not sent back");
});

QUnit.test("an oauth2 Teams server keeps team, channels, lookback and allow_send", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm(), "oauth2", "builtin:teams") as Record<string, unknown>;
    assert.strictEqual(out.team, "team-id");
    assert.strictEqual(out.channels, "general");
    assert.strictEqual(out.lookback, "2d");
    assert.strictEqual(out.allow_send, true);
    assert.notOk("recipients" in out);
});

QUnit.test("a remote oauth2 server stores client fields only", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm(), "oauth2", "https://mcp.example.hana.ondemand.com/mcp");
    assert.deepEqual(Object.keys(out as object).sort(),
        ["authorize_url", "client_id", "client_secret", "scope", "token_url", "uaa_url"]);
});

QUnit.test("allow_send is only ever sent as true", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ allow_send: false }), "oauth2", "builtin:outlook") as Record<string, unknown>;
    assert.notOk("allow_send" in out, "off means absent, so an export gains no field it never asked for");
});

QUnit.test("app-only keeps the client-credentials shape for any url", function (assert) {
    // Mirrors _CC_KEYS in agents/db.py: no authorize_url (nobody signs in),
    // plus the target mailbox and the built-in pins.
    const out = oauthConfig.cleanOAuth(fullForm(), "app_only", "builtin:outlook") as Record<string, unknown>;
    assert.deepEqual(Object.keys(out).sort(), [
        "allow_send", "channels", "client_id", "client_secret", "lookback", "mailbox",
        "recipients", "scope", "team", "token_url", "uaa_url"
    ]);
    assert.notOk("authorize_url" in out);
});

QUnit.test("app-only Teams never sends allow_send: Graph refuses application posts", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm(), "app_only", "builtin:teams") as Record<string, unknown>;
    assert.notOk("allow_send" in out);
});

QUnit.test("dcr on oauth2 reduces to the flag and an optional scope", function (assert) {
    assert.deepEqual(oauthConfig.cleanOAuth(fullForm({ dcr: true }), "oauth2", "builtin:outlook"),
        { dcr: true, scope: "Mail.Read" });
    assert.deepEqual(oauthConfig.cleanOAuth(fullForm({ dcr: true, scope: " " }), "oauth2"), { dcr: true });
});

QUnit.test("a public built-in on none keeps only the whitelisted knobs", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm(), "none", "builtin:sapnotes") as Record<string, unknown>;
    assert.deepEqual(Object.keys(out).sort(), validators.BUILTIN_PUBLIC_KEYS.slice().sort());
    assert.strictEqual(oauthConfig.cleanOAuth(fullForm({ min_score: "", lookback: "" }), "none", "builtin:sapnotes"),
        undefined, "nothing set means no block at all");
});

QUnit.test("destination keeps Jira filters, and Slack its channel pin", function (assert) {
    const jira = oauthConfig.cleanOAuth(fullForm(), "destination", "builtin:jira") as Record<string, unknown>;
    assert.deepEqual(Object.keys(jira).sort(),
        ["allow_comment", "api_base", "destination", "labels", "lookback", "project", "status"]);
    assert.notOk("client_id" in jira, "a destination server stores no credential");
    const slack = oauthConfig.cleanOAuth(fullForm(), "destination", "builtin:slack") as Record<string, unknown>;
    assert.deepEqual(Object.keys(slack).sort(), ["allow_send", "channels", "destination", "lookback"]);
});

QUnit.test("blank values are dropped, not stored as empty strings", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ scope: "  ", recipients: "" }), "oauth2", "builtin:outlook") as Record<string, unknown>;
    assert.notOk("scope" in out);
    assert.notOk("recipients" in out);
});

QUnit.module("oauthConfig.keepsAllowSend");

QUnit.test("agrees with where the dialog shows the switch", function (assert) {
    // Same truth table as the `visible` expression on oauthAllowSend in
    // McpServerDialog.fragment.xml; the two drifting apart is the bug.
    const cases: [string, AuthMode, boolean][] = [
        ["builtin:outlook", "oauth2", true],
        ["builtin:outlook", "app_only", true],
        ["builtin:teams", "oauth2", true],
        ["builtin:teams", "app_only", false],
        ["builtin:slack", "destination", true],
        ["builtin:jira", "destination", false],
        ["builtin:gmail", "oauth2", true],
        ["https://mcp.example.hana.ondemand.com/mcp", "oauth2", false],
        ["https://mcp.example.hana.ondemand.com/mcp", "app_only", true]
    ];
    cases.forEach(([url, mode, expected]) => {
        assert.strictEqual(oauthConfig.keepsAllowSend(url, mode), expected, `${url} on ${mode}`);
    });
});

// --- destinations ---
QUnit.module("oauthConfig.cleanOAuth on a destination, per built-in");

QUnit.test("outlook keeps mailbox, lookback, recipients and both switches", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", "builtin:outlook") as Record<string, unknown>;
    assert.deepEqual(Object.keys(out).sort(),
        ["allow_send", "destination", "lookback", "mailbox", "recipients", "user_context"]);
    assert.strictEqual(out.user_context, true);
    assert.notOk("client_id" in out, "a destination server stores no credential");
    assert.notOk("project" in out, "Jira's filters are not Outlook keys");
});

QUnit.test("gmail keeps only the mailbox and the user-context switch", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", "builtin:gmail") as Record<string, unknown>;
    assert.deepEqual(Object.keys(out).sort(), ["destination", "mailbox", "user_context"]);
    assert.notOk("allow_send" in out, "Gmail has no send tool to switch on");
});

QUnit.test("teams keeps its pins, and allow_send only as the signed-in user", function (assert) {
    const app = oauthConfig.cleanOAuth(fullForm(), "destination", "builtin:teams") as Record<string, unknown>;
    assert.deepEqual(Object.keys(app).sort(), ["channels", "destination", "lookback", "team"]);
    assert.notOk("allow_send" in app, "an app-level destination credential cannot post");
    const user = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", "builtin:teams") as Record<string, unknown>;
    assert.strictEqual(user.allow_send, true);
    assert.strictEqual(user.user_context, true);
});

QUnit.test("sapnotes keeps its public knobs; sapnotedetail only the name", function (assert) {
    const notes = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", "builtin:sapnotes") as Record<string, unknown>;
    assert.deepEqual(Object.keys(notes).sort(), ["destination", "lookback", "min_score"]);
    assert.notOk("user_context" in notes, "NVD has no user to act as");
    const detail = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", "builtin:sapnotedetail");
    assert.deepEqual(detail, { destination: "DEST" });
});

QUnit.test("smtp keeps its recipients, sender and send switch, nothing else", function (assert) {
    const out = oauthConfig.cleanOAuth(
        fullForm({ user_context: true, from: "reports@example.com" }), "destination", "builtin:smtp"
    );
    assert.deepEqual(out, {
        destination: "DEST", recipients: "team@example.com",
        from: "reports@example.com", allow_send: true
    });
    assert.notOk(oauthConfig.supportsUserContext("builtin:smtp"), "the relay credential is the app's");
});

QUnit.test("the switches are only ever sent as true", function (assert) {
    const out = oauthConfig.cleanOAuth(
        fullForm({ user_context: false, allow_send: false }), "destination", "builtin:outlook"
    ) as Record<string, unknown>;
    assert.notOk("user_context" in out);
    assert.notOk("allow_send" in out);
});

QUnit.test("keepsAllowSend on a destination follows the user-context switch for Teams", function (assert) {
    assert.strictEqual(oauthConfig.keepsAllowSend("builtin:teams", "destination", false), false);
    assert.strictEqual(oauthConfig.keepsAllowSend("builtin:teams", "destination", true), true);
    assert.strictEqual(oauthConfig.keepsAllowSend("builtin:outlook", "destination", false), true);
    assert.strictEqual(oauthConfig.keepsAllowSend("builtin:gmail", "destination", true), false);
    assert.strictEqual(oauthConfig.keepsAllowSend("builtin:slack", "destination"), true);
});

QUnit.test("supportsUserContext names the built-ins that act as a user", function (assert) {
    ["builtin:gmail", "builtin:outlook", "builtin:teams"].forEach((url) => {
        assert.ok(oauthConfig.supportsUserContext(url), url);
    });
    // A remote MCP server may too (Task 5b): see the remote-server module below.
    ["builtin:jira", "builtin:slack", "builtin:smtp", "builtin:sapnotes", "builtin:sapnotedetail"
    ].forEach((url) => {
        assert.notOk(oauthConfig.supportsUserContext(url), url);
    });
});

QUnit.module("oauthConfig — mail theme");

const THEME = { band: "#102030", logo_url: "https://example.com/logo.png", org_name: "Example" };

QUnit.test("the mail built-ins keep a theme object on every mode they send from", function (assert) {
    const smtp = oauthConfig.cleanOAuth(fullForm({ theme: THEME }), "destination", "builtin:smtp") as Record<string, unknown>;
    assert.deepEqual(smtp.theme, THEME, "smtp on destination");
    (["oauth2", "app_only", "destination"] as AuthMode[]).forEach((mode) => {
        const out = oauthConfig.cleanOAuth(fullForm({ theme: THEME }), mode, "builtin:outlook") as Record<string, unknown>;
        assert.deepEqual(out.theme, THEME, `outlook on ${mode}`);
    });
});

QUnit.test("a theme is dropped where no report mail is sent, and when empty", function (assert) {
    const jira = oauthConfig.cleanOAuth(fullForm({ theme: THEME }), "destination", "builtin:jira") as Record<string, unknown>;
    assert.notOk("theme" in jira, "jira sends no mail");
    const teams = oauthConfig.cleanOAuth(fullForm({ theme: THEME }), "oauth2", "builtin:teams") as Record<string, unknown>;
    assert.notOk("theme" in teams, "teams sends no mail");
    const empty = oauthConfig.cleanOAuth(fullForm({ theme: {} }), "destination", "builtin:smtp") as Record<string, unknown>;
    assert.notOk("theme" in empty, "an empty theme is no theme");
    assert.ok(oauthConfig.supportsMailTheme("builtin:outlook"));
    assert.notOk(oauthConfig.supportsMailTheme("builtin:gmail"));
});

QUnit.test("parseMailTheme reads the JSON textarea", function (assert) {
    assert.deepEqual(oauthConfig.parseMailTheme(""), { error: "" }, "blank means no theme");
    assert.deepEqual(oauthConfig.parseMailTheme("  "), { error: "" });
    assert.deepEqual(oauthConfig.parseMailTheme(JSON.stringify(THEME)), { theme: THEME, error: "" });
    const broken = oauthConfig.parseMailTheme("{ band: #102030 }");
    assert.notOk(broken.theme);
    assert.ok(/JSON/.test(broken.error), broken.error);
    assert.ok(/object/.test(oauthConfig.parseMailTheme("[1, 2]").error), "an array is refused");
    assert.ok(/object/.test(oauthConfig.parseMailTheme("\"#102030\"").error), "a string is refused");
});

QUnit.test("formatMailTheme round-trips what parseMailTheme reads", function (assert) {
    assert.strictEqual(oauthConfig.formatMailTheme(undefined), "");
    assert.strictEqual(oauthConfig.formatMailTheme({}), "");
    const text = oauthConfig.formatMailTheme(THEME);
    assert.deepEqual(oauthConfig.parseMailTheme(text).theme, THEME);
});

// --- destinations: a remote MCP server ---
// The destination holds URL and credential; the server stores only its name
// and whose credential it hands back (`_clean_destination` in agents/db.py).
// Dropping user_context on a re-save would silently switch every caller to
// the destination's technical credential.
QUnit.module("oauthConfig.cleanOAuth on a destination, remote MCP server");

const REMOTE = "https://arc1.example.com/mcp";

QUnit.test("posts exactly {destination, user_context}", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", REMOTE);
    assert.deepEqual(out, { destination: "DEST", user_context: true },
        "no Jira filters, no client fields, no allow_send");
});

QUnit.test("an app-level destination posts the name alone", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ user_context: false }), "destination", REMOTE);
    assert.deepEqual(out, { destination: "DEST" });
});

QUnit.test("only a boolean true acts as the user", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ user_context: "true" }), "destination", REMOTE);
    assert.deepEqual(out, { destination: "DEST" });
});

QUnit.test("a stored user-context server round-trips unchanged", function (assert) {
    // What the server returns for the server, loaded into the dialog model
    // as-is and sent back on OK.
    const stored = { destination: "arc1-abap-readonly", user_context: true };
    const out = oauthConfig.cleanOAuth(Object.assign({}, stored), "destination", REMOTE);
    assert.deepEqual(out, stored);
});

QUnit.test("the name is trimmed", function (assert) {
    const out = oauthConfig.cleanOAuth({ destination: "  arc1-abap-readonly " }, "destination", REMOTE);
    assert.deepEqual(out, { destination: "arc1-abap-readonly" });
});

QUnit.test("a remote server may act as the signed-in user; Jira still may not", function (assert) {
    assert.ok(oauthConfig.supportsUserContext(REMOTE), "https url");
    assert.ok(oauthConfig.isRemote(REMOTE), "isRemote for https");
    assert.notOk(oauthConfig.isRemote("builtin:jira"), "a built-in is not remote");
    assert.notOk(oauthConfig.isRemote(""), "no url is not remote");
    assert.notOk(oauthConfig.supportsUserContext("builtin:jira"));
    const jira = oauthConfig.cleanOAuth(fullForm({ user_context: true }), "destination", "builtin:jira") as Record<string, unknown>;
    assert.notOk("user_context" in jira, "Jira keeps its own shape");
});

// FIX-14: the dialog's user_context switch, cleanOAuth and the validators
// decide "remote" through one http(s) helper, so the switch is never shown
// for a url whose user_context cleanOAuth would then drop.
QUnit.test("isRemote is the shared http(s) helper", function (assert) {
    for (const url of [REMOTE, "http://x/mcp", "  HTTPS://X/mcp ", "", "builtin:gmail", "ftp://x", "mcp", "https:/x"]) {
        assert.strictEqual(oauthConfig.isRemote(url), isRemoteUrl(url), JSON.stringify(url));
    }
    assert.ok(isRemoteUrl("  HTTPS://X/mcp "), "case and whitespace are tolerated");
});

QUnit.test("the user_context switch shows exactly where cleanOAuth keeps user_context", function (assert) {
    for (const url of [REMOTE, "  HTTPS://X/mcp ", "", "ftp://x", "mcp.example.com", "builtin:gmail",
        "builtin:outlook", "builtin:teams", "builtin:jira", "builtin:slack", "builtin:sapnotes"]) {
        const kept = "user_context" in (oauthConfig.cleanOAuth(
            fullForm({ user_context: true }), "destination", url) as Record<string, unknown>);
        assert.strictEqual(formatter.userContextVisible("destination", url), kept, `destination ${JSON.stringify(url)}`);
        assert.strictEqual(formatter.userContextVisible("jwt", url), false, `jwt ${JSON.stringify(url)}`);
    }
});

QUnit.module("oauthConfig.defaultUserContext");

QUnit.test("on only for a remote url behind a destination", function (assert) {
    assert.strictEqual(oauthConfig.defaultUserContext("destination", "https://arc1.example.com/mcp"), true);
    assert.strictEqual(oauthConfig.defaultUserContext("destination", " HTTP://x.example.com "), true);
    assert.strictEqual(oauthConfig.defaultUserContext("destination", "builtin:outlook"), false, "built-ins unchanged");
    assert.strictEqual(oauthConfig.defaultUserContext("destination", ""), false);
    assert.strictEqual(oauthConfig.defaultUserContext("jwt", "https://arc1.example.com/mcp"), false);
});

// --- odata ---
QUnit.module("oauthConfig: the builtin:odata entry");

QUnit.test("an odata entry keeps exactly the services and allow_write as a real boolean", function (assert) {
    assert.deepEqual(
        oauthConfig.cleanOAuth(fullForm({ services: ["b", "a", "a"], allow_write: true, lookback: "2d" }), "destination", "builtin:odata"),
        { services: ["b", "a"], allow_write: true },
        "no destination, no user_context, no key of another toolset; duplicates dropped, order kept"
    );
    assert.deepEqual(
        oauthConfig.cleanOAuth({ services: ["a"], allow_write: false }, "destination", "builtin:odata"),
        { services: ["a"], allow_write: false }, "unticked is sent as false, not left out"
    );
    assert.deepEqual(
        oauthConfig.cleanOAuth({ services: ["a"] }, "destination", "builtin:odata"),
        { services: ["a"], allow_write: false }, "a missing key is false"
    );
    assert.strictEqual(oauthConfig.supportsUserContext("builtin:odata"), false, "the entry has no identity switch");
});

QUnit.test("only the boolean true opens writes", function (assert) {
    for (const value of ["true", "TRUE", 1, "1", "on", {}, [], [true], null, undefined, "false", 0]) {
        const cleaned = oauthConfig.cleanOAuth({ services: ["a"], allow_write: value }, "destination", "builtin:odata") as
            Record<string, unknown>;
        assert.strictEqual(cleaned.allow_write, false, `${JSON.stringify(value)} is sent as the boolean false`);
    }
    assert.strictEqual(
        (oauthConfig.cleanOAuth({ services: ["a"], allow_write: true }, "destination", "builtin:odata") as
            Record<string, unknown>).allow_write, true
    );
});

QUnit.test("no other toolset keeps services or allow_write", function (assert) {
    for (const [url, mode] of [["builtin:jira", "destination"], ["builtin:outlook", "destination"], ["builtin:slack", "destination"],
        ["https://arc1.example.com/mcp", "destination"], ["builtin:gmail", "oauth2"], ["builtin:outlook", "app_only"],
        ["builtin:sapnotes", "none"]] as [string, AuthMode][]) {
        const cleaned = (oauthConfig.cleanOAuth(fullForm({ services: ["a"], allow_write: true }), mode, url) ?? {}) as
            Record<string, unknown>;
        assert.notOk("services" in cleaned || "allow_write" in cleaned, `${url} on ${mode}`);
    }
});

// --- sharepoint ---
QUnit.module("oauthConfig — sharepoint");

const SP_VIEWS = { team: { kind: "table", table: "TeamMembers", columns: ["Name", "ID"] } };
const SP_PINS = { site: "example.sharepoint.com:/sites/planning", library: "Documents",
    path: "Team/Planning 2026.xlsx" };

QUnit.test("on a destination it keeps exactly the name, the pins and the views", function (assert) {
    const out = oauthConfig.cleanOAuth(
        fullForm({ destination: " GRAPH ", user_context: true, views: SP_VIEWS, ...SP_PINS }),
        "destination", "builtin:sharepoint") as Record<string, unknown>;
    assert.deepEqual(out, { destination: "GRAPH", ...SP_PINS, views: SP_VIEWS });
});

QUnit.test("app-only keeps the client fields, the pins and the views, no mailbox or switch", function (assert) {
    const out = oauthConfig.cleanOAuth(fullForm({ views: SP_VIEWS, ...SP_PINS }),
        "app_only", "builtin:sharepoint") as Record<string, unknown>;
    assert.deepEqual(Object.keys(out).sort(),
        ["client_id", "client_secret", "library", "path", "scope", "site", "token_url", "uaa_url", "views"]);
});

QUnit.test("no other toolset keeps the pins or the views", function (assert) {
    const teams = oauthConfig.cleanOAuth(fullForm({ destination: "D", views: SP_VIEWS, ...SP_PINS }),
        "destination", "builtin:teams") as Record<string, unknown>;
    ["site", "library", "path", "views"].forEach((k) => assert.notOk(k in teams, k));
    const app = oauthConfig.cleanOAuth(fullForm({ views: SP_VIEWS, ...SP_PINS }),
        "app_only", "builtin:outlook") as Record<string, unknown>;
    ["site", "library", "path", "views"].forEach((k) => assert.notOk(k in app, k));
});

QUnit.test("parseViews reads the JSON textarea and formatViews round-trips it", function (assert) {
    assert.deepEqual(oauthConfig.parseViews(""), { error: "" }, "blank is no views (the validator says required)");
    assert.deepEqual(oauthConfig.parseViews(JSON.stringify(SP_VIEWS)), { views: SP_VIEWS, error: "" });
    assert.ok(oauthConfig.parseViews("{ team: }").error.indexOf("not valid JSON") > -1);
    assert.ok(oauthConfig.parseViews("[1]").error.indexOf("JSON object") > -1);
    assert.deepEqual(oauthConfig.parseViews(oauthConfig.formatViews(SP_VIEWS)).views, SP_VIEWS);
    assert.strictEqual(oauthConfig.formatViews(undefined), "");
    assert.ok(oauthConfig.supportsViews("Builtin:SharePoint/"));
    assert.notOk(oauthConfig.supportsViews("builtin:teams"));
});
