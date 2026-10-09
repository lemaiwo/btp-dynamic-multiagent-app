import validators, { validateDeep, validateWorkflowSteps } from "com/agent/admin/model/validators";
import { DEEP_DEFAULTS } from "com/agent/admin/service/types";
import type { WorkflowStep } from "com/agent/admin/service/types";

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
    const msg = validators.validateServerUrl("builtin:discord", "none");
    assert.ok(msg.length > 0, "returns a message");
    assert.ok(msg.indexOf("builtin:gmail") > -1, "lists the known builtins");
});

QUnit.test("a blank url is rejected", function (assert) {
    assert.notStrictEqual(validators.validateServerUrl("   ", "jwt"), "");
});

QUnit.test("builtin:jira is a known toolset URL", (assert) => {
    assert.strictEqual(validators.validateServerUrl("builtin:jira", "destination"), "");
});

QUnit.test("accepts builtin:sapnotes", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:sapnotes", "none"), "");
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

QUnit.test("destination mode requires a destination name", (assert) => {
    const error = validators.validateOAuth({ project: "ABC" }, "destination", "builtin:jira");
    assert.ok(error.length > 0, "an error is returned");
    assert.ok(
        error.toLowerCase().indexOf("destination") > -1,
        "the message names the missing field"
    );
});

QUnit.test("destination mode accepts a name alone", (assert) => {
    assert.strictEqual(
        validators.validateOAuth(
            { destination: "MY_JIRA_DESTINATION" },
            "destination",
            "builtin:jira"
        ),
        ""
    );
});

QUnit.test("destination mode accepts a proxy API base path", (assert) => {
    assert.strictEqual(
        validators.validateOAuth(
            { destination: "MY_JIRA_DESTINATION", api_base: "/api/2" },
            "destination",
            "builtin:jira"
        ),
        ""
    );
});

QUnit.test("destination mode refuses a URL as the API base path", (assert) => {
    // A URL here would send the destination's credential to a host the
    // destination never named.
    const error = validators.validateOAuth(
        { destination: "MY_JIRA_DESTINATION", api_base: "https://evil.example" },
        "destination",
        "builtin:jira"
    );
    assert.ok(error.length > 0, "an error is returned");
    assert.ok(error.indexOf("path, not a URL") > -1, "the message says why");
});

QUnit.test("destination mode refuses a relative API base path", (assert) => {
    const error = validators.validateOAuth(
        { destination: "MY_JIRA_DESTINATION", api_base: "rest/api/2" },
        "destination",
        "builtin:jira"
    );
    assert.ok(error.indexOf("must start with") > -1, error);
});

QUnit.test("destination mode accepts comma-separated labels and statuses", (assert) => {
    assert.strictEqual(
        validators.validateOAuth(
            {
                destination: "MY_JIRA_DESTINATION",
                status: "Open, In Progress, In Analysis",
                labels: "TEAM-X, agent"
            },
            "destination",
            "builtin:jira"
        ),
        ""
    );
});

QUnit.test("destination mode caps the number of labels", (assert) => {
    // Not a Jira limit: a pasted list becomes a query nobody can read back
    // out of a run record.
    const many = Array.from({ length: 21 }, (_, i) => `l${i}`).join(",");
    const error = validators.validateOAuth(
        { destination: "MY_JIRA_DESTINATION", labels: many },
        "destination",
        "builtin:jira"
    );
    assert.ok(error.indexOf("at most 20") > -1 || error.indexOf("At most 20") > -1, error);
});

QUnit.test("duplicate labels do not count towards the cap", (assert) => {
    // The server dedupes before building clauses, so the dialog must agree —
    // otherwise a harmless repeated label is rejected here and accepted there.
    const dupes = Array.from({ length: 30 }, () => "same").join(",");
    assert.strictEqual(
        validators.validateOAuth(
            { destination: "MY_JIRA_DESTINATION", labels: dupes },
            "destination",
            "builtin:jira"
        ),
        ""
    );
});

QUnit.test("destination mode refuses credentials", (assert) => {
    const error = validators.validateOAuth(
        { destination: "MY_JIRA_DESTINATION", client_id: "x" },
        "destination",
        "builtin:jira"
    );
    assert.ok(error.length > 0, "credentials are rejected before the request is sent");
});

QUnit.test("destination mode refuses dynamic registration", (assert) => {
    assert.ok(
        validators.validateOAuth({ dcr: true }, "destination", "builtin:jira").length > 0
    );
});

// --- destinations ---
QUnit.module("validators.validateOAuth on a destination, per built-in");

QUnit.test("the destination name has a shape", (assert) => {
    assert.ok(validators.validateOAuth({ destination: "bad name!" }, "destination", "builtin:jira")
        .indexOf("destination name") > -1);
    assert.ok(validators.validateOAuth({ destination: "x".repeat(201) }, "destination", "builtin:jira")
        .length > 0);
    assert.strictEqual(validators.validateOAuth({ destination: "My.Dest-1_a" }, "destination", "builtin:jira"), "");
});

QUnit.test("outlook and gmail need a mailbox unless acting as the signed-in user", (assert) => {
    ["builtin:outlook", "builtin:gmail"].forEach((url) => {
        assert.ok(validators.validateOAuth({ destination: "D" }, "destination", url)
            .indexOf("mailbox") > -1, `${url} without a mailbox`);
        assert.strictEqual(validators.validateOAuth(
            { destination: "D", mailbox: "svc@example.com" }, "destination", url), "");
        assert.strictEqual(validators.validateOAuth(
            { destination: "D", user_context: true }, "destination", url), "");
    });
});

QUnit.test("teams needs its team on a destination and posts only as the user", (assert) => {
    assert.ok(validators.validateOAuth({ destination: "D" }, "destination", "builtin:teams")
        .indexOf("team") > -1);
    assert.strictEqual(validators.validateOAuth(
        { destination: "D", team: "t" }, "destination", "builtin:teams"), "");
    const error = validators.validateOAuth(
        { destination: "D", team: "t", allow_send: true }, "destination", "builtin:teams");
    assert.ok(error.indexOf("signed-in user") > -1, error);
    assert.strictEqual(validators.validateOAuth(
        { destination: "D", team: "t", allow_send: true, user_context: true },
        "destination", "builtin:teams"), "");
});

QUnit.test("built-ins with no user refuse the user-context switch", (assert) => {
    ["builtin:sapnotes", "builtin:sapnotedetail", "builtin:jira", "builtin:slack"].forEach((url) => {
        assert.ok(validators.validateOAuth({ destination: "D", user_context: true }, "destination", url)
            .indexOf("no signed-in user") > -1, url);
        assert.strictEqual(validators.validateOAuth({ destination: "D" }, "destination", url), "", url);
    });
});

QUnit.test("validateServers accepts a destination on every built-in", (assert) => {
    const errors = validators.validateServers([
        { url: "builtin:gmail", auth_mode: "destination", oauth: { destination: "G", user_context: true } },
        { url: "builtin:outlook", auth_mode: "destination", oauth: { destination: "O", mailbox: "a@b" } },
        { url: "builtin:teams", auth_mode: "destination", oauth: { destination: "T", team: "t" } },
        { url: "builtin:sapnotes", auth_mode: "destination", oauth: { destination: "N" } },
        { url: "builtin:sapnotedetail", auth_mode: "destination", oauth: { destination: "S" } }
    ]);
    assert.deepEqual(errors, {});
});

// --- destinations: a remote MCP server ---
QUnit.module("validators.validateOAuth on a destination, remote MCP server");

QUnit.test("a remote server may act as the signed-in user", (assert) => {
    assert.strictEqual(validators.validateOAuth(
        { destination: "arc1-abap-readonly", user_context: true }, "destination",
        "https://arc1.example.com/mcp"), "");
});

QUnit.test("a remote server on an app-level destination is fine too", (assert) => {
    assert.strictEqual(validators.validateOAuth(
        { destination: "arc1-abap-readonly" }, "destination", "https://arc1.example.com/mcp"), "");
});

QUnit.test("a remote server still requires a destination name", (assert) => {
    const error = validators.validateOAuth({ user_context: true }, "destination", "https://arc1.example.com/mcp");
    assert.ok(error.toLowerCase().indexOf("destination name") > -1, error);
});

QUnit.test("a remote server's destination name follows the naming rule", (assert) => {
    assert.notStrictEqual(validators.validateOAuth(
        { destination: "bad name/x", user_context: true }, "destination", "https://arc1.example.com/mcp"), "");
});

QUnit.test("Jira still has no signed-in user to act as", (assert) => {
    assert.notStrictEqual(validators.validateOAuth(
        { destination: "MY_JIRA_DESTINATION", user_context: true }, "destination", "builtin:jira"), "");
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

QUnit.module("validators.validateOAuth — app-only");

const APP_ONLY = {
    client_id: "c",
    client_secret: "s",
    token_url: "https://login.microsoftonline.com/t/oauth2/v2.0/token",
    mailbox: "service@example.com"
};

QUnit.test("a complete app-only config passes", function (assert) {
    assert.strictEqual(
        validators.validateOAuth(APP_ONLY, "app_only", "builtin:outlook"),
        ""
    );
});

QUnit.test("app-only needs a client id", function (assert) {
    const { client_id, ...rest } = APP_ONLY;
    assert.notStrictEqual(
        validators.validateOAuth(rest as never, "app_only", "builtin:outlook"),
        ""
    );
});

QUnit.test("app-only needs a token url, not an authorize url", function (assert) {
    const { token_url, ...rest } = APP_ONLY;
    const error = validators.validateOAuth(
        { ...rest, authorize_url: "https://login.microsoftonline.com/t/authorize" } as never,
        "app_only",
        "builtin:outlook"
    );
    assert.ok(error.indexOf("token URL") > -1, "an authorize URL does not substitute");
});

QUnit.test("a built-in toolset needs a mailbox", function (assert) {
    const { mailbox, ...rest } = APP_ONLY;
    const error = validators.validateOAuth(
        rest as never, "app_only", "builtin:outlook"
    );
    assert.ok(error.indexOf("mailbox") > -1, "the mailbox cannot be inferred");
});

QUnit.test("a remote MCP server needs no mailbox", function (assert) {
    const { mailbox, ...rest } = APP_ONLY;
    assert.strictEqual(
        validators.validateOAuth(
            rest as never, "app_only", "https://mcp.example.com/mcp"
        ),
        "",
        "only built-ins address a mailbox"
    );
});

// DCR issues a client with no admin-consented application permissions, so its
// app-only tokens are valid and useless. Better to refuse than to hand someone
// a config that authenticates and then 403s on every call.
QUnit.test("app-only rejects dynamic registration", function (assert) {
    const error = validators.validateOAuth(
        { dcr: true }, "app_only", "builtin:outlook"
    );
    assert.ok(error.indexOf("dynamic registration") > -1, error);
});

QUnit.module("validators.validateOAuth on a public builtin");

QUnit.test("a public builtin may carry min_score on 'none'", function (assert) {
    assert.strictEqual(
        validators.validateOAuth({ min_score: "9.0" }, "none", "builtin:sapnotes"), ""
    );
});

QUnit.test("a real MCP server on 'none' still carries no config", function (assert) {
    assert.notStrictEqual(
        validators.validateOAuth({ min_score: "9.0" }, "none", "https://x.example/mcp"), ""
    );
});

QUnit.test("a credential key is rejected even on a public builtin", function (assert) {
    const msg = validators.validateOAuth(
        { min_score: "9.0", client_secret: "hunter2" }, "none", "builtin:sapnotes"
    );
    assert.ok(msg.indexOf("client_secret") > -1, "names the offending key");
});

QUnit.test("an out-of-range min_score is rejected", function (assert) {
    assert.notStrictEqual(
        validators.validateOAuth({ min_score: "42" }, "none", "builtin:sapnotes"), ""
    );
});

QUnit.test("an empty block on 'none' is still fine", function (assert) {
    assert.strictEqual(validators.validateOAuth(undefined, "none", "builtin:sapnotes"), "");
});

QUnit.test("accepts builtin:sapnotedetail", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:sapnotedetail", "session"), "");
});

QUnit.test("session mode carries no oauth config", function (assert) {
    assert.strictEqual(
        validators.validateOAuth(undefined, "session", "builtin:sapnotedetail"), ""
    );
});

QUnit.test("session mode rejects an oauth config block", function (assert) {
    assert.notStrictEqual(
        validators.validateOAuth({ client_id: "x" }, "session", "builtin:sapnotedetail"), ""
    );
});

QUnit.module("validators.validateOAuth — builtin:teams");

const TEAMS_OAUTH2 = {
    client_id: "c",
    client_secret: "s",
    authorize_url: "https://login.microsoftonline.com/t/oauth2/v2.0/authorize",
    token_url: "https://login.microsoftonline.com/t/oauth2/v2.0/token",
    team: "team-guid"
};

QUnit.test("teams is a known builtin", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:teams", "oauth2"), "");
});

QUnit.test("teams on oauth2 with a team passes, and may post", function (assert) {
    assert.strictEqual(validators.validateOAuth(TEAMS_OAUTH2, "oauth2", "builtin:teams"), "");
    assert.strictEqual(
        validators.validateOAuth({ ...TEAMS_OAUTH2, allow_send: true }, "oauth2", "builtin:teams"),
        ""
    );
});

QUnit.test("teams on app-only needs a team, not a mailbox", function (assert) {
    const { authorize_url: _unused, ...appOnly } = TEAMS_OAUTH2;
    assert.strictEqual(validators.validateOAuth(appOnly, "app_only", "builtin:teams"), "");
});

QUnit.test("teams without a team is rejected", function (assert) {
    const msg = validators.validateOAuth({ ...TEAMS_OAUTH2, team: "" }, "oauth2", "builtin:teams");
    assert.ok(msg.indexOf("team ID") > -1, msg);
});

QUnit.test("teams cannot post as the application", function (assert) {
    const { authorize_url: _unused, ...appOnly } = TEAMS_OAUTH2;
    const msg = validators.validateOAuth(
        { ...appOnly, allow_send: true }, "app_only", "builtin:teams"
    );
    assert.ok(msg.indexOf("cannot post") > -1, msg);
});

QUnit.test("teams rejects modes it cannot authenticate with", function (assert) {
    assert.notStrictEqual(validators.validateOAuth(undefined, "jwt", "builtin:teams"), "");
    assert.notStrictEqual(validators.validateOAuth({ dcr: true }, "oauth2", "builtin:teams"), "");
});

// --- step kinds ---
QUnit.module("validators.validateWorkflowSteps");

function wfStep(over: Partial<WorkflowStep>): WorkflowStep {
    return {
        branch_key: null, position: 1, agent_name: "reader", instructions: "",
        fan_out: false, step_timeout_seconds: 600, kind: "agent", config: {}, ...over
    };
}

QUnit.test("an agent is required only for kind agent", function (assert) {
    assert.deepEqual(validateWorkflowSteps([wfStep({})]), {}, "a named agent step is fine");
    assert.ok(validateWorkflowSteps([wfStep({ agent_name: "" })])[0], "an agent step without an agent is not");
    assert.ok(validateWorkflowSteps([wfStep({ agent_name: "  " })])[0], "blank counts as missing");
    assert.deepEqual(
        validateWorkflowSteps([wfStep({ kind: "transform", agent_name: "", config: {} })]),
        {}, "a transform needs no agent"
    );
    assert.deepEqual(
        validateWorkflowSteps([wfStep({ kind: "condition", agent_name: "", config: { rules: [] } })]),
        {}, "a condition needs no agent"
    );
});

QUnit.test("a step without a kind is an agent step", function (assert) {
    assert.deepEqual(validateWorkflowSteps([wfStep({ kind: undefined })]), {});
    assert.ok(validateWorkflowSteps([wfStep({ kind: undefined, agent_name: "" })])[0]);
});

QUnit.test("the fan-out step must be an agent", function (assert) {
    const msg = validateWorkflowSteps([
        wfStep({ kind: "python", agent_name: "", fan_out: true, config: { code: "output = 1", timeout_seconds: 5 } })
    ])[0];
    assert.ok(msg && msg.indexOf("fan-out") > -1, msg);
    assert.deepEqual(validateWorkflowSteps([wfStep({ fan_out: true })]), {}, "an agent fan-out is fine");
});

QUnit.test("an unknown kind is rejected", function (assert) {
    const msg = validateWorkflowSteps([wfStep({ kind: "shell" as WorkflowStep["kind"] })])[0];
    assert.ok(msg && msg.indexOf("shell") > -1, msg);
});

QUnit.test("timeouts mirror the server's ranges", function (assert) {
    assert.ok(validateWorkflowSteps([wfStep({ step_timeout_seconds: 5 })])[0], "step timeout below 10");
    assert.ok(validateWorkflowSteps([wfStep({ step_timeout_seconds: 1801 })])[0], "step timeout above 1800");
    const py = (t: number): WorkflowStep => wfStep({ kind: "python", agent_name: "", config: { code: "output = 1", timeout_seconds: t } });
    assert.ok(validateWorkflowSteps([py(0)])[0], "python timeout below 1");
    assert.ok(validateWorkflowSteps([py(61)])[0], "python timeout above 60");
    assert.deepEqual(validateWorkflowSteps([py(60)]), {});
    const http = (t: number): WorkflowStep => wfStep({ kind: "http", agent_name: "", config: { destination: "d", path: "/x", timeout_seconds: t } });
    assert.ok(validateWorkflowSteps([http(0)])[0], "http timeout below 1");
    assert.ok(validateWorkflowSteps([http(601)])[0], "http timeout above 600");
    assert.deepEqual(validateWorkflowSteps([http(30)]), {});
});

QUnit.test("a python step needs code", function (assert) {
    const msg = validateWorkflowSteps([wfStep({ kind: "python", agent_name: "", config: { code: "  ", timeout_seconds: 5 } })])[0];
    assert.ok(msg && msg.indexOf("code") > -1, msg);
});

QUnit.test("an http step needs a destination and a confined relative path", function (assert) {
    const http = (over: Record<string, unknown>): string =>
        validateWorkflowSteps([wfStep({ kind: "http", agent_name: "", config: { destination: "d", path: "/x", timeout_seconds: 30, ...over } })])[0];
    assert.ok(http({ destination: "" }).indexOf("destination") > -1, "no destination");
    assert.ok(http({ path: "https://evil.example.com/" }).indexOf("URL") > -1, "absolute URL");
    assert.ok(http({ path: "//evil.example.com/" }), "protocol-relative");
    assert.ok(http({ path: "relative" }).indexOf("'/'") > -1, "no leading slash");
    assert.ok(http({ path: "/a/../b" }).indexOf("..") > -1, "dot-dot segment");
    assert.ok(http({ path: "/a?x=1" }).indexOf("query") > -1, "query in the path");
    assert.strictEqual(http({ path: "/issue/{{item.id}}/comment" }), undefined, "a templated path is fine");
    assert.strictEqual(http({ path: "/x" }), undefined);
});

QUnit.test("transform and condition configs are checked", function (assert) {
    const tf = (config: Record<string, unknown>): string =>
        validateWorkflowSteps([wfStep({ kind: "transform", agent_name: "", config })])[0];
    assert.ok(tf({ truncate: 0 }), "truncate must be positive");
    assert.ok(tf({ regex: { pattern: "", flags: "" } }), "a regex needs a pattern");
    assert.ok(tf({ regex: { pattern: "a", flags: "q" } }), "unknown regex flag");
    assert.strictEqual(tf({ truncate: null, regex: null, template: "{{text}}" }), undefined);
    const cond = validateWorkflowSteps([wfStep({ kind: "condition", agent_name: "", config: { rules: [{ when: { op: "nope" } }] } })])[0];
    assert.ok(cond && cond.indexOf("Rule 1") > -1, cond);
});

QUnit.test("errors are keyed by the step's index", function (assert) {
    const errors = validateWorkflowSteps([
        wfStep({}),
        wfStep({ agent_name: "" }),
        wfStep({ kind: "http", agent_name: "", config: { destination: "", path: "/x", timeout_seconds: 30 } })
    ]);
    assert.deepEqual(Object.keys(errors), ["1", "2"]);
});

// --- deep agents -------------------------------------------------------------

QUnit.module("validators.validateDeep");

QUnit.test("the defaults and the full range are valid", function (assert) {
    assert.deepEqual(validateDeep(undefined), {}, "no config, nothing to check");
    assert.deepEqual(validateDeep({ ...DEEP_DEFAULTS }), {});
    assert.deepEqual(validateDeep({ ...DEEP_DEFAULTS, max_subagents: 1, subagent_max_depth: 1 }), {});
    assert.deepEqual(validateDeep({ ...DEEP_DEFAULTS, max_subagents: 20, subagent_max_depth: 3 }), {});
});

QUnit.test("max_subagents must be 1..20", function (assert) {
    assert.ok(validateDeep({ ...DEEP_DEFAULTS, max_subagents: 0 }).max_subagents, "0 is refused");
    assert.ok(validateDeep({ ...DEEP_DEFAULTS, max_subagents: 21 }).max_subagents, "21 is refused");
    assert.ok(validateDeep({ ...DEEP_DEFAULTS, max_subagents: 2.5 }).max_subagents, "fractions are refused");
    assert.notOk(validateDeep({ ...DEEP_DEFAULTS, max_subagents: 0 }).subagent_max_depth,
        "an error on one field does not spill onto the other");
});

QUnit.test("subagent_max_depth must be 1..3", function (assert) {
    assert.ok(validateDeep({ ...DEEP_DEFAULTS, subagent_max_depth: 0 }).subagent_max_depth, "0 is refused");
    assert.ok(validateDeep({ ...DEEP_DEFAULTS, subagent_max_depth: 4 }).subagent_max_depth, "4 is refused");
    const both = validateDeep({ ...DEEP_DEFAULTS, max_subagents: 99, subagent_max_depth: 9 });
    assert.deepEqual(Object.keys(both).sort(), ["max_subagents", "subagent_max_depth"],
        "both fields report at once so the form can flag both controls");
});

// --- odata ---
QUnit.module("validators: the builtin:odata entry");

QUnit.test("an odata entry needs a service and no destination", function (assert) {
    assert.strictEqual(validators.validateOAuth({ services: ["a"] }, "destination", "builtin:odata"), "");
    assert.strictEqual(validators.validateOAuth({ services: ["a"], allow_write: true }, "destination", "BUILTIN:OData/"), "");
    assert.strictEqual(validators.validateOAuth({ services: [] }, "destination", "builtin:odata"), "Select at least one OData service.");
    assert.strictEqual(validators.validateOAuth(undefined, "destination", "builtin:odata"), "Select at least one OData service.");
});

QUnit.test("an odata entry is refused with anything else in it", function (assert) {
    const only = "An OData services entry holds only the services and 'Allow writes': "
        + "the destination and the identity belong to each catalogue service.";
    assert.strictEqual(validators.validateOAuth({ services: ["a"], destination: "S4_ODATA_TECH" }, "destination", "builtin:odata"), only);
    assert.strictEqual(validators.validateOAuth({ services: ["a"], user_context: false }, "destination", "builtin:odata"), only);
    assert.strictEqual(validators.validateOAuth({ services: ["a"], has_client_secret: true }, "destination", "builtin:odata"), only);
    assert.strictEqual(
        validators.validateOAuth({ services: ["a"], has_client_secret: false }, "destination", "builtin:odata"), "",
        "the server's own echo on a stored entry is accepted, as the server accepts it"
    );
    assert.strictEqual(
        validators.validateOAuth({ services: ["a"], allow_write: "true" } as never, "destination", "builtin:odata"),
        "'Allow writes' must be on or off; any other value does not open writes."
    );
    assert.notStrictEqual(validators.validateOAuth({ services: ["a"] }, "oauth2", "builtin:odata"), "", "only destination mode");
    assert.notStrictEqual(validators.validateOAuth({ services: ["Not A Slug"] }, "destination", "builtin:odata"), "");
    assert.notStrictEqual(validators.validateOAuth({ services: ["a", "a"] }, "destination", "builtin:odata"), "");
});

QUnit.test("a second odata entry is refused with a text that points to the first", function (assert) {
    const entry = { url: "builtin:odata", auth_mode: "destination" as const, oauth: { services: ["a"] } };
    assert.deepEqual(
        validators.validateServers([entry, { url: "https://x.hana.ondemand.com/mcp", auth_mode: "jwt" }, { ...entry }]),
        { 2: "An agent has one OData services entry; add the services to the existing one." }
    );
});

// --- sharepoint ---
QUnit.module("validators — sharepoint");

const SP_ENTRY = {
    destination: "GRAPH",
    site: "example.sharepoint.com:/sites/planning",
    library: "Documents",
    path: "Team/Planning 2026.xlsx",
    views: { team: { kind: "table", table: "TeamMembers", columns: ["Name", "ID"] } }
};

QUnit.test("sharepoint is a known builtin", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:sharepoint", "destination"), "");
    assert.ok(validators.isSharePoint("Builtin:SharePoint/"));
    assert.notOk(validators.isSharePoint("builtin:teams"));
});

