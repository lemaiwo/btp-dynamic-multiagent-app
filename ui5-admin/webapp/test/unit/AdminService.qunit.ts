import AdminService, { AdminError } from "com/agent/admin/service/AdminService";
import type { ODataServiceInput } from "com/agent/admin/service/types";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";

QUnit.module("AdminService", {
    beforeEach: function (this: { originalFetch: typeof fetch }) {
        this.originalFetch = window.fetch;
    },
    afterEach: function (this: { originalFetch: typeof fetch }) {
        window.fetch = this.originalFetch;
    }
});

/** Replaces window.fetch and records [url, method, body] for each call. */
function stubFetch(status: number, body: unknown, calls: string[][]): void {
    window.fetch = ((url: string, init?: RequestInit) => {
        calls.push([url, init?.method ?? "GET", (init?.body as string) ?? ""]);
        return Promise.resolve(new Response(JSON.stringify(body), {
            status,
            headers: { "Content-Type": "application/json" }
        }));
    }) as unknown as typeof fetch;
}

QUnit.test("listAgents calls the relative backend path", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, [{ id: 1, name: "a" }], calls);

    const agents = await new AdminService().listAgents();

    assert.strictEqual(calls[0][0], "backend/agents", "relative path, no leading slash");
    assert.strictEqual(calls[0][1], "GET", "uses GET");
    assert.strictEqual(agents.length, 1, "returns the parsed body");
});

QUnit.test("upsertAgent PUTs to the id path when the agent has one", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { id: 7, name: "a" }, calls);

    await new AdminService().upsertAgent({ name: "a" } as never, 7);

    assert.strictEqual(calls[0][0], "backend/agents/7", "targets the id");
    assert.strictEqual(calls[0][1], "PUT", "uses PUT for an existing agent");
});

QUnit.test("upsertAgent POSTs to the collection when there is no id", async function (assert) {
    const calls: string[][] = [];
    stubFetch(201, { id: 8, name: "a" }, calls);

    await new AdminService().upsertAgent({ name: "a" } as never);

    assert.strictEqual(calls[0][0], "backend/agents", "targets the collection");
    assert.strictEqual(calls[0][1], "POST", "uses POST for a new agent");
});

QUnit.test("a 422 becomes an AdminError carrying field errors", async function (assert) {
    stubFetch(422, {
        detail: [
            { loc: ["body", "mcp_servers", 0, "url"], msg: "url must use https://" }
        ]
    }, []);

    try {
        await new AdminService().upsertAgent({ name: "a" } as never);
        assert.ok(false, "should have thrown");
    } catch (e) {
        const err = e as AdminError;
        assert.ok(err instanceof AdminError, "throws AdminError");
        assert.strictEqual(err.status, 422, "carries the status");
        assert.strictEqual(
            err.fieldErrors["mcp_servers.0.url"],
            "url must use https://",
            "maps the FastAPI loc array to a dotted field key"
        );
    }
});

QUnit.test("a 409 from runNow carries the conflict detail", async function (assert) {
    stubFetch(409, { detail: "a run is already in progress" }, []);

    try {
        await new AdminService().runNow(3);
        assert.ok(false, "should have thrown");
    } catch (e) {
        const err = e as AdminError;
        assert.strictEqual(err.status, 409, "carries 409");
        assert.strictEqual(err.detail, "a run is already in progress", "carries the detail string");
    }
});

QUnit.test("a string detail is used verbatim", async function (assert) {
    stubFetch(404, { detail: "Agent not found" }, []);

    try {
        await new AdminService().getAgent(99);
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).detail, "Agent not found");
    }
});

QUnit.test("listRuns builds the query string", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, [], calls);

    await new AdminService().listRuns({ agentId: 4, limit: 10 });

    assert.strictEqual(calls[0][0], "backend/runs?agent_id=4&limit=10", "agent_id and limit");
});

QUnit.test("runReportUrl is relative so it works in both hosts", function (assert) {
    assert.strictEqual(
        new AdminService().runReportUrl("abc-123"),
        "backend/runs/abc-123/report.md"
    );
});

QUnit.test("a 204 resolves rather than failing to parse an empty body", async function (assert) {
    // Both delete endpoints return 204. Parsing that as JSON would throw, and
    // a caller cannot distinguish the resulting undefined from a failure —
    // which is why BaseController.runOk exists.
    window.fetch = (() => Promise.resolve(new Response(null, { status: 204 }))) as unknown as typeof fetch;

    await new AdminService().deleteAgent(5);
    assert.ok(true, "deleteAgent resolves on a 204 instead of throwing");
});

QUnit.test("a failed delete still rejects", async function (assert) {
    stubFetch(404, { detail: "Agent not found" }, []);

    try {
        await new AdminService().deleteAgent(5);
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 404, "rejects so runOk returns false");
    }
});

