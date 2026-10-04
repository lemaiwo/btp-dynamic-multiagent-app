/**
 * What the changes view adds to a rendered stacked diff
 * (`diffModel.renderUnifiedHtml`) in the DOM, after rendering: the selected
 * lines, the comment markers and the keyboard stops. Applied to the live DOM
 * so a selection or a comment change never re-renders the diff's HTML (the
 * focus and the scroll position stay).
 *
 * Keyboard model: one tab stop per diff (roving tabindex). Every row and
 * every fold summary is focusable (`tabindex=-1`); exactly one of them is
 * `tabindex=0`. The region root leaves the tab order once it has rows.
 *
 * Nothing here builds markup from data: markers are elements whose text is
 * set with `textContent`, labels with `setAttribute`.
 */

export interface DiffMarker {
    /** The revision's line the marker sits on. */
    line: number;
    /** Visible text (the count). */
    text: string;
    /** Accessible name. */
    label: string;
    /** The comments in a few words ("1 comment (Open)"), for the line's own name. */
    comments?: string;
}

/** What a line's accessible name is built from. */
export interface DiffRowInfo {
    /** `old` for a removed line (numbered in the SAP version), else `new`. */
    side: "new" | "old";
    line: number;
    kind: "add" | "del" | "same";
    /** The line's code, trimmed and cut to {@link ROW_CODE_MAX} code points (an ellipsis marks a cut). */
    code: string;
    marker?: DiffMarker;
}

export interface DiffDecoration {
    /** Lines of the revision (new side) selected, inclusive; `null` for none. */
    selected: [number, number] | null;
    markers: DiffMarker[];
    /** Each row's accessible name, set again on every decoration (comments change it). */
    rowLabel?: (row: DiffRowInfo) => string;
    /** The id of a short per-line hint, added to each row's `aria-describedby` (once). */
    rowHint?: string;
}

const SELECTED = "ideDiffSelected";
const MARK = "ideDiffCommentMark";
/** The fold's reasons to be open at its last decoration (`s12 x3 m3`): only a new one reopens it. */
const REVEALED = "data-revealed";

/** At most this many code points of a line's code go into its accessible name. */
export const ROW_CODE_MAX = 80;

/** `code` cut to ROW_CODE_MAX code points (a surrogate pair stays whole), with an ellipsis when cut. */
function nameCode(code: string): string {
    const points = Array.from(code);
    return points.length > ROW_CODE_MAX ? `${points.slice(0, ROW_CODE_MAX).join("")}\u2026` : code;
}

/** The rows (`tr[data-line]`) and fold summaries a keyboard can reach: none inside a closed fold. */
export function navigable(root: HTMLElement): HTMLElement[] {
    return Array.from(root.querySelectorAll<HTMLElement>("tr[data-line], summary"))
        .filter((el) => {
            const details = el.tagName === "SUMMARY" ? el.parentElement?.parentElement?.closest("details") : el.closest("details");
            return !details || details.hasAttribute("open");
        });
}

/** Everything that can hold the roving stop, reachable or folded. */
function stops(root: HTMLElement): HTMLElement[] {
    return Array.from(root.querySelectorAll<HTMLElement>("tr[data-line], summary"));
}

/** Makes `el` the diff's one tab stop. */
export function setStop(root: HTMLElement, el: HTMLElement): void {
    stops(root).forEach((s) => s.setAttribute("tabindex", s === el ? "0" : "-1"));
    if (root !== el) {
        root.setAttribute("tabindex", "-1");
    }
}

/** The reachable element `delta` stops away from `from` (clamped; ±Infinity = first / last). */
export function step(root: HTMLElement, from: HTMLElement, delta: number): HTMLElement {
    const list = navigable(root);
    if (!list.length) {
        return from;
    }
    if (delta === Infinity) {
        return list[list.length - 1];
    }
    if (delta === -Infinity) {
        return list[0];
    }
    const at = list.indexOf(from);
    const next = at < 0 ? 0 : Math.min(Math.max(at + delta, 0), list.length - 1);
    return list[next];
}

/** Opens the fold (`<details>`) that holds `row`, so it shows and the keyboard reaches it. */
export function revealRow(row: HTMLElement | null): void {
    const fold = row?.closest("details");
    if (fold && !fold.open) {
        fold.open = true;
    }
}

/** The diff row that holds `node`, if any. */
export function rowFor(node: Node | null): HTMLElement | null {
    const el = node && node.nodeType === 1 ? node as HTMLElement : node?.parentElement ?? null;
    return el?.closest<HTMLElement>("tr[data-line]") ?? null;
}

/** The revision's line number of a row; `null` for a removed (old-side) row. */
export function newLineOf(row: HTMLElement | null): number | null {
    if (!row || row.getAttribute("data-side") !== "new") {
        return null;
    }
    const n = Number(row.getAttribute("data-line"));
    return Number.isInteger(n) && n > 0 ? n : null;
}

