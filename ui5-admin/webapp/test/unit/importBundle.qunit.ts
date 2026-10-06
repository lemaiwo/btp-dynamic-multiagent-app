import importBundle from "com/agent/admin/model/importBundle";
import type { ImportResult } from "com/agent/admin/service/types";

QUnit.module("importBundle");

function service(over: Record<string, unknown> = {}): Record<string, unknown> {
    return {
        name: "sales-orders", title: "Sales orders", destination: "S4_ODATA_TECH", user_context: false,
        odata_version: "v2", service_path: "/sap/opu/odata/sap/X", enabled: true,
        definition: {
            entity_sets: [
                { name: "A_Order", title: "Order", operations: ["list", "get", "update"] },
                { name: "A_Item", title: "", operations: ["list"] }
            ],
            operations: [
                { name: "Release", title: "Release order", enabled: true, http_method: "POST", changes_data: true },
                { name: "Peek", title: "Peek", enabled: true, http_method: "GET", changes_data: false },
                { name: "Unmarked", title: "", enabled: true, http_method: "GET" },
                { name: "Off", title: "Off", enabled: false, http_method: "POST" }
            ]
        },
        ...over
    };
}

function agent(name: string, servers: unknown): Record<string, unknown> {
    return { name, mcp_servers: servers };
}

QUnit.test("opens: each service with identity, destination and its writes by the shared write rule", function (assert) {
    const found = importBundle.opens({
        agents: [], skills: [],
        odata_services: [service(), service({ name: "read-only", title: "", user_context: true, definition: { entity_sets: [], operations: [] } })]
    });
    assert.deepEqual(found, {
        hasCatalogue: true,
        services: [
            {
                name: "sales-orders", title: "Sales orders", userContext: false, destination: "S4_ODATA_TECH",
                // A GET without `changes_data: false` is a write (`operationIsWrite`); a disabled one is none.
                writes: ["Order (update)", "Release order", "Unmarked"]
            },
            { name: "read-only", title: "read-only", userContext: true, destination: "S4_ODATA_TECH", writes: [] }
        ],
        writers: []
    });
});

QUnit.test("opens: the agents whose OData entry has Allow writes as the JSON boolean true", function (assert) {
    const entry = (allow: unknown, url = "builtin:odata") => ({ url, auth_mode: "destination", oauth: { services: ["a", "b"], allow_write: allow } });
    const found = importBundle.opens({
        agents: [
            agent("writer", [{ url: "https://x.example/mcp", auth_mode: "jwt" }, entry(true)]),
            agent("other spelling", [entry(true, " Builtin:OData/ ")]),
            agent("reader", [entry(false)]),
            agent("text", [entry("true")]),
            agent("no servers", undefined)
        ]
    });
    assert.deepEqual(found, {
        hasCatalogue: false, services: [],
        writers: [{ agent: "writer", services: ["a", "b"] }, { agent: "other spelling", services: ["a", "b"] }]
    });
});

QUnit.test("mustAsk: for catalogue services, for an agent with writes, and for replace over a catalogue section", function (assert) {
    const none = importBundle.opens({ agents: [agent("a", [])], skills: [] });
    assert.strictEqual(importBundle.mustAsk(none!, false), false, "a bundle without either imports without a question");
    assert.strictEqual(importBundle.mustAsk(none!, true), false, "replace without a catalogue section deletes no service");
    assert.strictEqual(importBundle.mustAsk(importBundle.opens({ odata_services: [service()] })!, false), true);
    assert.strictEqual(importBundle.mustAsk(importBundle.opens({
        agents: [agent("w", [{ url: "builtin:odata", oauth: { services: ["a"], allow_write: true } }])]
    })!, false), true);
    const empty = importBundle.opens({ odata_services: [] })!;
    assert.strictEqual(importBundle.mustAsk(empty, false), false, "an empty section changes nothing");
    assert.strictEqual(importBundle.mustAsk(empty, true), true, "with replace it deletes every catalogue service");
});

QUnit.test("opens: a bundle whose shape cannot be read is null, never 'nothing'", function (assert) {
    const cases: [string, unknown][] = [
        ["no object", [service()]],
        ["null", null],
        ["services not a list", { odata_services: { a: service() } }],
        ["a service that is no object", { odata_services: ["sales-orders"] }],
        ["a service without a name", { odata_services: [service({ name: 7 })] }],
        ["a service without a definition", { odata_services: [service({ definition: undefined })] }],
        ["entity sets not a list", { odata_services: [service({ definition: { entity_sets: "A_Order", operations: [] } })] }],
        ["entity operations as text", { odata_services: [service({ definition: { entity_sets: [{ name: "A", operations: "create" }] } })] }],
        ["operations not a list", { odata_services: [service({ definition: { entity_sets: [], operations: { a: 1 } } })] }],
        ["user_context as text", { odata_services: [service({ user_context: "yes" })] }],
        ["agents not a list", { agents: { a: 1 } }],
        ["an agent that is no object", { agents: ["a"] }],
        ["servers not a list", { agents: [agent("a", "builtin:odata")] }]
    ];
    cases.forEach(([what, bundle]) => {
        assert.strictEqual(importBundle.opens(bundle), null, what);
    });
});

QUnit.test("opens: texts of a bundle are cut to one line", function (assert) {
    const found = importBundle.opens({ odata_services: [service({ title: "Sales\norders now" })] });
    assert.strictEqual(found?.services[0].title.indexOf("\n"), -1);
    assert.strictEqual(found?.services[0].title.indexOf(" "), -1);
});

QUnit.test("otherWarnings: the server's lines, without the two the lists already say", function (assert) {
    const removed = "OData service(s) removed by replace: 'old-one'";
    const identity = "OData service 'sales-orders': destination, user_context changed; used by agent(s) 'btp-agent'";
    const model = "Agent 'a': model 'x' is not deployed";
    const result: ImportResult = {
        status: "imported", warnings: [removed, identity, model, ""],
        removed_odata_service_names: ["old-one"],
        odata_identity_changes: [{ service: "sales-orders", changed: ["destination", "user_context"], agents: ["btp-agent"] }]
    };
    assert.deepEqual(importBundle.otherWarnings(result), [model]);
    assert.deepEqual(importBundle.otherWarnings({ status: "imported", warnings: [removed, identity, model] }),
        [removed, identity, model], "without the lists nothing is left out");
    assert.deepEqual(importBundle.otherWarnings({ status: "imported" }), []);
});
