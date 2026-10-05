import { hunksOf, unifiedRows, type UnifiedRow } from "./diffModel";
import type {
    BaseStatus, Comment, FileRevision, FileSummary, LintFinding, SyntaxItem, SyntaxStatus
} from "../service/types";
import { isProposal, isProposedObject } from "./stageGate";

/**
 * The changes view (Task U10): how a proposed object's base, syntax and lint
 * state are worded, the counts and the line ADT opens, which comments belong
 * to the revision shown, and the change stops. Pure: no DOM, no UI5.
 *
 * Wording (lead decisions): a syntax `ok` is "No syntax messages" in a
 * neutral state (never green, never "OK" or "compiles"); `unavailable` is
 * "Syntax not checked" and never shows messages; a base that is `unknown` or
 * not checked is "Base not checked" and never presented as new. Every state
 * carries an icon and a text, not a colour alone.
 */

type Translate = (key: string, args?: (string | number)[]) => string;

/** A status as an `ObjectStatus` shows it. */
export interface StatusInfo {
    text: string;
    /** A `sap.ui.core.ValueState`. */
    state: "None" | "Information" | "Warning" | "Error" | "Success";
    icon: string;
}

/**
 * The per-object diff caps of the changes view. Several diffs render on one
 * page: up to `maxLines` lines (both sides) an object shows its whole diff
 * (long unchanged runs folded); up to `hunksMaxLines` only its hunks (the
 * changes with their context, nothing else in the page); beyond that, or
 * beyond `maxEditLength` edits (jsdiff gives up), it is not compared at all
 * and an approve asks first (lead decision, final review M2).
 */
export const CHANGES_LIMITS = { maxLines: 5000, maxEditLength: 2000, hunksMaxLines: 40000 };

/** How a card shows its object: the whole diff, its hunks only, or not compared. */
export type CompareMode = "full" | "hunks" | "none";

/** The diff rows of a card and how they are shown ({@link CHANGES_LIMITS}); rows are `null` when not compared. */
export function compareObject(
    origin: string | null | undefined, proposed: string | null | undefined
): { rows: UnifiedRow[] | null; mode: CompareMode } {
    const rows = unifiedRows(origin, proposed, { maxLines: CHANGES_LIMITS.hunksMaxLines, maxEditLength: CHANGES_LIMITS.maxEditLength });
    if (!rows) {
        return { rows: null, mode: "none" };
    }
    // Lines of both sides: every row is one line of one side, an unchanged row one of each.
    const total = rows.reduce((n, r) => n + (r.oldNo !== null ? 1 : 0) + (r.newNo !== null ? 1 : 0), 0);
    return { rows, mode: total > CHANGES_LIMITS.maxLines ? "hunks" : "full" };
}

/**
 * Whether every line `start..end` of the revision is on screen: always in
 * the full diff; in hunks mode only lines inside a hunk are, so a selection
 * never covers lines the developer did not see.
 */
export function rangeShown(rows: readonly UnifiedRow[] | null, mode: CompareMode, start: number, end: number): boolean {
    if (mode === "full") {
        return true;
    }
    if (!rows || mode === "none") {
        return false;
    }
    const shown = new Set<number>();
    hunkRowsOf(rows).forEach((r) => {
        if (r.newNo !== null) {
            shown.add(r.newNo);
        }
    });
    for (let line = start; line <= end; line++) {
        if (!shown.has(line)) {
            return false;
        }
    }
    return true;
}

function hunkRowsOf(rows: readonly UnifiedRow[]): UnifiedRow[] {
    return hunksOf(rows.slice()).flatMap((h) => h.rows);
}

/** The loaded cards whose diff could not be computed: an approve covers them only after an explicit confirmation. */
export function notCompared<T extends { rows: readonly unknown[] | null; failed?: boolean }>(cards: readonly T[]): T[] {
    return cards.filter((c) => !c.failed && c.rows === null);
}

/** Where the proposal stands against SAP. */
export function baseInfo(
    file: { object_type: string | null; base_status: BaseStatus | null },
    originVersion: string | null | undefined,
    t: Translate
): StatusInfo & { isNew: boolean } {
    if (!file.object_type) {
        return { text: t("changesBaseNote"), state: "None", icon: "sap-icon://notes", isNew: false };
    }
    if (file.base_status === "absent") {
        return { text: t("changesBaseAbsent"), state: "Information", icon: "sap-icon://add-document", isNew: true };
    }
    if (file.base_status === "sap") {
        const version = typeof originVersion === "string" && originVersion.trim() ? originVersion.trim() : "";
        return {
            text: version ? t("changesBaseSapVersion", [version]) : t("changesBaseSapUnknown"),
            state: "None", icon: "sap-icon://edit", isNew: false
        };
    }
    // `unknown` and never checked: the base could not be read. Not "new": that would claim SAP has no such object.
    return { text: t("changesBaseUnknown"), state: "Warning", icon: "sap-icon://question-mark", isNew: false };
}