/** The row of the revision's `line`. */
export function rowOfLine(root: HTMLElement, line: number): HTMLElement | null {
    return root.querySelector<HTMLElement>(`tr[data-side='new'][data-line='${Math.floor(line)}']`);
}

/**
 * Applies a decoration, replacing the previous one; keeps the roving stop where it is when it can.
 * A fold opens for what lands in it (a selected line, a syntax mark, a comment marker); one the
 * user closed stays closed until something new lands in it.
 */
export function decorateDiff(root: HTMLElement, decoration: DiffDecoration): void {
    root.querySelectorAll(`.${MARK}`).forEach((m) => m.remove());
    root.querySelectorAll<HTMLElement>(`tr.${SELECTED}, tr[aria-selected]`).forEach((r) => {
        r.classList.remove(SELECTED);
        r.removeAttribute("aria-selected");
    });
    const reasons = new Map<HTMLDetailsElement, Set<string>>();
    const reveal = (row: HTMLElement | null, reason: string): void => {
        const fold = row?.closest("details");
        if (fold) {
            (reasons.get(fold) ?? reasons.set(fold, new Set()).get(fold)!).add(reason);
        }
    };
    const [from, to] = decoration.selected ?? [0, -1];
    root.querySelectorAll<HTMLElement>("tr[data-side='new'][data-line]").forEach((row) => {
        const line = Number(row.getAttribute("data-line"));
        // A selectable row of the (multiselectable) grid says whether it is selected.
        row.setAttribute("aria-selected", line >= from && line <= to ? "true" : "false");
        if (line >= from && line <= to) {
            row.classList.add(SELECTED);
            reveal(row, `s${line}`);
        }
    });
    // A folded line that carries something to read is shown: a syntax mark here, a marker below.
    root.querySelectorAll<HTMLElement>("tr.ideDiffSyntaxError, tr.ideDiffSyntaxWarning")
        .forEach((row) => reveal(row, `x${row.getAttribute("data-side")}${row.getAttribute("data-line")}`));
    decoration.markers.forEach((m) => {
        const row = rowOfLine(root, m.line);
        const cell = row?.querySelector("td.ideDiffMark");
        if (!cell) {
            return;
        }
        reveal(row, `m${m.line}`);
        const button = document.createElement("button");
        button.type = "button";
        button.className = MARK;
        button.tabIndex = -1;
        button.setAttribute("data-line", String(m.line));
        button.setAttribute("aria-label", m.label);
        button.title = m.label;
        button.textContent = m.text;
        cell.insertBefore(button, cell.firstChild);
    });
    root.querySelectorAll<HTMLDetailsElement>("details").forEach((fold) => {
        const now = reasons.get(fold) ?? new Set<string>();
        const before = new Set((fold.getAttribute(REVEALED) ?? "").split(" ").filter(Boolean));
        if (Array.from(now).some((r) => !before.has(r))) {
            fold.open = true;
        }
        fold.setAttribute(REVEALED, Array.from(now).join(" "));
    });
    if (decoration.rowLabel || decoration.rowHint) {
        nameRows(root, decoration);
    }
    const all = stops(root);
    if (!all.length) {
        return;
    }
    const current = all.find((s) => s.getAttribute("tabindex") === "0");
    const reachable = navigable(root);
    setStop(root, current && reachable.includes(current) ? current : reachable[0] ?? all[0]);
}

/** Every row's accessible name and short hint (next to a syntax description it may already have). */
function nameRows(root: HTMLElement, decoration: DiffDecoration): void {
    const markers = new Map(decoration.markers.map((m) => [m.line, m]));
    const hint = decoration.rowHint;
    root.querySelectorAll<HTMLElement>("tr[data-line]").forEach((row) => {
        const line = Number(row.getAttribute("data-line"));
        if (!Number.isInteger(line) || line <= 0) {
            return;
        }
        if (decoration.rowLabel) {
            const side = row.getAttribute("data-side") === "old" ? "old" : "new";
            const kind = row.classList.contains("ideDiffAdd") ? "add" : row.classList.contains("ideDiffDel") ? "del" : "same";
            const code = nameCode((row.querySelector("td.ideDiffCode")?.textContent ?? "").trim());
            row.setAttribute("aria-label", decoration.rowLabel({
                side, line, kind, code, marker: side === "new" ? markers.get(line) : undefined
            }));
            // The name says kind and number: the hidden "added/removed/unchanged line n" is not read again.
            row.querySelectorAll("td.ideDiffMark .sapUiInvisibleText:not([id])").forEach((span) => span.setAttribute("aria-hidden", "true"));
        }
        if (hint) {
            // Space and Enter select and comment lines of the revision only: a removed line does not get the hint.
            const ids = (row.getAttribute("aria-describedby") ?? "").split(" ").filter((id) => id && id !== hint);
            const own = row.getAttribute("data-side") === "old" ? ids : [...ids, hint];
            if (own.length) {
                row.setAttribute("aria-describedby", own.join(" "));
            } else {
                row.removeAttribute("aria-describedby");
            }
        }
    });
}
