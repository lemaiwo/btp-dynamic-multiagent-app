import AdminService, { AdminError } from "com/agent/admin/service/AdminService";
import type { InaQueryInput, InaUsedBy } from "com/agent/admin/service/inaTypes";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";
import FakeIna from "com/agent/admin/test/integration/FakeIna";

// The calls of the "Analytical queries" pages (InA queries) and the links an
// admin draws in the data catalogue. Apart from AdminService.qunit.ts, which
// is past the size a file may have.

QUnit.module("AdminService: InA queries and catalogue links", {
    beforeEach: function (this: { originalFetch: typeof fetch }) {
        this.originalFetch = window.fetch;
    },
    afterEach: function (this: { originalFetch: typeof fetch }) {
        window.fetch = this.originalFetch;
    }
});

/** Replaces window.fetch: every call answers `status` with `body` (none for
 *  a 204) and `headers`; records `METHOD url` and the body sent. */
function answer(status: number, body: unknown, calls: string[][], headers: Record<string, string> = {}): void {
    window.fetch = ((url: string, init?: RequestInit) => {
        calls.push([`${init?.method ?? "GET"} ${url}`, (init?.body as string) ?? ""]);
        return Promise.resolve(new Response(status === 204 ? null : JSON.stringify(body), {
            status, headers: { "Content-Type": "application/json", ...headers }
        }));
    }) as unknown as typeof fetch;
}

async function refusal(call: () => Promise<unknown>): Promise<AdminError> {
    try {
        await call();
    } catch (e) {
        return e as AdminError;
    }
    throw new Error("should have been refused");
}

const INA_INPUT: InaQueryInput = {
    name: "flight-revenue", title: "Flight revenue", purpose: "Revenue per airline and month", not_for: "",
    destination: "S4_ODATA_USER", user_context: true, query: "ZFLIGHT_REV_Q001", package: "", enabled: true
};

QUnit.test("InA query calls use the documented paths and methods", async function (assert) {
    const calls: string[][] = [];
    answer(200, {}, calls);
    const service = new AdminService();

    await service.listInaQueries();
    await service.getInaQuery("flight-revenue");
    await service.createInaQuery(INA_INPUT);
    await service.updateInaQuery("flight-revenue", { ...INA_INPUT, expected_updated_at: "2026-11-03T10:00:00+00:00" });
    await service.describeInaQuery("flight-revenue");
    await service.testInaQuery("flight-revenue");
    answer(204, null, calls);
    await service.deleteInaQuery("flight-revenue");

    assert.deepEqual(calls.map((c) => c[0]), [
        "GET backend/ina/queries",
        "GET backend/ina/queries/flight-revenue",
        "POST backend/ina/queries",
        "PUT backend/ina/queries/flight-revenue",
        "POST backend/ina/queries/flight-revenue/describe",
        "POST backend/ina/queries/flight-revenue/test",
        "DELETE backend/ina/queries/flight-revenue"
    ]);
    assert.deepEqual(JSON.parse(calls[2][1]), INA_INPUT, "create sends the payload as is");
    assert.deepEqual(JSON.parse(calls[3][1]), { ...INA_INPUT, expected_updated_at: "2026-11-03T10:00:00+00:00" },
        "update sends the payload and the stamp");
    assert.deepEqual(JSON.parse(calls[4][1]), {}, "describe sends an empty object");
    assert.deepEqual(JSON.parse(calls[5][1]), {}, "the test sends an empty object");
});

QUnit.test("a query name is url-encoded", async function (assert) {
    const calls: string[][] = [];
    answer(200, {}, calls);
    const service = new AdminService();

    await service.getInaQuery("a/b c");
    await service.updateInaQuery("a/b c", INA_INPUT);
    await service.describeInaQuery("a/b c");
    await service.testInaQuery("a/b c");
    answer(204, null, calls);
    await service.deleteInaQuery("a/b c");

    assert.deepEqual(calls.map((c) => c[0].split(" ")[1]), [
        "backend/ina/queries/a%2Fb%20c",
        "backend/ina/queries/a%2Fb%20c",
        "backend/ina/queries/a%2Fb%20c/describe",
        "backend/ina/queries/a%2Fb%20c/test",
        "backend/ina/queries/a%2Fb%20c"
    ]);
});