QUnit.test("listWorkflows calls the relative backend path", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, [{ id: 1, name: "a" }], calls);

    const workflows = await new AdminService().listWorkflows();

    assert.strictEqual(calls[0][0], "backend/workflows", "relative path, no leading slash");
    assert.strictEqual(calls[0][1], "GET", "uses GET");
    assert.strictEqual(workflows.length, 1, "returns the parsed body");
});

QUnit.test("getWorkflow calls the id path", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { id: 3, name: "a", branches: [], steps: [] }, calls);

    await new AdminService().getWorkflow(3);

    assert.strictEqual(calls[0][0], "backend/workflows/3", "targets the id");
    assert.strictEqual(calls[0][1], "GET", "uses GET");
});

QUnit.test("upsertWorkflow PUTs to the id path when the workflow has one", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { id: 7, name: "a" }, calls);

    await new AdminService().upsertWorkflow({ name: "a" } as never, 7);

    assert.strictEqual(calls[0][0], "backend/workflows/7", "targets the id");
    assert.strictEqual(calls[0][1], "PUT", "uses PUT for an existing workflow");
});

QUnit.test("upsertWorkflow POSTs to the collection when there is no id", async function (assert) {
    const calls: string[][] = [];
    stubFetch(201, { id: 8, name: "a" }, calls);

    await new AdminService().upsertWorkflow({ name: "a" } as never);

    assert.strictEqual(calls[0][0], "backend/workflows", "targets the collection");
    assert.strictEqual(calls[0][1], "POST", "uses POST for a new workflow");
});

QUnit.test("deleteWorkflow resolves on a 204", async function (assert) {
    window.fetch = (() => Promise.resolve(new Response(null, { status: 204 }))) as unknown as typeof fetch;

    await new AdminService().deleteWorkflow(5);
    assert.ok(true, "deleteWorkflow resolves on a 204 instead of throwing");
});

QUnit.test("runWorkflowNow posts to the run path", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { run_id: "wf-run-1" }, calls);

    const result = await new AdminService().runWorkflowNow(9);

    assert.strictEqual(calls[0][0], "backend/workflows/9/run", "targets the run path");
    assert.strictEqual(calls[0][1], "POST", "uses POST");
    assert.strictEqual(result.run_id, "wf-run-1", "returns the parsed body");
});

QUnit.test("a 409 from runWorkflowNow carries the conflict detail", async function (assert) {
    stubFetch(409, { detail: "a run is already in progress" }, []);

    try {
        await new AdminService().runWorkflowNow(9);
        assert.ok(false, "should have thrown");
    } catch (e) {
        const err = e as AdminError;
        assert.strictEqual(err.status, 409, "carries 409");
        assert.strictEqual(err.detail, "a run is already in progress", "carries the detail string");
    }
});

QUnit.test("listWorkflowRuns builds the query string", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, [], calls);

    await new AdminService().listWorkflowRuns({ workflowId: 4, limit: 10 });

    assert.strictEqual(calls[0][0], "backend/workflow-runs?workflow_id=4&limit=10", "workflow_id and limit");
});

QUnit.test("listWorkflowRuns defaults the limit when omitted", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, [], calls);

    await new AdminService().listWorkflowRuns();

    assert.strictEqual(calls[0][0], "backend/workflow-runs?limit=50", "no workflow_id, default limit");
});

QUnit.test("getWorkflowRun calls the run id path", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { run: {}, items: [], steps: [] }, calls);

    await new AdminService().getWorkflowRun("wf-run-1");

    assert.strictEqual(calls[0][0], "backend/workflow-runs/wf-run-1", "targets the run id");
    assert.strictEqual(calls[0][1], "GET", "uses GET");
});

// --- where used ---
QUnit.test("getAgentWhereUsed calls the agent's where-used path", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { agent: { id: 7, name: "a" }, peers: [], workflows: [] }, calls);

    const result = await new AdminService().getAgentWhereUsed(7);

    assert.strictEqual(calls[0][0], "backend/agents/7/where-used", "targets the agent's where-used path");
    assert.strictEqual(calls[0][1], "GET", "uses GET");
    assert.deepEqual(result.agent, { id: 7, name: "a" }, "returns the parsed body");
});

// --- odata ---
// AdminService's "backend/" prefix is mapped onto /admin/api/ by the
// approuter (and by ui5-local.yaml), so "backend/odata/services" is the
// server's /admin/api/odata/services.
const ODATA_INPUT: ODataServiceInput = {
    name: "purchase-requisitions", title: "Purchase requisitions",
    purpose: "Read requisitions and their items to judge an approval", not_for: "",
    destination: "S4_ODATA_USER", user_context: true, odata_version: "v2",
    service_path: "/sap/opu/odata/sap/SRV", enabled: true,
    definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null
};

