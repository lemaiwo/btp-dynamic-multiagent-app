import type { AuthMode, McpServer } from "../service/types";
import type { InaQuerySummary } from "../service/inaTypes";

/**
 * The agent's one `builtin:ina` server entry: which registered analytical
 * queries the agent may run (design addendum (k), part 2 point 3).
 *
 * Pure rules, no controls, so they can be unit-tested. Mirrors
 * `_validate_ina_entry` in `agents/admin.py` and `_clean_ina_entry` in
 * `agents/db.py`, as `odataEntry.ts` mirrors the OData ones: the entry holds
 * exactly `{queries}` and names no destination and no identity (both belong
 * to each registered query, so no agent can run a query as someone else by
 * editing its own entry). Nothing here writes: an InA query only reads.
 */

export const INA_URL = "builtin:ina";

/** `MAX_INA_ENTRY_QUERIES` in agents/db.py. */
export const MAX_INA_ENTRY_QUERIES = 50;

/** `QUERY_NAME_RE` in agents/ina/models.py: the slug of a registered query. */
const QUERY_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$/;

/** The one key the server keeps besides its own echo. */
const ENTRY_KEY = "queries";

/** The fixed texts of `validate` and of a second entry. Never a value. */
export const INA_TEXTS = {
    mode: "Analytical queries requires auth mode 'destination': every registered query is reached "
        + "through the BTP destination it names.",
    oneKey: "An analytical queries entry holds only the queries: the destination and who the calls run as "
        + "belong to each registered query.",
    none: "Select at least one analytical query.",
    tooMany: `At most ${MAX_INA_ENTRY_QUERIES} analytical queries per agent.`,
    invalid: "An analytical query name is not valid (lower-case letters, digits and '-', at most 64 characters).",
    twice: "An analytical query is listed twice.",
    secondEntry: "An agent has one analytical queries entry; add the queries to the existing one."
};

export interface InaEntry {
    queries: string[];
}

/** How a selected query stands among the registered ones. `unknown`: the
 *  list could not be read, so nothing can be said about it. */
export type InaEntryState = "ok" | "disabled" | "missing" | "unknown";

/** One selected query, as the dialog says it. */
export interface InaEntryRow {
    name: string;
    title: string;
    purpose: string;
    userContext: boolean;
    state: InaEntryState;
}

/** One choice of the query box. */
export interface InaEntryOption {
    key: string;
    title: string;
    userContext: boolean;
    state: InaEntryState;
}

/**
 * Whether a server url names the InA built-in, the way the server's save
 * gate reads it (`_server_key` in agents/admin.py): trimmed, without
 * trailing slashes, in any case. The dialog holds it as `INA_URL`.
 */
function isInaUrl(url: string | undefined | null): boolean {
    return String(url ?? "").trim().replace(/\/+$/, "").toLowerCase() === INA_URL;
}

/** Distinct query names in the order given, as typed: the server refuses a
 *  name with edge whitespace instead of repairing it, so the refusal reaches
 *  the admin. Anything that is no text, and the empty text, is left out. */
function distinct(queries: unknown): string[] {
    const out: string[] = [];
    (Array.isArray(queries) ? queries : []).forEach((name) => {
        if (typeof name === "string" && name !== "" && out.indexOf(name) === -1) {
            out.push(name);
        }
    });
    return out;
}

/** The block the API gets: exactly `{queries}`; every other key is dropped,
 *  `destination`, `user_context` and the server's `has_client_secret` echo
 *  included. */
function clean(raw: Record<string, unknown> | undefined | null): InaEntry {
    return { queries: distinct(raw?.queries) };
}

/** The position of the agent's `builtin:ina` entry, skipping `except` (the
 *  row being edited); -1 without one. */
function entryIndex(servers: readonly McpServer[] | undefined | null, except = -1): number {
    const list = servers ?? [];
    for (let i = 0; i < list.length; i++) {
        if (i !== except && isInaUrl(list[i]?.url)) {
            return i;
        }
    }
    return -1;
}

/** The agent's entry, cleaned; undefined without one. */
function entryOf(servers: readonly McpServer[] | undefined | null): InaEntry | undefined {
    const index = entryIndex(servers);
    return index === -1 ? undefined : clean((servers as McpServer[])[index].oauth as Record<string, unknown>);
}

