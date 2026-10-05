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

QUnit.test("an operation counts as a write only when it is enabled and changes data", function (assert) {
    const operation = WRITING.operations[0];
    const withOperation = (over: Partial<typeof operation>): ODataDefinition => ({
        entity_sets: READ_ONLY.entity_sets, operations: [{ ...operation, ...over }]
    });

    assert.strictEqual(odataCatalog.hasWrite(withOperation({})), true);
    assert.strictEqual(odataCatalog.hasWrite(withOperation({ enabled: false })), false, "disabled");
    assert.strictEqual(odataCatalog.hasWrite(withOperation({ changes_data: false })), false, "read-only call");
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
