import type {
    Agent, CredentialStatus, JobRunDetail, Skill, WorkflowDetail, WorkflowRunDetail
} from "com/agent/admin/service/types";
import { DEEP_DEFAULTS } from "com/agent/admin/service/types";

/** What `FakeBackend#failNext` accepts: the next call to `path` answers with `body`/`status` instead. */
export interface FailNext { path: string; status: number; body: unknown }

/**
 * An in-memory stand-in for /admin/api, installed over `window.fetch`.
 *
 * Deliberately at the network boundary rather than at AdminService: the real
 * service, its error mapping and every controller then run unchanged, which is
 * what makes these journeys evidence of parity rather than of mocking.
 */
export default class FakeBackend {

    public agents: Agent[] = [];
    public skills: Skill[] = [];
    public runs: JobRunDetail[] = [];
    public workflows: WorkflowDetail[] = [];
    public workflowRuns: WorkflowRunDetail[] = [];
    /** Credential rows served for agent 100 — one server per token state. */
    public credentials: CredentialStatus[] = [];
    /** Set to force the next matching call to fail. */
    public failNext?: FailNext;
    /** Every intercepted call as "METHOD path" (query string dropped), in
     * order, so a journey can assert that something was -- or was no
     * longer -- requested. */
    public requests: string[] = [];
    /** The paths of the POST .../run calls, in order. */
    public runNowCalls: string[] = [];

    private originalFetch?: typeof fetch;
    private nextId = 100;

    /**
     * Only intercepts `backend/*` calls (AdminService's own prefix) and lets
     * everything else through to the real fetch(). window.fetch is global,
     * and UI5's own resource loading (Fragment.load in particular) also uses
     * it to fetch .fragment.xml files; answering those with this class's
     * catch-all 404 JSON silently breaks Fragment.load with no visible error
     * (its caller never awaits it), so the MCP server dialog it builds would
     * simply never appear.
     */
    public install(): void {
        this.originalFetch = window.fetch;
        const original = this.originalFetch;
        window.fetch = ((input: string, init?: RequestInit) => {
            if (/^backend\//.test(String(input))) {
                return this.handle(String(input), init);
            }
            return original(input, init);
        }) as unknown as typeof fetch;
    }

    public restore(): void {
        if (this.originalFetch) {
            window.fetch = this.originalFetch;
        }
    }

