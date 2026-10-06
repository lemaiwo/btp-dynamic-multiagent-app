import odataCatalog from "com/agent/admin/model/odataCatalog";
import type {
    ODataDefinition, ODataEntitySet, ODataField, ODataServiceInput
} from "com/agent/admin/service/types";
import { ONE_LINE_ACCEPTED, PATH_CASES, RULE_CASES, VALID_PATHS, withField } from "./odataRuleCases";

QUnit.module("odataCatalog");

function field(name: string, flags: Partial<ODataField> = {}): ODataField {
    return {
        name, type: "Edm.String", label: "", selectable: false, filterable: false, writable: false,
        hint: "", values: [], personal_data: false, ...flags
    };
}

function entitySet(name: string, over: Partial<ODataEntitySet> = {}): ODataEntitySet {
    return {
        name, title: "", path: "", entity_type: "", description: "",
        keys: [{ name: "Id", type: "Edm.String" }], operations: ["list", "get"],
        fields: [field("Id", { selectable: true }), field("Text", { selectable: true, writable: true })],
        navigations: [], examples: [], ...over
    };
}

const WRITING: ODataDefinition = {
    entity_sets: [
        entitySet("A_PurchaseRequisitionItem", { title: "Requisition item", operations: ["list", "get", "update"] }),
        entitySet("A_PurchaseReqnItemText", { title: "Item text", operations: ["list", "get", "update", "create"] })
    ],
    operations: [{
        name: "ReleaseItem", qualified_name: "", title: "Release item", kind: "function_import",
        http_method: "POST", bound_to: "A_PurchaseRequisitionItem",
        parameters: [{ name: "ReleaseCode", type: "Edm.String", required: true }],
        description: "", enabled: true, changes_data: true
    }]
};

const READ_ONLY: ODataDefinition = {
    entity_sets: [entitySet("A_PurchaseRequisitionItem")],
    operations: []
};

function input(over: Partial<ODataServiceInput> = {}): ODataServiceInput {
    return {
        name: "purchase-requisitions", title: "Purchase requisitions",
        purpose: "Read requisitions and their items to judge an approval", not_for: "",
        destination: "S4_ODATA_USER", user_context: true, odata_version: "v2",
        service_path: "/sap/opu/odata/sap/SRV", enabled: true,
        definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null, ...over
    };
}

QUnit.test("counts, hasWrite and writeSummary", function (assert) {
    assert.deepEqual(odataCatalog.counts(WRITING), { entitySets: 2, operations: 1 });
    assert.strictEqual(odataCatalog.hasWrite(READ_ONLY), false);
    assert.strictEqual(odataCatalog.hasWrite(WRITING), true);
    assert.deepEqual(
        odataCatalog.writeSummary(WRITING),
        ["Requisition item (update)", "Item text (create, update)", "Release item"]
    );
    assert.deepEqual(odataCatalog.writeSummary(READ_ONLY), []);
});

QUnit.test("an operation counts as a write only when it is enabled and is not a plain read", function (assert) {
    const operation = WRITING.operations[0];
    const withOperation = (over: Partial<typeof operation>): ODataDefinition => ({
        entity_sets: READ_ONLY.entity_sets, operations: [{ ...operation, ...over }]
    });

    assert.strictEqual(odataCatalog.hasWrite(withOperation({})), true);
    assert.strictEqual(odataCatalog.hasWrite(withOperation({ enabled: false })), false, "disabled");
    assert.strictEqual(
        odataCatalog.hasWrite(withOperation({ changes_data: false, http_method: "GET" })), false, "read-only call"
    );
    assert.deepEqual(odataCatalog.writeSummary(withOperation({ enabled: false })), []);
    assert.deepEqual(
        odataCatalog.writeSummary(withOperation({ title: "" })), ["ReleaseItem"],
        "an operation without a title is named by its name"
    );
});

QUnit.test("counts, hasWrite and writeSummary accept a missing definition", function (assert) {
    assert.deepEqual(odataCatalog.counts(undefined), { entitySets: 0, operations: 0 });
    assert.strictEqual(odataCatalog.hasWrite(undefined), false);
    assert.deepEqual(odataCatalog.writeSummary(undefined), []);
});

QUnit.test("fieldCount is 'selectable of total'", function (assert) {
    const fields: ODataField[] = [];
    for (let i = 0; i < 89; i++) {
        fields.push(field(`F${i}`, { selectable: i < 24 }));
    }

    assert.deepEqual(odataCatalog.fieldCount(entitySet("A", { fields })), { selected: 24, total: 89 });
    assert.deepEqual(odataCatalog.fieldCount(entitySet("A", { fields: [] })), { selected: 0, total: 0 });
});

QUnit.test("titleOf is the title, or the name when there is none", function (assert) {
    assert.strictEqual(odataCatalog.titleOf(entitySet("A_Item", { title: "Requisition item" })), "Requisition item");
    assert.strictEqual(odataCatalog.titleOf(entitySet("A_Item")), "A_Item");
    assert.strictEqual(odataCatalog.titleOf(entitySet("A_Item", { title: "   " })), "A_Item");
});

QUnit.test("emptyService has every field of ODataServiceInput", function (assert) {
    assert.deepEqual(odataCatalog.emptyService(), {
        name: "", title: "", purpose: "", not_for: "", destination: "", user_context: false,
        odata_version: "v2", service_path: "", enabled: true,
        definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null
    });
    assert.notStrictEqual(
        odataCatalog.emptyService().definition, odataCatalog.emptyService().definition,
        "each call returns its own definition object"
    );
});

