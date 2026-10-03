import { ValueState } from "sap/ui/core/library";
import { renderDiffHtml, sideBySide, type DiffLabels } from "./diffModel";
import type { FileDetail, LintFinding } from "../service/types";

export type EditorMode = "source" | "proposed" | "diff";

/** One version entry of a document tab's version select. */
export interface DocVersion { id: string; label: string }

/**
 * One editor tab as the `ide>/tabs` model holds it. A file tab and a
 * document tab share the shape, so one TabContainerItem template serves both.
 */
export interface EditorTab {
    key: string;
    kind: "file" | "doc";
    title: string;
    subtitle: string;
    busy: boolean;
    // file tab
    path: string;
    mode: EditorMode;
    origin: string;
    proposed: string;
    hasProposal: boolean;
    /** An abapGit object path: Lint and Refresh apply. A scratch file is not. */
    isObject: boolean;
    editorType: "abap" | "text";
    /** What the code editor shows in the current mode. */
    text: string;
    /** The side-by-side diff, rendered only in diff mode. */
    diffHtml: string;
    lint: LintFinding[];
    lintSummary: string;
    lintState: ValueState;
    /** The source line (one-based) of the finding this tab was opened for; `null` when none. */
    findingLine: number | null;
    /** The line the editor highlights right now (`findingLine` clamped, Source mode only). */
    markedLine: number | null;
    /** Shown above the source when the finding's line cannot be mapped to it. */
    findingHint: string;
    // document tab
    docKind: string;
    versions: DocVersion[];
    artifactId: string;
    html: string;
}

export function fileKey(path: string): string {
    return `file:${path}`;
}

export function docKey(kind: string): string {
    return `doc:${kind}`;
}

const BLANK: EditorTab = {
    key: "", kind: "file", title: "", subtitle: "", busy: false,
    path: "", mode: "source", origin: "", proposed: "", hasProposal: false, isObject: false,
    editorType: "text", text: "", diffHtml: "", lint: [], lintSummary: "", lintState: ValueState.None,
    findingLine: null, markedLine: null, findingHint: "",
    docKind: "", versions: [], artifactId: "", html: ""
};

/** A new, still loading file tab. */
export function newFileTab(path: string): EditorTab {
    return {
        ...BLANK,
        key: fileKey(path),
        kind: "file",
        title: path.split("/").pop() || path,
        subtitle: path,
        busy: true,
        path,
        isObject: path.startsWith("src/"),
        editorType: path.endsWith(".abap") ? "abap" : "text"
    };
}

/** A new, still loading document tab. */
export function newDocTab(kind: string, title: string, versions: DocVersion[]): EditorTab {
    return { ...BLANK, key: docKey(kind), kind: "doc", title, busy: true, docKind: kind, versions };
}

/**
 * The tab with a (re-)read file applied. `keepMode` keeps the user's choice
 * on a refresh; a first load opens a proposal on Proposed and a read-only
 * file on Source.
 */
export function withFile(tab: EditorTab, file: FileDetail, keepMode = false): EditorTab {
    const proposed = file.proposed_source ?? "";
    const hasProposal = proposed.length > 0;
    let mode: EditorMode = keepMode ? tab.mode : hasProposal ? "proposed" : "source";
    if (!hasProposal && mode !== "source") {
        mode = "source";
    }
    return { ...tab, origin: file.origin_source ?? "", proposed, hasProposal, mode, lint: file.lint ?? [] };
}

/**
 * Recomputes what the tab shows for its mode. Diff mode needs jsdiff loaded
 * (`ensureDiff()`); the other modes do not touch it.
 */
export function withView(tab: EditorTab, labels: DiffLabels): EditorTab {
    if (tab.mode === "diff") {
        return { ...tab, text: "", diffHtml: renderDiffHtml(sideBySide(tab.origin, tab.proposed), labels) };
    }
    return { ...tab, text: tab.mode === "proposed" ? tab.proposed : tab.origin, diffHtml: "" };
}

/**
 * Whether the editor shows the text SAPLint ran on: the server lints the
 * proposal when there is one, otherwise the source.
 */
export function showsLintedText(tab: EditorTab): boolean {
    return tab.kind === "file" && (tab.mode === "proposed" || (tab.mode === "source" && !tab.hasProposal));
}
