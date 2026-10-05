import type {
    Agent, CredentialStatus, JobRunDetail, ODataDefinition, ODataEntityOp, ODataEntitySet, ODataField,
    ODataDestination, ODataDestinationList, ODataMetadataPreview, ODataPreviewEntitySet, ODataService,
    ODataServiceInput, ODataServiceSummary, ODataTestResult, ODataUsedBy, Skill, WorkflowDetail, WorkflowRunDetail
} from "com/agent/admin/service/types";
import { DEEP_DEFAULTS } from "com/agent/admin/service/types";

/** What `FakeBackend#failNext` accepts: the next call to `path` answers with `body`/`status` instead. */
export interface FailNext {
    path: string;
    status: number;
    body: unknown;
    /** Only a call with this method fails; without it, the next call to `path`. */
    method?: string;
    /** No answer at all: the call fails the way `fetch` does when the
     *  server cannot be reached (`status` and `body` are then not used). */
    network?: boolean;
}

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
    // --- odata ---
    /** The OData catalogue. `counts` and `has_write` are recomputed from the
     * definition on every answer, as the server does; `used_by` is kept on
     * the row, and a non-empty one refuses a delete. */
    public odataServices: ODataService[] = [];
    /** What POST odata/metadata answers, whatever the request names. */
    public metadataPreview!: ODataMetadataPreview;
    /** What POST odata/services/{name}/test answers. */
    public testResult!: ODataTestResult;
    /** The destinations GET odata/destinations lists, in any order. */
    public odataDestinations: ODataDestination[] = [];
    /**
     * How GET odata/destinations answers: `ok` -- the whole list;
     * `unavailable` -- no list at all (503 `no_destination_service`);
     * `truncated` -- the first two, with `truncated: true`; `partial` -- the
     * instance level only, with a `level_unavailable` warning for the
     * subaccount; `held` -- no answer until `releaseDestinations()`.
     */
    public destinationsMode: "ok" | "unavailable" | "truncated" | "partial" | "held" = "ok";
    private heldDestinations: (() => void)[] = [];
    /** Set to force the next matching call to fail. */
    public failNext?: FailNext;
    /** Every intercepted call as "METHOD path" (query string dropped), in
     * order, so a journey can assert that something was -- or was no
     * longer -- requested. */
    public requests: string[] = [];
    /** The JSON body of the last call per "<METHOD> <path>". */
    public bodies: Record<string, Record<string, unknown> | undefined> = {};
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
        this.bodies = {};
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
        this.seedOData();
        this.seedDestinations();
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

    /** Answers the GET odata/destinations calls that `held` kept waiting,
     *  the way `mode` says (the list, or no list): all of them, or with
     *  `newestOnly` the last one asked, leaving the earlier ones waiting. */
    public releaseDestinations(mode: "ok" | "unavailable" = "ok", newestOnly = false): void {
        const released = newestOnly ? this.heldDestinations.splice(-1) : this.heldDestinations.splice(0);
        this.destinationsMode = mode;
        released.forEach((release) => release());
        if (this.heldDestinations.length > 0) {
            this.destinationsMode = "held";
        }
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

    // --- odata ---
    /** `named` first, then filler fields up to `total`; the first
     * `selectable` of the whole list are readable and filterable. */
    private static odataFields(
        named: Partial<ODataField>[], selectable: number, total: number, writable: string[] = []
    ): ODataField[] {
        const fields: ODataField[] = [];
        for (let i = 0; i < total; i++) {
            const name = named[i]?.name ?? `Field${String(i + 1).padStart(3, "0")}`;
            fields.push({
                name, type: "Edm.String", label: "", selectable: i < selectable, filterable: i < selectable,
                writable: writable.indexOf(name) !== -1, hint: "", values: [], personal_data: false,
                ...(named[i] ?? {})
            });
        }
        return fields;
    }

    private static odataEntitySet(
        name: string, title: string, description: string, keys: string[],
        operations: ODataEntityOp[], fields: ODataField[]
    ): ODataEntitySet {
        return {
            name, title, path: "", entity_type: `${name}Type`, description,
            keys: keys.map((key) => ({ name: key, type: "Edm.String" })),
            operations, fields, navigations: [], examples: []
        };
    }

    /** The five entity sets of the purchase requisition service. `write`
     * adds the write operations the jobs copy enables. */
    private static requisitionEntitySets(write: boolean): ODataEntitySet[] {
        const itemKeys = ["PurchaseRequisition", "PurchaseRequisitionItem"];
        const item = FakeBackend.odataEntitySet(
            "A_PurchaseRequisitionItem", "Requisition item",
            "Central entity for approval decisions: release status, quantity, value, plant, supplier.",
            itemKeys, write ? ["list", "get", "update"] : ["list", "get"],
            FakeBackend.odataFields([
                { name: "PurchaseRequisition", label: "Purchase requisition", hint: "Key, 10 digits with leading zeros" },
                { name: "PurchaseRequisitionItem", label: "Requisition item" },
                {
                    name: "PurReqnReleaseStatus", label: "Release status", values: [
                        { value: "B", meaning: "awaiting release" },
                        { value: "05", meaning: "released" },
                        { value: "08", meaning: "rejected" }
                    ]
                },
                { name: "RequestedQuantity", label: "Requested quantity", type: "Edm.Decimal", hint: "In BaseUnit" },
                { name: "ItemNetAmount", label: "Net amount", type: "Edm.Decimal", hint: "In PurReqnItemCurrency" },
                { name: "Plant", label: "Plant" },
                { name: "DeliveryDate", label: "Delivery date", type: "Edm.DateTime" }
            ], 24, 88, write ? ["RequestedQuantity", "DeliveryDate"] : [])
        );
        // Personal data: known to the catalogue, never readable.
        item.fields.push({
            name: "CreatedByUser", type: "Edm.String", label: "Created by", selectable: false,
            filterable: false, writable: false, hint: "", values: [], personal_data: true
        });
        item.navigations = [
            { name: "to_PurchaseReqnItemText", target: "A_PurchaseReqnItemText", collection: true, description: "" },
            { name: "to_PurchaseReqnAcctAssgmt", target: "A_PurReqnAcctAssgmt", collection: true, description: "" },
            { name: "to_PurchaseReqnDeliveryAddress", target: "A_PurReqAddDelivery", collection: false, description: "" },
            {
                name: "to_PurchaseRequisition", target: "A_PurchaseRequisitionHeader", collection: false,
                description: "The header of this item."
            }
        ];
        item.examples = [
            {
                description: "Items awaiting release in one plant",
                filter: "PurReqnReleaseStatus eq 'B' and Plant eq '1010'",
                select: ["PurchaseRequisition", "PurchaseRequisitionItem", "ItemNetAmount"], orderby: "", top: 20
            },
            {
                description: "All items of one requisition", filter: "PurchaseRequisition eq '0010000123'",
                select: [], orderby: "PurchaseRequisitionItem", top: null
            }
        ];
        return [
            item,
            FakeBackend.odataEntitySet(
                "A_PurchaseRequisitionHeader", "Requisition header",
                "Number, document type and description only. Everything else is on the items.",
                ["PurchaseRequisition"], ["list", "get"],
                FakeBackend.odataFields([{ name: "PurchaseRequisition", label: "Purchase requisition" }], 4, 4)
            ),
            FakeBackend.odataEntitySet(
                "A_PurReqnAcctAssgmt", "Account assignment",
                "Cost centre, G/L account, WBS element and order per item.",
                itemKeys.concat(["PurchaseReqnAcctAssgmtNumber"]), ["list"],
                FakeBackend.odataFields([
                    { name: "PurchaseRequisition" }, { name: "PurchaseRequisitionItem" },
                    { name: "PurchaseReqnAcctAssgmtNumber" }
                ], 11, 37)
            ),
            FakeBackend.odataEntitySet(
                "A_PurchaseReqnItemText", "Item text", "The requester's justification text for one item.",
                itemKeys.concat(["DocumentText"]), write ? ["list", "get", "create", "update"] : ["list", "get"],
                FakeBackend.odataFields([
                    { name: "PurchaseRequisition" }, { name: "PurchaseRequisitionItem" }, { name: "DocumentText" },
                    { name: "Language" }, { name: "NoteDescription", label: "Text" }
                ], 5, 5, write ? ["NoteDescription"] : [])
            ),
            // Imported but not described yet: no readable field, so no operation.
            FakeBackend.odataEntitySet(
                "A_PurReqAddDelivery", "Delivery address", "", itemKeys, [],
                FakeBackend.odataFields([{ name: "PurchaseRequisition" }, { name: "PurchaseRequisitionItem" }], 0, 31)
            )
        ];
    }

    private makeODataService(
        input: ODataServiceInput, usedBy: ODataUsedBy[] = []
    ): ODataService {
        return {
            ...input, id: this.nextId++, created_at: "2026-10-05T08:00:00+00:00",
            updated_at: "2026-10-05T08:00:00+00:00",
            counts: { entity_sets: 0, operations: 0 }, has_write: false, used_by: usedBy
        };
    }

    /** `ODataService.to_dict()`: counts and has_write follow the definition. */
    private static odataDict(service: ODataService): ODataService {
        const definition: ODataDefinition = service.definition;
        const writes: ODataEntityOp[] = ["create", "update", "delete"];
        return {
            ...service,
            counts: { entity_sets: definition.entity_sets.length, operations: definition.operations.length },
            has_write: definition.entity_sets.some((e) => e.operations.some((op) => writes.indexOf(op) !== -1))
                || definition.operations.some((o) => o.enabled && o.changes_data)
        };
    }

    /** `ODataService.to_summary()`: the same without the definition. */
    private static odataSummary(service: ODataService): ODataServiceSummary {
        const { definition, ...summary } = FakeBackend.odataDict(service);
        void definition;
        return summary;
    }

    private static readonly ODATA_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;
    // `_EXPECTED_RE` in agents/odata/admin_routes.py, as a full match (in
    // JS `$` does not accept a trailing newline). The server then compares
    // instants; this fake compares the text, which is the same for a client
    // that hands back what it was given.
    private static readonly ODATA_EXPECTED_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$/;
    private static readonly ODATA_DESTINATION_RE = /^[A-Za-z0-9_.-]{1,200}$/;
    private static readonly ODATA_NAME_MSG = "String should match pattern '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'";
    private static readonly ODATA_DESTINATION_MSG = "String should match pattern '^[A-Za-z0-9_.-]{1,200}$'";
    private static readonly ODATA_PAYLOAD_KEYS = [
        "name", "title", "purpose", "not_for", "destination", "user_context", "odata_version",
        "service_path", "enabled", "definition", "metadata_fetched_at"
    ];
    private static readonly ODATA_DUPLICATE_KEYS = ["name", "title", "destination", "user_context"];

    /** `create_odata_service` in agents/db.py: the one text for a taken name,
     * on create and on duplicate. */
    private static odataNameTaken(name: string): string {
        return `Service name '${name}' already exists`;
    }

    /** `confine_service_path` in agents/odata/urls.py, in its order and with
     * its messages (none of which repeats the value). "" means accepted. */
    private static odataPathProblem(value: string): string {
        const control = (ch: string) => ch.charCodeAt(0) < 0x20 || (ch.charCodeAt(0) >= 0x7f && ch.charCodeAt(0) <= 0x9f);
        if (!value) {
            return "service_path is required";
        }
        if (value.length > 512) {
            return "service_path must be at most 512 characters";
        }
        if (value.split("").some(control)) {
            return "service_path must not contain control characters";
        }
        if (/\s/.test(value)) {
            return "service_path must not contain whitespace";
        }
        if (value.indexOf("\\") !== -1) {
            return "service_path must not contain a backslash";
        }
        if (value.indexOf("://") !== -1) {
            return "service_path is a path, not a URL; the host comes from the destination";
        }
        if (value.charAt(0) !== "/") {
            return "service_path must start with '/'";
        }
        if (/[?#]/.test(value)) {
            return "service_path must not contain '?' or '#'; query parameters belong in the destination";
        }
        if (value.indexOf("%") !== -1) {
            return "service_path must not contain '%'";
        }
        if (value.charAt(value.length - 1) === "/") {
            return "service_path must not end with '/'";
        }
        if (value.indexOf("//") !== -1) {
            return "service_path must not contain '//'";
        }
        if (value.indexOf("..") !== -1 || value.split("/").indexOf(".") !== -1) {
            return "service_path must not contain '.' or '..' segments";
        }
        return "";
    }

    /** pydantic's `strip_whitespace`: Unicode White_Space off both ends.
     * Not JS `trim()`, which leaves U+0085 and takes U+FEFF. */
    private static odataStrip(value: string): string {
        const space = "[\\t\\n\\v\\f\\r \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
        return value.replace(new RegExp(`^${space}+|${space}+$`, "g"), "");
    }

    /** What the catalogue answers (409) to an update whose
     * `expected_updated_at` is not the stored `updated_at`. */
    public static odataChangedElsewhere(name: string): string {
        return `Service '${name}' was changed since it was loaded; reload it and save again`;
    }

    /** A new `updated_at`, never the one a service already has. */
    private odataStamp = Date.parse("2026-10-05T09:00:00Z");
    private nextODataStamp(): string {
        this.odataStamp += 1000;
        return new Date(this.odataStamp).toISOString().replace(".000Z", "+00:00");
    }

    /** A stripped string of 1..max characters, in pydantic's words. */
    private static odataLength(value: string, max: number): string {
        const length = FakeBackend.odataStrip(value).length;
        if (length < 1) {
            return "String should have at least 1 character";
        }
        return length > max ? `String should have at most ${max} characters` : "";
    }

    /** `_one_line` in agents/odata/models.py: no control character
     * (category Cc) and no line or paragraph separator (Zl, Zp). */
    private static odataOneLine(field: string, value: string): string {
        const control = (ch: string) => ch.charCodeAt(0) < 0x20 || (ch.charCodeAt(0) >= 0x7f && ch.charCodeAt(0) <= 0x9f);
        return value.split("").some((ch) => control(ch) || ch === "\u2028" || ch === "\u2029")
            ? `Value error, ${field} must be one line of text without control characters` : "";
    }

    /** One `<key>: <problem>` per string field that is missing (when
     * required), not a string, or refused by `check`. */
    private static odataText(
        data: Record<string, unknown>, key: string, required: boolean,
        check: (value: string) => string, problems: string[]
    ): void {
        const value = data[key];
        if (value === undefined) {
            if (required) {
                problems.push(`${key}: Field required`);
            }
            return;
        }
        const problem = typeof value === "string" ? check(value) : "Input should be a valid string";
        if (problem) {
            problems.push(`${key}: ${problem}`);
        }
    }

    private static odataExtras(data: Record<string, unknown>, known: string[], problems: string[]): void {
        Object.keys(data).filter((key) => known.indexOf(key) === -1).forEach((key) => {
            // `_loc` in agents/odata/models.py: a key is the client's own
            // text, so it is repeated only when it looks like a field name.
            const shown = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key) ? key : "<unknown field>";
            problems.push(`${shown}: Extra inputs are not permitted`);
        });
    }

    // `EDM_NAME_RE` in agents/odata/models.py.
    private static readonly ODATA_EDM_RE = /^[A-Za-z_][A-Za-z0-9_.]{0,127}$/;

    /** An EDM name, in pydantic's words. */
    private static odataEdm(value: unknown): string {
        return typeof value === "string" && FakeBackend.ODATA_EDM_RE.test(value)
            ? "" : "String should match pattern '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'";
    }

    /** A string of `min`..`max` characters (not stripped), in pydantic's
     * words. Left out, the server's default applies. */
    private static odataMax(value: unknown, max: number, min = 0): string {
        const text = typeof value === "string" ? value : "";
        if (text.length < min) {
            return "String should have at least 1 character";
        }
        return text.length > max ? `String should have at most ${max} characters` : "";
    }

    /** `EntitySetDef._confined_segment`: one URL segment, or empty. */
    private static odataSegment(value: string): string {
        return !value || (/^[A-Za-z0-9_.-]{1,128}$/.test(value) && value.indexOf("..") === -1 && value !== ".")
            ? "" : "Value error, path must be one URL segment of letters, digits, '_', '.' and '-' "
                + "(empty = the entity set name)";
    }

    /** The first rule of `EntitySetDef._consistent` the entity set breaks. */
    private static odataEntitySetProblem(entitySet: ODataEntitySet): string {
        const who = `entity set '${entitySet.name}'`;
        const fields = entitySet.fields ?? [];
        const keys = entitySet.keys ?? [];
        const names = fields.map((f) => f.name);
        const duplicate = names.filter((name, i) => names.indexOf(name) !== i)[0];
        if (duplicate !== undefined) {
            return `duplicate field '${duplicate}' in ${who}`;
        }
        const first = (list: string[]) => list.filter((name, i) => list.indexOf(name) !== i)[0];
        const navigation = first((entitySet.navigations ?? []).map((n) => n.name));
        if (navigation !== undefined) {
            return `duplicate navigation '${navigation}' in ${who}`;
        }
        const key = first(keys.map((k) => k.name));
        if (key !== undefined) {
            return `duplicate key '${key}' in ${who}`;
        }
        const strayKey = keys.filter((key) => names.indexOf(key.name) === -1)[0];
        if (strayKey) {
            return `key '${strayKey.name}' of ${who} is not one of its fields`;
        }
        for (const op of entitySet.operations ?? []) {
            if ((op === "get" || op === "update" || op === "delete") && !keys.length) {
                return `${who} has '${op}' but no key`;
            }
            if ((op === "list" || op === "get") && !fields.some((f) => f.selectable)) {
                return `${who} has '${op}' but no selectable field`;
            }
            if ((op === "create" || op === "update") && !fields.some((f) => f.writable)) {
                return `${who} has '${op}' but no writable field`;
            }
        }
        return "";
    }

    /**
     * The cross-field rules of a definition (agents/odata/models.py), with
     * the server's `loc` and text. Like pydantic, an entity set is checked as
     * a whole only when its fields passed, and the definition as a whole only
     * when everything in it passed. The per-value rules of an entity set
     * (EDM names, text lengths, one-line texts, value meanings, example
     * queries) are mirrored too: the entity set dialog builds those values
     * (`ENTITY_CASES` in test/unit/odataRuleCases.ts holds both sides to
     * them). Of the operations: the names an operation gives for the entity
     * set it is bound to or returns must be entity sets of the definition
     * (`REMOVAL_CASES` in the same file). Not mirrored: the size cap and the
     * other operation rules.
     */
    private static odataDefinitionProblems(definition: unknown): string[] {
        if (definition === undefined) {
            // No default: a payload without the key is refused, so that an
            // update can never replace the stored definition by an empty one.
            return ["definition: Field required"];
        }
        if (definition === null || typeof definition !== "object" || Array.isArray(definition)) {
            return ["definition: Input should be a valid dictionary or instance of ServiceDefinition"];
        }
        const problems: string[] = [];
        // StrictBool everywhere: "true", "false", 1 and 0 are refused.
        const flags = (loc: string, source: unknown, keys: string[]) => {
            const data = (source ?? {}) as Record<string, unknown>;
            keys.forEach((key) => {
                if (data[key] !== undefined && typeof data[key] !== "boolean") {
                    problems.push(`${loc}.${key}: Input should be a valid boolean`);
                }
            });
        };
        const entitySets = (definition as Partial<ODataDefinition>).entity_sets ?? [];
        entitySets.forEach((entitySet, i) => {
            const before = problems.length;
            const at = `definition.entity_sets.${i}`;
            // The values first, in the order of the model's fields; pydantic
            // checks a model as a whole only when its values passed.
            const say = (loc: string, problem: string) => {
                if (problem) {
                    problems.push(`${at}.${loc}: ${problem}`);
                }
            };
            say("name", FakeBackend.odataEdm(entitySet.name));
            say("title", FakeBackend.odataMax(entitySet.title, 120) || FakeBackend.odataOneLine("title", entitySet.title ?? ""));
            say("path", FakeBackend.odataSegment(entitySet.path ?? ""));
            say("description", FakeBackend.odataMax(entitySet.description, 600));
            (entitySet.keys ?? []).forEach((key, k) => {
                say(`keys.${k}.name`, FakeBackend.odataEdm(key.name));
                say(`keys.${k}.type`, FakeBackend.odataMax(key.type ?? "Edm.String", 200, 1));
            });
            if ((entitySet.fields ?? []).length > 500) {
                say("fields", `List should have at most 500 items after validation, not ${(entitySet.fields ?? []).length}`);
            }
            (entitySet.fields ?? []).forEach((field, j) => {
                const loc = `definition.entity_sets.${i}.fields.${j}`;
                const own = problems.length;
                say(`fields.${j}.name`, FakeBackend.odataEdm(field.name));
                say(`fields.${j}.type`, FakeBackend.odataMax(field.type ?? "Edm.String", 200, 1));
                say(`fields.${j}.label`, FakeBackend.odataMax(field.label, 120) || FakeBackend.odataOneLine("label", field.label ?? ""));
                flags(loc, field, ["selectable", "filterable", "writable"]);
                say(`fields.${j}.hint`, FakeBackend.odataMax(field.hint, 300));
                (field.values ?? []).forEach((pair, v) => {
                    say(`fields.${j}.values.${v}.value`,
                        FakeBackend.odataMax(pair.value, 64, 1) || FakeBackend.odataOneLine("value", pair.value));
                    say(`fields.${j}.values.${v}.meaning`,
                        FakeBackend.odataMax(pair.meaning, 200, 1) || FakeBackend.odataOneLine("meaning", pair.meaning));
                });
                flags(loc, field, ["personal_data"]);
                if (problems.length === own && field.filterable && !field.selectable) {
                    problems.push(
                        `${loc}: Value error, field '${field.name}' is `
                        + "filterable but not selectable; a filterable field must also be selectable"
                    );
                }
            });
            (entitySet.navigations ?? []).forEach((navigation, k) => {
                say(`navigations.${k}.name`, FakeBackend.odataEdm(navigation.name));
                say(`navigations.${k}.target`, FakeBackend.odataEdm(navigation.target));
                flags(`definition.entity_sets.${i}.navigations.${k}`, navigation, ["collection"]);
                say(`navigations.${k}.description`, FakeBackend.odataMax(navigation.description, 300));
            });
            (entitySet.examples ?? []).forEach((example, k) => {
                say(`examples.${k}.description`, FakeBackend.odataMax(example.description, 200, 1));
                say(`examples.${k}.filter`, FakeBackend.odataMax(example.filter, 1000));
                (example.select ?? []).forEach((name, n) => {
                    say(`examples.${k}.select.${n}`, FakeBackend.odataMax(name, 128));
                });
                say(`examples.${k}.orderby`, FakeBackend.odataMax(example.orderby, 300));
                const top: unknown = example.top;
                if (typeof top === "number" && !Number.isInteger(top)) {
                    say(`examples.${k}.top`, "Input should be a valid integer, got a number with a fractional part");
                } else if (typeof top === "number" && top < 1) {
                    say(`examples.${k}.top`, "Input should be greater than or equal to 1");
                }
            });
            const problem = problems.length === before ? FakeBackend.odataEntitySetProblem(entitySet) : "";
            if (problem) {
                problems.push(`definition.entity_sets.${i}: Value error, ${problem}`);
            }
        });
        ((definition as Partial<ODataDefinition>).operations ?? []).forEach((operation, m) => {
            (operation.parameters ?? []).forEach((parameter, p) => {
                flags(`definition.operations.${m}.parameters.${p}`, parameter, ["required"]);
            });
            flags(`definition.operations.${m}`, operation, ["enabled", "changes_data"]);
        });
        if (!problems.length) {
            const names = entitySets.map((e) => e.name);
            const duplicate = names.filter((name, i) => names.indexOf(name) !== i)[0];
            const operations = (definition as Partial<ODataDefinition>).operations ?? [];
            // One refusal, the first in the server's order.
            const dangling = operations.map((operation) => {
                if (operation.bound_to !== null && operation.bound_to !== undefined && names.indexOf(operation.bound_to) === -1) {
                    return `bound_to '${operation.bound_to}' of operation '${operation.name}' is not an entity set of this service`;
                }
                if (operation.returns && names.indexOf(operation.returns.entity_set) === -1) {
                    return `returns entity set '${operation.returns.entity_set}' of operation '${operation.name}' `
                        + "is not an entity set of this service";
                }
                return "";
            }).filter(Boolean)[0];
            if (duplicate !== undefined) {
                problems.push(`definition: Value error, duplicate entity set '${duplicate}'`);
            } else if (dangling) {
                problems.push(`definition: Value error, ${dangling}`);
            }
        }
        return problems;
    }

    /**
     * What `validate_odata_service` refuses, as the list of `<loc>: <msg>`
     * the real route joins with "; " into the 422's string detail. Empty
     * means the payload is accepted. Never contains a refused value.
     *
     * Without this the fake would store whatever a form sends, and a journey
     * could pass against input the real backend refuses.
     */
    private static validateODataPayload(body: Record<string, unknown> | undefined): string[] {
        const data = body ?? {};
        const problems: string[] = [];
        const flag = (key: string) => {
            if (data[key] !== undefined && typeof data[key] !== "boolean") {
                problems.push(`${key}: Input should be a valid boolean`);
            }
        };
        FakeBackend.odataText(data, "name", true, (v) => (
            FakeBackend.ODATA_NAME_RE.test(v) ? "" : FakeBackend.ODATA_NAME_MSG
        ), problems);
        // Stripped, measured, and only then held to one line: what
        // surrounds a title or purpose is gone before `_one_line` runs.
        FakeBackend.odataText(data, "title", true, (v) => (
            FakeBackend.odataLength(v, 120) || FakeBackend.odataOneLine("title", FakeBackend.odataStrip(v))
        ), problems);
        FakeBackend.odataText(data, "purpose", true, (v) => (
            FakeBackend.odataLength(v, 200) || FakeBackend.odataOneLine("purpose", FakeBackend.odataStrip(v))
        ), problems);
        FakeBackend.odataText(data, "not_for", false, (v) => (
            v.length > 200 ? "String should have at most 200 characters" : FakeBackend.odataOneLine("not_for", v)
        ), problems);
        FakeBackend.odataText(data, "destination", true, (v) => (
            FakeBackend.ODATA_DESTINATION_RE.test(v) ? "" : FakeBackend.ODATA_DESTINATION_MSG
        ), problems);
        flag("user_context");
        if (data.odata_version === undefined) {
            problems.push("odata_version: Field required");
        } else if (data.odata_version !== "v2" && data.odata_version !== "v4") {
            problems.push("odata_version: Input should be 'v2' or 'v4'");
        }
        FakeBackend.odataText(data, "service_path", true, (v) => {
            const problem = FakeBackend.odataPathProblem(v);
            return problem ? `Value error, ${problem}` : "";
        }, problems);
        flag("enabled");
        FakeBackend.odataDefinitionProblems(data.definition).forEach((problem) => problems.push(problem));
        FakeBackend.odataExtras(data, FakeBackend.ODATA_PAYLOAD_KEYS, problems);
        return problems;
    }

    /** What `DuplicateBody` (agents/odata/admin_routes.py) refuses: the new
     * name by the same rule as a service's, and nothing but the four keys. */
    private static validateODataDuplicate(body: Record<string, unknown> | undefined): string[] {
        const data = body ?? {};
        const problems: string[] = [];
        FakeBackend.odataText(data, "name", true, (v) => (
            FakeBackend.ODATA_NAME_RE.test(v) ? "" : FakeBackend.ODATA_NAME_MSG
        ), problems);
        if (data.title !== null) {
            FakeBackend.odataText(data, "title", false, (v) => FakeBackend.odataLength(v, 120), problems);
        }
        if (data.destination !== null) {
            FakeBackend.odataText(data, "destination", false, (v) => (
                FakeBackend.ODATA_DESTINATION_RE.test(v) ? "" : FakeBackend.ODATA_DESTINATION_MSG
            ), problems);
        }
        if (data.user_context !== undefined && data.user_context !== null && typeof data.user_context !== "boolean") {
            problems.push("user_context: Input should be a valid boolean");
        }
        FakeBackend.odataExtras(data, FakeBackend.ODATA_DUPLICATE_KEYS, problems);
        return problems;
    }

    /** The 422 of the catalogue routes: ONE string, "<loc>: <msg>; ...",
     * not FastAPI's `detail[]` -- that array would echo each refused input. */
    private refused(problems: string[]): Promise<Response> {
        return this.json({ detail: problems.join("; ") }, 422);
    }

    /** A validated payload as the server stores it: title and purpose
     * stripped, what is optional filled in with the model's defaults. Call
     * only after `validateODataPayload` accepted the body. */
    private static odataInput(body: Record<string, unknown> | undefined): ODataServiceInput {
        const input = (body ?? {}) as Partial<ODataServiceInput>;
        return {
            name: String(input.name), title: FakeBackend.odataStrip(String(input.title)),
            purpose: FakeBackend.odataStrip(String(input.purpose)), not_for: input.not_for ?? "",
            destination: String(input.destination), user_context: input.user_context === true,
            odata_version: input.odata_version === "v4" ? "v4" : "v2",
            service_path: String(input.service_path), enabled: input.enabled !== false,
            // Always there: `validateODataPayload` refuses a payload
            // without a definition. A PUT is a full replacement, so a form
            // sends the definition it loaded even when only a General field
            // changed.
            definition: {
                entity_sets: input.definition?.entity_sets ?? [],
                operations: input.definition?.operations ?? []
            },
            metadata_fetched_at: input.metadata_fetched_at ?? null
        };
    }

    /** The offer in `metadataPreview` compared with a stored definition
     * (`undefined`: nothing to compare with, so everything is new). */
    private odataPreview(stored: ODataDefinition | undefined): ODataMetadataPreview {
        const entitySets = this.metadataPreview.entity_sets.map((offered) => {
            const known = stored?.entity_sets.filter((e) => e.name === offered.name)[0];
            if (!known) {
                return { ...offered, status: "new" as const, new_fields: [], removed_fields: [] };
            }
            const have = known.fields.map((f) => f.name);
            const offer = offered.fields.map((f) => f.name);
            const added = offer.filter((name) => have.indexOf(name) === -1);
            const removed = have.filter((name) => offer.indexOf(name) === -1);
            return {
                ...offered, new_fields: added, removed_fields: removed,
                status: added.length || removed.length ? "changed" as const : "in_service" as const
            };
        });
        const operations = this.metadataPreview.operations.map((offered) => ({
            ...offered,
            status: stored?.operations.some((o) => o.name === offered.name) ? "in_service" as const : "new" as const
        }));
        return {
            ...this.metadataPreview, entity_sets: entitySets, operations,
            summary: {
                entity_sets: entitySets.length, operations: operations.length,
                in_service: entitySets.filter((e) => e.status === "in_service").length,
                changed: entitySets.filter((e) => e.status === "changed").length
            }
        };
    }

    private seedOData(): void {
        const user = (agent: Agent, allowWrite = false): ODataUsedBy => ({
            agent_id: agent.id, agent: agent.name, enabled: agent.enabled,
            expose_api: agent.expose_api, api_slug: agent.api_slug, allow_write: allowWrite
        });
        const [btp, gmail] = this.agents;
        const path = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV";
        const partnerKeys = ["BusinessPartner"];

        this.odataServices = [
            // Signed-in user, read-only: what a chat agent uses.
            this.makeODataService({
                name: "purchase-requisitions", title: "Purchase requisitions",
                purpose: "Read requisitions and their items to judge an approval", not_for: "",
                destination: "S4_ODATA_USER", user_context: true, odata_version: "v2", service_path: path,
                enabled: true, metadata_fetched_at: "2026-10-05T07:30:00+00:00",
                definition: { entity_sets: FakeBackend.requisitionEntitySets(false), operations: [] }
            }, [user(gmail)]),
            // The same service through a technical user, with writes: for jobs.
            this.makeODataService({
                name: "purchase-requisitions-jobs", title: "Purchase requisitions (jobs)",
                purpose: "Nightly checks and release of requisitions",
                not_for: "Purchase orders, contracts or supplier master data: use the matching service",
                destination: "S4_ODATA_TECH", user_context: false, odata_version: "v2", service_path: path,
                enabled: true, metadata_fetched_at: "2026-10-05T07:30:00+00:00",
                definition: {
                    entity_sets: FakeBackend.requisitionEntitySets(true),
                    operations: [{
                        name: "ReleaseItem", qualified_name: "", title: "Release item", kind: "function_import",
                        http_method: "POST", bound_to: "A_PurchaseRequisitionItem",
                        parameters: [
                            { name: "PurchaseRequisition", type: "Edm.String", required: true },
                            { name: "PurchaseRequisitionItem", type: "Edm.String", required: true },
                            { name: "ReleaseCode", type: "Edm.String", required: true }
                        ],
                        description: "Releases one requisition item with a release code.",
                        enabled: true, changes_data: true
                    }]
                }
            }, [user(btp, true)]),
            this.makeODataService({
                name: "business-partners", title: "Business partners",
                purpose: "Look up suppliers and their addresses", not_for: "",
                destination: "S4_ODATA_USER", user_context: true, odata_version: "v2",
                service_path: "/sap/opu/odata/sap/API_BUSINESS_PARTNER", enabled: true,
                metadata_fetched_at: "2026-10-04T15:00:00+00:00",
                definition: {
                    entity_sets: [
                        FakeBackend.odataEntitySet(
                            "A_BusinessPartner", "Business partner", "Name, category and grouping of a partner.",
                            partnerKeys, ["list", "get"],
                            FakeBackend.odataFields([{ name: "BusinessPartner", label: "Business partner" }], 6, 20)
                        ),
                        FakeBackend.odataEntitySet(
                            "A_Supplier", "Supplier", "Supplier-specific data of a partner.",
                            ["Supplier"], ["list", "get"],
                            FakeBackend.odataFields([{ name: "Supplier", label: "Supplier" }], 5, 12)
                        ),
                        FakeBackend.odataEntitySet(
                            "A_BusinessPartnerAddress", "Address", "Postal addresses of a partner.",
                            partnerKeys.concat(["AddressID"]), ["list"],
                            FakeBackend.odataFields([{ name: "BusinessPartner" }, { name: "AddressID" }], 8, 30)
                        )
                    ],
                    operations: []
                }
            }, [user(btp), user(gmail)]),
            // Disabled, V4, used by nobody: the one a journey can delete.
            this.makeODataService({
                name: "purchase-requisitions-v4", title: "Purchase requisitions (V4)",
                purpose: "Same object over the V4 API", not_for: "",
                destination: "S4_ODATA_USER", user_context: true, odata_version: "v4",
                service_path: "/sap/opu/odata4/sap/api_purchaserequisition_2/srvd_a2x/sap/purchaserequisition/0001",
                enabled: false, metadata_fetched_at: null,
                definition: {
                    entity_sets: [
                        FakeBackend.odataEntitySet(
                            "PurchaseReqn", "Requisition", "The requisition header.",
                            ["PurchaseRequisition"], ["list", "get"],
                            FakeBackend.odataFields([{ name: "PurchaseRequisition" }], 4, 9)
                        ),
                        FakeBackend.odataEntitySet(
                            "PurchaseReqnItem", "Requisition item", "The items of a requisition.",
                            ["PurchaseRequisition", "PurchaseRequisitionItem"], ["list", "get"],
                            FakeBackend.odataFields(
                                [{ name: "PurchaseRequisition" }, { name: "PurchaseRequisitionItem" }], 10, 60
                            )
                        )
                    ],
                    operations: [
                        {
                            name: "Release", qualified_name: "com.sap.gateway.srvd_a2x.purchaserequisition.v0001.Release",
                            title: "Release", kind: "action", http_method: "POST", bound_to: "PurchaseReqnItem",
                            parameters: [{ name: "ReleaseCode", type: "Edm.String", required: true }],
                            description: "", enabled: false, changes_data: true
                        },
                        {
                            name: "Reject", qualified_name: "com.sap.gateway.srvd_a2x.purchaserequisition.v0001.Reject",
                            title: "Reject", kind: "action", http_method: "POST", bound_to: "PurchaseReqnItem",
                            parameters: [], description: "", enabled: false, changes_data: true
                        },
                        {
                            name: "GetReleaseStrategy",
                            qualified_name: "com.sap.gateway.srvd_a2x.purchaserequisition.v0001.GetReleaseStrategy",
                            title: "Release strategy", kind: "function", http_method: "GET",
                            bound_to: "PurchaseReqnItem", parameters: [], description: "",
                            enabled: false, changes_data: false
                        }
                    ]
                }
            })
        ];

        // What $metadata of the requisition service offers: the five entity
        // sets and the function import of the jobs service, with two fields
        // on the item that no stored service has yet. `status`, `new_fields`,
        // `removed_fields` and `summary` are placeholders here: every answer
        // computes them against the service the request names (odataPreview).
        const jobs = this.odataServices[1].definition;
        const labels: Record<string, string> = {
            A_PurchaseRequisitionItem: "Purchase requisition item",
            A_PurchaseRequisitionHeader: "Purchase requisition",
            A_PurReqnAcctAssgmt: "Account assignment",
            A_PurchaseReqnItemText: "Item text",
            A_PurReqAddDelivery: "Delivery address"
        };
        const previewSets: ODataPreviewEntitySet[] = jobs.entity_sets.map((e) => ({
            name: e.name, entity_type: e.entity_type, label: labels[e.name], keys: e.keys,
            fields: e.fields.map((f) => ({
                name: f.name, type: f.type, label: f.label, filterable: true, creatable: false, updatable: false
            })),
            navigations: e.navigations.map((n) => ({ name: n.name, target: n.target, collection: n.collection })),
            capabilities: { creatable: false, updatable: e.name !== "A_PurReqAddDelivery", deletable: false },
            status: "new", new_fields: [], removed_fields: []
        }));
        previewSets[0].fields = previewSets[0].fields.concat([
            { name: "PurReqnOrigin", type: "Edm.String", label: "Origin of requisition",
              filterable: true, creatable: false, updatable: false },
            { name: "LastChangeDateTime", type: "Edm.DateTimeOffset", label: "Last changed on",
              filterable: true, creatable: false, updatable: false }
        ]);
        this.metadataPreview = {
            fetched_at: "2026-10-05T09:00:00+00:00",
            entity_sets: previewSets,
            operations: jobs.operations.map((o) => ({
                name: o.name, qualified_name: o.qualified_name, kind: o.kind, http_method: o.http_method,
                bound_to: o.bound_to, parameters: o.parameters, label: "", status: "new"
            })),
            // One element the parser left out (`ParsedMetadata.skipped`), so
            // the import dialog has an "n elements skipped" to show. Set it
            // to [] in a journey for the ordinary case.
            skipped: [{ kind: "property", entity_set: "A_PurReqnAcctAssgmt", position: 38, reason: "invalid_type" }],
            summary: { entity_sets: 5, operations: 1, in_service: 0, changed: 0 }
        };
        this.testResult = {
            ok: true, status: 200, duration_ms: 412, target: "A_PurchaseRequisitionItem", rows: 1,
            identity: "technical", destination: "S4_ODATA_TECH", auth_type: "BasicAuthentication",
            proxy_type: "OnPremise", message: ""
        };
    }

    /**
     * The eight routes of /admin/api/odata, answering what
     * agents/odata/admin_routes.py answers, in its order of checks.
     * Undefined: not an OData call.
     *
     * Not mirrored: 413 for an oversized body, and 424 "signed-in user
     * required" from `metadata` and `test` (no JWT bound with `user_context`
     * on) -- the fake has no session, so a journey reaches a 424 only
     * through `failNext`.
     */
    private handleOData(
        path: string, method: string, body: Record<string, unknown> | undefined
    ): Promise<Response> | undefined {
        if (path === "odata/metadata" && method === "POST") {
            // Compared with the stored service the request names; without
            // `service` everything is new. (The real route does not exist
            // yet: a 404 for an unknown `service` is this fake's assumption.)
            if (body?.service === undefined || body.service === null) {
                return this.json(this.odataPreview(undefined));
            }
            const compared = this.odataServices.filter((s) => s.name === body.service)[0];
            return compared
                ? this.json(this.odataPreview(compared.definition))
                : this.json({ detail: "Service not found" }, 404);
        }
        if (path === "odata/services" && method === "GET") {
            const sorted = this.odataServices.slice().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
            return this.json(sorted.map((s) => FakeBackend.odataSummary(s)));
        }
        if (path === "odata/services" && method === "POST") {
            const problems = FakeBackend.validateODataPayload(body);
            if (problems.length) {
                return this.refused(problems);
            }
            const input = FakeBackend.odataInput(body);
            if (this.odataServices.some((s) => s.name === input.name)) {
                return this.json({ detail: FakeBackend.odataNameTaken(input.name) }, 409);
            }
            const created = this.makeODataService(input);
            this.odataServices.push(created);
            return this.json(FakeBackend.odataDict(created), 201);
        }
        const match = /^odata\/services\/([^/]+)(\/duplicate|\/test)?$/.exec(path);
        if (!match) {
            return undefined;
        }
        const name = decodeURIComponent(match[1]);
        const action = match[2] ?? "";
        const notFound = () => this.json({ detail: "Service not found" }, 404);
        // A name that cannot be a service name is the same 404 as an unknown
        // one, before the body is looked at (`_service_name`).
        if (!FakeBackend.ODATA_NAME_RE.test(name)) {
            return notFound();
        }
        const index = this.odataServices.findIndex((s) => s.name === name);
        const stored = this.odataServices[index];
        if (action === "/test" && method === "POST") {
            return stored ? this.json(this.testResult) : notFound();
        }
        if (action === "/duplicate" && method === "POST") {
            const problems = FakeBackend.validateODataDuplicate(body);
            if (problems.length) {
                return this.refused(problems);
            }
            if (!stored) {
                return notFound();
            }
            const copyName = String(body?.name);
            const { id, created_at, updated_at, counts, has_write, used_by, ...source } = stored;
            void [id, created_at, updated_at, counts, has_write, used_by];
            const given = (key: string) => body?.[key] !== undefined && body[key] !== null;
            const copyBody: Record<string, unknown> = {
                ...source,
                name: copyName,
                // `_copy_title`: "<title> (copy)", cut to fit the 120 limit.
                title: given("title") ? body?.title : `${source.title.substring(0, 113).trim()} (copy)`,
                destination: given("destination") ? body?.destination : source.destination,
                user_context: given("user_context") ? body?.user_context : source.user_context,
                definition: JSON.parse(JSON.stringify(source.definition)) as ODataDefinition
            };
            // The copy goes through the same gate as a new service.
            const copyProblems = FakeBackend.validateODataPayload(copyBody);
            if (copyProblems.length) {
                return this.refused(copyProblems);
            }
            if (this.odataServices.some((s) => s.name === copyName)) {
                return this.json({ detail: FakeBackend.odataNameTaken(copyName) }, 409);
            }
            const copy = this.makeODataService(FakeBackend.odataInput(copyBody));
            this.odataServices.push(copy);
            return this.json(FakeBackend.odataDict(copy), 201);
        }
        if (action === "" && method === "PUT") {
            // An update may say which version it was made from; that key is
            // not part of the payload.
            const { expected_updated_at: expected, ...payload } = body ?? {};
            const problems = FakeBackend.validateODataPayload(payload);
            if (expected !== undefined && expected !== null && typeof expected !== "string") {
                problems.push("expected_updated_at: Input should be a valid string");
            } else if (typeof expected === "string" && !FakeBackend.ODATA_EXPECTED_RE.test(expected)) {
                // `_expected_updated_at`: only the format the API emits
                // `updated_at` in; anything else is a 422, not a 409.
                problems.push("expected_updated_at: expected the updated_at this service was loaded with");
            }
            if (problems.length) {
                return this.refused(problems);
            }
            if (!stored) {
                return notFound();
            }
            const input = FakeBackend.odataInput(payload);
            if (input.name !== name) {
                return this.refused(["name cannot be changed; duplicate the service instead"]);
            }
            // Stale-write protection: the service was saved by someone else
            // since this client read it. Exact comparison of the ISO text
            // the GET answered; left out or null means "do not check".
            if (typeof expected === "string" && expected !== stored.updated_at) {
                return this.json({ detail: FakeBackend.odataChangedElsewhere(name) }, 409);
            }
            // A full replacement of the payload fields. `used_by` stays as
            // stored: the fake keeps it on the row for now; the task that
            // attaches services to agents derives it from the agents' server
            // entries, as `odata_service_referrers` does.
            this.odataServices[index] = { ...stored, ...input, updated_at: this.nextODataStamp() };
            return this.json(FakeBackend.odataDict(this.odataServices[index]));
        }
        if (!stored) {
            return notFound();
        }
        if (action === "" && method === "GET") {
            return this.json(FakeBackend.odataDict(stored));
        }
        if (action === "" && method === "DELETE") {
            if (stored.used_by.length) {
                const agents = stored.used_by.map((u) => `'${u.agent}'`).join(", ");
                return this.json({ detail: `Service '${name}' is used by agent(s) ${agents}` }, 409);
            }
            this.odataServices.splice(index, 1);
            return this.noContent();
        }
        return undefined;
    }

    /**
     * What a destination service with two levels would list. Generic names
     * only. The two destinations the seeded services use are among them, so
     * that a stored service is the ordinary case: its destination is listed
     * and matches its identity. No name here is the beginning of another: a
     * combo box completes what is typed to the first item that starts with it.
     */
    private seedDestinations(): void {
        const item = (name: string, over: Partial<ODataDestination>): ODataDestination => ({
            name, description: "", type: "HTTP", proxy_type: "Internet", authentication: "BasicAuthentication",
            level: "subaccount", user_propagating: false, usable: true, reason: null, notes: [],
            shadows_subaccount: false, ...over
        });
        const onPremise = { proxy_type: "OnPremise", notes: ["on_premise"] };
        this.destinationsMode = "ok";
        this.heldDestinations = [];
        this.odataDestinations = [
            item("S4_ODATA_USER", {
                ...onPremise, authentication: "PrincipalPropagation", user_propagating: true,
                description: "Development system, as the signed-in user"
            }),
            item("S4_ODATA_TECH", { ...onPremise, description: "Development system, technical user for jobs" }),
            item("S4_DEV_BASIC", { level: "instance", shadows_subaccount: true }),
            item("S4_DEV_USER", { authentication: "OAuth2SAMLBearerAssertion", user_propagating: true }),
            item("S4_DEV_RFC", { type: "RFC", authentication: "", usable: false, reason: "not_http" }),
            item("S4 DEV invalid", { usable: false, reason: "invalid_name" })
        ];
    }

    /** `list_destinations` in agents/odata/destinations.py: sorted by
     *  name, case-insensitively. */
    private destinationList(): ODataDestinationList {
        const lower = (item: ODataDestination) => item.name.toLowerCase();
        let items = this.odataDestinations.slice().sort((a, b) => (lower(a) < lower(b) ? -1 : lower(a) > lower(b) ? 1 : 0));
        const list: ODataDestinationList = { items, truncated: false, skipped: 0, warnings: [] };
        if (this.destinationsMode === "truncated") {
            // Two that can be used, so that the cut list still offers something.
            items = items.filter((entry) => entry.usable).slice(0, 2);
            return { ...list, items, truncated: true };
        }
        if (this.destinationsMode === "partial") {
            return {
                ...list,
                items: items.filter((entry) => entry.level === "instance"),
                warnings: [{
                    code: "level_unavailable", level: "subaccount", reason: "http_error", status: 500,
                    message: "the destinations of the subaccount could not be listed"
                }]
            };
        }
        return list;
    }

    /** GET odata/destinations (`api_list_odata_destinations`). */
    private answerDestinations(query: URLSearchParams): Promise<Response> {
        if (Array.from(query.keys()).length > 0) {
            return this.json({ detail: "query: this route takes no parameters" }, 422);
        }
        const answer = (): Promise<Response> => {
            if (this.destinationsMode === "unavailable") {
                return Promise.resolve(new Response(JSON.stringify({
                    detail: "no destination service is bound to this application, so its destinations "
                        + "cannot be listed; type the destination name instead"
                }), {
                    status: 503,
                    headers: { "Content-Type": "application/json", "X-OData-Error": "no_destination_service" }
                }));
            }
            return this.json(this.destinationList());
        };
        if (this.destinationsMode === "held") {
            return new Promise<Response>((resolve) => {
                this.heldDestinations.push(() => resolve(answer()));
            });
        }
        return answer();
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
        this.bodies[`${method} ${path}`] = body;

        if (this.failNext && this.failNext.path === path
            && (!this.failNext.method || this.failNext.method === method)) {
            const failure = this.failNext;
            this.failNext = undefined;
            if (failure.network) {
                return Promise.reject(new TypeError("Failed to fetch"));
            }
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
        // --- odata ---
        if (path === "odata/destinations" && method === "GET") {
            return this.answerDestinations(query);
        }
        const odata = this.handleOData(path, method, body);
        if (odata) {
            return odata;
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
