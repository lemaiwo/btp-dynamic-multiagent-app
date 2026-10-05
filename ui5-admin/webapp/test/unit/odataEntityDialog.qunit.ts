import odataCatalog from "com/agent/admin/model/odataCatalog";
import type { ODataDefinition, ODataEntitySet, ODataField } from "com/agent/admin/service/types";
import { ENTITY_ACCEPTED, ENTITY_CASES, changed, validEntitySet } from "./odataRuleCases";

QUnit.module("odataCatalog: the entity set dialog");

function field(name: string, flags: Partial<ODataField> = {}): ODataField {
    return {
        name, type: "Edm.String", label: "", selectable: false, filterable: false, writable: false,
        hint: "", values: [], personal_data: false, ...flags
    };
}

function definition(...entitySets: ODataEntitySet[]): ODataDefinition {
    return { entity_sets: entitySets, operations: [] };
}

QUnit.test("value meanings parse and format round trip", function (assert) {
    assert.deepEqual(
        odataCatalog.parseValueMeanings("B = awaiting release; 05 = released"),
        [{ value: "B", meaning: "awaiting release" }, { value: "05", meaning: "released" }]
    );
    assert.strictEqual(odataCatalog.formatValueMeanings([{ value: "B", meaning: "awaiting release" }]), "B = awaiting release");
    assert.deepEqual(odataCatalog.parseValueMeanings("  "), []);
    assert.deepEqual(odataCatalog.parseValueMeanings(undefined), []);
    assert.strictEqual(odataCatalog.formatValueMeanings(undefined), "");
    assert.deepEqual(
        odataCatalog.parseValueMeanings("A=x=y;;  "), [{ value: "A", meaning: "x=y" }],
        "the first '=' separates; an empty part is nothing"
    );
    const text = "B = awaiting release; 05 = released";
    assert.strictEqual(odataCatalog.formatValueMeanings(odataCatalog.parseValueMeanings(text)), text);
});

QUnit.test("valueMeaningsProblem refuses what cannot be stored, and never drops a part silently", function (assert) {
    assert.strictEqual(odataCatalog.valueMeaningsProblem(""), "");
    assert.strictEqual(odataCatalog.valueMeaningsProblem("B = open; 05 = released;"), "");
    assert.strictEqual(odataCatalog.valueMeaningsProblem("B = open; released"), "odataErrMeaningsFormat", "a part without '='");
    assert.strictEqual(odataCatalog.valueMeaningsProblem("= open"), "odataErrMeaningsFormat", "no value");
    assert.strictEqual(odataCatalog.valueMeaningsProblem("B ="), "odataErrMeaningsFormat", "no meaning");
    assert.strictEqual(odataCatalog.valueMeaningsProblem(`${"x".repeat(65)} = a`), "odataErrMeaningsLength");
    assert.strictEqual(odataCatalog.valueMeaningsProblem(`B = ${"x".repeat(201)}`), "odataErrMeaningsLength");
    assert.strictEqual(odataCatalog.valueMeaningsProblem(`B = ${"x".repeat(200)}`), "");
    assert.strictEqual(odataCatalog.valueMeaningsProblem("B = a\tb"), "odataErrMeaningsLength", "one line each");
});

QUnit.test("filterFields by text and by all | ticked | unticked | personal data", function (assert) {
    const fields = [
        field("PurchaseRequisition", { label: "Requisition", selectable: true, filterable: true }),
        field("Plant", { label: "Plant" }),
        field("RequestedQuantity", { label: "Quantity", writable: true }),
        field("CreatedByUser", { label: "Created by", personal_data: true })
    ];
    const names = (query: string, mode: "all" | "ticked" | "unticked" | "personal") => (
        odataCatalog.filterFields(fields, query, mode).map((f) => f.name)
    );
    assert.deepEqual(names("", "all"), ["PurchaseRequisition", "Plant", "RequestedQuantity", "CreatedByUser"]);
    assert.deepEqual(names("  requ ", "all"), ["PurchaseRequisition", "RequestedQuantity"], "the technical name, any case");
    assert.deepEqual(names("created by", "all"), ["CreatedByUser"], "the label");
    assert.deepEqual(names("", "ticked"), ["PurchaseRequisition", "RequestedQuantity"], "Read, Filter or Write");
    assert.deepEqual(names("", "unticked"), ["Plant", "CreatedByUser"]);
    assert.deepEqual(names("", "personal"), ["CreatedByUser"]);
    assert.deepEqual(names("plant", "ticked"), [], "both at once");
});

