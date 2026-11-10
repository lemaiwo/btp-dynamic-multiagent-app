import hanaEntry, { HANA_TEXTS } from "com/agent/admin/model/hanaEntry";
import oauthConfig from "com/agent/admin/model/oauthConfig";
import validators from "com/agent/admin/model/validators";
import { authModesFor, findBuiltin } from "com/agent/admin/model/builtins";
import type { McpServer } from "com/agent/admin/service/types";

const URL = "builtin:hana";
const TENANT = { name: "erp_tenant", destination: "hana-erp-tenant" };
const SYSTEMDB = { name: "erp_systemdb", destination: "hana-erp-systemdb" };

/** A dialog model holding fields of every other toolset as well. */
function form(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return Object.assign({
        dcr: false, client_id: "cid", client_secret: "sec", mailbox: "x@example.com",
        allow_send: true, lookback: "2d", project: "ABC", status: "Open", api_base: "/api/2",
        user_context: false, has_client_secret: false, destination: "OTHER", workspace: "ws",
        provider: "github", repositories: ["acme/web"], queries: ["q"], services: ["s"], allow_write: true,
        systems: [TENANT, SYSTEMDB], allow_select: true
    }, overrides);
}

function entry(oauth: Record<string, unknown>, url = URL): McpServer {
    return { url, auth_mode: "destination", oauth: oauth as McpServer["oauth"] };
}

QUnit.module("hanaEntry");

QUnit.test("hana is a destination-only builtin", function (assert) {
    assert.strictEqual(findBuiltin("Builtin:HANA/")?.url, URL);
    assert.strictEqual(findBuiltin(URL)?.titleKey, "builtinHana");
    assert.strictEqual(findBuiltin(URL)?.descriptionKey, "builtinHanaDesc");
    assert.deepEqual(authModesFor(URL), ["destination"]);
    assert.strictEqual(validators.validateServerUrl(URL, "destination"), "");
    assert.ok(hanaEntry.isHanaUrl("Builtin:HANA/"));
    assert.ok(hanaEntry.isHanaUrl(" builtin:hana// "));
    assert.notOk(hanaEntry.isHanaUrl("builtin:hana_x"));
    assert.notOk(hanaEntry.isHanaUrl(undefined));
    assert.notOk(oauthConfig.supportsUserContext(URL), "no signed-in-user switch");
});

QUnit.test("clean builds { systems, allow_select } from nothing", function (assert) {
    assert.deepEqual(hanaEntry.clean(form()), { systems: [TENANT, SYSTEMDB], allow_select: true },
        "no destination, user_context, has_client_secret or key of another toolset");
    assert.deepEqual(Object.keys(hanaEntry.clean(form())), ["systems", "allow_select"]);
    assert.deepEqual(hanaEntry.clean(form({ allow_select: false })), { systems: [TENANT, SYSTEMDB] },
        "allow_select only as true");
    assert.deepEqual(hanaEntry.clean(form({ allow_select: "true" })), { systems: [TENANT, SYSTEMDB] },
        "a string does not switch it on");
    assert.deepEqual(hanaEntry.clean({ systems: [{ ...TENANT, extra: 1, user_context: true }] }),
        { systems: [TENANT] }, "a system keeps only name and destination");
    assert.deepEqual(hanaEntry.clean(undefined), { systems: [] });
    assert.deepEqual(hanaEntry.clean({ systems: "x" }), { systems: [] });
    const raw = { systems: [TENANT] };
    const out = hanaEntry.clean(raw);
    assert.notStrictEqual(out.systems[0], raw.systems[0], "a new object, not the dialog's row");
});

QUnit.test("values are sent as typed, never trimmed, and refused before the save", function (assert) {
    const out = hanaEntry.clean({ systems: [{ name: " erp", destination: "hana-erp " }] });
    assert.deepEqual(out.systems, [{ name: " erp", destination: "hana-erp " }]);
    assert.ok(hanaEntry.validate(out, "destination"));
});

QUnit.test("cleanOAuth and validateOAuth use the entry's own rules", function (assert) {
    const out = oauthConfig.cleanOAuth(form(), "destination", URL) as Record<string, unknown>;
    assert.deepEqual(out, { systems: [TENANT, SYSTEMDB], allow_select: true });
    assert.strictEqual(validators.validateOAuth(out as McpServer["oauth"], "destination", URL), "");
    assert.strictEqual(validators.validateOAuth({} as McpServer["oauth"], "destination", "Builtin:Hana/"), HANA_TEXTS.none);
});

