import { escapeHtml } from "./diffModel";
import type { LintFinding } from "../service/types";

/**
 * A read-only source viewer rendered as plain HTML for a `core:HTML` host,
 * the read-only display (it replaced an editor control): no editor private API, one
 * table row per line so a line can be highlighted, selected and commented by
 * its `data-line`.
 */

/** Texts of the viewer (i18n from the caller); English fallbacks keep the markup accessible. */
export interface SourceLabels {
    /** Accessible name of the scrollable region. */
    region?: string;
    /** Visually hidden column headers. */
    lineHeader?: string;
    lintHeader?: string;
    codeHeader?: string;
    /** Severity words read out before a finding's message. */
    error?: string;
    warning?: string;
    info?: string;
    /** Shown under the rendered head of a file beyond the size cap. */
    tooLarge?: (shown: number, total: number) => string;
}

export interface SourceOptions {
    /** 1-based line marked as the current one (`aria-current="true"`). */
    highlightLine?: number | null;
    /** 1-based inclusive line range marked as selected, in either order. */
    selected?: [number, number] | null;
    /** SAPLint findings (1-based `line`) shown in a gutter column. */
    lint?: LintFinding[] | null;
    labels?: SourceLabels;
    /** Lines rendered at most; the rest is cut with a notice. */
    maxLines?: number;
}

/** Bounds on the DOM one source view builds. */
export const SOURCE_LIMITS = { maxLines: 20000 };

type Severity = "error" | "warning" | "info";

const SOURCE_DEFAULTS: Required<SourceLabels> = {
    region: "Source",
    lineHeader: "Line",
    lintHeader: "Findings",
    codeHeader: "Source",
    error: "error",
    warning: "warning",
    info: "information",
    tooLarge: (shown, total) => `Showing the first ${shown} of ${total} lines`
};

const SEVERITY_RANK: Record<Severity, number> = { error: 3, warning: 2, info: 1 };
const SEVERITY_GLYPH: Record<Severity, string> = { error: "!", warning: "⚠", info: "i" };
const SEVERITY_CLASS: Record<Severity, string> = {
    error: "ideSourceLintError",
    warning: "ideSourceLintWarning",
    info: "ideSourceLintInfo"
};

function severityOf(value: string): Severity {
    const s = (value || "").toLowerCase();
    if (s.startsWith("e")) {
        return "error";
    }
    if (s.startsWith("w")) {
        return "warning";
    }
    return "info";
}

/** The text as lines: LF line ends, a final newline does not add an empty line. */
function splitLines(text: string | null | undefined): string[] {
    const lf = (text ?? "").replace(/\r\n?/g, "\n");
    if (!lf) {
        return [];
    }
    const out = lf.split("\n");
    if (out[out.length - 1] === "") {
        out.pop();
    }
    return out;
}

function mergeLabels(labels: SourceLabels | undefined): Required<SourceLabels> {
    const merged: Required<SourceLabels> = { ...SOURCE_DEFAULTS };
    for (const [key, value] of Object.entries(labels ?? {})) {
        if (value !== undefined && value !== "") {
            (merged as Record<string, unknown>)[key] = value;
        }
    }
    return merged;
}

function lintByLine(lint: LintFinding[] | null | undefined): Map<number, LintFinding[]> {
    const byLine = new Map<number, LintFinding[]>();
    for (const f of lint ?? []) {
        const line = Math.floor(Number(f.line));
        if (!Number.isFinite(line) || line < 1) {
            continue;
        }
        const list = byLine.get(line) ?? [];
        list.push(f);
        byLine.set(line, list);
    }
    return byLine;
}