QUnit.test("validate requires title, purpose (<= 200), destination, name slug and a confined path", function (assert) {
    assert.deepEqual(odataCatalog.validate(input()), {}, "a complete input has no errors");

    assert.deepEqual(odataCatalog.validate(odataCatalog.emptyService()), {
        name: "odataErrNameRequired", title: "odataErrTitleRequired", purpose: "odataErrPurposeRequired",
        destination: "odataErrDestinationRequired", service_path: "odataErrPathRequired"
    }, "one key per missing field");

    assert.deepEqual(odataCatalog.validate(input({ title: "   ", purpose: " \n" })), {
        title: "odataErrTitleRequired", purpose: "odataErrPurposeRequired"
    }, "blank is missing");
    assert.deepEqual(odataCatalog.validate(input({ title: "x".repeat(121) })), { title: "odataErrTitleTooLong" });
    assert.deepEqual(odataCatalog.validate(input({ purpose: "x".repeat(200) })), {}, "200 characters fit");
    assert.deepEqual(odataCatalog.validate(input({ purpose: "x".repeat(201) })), { purpose: "odataErrPurposeTooLong" });
    assert.deepEqual(odataCatalog.validate(input({ not_for: "x".repeat(201) })), { not_for: "odataErrNotForTooLong" });
    assert.deepEqual(odataCatalog.validate(input({ destination: "has space" })), {
        destination: "odataErrDestinationInvalid"
    });
});

QUnit.test("validate accepts only the slug the server accepts as a name", function (assert) {
    ["a", "a1", "purchase-requisitions", "0-9", "a".repeat(64)].forEach((name) => {
        assert.deepEqual(odataCatalog.validate(input({ name })), {}, name);
    });
    ["Purchase", "a_b", "-a", "a-", "a b", "a".repeat(65), "é"].forEach((name) => {
        assert.deepEqual(odataCatalog.validate(input({ name })), { name: "odataErrNameInvalid" }, name);
    });
});

QUnit.test("validate confines the service path like the server does", function (assert) {
    VALID_PATHS.forEach((path) => {
        assert.deepEqual(odataCatalog.validate(input({ service_path: path })), {}, path.substring(0, 60));
    });
    PATH_CASES.forEach((testCase) => {
        assert.deepEqual(
            odataCatalog.validate(withField(testCase)), { service_path: testCase.clientKey }, testCase.rule
        );
    });
});

QUnit.test("validate names one key per refused field, for every rule of the shared table", function (assert) {
    RULE_CASES.forEach((testCase) => {
        assert.deepEqual(
            odataCatalog.validate(withField(testCase)), { [testCase.field]: testCase.clientKey }, testCase.rule
        );
    });
});

QUnit.test("validate accepts a no-break space and whatever surrounds a title or purpose", function (assert) {
    ONE_LINE_ACCEPTED.forEach((over) => {
        assert.deepEqual(odataCatalog.validate(input(over)), {}, JSON.stringify(over));
    });
});

QUnit.test("validate accepts a payload without the two flags: the server defaults them", function (assert) {
    const { user_context, enabled, ...withoutFlags } = input();
    void [user_context, enabled];

    assert.deepEqual(odataCatalog.validate(withoutFlags as ODataServiceInput), {});
    assert.deepEqual(
        odataCatalog.validate({ ...withoutFlags, enabled: null } as unknown as ODataServiceInput),
        { enabled: "odataErrBoolean" }, "present and not a boolean is still refused"
    );
});

QUnit.test("serverStrip strips what the server strips, which is not what trim() strips", function (assert) {
    assert.strictEqual(odataCatalog.serverStrip("  a b \t\r\n"), "a b");
    assert.strictEqual(odataCatalog.serverStrip("\u0085a\u00a0\u2028\u3000"), "a", "NEL and the Unicode spaces go");
    assert.strictEqual(odataCatalog.serverStrip("\ufeffa\ufeff"), "\ufeffa\ufeff", "U+FEFF stays");
    assert.strictEqual(odataCatalog.serverStrip("a\u001f"), "a\u001f", "a separator control stays (and is refused)");
    assert.strictEqual(odataCatalog.serverStrip(undefined), "");
    assert.strictEqual(odataCatalog.purposeLength("\u0085abc\u0085"), 3);
});

QUnit.test("purposeLength counts what the server counts: the stripped text", function (assert) {
    assert.strictEqual(odataCatalog.purposeLength("Nightly checks and release of requisitions"), 42);
    assert.strictEqual(odataCatalog.purposeLength("  padded \n"), 6);
    assert.strictEqual(odataCatalog.purposeLength(""), 0);
    assert.strictEqual(odataCatalog.purposeLength(undefined), 0);
});

QUnit.test("payloadOf keeps the payload fields of a stored service and nothing else", function (assert) {
    const stored = {
        ...input({ definition: WRITING, metadata_fetched_at: "2026-10-05T07:30:00+00:00" }),
        id: 7, created_at: "2026-10-01T00:00:00+00:00", updated_at: null,
        counts: { entity_sets: 2, operations: 1 }, has_write: true, used_by: []
    };

    const payload = odataCatalog.payloadOf(stored);
    assert.deepEqual(payload, input({ definition: WRITING, metadata_fetched_at: "2026-10-05T07:30:00+00:00" }));
    assert.notStrictEqual(payload.definition, stored.definition, "the definition is a copy");
    assert.notStrictEqual(
        payload.definition.entity_sets[0], stored.definition.entity_sets[0], "all the way down"
    );
});

QUnit.test("identityChange names what changes who the agents act as", function (assert) {
    const stored = input({ user_context: true, destination: "S4_ODATA_USER" });

    assert.deepEqual(
        odataCatalog.identityChange(stored, input({ title: "Other", purpose: "Other", enabled: false })),
        { runsAs: null, destination: null }, "other fields change nobody's identity"
    );
    assert.deepEqual(
        odataCatalog.identityChange(stored, input({ user_context: false })),
        { runsAs: "technical", destination: null }
    );
    assert.deepEqual(
        odataCatalog.identityChange(input({ user_context: false }), input({ user_context: true })),
        { runsAs: "user", destination: null }
    );
    assert.deepEqual(
        odataCatalog.identityChange(stored, input({ destination: "S4_ODATA_TECH" })),
        { runsAs: null, destination: { from: "S4_ODATA_USER", to: "S4_ODATA_TECH" } }
    );
    assert.deepEqual(
        odataCatalog.identityChange(stored, input({ user_context: false, destination: "S4_ODATA_TECH" })),
        { runsAs: "technical", destination: { from: "S4_ODATA_USER", to: "S4_ODATA_TECH" } }
    );
});

