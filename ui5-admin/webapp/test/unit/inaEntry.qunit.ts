import ResourceBundle from "sap/base/i18n/ResourceBundle";
import inaEntry, { INA_URL } from "com/agent/admin/model/inaEntry";
import { findBuiltin } from "com/agent/admin/model/builtins";
import validators from "com/agent/admin/model/validators";
import oauthConfig from "com/agent/admin/model/oauthConfig";
import type { InaQuerySummary } from "com/agent/admin/service/inaTypes";
import type { McpServer } from "com/agent/admin/service/types";

QUnit.module("inaEntry");

const MARKUP = "<b>x</b> {y}";

function query(name: string, over: Partial<InaQuerySummary> = {}): InaQuerySummary {
    return {
        name, title: `Title of ${name}`, purpose: `Purpose of ${name}`, not_for: "", destination: "S4_INA_TECH",
        user_context: false, query: "ZQ001", package: "", enabled: true, described: false, metadata_fetched_at: null,
        updated_at: null, used_by: [], ...over
    };
}

QUnit.test("the url is builtin:ina, in any spelling the save gate reads", function (assert) {
    assert.strictEqual(INA_URL, "builtin:ina");
    assert.strictEqual(inaEntry.isInaUrl(" BUILTIN:InA/ "), true, "trimmed, any case, without trailing slashes");
    assert.strictEqual(inaEntry.isInaUrl("builtin:odata"), false);
    assert.strictEqual(inaEntry.isInaUrl("builtin:inactive"), false);
    assert.strictEqual(inaEntry.isInaUrl(undefined), false);
});

QUnit.test("clean is exactly the queries, as typed and never trimmed", function (assert) {
    assert.deepEqual(
        inaEntry.clean({ queries: ["b", " a ", "b", 7, "", null], destination: "X", user_context: true, has_client_secret: false }),
        { queries: ["b", " a "] }, "a name is sent as typed, for the server to refuse edge whitespace; every other key is dropped"
    );
    assert.deepEqual(Object.keys(inaEntry.clean({ queries: ["a"], allow_write: true })), ["queries"], "one key, never more");
    assert.deepEqual(inaEntry.clean(undefined), { queries: [] });
    assert.deepEqual(inaEntry.clean({ queries: "a" }), { queries: [] }, "a text is no list");
});

QUnit.test("entryIndex finds the agent's InA entry and can skip the row being edited", function (assert) {
    const servers: McpServer[] = [
        { url: "https://x.hana.ondemand.com/mcp", auth_mode: "jwt" },
        { url: "builtin:odata", auth_mode: "destination", oauth: { services: ["a"], allow_write: false } },
        { url: " Builtin:INA/ ", auth_mode: "destination", oauth: { queries: ["a"] } }
    ];
    assert.strictEqual(inaEntry.entryIndex(servers), 2);
    assert.strictEqual(inaEntry.entryIndex(servers, 2), -1, "the row being edited is not a second entry");
    assert.strictEqual(inaEntry.entryIndex(servers, 0), 2);
    assert.strictEqual(inaEntry.entryIndex([]), -1);
    assert.strictEqual(inaEntry.entryIndex(undefined), -1);
    assert.deepEqual(inaEntry.entryOf(servers), { queries: ["a"] });
    assert.strictEqual(inaEntry.entryOf(servers.slice(0, 2)), undefined);
});

QUnit.test("rowsOf keeps a query that is not registered, as missing", function (assert) {
    const queries = [query("a", { user_context: true }), query("b", { enabled: false }), query("c", { title: MARKUP, purpose: MARKUP })];
    assert.deepEqual(inaEntry.rowsOf(queries, { queries: ["gone", "a", "b", "c"] }), [
        { name: "gone", title: "gone", purpose: "", userContext: false, state: "missing" },
        { name: "a", title: "Title of a", purpose: "Purpose of a", userContext: true, state: "ok" },
        { name: "b", title: "Title of b", purpose: "Purpose of b", userContext: false, state: "disabled" },
        { name: "c", title: MARKUP, purpose: MARKUP, userContext: false, state: "ok" }
    ], "in the order selected, the admin's text as it is");
    assert.deepEqual(inaEntry.rowsOf(null, { queries: ["gone", "a"] }).map((row) => row.state), ["unknown", "unknown"],
        "an unread list calls nothing missing");
    assert.strictEqual(inaEntry.rowsOf([], { queries: ["toString"] })[0].state, "missing", "a name every object has is still a name");
    assert.strictEqual(inaEntry.rowsOf([query("a", { title: "  " })], { queries: ["a"] })[0].title, "a", "a blank title shows the name");
    assert.deepEqual(inaEntry.rowsOf(queries, undefined), [], "no entry, no rows");
});

