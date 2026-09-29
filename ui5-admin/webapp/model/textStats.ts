/**
 * The character / line counter under the workflow editor's long text fields.
 *
 * Pure, so the wording is unit-testable; the controller only feeds it the
 * bound value through a formatter.
 */

export interface TextStats {
    chars: number;
    lines: number;
}

/** Characters and lines of `text`. Empty text is 0 characters and 0 lines;
 * otherwise every line break starts another line, a trailing one included,
 * which is what the cursor position in the field suggests too. */
export function textStats(text: string | undefined | null): TextStats {
    const value = String(text ?? "");
    if (!value.length) {
        return { chars: 0, lines: 0 };
    }
    return { chars: value.length, lines: value.split("\n").length };
}

/** "1,234 characters · 12 lines", with the singulars right. */
export function counterText(text: string | undefined | null): string {
    const { chars, lines } = textStats(text);
    const c = `${chars.toLocaleString("en-US")} character${chars === 1 ? "" : "s"}`;
    const l = `${lines} line${lines === 1 ? "" : "s"}`;
    return `${c} · ${l}`;
}
