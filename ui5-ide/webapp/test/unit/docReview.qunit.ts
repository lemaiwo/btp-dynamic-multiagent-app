import {
    basedOnLink, byParagraph, commentRow, commentsLeftText, documentComments, markerLabel, otherVersionComments,
    splitBlocks, versionItems, blockHint, blockLabel, blockText
} from "com/agent/ide/model/docReview";
import { quoteOf } from "com/agent/ide/model/comments";
import { renderDocument } from "com/agent/ide/model/docView";
import { ensureMarkdown } from "com/agent/ide/model/vendor";
import type { ArtifactSummary, Comment, CommentState } from "com/agent/ide/service/types";

const t = (key: string, args?: (string | number)[]): string => (args?.length ? `${key}(${args.join("|")})` : key);

function summary(kind: ArtifactSummary["kind"], version: number, basedOn: Record<string, number> | null = null): ArtifactSummary {
    return { id: `${kind}-${version}`, stage: "design", kind, version, created_at: null, based_on: basedOn };
}

function docComment(id: string, state: CommentState, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 2, paragraph: 1, body: "b", state, answer: null,
        created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z", ...extra
    };
}

QUnit.module("docReview", {
    before: function () {
        return ensureMarkdown();
    }
});

QUnit.test("versionItems: ascending versions of one kind; the pinned one is marked approved", function (assert) {
    const items = versionItems([summary("design", 2), summary("plan", 1), summary("design", 1), summary("design", 3)], "design", 2, t);
    assert.deepEqual(items.map((i) => i.key), ["1", "2", "3"], "only design, oldest first");
    assert.deepEqual(items.map((i) => i.approved), [false, true, false], "v2 is the pin");
    assert.strictEqual(items[1].text, "docVersionApproved(2)", "the pinned version says approved in its text, not only by colour");
    assert.strictEqual(items[0].text, "docVersion(1)");
    assert.strictEqual(items[1].tooltip, "docVersionApprovedTooltip(docKindDesign|2)");
    assert.deepEqual(versionItems([summary("design", 1)], "design", undefined, t).map((i) => i.approved), [false], "no pin, nothing approved");
});

QUnit.test("basedOnLink: the first known pin kind of based_on, worded; null when none", function (assert) {
    assert.deepEqual(basedOnLink(summary("plan", 2, { design: 2 }), t),
        { text: "docBasedOn(docKindDesign|2)", kind: "design", version: 2 });
    assert.strictEqual(basedOnLink(summary("plan", 2, null), t), null);
    assert.strictEqual(basedOnLink(undefined, t), null);
    assert.strictEqual(basedOnLink(summary("plan", 2, { files: 3 } as unknown as Record<string, number>), t), null,
        "an unknown kind is no link");
    assert.strictEqual(basedOnLink(summary("plan", 2, { design: 0 }), t), null, "no version 0");
    assert.strictEqual(basedOnLink(summary("plan", 2, { design: 1.5 }), t), null, "integers only");
});

QUnit.test("splitBlocks: one HTML string per paragraph block, in order, each with its anchor", function (assert) {
    const doc = renderDocument("# Title\n\nFirst.\n\n- a\n- b\n\nLast.");
    const blocks = splitBlocks(doc.html, "hint-id");
    assert.strictEqual(blocks.length, doc.paragraphs, "as many blocks as paragraphs");
    blocks.forEach((html, i) => {
        const el = new DOMParser().parseFromString(html, "text/html").body.firstElementChild!;
        assert.strictEqual(el.getAttribute("data-para"), String(i), `block ${i} keeps its anchor`);
        assert.strictEqual(el.getAttribute("tabindex"), i === 0 ? "0" : "-1", "one tab stop: the first block, the rest by arrow keys");
        assert.strictEqual(el.getAttribute("aria-describedby"), "hint-id", "and names how to comment");
    });
    assert.ok(blocks[2].includes("<li>a</li>"), "a list is one block");
});

