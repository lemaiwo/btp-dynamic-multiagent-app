import odataEntry from "com/agent/admin/model/odataEntry";
import type { McpServer, ODataDefinition, ODataEntitySet, ODataServiceSummary } from "com/agent/admin/service/types";

QUnit.module("odataEntry");

const MARKUP = "<b>x</b> {y}";

function summary(name: string, over: Partial<ODataServiceSummary> = {}): ODataServiceSummary {
    return {
        name, title: `Title of ${name}`, purpose: `Purpose of ${name}`, not_for: "", destination: "S4_ODATA_TECH",
        user_context: false, odata_version: "v2", service_path: "/sap/opu/odata/sap/X", enabled: true,
        metadata_fetched_at: null, id: 1, created_at: null, updated_at: null,
        counts: { entity_sets: 0, operations: 0 }, has_write: false, used_by: [], ...over
    } as ODataServiceSummary;
}

function entitySet(name: string, title: string, operations: ODataEntitySet["operations"]): ODataEntitySet {
    return {
        name, title, path: "", entity_type: "", description: "", keys: [{ name: "Id", type: "Edm.String" }], operations,
        fields: [{
            name: "Id", type: "Edm.String", label: "", selectable: true, filterable: false, writable: true,
            hint: "", values: [], personal_data: false
        }],
        navigations: [], examples: []
    };
}

const WRITING: ODataDefinition = {
    entity_sets: [entitySet("A_Item", "Requisition item", ["list", "get", "update"]), entitySet("A_Head", "Requisition", ["list"])],
    operations: [
        {
            name: "ReleaseItem", qualified_name: "", title: "Release item", kind: "function_import", http_method: "POST",
            bound_to: null, parameters: [], description: "", enabled: true, changes_data: true
        },
        {
            name: "GetStrategy", qualified_name: "", title: "Strategy", kind: "function_import", http_method: "GET",
            bound_to: null, parameters: [], description: "", enabled: true, changes_data: false
        },
        {
            name: "Reject", qualified_name: "", title: "Reject", kind: "function_import", http_method: "POST",
            bound_to: null, parameters: [], description: "", enabled: false, changes_data: true
        }
    ]
};
const READING: ODataDefinition = { entity_sets: [entitySet("A_Head", "Requisition", ["list", "get"])], operations: [] };

QUnit.test("clean keeps the services and makes allow_write a real boolean", function (assert) {
    assert.deepEqual(odataEntry.clean({ services: ["b", " a ", "b", 7, "", null], allow_write: true, destination: "X" }),
        { services: ["b", " a "], allow_write: true }, "a name is sent as typed, for the server to refuse edge whitespace");
    for (const value of ["true", 1, "1", {}, [], null, undefined, false]) {
        assert.strictEqual(odataEntry.clean({ services: ["a"], allow_write: value }).allow_write, false, JSON.stringify(value));
    }
    assert.deepEqual(odataEntry.clean(undefined), { services: [], allow_write: false });
    assert.deepEqual(odataEntry.clean({ services: "a" }), { services: [], allow_write: false }, "a text is no list");
});

QUnit.test("entryIndex finds the agent's odata entry and can skip the row being edited", function (assert) {
    const servers: McpServer[] = [
        { url: "https://x.hana.ondemand.com/mcp", auth_mode: "jwt" },
        { url: " BUILTIN:OData/ ", auth_mode: "destination", oauth: { services: ["a"], allow_write: true } }
    ];
    assert.strictEqual(odataEntry.entryIndex(servers), 1);
    assert.strictEqual(odataEntry.entryIndex(servers, 1), -1, "the row being edited is not a second entry");
    assert.strictEqual(odataEntry.entryIndex(servers, 0), 1);
    assert.strictEqual(odataEntry.entryIndex([]), -1);
    assert.deepEqual(odataEntry.entryOf(servers), { services: ["a"], allow_write: true });
    assert.strictEqual(odataEntry.entryOf([servers[0]]), undefined);
});

QUnit.test("rows keep a service the catalogue lacks, as missing", function (assert) {
    const catalogue = [summary("a", { user_context: true }), summary("b", { enabled: false }), summary("c", { title: MARKUP, purpose: MARKUP })];
    assert.deepEqual(odataEntry.rows(["gone", "a", "b", "c"], catalogue), [
        { name: "gone", title: "gone", purpose: "", userContext: false, state: "missing" },
        { name: "a", title: "Title of a", purpose: "Purpose of a", userContext: true, state: "ok" },
        { name: "b", title: "Title of b", purpose: "Purpose of b", userContext: false, state: "disabled" },
        { name: "c", title: MARKUP, purpose: MARKUP, userContext: false, state: "ok" }
    ]);
    assert.deepEqual(odataEntry.rows(["gone", "a"], null).map((row) => row.state), ["unknown", "unknown"],
        "an unread catalogue calls nothing missing");
    assert.deepEqual(odataEntry.rows(["toString"], [])[0].state, "missing", "a name that every object has is still a name");
});

