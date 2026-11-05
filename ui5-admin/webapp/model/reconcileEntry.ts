import type { AuthMode } from "../service/types";

/**
 * The agent's `builtin:reconcile` server entry: the migration reconciliation
 * toolset, which compares a migrated CSV with its source table read through
 * the SAME agent's ARC-1 MCP server entry.
 *
 * Pure rules, no controls, so they can be unit-tested. Mirrors
 * `agents/reconcile/config.py` (`RECONCILE_KEYS`, `validate_reconcile_block`)
 * and the gate in `agents/admin.py`: the entry is `auth_mode: "none"` (it
 * holds no credential; ARC-1's own entry carries the signed-in user) and its
 * block holds at most these four strings, blank meaning the default. The
 * texts below name the field and the rule, never the value: a URL could
 * carry a token.
 */

export const RECONCILE_URL = "builtin:reconcile";

/** `RECONCILE_KEYS` in agents/reconcile/config.py, in the dialog's order. */
export const RECONCILE_KEYS = ["arc1_server", "target", "batch_size", "mismatch_cap"] as const;

/** `MAX_BATCH_SIZE` / `MAX_MISMATCH_CAP` in agents/reconcile/config.py. */
export const MAX_BATCH_SIZE = 10000;
export const MAX_MISMATCH_CAP = 200000;

/** `max_length` of `arc1_server` in `OAuthClientPayload` (agents/admin.py). */
const MAX_URL_CHARS = 512;

/** `TARGET_RE` in agents/reconcile/config.py. */
const TARGET_RE = /^[A-Za-z0-9_.-]{1,64}$/;

/** Whole numbers, as long as the payload's `max_length` allows (6 and 7). */
const BATCH_RE = /^[0-9]{1,6}$/;
const CAP_RE = /^[0-9]{1,7}$/;

/** The fixed texts of `validate`. Never a value. */
export const RECONCILE_TEXTS = {
    mode: "Migration reconciliation requires auth mode 'none': it holds no credential and reads "
        + "through this agent's ARC-1 server entry.",
    stray: "A migration reconciliation entry holds only the ARC-1 server, the target, the batch size "
        + "and the mismatch cap.",
    arc1Server: `ARC-1 server: the https URL of this agent's ARC-1 server entry, with a host and without `
        + `user, query or fragment, at most ${MAX_URL_CHARS} characters.`,
    target: "Target: 1 to 64 letters, digits, '_', '.' or '-'.",
    batchSize: `Batch size: a whole number from 1 to ${MAX_BATCH_SIZE}.`,
    mismatchCap: `Mismatch cap: a whole number from 1 to ${MAX_MISMATCH_CAP}.`
};

export type ReconcileBlock = Partial<Record<(typeof RECONCILE_KEYS)[number], string>>;

function text(value: unknown): string {
    return typeof value === "string" ? value.trim() : value == null ? "" : String(value).trim();
}

function arc1ServerOk(value: string): boolean {
    if (value.length > MAX_URL_CHARS || value.indexOf("?") > -1 || value.indexOf("#") > -1) {
        return false;
    }
    let parsed: URL;
    try {
        parsed = new URL(value);
    } catch {
        return false;
    }
    // `@` anywhere before the path is userinfo, also an empty one (`https://@h`).
    const authority = value.slice(value.indexOf("//") + 2).split("/")[0];
    return parsed.protocol === "https:" && !!parsed.hostname && authority.indexOf("@") === -1;
}

function numberOk(value: string, re: RegExp, max: number): boolean {
    return re.test(value) && Number(value) >= 1 && Number(value) <= max;
}

export default {

    RECONCILE_URL,

    RECONCILE_KEYS,

    RECONCILE_TEXTS,

    isReconcileUrl(url: string): boolean {
        return (url || "").trim().replace(/\/+$/, "").toLowerCase() === RECONCILE_URL;
    },

    /**
     * The block to send: exactly the four keys, trimmed, a blank one left
     * out (the server's default); `undefined` when all are blank. Whatever
     * else the dialog model holds (another type's fields) never rides along.
     */
    clean(raw: Record<string, unknown> | undefined): ReconcileBlock | undefined {
        const out: ReconcileBlock = {};
        RECONCILE_KEYS.forEach((key) => {
            const value = text(raw?.[key]);
            if (value) {
                out[key] = value;
            }
        });
        return Object.keys(out).length ? out : undefined;
    },

    /** An error message, or "" when the server would accept the entry. */
    validate(oauth: Record<string, unknown> | undefined, authMode: AuthMode): string {
        if (authMode !== "none") {
            return RECONCILE_TEXTS.mode;
        }
        const block = oauth ?? {};
        const keys = RECONCILE_KEYS as readonly string[];
        if (Object.keys(block).some((k) => keys.indexOf(k) === -1)) {
            return RECONCILE_TEXTS.stray;
        }
        const arc1 = text(block.arc1_server);
        if (arc1 && !arc1ServerOk(arc1)) {
            return RECONCILE_TEXTS.arc1Server;
        }
        const target = text(block.target);
        if (target && !TARGET_RE.test(target)) {
            return RECONCILE_TEXTS.target;
        }
        const batch = text(block.batch_size);
        if (batch && !numberOk(batch, BATCH_RE, MAX_BATCH_SIZE)) {
            return RECONCILE_TEXTS.batchSize;
        }
        const cap = text(block.mismatch_cap);
        if (cap && !numberOk(cap, CAP_RE, MAX_MISMATCH_CAP)) {
            return RECONCILE_TEXTS.mismatchCap;
        }
        return "";
    }
};
