import AdminService, { AdminError } from "com/agent/admin/service/AdminService";
import type { ODataServiceInput } from "com/agent/admin/service/types";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";
import {
    ENTITY_ACCEPTED, ENTITY_CASES, ONE_LINE_ACCEPTED, REMOVAL_CASES, RULE_CASES, VALID_INPUT, changed, entityRefusal,
    removalDefinition, withEntitySet,
    withField
} from "./odataRuleCases";

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

QUnit.test("every request, a GET included, is marked as AJAX for the approuter", async function (assert) {
    // The approuter answers an expired session with 401 only for an AJAX
    // request (or a non-GET); a plain GET gets a 302 to the identity
    // provider, which fetch follows cross-origin into "Failed to fetch".
    const seen: Record<string, string>[] = [];
    window.fetch = ((_url: string, init?: RequestInit) => {
        seen.push({ ...(init?.headers as Record<string, string>) });
        return Promise.resolve(new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));
    }) as unknown as typeof fetch;

    await new AdminService().listAgents();
    await new AdminService().getNotifications();
    await new AdminService().upsertAgent({ name: "a" } as never, 7);

    seen.forEach((headers, i) => {
        assert.strictEqual(headers["X-Requested-With"], "XMLHttpRequest", `call ${i} carries X-Requested-With`);
        assert.strictEqual(headers.Accept, "application/json", `call ${i} keeps Accept`);
    });
    assert.strictEqual(seen[2]["Content-Type"], "application/json", "a body keeps its Content-Type");
    assert.strictEqual(seen.length, 3, "three calls");
});