QUnit.test("a query delete reads the reload outcome from the two X-Ina headers", async function (assert) {
    answer(204, null, [], { "X-Ina-Reloaded": "false", "X-Ina-Reload-Failed": "true" });
    assert.deepEqual(await new AdminService().deleteInaQuery("flight-revenue"), { reloaded: false, reload_failed: true },
        "deleted, not live yet");
    answer(204, null, [], { "X-Ina-Reloaded": "true", "X-Ina-Reload-Failed": "false" });
    assert.deepEqual(await new AdminService().deleteInaQuery("flight-revenue"), { reloaded: true, reload_failed: false });
    answer(204, null, [], { "X-OData-Reloaded": "true", "X-OData-Reload-Failed": "true" });
    assert.deepEqual(await new AdminService().deleteInaQuery("flight-revenue"), { reloaded: false, reload_failed: false },
        "the OData headers are not this route's");
    answer(204, null, []);
    assert.deepEqual(await new AdminService().deleteInaQuery("flight-revenue"), { reloaded: false, reload_failed: false },
        "an answer without the headers reports no failure");
});

QUnit.test("a refused describe or test carries the code of its X-Ina-Error header", async function (assert) {
    answer(502, { detail: "SAP does not know the InA query of this registration" }, [], { "X-Ina-Error": "query_unknown" });
    const described = await refusal(() => new AdminService().describeInaQuery("flight-revenue"));
    assert.ok(described instanceof AdminError);
    assert.strictEqual(described.status, 502);
    assert.strictEqual(described.code, "query_unknown", "the code of the header");
    assert.strictEqual(described.detail, "SAP does not know the InA query of this registration");

    answer(424, { detail: "a signed-in user is needed" }, [], { "X-Ina-Error": "user_token_required" });
    assert.strictEqual((await refusal(() => new AdminService().testInaQuery("flight-revenue"))).code, "user_token_required");

    answer(409, { detail: "Query 'flight-revenue' is used by agent(s) 'gmail-agent'" }, []);
    const inUse = await refusal(() => new AdminService().deleteInaQuery("flight-revenue"));
    assert.strictEqual(inUse.status, 409);
    assert.strictEqual(inUse.code, "", "no header, no code");
});

QUnit.test("catalogue link calls use the documented paths and methods", async function (assert) {
    const calls: string[][] = [];
    answer(201, { id: 9, from: 310, to: 210, kind: "aggregates", label: "", curated: true }, calls);
    const edge = await new AdminService().createDataEdge({ from_id: 310, to_id: 210, kind: "aggregates" });
    answer(204, null, calls);
    await new AdminService().deleteDataEdge(9);
    await new AdminService().deleteDataEdge("9/x");

    assert.deepEqual(calls.map((c) => c[0]), [
        "POST backend/data-catalog/edges",
        "DELETE backend/data-catalog/edges/9",
        "DELETE backend/data-catalog/edges/9%2Fx"
    ]);
    assert.deepEqual(JSON.parse(calls[0][1]), { from_id: 310, to_id: 210, kind: "aggregates" }, "the body as given");
    assert.strictEqual(edge.id, 9, "the answer is the stored edge");
    assert.strictEqual(edge.curated, true);
});

// The fake backend answers the same routes, so the journeys that use them
// run the real service against it.
QUnit.module("AdminService against the fake InA queries", {
    beforeEach: function (this: { backend: FakeBackend }) {
        this.backend = new FakeBackend();
        this.backend.reset();
        this.backend.install();
    },
    afterEach: function (this: { backend: FakeBackend }) {
        this.backend.restore();
    }
});

QUnit.test("the list is the two seeded queries, without their definition", async function (assert) {
    const list = await new AdminService().listInaQueries();
    assert.deepEqual(list.map((q) => q.name), ["cost-centre-actuals", "flight-revenue"], "by name");
    const flight = list.filter((q) => q.name === "flight-revenue")[0];
    assert.strictEqual(flight.described, true);
    assert.strictEqual(flight.user_context, true);
    assert.deepEqual((flight.used_by as InaUsedBy[]).map((u) => u.agent), ["gmail-agent"]);
    assert.notOk("definition" in flight, "the list carries no definition");
    const cost = list.filter((q) => q.name === "cost-centre-actuals")[0];
    assert.strictEqual(cost.described, false);
    assert.strictEqual(cost.enabled, false);
    assert.strictEqual(cost.metadata_fetched_at, null);
});

