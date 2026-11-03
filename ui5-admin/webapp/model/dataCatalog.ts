import odataCatalog from "./odataCatalog";
import type {
    DataEdge, DataEdgeEnd, DataGraph, DataHintsUpdate, DataLinkKind, DataNode, DataNodeKind, DataSearchCandidate,
    DataSourceDetail, DataSourceKind
} from "../service/dataCatalogTypes";

/**
 * The logic of the data catalogue pages, without a view: what the list,
 * the detail and the graph show of an answer, and what a hint edit sends.
 * Every function here is pure; the controllers only wire them to controls.
 */

/** The node kinds the graph can be filtered on, in the order they are offered. */
export const NODE_KINDS: readonly DataNodeKind[] = [
    "source", "entity", "field", "query", "dimension", "measure", "function"
];

/** The node kinds the graph opens with: every kind but `field`. One service
 *  of 4 entities and 53 fields would otherwise be drawn at 10% and nothing
 *  could be read; fields are shown once the admin picks them. */
export const DEFAULT_GRAPH_KINDS: readonly DataNodeKind[] = NODE_KINDS.filter((kind) => kind !== "field");

/** The source kinds, in the order of the legend. */
export const SOURCE_KINDS: readonly DataSourceKind[] = ["odata", "ina", "generic"];

/** The graph API answers at most this many nodes; reaching it means some were left out. */
export const GRAPH_NODE_CAP = 2000;

/** Below this share of curated nodes a source is shown as a warning. */
export const COMPLETENESS_WARNING = 0.5;

/** The completeness as a share between 0 and 1; anything that is not a number is 0. */
function share(completeness: unknown): number {
    const value = typeof completeness === "number" && isFinite(completeness) ? completeness : 0;
    return Math.min(1, Math.max(0, value));
}

/** 0..100, for a `ProgressIndicator`'s `percentValue`. */
export function completenessPercent(completeness: unknown): number {
    return Math.round(share(completeness) * 100);
}

/** "62%". */
export function completenessText(completeness: unknown): string {
    return `${completenessPercent(completeness)}%`;
}

/** `Warning` below half, `Success` from there. */
export function completenessState(completeness: unknown): "Warning" | "Success" {
    return share(completeness) < COMPLETENESS_WARNING ? "Warning" : "Success";
}

/** The `Status` key the graph colours a source kind with. */
export function statusKey(sourceKind: string): string {
    return `dc-${SOURCE_KINDS.indexOf(sourceKind as DataSourceKind) === -1 ? "generic" : sourceKind}`;
}

/** What a node is called on screen: its label, else its name. */
export function displayName(node: { label?: string | null; name: string }): string {
    return (node.label ?? "").trim() || node.name;
}

/** The icon of each node kind on the detail page and in the link search. */
const NODE_ICONS: Record<string, string> = {
    source: "sap-icon://database",
    entity: "sap-icon://table-view",
    field: "sap-icon://text-formatting",
    query: "sap-icon://multiple-bar-chart",
    dimension: "sap-icon://dimension",
    measure: "sap-icon://measure",
    function: "sap-icon://fx"
};

/** The icon of a node kind; a kind this page does not know gets a plain document. */
export function nodeIcon(kind: DataNodeKind): string {
    return Object.prototype.hasOwnProperty.call(NODE_ICONS, kind) ? NODE_ICONS[kind] : "sap-icon://document";
}

/** One line of the entity and field table of the detail page (for an InA
 *  source: the query and its dimensions and measures). */
export interface EntityRow {
    id: number | string;
    /** 0 for an entity (or query, or function), 1 for its fields (or the query's members). */
    depth: number;
    kind: DataNodeKind;
    name: string;
    /** The entity (or query) a row belongs to, by name; its own name for a row at depth 0. */
    entity: string;
    icon: string;
    label: string;
    /** The label as stored, to tell an edit from what is there. */
    savedLabel: string;
    purpose: string;
    unit: string;
    readable: boolean;
    writable: boolean;
    isKey: boolean;
    typeName: string;
    /** The value state of the label input: "None" or "Error" with `labelError`. */
    labelState: string;
    labelError: string;
}

