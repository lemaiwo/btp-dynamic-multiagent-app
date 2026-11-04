import type { McpServer } from "../service/types";

/**
 * `builtin:git`: the agent's one Git entry (GitHub, Bitbucket Cloud or GitLab,
 * through a BTP destination whose credential is the technical user's token).
 *
 * Mirrors `agents/git_config.py` (addendum h, decision 1 of the web chat
 * design), which is authoritative: this module adds no rule of its own. The
 * server refuses every key that is not the entry's, so `clean` builds the
 * block from nothing instead of filtering the dialog's form, and a refusal
 * here is a fixed text that never quotes what was typed.
 */
export const GIT_URL = "builtin:git";
export type GitProvider = "github" | "bitbucket" | "gitlab";
/** The providers in the order the select offers them. */
export const GIT_PROVIDERS: GitProvider[] = ["github", "bitbucket", "gitlab"];
const DESTINATION_RE = /^[A-Za-z0-9_.-]{1,200}$/;
/** `_GITHUB_RE`, `_BITBUCKET_RE`, `_GITLAB_SEGMENT_RE` of agents/git_config.py. */
const GITHUB_RE = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/;
const BITBUCKET_RE = /^[a-z0-9][a-z0-9_-]{0,61}\/[a-z0-9_][a-z0-9._-]{0,61}$/;
const GITLAB_SEGMENT_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$/;
const BRANCH_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/;
/** `branch_prefix` of decision 1: a short name ending in `/`. */
const PREFIX_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}\/$/;
const MAX_REPOSITORIES = 50;
/** The server's defaults for an entry that names none. */
export const DEFAULT_BASE_BRANCH = "main";
export const DEFAULT_BRANCH_PREFIX = "agent/";
const SWITCHES = ["allow_push", "allow_pull_request"];

function isUrl(url: string | undefined | null): boolean {
    return (url || "").trim().replace(/\/+$/, "").toLowerCase() === GIT_URL;
}

function matches(pattern: RegExp, value: unknown): boolean {
    return typeof value === "string" && pattern.test(value);
}

/**
 * A repository path in the provider's form, as `repository_ok` in
 * agents/git_config.py reads it: `owner/name` (GitHub), `workspace/slug`
 * (Bitbucket, lower case), `group[/subgroup]/project` (GitLab, 2 to 20
 * segments, at most 255 characters).
 */
function isRepositoryPath(value: unknown, provider: unknown): boolean {
    if (typeof value !== "string") {
        return false;
    }
    if (provider === "github") {
        const name = value.split("/")[1];
        return GITHUB_RE.test(value) && name !== "." && name !== "..";
    }
    if (provider === "bitbucket") {
        return BITBUCKET_RE.test(value);
    }
    if (provider === "gitlab") {
        const parts = value.split("/");
        return value.length <= 255 && parts.length >= 2 && parts.length <= 20 && parts.every((p) =>
            GITLAB_SEGMENT_RE.test(p) && !/(\.git|\.atom|\.)$/.test(p) && p.indexOf("..") === -1);
    }
    return false;
}

/** `branch_ok` of agents/git_config.py. */
function isBranch(value: unknown): boolean {
    return typeof value === "string" && BRANCH_RE.test(value) && value.indexOf("..") === -1
        && value.indexOf("//") === -1 && value.indexOf("/.") === -1 && !/(\/|\.|\.lock)$/.test(value);
}