QUnit.test("options offer the catalogue and keep an item for every selected name", function (assert) {
    const catalogue = [summary("a"), summary("b", { enabled: false })];
    assert.deepEqual(odataEntry.options(["gone", "a"], catalogue).map((o) => [o.key, o.state]),
        [["a", "ok"], ["b", "disabled"], ["gone", "missing"]]);
    assert.deepEqual(odataEntry.options(["gone", "a"], null).map((o) => [o.key, o.state]),
        [["gone", "unknown"], ["a", "unknown"]], "without a catalogue the selection is still offered, so it is not lost");
});

QUnit.test("opens lists the enabled writes by service, in the words of the service page", function (assert) {
    const selected = odataEntry.rows(["w", "r", "unread", "gone"], [summary("w"), summary("r"), summary("unread")]);
    assert.deepEqual(odataEntry.opens(selected, { "=w": WRITING, "=r": READING }), [
        { name: "w", title: "Title of w", items: ["Requisition item (update)", "Release item"] },
        { name: "r", title: "Title of r", items: [] },
        { name: "unread", title: "Title of unread", items: null }
    ], "a GET that only reads and an operation that is not enabled are no writes; an unread service is not 'nothing'");
});

QUnit.test("newlyGiven: off to on names every service", function (assert) {
    const on = { services: ["a", "b"], allow_write: true };
    assert.deepEqual(odataEntry.newlyGiven({ services: ["a", "b"], allow_write: false }, on), { switchedOn: true, services: ["a", "b"] });
    assert.deepEqual(odataEntry.newlyGiven(undefined, on), { switchedOn: true, services: ["a", "b"] }, "a new entry");
});

QUnit.test("newlyGiven: a service added to an entry that already writes names the added one", function (assert) {
    assert.deepEqual(
        odataEntry.newlyGiven({ services: ["a"], allow_write: true }, { services: ["b", "a", "c"], allow_write: true }),
        { switchedOn: false, services: ["b", "c"] }
    );
});

QUnit.test("newlyGiven: unticking, removing and no change give nothing", function (assert) {
    const none = { switchedOn: false, services: [] };
    const stored = { services: ["a", "b"], allow_write: true };
    assert.deepEqual(odataEntry.newlyGiven(stored, { services: ["a", "b"], allow_write: false }), none, "unticked");
    assert.deepEqual(odataEntry.newlyGiven(stored, { services: ["b"], allow_write: true }), none, "a service removed");
    assert.deepEqual(odataEntry.newlyGiven(stored, undefined), none, "the entry removed");
    assert.deepEqual(odataEntry.newlyGiven(stored, { services: ["b", "a"], allow_write: true }), none, "the same, reordered");
    assert.deepEqual(odataEntry.newlyGiven({ services: ["a"], allow_write: false }, { services: ["a", "b"], allow_write: false }), none,
        "a service added without Allow writes");
});

QUnit.test("explicit: every odata entry is sent as exactly the services and a boolean allow_write, the other servers as they are", function (assert) {
    const other: McpServer = { url: "https://x.hana.ondemand.com/mcp", auth_mode: "oauth2", oauth: { client_id: "c", has_client_secret: true } } as McpServer;
    const echoedOff = { url: "builtin:odata", auth_mode: "destination", oauth: { services: ["a"], has_client_secret: false } } as unknown as McpServer;
    const servers = [other, echoedOff];
    const before = JSON.stringify(servers);

    const sent = odataEntry.explicit(servers);
    assert.deepEqual(sent[1], { url: "builtin:odata", auth_mode: "destination", oauth: { services: ["a"], allow_write: false } },
        "stored off, echoed without the key: sent as the boolean false, without the echo");
    assert.strictEqual(sent[0], other, "another server is passed on untouched");
    assert.strictEqual(JSON.stringify(servers), before, "what was given is not changed");

    const on = { url: " BUILTIN:OData/ ", auth_mode: "destination", oauth: { services: ["a", "b"], allow_write: true, has_client_secret: false } } as unknown as McpServer;
    assert.deepEqual(odataEntry.explicit([on])[0].oauth, { services: ["a", "b"], allow_write: true }, "stored on stays on");
    const text = { url: "builtin:odata", auth_mode: "destination", oauth: { services: ["a"], allow_write: "true" } } as unknown as McpServer;
    assert.deepEqual(odataEntry.explicit([text])[0].oauth, { services: ["a"], allow_write: false }, "a text never opens writes");
    const bare = { url: "builtin:odata", auth_mode: "destination" } as McpServer;
    assert.deepEqual(odataEntry.explicit([bare])[0].oauth, { services: [], allow_write: false }, "an entry without a block still names both");
    assert.deepEqual(odataEntry.explicit(undefined), [], "no servers");
});