QUnit.test("get returns the definition; an unknown name is a 404", async function (assert) {
    const flight = await new AdminService().getInaQuery("flight-revenue");
    assert.deepEqual(flight.definition?.measures.map((m) => m.name), ["ZREVENUE"]);
    assert.strictEqual((await new AdminService().getInaQuery("cost-centre-actuals")).definition, null);
    const missing = await refusal(() => new AdminService().getInaQuery("nope"));
    assert.strictEqual(missing.status, 404);
    assert.strictEqual(missing.detail, "Query not found");
});

QUnit.test("create stores a query; a taken name is a 409; a refusal names fields, never values", async function (assert) {
    const service = new AdminService();
    const created = await service.createInaQuery({ ...INA_INPUT, name: "airline-load" });
    assert.strictEqual(created.name, "airline-load");
    assert.strictEqual(created.definition, null, "not described yet");
    assert.strictEqual(created.described, false);
    assert.ok(created.updated_at, "stamped");
    assert.ok((await service.listInaQueries()).some((q) => q.name === "airline-load"), "listed");

    const taken = await refusal(() => service.createInaQuery({ ...INA_INPUT }));
    assert.strictEqual(taken.status, 409);
    assert.strictEqual(taken.detail, "name: already exists");

    const refused = await refusal(() => service.createInaQuery({
        ...INA_INPUT, name: "Secret Name", title: "  ", query: "ZQ\u200bSECRET", user_context: "yes" as never
    }));
    assert.strictEqual(refused.status, 422);
    assert.strictEqual(refused.detail, "name: String should match pattern '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'; "
        + "title: String should have at least 1 character; user_context: Input should be a valid boolean; "
        + "query: Value error, query must be one line of text without control or format characters",
    "in the order of the payload model, as the server's gate words them");
    assert.strictEqual(refused.detail.indexOf("Secret"), -1, "no value is echoed");
});

QUnit.test("update replaces the fields, keeps the definition and moves updated_at", async function (assert) {
    const service = new AdminService();
    const loaded = await service.getInaQuery("flight-revenue");
    const saved = await service.updateInaQuery("flight-revenue", {
        ...INA_INPUT, title: "Flight revenue (EUR)", expected_updated_at: loaded.updated_at
    });
    assert.strictEqual(saved.title, "Flight revenue (EUR)");
    assert.deepEqual(saved.definition, loaded.definition, "no definition sent: the stored one stays");
    assert.notStrictEqual(saved.updated_at, loaded.updated_at, "a new stamp");
    assert.strictEqual(saved.reloaded, false, "the reload keys are answered");
    assert.strictEqual(saved.reload_failed, false);

    const stale = await refusal(() => service.updateInaQuery("flight-revenue", {
        ...INA_INPUT, expected_updated_at: loaded.updated_at
    }));
    assert.strictEqual(stale.status, 409, "saved from the version before");
    assert.strictEqual(stale.detail, FakeIna.changedElsewhere("flight-revenue"));

    const renamed = await refusal(() => service.updateInaQuery("flight-revenue", { ...INA_INPUT, name: "other-name" }));
    assert.strictEqual(renamed.status, 409);
    assert.strictEqual(renamed.detail, "name: cannot be changed; register the query under the new name instead");
});

QUnit.test("an update answers the reload outcome the backend is set to", async function (this: { backend: FakeBackend }, assert) {
    this.backend.reloadOutcome = "failed";
    const saved = await new AdminService().updateInaQuery("flight-revenue", INA_INPUT);
    assert.strictEqual(saved.reload_failed, true, "stored, not live");
    assert.deepEqual(await new AdminService().deleteInaQuery("cost-centre-actuals"), { reloaded: false, reload_failed: true });
});

