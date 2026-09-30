import oauthConfig from "com/agent/admin/model/oauthConfig";
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
        ["builtin:gmail", "oauth2", false],
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
    ["builtin:jira", "builtin:slack", "builtin:smtp", "builtin:sapnotes", "builtin:sapnotedetail",
        "https://mcp.example.hana.ondemand.com/mcp"].forEach((url) => {
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
