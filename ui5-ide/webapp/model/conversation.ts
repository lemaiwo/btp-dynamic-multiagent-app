import type { Activity, Approval, Message } from "../service/types";

/**
 * The conversation column's order: stored messages and a diagnose session's
 * trace approvals in one list, by time. Pure functions; no UI imports.
 */

/** One entry of the conversation column, in time order. */
export type Entry = { type: "message"; message: Message } | { type: "approval"; approval: Approval };

/** An API time (UTC, with or without a zone) in ms; `null` when there is none or it cannot be read. */
export function timeOf(iso: string | null | undefined): number | null {
    if (!iso) {
        return null;
    }
    const ms = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
    return Number.isNaN(ms) ? null : ms;
}

/**
 * Messages and approvals by `created_at`, oldest first: a card shows up
 * between the messages it came between. On the same time a message comes
 * first; equal entries keep their input order (messages as listed, the
 * approvals oldest first); entries without a readable time follow the dated
 * ones, messages first. The inputs are not changed.
 */
export function interleave(messages: readonly Message[], approvals: readonly Approval[]): Entry[] {
    const oldestFirst = [...approvals].reverse();   // the list route answers newest first
    const entries = [
        ...messages.map((message, i) => ({ entry: { type: "message", message } as Entry, t: timeOf(message.created_at), rank: 0, i })),
        ...oldestFirst.map((approval, i) => ({ entry: { type: "approval", approval } as Entry, t: timeOf(approval.created_at), rank: 1, i }))
    ];
    entries.sort((a, b) => {
        if (a.t !== b.t) {
            if (a.t === null) {
                return 1;
            }
            if (b.t === null) {
                return -1;
            }
            return a.t - b.t;
        }
        return a.rank - b.rank || a.i - b.i;
    });
    return entries.map((e) => e.entry);
}

/** How many tool calls (dropped ones included) and plan steps an activity holds. */
export function activityCounts(activity: Activity): { tools: number; steps: number } {
    const tools = (activity.events ?? []).filter((e) => e.kind === "tool").length + (activity.dropped ?? 0);
    return { tools, steps: (activity.plan ?? []).length };
}

/**
 * Rendered (sanitised) HTML of stored messages by id: a message is rendered
 * again only when its content changed, so re-rendering the conversation does
 * not re-parse and re-sanitise every answer.
 */
export class HtmlCache {
    private readonly entries = new Map<string, { content: string; html: string }>();

    public get(id: string, content: string, render: (md: string) => string): string {
        const hit = this.entries.get(id);
        if (hit && hit.content === content) {
            return hit.html;
        }
        const html = render(content);
        this.entries.set(id, { content, html });
        return html;
    }

    public clear(): void {
        this.entries.clear();
    }
}
