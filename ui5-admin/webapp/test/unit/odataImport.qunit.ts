import odataCatalog from "com/agent/admin/model/odataCatalog";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";
import type {
    ODataDefinition, ODataEntitySet, ODataField, ODataMetadataPreview, ODataOperation, ODataPreviewEntitySet,
    ODataPreviewOperation
} from "com/agent/admin/service/types";

QUnit.module("odataCatalog: import from $metadata");

function field(name: string, flags: Partial<ODataField> = {}): ODataField {
    return {
        name, type: "Edm.String", label: "", selectable: false, filterable: false, writable: false,
        hint: "", values: [], personal_data: false, ...flags
    };
}

function entitySet(name: string, fields: ODataField[], rest: Partial<ODataEntitySet> = {}): ODataEntitySet {
    return {
        name, title: "", path: "", entity_type: `NS.${name}Type`, description: "",
        keys: [{ name: fields[0].name, type: fields[0].type }], operations: [], fields, navigations: [], examples: [],
        ...rest
    };
}

function operation(name: string, rest: Partial<ODataOperation> = {}): ODataOperation {
    return {
        name, qualified_name: "", title: "", kind: "function_import", http_method: "POST", bound_to: null,
        parameters: [], description: "", enabled: false, changes_data: true, ...rest
    };
}

/** A `$metadata` answer offering `sets` and `operations`; everything else
 *  as a complete, uncut document has it. */
function preview(
    sets: ODataPreviewEntitySet[], operations: ODataPreviewOperation[] = [], rest: Partial<ODataMetadataPreview> = {}
): ODataMetadataPreview {
    return {
        fetched_at: "2026-10-06T08:00:00+00:00", entity_sets: sets, operations, skipped: [],
        removed_entity_sets: [], removed_operations: [], removed_complete: true, skipped_stored_entity_sets: [],
        summary: {
            entity_sets: sets.length, operations: operations.length, in_service: 0, changed: 0, skipped: 0,
            removed_entity_sets: 0, removed_operations: 0, skipped_stored_entity_sets: 0
        },
        truncated: false, totals: { entity_sets: sets.length, operations: operations.length, skipped: 0 }, warnings: [],
        ...rest
    };
}

/** What the document says about `set`: everything declared as SAP allows it. */
function offered(set: ODataEntitySet, label = ""): ODataPreviewEntitySet {
    const out = FakeBackend.previewEntitySet(set, label);
    out.declared = { creatable: true, updatable: true, deletable: true };
    out.fields.forEach((f) => { f.declared = { filterable: true, creatable: true, updatable: true }; });
    return out;
}

const ORDER = entitySet("Orders", [
    field("OrderID", { label: "Order" }), field("Note", { label: "Note" }), field("Amount", { label: "Amount", type: "Edm.Decimal" })
], { navigations: [{ name: "to_Items", target: "Items", collection: true, description: "" }] });
const ITEM = entitySet("Items", [field("ItemID", { label: "Item" }), field("OrderID")]);

/** The admin's work on Orders: everything an import must not touch. */
function workedOn(): ODataEntitySet {
    return entitySet("Orders", [
        field("OrderID", { label: "My order number", selectable: true, filterable: true }),
        field("Note", { selectable: true, writable: true, hint: "Free text", personal_data: true,
            values: [{ value: "X", meaning: "urgent" }] }),
        field("Amount", { type: "Edm.Decimal", label: "Amount" })
    ], {
        title: "Sales orders", description: "Orders of a customer.", operations: ["list", "get", "update"],
        navigations: [{ name: "to_Items", target: "Items", collection: true, description: "The items." }],
        examples: [{ description: "Open orders", filter: "Note eq 'X'", select: ["OrderID"], orderby: "", top: 5 }]
    });
}

const EMPTY: ODataDefinition = { entity_sets: [], operations: [] };

