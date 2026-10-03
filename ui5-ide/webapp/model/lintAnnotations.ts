import type { Finding } from "../service/types";

/** An Ace editor annotation (gutter marker); `row` is zero-based. */
export interface AceAnnotation {
    row: number;
    column: number;
    type: "error" | "warning" | "info";
    text: string;
}

function aceType(severity: string): AceAnnotation["type"] {
    const s = (severity || "").toLowerCase();
    if (s.startsWith("e")) {
        return "error";
    }
    if (s.startsWith("w")) {
        return "warning";
    }
    return "info";
}

/**
 * SAPLint findings (`line` one-based, plan §1.2) as Ace annotations, for
 * `getInternalEditorInstance().getSession().setAnnotations(...)`.
 */
export function toAceAnnotations(findings: Finding[] | null | undefined): AceAnnotation[] {
    return (findings ?? []).map((f) => ({
        row: Math.max(0, (Number(f.line) || 1) - 1),
        column: Number.isFinite(f.column) && f.column > 0 ? f.column : 0,
        type: aceType(f.severity),
        text: f.rule ? `${f.message} (${f.rule})` : f.message
    }));
}
