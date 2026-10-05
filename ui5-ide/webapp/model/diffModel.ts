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
export interface DiffBlock<T extends { kind: string } = DiffRow> {
    folded: boolean;
    rows: T[];
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
    const changes = lineChanges(origin, proposed, limits, "sideBySide");
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

    walkChanges(changes, pushPair, (same) => {
        for (const line of same) {
            rows.push({ leftNo: ++leftNo, left: line, rightNo: ++rightNo, right: line, kind: "same" });
        }
    });
    return rows;
}

/**
 * jsdiff's line changes of the normalised texts, or `null` beyond `limits`
 * (too many lines, or more edits than `maxEditLength`).
 */
function lineChanges(
    origin: string | null | undefined, proposed: string | null | undefined, limits: DiffLimits, caller: string
): DiffChange[] | null {
    const lib = window.Diff;
    if (!lib) {
        throw new Error(`ensureDiff() must run before ${caller}()`);
    }
    const { maxLines, maxEditLength } = { ...DIFF_LIMITS, ...limits };
    const left = normalize(origin);
    const right = normalize(proposed);
    if (lines(left).length + lines(right).length > maxLines) {
        return null;
    }
    // jsdiff answers undefined once the edit script would exceed maxEditLength.
    return lib.diffLines(left, right, { maxEditLength }) ?? null;
}

/**
 * Walks jsdiff's changes as unchanged runs and replacements: a removed
 * block next to an added block (jsdiff emits them as two neighbours, in
 * either order) is one replacement.
 */
