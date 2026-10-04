import {
    CHANGES_LIMITS, baseInfo, baseLineOfFirstChange, changeCounts, commentsInRange, fileComments, lineMarkers, lintInfo,
    otherRevisionComments, proposedObjects, revisionItems, selectionRange, stepStop, syntaxInfo,
    approveRevisions, diffSyntax, settleLimited, compareObject, rangeShown, notCompared
} from "com/agent/ide/model/changesView";
import { decorateDiff, navigable, newLineOf, revealRow, rowFor, rowOfLine, setStop, step } from "com/agent/ide/model/diffDecor";
import { isProposal, isProposedObject } from "com/agent/ide/model/stageGate";
import { renderUnifiedHtml, unifiedRows } from "com/agent/ide/model/diffModel";
import { renderSource } from "com/agent/ide/model/sourceView";
import { ensureDiff } from "com/agent/ide/model/vendor";
import type { Comment, CommentState, FileSummary } from "com/agent/ide/service/types";

const t = (key: string, args?: (string | number)[]): string => (args?.length ? `${key}(${args.join("|")})` : key);

function fileComment(id: string, revision: number, start: number, end: number, state: CommentState = "open"): Comment {
    return {
        id, anchor: "file", path: "src/CLAS/zcl_x.clas.abap", revision, line_start: start, line_end: end,
        kind: null, version: null, paragraph: null, body: "b", state, answer: null,
        created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z"
    };
}

function summary(path: string, revision: number, extra: Partial<FileSummary> = {}): FileSummary {
    return {
        path, state: "modified", object_type: "CLAS", object_name: "ZCL_X", revision, base_status: "sap",
        syntax_status: null, ...extra
    };
}

function host(html: string): HTMLElement {
    const div = document.createElement("div");
    div.innerHTML = html;
    document.body.appendChild(div);
    return div.firstElementChild as HTMLElement;
}

const OLD = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");
const NEW = OLD.replace("line 12", "LINE 12\nextra").replace("line 25", "LINE 25");

QUnit.module("changesView", {
    before: function () {
        return ensureDiff();
    }
});

QUnit.test("baseInfo: absent is new in SAP; unknown and null are 'base not checked', never new", function (assert) {
    const absent = baseInfo({ object_type: "CLAS", base_status: "absent" }, null, t);
    assert.deepEqual([absent.text, absent.isNew], ["changesBaseAbsent", true]);
    const unknown = baseInfo({ object_type: "CLAS", base_status: "unknown" }, null, t);
    assert.deepEqual([unknown.text, unknown.state, unknown.isNew], ["changesBaseUnknown", "Warning", false]);
    const none = baseInfo({ object_type: "CLAS", base_status: null }, null, t);
    assert.deepEqual([none.text, none.isNew], ["changesBaseUnknown", false], "null is not checked either");
    assert.strictEqual(baseInfo({ object_type: "CLAS", base_status: "sap" }, "00042", t).text, "changesBaseSapVersion(00042)");
    assert.strictEqual(baseInfo({ object_type: "CLAS", base_status: "sap" }, null, t).text, "changesBaseSapUnknown");
    assert.strictEqual(baseInfo({ object_type: null, base_status: null }, null, t).text, "changesBaseNote", "a scratch note has no SAP base");
    [absent, unknown, none].forEach((b) => assert.ok(b.icon.startsWith("sap-icon://"), "an icon besides the colour"));
});