QUnit.test("a new entity set arrives with labels and keys, all operations off and no field readable, whatever SAP declares", function (assert) {
    const doc = preview([offered(ORDER, "Sales order"), offered(ITEM)]);
    const out = odataCatalog.mergeImport(EMPTY, doc, ["set:Orders"]);
    assert.deepEqual(out.entity_sets.map((e) => e.name), ["Orders"], "only the ticked entity set");
    const set = out.entity_sets[0];
    assert.deepEqual(set.operations, [], "no operation is enabled although SAP declares create, update and delete");
    assert.deepEqual(
        set.fields.filter((f) => f.selectable || f.filterable || f.writable || f.personal_data).map((f) => f.name), [],
        "no field is readable, filterable or writable although SAP declares all of it"
    );
    assert.deepEqual(set.fields.map((f) => [f.name, f.type, f.label]), [
        ["OrderID", "Edm.String", "Order"], ["Note", "Edm.String", "Note"], ["Amount", "Edm.Decimal", "Amount"]
    ], "names, types and labels come from the document");
    assert.deepEqual(set.keys, [{ name: "OrderID", type: "Edm.String" }]);
    assert.strictEqual(set.title, "Sales order", "SAP's label is the first title");
    assert.strictEqual(set.description, "");
    assert.deepEqual(set.navigations, [{ name: "to_Items", target: "Items", collection: true, description: "" }]);
    assert.strictEqual(odataCatalog.hasWrite(out), false);
    assert.strictEqual(odataCatalog.pendingCount(odataCatalog.pendingWrites(EMPTY, out, false)), 0, "nothing for the pending strip");
    assert.deepEqual(EMPTY, { entity_sets: [], operations: [] }, "the definition handed in is not changed");
});

QUnit.test("an imported operation is disabled; changes_data follows the suggestion only when the document knows", function (assert) {
    const doc = preview([offered(ORDER)], [
        FakeBackend.previewOperation({ name: "Release", kind: "function_import", http_method: "POST" }),
        FakeBackend.previewOperation({ name: "GetStrategy", kind: "function_import", http_method: "GET" }),
        FakeBackend.previewOperation({
            name: "Guess", kind: "function_import", http_method: "GET",
            suggested: { changes_data: false, known: false, returns: null }
        }),
        FakeBackend.previewOperation({
            name: "Count", kind: "function", http_method: "GET", qualified_name: "NS.Count",
            suggested: { changes_data: false, known: true, returns: { entity_set: "Orders", collection: true, type: "" } }
        }),
        FakeBackend.previewOperation({
            name: "Other", kind: "function", http_method: "GET", qualified_name: "NS.Other", label: "Other one",
            suggested: { changes_data: false, known: true, returns: { entity_set: "Items", collection: false, type: "" } }
        })
    ]);
    const out = odataCatalog.mergeImport({ entity_sets: [workedOn()], operations: [] }, doc,
        ["op:Release", "op:GetStrategy", "op:Guess", "op:Count", "op:Other"]);
    assert.deepEqual(out.operations.map((o) => [o.name, o.enabled, o.changes_data]), [
        ["Release", false, true], ["GetStrategy", false, true], ["Guess", false, true], ["Count", false, false],
        ["Other", false, false]
    ], "all disabled; only a suggestion the document KNOWS marks an operation as only reading");
    assert.deepEqual(out.operations[3].returns, { entity_set: "Orders", collection: true }, "what it returns is kept");
    assert.strictEqual(out.operations[4].returns, null, "an entity set the service does not hold is not named");
    assert.strictEqual(out.operations[4].title, "Other one");
    assert.strictEqual(out.operations[3].qualified_name, "NS.Count");
    assert.deepEqual(odataCatalog.pendingReads({ entity_sets: [workedOn()], operations: [] }, out), [], "no read is opened");
    assert.strictEqual(odataCatalog.pendingCount(odataCatalog.pendingWrites(EMPTY, out, false)),
        odataCatalog.pendingCount(odataCatalog.pendingWrites(EMPTY, { entity_sets: [workedOn()], operations: [] }, false)),
        "and no write");
});

