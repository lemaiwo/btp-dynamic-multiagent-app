import { ensureMarkdown } from "com/agent/ide/model/vendor";
import { renderDocument } from "com/agent/ide/model/docView";
import { KITCHEN_SINK } from "./markdownVectors";

function root(html: string): HTMLElement {
    const div = document.createElement("div");
    div.innerHTML = html;
    assert0(div.children.length === 1, "one root");
    return div.firstElementChild as HTMLElement;
}

function assert0(ok: boolean, msg: string): void {
    if (!ok) {
        throw new Error(msg);
    }
}

const DOC = [
    "# Design",
    "",
    "Intro paragraph with **bold**.",
    "",
    "- one",
    "- two",
    "",
    "```abap",
    "DATA x TYPE i.",
    "",
    "x = 1.",
    "```",
    "",
    "| a | b |",
    "|---|---|",
    "| 1 | 2 |",
    "",
    "## Second",
    "",
    "> quoted",
    "",
    "---",
    "",
    "Last."
].join("\n");

QUnit.module("docView", {
    before: function () {
        return ensureMarkdown();
    }
});

QUnit.test("one root element, each top-level block numbered and focusable", function (assert) {
    const doc = renderDocument(DOC);
    const r = root(doc.html);
    assert.ok(r.classList.contains("ideMarkdown"), "markdown root class");
    const blocks = Array.from(r.children) as HTMLElement[];
    assert.strictEqual(doc.paragraphs, blocks.length, "paragraph count = top-level blocks");
    assert.strictEqual(doc.paragraphs, 9, "h1, p, ul, pre, table, h2, blockquote, hr, p");
    blocks.forEach((b, i) => {
        assert.strictEqual(b.getAttribute("data-para"), String(i), `block ${i} index`);
        assert.strictEqual(b.getAttribute("tabindex"), "0", `block ${i} keyboard reachable`);
    });
    const firstOf = (i: number): string => (blocks[i].firstElementChild?.tagName ?? "").toLowerCase();
    assert.deepEqual(
        [0, 1, 2, 3, 4, 5, 6, 7, 8].map(firstOf),
        ["h1", "p", "ul", "pre", "table", "h2", "blockquote", "hr", "p"],
        "a block keeps its element inside the anchor wrapper"
    );
});

QUnit.test("a code block with blank lines and a list with nested items stay one block each", function (assert) {
    const doc = renderDocument("- a\n  - a1\n  - a2\n- b\n\n```\nx\n\n\ny\n```");
    assert.strictEqual(doc.paragraphs, 2);
});

QUnit.test("indexes are stable: the same markdown gives the same anchors", function (assert) {
    assert.strictEqual(renderDocument(DOC).html, renderDocument(DOC).html);
});

QUnit.test("only the wrappers carry data-para; nested blocks do not", function (assert) {
    const r = root(renderDocument(DOC).html);
    const all = Array.from(r.querySelectorAll("[data-para]"));
    assert.strictEqual(all.length, 9);
    all.forEach((el) => assert.strictEqual(el.parentElement, r, "every anchor is a direct child of the root"));
});

QUnit.test("model-written data-para or tabindex cannot forge an anchor", function (assert) {
    const doc = renderDocument("<p data-para=\"99\" tabindex=\"3\">x</p>\n\nnext");
    const r = root(doc.html);
    assert.strictEqual(r.querySelectorAll("[data-para]").length, doc.paragraphs);
    assert.notOk(r.querySelector("[data-para='99']"), "the forged index is gone");
    assert.notOk(r.querySelector("[tabindex='3']"), "the forged tabindex is gone");
});

QUnit.test("the document is sanitised like any markdown", function (assert) {
    const html = renderDocument("<script>alert(1)</script>\n\n[x](javascript:alert(2)) <img src=x onerror=alert(3)>").html;
    assert.notOk(/<script|onerror|javascript:/i.test(html));
});

QUnit.test("empty or missing markdown is an empty root with no paragraphs", function (assert) {
    for (const md of ["", null, undefined]) {
        const doc = renderDocument(md);
        assert.strictEqual(doc.paragraphs, 0);
        assert.strictEqual(root(doc.html).children.length, 0);
    }
});

QUnit.test("top-level text outside any element is wrapped and numbered too", function (assert) {
    const doc = renderDocument("<b>bold</b> loose text");
    const r = root(doc.html);
    assert.ok(doc.paragraphs >= 1);
    assert.strictEqual(r.childNodes.length, doc.paragraphs, "no unnumbered loose node at the top level");
    assert.ok(r.textContent?.includes("loose text"));
});

/** Every element with its sorted attributes (name=value), in document order. */
function signature(html: string): string[] {
    const div = document.createElement("div");
    div.innerHTML = html;
    return Array.from(div.querySelectorAll("*")).map((el) => {
        const attrs = Array.from(el.attributes).map((a) => `${a.name}=${a.value}`).sort();
        return `${el.namespaceURI ?? ""} ${el.nodeName}[${attrs.join(" ")}]`;
    });
}

QUnit.test("kitchen sink: the returned html re-parses to the same elements and attributes", function (assert) {
    const html = renderDocument(KITCHEN_SINK).html;
    const first = signature(html);
    const div = document.createElement("div");
    div.innerHTML = html;
    const second = signature(div.innerHTML);
    assert.ok(first.length > 10, "a real document");
    assert.deepEqual(second, first, "no element or attribute appears or changes on re-parse");
    assert.strictEqual(div.innerHTML, html, "serialisation is a fixed point");
});
