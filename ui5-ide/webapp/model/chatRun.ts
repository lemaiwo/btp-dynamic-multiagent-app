import type {
    ArtifactEventData, DoneEventData, ErrorEventData, FileEventData, Message, SseEvent, Stage, Todo,
    ToolEventData, UsageEventData
} from "../service/types";

/**
 * What one streamed run (plan §1.3) has delivered so far. `reduceRun` folds
 * one SSE event into it without mutating the old state; the controller
 * renders from it and runs the side effects (open a document, reload the
 * tree, reload the session).
 */
export interface RunState {
    runId: string;
    stage: Stage | "";
    /** The answer text so far, from the `text` deltas. */
    text: string;
    /** RunActivity tool events, upserted by `id` (start and end share it). */
    tools: ToolEventData[];
    todos: Todo[];
    usage: UsageEventData | null;
    error: ErrorEventData | null;
    /** `error` frames that are remarks, not failures ({@link isRunNote}): the run went on. */
    notes: ErrorEventData[];
    artifacts: ArtifactEventData[];
    /** One entry per changed workspace path. */
    files: FileEventData[];
    done: DoneEventData | null;
    /** `done` arrived (always the last frame). */
    finished: boolean;
}

/**
 * `error` frames that do not fail the run: the diagnose agent has no server
 * for the session's target, or the target's conventions could not be read.
 * The run carries on without its diagnostics tools; the UI shows a warning.
 */
const NOTE_CODES = ["no_diagnose_server", "conventions_unavailable"];

export function isRunNote(code: string | null | undefined): boolean {
    return !!code && NOTE_CODES.includes(code);
}

export function newRun(): RunState {
    return {
        runId: "", stage: "", text: "", tools: [], todos: [], usage: null, error: null, notes: [],
        artifacts: [], files: [], done: null, finished: false
    };
}

export function reduceRun(state: RunState, event: SseEvent): RunState {
    switch (event.type) {
        case "run":
            return { ...state, runId: event.data.run_id, stage: event.data.stage };
        case "text":
            return { ...state, text: state.text + (event.data.delta ?? "") };
        case "tool": {
            const index = state.tools.findIndex((t) => t.id === event.data.id);
            const tools = state.tools.slice();
            if (index >= 0) {
                tools[index] = event.data;
            } else {
                tools.push(event.data);
            }
            return { ...state, tools };
        }
        case "plan":
            return { ...state, todos: event.data.todos ?? [] };
        case "usage":
            return { ...state, usage: event.data };
        case "artifact":
            return { ...state, artifacts: [...state.artifacts, event.data] };
        case "file":
            return {
                ...state,
                files: [...state.files.filter((f) => f.path !== event.data.path), event.data]
            };
        case "error":
            return isRunNote(event.data.code)
                ? { ...state, notes: [...state.notes, event.data] }
                : { ...state, error: event.data };
        case "done":
            return { ...state, done: event.data, finished: true };
        default:
            return state;
    }
}

/** The latest tool call that has not ended yet. */
export function activeTool(state: RunState): ToolEventData | undefined {
    for (let i = state.tools.length - 1; i >= 0; i--) {
        if (state.tools[i].status === "running") {
            return state.tools[i];
        }
    }
    return undefined;
}

/** The runner saves a stopped run's answer with a "(cancelled)" tail. */
export function isCancelled(content: string | null | undefined): boolean {
    return /\(cancelled\)\s*$/.test(content ?? "");
}

/** One row of the chat list. */
export interface ChatItem {
    id: string;
    role: Message["role"];
    isUser: boolean;
    /** User text, shown as plain text. */
    content: string;
    /** Assistant markdown, rendered and sanitized (model/markdown); "" for the user. */
    html: string;
    cancelled: boolean;
}

/** A stored message as a chat row; `render` turns markdown into sanitized HTML. */
export function toChatItem(message: Message, render: (md: string) => string): ChatItem {
    const isUser = message.role === "user";
    return {
        id: message.id,
        role: message.role,
        isUser,
        content: message.content ?? "",
        html: isUser ? "" : render(message.content ?? ""),
        cancelled: !isUser && isCancelled(message.content)
    };
}
