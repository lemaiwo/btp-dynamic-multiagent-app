import type {
    DataEdge, DataEdgeEnd, DataGraphEdge, DataGraphNode, DataNode, DataNodeKind, DataSearchCandidate, DataSourceKind,
    DataSourceSummary
} from "com/agent/admin/service/dataCatalogTypes";

type Json = (body: unknown, status?: number) => Promise<Response>;

/** The keys `PUT nodes/{id}/hints` accepts; anything else is a 422. */
const HINT_KEYS = ["label", "purpose", "not_for", "synonyms", "examples", "readable"];

/** The answer to `readable: true` on a node the OData catalogue hides. */
export const NOT_READABLE = "readable: the catalogue does not let agents read this node";

/** What an admin may link, and by which kinds (`LINKABLE_KINDS`, `CURATED_EDGE_KINDS`). */
const LINKABLE_KINDS = ["entity", "query", "function"];
const LINK_KINDS = ["same_object", "aggregates"];
const MAX_ID = 2147483647;
/** The catalogue services' two kinds: one name is one row of `odata_services`. */
const CATALOGUE_KINDS = ["odata", "generic"];
/** `LOC_FIELD_RE` of agents/loc_fields.py: a key that may be repeated in a refusal. */
const LOC_FIELD_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

/** The fixed texts of the edge routes (agents/data_catalog/routes.py). */
export const LINK_NOT_FOUND = "Link not found";
export const LINK_IMPORTED = "Only a link an admin drew can be removed; an import made this one";
export const ALREADY_LINKED = "The two nodes are already linked";

/**
 * The `/admin/api/data-catalog/...` routes of the fake backend, apart from
 * `FakeBackend` so neither file grows past its size. Three OData sources: a
 * requisition service with three entities and relations between them, used
 * by btp-agent, a small business-partner service used by gmail-agent and one
 * never imported. A generic source (ZAGENT_DATA: the demo airlines and a
 * function) and an InA source (a query with two dimensions and a measure,
 * used by gmail-agent), joined by a link an admin drew.
 */
export default class FakeDataCatalog {

