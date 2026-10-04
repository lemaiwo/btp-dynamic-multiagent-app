import { renderSource, scrollTargetLine, SOURCE_LIMITS, type SourceLabels } from "com/agent/ide/model/sourceView";

const LABELS: SourceLabels = {
    region: "Source of zcl_demo",
    error: "error",
    warning: "warning",
    info: "information",
    tooLarge: (shown: number, total: number) => `Showing the first ${shown} of ${total} lines`
};

function host(html: string): HTMLDivElement {
    const div = document.createElement("div");
    div.innerHTML = html;
    return div;
}

QUnit.module("sourceView");

QUnit.test("one keyboard-scrollable region root with a table of numbered lines", function (assert) {
    const html = renderSource("line a\nline b\nline c", { labels: LABELS });
    assert.ok(/^<div[^>]*>/.test(html) && html.endsWith("</div>"), "one root element for core:HTML");
    const div = host(html);
    assert.strictEqual(div.children.length, 1, "a single root");
    const root = div.firstElementChild as HTMLElement;
    assert.ok(root.classList.contains("ideSource"), "root class");
    assert.strictEqual(root.getAttribute("tabindex"), "0", "focusable so the keyboard can scroll it");
    assert.strictEqual(root.getAttribute("role"), "region");
    assert.strictEqual(root.getAttribute("aria-label"), "Source of zcl_demo");
    const rows = Array.from(root.querySelectorAll("table tbody tr"));
    assert.deepEqual(rows.map((tr) => tr.getAttribute("data-line")), ["1", "2", "3"], "data-line per row");
    assert.deepEqual(rows.map((tr) => tr.querySelector(".ideSourceNo")?.textContent), ["1", "2", "3"], "line numbers");
    assert.deepEqual(rows.map((tr) => tr.querySelector(".ideSourceCode")?.textContent), ["line a", "line b", "line c"]);
});

QUnit.test("the region has an accessible name even without labels", function (assert) {
    const root = host(renderSource("x")).firstElementChild as HTMLElement;
    assert.ok((root.getAttribute("aria-label") ?? "").length > 0, "never an empty aria-label");
});

QUnit.test("CRLF line ends, a trailing newline and empty text", function (assert) {
    const rows = host(renderSource("a\r\nb\r\n")).querySelectorAll("tbody tr");
    assert.strictEqual(rows.length, 2, "the final newline does not add an empty line");
    assert.strictEqual(rows[0].querySelector(".ideSourceCode")?.textContent, "a", "no stray carriage return");
    assert.strictEqual(host(renderSource("")).querySelectorAll("tbody tr").length, 0, "empty text, no rows");
    assert.strictEqual(host(renderSource(null)).querySelectorAll("tbody tr").length, 0, "null counts as empty");
});

QUnit.test("markup in the source is escaped and creates no element", function (assert) {
    const evil = "<script>window.__u4 = 1</script> <img src=x onerror=\"window.__u4 = 2\"> & 'q'";
    const html = renderSource(evil, { labels: { ...LABELS, region: "<b>R</b>" } });
    assert.notOk(html.includes("<script"), "no raw script tag");
    assert.notOk(html.includes("<img"), "no raw img tag");
    assert.notOk(html.includes("<b>"), "the label is escaped too");
    const div = host(html);
    assert.strictEqual(div.querySelectorAll("script, img, b").length, 0, "parsing creates no element from the text");
    assert.strictEqual(div.querySelector(".ideSourceCode")?.textContent, evil, "the text reads exactly as the source");
    assert.strictEqual((div.firstElementChild as HTMLElement).getAttribute("aria-label"), "<b>R</b>");
});

QUnit.test("the highlight line carries aria-current and a class", function (assert) {
    const div = host(renderSource("a\nb\nc", { highlightLine: 2 }));
    const current = div.querySelectorAll("[aria-current]");
    assert.strictEqual(current.length, 1, "exactly one current line");
    assert.strictEqual(current[0].getAttribute("aria-current"), "true");
    assert.strictEqual(current[0].getAttribute("data-line"), "2");
    assert.ok(current[0].classList.contains("ideSourceHighlight"));
    assert.strictEqual(host(renderSource("a", { highlightLine: 9 })).querySelectorAll("[aria-current]").length, 0,
        "a line beyond the text highlights nothing");
});

QUnit.test("a selected range marks its rows, in either order", function (assert) {
    const sel = (from: number, to: number): string[] => Array.from(
        host(renderSource("1\n2\n3\n4\n5", { selected: [from, to] })).querySelectorAll("tr.ideSourceSelected")
    ).map((tr) => tr.getAttribute("data-line") ?? "");
    assert.deepEqual(sel(2, 4), ["2", "3", "4"]);
    assert.deepEqual(sel(4, 2), ["2", "3", "4"], "reversed range");
});

QUnit.test("lint findings show a severity glyph with hidden text, not colour alone", function (assert) {
    const div = host(renderSource("a\nb\nc", {
        labels: LABELS,
        lint: [
            { line: 2, column: 1, severity: "Error", message: "<b>bad</b>", rule: "R1" },
            { line: 2, column: 4, severity: "warning", message: "meh", rule: "" },
            { line: 3, column: 1, severity: "info", message: "fyi", rule: "" }
        ]
    }));
    const rows = div.querySelectorAll("tbody tr");
    assert.strictEqual(rows[0].querySelector(".ideSourceLint")?.textContent, "", "no finding, no marker");
    assert.ok(rows[1].classList.contains("ideSourceLintError"), "worst severity wins on the row");
    assert.strictEqual(rows[1].querySelector(".ideSourceLint [aria-hidden='true']")?.textContent, "!");
    const hidden = rows[1].querySelector(".ideSourceLint .sapUiInvisibleText")?.textContent ?? "";
    assert.ok(hidden.includes("error: <b>bad</b> (R1)") && hidden.includes("warning: meh"), "each finding is read out");
    assert.strictEqual(div.querySelectorAll("b").length, 0, "lint messages are escaped");
    assert.ok(rows[2].classList.contains("ideSourceLintInfo"));
});

QUnit.test("a file beyond the size cap shows its head and says so", function (assert) {
    const text = Array.from({ length: 30 }, (_, i) => `l${i + 1}`).join("\n");
    const div = host(renderSource(text, { labels: LABELS, maxLines: 10 }));
    assert.strictEqual(div.children.length, 1, "still one root");
    assert.strictEqual(div.querySelectorAll("tbody tr").length, 10, "only the first 10 lines are rendered");
    assert.strictEqual(div.querySelector(".ideSourceTooLarge")?.textContent, "Showing the first 10 of 30 lines");
    assert.ok(SOURCE_LIMITS.maxLines >= 10000, "a sensible default cap");
});

QUnit.test("scrollTargetLine clamps a line into the text", function (assert) {
    assert.strictEqual(scrollTargetLine(5, 10), 5);
    assert.strictEqual(scrollTargetLine(0, 10), 1, "below the first line");
    assert.strictEqual(scrollTargetLine(-3, 10), 1);
    assert.strictEqual(scrollTargetLine(99, 10), 10, "beyond the last line");
    assert.strictEqual(scrollTargetLine(3.7, 10), 3, "whole lines");
    assert.strictEqual(scrollTargetLine(Number.NaN, 10), 1, "not a number");
    assert.strictEqual(scrollTargetLine(4, 0), 0, "no lines, no target");
});
