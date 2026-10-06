import type {
    ImportResult,
    Agent, AgentInput, AgentSaved, AgentWhereUsed, AdminConfig, CredentialHealth, CredentialStatus, ImportPayload,
    JobRun, JobRunDetail, ModelInfo, ODataDestinationList, ODataDuplicateRequest, ODataMetadataPreview,
    ODataMetadataRequest, ODataService, ODataServiceInput, ODataServiceSummary, ODataServiceUpdate, ODataTestResult,
    OrchestratorInfo, ReloadOutcome, ReloadResult, Skill, SkillInput, WhoAmI,
    Workflow, WorkflowDetail, WorkflowInput, WorkflowRun, WorkflowRunDetail
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
    /** The stable code of a refusal, from the `X-OData-Error` header of the
     *  OData routes (`busy`, `user_token_required`, ...); "" without one. */
    public readonly code: string;

    public constructor(status: number, detail: string, fieldErrors: Record<string, string> = {}, code = "") {
        super(detail || `Request failed with status ${status}`);
        this.name = "AdminError";
        this.status = status;
        this.detail = detail;
        this.fieldErrors = fieldErrors;
        this.code = code;
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

    /** The answer of a call that worked; a non-2xx one rejects. */
    private async send(path: string, init?: RequestInit): Promise<Response> {
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
        return response;
    }

    private async request<T>(path: string, init?: RequestInit): Promise<T> {
        const response = await this.send(path, init);
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
        return new AdminError(response.status, detail, fieldErrors, response.headers?.get("X-OData-Error") ?? "");
    }

    /**
     * A DELETE whose 204 says in two headers whether the running agents were
     * reloaded. A header that is missing (an older server) reads as false.
     */
    private async remove(path: string): Promise<ReloadOutcome> {
        const response = await this.send(path, { method: "DELETE" });
        const flag = (name: string) => (response.headers?.get(name) ?? "").trim().toLowerCase() === "true";
        return { reloaded: flag("X-OData-Reloaded"), reload_failed: flag("X-OData-Reload-Failed") };
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
    public upsertAgent(agent: AgentInput, id?: number): Promise<AgentSaved> {
        return id === undefined
            ? this.request<AgentSaved>("agents", { method: "POST", ...AdminService.json(agent) })
            : this.request<AgentSaved>(`agents/${id}`, { method: "PUT", ...AdminService.json(agent) });
    }

    /** Resolves with what the 204 says about the reload of the running agents. */
    public deleteAgent(id: number): Promise<ReloadOutcome> {
        return this.remove(`agents/${id}`);
    }

    public agentCredentials(id: number, principal = ""): Promise<CredentialStatus[]> {
        const query = principal ? `?principal=${encodeURIComponent(principal)}` : "";
        return this.request<CredentialStatus[]>(`agents/${id}/credentials${query}`);
    }

    /**
     * Health of the credentials scheduled runs depend on.
     *
     * Cheap enough to call on every Agents list load: one query per distinct
     * run-as principal, not one per agent.
     */
    public credentialHealth(): Promise<CredentialHealth> {
        return this.request<CredentialHealth>("credential-health");
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

    public importConfig(payload: ImportPayload): Promise<ImportResult> {
        return this.request<ImportResult>("import", { method: "POST", ...AdminService.json(payload) });
    }

    public whoami(): Promise<WhoAmI> {
        return this.request<WhoAmI>("whoami");
    }

    public getConfig(): Promise<AdminConfig> {
        return this.request<AdminConfig>("config");
    }

    // --- Workflows ---------------------------------------------------------
    public listWorkflows(): Promise<Workflow[]> {
        return this.request<Workflow[]>("workflows");
    }

    public getWorkflow(id: number): Promise<WorkflowDetail> {
        return this.request<WorkflowDetail>(`workflows/${id}`);
    }

    /** POSTs when `id` is omitted, PUTs when it is supplied. */
    public upsertWorkflow(workflow: WorkflowInput, id?: number): Promise<WorkflowDetail> {
        return id === undefined
            ? this.request<WorkflowDetail>("workflows", { method: "POST", ...AdminService.json(workflow) })
            : this.request<WorkflowDetail>(`workflows/${id}`, { method: "PUT", ...AdminService.json(workflow) });
    }

    public deleteWorkflow(id: number): Promise<void> {
        return this.request<void>(`workflows/${id}`, { method: "DELETE" });
    }

    public runWorkflowNow(id: number): Promise<{ run_id: string }> {
        return this.request<{ run_id: string }>(`workflows/${id}/run`, { method: "POST" });
    }

    public listWorkflowRuns(opts: { workflowId?: number; limit?: number } = {}): Promise<WorkflowRun[]> {
        const params = new URLSearchParams();
        if (opts.workflowId !== undefined) {
            params.set("workflow_id", String(opts.workflowId));
        }
        params.set("limit", String(opts.limit ?? 50));
        return this.request<WorkflowRun[]>(`workflow-runs?${params.toString()}`);
    }

    public getWorkflowRun(runId: string): Promise<WorkflowRunDetail> {
        return this.request<WorkflowRunDetail>(`workflow-runs/${encodeURIComponent(runId)}`);
    }

    // --- where used ---
    /** Who refers to an agent: peers that list it and workflows that run it,
     * disabled ones included and flagged. */
    public getAgentWhereUsed(id: number): Promise<AgentWhereUsed> {
        return this.request<AgentWhereUsed>(`agents/${id}/where-used`);
    }

    // --- odata ---
    // The catalogue of OData services (`/admin/api/odata/...`). A service is
    // addressed by its name, the slug agents use, not by an id.
    private static odataServicePath(name: string): string {
        return `odata/services/${encodeURIComponent(name)}`;
    }

    /** The list carries no definitions; `getODataService` does. */
    public listODataServices(): Promise<ODataServiceSummary[]> {
        return this.request<ODataServiceSummary[]>("odata/services");
    }

    public getODataService(name: string): Promise<ODataService> {
        return this.request<ODataService>(AdminService.odataServicePath(name));
    }

    public createODataService(input: ODataServiceInput): Promise<ODataService> {
        return this.request<ODataService>("odata/services", { method: "POST", ...AdminService.json(input) });
    }

    /** The name is immutable: `input.name` must be `name`, or the server
     * answers 422. With `expected_updated_at`, a service that was changed
     * since that moment is not overwritten: 409. */
    public updateODataService(name: string, input: ODataServiceUpdate): Promise<ODataService> {
        return this.request<ODataService>(AdminService.odataServicePath(name), {
            method: "PUT", ...AdminService.json(input)
        });
    }

    /** Rejects with a 409 while an agent, enabled or not, uses the service.
     *  Resolves with what the 204 says about the reload of the running agents. */
    public deleteODataService(name: string): Promise<ReloadOutcome> {
        return this.remove(AdminService.odataServicePath(name));
    }

    /** A copy with the same definition under another name, e.g. the same
     * service through a technical-user destination for jobs. */
    public duplicateODataService(name: string, body: ODataDuplicateRequest): Promise<ODataService> {
        return this.request<ODataService>(`${AdminService.odataServicePath(name)}/duplicate`, {
            method: "POST", ...AdminService.json(body)
        });
    }

    /**
     * The destinations a service can name, read from the destination
     * service on every call (the server caches nothing). Rejects when there
     * is no list at all (503, 502, 504): the name can then only be typed.
     * The route takes no parameter.
     */
    public listODataDestinations(): Promise<ODataDestinationList> {
        return this.request<ODataDestinationList>("odata/destinations");
    }

    /** Reads a service's $metadata through its destination. Stores nothing. */
    public readODataMetadata(body: ODataMetadataRequest): Promise<ODataMetadataPreview> {
        return this.request<ODataMetadataPreview>("odata/metadata", { method: "POST", ...AdminService.json(body) });
    }

    /** One read of one row through the stored service; the answer says
     * whether it worked and as whom, never what was read. */
    public testODataService(name: string): Promise<ODataTestResult> {
        return this.request<ODataTestResult>(`${AdminService.odataServicePath(name)}/test`, {
            method: "POST", ...AdminService.json({})
        });
    }
}
