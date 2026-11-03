import odataCatalog from "com/agent/admin/model/odataCatalog";
import type { ODataDefinition, ODataOperation, ODataServiceInput } from "com/agent/admin/service/types";

/**
 * The service kind in the catalogue's rules (addendum (k), part 3): a
 * `generic` service is the ZAGENT_DATA registration of an SAP system, and
 * its function imports sent with GET are reads whatever their Changes data
 * flag says. The server's rule (`operation_is_write` in
 * agents/odata/models.py) takes the SERVICE kind and the OPERATION kind, and
 * relaxes only on exactly `generic`, `function_import` and `GET`; every
 * default is the rule of `odata`.
 */
QUnit.module("odataCatalog: the service kind");

/** What ZAGENT_DATA offers: a function module read with GET. SAP's preview
 *  suggests `changes_data: true` for it (nothing is known), as an import
 *  stores it. */
function months(over: Partial<ODataOperation> = {}): ODataOperation {
    return {
        name: "MONTH_NAMES_GET", qualified_name: "", title: "Month names", kind: "function_import", http_method: "GET",
        bound_to: null, parameters: [{ name: "LANGUAGE", type: "Edm.String", required: true }],
        description: "", enabled: true, changes_data: true, ...over
    };
}

function definition(...operations: ODataOperation[]): ODataDefinition {
    return { entity_sets: [], operations };
}

function service(over: Partial<ODataServiceInput> = {}): ODataServiceInput {
    return {
        name: "agent-data", kind: "generic", title: "Agent data", purpose: "Allow-listed tables and modules of A4H",
        not_for: "", destination: "AGENT_DATA_A4H", user_context: false, odata_version: "v2",
        service_path: "/sap/opu/odata/sap/ZAGENT_DATA_SRV", enabled: true,
        definition: definition(months()), metadata_fetched_at: null, ...over
    };
}

const MONTHS = [{ name: "MONTH_NAMES_GET", title: "Month names" }];

QUnit.test("operationIsWrite: a GET function import of a generic service is a read, whatever its flag says", function (assert) {
    const isWrite = (over: Partial<ODataOperation>, kind?: unknown) => odataCatalog.operationIsWrite(months(over), kind);

    assert.strictEqual(isWrite({}, "generic"), false, "changes_data true");
    assert.strictEqual(isWrite({ changes_data: undefined as unknown as boolean }, "generic"), false, "no flag at all");
    assert.strictEqual(isWrite({ changes_data: false }, "generic"), false, "marked as reading");
    assert.strictEqual(isWrite({ http_method: "POST" }, "generic"), true, "sent with POST: a write on every kind");
    assert.strictEqual(isWrite({ http_method: "POST", changes_data: false }, "generic"), true, "also marked as reading");
    assert.strictEqual(isWrite({ kind: "function" }, "generic"), true, "not a function import: the rule of odata");
    assert.strictEqual(isWrite({ kind: "function", changes_data: false }, "generic"), false, "which still has its one read");
    assert.strictEqual(isWrite({ kind: undefined as unknown as "function_import" }, "generic"), true, "an operation kind not given");
    assert.strictEqual(isWrite({ http_method: undefined as unknown as "GET" }, "generic"), true, "a method not given");

    assert.strictEqual(isWrite({}, "odata"), true, "the same operation on an odata service is a write");
    assert.strictEqual(isWrite({}), true, "no kind given: odata");
    assert.strictEqual(isWrite({}, undefined), true);
    ["Generic", " generic", "generic ", "GENERIC", "other", null, 1, true].forEach((kind) => {
        assert.strictEqual(isWrite({}, kind), true, `only exactly "generic" relaxes: ${JSON.stringify(kind)}`);
    });
    assert.strictEqual(isWrite({ changes_data: false }, "odata"), false, "the read of odata is unchanged");
});

QUnit.test("the Write tag, the summary and the operation list follow the service kind", function (assert) {
    const generic = definition(months(), months({ name: "SET_STATUS", title: "Set status", http_method: "POST" }));

    assert.strictEqual(odataCatalog.hasWrite(definition(months()), "generic"), false, "only a GET import: no write");
    assert.strictEqual(odataCatalog.hasWrite(definition(months())), true, "odata by default");
    assert.strictEqual(odataCatalog.hasWrite(definition(months()), "odata"), true);
    assert.strictEqual(odataCatalog.hasWrite(generic, "generic"), true, "a POST import is still one");
    assert.deepEqual(odataCatalog.writeSummary(generic, "generic"), ["Set status"]);
    assert.deepEqual(odataCatalog.writeSummary(generic), ["Month names", "Set status"]);
    assert.deepEqual(odataCatalog.writeOperations(generic, "generic").map((o) => o.name), ["SET_STATUS"]);
    assert.deepEqual(odataCatalog.writeOperations(generic, "odata").map((o) => o.name), ["MONTH_NAMES_GET", "SET_STATUS"]);
});

