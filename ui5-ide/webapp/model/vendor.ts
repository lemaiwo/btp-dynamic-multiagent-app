/**
 * The vendored browser libraries (see vendor/README.md), loaded on demand
 * with a <script> tag. They are UMD builds: with no AMD `define` on the page
 * (UI5's loader does not expose one) each sets a window global.
 *
 * Bundled rather than taken from a CDN because a Work Zone CSP blocks
 * external hosts, the reasoning of ui5-admin's ReportRenderer.
 */

/** The part of jsdiff 5 this app uses. */
export interface DiffChange {
    value: string;
    count?: number;
    added?: boolean;
    removed?: boolean;
}
export interface DiffLib {
    /** Undefined when `maxEditLength` is exceeded. */
    diffLines(oldStr: string, newStr: string, options?: { maxEditLength?: number }): DiffChange[] | undefined;
}
export interface MarkedLib { parse(md: string, options: { gfm: boolean; async?: false }): string }
export interface PurifyAttrData { attrName: string; attrValue: string; keepAttr: boolean }
export interface PurifyLib {
    sanitize(html: string, options: object): string;
    addHook(entryPoint: "uponSanitizeAttribute", hook: (node: Element, data: PurifyAttrData) => void): void;
}

declare global {
    interface Window {
        Diff?: DiffLib;
        marked?: MarkedLib;
        DOMPurify?: PurifyLib;
    }
}

const loading: Record<string, Promise<void>> = {};

function loadScript(file: string): Promise<void> {
    loading[file] ??= new Promise<void>((resolve, reject) => {
        const script = document.createElement("script");
        // Resolved through the UI5 loader, so it works under any host prefix.
        script.src = sap.ui.require.toUrl(`com/agent/ide/vendor/${file}`);
        script.onload = () => resolve();
        script.onerror = () => {
            delete loading[file];
            reject(new Error(`failed to load ${file}`));
        };
        document.head.appendChild(script);
    });
    return loading[file];
}

/** Loads jsdiff once; resolves when `window.Diff` is there. */
export async function ensureDiff(): Promise<void> {
    if (!window.Diff) {
        await loadScript("diff.min.js");
    }
}

/** Loads marked and DOMPurify once. */
export async function ensureMarkdown(): Promise<void> {
    await Promise.all([
        window.marked ? undefined : loadScript("marked.min.js"),
        window.DOMPurify ? undefined : loadScript("purify.min.js")
    ]);
}
