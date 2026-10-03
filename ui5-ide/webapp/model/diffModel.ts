import type { DiffChange } from "./vendor";

export type DiffKind = "same" | "add" | "del" | "chg";

/** One row of a side-by-side diff; a missing side has `null` as its number and "" as its text. */
export interface DiffRow {
    leftNo: number | null;
    left: string;
    rightNo: number | null;
    right: string;
    kind: DiffKind;
}

export interface DiffLabels {
    left: string;
    right: string;
    /** Accessible name of the table (a visually hidden caption). */
    caption?: string;
    /** Visually hidden header of the +/−/~ marker column. */
    markHeader?: string;
    /** Visually hidden texts of the markers, so a row's state is not told by colour alone. */
    added?: string;
    removed?: string;
    changed?: string;
    /** Summary of a folded run of unchanged lines, given its length. */
    unchanged?: (count: number) => string;
    /** Shown instead of the table when the diff exceeds its limits. */
    tooLarge?: string;
}

/** Bounds on the work the UI thread does for one diff. */
export interface DiffLimits {
    /** Lines of both sides together; more is not diffed at all. */
    maxLines?: number;
    /** jsdiff's `maxEditLength`: it gives up beyond this many edits. */
    maxEditLength?: number;
}

export const DIFF_LIMITS: Required<DiffLimits> = { maxLines: 20000, maxEditLength: 2000 };

/** Unchanged lines kept around a change when a run is folded. */
export const DIFF_CONTEXT = 3;

/** A run of rows; a folded one renders collapsed behind a disclosure. */
export interface DiffBlock {
    folded: boolean;
    rows: DiffRow[];
}

/** The marker glyph per kind; same rows have none. */
const MARK: Record<DiffKind, string> = { same: "", add: "+", del: "\u2212", chg: "~" };

const KIND_CLASS: Record<DiffKind, string> = {
    same: "ideDiffSame",
    add: "ideDiffAdd",
    del: "ideDiffDel",
    chg: "ideDiffChg"
};

/** LF line ends, and a final newline so the last line compares like every other. */
function normalize(text: string | null | undefined): string {
    const lf = (text ?? "").replace(/\r\n?/g, "\n");
    return lf && !lf.endsWith("\n") ? lf + "\n" : lf;
}

function lines(value: string): string[] {
    const out = value.split("\n");
    if (out[out.length - 1] === "") {
        out.pop();
    }
    return out;
}

/**
 * A side-by-side diff of the origin (the diff base, as ARC-1 returned it)
 * and the proposed source, line by line, using the vendored jsdiff
 * (`ensureDiff()` must have run).
 *
 * A removed block directly next to an added block is a change: its lines
 * pair up as `chg` rows, and the longer block's remainder becomes `del` or
 * `add` rows.
 *
 * `null` means the inputs exceed `limits` (DIFF_LIMITS by default): jsdiff
 * is O(ND) on the UI thread, so a huge or wildly different file is not
 * diffed and the editor offers Source and Proposed instead.
 */
export function sideBySide(
    origin: string | null | undefined, proposed: string | null | undefined, limits: DiffLimits = {}
): DiffRow[] | null {
    const lib = window.Diff;
    if (!lib) {
        throw new Error("ensureDiff() must run before sideBySide()");
    }
    const { maxLines, maxEditLength } = { ...DIFF_LIMITS, ...limits };
    const left = normalize(origin);
    const right = normalize(proposed);
    if (lines(left).length + lines(right).length > maxLines) {
        return null;
    }
    // jsdiff answers undefined once the edit script would exceed maxEditLength.
    const changes: DiffChange[] | undefined = lib.diffLines(left, right, { maxEditLength });
    if (!changes) {
        return null;
    }
    const rows: DiffRow[] = [];
    let leftNo = 0;
    let rightNo = 0;

    const pushPair = (removed: string[], added: string[]): void => {
        const n = Math.max(removed.length, added.length);
        for (let i = 0; i < n; i++) {
            const hasLeft = i < removed.length;
            const hasRight = i < added.length;
            rows.push({
                leftNo: hasLeft ? ++leftNo : null,
                left: hasLeft ? removed[i] : "",
                rightNo: hasRight ? ++rightNo : null,
                right: hasRight ? added[i] : "",
                kind: hasLeft && hasRight ? "chg" : hasLeft ? "del" : "add"
            });
        }
    };

    for (let i = 0; i < changes.length; i++) {
        const change = changes[i];
        const next = changes[i + 1];
        if (change.removed || change.added) {
            let removed: string[] = [];
            let added: string[] = [];
            if (change.removed) {
                removed = lines(change.value);
            } else {
                added = lines(change.value);
            }
            // jsdiff emits a replacement as two neighbours; pair them in either order.
            if (next && ((change.removed && next.added) || (change.added && next.removed))) {
                if (next.added) {
                    added = lines(next.value);
                } else {
                    removed = lines(next.value);
                }
                i++;
            }
            pushPair(removed, added);
        } else {
            for (const line of lines(change.value)) {
                rows.push({ leftNo: ++leftNo, left: line, rightNo: ++rightNo, right: line, kind: "same" });
            }
        }
    }
    return rows;
}