QUnit.test("a destination entry with the three pins and a view passes", function (assert) {
    assert.strictEqual(validators.validateSharePoint(SP_ENTRY, "destination"), "");
    assert.strictEqual(validators.validateOAuth(SP_ENTRY, "destination", "builtin:sharepoint"), "");
    assert.deepEqual(validators.validateServers(
        [{ url: "builtin:sharepoint", auth_mode: "destination", oauth: SP_ENTRY }]), {});
});

QUnit.test("sharepoint reads as the application: no user mode, no user context", function (assert) {
    (["oauth2", "jwt"] as const).forEach((mode) => {
        const msg = validators.validateSharePoint(SP_ENTRY, mode);
        assert.ok(msg.indexOf("'destination' or 'app_only'") > -1, `${mode}: ${msg}`);
        assert.strictEqual(validators.validateOAuth(SP_ENTRY, mode, "builtin:sharepoint"), msg, mode);
    });
    const msg = validators.validateSharePoint({ ...SP_ENTRY, user_context: true }, "destination");
    assert.ok(msg.indexOf("no signed-in user") > -1, msg);
    assert.strictEqual(
        validators.validateOAuth({ ...SP_ENTRY, user_context: true }, "destination", "builtin:sharepoint"), msg);
});

QUnit.test("the three pins have a shape and the views are required", function (assert) {
    const check = (patch: Record<string, unknown>, part: string): void => {
        const msg = validators.validateSharePoint({ ...SP_ENTRY, ...patch }, "destination");
        assert.ok(msg.indexOf(part) > -1, `${JSON.stringify(patch)}: ${msg}`);
    };
    check({ site: "https://example.sharepoint.com/sites/planning" }, "Site must be");
    check({ site: "" }, "Site must be");
    check({ library: "  " }, "document library");
    check({ path: "Team/Planning 2026.xlsm" }, ".xlsx");
    check({ path: "" }, ".xlsx");
    check({ views: undefined }, "at least one view");
    check({ views: {} }, "at least one view");
    check({ views: [] }, "at least one view");
    assert.strictEqual(
        validators.validateSharePoint({ ...SP_ENTRY, path: "Team/PLANNING.XLSX" }, "destination"), "",
        "the extension is not case-sensitive");
});