QUnit.test("syntaxInfo: ok is 'No syntax messages' (neutral), unavailable 'not checked', errors counted with lines", function (assert) {
    const ok = syntaxInfo("ok", [], t);
    assert.deepEqual([ok.text, ok.state], ["changesSyntaxOk", "None"], "never a green OK");
    const warn = syntaxInfo("ok", [{ line: 3, message: "w", severity: "warning" }], t);
    assert.deepEqual([warn.text, warn.state], ["changesSyntaxWarningsOne", "Warning"]);
    const unavailable = syntaxInfo("unavailable", [{ line: 1, message: "x", severity: "error" }], t);
    assert.deepEqual([unavailable.text, unavailable.state, unavailable.messages], ["changesSyntaxUnavailable", "Warning", []],
        "unavailable never shows messages or OK");
    const pending = syntaxInfo(null, [], t);
    assert.deepEqual([pending.text, pending.state], ["changesSyntaxPending", "None"]);
    const errors = syntaxInfo("errors", [
        { line: 31, message: "B", severity: "error" }, { line: 13, message: "A", severity: "error" }, { line: null, message: "C", severity: "warning" }
    ], t);
    assert.deepEqual([errors.text, errors.state], ["changesSyntaxErrors(2)", "Error"]);
    assert.deepEqual(errors.messages, [
        "changesSyntaxLine(changesSeverityError|13|A)", "changesSyntaxLine(changesSeverityError|31|B)",
        "changesSyntaxNoLine(changesSeverityWarning|C)"
    ], "by line, unplaced last");
    assert.strictEqual(syntaxInfo("errors", [{ line: 1, message: "A", severity: "error" }], t).text, "changesSyntaxErrorsOne");
    [ok, warn, unavailable, pending, errors].forEach((s) => assert.ok(s.icon.startsWith("sap-icon://"), "an icon besides the colour"));
    assert.notOk([ok, warn, unavailable, pending, errors].some((s) => s.state === "Success"), "no syntax state is ever green");
});

QUnit.test("lintInfo: not run, none, findings by worst severity", function (assert) {
    assert.strictEqual(lintInfo([], false, t).text, "changesLintNotRun");
    assert.strictEqual(lintInfo([], true, t).text, "changesLintNone");
    const one = lintInfo([{ line: 1, column: 1, severity: "warning", message: "m", rule: "r" }], true, t);
    assert.deepEqual([one.text, one.state], ["changesLintOne", "Warning"]);
    const many = lintInfo([
        { line: 1, column: 1, severity: "warning", message: "m", rule: "r" },
        { line: 2, column: 1, severity: "error", message: "m", rule: "r" }
    ], false, t);
    assert.deepEqual([many.text, many.state], ["changesLintMany(2)", "Error"], "stored findings count even when not run here");
});

QUnit.test("counts, first base line, proposed objects, revisions and the size cap", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    assert.deepEqual(changeCounts(rows), { added: 3, removed: 2 });
    assert.deepEqual(changeCounts(null), { added: 0, removed: 0 });
    assert.strictEqual(baseLineOfFirstChange(rows), 12, "the SAP line of the first change");
    assert.strictEqual(baseLineOfFirstChange(unifiedRows("a\nb", "a\nb\nc")!), 3, "an addition after line 2 opens at 3");
    assert.strictEqual(baseLineOfFirstChange(unifiedRows(null, "a")!), 1, "a new object at 1");
    assert.strictEqual(baseLineOfFirstChange(unifiedRows("a", "a")!), null);
    const files = [summary("a", 0), summary("b", 2), summary("notes/n.md", 1, { object_type: null })];
    assert.deepEqual(proposedObjects(files).map((f) => f.path), ["b", "notes/n.md"]);
    assert.deepEqual(revisionItems([
        { revision: 2, run_id: null, created_at: null, chars: 1, syntax_status: null },
        { revision: 1, run_id: null, created_at: null, chars: 1, syntax_status: null }
    ], 2, t), [{ key: "2", text: "changesRevisionLatest(2)" }, { key: "1", text: "changesRevision(1)" }]);
    assert.deepEqual(revisionItems([], 3, t), [{ key: "3", text: "changesRevisionLatest(3)" }], "the latest even before the list is read");
    const big = Array.from({ length: CHANGES_LIMITS.maxLines }, (_, i) => `x ${i}`).join("\n");
    assert.strictEqual(unifiedRows(big, `${big}\ny`, CHANGES_LIMITS), null, "past the cap the diff is not computed");
});

QUnit.test("comments per revision, by line, in a range; selection and stops", function (assert) {
    const list = [fileComment("a", 1, 12, 14), fileComment("b", 2, 3, 3), fileComment("c", 1, 12, 12, "dismissed"),
        fileComment("d", 2, 5, 5, "dismissed")];
    assert.deepEqual(fileComments(list, "src/CLAS/zcl_x.clas.abap", 1).map((c) => c.id), ["a", "c"]);
    assert.deepEqual(otherRevisionComments(list, "src/CLAS/zcl_x.clas.abap", 1).map((c) => c.id), ["b"], "not dismissed ones");
    assert.deepEqual(Array.from(lineMarkers(fileComments(list, "src/CLAS/zcl_x.clas.abap", 1)).keys()), [12]);
    assert.deepEqual(commentsInRange(list.slice(0, 1), 13, 20).map((c) => c.id), ["a"], "overlapping");
    assert.deepEqual(commentsInRange(list.slice(0, 1), 15, 20), []);
    assert.deepEqual(selectionRange(14, 12), [12, 14]);
    assert.strictEqual(stepStop(3, -1, 1), 0);
    assert.strictEqual(stepStop(3, 0, 1), 1);
    assert.strictEqual(stepStop(3, 2, 1), 2, "no wrap at the end");
    assert.strictEqual(stepStop(3, -1, -1), 2, "previous from nowhere is the last");
    assert.strictEqual(stepStop(3, 0, -1), 0);
    assert.strictEqual(stepStop(0, -1, 1), -1);
});