QUnit.test("validate: each rule of agents/hana/config.py with a fixed text that never quotes a value", function (assert) {
    const secret = "secret_value";
    const ok = { systems: [TENANT, SYSTEMDB] };
    const eleven = Array.from({ length: 11 }, (_v, i) => ({ name: `s${i}`, destination: `d${i}` }));
    const cases: [string, unknown, string][] = [
        ["user_context true", { ...ok, user_context: true }, HANA_TEXTS.userContext],
        ["a top-level destination", { ...ok, destination: secret }, HANA_TEXTS.destination],
        ["a stray key", { ...ok, [secret]: 1 }, HANA_TEXTS.oneKey],
        ["no systems", {}, HANA_TEXTS.none],
        ["systems not a list", { systems: secret }, HANA_TEXTS.none],
        ["an empty list", { systems: [] }, HANA_TEXTS.none],
        ["eleven systems", { systems: eleven }, HANA_TEXTS.tooMany],
        ["a system that is no object", { systems: [secret] }, "System 1:"],
        ["a stray key in a system", { systems: [{ ...TENANT, [secret]: 1 }] }, "System 1:"],
        ["no name", { systems: [{ destination: "d" }] }, "System 1: the name"],
        ["an upper-case name", { systems: [TENANT, { name: "Secret_value", destination: "d" }] }, "System 2: the name"],
        ["a name starting with a digit", { systems: [{ name: "1abc", destination: "d" }] }, "System 1: the name"],
        ["a name of 33 characters", { systems: [{ name: `a${"b".repeat(32)}`, destination: "d" }] }, "System 1: the name"],
        ["a name with a line feed", { systems: [{ name: "abc\n", destination: "d" }] }, "System 1: the name"],
        ["a name twice", { systems: [TENANT, { name: TENANT.name, destination: "other" }] }, "System 2: the name"],
        ["no destination", { systems: [{ name: "abc" }] }, "System 1: the destination"],
        ["a destination with a space", { systems: [{ name: "abc", destination: `${secret} x` }] }, "System 1: the destination"],
        ["a destination of 201 characters", { systems: [{ name: "abc", destination: "d".repeat(201) }] }, "System 1: the destination"],
        ["allow_select a string", { ...ok, allow_select: "true" }, HANA_TEXTS.allowSelect]
    ];
    cases.forEach(([what, cfg, expected]) => {
        const text = hanaEntry.validate(cfg as McpServer["oauth"], "destination");
        assert.ok(text.indexOf(expected) === 0, `${what}: ${text}`);
        assert.strictEqual(text.indexOf(secret), -1, `${what}: the value is not quoted`);
        assert.strictEqual(text.indexOf("Secret"), -1, `${what}: the value is not quoted`);
    });
    assert.strictEqual(hanaEntry.validate(ok as McpServer["oauth"], "jwt"), HANA_TEXTS.mode);
});

QUnit.test("validate accepts what the server accepts", function (assert) {
    const ten = Array.from({ length: 10 }, (_v, i) => ({ name: `s${i}`, destination: `d${i}` }));
    const accepted: unknown[] = [
        { systems: [TENANT] },
        { systems: ten, allow_select: true },
        { systems: [TENANT], allow_select: false },
        { systems: [TENANT], allow_select: null, user_context: false, has_client_secret: false },
        { systems: [{ name: `a${"b".repeat(31)}`, destination: "A.b_c-1" }] },
        { systems: [{ name: "abc", destination: "d", extra: null }] }
    ];
    accepted.forEach((cfg, i) => {
        assert.strictEqual(hanaEntry.validate(cfg as McpServer["oauth"], "destination"), "", `case ${i}`);
    });
});

QUnit.test("problem names the row and the field it refuses", function (assert) {
    assert.deepEqual(hanaEntry.problem({ systems: [TENANT, { name: "X", destination: "d" }] } as McpServer["oauth"], "destination"),
        { text: hanaEntry.validate({ systems: [TENANT, { name: "X", destination: "d" }] } as McpServer["oauth"], "destination"),
            system: 1, field: "name" });
    assert.strictEqual(hanaEntry.problem({ systems: [{ name: "abc" }] } as McpServer["oauth"], "destination")?.field,
        "destination");
    assert.strictEqual(hanaEntry.problem({ systems: [] } as McpServer["oauth"], "destination")?.system, undefined);
    assert.strictEqual(hanaEntry.problem({ systems: [TENANT] } as McpServer["oauth"], "destination"), null);
});