QUnit.test("OData catalogue calls use the documented paths and methods", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, {}, calls);
    const service = new AdminService();

    await service.listODataServices();
    await service.getODataService("purchase-requisitions");
    await service.createODataService(ODATA_INPUT);
    await service.updateODataService("purchase-requisitions", ODATA_INPUT);
    await service.deleteODataService("purchase-requisitions");
    await service.duplicateODataService("purchase-requisitions", {
        name: "pr-jobs", destination: "S4_ODATA_TECH", user_context: false
    });
    await service.readODataMetadata({
        destination: "S4_ODATA_TECH", service_path: "/sap/opu/odata/sap/SRV", odata_version: "v2"
    });
    await service.testODataService("purchase-requisitions");

    assert.deepEqual(calls.map((c) => `${c[1]} ${c[0]}`), [
        "GET backend/odata/services",
        "GET backend/odata/services/purchase-requisitions",
        "POST backend/odata/services",
        "PUT backend/odata/services/purchase-requisitions",
        "DELETE backend/odata/services/purchase-requisitions",
        "POST backend/odata/services/purchase-requisitions/duplicate",
        "POST backend/odata/metadata",
        "POST backend/odata/services/purchase-requisitions/test"
    ]);
    assert.deepEqual(JSON.parse(calls[2][2]), ODATA_INPUT, "create sends the payload as is");
    assert.deepEqual(JSON.parse(calls[3][2]), ODATA_INPUT, "update sends the payload as is");
    assert.deepEqual(JSON.parse(calls[5][2]), {
        name: "pr-jobs", destination: "S4_ODATA_TECH", user_context: false
    }, "duplicate sends the new name and its overrides");
    assert.deepEqual(JSON.parse(calls[6][2]), {
        destination: "S4_ODATA_TECH", service_path: "/sap/opu/odata/sap/SRV", odata_version: "v2"
    }, "metadata sends where to read from");
    assert.deepEqual(JSON.parse(calls[7][2]), {}, "the test call sends an empty object");
});

QUnit.test("a service name is url-encoded", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, {}, calls);
    const service = new AdminService();

    await service.getODataService("a/b c");
    await service.updateODataService("a/b c", ODATA_INPUT);
    await service.deleteODataService("a/b c");
    await service.duplicateODataService("a/b c", { name: "copy" });
    await service.testODataService("a/b c");

    assert.deepEqual(calls.map((c) => c[0]), [
        "backend/odata/services/a%2Fb%20c",
        "backend/odata/services/a%2Fb%20c",
        "backend/odata/services/a%2Fb%20c",
        "backend/odata/services/a%2Fb%20c/duplicate",
        "backend/odata/services/a%2Fb%20c/test"
    ]);
});

QUnit.test("deleteODataService resolves on a 204 and rejects with the 409 detail", async function (assert) {
    window.fetch = (() => Promise.resolve(new Response(null, { status: 204 }))) as unknown as typeof fetch;
    await new AdminService().deleteODataService("purchase-requisitions");
    assert.ok(true, "resolves on a 204");

    stubFetch(409, { detail: "Service 'purchase-requisitions' is used by agent(s) 'btp-agent'" }, []);
    try {
        await new AdminService().deleteODataService("purchase-requisitions");
        assert.ok(false, "should have thrown");
    } catch (e) {
        const err = e as AdminError;
        assert.strictEqual(err.status, 409, "carries 409");
        assert.strictEqual(err.detail, "Service 'purchase-requisitions' is used by agent(s) 'btp-agent'");
    }
});

// The fake backend answers the same eight routes, so the journeys that follow
// run the real service against it.
QUnit.module("AdminService against the fake OData catalogue", {
    beforeEach: function (this: { backend: FakeBackend }) {
        this.backend = new FakeBackend();
        this.backend.reset();
        this.backend.install();
    },
    afterEach: function (this: { backend: FakeBackend }) {
        this.backend.restore();
    }
});

QUnit.test("the list is the four seeded services by name, without their definition", async function (assert) {
    const list = await new AdminService().listODataServices();

    assert.deepEqual(list.map((s) => s.name), [
        "business-partners", "purchase-requisitions", "purchase-requisitions-jobs", "purchase-requisitions-v4"
    ]);
    assert.notOk("definition" in list[0], "a summary carries no definition");
    const byName = (name: string) => list.filter((s) => s.name === name)[0];
    assert.deepEqual(byName("purchase-requisitions").counts, { entity_sets: 5, operations: 0 });
    assert.strictEqual(byName("purchase-requisitions").user_context, true);
    assert.strictEqual(byName("purchase-requisitions").has_write, false);
    assert.deepEqual(byName("purchase-requisitions-jobs").counts, { entity_sets: 5, operations: 1 });
    assert.strictEqual(byName("purchase-requisitions-jobs").user_context, false);
    assert.strictEqual(byName("purchase-requisitions-jobs").has_write, true);
    assert.strictEqual(byName("business-partners").used_by.length, 2);
    assert.strictEqual(byName("purchase-requisitions-v4").enabled, false);
    assert.strictEqual(byName("purchase-requisitions-v4").odata_version, "v4");
});

