import AdminService, { AdminError } from "com/infrabel/agentadmin/service/AdminService";

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