QUnit.test("renderUnifiedHtml: syntax lines are marked with a glyph, a tooltip and a description; text escaped", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    const root = host(renderUnifiedHtml(rows, {
        path: "p", revision: 1, idPrefix: "x1",
        syntax: [{ line: 12, message: "<img src=x onerror=alert(1)> {/a}", severity: "error" }, { line: 99, message: "far", severity: "error" }],
        labels: { syntaxError: (m) => `Syntax error: ${m}`, syntaxWarning: (m) => `Syntax warning: ${m}` }
    }));
    const row = root.querySelector<HTMLElement>("tr[data-side='new'][data-line='12']")!;
    assert.ok(row.classList.contains("ideDiffSyntaxError"));
    assert.strictEqual(row.getAttribute("title"), "<img src=x onerror=alert(1)> {/a}", "the message as tooltip, literally");
    const desc = document.getElementById(row.getAttribute("aria-describedby")!);
    assert.strictEqual(desc?.textContent, "Syntax error: <img src=x onerror=alert(1)> {/a}");
    assert.strictEqual(root.querySelectorAll("img").length, 0, "nothing is parsed as markup");
    assert.ok(row.querySelector(".ideDiffSyntaxMark"), "a glyph in the gutter");
    assert.notOk(root.querySelector("tr[data-side='old'].ideDiffSyntaxError"), "removed lines are not the revision's");
    root.parentElement!.remove();
});

QUnit.test("decorateDiff: selection with aria-selected, comment markers, one tab stop; navigation skips folded rows", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    const root = host(renderUnifiedHtml(rows, { path: "p", revision: 1 }));
    decorateDiff(root, { selected: [12, 13], markers: [{ line: 12, text: "1", label: "Line 12: 1 comment" }] });
    const sel = Array.from(root.querySelectorAll("tr[aria-selected='true']")).map((r) => r.getAttribute("data-line"));
    assert.deepEqual(sel, ["12", "13"], "the new-side rows of the range");
    assert.ok(root.querySelector("tr[data-side='new'][data-line='12']")!.classList.contains("ideDiffSelected"));
    assert.notOk(root.querySelector("tr[data-side='old'][data-line='12']")!.classList.contains("ideDiffSelected"), "a removed line is never selected");
    assert.ok(Array.from(root.querySelectorAll("tr[data-line]")).every((r) => r.hasAttribute("tabindex")), "every row is focusable");
    assert.strictEqual(root.querySelectorAll("[tabindex='0']").length, 1, "exactly one tab stop in the diff");
    assert.strictEqual(root.getAttribute("tabindex"), "-1", "the region itself is no longer a second stop");
    assert.ok(Array.from(root.querySelectorAll("summary")).every((s) => s.getAttribute("tabindex") !== "0"
        || s === root.querySelector("[tabindex='0']")), "fold summaries are not extra stops");
    const mark = root.querySelector<HTMLElement>(".ideDiffCommentMark")!;
    assert.strictEqual(mark.textContent, "1");
    assert.strictEqual(mark.getAttribute("aria-label"), "Line 12: 1 comment");
    assert.strictEqual(mark.getAttribute("data-line"), "12");
    assert.strictEqual(mark.getAttribute("tabindex"), "-1");
    const nav = navigable(root);
    assert.ok(nav.every((el) => !el.closest("details:not([open]) tbody")), "nothing inside a closed fold");
    assert.ok(nav.some((el) => el.tagName === "SUMMARY"), "the fold summary is a stop");
    const first = nav[0];
    assert.strictEqual(step(root, first, 1), nav[1]);
    assert.strictEqual(step(root, first, -1), first, "stays at the top");
    assert.strictEqual(step(root, first, Infinity), nav[nav.length - 1], "End");
    setStop(root, nav[2]);
    assert.strictEqual(root.querySelector("[tabindex='0']"), nav[2], "the stop moves with the focus");
    decorateDiff(root, { selected: null, markers: [] });
    assert.strictEqual(root.querySelectorAll("tr[aria-selected='true'], .ideDiffCommentMark").length, 0, "decoration is replaced");
    assert.strictEqual(root.querySelector("[tabindex='0']"), nav[2], "a re-decoration keeps the stop");
    const r12 = rowOfLine(root, 12)!;
    assert.strictEqual(r12.getAttribute("data-side"), "new");
    assert.strictEqual(rowFor(r12.querySelector("td")!), r12);
    assert.strictEqual(newLineOf(r12), 12);
    assert.strictEqual(newLineOf(root.querySelector("tr[data-side='old']") as HTMLElement), null);
    root.parentElement!.remove();
});