function severityKey(item: SyntaxItem): string {
    return item.severity === "error" ? "changesSeverityError" : "changesSeverityWarning";
}

/** The revision's syntax state and its messages (sorted by line, unplaced last), as text. */
export function syntaxInfo(
    status: SyntaxStatus | null | undefined, items: readonly SyntaxItem[] | null | undefined, t: Translate
): StatusInfo & { messages: string[] } {
    if (status === "unavailable") {
        return { text: t("changesSyntaxUnavailable"), state: "Warning", icon: "sap-icon://question-mark", messages: [] };
    }
    if (status !== "ok" && status !== "errors") {
        return { text: t("changesSyntaxPending"), state: "None", icon: "sap-icon://pending", messages: [] };
    }
    const list = (items ?? []).filter((i) => i && typeof i.message === "string");
    const sorted = list.slice().sort((a, b) => {
        const la = typeof a.line === "number" ? a.line : Number.MAX_SAFE_INTEGER;
        const lb = typeof b.line === "number" ? b.line : Number.MAX_SAFE_INTEGER;
        return la - lb;
    });
    const messages = sorted.map((i) => (typeof i.line === "number"
        ? t("changesSyntaxLine", [t(severityKey(i)), i.line, i.message])
        : t("changesSyntaxNoLine", [t(severityKey(i)), i.message])));
    const errors = list.filter((i) => i.severity === "error").length;
    const warnings = list.length - errors;
    if (status === "errors" || errors > 0) {
        const n = Math.max(errors, 1);
        return {
            text: n === 1 ? t("changesSyntaxErrorsOne") : t("changesSyntaxErrors", [n]),
            state: "Error", icon: "sap-icon://error", messages
        };
    }
    if (warnings > 0) {
        return {
            text: warnings === 1 ? t("changesSyntaxWarningsOne") : t("changesSyntaxWarnings", [warnings]),
            state: "Warning", icon: "sap-icon://alert", messages
        };
    }
    return { text: t("changesSyntaxOk"), state: "None", icon: "sap-icon://message-information", messages };
}

/**
 * The syntax messages a diff marks: only those of a finished check (`ok`,
 * `errors`). A pending or unknown check marks nothing, and `unavailable`
 * vouches for nothing.
 */
export function diffSyntax<T extends SyntaxItem>(status: SyntaxStatus | null | undefined, items: readonly T[] | null | undefined): T[] {
    return status === "ok" || status === "errors" ? (items ?? []).slice() : [];
}

/**
 * SAPLint state. Findings are stored per file only after `POST file/lint`,
 * so an empty list is ambiguous: "Lint not run" until it ran on this page.
 * Lint runs on the latest revision only: on an older one the status says so
 * (neutral) instead of showing findings of another revision.
 */
export function lintInfo(
    lint: readonly LintFinding[] | null | undefined, lintedHere: boolean, t: Translate, olderRevision = false
): StatusInfo {
    if (olderRevision) {
        return { text: t("changesLintLatestOnly"), state: "None", icon: "sap-icon://history" };
    }
    const list = lint ?? [];
    if (!list.length) {
        return lintedHere
            ? { text: t("changesLintNone"), state: "None", icon: "sap-icon://message-information" }
            : { text: t("changesLintNotRun"), state: "None", icon: "sap-icon://pending" };
    }
    const error = list.some((f) => String(f.severity ?? "").toLowerCase().startsWith("e"));
    return {
        text: list.length === 1 ? t("changesLintOne") : t("changesLintMany", [list.length]),
        state: error ? "Error" : "Warning",
        icon: error ? "sap-icon://error" : "sap-icon://alert"
    };
}

/** Lines added and removed. */
export function changeCounts(rows: readonly UnifiedRow[] | null): { added: number; removed: number } {
    let added = 0;
    let removed = 0;
    (rows ?? []).forEach((r) => {
        if (r.kind === "add") {
            added++;
        } else if (r.kind === "del") {
            removed++;
        }
    });
    return { added, removed };
}

/**
 * The SAP (old-side) line of the first change, for "Open in ADT": ADT opens
 * the object as it is in SAP. An addition opens at the line after the last
 * unchanged one before it; `null` when nothing changed.
 */
