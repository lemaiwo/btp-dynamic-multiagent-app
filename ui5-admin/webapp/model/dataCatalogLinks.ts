import { displayName, graphQuery, nodeIcon } from "./dataCatalog";
import type {
    DataEdge, DataEdgeEnd, DataGraph, DataLinkKind, DataNode, DataNodeKind, DataSourceDetail
} from "../service/dataCatalogTypes";

/**
 * The links of the data catalogue, without a view: the Links table of the
 * detail page (`relationRows`), and what the Add link dialog offers and
 * sends (`linkStarts`, `linkQuery`, `linkCandidates`). Every function here
 * is pure. Apart from `dataCatalog` so neither file grows past its size;
 * this module imports from that one, never the other way round.
 */

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

export default {
    LINK_KINDS, LINK_NODE_KINDS, relationKindKey, relationRows, sameSource, linkStarts, linkQuery, linkCandidates
};