QUnit.test("pending writes leave out a GET import of a generic service; it is a pending read instead", function (assert) {
    const off = definition(months({ enabled: false }));
    const on = definition(months());
    const generic = { stored: "generic", current: "generic" };

    assert.deepEqual(odataCatalog.pendingWrites(off, on, false, generic).operations, [], "enabling it opens no write");
    assert.deepEqual(odataCatalog.pendingReads(off, on, generic), MONTHS,
        "but every agent of the service can call it: said and asked about as a read");
    assert.deepEqual(odataCatalog.pendingWrites(undefined, on, false, { current: "generic" }).operations, [], "a new service");
    assert.deepEqual(odataCatalog.pendingReads(undefined, on, { current: "generic" }), MONTHS);
    assert.deepEqual(odataCatalog.pendingWrites(on, on, true, generic).operations, [], "switching the service on");
    assert.deepEqual(odataCatalog.pendingReads(on, on, generic), [], "stored as a read: nothing new");

    assert.deepEqual(odataCatalog.pendingWrites(off, on).operations, MONTHS, "without kinds: the rule of odata");
    assert.deepEqual(odataCatalog.pendingReads(off, on), [], "and no read");
});

QUnit.test("changing the kind of a service is a widening or a new write, as the rule says", function (assert) {
    const on = definition(months());

    assert.deepEqual(odataCatalog.pendingReads(on, on, { stored: "odata", current: "generic" }), MONTHS,
        "odata -> generic: the import becomes callable without Allow writes and is no longer recorded");
    assert.deepEqual(odataCatalog.pendingWrites(on, on, false, { stored: "odata", current: "generic" }).operations, []);
    assert.deepEqual(odataCatalog.pendingWrites(on, on, false, { stored: "generic", current: "odata" }).operations, MONTHS,
        "generic -> odata: it is a write again, for agents with Allow writes only");
    assert.deepEqual(odataCatalog.pendingReads(on, on, { stored: "generic", current: "odata" }), []);
});

QUnit.test("an operation row of a generic service: no write, and the Changes data box is shown, not offered", function (assert) {
    const row = (over: Partial<ODataOperation>, kind?: unknown) => {
        const current = definition(months(over));
        return odataCatalog.operationRows(current, {}, current, kind)[0];
    };

    assert.deepEqual([row({}, "generic").write, row({}, "generic").readByKind, row({}, "generic").post], [false, true, false],
        "a GET import: a read by its kind");
    assert.deepEqual([row({}).write, row({}).readByKind], [true, false], "the same row of an odata service");
    assert.deepEqual([row({}, "odata").write, row({}, "odata").readByKind], [true, false]);
    assert.deepEqual([row({ http_method: "POST" }, "generic").write, row({ http_method: "POST" }, "generic").readByKind,
        row({ http_method: "POST" }, "generic").post], [true, false, true], "a POST import stays a write");
    assert.deepEqual([row({ kind: "function" }, "generic").write, row({ kind: "function" }, "generic").readByKind],
        [true, false], "not a function import");
    assert.strictEqual(odataCatalog.operationRow(months(), 0, definition(months()), {}, undefined, "generic").write, false,
        "operationRow takes the kind as well");
});

QUnit.test("kind is part of the payload: always sent, odata when a service does not say", function (assert) {
    assert.strictEqual(odataCatalog.emptyService().kind, "odata", "a new service starts as odata");
    assert.strictEqual(odataCatalog.payloadOf(service()).kind, "generic");
    assert.deepEqual(Object.keys(odataCatalog.payloadOf(service())).slice(0, 3), ["name", "kind", "title"],
        "in the order of ODataServicePayload");

    const { kind, ...withoutKind } = service();
    void kind;
    const payload = odataCatalog.payloadOf(withoutKind as ODataServiceInput) as unknown as Record<string, unknown>;
    assert.strictEqual(payload.kind, "odata", "absent (an older answer): sent as odata, the server's default");
    assert.strictEqual(
        (odataCatalog.payloadOf({ ...service(), kind: "other" } as unknown as ODataServiceInput) as unknown as Record<string, unknown>).kind,
        "other", "a value that is there is sent as it is, for the server to refuse"
    );
});

QUnit.test("validate: a kind the server takes, and a generic service is OData V2", function (assert) {
    assert.deepEqual(odataCatalog.validate(service()), {}, "a generic V2 service");
    assert.deepEqual(odataCatalog.validate(service({ kind: "odata" })), {});
    assert.deepEqual(odataCatalog.validate(service({ kind: "odata", odata_version: "v4" })), {});
    const { kind, ...withoutKind } = service({ odata_version: "v4" });
    void kind;
    assert.deepEqual(odataCatalog.validate(withoutKind as ODataServiceInput), {}, "left out: the server's default, odata");
    assert.deepEqual(odataCatalog.validate(service({ odata_version: "v4" })), { kind: "odataErrKindGenericV2" });
    ["other", "Generic", "", null, 1].forEach((value) => {
        assert.deepEqual(
            odataCatalog.validate(service({ kind: value as unknown as "generic" })), { kind: "odataErrKind" }, JSON.stringify(value)
        );
    });
});