/** The servers as a save sends them: every `builtin:ina` entry carries
 *  exactly `{queries}`, whatever form it was held in (the server answers
 *  with `has_client_secret: false`). Every other server is passed on as it
 *  is; nothing given is changed. */
function explicit(servers: readonly McpServer[] | undefined | null): McpServer[] {
    return (servers ?? []).map((server) => (
        isInaUrl(server?.url)
            ? { ...server, oauth: clean(server.oauth as Record<string, unknown>) as McpServer["oauth"] }
            : server
    ));
}

function byName(queries: readonly InaQuerySummary[] | undefined | null): Record<string, InaQuerySummary> {
    const found: Record<string, InaQuerySummary> = {};
    (queries ?? []).forEach((query) => {
        found[`=${query.name}`] = query;
    });
    return found;
}

function stateOf(query: InaQuerySummary | undefined, queries: unknown): InaEntryState {
    if (!Array.isArray(queries)) {
        return "unknown";
    }
    return !query ? "missing" : query.enabled === false ? "disabled" : "ok";
}

/**
 * The selected queries in the order selected. A name the registered list
 * does not hold stays as `missing` (deleted since the agent was saved);
 * with `queries` null (the list could not be read) every row is `unknown`
 * and none is called missing.
 */
function rowsOf(queries: readonly InaQuerySummary[] | undefined | null, entry: InaEntry | undefined): InaEntryRow[] {
    const found = byName(queries);
    return (entry?.queries ?? []).map((name) => {
        const query = found[`=${name}`];
        return {
            name,
            title: (query?.title ?? "").trim() || name,
            purpose: query?.purpose ?? "",
            userContext: query?.user_context === true,
            state: stateOf(query, queries)
        };
    });
}

/** What the query box offers: every registered query by name, then the
 *  selected names the list lacks. A selected key always has an item, so the
 *  box can never drop it on its own. */
function options(queries: readonly InaQuerySummary[] | undefined | null, entry: InaEntry | undefined): InaEntryOption[] {
    const found = byName(queries);
    const listed: InaEntryOption[] = (queries ?? []).map((query) => ({
        key: query.name,
        title: (query.title ?? "").trim() || query.name,
        userContext: query.user_context === true,
        state: stateOf(query, queries)
    }));
    rowsOf(queries, { queries: (entry?.queries ?? []).filter((name) => !found[`=${name}`]) }).forEach((row) => {
        listed.push({ key: row.name, title: row.name, userContext: false, state: row.state });
    });
    return listed;
}

/** The selected names the registered list lacks; none while it is unread. */
function missing(entry: InaEntry | undefined, queries: readonly InaQuerySummary[] | undefined | null): string[] {
    return rowsOf(queries, entry).filter((row) => row.state === "missing").map((row) => row.name);
}

/**
 * The rules of a `builtin:ina` block, as `_validate_ina_entry` has them:
 * destination mode, only `queries` (the server's `has_client_secret: false`
 * echo is taken back), 1 to 50 distinct query slugs. The texts are fixed and
 * never quote a value.
 */
function validate(oauth: McpServer["oauth"] | undefined, authMode: AuthMode | string): string {
    if (authMode !== "destination") {
        return INA_TEXTS.mode;
    }
    const cfg = (oauth || {}) as Record<string, unknown>;
    const stray = Object.keys(cfg).filter((key) => key !== ENTRY_KEY
        && !(key === "has_client_secret" && cfg[key] === false));
    if (stray.length) {
        return INA_TEXTS.oneKey;
    }
    const queries = cfg.queries;
    if (!Array.isArray(queries) || queries.length === 0) {
        return INA_TEXTS.none;
    }
    if (queries.length > MAX_INA_ENTRY_QUERIES) {
        return INA_TEXTS.tooMany;
    }
    if (queries.some((name) => typeof name !== "string" || !QUERY_NAME_RE.test(name))) {
        return INA_TEXTS.invalid;
    }
    if (new Set(queries).size !== queries.length) {
        return INA_TEXTS.twice;
    }
    return "";
}

export default {
    INA_URL,
    isInaUrl,
    clean,
    entryIndex,
    entryOf,
    explicit,
    rowsOf,
    options,
    missing,
    validate
};
