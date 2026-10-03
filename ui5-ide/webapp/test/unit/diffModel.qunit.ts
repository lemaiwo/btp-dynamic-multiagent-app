import { ensureDiff } from "com/agent/ide/model/vendor";
import {
    sideBySide as diffRows, renderDiffHtml, foldUnchanged, type DiffRow, type DiffLabels
} from "com/agent/ide/model/diffModel";

/** The rows of a diff that is expected to stay within the limits. */
function sideBySide(origin: string | null, proposed: string | null): DiffRow[] {
    const rows = diffRows(origin, proposed);
    if (!rows) {
        throw new Error("unexpectedly too large to diff");
    }
    return rows;
}

const LABELS: DiffLabels = {
    left: "Source", right: "Proposed", caption: "Changes",
    markHeader: "Change", added: "added line", removed: "removed line", changed: "changed line",
    unchanged: (n: number) => `${n} unchanged lines`,
    tooLarge: "File too large to diff"
};

QUnit.module("diffModel", {
    before: function () {
        return ensureDiff();
    }
});

QUnit.test("identical inputs give only 'same' rows with matching line numbers", function (assert) {
    const rows = sideBySide("a\nb\nc\n", "a\nb\nc\n");
    assert.strictEqual(rows.length, 3);
    assert.ok(rows.every((r) => r.kind === "same"), "all same");
    assert.deepEqual(rows.map((r) => [r.leftNo, r.rightNo]), [[1, 1], [2, 2], [3, 3]]);
    assert.deepEqual(rows.map((r) => r.left), ["a", "b", "c"]);
});

QUnit.test("an added line is an 'add' row with no left side", function (assert) {
    const rows = sideBySide("a\nc", "a\nb\nc");
    assert.deepEqual(rows.map((r) => r.kind), ["same", "add", "same"]);
    const add = rows[1];
    assert.strictEqual(add.leftNo, null);
    assert.strictEqual(add.left, "");
    assert.strictEqual(add.rightNo, 2);
    assert.strictEqual(add.right, "b");
    assert.deepEqual([rows[2].leftNo, rows[2].rightNo], [2, 3], "numbering continues on both sides");
});

QUnit.test("a removed line is a 'del' row with no right side", function (assert) {
    const rows = sideBySide("a\nb\nc", "a\nc");
    assert.deepEqual(rows.map((r) => r.kind), ["same", "del", "same"]);
    assert.deepEqual([rows[1].leftNo, rows[1].left, rows[1].rightNo, rows[1].right], [2, "b", null, ""]);
});

QUnit.test("a removed block followed by an added block pairs up as 'chg', the rest as del/add", function (assert) {
    const rows = sideBySide("a\nx1\nx2\nz", "a\ny1\nz");
    assert.deepEqual(rows.map((r) => r.kind), ["same", "chg", "del", "same"]);
    assert.deepEqual([rows[1].left, rows[1].right], ["x1", "y1"]);
    assert.deepEqual([rows[1].leftNo, rows[1].rightNo], [2, 2]);
    assert.deepEqual([rows[2].leftNo, rows[2].rightNo], [3, null]);

    const more = sideBySide("a\nx\nz", "a\ny1\ny2\nz");
    assert.deepEqual(more.map((r) => r.kind), ["same", "chg", "add", "same"]);
});

QUnit.test("empty origin (a new file) is all 'add'; null counts as empty", function (assert) {
    const rows = sideBySide(null, "line 1\nline 2");
    assert.deepEqual(rows.map((r) => r.kind), ["add", "add"]);
    assert.deepEqual(sideBySide("", ""), [], "two empty sides have no rows");
});

QUnit.test("CRLF line ends compare equal to LF", function (assert) {
    const rows = sideBySide("a\r\nb\r\n", "a\nb\n");
    assert.ok(rows.every((r) => r.kind === "same"));
    assert.strictEqual(rows[0].left, "a", "no stray carriage return");
});

QUnit.test("renderDiffHtml uses the row classes and one root element", function (assert) {
    const html = renderDiffHtml(sideBySide("a\nb\nc", "a\nB\nc\nd"), { left: "Source", right: "Proposed" });
    assert.ok(/^<div[^>]*>/.test(html) && html.endsWith("</div>"), "one root element for core:HTML");
    assert.ok(html.includes("ideDiffChg"), "change class");
    assert.ok(html.includes("ideDiffAdd"), "add class");
    assert.ok(!html.includes("ideDiffDel"), "no delete here");
    assert.ok(html.includes(">Source<") && html.includes(">Proposed<"), "column headers");
    const del = renderDiffHtml(sideBySide("a\nb", "a"));
    assert.ok(del.includes("ideDiffDel"), "delete class");
});

