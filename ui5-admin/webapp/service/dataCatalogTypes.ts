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
    /** How the candidates were ranked: by their vectors, or by words (no
     *  embedding model in effect). Absent from a server before 2.36.0. */
    mode?: "vector" | "lexical";
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

// --- embeddings (design addendum (l)) ---
// `/admin/api/data-catalog/embeddings`: the setting, the vectors, the
// background run and the AI Core deployment of an embedding model.

/** Where the effective embedding model comes from: the setting saved here,
 *  `AICORE_EMBEDDING_MODEL`, discovery among the deployments, or none. */
export type EmbeddingSource = "setting" | "env" | "discovery" | "none";

/** The stored setting, and the body of `PUT data-catalog/embeddings`.
 *  `model` null: automatic (the landscape's variable, else discovery). */
export interface EmbeddingSetting {
    enabled: boolean;
    model: string | null;
}

/** A deployment of an embedding model in the app's resource group. */
export interface EmbeddingDeployment {
    model: string;
    /** AI Core's status: `RUNNING`, `PENDING`, `STOPPED`, `DEAD`, ... */
    status: string;
    deployment_id: string;
}

/** An embedding model the scenario offers that has no deployment yet. */
export interface EmbeddingModelOffer {
    model: string;
    versions: string[];
}

/** The last background run, kept in memory by the app instance. */
export interface EmbeddingRun {
    started_at: string;
    /** null: the run did not finish (the instance stopped meanwhile). */
    finished_at: string | null;
    model: string | null;
    /** How many nodes it embedded. */
    embedded: number;
    /** The exception class of a failed run; null when it worked. */
    error_class: string | null;
}

/** `GET data-catalog/embeddings`, also the answer of the `PUT`. */
export interface EmbeddingsStatus {
    setting: EmbeddingSetting;
    /** The model vectors are made and searched with; null: none, agents rank by words. */
    effective_model: string | null;
    source: EmbeddingSource;
    /** `AICORE_EMBEDDING_MODEL` is set on this landscape. */
    env_override: boolean;
    /** Embedding models only. */
    deployed: EmbeddingDeployment[];
    available: EmbeddingModelOffer[];
    /** The nodes of the embeddable kinds. */
    nodes: number;
    /** The nodes with a vector of the effective model. */
    vectors: number;
    /** The nodes without a vector or with one of an older text. */
    stale: number;
    running: boolean;
    progress: { done: number; total: number } | null;
    last_run: EmbeddingRun | null;
    /** The AI Core resource group a deployment is made in (the deploy
     *  confirmation names it). */
    resource_group: string;
    /** The code when AI Core could not be read (`aicore_unavailable`,
     *  `token_failed`, ...): the rest is answered all the same, with
     *  `deployed` and `available` empty, so the deployment state is unknown. */
    aicore_error?: string | null;
}

/** `POST data-catalog/embeddings/run`: 202, or 409 while a run is in flight. */
export interface EmbeddingsRunStarted {
    running: boolean;
}

/** `POST data-catalog/embeddings/deploy`: 202 with the new deployment, or the
 *  RUNNING / PENDING one that already exists (nothing is created then). */
export interface EmbeddingDeployResult {
    deployment_id: string;
    status: string;
    /** false: a live deployment of the model was answered, nothing was made. */
    created: boolean;
}