    public reset(): void {
        // Without this, ids drift across tests: nextId is a field on this
        // singleton instance, so any agent/skill a PRIOR test created (e.g.
        // the "new-agent" POST in AgentJourney's first test) permanently
        // shifts every id assigned afterwards -- silently breaking any test
        // that opens a fixture by a hardcoded id/hash, and `this.runs` below
        // already assumes btp-agent is id 100.
        this.nextId = 100;
        this.requests = [];
        this.runNowCalls = [];
        this.agents = [this.makeAgent("btp-agent"), this.makeAgent("gmail-agent")];
        // btp-agent is exposed as a job API, so its Run now button is live
        // on the list and detail pages; gmail-agent is not.
        this.agents[0].expose_api = true;
        this.agents[0].api_slug = "btp-agent";
        // btp-agent carries a peer and a model override that IS in the fake
        // GET /model list below, so it exercises the ordinary populate-on-load
        // path. gmail-agent's override is deliberately NOT in that list, so it
        // exercises the "stored override the fetch didn't return" path.
        this.agents[0].peers = ["gmail-agent"];
        this.agents[0].model_name = "gpt-4o";
        this.agents[1].model_name = "retired-model";
        // --- destinations --- gmail-agent also reaches a remote MCP server
        // through a user-propagating destination, so a journey can check that
        // a round-trip edit keeps acting as the signed-in user.
        this.agents[1].mcp_servers = this.agents[1].mcp_servers.concat([{
            url: "https://arc1.example.com/mcp", auth_mode: "destination",
            oauth: { destination: "arc1-abap-readonly", user_context: true }
        }]);
        // --- deep agents --- btp-agent carries a non-default config so a
        // journey can check it is shown and resent unchanged; gmail-agent
        // keeps the defaults (GET always returns the object).
        this.agents[0].deep = {
            enabled: true, planning: true, scratchpad: false, subagents: true,
            max_subagents: 3, subagent_max_depth: 2, subagent_instructions: "Be brief."
        };
        // One row per token state, so a journey can assert that the sign-in
        // button is offered for a working credential as well as a dead one.
        this.credentials = [
            {
                url: "https://a.hana.ondemand.com/mcp", auth_mode: "oauth2",
                needs_token: true, has_token: true, token_state: "valid",
                expires_at: "2099-01-01T00:00:00+00:00",
                login_url: "/oauth/login?agent=btp-agent&server=a", no_user_token: false
            },
            {
                url: "https://b.hana.ondemand.com/mcp", auth_mode: "oauth2",
                needs_token: true, has_token: false, token_state: "expired",
                expires_at: "2020-01-01T00:00:00+00:00",
                login_url: "/oauth/login?agent=btp-agent&server=b", no_user_token: false
            },
            {
                url: "builtin:jira", auth_mode: "destination",
                needs_token: false, has_token: true, token_state: "valid",
                expires_at: null, login_url: "", no_user_token: true
            }
        ];
        this.skills = [{
            id: 1, name: "sap-notes", description: "How to read SAP notes",
            content: "Full instructions here.", created_at: null, updated_at: null
        }];
        // Newest first, as the server lists them. run-1 (finished, with a
        // report) stays first: RunReportJourney opens the first row. run-2
        // is still running, for the auto-refresh journeys; run-3 belongs to
        // the other agent, so a per-agent filter can be told from no filter.
        this.runs = [
            {
                id: "run-1", agent_id: 100, agent_name: "btp-agent", trigger: "manual",
                status: "success", started_at: "2026-08-24T10:00:00",
                finished_at: "2026-08-24T10:01:30", summary: "All good", error: null,
                notified: false, created_by: "tester",
                report: { body_md: "# Report\n\n| a | b |\n|---|---|\n| 1 | 2 |" }
            },
            {
                id: "run-2", agent_id: 100, agent_name: "btp-agent", trigger: "scheduler",
                status: "running", started_at: "2026-08-24T09:00:00",
                finished_at: null, summary: null, error: null,
                notified: false, created_by: "scheduler", report: null
            },
            {
                id: "run-3", agent_id: 101, agent_name: "gmail-agent", trigger: "manual",
                status: "failed", started_at: "2026-08-23T10:00:00",
                finished_at: "2026-08-23T10:00:10", summary: "Mailbox unreachable", error: "timeout",
                notified: false, created_by: "tester", report: null
            }
        ];
        // One main-line fan-out step, plus two branches -- "support" has two
        // steps, "billing" has one -- so a journey can assert against a
        // branch with more than one step without inventing its own fixture.
        this.workflows = [this.makeWorkflow("triage-inbox")];
        this.workflows[0].description = "Triages inbound mail and routes it by topic.";
        this.workflows[0].branches = [
            { key: "billing", description: "Billing questions", position: 1 },
            { key: "support", description: "Support requests", position: 2 }
        ];
        this.workflows[0].steps = [
            {
                branch_key: null, position: 1, agent_name: "gmail-agent",
                instructions: "Read new mail.", fan_out: true, step_timeout_seconds: 600
            },
            {
                branch_key: "billing", position: 1, agent_name: "btp-agent",
                instructions: "Draft a billing reply.", fan_out: false, step_timeout_seconds: 600
            },
            {
                branch_key: "support", position: 1, agent_name: "btp-agent",
                instructions: "Draft a support reply.", fan_out: false, step_timeout_seconds: 600
            },
            {
                branch_key: "support", position: 2, agent_name: "btp-agent",
                instructions: "Send the reply.", fan_out: false, step_timeout_seconds: 600
            }
        ];
        // wf-run-1 is finished with items and steps; wf-run-2 is still
        // running with nothing produced yet, for the auto-refresh journeys.
        this.workflowRuns = [{
            run: {
                id: "wf-run-1", workflow_id: this.workflows[0].id, workflow_name: "triage-inbox",
                trigger: "manual", status: "success",
                started_at: "2026-08-24T10:00:00", finished_at: "2026-08-24T10:05:00",
                items_total: 2, items_succeeded: 1, items_failed: 0, items_skipped: 1,
                summary: "Processed 2 items.", error: null, created_by: "tester"
            },
            items: [
                {
                    id: "item-1", workflow_run_id: "wf-run-1", item_key: "msg-1",
                    title: "Invoice question", branches: ["billing"], status: "success",
                    error: null, started_at: "2026-08-24T10:00:05", finished_at: "2026-08-24T10:02:00"
                },
                {
                    id: "item-2", workflow_run_id: "wf-run-1", item_key: "msg-2",
                    title: "Already handled", branches: [], status: "skipped",
                    error: null, started_at: "2026-08-24T10:00:05", finished_at: "2026-08-24T10:00:05"
                }
            ],
            steps: [
                {
                    id: "step-1", workflow_run_id: "wf-run-1", item_run_id: null, branch_key: null,
                    position: 1, agent_name: "gmail-agent", status: "success",
                    output: "Found 2 items.", error: null,
                    started_at: "2026-08-24T10:00:00", finished_at: "2026-08-24T10:00:05"
                },
                {
                    id: "step-2", workflow_run_id: "wf-run-1", item_run_id: "item-1",
                    branch_key: "billing", position: 1, agent_name: "btp-agent", status: "success",
                    output: "Drafted a billing reply.", error: null,
                    started_at: "2026-08-24T10:00:05", finished_at: "2026-08-24T10:02:00"
                }
            ]
        }, {
            run: {
                id: "wf-run-2", workflow_id: this.workflows[0].id, workflow_name: "triage-inbox",
                trigger: "scheduler", status: "running",
                started_at: "2026-08-25T08:00:00", finished_at: null,
                items_total: 0, items_succeeded: 0, items_failed: 0, items_skipped: 0,
                summary: null, error: null, created_by: "scheduler"
            },
            items: [],
            steps: []
        }];
        this.failNext = undefined;
    }