function rowOf(node: DataNode, depth: number, entity: string): EntityRow {
    return {
        id: node.id, depth, kind: node.kind, name: node.name, entity, icon: nodeIcon(node.kind),
        label: node.label ?? "", savedLabel: node.label ?? "", purpose: node.purpose ?? "",
        unit: node.unit ?? "", readable: node.readable === true, writable: node.writable === true,
        isKey: node.is_key === true, typeName: node.type_name ?? "",
        labelState: "None", labelError: ""
    };
}

/** Each entity followed by its fields, in the order of the answer, then
 *  the source's functions (one level, like an entity without fields). An
 *  InA source: its query, followed by its dimensions and then its measures. */
export function entityRows(detail: Pick<DataSourceDetail, "entities" | "functions" | "query">): EntityRow[] {
    const rows: EntityRow[] = [];
    (detail.entities ?? []).forEach((entry) => {
        rows.push(rowOf(entry.node, 0, entry.node.name));
        (entry.fields ?? []).forEach((field) => rows.push(rowOf(field, 1, entry.node.name)));
    });
    (detail.functions ?? []).forEach((fn) => rows.push(rowOf(fn, 0, fn.name)));
    if (detail.query) {
        const query = detail.query.node;
        rows.push(rowOf(query, 0, query.name));
        (detail.query.dimensions ?? []).concat(detail.query.measures ?? [])
            .forEach((member) => rows.push(rowOf(member, 1, query.name)));
    }
    return rows;
}

/** The edge kinds of the catalogue (`EDGE_KINDS` of agents/data_catalog/models.py). */
const EDGE_KINDS: readonly string[] = ["navigation", "association", "foreign_key", "same_object", "aggregates"];

/** The kinds of link an admin may draw (`CURATED_EDGE_KINDS`), in the order the Select offers them. */
export const LINK_KINDS: readonly DataLinkKind[] = ["same_object", "aggregates"];

/** The i18n key of a relation's kind; "" for a kind the catalogue does not name (shown as it is). */
export function relationKindKey(kind: string): string {
    return EDGE_KINDS.indexOf(kind) === -1 ? "" : `dcRelKind_${kind}`;
}

/** One relation as the Links table of the detail page lists it. */
export interface RelationRow {
    /** What `DELETE data-catalog/edges/{id}` takes. */
    id: number | string;
    kind: string;
    label: string;
    from: string;
    to: string;
    /** An admin drew it: only such a link can be removed (an import made the others). */
    curated: boolean;
    /** "Added by an admin" or "Imported"; the i18n key when no text function is given. */
    origin: string;
}

/** The text of the origin of a link. */
export type TextOf = (key: string) => string;

/** The nodes of the answer by id, with the names the detail page gives them:
 *  a field (or a query's member) after its entity's (or query's) name. */
function ownNames(detail: Pick<DataSourceDetail, "source" | "entities" | "functions" | "query">): Record<string, string> {
    const names: Record<string, string> = {};
    const note = (node: DataNode, prefix = ""): void => {
        names[String(node.id)] = prefix + displayName(node);
    };
    if (detail.source) {
        note(detail.source);
    }
    (detail.entities ?? []).forEach((entry) => {
        note(entry.node);
        (entry.fields ?? []).forEach((field) => note(field, `${displayName(entry.node)}.`));
    });
    (detail.functions ?? []).forEach((fn) => note(fn));
    if (detail.query) {
        const query = detail.query.node;
        note(query);
        (detail.query.dimensions ?? []).concat(detail.query.measures ?? [])
            .forEach((member) => note(member, `${displayName(query)}.`));
    }
    return names;
}

/** The edges of a source with the names of both ends: the node of the
 *  answer, else the node the edge carries (`from_node` / `to_node`), else
 *  the id. An end in another source is named "source · node". `curated` only
 *  for the boolean `true`: a remove is offered on nothing else. */