QUnit.test("sharepoint on app-only needs the pins and the views, not a mailbox", function (assert) {
    const { destination: _unused, ...pins } = SP_ENTRY;
    const appOnly = {
        ...pins, client_id: "c", client_secret: "s",
        token_url: "https://login.microsoftonline.com/t/oauth2/v2.0/token"
    };
    assert.strictEqual(validators.validateOAuth(appOnly, "app_only", "builtin:sharepoint"), "");
    assert.ok(validators.validateOAuth({ ...appOnly, client_id: "" }, "app_only", "builtin:sharepoint")
        .indexOf("client ID") > -1, "the app-only client rules still apply");
    assert.ok(validators.validateOAuth({ ...appOnly, views: {} }, "app_only", "builtin:sharepoint")
        .indexOf("at least one view") > -1);
});

// --- sharepoint: amendment 1 (pinned row kinds, exact pins, run keys) ---
const SP_CALENDAR = {
    kind: "calendar", sheet: "{year}", date_row: 8, first_row: 10, first_date_column: "D",
    labels: { member: "A", team: "B", kind: "C" },
    kinds: ["Presence", "Guard"],
    stop_at: "Summary",
    codes: { H: "unavailable", T: "available", GDI: "GDI" },
    lookup: { view: "team", on: "Name", add: ["ID"] },
    conflict: { kind: "Guard", against: "Presence", when: ["unavailable"] }
};