QUnit.test("a re-import keeps title, description, labels, value meanings, hints, ticks, examples and enabled switches", function (assert) {
    const mine: ODataDefinition = {
        entity_sets: [workedOn()],
        operations: [operation("Release", {
            title: "Release order", description: "Releases.", enabled: true, changes_data: false, http_method: "GET",
            returns: { entity_set: "Orders", collection: false }
        })]
    };
    const before = JSON.stringify(mine);
    const changed = offered(ORDER, "Sales order");
    changed.fields.push({ name: "Plant", type: "Edm.String", label: "Plant", declared: { filterable: true, creatable: true, updatable: true } });
    const doc = preview([changed, offered(ITEM)], [
        FakeBackend.previewOperation({ name: "Release", kind: "function_import", http_method: "GET", label: "SAP's name" })
    ]);

    assert.strictEqual(JSON.stringify(odataCatalog.mergeImport(mine, doc, [])), before, "nothing ticked: the same definition");
    const out = odataCatalog.mergeImport(mine, doc, ["field:Orders:Plant", "set:Items"]);
    assert.strictEqual(JSON.stringify(mine), before, "the definition handed in is not changed");
    const set = out.entity_sets[0];
    assert.deepEqual({ ...set, fields: set.fields.slice(0, 3) }, workedOn(), "everything the admin wrote on the entity set is as it was");
    assert.deepEqual(set.fields[3], field("Plant", { label: "Plant" }), "the ticked new field arrives unticked");
    assert.deepEqual(out.operations, mine.operations, "the operation keeps its title, description, switches and returns");
    assert.deepEqual(out.entity_sets.map((e) => e.name), ["Orders", "Items"]);
});

QUnit.test("only the ticked new fields are added to an existing entity set", function (assert) {
    const changed = offered(ORDER);
    changed.fields.push(
        { name: "Plant", type: "Edm.String", label: "Plant", declared: { filterable: true, creatable: true, updatable: true } },
        { name: "Origin", type: "Edm.String", label: "<b>x</b> {y}", declared: { filterable: false, creatable: false, updatable: false } }
    );
    const mine = { entity_sets: [workedOn()], operations: [] };
    const doc = preview([changed]);
    const plan = odataCatalog.importPlan(mine, doc);
    assert.deepEqual(plan.items.map((i) => [i.id, i.status, i.selectable]), [
        ["set:Orders", "changed", false], ["field:Orders:Plant", "new", true], ["field:Orders:Origin", "new", true],
        ["labels", "changed", true]
    ]);
    assert.deepEqual(plan.items[1].declared, ["filterable", "creatable", "updatable"], "what SAP declares is shown, as facts");
    const out = odataCatalog.mergeImport(mine, doc, ["field:Orders:Origin"]);
    assert.deepEqual(out.entity_sets[0].fields.map((f) => f.name), ["OrderID", "Note", "Amount", "Origin"]);
    assert.strictEqual(out.entity_sets[0].fields[3].label, "<b>x</b> {y}", "a label is text, as it came");
    assert.strictEqual(out.entity_sets[0].fields[1].label, "", "an empty label stays empty unless that row is ticked");
});

QUnit.test("a label the admin changed is kept; an empty one is filled from the metadata when asked", function (assert) {
    const mine = { entity_sets: [workedOn()], operations: [] };
    const doc = preview([offered(ORDER)]);
    const plan = odataCatalog.importPlan(mine, doc);
    assert.strictEqual(plan.items.filter((i) => i.id === "labels")[0].count, 1, "one empty label the document can fill");
    const out = odataCatalog.mergeImport(mine, doc, ["labels"]);
    assert.deepEqual(out.entity_sets[0].fields.map((f) => f.label), ["My order number", "Note", "Amount"]);
    assert.deepEqual(
        { ...out.entity_sets[0], fields: [] }, { ...workedOn(), fields: [] }, "and nothing else of the entity set changes"
    );
    assert.strictEqual(odataCatalog.importLabel("  two\nlines here\t "), "two lines here", "a label is one line");
    assert.strictEqual(odataCatalog.importLabel("x".repeat(300)).length, 120, "of at most 120 characters");
});