export function relationRows(
    detail: Pick<DataSourceDetail, "source" | "entities" | "edges" | "functions" | "query">,
    text: TextOf = (key) => key
): RelationRow[] {
    const names = ownNames(detail);
    const own = detail.source ? detail.source.source_name : "";
    const end = (id: number | string, carried: DataEdgeEnd | null | undefined): string => {
        if (names[String(id)] !== undefined) {
            return names[String(id)];
        }
        if (carried) {
            const name = displayName(carried);
            return carried.source_name && carried.source_name !== own ? `${carried.source_name} · ${name}` : name;
        }
        return String(id);
    };
    return (detail.edges ?? []).map((edge: DataEdge) => {
        const curated = edge.curated === true;
        return {
            id: edge.id,
            kind: edge.kind,
            label: edge.label ?? "",
            from: end(edge.from, edge.from_node),
            to: end(edge.to, edge.to_node),
            curated,
            origin: text(curated ? "dcLinkOriginAdmin" : "dcLinkOriginImported")
        };
    });
}

/** The node kinds a link joins (`search.CANDIDATE_KINDS`), in the order the graph route is asked for them. */
export const LINK_NODE_KINDS: readonly DataNodeKind[] = ["entity", "query", "function"];

/** The two kinds of a catalogue service: one name is one row of the
 *  catalogue, whatever its kind, so the server treats both as one source. */
const CATALOGUE_KINDS: readonly string[] = ["odata", "generic"];

/** Whether two nodes belong to one source, as `POST edges` decides it. */
export function sameSource(
    a: { source_kind: string; source_name: string }, b: { source_kind: string; source_name: string }
): boolean {
    return a.source_name === b.source_name && (a.source_kind === b.source_kind
        || (CATALOGUE_KINDS.indexOf(a.source_kind) !== -1 && CATALOGUE_KINDS.indexOf(b.source_kind) !== -1));
}

/** A node of this source a link can start at, as the From select offers it. */
export interface LinkStart {
    key: string;
    text: string;
    kind: DataNodeKind;
}

/** The entities and functions of an OData or generic source, or the query
 *  of an InA source: what may be linked (no field, no member). None for a
 *  source that was never imported. */
export function linkStarts(detail: Pick<DataSourceDetail, "source" | "entities" | "functions" | "query">): LinkStart[] {
    if (!detail.source) {
        return [];
    }
    const nodes: DataNode[] = (detail.entities ?? []).map((entry) => entry.node)
        .concat(detail.functions ?? [])
        .concat(detail.query ? [detail.query.node] : []);
    return nodes.map((node) => ({ key: String(node.id), text: displayName(node), kind: node.kind }));
}

/** A node of another source the Add link dialog offers. */
export interface LinkCandidate {
    id: number | string;
    title: string;
    /** "agent-data · entity SCARRSet", as the test search names a candidate. */
    subtitle: string;
    kind: DataNodeKind;
    icon: string;
}

/** The query of `GET graph` for the Add link search: the kinds a link
 *  joins, and the typed text when there is one. */
export function linkQuery(q: string): string {
    return graphQuery({ kinds: LINK_NODE_KINDS, q });
}

/** The nodes of a graph answer a link from `source` may go to: entities,
 *  queries and functions of OTHER sources, in the order of the answer. */
export function linkCandidates(graph: DataGraph, source: { kind: string; name: string }): LinkCandidate[] {
    const own = { source_kind: source.kind, source_name: source.name };
    return (graph.nodes ?? [])
        .filter((node) => LINK_NODE_KINDS.indexOf(node.kind) !== -1 && !sameSource(node, own))
        .map((node) => ({
            id: node.id,
            title: displayName(node),
            subtitle: `${node.source_name} · ${node.kind} ${node.name}`,
            kind: node.kind,
            icon: nodeIcon(node.kind)
        }));
}

/** The hints of a node as the editor holds them. */
export interface HintsForm {
    label: string;
    purpose: string;
    not_for: string;
    synonyms: string[];
    examples: string[];
    readable?: boolean;
}

/** The editable hints of `node`, copied. */
export function hintsOf(node: DataNode): HintsForm {
    return {
        label: node.label ?? "",
        purpose: node.purpose ?? "",
        not_for: node.not_for ?? "",
        synonyms: (node.synonyms ?? []).slice(),
        examples: (node.examples ?? []).slice(),
        readable: node.readable === true
    };
}

/** A list as it is sent: entries trimmed, empty ones and repeats dropped. */
export function cleanList(values: readonly string[] | undefined): string[] {
    const seen: string[] = [];
    (values ?? []).forEach((value) => {
        const text = String(value ?? "").trim();
        if (text && seen.indexOf(text) === -1) {
            seen.push(text);
        }
    });
    return seen;
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
    return a.length === b.length && a.every((value, index) => value === b[index]);
}

