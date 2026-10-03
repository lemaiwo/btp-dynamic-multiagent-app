/**
 * Markdown (design, plan and review artifacts) to sanitized HTML.
 *
 * The DOMPurify options are those of ui5-admin's ReportRenderer: no style,
 * no form controls. Images keep only an inline `data:image/` source: a
 * remote one in model-written markdown would be a tracking request (and CSP
 * noise). `ensureMarkdown()` must have run.
 */

const SANITIZE_HTML = {
    FORBID_TAGS: ["style", "form", "input", "button", "select", "textarea"],
    FORBID_ATTR: ["style"]
};

const hooked = new WeakSet<object>();

function dropRemoteImages(purify: NonNullable<Window["DOMPurify"]>): void {
    if (hooked.has(purify)) {
        return;
    }
    hooked.add(purify);
    purify.addHook("uponSanitizeAttribute", (node, data) => {
        if (node.nodeName === "IMG" && data.attrName === "src" && !/^data:image\//i.test(data.attrValue)) {
            data.keepAttr = false;
        }
    });
}

/** One root element, as a `core:HTML` host needs. */
export function renderMarkdown(md: string | null | undefined): string {
    const marked = window.marked;
    const purify = window.DOMPurify;
    if (!marked || !purify) {
        throw new Error("ensureMarkdown() must run before renderMarkdown()");
    }
    dropRemoteImages(purify);
    const html = md ? purify.sanitize(marked.parse(md, { gfm: true, async: false }), SANITIZE_HTML) : "";
    return `<div class="ideMarkdown">${html}</div>`;
}
