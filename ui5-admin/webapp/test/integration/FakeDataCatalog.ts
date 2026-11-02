import type {
    DataEdge, DataGraphNode, DataNode, DataNodeKind, DataSearchCandidate, DataSourceKind, DataSourceSummary
} from "com/agent/admin/service/dataCatalogTypes";

type Json = (body: unknown, status?: number) => Promise<Response>;

/** The keys `PUT nodes/{id}/hints` accepts; anything else is a 422. */
const HINT_KEYS = ["label", "purpose", "not_for", "synonyms", "examples", "readable"];

/** The answer to `readable: true` on a node the OData catalogue hides. */
export const NOT_READABLE = "readable: the catalogue does not let agents read this node";

/**
 * The `/admin/api/data-catalog/...` routes of the fake backend, apart from
 * `FakeBackend` so neither file grows past its size. Two OData sources: a
 * requisition service with three entities and relations between them, used
 * by btp-agent, and a small business-partner service used by gmail-agent.
 */
export default class FakeDataCatalog {

    public sources: DataSourceSummary[] = [];
    public nodes: DataNode[] = [];
    public edges: DataEdge[] = [];
    /** Nodes the OData catalogue does not let agents read: `readable: true` is refused. */
    public catalogueHidden: (number | string)[] = [];

    public reset(): void {
        this.sources = [
            {
                kind: "odata", name: "purchase-requisitions", title: "Purchase requisitions",
                destination: "S4_ODATA_USER", user_context: true, enabled: true, used_by: ["btp-agent"],
                nodes: 13, last_import: "2026-11-01T08:30:00+00:00", hint_completeness: 0.72
            },
            {
                kind: "odata", name: "business-partners", title: "Business partners",
                destination: "S4_ODATA_TECH", user_context: false, enabled: true, used_by: ["gmail-agent"],
                nodes: 4, last_import: "2026-10-31T17:05:00+00:00", hint_completeness: 0.25
            },
            {
                // In the catalogue, attached to no agent: never imported.
                kind: "odata", name: "sales-orders", title: "Sales orders",
                destination: "S4_ODATA_USER", user_context: true, enabled: true, used_by: [],
                nodes: 0, last_import: null, hint_completeness: 0
            }
        ];
        this.catalogueHidden = [32];
        const pr = (id: number, kind: DataNodeKind, name: string, parent: number | null, hints: Partial<DataNode> = {}) =>
            FakeDataCatalog.node(id, kind, "odata", "purchase-requisitions", name, parent, hints);
        const bp = (id: number, kind: DataNodeKind, name: string, parent: number | null, hints: Partial<DataNode> = {}) =>
            FakeDataCatalog.node(id, kind, "odata", "business-partners", name, parent, hints);
        this.nodes = [
            pr(1, "source", "purchase-requisitions", null, {
                label: "Purchase requisitions", purpose: "Requests to buy goods or services, before an order",
                not_for: "Purchase orders and invoices", synonyms: ["PR", "requisition", "purchase request"],
                examples: ["Which requisitions are waiting for my approval?", "How many PRs did plant 1010 create this week?"]
            }),
            pr(10, "entity", "A_PurchaseRequisitionHeader", 1, {
                label: "Requisition", purpose: "One purchase requisition with its description",
                synonyms: ["PR header"]
            }),
            pr(11, "field", "PurchaseRequisition", 10, { label: "Requisition number", is_key: true, type_name: "Edm.String" }),
            pr(12, "field", "PurReqnDescription", 10, { label: "Description" }),
            pr(20, "entity", "A_PurchaseRequisitionItem", 1, {
                label: "Requisition item", purpose: "A line of a requisition: material, quantity, supplier",
                writable: true
            }),
            pr(21, "field", "PurchaseRequisitionItem", 20, { label: "Item", is_key: true, type_name: "Edm.String" }),
            pr(22, "field", "Material", 20, { label: "Material", synonyms: ["product", "article"] }),
            pr(23, "field", "RequestedQuantity", 20, { label: "Quantity", unit: "EA", writable: true }),
            pr(24, "field", "Supplier", 20, { label: "" }),
            pr(30, "entity", "A_Supplier", 1, { label: "Supplier", purpose: "Who delivers the goods" }),
            pr(31, "field", "Supplier", 30, { label: "Supplier number", is_key: true, type_name: "Edm.String" }),
            pr(32, "field", "SupplierName", 30, { label: "", readable: false, type_name: "Edm.String" }),
            pr(40, "function", "ReleasePurchaseRequisition", 1, {
                label: "Release requisition", purpose: "Releases a requisition for ordering", writable: true
            }),
            bp(100, "source", "business-partners", null, { label: "Business partners", purpose: "Customers and suppliers as partners" }),
            bp(110, "entity", "A_BusinessPartner", 100, { label: "" }),
            bp(111, "field", "BusinessPartner", 110, { label: "" }),
            bp(112, "field", "BusinessPartnerFullName", 110, { label: "Name" })
        ];
        this.edges = [
            { from: 10, to: 20, kind: "navigation", label: "to_PurchaseReqnItem" },
            { from: 20, to: 30, kind: "association", label: "to_Supplier" },
            { from: 24, to: 31, kind: "foreign_key", label: "Supplier" }
        ];
    }

