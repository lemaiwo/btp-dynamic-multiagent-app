/**
 * Markdown report rendering, carried over from `templates/admin.html`.
 *
 * The libraries are bundled rather than loaded from a CDN because Work Zone's
 * CSP blocks external hosts. The DOMPurify options below are reproduced
 * verbatim from the existing admin and are asserted by
 * `tests/test_report_sanitize.mjs` — changing them is a security change.
 */

interface MarkedLib { parse(md: string, options: { gfm: boolean }): string }
interface PurifyLib { sanitize(html: string, options: object): string }
interface MermaidLib {
    initialize(config: object): void;
    render(id: string, definition: string): Promise<{ svg: string }>;
}

declare global {
    interface Window {
        marked?: MarkedLib;
        DOMPurify?: PurifyLib;
        mermaid?: MermaidLib;
    }
}

const SANITIZE_HTML = {
    FORBID_TAGS: ["style", "form", "input", "button", "select", "textarea"],
    FORBID_ATTR: ["style"]
};

const SANITIZE_SVG = { USE_PROFILES: { svg: true, svgFilters: true } };

let mermaidLoading: Promise<MermaidLib> | null = null;

function loadScript(src: string): Promise<void> {
    return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error(`failed to load ${src}`));
        document.head.appendChild(script);
    });
}

/** Resolves a path inside this app, so it works under any host prefix. */
function assetUrl(relative: string): string {
    return sap.ui.require.toUrl(`com/infrabel/agentadmin/${relative}`);
}

export default {

    /** Loads marked + DOMPurify once; both are small enough to bundle eagerly. */
    async ensureLibraries(): Promise<void> {
        if (!window.marked) {
            await loadScript(assetUrl("vendor/marked.min.js"));
        }
        if (!window.DOMPurify) {
            await loadScript(assetUrl("vendor/purify.min.js"));
        }
    },

    /** Markdown to sanitized HTML. Returns an empty string for empty input. */
    renderMarkdown(bodyMd: string): string {
        if (!bodyMd) {
            return "";
        }
        const marked = window.marked;
        const purify = window.DOMPurify;
        if (!marked || !purify) {
            throw new Error("ReportRenderer.ensureLibraries() must run first");
        }
        return purify.sanitize(marked.parse(bodyMd, { gfm: true }), SANITIZE_HTML);
    },

    /**
     * Replaces ```mermaid fences inside an already-rendered container.
     *
     * A failure leaves the fence as a code block: the report stays readable,
     * which is the same degradation the existing admin chose.
     */
    async renderMermaid(container: HTMLElement, runId: string): Promise<void> {
        const fences = container.querySelectorAll("pre > code.language-mermaid");
        if (fences.length === 0) {
            return;
        }
        let mermaid: MermaidLib;
        try {
            mermaid = await this.loadMermaid();
        } catch {
            return;
        }
        const purify = window.DOMPurify as PurifyLib;

        for (let i = 0; i < fences.length; i++) {
            const code = fences[i];
            try {
                const { svg } = await mermaid.render(`md-${runId}-${i}`, code.textContent ?? "");
                const holder = document.createElement("div");
                holder.innerHTML = purify.sanitize(svg, SANITIZE_SVG);
                code.parentElement?.replaceWith(holder);
            } catch {
                const note = document.createElement("p");
                note.className = "agentAdminDiagramError";
                note.textContent = "Diagram could not be rendered.";
                code.parentElement?.after(note);
            }
        }
    },

    /** Mermaid is 3.5 MB, so it is fetched only when a diagram appears. */
    loadMermaid(): Promise<MermaidLib> {
        if (window.mermaid) {
            return Promise.resolve(window.mermaid);
        }
        if (mermaidLoading) {
            return mermaidLoading;
        }
        mermaidLoading = loadScript(assetUrl("vendor/mermaid.min.js")).then(() => {
            const mermaid = window.mermaid;
            if (!mermaid) {
                mermaidLoading = null;
                throw new Error("mermaid loaded but window.mermaid is missing");
            }
            // Mirrors templates/admin.html:1013 verbatim.
            mermaid.initialize({
                startOnLoad: false,
                securityLevel: "strict",
                theme: "dark",
                // foreignObject labels would be stripped whole by the SVG
                // sanitize profile below, leaving unlabeled shapes.
                htmlLabels: false,
                flowchart: { htmlLabels: false },
                // A malformed fence must not inject mermaid's error SVG
                // straight into document.body, outside our sanitized container.
                suppressErrorRendering: true
            });
            return mermaid;
        }).catch((error: Error) => {
            mermaidLoading = null;
            throw error;
        });
        return mermaidLoading;
    }
};
