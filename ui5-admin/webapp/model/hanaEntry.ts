import type { AuthMode, McpServer } from "../service/types";

/**
 * The agent's one `builtin:hana` server entry: which on-premise SAP HANA
 * databases the agent may read monitoring and catalogue views of, and
 * whether it may run its own SELECT on them.
 *
 * Pure rules, no controls, so they can be unit-tested. Mirrors
 * `agents/hana/config.py` rule for rule, which is authoritative: this module
 * adds no rule of its own. The entry holds exactly `{systems, allow_select}`
 * and names no top-level destination (each system has its own) and no
 * identity (each destination's technical database user). The server refuses
 * every key that is not the entry's, so `clean` builds the block from
 * nothing, and a refusal here is a fixed text that never quotes a value.
 */

export const HANA_URL = "builtin:hana";

/** `MAX_SYSTEMS` in agents/hana/config.py. */
export const MAX_HANA_SYSTEMS = 10;

/** `SYSTEM_NAME_RE` in agents/hana/config.py (`fullmatch`). */
const SYSTEM_NAME_RE = /^[a-z][a-z0-9_]{0,31}$/;
/** `DESTINATION_PATTERN` in agents/hana/config.py, the OData gate's pattern. */
const DESTINATION_RE = /^[A-Za-z0-9_.-]{1,200}$/;
const ENTRY_KEYS = ["systems", "allow_select"];
const SYSTEM_KEYS = ["name", "destination"];
/** `_ECHOED`: tolerated only while they say nothing (`false`). */
const ECHOED = ["has_client_secret", "user_context"];

/** The fixed texts of `validate` and of a second entry. Never a value. */
export const HANA_TEXTS = {
    mode: "SAP HANA (monitoring) requires auth mode 'destination': each system is reached through "
        + "the BTP destination it names.",
    userContext: "SAP HANA (monitoring) cannot act as the signed-in user: it connects as the technical "
        + "user of each system's destination.",
    destination: "A SAP HANA (monitoring) entry has no destination of its own: the destinations are per system.",
    oneKey: "A SAP HANA (monitoring) entry holds only its systems and the SELECT switch.",
    none: `Add 1 to ${MAX_HANA_SYSTEMS} systems.`,
    tooMany: `At most ${MAX_HANA_SYSTEMS} systems per entry.`,
    allowSelect: "Allow SELECT must be on or off.",
    secondEntry: "An agent has one SAP HANA (monitoring) entry; add the systems to the existing one."
};

/** The per-system texts, behind "System <n>: ". */
const SYSTEM_TEXTS = {
    shape: "each system is a name and a destination.",
    oneKey: "a system holds only a name and a destination.",
    name: "the name is required: 1 to 32 lower-case letters, digits or '_', starting with a letter.",
    twice: "the name is listed twice; each system name is listed once.",
    destination: "the destination is required: 1 to 200 letters, digits, '_', '.' or '-', without spaces."
};

export interface HanaSystem {
    name: string;
    destination: string;
}

export interface HanaEntry {
    systems: HanaSystem[];
    allow_select?: true;
}

/** A refusal and, for one system, its position (0-based) and field. */
export interface HanaProblem {
    text: string;
    system?: number;
    field?: "name" | "destination" | "";
}

/** Where a server 422 points: the server row, the system row, the field. */
export interface HanaRefusal {
    server: number;
    system: number;
    field: "name" | "destination" | "";
}

/** The entry in any spelling the save gate accepts (`_server_key` in
 *  agents/admin.py): trimmed, without trailing slashes, in any case. */
