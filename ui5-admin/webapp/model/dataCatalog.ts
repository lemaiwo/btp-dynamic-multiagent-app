import odataCatalog from "./odataCatalog";
import type {
    DataEdge, DataGraph, DataHintsUpdate, DataNode, DataNodeKind, DataSearchCandidate, DataSourceDetail,
    DataSourceKind
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

/** One line of the entity and field table of the detail page. */
export interface EntityRow {
    id: number | string;
    /** 0 for an entity (or query), 1 for its fields. */
    depth: number;
    kind: DataNodeKind;
    name: string;
    /** The entity a field belongs to, by name; the entity itself for an entity row. */
    entity: string;
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
        id: node.id, depth, kind: node.kind, name: node.name, entity,
        label: node.label ?? "", savedLabel: node.label ?? "", purpose: node.purpose ?? "",
        unit: node.unit ?? "", readable: node.readable === true, writable: node.writable === true,
        isKey: node.is_key === true, typeName: node.type_name ?? "",
        labelState: "None", labelError: ""
    };
}

/** Each entity followed by its fields, in the order of the answer, then
 *  the source's functions (one level, like an entity without fields). */
export function entityRows(detail: Pick<DataSourceDetail, "entities" | "functions">): EntityRow[] {
    const rows: EntityRow[] = [];
    (detail.entities ?? []).forEach((entry) => {
        rows.push(rowOf(entry.node, 0, entry.node.name));
        (entry.fields ?? []).forEach((field) => rows.push(rowOf(field, 1, entry.node.name)));
    });
    (detail.functions ?? []).forEach((fn) => rows.push(rowOf(fn, 0, fn.name)));
    return rows;
}

/** One relation as the detail page lists it. */
export interface RelationRow {
    kind: string;
    label: string;
    from: string;
    to: string;
}

/** The edges of a source with the names of both ends: the node the edge
 *  carries (`from_node` / `to_node`), else the node of the answer, else the
 *  id. An end in another source is named with that source. */
export function relationRows(
    detail: Pick<DataSourceDetail, "source" | "entities" | "edges" | "functions">
): RelationRow[] {
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
    const own = detail.source ? detail.source.source_name : "";
    const end = (id: number | string, carried: DataNode | null | undefined): string => {
        if (names[String(id)] !== undefined) {
            return names[String(id)];
        }
        if (carried) {
            const name = displayName(carried);
            return carried.source_name && carried.source_name !== own ? `${name} (${carried.source_name})` : name;
        }
        return String(id);
    };
    return (detail.edges ?? []).map((edge: DataEdge) => ({
        kind: edge.kind,
        label: edge.label ?? "",
        from: end(edge.from, edge.from_node),
        to: end(edge.to, edge.to_node)
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
     *  shows both in the line's popover, it draws no text on a line. */
    lines: { from: string; to: string; title: string; description: string }[];
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
            from: String(edge.from), to: String(edge.to), title: edge.kind, description: edge.label ?? ""
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
    NODE_KINDS, DEFAULT_GRAPH_KINDS, SOURCE_KINDS, GRAPH_NODE_CAP, COMPLETENESS_WARNING,
    completenessPercent, completenessText, completenessState, statusKey, displayName, entityRows,
    relationRows, hintsOf, cleanList, hintsUpdate, hintErrors, hasChanges, scoreText, candidateRow, candidateRows,
    groupKey, graphModel, graphQuery, fitZoom
};