QUnit.test("validateDuplicate checks the copy's name and destination like a service's", function (assert) {
    assert.deepEqual(odataCatalog.validateDuplicate({ name: "copy", destination: "S4_ODATA_TECH" }), {});
    assert.deepEqual(odataCatalog.validateDuplicate({ name: "copy" }), {}, "the destination may be left to the source");
    assert.deepEqual(odataCatalog.validateDuplicate({ name: "", destination: "" }), {
        name: "odataErrNameRequired", destination: "odataErrDestinationRequired"
    });
    assert.deepEqual(odataCatalog.validateDuplicate({ name: "Not A Slug", destination: "has space" }), {
        name: "odataErrNameInvalid", destination: "odataErrDestinationInvalid"
    });
});

QUnit.test("serverErrors turns a refusal's detail into one message per field", function (assert) {
    assert.deepEqual(
        odataCatalog.serverErrors(
            "title: String should have at least 1 character; purpose: String should have at most 200 characters"
        ),
        {
            title: "String should have at least 1 character",
            purpose: "String should have at most 200 characters"
        }
    );
    assert.deepEqual(
        odataCatalog.serverErrors("service_path: Value error, service_path must start with '/'"),
        { service_path: "service_path must start with '/'" },
        "pydantic's 'Value error, ' prefix is dropped"
    );
    assert.deepEqual(
        odataCatalog.serverErrors(
            "definition.entity_sets.0.fields.1: Value error, field 'B' is filterable but not selectable; "
            + "a filterable field must also be selectable; name: Field required; and 3 more"
        ),
        {
            "definition.entity_sets.0.fields.1":
                "field 'B' is filterable but not selectable; a filterable field must also be selectable",
            name: "Field required"
        },
        "a '; ' inside a message does not start a new field, and the 'and n more' tail is no field"
    );
});

QUnit.test("serverErrors is empty for a refusal that names no field", function (assert) {
    assert.deepEqual(odataCatalog.serverErrors("Service name 'suppliers' already exists"), {});
    assert.deepEqual(odataCatalog.serverErrors("name cannot be changed; duplicate the service instead"), {});
    assert.deepEqual(odataCatalog.serverErrors("Service 'a' is used by agent(s) 'b', 'c'"), {});
    assert.deepEqual(odataCatalog.serverErrors(""), {});
    assert.deepEqual(odataCatalog.serverErrors(undefined), {});
});

QUnit.test("toggleOperation adds and removes an entity operation without duplicates", function (assert) {
    assert.deepEqual(odataCatalog.toggleOperation(["list"], "get", true), ["list", "get"]);
    assert.deepEqual(odataCatalog.toggleOperation(["list", "get"], "get", true), ["list", "get"], "no duplicate");
    assert.deepEqual(odataCatalog.toggleOperation(["list", "get"], "list", false), ["get"]);
    assert.deepEqual(odataCatalog.toggleOperation(["get"], "list", false), ["get"], "removing what is not there");
    assert.deepEqual(
        odataCatalog.toggleOperation(["delete", "list"], "create", true), ["list", "create", "delete"],
        "the result is in the server's order"
    );
    assert.deepEqual(odataCatalog.toggleOperation(["get", "get", "list"], "update", true), ["list", "get", "update"]);

    const before: ("list" | "get")[] = ["list"];
    odataCatalog.toggleOperation(before, "get", true);
    assert.deepEqual(before, ["list"], "the input is not changed");
});

// --- the entity sets table (U4) ---------------------------------------------

QUnit.test("entitySetRow holds what the table shows of an entity set", function (assert) {
    const row = odataCatalog.entitySetRow(entitySet("A_Item", {
        title: "Item", description: "The item.", operations: ["list", "update"],
        fields: [field("Id", { selectable: true }), field("Text", { writable: true }), field("Hidden")]
    }), 3);
    assert.deepEqual(row, {
        index: 3, name: "A_Item", title: "Item", path: "", description: "The item.", described: true,
        list: true, get: false, create: false, update: true, delete: false, anyOperation: true,
        selectable: 1, total: 3, navigationHint: false, note: "", error: "", label: "Item A_Item"
    });

    const bare = odataCatalog.entitySetRow(entitySet("A_Bare", { description: "  ", operations: [], path: "Bare" }), 0);
    assert.strictEqual(bare.title, "A_Bare", "without a title the name is the title");
    assert.strictEqual(bare.path, "Bare", "a path that is not the name is shown");
    assert.strictEqual(bare.described, false, "a blank description is none");
    assert.strictEqual(bare.anyOperation, false);
    assert.strictEqual(odataCatalog.entitySetRow(entitySet("A", { path: "A" }), 0).path, "", "a path equal to the name is not repeated");
    assert.deepEqual(odataCatalog.entitySetRows(undefined), []);
    assert.deepEqual(odataCatalog.entitySetRows(WRITING).map((r) => r.index), [0, 1]);
});

QUnit.test("the navigation hint is for an entity set in use that has navigations and no Get", function (assert) {
    const navigations = [{ name: "to_Text", target: "A_Text", collection: true, description: "" }];
    const hint = (operations: ("list" | "get" | "create")[], navs = navigations) => (
        odataCatalog.entitySetRow(entitySet("A", { operations, navigations: navs }), 0).navigationHint
    );
    assert.strictEqual(hint(["list"]), true, "List without Get: its navigations cannot be followed");
    assert.strictEqual(hint(["list", "get"]), false);
    assert.strictEqual(hint([]), false, "nothing enabled: agents do not see it at all");
    assert.strictEqual(hint(["list"], []), false, "no navigations, no hint");
});

