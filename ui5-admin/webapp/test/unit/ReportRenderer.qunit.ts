import ReportRenderer from "com/infrabel/agentadmin/service/ReportRenderer";

// renderMarkdown() deliberately throws unless marked and DOMPurify are loaded,
// so the suite must load them once up front. Keeping the guard is correct — it
// catches a real misuse — which is why the test adapts rather than the code.
QUnit.module("ReportRenderer.renderMarkdown", {
    before: async function () {
        await ReportRenderer.ensureLibraries();
    }
});

QUnit.test("renders GitHub-flavoured markdown tables", function (assert) {
    const html = ReportRenderer.renderMarkdown("| a | b |\n|---|---|\n| 1 | 2 |");
    assert.ok(html.indexOf("<table") > -1, "produces a table");
    assert.ok(html.indexOf("<td>1</td>") > -1, "produces cells");
});

QUnit.test("strips script tags", function (assert) {
    const html = ReportRenderer.renderMarkdown("hello <script>alert(1)</script>");
    assert.strictEqual(html.indexOf("<script"), -1, "no script element survives");
});

QUnit.test("strips the forbidden interactive tags", function (assert) {
    const html = ReportRenderer.renderMarkdown(
        "<form><input name='x'><button>go</button><select></select><textarea></textarea></form>"
    );
    ["<form", "<input", "<button", "<select", "<textarea"].forEach((tag) => {
        assert.strictEqual(html.indexOf(tag), -1, `${tag} is removed`);
    });
});

QUnit.test("strips style attributes and style elements", function (assert) {
    const html = ReportRenderer.renderMarkdown("<p style='color:red'>x</p><style>p{}</style>");
    assert.strictEqual(html.indexOf("style="), -1, "no style attribute survives");
    assert.strictEqual(html.indexOf("<style"), -1, "no style element survives");
});

QUnit.test("strips inline event handlers", function (assert) {
    const html = ReportRenderer.renderMarkdown("<img src=x onerror='alert(1)'>");
    assert.strictEqual(html.indexOf("onerror"), -1, "no event handler survives");
});

QUnit.test("an empty report renders as empty, not as 'undefined'", function (assert) {
    assert.strictEqual(ReportRenderer.renderMarkdown(""), "");
});