function spWithCalendar(patch: Record<string, unknown>): Record<string, unknown> {
    return { ...SP_ENTRY, views: { ...SP_ENTRY.views, planning: { ...SP_CALENDAR, ...patch } } };
}

QUnit.test("sharepoint: a calendar view with a kind label and its kinds passes", function (assert) {
    assert.strictEqual(validators.validateSharePoint(spWithCalendar({}), "destination"), "");
    assert.strictEqual(validators.validateOAuth(spWithCalendar({}), "destination", "builtin:sharepoint"), "");
});

QUnit.test("sharepoint: a calendar view with a kind label must pin its kinds", function (assert) {
    const refused = (kinds: unknown, why: string): void => {
        const msg = validators.validateSharePoint(spWithCalendar({ kinds }), "destination");
        assert.ok(msg.indexOf("kinds") > -1 && msg.indexOf("1 to 10") > -1, `${why}: ${msg}`);
    };
    refused(undefined, "missing");
    refused([], "empty");
    refused("Guard", "not a list");
    refused(["Guard", 1], "an item that is not a string");
    refused(["Guard", "  "], "a blank item");
    refused(["Guard", "x".repeat(61)], "an item over 60 characters");
    refused(["Guard", "Pre\nsence"], "an item of two lines");
    refused(Array.from({ length: 11 }, (_v, i) => `K${i}`), "eleven items");
    const twice = validators.validateSharePoint(
        spWithCalendar({ kinds: ["Presence", "Guard", " Guard "] }), "destination");
    assert.ok(twice.indexOf("kinds") > -1 && twice.indexOf("repeat") > -1, twice);
    assert.notOk(/Presence|Guard/.test(twice), "the message names the field and the rule, never a value");
});