QUnit.test("decorateDiff: every row gets an accessible name (kept current) and the short hint next to its own description", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    const root = host(renderUnifiedHtml(rows, {
        path: "p", revision: 1, idPrefix: "dx", syntax: [{ line: 13, message: "bad", severity: "error" }]
    }));
    const label = (r: { side: string; line: number; kind: string; code: string; marker?: { comments?: string } }): string =>
        `${r.kind} ${r.side} ${r.line} [${r.marker?.comments ?? ""}] ${r.code}`;
    decorateDiff(root, { selected: null, markers: [], rowLabel: label, rowHint: "hint" });
    const r12 = rowOfLine(root, 12)!;
    assert.strictEqual(r12.getAttribute("aria-label"), "add new 12 [] LINE 12");
    assert.strictEqual(root.querySelector("tr[data-side='old'][data-line='12']")!.getAttribute("aria-label"), "del old 12 [] line 12");
    assert.strictEqual(rowOfLine(root, 1)!.getAttribute("aria-label"), "same new 1 [] line 1", "rows inside a fold too");
    assert.deepEqual(rowOfLine(root, 13)!.getAttribute("aria-describedby")!.split(" "), ["dx-syntax-13", "hint"],
        "the syntax description stays, the hint is added");
    decorateDiff(root, { selected: null, markers: [{ line: 12, text: "2", label: "L", comments: "2 comments, 1 open" }], rowLabel: label, rowHint: "hint" });
    assert.strictEqual(r12.getAttribute("aria-label"), "add new 12 [2 comments, 1 open] LINE 12", "the comments follow");
    assert.notOk(r12.getAttribute("aria-label")!.includes("2L"), "the marker's own text is not read as code");
    assert.deepEqual(rowOfLine(root, 13)!.getAttribute("aria-describedby")!.split(" "), ["dx-syntax-13", "hint"], "the hint is not added twice");
    root.parentElement!.remove();
});

QUnit.test("renderSource: a selected range carries aria-selected, not only a colour", function (assert) {
    const root = host(renderSource("a\nb\nc\nd", { selected: [2, 3], highlightLine: 4 }));
    assert.deepEqual(Array.from(root.querySelectorAll("tr[aria-selected='true']")).map((r) => r.getAttribute("data-line")), ["2", "3"]);
    root.parentElement!.remove();
});

// --- Fix round 1 -----------------------------------------------------------------

QUnit.test("proposals are files in state modified/new only: a dropped proposal (read, revisions kept) is not one", function (assert) {
    const files: FileSummary[] = [
        summary("src/CLAS/zcl_a.clas.abap", 2),
        summary("src/CLAS/zcl_b.clas.abap", 3, { state: "read" }),
        summary("src/INTF/zif_c.intf.abap", 1, { state: "new", object_type: "INTF", object_name: "ZIF_C" }),
        summary("notes/plan.md", 1, { state: "new", object_type: null, object_name: null }),
        summary("src/PROG/zd.prog.abap", 0, { state: "read", object_type: "PROG", object_name: "ZD" })
    ];
    assert.deepEqual(proposedObjects(files).map((f) => f.path),
        ["src/CLAS/zcl_a.clas.abap", "src/INTF/zif_c.intf.abap", "notes/plan.md"], "state decides, not the revision");
    assert.ok(isProposal({ state: "modified" }) && isProposal({ state: "new" }));
    assert.notOk(isProposal({ state: "read" }), "read is no proposal");
    assert.ok(isProposedObject({ state: "new", object_type: "CLAS" }));
    assert.notOk(isProposedObject({ state: "new", object_type: null }), "a note is no object");
    assert.notOk(isProposedObject({ state: "read", object_type: "CLAS" }));
});

