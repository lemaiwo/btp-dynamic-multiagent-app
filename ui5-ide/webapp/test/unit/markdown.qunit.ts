import { ensureMarkdown } from "com/agent/ide/model/vendor";
import { renderMarkdown } from "com/agent/ide/model/markdown";

QUnit.module("markdown", {
    before: function () {
        return ensureMarkdown();
    }
});

QUnit.test("markdown renders to HTML inside one root element", function (assert) {
    const html = renderMarkdown("# Design\n\n- one\n- two");
    assert.ok(html.startsWith("<div") && html.endsWith("</div>"), "one root element for core:HTML");
    assert.ok(html.includes("<h1>Design</h1>"));
    assert.ok(html.includes("<li>one</li>"));
});

QUnit.test("scripts, event handlers and javascript: links are stripped", function (assert) {
    const html = renderMarkdown("Hi <script>alert(1)</script><img src=x onerror=\"alert(2)\"> [x](javascript:alert(3))");
    assert.notOk(/<script/i.test(html), "no script tag");
    assert.notOk(/onerror/i.test(html), "no event handler");
    assert.notOk(/javascript:/i.test(html), "no javascript: URL");
});

QUnit.test("style tags and attributes are stripped, as in the admin's report renderer", function (assert) {
    const html = renderMarkdown("<style>body{display:none}</style><p style=\"color:red\">x</p>");
    assert.notOk(/<style/i.test(html));
    assert.notOk(/style=/i.test(html));
});

QUnit.test("empty markdown is an empty root", function (assert) {
    assert.strictEqual(renderMarkdown(""), "<div class=\"ideMarkdown\"></div>");
});

QUnit.test("remote images are dropped, inline data images stay", function (assert) {
    const html = renderMarkdown("![t](https://tracker.example.com/p.png) ![i](data:image/png;base64,iVBORw0KGgo=)");
    assert.notOk(/tracker\.example\.com/.test(html), "no remote image source");
    assert.ok(html.includes("data:image/png"), "a data: image is kept");
});