QUnit.test("sharepoint: kinds is refused on a calendar view without a kind label", function (assert) {
    const noKind = { labels: { member: "A", team: "B" }, conflict: undefined };
    const msg = validators.validateSharePoint(spWithCalendar(noKind), "destination");
    assert.ok(msg.indexOf("kinds") > -1 && msg.indexOf("labels.kind") > -1, msg);
    const { kinds: _kinds, conflict: _conflict, ...plain } = SP_CALENDAR;
    assert.strictEqual(validators.validateSharePoint(
        { ...SP_ENTRY, views: { planning: { ...plain, labels: noKind.labels } } }, "destination"), "");
});

QUnit.test("sharepoint: conflict.kind and conflict.against must be members of kinds", function (assert) {
    const kind = validators.validateSharePoint(
        spWithCalendar({ conflict: { kind: "Standby", against: "Presence", when: ["unavailable"] } }), "destination");
    assert.ok(kind.indexOf("conflict.kind") > -1 && kind.indexOf("kinds") > -1, kind);
    assert.notOk(kind.indexOf("Standby") > -1, "no value in the message");
    const against = validators.validateSharePoint(
        spWithCalendar({ conflict: { kind: "Guard", against: "presence", when: ["unavailable"] } }), "destination");
    assert.ok(against.indexOf("conflict.against") > -1 && against.indexOf("kinds") > -1,
        `membership is case-sensitive: ${against}`);
});