    private static node(
        id: number, kind: DataNodeKind, sourceKind: DataSourceKind, sourceName: string, name: string,
        parent: number | null, hints: Partial<DataNode>
    ): DataNode {
        return {
            id, kind, source_kind: sourceKind, source_name: sourceName, name,
            label: "", purpose: "", not_for: "", synonyms: [], examples: [], unit: "",
            readable: true, writable: false, is_key: false, type_name: "", parent_id: parent,
            updated_at: "2026-11-01T08:30:00+00:00", curated_at: null, ...hints
        };
    }

    private sourceNodes(kind: string, name: string): DataNode[] {
        return this.nodes.filter((node) => node.source_kind === kind && node.source_name === name);
    }

    /** The sources an agent attaches; every source without an agent. */
    private scope(agent: string | undefined): DataSourceSummary[] {
        return agent ? this.sources.filter((source) => source.used_by.indexOf(agent) !== -1) : this.sources;
    }

    private inScope(node: DataNode, scope: DataSourceSummary[]): boolean {
        return scope.some((source) => source.kind === node.source_kind && source.name === node.source_name);
    }

    /** Answers a data-catalog route, or `undefined` for any other path.
     *  `agents`: the names of the agents that exist (an unknown one is a 404). */
    public handle(
        path: string, method: string, body: Record<string, unknown> | undefined, query: URLSearchParams, json: Json,
        agents: string[] = []
    ): Promise<Response> | undefined {
        if (!/^data-catalog\//.test(path)) {
            return undefined;
        }
        const sub = path.replace(/^data-catalog\//, "");
        if (sub === "sources" && method === "GET") {
            return json({ sources: this.sources });
        }
        const detail = /^sources\/([^/]+)\/([^/]+)$/.exec(sub);
        if (detail && method === "GET") {
            return this.detail(decodeURIComponent(detail[1]), decodeURIComponent(detail[2]), json);
        }
        const hints = /^nodes\/([^/]+)\/hints$/.exec(sub);
        if (hints && method === "PUT") {
            return this.updateHints(decodeURIComponent(hints[1]), body ?? {}, json);
        }
        const agent = (sub === "graph" ? query.get("agent") : body?.agent as string | undefined) || undefined;
        if ((sub === "graph" || sub === "search") && agent && agents.indexOf(agent) === -1) {
            return json({ detail: "Agent not found" }, 404);
        }
        if (sub === "search" && method === "POST") {
            return this.search(String(body?.question ?? ""), body?.agent as string | undefined, json);
        }
        if (sub === "graph" && method === "GET") {
            return this.graph(query, json);
        }
        if (sub === "rebuild" && method === "POST") {
            const source = body?.source as { kind: string; name: string } | undefined;
            const count = source ? this.sourceNodes(source.kind, source.name).length : this.nodes.length;
            return json({ imported: count, removed: 0, vectors: count });
        }
        return json({ detail: "Not Found" }, 404);
    }

    private detail(kind: string, name: string, json: Json): Promise<Response> {
        const summary = this.sources.filter((source) => source.kind === kind && source.name === name)[0];
        if (["odata", "ina", "generic"].indexOf(kind) === -1 || !summary) {
            return json({ detail: "Source not found" }, 404);
        }
        const nodes = this.sourceNodes(kind, name);
        const source = nodes.filter((node) => node.kind === "source")[0];
        if (!source) {
            return json({ source: null, entities: [], functions: [], edges: [], used_by: summary.used_by });
        }
        const children = nodes.filter((node) => node.parent_id === source.id);
        const entities = children.filter((node) => node.kind === "entity").map((entity) => ({
            node: entity, fields: nodes.filter((node) => node.parent_id === entity.id)
        }));
        const functions = children.filter((node) => node.kind === "function");
        const ids = nodes.map((node) => String(node.id));
        const byId = (id: number | string) => this.nodes.filter((other) => String(other.id) === String(id))[0] ?? null;
        const edges = this.edges.filter((edge) => ids.indexOf(String(edge.from)) !== -1)
            .map((edge) => ({ ...edge, from_node: byId(edge.from), to_node: byId(edge.to) }));
        return json({ source, entities, functions, edges, used_by: summary.used_by });
    }

    private updateHints(id: string, body: Record<string, unknown>, json: Json): Promise<Response> {
        const node = this.nodes.filter((candidate) => String(candidate.id) === id)[0];
        if (!node) {
            return json({ detail: "Node not found" }, 404);
        }
        // The brief's refusal of an empty label, in FastAPI's `detail[]`. The
        // real route stores an empty label; the rest is its `_refusal` form.
        if ("label" in body && String(body.label ?? "").trim() === "") {
            return json({ detail: [{ loc: ["body", "label"], msg: "Value error, label: must not be empty", type: "value_error" }] }, 422);
        }
        const problems: string[] = [];
        Object.keys(body).filter((key) => HINT_KEYS.indexOf(key) === -1)
            .forEach((key) => problems.push(`${key}: Extra inputs are not permitted`));
        if ("readable" in body && typeof body.readable !== "boolean") {
            problems.push("readable: Input should be a valid boolean");
        }
        ["label", "purpose", "not_for"].forEach((key) => {
            if (/[\r\n]/.test(String(body[key] ?? ""))) {
                problems.push(`${key}: Value error, must be one line of text without control characters`);
            }
        });
        ([["synonyms", 50], ["examples", 20]] as const).forEach(([key, max]) => {
            if (Array.isArray(body[key]) && (body[key] as unknown[]).length > max) {
                problems.push(`${key}: List should have at most ${max} items after validation`);
            }
        });
        if (problems.length) {
            return json({ detail: problems.join("; ") }, 422);
        }
        if (body.readable === true && !node.readable
            && this.catalogueHidden.map(String).indexOf(String(node.id)) !== -1) {
            return json({ detail: NOT_READABLE }, 422);
        }
        Object.assign(node, body, { curated_at: "2026-11-01T10:00:00+00:00", updated_at: "2026-11-01T10:00:00+00:00" });
        return json(node);
    }

    /** A lexical stand-in for the retrieval: words of the question against names and hints. */
    private search(question: string, agent: string | undefined, json: Json): Promise<Response> {
        const words = question.toLowerCase().split(/[^a-z0-9]+/).filter((word) => word.length > 2);
        const scope = this.scope(agent);
        const candidates: DataSearchCandidate[] = this.nodes
            .filter((node) => node.kind === "entity" && this.inScope(node, scope))
            .map((node) => {
                const text = [node.name, node.label, node.purpose, ...node.synonyms].join(" ").toLowerCase();
                const hits = words.filter((word) => text.indexOf(word) !== -1).length;
                return { node, score: words.length ? hits / words.length : 0 };
            })
            .filter((entry) => entry.score > 0)
            .sort((a, b) => b.score - a.score)
            .slice(0, 5)
            .map(({ node, score }) => this.candidate(node, score));
        return json({ candidates, scope: agent ? "agent" : "all" });
    }

    private candidate(node: DataNode, score: number): DataSearchCandidate {
        const byId = (id: number | string) => this.nodes.filter((other) => String(other.id) === String(id))[0];
        const keyFields = this.nodes.filter((field) => field.parent_id === node.id).slice(0, 1).map((field) => field.name);
        const relations = this.edges.filter((edge) => String(edge.from) === String(node.id)).map((edge) => {
            const to = byId(edge.to);
            return { kind: edge.kind, to: { source_name: to.source_name, name: to.name, label: to.label } };
        });
        return {
            source_kind: node.source_kind, source_name: node.source_name, kind: node.kind, name: node.name,
            label: node.label, purpose: node.purpose, not_for: node.not_for, keys: keyFields, relations,
            score: Math.round(score * 100) / 100
        };
    }

    private graph(query: URLSearchParams, json: Json): Promise<Response> {
        if ((query.get("q") ?? "").length > 200) {
            return json({ detail: "q: at most 200 characters" }, 422);
        }
        const scope = this.scope(query.get("agent") || undefined);
        const kinds = (query.get("kind") ?? "").split(",").filter(Boolean);
        const q = (query.get("q") ?? "").trim().toLowerCase();
        const nodes: DataGraphNode[] = this.nodes
            .filter((node) => this.inScope(node, scope))
            .filter((node) => !kinds.length || kinds.indexOf(node.kind) !== -1)
            .filter((node) => !q || `${node.name} ${node.label}`.toLowerCase().indexOf(q) !== -1)
            .map((node) => ({
                id: node.id, kind: node.kind, source_kind: node.source_kind, source_name: node.source_name,
                name: node.name, label: node.label, group: `${node.source_kind}:${node.source_name}`
            }));
        const shown = nodes.map((node) => String(node.id));
        const parentEdges: DataEdge[] = this.nodes
            .filter((node) => node.parent_id !== null && shown.indexOf(String(node.id)) !== -1
                && shown.indexOf(String(node.parent_id)) !== -1)
            .map((node) => ({ from: node.parent_id as number, to: node.id, kind: "contains", label: "" }));
        const edges = this.edges
            .filter((edge) => shown.indexOf(String(edge.from)) !== -1 && shown.indexOf(String(edge.to)) !== -1)
            .concat(parentEdges);
        return json({ nodes: nodes.slice(0, 2000), edges, truncated: nodes.length > 2000 });
    }
}