QUnit.test("only a Run now trigger marks its error as runTrigger", async function (assert) {
    stubFetch(409, { detail: "conflict" }, []);
    const errorOf = async (call: () => Promise<unknown>): Promise<AdminError> => {
        try {
            await call();
        } catch (e) {
            return e as AdminError;
        }
        throw new Error("should have thrown");
    };
    const service = new AdminService();

    assert.strictEqual((await errorOf(() => service.runNow(3))).runTrigger, true, "agent run");
    assert.strictEqual((await errorOf(() => service.runWorkflowNow(9))).runTrigger, true, "workflow run");
    assert.strictEqual((await errorOf(() => service.upsertAgent({ name: "a" } as never))).runTrigger, false, "agent save");
    assert.strictEqual((await errorOf(() => service.deleteAgent(3))).runTrigger, false, "agent delete");
    assert.strictEqual((await errorOf(() => service.upsertWorkflow({ name: "w" } as never, 9))).runTrigger, false, "workflow rename");
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

QUnit.test("a delete resolves with what the 204 says about the reload, from its two headers", async function (assert) {
    const answer = (headers: Record<string, string>) => {
        window.fetch = (() => Promise.resolve(new Response(null, { status: 204, headers }))) as unknown as typeof fetch;
    };
    answer({ "X-OData-Reloaded": "false", "X-OData-Reload-Failed": "true" });
    assert.deepEqual(await new AdminService().deleteODataService("purchase-requisitions"),
        { reloaded: false, reload_failed: true }, "a service: stored, not live");
    assert.deepEqual(await new AdminService().deleteAgent(5), { reloaded: false, reload_failed: true }, "an agent");

    answer({ "X-OData-Reloaded": "true", "X-OData-Reload-Failed": "false" });
    assert.deepEqual(await new AdminService().deleteAgent(5), { reloaded: true, reload_failed: false });

    answer({});
    assert.deepEqual(await new AdminService().deleteODataService("purchase-requisitions"),
        { reloaded: false, reload_failed: false }, "an answer without the headers reports no failure");
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
    assert.strictEqual(copy.title, "Purchase requisitions (copy)", "the title says it is a copy unless one is given");
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

QUnit.test("metadata compares the offer with the service the request names", async function (assert) {
    const service = new AdminService();
    const where = { destination: "S4_ODATA_TECH", service_path: "/sap/opu/odata/sap/SRV", odata_version: "v2" as const };

    const fresh = await service.readODataMetadata(where);
    assert.deepEqual(
        fresh.entity_sets.map((e) => e.status), ["new", "new", "new", "new", "new"],
        "without a service everything is new"
    );
    assert.deepEqual(fresh.operations.map((o) => o.status), ["new"]);
    assert.deepEqual(fresh.summary, { entity_sets: 5, operations: 1, in_service: 0, changed: 0, skipped: 1, removed_entity_sets: 0, removed_operations: 0, skipped_stored_entity_sets: 0 });
    assert.deepEqual(fresh.entity_sets[0].new_fields, [], "nothing to compare with, so no new fields");

    const compared = await service.readODataMetadata({ ...where, service: "purchase-requisitions-jobs" });
    assert.deepEqual(
        compared.entity_sets.map((e) => e.status),
        ["changed", "in_service", "in_service", "in_service", "in_service"]
    );
    assert.deepEqual(compared.entity_sets[0].new_fields, ["PurReqnOrigin", "LastChangeDateTime"]);
    assert.deepEqual(compared.entity_sets[0].removed_fields, []);
    assert.deepEqual(compared.operations.map((o) => o.status), ["in_service"]);
    assert.deepEqual(compared.summary, { entity_sets: 5, operations: 1, in_service: 4, changed: 1, skipped: 1, removed_entity_sets: 0, removed_operations: 0, skipped_stored_entity_sets: 0 });

    const other = await service.readODataMetadata({ ...where, service: "business-partners" });
    assert.deepEqual(other.removed_entity_sets, ["A_BusinessPartner", "A_Supplier", "A_BusinessPartnerAddress"],
        "what the stored service has and the document does not");
    assert.deepEqual(other.summary, { entity_sets: 5, operations: 1, in_service: 0, changed: 0, skipped: 1, removed_entity_sets: 3, removed_operations: 0, skipped_stored_entity_sets: 0 });

    assert.deepEqual(compared.skipped, [
        {
            kind: "property", entity_set: "A_PurReqnAcctAssgmt", position: 38, reason: "invalid_type",
            entity_type: "API_PURCHASEREQ_PROCESS_SRV.A_PurReqnAcctAssgmtType"
        }
    ], "what the parser left out is listed by kind, set, position and reason");
});

QUnit.test("a refusal carries the stable code of its X-OData-Error header, and none without it", async function (this: { backend: FakeBackend }, assert) {
    const backend = this.backend;
    backend.failNext = {
        path: "odata/services/purchase-requisitions-jobs/test", status: 429, body: { detail: "busy, try again" },
        headers: { "X-OData-Error": "busy" }
    };
    let refused: unknown;
    await new AdminService().testODataService("purchase-requisitions-jobs").catch((error: unknown) => { refused = error; });
    assert.ok(refused instanceof AdminError, "an AdminError");
    assert.strictEqual((refused as AdminError).status, 429);
    assert.strictEqual((refused as AdminError).code, "busy", "the code of the header");
    assert.strictEqual((refused as AdminError).detail, "busy, try again");

    backend.failNext = { path: "odata/services/purchase-requisitions-jobs/test", status: 429, body: { detail: "from a proxy" } };
    await new AdminService().testODataService("purchase-requisitions-jobs").catch((error: unknown) => { refused = error; });
    assert.strictEqual((refused as AdminError).code, "", "no header, no code");
    assert.strictEqual(new AdminError(500, "x").code, "", "and none by default");
});

QUnit.test("the fake's has_write goes by the server's write rule for operations", async function (this: { backend: FakeBackend }, assert) {
    const backend = this.backend;
    const service = backend.odataServices.filter((s) => s.name === "purchase-requisitions-jobs")[0];
    service.definition.entity_sets.forEach((entitySet) => {
        entitySet.operations = entitySet.operations.filter((op) => op === "list" || op === "get");
    });
    const flag = async () => (await new AdminService().getODataService("purchase-requisitions-jobs")).has_write;
    const operation = service.definition.operations[0];
    operation.changes_data = false;
    assert.strictEqual(await flag(), true, "an enabled POST stored with changes_data false counts");
    operation.http_method = "GET";
    assert.strictEqual(await flag(), false, "a GET marked as only reading does not");
    delete (operation as unknown as Record<string, unknown>).changes_data;
    assert.strictEqual(await flag(), true, "a missing flag is a write");
    operation.enabled = false;
    assert.strictEqual(await flag(), false, "an operation that is off is none");
});

QUnit.test("the test call answers with the fake's result", async function (assert) {
    const result = await new AdminService().testODataService("purchase-requisitions-jobs");

    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.identity, "technical");
    assert.strictEqual(result.destination, "S4_ODATA_TECH");
});

// --- what the real routes refuse ---
// agents/odata/admin_routes.py answers a refused body with 422 and a STRING
// detail, "<loc>: <msg>; <loc>: <msg>", which never repeats the input.
async function refusal(call: () => Promise<unknown>): Promise<AdminError> {
    try {
        await call();
    } catch (e) {
        return e as AdminError;
    }
    throw new Error("the call was not refused");
}

QUnit.test("create answers 422 with the server's text for every rule of the shared table", async function (assert) {
    const service = new AdminService();

    for (const testCase of RULE_CASES) {
        const error = await refusal(() => service.createODataService(withField(testCase)));
        assert.strictEqual(error.status, 422, `${testCase.rule}: 422`);
        assert.strictEqual(error.detail, testCase.server, `${testCase.rule}: detail`);
    }
    assert.strictEqual((await service.listODataServices()).length, 4, "nothing was stored");
});

QUnit.test("create answers 422 with the server's text for every entity-set rule of the shared table", async function (assert) {
    const service = new AdminService();

    for (const testCase of ENTITY_CASES) {
        const error = await refusal(() => service.createODataService(withEntitySet(changed(testCase.change))));
        assert.strictEqual(error.status, 422, `${testCase.rule}: 422`);
        assert.strictEqual(error.detail, entityRefusal(testCase), `${testCase.rule}: detail`);
    }
    assert.strictEqual((await service.listODataServices()).length, 4, "nothing was stored");
    for (const accepted of ENTITY_ACCEPTED) {
        const created = await service.createODataService({
            ...withEntitySet(changed(accepted.change)), name: `accepted-${ENTITY_ACCEPTED.indexOf(accepted)}`
        });
        assert.strictEqual(created.definition.entity_sets.length, 1, `${accepted.rule}: stored`);
    }
});

QUnit.test("a definition whose operation names an entity set that is not there is refused with the server's text", async function (assert) {
    const service = new AdminService();

    for (const testCase of REMOVAL_CASES) {
        const error = await refusal(() => service.createODataService({ ...VALID_INPUT, definition: removalDefinition(testCase, true) }));
        assert.strictEqual(error.status, 422, `${testCase.rule}: 422`);
        assert.strictEqual(error.detail, testCase.server, `${testCase.rule}: detail`);
        const created = await service.createODataService({
            ...VALID_INPUT, name: `whole-${REMOVAL_CASES.indexOf(testCase)}`, definition: removalDefinition(testCase)
        });
        assert.strictEqual(created.definition.operations.length, 1, `${testCase.rule}: stored with the entity set`);
    }
});

QUnit.test("update is held to the same rules as create", async function (assert) {
    const service = new AdminService();
    const stored = await service.getODataService("purchase-requisitions");

    for (const testCase of RULE_CASES.filter((c) => c.field !== "name")) {
        const error = await refusal(() => service.updateODataService("purchase-requisitions", withField(testCase)));
        assert.strictEqual(error.status, 422, `${testCase.rule}: 422`);
        assert.strictEqual(error.detail, testCase.server, `${testCase.rule}: detail`);
    }
    assert.deepEqual(await service.getODataService("purchase-requisitions"), stored, "the service is unchanged");
});

QUnit.test("a refusal is a string that lists every problem and never the input", async function (assert) {
    const error = await refusal(() => new AdminService().createODataService({
        ...VALID_INPUT, title: " ", purpose: "x".repeat(201),
        service_path: "https://alice:s3cret@s4.internal:44300/sap"
    }));

    assert.strictEqual(
        error.detail,
        "title: String should have at least 1 character; "
        + "purpose: String should have at most 200 characters; "
        + "service_path: Value error, service_path is a path, not a URL; the host comes from the destination"
    );
    assert.strictEqual(error.detail.indexOf("s3cret"), -1, "the refused value is not repeated");
    assert.deepEqual(error.fieldErrors, {}, "a string detail carries no field errors of its own");
});

function definitionWith(over: Record<string, unknown>): ODataServiceInput {
    const entitySet = {
        name: "A_Item", title: "", path: "", entity_type: "", description: "",
        keys: [{ name: "Id", type: "Edm.String" }], operations: ["list", "get"],
        fields: [{
            name: "Id", type: "Edm.String", label: "", selectable: true, filterable: true, writable: false,
            hint: "", values: [], personal_data: false
        }],
        navigations: [], examples: [], ...over
    };
    return { ...VALID_INPUT, definition: { entity_sets: [entitySet], operations: [] } } as ODataServiceInput;
}

const HIDDEN = {
    name: "Secret", type: "Edm.String", label: "", selectable: false, filterable: false, writable: false,
    hint: "", values: [], personal_data: false
};

QUnit.test("a filterable field must also be selectable", async function (assert) {
    const input = definitionWith({});
    input.definition.entity_sets[0].fields.push({ ...HIDDEN, filterable: true });

    const error = await refusal(() => new AdminService().createODataService(input));
    assert.strictEqual(error.status, 422);
    assert.strictEqual(
        error.detail,
        "definition.entity_sets.0.fields.1: Value error, field 'Secret' is filterable but not selectable; "
        + "a filterable field must also be selectable"
    );
});

QUnit.test("an entity set with list or get needs a selectable field", async function (assert) {
    const service = new AdminService();

    const list = await refusal(() => service.createODataService(
        definitionWith({ fields: [{ ...HIDDEN, name: "Id" }], operations: ["list"] })
    ));
    assert.strictEqual(list.status, 422);
    assert.strictEqual(
        list.detail, "definition.entity_sets.0: Value error, entity set 'A_Item' has 'list' but no selectable field"
    );
    const get = await refusal(() => service.createODataService(
        definitionWith({ fields: [{ ...HIDDEN, name: "Id" }], operations: ["get"] })
    ));
    assert.strictEqual(
        get.detail, "definition.entity_sets.0: Value error, entity set 'A_Item' has 'get' but no selectable field"
    );
    const none = await service.createODataService({
        ...definitionWith({ fields: [{ ...HIDDEN, name: "Id" }], operations: [] }), name: "not-described-yet"
    });
    assert.strictEqual(none.name, "not-described-yet", "without an operation the set needs no readable field");
});

QUnit.test("a key must be a field, and get, update and delete need a key", async function (assert) {
    const service = new AdminService();

    const unknown = await refusal(() => service.createODataService(
        definitionWith({ keys: [{ name: "Nope", type: "Edm.String" }] })
    ));
    assert.strictEqual(unknown.status, 422);
    assert.strictEqual(
        unknown.detail,
        "definition.entity_sets.0: Value error, key 'Nope' of entity set 'A_Item' is not one of its fields"
    );
    const keyless = await refusal(() => service.createODataService(definitionWith({ keys: [] })));
    assert.strictEqual(
        keyless.detail, "definition.entity_sets.0: Value error, entity set 'A_Item' has 'get' but no key"
    );
});

QUnit.test("create and update need a writable field", async function (assert) {
    const error = await refusal(() => new AdminService().createODataService(
        definitionWith({ operations: ["list", "update"] })
    ));
    assert.strictEqual(
        error.detail,
        "definition.entity_sets.0: Value error, entity set 'A_Item' has 'update' but no writable field"
    );
});

QUnit.test("entity set names are unique", async function (assert) {
    const input = definitionWith({});
    input.definition.entity_sets.push(input.definition.entity_sets[0]);

    const error = await refusal(() => new AdminService().createODataService(input));
    assert.strictEqual(error.status, 422);
    assert.strictEqual(error.detail, "definition: Value error, duplicate entity set 'A_Item'");
});

QUnit.test("an unknown or a missing top-level key is refused", async function (assert) {
    const service = new AdminService();

    const extra = await refusal(() => service.createODataService(
        { ...VALID_INPUT, id: 7, used_by: [] } as unknown as ODataServiceInput
    ));
    assert.strictEqual(extra.status, 422);
    assert.strictEqual(
        extra.detail, "id: Extra inputs are not permitted; used_by: Extra inputs are not permitted",
        "a stored service sent back whole is refused, not silently trimmed"
    );
    const { odata_version, destination, ...partial } = VALID_INPUT;
    void [odata_version, destination];
    const missing = await refusal(() => service.createODataService(partial as ODataServiceInput));
    assert.strictEqual(missing.detail, "destination: Field required; odata_version: Field required");
    const version = await refusal(() => service.createODataService(
        { ...VALID_INPUT, odata_version: "v3" } as unknown as ODataServiceInput
    ));
    assert.strictEqual(version.detail, "odata_version: Input should be 'v2' or 'v4'");
});

QUnit.test("a stored service has its title and purpose stripped", async function (assert) {
    const created = await new AdminService().createODataService(
        { ...VALID_INPUT, name: "suppliers", title: "  Suppliers ", purpose: " Look up suppliers\n" }
    );
    assert.strictEqual(created.title, "Suppliers");
    assert.strictEqual(created.purpose, "Look up suppliers");
});

QUnit.test("an update is a full replacement and moves updated_at", async function (assert) {
    const service = new AdminService();
    const stored = await service.getODataService("purchase-requisitions");

    const updated = await service.updateODataService("purchase-requisitions", VALID_INPUT);
    assert.notStrictEqual(updated.updated_at, stored.updated_at, "updated_at moved");
    assert.strictEqual(updated.created_at, stored.created_at, "created_at did not");
    assert.deepEqual(updated.definition, { entity_sets: [], operations: [] }, "the definition sent is the one stored");
    assert.deepEqual(updated.counts, { entity_sets: 0, operations: 0 });
    assert.strictEqual(updated.used_by.length, 1, "who uses it is not the payload's to say");
});

QUnit.test("a payload without a definition is refused and the stored one stays", async function (assert) {
    const service = new AdminService();
    const stored = await service.getODataService("purchase-requisitions");
    const { definition, ...withoutDefinition } = VALID_INPUT;
    void definition;

    const put = await refusal(() => service.updateODataService(
        "purchase-requisitions", withoutDefinition as ODataServiceInput
    ));
    assert.strictEqual(put.status, 422);
    assert.strictEqual(put.detail, "definition: Field required");
    assert.deepEqual(
        await service.getODataService("purchase-requisitions"), stored,
        "the stored definition was not replaced by an empty one"
    );
    const post = await refusal(() => service.createODataService(
        { ...withoutDefinition, name: "suppliers" } as ODataServiceInput
    ));
    assert.strictEqual(post.detail, "definition: Field required");
});

QUnit.test("an update made from a stale version is refused with 409 and stores nothing", async function (assert) {
    const service = new AdminService();
    const loaded = await service.getODataService("purchase-requisitions");
    const input = { ...VALID_INPUT, definition: loaded.definition };

    // Someone else saves first.
    const theirs = await service.updateODataService("purchase-requisitions", { ...input, title: "Theirs" });
    assert.notStrictEqual(theirs.updated_at, loaded.updated_at, "their save moved updated_at");

    const stale = await refusal(() => service.updateODataService(
        "purchase-requisitions", { ...input, title: "Mine", expected_updated_at: loaded.updated_at }
    ));
    assert.strictEqual(stale.status, 409);
    assert.strictEqual(
        stale.detail, "Service 'purchase-requisitions' was changed since it was loaded; reload it and save again"
    );
    assert.strictEqual((await service.getODataService("purchase-requisitions")).title, "Theirs", "nothing was stored");

    const fresh = await service.updateODataService(
        "purchase-requisitions", { ...input, title: "Mine", expected_updated_at: theirs.updated_at }
    );
    assert.strictEqual(fresh.title, "Mine", "made from the current version, it is saved");
    assert.notStrictEqual(fresh.updated_at, theirs.updated_at, "and updated_at moves again");
    assert.strictEqual(
        (await service.updateODataService("purchase-requisitions", { ...input, expected_updated_at: null })).title,
        VALID_INPUT.title, "null, like a missing key, is not checked"
    );
});

QUnit.test("expected_updated_at belongs to an update only, and is a string", async function (assert) {
    const service = new AdminService();

    const wrong = await refusal(() => service.updateODataService(
        "purchase-requisitions", { ...VALID_INPUT, expected_updated_at: 7 as unknown as string }
    ));
    assert.strictEqual(wrong.status, 422);
    assert.strictEqual(wrong.detail, "expected_updated_at: Input should be a valid string");
    // A string that is no timestamp of the API is refused as such (422),
    // not answered as "changed elsewhere" (409).
    for (const malformed of ["x", "", "2026-10-05", "2026-10-05T09:00:00+00:00\n", "2026-10-05 09:00:00"]) {
        const refused = await refusal(() => service.updateODataService(
            "purchase-requisitions", { ...VALID_INPUT, expected_updated_at: malformed }
        ));
        assert.strictEqual(refused.status, 422, JSON.stringify(malformed));
        assert.strictEqual(refused.detail, "expected_updated_at: expected the updated_at this service was loaded with");
    }
    const create = await refusal(() => service.createODataService(
        { ...VALID_INPUT, name: "suppliers", expected_updated_at: "x" } as ODataServiceInput
    ));
    assert.strictEqual(create.detail, "expected_updated_at: Extra inputs are not permitted");
});

QUnit.test("an unknown key is named only when it looks like a field name", async function (assert) {
    const service = new AdminService();
    const withKey = (key: string) => refusal(() => service.createODataService(
        { ...VALID_INPUT, name: "suppliers", [key]: 1 } as ODataServiceInput
    ));

    assert.strictEqual((await withKey("used_by")).detail, "used_by: Extra inputs are not permitted");
    assert.strictEqual((await withKey("a".repeat(64))).detail, `${"a".repeat(64)}: Extra inputs are not permitted`);
    for (const key of ["https://alice:s3cret@s4.internal", "bad key", "a".repeat(65), "x\n", "9lives"]) {
        assert.strictEqual(
            (await withKey(key)).detail, "<unknown field>: Extra inputs are not permitted", JSON.stringify(key.substring(0, 20))
        );
    }
});

QUnit.test("a payload without the two flags is stored with the defaults", async function (assert) {
    const { user_context, enabled, ...withoutFlags } = VALID_INPUT;
    void [user_context, enabled];

    const created = await new AdminService().createODataService(
        { ...withoutFlags, name: "suppliers" } as ODataServiceInput
    );
    assert.strictEqual(created.user_context, false, "technical user");
    assert.strictEqual(created.enabled, true, "enabled");
});

QUnit.test("the one-line rule accepts a no-break space and strips title and purpose first", async function (assert) {
    const service = new AdminService();

    for (let i = 0; i < ONE_LINE_ACCEPTED.length; i++) {
        const created = await service.createODataService({ ...VALID_INPUT, ...ONE_LINE_ACCEPTED[i], name: `ok-${i}` });
        assert.strictEqual(created.name, `ok-${i}`, JSON.stringify(ONE_LINE_ACCEPTED[i]));
    }
});

QUnit.test("every boolean inside a definition is strict", async function (assert) {
    const service = new AdminService();

    const field = await refusal(() => service.createODataService(definitionWith({
        fields: [{ ...HIDDEN, name: "Id", selectable: "true" }]
    })));
    assert.strictEqual(field.status, 422);
    assert.strictEqual(
        field.detail, "definition.entity_sets.0.fields.0.selectable: Input should be a valid boolean",
        "a refused field keeps the entity set's own rules from being checked"
    );

    const input = definitionWith({
        navigations: [{ name: "to_Item", target: "A_Item", collection: 1, description: "" }]
    });
    (input.definition.operations as unknown[]).push({
        name: "Release", qualified_name: "", title: "", kind: "function_import", http_method: "POST",
        bound_to: null, parameters: [{ name: "Code", type: "Edm.String", required: "true" }],
        description: "", enabled: "false", changes_data: 0
    });
    const many = await refusal(() => service.createODataService(input));
    assert.strictEqual(
        many.detail,
        "definition.entity_sets.0.navigations.0.collection: Input should be a valid boolean; "
        + "definition.operations.0.parameters.0.required: Input should be a valid boolean; "
        + "definition.operations.0.enabled: Input should be a valid boolean; "
        + "definition.operations.0.changes_data: Input should be a valid boolean"
    );
});

QUnit.test("key and navigation names are unique within an entity set", async function (assert) {
    const service = new AdminService();
    const key = { name: "Id", type: "Edm.String" };
    const navigation = { name: "to_Item", target: "A_Item", collection: true, description: "" };

    const keys = await refusal(() => service.createODataService(definitionWith({ keys: [key, key] })));
    assert.strictEqual(keys.status, 422);
    assert.strictEqual(
        keys.detail, "definition.entity_sets.0: Value error, duplicate key 'Id' in entity set 'A_Item'"
    );
    const navigations = await refusal(() => service.createODataService(
        definitionWith({ navigations: [navigation, { ...navigation, collection: false }] })
    ));
    assert.strictEqual(
        navigations.detail,
        "definition.entity_sets.0: Value error, duplicate navigation 'to_Item' in entity set 'A_Item'"
    );
    const both = await refusal(() => service.createODataService(
        definitionWith({ keys: [key, key], navigations: [navigation, navigation] })
    ));
    assert.strictEqual(
        both.detail, "definition.entity_sets.0: Value error, duplicate navigation 'to_Item' in entity set 'A_Item'",
        "in the server's order: fields, navigations, keys"
    );
});

QUnit.test("a name that cannot be a service name is the same 404 as an unknown one", async function (assert) {
    const service = new AdminService();
    const calls: (() => Promise<unknown>)[] = [
        () => service.getODataService("Not A Slug"),
        () => service.updateODataService("Not A Slug", { ...VALID_INPUT, title: " " }),
        () => service.deleteODataService("Not A Slug"),
        () => service.duplicateODataService("Not A Slug", { name: "Also Bad" }),
        () => service.testODataService("Not A Slug")
    ];

    for (const call of calls) {
        const error = await refusal(call);
        assert.strictEqual(error.status, 404, "404 before the body is looked at");
        assert.strictEqual(error.detail, "Service not found");
    }
});

QUnit.test("duplicate refuses a body the server refuses", async function (assert) {
    const service = new AdminService();
    const duplicate = (body: Record<string, unknown>) => refusal(
        () => service.duplicateODataService("purchase-requisitions", body as never)
    );

    assert.strictEqual(
        (await duplicate({ name: "Not A Slug" })).detail,
        "name: String should match pattern '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'"
    );
    assert.strictEqual((await duplicate({})).detail, "name: Field required");
    assert.strictEqual((await duplicate({ name: "copy", title: "  " })).detail,
        "title: String should have at least 1 character");
    assert.strictEqual((await duplicate({ name: "copy", destination: "a b" })).detail,
        "destination: String should match pattern '^[A-Za-z0-9_.-]{1,200}$'");
    assert.strictEqual((await duplicate({ name: "copy", user_context: "true" })).detail,
        "user_context: Input should be a valid boolean");
    assert.strictEqual((await duplicate({ name: "copy", definition: {} })).detail,
        "definition: Extra inputs are not permitted");
    for (const body of [{ name: "Not A Slug" }, {}]) {
        assert.strictEqual((await duplicate(body)).status, 422);
    }
    assert.strictEqual((await service.listODataServices()).length, 4, "no copy was stored");

    const copy = await service.duplicateODataService(
        "purchase-requisitions", { name: "copy", title: " Requisitions for jobs " }
    );
    assert.strictEqual(copy.title, "Requisitions for jobs", "a given title is stripped and used as is");
});

// --- the destinations a service can name -------------------------------------

QUnit.test("the destinations are read once from odata/destinations, as the server describes them", async function (this: { backend: FakeBackend }, assert) {
    const list = await new AdminService().listODataDestinations();

    assert.deepEqual(this.backend.requests, ["GET odata/destinations"]);
    assert.deepEqual(list.items.map((item) => item.name), [
        "S4 DEV invalid", "S4_DEV", "S4_DEV_BASIC", "S4_DEV_RFC", "S4_DEV_USER", "S4_ODATA_TECH", "S4_ODATA_USER"
    ], "sorted by name, case-insensitively");
    assert.deepEqual(
        list.items.filter((item) => item.user_propagating).map((item) => item.name), ["S4_DEV_USER", "S4_ODATA_USER"]
    );
    assert.deepEqual(
        list.items.filter((item) => !item.usable).map((item) => [item.name, item.reason]),
        [["S4 DEV invalid", "invalid_name"], ["S4_DEV_RFC", "not_http"]]
    );
    assert.deepEqual(
        list.items.filter((item) => item.proxy_type === "OnPremise").map((item) => [item.name, item.notes]),
        [["S4_ODATA_TECH", ["on_premise"]], ["S4_ODATA_USER", ["on_premise"]]]
    );
    assert.deepEqual(
        [list.truncated, list.skipped, list.warnings], [false, 0, []], "a complete list"
    );
    assert.deepEqual(Object.keys(list.items[0]).sort(), [
        "authentication", "description", "level", "name", "notes", "proxy_type", "reason", "shadows_subaccount",
        "type", "usable", "user_propagating"
    ], "the eleven fields of the contract and nothing else");
});

QUnit.test("no list at all is a rejection with the status, never an empty list", async function (this: { backend: FakeBackend }, assert) {
    this.backend.destinationsMode = "unavailable";

    const error = await refusal(() => new AdminService().listODataDestinations());

    assert.ok(error instanceof AdminError);
    assert.strictEqual(error.status, 503);
    assert.strictEqual(
        error.detail,
        "no destination service is bound to this application, so its destinations cannot be listed; "
        + "type the destination name instead"
    );
});

QUnit.test("a cut list and a list of one level say so", async function (this: { backend: FakeBackend }, assert) {
    this.backend.destinationsMode = "truncated";
    const cut = await new AdminService().listODataDestinations();
    assert.strictEqual(cut.truncated, true);
    assert.ok(cut.items.length > 0, "what was read is still listed");

    this.backend.destinationsMode = "partial";
    const partial = await new AdminService().listODataDestinations();
    assert.strictEqual(partial.truncated, false);
    assert.deepEqual(partial.warnings.map((w) => [w.code, w.level]), [["level_unavailable", "subaccount"]]);
    assert.deepEqual(partial.items.map((item) => item.level), ["instance"], "the level that answered");
});

QUnit.test("the destinations route takes no query parameter", async function (assert) {
    const response = await fetch("backend/odata/destinations?level=subaccount");

    assert.strictEqual(response.status, 422);
    assert.deepEqual(await response.json(), { detail: "query: this route takes no parameters" });
});

QUnit.test("getNotifications GETs the relative notifications path", async function (assert) {
    const calls: string[][] = [];
    const body = { items: [], unread_count: 0, seen_at: "2026-10-09T11:00:00+00:00" };
    stubFetch(200, body, calls);

    const list = await new AdminService().getNotifications();

    assert.strictEqual(calls[0][0], "backend/notifications");
    assert.strictEqual(calls[0][1], "GET");
    assert.deepEqual(list, body, "returns the parsed body");
});

QUnit.test("markNotificationsSeen POSTs up_to and returns the marker", async function (assert) {
    const calls: string[][] = [];
    stubFetch(200, { seen_at: "2026-10-09T12:00:00+00:00" }, calls);

    const out = await new AdminService().markNotificationsSeen("2026-10-09T12:00:00+00:00");

    assert.strictEqual(calls[0][0], "backend/notifications/seen");
    assert.strictEqual(calls[0][1], "POST");
    assert.deepEqual(JSON.parse(calls[0][2]), { up_to: "2026-10-09T12:00:00+00:00" });
    assert.strictEqual(out.seen_at, "2026-10-09T12:00:00+00:00");
});

QUnit.test("markNotificationsSeen rejects with the fixed 422 text", async function (assert) {
    stubFetch(422, { detail: "up_to must be a timestamp with a time zone" }, []);
    try {
        await new AdminService().markNotificationsSeen("x");
        assert.ok(false, "should reject");
    } catch (e) {
        assert.strictEqual((e as AdminError).status, 422);
        assert.strictEqual((e as AdminError).detail, "up_to must be a timestamp with a time zone");
    }
});