    /** Flips a job run to finished, the way the runner would between two
     * polls of the detail page. */
    public finishRun(id: string, status: JobRunDetail["status"] = "success"): void {
        const run = this.runs.find((r) => r.id === id);
        if (run) {
            run.status = status;
            run.finished_at = "2026-08-25T08:00:42";
            run.summary = "Done.";
        }
    }

    /** Flips a workflow run to finished and gives it one succeeded item with
     * one step, so a poll has items and steps to pick up as well. */
    public finishWorkflowRun(id: string, status: WorkflowRunDetail["run"]["status"] = "success"): void {
        const detail = this.workflowRuns.find((r) => r.run.id === id);
        if (!detail) {
            return;
        }
        detail.run.status = status;
        detail.run.finished_at = "2026-08-25T08:00:42";
        detail.run.items_total = 1;
        detail.run.items_succeeded = 1;
        detail.run.summary = "Processed 1 item.";
        detail.items = [{
            id: `${id}-item-1`, workflow_run_id: id, item_key: "msg-9",
            title: "Late mail", branches: ["billing"], status: "success",
            error: null, started_at: "2026-08-25T08:00:05", finished_at: "2026-08-25T08:00:40"
        }];
        detail.steps = [{
            id: `${id}-step-1`, workflow_run_id: id, item_run_id: `${id}-item-1`,
            branch_key: "billing", position: 1, agent_name: "btp-agent", status: "success",
            output: "Drafted.", error: null,
            started_at: "2026-08-25T08:00:05", finished_at: "2026-08-25T08:00:40"
        }];
    }

    /** How many times "METHOD path" was requested so far. */
    public countRequests(entry: string): number {
        return this.requests.filter((r) => r === entry).length;
    }

    private static queryOf(url: string): URLSearchParams {
        const index = url.indexOf("?");
        return new URLSearchParams(index === -1 ? "" : url.substring(index + 1));
    }

    private makeAgent(name: string): Agent {
        return {
            id: this.nextId++, name, description: `${name} description`,
            instructions: "Do the thing.",
            mcp_servers: [{ url: "https://x.hana.ondemand.com/mcp", auth_mode: "jwt" }],
            skills: [], enabled: true, expose_chat: true, expose_api: false,
            api_slug: "", run_as_principal: "", run_prompt: "",
            run_timeout_seconds: 1800, peers: [], model_name: "",
            deep: { ...DEEP_DEFAULTS },
            created_at: null, updated_at: null,
            mcp_url: "", auth_mode: "jwt"
        };
    }

    private makeWorkflow(name: string): WorkflowDetail {
        return {
            id: this.nextId++, name, description: `${name} description`,
            api_slug: "", run_as_principal: "", run_timeout_seconds: 1800,
            skip_seen_items: true, max_parallel_items: 1, on_unknown_branch: "fail",
            enabled: true, branches: [], steps: []
        };
    }