QUnit.test("sharepoint: lookup.add may not name a run field", function (assert) {
    ["member", "team", "kind", "status", "from", "to", "Status", " TO "].forEach((column) => {
        const msg = validators.validateSharePoint(
            spWithCalendar({ lookup: { view: "team", on: "Name", add: ["ID", column] } }), "destination");
        assert.ok(msg.indexOf("lookup.add") > -1 && msg.indexOf("run field") > -1, `${column}: ${msg}`);
    });
});

QUnit.test("sharepoint: a pin must be typed in its exact form", function (assert) {
    const refused = (patch: Record<string, unknown>, part: string): void => {
        const msg = validators.validateSharePoint({ ...SP_ENTRY, ...patch }, "destination");
        assert.ok(msg.indexOf(part) > -1, `${JSON.stringify(patch)}: ${msg}`);
    };
    refused({ site: " example.sharepoint.com:/sites/planning" }, "Site must be");
    refused({ site: "example.sharepoint.com:/sites/planning\n" }, "Site must be");
    refused({ library: " Documents" }, "leading or trailing whitespace");
    refused({ library: "Documents " }, "leading or trailing whitespace");
    refused({ library: "Docu\u2028ments" }, "one line");
    refused({ library: "Docu\u0007ments" }, "control characters");
    refused({ library: "Cafe\u0301" }, "composed");
    refused({ path: " Team/Planning 2026.xlsx" }, "leading or trailing whitespace");
    refused({ path: "Team/Planning 2026.xlsx " }, "leading or trailing whitespace");
    refused({ path: "Team/Plan\tning.xlsx" }, "control characters");
    refused({ path: "Team/Cafe\u0301.xlsx" }, "composed");
    assert.strictEqual(
        validators.validateSharePoint({ ...SP_ENTRY, library: "Caf\u00e9", path: "Team/Caf\u00e9.xlsx" }, "destination"),
        "", "composed characters pass");
});

