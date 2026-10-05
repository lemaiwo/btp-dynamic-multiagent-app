import { markdownFragment } from "./markdown";

/**
 * A document (design, plan, note, review, report) rendered for reading and
 * commenting: the sanitized markdown of `markdown.ts`, with every top-level
 * block wrapped in `<div class="ideDocBlock" data-para="i" tabindex="0">`.
 * The 0-based block index `i` is a document comment's `paragraph` anchor
 * (contract §1.1), so it depends only on the markdown: a heading, a list
 * (with its nested items), a code block, a table, a quote and a rule are one
 * block each. `tabindex="0"` keeps every block keyboard reachable, so a
 * comment can be added without a mouse.
 *
 * The anchor attributes are set here, after sanitizing; model-written
 * `data-*` and `tabindex` are stripped by `markdown.ts`, so an index cannot
 * be forged from the content.
 */

export interface RenderedDocument {
    /** One root element, as a `core:HTML` host needs. */
    html: string;
    /** Number of top-level blocks: valid `paragraph` anchors are 0..paragraphs-1. */
    paragraphs: number;
}

export function renderDocument(md: string | null | undefined): RenderedDocument {
    const fragment = markdownFragment(md);
    const root = document.createElement("div");
    root.className = "ideMarkdown ideDocument";
    let paragraphs = 0;
    // Consecutive loose nodes (text or inline elements outside any block) share one block.
    let loose: HTMLDivElement | null = null;
    const block = (): HTMLDivElement => {
        const div = document.createElement("div");
        div.className = "ideDocBlock";
        div.setAttribute("data-para", String(paragraphs++));
        div.setAttribute("tabindex", "0");
        root.appendChild(div);
        return div;
    };
    for (const node of Array.from(fragment.childNodes)) {
        if (node.nodeType === Node.TEXT_NODE && !(node.textContent ?? "").trim()) {
            continue;
        }
        if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.TEXT_NODE) {
            continue;
        }
        if (node.nodeType === Node.ELEMENT_NODE && isBlock(node as Element)) {
            loose = null;
            block().appendChild(node);
        } else {
            loose ??= block();
            loose.appendChild(node);
        }
    }
    return { html: root.outerHTML, paragraphs };
}

const INLINE = new Set([
    "A", "ABBR", "B", "BDI", "BDO", "BR", "CITE", "CODE", "DATA", "DEL", "DFN", "EM", "I", "IMG", "INS", "KBD",
    "MARK", "Q", "S", "SAMP", "SMALL", "SPAN", "STRONG", "SUB", "SUP", "TIME", "U", "VAR", "WBR"
]);

function isBlock(el: Element): boolean {
    return !INLINE.has(el.nodeName);
}
