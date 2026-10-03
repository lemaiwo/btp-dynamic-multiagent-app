/** The arguments of Ace's `Range(startRow, startColumn, endRow, endColumn)`. */
export type AceRangeArgs = [number, number, number, number];

/**
 * The line of the opened source a finding points at: one-based, cut to a
 * whole number and clamped into the source. `null` when the finding has no
 * line (a method include, plan Q9) or the source has no lines to mark.
 */
export function targetLine(line: number | null, lineCount: number): number | null {
    if (typeof line !== "number" || !Number.isFinite(line) || !Number.isFinite(lineCount) || lineCount < 1) {
        return null;
    }
    return Math.min(Math.max(1, Math.trunc(line)), Math.trunc(lineCount));
}

/** The Ace range of the whole one-based `line`, for a `fullLine` marker (Ace rows are zero-based). */
export function markerRange(line: number): AceRangeArgs {
    const row = line - 1;
    return [row, 0, row, Infinity];
}