QUnit.test("approveRevisions: the revision each card shows, proposed objects only", function (assert) {
    const cards = [
        { summary: summary("src/CLAS/zcl_a.clas.abap", 3), revision: 2 },
        { summary: summary("src/CLAS/zcl_b.clas.abap", 3, { state: "read" }), revision: 3 },
        { summary: summary("notes/plan.md", 1, { state: "new", object_type: null, object_name: null }), revision: 1 },
        { summary: summary("src/INTF/zif_c.intf.abap", 1, { state: "new", object_type: "INTF" }), revision: 1 },
        { summary: summary("src/PROG/zd.prog.abap", 0, { object_type: "PROG" }), revision: 0 }
    ];
    assert.deepEqual(approveRevisions(cards), { "src/CLAS/zcl_a.clas.abap": 2, "src/INTF/zif_c.intf.abap": 1 });
    assert.deepEqual(approveRevisions([]), {});
});

QUnit.test("lintInfo on an older revision: lint applies to the latest (neutral)", function (assert) {
    const older = lintInfo([{ line: 1, message: "m", severity: "error" } as never], true, t, true);
    assert.deepEqual([older.text, older.state], ["changesLintLatestOnly", "None"]);
    assert.ok(older.icon.startsWith("sap-icon://"));
    assert.strictEqual(lintInfo([], false, t, false).text, "changesLintNotRun", "the latest keeps its state");
});

QUnit.test("diffSyntax: lines are marked only for a finished check (ok/errors)", function (assert) {
    const items = [{ line: 2, message: "m", severity: "error" as const }];
    assert.deepEqual(diffSyntax("errors", items), items);
    assert.deepEqual(diffSyntax("ok", items), items);
    assert.deepEqual(diffSyntax(null, items), [], "pending");
    assert.deepEqual(diffSyntax(undefined, items), [], "not known");
    assert.deepEqual(diffSyntax("unavailable", items), [], "unavailable vouches for nothing");
    assert.deepEqual(diffSyntax("pending" as never, items), []);
});

QUnit.test("settleLimited: at most n at a time, results in input order, a rejection does not stop the rest", async function (assert) {
    let running = 0;
    let peak = 0;
    const out = await settleLimited([1, 2, 3, 4, 5, 6, 7, 8, 9], 4, async (n) => {
        running++;
        peak = Math.max(peak, running);
        await new Promise((r) => setTimeout(r, 5 * (10 - n)));
        running--;
        if (n === 3) {
            throw new Error("three");
        }
        return n * 10;
    });
    assert.strictEqual(peak, 4, "never more than 4 in flight");
    assert.deepEqual(out.map((r) => (r.status === "fulfilled" ? r.value : (r.reason as Error).message)),
        [10, 20, "three", 40, 50, 60, 70, 80, 90]);
    assert.deepEqual(await settleLimited([], 4, async () => 1), []);
});

QUnit.test("renderUnifiedHtml: a grid of rows and gridcells (multiselectable); unchanged rows say so", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    const root = host(renderUnifiedHtml(rows, {
        path: "p", revision: 1, labels: { region: "Diff", sameLine: (n: number) => `unchanged line ${n}` }
    }));
    const grids = Array.from(root.querySelectorAll("table"));
    assert.ok(grids.length > 1, "folds nest a table");
    grids.forEach((g) => {
        assert.strictEqual(g.getAttribute("role"), "grid", "every table is a grid");
        assert.strictEqual(g.getAttribute("aria-multiselectable"), "true");
    });
    root.querySelectorAll("tr[data-line]").forEach((tr) => {
        assert.strictEqual(tr.getAttribute("role"), "row");
        Array.from(tr.children).forEach((td) => assert.strictEqual(td.getAttribute("role"), "gridcell"));
    });
    root.querySelectorAll("thead th").forEach((th) => assert.strictEqual(th.getAttribute("role"), "columnheader"));
    const fold = root.querySelector("tr.ideDiffFold")!;
    assert.strictEqual(fold.getAttribute("role"), "row");
    assert.strictEqual(fold.firstElementChild!.getAttribute("role"), "gridcell");
    assert.ok(root.querySelector("table.ideDiffFoldTable")!.getAttribute("aria-label"), "a nested grid is named");
    const same = rowOfLine(root, 1)!;
    assert.strictEqual(same.querySelector("td.ideDiffMark .sapUiInvisibleText")!.textContent, "unchanged line 1");
    const added = rowOfLine(root, 12)!;
    assert.notOk((added.querySelector("td.ideDiffMark")!.textContent ?? "").includes("unchanged"));
    root.parentElement!.remove();
});