export default {
    GIT_URL,
    GIT_PROVIDERS,
    MAX_REPOSITORIES,

    /** True for the entry in any spelling the save gate accepts
     * (`Builtin:Git`, a trailing `/`). */
    isGitUrl(url: string | undefined | null): boolean {
        return isUrl(url);
    },

    /** The "Repositories" field as a list: comma-separated, the blanks around
     * a comma (the separator's) and empty parts dropped; each path otherwise
     * as typed. */
    parseRepositories(text: string | undefined | null): string[] {
        return (text || "").split(",").map((part) => part.trim()).filter((part) => part !== "");
    },

    /** A stored list as the field shows it; anything else is shown as blank. */
    formatRepositories(value: unknown): string {
        return Array.isArray(value) ? value.map((item) => String(item)).join(", ") : "";
    },

    /**
     * Exactly what the server's cleaner stores: its own keys and nothing of
     * another toolset, a switch only as the JSON boolean `true` (off is the
     * key left out), a blank base branch or prefix left out (the server's
     * default). The destination name, the provider, the paths, the base
     * branch and the prefix are sent as typed: the server refuses edge
     * whitespace instead of repairing it, so `validate` and the server see
     * the same text.
     */
    clean(raw: Record<string, unknown>): Record<string, unknown> {
        const out: Record<string, unknown> = {
            destination: typeof raw.destination === "string" ? raw.destination : "",
            provider: typeof raw.provider === "string" ? raw.provider : "",
            repositories: Array.isArray(raw.repositories) ? raw.repositories.slice() : []
        };
        if (typeof raw.base_branch === "string" && raw.base_branch) {
            out.base_branch = raw.base_branch;
        }
        if (typeof raw.branch_prefix === "string" && raw.branch_prefix) {
            out.branch_prefix = raw.branch_prefix;
        }
        if (raw.allow_push === true) {
            out.allow_push = true;
        }
        if (raw.allow_pull_request === true) {
            out.allow_pull_request = true;
        }
        return out;
    },

    /** An error text, or "" when the entry is valid. Fixed texts: a value
     * that was typed is never quoted. */
    validate(oauth: McpServer["oauth"] | undefined, authMode: string): string {
        if (authMode !== "destination") {
            return "Git requires auth mode 'destination': the technical user's token lives in the BTP destination.";
        }
        const cfg = (oauth || {}) as Record<string, unknown>;
        if (cfg.user_context === true) {
            return "Git cannot act as the signed-in user: the destination's technical user commits.";
        }
        if (!matches(DESTINATION_RE, cfg.destination)) {
            return "Git requires a destination name (letters, digits, '_', '.' and '-').";
        }
        if (typeof cfg.provider !== "string" || GIT_PROVIDERS.indexOf(cfg.provider as GitProvider) === -1) {
            return "Git requires a provider: GitHub, Bitbucket Cloud or GitLab.";
        }
        const list = Array.isArray(cfg.repositories) ? cfg.repositories as unknown[] : [];
        if (list.length === 0 || list.length > MAX_REPOSITORIES) {
            return `Repositories: 1 to ${MAX_REPOSITORIES} repository paths, separated by commas.`;
        }
        if (!list.every((item) => isRepositoryPath(item, cfg.provider))) {
            return cfg.provider === "gitlab"
                ? "Repositories: each entry is a GitLab project path, group/project or group/subgroup/project."
                : cfg.provider === "bitbucket"
                    ? "Repositories: each entry is workspace/slug."
                    : "Repositories: each entry is owner/name.";
        }
        if (new Set(list).size !== list.length) {
            return "Repositories: a repository is listed twice.";
        }
        if (cfg.base_branch !== undefined && cfg.base_branch !== null && !isBranch(cfg.base_branch)) {
            return "The base branch is not a valid branch name (no spaces around it).";
        }
        if (cfg.branch_prefix !== undefined && cfg.branch_prefix !== null
            && !matches(PREFIX_RE, cfg.branch_prefix)) {
            return "The branch prefix is one short name ending in '/', e.g. agent/ (letters, digits, '.', '_' and '-').";
        }
        const base = typeof cfg.base_branch === "string" && cfg.base_branch ? cfg.base_branch : DEFAULT_BASE_BRANCH;
        const prefix = typeof cfg.branch_prefix === "string" ? cfg.branch_prefix : DEFAULT_BRANCH_PREFIX;
        if (base.indexOf(prefix) === 0) {
            return "The base branch must not carry the branch prefix: the agent pushes only to prefixed branches.";
        }
        if (SWITCHES.some((key) => cfg[key] !== undefined && cfg[key] !== null && typeof cfg[key] !== "boolean")) {
            return "Git: a switch must be on or off.";
        }
        return "";
    }
};
