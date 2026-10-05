import { allowedTransitions, isEditable, stateText } from "./comments";
import type { ArtifactKind, ArtifactSummary, Comment, CommentState } from "../service/types";

/**
 * The document view's pure parts (Task U9): the version switcher, the
 * "based on" link, the paragraph blocks of a rendered document and the rows
 * of its comments.
 *
 * Comment bodies and answers stay raw strings here: the view binds them to
 * `sap.m.Text` (model values are never parsed as binding syntax or HTML).
 * Only {@link splitBlocks} handles markup, and only markup that
 * `docView.renderDocument` produced (sanitised by `markdown.ts`).
 */

type Translate = (key: string, args?: (string | number)[]) => string;

const KIND_KEYS: Record<ArtifactKind, string> = {
    design: "docKindDesign",
    plan: "docKindPlan",
    note: "docKindNote",
    review: "docKindReview",
    report: "docKindReport"
};

function own<T>(map: Record<string, T>, key: unknown): T | undefined {
    return typeof key === "string" && Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

export function kindText(kind: ArtifactKind, t: Translate): string {
    const key = own(KIND_KEYS, kind);
    return key ? t(key) : String(kind);
}

export interface VersionItem {
    key: string;
    text: string;
    tooltip: string;
    /** The version the session pinned when the stage was approved. */
    approved: boolean;
}

/** The versions of `kind`, oldest first; the pinned one says "approved" in its text. */
export function versionItems(
    artifacts: readonly ArtifactSummary[], kind: ArtifactKind, pinned: number | undefined, t: Translate
): VersionItem[] {
    const versions = Array.from(new Set(artifacts.filter((a) => a.kind === kind).map((a) => a.version))).sort((a, b) => a - b);
    return versions.map((v) => {
        const approved = pinned === v;
        return {
            key: String(v),
            text: t(approved ? "docVersionApproved" : "docVersion", [v]),
            tooltip: t(approved ? "docVersionApprovedTooltip" : "docVersionTooltip", [kindText(kind, t), v]),
            approved
        };
    });
}

export interface BasedOn {
    text: string;
    kind: ArtifactKind;
    version: number;
}

/** "based on Design v2" from a document's `based_on`; null when it names no known document version. */
export function basedOnLink(summary: ArtifactSummary | undefined, t: Translate): BasedOn | null {
    const pins = summary?.based_on;
    if (!pins || typeof pins !== "object") {
        return null;
    }
    for (const [kind, version] of Object.entries(pins)) {
        if (own(KIND_KEYS, kind) && Number.isSafeInteger(version) && version > 0) {
            return { text: t("docBasedOn", [kindText(kind as ArtifactKind, t), version]), kind: kind as ArtifactKind, version };
        }
    }
    return null;
}

/**
 * The blocks of `docView.renderDocument(...).html`, one HTML string each
 * (its `div.ideDocBlock` with `data-para` and `tabindex`), in order, so each
 * paragraph can sit in its own row next to its comment marker. Parsed in an
 * inert `<template>` (nothing loads or runs); `describedBy` names the
 * keyboard hint of the view.
 *
 * One tab stop for the whole document (roving tabindex): the first block
 * has `tabindex="0"`, the others `-1` (the view moves between them with the
 * arrow keys). A document that cannot be commented has no focusable blocks.
 * `describedBy` is the short per-block hint; the arrow-key help belongs on
 * the container, once.
 */
export function splitBlocks(html: string, describedBy?: string, commentable = true): string[] {
    if (!html) {
        return [];
    }
    const template = document.createElement("template");
    template.innerHTML = html;
    const root = template.content.firstElementChild;
    if (!root) {
        return [];
    }
    return Array.from(root.children)
        .filter((el) => el.classList.contains("ideDocBlock"))
        .map((el, i) => {
            if (!commentable) {
                el.removeAttribute("tabindex");
            } else {
                el.setAttribute("tabindex", i === 0 ? "0" : "-1");
                // A named group: the view sets its aria-label (number, total, comments) on the live DOM.
                el.setAttribute("role", "group");
            }
            if (describedBy && commentable) {
                el.setAttribute("aria-describedby", describedBy);
            }
            return el.outerHTML;
        });
}

/** Document comments on this kind and version. */
export function documentComments(list: readonly Comment[], kind: ArtifactKind, version: number): Comment[] {
    return list.filter((c) => c.anchor === "document" && c.kind === kind && c.version === version);
}

/** Comments on other versions of the same kind that still matter (not dismissed). */
export function otherVersionComments(list: readonly Comment[], kind: ArtifactKind, version: number): Comment[] {
    return list.filter((c) => c.anchor === "document" && c.kind === kind && c.version !== version && c.state !== "dismissed");
}

/**
 * The comments per block (`paragraphs` groups). An anchor past the end (a
 * document shorter than expected) goes to the last block, a negative one to
 * the first, so no comment is hidden.
 */
export function byParagraph(list: readonly Comment[], paragraphs: number): Comment[][] {
    const groups: Comment[][] = Array.from({ length: Math.max(0, paragraphs) }, () => []);
    if (!groups.length) {
        return groups;
    }
    for (const c of list) {
        const p = Math.min(Math.max(0, c.paragraph ?? 0), groups.length - 1);
        groups[p].push(c);
    }
    return groups;
}

const STATE_ICONS: Record<CommentState, string> = {
    open: "sap-icon://comment",
    sent: "sap-icon://paper-plane",
    addressed: "sap-icon://accept",
    dismissed: "sap-icon://decline"
};

const STATE_VALUES: Record<CommentState, string> = {
    open: "Information",
    sent: "None",
    addressed: "Success",
    dismissed: "None"
};

export interface CommentRow {
    id: string;
    state: CommentState;
    stateText: string;
    /** A `sap.ui.core.ValueState`; the icon and the text carry the state too. */
    stateValue: string;
    icon: string;
    /** Raw text, bound to a Text control. */
    body: string;
    hasAnswer: boolean;
    answerText: string;
    canEdit: boolean;
    canDelete: boolean;
    canReopen: boolean;
    canDismiss: boolean;
}

export function commentRow(c: Comment, t: Translate): CommentRow {
    const moves = allowedTransitions(c.state);
    const answer = typeof c.answer === "string" ? c.answer : "";
    return {
        id: c.id,
        state: c.state,
        stateText: stateText(c.state, t),
        stateValue: own(STATE_VALUES, c.state) ?? "None",
        icon: own(STATE_ICONS, c.state) ?? "sap-icon://comment",
        body: String(c.body ?? ""),
        hasAnswer: !!answer,
        answerText: answer ? t("commentAnswer", [answer]) : "",
        canEdit: isEditable(c.state),
        canDelete: isEditable(c.state),
        canReopen: moves.includes("open"),
        canDismiss: moves.includes("dismissed")
    };
}

/** The accessible name of a paragraph's comment marker (paragraph 1-based). */
export function markerLabel(paragraph: number, comments: readonly Comment[], t: Translate): string {
    const n = paragraph + 1;
    if (!comments.length) {
        return t("docMarkerLabel", [n]);
    }
    if (comments.length === 1) {
        return t("docMarkerLabelOne", [n, stateText(comments[0].state, t)]);
    }
    return t("docMarkerLabelMany", [n, comments.length, comments.filter((c) => c.state === "open").length]);
}

/** The note on open comments the server's send cap held back for the next round; "" when none. */
export function commentsLeftText(left: number | undefined, t: Translate): string {
    if (!left || left <= 0) {
        return "";
    }
    return left === 1 ? t("commentsLeftOne") : t("commentsLeft", [left]);
}

/**
 * A comment whose block is past the end of the document shown (the layout
 * changed) sits on the last block; this says where it was ("" otherwise).
 */
export function blockHint(c: Comment, blocks: number, t: Translate): string {
    const p = c.paragraph ?? 0;
    return blocks > 0 && p >= blocks ? t("commentMovedBlock", [p + 1]) : "";
}

/**
 * The accessible name of a focusable block: its number, the total and its
 * comments ("Block 2 of 9, 1 comment (Open)"), 1-based for people.
 */
export function blockLabel(paragraph: number, total: number, comments: readonly Comment[], t: Translate): string {
    const n = paragraph + 1;
    if (!comments.length) {
        return t("docBlockLabel", [n, total]);
    }
    if (comments.length === 1) {
        return t("docBlockLabelOne", [n, total, stateText(comments[0].state, t)]);
    }
    return t("docBlockLabelMany", [n, total, comments.length, comments.filter((c) => c.state === "open").length]);
}

/**
 * A block's text as a comment quotes it: without the link host span the
 * renderer adds ("(example.com)" is not the author's text) and with a space
 * between table cells, so cells do not run together. The block is not
 * changed (a detached copy is read).
 */
export function blockText(el: Element): string {
    const copy = el.cloneNode(true) as Element;
    copy.querySelectorAll(".ideLinkHost").forEach((host) => {
        // With the blank the renderer put in front of it ("text (host)"); after a
        // serialise/parse round trip it is the end of the link's text node.
        const before = host.previousSibling;
        if (before?.nodeType === Node.TEXT_NODE) {
            before.textContent = (before.textContent ?? "").replace(/\s$/, "");
        }
        host.remove();
    });
    copy.querySelectorAll("td, th").forEach((cell) => cell.appendChild(cell.ownerDocument.createTextNode(" ")));
    return copy.textContent ?? "";
}