function isHanaUrl(url: string | undefined | null): boolean {
    return String(url ?? "").trim().replace(/\/+$/, "").toLowerCase() === HANA_URL;
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function matches(pattern: RegExp, value: unknown): boolean {
    return typeof value === "string" && pattern.test(value);
}

/** A key the block holds with a value (`None` is no value to the server). */
function present(cfg: Record<string, unknown>): string[] {
    return Object.keys(cfg).filter((key) => cfg[key] !== null && cfg[key] !== undefined);
}

/**
 * Exactly what the server's cleaner stores: `systems` as `{name,
 * destination}` per row, as typed (the server refuses edge whitespace
 * instead of repairing it, so `validate` and the server see the same text),
 * and `allow_select` only as the JSON boolean `true`.
 */
function clean(raw: Record<string, unknown> | undefined | null): HanaEntry {
    const systems = Array.isArray(raw?.systems) ? raw?.systems as unknown[] : [];
    const out: HanaEntry = {
        systems: systems.map((item) => {
            const row = isRecord(item) ? item : {};
            return {
                name: typeof row.name === "string" ? row.name : "",
                destination: typeof row.destination === "string" ? row.destination : ""
            };
        })
    };
    if (raw?.allow_select === true) {
        out.allow_select = true;
    }
    return out;
}

/** The rules of `check_entry`, in its order, with where they point. */
function problem(oauth: McpServer["oauth"] | undefined, authMode: AuthMode | string): HanaProblem | null {
    if (authMode !== "destination") {
        return { text: HANA_TEXTS.mode };
    }
    const cfg = (isRecord(oauth) ? oauth : {}) as Record<string, unknown>;
    if (cfg.user_context === true) {
        return { text: HANA_TEXTS.userContext };
    }
    if (cfg.destination !== null && cfg.destination !== undefined) {
        return { text: HANA_TEXTS.destination };
    }
    const stray = present(cfg).filter((key) => ENTRY_KEYS.indexOf(key) === -1
        && !(ECHOED.indexOf(key) > -1 && cfg[key] === false));
    if (stray.length > 0) {
        return { text: HANA_TEXTS.oneKey };
    }
    const systems = cfg.systems;
    if (!Array.isArray(systems) || systems.length === 0) {
        return { text: HANA_TEXTS.none };
    }
    if (systems.length > MAX_HANA_SYSTEMS) {
        return { text: HANA_TEXTS.tooMany };
    }
    const seen: string[] = [];
    for (let i = 0; i < systems.length; i++) {
        const at = (text: string, field: HanaProblem["field"]): HanaProblem => (
            { text: `System ${i + 1}: ${text}`, system: i, field });
        const item = systems[i] as unknown;
        if (!isRecord(item)) {
            return at(SYSTEM_TEXTS.shape, "");
        }
        if (present(item).some((key) => SYSTEM_KEYS.indexOf(key) === -1)) {
            return at(SYSTEM_TEXTS.oneKey, "");
        }
        if (!matches(SYSTEM_NAME_RE, item.name)) {
            return at(SYSTEM_TEXTS.name, "name");
        }
        if (seen.indexOf(item.name as string) > -1) {
            return at(SYSTEM_TEXTS.twice, "name");
        }
        seen.push(item.name as string);
        if (!matches(DESTINATION_RE, item.destination)) {
            return at(SYSTEM_TEXTS.destination, "destination");
        }
    }
    const allow = cfg.allow_select;
    if (allow !== null && allow !== undefined && typeof allow !== "boolean") {
        return { text: HANA_TEXTS.allowSelect };
    }
    return null;
}

/** An error text, or "" when the entry is valid. */
function validate(oauth: McpServer["oauth"] | undefined, authMode: AuthMode | string): string {
    return problem(oauth, authMode)?.text ?? "";
}

/** True when a save would switch SELECT on where it was not: `before` the
 *  block as stored (or as the dialog opened), `after` the one to send. */
function selectNewlyAllowed(before: Record<string, unknown> | undefined | null,
    after: Record<string, unknown> | undefined | null): boolean {
    return before?.allow_select !== true && after?.allow_select === true;
}

/** The position of the agent's `builtin:hana` entry, skipping `except` (the
 *  row being edited); -1 without one. */
function entryIndex(servers: readonly McpServer[] | undefined | null, except = -1): number {
    const list = servers ?? [];
    for (let i = 0; i < list.length; i++) {
        if (i !== except && isHanaUrl(list[i]?.url)) {
            return i;
        }
    }
    return -1;
}

/** The servers as a save sends them: every `builtin:hana` entry carries
 *  exactly its cleaned block (the server answers with `has_client_secret:
 *  false`). Every other server is passed on as it is; nothing given is
 *  changed. */
function explicit(servers: readonly McpServer[] | undefined | null): McpServer[] {
    return (servers ?? []).map((server) => (
        isHanaUrl(server?.url)
            ? { ...server, oauth: clean(server.oauth as Record<string, unknown>) as McpServer["oauth"] }
            : server
    ));
}

const NESTED_LOC = /^mcp_servers\.(\d+)\.oauth\.systems\.(\d+)(?:\.(name|destination))?$/;
const SERVER_LOC = /^mcp_servers\.(\d+)$/;
/** The field prefix of a refusal of agents/hana/config.py (behind
 *  pydantic's "Value error, "). */
const MESSAGE_LOC = /(?:^|\s)oauth\.systems\[(\d+)\](?:\.(name|destination))?:/;

/** Where a 422 of a save points inside a hana entry: the key is the client's
 *  `loc` without "body" (`mcp_servers.2`, or nested), the message the
 *  server's fixed text. Null when it points at no system. */
function refusedAt(key: string, message: string): HanaRefusal | null {
    const nested = NESTED_LOC.exec(key);
    if (nested) {
        return { server: Number(nested[1]), system: Number(nested[2]),
            field: (nested[3] ?? "") as HanaRefusal["field"] };
    }
    const server = SERVER_LOC.exec(key);
    const inMessage = MESSAGE_LOC.exec(message ?? "");
    if (server && inMessage) {
        return { server: Number(server[1]), system: Number(inMessage[1]),
            field: (inMessage[2] ?? "") as HanaRefusal["field"] };
    }
    return null;
}

export default {
    HANA_URL,
    MAX_HANA_SYSTEMS,
    isHanaUrl,
    clean,
    problem,
    validate,
    selectNewlyAllowed,
    entryIndex,
    explicit,
    refusedAt
};