QUnit.test("operationRefusal mirrors the three per-operation rules of the server", function (assert) {
    const noKey = entitySet("A", { keys: [] });
    const noRead = entitySet("A", { fields: [field("Id"), field("Text", { writable: true })] });
    const noWrite = entitySet("A", { fields: [field("Id", { selectable: true })] });
    const full = entitySet("A");

    assert.strictEqual(odataCatalog.operationRefusal(noKey, "list"), "", "List needs no key");
    assert.strictEqual(odataCatalog.operationRefusal(noKey, "create"), "", "nor does Create");
    ["get", "update", "delete"].forEach((op) => {
        assert.strictEqual(odataCatalog.operationRefusal(noKey, op as "get"), "odataNeedsKey", `${op} needs a key`);
    });
    assert.strictEqual(odataCatalog.operationRefusal(noRead, "list"), "odataNeedsSelectable");
    assert.strictEqual(odataCatalog.operationRefusal(noRead, "get"), "odataNeedsSelectable");
    assert.strictEqual(odataCatalog.operationRefusal(noRead, "update"), "", "a write needs no readable field");
    assert.strictEqual(odataCatalog.operationRefusal(noRead, "delete"), "");
    assert.strictEqual(odataCatalog.operationRefusal(noWrite, "create"), "odataNeedsWritable");
    assert.strictEqual(odataCatalog.operationRefusal(noWrite, "update"), "odataNeedsWritable");
    assert.strictEqual(odataCatalog.operationRefusal(noWrite, "delete"), "", "Delete sends no field");
    odataCatalog.ENTITY_OPS.forEach((op) => {
        assert.strictEqual(odataCatalog.operationRefusal(full, op), "", `${op} is fine with key, readable and writable field`);
    });
});

QUnit.test("newWrites lists only the writes a save newly enables", function (assert) {
    const stored: ODataDefinition = {
        entity_sets: [
            entitySet("A_Item", { title: "Item", operations: ["list", "get", "update"] }),
            entitySet("A_Text", { title: "Text", operations: ["list", "create", "delete"] })
        ],
        operations: []
    };
    const current: ODataDefinition = {
        entity_sets: [
            // Update was there, Delete is new, Get went away.
            entitySet("A_Item", { title: "Item", operations: ["list", "update", "delete"] }),
            // A write switched off is no question.
            entitySet("A_Text", { title: "Text", operations: ["list", "create"] }),
            // Not stored at all: every write of it is new.
            entitySet("A_New", { operations: ["create", "update"] }),
            entitySet("A_ReadOnly", { operations: ["list", "get"] })
        ],
        operations: []
    };
    assert.deepEqual(odataCatalog.newWrites(stored, current), [
        { name: "A_Item", title: "Item", operations: ["delete"] },
        { name: "A_New", title: "A_New", operations: ["create", "update"] }
    ]);
    assert.deepEqual(odataCatalog.newWrites(stored, stored), [], "nothing changed, nothing new");
    assert.deepEqual(odataCatalog.newWrites(undefined, stored).map((w) => w.name), ["A_Item", "A_Text"], "a new service");
    assert.deepEqual(odataCatalog.newWrites(stored, undefined), []);
    assert.deepEqual(
        odataCatalog.newWrites({ entity_sets: [], operations: [] }, {
            entity_sets: [entitySet("constructor", { operations: ["delete"] })], operations: []
        }),
        [{ name: "constructor", title: "constructor", operations: ["delete"] }],
        "a name that an object has anyway is still new"
    );
});

QUnit.test("writers splits the agents by Allow writes", function (assert) {
    const used = (agent: string, allow: boolean) => ({
        agent_id: 1, agent, enabled: true, expose_api: false, api_slug: "", allow_write: allow
    });
    assert.deepEqual(
        odataCatalog.writers([used("a", true), used("b", false), used("c", true)]),
        { allowed: ["a", "c"], others: ["b"] }
    );
    assert.deepEqual(odataCatalog.writers(undefined), { allowed: [], others: [] });
});

QUnit.test("definitionProblems names one problem per entity set, by the server's rules", function (assert) {
    const key = (set: ODataEntitySet) => {
        const problems = odataCatalog.definitionProblems({ entity_sets: [set], operations: [] });
        return problems.length ? `${problems[0].key}(${problems[0].args.join(",")})` : "";
    };
    assert.strictEqual(key(entitySet("A")), "", "a consistent entity set");
    assert.strictEqual(key(entitySet("A", { operations: [], fields: [], keys: [] })), "", "nothing enabled, nothing required");
    assert.strictEqual(key(entitySet("1A")), "odataErrEntityName()");
    assert.strictEqual(key(entitySet("")), "odataErrEntityName()");
    assert.strictEqual(
        key(entitySet("A", { fields: [field("Id", { selectable: true }), field("X", { filterable: true })] })),
        "odataErrFilterNotSelectable(X)", "a filterable field must be selectable"
    );
    assert.strictEqual(
        key(entitySet("A", { fields: [field("Id", { selectable: true }), field("Id")] })), "odataErrDuplicateField(Id)"
    );
    assert.strictEqual(key(entitySet("A", {
        navigations: [
            { name: "to_B", target: "B", collection: true, description: "" },
            { name: "to_B", target: "B", collection: false, description: "" }
        ]
    })), "odataErrDuplicateNavigation(to_B)");
    assert.strictEqual(
        key(entitySet("A", { keys: [{ name: "Id", type: "Edm.String" }, { name: "Id", type: "Edm.String" }] })),
        "odataErrDuplicateKey(Id)"
    );
    assert.strictEqual(
        key(entitySet("A", { keys: [{ name: "Gone", type: "Edm.String" }] })), "odataErrKeyNotField(Gone)",
        "a key must be one of the fields"
    );
    assert.strictEqual(key(entitySet("A", { keys: [] })), "odataNeedsKey()", "Get without a key");
    assert.strictEqual(key(entitySet("A", { keys: [], operations: ["list"] })), "", "List alone needs none");
    assert.strictEqual(key(entitySet("A", { fields: [field("Id")] })), "odataNeedsSelectable()");
    assert.strictEqual(
        key(entitySet("A", { operations: ["create"], fields: [field("Id", { selectable: true })] })), "odataNeedsWritable()"
    );
    assert.strictEqual(
        key(entitySet("A", { operations: ["delete"], fields: [field("Id")] })), "",
        "Delete needs a key and nothing else"
    );

    assert.deepEqual(
        odataCatalog.definitionProblems({
            entity_sets: [entitySet("A"), entitySet("B"), entitySet("A"), entitySet("C", { keys: [] })], operations: []
        }),
        [
            { index: 0, key: "odataErrDuplicateEntitySet", args: ["A"] },
            { index: 2, key: "odataErrDuplicateEntitySet", args: ["A"] },
            { index: 3, key: "odataNeedsKey", args: [] }
        ],
        "a name used twice marks both rows"
    );
    assert.deepEqual(odataCatalog.definitionProblems(undefined), []);
});