QUnit.test("options offer every registered query and keep an item for every selected name", function (assert) {
    const queries = [query("a", { user_context: true }), query("b", { enabled: false })];
    assert.deepEqual(inaEntry.options(queries, { queries: ["gone", "a"] }).map((o) => [o.key, o.title, o.userContext, o.state]), [
        ["a", "Title of a", true, "ok"], ["b", "Title of b", false, "disabled"], ["gone", "gone", false, "missing"]
    ]);
    assert.deepEqual(inaEntry.options(null, { queries: ["gone", "a"] }).map((o) => [o.key, o.state]),
        [["gone", "unknown"], ["a", "unknown"]], "without the list the selection is still offered, so it is not lost");
});

QUnit.test("missing names the selected queries the registered list lacks, only when it was read", function (assert) {
    const queries = [query("a"), query("b", { enabled: false })];
    assert.deepEqual(inaEntry.missing({ queries: ["x", "a", "b", "y"] }, queries), ["x", "y"], "a disabled query is not missing");
    assert.deepEqual(inaEntry.missing({ queries: ["x"] }, null), [], "an unread list calls nothing missing");
    assert.deepEqual(inaEntry.missing(undefined, queries), []);
});

QUnit.test("validate: destination mode, one key, 1 to 50 distinct slugs, fixed texts that never quote a value", function (assert) {
    const ok = (queries: unknown, extra: Record<string, unknown> = {}) =>
        inaEntry.validate({ queries, ...extra } as McpServer["oauth"], "destination");
    assert.strictEqual(ok(["flight-revenue", "a1"]), "");
    assert.strictEqual(ok(["a"], { has_client_secret: false }), "", "the server's own echo is taken back");
    assert.strictEqual(inaEntry.validate({ queries: ["a"] } as McpServer["oauth"], "jwt"),
        "Analytical queries requires auth mode 'destination': every registered query is reached through the BTP destination it names.");
    const ONE_KEY = "An analytical queries entry holds only the queries: the destination and who the calls run as belong to each registered query.";
    assert.strictEqual(ok(["a"], { destination: "S4" }), ONE_KEY);
    assert.strictEqual(ok(["a"], { user_context: false }), ONE_KEY, "user_context is refused, also with its default");
    assert.strictEqual(ok(["a"], { has_client_secret: true }), ONE_KEY);
    assert.strictEqual(ok([]), "Select at least one analytical query.");
    assert.strictEqual(ok(undefined), "Select at least one analytical query.");
    assert.strictEqual(inaEntry.validate(undefined, "destination"), "Select at least one analytical query.");
    assert.strictEqual(ok(Array.from({ length: 51 }, (_, i) => `q${i}`)), "At most 50 analytical queries per agent.");
    assert.strictEqual(ok(Array.from({ length: 50 }, (_, i) => `q${i}`)), "", "50 is allowed");
    const INVALID = "An analytical query name is not valid (lower-case letters, digits and '-', at most 64 characters).";
    [" a", "a ", "A", "-a", "a-", "a_b", "a".repeat(65), 7, null].forEach((name) => {
        assert.strictEqual(ok([name]), INVALID, JSON.stringify(name));
    });
    assert.strictEqual(ok(["a", "a"]), "An analytical query is listed twice.");
    [ok([" secret "]), ok(["a"], { destination: "secret" }), ok(["secret", "secret"])].forEach((text) => {
        assert.strictEqual(text.indexOf("secret"), -1, "no text repeats a value");
    });
});

