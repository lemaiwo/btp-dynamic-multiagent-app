import type { SessionSummary, SessionType, Stage, WaitingReason } from "../service/types";

/**
 * The session worklist: the "waiting" column text, search and filters, and
 * the order (last updated first). Pure functions; no input is mutated.
 */

type Translate = (key: string, args?: (string | number)[]) => string;

const WAITING_KEYS: Record<WaitingReason, string> = {
    approval: "worklistWaitingApproval",
    comments: "worklistWaitingComments",
    changes: "worklistWaitingChanges",
    document: "worklistWaitingDocument"
};

/** What the session waits for, worded; `""` for none or a reason the UI does not know. */
export function waitingText(w: WaitingReason | null | undefined, t: Translate): string {
    return typeof w === "string" && Object.prototype.hasOwnProperty.call(WAITING_KEYS, w) ? t(WAITING_KEYS[w]) : "";
}

/** Worklist filters; a missing or `null` filter does not filter. */
export interface WorklistFilters {
    type?: SessionType | null;
    stage?: Stage | null;
    /** `any`: waiting for something; `none`: waiting for nothing; a reason: exactly that. */
    waiting?: WaitingReason | "any" | "none" | null;
}

/** The session passes the search (title, target or an object name, case-insensitive) and every filter set. */
export function matches(session: SessionSummary, query: string | null | undefined, filters: WorklistFilters): boolean {
    if (filters.type && session.type !== filters.type) {
        return false;
    }
    if (filters.stage && session.stage !== filters.stage) {
        return false;
    }
    if (filters.waiting === "any" && !session.waiting) {
        return false;
    }
    if (filters.waiting === "none" && session.waiting) {
        return false;
    }
    if (filters.waiting && filters.waiting !== "any" && filters.waiting !== "none" && session.waiting !== filters.waiting) {
        return false;
    }
    const q = (query ?? "").trim().toLowerCase();
    if (!q) {
        return true;
    }
    return (session.title ?? "").toLowerCase().includes(q) || (session.target ?? "").toLowerCase().includes(q)
        || (session.objects ?? []).some((name) => name.toLowerCase().includes(q));
}

function time(value: string | null | undefined): number | null {
    if (!value) {
        return null;
    }
    const ms = Date.parse(value);
    return Number.isNaN(ms) ? null : ms;
}

/** Newest first, missing or unreadable dates last. */
function byTimeDesc(a: number | null, b: number | null): number {
    if (a === b) {
        return 0;
    }
    if (a === null) {
        return 1;
    }
    if (b === null) {
        return -1;
    }
    return b - a;
}

/** A sorted copy: `updated_at` descending, then `created_at` descending, then `id`. */
export function sortSessions<T extends SessionSummary>(list: readonly T[]): T[] {
    return [...list].sort((a, b) =>
        byTimeDesc(time(a.updated_at), time(b.updated_at))
        || byTimeDesc(time(a.created_at), time(b.created_at))
        || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

// --- U6: what a worklist row shows ------------------------------------------

/** The status cell: a "waiting for you" marker with its reason, else a running / done / idle badge. */
export interface StatusView {
    text: string;
    /** A `sap.ui.core.ValueState` name. */
    state: "Warning" | "Information" | "Success" | "None";
    /** Badges are inverted; the waiting marker is not, so it reads as a call to act. */
    inverted: boolean;
    /** What the session waits for, worded; `""` unless waiting. */
    reason: string;
    icon: string;
}

/** Waiting wins over running and done: the developer has something to decide either way. */
export function statusView(session: SessionSummary, t: Translate): StatusView {
    const reason = waitingText(session.waiting, t);
    if (reason) {
        return { text: t("worklistStatusWaiting"), state: "Warning", inverted: false, reason, icon: "sap-icon://alert" };
    }
    if (session.status === "running") {
        return { text: t("worklistStatusRunning"), state: "Information", inverted: true, reason: "", icon: "" };
    }
    if (session.stage === "done") {
        return { text: t("worklistStatusDone"), state: "Success", inverted: true, reason: "", icon: "" };
    }
    return { text: t("worklistStatusIdle"), state: "None", inverted: true, reason: "", icon: "" };
}

/** The header line: sessions waiting for the developer (a reason the UI knows) and sessions running. */
export function counts(list: readonly SessionSummary[]): { waiting: number; running: number } {
    return {
        waiting: list.filter((s) => waitingText(s.waiting, (k) => k) !== "").length,
        running: list.filter((s) => s.status === "running").length
    };
}

const LINE_MAX = 3;

/**
 * Up to three object names; more are summed up as "A, B, C +2 more". The
 * server lists at most five names (`SessionOut.objects`) and counts them all
 * in `objects_total`; a `total` below the names listed is ignored.
 */
export function objectLine(names: readonly string[], t: Translate, total = names.length): string {
    const all = Math.max(total, names.length);
    if (all <= LINE_MAX) {
        return names.join(", ");
    }
    return t("worklistObjectsMore", [names.slice(0, LINE_MAX).join(", "), all - LINE_MAX]);
}

/**
 * The "changes / findings" cell: the objects with a proposed change of a
 * change session, the findings of a diagnose session ("1 finding",
 * "3 findings"); a dash for none or a server without the field.
 */
export function changedText(session: SessionSummary, t: Translate): string {
    if (session.type === "diagnose") {
        const n = session.findings_count ?? 0;
        return n === 1 ? t("worklistFindingsOne") : n > 1 ? t("worklistFindings", [n]) : t("worklistNoValue");
    }
    const changed = session.changed_objects ?? 0;
    return changed > 0 ? String(changed) : t("worklistNoValue");
}

/** A search text or any filter is set (the empty state then says "no match" instead of "no sessions"). */
export function hasFilters(query: string | null | undefined, filters: WorklistFilters): boolean {
    return !!(query ?? "").trim() || !!filters.type || !!filters.stage || !!filters.waiting;
}