QUnit.test("rowErrors puts a refusal on the row it names", function (assert) {
    const names = ["A_Item", "A_Text", "A_Item"];
    const rows = (detail: string) => odataCatalog.rowErrors(odataCatalog.serverErrors(detail), names);

    assert.deepEqual(
        rows("definition.entity_sets.1: Value error, entity set 'A_Text' has 'list' but no selectable field"),
        { 1: "entity set 'A_Text' has 'list' but no selectable field" }, "by its position"
    );
    assert.deepEqual(
        rows("definition.entity_sets.0.fields.4: Value error, field 'B' is filterable but not selectable; "
            + "a filterable field must also be selectable; title: Field required"),
        { 0: "fields.4: field 'B' is filterable but not selectable; a filterable field must also be selectable" },
        "a problem inside an entity set says where; what is no entity set is not a row's"
    );
    assert.deepEqual(
        rows("definition.entity_sets.1.description: String should have at most 600 characters; "
            + "definition.entity_sets.1.title: Value error, title must be one line of text without control characters"),
        {
            1: "description: String should have at most 600 characters "
                + "title: title must be one line of text without control characters"
        },
        "two problems of one row are both shown"
    );
    assert.deepEqual(
        rows("definition: Value error, duplicate entity set 'A_Item'"),
        { 0: "duplicate entity set 'A_Item'", 2: "duplicate entity set 'A_Item'" },
        "a problem of the definition goes to every row of the name it gives"
    );
    assert.deepEqual(
        rows("definition.entity_sets.0: Value error, entity set 'A_Text' has 'get' but no key"),
        { 1: "entity set 'A_Text' has 'get' but no key" },
        "when position and name disagree, the name decides"
    );
    assert.deepEqual(
        rows("definition.entity_sets.0: Value error, entity set 'Other' has 'get' but no key"), {},
        "a name the page does not have marks no row"
    );
    assert.deepEqual(rows("definition.entity_sets.7: Value error, something"), {}, "nor does a position it does not have");
    assert.deepEqual(
        rows("definition.entity_sets.0.<unknown field>: Extra inputs are not permitted"),
        { 0: "<unknown field>: Extra inputs are not permitted" },
        "a key the server does not repeat is still a problem of that row"
    );
    assert.deepEqual(
        odataCatalog.serverErrors("<unknown field>: Extra inputs are not permitted; title: Field required"),
        { "<unknown field>": "Extra inputs are not permitted", title: "Field required" }
    );
    assert.deepEqual(rows("definition.operations.0: Value error, x; definition: Value error, the definition is larger than 2000000 bytes"), {});
    assert.deepEqual(rows("Service not found"), {});
});

QUnit.test("newEntitySetName and emptyEntitySet start an entity set that enables nothing", function (assert) {
    assert.strictEqual(odataCatalog.newEntitySetName([]), "NewEntitySet");
    assert.strictEqual(odataCatalog.newEntitySetName(["NewEntitySet", "NewEntitySet2"]), "NewEntitySet3");
    const added = odataCatalog.emptyEntitySet("NewEntitySet");
    assert.deepEqual(added.operations, [], "nothing is enabled by default");
    assert.deepEqual(odataCatalog.definitionProblems({ entity_sets: [added], operations: [] }), [], "and it can be saved");
});

// --- review round 1 ---------------------------------------------------------

QUnit.test("refusalLines says which entity set each part of a refusal is about", function (assert) {
    const rows = [
        { name: "A_Item", title: "Item" }, { name: "A_Text", title: "A_Text" }, { name: "A_Item", title: "Item again" }
    ];
    const lines = (detail: string) => odataCatalog.refusalLines(odataCatalog.serverErrors(detail), rows);
    assert.deepEqual(
        lines("definition.entity_sets.0.fields.4: Value error, field 'B' is filterable but not selectable; "
            + "a filterable field must also be selectable; title: Field required; "
            + "definition.entity_sets.1: Value error, entity set 'A_Text' has 'get' but no key; "
            + "definition.entity_sets.9: Value error, something; "
            + "definition: Value error, duplicate entity set 'A_Item'"),
        [
            "Item (A_Item): fields.4: field 'B' is filterable but not selectable; a filterable field must also be selectable",
            "title: Field required",
            "A_Text: entity set 'A_Text' has 'get' but no key",
            "definition.entity_sets.9: something",
            "Item (A_Item), Item again (A_Item): duplicate entity set 'A_Item'"
        ],
        "title and technical name where a row is named; the location where none is"
    );
    assert.deepEqual(lines("Service not found"), []);
});

QUnit.test("newWrites counts every write as new when the stored service is switched off", function (assert) {
    assert.deepEqual(
        odataCatalog.newWrites(WRITING, WRITING, true).map((w) => `${w.name}:${w.operations.join(",")}`),
        ["A_PurchaseRequisitionItem:update", "A_PurchaseReqnItemText:create,update"],
        "all writes of the service, in the server's order"
    );
    assert.deepEqual(odataCatalog.newWrites(WRITING, WRITING, false), []);
});

