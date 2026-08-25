import type {
    Agent, AgentInput, AdminConfig, CredentialStatus, ImportPayload,
    JobRun, JobRunDetail, ModelInfo, OrchestratorInfo, ReloadResult, Skill, SkillInput, WhoAmI
} from "./types";

/**
 * A non-2xx response from the admin API.
 *
 * `fieldErrors` flattens FastAPI's 422 `detail` array into dotted keys
 * (`mcp_servers.0.url`) so a form can attach each message to its control.
 */
export class AdminError extends Error {
    public readonly status: number;
    public readonly detail: string;
    public readonly fieldErrors: Record<string, string>;

    public constructor(status: number, detail: string, fieldErrors: Record<string, string> = {}) {
        super(detail || `Request failed with status ${status}`);
        this.name = "AdminError";
        this.status = status;
        this.detail = detail;
        this.fieldErrors = fieldErrors;
    }
}

interface ValidationItem { loc?: (string | number)[]; msg?: string }

/**
 * The only class in the application that performs HTTP.
 *
 * Every path is *relative* (`backend/...`, never `/backend/...`). That is what
 * lets the same build run behind the standalone approuter at `/ui5admin/` and
 * behind a Work Zone site at `/<service>.<app>/` without knowing which.
 */
export default class AdminService {

    private static readonly PREFIX = "backend/";

    private async request<T>(path: string, init?: RequestInit): Promise<T> {
        const response = await fetch(AdminService.PREFIX + path, {
            ...init,
            headers: {
                Accept: "application/json",
                ...(init?.body ? { "Content-Type": "application/json" } : {}),
                ...(init?.headers ?? {})
            }
        });

        if (!response.ok) {
            throw await AdminService.toError(response);
        }
        if (response.status === 204) {
            return undefined as T;
        }
        return await response.json() as T;
    }

    private static async toError(response: Response): Promise<AdminError> {
        let detail = "";
        const fieldErrors: Record<string, string> = {};
        try {
            const body = await response.json() as { detail?: string | ValidationItem[] };
            if (typeof body.detail === "string") {
                detail = body.detail;
            } else if (Array.isArray(body.detail)) {
                // FastAPI 422: loc is ["body", "mcp_servers", 0, "url"].
                // Drop the leading "body" and join the rest into a dotted key.
                body.detail.forEach((item) => {
                    const loc = (item.loc ?? []).filter((p) => p !== "body");
                    const key = loc.join(".");
                    if (key && item.msg) {
                        fieldErrors[key] = item.msg;
                    }
                });
                detail = body.detail.map((i) => i.msg).filter(Boolean).join("; ");
            }
        } catch {
            detail = response.statusText;
        }
        return new AdminError(response.status, detail, fieldErrors);
    }

    private static json(body: unknown): RequestInit {
        return { body: JSON.stringify(body) };
    }

    // --- Agents ----------------------------------------------------------
    public listAgents(): Promise<Agent[]> {
        return this.request<Agent[]>("agents");
    }

    public getAgent(id: number): Promise<Agent> {
        return this.request<Agent>(`agents/${id}`);
    }

    /** POSTs when `id` is omitted, PUTs when it is supplied. */
    public upsertAgent(agent: AgentInput, id?: number): Promise<Agent> {
        return id === undefined
            ? this.request<Agent>("agents", { method: "POST", ...AdminService.json(agent) })
            : this.request<Agent>(`agents/${id}`, { method: "PUT", ...AdminService.json(agent) });
    }

    public deleteAgent(id: number): Promise<void> {
        return this.request<void>(`agents/${id}`, { method: "DELETE" });
    }

    public agentCredentials(id: number, principal = ""): Promise<CredentialStatus[]> {
        const query = principal ? `?principal=${encodeURIComponent(principal)}` : "";
        return this.request<CredentialStatus[]>(`agents/${id}/credentials${query}`);
    }

    public runNow(id: number): Promise<{ run_id: string }> {
        return this.request<{ run_id: string }>(`agents/${id}/run`, { method: "POST" });
    }

    // --- Skills ----------------------------------------------------------
    public listSkills(): Promise<Skill[]> {
        return this.request<Skill[]>("skills");
    }

    public getSkill(id: number): Promise<Skill> {
        return this.request<Skill>(`skills/${id}`);
    }

    public upsertSkill(skill: SkillInput, id?: number): Promise<Skill> {
        return id === undefined
            ? this.request<Skill>("skills", { method: "POST", ...AdminService.json(skill) })
            : this.request<Skill>(`skills/${id}`, { method: "PUT", ...AdminService.json(skill) });
    }

    public deleteSkill(id: number): Promise<void> {
        return this.request<void>(`skills/${id}`, { method: "DELETE" });
    }

    // --- Runs ------------------------------------------------------------
    public listRuns(opts: { agentId?: number; limit?: number } = {}): Promise<JobRun[]> {
        const params = new URLSearchParams();
        if (opts.agentId !== undefined) {
            params.set("agent_id", String(opts.agentId));
        }
        params.set("limit", String(opts.limit ?? 50));
        return this.request<JobRun[]>(`runs?${params.toString()}`);
    }

    public getRun(runId: string): Promise<JobRunDetail> {
        return this.request<JobRunDetail>(`runs/${encodeURIComponent(runId)}`);
    }

    /** Download link for the markdown report; used by an anchor, not fetched. */
    public runReportUrl(runId: string): string {
        return `${AdminService.PREFIX}runs/${encodeURIComponent(runId)}/report.md`;
    }

    // --- Settings --------------------------------------------------------
    public getOrchestrator(): Promise<OrchestratorInfo> {
        return this.request<OrchestratorInfo>("orchestrator");
    }

    public setOrchestrator(instructions: string): Promise<OrchestratorInfo> {
        return this.request<OrchestratorInfo>("orchestrator", {
            method: "PUT", ...AdminService.json({ instructions })
        });
    }

    public getModel(): Promise<ModelInfo> {
        return this.request<ModelInfo>("model");
    }

    public setModel(modelName: string): Promise<ModelInfo> {
        return this.request<ModelInfo>("model", {
            method: "PUT", ...AdminService.json({ model_name: modelName })
        });
    }

    public reload(): Promise<ReloadResult> {
        return this.request<ReloadResult>("reload", { method: "POST" });
    }

    public restart(): Promise<unknown> {
        return this.request<unknown>("restart", { method: "POST" });
    }

    public exportConfig(): Promise<ImportPayload> {
        return this.request<ImportPayload>("export");
    }

    public importConfig(payload: ImportPayload): Promise<unknown> {
        return this.request<unknown>("import", { method: "POST", ...AdminService.json(payload) });
    }

    public whoami(): Promise<WhoAmI> {
        return this.request<WhoAmI>("whoami");
    }

    public getConfig(): Promise<AdminConfig> {
        return this.request<AdminConfig>("config");
    }
}