/**
 * The body of `PUT nodes/{id}/hints`: only the fields that differ from what
 * is stored. Texts are sent as typed (the server decides what it accepts);
 * the lists are cleaned first. `{}` means nothing to save.
 */
export function hintsUpdate(saved: HintsForm, edited: HintsForm): DataHintsUpdate {
    const body: DataHintsUpdate = {};
    (["label", "purpose", "not_for"] as const).forEach((field) => {
        if ((edited[field] ?? "") !== (saved[field] ?? "")) {
            body[field] = edited[field] ?? "";
        }
    });
    (["synonyms", "examples"] as const).forEach((field) => {
        const next = cleanList(edited[field]);
        if (!sameList(next, cleanList(saved[field]))) {
            body[field] = next;
        }
    });
    if (edited.readable !== undefined && edited.readable !== saved.readable) {
        body.readable = edited.readable;
    }
    return body;
}

/** The hint fields a refusal can name. */
const HINT_FIELDS: readonly string[] = ["label", "purpose", "not_for", "synonyms", "examples", "readable"];

/**
 * A 422 of `PUT nodes/{id}/hints` by hint field. The route answers one string
 * (`"<loc>: <msg>; <loc>: <msg>"`, as the OData catalogue routes do and read
 * by the same `serverErrors`); FastAPI's `detail[]`, flattened by
 * `AdminError.fieldErrors`, is read too. `synonyms.1` counts as `synonyms`.
 * Empty: the refusal names no hint field, so show it as a whole.
 */
export function hintErrors(fieldErrors: Record<string, string> | undefined, detail: string | undefined): Record<string, string> {
    const named = { ...odataCatalog.serverErrors(detail), ...(fieldErrors ?? {}) };
    const errors: Record<string, string> = {};
    Object.keys(named).forEach((key) => {
        const field = key.split(".")[0];
        if (HINT_FIELDS.indexOf(field) !== -1 && !errors[field]) {
            errors[field] = named[key];
        }
    });
    return errors;
}

/** Whether a hints body changes anything. */
export function hasChanges(body: DataHintsUpdate): boolean {
    return Object.keys(body).length > 0;
}

/** A search candidate as the test search lists it. */
export interface CandidateRow {
    title: string;
    /** "purchase-requisitions · entity A_PurchaseRequisitionItem". */
    subtitle: string;
    /** Two decimals. */
    score: string;
    purpose: string;
    notFor: string;
    keys: string;
    /** "navigation → Supplier (business-partners)". */
    relations: string[];
}

/** "0.87"; a score that is not a number is "–". */
export function scoreText(score: unknown): string {
    return typeof score === "number" && isFinite(score) ? score.toFixed(2) : "–";
}

export function candidateRow(candidate: DataSearchCandidate): CandidateRow {
    return {
        title: displayName(candidate),
        subtitle: `${candidate.source_name} · ${candidate.kind} ${candidate.name}`,
        score: scoreText(candidate.score),
        purpose: candidate.purpose ?? "",
        notFor: candidate.not_for ?? "",
        keys: (candidate.keys ?? []).join(", "),
        relations: (candidate.relations ?? []).map((relation) =>
            `${relation.kind} → ${displayName(relation.to)} (${relation.to.source_name})`)
    };
}

/** The candidates best first, as rows. */
export function candidateRows(candidates: readonly DataSearchCandidate[] | undefined): CandidateRow[] {
    return (candidates ?? []).slice()
        .sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0))
        .map(candidateRow);
}

/** What the networkgraph is bound to. */
export interface GraphModel {
    groups: { key: string; title: string; status: string }[];
    nodes: {
        key: string; title: string; description: string; group: string; status: string; shape: string;
        kind: DataNodeKind; source_kind: DataSourceKind; source_name: string;
    }[];
    /** `title` is the edge's kind, `description` its label; the control
     *  shows both in the line's popover, it draws no text on a line.
     *  `curated`: an admin drew it, drawn dashed in both views. */
    lines: { from: string; to: string; title: string; description: string; curated: boolean }[];
    /** The API answered as many nodes as it may: some may be missing. */
    capped: boolean;
}