QUnit.test("entitySetRow labels a row by title and technical name", function (assert) {
    assert.strictEqual(odataCatalog.entitySetRow(entitySet("A_Item", { title: "Item" }), 0).label, "Item A_Item");
    assert.strictEqual(odataCatalog.entitySetRow(entitySet("A_Item"), 0).label, "A_Item", "not twice when there is no title");
});

// --- review round 2 ---------------------------------------------------------

QUnit.test("an enabled operation is a write unless it is marked as reading AND sent with GET", function (assert) {
    const operation = WRITING.operations[0];
    const withOperation = (over: Partial<typeof operation>): ODataDefinition => ({
        entity_sets: READ_ONLY.entity_sets, operations: [{ ...operation, ...over }]
    });
    const isWrite = (over: Partial<typeof operation>) => odataCatalog.operationIsWrite({ ...operation, ...over });

    assert.strictEqual(isWrite({ changes_data: true, http_method: "POST" }), true);
    assert.strictEqual(isWrite({ changes_data: true, http_method: "GET" }), true, "marked as changing data");
    assert.strictEqual(isWrite({ changes_data: false, http_method: "POST" }), true, "a POST is a write whatever the flag says");
    assert.strictEqual(isWrite({ changes_data: false, http_method: "GET" }), false, "the one read");
    assert.strictEqual(
        isWrite({ changes_data: undefined as unknown as boolean, http_method: "GET" }), true,
        "a missing flag does not read as 'only reads'"
    );
    assert.strictEqual(
        isWrite({ changes_data: false, http_method: undefined as unknown as "GET" }), true,
        "nor does a missing method read as GET: an operation without one is a write"
    );

    // The tag, the summary, the strip and the question all follow it.
    assert.strictEqual(odataCatalog.hasWrite(withOperation({ changes_data: false, http_method: "POST" })), true);
    assert.strictEqual(odataCatalog.hasWrite(withOperation({ changes_data: false, http_method: "GET" })), false);
    assert.strictEqual(
        odataCatalog.hasWrite(withOperation({ changes_data: false, http_method: "POST", enabled: false })), false, "disabled"
    );
    assert.deepEqual(
        odataCatalog.writeSummary(withOperation({ changes_data: false, http_method: "POST" })), ["Release item"]
    );
    assert.deepEqual(
        odataCatalog.writeOperations(withOperation({ changes_data: false, http_method: "POST" })).map((o) => o.name),
        ["ReleaseItem"]
    );
    assert.deepEqual(odataCatalog.writeOperations(withOperation({ enabled: false })), []);
    assert.deepEqual(odataCatalog.writeOperations(undefined), []);
});

QUnit.test("pendingWrites adds the enabled write operations a save newly opens", function (assert) {
    const operation = WRITING.operations[0];
    const off: ODataDefinition = { entity_sets: WRITING.entity_sets, operations: [{ ...operation, enabled: false }] };
    const none: ODataDefinition = { entity_sets: WRITING.entity_sets, operations: [] };
    const release = [{ name: "ReleaseItem", title: "Release item" }];

    assert.deepEqual(odataCatalog.pendingWrites(WRITING, WRITING), odataCatalog.noPending(), "stored and enabled: not new");
    assert.deepEqual(odataCatalog.pendingWrites(off, WRITING).operations, release, "stored but not enabled");
    assert.deepEqual(odataCatalog.pendingWrites(none, WRITING).operations, release, "not stored (matched by name)");
    assert.deepEqual(odataCatalog.pendingWrites(undefined, WRITING).operations, release, "a new service");
    assert.deepEqual(
        odataCatalog.pendingWrites(WRITING, WRITING, true),
        {
            entitySets: [
                { name: "A_PurchaseRequisitionItem", title: "Requisition item", operations: ["update"] },
                { name: "A_PurchaseReqnItemText", title: "Item text", operations: ["create", "update"] }
            ],
            operations: release,
            fields: []
        },
        "a service that is switched on opens everything it has"
    );
    assert.deepEqual(odataCatalog.pendingWrites(WRITING, off).operations, [], "an operation that is off is no write");
    assert.deepEqual(
        odataCatalog.pendingWrites(
            { entity_sets: [], operations: [{ ...operation, changes_data: false, http_method: "GET" }] },
            { entity_sets: [], operations: [{ ...operation, title: "", changes_data: false, http_method: "POST" }] }
        ).operations,
        [{ name: "ReleaseItem", title: "ReleaseItem" }],
        "stored as a read, now a write: new, and named by its name without a title"
    );
});

QUnit.test("pendingCount and pendingMinus say what one tick changed", function (assert) {
    const before = odataCatalog.pendingWrites(READ_ONLY, WRITING);
    const item = { name: "A_PurchaseRequisitionItem", title: "Requisition item" };
    const after = {
        entitySets: [{ ...item, operations: ["update", "delete"] as const }].map((w) => ({ ...w, operations: [...w.operations] })),
        operations: [] as { name: string; title: string }[],
        fields: [{ ...item, fields: ["Text", "Other"] }]
    };
    assert.strictEqual(
        odataCatalog.pendingCount(before), 6, "update + create, update + one operation + one writable field on each"
    );
    assert.strictEqual(odataCatalog.pendingCount(odataCatalog.noPending()), 0);
    assert.deepEqual(
        odataCatalog.pendingMinus(after, before),
        { entitySets: [{ ...item, operations: ["delete"] }], operations: [], fields: [{ ...item, fields: ["Other"] }] },
        "what is new in it"
    );
    assert.deepEqual(
        odataCatalog.pendingMinus(before, after),
        {
            entitySets: [{ name: "A_PurchaseReqnItemText", title: "Item text", operations: ["create", "update"] }],
            operations: [{ name: "ReleaseItem", title: "Release item" }],
            fields: [{ name: "A_PurchaseReqnItemText", title: "Item text", fields: ["Text"] }]
        },
        "what is gone from it"
    );
    assert.deepEqual(odataCatalog.pendingMinus(before, before), odataCatalog.noPending());
});