function lintCell(findings: LintFinding[] | undefined, labels: Required<SourceLabels>): { cell: string; cls: string } {
    if (!findings?.length) {
        return { cell: "<td class=\"ideSourceLint\"></td>", cls: "" };
    }
    let worst: Severity = "info";
    const texts = findings.map((f) => {
        const sev = severityOf(f.severity);
        if (SEVERITY_RANK[sev] > SEVERITY_RANK[worst]) {
            worst = sev;
        }
        return `${labels[sev]}: ${f.message}${f.rule ? ` (${f.rule})` : ""}`;
    });
    const cell = "<td class=\"ideSourceLint\">"
        + `<span aria-hidden="true">${SEVERITY_GLYPH[worst]}</span>`
        + `<span class="sapUiInvisibleText">${escapeHtml(texts.join("; "))}</span>`
        + "</td>";
    return { cell, cls: SEVERITY_CLASS[worst] };
}

/**
 * The source as one focusable, scrollable region root
 * (`<div class="ideSource" tabindex="0" role="region" aria-label=…>`) holding
 * a table of line-number, lint and code cells. Each row has `data-line`; the
 * highlight line has `aria-current="true"`; a selected range has
 * `ideSourceSelected`. A lint finding shows a severity glyph plus visually
 * hidden text, never colour alone. Every piece of text is escaped: source
 * and lint messages are untrusted. A file beyond `maxLines`
 * (SOURCE_LIMITS by default) renders its head and a notice saying so.
 */
export function renderSource(text: string | null | undefined, options: SourceOptions = {}): string {
    const labels = mergeLabels(options.labels);
    const all = splitLines(text);
    const cap = Math.max(1, Math.floor(options.maxLines ?? SOURCE_LIMITS.maxLines));
    const shown = all.length > cap ? all.slice(0, cap) : all;
    const lint = lintByLine(options.lint);
    const current = options.highlightLine ?? null;
    let from = 0;
    let to = -1;
    if (options.selected) {
        from = Math.min(options.selected[0], options.selected[1]);
        to = Math.max(options.selected[0], options.selected[1]);
    }
    const body = shown.map((line, i) => {
        const no = i + 1;
        const { cell, cls } = lintCell(lint.get(no), labels);
        const classes = ["ideSourceLine"];
        if (cls) {
            classes.push(cls);
        }
        const selected = no >= from && no <= to;
        if (selected) {
            classes.push("ideSourceSelected");
        }
        const isCurrent = no === current;
        if (isCurrent) {
            classes.push("ideSourceHighlight");
        }
        return `<tr class="${classes.join(" ")}" data-line="${no}"${isCurrent ? " aria-current=\"true\"" : ""}`
            + `${selected ? " aria-selected=\"true\"" : ""}>`
            + `<td class="ideSourceNo">${no}</td>`
            + cell
            + `<td class="ideSourceCode">${escapeHtml(line)}</td>`
            + "</tr>";
    }).join("");
    const th = (t: string): string => `<th scope="col"><span class="sapUiInvisibleText">${escapeHtml(t)}</span></th>`;
    const notice = shown.length < all.length
        ? `<p class="ideSourceTooLarge" role="note">${escapeHtml(labels.tooLarge(shown.length, all.length))}</p>`
        : "";
    return `<div class="ideSource" tabindex="0" role="region" aria-label="${escapeHtml(labels.region)}">`
        + "<table class=\"ideSourceTable\"><colgroup><col class=\"ideSourceNoCol\"><col class=\"ideSourceLintCol\"><col></colgroup>"
        + `<thead class="ideSourceHead"><tr>${th(labels.lineHeader)}${th(labels.lintHeader)}${th(labels.codeHeader)}</tr></thead>`
        + `<tbody>${body}</tbody></table>`
        + notice
        + "</div>";
}

/**
 * The line to scroll to: `line` clamped into 1..`lineCount` (whole lines;
 * not a number counts as 1); 0 when there are no lines.
 */
export function scrollTargetLine(line: number, lineCount: number): number {
    const count = Math.max(0, Math.floor(Number(lineCount) || 0));
    if (count === 0) {
        return 0;
    }
    const n = Math.floor(Number(line));
    if (!Number.isFinite(n) || n < 1) {
        return 1;
    }
    return Math.min(n, count);
}