function walkChanges(
    changes: DiffChange[],
    onReplace: (removed: string[], added: string[]) => void,
    onSame: (same: string[]) => void
): void {
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
            if (next && ((change.removed && next.added) || (change.added && next.removed))) {
                if (next.added) {
                    added = lines(next.value);
                } else {
                    removed = lines(next.value);
                }
                i++;
            }
            onReplace(removed, added);
        } else {
            onSame(lines(change.value));
        }
    }
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
export function foldUnchanged<T extends { kind: string } = DiffRow>(rows: T[], context = DIFF_CONTEXT): DiffBlock<T>[] {
    const blocks: DiffBlock<T>[] = [];
    const visible = (part: T[]): void => {
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

// ---------------------------------------------------------------------------
// Stacked (unified) diff: one column of code, removed lines above added ones.
// ---------------------------------------------------------------------------

export type UnifiedKind = "same" | "add" | "del";

/** One row of a stacked diff; `oldNo` is null on an added row, `newNo` on a removed one. */
export interface UnifiedRow {
    kind: UnifiedKind;
    oldNo: number | null;
    newNo: number | null;
    text: string;
}

/** Texts of the stacked diff (i18n from the caller); English fallbacks keep the markup accessible. */
export interface UnifiedLabels {
    /** Accessible name of the scrollable region. */
    region?: string;
    /** Visually hidden column headers. */
    markHeader?: string;
    oldHeader?: string;
    newHeader?: string;
    codeHeader?: string;
    /** Hidden text of a marker, given the line number on its side ("added line 5"). */
    added?: (line: number) => string;
    removed?: (line: number) => string;
    /** Hidden text of an unchanged line, given its number in the revision ("unchanged line 5"). */
    sameLine?: (line: number) => string;
    /** Summary of a folded run of unchanged lines, given its length. */
    unchanged?: (count: number) => string;
    /** Shown instead of the table when the diff exceeds its limits. */
    tooLarge?: string;
    /** Description of a line with a syntax error / warning, given its message(s). */
    syntaxError?: (message: string) => string;
    syntaxWarning?: (message: string) => string;
    /** The header of a hunk in a hunks-only diff (its line ranges). */
    hunkHeader?: (hunk: Hunk) => string;
}

/** A syntax message placed on a line of the revision shown (the new side). */
export interface UnifiedSyntaxItem {
    line: number | null;
    message: string;
    severity: "error" | "warning";
}

export interface UnifiedOptions {
    /** The workspace path, echoed as `data-path` for comment anchors. */
    path: string;
    /** The revision shown on the new side, echoed as `data-revision`. */
    revision: number | null;
    labels?: UnifiedLabels;
    /** Syntax messages of the revision: their lines get a gutter glyph, a tooltip and a description. */
    syntax?: UnifiedSyntaxItem[] | null;
    /** Prefix of the ids the descriptions get (unique per page); generated when absent. */
    idPrefix?: string;
    /**
     * Only the hunks: each change with DIFF_CONTEXT lines around it under a
     * header with its line numbers; unchanged lines further away are left
     * out, not folded (an object too large to render whole).
     */
    hunksOnly?: boolean;
}

/**
 * A run of changed rows with up to DIFF_CONTEXT unchanged rows on each side
 * (changes whose context touches share one hunk). Starts and counts are per
 * side, as in a unified diff header; a side without lines has count 0 and
 * the start of the line before it.
 */
export interface Hunk {
    oldStart: number;
    oldCount: number;
    newStart: number;
    newCount: number;
    rows: UnifiedRow[];
}

let unifiedIds = 0;

const UNIFIED_DEFAULTS: Required<UnifiedLabels> = {
    region: "Changes",
    markHeader: "Change",
    oldHeader: "Old line",
    newHeader: "New line",
    codeHeader: "Source",
    added: (n) => `added line ${n}`,
    removed: (n) => `removed line ${n}`,
    sameLine: (n) => `unchanged line ${n}`,
    unchanged: (n) => `${n} unchanged lines`,
    tooLarge: "Too large to compare",
    syntaxError: (m) => `Syntax error: ${m}`,
    syntaxWarning: (m) => `Syntax warning: ${m}`,
    hunkHeader: (h) => `@@ -${h.oldStart},${h.oldCount} +${h.newStart},${h.newCount} @@`
};

/** The syntax messages of one line, joined, and whether one of them is an error. */
interface LineSyntax {
    message: string;
    error: boolean;
}

function syntaxByLine(items: UnifiedSyntaxItem[] | null | undefined): Map<number, LineSyntax> {
    const map = new Map<number, LineSyntax>();
    for (const item of items ?? []) {
        const line = typeof item?.line === "number" ? Math.floor(item.line) : NaN;
        if (!Number.isFinite(line) || line < 1) {
            continue;
        }
        const known = map.get(line);
        const message = String(item.message ?? "");
        map.set(line, {
            message: known ? `${known.message}; ${message}` : message,
            error: (known?.error ?? false) || item.severity === "error"
        });
    }
    return map;
}

/**
 * A stacked diff of `oldText` (the base; `null` for a new object) and
 * `newText`: unchanged rows carry both numbers, each replacement lists its
 * removed lines first and its added lines after. `null` beyond `limits`
 * (DIFF_LIMITS by default), like {@link sideBySide}.
 */
export function unifiedRows(
    oldText: string | null | undefined, newText: string | null | undefined, limits: DiffLimits = {}
): UnifiedRow[] | null {
    const changes = lineChanges(oldText, newText, limits, "unifiedRows");
    if (!changes) {
        return null;
    }
    const rows: UnifiedRow[] = [];
    let oldNo = 0;
    let newNo = 0;
    walkChanges(changes, (removed, added) => {
        for (const text of removed) {
            rows.push({ kind: "del", oldNo: ++oldNo, newNo: null, text });
        }
        for (const text of added) {
            rows.push({ kind: "add", oldNo: null, newNo: ++newNo, text });
        }
    }, (same) => {
        for (const text of same) {
            rows.push({ kind: "same", oldNo: ++oldNo, newNo: ++newNo, text });
        }
    });
    return rows;
}

/** The index of the first row of every hunk (a run of changed rows); `[]` when nothing changed. */
export function changeStops(rows: { kind: string }[]): number[] {
    const stops: number[] = [];
    rows.forEach((r, i) => {
        if (r.kind !== "same" && (i === 0 || rows[i - 1].kind === "same")) {
            stops.push(i);
        }
    });
    return stops;
}

/**
 * The hunks of a stacked diff: every changed row with `context` unchanged
 * rows before and after it; two changes at most 2 x `context` unchanged
 * rows apart are one hunk. The rows are the diff's own row objects.
 */
export function hunksOf(rows: UnifiedRow[], context = DIFF_CONTEXT): Hunk[] {
    const ranges: [number, number][] = [];
    rows.forEach((r, i) => {
        if (r.kind === "same") {
            return;
        }
        const from = Math.max(0, i - context);
        const to = Math.min(rows.length - 1, i + context);
        const last = ranges[ranges.length - 1];
        if (last && from <= last[1] + 1) {
            last[1] = Math.max(last[1], to);
        } else {
            ranges.push([from, to]);
        }
    });
    return ranges.map(([from, to]) => {
        const part = rows.slice(from, to + 1);
        const olds = part.filter((r) => r.oldNo !== null).map((r) => r.oldNo as number);
        const news = part.filter((r) => r.newNo !== null).map((r) => r.newNo as number);
        // A side with no line in the hunk starts after the last line before it (0 at the top).
        const before = (side: "oldNo" | "newNo"): number => {
            for (let i = from - 1; i >= 0; i--) {
                const n = rows[i][side];
                if (n !== null) {
                    return n;
                }
            }
            return 0;
        };
        return {
            oldStart: olds.length ? olds[0] : before("oldNo"),
            oldCount: olds.length,
            newStart: news.length ? news[0] : before("newNo"),
            newCount: news.length,
            rows: part
        };
    });
}

const UNIFIED_CLASS: Record<UnifiedKind, string> = { same: "ideDiffSame", add: "ideDiffAdd", del: "ideDiffDel" };

const UNIFIED_COLGROUP = "<colgroup><col class=\"ideDiffMarkCol\"><col class=\"ideDiffNoCol\">"
    + "<col class=\"ideDiffNoCol\"><col></colgroup>";

/** The rows with long unchanged runs folded behind a disclosure, each fold a nested grid named by its summary. */
function foldedHtml(rows: UnifiedRow[], labels: Required<UnifiedLabels>, rowsHtml: (list: UnifiedRow[]) => string): string {
    return foldUnchanged(rows).map((block) => {
        const inner = rowsHtml(block.rows);
        if (!block.folded) {
            return inner;
        }
        const unchanged = escapeHtml(labels.unchanged(block.rows.length));
        return "<tr role=\"row\" class=\"ideDiffFold\"><td role=\"gridcell\" colspan=\"4\"><details>"
            + `<summary>\u2026 ${unchanged}</summary>`
            + `<table class="ideDiffTable ideDiffFoldTable" role="grid" aria-multiselectable="true" aria-label="${unchanged}">`
            + `${UNIFIED_COLGROUP}<tbody>${inner}</tbody></table>`
            + "</details></td></tr>";
    }).join("");
}

function unifiedRowHtml(
    r: UnifiedRow, labels: Required<UnifiedLabels>, stop: number | undefined, syntax: Map<number, LineSyntax>, idPrefix: string
): string {
    const num = (n: number | null): string => (n === null ? "" : String(n));
    // A removed line only exists on the old side; everything else is a line of the revision shown.
    const side = r.kind === "del" ? "old" : "new";
    const line = r.kind === "del" ? r.oldNo : r.newNo;
    let mark = "";
    let code = escapeHtml(r.text);
    if (r.kind === "add") {
        mark = `<span aria-hidden="true">+</span><span class="sapUiInvisibleText">${escapeHtml(labels.added(r.newNo ?? 0))}</span>`;
        code = `<ins>${code}</ins>`;
    } else if (r.kind === "del") {
        mark = `<span aria-hidden="true">\u2212</span><span class="sapUiInvisibleText">${escapeHtml(labels.removed(r.oldNo ?? 0))}</span>`;
        code = `<del>${code}</del>`;
    } else {
        mark = `<span class="sapUiInvisibleText">${escapeHtml(labels.sameLine(r.newNo ?? 0))}</span>`;
    }
    // Syntax messages belong to the revision: only its lines (added or unchanged) carry them.
    const syn = side === "new" && line !== null ? syntax.get(line) : undefined;
    let classes = UNIFIED_CLASS[r.kind];
    let attrs = "";
    let description = "";
    if (syn) {
        const id = `${idPrefix}-syntax-${line}`;
        classes += syn.error ? " ideDiffSyntaxError" : " ideDiffSyntaxWarning";
        attrs = ` title="${escapeHtml(syn.message)}" aria-describedby="${escapeHtml(id)}"`;
        const text = syn.error ? labels.syntaxError(syn.message) : labels.syntaxWarning(syn.message);
        mark += `<span class="ideDiffSyntaxMark" aria-hidden="true">${syn.error ? "!" : "\u26a0"}</span>`;
        description = `<span id="${escapeHtml(id)}" class="sapUiInvisibleText" aria-hidden="true">${escapeHtml(text)}</span>`;
    }
    return `<tr role="row" class="${classes}" data-side="${side}" data-line="${num(line)}"`
        + (stop === undefined ? "" : ` data-stop="${stop}"`) + attrs + ">"
        + `<td role="gridcell" class="ideDiffMark">${mark}${description}</td>`
        + `<td role="gridcell" class="ideDiffNo ideUnifiedOld">${num(r.oldNo)}</td>`
        + `<td role="gridcell" class="ideDiffNo ideUnifiedNew">${num(r.newNo)}</td>`
        + `<td role="gridcell" class="ideDiffCode">${code}</td>`
        + "</tr>";
}

/**
 * The stacked diff as one focusable, scrollable region (`tabindex="0"`,
 * `role="region"`, `aria-label`) for a `core:HTML` host. Every row has
 * `data-side` (`old` for a removed line, else `new`) and `data-line` (the
 * line number on that side) for comment anchors; the first row of each hunk
 * has `data-stop` (0-based, matching {@link changeStops}). Removed and added
 * text are `<del>`/`<ins>` and each row starts with a −/+ marker plus
 * visually hidden "removed line n"/"added line n" (an unchanged row: only
 * the hidden "unchanged line n"), so the state never depends on colour.
 * Long unchanged runs fold into a native `<details>`; with `hunksOnly`
 * only the hunks ({@link hunksOf}) are rendered, each under a header row
 * (a grid row with one cell, no `data-line`: never a line stop), and
 * nothing else of the object is in the markup.
 * The table is a `grid` (`aria-multiselectable`, rows and gridcells), so
 * the selection (`aria-selected`) and the arrow-key roving that
 * `diffDecor` adds are valid; a fold's rows sit in a nested grid named by
 * the fold's summary, inside the fold row's one gridcell.
 * All text — source, path, labels — is escaped: source is untrusted.
 * `null` rows (beyond the diff limits) render the `tooLarge` text instead.
 */
export function renderUnifiedHtml(rows: UnifiedRow[] | null, options: UnifiedOptions): string {
    const labels: Required<UnifiedLabels> = { ...UNIFIED_DEFAULTS };
    for (const [key, value] of Object.entries(options.labels ?? {})) {
        if (value !== undefined && value !== "") {
            (labels as Record<string, unknown>)[key] = value;
        }
    }
    const open = `<div class="ideDiff ideUnified" tabindex="0" role="region" aria-label="${escapeHtml(labels.region)}"`
        + ` data-path="${escapeHtml(options.path ?? "")}"`
        + ` data-revision="${options.revision === null || options.revision === undefined ? "" : escapeHtml(String(options.revision))}">`;
    if (!rows) {
        return `${open}<p class="ideDiffTooLarge">${escapeHtml(labels.tooLarge)}</p></div>`;
    }
    const syntax = syntaxByLine(options.syntax);
    const idPrefix = options.idPrefix || `ideUnified${++unifiedIds}`;
    const stopOf = new Map<UnifiedRow, number>();
    changeStops(rows).forEach((index, n) => stopOf.set(rows[index], n));
    const rowsHtml = (list: UnifiedRow[]): string => list.map((r) => unifiedRowHtml(r, labels, stopOf.get(r), syntax, idPrefix)).join("");
    const body = options.hunksOnly
        ? hunksOf(rows).map((h) => "<tr role=\"row\" class=\"ideDiffHunk\"><td role=\"gridcell\" colspan=\"4\">"
            + `${escapeHtml(labels.hunkHeader(h))}</td></tr>${rowsHtml(h.rows)}`).join("")
        : foldedHtml(rows, labels, rowsHtml);
    const th = (text: string): string =>
        `<th role="columnheader" scope="col"><span class="sapUiInvisibleText">${escapeHtml(text)}</span></th>`;
    return open
        + `<table class="ideDiffTable" role="grid" aria-multiselectable="true" aria-label="${escapeHtml(labels.region)}">${UNIFIED_COLGROUP}`
        + `<thead class="ideUnifiedHead"><tr role="row">${th(labels.markHeader)}${th(labels.oldHeader)}${th(labels.newHeader)}${th(labels.codeHeader)}</tr></thead>`
        + `<tbody>${body}</tbody></table></div>`;
}
