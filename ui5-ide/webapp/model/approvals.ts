import type { Approval, SseEvent, TraceParams } from "../service/types";

/** Events the reducer understands: the SSE stream plus a route's decided approval. */
export type ApprovalEvent = SseEvent | { type: "decided"; data: Approval };

/** Newest first by creation time; ties keep their relative order. */
function byNewest(list: Approval[]): Approval[] {
    return list
        .map((a, i) => ({ a, i }))
        .sort((x, y) => {
            const cx = x.a.created_at ?? "";
            const cy = y.a.created_at ?? "";
            return cx === cy ? x.i - y.i : cx < cy ? 1 : -1;
        })
        .map((x) => x.a);
}

/**
 * Folds one event into the approvals list (plan 1c §1.2, §1.3): upsert by
 * `id`, newest first. `approval_required` adds a pending row; `decided` (the
 * decide route's answer, or what a refusal says about the row) replaces it.
 * An `approval` frame is folded the same way, though no stream sends one:
 * there is no resumed run. Any other event returns the list unchanged.
 * Never mutates its input.
 */
export function reduceApprovals(list: Approval[], event: ApprovalEvent): Approval[] {
    if (event.type !== "approval_required" && event.type !== "approval" && event.type !== "decided") {
        return list;
    }
    const next = event.data;
    if (!next || typeof next.id !== "string") {
        return list;
    }
    const without = list.filter((a) => a.id !== next.id);
    return byNewest([...without, next]);
}

export function pendingCount(list: Approval[]): number {
    return list.filter((a) => a.status === "pending").length;
}

type Translate = (key: string, args?: (string | number)[]) => string;

const PROCESS_KEYS: Record<string, string> = {
    http: "approvalProcessHttp", dialog: "approvalProcessDialog", batch: "approvalProcessBatch", rfc: "approvalProcessRfc"
};
const OBJECT_KEYS: Record<string, string> = {
    any: "approvalObjectAny", url: "approvalObjectUrl", transaction: "approvalObjectTransaction",
    report: "approvalObjectReport", functionModule: "approvalObjectFunctionModule"
};

/**
 * The display text of an enum value. A value the UI does not know is shown
 * as the server sent it, never as some other value: the card is what the
 * user consents to.
 */
function enumText(keys: Record<string, string>, value: unknown, t: Translate): string {
    const key = typeof value === "string" && Object.prototype.hasOwnProperty.call(keys, value) ? keys[value] : "";
    return key ? t(key) : String(value ?? "");
}