    public sources: DataSourceSummary[] = [];
    public nodes: DataNode[] = [];
    public edges: DataEdge[] = [];
    /** Nodes the OData catalogue does not let agents read: `readable: true` is refused. */
    public catalogueHidden: (number | string)[] = [];
    private nextEdgeId = 1;

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
            },
            {
                kind: "generic", name: "agent-data", title: "Agent data (ZAGENT_DATA)",
                destination: "S4_ODATA_TECH", user_context: false, enabled: true, used_by: [],
                nodes: 5, last_import: "2026-11-03T09:00:00+00:00", hint_completeness: 0
            },
            {
                kind: "ina", name: "flight-revenue", title: "Flight revenue",
                destination: "S4_ODATA_USER", user_context: true, enabled: true, used_by: ["gmail-agent"],
                nodes: 5, last_import: "2026-11-03T09:00:00+00:00", hint_completeness: 0
            }
        ];
        this.catalogueHidden = [32];
        const pr = (id: number, kind: DataNodeKind, name: string, parent: number | null, hints: Partial<DataNode> = {}) =>
            FakeDataCatalog.node(id, kind, "odata", "purchase-requisitions", name, parent, hints);
        const bp = (id: number, kind: DataNodeKind, name: string, parent: number | null, hints: Partial<DataNode> = {}) =>
            FakeDataCatalog.node(id, kind, "odata", "business-partners", name, parent, hints);
        const ad = (id: number, kind: DataNodeKind, name: string, parent: number | null, hints: Partial<DataNode> = {}) =>
            FakeDataCatalog.node(id, kind, "generic", "agent-data", name, parent, hints);
        const fr = (id: number, kind: DataNodeKind, name: string, parent: number | null, hints: Partial<DataNode> = {}) =>
            FakeDataCatalog.node(id, kind, "ina", "flight-revenue", name, parent, hints);
        this.nodes = [
            pr(1, "source", "purchase-requisitions", null, {
                label: "Purchase requisitions", purpose: "Requests to buy goods or services, before an order",
                not_for: "Purchase orders and invoices", synonyms: ["PR", "requisition", "purchase request"],
                examples: ["Which requisitions are waiting for my approval?", "How many PRs did plant 1010 create this week?"]
            }),
            pr(10, "entity", "A_PurchaseRequisitionHeader", 1, {
                label: "Requisition", purpose: "One purchase requisition with its description",
                synonyms: ["PR header"], type_name: "API_PURCHASEREQ_PROCESS_SRV.A_PurchaseRequisitionHeaderType"
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
            bp(112, "field", "BusinessPartnerFullName", 110, { label: "Name" }),
            ad(200, "source", "agent-data", null, { label: "Agent data", purpose: "Demo data of the generic service" }),
            ad(210, "entity", "SCARRSet", 200, { label: "Demo airlines", type_name: "ZAGENT_DATA_SRV.SCARR" }),
            ad(211, "field", "CARRID", 210, { label: "Airline", is_key: true, type_name: "Edm.String" }),
            ad(212, "field", "CARRNAME", 210, { label: "Airline name", type_name: "Edm.String" }),
            ad(220, "function", "MONTH_NAMES_GET", 200, { label: "Month names", purpose: "The names of the months" }),
            fr(300, "source", "flight-revenue", null, { label: "Flight revenue", purpose: "Revenue per airline and month" }),
            fr(310, "query", "ZFLIGHT_REV_Q001", 300, { label: "Flight revenue by month" }),
            fr(311, "dimension", "0CALMONTH", 310, { label: "Calendar month" }),
            fr(312, "dimension", "ZCARRID", 310, { label: "Airline" }),
            fr(313, "measure", "ZREVENUE", 310, { label: "Revenue", unit: "EUR" })
        ];
        this.nextEdgeId = 1;
        this.edges = [
            this.edge(10, 20, "navigation", "to_PurchaseReqnItem"),
            this.edge(20, 30, "association", "to_Supplier"),
            this.edge(24, 31, "foreign_key", "Supplier"),
            { ...this.edge(310, 210, "aggregates", ""), curated: true }
        ];
    }

    /** An imported edge; the detail adds its two ends. */
    private edge(from: number, to: number, kind: string, label: string): DataEdge {
        return { id: this.nextEdgeId++, from, to, kind, label, curated: false };
    }

    private nodeOf(id: number | string): DataNode | undefined {
        return this.nodes.filter((node) => String(node.id) === String(id))[0];
    }

    /** An edge's end as the detail answers it: the node in brief. */
    private endOf(id: number | string): DataEdgeEnd | null {
        const node = this.nodeOf(id);
        return node ? {
            id: node.id, kind: node.kind, source_kind: node.source_kind, source_name: node.source_name,
            name: node.name, label: node.label || node.name
        } : null;
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
        if (sub === "edges" && method === "POST") {
            return this.createEdge(body, json);
        }
        const edge = /^edges\/([^/]+)$/.exec(sub);
        if (edge && method === "DELETE") {
            return this.deleteEdge(decodeURIComponent(edge[1]), json);
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
        const shape = (parts: Record<string, unknown>) =>
            json({ source: source ?? null, ...parts, edges: source ? this.edgesOf(nodes) : [], used_by: summary.used_by });
        if (kind === "ina") {
            // One query with its members; `null` before the first import.
            const query = nodes.filter((node) => node.kind === "query")[0];
            const members = (memberKind: DataNodeKind) =>
                nodes.filter((node) => node.kind === memberKind && query && node.parent_id === query.id);
            return shape({
                query: source && query ? { node: query, dimensions: members("dimension"), measures: members("measure") } : null
            });
        }
        if (!source) {
            return shape({ entities: [], functions: [] });
        }
        const children = nodes.filter((node) => node.parent_id === source.id);
        const entities = children.filter((node) => node.kind === "entity").map((entity) => ({
            node: entity, fields: nodes.filter((node) => node.parent_id === entity.id)
        }));
        const functions = children.filter((node) => node.kind === "function");
        return shape({ entities, functions });
    }

    /** Every edge that starts or ends in `nodes`, in id order, with both ends. */
    private edgesOf(nodes: DataNode[]): DataEdge[] {
        const ids = nodes.map((node) => String(node.id));
        return this.edges
            .filter((edge) => ids.indexOf(String(edge.from)) !== -1 || ids.indexOf(String(edge.to)) !== -1)
            .sort((a, b) => a.id - b.id)
            .map((edge) => ({ ...edge, from_node: this.endOf(edge.from), to_node: this.endOf(edge.to) }));
    }

    /** `POST edges`: the body's own gate, then the two nodes, then the pair. */
    private createEdge(body: unknown, json: Json): Promise<Response> {
        if (typeof body !== "object" || body === null || Array.isArray(body)) {
            return json({ detail: "body: expected a JSON object" }, 422);
        }
        const given = body as Record<string, unknown>;
        const problems: string[] = [];
        ["from_id", "to_id"].forEach((key) => {
            const value = given[key];
            if (!(key in given)) {
                problems.push(`${key}: Field required`);
            } else if (typeof value !== "number" || !Number.isInteger(value)) {
                problems.push(`${key}: Input should be a valid integer`);
            } else if (value < 1) {
                problems.push(`${key}: Input should be greater than or equal to 1`);
            } else if (value > MAX_ID) {
                problems.push(`${key}: Input should be less than or equal to ${MAX_ID}`);
            }
        });
        if (!("kind" in given)) {
            problems.push("kind: Field required");
        } else if (typeof given.kind !== "string") {
            problems.push("kind: Input should be a valid string");
        } else if (LINK_KINDS.indexOf(given.kind) === -1) {
            problems.push("kind: Value error, must be same_object or aggregates");
        }
        Object.keys(given).filter((key) => ["from_id", "to_id", "kind"].indexOf(key) === -1).forEach((key) =>
            problems.push(`${LOC_FIELD_RE.test(key) ? key : "<unknown field>"}: Extra inputs are not permitted`));
        if (problems.length) {
            return json({ detail: problems.join("; ") }, 422);
        }
        const ends: DataNode[] = [];
        for (const key of ["from_id", "to_id"]) {
            const node = this.nodeOf(given[key] as number);
            if (!node) {
                return json({ detail: `${key}: not a node of the catalogue` }, 422);
            }
            if (LINKABLE_KINDS.indexOf(node.kind) === -1) {
                return json({ detail: `${key}: not an entity, query or function` }, 422);
            }
            ends.push(node);
        }
        const [start, end] = ends;
        const sameSource = start.source_name === end.source_name && (start.source_kind === end.source_kind
            || (CATALOGUE_KINDS.indexOf(start.source_kind) !== -1 && CATALOGUE_KINDS.indexOf(end.source_kind) !== -1));
        if (sameSource) {
            return json({ detail: "to_id: same source as from_id" }, 422);
        }
        // One link per kind per pair, in either direction, imported or drawn.
        const linked = this.edges.some((edge) => edge.kind === given.kind && (
            (String(edge.from) === String(start.id) && String(edge.to) === String(end.id))
            || (String(edge.from) === String(end.id) && String(edge.to) === String(start.id))));
        if (linked) {
            return json({ detail: ALREADY_LINKED }, 409);
        }
        const created: DataEdge = { ...this.edge(start.id as number, end.id as number, given.kind as string, ""), curated: true };
        this.edges.push(created);
        return json({ ...created, from_node: this.endOf(start.id), to_node: this.endOf(end.id) }, 201);
    }

    /** `DELETE edges/{id}`: only a link an admin drew. */
    private deleteEdge(raw: string, json: Json): Promise<Response> {
        const found = /^\d{1,10}$/.test(raw) ? this.edges.filter((edge) => edge.id === Number(raw))[0] : undefined;
        if (!found) {
            return json({ detail: LINK_NOT_FOUND }, 404);
        }
        if (!found.curated) {
            return json({ detail: LINK_IMPORTED }, 409);
        }
        this.edges.splice(this.edges.indexOf(found), 1);
        return Promise.resolve(new Response(null, { status: 204 }));
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
        const parentEdges: DataGraphEdge[] = this.nodes
            .filter((node) => node.parent_id !== null && shown.indexOf(String(node.id)) !== -1
                && shown.indexOf(String(node.parent_id)) !== -1)
            .map((node) => ({ from: node.parent_id as number, to: node.id, kind: "contains", label: "", curated: false }));
        // As the route answers them: no id, the origin as `curated`.
        const edges: DataGraphEdge[] = this.edges
            .filter((edge) => shown.indexOf(String(edge.from)) !== -1 && shown.indexOf(String(edge.to)) !== -1)
            .map((edge): DataGraphEdge => ({ from: edge.from, to: edge.to, kind: edge.kind, label: edge.label, curated: edge.curated }))
            .concat(parentEdges);
        return json({ nodes: nodes.slice(0, 2000), edges, truncated: nodes.length > 2000 });
    }
}
