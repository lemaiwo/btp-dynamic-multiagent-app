import type { McpServer } from "../service/types";

/**
 * `builtin:bitbucket`: the agent's pull request review entry.
 *
 * Mirrors `agents/bitbucket_config.py` (`check_entry`, `clean_entry`), which
 * is authoritative: this module adds no rule of its own. The server refuses
 * every key that is not the entry's, so `clean` builds the block from nothing
 * instead of filtering the dialog's form.
 */
export const BITBUCKET_URL = "builtin:bitbucket";
const DESTINATION_RE = /^[A-Za-z0-9_.-]{1,200}$/;
const WORKSPACE_RE = /^[a-z0-9][a-z0-9_-]{0,61}$/;
const REPOSITORY_RE = /^[a-z0-9_][a-z0-9._-]{0,61}$/;
const BRANCH_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/;
const MAX_REPOSITORIES = 50;
const SWITCHES = ["allow_comment", "allow_approve", "require_green_builds"];

function isUrl(url: string | undefined | null): boolean {
    return (url || "").trim().replace(/\/+$/, "").toLowerCase() === BITBUCKET_URL;
}

function matches(pattern: RegExp, value: unknown): boolean {
    return typeof value === "string" && pattern.test(value);
}

function block(server: McpServer): Record<string, unknown> {
    return (server.oauth || {}) as Record<string, unknown>;
}

function approving(servers: McpServer[] | undefined): string[] {
    return (servers || [])
        .filter((s) => isUrl(s.url) && block(s).allow_approve === true)
        .map((s) => String(block(s).workspace ?? ""));
}

export default {
    BITBUCKET_URL,
    MAX_REPOSITORIES,

    /** True for the entry in any spelling the save gate accepts
     * (`Builtin:Bitbucket`, a trailing `/`). */
    isBitbucketUrl(url: string | undefined | null): boolean {
        return isUrl(url);
    },

    /** The "Repositories" field as a list: comma-separated, the blanks around
     * a comma and empty parts dropped. An empty field is an empty list, which
     * `clean` leaves out (the whole workspace). */
    parseRepositories(text: string | undefined | null): string[] {
        return (text || "").split(",").map((part) => part.trim()).filter((part) => part !== "");
    },

    /** A stored list as the field shows it; anything else is shown as blank. */
    formatRepositories(value: unknown): string {
        return Array.isArray(value) ? value.map((item) => String(item)).join(", ") : "";
    },

    /**
     * Exactly what `clean_entry` stores: its own keys and nothing of another
     * toolset, a switch only as the one boolean that means something.
     *
     * The destination name is trimmed, as for every other toolset. The pins
     * (`workspace`, `repositories`, `branch`) are sent as typed: the server
     * stores a pin in the form it checked and refuses edge whitespace instead
     * of repairing it, so `validate` and the server must see the same text.
     */
    clean(raw: Record<string, unknown>): Record<string, unknown> {
        const out: Record<string, unknown> = {
            destination: String(raw.destination ?? "").trim(),
            workspace: typeof raw.workspace === "string" ? raw.workspace : ""
        };
        if (Array.isArray(raw.repositories) && raw.repositories.length) {
            out.repositories = raw.repositories.slice();
        }
        if (typeof raw.branch === "string" && raw.branch) {
            out.branch = raw.branch;
        }
        if (raw.allow_comment === true) {
            out.allow_comment = true;
        }
        if (raw.allow_approve === true) {
            out.allow_approve = true;
        }
        if (raw.require_green_builds === false) {
            out.require_green_builds = false;
        }
        return out;
    },

    /** An error text, or "" when the entry is valid. Fixed texts: a value
     * that was typed is never quoted. */
    validate(oauth: McpServer["oauth"] | undefined, authMode: string): string {
        if (authMode !== "destination") {
            return "Bitbucket requires auth mode 'destination': the technical user's "
                + "credential lives in the BTP destination.";
        }
        const cfg = (oauth || {}) as Record<string, unknown>;
        if (!matches(DESTINATION_RE, cfg.destination)) {
            return "Bitbucket requires a destination name (letters, digits, '_', '.' and '-').";
        }
        if (!matches(WORKSPACE_RE, cfg.workspace)) {
            return "Bitbucket requires one workspace slug (lower-case letters, digits, '_' and '-', "
                + "no spaces).";
        }
        if (cfg.repositories !== undefined && cfg.repositories !== null) {
            const list = Array.isArray(cfg.repositories) ? cfg.repositories as unknown[] : [];
            if (list.length === 0 || list.length > MAX_REPOSITORIES) {
                return `Repositories: 1 to ${MAX_REPOSITORIES} slugs, or leave the field empty for the whole workspace.`;
            }
            if (!list.every((item) => matches(REPOSITORY_RE, item))) {
                return "Repositories: each entry is one repository slug (lower-case letters, digits, "
                    + "'_', '-' and '.').";
            }
            if (new Set(list).size !== list.length) {
                return "Repositories: a repository is listed twice.";
            }
        }
        if (cfg.branch !== undefined && cfg.branch !== null) {
            const branch = cfg.branch;
            if (typeof branch !== "string" || !BRANCH_RE.test(branch) || branch.indexOf("..") > -1
                || branch.indexOf("//") > -1 || /(\/|\.|\.lock)$/.test(branch)) {
                return "The target branch is not a valid branch name (no spaces around it).";
            }
        }
        if (SWITCHES.some((key) => cfg[key] !== undefined && cfg[key] !== null
            && typeof cfg[key] !== "boolean")) {
            return "Bitbucket: a switch must be on or off.";
        }
        if (cfg.allow_approve === true && cfg.allow_comment !== true) {
            return "Approving requires commenting: an approval is only sent after the review comment.";
        }
        return "";
    },

    /** Workspaces whose entry approves after this save and did not before
     * (only exactly `allow_approve: true` approves). */
    newlyApproves(stored: McpServer[] | undefined, toSave: McpServer[] | undefined): string[] {
        const before = approving(stored);
        return approving(toSave).filter((workspace) => before.indexOf(workspace) === -1);
    }
};