QUnit.test("decorateDiff opens a fold holding a comment marker, a syntax mark or the selection; revealRow opens one", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    const make = (syntaxLine?: number): HTMLElement => host(renderUnifiedHtml(rows, {
        path: "p", revision: 1, syntax: syntaxLine ? [{ line: syntaxLine, message: "x", severity: "warning" }] : []
    }));
    const foldOf = (root: HTMLElement, line: number): HTMLDetailsElement => rowOfLine(root, line)!.closest("details")!;

    const plain = make();
    decorateDiff(plain, { selected: null, markers: [] });
    assert.ok(foldOf(plain, 2), "line 2 is folded");
    assert.notOk(foldOf(plain, 2).open, "a fold with nothing in it stays closed");
    decorateDiff(plain, { selected: null, markers: [{ line: 2, text: "1", label: "L" }] });
    assert.ok(foldOf(plain, 2).open, "a comment marker opens it");
    plain.parentElement!.remove();

    const syn = make(3);
    decorateDiff(syn, { selected: null, markers: [] });
    assert.ok(foldOf(syn, 3).open, "a syntax mark opens it");
    syn.parentElement!.remove();

    const sel = make();
    decorateDiff(sel, { selected: [4, 4], markers: [] });
    assert.ok(foldOf(sel, 4).open, "the selection opens it");
    sel.parentElement!.remove();

    const link = make();
    decorateDiff(link, { selected: null, markers: [] });
    assert.notOk(foldOf(link, 5).open);
    revealRow(rowOfLine(link, 5)!);
    assert.ok(foldOf(link, 5).open, "an anchor-link target is revealed");
    assert.ok(navigable(link).includes(rowOfLine(link, 5)!), "and reachable by keyboard");
    link.parentElement!.remove();
});

// --- Review follow-ups 2 -------------------------------------------------------------

QUnit.test("decorateDiff: a row's name cuts the code at 80 code points; the hidden kind text is not read next to the name", function (assert) {
    const emoji = "\u{1F600}";
    const long = `${"x".repeat(79)}${emoji}${"y".repeat(40)}`;
    const rows = unifiedRows(OLD, NEW.replace("LINE 12", long))!;
    const root = host(renderUnifiedHtml(rows, { path: "p", revision: 1, labels: { sameLine: (n: number) => `unchanged line ${n}` } }));
    const codes: string[] = [];
    decorateDiff(root, { selected: null, markers: [], rowLabel: (r) => { codes.push(r.code); return `L${r.line}: ${r.code}`; } });
    const r12 = rowOfLine(root, 12)!;
    assert.strictEqual(r12.getAttribute("aria-label"), `L12: ${"x".repeat(79)}${emoji}…`, "80 code points, the pair whole, then an ellipsis");
    assert.strictEqual(rowOfLine(root, 13)!.getAttribute("aria-label"), "L13: extra", "a short line is whole");
    assert.ok(codes.every((c) => Array.from(c).length <= 81), "no name carries more than 80 code points of code");
    root.querySelectorAll<HTMLElement>("tr[data-line] td.ideDiffMark .sapUiInvisibleText:not([id])").forEach((span) => {
        assert.strictEqual(span.getAttribute("aria-hidden"), "true", `"${span.textContent}" is left to the row's name`);
    });
    const plain = host(renderUnifiedHtml(rows, { path: "p", revision: 1 }));
    decorateDiff(plain, { selected: null, markers: [] });
    assert.notOk(rowOfLine(plain, 1)!.querySelector("td.ideDiffMark .sapUiInvisibleText")!.hasAttribute("aria-hidden"),
        "without a row name the hidden kind text stays readable");
    root.parentElement!.remove();
    plain.parentElement!.remove();
});

