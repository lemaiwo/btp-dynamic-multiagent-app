/**
 * The shapes of the data catalogue routes (`/admin/api/data-catalog/...`).
 *
 * Kept apart from `types.ts`, which is past the size a file may have.
 */

/** Where a catalogue source comes from. */
export type DataSourceKind = "odata" | "ina" | "generic";

/** What a node of the catalogue graph stands for. */
export type DataNodeKind = "source" | "entity" | "field" | "query" | "dimension" | "measure" | "function";

/** One row of `GET data-catalog/sources`. */
export interface DataSourceSummary {
    kind: DataSourceKind;
    name: string;
    title: string;
    destination: string;
    user_context: boolean;
    enabled: boolean;
    /** The names of the agents that attach the source. */
    used_by: string[];
    /** How many nodes the import made of it. */
    nodes: number;
    /** When it was last imported; `null` before the first import. */
    last_import: string | null;
    /** The share of its nodes with curated hints, 0..1. */
    hint_completeness: number;
}

/** A node of the graph, with its hints. */
export interface DataNode {
    id: number | string;
    kind: DataNodeKind;
    source_kind: DataSourceKind;
    source_name: string;
    name: string;
    label: string;
    purpose: string;
    not_for: string;
    synonyms: string[];
    /** Example questions. */
    examples: string[];
    unit: string;
    readable: boolean;
    writable: boolean;
    /** A key field of its entity. */
    is_key?: boolean;
    /** The SAP type (`Edm.String`, ...); "" when there is none. */
    type_name?: string;
    parent_id: number | string | null;
    updated_at: string | null;
    curated_at: string | null;
}

/** An edge of `GET data-catalog/graph`: the parent-to-child `contains`
 *  lines and the links between nodes. It carries no id. */
export interface DataGraphEdge {
    from: number | string;
    to: number | string;
    kind: string;
    label: string;
    /** An admin drew the link (it survives a re-import); false for an
     *  imported edge and for `contains`. Read as `=== true`. */
    curated?: boolean;
}

/** One end of an edge in the detail answer: the node in brief. */
export interface DataEdgeEnd {
    id: number | string;
    kind: DataNodeKind;
    source_kind: DataSourceKind;
    source_name: string;
    name: string;
    /** The label, else the name. */
    label: string;
}

/** An edge of the detail answer and of `POST data-catalog/edges`. */
export interface DataEdge extends DataGraphEdge {
    /** What `DELETE data-catalog/edges/{id}` takes. */
    id: number;
    curated: boolean;
    /** The two ends; null for an end that is no longer in the catalogue. */
    from_node?: DataEdgeEnd | null;
    to_node?: DataEdgeEnd | null;
}

/** The two kinds of link an admin may draw (`CURATED_EDGE_KINDS`). */
export type DataLinkKind = "same_object" | "aggregates";

/** The body of `POST data-catalog/edges`: two entities, InA queries or
 *  functions of different sources that are not linked yet. */
export interface DataEdgeInput {
    from_id: number;
    to_id: number;
    kind: DataLinkKind;
}

/** An InA source's one query with its members. */
export interface DataQueryDetail {
    node: DataNode;
    dimensions: DataNode[];
    measures: DataNode[];
}

/** `GET data-catalog/sources/{kind}/{name}`. */
export interface DataSourceDetail {
    /** `null`: the source is registered but was never imported. */
    source: DataNode | null;
    /** Kinds `odata` and `generic` only. */
    entities?: { node: DataNode; fields: DataNode[] }[];
    /** Function imports / operations of the source (`odata`, `generic`). */
    functions?: DataNode[];
    /** Kind `ina` only; `null` before the first import. */
    query?: DataQueryDetail | null;
    edges: DataEdge[];
    used_by: string[];
}

/** The body of `PUT data-catalog/nodes/{id}/hints`: only what changes. */
export interface DataHintsUpdate {
    label?: string;
    purpose?: string;
    not_for?: string;
    synonyms?: string[];
    examples?: string[];
    readable?: boolean;
}

/** One answer of `POST data-catalog/search`. */
export interface DataSearchCandidate {
    source_kind: DataSourceKind;
    source_name: string;
    kind: DataNodeKind;
    name: string;
    label: string;
    purpose: string;
    not_for: string;
    keys: string[];
    relations: { kind: string; to: { source_name: string; name: string; label: string } }[];
    score: number;
}

export interface DataSearchResult {
    candidates: DataSearchCandidate[];
    /** `agent`: limited to what the chosen agent attaches. */
    scope: "agent" | "all";
}

export interface DataGraphNode {
    id: number | string;
    kind: DataNodeKind;
    source_kind: DataSourceKind;
    source_name: string;
    name: string;
    label: string;
    group: string;
}

/** `GET data-catalog/graph`. */
export interface DataGraph {
    nodes: DataGraphNode[];
    /** Parent-to-child links are edges of kind `contains`. */
    edges: DataGraphEdge[];
    /** More nodes matched than the 2,000 the route answers. */
    truncated?: boolean;
}

/** `POST data-catalog/rebuild`. */
export interface DataRebuildResult {
    imported: number;
    removed: number;
    vectors: number;
}
