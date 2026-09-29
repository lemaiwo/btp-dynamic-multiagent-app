/**
 * Pure helpers behind the "Last runs" panel on the agent and workflow detail
 * pages, and the dirty check their Refresh buttons make before discarding
 * the form. Kept out of the controllers so the wording and the comparison
 * are unit-testable.
 */

/** How many runs the detail pages ask for. Small on purpose: the full list
 * lives on the Runs pages, this panel answers "did the last one work?". */
export const LAST_RUNS_LIMIT = 10;

/** Delays after a manual run-now, in ms, at which the panel is reloaded:
 * once so the new run shows up as running, once more so a short run shows
 * its outcome without anyone pressing Refresh. */
export const RUN_REFRESH_DELAYS_MS = [1000, 5000];

/**
 * The count shown in the panel's header. `count >= limit` reads as "10+":
 * the page asked for at most `limit` rows, so a full page says nothing
 * about how many more there are.
 */
export function runsCountLabel(count: number, limit: number = LAST_RUNS_LIMIT): string {
    if (!count || count <= 0) {
        return "No runs";
    }
    if (count >= limit) {
        return `${limit}+ runs`;
    }
    return count === 1 ? "1 run" : `${count} runs`;
}

/**
 * A stable serialisation for comparing a form payload against the snapshot
 * taken when it was loaded: object keys are sorted so two payloads built
 * from the same data in a different order still compare equal, and
 * `undefined` values (which JSON.stringify drops anyway) are treated the
 * same as a missing key.
 */
export function canonical(value: unknown): string {
    return JSON.stringify(sortKeys(value));
}

function sortKeys(value: unknown): unknown {
    if (Array.isArray(value)) {
        return value.map(sortKeys);
    }
    if (value && typeof value === "object") {
        const source = value as Record<string, unknown>;
        const out: Record<string, unknown> = {};
        Object.keys(source).sort().forEach((key) => {
            if (source[key] !== undefined) {
                out[key] = sortKeys(source[key]);
            }
        });
        return out;
    }
    return value;
}

/** Whether `current` differs from the `snapshot` taken at load, by value. */
export function isDirty(snapshot: string | undefined, current: unknown): boolean {
    if (snapshot === undefined) {
        return false;
    }
    return canonical(current) !== snapshot;
}
