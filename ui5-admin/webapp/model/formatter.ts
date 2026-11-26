import { ValueState } from "sap/ui/core/library";
import NumberFormat from "sap/ui/core/format/NumberFormat";
import formatMessage from "sap/base/strings/formatMessage";
import type {
    McpServer, RunStatus, RunUsage, TokenState, WorkflowItemStatus, WorkflowRunStatus
} from "../service/types";

import oauthConfig from "./oauthConfig";

const EM_DASH = "—";

const STATUS_STATES: Record<string, ValueState> = {
    success: ValueState.Success,
    failed: ValueState.Error,
    interrupted: ValueState.Warning,
    running: ValueState.Information,
    // Legacy: no current code writes it, but stored rows still carry it —
    // it meant "finished, but something needs attention", so Warning.
    degraded: ValueState.Warning
};

const WORKFLOW_RUN_STATUS_STATES: Record<WorkflowRunStatus, ValueState> = {
    success: ValueState.Success,
    failed: ValueState.Error,
    interrupted: ValueState.Warning,
    running: ValueState.Information,
    // Some items succeeded while at least one other failed -- the most
    // common real outcome. Warning rather than Error keeps it from reading
    // as a generic failure.
    partial: ValueState.Warning
};

const TOKEN_STATES: Record<TokenState, ValueState> = {
    valid: ValueState.Success,
    // The access token has expired, but PerUserOAuth2Auth renews it from the
    // refresh token without anyone signing in — the connection works, so this
    // is not something to flag.
    refreshable: ValueState.Success,
    // Nothing is broken yet: the agent has simply never been connected.
    none: ValueState.Warning,
    expired: ValueState.Error
};

const WORKFLOW_ITEM_STATUS_STATES: Record<WorkflowItemStatus, ValueState> = {
    success: ValueState.Success,
    failed: ValueState.Error,
    interrupted: ValueState.Warning,
    running: ValueState.Information,
    // skip_seen_items refusing an item already completed by an earlier run
    // -- not a failure, so neither Error nor Warning.
    skipped: ValueState.None
};

/** A job run's activity entry: `agents/run_activity.py` writes these. */
const ACTIVITY_STATES: Record<string, ValueState> = {
    running: ValueState.Information,
    ok: ValueState.Success,
    error: ValueState.Error
};

/** A deep agent's todo, as `write_todos` sends it. */
const TODO_STATES: Record<string, ValueState> = {
    completed: ValueState.Success,
    in_progress: ValueState.Information,
    pending: ValueState.None
};

const USAGE_KEYS: (keyof RunUsage)[] = [
    "input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "requests"
];

/**
 * A run's `usage` when it is the object the server promises: every count a
 * finite, non-negative number. Anything else (null, an older backend that
 * sends nothing, a malformed value) is "no usage", so the cell stays empty
 * rather than showing NaN or a half-filled line.
 */
function readUsage(value: unknown): RunUsage | null {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return null;
    }
    const source = value as Record<string, unknown>;
    const ok = USAGE_KEYS.every((key) => {
        const count = source[key];
        return typeof count === "number" && Number.isFinite(count) && count >= 0;
    });
    return ok ? value as RunUsage : null;
}

/** A token count with the locale's grouping (`12,345` in English). */
function tokenCount(count: number): string {
    return NumberFormat.getIntegerInstance({ groupingEnabled: true }).format(count);
}