QUnit.test("renderDiffHtml escapes markup in the source instead of rendering it", function (assert) {
    const evil = "<script>alert('x')</script> & \"q\"";
    const rows: DiffRow[] = sideBySide("", evil);
    const html = renderDiffHtml(rows, { left: "<b>L</b>", right: "R" });
    assert.notOk(html.includes("<script>"), "no raw script tag");
    assert.notOk(html.includes("<b>L</b>"), "headers are escaped too");
    assert.ok(html.includes("&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt; &amp; &quot;q&quot;"), "every special character escaped");

    const host = document.createElement("div");
    host.innerHTML = html;
    assert.strictEqual(host.querySelectorAll("script").length, 0, "parsing the HTML creates no script element");
    assert.ok((host.textContent ?? "").includes(evil), "the text reads exactly as the source");
});

QUnit.test("every changed row carries a +/-/~ marker with hidden text, not colour alone", function (assert) {
    const host = document.createElement("div");
    host.innerHTML = renderDiffHtml(sideBySide("a\nb\nc\nd", "a\nB\nc\nnew"), LABELS);
    const rows = Array.from(host.querySelectorAll("tbody tr"));
    const marks = rows.map((tr) => tr.querySelector(".ideDiffMark [aria-hidden='true']")?.textContent ?? "");
    const hidden = rows.map((tr) => tr.querySelector(".ideDiffMark .sapUiInvisibleText")?.textContent ?? "");
    assert.deepEqual(marks, ["", "~", "", "~"], "a change reads ~, unchanged rows have no marker");
    assert.deepEqual(hidden, ["", "changed line", "", "changed line"], "the marker has a text for screen readers");

    host.innerHTML = renderDiffHtml(sideBySide("a\nb", "a\nc\nd"), LABELS);
    const kinds = Array.from(host.querySelectorAll("tbody tr .ideDiffMark")).map((td) => td.textContent);
    assert.deepEqual(kinds, ["", "~changed line", "+added line"], "an added line reads + and 'added line'");
    host.innerHTML = renderDiffHtml(sideBySide("a\nb", "a"), LABELS);
    assert.strictEqual(host.querySelectorAll("tbody tr .ideDiffMark")[1].textContent, "\u2212removed line",
        "a removed line reads \u2212 and 'removed line'");
    assert.strictEqual(host.querySelector("thead th .sapUiInvisibleText")?.textContent, "Change",
        "the marker column has a header for screen readers");
});

QUnit.test("diffs beyond the limits give null and render the too-large message", function (assert) {
    const big = Array.from({ length: 50 }, (_, i) => `line ${i}`).join("\n");
    const other = Array.from({ length: 50 }, (_, i) => `other ${i}`).join("\n");
    assert.strictEqual(diffRows(big, other, { maxLines: 60 }), null, "too many lines");
    assert.strictEqual(diffRows(big, other, { maxEditLength: 5 }), null, "too many edits for jsdiff");
    assert.ok(diffRows(big, big, { maxEditLength: 5 }), "an unchanged file stays within the edit limit");
    const html = renderDiffHtml(null, LABELS);
    assert.ok(html.startsWith("<div") && html.endsWith("</div>"), "one root element");
    assert.ok(html.includes("File too large to diff"), "the message is shown");
    assert.notOk(html.includes("<table"), "no table");
});

QUnit.test("long runs of unchanged lines fold to 3 lines of context", function (assert) {
    const origin = Array.from({ length: 20 }, (_, i) => `l${i + 1}`);
    const proposed = origin.slice();
    proposed[9] = "CHANGED";
    const blocks = foldUnchanged(sideBySide(origin.join("\n"), proposed.join("\n")));
    assert.deepEqual(blocks.map((b) => [b.folded, b.rows.length]), [
        [true, 6], [false, 7], [true, 7]
    ], "6 hidden before (3 context), the change with 3 context each side, 7 hidden after");
    assert.deepEqual(blocks[1].rows.map((r) => r.kind), ["same", "same", "same", "chg", "same", "same", "same"]);

    const short = foldUnchanged(sideBySide("a\nb\nc", "a\nB\nc"));
    assert.ok(short.every((b) => !b.folded), "short runs are not folded");

    const host = document.createElement("div");
    host.innerHTML = renderDiffHtml(sideBySide(origin.join("\n"), proposed.join("\n")), LABELS);
    const folds = host.querySelectorAll("details");
    assert.strictEqual(folds.length, 2, "each fold is a native, keyboard-operable disclosure");
    assert.strictEqual(folds[0].querySelector("summary")?.textContent, "\u2026 6 unchanged lines");
    assert.strictEqual(folds[0].querySelectorAll("tr").length, 6, "the folded rows are there to expand");
    assert.strictEqual(host.querySelectorAll("table.ideDiffTable:not(.ideDiffFoldTable) > tbody > tr:not(.ideDiffFold)").length, 7,
        "only the context rows show unfolded");
});
