import type { McpServer, ODataDefinition, ODataServiceSummary } from "../service/types";
import odataCatalog from "./odataCatalog";

/**
 * The agent's one `builtin:odata` server entry: which catalogue services the
 * agent may use and whether it may write through them.
 *
 * Pure rules, no controls, so they can be unit-tested. Mirrors
 * `_validate_odata_entry` in `agents/admin.py` and `_clean_odata_entry` in
 * `agents/db.py`: the entry holds exactly `{services, allow_write}`, names no
 * destination and no identity (both belong to each catalogue service), and
 * `allow_write` opens writes only as the JSON boolean `true`.
 */

export const ODATA_URL = "builtin:odata";

export interface ODataEntry {
    services: string[];
    allow_write: boolean;
}

/** How a selected service stands in the catalogue. `unknown`: the catalogue
 *  could not be read, so nothing can be said about it. */
export type ODataEntryState = "ok" | "disabled" | "missing" | "unknown";

/** One selected service, as the dialog lists it. */
export interface ODataEntryRow {
    name: string;
    title: string;
    purpose: string;
    userContext: boolean;
    state: ODataEntryState;
}

/** One choice of the service box. */
export interface ODataEntryOption {
    key: string;
    title: string;
    userContext: boolean;
    state: ODataEntryState;
}

/** What `allow_write` opens in one service: `items` as the service page
 *  words them, or `null` when the service could not be read. */
export interface ODataEntryOpens {
    name: string;
    title: string;
    items: string[] | null;
}

/** Whether a save newly gives writes, and through which services. */
export interface ODataEntryGiven {
    /** `allow_write` goes from off (or no entry) to on. */
    switchedOn: boolean;
    /** The services to name: all of the entry when switched on, otherwise
     *  the ones added to an entry that already allowed writes. */
    services: string[];
}

/**
 * Whether a server url names the OData built-in, the way the server's save
 * gate reads it (`_validate_url` and `_server_key` in agents/admin.py):
 * trimmed, without trailing slashes, in any case. The server stores it as
 * `builtin:odata`; the dialog holds it in that spelling too (`ODATA_URL`).
 */
function isODataUrl(url: string | undefined | null): boolean {
    return String(url ?? "").trim().replace(/\/+$/, "").toLowerCase() === ODATA_URL;
}

/** Distinct service names in the order given, as typed: the server
 *  (`_clean_odata_entry`) refuses a name with edge whitespace instead of
 *  repairing it, so the refusal reaches the admin. Anything that is no text,
 *  and the empty text, is left out. */
function distinct(services: unknown): string[] {
    const out: string[] = [];
    (Array.isArray(services) ? services : []).forEach((name) => {
        if (typeof name === "string" && name !== "" && out.indexOf(name) === -1) {
            out.push(name);
        }
    });
    return out;
}

/**
 * The block the API gets: the services, and `allow_write` as a real
 * boolean, `true` only for exactly `true`. The string "true" or the number
 * 1 does not open writes; every other key is dropped.
 */
function clean(raw: Record<string, unknown> | undefined | null): ODataEntry {
    return { services: distinct(raw?.services), allow_write: raw?.allow_write === true };
}

/** The position of the agent's `builtin:odata` entry, skipping `except`
 *  (the row being edited); -1 without one. */
function entryIndex(servers: readonly McpServer[] | undefined | null, except = -1): number {
    const list = servers ?? [];
    for (let i = 0; i < list.length; i++) {
        if (i !== except && isODataUrl(list[i]?.url)) {
            return i;
        }
    }
    return -1;
}

/** The agent's entry, cleaned; undefined without one. */
function entryOf(servers: readonly McpServer[] | undefined | null): ODataEntry | undefined {
    const index = entryIndex(servers);
    return index === -1 ? undefined : clean((servers as McpServer[])[index].oauth as Record<string, unknown>);
}

/**
 * The servers as a save sends them: every `builtin:odata` entry carries
 * exactly `{services, allow_write: <boolean>}`, whatever form it was held in
 * (the server answers with `has_client_secret` and leaves `allow_write` out
 * when it is off). Every other server is passed on as it is; nothing given
 * is changed.
 */
