import type { Agent, JobRunDetail, Skill } from "com/infrabel/agentadmin/service/types";

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
    /** Set to force the next matching call to fail. */
    public failNext?: FailNext;

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
        this.agents = [this.makeAgent("btp-agent"), this.makeAgent("gmail-agent")];
        this.skills = [{
            id: 1, name: "sap-notes", description: "How to read SAP notes",
            content: "Full instructions here.", created_at: null, updated_at: null
        }];
        this.runs = [{
            id: "run-1", agent_id: 100, agent_name: "btp-agent", trigger: "manual",
            status: "success", started_at: "2026-08-24T10:00:00",
            finished_at: "2026-08-24T10:01:30", summary: "All good", error: null,
            notified: false, created_by: "tester",
            report: { body_md: "# Report\n\n| a | b |\n|---|---|\n| 1 | 2 |" }
        }];
        this.failNext = undefined;
    }

    private makeAgent(name: string): Agent {
        return {
            id: this.nextId++, name, description: `${name} description`,
            instructions: "Do the thing.",
            mcp_servers: [{ url: "https://x.hana.ondemand.com/mcp", auth_mode: "jwt" }],
            skills: [], enabled: true, expose_chat: true, expose_api: false,
            api_slug: "", run_as_principal: "", run_prompt: "",
            run_timeout_seconds: 1800, created_at: null, updated_at: null,
            mcp_url: "", auth_mode: "jwt"
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
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(init.body as string) as Record<string, unknown> : undefined;

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
        if (/^agents\/\d+\/credentials$/.test(path)) {
            return this.json([]);
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
            return this.json(this.runs);
        }
        if (/^runs\/[^/]+$/.test(path)) {
            return this.json(this.runs[0]);
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
        return this.json({ detail: `unhandled ${method} ${path}` }, 404);
    }
}