QUnit.test("selectNewlyAllowed is true only when allow_select goes from off to on", function (assert) {
    assert.ok(hanaEntry.selectNewlyAllowed(undefined, { allow_select: true }));
    assert.ok(hanaEntry.selectNewlyAllowed({ allow_select: false }, { allow_select: true }));
    assert.ok(hanaEntry.selectNewlyAllowed({ allow_select: "true" }, { allow_select: true }), "a string was never on");
    assert.notOk(hanaEntry.selectNewlyAllowed({ allow_select: true }, { allow_select: true }));
    assert.notOk(hanaEntry.selectNewlyAllowed({ allow_select: true }, {}));
    assert.notOk(hanaEntry.selectNewlyAllowed({}, { allow_select: "true" }));
    assert.notOk(hanaEntry.selectNewlyAllowed(undefined, undefined));
});

QUnit.test("entryIndex and explicit: one entry per agent, sent as exactly its block", function (assert) {
    const servers: McpServer[] = [
        { url: "https://a.hana.ondemand.com/mcp", auth_mode: "jwt" },
        entry({ systems: [TENANT], has_client_secret: false, user_context: false }, "Builtin:HANA/")
    ];
    assert.strictEqual(hanaEntry.entryIndex(servers), 1);
    assert.strictEqual(hanaEntry.entryIndex(servers, 1), -1, "the row being edited is skipped");
    assert.strictEqual(hanaEntry.entryIndex(undefined), -1);
    const sent = hanaEntry.explicit(servers);
    assert.deepEqual(sent[1].oauth, { systems: [TENANT] }, "the server's echo is dropped");
    assert.strictEqual(sent[0], servers[0], "another server is passed on as it is");
    assert.deepEqual((servers[1].oauth as Record<string, unknown>).has_client_secret, false, "nothing given is changed");
    assert.deepEqual(validators.validateServers([entry({ systems: [TENANT] }), entry({ systems: [SYSTEMDB] })])[1],
        HANA_TEXTS.secondEntry, "a second entry is refused with a fixed text");
});

QUnit.test("refusedAt reads where a server 422 points in a hana entry", function (assert) {
    const message = "Value error, oauth.systems[1].name: required, 1-32 lower-case letters";
    assert.deepEqual(hanaEntry.refusedAt("mcp_servers.2", message), { server: 2, system: 1, field: "name" });
    assert.deepEqual(hanaEntry.refusedAt("mcp_servers.0.oauth.systems.3.destination", "Value error"),
        { server: 0, system: 3, field: "destination" }, "a nested loc");
    assert.deepEqual(hanaEntry.refusedAt("mcp_servers.1", "Value error, oauth.systems[0]: each system is an object"),
        { server: 1, system: 0, field: "" }, "the row without a field");
    assert.strictEqual(hanaEntry.refusedAt("mcp_servers.1", "Value error, oauth.systems: required"), null);
    assert.strictEqual(hanaEntry.refusedAt("name", message), null);
});

QUnit.test("unconfirmed: the systems Allow SELECT would cover that were not confirmed, by name AND destination", function (assert) {
    const confirmed = [TENANT];
    assert.deepEqual(hanaEntry.unconfirmed(confirmed, [TENANT]), []);
    assert.deepEqual(hanaEntry.unconfirmed(confirmed, [TENANT, SYSTEMDB]), [SYSTEMDB], "a system added after the question");
    assert.deepEqual(hanaEntry.unconfirmed(confirmed, [{ name: TENANT.name, destination: "hana-prod" }]),
        [{ name: TENANT.name, destination: "hana-prod" }], "the same name on another destination");
    assert.deepEqual(hanaEntry.unconfirmed(confirmed, [{ name: "other", destination: TENANT.destination }]),
        [{ name: "other", destination: TENANT.destination }], "the same destination under another name");
    assert.deepEqual(hanaEntry.unconfirmed([], []), []);
    assert.deepEqual(hanaEntry.unconfirmed(undefined, [TENANT]), [TENANT]);
});

QUnit.test("shownName caps a name at 64 characters with an ellipsis", function (assert) {
    assert.strictEqual(hanaEntry.shownName("erp_tenant"), "erp_tenant");
    assert.strictEqual(hanaEntry.shownName("a".repeat(64)), "a".repeat(64));
    assert.strictEqual(hanaEntry.shownName("a".repeat(70)), `${"a".repeat(63)}…`);
    assert.strictEqual(hanaEntry.shownName("a".repeat(70)).length, 64);
});
