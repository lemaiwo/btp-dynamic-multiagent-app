import { ensureMarkdown } from "com/agent/ide/model/vendor";
import { renderMarkdown } from "com/agent/ide/model/markdown";
import { FETCH_VECTORS, KITCHEN_SINK, R } from "./markdownVectors";

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

// --- U5: link safety in model-written markdown --------------------------------

function anchors(html: string): HTMLAnchorElement[] {
    const div = document.createElement("div");
    div.innerHTML = html;
    return Array.from(div.querySelectorAll("a"));
}

QUnit.test("an https: link keeps its href, opens in a new tab without opener and shows its host", function (assert) {
    const links = anchors(renderMarkdown("See [the docs](https://example.com/x?y=1)."));
    assert.strictEqual(links.length, 1);
    const a = links[0];
    assert.strictEqual(a.getAttribute("href"), "https://example.com/x?y=1");
    assert.strictEqual(a.getAttribute("target"), "_blank");
    assert.strictEqual(a.getAttribute("rel"), "noopener noreferrer");
    const host = a.querySelector("span.ideLinkHost");
    assert.ok(host, "a host span");
    assert.strictEqual(host?.textContent, "(example.com)");
    assert.strictEqual(a.lastElementChild, host, "the host span is trailing");
});

QUnit.test("the host shown is the URL's real host, port included", function (assert) {
    const a = anchors(renderMarkdown("[x](https://evil.example.org:8443/a)"))[0];
    assert.strictEqual(a.querySelector(".ideLinkHost")?.textContent, "(evil.example.org:8443)");
});

QUnit.test("a raw HTML https link gets the same treatment and loses a model-set target/rel", function (assert) {
    const a = anchors(renderMarkdown("<a href=\"https://example.com\" target=\"_self\" rel=\"opener\">x</a>"))[0];
    assert.strictEqual(a.getAttribute("href"), "https://example.com");
    assert.strictEqual(a.getAttribute("target"), "_blank");
    assert.strictEqual(a.getAttribute("rel"), "noopener noreferrer");
});

const UNSAFE_HREFS: [string, string][] = [
    ["javascript:", "javascript:alert(1)"],
    ["mixed-case javascript:", "JaVaScRiPt:alert(1)"],
    ["http:", "http://example.com/x"],
    ["data:", "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="],
    ["vbscript:", "vbscript:msgbox(1)"],
    ["mailto:", "mailto:jane.doe@example.com"],
    ["relative", "notes/x.md"],
    ["root-relative", "/ide/api/sessions"],
    ["fragment", "#top"],
    ["protocol-relative", "//evil.example.com/x"],
    ["backslash protocol-relative", "\\\\evil.example.com/x"],
    ["https without slashes", "https:evil.example.com"],
    ["https with one slash", "https:/evil.example.com"],
    ["https with backslashes", "https:\\\\evil.example.com"],
    ["https with credentials", "https://user:pw@evil.example.com/"],
    ["leading space before javascript:", " javascript:alert(1)"],
    ["tab inside the scheme", "java\tscript:alert(1)"],
    ["newline inside the scheme", "java\nscript:alert(1)"],
    ["NUL inside the scheme", "java\u0000script:alert(1)"],
    ["entity-encoded tab", "java&#9;script:alert(1)"],
    ["entity-encoded colon", "javascript&colon;alert(1)"],
    ["control character before https", "\u0001https://example.com"],
    ["tab inside https", "ht\ttps://example.com"]
];