QUnit.test("explicit: every InA entry is sent as exactly the queries, the other servers as they are", function (assert) {
    const other: McpServer = { url: "https://x.hana.ondemand.com/mcp", auth_mode: "oauth2", oauth: { client_id: "c", has_client_secret: true } } as McpServer;
    const echoed = { url: "builtin:ina", auth_mode: "destination", oauth: { queries: ["a", "b"], has_client_secret: false } } as McpServer;
    const servers = [other, echoed];
    const before = JSON.stringify(servers);
    const sent = inaEntry.explicit(servers);
    assert.deepEqual(sent[1], { url: "builtin:ina", auth_mode: "destination", oauth: { queries: ["a", "b"] } }, "without the echo");
    assert.strictEqual(sent[0], other, "another server is passed on untouched");
    assert.strictEqual(JSON.stringify(servers), before, "what was given is not changed");
    const bare = { url: "builtin:ina", auth_mode: "destination" } as McpServer;
    assert.deepEqual(inaEntry.explicit([bare])[0].oauth, { queries: [] }, "an entry without a block still names its one key");
    assert.deepEqual(inaEntry.explicit(undefined), []);
});

// The two shared gates of the dialog route the entry to these rules.
QUnit.test("validators and oauthConfig use the InA rules for builtin:ina, and none of the destination ones", function (assert) {
    assert.strictEqual(validators.validateServerUrl("builtin:ina", "destination"), "", "a known built-in");
    assert.strictEqual(validators.validateOAuth({ queries: ["a"] } as McpServer["oauth"], "destination", "builtin:ina"), "",
        "no destination name is asked for");
    assert.strictEqual(validators.validateOAuth({ queries: [] } as McpServer["oauth"], "destination", "builtin:ina"),
        "Select at least one analytical query.");
    assert.deepEqual(oauthConfig.cleanOAuth({ queries: [" a "], destination: "S4", user_context: true, lookback: "7d" }, "destination", INA_URL),
        { queries: [" a "] }, "the dialog's flat block becomes exactly the queries");
    assert.strictEqual(oauthConfig.supportsUserContext(INA_URL), false, "no 'Act as signed-in user' switch");
});

QUnit.test("two InA entries are refused, with a fixed text that points to the first", function (assert) {
    const entry = (queries: string[]): McpServer => ({ url: INA_URL, auth_mode: "destination", oauth: { queries } } as McpServer);
    const errors = validators.validateServers([entry(["a"]), entry(["b"])]);
    assert.deepEqual(errors, { 1: "An agent has one analytical queries entry; add the queries to the existing one." });
    assert.deepEqual(validators.validateServers([entry(["a"]), { ...entry(["b"]), url: " BUILTIN:ina/ " }]),
        { 1: "An agent has one analytical queries entry; add the queries to the existing one." }, "in any spelling");
    assert.deepEqual(validators.validateServers([entry(["a"])]), {}, "one is fine");
});

QUnit.test("every text key of the toolset and its dialog exists", async function (assert) {
    const bundle = await ResourceBundle.create({
        url: sap.ui.require.toUrl("com/agent/admin/i18n/i18n.properties"), async: true
    }) as ResourceBundle;
    const ina = findBuiltin(INA_URL);
    // The keys controller/agent/InaEntryPart.ts asks for.
    [ina?.titleKey ?? "", ina?.descriptionKey ?? "", "inaEntryQueries", "inaEntryQueriesPlaceholder", "inaEntryRunsAs",
        "inaEntryUser", "inaEntryTechnical", "inaEntryOptionDisabled", "inaEntryRowMissing", "inaEntryLoadFailed",
        "inaEntryNone", "inaEntryNoQueries", "inaEntryMissingOne", "inaEntryMissingMany", "inaEntryMissingTitle",
        "inaEntryMissingConfirm", "inaEntryMissingRemove", "inaEntryDisabledOne", "inaEntryDisabledMany",
        "inaEntryScheduledWarning", "inaEntryScheduledWarningMany", "inaEntryDuplicate"].forEach((key) => {
        assert.ok(bundle.hasText(key), key);
    });
    assert.strictEqual(bundle.getText("builtinIna"), "Analytical queries (InA)", "the toolset's name");
});
