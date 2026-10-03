import type { Message, SseEvent, Todo, ToolEventData } from "../service/types";

/** The server keeps at most this many events per run (agents/run_activity.py MAX_EVENTS). */
export const MAX_EVENTS = 500;

/** The stored shape of a run's activity (`RunActivity.to_dict`), on a message as `activity`. */
export interface StoredActivity {
    events?: ToolEventData[];
    plan?: Todo[];
    dropped?: number;
}

/**
 * The activity of one run: the tool timeline and the current plan. `tool`
 * events upsert by `id` (start and end share it), `plan` replaces the todos,
 * and the oldest event goes once there are more than MAX_EVENTS.
 */
export default class ActivityState {
    public events: ToolEventData[] = [];
    public todos: Todo[] = [];

    public apply(event: SseEvent): void {
        if (event.type === "plan") {
            this.todos = (event.data.todos ?? []).map((t) => ({ content: t.content, status: t.status }));
        } else if (event.type === "tool") {
            const id = event.data.id;
            const index = id ? this.events.findIndex((e) => e.id === id) : -1;
            if (index >= 0) {
                this.events[index] = event.data;
            } else {
                this.events.push(event.data);
                if (this.events.length > MAX_EVENTS) {
                    this.events.shift();
                }
            }
        }
    }

    /** Replaces everything with a stored activity (or clears it for none). */
    public load(stored: StoredActivity | null | undefined): void {
        this.events = (stored?.events ?? []).slice(-MAX_EVENTS);
        this.todos = (stored?.plan ?? []).map((t) => ({ content: t.content, status: t.status }));
    }
}

/** The activity of the last assistant message, if the server stored one. */
export function lastActivity(messages: (Message & { activity?: StoredActivity })[]): StoredActivity | undefined {
    for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === "assistant") {
            return messages[i].activity;
        }
    }
    return undefined;
}

export interface TodoRow { content: string; mark: string; icon: string; state: string }

/** `[ ]` pending, `[~]` in progress, `[x]` done, as a mark, an icon and a state. */
export function todoView(todos: Todo[]): TodoRow[] {
    return todos.map((t) => {
        switch (t.status) {
            case "completed":
                return { content: t.content, mark: "[x]", icon: "sap-icon://accept", state: "Success" };
            case "in_progress":
                return { content: t.content, mark: "[~]", icon: "sap-icon://status-in-process", state: "Information" };
            default:
                return { content: t.content, mark: "[ ]", icon: "sap-icon://border", state: "None" };
        }
    });
}

export interface EventRow {
    id: string;
    agent: string;
    title: string;
    detail: string;
    icon: string;
    state: string;
    status: string;
    output: string;
    hasOutput: boolean;
    /** The read-only guard refused the call (code `readonly_refused`), not a failing tool. */
    refused: boolean;
}

/**
 * One timeline row per event: running / ok / error get an icon, notes none.
 * A call the read-only guard refused is a warning with a lock, labelled
 * "refused (read-only)" by the view.
 */
export function eventRows(events: ToolEventData[]): EventRow[] {
    return events.map((e) => {
        const output = e.output ?? "";
        const base = {
            id: e.id, agent: e.agent, status: e.status, output,
            title: e.tool || e.detail, detail: e.tool ? e.detail : "",
            hasOutput: !!output, refused: false
        };
        if (e.code === "readonly_refused") {
            return { ...base, refused: true, icon: "sap-icon://locked", state: "Warning" };
        }
        switch (e.status) {
            case "running":
                return { ...base, icon: "sap-icon://status-in-process", state: "Information" };
            case "ok":
                return { ...base, icon: "sap-icon://sys-enter-2", state: "Success" };
            case "error":
                return { ...base, icon: "sap-icon://error", state: "Error" };
            default:
                return { ...base, icon: "", state: "None" };
        }
    });
}
