import { ValueState } from "sap/ui/core/library";
import type {
    McpServer, RunStatus, TokenState, WorkflowItemStatus, WorkflowRunStatus
} from "../service/types";

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