/** The group key of a source: kind and name, as a source name is unique per kind only. */
export function groupKey(sourceKind: string, sourceName: string): string {
    return `${sourceKind}:${sourceName}`;
}

/**
 * The graph answer as the control needs it: one group per source, coloured
 * by its kind, the nodes in their source's group, and only the edges whose
 * two ends are shown (the control fails on a line to a node it does not
 * have). `kinds`, when not empty, keeps only nodes of those kinds.
 */
export function graphModel(answer: DataGraph, kinds: readonly string[] = []): GraphModel {
    const allNodes = answer.nodes ?? [];
    const nodes = kinds.length ? allNodes.filter((node) => kinds.indexOf(node.kind) !== -1) : allNodes;
    const groups: GraphModel["groups"] = [];
    const seenGroups: Record<string, boolean> = {};
    const shown: Record<string, boolean> = {};
    const mapped = nodes.map((node) => {
        const group = node.group || groupKey(node.source_kind, node.source_name);
        if (!seenGroups[group]) {
            seenGroups[group] = true;
            groups.push({ key: group, title: node.source_name, status: statusKey(node.source_kind) });
        }
        shown[String(node.id)] = true;
        const boxed = node.kind === "source" || node.kind === "entity" || node.kind === "query";
        return {
            key: String(node.id), title: displayName(node), description: `${node.kind} ${node.name}`,
            group, status: statusKey(node.source_kind), shape: boxed ? "Box" : "Circle",
            kind: node.kind, source_kind: node.source_kind, source_name: node.source_name
        };
    });
    const lines = (answer.edges ?? [])
        .filter((edge) => shown[String(edge.from)] && shown[String(edge.to)])
        .map((edge) => ({
            from: String(edge.from), to: String(edge.to), title: edge.kind, description: edge.label ?? "",
            curated: edge.curated === true
        }));
    return { groups, nodes: mapped, lines, capped: answer.truncated === true || allNodes.length >= GRAPH_NODE_CAP };
}

/**
 * The largest of the control's zoom steps at which `content` fits `view`,
 * never above 100%; the smallest step when none fits, 1 for an empty graph.
 * Sizes are at 100%.
 */
export function fitZoom(
    content: { width: number; height: number }, view: { width: number; height: number }, steps: readonly number[]
): number {
    if (!(content.width > 0 && content.height > 0 && view.width > 0 && view.height > 0)) {
        return 1;
    }
    const ratio = Math.min(1, view.width / content.width, view.height / content.height);
    const sorted = steps.filter((step) => step > 0).sort((a, b) => a - b);
    const fitting = sorted.filter((step) => step <= ratio);
    return fitting.length ? fitting[fitting.length - 1] : (sorted[0] ?? 1);
}

/** The query string of `GET graph`: only the filters that are set. The
 *  picked kinds go comma-separated, so the route applies them before its
 *  node cap; every kind picked is no filter. `graphModel` keeps the picked
 *  kinds on the client as well. */
export function graphQuery(filters: { agent?: string; kinds?: readonly string[]; q?: string }): string {
    const params: string[] = [];
    if (filters.agent) {
        params.push(`agent=${encodeURIComponent(filters.agent)}`);
    }
    const kinds = filters.kinds ?? [];
    if (kinds.length && NODE_KINDS.some((kind) => kinds.indexOf(kind) === -1)) {
        params.push(`kind=${kinds.map((kind) => encodeURIComponent(kind)).join(",")}`);
    }
    const q = (filters.q ?? "").trim();
    if (q) {
        params.push(`q=${encodeURIComponent(q)}`);
    }
    return params.length ? `?${params.join("&")}` : "";
}

export default {
    NODE_KINDS, DEFAULT_GRAPH_KINDS, SOURCE_KINDS, GRAPH_NODE_CAP, COMPLETENESS_WARNING, LINK_KINDS, LINK_NODE_KINDS,
    completenessPercent, completenessText, completenessState, statusKey, displayName, nodeIcon, entityRows,
    relationKindKey, relationRows, sameSource, linkStarts, linkQuery, linkCandidates,
    hintsOf, cleanList, hintsUpdate, hintErrors, hasChanges, scoreText, candidateRow, candidateRows,
    groupKey, graphModel, graphQuery, fitZoom
};