QUnit.test("splitBlocks: not commentable means no tabindex at all; tables and lists split like renderDocument", function (assert) {
    const md = "| a | b |\n|---|---|\n| 1 | 2 |\n\n1. one\n   - nested\n2. two\n\n> quote\n\n```\ncode\n```";
    const doc = renderDocument(md);
    const blocks = splitBlocks(doc.html, undefined, false);
    assert.strictEqual(blocks.length, doc.paragraphs, "one string per block of renderDocument");
    const whole = new DOMParser().parseFromString(doc.html, "text/html").body.firstElementChild!;
    blocks.forEach((html, i) => {
        const el = new DOMParser().parseFromString(html, "text/html").body.firstElementChild!;
        assert.notOk(el.hasAttribute("tabindex"), `block ${i} is not focusable when it cannot be commented`);
        assert.strictEqual(el.innerHTML, whole.children[i].innerHTML, `block ${i} has the structure renderDocument gave it`);
    });
    assert.ok(blocks[0].includes("<table>") && blocks[1].includes("<ol>") && blocks[1].includes("<ul>"), "table and nested list stay whole");
});

QUnit.test("quoteOf: the start of a block or line, whitespace collapsed, at most 200 characters", function (assert) {
    assert.strictEqual(quoteOf("  Round   the\n amount  "), "Round the amount");
    assert.strictEqual(quoteOf("x".repeat(300)).length, 200);
    assert.strictEqual(quoteOf(null), "");
});

QUnit.test("quoteOf: cut by code points (never half a surrogate pair), then trimmed", function (assert) {
    const emoji = "\u{1F600}";
    const cut = quoteOf(`${"x".repeat(199)}${emoji}tail`);
    assert.strictEqual(Array.from(cut).length, 200, "200 code points");
    assert.ok(cut.endsWith(emoji), "the pair stays whole");
    assert.notOk(/[\uD800-\uDBFF]$/.test(cut), "no lone high surrogate at the end");
    assert.strictEqual(quoteOf(`${"y".repeat(199)} z`), "y".repeat(199), "a blank at the cut is trimmed after cutting");
});

QUnit.test("blockText: the link host span is left out and table cells are joined with a space", function (assert) {
    const blocks = splitBlocks(renderDocument("See [docs](https://example.com/a) now.\n\n| a | b |\n|---|---|\n| 1 | 2 |").html);
    const el = (html: string): Element => new DOMParser().parseFromString(html, "text/html").body.firstElementChild!;
    assert.strictEqual(quoteOf(blockText(el(blocks[0]))), "See docs now.", "no (example.com) in the quote");
    assert.strictEqual(quoteOf(blockText(el(blocks[1]))), "a b 1 2", "cells do not run together");
    assert.ok(el(blocks[0]).querySelector(".ideLinkHost"), "the block itself is not changed");
    const end = splitBlocks(renderDocument("Read [the guide](https://example.com/g).").html);
    assert.strictEqual(quoteOf(blockText(el(end[0]))), "Read the guide.", "nor the blank the renderer put before the host");
});

QUnit.test("blockLabel: number, total and the comments' count and state", function (assert) {
    assert.strictEqual(blockLabel(1, 4, [], t), "docBlockLabel(2|4)");
    assert.strictEqual(blockLabel(0, 4, [docComment("a", "addressed")], t), "docBlockLabelOne(1|4|commentStateAddressed)");
    assert.strictEqual(blockLabel(3, 4, [docComment("a", "open"), docComment("b", "sent")], t), "docBlockLabelMany(4|4|2|1)");
});

QUnit.test("blockHint: a comment whose block is past the end says where it was", function (assert) {
    assert.strictEqual(blockHint(docComment("a", "open", { paragraph: 7 }), 3, t), "commentMovedBlock(8)");
    assert.strictEqual(blockHint(docComment("a", "open", { paragraph: 2 }), 3, t), "");
});

QUnit.test("splitBlocks: a block never gains script or handlers from the round trip", function (assert) {
    const doc = renderDocument("Text <img src=x onerror=alert(1)> and <script>alert(1)</script>");
    const blocks = splitBlocks(doc.html);
    assert.ok(blocks.length >= 1);
    blocks.forEach((html) => {
        assert.notOk(/onerror|<script/i.test(html), "nothing executable survives");
    });
    assert.deepEqual(splitBlocks(""), [], "an empty document has no blocks");
});

