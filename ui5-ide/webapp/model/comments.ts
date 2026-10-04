import type { ArtifactKind, Comment, CommentState } from "../service/types";

/**
 * Review comments (contract §1.1): what the user may do with a comment in
 * each state, how its anchor is worded, and the lists the views bind.
 *
 * A comment's body and answer are plain text written by a person or the
 * model: callers render them as text (`sap.m.Text`, `textContent`), never
 * as HTML. Nothing here builds markup.
 *
 * `sent` exists only while a request-changes run is in flight (lead
 * decision): the run moves `open` comments to `sent`, `resolve_comments`
 * moves them to `addressed`, and when the run ends every comment still
 * `sent` is `open` again ({@link endRun}). The user has no transition out of
 * `sent`.
 */

type Translate = (key: string, args?: (string | number)[]) => string;

/** User transitions per state (server-enforced; the UI offers only these). */
const USER_TRANSITIONS: Record<CommentState, readonly CommentState[]> = {
    open: ["dismissed"],
    sent: [],
    addressed: ["open", "dismissed"],
    dismissed: ["open"]
};

const STATE_KEYS: Record<CommentState, string> = {
    open: "commentStateOpen",
    sent: "commentStateSent",
    addressed: "commentStateAddressed",
    dismissed: "commentStateDismissed"
};

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

/** The states the user may move a comment to from `state` (a fresh array). */
export function allowedTransitions(state: CommentState): CommentState[] {
    return [...(own(USER_TRANSITIONS, state) ?? [])];
}

/** Body edit and delete are allowed only while `open`. */
export function isEditable(state: CommentState): boolean {
    return state === "open";
}

/**
 * The comments as they stand once a request-changes run has ended: every
 * comment still `sent` is `open` again. Changed comments are copies; the
 * input is not mutated.
 */
export function endRun(list: readonly Comment[]): Comment[] {
    return list.map((c) => (c.state === "sent" ? { ...c, state: "open" } : c));
}

function fileName(path: string | null): string {
    const p = path ?? "";
    return p.slice(p.lastIndexOf("/") + 1);
}

/**
 * Where a comment sits, as plain text: "zcl_x.clas.abap, revision 3, lines
 * 12-14" or "Design v2, paragraph 5" (paragraph 1-based for people; the
 * anchor itself is 0-based).
 */
export function anchorText(c: Comment, t: Translate): string {
    if (c.anchor === "document") {
        const kindKey = own(KIND_KEYS, c.kind);
        return t("commentAnchorDocument", [
            kindKey ? t(kindKey) : String(c.kind ?? ""), c.version ?? 0, (c.paragraph ?? 0) + 1
        ]);
    }
    const start = c.line_start ?? 0;
    const end = c.line_end ?? start;
    return end === start
        ? t("commentAnchorFileLine", [fileName(c.path), c.revision ?? 0, start])
        : t("commentAnchorFileRange", [fileName(c.path), c.revision ?? 0, start, end]);
}

/**
 * The key comments share when they sit on the same spot: a document's kind,
 * version and paragraph, or a file's path, revision and first line (where
 * the marker is drawn).
 */
export function anchorKey(c: Comment): string {
    return c.anchor === "document"
        ? JSON.stringify(["document", c.kind, c.version, c.paragraph])
        : JSON.stringify(["file", c.path, c.revision, c.line_start]);
}

/** Comments grouped by {@link anchorKey}, groups in first-seen order, each group in input order. */
export function groupByAnchor(list: readonly Comment[]): Map<string, Comment[]> {
    const groups = new Map<string, Comment[]>();
    for (const c of list) {
        const key = anchorKey(c);
        const group = groups.get(key);
        if (group) {
            group.push(c);
        } else {
            groups.set(key, [c]);
        }
    }
    return groups;
}

/** Comments in state `open` (the "Request changes (n)" count). */
export function openCount(list: readonly Comment[]): number {
    return list.filter((c) => c.state === "open").length;
}

/** The state's display text; a state the UI does not know is shown as the server sent it. */
export function stateText(state: CommentState, t: Translate): string {
    const key = own(STATE_KEYS, state);
    return key ? t(key) : String(state ?? "");
}

/** The longest `quote` the server stores. */
export const QUOTE_MAX = 200;

/**
 * What a comment points at, as plain text for the model: the start of the
 * commented block or the first selected line, whitespace collapsed, at most
 * {@link QUOTE_MAX} characters. Cut by code points (as the server counts),
 * so a surrogate pair is never split, and trimmed after the cut.
 */
export function quoteOf(text: string | null | undefined): string {
    const collapsed = String(text ?? "").replace(/\s+/g, " ").trim();
    return Array.from(collapsed).slice(0, QUOTE_MAX).join("").trim();
}