function explicit(servers: readonly McpServer[] | undefined | null): McpServer[] {
    return (servers ?? []).map((server) => (
        isODataUrl(server?.url)
            ? { ...server, oauth: clean(server.oauth as Record<string, unknown>) as McpServer["oauth"] }
            : server
    ));
}

function stateOf(summary: ODataServiceSummary | undefined, catalogue: unknown): ODataEntryState {
    if (!Array.isArray(catalogue)) {
        return "unknown";
    }
    return !summary ? "missing" : summary.enabled === false ? "disabled" : "ok";
}

function byName(catalogue: readonly ODataServiceSummary[] | undefined | null): Record<string, ODataServiceSummary> {
    const found: Record<string, ODataServiceSummary> = {};
    (catalogue ?? []).forEach((summary) => {
        found[`=${summary.name}`] = summary;
    });
    return found;
}

/**
 * The selected services in the order selected. A name the catalogue does
 * not hold stays in the list as `missing` (it was deleted or renamed since
 * the agent was saved); with `catalogue` null (it could not be read) every
 * row is `unknown` and none is called missing.
 */
function rows(
    selected: readonly string[], catalogue: readonly ODataServiceSummary[] | undefined | null
): ODataEntryRow[] {
    const found = byName(catalogue);
    return selected.map((name) => {
        const summary = found[`=${name}`];
        return {
            name,
            title: (summary?.title ?? "").trim() || name,
            purpose: summary?.purpose ?? "",
            userContext: summary?.user_context === true,
            state: stateOf(summary, catalogue)
        };
    });
}

/**
 * What the service box offers: every catalogue service by name, then the
 * selected names the catalogue lacks. A selected key always has an item,
 * so the box can never drop it on its own.
 */
function options(
    selected: readonly string[], catalogue: readonly ODataServiceSummary[] | undefined | null
): ODataEntryOption[] {
    const found = byName(catalogue);
    const listed: ODataEntryOption[] = (catalogue ?? []).map((summary) => ({
        key: summary.name,
        title: (summary.title ?? "").trim() || summary.name,
        userContext: summary.user_context === true,
        state: stateOf(summary, catalogue)
    }));
    rows(selected.filter((name) => !found[`=${name}`]), catalogue).forEach((row) => {
        listed.push({ key: row.name, title: row.name, userContext: false, state: row.state });
    });
    return listed;
}

/**
 * What `allow_write` opens, by service, in the words of the service page
 * (`odataCatalog.writeSummary`: entity create/update/delete and the enabled
 * operations that are writes by `operationIsWrite`). `definitions` holds
 * the definition per name; a name without one (not read, or the read
 * failed) gets `items: null`, never an empty list: "nothing" is only said
 * about a service that was read.
 */
function opens(
    selected: readonly ODataEntryRow[], definitions: Record<string, ODataDefinition | null | undefined>
): ODataEntryOpens[] {
    return selected.filter((row) => row.state !== "missing").map((row) => {
        const definition = definitions[`=${row.name}`];
        return { name: row.name, title: row.title, items: definition ? odataCatalog.writeSummary(definition) : null };
    });
}

/**
 * Whether saving `current` over `stored` (the agent as the server has it)
 * newly gives writes: `allow_write` off -> on names every service of the
 * entry; a service added to an entry that already allowed writes names the
 * added ones. Unticking and removing give nothing, and so does an entry
 * that allows as much as the stored one.
 */
function newlyGiven(stored: ODataEntry | undefined, current: ODataEntry | undefined): ODataEntryGiven {
    if (!current || current.allow_write !== true) {
        return { switchedOn: false, services: [] };
    }
    if (!stored || stored.allow_write !== true) {
        return { switchedOn: true, services: current.services.slice() };
    }
    return {
        switchedOn: false,
        services: current.services.filter((name) => stored.services.indexOf(name) === -1)
    };
}

export default {
    ODATA_URL,
    isODataUrl,
    clean,
    entryIndex,
    entryOf,
    explicit,
    rows,
    options,
    opens,
    newlyGiven
};
