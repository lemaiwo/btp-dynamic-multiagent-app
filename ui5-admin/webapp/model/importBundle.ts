import odataCatalog from "./odataCatalog";
import odataEntry from "./odataEntry";
import type { ImportResult, ODataDefinition } from "../service/types";

/**
 * What a configuration bundle (the text pasted into Settings > Import) opens
 * in SAP, read from the bundle itself before anything is sent.
 *
 * Pure rules, no controls. The write rule is the catalogue's
 * (`odataCatalog.writeSummary`, which goes through `operationIsWrite`) and
 * the entry rule is the agent page's (`odataEntry.isODataUrl`, `allow_write`
 * only as the JSON boolean `true`): nothing is decided here a second time.
 * Every text of a bundle is untrusted: it is cut to one line
 * (`odataCatalog.importLabel`) and shown as text by the caller.
 */

/** One catalogue service of a bundle. */
export interface BundleService {
    name: string;
    /** The title, or the name when the bundle gives none. */
    title: string;
    userContext: boolean;
    destination: string;
    /** `odataCatalog.writeSummary` of its definition. */
    writes: string[];
}

/** An agent of a bundle whose OData entry has "Allow writes". */
export interface BundleWriter {
    agent: string;
    services: string[];
}

export interface BundleOpens {
    /** The bundle has an `odata_services` section: only then does "replace"
     *  delete catalogue services that are not in it. */
    hasCatalogue: boolean;
    services: BundleService[];
    writers: BundleWriter[];
}

class Unreadable extends Error {}

function isObject(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** `value` as a list of objects; undefined or null is an empty list. */
function objects(value: unknown): Record<string, unknown>[] {
    if (value === undefined || value === null) {
        return [];
    }
    if (!Array.isArray(value) || !value.every(isObject)) {
        throw new Unreadable();
    }
    return value as Record<string, unknown>[];
}

function label(value: unknown): string {
    return odataCatalog.importLabel(typeof value === "string" ? value : "");
}

/** The definition, checked as far as the write rule reads it. */
function definitionOf(raw: unknown): ODataDefinition {
    if (!isObject(raw)) {
        throw new Unreadable();
    }
    objects(raw.entity_sets).forEach((entitySet) => {
        const operations = entitySet.operations;
        if (operations !== undefined && operations !== null && !Array.isArray(operations)) {
            throw new Unreadable();
        }
    });
    objects(raw.operations);
    return raw as unknown as ODataDefinition;
}

function serviceOf(raw: Record<string, unknown>): BundleService {
    const userContext = raw.user_context;
    if (typeof raw.name !== "string" || typeof raw.destination !== "string"
        || (userContext !== undefined && typeof userContext !== "boolean")) {
        throw new Unreadable();
    }
    return {
        name: label(raw.name),
        title: label(raw.title) || label(raw.name),
        userContext: userContext === true,
        destination: label(raw.destination),
        writes: odataCatalog.writeSummary(definitionOf(raw.definition)).map(label)
    };
}

function writersOf(agent: Record<string, unknown>): BundleWriter[] {
    return objects(agent.mcp_servers)
        .filter((server) => odataEntry.isODataUrl(server.url as string) && isObject(server.oauth)
            && server.oauth.allow_write === true)
        .map((server) => ({
            agent: label(agent.name),
            services: odataEntry.clean(server.oauth as Record<string, unknown>).services.map(label)
        }));
}

/**
 * What `bundle` (the parsed text) opens, or `null` when that cannot be read
 * from it: it is no object, or its OData services or agents do not have the
 * form the rules need. `null` never means "nothing": the caller asks then.
 */
function opens(bundle: unknown): BundleOpens | null {
    try {
        if (!isObject(bundle)) {
            return null;
        }
        const catalogue = bundle.odata_services;
        const writers: BundleWriter[] = [];
        objects(bundle.agents).forEach((agent) => {
            writers.push(...writersOf(agent));
        });
        return {
            hasCatalogue: catalogue !== undefined && catalogue !== null,
            services: objects(catalogue).map(serviceOf),
            writers
        };
    } catch {
        return null;
    }
}

/** Whether importing has to be asked about first: it brings catalogue
 *  services, gives an agent writes, or (with `replace`) deletes the
 *  catalogue services the bundle does not name. */
function mustAsk(found: BundleOpens, replace: boolean): boolean {
    return found.services.length > 0 || found.writers.length > 0 || (found.hasCatalogue && replace);
}

/** The server's own lines for what `removed_odata_service_names` and
 *  `odata_identity_changes` already say (`api_import` in agents/admin.py). */
const REPORTED_RE = /^OData service(\(s\) removed by replace: | '[^']*': .* changed; used by agent\(s\) )/;

/**
 * The lines of `warnings` to show as the server wrote them: all of them,
 * except -- when the answer also carries the two lists -- the ones that only
 * repeat a list the page words itself.
 */
function otherWarnings(result: ImportResult): string[] {
    const listed = Array.isArray(result.removed_odata_service_names) && Array.isArray(result.odata_identity_changes);
    return (Array.isArray(result.warnings) ? result.warnings : [])
        .filter((line) => typeof line === "string" && line.trim() !== "")
        .filter((line) => !(listed && REPORTED_RE.test(line)));
}

export default { opens, mustAsk, otherWarnings };