    private json(body: unknown, status = 200): Promise<Response> {
        return Promise.resolve(new Response(JSON.stringify(body), {
            status, headers: { "Content-Type": "application/json" }
        }));
    }

    /** Both delete endpoints really return 204; the fake must too, or the
     *  journeys would pass against behaviour the server does not have. */
    private noContent(): Promise<Response> {
        return Promise.resolve(new Response(null, { status: 204 }));
    }

    private handle(url: string, init?: RequestInit): Promise<Response> {
        const path = url.replace(/^backend\//, "").split("?")[0];
        const query = FakeBackend.queryOf(url);
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(init.body as string) as Record<string, unknown> : undefined;
        this.requests.push(`${method} ${path}`);

        if (this.failNext && this.failNext.path === path) {
            const failure = this.failNext;
            this.failNext = undefined;
            return this.json(failure.body, failure.status);
        }

        if (path === "whoami") {
            return this.json({ principal: "uuid-1234", label: "tester@example.com" });
        }
        if (path === "config") {
            return this.json({ public_base_url: "https://backend.example.test" });
        }
        if (path === "agents" && method === "GET") {
            return this.json(this.agents);
        }
        if (path === "agents" && method === "POST") {
            const created = { ...this.makeAgent(String(body?.name)), ...body, id: this.nextId++ } as Agent;
            this.agents.push(created);
            return this.json(created, 201);
        }
        if (/^agents\/\d+$/.test(path)) {
            const id = Number(path.split("/")[1]);
            const index = this.agents.findIndex((a) => a.id === id);
            if (method === "GET") {
                return this.json(this.agents[index]);
            }
            if (method === "PUT") {
                this.agents[index] = { ...this.agents[index], ...body } as Agent;
                return this.json(this.agents[index]);
            }
            if (method === "DELETE") {
                this.agents.splice(index, 1);
                return this.noContent();
            }
        }
        if (/^agents\/\d+\/run$/.test(path) && method === "POST") {
            // Like the server: acknowledge at once and record a running run,
            // which the next list call then shows.
            const agent = this.agents.find((a) => a.id === Number(path.split("/")[1]));
            if (!agent) {
                return this.json({ detail: "Agent not found" }, 404);
            }
            this.runNowCalls.push(path);
            const runId = `run-${this.nextId++}`;
            this.runs.unshift({
                id: runId, agent_id: agent.id, agent_name: agent.name, trigger: "manual",
                status: "running", started_at: new Date().toISOString(), finished_at: null,
                summary: null, error: null, notified: false, created_by: "tester", report: null
            });
            return this.json({ run_id: runId });
        }
        if (/^agents\/\d+\/credentials$/.test(path)) {
            return this.json(Number(path.split("/")[1]) === 100 ? this.credentials : []);
        }
        if (path === "skills" && method === "GET") {
            return this.json(this.skills);
        }
        if (path === "skills" && method === "POST") {
            const created = { ...body, id: this.nextId++, created_at: null, updated_at: null } as Skill;
            this.skills.push(created);
            return this.json(created, 201);
        }
        if (/^skills\/\d+$/.test(path)) {
            const id = Number(path.split("/")[1]);
            const index = this.skills.findIndex((s) => s.id === id);
            if (method === "GET") {
                return this.json(this.skills[index]);
            }
            if (method === "PUT") {
                this.skills[index] = { ...this.skills[index], ...body } as Skill;
                return this.json(this.skills[index]);
            }
            if (method === "DELETE") {
                this.skills.splice(index, 1);
                return this.noContent();
            }
        }
        if (path === "runs" && method === "GET") {
            // Filtered and capped the way GET /admin/api/runs is.
            const agentId = query.get("agent_id");
            const limit = Number(query.get("limit") ?? 50);
            const runs = this.runs.filter((r) => agentId === null || r.agent_id === Number(agentId));
            return this.json(runs.slice(0, limit));
        }
        if (/^runs\/[^/]+$/.test(path)) {
            const id = path.split("/")[1];
            const run = this.runs.find((r) => r.id === id);
            return run ? this.json(run) : this.json({ detail: "Run not found" }, 404);
        }
        if (path === "workflows" && method === "GET") {
            // Mirrors Workflow.to_dict(): the list carries no branches/steps.
            return this.json(this.workflows.map(({ branches, steps, ...rest }) => rest));
        }
        if (path === "workflows" && method === "POST") {
            const created = {
                ...this.makeWorkflow(String(body?.name)), ...body, id: this.nextId++
            } as WorkflowDetail;
            this.workflows.push(created);
            return this.json(created, 201);
        }
        if (/^workflows\/\d+$/.test(path)) {
            const id = Number(path.split("/")[1]);
            const index = this.workflows.findIndex((w) => w.id === id);
            if (method === "GET") {
                return this.json(this.workflows[index]);
            }
            if (method === "PUT") {
                this.workflows[index] = { ...this.workflows[index], ...body } as WorkflowDetail;
                return this.json(this.workflows[index]);
            }
            if (method === "DELETE") {
                this.workflows.splice(index, 1);
                return this.noContent();
            }
        }
        if (/^workflows\/\d+\/run$/.test(path)) {
            const workflow = this.workflows.find((w) => w.id === Number(path.split("/")[1]));
            this.runNowCalls.push(path);
            const runId = `wf-run-${this.nextId++}`;
            if (workflow) {
                this.workflowRuns.unshift({
                    run: {
                        id: runId, workflow_id: workflow.id, workflow_name: workflow.name,
                        trigger: "manual", status: "running",
                        started_at: new Date().toISOString(), finished_at: null,
                        items_total: 0, items_succeeded: 0, items_failed: 0, items_skipped: 0,
                        summary: null, error: null, created_by: "tester"
                    },
                    items: [],
                    steps: []
                });
            }
            return this.json({ run_id: runId });
        }
        if (path === "workflow-runs" && method === "GET") {
            const workflowId = query.get("workflow_id");
            const limit = Number(query.get("limit") ?? 50);
            const runs = this.workflowRuns
                .map((r) => r.run)
                .filter((r) => workflowId === null || r.workflow_id === Number(workflowId));
            return this.json(runs.slice(0, limit));
        }
        if (/^workflow-runs\/[^/]+$/.test(path)) {
            const id = path.split("/")[1];
            return this.json(this.workflowRuns.find((r) => r.run.id === id) ?? this.workflowRuns[0]);
        }
        if (path === "reload" || path === "restart") {
            return this.json({ status: "ok" });
        }
        if (path === "model" && method === "GET") {
            return this.json({ model_name: "gpt-4o", available: ["gpt-4o", "claude-opus-5"] });
        }
        if (path === "model" && method === "PUT") {
            return this.json({ model_name: String(body?.model_name), available: ["gpt-4o", "claude-opus-5"] });
        }
        if (path === "orchestrator" && method === "GET") {
            return this.json({ instructions: "Delegate wisely." });
        }
        if (path === "orchestrator" && method === "PUT") {
            return this.json({ instructions: String(body?.instructions) });
        }
        if (path === "export") {
            return this.json({ agents: [], skills: [], orchestrator_instructions: "", replace: false });
        }
        if (path === "import") {
            return this.json({ status: "ok" });
        }
        // --- where used ---
        // Derived from the agents and workflows above the way the server
        // derives it from its tables, so a journey that edits a peer list or
        // a step sees the change reflected here without a second fixture.
        if (/^agents\/\d+\/where-used$/.test(path)) {
            const id = Number(path.split("/")[1]);
            const agent = this.agents.find((a) => a.id === id);
            if (!agent) {
                return this.json({ detail: "Agent not found" }, 404);
            }
            return this.json({
                agent: { id: agent.id, name: agent.name },
                peers: this.agents
                    .filter((a) => a.id !== agent.id && (a.peers ?? []).indexOf(agent.name) !== -1)
                    .map((a) => ({ id: a.id, name: a.name, enabled: a.enabled })),
                workflows: this.workflows
                    .filter((w) => w.steps.some((s) => s.agent_name === agent.name))
                    .map((w) => ({
                        id: w.id, name: w.name, api_slug: w.api_slug || null, enabled: w.enabled,
                        steps: w.steps
                            .filter((s) => s.agent_name === agent.name)
                            .map((s) => ({ position: s.position, branch_key: s.branch_key }))
                    }))
            });
        }
        return this.json({ detail: `unhandled ${method} ${path}` }, 404);
    }
}
