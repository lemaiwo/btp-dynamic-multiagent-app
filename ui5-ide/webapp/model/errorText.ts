import { IdeError } from "../service/IdeService";
import type { ErrorEventData } from "../service/types";

/** Looks up an i18n text; the controllers pass BaseController#text. */
export type TextLookup = (key: string, args?: (string | number)[]) => string;

/**
 * The message a failed call shows, from its status and code (plan §1.1/1.2):
 *
 * - 424 `user_token_required`: the approuter did not forward the user's
 *   token, so ARC-1 cannot be called as the user. Reloading signs in again.
 * - 502 (`sap_*` codes): ARC-1 answered, SAP refused. SAP's message is shown
 *   with a hint at the SAP user mapping and authorizations.
 * - 401 / `session_expired`: the approuter session ended.
 * - 403 `readonly_refused`: the read-only guard refused the call (a write
 *   or an unlisted tool); not a role problem.
 * - 403 without a code, or with `forbidden`: the caller lacks the developer
 *   role. Another 403 code shows its detail.
 * - 409: a stage-gate refusal; the server's sentence is already user-facing.
 * - 429 `usage_exhausted`: the session's request cap is used up.
 *
 * Anything else, including a non-IdeError, is "The request failed: detail".
 */
export function errorText(error: unknown, text: TextLookup): string {
    if (!(error instanceof IdeError)) {
        const message = error instanceof Error ? error.message : String(error);
        return text("requestFailed", [message]);
    }
    const detail = error.detail || error.message;
    if (error.status === 424 || error.code === "user_token_required") {
        return text("userTokenRequired");
    }
    if (error.status === 502 || error.code?.startsWith("sap_")) {
        return text("sapError", [detail]);
    }
    if (error.status === 401 || error.code === "session_expired") {
        return text("sessionExpired");
    }
    if (error.code === "readonly_refused") {
        return text("readOnlyRefused");
    }
    if (error.status === 403 && (!error.code || error.code === "forbidden")) {
        return text("missingRole");
    }
    if (error.status === 429 || error.code === "usage_exhausted") {
        return text("usageExhausted", [detail]);
    }
    if (error.status === 409 && detail) {
        return detail;
    }
    return text("requestFailed", [detail]);
}

/** Stage-gate refusal codes (plan §1.1 and later tasks) and their i18n keys. */
const GATE_KEYS: Record<string, string> = {
    run_in_progress: "gateRunInProgress",
    stage_done: "gateStageDone",
    missing_artifact: "gateMissingArtifact",
    no_proposals: "gateNoProposals",
    revise_not_allowed: "gateReviseNotAllowed",
    stage_changed: "gateStageChanged",
    invalid_stage: "gateInvalidStage",
    run_on_other_instance: "cancelOtherInstance"
};

/**
 * The message for a refused approve / send / revise / cancel: a known
 * 409 gate code gets its i18n sentence, an unknown one the server's own
 * sentence, anything else the {@link errorText} wording.
 */
export function gateErrorText(error: unknown, text: TextLookup): string {
    if (error instanceof IdeError && error.status === 409 && error.code && GATE_KEYS[error.code]) {
        return text(GATE_KEYS[error.code]);
    }
    return errorText(error, text);
}

/**
 * The message for an `error` frame of a run's stream (plan §1.3), by code:
 * `stream_incomplete` (the stream ended without `done`, IdeService),
 * `run_timeout` and `run_failed` (the server's sentence carries the run
 * reference), `usage_exhausted`, the user-token, read-only and SAP codes as in
 * {@link errorText}; anything else shows the server's message.
 */
export function runErrorText(data: ErrorEventData, text: TextLookup): string {
    const message = data.message || "";
    switch (data.code) {
        case "stream_incomplete":
            return text("streamIncomplete");
        case "run_timeout":
            return text("runTimeout", [message]);
        case "run_failed":
            return text("runFailed", [message]);
        case "usage_exhausted":
            return text("usageExhausted", [message]);
        case "user_token_required":
            return text("userTokenRequired");
        case "readonly_refused":
            return text("readOnlyRefused");
        default:
            if (data.code?.startsWith("sap_")) {
                return text("sapError", [message]);
            }
            return message || text("requestFailed", [data.code ?? ""]);
    }
}