QUnit.test("documentComments / otherVersionComments split by kind and version", function (assert) {
    const list = [
        docComment("a", "open"),
        docComment("b", "addressed", { version: 1 }),
        docComment("c", "dismissed", { version: 1 }),
        docComment("d", "open", { kind: "plan" }),
        { ...docComment("e", "open"), anchor: "file" as const, kind: null, version: null, paragraph: null, path: "src/x", revision: 1, line_start: 1, line_end: 1 }
    ];
    assert.deepEqual(documentComments(list, "design", 2).map((c) => c.id), ["a"]);
    assert.deepEqual(otherVersionComments(list, "design", 2).map((c) => c.id), ["b"],
        "other versions of the same kind, dismissed ones left out");
});

QUnit.test("byParagraph: comments per block; an anchor past the end lands on the last block", function (assert) {
    const groups = byParagraph([docComment("a", "open", { paragraph: 0 }), docComment("b", "open", { paragraph: 9 }),
        docComment("c", "sent", { paragraph: 0 }), docComment("d", "open", { paragraph: -1 })], 3);
    assert.deepEqual(groups.map((g) => g.map((c) => c.id)), [["a", "c", "d"], [], ["b"]]);
    assert.deepEqual(byParagraph([docComment("a", "open")], 0), [], "no blocks, no groups");
});

QUnit.test("commentRow: state text and icon (not colour alone), actions per allowedTransitions", function (assert) {
    const open = commentRow(docComment("a", "open"), t);
    assert.deepEqual([open.canEdit, open.canDelete, open.canReopen, open.canDismiss], [true, true, false, true]);
    assert.strictEqual(open.stateText, "commentStateOpen");
    assert.ok(open.icon, "a state icon");
    const sent = commentRow(docComment("b", "sent"), t);
    assert.deepEqual([sent.canEdit, sent.canDelete, sent.canReopen, sent.canDismiss], [false, false, false, false]);
    const addressed = commentRow(docComment("c", "addressed", { answer: "Done in v3." }), t);
    assert.deepEqual([addressed.canEdit, addressed.canDelete, addressed.canReopen, addressed.canDismiss], [false, false, true, true]);
    assert.strictEqual(addressed.answerText, "commentAnswer(Done in v3.)");
    assert.ok(addressed.hasAnswer);
    const dismissed = commentRow(docComment("d", "dismissed"), t);
    assert.deepEqual([dismissed.canReopen, dismissed.canDismiss], [true, false]);
    assert.notStrictEqual(open.icon, addressed.icon, "states differ by icon");
});

QUnit.test("commentRow keeps body and answer as the raw strings (rendered as text by the view)", function (assert) {
    const row = commentRow(docComment("a", "addressed", { body: "<img src=x onerror=alert(1)> {/path}", answer: "{= 1 }" }), t);
    assert.strictEqual(row.body, "<img src=x onerror=alert(1)> {/path}");
    assert.strictEqual(row.answerText, "commentAnswer({= 1 })");
});

QUnit.test("markerLabel names the paragraph and counts its comments", function (assert) {
    assert.strictEqual(markerLabel(2, [], t), "docMarkerLabel(3)", "1-based for people");
    assert.strictEqual(markerLabel(0, [docComment("a", "open")], t), "docMarkerLabelOne(1|commentStateOpen)");
    assert.strictEqual(markerLabel(0, [docComment("a", "open"), docComment("b", "addressed")], t),
        "docMarkerLabelMany(1|2|1)");
});

QUnit.test("commentsLeftText: nothing for 0, singular and plural", function (assert) {
    assert.strictEqual(commentsLeftText(0, t), "");
    assert.strictEqual(commentsLeftText(undefined, t), "");
    assert.strictEqual(commentsLeftText(1, t), "commentsLeftOne");
    assert.strictEqual(commentsLeftText(4, t), "commentsLeft(4)");
});
