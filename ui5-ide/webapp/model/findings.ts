import type { DiagnoseFinding } from "../service/types";

/**
 * Folds one `finding` SSE frame (plan 1c §1.3) into the findings list,
 * newest first as `GET findings` answers it. A known finding (same id, or
 * the server's unique key `kind` + `ref_id`) is replaced in place; a new one
 * goes to the front. The explorer lists the metadata columns only, whatever
 * else a finding carries.
 */
export function upsertFinding(list: DiagnoseFinding[], finding: DiagnoseFinding): DiagnoseFinding[] {
    if (!finding || !finding.id || !finding.kind) {
        return list;
    }
    const index = list.findIndex((f) => f.id === finding.id || (f.kind === finding.kind && f.ref_id === finding.ref_id));
    if (index < 0) {
        return [finding, ...list];
    }
    const next = list.slice();
    next[index] = finding;
    return next;
}