// --- bitbucket: an agent that approves is reachable by no chat user -------

QUnit.module("validators.approverProblem");

QUnit.test("an agent that approves pull requests is not in chat and nobody's peer", function (assert) {
    const entry = (approve: boolean) => [{
        url: "Builtin:Bitbucket/", auth_mode: "destination",
        oauth: Object.assign({ destination: "B", workspace: "acme-ws", allow_comment: true },
            approve ? { allow_approve: true } : {})
    }] as never;
    const me = (over: Record<string, unknown> = {}) => Object.assign(
        { name: "rev", expose_chat: false, mcp_servers: entry(true), peers: [] as string[] }, over);
    const other = (name: string, approve: boolean, peers: string[] = []) => (
        { name, expose_chat: false, mcp_servers: entry(approve), peers });

    assert.strictEqual(validators.approverProblem(me(), []), null);
    assert.deepEqual(validators.approverProblem(me({ expose_chat: true }), []), { rule: "chat", names: [] });
    assert.deepEqual(validators.approverProblem(me({ expose_chat: undefined }), []), { rule: "chat", names: [] },
        "only exactly false is not exposed");
    assert.strictEqual(validators.approverProblem(me({ expose_chat: true, mcp_servers: entry(false) }), []), null,
        "an agent that does not approve may be in chat");

    assert.deepEqual(
        validators.approverProblem(me(), [other("a", false, ["rev"]), other("b", false), other("c", false, ["x", "rev"])]),
        { rule: "peerOf", names: ["a", "c"] }, "other agents name it as their peer");
    assert.deepEqual(
        validators.approverProblem(me({ mcp_servers: entry(false), peers: ["a", "b", "gone"] }),
            [other("a", true), other("b", false)]),
        { rule: "peers", names: ["a"] }, "it names an agent that approves as its peer");
    assert.strictEqual(validators.approverProblem(me({ mcp_servers: entry(false), peers: ["b"] }), [other("b", false)]), null);
    assert.strictEqual(validators.approverProblem(me({ mcp_servers: entry(false), peers: ["a"] }), undefined), null,
        "without the other agents on the page the peer rule is the server's");
    assert.deepEqual(validators.approverProblem(me({ expose_chat: true }), undefined), { rule: "chat", names: [] });
});
