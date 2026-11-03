/**
 * The shapes of the InA admin routes (`/admin/api/ina/...`): the analytical
 * queries agents read through `builtin:ina` (design addendum (k), part 2).
 *
 * Kept apart from `types.ts`, which is past the size a file may have.
 */
import type { ReloadOutcome } from "./types";

/**
 * The code of a refused describe or test, from the `X-Ina-Error` header
 * (`AdminError.code`): `ERROR_CODES` in agents/ina/errors.py, a closed list.
 */
export type InaErrorCode =
    | "destination_error" | "proxy_refused" | "user_token_required" | "unauthorized" | "forbidden"
    | "csrf_failed" | "query_unknown" | "variable_required" | "invalid_filter" | "result_too_large"
    | "sap_error" | "unreachable" | "timeout" | "not_json";

export interface InaDimension {
    /** SAP's technical name (`0CALMONTH`); case sensitive. */
    name: string;
    label: string;
    key_field?: string | null;
}

export interface InaMeasure {
    name: string;
    label: string;
    unit?: string | null;
}

export type InaVariableType = "single" | "interval" | "multiple";

export interface InaVariable {
    name: string;
    label: string;
    mandatory: boolean;
    type: InaVariableType;
}

/** What `Describe` read from SAP (`InaDefinition` in agents/ina/models.py). */
export interface InaDefinition {
    dimensions: InaDimension[];
    measures: InaMeasure[];
    variables: InaVariable[];
}

/** One agent that names the query in its `builtin:ina` entry (`ina_query_referrers`). */
export interface InaUsedBy {
    agent_id: number;
    agent: string;
    enabled: boolean;
    expose_api: boolean;
    api_slug: string | null;
}

/**
 * What `POST ina/queries` accepts (`InaQueryPayload`). `definition` and
 * `metadata_fetched_at` are a pair the server keeps when both are left out,
 * which is how a form saves without touching what `Describe` stored.
 */
export interface InaQueryInput {
    /** The slug agents and server entries use. Immutable once created. */
    name: string;
    title: string;
    purpose: string;
    not_for: string;
    destination: string;
    /** On, calls run as the signed-in user; off, as the destination's technical user. */
    user_context: boolean;
    /** The InA `DataSource.ObjectName` (typed: SAP does not list its queries). */
    query: string;
    /** The InA `PackageName`; may be blank. */
    package: string;
    enabled: boolean;
    definition?: InaDefinition | null;
    metadata_fetched_at?: string | null;
}

/**
 * What `PUT ina/queries/{name}` accepts: the payload plus the `updated_at`
 * the query had when the page loaded it. A query saved since then is not
 * overwritten (409). Left out or null: not checked.
 */
export interface InaQueryUpdate extends InaQueryInput {
    expected_updated_at?: string | null;
}

/** One row of `GET ina/queries`: everything but the definition. */
export interface InaQuerySummary {
    id?: number;
    name: string;
    title: string;
    purpose: string;
    not_for: string;
    destination: string;
    user_context: boolean;
    query: string;
    package: string;
    enabled: boolean;
    /** `Describe` has stored a definition. */
    described: boolean;
    /** When `Describe` last read SAP; null before the first. */
    metadata_fetched_at: string | null;
    created_at?: string | null;
    /** What a client sends back as `expected_updated_at`. */
    updated_at: string | null;
    /** The agents that name the query, enabled or not: objects as the
     *  record's `to_dict` gives them; a plain agent name is read as well
     *  (`inaQueries.usedByNames`). */
    used_by: (InaUsedBy | string)[];
}

/** `GET ina/queries/{name}`. The answer of a `PUT` also carries the
 *  `ReloadOutcome` keys; a `GET` and a create do not. */
export interface InaQuery extends InaQuerySummary, ReloadOutcome {
    /** null: not described yet. */
    definition: InaDefinition | null;
}

/** `POST ina/queries/{name}/describe`: what SAP described, now stored. */
export interface InaDescribeResult {
    dimensions: number;
    measures: number;
    variables: number;
    /** The technical names that were read. */
    names: string[];
    /** SAP described more members than a definition may hold. */
    truncated: boolean;
    metadata_fetched_at: string | null;
}

/** `POST ina/queries/{name}/test`: one read of the first measure, `top=1`.
 *  Outcome and names only, never a value. A refused test is an error with
 *  its code in `X-Ina-Error`. */
export interface InaTestResult {
    ok: boolean;
    /** The HTTP status SAP answered, or null without one. */
    status: number | null;
    duration_ms: number;
    columns: string[];
}