QUnit.test("decorateDiff: a fold the user closed stays closed until a new mark or selection lands in it", function (assert) {
    const rows = unifiedRows(OLD, NEW)!;
    const root = host(renderUnifiedHtml(rows, { path: "p", revision: 1, syntax: [{ line: 3, message: "x", severity: "warning" }] }));
    const fold = rowOfLine(root, 2)!.closest("details")!;
    assert.strictEqual(rowOfLine(root, 5)!.closest("details"), fold, "lines 2 to 5 share a fold");
    decorateDiff(root, { selected: null, markers: [{ line: 2, text: "1", label: "L" }] });
    assert.ok(fold.open, "the marker and the syntax mark open it");
    fold.open = false;
    decorateDiff(root, { selected: null, markers: [{ line: 2, text: "2", label: "L" }] });
    assert.notOk(fold.open, "the same marker (another count) and the same syntax mark do not reopen it");
    decorateDiff(root, { selected: [4, 4], markers: [{ line: 2, text: "2", label: "L" }] });
    assert.ok(fold.open, "a new selection in it opens it");
    fold.open = false;
    decorateDiff(root, { selected: [4, 4], markers: [{ line: 2, text: "2", label: "L" }] });
    assert.notOk(fold.open, "the same selection does not");
    decorateDiff(root, { selected: [4, 4], markers: [{ line: 2, text: "2", label: "L" }, { line: 5, text: "1", label: "L" }] });
    assert.ok(fold.open, "a new marker in it opens it");
    root.parentElement!.remove();
});

// --- Final review M2: what a card can show of a large object ---

function bigText(count: number, tag = "line"): string[] {
    return Array.from({ length: count }, (_, i) => `${tag} ${i + 1}`);
}

QUnit.test("compareObject: up to 5,000 lines the full diff; beyond, hunks only up to 40,000; beyond that, or past the edit cap, nothing", function (assert) {
    assert.strictEqual(CHANGES_LIMITS.maxLines, 5000);
    assert.strictEqual(CHANGES_LIMITS.hunksMaxLines, 40000);
    const small = bigText(1000);
    const smallNext = small.slice();
    smallNext[500] = "changed";
    const full = compareObject(small.join("\n"), smallNext.join("\n"));
    assert.strictEqual(full.mode, "full");
    assert.strictEqual(full.rows?.length, 1001);

    const big = bigText(2600);
    const bigNext = big.slice();
    bigNext[1299] = "changed";
    const hunks = compareObject(big.join("\n"), bigNext.join("\n"));
    assert.strictEqual(hunks.mode, "hunks", "a one-line change in a 2,600-line class is compared, hunks only");
    assert.ok(hunks.rows && hunks.rows.some((r) => r.kind === "add" && r.text === "changed"));

    const huge = bigText(20001);
    const none = compareObject(huge.join("\n"), huge.concat("one more").join("\n"));
    assert.deepEqual([none.mode, none.rows], ["none", null], "over 40,000 lines in all: not compared");

    const edits = compareObject(bigText(2100, "a").join("\n"), bigText(2100, "b").join("\n"));
    assert.deepEqual([edits.mode, edits.rows], ["none", null], "past the edit cap: not compared");

    const fresh = compareObject(null, "x\ny");
    assert.strictEqual(fresh.mode, "full", "a new object");
});

QUnit.test("rangeShown: in hunks mode a selection may only cover lines that are on screen", function (assert) {
    const base = bigText(3000);
    const next = base.slice();
    next[99] = "c100";
    next[1999] = "c2000";
    const { rows, mode } = compareObject(base.join("\n"), next.join("\n"));
    assert.strictEqual(mode, "hunks");
    assert.ok(rangeShown(rows, mode, 97, 103), "within one hunk");
    assert.notOk(rangeShown(rows, mode, 100, 2000), "across the lines that are not shown");
    assert.notOk(rangeShown(rows, mode, 500, 500), "a line that is not shown");
    assert.ok(rangeShown(rows, "full", 100, 2000), "the full diff shows every line");
});

QUnit.test("notCompared: the loaded cards whose diff could not be computed (not failed reads)", function (assert) {
    const cards = [
        { path: "a", rows: [], failed: false },
        { path: "b", rows: null, failed: false },
        { path: "c", rows: [], failed: true },
        { path: "d", rows: null }
    ];
    assert.deepEqual(notCompared(cards).map((c) => c.path), ["b", "d"]);
});
