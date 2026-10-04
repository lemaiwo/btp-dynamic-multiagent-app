import type { DiagnoseFinding } from "../service/types";
import { adtUri } from "./adtLink";

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

// --- Task U11: the findings list of the session page -----------------------------

/** Looks up an i18n text (BaseController#text). */
type TextLookup = (key: string, args?: (string | number)[]) => string;

const KIND_KEYS: Record<string, string> = {
    dump: "findingKindDump",
    trace: "findingKindTrace",
    gateway_error: "findingKindGatewayError",
    auth_check: "findingKindAuthCheck",
    odata_call: "findingKindOdataCall"
};

const KIND_ICONS: Record<string, string> = {
    dump: "sap-icon://error",
    trace: "sap-icon://performance",
    gateway_error: "sap-icon://chain-link",
    auth_check: "sap-icon://locked",
    odata_call: "sap-icon://cloud"
};

/**
 * The kind's colour next to its text and icon (never colour alone). Indication
 * colours, not value states: a dump is not an "invalid entry", which is what
 * a screen reader would hear for the Error state.
 */
const KIND_STATES: Record<string, string> = {
    dump: "Indication01",
    trace: "Indication03",
    gateway_error: "Indication01",
    auth_check: "Indication03",
    odata_call: "Indication05"
};

/** The kinds SAP has a text for (the detail route answers 422 `no_detail` for the others). */
const WITH_DETAIL = ["dump", "trace", "gateway_error"];

const own = (map: Record<string, string>, key: string): string | undefined =>
    Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;

/** A class pool names its parts `<class padded with "=" to 30><suffix>` (`agents/ide/paths.py`). */
const POOL_NAME_LENGTH = 30;
const POOL_SUFFIX = /^(?:CP|CU|CO|CI|CCDEF|CCIMP|CCMAC|CCAU|CM[0-9A-Z]{3})$/;
const CLASS_NAME = /^[A-Z0-9_/]{1,30}$/;
/** Class-local sections: suffix -> the abapGit file part adtUri takes (the line is the section's own). */
const CLASS_SECTIONS: Record<string, string> = {
    CCDEF: "locals_def",
    CCIMP: "locals_imp",
    CCMAC: "macros",
    CCAU: "testclasses"
};

/**
 * The class and suffix of a class pool part, as the server's `class_include`
 * decides: a `=` before the suffix, or a name longer than 30 characters (a
 * class name of exactly 30 has no padding); `null` for anything else
 * (`ZREPORT_CP` is a program).
 */
export function classPoolPart(name: unknown): { cls: string; suffix: string } | null {
    const text = typeof name === "string" ? name.trim().toUpperCase() : "";
    let cut: number;
    if (text.includes("=")) {
        cut = text.lastIndexOf("=") + 1;
    } else if (text.length > POOL_NAME_LENGTH) {
        cut = POOL_NAME_LENGTH;
    } else {
        return null;
    }
    const cls = text.slice(0, cut).replace(/=+$/, "");
    const suffix = text.slice(cut);
    return CLASS_NAME.test(cls) && POOL_SUFFIX.test(suffix) ? { cls, suffix } : null;
}

/**
 * "Open in ADT" for a finding, only through adtUri (validated parts,
 * `adt:` only); `null` without a program or when a part does not validate.
 * Mirrors the server's `resolve_include`:
 *
 * - A class pool part (the include, else the program): a class-local
 *   section (CCDEF/CCIMP/CCMAC/CCAU) opens that include at the line; the
 *   pool itself (CP) opens the class at the line; any other part (a method
 *   include, CU/CO/CI) opens the class without a line (its line is not a
 *   line of the class source).
 * - An include that is not the program itself opens that include at the
 *   line (the line is the include's); one that does not validate gives no
 *   link rather than the program at a line of another source.
 * - A function pool itself (`SAPL...`, no other include) has no link: its
 *   frame holds no code.
 * - Any other program opens at the line.
 *
 * Program and include are compared trimmed and in upper case, as the server does.
 */
export function findingAdtHref(target: string, finding: DiagnoseFinding): string | null {
    // Compared as the server does (`_upper`): trimmed, upper case.
    const program = typeof finding?.program === "string" ? finding.program.trim().toUpperCase() : "";
    if (!program) {
        return null;
    }
    const given = typeof finding.include === "string" ? finding.include.trim().toUpperCase() : "";
    const include = given && given !== program ? given : null;
    const part = classPoolPart(include ?? program);
    if (part) {
        const section = Object.prototype.hasOwnProperty.call(CLASS_SECTIONS, part.suffix) ? CLASS_SECTIONS[part.suffix] : null;
        if (section) {
            return adtUri(target, "CLAS", part.cls, finding.line, section);
        }
        return adtUri(target, "CLAS", part.cls, part.suffix === "CP" ? finding.line : undefined);
    }
    if (include) {
        return adtUri(target, "INCL", include, finding.line);
    }
    if (program.startsWith("SAPL")) {
        // A function pool's frame holds no code of its own: the server opens nothing for it either.
        return null;
    }
    return adtUri(target, "PROG", program, finding.line);
}

/** One row of the findings list (`s>/artifact/findings/items`): every value is data for Text controls. */
export interface FindingRow {
    id: string;
    kindText: string;
    kindIcon: string;
    kindState: string;
    /** As SAP or the model wrote it: bound to a Text, never parsed. */
    title: string;
    /** "program · include · line N", or that the position is unknown. */
    where: string;
    time: string;
    /** A program to open (the open route answers 422 `no_source` otherwise). */
    hasSource: boolean;
    hasDetail: boolean;
    /** From findingAdtHref only; "" hides the link. */
    adtHref: string;
}

/** The row of `finding` for the findings list; `formatTime` formats an API timestamp. */
export function findingRow(
    finding: DiagnoseFinding, target: string, t: TextLookup, formatTime: (iso: string) => string
): FindingRow {
    const kind = String(finding.kind ?? "");
    const key = own(KIND_KEYS, kind);
    const kindText = key ? t(key) : kind;
    const parts: string[] = [];
    if (finding.program) {
        parts.push(finding.program);
    }
    if (finding.include && finding.include !== finding.program) {
        parts.push(finding.include);
    }
    if (typeof finding.line === "number") {
        parts.push(t("findingLine", [finding.line]));
    }
    const where = parts.length ? parts.join(" · ") : t("findingWhereUnknown");
    const stamp = finding.occurred_at || finding.created_at || "";
    const time = stamp ? formatTime(stamp) : "";
    const title = String(finding.title ?? "");
    return {
        id: finding.id,
        kindText,
        kindIcon: own(KIND_ICONS, kind) ?? "sap-icon://inspection",
        kindState: own(KIND_STATES, kind) ?? "None",
        title,
        where,
        time,
        hasSource: !!finding.program,
        hasDetail: WITH_DETAIL.includes(kind),
        adtHref: findingAdtHref(target, finding) ?? ""
    };
}

/** How many trace requests wait for a decision (the diagnose banner says so). */
export function pendingApprovalCount(approvals: { status: string }[]): number {
    return approvals.filter((a) => a.status === "pending").length;
}