/** Escapes text for an HTML text node or a quoted attribute value. */
export function escapeHtml(text: string): string {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/**
 * Splits rows into blocks, folding every run of unchanged rows that is
 * longer than the context kept around a change: DIFF_CONTEXT lines stay
 * visible next to each change, the rest of the run is folded. Runs of fewer
 * than four hidden lines are not worth a fold and stay as they are.
 */
export function foldUnchanged(rows: DiffRow[], context = DIFF_CONTEXT): DiffBlock[] {
    const blocks: DiffBlock[] = [];
    const visible = (part: DiffRow[]): void => {
        if (!part.length) {
            return;
        }
        const last = blocks[blocks.length - 1];
        if (last && !last.folded) {
            last.rows.push(...part);
        } else {
            blocks.push({ folded: false, rows: part.slice() });
        }
    };
    let i = 0;
    while (i < rows.length) {
        if (rows[i].kind !== "same") {
            visible([rows[i++]]);
            continue;
        }
        let end = i;
        while (end < rows.length && rows[end].kind === "same") {
            end++;
        }
        const run = rows.slice(i, end);
        const head = i === 0 ? 0 : context;
        const tail = end === rows.length ? 0 : context;
        if (run.length - head - tail >= 4) {
            visible(run.slice(0, head));
            blocks.push({ folded: true, rows: run.slice(head, run.length - tail) });
            visible(run.slice(run.length - tail));
        } else {
            visible(run);
        }
        i = end;
    }
    return blocks;
}

const COLGROUP = "<colgroup><col class=\"ideDiffMarkCol\"><col class=\"ideDiffNoCol\"><col>"
    + "<col class=\"ideDiffNoCol\"><col></colgroup>";

function rowHtml(r: DiffRow, labels: DiffLabels): string {
    const num = (n: number | null): string => (n === null ? "" : String(n));
    const hidden = { same: "", add: labels.added, del: labels.removed, chg: labels.changed }[r.kind] ?? "";
    const mark = r.kind === "same"
        ? ""
        : `<span aria-hidden="true">${MARK[r.kind]}</span>`
            + (hidden ? `<span class="sapUiInvisibleText">${escapeHtml(hidden)}</span>` : "");
    return `<tr class="${KIND_CLASS[r.kind]}">`
        + `<td class="ideDiffMark">${mark}</td>`
        + `<td class="ideDiffNo">${num(r.leftNo)}</td>`
        + `<td class="ideDiffCode ideDiffLeft">${escapeHtml(r.left)}</td>`
        + `<td class="ideDiffNo">${num(r.rightNo)}</td>`
        + `<td class="ideDiffCode ideDiffRight">${escapeHtml(r.right)}</td>`
        + "</tr>";
}

/**
 * The rows as one `<div>` holding a table, for a `core:HTML` host (which
 * needs a single root element). Every piece of text, source lines and the
 * labels alike, goes through escapeHtml: source is untrusted.
 *
 * Each changed row starts with a +/−/~ marker plus a visually hidden
 * text, so its state does not depend on colour (WCAG 1.4.1, high-contrast
 * themes). Long unchanged runs are folded into a native `<details>` that
 * holds their rows in a nested table with the same columns. `null` rows
 * (beyond the diff limits) render the `tooLarge` message instead.
 */
export function renderDiffHtml(rows: DiffRow[] | null, labels: DiffLabels = { left: "", right: "" }): string {
    if (!rows) {
        return `<div class="ideDiff"><p class="ideDiffTooLarge">${escapeHtml(labels.tooLarge ?? "")}</p></div>`;
    }
    const body = foldUnchanged(rows).map((block) => {
        const inner = block.rows.map((r) => rowHtml(r, labels)).join("");
        if (!block.folded) {
            return inner;
        }
        const summary = labels.unchanged ? labels.unchanged(block.rows.length) : String(block.rows.length);
        return "<tr class=\"ideDiffFold\"><td colspan=\"5\"><details>"
            + `<summary>\u2026 ${escapeHtml(summary)}</summary>`
            + `<table class="ideDiffTable ideDiffFoldTable">${COLGROUP}<tbody>${inner}</tbody></table>`
            + "</details></td></tr>";
    }).join("");
    const caption = labels.caption
        ? `<caption class="sapUiInvisibleText">${escapeHtml(labels.caption)}</caption>`
        : "";
    return "<div class=\"ideDiff\"><table class=\"ideDiffTable\">"
        + caption
        + COLGROUP
        + "<thead><tr>"
        + `<th scope="col"><span class="sapUiInvisibleText">${escapeHtml(labels.markHeader ?? "")}</span></th>`
        + `<th scope="col" colspan="2">${escapeHtml(labels.left)}</th>`
        + `<th scope="col" colspan="2">${escapeHtml(labels.right)}</th>`
        + "</tr></thead>"
        + `<tbody>${body}</tbody></table></div>`;
}
