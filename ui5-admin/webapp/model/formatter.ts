import { ValueState } from "sap/ui/core/library";
import type { McpServer, RunStatus } from "../service/types";

const EM_DASH = "—";

const STATUS_STATES: Record<string, ValueState> = {
    success: ValueState.Success,
    failed: ValueState.Error,
    interrupted: ValueState.Warning,
    running: ValueState.Information
};

export default {

    /** Maps a run status onto a semantic colour. Unknown values stay neutral. */
    runStatusState(status: RunStatus): ValueState {
        return STATUS_STATES[status] ?? ValueState.None;
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
    }
};