UNSAFE_HREFS.forEach(([label, href]) => {
    QUnit.test(`raw HTML link with a ${label} href loses its href`, function (assert) {
        const raw = href.replace(/"/g, "&quot;");
        const links = anchors(renderMarkdown(`<a href="${raw}">click</a>`));
        links.forEach((a) => {
            assert.notOk(a.hasAttribute("href"), `no href (was ${JSON.stringify(href)})`);
            assert.notOk(a.querySelector(".ideLinkHost"), "no host span on an unsafe link");
            assert.notOk(a.hasAttribute("target"), "no target");
        });
        assert.ok(true, `${links.length} anchors checked`);
    });
});

QUnit.test("markdown links with unsafe schemes lose their href", function (assert) {
    const md = [
        "[a](javascript:alert(1))", "[b](http://example.com)", "[c](data:text/html,x)",
        "[d](relative/path)", "[e](//evil.example.com)", "[f](JAVASCRIPT:alert(1))", "<http://example.com>"
    ].join(" ");
    const links = anchors(renderMarkdown(md));
    assert.ok(links.length >= 6, "the anchors are rendered");
    links.forEach((a) => assert.notOk(a.hasAttribute("href"), `${a.textContent} has no href`));
});

QUnit.test("link text survives when the href is dropped", function (assert) {
    const html = renderMarkdown("[keep me](javascript:alert(1))");
    assert.ok(html.includes("keep me"));
});

QUnit.test("non-anchor href and xlink:href are removed", function (assert) {
    const html = renderMarkdown(
        "<map><area href=\"https://example.com\" alt=\"x\"></map>"
        + "<svg><a xlink:href=\"javascript:alert(1)\"><text>x</text></a></svg>"
    );
    assert.notOk(/javascript:/i.test(html), "no javascript: URL");
    const div = document.createElement("div");
    div.innerHTML = html;
    assert.notOk(div.querySelector("area[href]"), "area has no href");
    assert.notOk(/xlink:href/i.test(html), "no xlink:href");
});

QUnit.test("model-written data-* and tabindex attributes are stripped", function (assert) {
    const html = renderMarkdown("<p data-para=\"7\" tabindex=\"5\" data-x=\"1\">x</p>");
    assert.notOk(/data-para|data-x/.test(html), "no data attributes");
    assert.notOk(/tabindex/.test(html), "no tabindex");
});

QUnit.test("surrounding blanks are trimmed by the sanitizer: a padded https link is the same https link", function (assert) {
    // DOMPurify trims attribute values (as browsers do for href), so the hook sees the clean URL;
    // the scheme check is unaffected (" javascript:" is in the refused list above).
    const a = anchors(renderMarkdown("<a href=\" https://example.com \">x</a>"))[0];
    assert.strictEqual(a.getAttribute("href"), "https://example.com");
    assert.strictEqual(a.querySelector(".ideLinkHost")?.textContent, "(example.com)");
});

// --- U5 fix round 1: no remote fetch from model-written markdown ---------------

/** Attributes in the output that could make the browser fetch something, other than `<a href>`. */
function fetchingAttributes(html: string): string[] {
    const div = document.createElement("div");
    div.innerHTML = html;
    const found: string[] = [];
    div.querySelectorAll("*").forEach((el) => {
        Array.from(el.attributes).forEach((attr) => {
            const isLink = el.nodeName === "A" && attr.name === "href";
            const remote = /^\s*(https?:|\/\/|\\\\)/i.test(attr.value) || /url\s*\(/i.test(attr.value);
            if (!isLink && remote) {
                found.push(`${el.nodeName.toLowerCase()}[${attr.name}]`);
            }
            if (["src", "srcset", "poster", "background", "ping", "data", "action", "formaction"].includes(attr.name)
                && !(el.nodeName === "IMG" && attr.name === "src" && /^data:image\//i.test(attr.value))) {
                found.push(`${el.nodeName.toLowerCase()}[${attr.name}]`);
            }
        });
    });
    return found;
}

FETCH_VECTORS.forEach(([label, raw, goneTags]) => {
    QUnit.test(`no remote fetch survives: ${label}`, function (assert) {
        const html = renderMarkdown(raw);
        assert.deepEqual(fetchingAttributes(html), [], "no fetching attribute");
        const div = document.createElement("div");
        div.innerHTML = html;
        goneTags.forEach((tag) => assert.notOk(div.querySelector(tag), `no <${tag}>`));
    });
});

QUnit.test("an inline data: image keeps its src; srcset does not survive even beside it", function (assert) {
    const div = document.createElement("div");
    div.innerHTML = renderMarkdown(`<img src="data:image/png;base64,iVBORw0KGgo=" srcset="${R}/a.png 2x">`);
    const img = div.querySelector("img");
    assert.ok(img?.getAttribute("src")?.startsWith("data:image/png"));
    assert.notOk(img?.hasAttribute("srcset"));
});

QUnit.test("kitchen sink: no attribute starting with http (or url()) survives except <a href>", function (assert) {
    const html = renderMarkdown(KITCHEN_SINK);
    assert.deepEqual(fetchingAttributes(html), []);
    const div = document.createElement("div");
    div.innerHTML = html;
    const httpAttrs: string[] = [];
    div.querySelectorAll("*").forEach((el) => Array.from(el.attributes).forEach((a) => {
        if (/^\s*http/i.test(a.value) && !(el.nodeName === "A" && a.name === "href")) {
            httpAttrs.push(`${el.nodeName}[${a.name}]`);
        }
    }));
    assert.deepEqual(httpAttrs, []);
    assert.ok(div.querySelector("a[href='https://example.com/x']"), "the https link survives");
    assert.ok(div.querySelector("img[src^='data:image/png']"), "the inline image survives");
});

QUnit.test("model markup cannot set id, name or app classes; code keeps its language class, links keep the host span", function (assert) {
    const html = renderMarkdown('<p id="artifactTitle" name="x" class="sapUiHidden ideDocBlock">t</p>\n\n```abap\nx\n```\n\n[a](https://example.com/)\n\n<a id="y" name="z" href="https://example.com/">b</a>');
    const root = new DOMParser().parseFromString(html, "text/html").body.firstElementChild!;
    assert.strictEqual(root.querySelectorAll("[id], [name]").length, 0, "no id or name survives");
    assert.notOk(root.querySelector("p")!.hasAttribute("class"), "no class from the model");
    assert.strictEqual(root.querySelector("code")!.getAttribute("class"), "language-abap", "a fenced block's language stays");
    assert.strictEqual(root.querySelectorAll(".ideLinkHost").length, 2, "the renderer's own class stays");
    assert.strictEqual(root.getAttribute("class"), "ideMarkdown");
});

QUnit.test("a code class that is not a plain language name is dropped", function (assert) {
    const html = renderMarkdown('<code class="language-x ideDocBlock">a</code>');
    assert.notOk(html.includes("ideDocBlock"));
    assert.notOk(html.includes("language-x"), "with a second class, not even the language class is kept");
    assert.notOk(/<code[^>]*class=/.test(html), "the code element has no class at all");
});

QUnit.test("model markup cannot forge the renderer's own link host span or style a pre", function (assert) {
    const html = renderMarkdown('<span class="ideLinkHost">(bank.example)</span>\n\n<pre class="ideDocBlock language-abap">x</pre>');
    const root = new DOMParser().parseFromString(html, "text/html").body.firstElementChild!;
    assert.strictEqual(root.querySelectorAll(".ideLinkHost").length, 0, "a forged host span loses its class");
    assert.ok(root.textContent!.includes("(bank.example)"), "its text stays as text");
    assert.notOk(root.querySelector("pre")!.hasAttribute("class"), "class on <pre> is stripped");
});