QUnit.test("removed fields, entity sets and operations are listed and removed only when ticked", function (assert) {
    const mine: ODataDefinition = {
        entity_sets: [workedOn(), entitySet("Gone", [field("ID", { selectable: true })], { title: "Old set", operations: ["list", "get"] })],
        operations: [operation("OldAction", { title: "Old action", enabled: true, bound_to: "Gone" })]
    };
    const less = offered(ORDER);
    less.fields = less.fields.filter((f) => f.name !== "Note");
    const doc = preview([less]);
    const plan = odataCatalog.importPlan(mine, doc);
    const row = (id: string) => plan.items.filter((i) => i.id === id)[0];
    assert.deepEqual(plan.items.map((i) => [i.id, i.status]), [
        ["set:Orders", "changed"], ["rmfield:Orders:Note", "removed"], ["rmset:Gone", "removed"], ["rmop:OldAction", "removed"]
    ]);
    assert.deepEqual([row("rmfield:Orders:Note").lostReadable, row("rmfield:Orders:Note").lostWritable], [1, 1], "what agents lose with the field");
    assert.deepEqual(row("rmset:Gone").lostOperations, ["list", "get"]);
    assert.strictEqual(row("rmset:Gone").lostReadable, 1);
    assert.deepEqual(row("rmset:Gone").blockers, [{ key: "odataRemoveEntityBound", operations: ["Old action"] }]);
    assert.strictEqual(row("rmop:OldAction").lostEnabled, true);

    assert.deepEqual(odataCatalog.mergeImport(mine, doc, []), mine, "nothing is removed by itself");
    const one = odataCatalog.mergeImport(mine, doc, ["rmfield:Orders:Note"]);
    assert.deepEqual(one.entity_sets[0].fields.map((f) => f.name), ["OrderID", "Amount"]);
    assert.deepEqual(one.entity_sets[1], mine.entity_sets[1]);

    const blocked = odataCatalog.mergeImport(mine, doc, ["rmset:Gone"]);
    assert.deepEqual(blocked.entity_sets.map((e) => e.name), ["Orders", "Gone"], "an entity set an operation is bound to stays");
    assert.deepEqual(odataCatalog.importSummary(mine, doc, ["rmset:Gone"]).blocked, [
        { id: "rmset:Gone", key: "odataRemoveEntityBound", args: ["Old set", "Gone", "Old action"] }
    ], "and Apply is told why");
    const both = odataCatalog.mergeImport(mine, doc, ["rmset:Gone", "rmop:OldAction"]);
    assert.deepEqual([both.entity_sets.map((e) => e.name), both.operations], [["Orders"], []], "with the operation it goes");
    assert.deepEqual(odataCatalog.importSummary(mine, doc, ["rmset:Gone", "rmop:OldAction"]).blocked, []);
    assert.deepEqual(
        odataCatalog.mergeImport(mine, doc, ["rmfield:Orders:OrderID", "rmset:Orders", "rmop:Nothing"]), mine,
        "a removal of something the document still has is not taken"
    );
});

QUnit.test("a removed key field takes its key entry along", function (assert) {
    const mine = { entity_sets: [entitySet("Orders", [field("OrderID"), field("Client")], {
        keys: [{ name: "Client", type: "Edm.String" }, { name: "OrderID", type: "Edm.String" }]
    })], operations: [] };
    const doc = preview([offered(entitySet("Orders", [field("OrderID")]))]);
    const plan = odataCatalog.importPlan(mine, doc);
    assert.deepEqual(plan.items.map((i) => i.id), ["set:Orders", "key:Orders", "rmfield:Orders:Client"]);
    assert.strictEqual(plan.items[2].lostKey, true);
    const out = odataCatalog.mergeImport(mine, doc, ["rmfield:Orders:Client"]);
    assert.deepEqual(out.entity_sets[0].keys, [{ name: "OrderID", type: "Edm.String" }]);
});