export function baseLineOfFirstChange(rows: readonly UnifiedRow[] | null): number | null {
    let lastOld = 0;
    for (const r of rows ?? []) {
        if (r.kind === "del") {
            return r.oldNo;
        }
        if (r.kind === "add") {
            return lastOld + 1;
        }
        lastOld = r.oldNo ?? lastOld;
    }
    return null;
}

/**
 * The workspace files that carry a proposal (state `modified` or `new`,
 * {@link isProposal}), in the server's order. A proposal dropped because it
 * equals SAP again is `read` and keeps its revisions: it is not listed.
 * `revision > 0` mirrors the server's `rev >= 1` (`stages._proposed_revisions`):
 * revisions are whole numbers from 1, 0 means no proposal written yet.
 */
export function proposedObjects(files: readonly FileSummary[] | null | undefined): FileSummary[] {
    return (files ?? []).filter((f) => isProposal(f) && (f.revision ?? 0) > 0);
}

/**
 * What a propose approve pins (`revisions`): every proposed ABAP object
 * card and the revision it shows, so the approve covers what the developer
 * saw. Notes and anything without a revision are left out, as on the server.
 */
export function approveRevisions(cards: readonly { summary: FileSummary; revision: number }[]): Record<string, number> {
    const out: Record<string, number> = {};
    cards.forEach((c) => {
        if (isProposedObject(c.summary) && Number.isInteger(c.revision) && c.revision >= 1) {
            out[c.summary.path] = c.revision;
        }
    });
    return out;
}

/**
 * Runs `fn` over `items` with at most `limit` calls in flight; the results
 * come back in input order, settled (a rejection does not stop the rest).
 */
export async function settleLimited<T, R>(
    items: readonly T[], limit: number, fn: (item: T, index: number) => Promise<R>
): Promise<PromiseSettledResult<R>[]> {
    const results: PromiseSettledResult<R>[] = new Array(items.length);
    let next = 0;
    const worker = async (): Promise<void> => {
        while (next < items.length) {
            const i = next++;
            try {
                results[i] = { status: "fulfilled", value: await fn(items[i], i) };
            } catch (reason) {
                results[i] = { status: "rejected", reason };
            }
        }
    };
    await Promise.all(Array.from({ length: Math.min(Math.max(1, limit), items.length) }, worker));
    return results;
}

/** The revision picker's items, newest first; the latest is marked as such (text, not colour). */
export function revisionItems(
    revisions: readonly FileRevision[] | null | undefined, latest: number, t: Translate
): { key: string; text: string }[] {
    const numbers = (revisions ?? []).map((r) => r.revision);
    if (!numbers.includes(latest) && latest > 0) {
        numbers.push(latest);
    }
    return Array.from(new Set(numbers)).sort((a, b) => b - a).map((n) => ({
        key: String(n),
        text: n === latest ? t("changesRevisionLatest", [n]) : t("changesRevision", [n])
    }));
}

/** Comments on `path` at `revision`, in input (oldest-first) order. */
export function fileComments(list: readonly Comment[], path: string, revision: number): Comment[] {
    return list.filter((c) => c.anchor === "file" && c.path === path && c.revision === revision);
}

/** Comments on other revisions of `path` that still matter (not dismissed). */
export function otherRevisionComments(list: readonly Comment[], path: string, revision: number): Comment[] {
    return list.filter((c) => c.anchor === "file" && c.path === path && c.revision !== revision && c.state !== "dismissed");
}

/** Comments by the line their marker sits on (`line_start`), lines ascending. */
export function lineMarkers(list: readonly Comment[]): Map<number, Comment[]> {
    const map = new Map<number, Comment[]>();
    list.slice().filter((c) => typeof c.line_start === "number")
        .sort((a, b) => (a.line_start as number) - (b.line_start as number))
        .forEach((c) => {
            const line = c.line_start as number;
            map.set(line, [...(map.get(line) ?? []), c]);
        });
    return map;
}

/** Comments whose lines overlap `start..end`. */
export function commentsInRange(list: readonly Comment[], start: number, end: number): Comment[] {
    return list.filter((c) => {
        const from = c.line_start ?? 0;
        const to = c.line_end ?? from;
        return from <= end && to >= start;
    });
}

/** A range from two lines in either order. */
export function selectionRange(a: number, b: number): [number, number] {
    return a <= b ? [a, b] : [b, a];
}

/**
 * The next change stop from `cursor` (-1 = none yet) in direction `dir`;
 * no wrap. "Previous" from nowhere is the last stop; -1 when there is none.
 */
export function stepStop(count: number, cursor: number, dir: 1 | -1): number {
    if (count <= 0) {
        return -1;
    }
    if (cursor < 0) {
        return dir > 0 ? 0 : count - 1;
    }
    return Math.min(Math.max(cursor + dir, 0), count - 1);
}
