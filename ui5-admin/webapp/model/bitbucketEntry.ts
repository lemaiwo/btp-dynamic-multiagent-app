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
/** `DEFAULT_BRANCH` of `agents/bitbucket_config.py`: the branch of an entry that names none. */
const DEFAULT_BRANCH = "main";
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

/** The destination name as `clean` sends it: as typed. */
function destinationOf(cfg: Record<string, unknown>): string {
    return typeof cfg.destination === "string" ? cfg.destination : "";
}

function quoted(names: string[]): string {
    return names.map((name) => `"${name}"`).join(", ");
}

function branchOf(cfg: Record<string, unknown>): string {
    return typeof cfg.branch === "string" && cfg.branch ? cfg.branch : DEFAULT_BRANCH;
}

/** The pinned repositories; an empty list is the whole workspace. */
function repositoriesOf(cfg: Record<string, unknown>): string[] {
    return Array.isArray(cfg.repositories) ? cfg.repositories.map((item) => String(item)) : [];
}

/**
 * One thing the agent's Save has to ask about: an entry that approves after
 * the save and either did not before (`approve`: everything it opens is its
 * own values) or did, for less (`widened`: the four last fields say exactly
 * what is new; a narrowing is in none of them).
 */
export interface BitbucketApprovalAsk {
    reason: "approve" | "widened";
    workspace: string;
    /** The branch after the save (the server's default when none is pinned). */
    branch: string;
    /** The repositories after the save; empty = the whole workspace. */
    repositories: string[];
    requireGreenBuilds: boolean;
    /** The branch it approved to before, when that changes; else "". */
    branchBefore: string;
    /** Repositories it did not approve in before. */
    repositoriesAdded: string[];
    /** A list replaced by the whole workspace. */
    wholeWorkspace: boolean;
    /** Builds had to be successful before and no longer have to. */
    buildsDropped: boolean;
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
     * The destination name and the pins (`workspace`, `repositories`,
     * `branch`) are sent as typed: the server (`bitbucket_config.check_entry`)
     * stores a value in the form it checked and refuses edge whitespace
     * instead of repairing it, so `validate` and the server must see the same
     * text, and a refusal reaches the admin instead of a silent repair.
     */
    clean(raw: Record<string, unknown>): Record<string, unknown> {
        const out: Record<string, unknown> = {
            destination: destinationOf(raw),
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

    /** Whether an agent with these servers approves pull requests (only
     * exactly `allow_approve: true` on an entry of this toolset does). */
    approves(servers: McpServer[] | undefined | null): boolean {
        return (servers || []).some((s) => isUrl(s.url) && block(s).allow_approve === true);
    },

    /**
     * Whether an agent with these servers ASKS for approving, as the rules
     * about who may reach such an agent read it (`asks_to_approve` in
     * `agents/bitbucket_config.py`): `allow_approve` holds anything but
     * `false` or nothing. Wider than `approves` on purpose, as on the
     * server: a hand-written `"true"` gives no toolset at all and still
     * must not read as "does not approve" there.
     */
    asksToApprove(servers: McpServer[] | undefined | null): boolean {
        return (servers || []).some((s) => {
            const allow = isUrl(s.url) && s.oauth !== null && typeof s.oauth === "object"
                ? block(s).allow_approve : undefined;
            return allow !== undefined && allow !== null && allow !== false;
        });
    },

    /**
     * What the agent's Save asks about: every entry that approves after the
     * save (exactly `allow_approve: true`) and opens something by it. An
     * entry is the same one when its workspace and its destination are (an
     * agent has one entry). Another workspace is a new approval, said in
     * full, and so is another destination: it decides which technical
     * account approves and which repositories that account sees. Removing the
     * entry, switching approving off, removing a repository, cutting the
     * whole workspace down to a list and requiring the builds again ask
     * nothing.
     */
    approvalsToAsk(stored: McpServer[] | undefined, toSave: McpServer[] | undefined): BitbucketApprovalAsk[] {
        const approvingBefore = (stored || [])
            .filter((s) => isUrl(s.url) && block(s).allow_approve === true).map(block);
        const asks: BitbucketApprovalAsk[] = [];
        (toSave || []).forEach((server) => {
            const cfg = block(server);
            if (!isUrl(server.url) || cfg.allow_approve !== true) {
                return;
            }
            const workspace = String(cfg.workspace ?? "");
            const ask: BitbucketApprovalAsk = {
                reason: "approve",
                workspace,
                branch: branchOf(cfg),
                repositories: repositoriesOf(cfg),
                requireGreenBuilds: cfg.require_green_builds !== false,
                branchBefore: "",
                repositoriesAdded: [],
                wholeWorkspace: false,
                buildsDropped: false
            };
            const destination = destinationOf(cfg);
            const before = approvingBefore.filter((b) => String(b.workspace ?? "") === workspace
                && destinationOf(b) === destination)[0];
            if (!before) {
                asks.push(ask);
                return;
            }
            const listedBefore = repositoriesOf(before);
            if (branchOf(before) !== ask.branch) {
                ask.branchBefore = branchOf(before);
            }
            if (listedBefore.length > 0) {
                ask.wholeWorkspace = ask.repositories.length === 0;
                ask.repositoriesAdded = ask.repositories.filter((name) => listedBefore.indexOf(name) === -1);
            }
            ask.buildsDropped = before.require_green_builds !== false && !ask.requireGreenBuilds;
            if (ask.branchBefore || ask.wholeWorkspace || ask.repositoriesAdded.length > 0 || ask.buildsDropped) {
                ask.reason = "widened";
                asks.push(ask);
            }
        });
        return asks;
    },

    /**
     * One ask in plain words, from the texts of the resource bundle (`text`
     * is the controller's lookup). A new approval names all it opens
     * (branch, workspace, repositories, whether builds count); a widened one
     * names only what is new. The agent page's Save and the import use it,
     * so both say the same.
     */
    question(
        agentName: string, ask: BitbucketApprovalAsk, text: (key: string, args?: (string | number)[]) => string
    ): string {
        if (ask.reason === "approve") {
            const repositories = ask.repositories.length > 0
                ? text("bitbucketSomeRepositories", [quoted(ask.repositories)])
                : text("bitbucketAllRepositories");
            return text(
                ask.requireGreenBuilds ? "bitbucketApproveSaveQuestion" : "bitbucketApproveSaveQuestionNoBuilds",
                [agentName, ask.branch, ask.workspace, repositories]);
        }
        const lines = [text("bitbucketWidenSaveQuestion", [agentName, ask.workspace])];
        if (ask.branchBefore) {
            lines.push(text("bitbucketWidenBranch", [ask.branch, ask.branchBefore]));
        }
        if (ask.wholeWorkspace) {
            lines.push(text("bitbucketWidenRepositoriesAll"));
        }
        if (ask.repositoriesAdded.length > 0) {
            lines.push(text("bitbucketWidenRepositoriesAdded", [quoted(ask.repositoriesAdded)]));
        }
        if (ask.buildsDropped) {
            lines.push(text("bitbucketWidenBuilds"));
        }
        return lines.join("\n");
    }
};