QUnit.test("a changed key, entity type or field type is a row of its own and is taken only when ticked", function (assert) {
    const mine = { entity_sets: [workedOn()], operations: [] };
    const other = offered(entitySet("Orders", [
        field("OrderID", { type: "Edm.Guid" }), field("Note"), field("Amount", { type: "Edm.Decimal" }), field("Client")
    ], { entity_type: "NS2.OrderType" }));
    other.keys = [{ name: "Client", type: "Edm.String" }, { name: "OrderID", type: "Edm.Guid" }];
    other.keys_total = 2;
    const doc = preview([other]);
    const plan = odataCatalog.importPlan(mine, doc);
    assert.deepEqual(plan.items.map((i) => [i.id, i.was, i.now]), [
        ["set:Orders", "", ""], ["key:Orders", "OrderID", "Client, OrderID"],
        ["etype:Orders", "NS.OrdersType", "NS2.OrderType"], ["type:Orders:OrderID", "Edm.String", "Edm.Guid"],
        ["field:Orders:Client", "", ""]
    ]);
    assert.deepEqual(odataCatalog.mergeImport(mine, doc, ["field:Orders:Client"]).entity_sets[0],
        { ...workedOn(), fields: workedOn().fields.concat([field("Client")]) }, "a new field alone changes no key and no type");

    const typed = odataCatalog.mergeImport(mine, doc, ["type:Orders:OrderID"]).entity_sets[0];
    assert.deepEqual([typed.fields[0].type, typed.keys, typed.entity_type],
        ["Edm.Guid", [{ name: "OrderID", type: "Edm.Guid" }], "NS.OrdersType"], "the type, also in the key; nothing else");
    assert.strictEqual(typed.fields[0].label, "My order number");
    assert.strictEqual(typed.fields[0].selectable, true, "the ticks stay");

    const keyed = odataCatalog.mergeImport(mine, doc, ["key:Orders", "etype:Orders"]).entity_sets[0];
    assert.deepEqual(keyed.keys, [{ name: "Client", type: "Edm.String" }, { name: "OrderID", type: "Edm.Guid" }]);
    assert.strictEqual(keyed.entity_type, "NS2.OrderType");
    assert.deepEqual(keyed.fields[3], field("Client"), "a key field the service lacks comes along, unticked");
    assert.strictEqual(keyed.fields[0].type, "Edm.String", "a field's type is its own row");

    const cut = { ...other, keys_total: 70 };
    assert.strictEqual(
        odataCatalog.importPlan(mine, preview([cut])).items.some((i) => i.kind === "key_change"), false,
        "a key the server cut is not offered"
    );
});

QUnit.test("importSummary counts what Apply will add, change and remove, and what blocks it", function (assert) {
    const mine = { entity_sets: [workedOn()], operations: [] };
    const doc = preview([offered(ORDER), offered(ITEM)], [
        FakeBackend.previewOperation({ name: "Split", kind: "function_import", http_method: "POST", bound_to: "Items" })
    ]);
    const plan = odataCatalog.importPlan(mine, doc);
    assert.strictEqual(plan.items.filter((i) => i.id === "op:Split")[0].needs, "Items", "the row names the entity set it needs");
    assert.deepEqual(odataCatalog.importSummary(mine, doc, []), { add: 0, change: 0, remove: 0, total: 0, blocked: [] });
    assert.deepEqual(odataCatalog.importSummary(mine, doc, ["op:Split", "labels"]), {
        add: 1, change: 1, remove: 0, total: 2,
        blocked: [{ id: "op:Split", key: "odataImportNeedsSet", args: ["Split", "Items"] }]
    });
    assert.deepEqual(odataCatalog.mergeImport(mine, doc, ["op:Split"]).operations, [], "and it is not added without it");
    assert.deepEqual(odataCatalog.importSummary(mine, doc, ["op:Split", "set:Items"]), {
        add: 2, change: 0, remove: 0, total: 2, blocked: []
    });
    assert.deepEqual(odataCatalog.mergeImport(mine, doc, ["op:Split", "set:Items"]).operations.map((o) => o.bound_to), ["Items"]);
    assert.deepEqual(plan.counts, { entitySets: 2, operations: 1, isNew: 2, changed: 0, inService: 1, removed: 0 });
});

