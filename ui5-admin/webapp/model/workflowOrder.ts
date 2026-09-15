import { branchOrder } from "com/agent/admin/model/processFlowGraph";
import { WorkflowBranch } from "com/agent/admin/service/types";

/**
 * Row ordering for the workflow editor's Steps table.
 *
 * A step carries no link to the step before it: the runner derives the order
 * from `branch_key` plus a `position` numbered 1..n *within that group*, and
 * the editor derives `position` from the order the rows happen to sit in. So
 * "run this one first" is expressed by moving a row, and a row only competes
 * with the other rows in its own group.
 *
 * Keeping the table grouped is what makes a move button honest. Ungrouped,
 * the row above a branch step can belong to the main line, and swapping the
 * two changes nothing at all -- both keep the position they already had, and
 * the flow graph does not move. Grouped, every neighbour shares the group, so
 * every swap is a real reordering.
 *
 * Group order comes from `branchOrder` so the table and the flow preview
 * cannot disagree about which branch comes first.
 */

/** The little a row needs to expose to be ordered; `WorkflowStep` satisfies
 * it, and so does the editor's row type that decorates it with dropdowns. */
interface Grouped {
    branch_key: string | null;
}

/** Main-line rows first, then one contiguous block per branch. Rows keep
 * their relative order inside a block, so grouping an already-edited table
 * never silently reorders the steps an operator arranged. */
export function groupSteps<T extends Grouped>(steps: T[], branches: WorkflowBranch[]): T[] {
    const keys = branchOrder(branches, steps);
    const rank = new Map<string, number>(keys.map((key, index) => [key, index + 1]));
    const unknown = keys.length + 1;

    return steps
        .map((step, index) => ({ step, index }))
        .sort((a, b) => {
            const ra = a.step.branch_key === null ? 0 : rank.get(a.step.branch_key) ?? unknown;
            const rb = b.step.branch_key === null ? 0 : rank.get(b.step.branch_key) ?? unknown;
            // The index tie-break is what keeps the sort stable per group.
            return ra - rb || a.index - b.index;
        })
        .map((entry) => entry.step);
}

/** Whether the row at `index` may swap with the one `delta` away: it has to
 * exist and belong to the same group. False is what greys the button out. */
export function canMoveWithinGroup(steps: Grouped[], index: number, delta: number): boolean {
    const target = index + delta;
    if (index < 0 || index >= steps.length) {
        return false;
    }
    if (target < 0 || target >= steps.length) {
        return false;
    }
    return (steps[index].branch_key ?? null) === (steps[target].branch_key ?? null);
}

/** The row at `index` exchanged with the one `delta` away, as a new array.
 * Out of range returns a copy rather than throwing: a disabled button that
 * still fires should do nothing, not break the page. */
export function swap<T>(list: T[], index: number, delta: number): T[] {
    const next = list.slice();
    const target = index + delta;
    if (index < 0 || index >= list.length || target < 0 || target >= list.length) {
        return next;
    }
    const held = next[index];
    next[index] = next[target];
    next[target] = held;
    return next;
}