export default {

    /** Maps a run status onto a semantic colour. Unknown values stay neutral. */
    runStatusState(status: RunStatus): ValueState {
        return STATUS_STATES[status] ?? ValueState.None;
    },

    /** Maps a workflow run status onto a semantic colour, `"partial"`
     * included -- see WORKFLOW_RUN_STATUS_STATES. */
    workflowRunStatusState(status: WorkflowRunStatus): ValueState {
        return WORKFLOW_RUN_STATUS_STATES[status] ?? ValueState.None;
    },

    /** Maps a workflow item status onto a semantic colour, `"skipped"`
     * included. */
    workflowItemStatusState(status: WorkflowItemStatus): ValueState {
        return WORKFLOW_ITEM_STATUS_STATES[status] ?? ValueState.None;
    },

    /** Empty while a run is still in flight — there is no duration yet. */
    runDuration(startedAt: string | null, finishedAt: string | null): string {
        if (!startedAt || !finishedAt) {
            return "";
        }
        const ms = Date.parse(finishedAt) - Date.parse(startedAt);
        if (isNaN(ms) || ms < 0) {
            return "";
        }
        const totalSeconds = Math.round(ms / 1000);
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
    },

    /** The one line a run table shows for a run: its summary, or, when it
     * produced none (a failed preflight, a crashed step), its error. Without
     * the fallback a failed run shows an empty cell next to a red status. */
    runSummary(summary: string | null | undefined, error: string | null | undefined): string {
        const text = (summary || "").trim() || (error || "").trim();
        return text;
    },

    /** Colour for one activity entry of a job run; notes stay neutral. */
    activityState(status: string | undefined): ValueState {
        return (status && ACTIVITY_STATES[status]) || ValueState.None;
    },

    /** Colour for one item of a deep agent's plan. */
    todoState(status: string | undefined): ValueState {
        return (status && TODO_STATES[status]) || ValueState.None;
    },

    /** Time of day only: activity entries all belong to one run. */
    timeOfDay(iso: string | null | undefined): string {
        if (!iso) {
            return EM_DASH;
        }
        const parsed = new Date(iso);
        return isNaN(parsed.getTime()) ? EM_DASH : parsed.toLocaleTimeString();
    },

    /** Renders a null timestamp as an em dash rather than the string "null". */
    timestamp(iso: string | null): string {
        if (!iso) {
            return EM_DASH;
        }
        const parsed = new Date(iso);
        return isNaN(parsed.getTime()) ? EM_DASH : parsed.toLocaleString();
    },

    /** Colour for a server's credential. Servers that need no user token
     * (app_only, destination, none) stay neutral: they are connected by
     * configuration, and colouring them would suggest an action nobody can
     * take from this screen. */
    credentialState(needsToken: boolean, tokenState: TokenState): ValueState {
        if (!needsToken) {
            return ValueState.None;
        }
        return TOKEN_STATES[tokenState] ?? ValueState.Warning;
    },

    /**
     * Visibility of the server dialog's "Act as signed-in user" switch: a
     * destination server whose url may act as the user. Goes through
     * `oauthConfig.supportsUserContext` (the shared http(s) helper for remote
     * urls), so the switch shows exactly where `cleanOAuth` keeps the value.
     */
    userContextVisible(authMode: string | null | undefined, url: string | null | undefined): boolean {
        return authMode === "destination" && oauthConfig.supportsUserContext(url || "");
    },

    /** One-line description of an agent's toolsets for the list view. */
    serverSummary(servers: McpServer[]): string {
        if (!servers || servers.length === 0) {
            return EM_DASH;
        }
        if (servers.length === 1) {
            const url = servers[0].url;
            return url.toLowerCase().startsWith("builtin:") ? url : "1 MCP server";
        }
        return `${servers.length} MCP servers`;
    },

    /** Whether a run has token usage to show; false for null or a value
     * that is not the promised object. */
    hasTokenUsage(usage: unknown): boolean {
        return readUsage(usage) !== null;
    },

    /** The total tokens of a run, locale-grouped; empty without usage. */
    tokenTotal(usage: unknown): string {
        const read = readUsage(usage);
        return read ? tokenCount(read.total_tokens) : "";
    },

    /** Input and output tokens through the i18n `pattern` (`{0}` in, `{1}`
     * out); empty without usage. The pattern is a binding part so the
     * function stays pure and the text stays in the bundle. */
    tokenBreakdown(pattern: string, usage: unknown): string {
        const read = readUsage(usage);
        if (!read || !pattern) {
            return "";
        }
        return formatMessage(pattern, [tokenCount(read.input_tokens), tokenCount(read.output_tokens)]);
    },

    /** One step's tokens through the i18n `pattern` (`{0}` total, `{1}` in,
     * `{2}` out); empty without usage. */
    tokenStep(pattern: string, usage: unknown): string {
        const read = readUsage(usage);
        if (!read || !pattern) {
            return "";
        }
        return formatMessage(pattern, [
            tokenCount(read.total_tokens), tokenCount(read.input_tokens), tokenCount(read.output_tokens)
        ]);
    },

    /**
     * The run detail's token line: total, in, out, cached (only when any
     * were read from cache) and the number of model requests. Each pattern
     * is an i18n text passed as a binding part: `plain` is `{0}` total,
     * `{1}` in, `{2}` out, `{3}` the requests text; `cached` adds the cached
     * count as `{3}` and the requests text as `{4}`; `requestOne` and
     * `requestMany` take the request count as `{0}`. Empty without usage.
     */
    tokenDetail(
        plain: string, cached: string, requestOne: string, requestMany: string, usage: unknown
    ): string {
        const read = readUsage(usage);
        if (!read || !plain || !cached || !requestOne || !requestMany) {
            return "";
        }
        const requests = formatMessage(
            read.requests === 1 ? requestOne : requestMany, [tokenCount(read.requests)]
        );
        const total = tokenCount(read.total_tokens);
        const input = tokenCount(read.input_tokens);
        const output = tokenCount(read.output_tokens);
        if (read.cache_read_tokens > 0) {
            return formatMessage(cached, [total, input, output, tokenCount(read.cache_read_tokens), requests]);
        }
        return formatMessage(plain, [total, input, output, requests]);
    },

    /** Renders a workflow item's selected branches as a comma list. Empty
     * means the item entered no branch (main line only, or skipped before
     * the fan-out step could select one). */
    joinList(items: string[] | null | undefined): string {
        if (!items || items.length === 0) {
            return EM_DASH;
        }
        return items.join(", ");
    }
};