QUnit.test("what was removed is not guessed: an unfinished document lists none, a cut one only what the server compared", function (assert) {
    const mine = { entity_sets: [workedOn(), entitySet("Gone", [field("ID")]), entitySet("Odd", [field("ID")])], operations: [operation("OldAction")] };
    const unfinished = odataCatalog.importPlan(mine, preview([offered(ORDER)], [], {
        removed_complete: false, removed_entity_sets: null, removed_operations: null
    }));
    assert.strictEqual(unfinished.removalsKnown, false);
    assert.deepEqual(unfinished.items.filter((i) => i.status === "removed"), []);

    const cut = odataCatalog.importPlan(mine, preview([offered(ORDER)], [], {
        totals: { entity_sets: 300, operations: 300, skipped: 0 }, truncated: true,
        removed_entity_sets: ["Gone"], removed_operations: []
    }));
    assert.deepEqual(cut.items.filter((i) => i.status === "removed").map((i) => i.id), ["rmset:Gone"],
        "past the cut only what the server names is removed");

    const skipped = odataCatalog.importPlan(mine, preview([offered(ORDER)], [], {
        skipped_stored_entity_sets: [{ name: "Odd", reason: "unrepresentable_key" }]
    }));
    assert.deepEqual(skipped.items.filter((i) => i.name === "Odd").map((i) => [i.id, i.status, i.selectable, i.skippedReason]),
        [["set:Odd", "in_service", false, "unrepresentable_key"]], "declared but left out by the parser is not removed");

    const wide = offered(ORDER);
    wide.fields = wide.fields.filter((f) => f.name !== "Note");
    wide.truncated = true;
    wide.status = "changed";
    wide.removed_fields = [];
    assert.deepEqual(
        odataCatalog.importPlan(mine, preview([wide])).items.filter((i) => i.kind === "removed_field"), [],
        "a field that is not listed of a cut entity set is not called removed"
    );
});

QUnit.test("a document of 200 entity sets, one of them with 500 fields, is planned and filtered without listing every field", function (assert) {
    const sets: ODataPreviewEntitySet[] = [];
    const big: ODataField[] = [];
    for (let i = 0; i < 500; i++) {
        big.push(field(`F${i}`, { label: `Field ${i}` }));
    }
    for (let i = 0; i < 200; i++) {
        sets.push(offered(entitySet(`Set${i}`, i === 7 ? big : [field("ID")])));
    }
    const mine = { entity_sets: [entitySet("Set7", [field("F0", { label: "Mine" })])], operations: [] };
    const doc = preview(sets);
    const plan = odataCatalog.importPlan(mine, doc);
    assert.strictEqual(plan.items.length, 200 + 499, "one row per entity set, and the 499 new fields under the one in the service");
    assert.strictEqual(odataCatalog.importRows(plan.items, "", "all", {}).length, 200, "fields are listed when their entity set is opened");
    assert.strictEqual(odataCatalog.importRows(plan.items, "", "all", { "set:Set7": true }).length, 699);
    assert.deepEqual(odataCatalog.importRows(plan.items, "field 49", "all", { "set:Set7": true }).map((i) => i.name),
        ["Set7", "F49", "F490", "F491", "F492", "F493", "F494", "F495", "F496", "F497", "F498", "F499"],
        "a search finds a field by its label and keeps its entity set above it");
    assert.deepEqual(odataCatalog.importRows(plan.items, "", "changed", {}).map((i) => i.name), ["Set7"]);
    assert.strictEqual(odataCatalog.importRows(plan.items, "", "new", {}).length, 200, "new entity sets, and the one with new fields");
    assert.strictEqual(odataCatalog.importRows(plan.items, "", "in_service", {}).length, 0);
    const all = plan.items.filter((i) => i.selectable).map((i) => i.id);
    const summary = odataCatalog.importSummary(mine, doc, all);
    assert.strictEqual(summary.add, 199 + 499);
    assert.deepEqual(summary.blocked, []);
    const out = odataCatalog.mergeImport(mine, doc, all);
    assert.deepEqual([out.entity_sets.length, out.entity_sets[0].fields.length], [200, 500]);
    const over = odataCatalog.importSummary({ entity_sets: mine.entity_sets.concat([entitySet("Extra", [field("ID")])]), operations: [] }, doc, all);
    assert.deepEqual(over.blocked, [{ id: "", key: "odataImportTooManySets", args: [201, 200] }], "more than a service holds is said");
});