QUnit.test("serverRefusal keeps what a refusal says before its first field and its 'and n more' tail", function (assert) {
    assert.deepEqual(
        odataCatalog.serverRefusal("Not accepted; definition.entity_sets.0: Value error, something; title: Field required; and 12 more"),
        {
            lead: "Not accepted",
            byLoc: { "definition.entity_sets.0": "something", title: "Field required" },
            more: "and 12 more"
        }
    );
    assert.deepEqual(
        odataCatalog.serverRefusal("title: Field required"), { lead: "", byLoc: { title: "Field required" }, more: "" }
    );
    assert.deepEqual(
        odataCatalog.serverRefusal("Service 'a' is used by agent(s) 'b'; 'c'"),
        { lead: "Service 'a' is used by agent(s) 'b'; 'c'", byLoc: {}, more: "" }
    );
    assert.deepEqual(odataCatalog.serverRefusal(undefined), { lead: "", byLoc: {}, more: "" });
    assert.deepEqual(
        odataCatalog.serverErrors("x; title: Field required; and 3 more"), { title: "Field required" }, "serverErrors is its byLoc"
    );
});

// --- operations (U6) -----------------------------------------------------------

QUnit.test("operationRow: what the operations table shows of an operation", function (assert) {
    const definition = {
        entity_sets: [{ ...odataCatalog.emptyEntitySet("A_Item"), title: "Item" }],
        operations: [
            {
                name: "Release", qualified_name: "", title: "Release item", kind: "function_import" as const,
                http_method: "POST" as const, bound_to: "A_Item",
                parameters: [{ name: "Item", type: "Edm.String", required: true }, { name: "Note", type: "Edm.String", required: false }],
                description: " Releases it. ", enabled: true, changes_data: false
            },
            {
                name: "Probe", qualified_name: "", title: "", kind: "function_import" as const, http_method: "GET" as const,
                bound_to: "A_Gone", parameters: [], description: "", enabled: false, changes_data: false
            }
        ]
    };
    const uncallable = odataCatalog.uncallableByName([
        { name: "Release", reason: "key_not_declared" }, { name: "Probe", reason: "bound_set_missing" }
    ]);
    const rows = odataCatalog.operationRows(definition, uncallable);
    assert.deepEqual(rows[0], {
        index: 0, name: "Release", title: "Release item", technical: "Release · POST", description: "Releases it.",
        boundTo: "Item", parameters: "Item, [Note]", enabled: true,
        write: true, post: true, uncallable: "odataUncallableKeyNotDeclared", note: "", label: "Release item (Release)"
    }, "a POST is a write whatever its flag says; the bound entity set by its title; an optional parameter in brackets");
    assert.deepEqual(rows[1], {
        index: 1, name: "Probe", title: "Probe", technical: "Probe · GET", description: "",
        boundTo: "A_Gone", parameters: "", enabled: false,
        write: false, post: false, uncallable: "", note: "", label: "Probe"
    }, "a GET marked as only reading is a read; an entity set the definition lacks by its name; no reason while it is off");
    assert.deepEqual(odataCatalog.operationRows(undefined), [], "no definition, no rows");
});

QUnit.test("uncallableKey: every reason of the server has its own words, an unknown one the fallback", function (assert) {
    // `CALL_REFUSALS` in agents/odata/client.py that `uncallable_operations` can answer, and `invalid_definition`.
    const keys = [
        "calls_not_available", "bound_set_missing", "bound_set_without_key", "key_not_declared", "bound_key_type",
        "parameter_type", "invalid_definition"
    ].map((reason) => odataCatalog.uncallableKey(reason));
    assert.deepEqual(keys, [
        "odataUncallableKind", "odataUncallableBoundMissing", "odataUncallableNoKey", "odataUncallableKeyNotDeclared",
        "odataUncallableKeyType", "odataUncallableParameterType", "odataUncallableInvalid"
    ]);
    assert.strictEqual(odataCatalog.uncallableKey("something_new"), "odataUncallableOther", "a reason this build does not know");
    assert.strictEqual(odataCatalog.uncallableKey("constructor"), "odataUncallableOther", "a reason named like an object member");
    assert.strictEqual(odataCatalog.uncallableKey(""), "", "no reason, no text");
    assert.deepEqual(odataCatalog.uncallableByName(undefined), {}, "a server that sends no list");
    assert.deepEqual(odataCatalog.uncallableByName("x" as never), {}, "or something that is no list");
});

QUnit.test("payloadOf: `returns` of an operation is kept, the read-only list is left out", function (assert) {
    const service = {
        ...odataCatalog.emptyService(),
        definition: {
            entity_sets: [odataCatalog.emptyEntitySet("A_Item")],
            operations: [{
                name: "Next", qualified_name: "", title: "", kind: "function_import" as const, http_method: "GET" as const,
                bound_to: null, parameters: [], description: "", enabled: true, changes_data: false,
                returns: { entity_set: "A_Item", collection: true }
            }]
        },
        uncallable_operations: [{ name: "Next", reason: "parameter_type" }], used_by: [], id: 7
    };
    const payload = odataCatalog.payloadOf(service) as unknown as Record<string, unknown>;
    assert.deepEqual(
        (payload.definition as typeof service.definition).operations[0].returns, { entity_set: "A_Item", collection: true }
    );
    assert.strictEqual("uncallable_operations" in payload, false, "never sent back");
    assert.strictEqual("used_by" in payload, false);
});

QUnit.test("jobAgents: the agents with a run endpoint", function (assert) {
    const used = (agent: string, exposed: boolean) => ({
        agent_id: 1, agent, enabled: true, expose_api: exposed, api_slug: exposed ? agent : "", allow_write: false
    });
    assert.deepEqual(odataCatalog.jobAgents([used("chat", false), used("nightly", true)]), ["nightly"]);
    assert.deepEqual(odataCatalog.jobAgents(undefined), []);
    assert.deepEqual(
        odataCatalog.jobAgents([{ ...used("off", true), enabled: false }, used("nightly", true)]), ["nightly"],
        "an agent that is switched off is not started by the scheduler"
    );
    assert.deepEqual(
        odataCatalog.jobAgents([{ ...used("no-slug", true), api_slug: "" }, { ...used("blank", true), api_slug: "  " }]), [],
        "expose_api without a slug is no run endpoint"
    );
});