QUnit.test("entitySetIssues names, first, what the server refuses first: every rule of the shared table", function (assert) {
    assert.deepEqual(odataCatalog.entitySetIssues(validEntitySet()), [], "the valid one");
    ENTITY_CASES.forEach((testCase) => {
        const first = odataCatalog.entitySetIssues(changed(testCase.change))[0];
        assert.deepEqual(
            { loc: first?.loc, key: first?.key }, { loc: testCase.loc, key: testCase.clientKey }, testCase.rule
        );
    });
    ENTITY_ACCEPTED.forEach((accepted) => {
        assert.deepEqual(odataCatalog.entitySetIssues(changed(accepted.change)), [], accepted.rule);
    });
});

QUnit.test("entitySetIssues: the name of another entity set, and arguments that name the field", function (assert) {
    assert.deepEqual(
        odataCatalog.entitySetIssues(validEntitySet(), ["A_Other", "A_Item"]),
        [{ loc: "name", key: "odataErrDuplicateEntitySet", args: ["A_Item"] }]
    );
    assert.deepEqual(
        odataCatalog.entitySetIssues(changed((e) => { e.fields[1].label = "x".repeat(121); e.fields[0].hint = "x".repeat(301); })),
        [
            { loc: "fields.0.hint", key: "odataErrFieldHintTooLong", args: ["Id"] },
            { loc: "fields.1.label", key: "odataErrFieldLabelTooLong", args: ["Status"] }
        ],
        "all of them, each with the field it is about"
    );
});

QUnit.test("a field newly writable where Create or Update is, or becomes, enabled is a pending write", function (assert) {
    const item = (operations: ODataEntitySet["operations"], writable: string[]): ODataEntitySet => ({
        ...validEntitySet(), title: "Item", operations,
        fields: [
            field("Id", { selectable: true }), field("Status", { selectable: true, writable: writable.indexOf("Status") !== -1 }),
            field("Note", { writable: writable.indexOf("Note") !== -1 })
        ]
    });
    const pending = (stored: ODataEntitySet, current: ODataEntitySet, switchedOn = false) => (
        odataCatalog.pendingWrites(definition(stored), definition(current), switchedOn)
    );
    const one = [{ name: "A_Item", title: "Item", fields: ["Note"] }];

    assert.deepEqual(
        pending(item(["list", "update"], ["Status"]), item(["list", "update"], ["Status", "Note"])).fields, one,
        "newly marked, Update already on"
    );
    assert.deepEqual(
        pending(item(["list", "update"], ["Status", "Note"]), item(["list", "update"], ["Status", "Note"])).fields, [],
        "nothing new"
    );
    assert.deepEqual(
        pending(item(["list", "update"], ["Status", "Note"]), item(["list", "update"], ["Status"])).fields, [],
        "unmarking is not pending"
    );
    assert.deepEqual(
        pending(item(["list"], ["Status"]), item(["list"], ["Status", "Note"])).fields, [],
        "without Create or Update nothing can be written"
    );
    assert.deepEqual(
        pending(item(["list", "delete"], ["Status"]), item(["list", "delete"], ["Status", "Note"])).fields, [],
        "Delete sends no fields"
    );
    assert.deepEqual(
        pending(item(["list"], ["Status"]), item(["list", "create"], ["Status"])),
        {
            entitySets: [{ name: "A_Item", title: "Item", operations: ["create"] }], operations: [],
            fields: [{ name: "A_Item", title: "Item", fields: ["Status"] }]
        },
        "Create becomes enabled: the fields marked before can be written from now on"
    );
    assert.deepEqual(
        pending(item(["list", "update"], ["Status"]), item(["list", "update"], ["Status"]), true),
        { entitySets: [{ name: "A_Item", title: "Item", operations: ["update"] }], operations: [], fields: [] },
        "a service that is switched on names its operations; its fields were writable before"
    );
    assert.deepEqual(
        odataCatalog.pendingWrites(undefined, definition(item(["update"], ["Note"]))).fields, one, "a new service"
    );
    assert.strictEqual(
        odataCatalog.pendingCount(pending(item(["list", "update"], []), item(["list", "update"], ["Status", "Note"]))), 2,
        "each field counts"
    );
});

