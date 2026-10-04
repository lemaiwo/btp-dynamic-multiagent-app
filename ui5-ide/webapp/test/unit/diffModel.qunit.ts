import { ensureDiff } from "com/agent/ide/model/vendor";
import {
    sideBySide as diffRows, renderDiffHtml, foldUnchanged, unifiedRows, changeStops, renderUnifiedHtml, hunksOf,
    type DiffRow, type Hunk, type DiffLabels, type UnifiedRow, type UnifiedLabels
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

QUnit.module("diffModel: stacked (unified)", {
    before: function () {
        return ensureDiff();
    }
});

function unified(oldText: string | null, newText: string | null): UnifiedRow[] {
    const rows = unifiedRows(oldText, newText);
    if (!rows) {
        throw new Error("unexpectedly too large to diff");
    }
    return rows;
}

const BASE40 = Array.from({ length: 40 }, (_, i) => `l${i + 1}`);

/** Three separate hunks: a change at 3, an insert after 20, a removal of 35. */
function threeHunks(): { oldText: string; newText: string } {
    const next = BASE40.slice();
    next[2] = "L3";
    next.splice(20, 0, "inserted");
    next.splice(next.indexOf("l35"), 1);
    return { oldText: BASE40.join("\n"), newText: next.join("\n") };
}

QUnit.test("unifiedRows stacks removed before added lines with old and new numbers", function (assert) {
    const rows = unified("a\nb\nc", "a\nB\nc\nd");
    assert.deepEqual(rows.map((r) => [r.kind, r.oldNo, r.newNo, r.text]), [
        ["same", 1, 1, "a"],
        ["del", 2, null, "b"],
        ["add", null, 2, "B"],
        ["same", 3, 3, "c"],
        ["add", null, 4, "d"]
    ]);
});

QUnit.test("a new file (no base) is all added; identical texts have no change stops", function (assert) {
    const rows = unified(null, "x\ny");
    assert.deepEqual(rows.map((r) => [r.kind, r.oldNo, r.newNo]), [["add", null, 1], ["add", null, 2]]);
    assert.deepEqual(changeStops(rows), [0], "one hunk at the top");
    const same = unified("a\nb\r\n", "a\r\nb");
    assert.ok(same.every((r) => r.kind === "same"), "CRLF and a final newline do not count as changes");
    assert.deepEqual(changeStops(same), [], "no stops");
    assert.deepEqual(unified("", ""), []);
});

QUnit.test("changeStops gives the first row index of each hunk", function (assert) {
    const { oldText, newText } = threeHunks();
    const rows = unified(oldText, newText);
    const stops = changeStops(rows);
    assert.strictEqual(stops.length, 3, "three hunks");
    assert.deepEqual(stops.map((i) => [rows[i].kind, rows[i].text]), [["del", "l3"], ["add", "inserted"], ["del", "l35"]]);
    assert.ok(stops.every((i) => i === 0 || rows[i - 1].kind === "same"), "each stop opens a hunk");
});

QUnit.test("unifiedRows answers null beyond the limits", function (assert) {
    const big = Array.from({ length: 50 }, (_, i) => `line ${i}`).join("\n");
    const other = Array.from({ length: 50 }, (_, i) => `other ${i}`).join("\n");
    assert.strictEqual(unifiedRows(big, other, { maxLines: 60 }), null);
    assert.strictEqual(unifiedRows(big, other, { maxEditLength: 5 }), null);
});

const ULABELS: UnifiedLabels = {
    region: "Changes in zcl_demo",
    added: (n: number) => `added line ${n}`,
    removed: (n: number) => `removed line ${n}`,
    unchanged: (n: number) => `${n} unchanged lines`,
    tooLarge: "File too large to compare"
};

QUnit.test("renderUnifiedHtml: one focusable region root with data-line/data-side per row", function (assert) {
    const html = renderUnifiedHtml(unified("a\nb\nc", "a\nB\nc"), {
        path: "src/CLAS/zcl_demo.clas.abap", revision: 3, labels: ULABELS
    });
    assert.ok(/^<div[^>]*>/.test(html) && html.endsWith("</div>"), "one root element for core:HTML");
    const div = document.createElement("div");
    div.innerHTML = html;
    assert.strictEqual(div.children.length, 1);
    const root = div.firstElementChild as HTMLElement;
    assert.ok(root.classList.contains("ideUnified"));
    assert.strictEqual(root.getAttribute("tabindex"), "0");
    assert.strictEqual(root.getAttribute("role"), "region");
    assert.strictEqual(root.getAttribute("aria-label"), "Changes in zcl_demo");
    assert.strictEqual(root.getAttribute("data-path"), "src/CLAS/zcl_demo.clas.abap");
    assert.strictEqual(root.getAttribute("data-revision"), "3");
    const rows = Array.from(root.querySelectorAll("tbody tr"));
    assert.deepEqual(rows.map((tr) => [tr.getAttribute("data-side"), tr.getAttribute("data-line")]), [
        ["new", "1"], ["old", "2"], ["new", "2"], ["new", "3"]
    ], "removed rows point at the old side, the rest at the new revision");
    assert.deepEqual(rows.map((tr) => [tr.querySelector(".ideUnifiedOld")?.textContent, tr.querySelector(".ideUnifiedNew")?.textContent]),
        [["1", "1"], ["2", ""], ["", "2"], ["3", "3"]], "both gutters");
});

QUnit.test("renderUnifiedHtml: <ins>/<del> plus +/− markers with hidden text, never colour alone", function (assert) {
    const div = document.createElement("div");
    div.innerHTML = renderUnifiedHtml(unified("a\nb", "a\nB"), { path: "p", revision: 1, labels: ULABELS });
    const rows = div.querySelectorAll("tbody tr");
    assert.strictEqual(rows[1].querySelector(".ideDiffCode del")?.textContent, "b", "removed text is a <del>");
    assert.strictEqual(rows[2].querySelector(".ideDiffCode ins")?.textContent, "B", "added text is an <ins>");
    assert.strictEqual(rows[0].querySelectorAll("ins, del").length, 0, "unchanged text is plain");
    assert.strictEqual(rows[1].querySelector(".ideDiffMark")?.textContent, "−removed line 2");
    assert.strictEqual(rows[2].querySelector(".ideDiffMark")?.textContent, "+added line 2");
    assert.strictEqual(rows[1].querySelector(".ideDiffMark [aria-hidden='true']")?.textContent, "−");
    assert.strictEqual(rows[2].querySelector(".ideDiffMark .sapUiInvisibleText")?.textContent, "added line 2");
    assert.ok(rows[1].classList.contains("ideDiffDel") && rows[2].classList.contains("ideDiffAdd"));
});

QUnit.test("renderUnifiedHtml escapes source, path and labels; no element is created", function (assert) {
    const evil = "<script>window.__u4 = 1</script><img src=x onerror=\"window.__u4 = 2\">";
    const html = renderUnifiedHtml(unified("<b>old</b>", evil), {
        path: "\"><img src=x onerror=alert(1)>", revision: 1, labels: { ...ULABELS, region: "<i>r</i>" }
    });
    assert.notOk(/<(script|img|b|i)[\s>]/.test(html), "no raw tag from the inputs");
    const div = document.createElement("div");
    div.innerHTML = html;
    assert.strictEqual(div.querySelectorAll("script, img, b, i").length, 0, "parsing creates no element");
    assert.strictEqual(div.querySelector("ins")?.textContent, evil, "the text reads exactly as the source");
    assert.strictEqual(div.querySelector("del")?.textContent, "<b>old</b>");
    assert.strictEqual((div.firstElementChild as HTMLElement).getAttribute("data-path"), "\"><img src=x onerror=alert(1)>");
});

QUnit.test("renderUnifiedHtml folds long unchanged runs and keeps a stop per hunk", function (assert) {
    const { oldText, newText } = threeHunks();
    const div = document.createElement("div");
    div.innerHTML = renderUnifiedHtml(unified(oldText, newText), { path: "p", revision: 2, labels: ULABELS });
    const folds = div.querySelectorAll("details");
    assert.strictEqual(folds.length, 2, "a native, keyboard-operable disclosure per long unchanged run");
    assert.ok(/^… \d+ unchanged lines$/.test(folds[0].querySelector("summary")?.textContent ?? ""));
    assert.strictEqual(div.querySelectorAll("tr[data-stop]").length, 3, "each hunk start is marked as a stop");
    assert.deepEqual(Array.from(div.querySelectorAll("tr[data-stop]")).map((tr) => tr.getAttribute("data-stop")),
        ["0", "1", "2"]);
});

QUnit.test("renderUnifiedHtml beyond the limits shows the fallback in the same region", function (assert) {
    const div = document.createElement("div");
    div.innerHTML = renderUnifiedHtml(null, { path: "p", revision: 1, labels: ULABELS });
    const root = div.firstElementChild as HTMLElement;
    assert.strictEqual(div.children.length, 1);
    assert.strictEqual(root.getAttribute("role"), "region");
    assert.strictEqual(root.getAttribute("tabindex"), "0");
    assert.strictEqual(root.querySelector(".ideDiffTooLarge")?.textContent, "File too large to compare");
    assert.strictEqual(root.querySelectorAll("table").length, 0);
    const bare = document.createElement("div");
    bare.innerHTML = renderUnifiedHtml(unified("a", "b"), { path: "p", revision: 1 });
    assert.ok(((bare.firstElementChild as HTMLElement).getAttribute("aria-label") ?? "").length > 0,
        "an accessible name without labels");
});

// --- Hunks only (final review M2): a large object shows its changes with context, nothing folded ---

QUnit.test("hunksOf: each change with 3 lines of context, its line ranges on both sides", function (assert) {
    const { oldText, newText } = threeHunks();
    const rows = unified(oldText, newText);
    const hunks = hunksOf(rows);
    assert.deepEqual(hunks.map((h) => [h.oldStart, h.oldCount, h.newStart, h.newCount]),
        [[1, 6, 1, 6], [18, 6, 18, 7], [32, 7, 33, 6]], "ranges as old start/count, new start/count");
    assert.deepEqual(hunks[1].rows.map((r) => r.text), ["l18", "l19", "l20", "inserted", "l21", "l22", "l23"]);
    assert.ok(hunks.every((h) => h.rows.every((r) => rows.includes(r))), "the hunks hold the diff's own rows");
});

QUnit.test("hunksOf: changes whose context touches are one hunk; further apart, two", function (assert) {
    const base = Array.from({ length: 60 }, (_, i) => `l${i + 1}`);
    const near = base.slice();
    near[9] = "X10";
    near[15] = "X16";
    assert.strictEqual(hunksOf(unified(base.join("\n"), near.join("\n"))).length, 1, "5 unchanged lines between: one hunk");
    const far = base.slice();
    far[9] = "X10";
    far[17] = "X18";
    assert.strictEqual(hunksOf(unified(base.join("\n"), far.join("\n"))).length, 2, "7 unchanged lines between: two hunks");
    assert.deepEqual(hunksOf(unified("a\nb", "a\nb")), [], "nothing changed: no hunk");
});

function hunksHtml(oldText: string, newText: string, extra: Partial<UnifiedLabels> = {}): HTMLElement {
    const div = document.createElement("div");
    div.innerHTML = renderUnifiedHtml(unified(oldText, newText), {
        path: "src/CLAS/zcl_demo.clas.abap", revision: 2, hunksOnly: true,
        labels: { ...ULABELS, hunkHeader: (h: Hunk) => `Lines ${h.newStart}-${h.newStart + h.newCount - 1}`, ...extra }
    });
    return div.firstElementChild as HTMLElement;
}

QUnit.test("renderUnifiedHtml hunksOnly: only the hunks' rows, a header per hunk, no folds; stops as in the full diff", function (assert) {
    const { oldText, newText } = threeHunks();
    const root = hunksHtml(oldText, newText);
    assert.strictEqual(root.querySelectorAll("details").length, 0, "no expandable unchanged regions");
    assert.strictEqual(root.querySelectorAll("tr[data-line]").length, 6 + 1 + 7 + 7, "the rows of the three hunks only");
    const headers = Array.from(root.querySelectorAll("tr.ideDiffHunk"));
    assert.deepEqual(headers.map((h) => h.textContent), ["Lines 1-6", "Lines 18-24", "Lines 33-38"], "one header per hunk");
    assert.ok(headers.every((h) => h.getAttribute("role") === "row" && h.querySelector("[role='gridcell']")), "header rows are grid rows");
    assert.ok(headers.every((h) => !h.hasAttribute("data-line") && !h.hasAttribute("tabindex")), "a header is not a line: not selectable, not a stop");
    assert.deepEqual(Array.from(root.querySelectorAll("tr[data-stop]")).map((tr) => tr.getAttribute("data-stop")), ["0", "1", "2"],
        "the change stops are numbered as in the full diff");
    assert.strictEqual(root.querySelector("tr[data-side='new'][data-line='21']")?.textContent?.includes("inserted"), true,
        "rows carry the revision's line numbers for comment anchors");
    assert.strictEqual(root.querySelector("tr[data-side='old'][data-line='35']")?.querySelector("del")?.textContent, "l35");
});

QUnit.test("renderUnifiedHtml hunksOnly: grid, names and non-colour cues as in the full diff; everything escaped", function (assert) {
    const hostile = "<img src=x onerror=alert(1)> {/x}";
    const base = Array.from({ length: 30 }, (_, i) => `l${i + 1}`);
    const next = base.slice();
    next[14] = hostile;
    const root = hunksHtml(base.join("\n"), next.join("\n"), { hunkHeader: () => "<b>header</b>" });
    assert.strictEqual(root.getAttribute("role"), "region");
    assert.strictEqual(root.getAttribute("tabindex"), "0");
    assert.strictEqual(root.getAttribute("aria-label"), "Changes in zcl_demo");
    const table = root.querySelector("table")!;
    assert.strictEqual(table.getAttribute("role"), "grid");
    assert.strictEqual(table.getAttribute("aria-multiselectable"), "true");
    assert.strictEqual(root.querySelectorAll("img, b").length, 0, "no element from the source or the header");
    assert.strictEqual(root.querySelector("tr[data-side='new'][data-line='15'] ins")?.textContent, hostile, "the line as text");
    assert.strictEqual(root.querySelector("tr.ideDiffHunk")?.textContent, "<b>header</b>", "the header as text");
    const added = root.querySelector("tr[data-side='new'][data-line='15'] .ideDiffMark")!;
    assert.ok(added.textContent!.includes("+") && added.textContent!.includes("added line 15"), "marker and hidden text");
    assert.ok(root.querySelector("tr[data-side='new'][data-line='12'] .sapUiInvisibleText")?.textContent?.includes("unchanged line 12"),
        "context lines say they are unchanged");
});

QUnit.test("renderUnifiedHtml hunksOnly: syntax marks on shown lines; the default header names both ranges", function (assert) {
    const base = Array.from({ length: 30 }, (_, i) => `l${i + 1}`);
    const next = base.slice();
    next[14] = "X15";
    const div = document.createElement("div");
    div.innerHTML = renderUnifiedHtml(unified(base.join("\n"), next.join("\n")), {
        path: "p", revision: 1, hunksOnly: true, idPrefix: "t",
        syntax: [{ line: 15, message: "bad", severity: "error" }, { line: 2, message: "not shown", severity: "error" }]
    });
    const root = div.firstElementChild as HTMLElement;
    assert.ok(root.querySelector("tr[data-side='new'][data-line='15']")!.classList.contains("ideDiffSyntaxError"));
    assert.notOk(root.querySelector("#t-syntax-2"), "a line that is not shown carries nothing");
    assert.strictEqual(root.querySelector("tr.ideDiffHunk")?.textContent, "@@ -12,7 +12,7 @@");
});