function isObject(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * `trace_start` parameters the card can show in full: an object with its
 * process and object type and both numbers. `params` comes from the server
 * as JSON, so it is checked, never trusted to be the declared type.
 */
function isTraceParams(params: unknown): params is TraceParams {
    return isObject(params) && typeof params.processType === "string" && typeof params.objectType === "string"
        && Number.isFinite(params.maxExecutions) && Number.isFinite(params.expiresHours);
}

/** `trace_cancel` parameters: the trace request to cancel. */
function isCancelParams(params: unknown): params is { id: string } {
    return isObject(params) && typeof params.id === "string" && params.id !== "" && !("processType" in params);
}

/** The approval's params say in full what approving would do. Anything else cannot be consented to. */
function hasValidParams(approval: Approval): boolean {
    return approval.action === "trace_cancel" ? isCancelParams(approval.params) : isTraceParams(approval.params);
}

/**
 * One line that says what an approval would do, parts joined by " · ", for
 * example `HTTP request · max 1 execution · expires in 1 h · SQL on`. `t`
 * translates an i18n key with arguments (the controller's `text`). Params
 * that are neither a trace nor a cancel request read "not valid".
 */
export function paramsText(params: TraceParams | { id: string }, t: Translate): string {
    if (isCancelParams(params)) {
        return t("approvalParamRequest", [params.id]);
    }
    if (!isTraceParams(params)) {
        return t("approvalInvalid");
    }
    const parts = [
        enumText(PROCESS_KEYS, params.processType, t),
        t("approvalParamMaxExec", [params.maxExecutions]),
        t("approvalParamExpires", [params.expiresHours])
    ];
    if (params.sqlTrace) {
        parts.push(t("approvalParamSqlOn"));
    }
    return parts.join(" · ");
}

/** Error codes of a failed or refused approval and the i18n keys of their sentences. */
const ERROR_KEYS: Record<string, string> = {
    arc1_timeout_unknown: "approvalErrArc1TimeoutUnknown",
    interrupted: "approvalErrInterrupted",
    unknown_trace_request: "approvalErrUnknownTraceRequest",
    too_many_pending: "approvalErrTooManyPending",
    audit_unavailable: "approvalErrAuditUnavailable",
    target_not_non_production: "approvalNotNonProd",
    approval_expired: "approvalExpired",
    approval_not_pending: "approvalErrNotPending"
};

/** The i18n key of an approval error code; `undefined` for a code the UI does not know. */
export function approvalErrorKey(code: string | null | undefined): string | undefined {
    return code && Object.prototype.hasOwnProperty.call(ERROR_KEYS, code) ? ERROR_KEYS[code] : undefined;
}

/** Codes whose own sentence already says what the server's `note` says. */
const NOTE_IN_TEXT = ["arc1_timeout_unknown", "interrupted"];

/** Why an approval failed: the code's sentence, the code itself, or "no reason given", plus the server's note. */
export function approvalErrorText(code: string | null | undefined, note: string | undefined, t: Translate): string {
    const key = approvalErrorKey(code);
    const text = key ? t(key) : code ? t("approvalErrOther", [code]) : t("approvalErrNoCode");
    return note && !(code && NOTE_IN_TEXT.includes(code)) ? `${text} — ${note}` : text;
}

/** One approval as the assistant pane shows it: a card while pending, one status line once decided. */
export interface ApprovalRow {
    id: string;
    /** Pending: the card with Approve and Reject. Anything else is read-only. */
    pending: boolean;
    /**
     * The card offers Approve: pending and its params are valid, so the card
     * lists everything the decision covers. Without it the card has Reject only.
     */
    canApprove: boolean;
    /** Why there is no Approve on a pending card; "" when there is. */
    invalidText: string;
    /** The question the card asks, with the target system. */
    title: string;
    /** Every parameter the decision covers, as label and value. */
    fields: { label: string; value: string }[];
    /** For whom and how long, in a sentence. */
    who: string;
    approveText: string;
    /** "Decide before ..." when the server says how long the approval can be decided; else "". */
    decideBy: string;
    /** The decided line; "" while pending. */
    statusText: string;
    statusState: "None" | "Success" | "Warning" | "Error";
    statusIcon: string;
    /** A decision of this row is on its way (set by the controller). */
    busy: boolean;
}

function onOff(value: unknown, t: Translate): string {
    return t(value === true ? "approvalOn" : "approvalOff");
}

function fieldsOf(approval: Approval, t: Translate): ApprovalRow["fields"] {
    const params = approval.params;
    if (!hasValidParams(approval)) {
        // Nothing is listed rather than half a request: there is no Approve to consent with.
        return [];
    }
    if (!isTraceParams(params)) {
        return [{ label: t("approvalFieldRequest"), value: params.id }];
    }
    const fields = [
        { label: t("approvalFieldProcess"), value: enumText(PROCESS_KEYS, params.processType, t) },
        { label: t("approvalFieldObject"), value: enumText(OBJECT_KEYS, params.objectType, t) },
        { label: t("approvalFieldMaxExec"), value: String(params.maxExecutions) },
        { label: t("approvalFieldExpires"), value: t("approvalHours", [params.expiresHours]) },
        { label: t("approvalFieldSql"), value: onOff(params.sqlTrace, t) },
        { label: t("approvalFieldAggregate"), value: onOff(params.aggregate, t) }
    ];
    if (params.description) {
        fields.push({ label: t("approvalFieldDescription"), value: params.description });
    }
    fields.push({ label: t("approvalFieldUser"), value: t("approvalOwnUser") });
    return fields;
}

/**
 * Which request a decided line is about: the trace's description, the trace
 * request a cancel names, or else the approval's own id.
 */
function identityOf(approval: Approval, t: Translate): string {
    const params: unknown = approval.params;
    if (approval.action === "trace_cancel" && isCancelParams(params)) {
        return t("approvalIdentityCancel", [params.id]);
    }
    if (approval.action === "trace_start" && isObject(params) && typeof params.description === "string" && params.description) {
        return t("approvalIdentityStart", [params.description]);
    }
    return t("approvalIdentityRequest", [approval.id]);
}

function statusOf(approval: Approval, t: Translate, formatTime: (iso: string) => string):
Pick<ApprovalRow, "statusText" | "statusState" | "statusIcon"> {
    const result = approval.result;
    // A line that carries no trace request id says which request it is about.
    const named = (line: string): string => t("approvalLineFor", [line, identityOf(approval, t)]);
    switch (approval.status) {
        case "pending":
            return { statusText: "", statusState: "None", statusIcon: "" };
        case "approved": {
            if (approval.action === "trace_cancel" && result) {
                const id = isCancelParams(approval.params) ? approval.params.id : String(result.trace_request_id ?? "");
                return { statusText: t("approvalCancelled", [id]), statusState: "Success", statusIcon: "sap-icon://accept" };
            }
            if (approval.action === "trace_start" && result?.trace_request_id) {
                return {
                    statusText: result.expires_at
                        ? t("approvalArmed", [result.trace_request_id, formatTime(result.expires_at)])
                        : t("approvalArmedNoExpiry", [result.trace_request_id]),
                    statusState: "Success", statusIcon: "sap-icon://accept"
                };
            }
            // Approved, and nothing came back that says it worked: never call that "armed".
            return { statusText: named(t("approvalOutcomeUnknown")), statusState: "Warning", statusIcon: "sap-icon://question-mark" };
        }
        case "denied":
            return { statusText: named(t("approvalDenied")), statusState: "None", statusIcon: "sap-icon://decline" };
        case "expired":
            return { statusText: named(t("approvalExpired")), statusState: "Warning", statusIcon: "sap-icon://history" };
        case "failed":
            return {
                statusText: named(t("approvalFailed", [approvalErrorText(approval.error_code, result?.note, t)])),
                statusState: "Error", statusIcon: "sap-icon://error"
            };
        default:
            // A status this UI does not know: shown raw, read-only.
            return { statusText: String(approval.status), statusState: "None", statusIcon: "sap-icon://question-mark" };
    }
}

/**
 * What the assistant pane shows for one approval. Only a `pending`
 * `trace_start` or `trace_cancel` gives a card with buttons: every other
 * status or action, known or not, is a read-only line. A card whose params
 * are not valid (not an object, or without what the action needs) has no
 * Approve: it says so and can only be rejected.
 * `formatTime` turns an ISO timestamp into the user's date and time.
 */
export function approvalRow(
    approval: Approval, target: string, t: Translate, formatTime: (iso: string) => string
): ApprovalRow {
    const cancel = approval.action === "trace_cancel";
    // An action this UI cannot describe is never offered for approval: it is a read-only line.
    const known = cancel || approval.action === "trace_start";
    let decideBy = "";
    const created = approval.created_at ? Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(approval.created_at) ? approval.created_at : `${approval.created_at}Z`) : NaN;
    if (typeof approval.ttl_min === "number" && approval.ttl_min > 0 && Number.isFinite(created)) {
        decideBy = t("approvalDecideBy", [formatTime(new Date(created + approval.ttl_min * 60000).toISOString())]);
    }
    const pending = known && approval.status === "pending";
    const valid = hasValidParams(approval);
    return {
        id: approval.id,
        pending,
        canApprove: pending && valid,
        invalidText: pending && !valid ? t("approvalInvalid") : "",
        title: t(cancel ? "approvalCancelTitle" : "approvalTitle", [target]),
        fields: fieldsOf(approval, t),
        who: t(cancel ? "approvalCancelWho" : "approvalWho"),
        approveText: t(cancel ? "approveCancel" : "approveTrace"),
        decideBy,
        ...(known
            ? statusOf(approval, t, formatTime)
            : { statusText: `${String(approval.action)}: ${String(approval.status)}`, statusState: "None" as const, statusIcon: "sap-icon://question-mark" }),
        busy: false
    };
}