// --- U6 fix round 1 ------------------------------------------------------------

/** A definition with the one operation `over` describes. */
function withOperation(over: Record<string, unknown> | null): ODataDefinition {
    const operation = {
        name: "GetStrategy", qualified_name: "", title: "Release strategy", kind: "function_import",
        http_method: "GET", bound_to: null, parameters: [], description: "", enabled: true, changes_data: true, ...over
    };
    return { entity_sets: [], operations: over === null ? [] : [operation] } as unknown as ODataDefinition;
}

QUnit.test("pendingReads: an operation that a save newly marks as only reading", function (assert) {
    const entry = [{ name: "GetStrategy", title: "Release strategy" }];
    const read = withOperation({ changes_data: false });

    assert.deepEqual(odataCatalog.pendingReads(withOperation({}), read), entry, "GET, Changes data true -> false");
    const noFlag = withOperation({});
    delete (noFlag.operations[0] as unknown as Record<string, unknown>).changes_data;
    assert.deepEqual(odataCatalog.pendingReads(noFlag, read), entry, "a missing flag is a write: -> false is an entry");
    assert.deepEqual(odataCatalog.pendingReads(noFlag, noFlag), [], "and a missing flag is never a read");
    assert.deepEqual(
        odataCatalog.pendingReads(withOperation({ enabled: false, changes_data: false }), read), entry,
        "stored switched off and marked as reading, then enabled"
    );
    assert.deepEqual(
        odataCatalog.pendingReads(withOperation({}), withOperation({ enabled: false, changes_data: false })), [],
        "marked as reading but not enabled: nothing an agent can call yet"
    );
    assert.deepEqual(
        odataCatalog.pendingReads(withOperation({ http_method: "POST" }), withOperation({ http_method: "POST", changes_data: false })),
        [], "a POST never: it stays a write whatever its flag says"
    );
    assert.deepEqual(odataCatalog.pendingReads(read, read), [], "a stored read stays a read: no entry");
    assert.deepEqual(odataCatalog.pendingReads(read, withOperation({})), [], "a read that becomes a write is no entry here");
    assert.deepEqual(odataCatalog.pendingReads(withOperation(null), read), entry, "not stored at all (matched by name)");
    assert.deepEqual(odataCatalog.pendingReads(undefined, read), entry, "a new service, or one saved as new");
    assert.deepEqual(
        odataCatalog.pendingReads(undefined, withOperation({ changes_data: false, title: " " })),
        [{ name: "GetStrategy", title: "GetStrategy" }], "named by its name without a title"
    );
    assert.deepEqual(odataCatalog.pendingReads(read, undefined), [], "no definition, no entry");
    // The two categories never hold the same operation.
    assert.deepEqual(odataCatalog.pendingWrites(withOperation({}), read).operations, [], "and it is no pending write");
});

QUnit.test("operationsMinus: what one click added to or took from the pending reads", function (assert) {
    const a = { name: "A", title: "a" };
    const b = { name: "B", title: "b" };
    assert.deepEqual(odataCatalog.operationsMinus([a, b], [a]), [b]);
    assert.deepEqual(odataCatalog.operationsMinus([a], [a, b]), []);
    assert.deepEqual(odataCatalog.operationsMinus([], [a]), []);
});

QUnit.test("operationRow: a reason is about the stored operation and goes when the row was changed on the page", function (assert) {
    const stored = withOperation({});
    const uncallable = odataCatalog.uncallableByName([{ name: "GetStrategy", reason: "parameter_type" }]);
    const reasonOf = (current: ODataDefinition, saved?: ODataDefinition) => (
        odataCatalog.operationRows(current, uncallable, saved)[0].uncallable
    );
    assert.strictEqual(reasonOf(stored, stored), "odataUncallableParameterType", "as stored: the reason");
    assert.strictEqual(reasonOf(withOperation({}), stored), "odataUncallableParameterType", "equal by content, not by identity");
    assert.strictEqual(reasonOf(withOperation({ description: "Changed" }), stored), "", "changed on the page: no reason");
    assert.strictEqual(reasonOf(withOperation({ changes_data: false }), stored), "", "its flag changed: no reason");
    assert.strictEqual(reasonOf(stored, withOperation(null)), "", "not stored: the reason is about nothing");
    assert.strictEqual(reasonOf(stored), "odataUncallableParameterType", "without the stored definition, as before");
});

QUnit.test("operationTextProblems: the title and description the server would refuse", function (assert) {
    assert.deepEqual(odataCatalog.operationTextProblems("Release item", "Releases one item."), {});
    assert.deepEqual(odataCatalog.operationTextProblems("", ""), {}, "both may be empty");
    assert.deepEqual(odataCatalog.operationTextProblems(undefined, null), {});
    assert.deepEqual(odataCatalog.operationTextProblems("x".repeat(120), "y".repeat(600)), {}, "at the caps");
    assert.deepEqual(odataCatalog.operationTextProblems("x".repeat(121), ""), { title: "odataErrTitleTooLong" });
    assert.deepEqual(odataCatalog.operationTextProblems("a\tb", ""), { title: "odataErrTitleOneLine" }, "a tab");
    assert.deepEqual(odataCatalog.operationTextProblems("a\u2028b", ""), { title: "odataErrTitleOneLine" }, "a line separator");
    assert.deepEqual(
        odataCatalog.operationTextProblems("", "one\ntwo"), {}, "a description may have several lines, as the server takes it"
    );
    assert.deepEqual(
        odataCatalog.operationTextProblems("", "y".repeat(601)), { description: "odataErrEntityDescriptionTooLong" }
    );
    assert.strictEqual(odataCatalog.MAX_OPERATION_DESCRIPTION, 600);
});