QUnit.test("get returns the definition; an unknown name is a 404", async function (assert) {
    const service = new AdminService();
    const jobs = await service.getODataService("purchase-requisitions-jobs");

    assert.strictEqual(jobs.definition.entity_sets.length, 5);
    assert.strictEqual(jobs.definition.operations[0].name, "ReleaseItem");
    try {
        await service.getODataService("nope");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 404);
        assert.strictEqual((e as AdminError).detail, "Service not found");
    }
});

QUnit.test("create stores a service; a taken name is a 409", async function (assert) {
    const service = new AdminService();
    const created = await service.createODataService({ ...ODATA_INPUT, name: "suppliers" });

    assert.strictEqual(created.name, "suppliers");
    assert.deepEqual(created.used_by, [], "a new service is used by nobody");
    assert.strictEqual((await service.listODataServices()).length, 5);
    try {
        await service.createODataService({ ...ODATA_INPUT, name: "suppliers" });
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 409);
        assert.strictEqual((e as AdminError).detail, "Service name 'suppliers' already exists");
    }
});

QUnit.test("update changes a service but never its name", async function (assert) {
    const service = new AdminService();
    const stored = await service.getODataService("business-partners");
    const input: ODataServiceInput = {
        name: stored.name, title: "Suppliers", purpose: stored.purpose, not_for: stored.not_for,
        destination: stored.destination, user_context: stored.user_context,
        odata_version: stored.odata_version, service_path: stored.service_path, enabled: stored.enabled,
        definition: stored.definition, metadata_fetched_at: stored.metadata_fetched_at
    };

    const updated = await service.updateODataService("business-partners", input);
    assert.strictEqual(updated.title, "Suppliers");
    assert.strictEqual(updated.used_by.length, 2, "who uses it is the server's to say");
    try {
        await service.updateODataService("business-partners", { ...input, name: "renamed" });
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 422);
        assert.strictEqual((e as AdminError).detail, "name cannot be changed; duplicate the service instead");
    }
});

QUnit.test("delete is refused while an agent uses the service", async function (assert) {
    const service = new AdminService();

    try {
        await service.deleteODataService("business-partners");
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 409);
        assert.strictEqual(
            (e as AdminError).detail,
            "Service 'business-partners' is used by agent(s) 'btp-agent', 'gmail-agent'"
        );
    }
    await service.deleteODataService("purchase-requisitions-v4");
    assert.strictEqual((await service.listODataServices()).length, 3, "an unused service is deleted");
});

QUnit.test("duplicate copies the definition under a new name; a taken name is a 409", async function (assert) {
    const service = new AdminService();
    const source = await service.getODataService("purchase-requisitions");

    const copy = await service.duplicateODataService("purchase-requisitions", {
        name: "pr-copy", destination: "S4_ODATA_TECH", user_context: false
    });
    assert.strictEqual(copy.name, "pr-copy");
    assert.strictEqual(copy.title, source.title, "the title is kept unless one is given");
    assert.strictEqual(copy.destination, "S4_ODATA_TECH");
    assert.strictEqual(copy.user_context, false);
    assert.deepEqual(copy.definition, source.definition, "same definition");
    assert.deepEqual(copy.used_by, [], "the copy is used by nobody");
    assert.notStrictEqual(copy.id, source.id);
    try {
        await service.duplicateODataService("purchase-requisitions", { name: "business-partners" });
        assert.ok(false, "should have thrown");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 409);
    }
});

QUnit.test("metadata and test answer with the fake's preview and result", async function (assert) {
    const service = new AdminService();

    const preview = await service.readODataMetadata({
        destination: "S4_ODATA_TECH", service_path: "/sap/opu/odata/sap/SRV", odata_version: "v2",
        service: "purchase-requisitions-jobs"
    });
    assert.strictEqual(preview.summary.entity_sets, preview.entity_sets.length);
    assert.strictEqual(preview.summary.operations, preview.operations.length);
    assert.deepEqual(
        preview.entity_sets.filter((e) => e.status === "changed").map((e) => e.new_fields),
        [["PurReqnOrigin", "LastChangeDateTime"]]
    );

    const result = await service.testODataService("purchase-requisitions-jobs");
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.identity, "technical");
    assert.strictEqual(result.destination, "S4_ODATA_TECH");
});