QUnit.test("delete is refused while an agent names the query", async function (assert) {
    const service = new AdminService();
    const inUse = await refusal(() => service.deleteInaQuery("flight-revenue"));
    assert.strictEqual(inUse.status, 409);
    assert.strictEqual(inUse.detail, "Query 'flight-revenue' is used by agent(s) 'gmail-agent'");

    assert.deepEqual(await service.deleteInaQuery("cost-centre-actuals"), { reloaded: false, reload_failed: false });
    assert.strictEqual((await refusal(() => service.getInaQuery("cost-centre-actuals"))).status, 404, "gone");
});

QUnit.test("describe stores what SAP describes and answers counts and names", async function (assert) {
    const service = new AdminService();
    const result = await service.describeInaQuery("cost-centre-actuals");
    assert.deepEqual(result, {
        dimensions: 2, measures: 2, variables: 1, names: ["0COSTCENTER", "0CALMONTH", "0AMOUNT", "0QUANTITY", "ZV_YEAR"],
        truncated: false, skipped: 0, metadata_fetched_at: result.metadata_fetched_at, reloaded: false, reload_failed: false
    }, "the route's answer: counts, names, what was left out, the stamp and the reload outcome");
    assert.ok(result.metadata_fetched_at, "stamped");
    const stored = await service.getInaQuery("cost-centre-actuals");
    assert.strictEqual(stored.described, true);
    assert.deepEqual(stored.definition?.variables, [{ name: "ZV_YEAR", label: "Fiscal year", mandatory: true, type: "single" }]);
    assert.strictEqual(stored.metadata_fetched_at, result.metadata_fetched_at);
});

QUnit.test("a query SAP does not know is refused with the code of its header", async function (assert) {
    const service = new AdminService();
    await service.createInaQuery({ ...INA_INPUT, name: "unknown-in-sap", query: "ZNOT_THERE" });
    const described = await refusal(() => service.describeInaQuery("unknown-in-sap"));
    assert.strictEqual(described.status, 502);
    assert.strictEqual(described.code, "query_unknown");
    assert.strictEqual(described.detail, "SAP does not know the InA query of this registration");
    assert.strictEqual((await service.getInaQuery("unknown-in-sap")).definition, null, "nothing stored");
    assert.strictEqual((await refusal(() => service.testInaQuery("unknown-in-sap"))).code, "query_unknown");
    assert.strictEqual((await refusal(() => service.describeInaQuery("nope"))).status, 404);
});

QUnit.test("the test answers outcome and column names, never a value", async function (assert) {
    const result = await new AdminService().testInaQuery("flight-revenue");
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.status, 200);
    assert.deepEqual(result.columns, ["ZREVENUE"], "the first measure");
    assert.deepEqual(Object.keys(result).sort(), ["columns", "duration_ms", "ok", "status"]);
    // A mandatory variable cannot be filled by a test: refused before anything is sent.
    const required = await refusal(() => new AdminService().testInaQuery("cost-centre-actuals"));
    assert.strictEqual(required.status, 422);
    assert.strictEqual(required.code, "variable_required", "the code of the header");
});

QUnit.module("AdminService against the fake catalogue links", {
    beforeEach: function (this: { backend: FakeBackend }) {
        this.backend = new FakeBackend();
        this.backend.reset();
        this.backend.install();
    },
    afterEach: function (this: { backend: FakeBackend }) {
        this.backend.restore();
    }
});

QUnit.test("the sources are of every kind; an InA source answers its query and members", async function (assert) {
    const service = new AdminService();
    const sources = await service.listDataSources();
    assert.deepEqual(sources.map((s) => `${s.kind}:${s.name}`), [
        "odata:purchase-requisitions", "odata:business-partners", "odata:sales-orders", "generic:agent-data",
        "ina:flight-revenue"
    ]);
    const ina = await service.getDataSource("ina", "flight-revenue");
    assert.strictEqual(ina.query?.node.name, "ZFLIGHT_REV_Q001");
    assert.deepEqual(ina.query?.dimensions.map((d) => d.name), ["0CALMONTH", "ZCARRID"]);
    assert.deepEqual(ina.query?.measures.map((m) => m.unit), ["EUR"]);
    assert.notOk("entities" in ina, "no entities for an InA source");
    const generic = await service.getDataSource("generic", "agent-data");
    assert.deepEqual(generic.entities?.map((e) => e.node.name), ["SCARRSet"]);
    assert.deepEqual(generic.functions?.map((f) => f.name), ["MONTH_NAMES_GET"]);
    assert.notOk("query" in generic);
});

