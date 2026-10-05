/**
 * Markdown (design, plan and review artifacts) to sanitized HTML.
 *
 * The DOMPurify options are those of ui5-admin's ReportRenderer: no style,
 * no form controls. On top of that, for model-written text:
 * - nothing in the output may make the browser fetch a remote resource
 *   when the HTML is shown (a zero-click channel for a prompt-injected
 *   model): HTML profile only (no SVG/MathML), media and embedding elements
 *   forbidden, `srcset`/`poster`/`background`/`ping`/`cite`/... forbidden,
 *   `src` kept only on `<img>` with an inline `data:image/` value, and any
 *   other attribute whose value looks like a URL (`http…`, `//…`, `url(`)
 *   dropped. Only `<a href>` may hold a remote URL, and it is not fetched
 *   until clicked;
 * - a link keeps its `href` only when it is a plain absolute `https:` URL
 *   with a host and no credentials; every other scheme, a relative or
 *   protocol-relative URL, and any URL with whitespace or a control
 *   character in it lose the `href` (the text stays). A kept link opens in a
 *   new tab without opener or referrer and shows its real host after the
 *   text, so link text cannot pass for another site;
 * - `href` on anything but `<a>`, `xlink:href`, `data-*` and `tabindex` are
 *   removed: the document view sets its own `data-para`/`tabindex` anchors;
 * - `id` and `name` are removed (model markup must not collide with view
 *   ids such as `--artifactTitle`), and `class` is kept only as a plain
 *   `language-*` class on `<code>` (a fenced block's language) or on the
 *   elements this renderer adds itself: model markup cannot pick up app
 *   CSS classes (`ideDocBlock`, `sapUiHidden`, ...).
 *
 * `ensureMarkdown()` must have run.
 */

const SANITIZE_HTML = {
    USE_PROFILES: { html: true },
    FORBID_TAGS: [
        "style", "form", "input", "button", "select", "textarea",
        "audio", "video", "source", "track", "picture", "object", "embed", "iframe", "frame", "frameset",
        "link", "base", "meta", "svg", "math", "image", "use", "feimage", "applet", "portal", "noscript", "template"
    ],
    FORBID_ATTR: [
        "style", "tabindex", "srcset", "sizes", "poster", "background", "ping", "cite", "longdesc", "lowsrc",
        "dynsrc", "data", "codebase", "action", "formaction", "manifest", "xlink:href", "id", "name"
    ],
    ALLOW_DATA_ATTR: false
};

/** An attribute value the browser could resolve to a remote resource. */
const REMOTE_VALUE = /^\s*(?:https?:|\/\/|\\\\)|url\s*\(/i;

const hooked = new WeakSet<object>();

/** Elements this module creates while sanitising: the only ones whose `class` is ours. */
const ownNodes = new WeakSet<Node>();

/** The class a model-written `<code>` may keep: one language name, as marked writes it. */
const LANGUAGE_CLASS = /^language-[A-Za-z0-9_+-]{1,40}$/;

/**
 * The `https:` URL a link may keep, or null. The raw attribute value is
 * checked before URL parsing: the parser (like the browser) would strip
 * leading blanks, tabs and newlines and accept `https:host` or backslashes,
 * which is exactly the obfuscation to refuse.
 */
export function safeHttpsUrl(raw: string | null | undefined): URL | null {
    if (typeof raw !== "string" || !/^https:\/\/[^\s/\\]/i.test(raw)) {
        return null;
    }
    // eslint-disable-next-line no-control-regex
    if (/[\u0000- \u007f-\u009f\\]/.test(raw)) {
        return null;
    }
    let url: URL;
    try {
        url = new URL(raw);
    } catch {
        return null;
    }
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password) {
        return null;
    }
    return url;
}

function secureLink(node: Element): void {
    if (node.hasAttribute("xlink:href")) {
        node.removeAttribute("xlink:href");
    }
    if (node.removeAttributeNS) {
        node.removeAttributeNS("http://www.w3.org/1999/xlink", "href");
    }
    if (node.nodeName !== "A" && !node.hasAttribute("href")) {
        return;
    }
    // HTML <a> only (an SVG <a> has a lower-case nodeName); `href` anywhere else goes.
    const url = node.nodeName === "A" ? safeHttpsUrl(node.getAttribute("href")) : null;
    if (!url) {
        node.removeAttribute("href");
        node.removeAttribute("target");
        node.removeAttribute("rel");
        return;
    }
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
    const host = node.ownerDocument.createElement("span");
    host.className = "ideLinkHost";
    ownNodes.add(host);
    host.textContent = `(${url.host})`;
    node.appendChild(node.ownerDocument.createTextNode(" "));
    node.appendChild(host);
}

/**
 * The hooks go on the page's single `window.DOMPurify`, so every later
 * caller of that instance (this app or a library on the same page)
 * inherits them: stricter `src`/URL-valued attributes, and every kept
 * `https:` link gets `target`/`rel` and the host span. A future caller that
 * needs other rules must use its own `DOMPurify(window)` instance.
 * Installed once per instance.
 */
function installHooks(purify: NonNullable<Window["DOMPurify"]>): void {
    if (hooked.has(purify)) {
        return;
    }
    hooked.add(purify);
    purify.addHook("uponSanitizeAttribute", (node, data) => {
        if (data.attrName === "class") {
            data.keepAttr = ownNodes.has(node) || (node.nodeName === "CODE" && LANGUAGE_CLASS.test(data.attrValue));
        } else if (data.attrName === "src") {
            // `src` only as an inline image; on anything else (or remote) it would fetch.
            if (!(node.nodeName === "IMG" && /^data:image\//i.test(data.attrValue))) {
                data.keepAttr = false;
            }
        } else if (!(node.nodeName === "A" && data.attrName === "href") && REMOTE_VALUE.test(data.attrValue)) {
            data.keepAttr = false;
        }
    });
    purify.addHook("afterSanitizeAttributes", (node) => {
        if (node.nodeType === 1) {
            secureLink(node);
        }
    });
}

/**
 * The sanitized markdown as a detached fragment (no serialise/re-parse
 * between sanitising and the caller's own DOM work).
 */
export function markdownFragment(md: string | null | undefined): DocumentFragment {
    const marked = window.marked;
    const purify = window.DOMPurify;
    if (!marked || !purify) {
        throw new Error("ensureMarkdown() must run before renderMarkdown()");
    }
    installHooks(purify);
    if (!md) {
        return document.createDocumentFragment();
    }
    const out: unknown = purify.sanitize(
        marked.parse(md, { gfm: true, async: false }), { ...SANITIZE_HTML, RETURN_DOM_FRAGMENT: true }
    );
    // Input that leaves the parsed document without a body (e.g. `<frameset>`)
    // gives no node: fail closed to nothing rather than throw.
    return out instanceof Node && out.nodeType === Node.DOCUMENT_FRAGMENT_NODE
        ? out as DocumentFragment
        : document.createDocumentFragment();
}

/** One root element, as a `core:HTML` host needs. */
export function renderMarkdown(md: string | null | undefined): string {
    const root = document.createElement("div");
    root.className = "ideMarkdown";
    root.appendChild(markdownFragment(md));
    return root.outerHTML;
}