QUnit.test("navigationFollow says whether an agent could follow a navigation today", function (assert) {
    const source = validEntitySet();
    const target = (operations: ODataEntitySet["operations"]): ODataEntitySet => (
        { ...validEntitySet(), name: "A_Text", title: "Text", operations }
    );
    const many = source.navigations[0];
    const single = { ...many, collection: false };
    const follow = (from: ODataEntitySet, navigation: typeof many, ...others: ODataEntitySet[]) => (
        odataCatalog.navigationFollow(from, navigation, definition(from, ...others))
    );

    assert.deepEqual(follow(source, many, target(["list"])), { ok: true, key: "odataNavFollowable", args: [] });
    assert.deepEqual(follow(source, many), { ok: false, key: "odataNavTargetMissing", args: ["A_Text"] });
    assert.deepEqual(
        follow({ ...source, operations: ["list"] }, many, target(["list"])), { ok: false, key: "odataNavNeedsGet", args: [] },
        "Get on the entity set it starts from"
    );
    assert.deepEqual(
        follow(source, many, target(["get"])), { ok: false, key: "odataNavTargetNeedsList", args: ["Text (A_Text)"] },
        "a collection needs List on the target"
    );
    assert.deepEqual(
        follow(source, single, target(["list"])), { ok: false, key: "odataNavTargetNeedsGet", args: ["Text (A_Text)"] },
        "a single entity needs Get on the target"
    );
    assert.strictEqual(follow(source, single, target(["get"])).ok, true);
    assert.strictEqual(
        odataCatalog.navigationFollow(source, { ...many, target: "A_Item" }, definition()).ok, true,
        "a navigation to itself is judged by the entity set as it is in the dialog"
    );
});

QUnit.test("exampleWarning says which example agents will not see, by the run-time rule", function (assert) {
    const entitySet: ODataEntitySet = {
        ...validEntitySet(),
        fields: [
            field("Id", { selectable: true }), field("Status", { selectable: true }),
            field("CreatedByUser", { personal_data: true }), field("Note", { writable: true }), field("Plant")
        ]
    };
    const example = (over: Partial<ODataEntitySet["examples"][0]>) => (
        odataCatalog.exampleWarning(entitySet, { description: "Open items", filter: "", select: [], orderby: "", top: null, ...over })
    );

    assert.strictEqual(example({ filter: "Status eq 'B'", select: ["Id"], orderby: "Id desc" }), undefined, "all readable");
    assert.deepEqual(
        example({ filter: "createdbyuser eq 'X' and Plant eq '1'" }),
        { dropped: true, key: "odataExampleDropped", args: ["CreatedByUser, Plant"] },
        "a hidden field in the filter, whatever the case"
    );
    assert.strictEqual(example({ orderby: "Plant" })?.key, "odataExampleDropped", "in the orderby");
    assert.strictEqual(example({ description: "Items of one plant" })?.key, "odataExampleDropped", "even in the description");
    assert.strictEqual(example({ filter: "Plants eq 1 and MyPlant eq 2" }), undefined, "as a word, not as part of one");
    assert.deepEqual(
        example({ select: ["Plant", "Nope"] }), { dropped: true, key: "odataExampleDroppedSelect", args: ["Plant, Nope"] },
        "a select of which nothing is left"
    );
    assert.deepEqual(
        example({ select: ["Id", "Plant"] }), { dropped: false, key: "odataExampleSelectTrimmed", args: ["Plant"] },
        "a select that loses an entry"
    );
    assert.deepEqual(
        example({ filter: "Note eq 'x'" }), { dropped: false, key: "odataExampleWriteOnly", args: ["Note"] },
        "a write-only field: agents with Allow writes still see the example"
    );
    assert.strictEqual(
        odataCatalog.exampleWarning(
            { ...entitySet, operations: ["list", "get"] },
            { description: "d", filter: "Note eq 'x'", select: [], orderby: "", top: null }
        )?.key, "odataExampleDropped", "without Create or Update a writable field is hidden from everyone"
    );
    assert.strictEqual(
        odataCatalog.exampleWarning(
            { ...entitySet, fields: [field("Id"), field("Status", { selectable: true })] },
            { description: "d", filter: "Id eq '1'", select: [], orderby: "", top: null }
        ), undefined, "a key's name is never hidden"
    );
});

QUnit.test("hiddenKeys: key fields an agent can address but not see, while Get is on", function (assert) {
    const entitySet = validEntitySet();
    assert.deepEqual(odataCatalog.hiddenKeys(entitySet), []);
    entitySet.fields[0].selectable = false;
    entitySet.fields[0].filterable = false;
    assert.deepEqual(odataCatalog.hiddenKeys(entitySet), ["Id"]);
    assert.deepEqual(odataCatalog.hiddenKeys({ ...entitySet, operations: ["list"] }), [], "only Get returns one record by key");
});

QUnit.test("boundOperations names the operations that keep an entity set from being removed", function (assert) {
    const bound: ODataDefinition = {
        entity_sets: [validEntitySet()],
        operations: [{
            name: "Release", qualified_name: "", title: "Release item", kind: "function_import", http_method: "POST",
            bound_to: "A_Item", parameters: [], description: "", enabled: false, changes_data: true
        }]
    };
    assert.deepEqual(odataCatalog.boundOperations(bound, "A_Item"), ["Release item"]);
    assert.deepEqual(odataCatalog.boundOperations(bound, "A_Text"), []);
    assert.deepEqual(odataCatalog.boundOperations(undefined, "A_Item"), []);
});