QUnit.test("every edge of a detail carries its id, its origin and both ends", async function (assert) {
    const detail = await new AdminService().getDataSource("generic", "agent-data");
    assert.deepEqual(detail.edges.map((e) => [e.id, e.kind, e.curated]), [[4, "aggregates", true]],
        "an edge that ends in the source is listed too");
    assert.deepEqual(detail.edges[0].from_node, {
        id: 310, kind: "query", source_kind: "ina", source_name: "flight-revenue", name: "ZFLIGHT_REV_Q001",
        label: "Flight revenue by month"
    });
    const imported = (await new AdminService().getDataSource("odata", "purchase-requisitions")).edges;
    assert.deepEqual(imported.map((e) => e.curated), [false, false, false]);
    const graph = await new AdminService().getDataGraph();
    assert.ok(graph.edges.every((e) => typeof e.curated === "boolean"), "graph edges carry the flag");
    assert.ok(graph.edges.every((e) => !("id" in e)), "and no id");
});

QUnit.test("an admin draws a link and removes it; an imported one stays", async function (assert) {
    const service = new AdminService();
    const edge = await service.createDataEdge({ from_id: 20, to_id: 210, kind: "same_object" });
    assert.strictEqual(edge.curated, true);
    assert.strictEqual(edge.label, "");
    assert.strictEqual(edge.to_node?.source_kind, "generic");
    const listed = (await service.getDataSource("odata", "purchase-requisitions")).edges;
    assert.ok(listed.some((e) => e.id === edge.id && e.curated), "the detail lists it");

    const again = await refusal(() => service.createDataEdge({ from_id: 210, to_id: 20, kind: "aggregates" }));
    assert.strictEqual(again.status, 409, "the pair is linked, in either direction and by any kind");
    assert.strictEqual(again.detail, "The two nodes are already linked");

    const imported = await refusal(() => service.deleteDataEdge(1));
    assert.strictEqual(imported.status, 409);
    assert.strictEqual(imported.detail, "Only a link an admin drew can be removed; an import made this one");

    await service.deleteDataEdge(edge.id);
    assert.notOk((await service.getDataSource("odata", "purchase-requisitions")).edges.some((e) => e.id === edge.id),
        "removed");
    assert.strictEqual((await refusal(() => service.deleteDataEdge(edge.id))).status, 404, "a second time: not found");
    assert.strictEqual((await refusal(() => service.deleteDataEdge("abc"))).detail, "Link not found");
});

QUnit.test("a link is refused with the field and the rule, never the value", async function (assert) {
    const service = new AdminService();
    const detailOf = async (body: unknown) => (await refusal(() => service.createDataEdge(body as never))).detail;

    assert.strictEqual(await detailOf({ from_id: 99999, to_id: 210, kind: "aggregates" }),
        "from_id: not a node of the catalogue");
    assert.strictEqual(await detailOf({ from_id: 20, to_id: 211, kind: "aggregates" }),
        "to_id: not an entity, query or function", "a field cannot be linked");
    assert.strictEqual(await detailOf({ from_id: 10, to_id: 20, kind: "aggregates" }), "to_id: same source as from_id");
    assert.strictEqual(await detailOf({ from_id: 20, to_id: 20, kind: "aggregates" }), "to_id: same source as from_id");
    assert.strictEqual(await detailOf({ from_id: 20, to_id: 210, kind: "navigation" }),
        "kind: Value error, must be same_object or aggregates");
    assert.strictEqual(await detailOf({ from_id: "20", to_id: 0, kind: "aggregates", note: "x" }),
        "from_id: Input should be a valid integer; to_id: Input should be greater than or equal to 1; "
        + "note: Extra inputs are not permitted");
    assert.strictEqual(await detailOf({ to_id: 210 }), "from_id: Field required; kind: Field required");
    assert.strictEqual(await detailOf([1, 2]), "body: expected a JSON object");
    assert.strictEqual((await refusal(() => service.createDataEdge({ from_id: 1, to_id: 2, kind: "x" } as never))).status,
        422);
});
