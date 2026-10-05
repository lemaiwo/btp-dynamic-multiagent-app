import { ValueState } from "sap/ui/core/library";
import DateFormat from "sap/ui/core/format/DateFormat";
import formatMessage from "sap/base/strings/formatMessage";
import type { FileState, FindingKind, Stage } from "../service/types";

/** Entries a JSONModel list binding shows at most (UI5's default of 100 would cut long lists silently). */
export const MODEL_SIZE_LIMIT = 5000;

/** What the text formatters need from their `this`: the view's controller. */
interface I18nHost {
    getModel(name?: string): unknown;
}

interface Bundle { getText(key: string, args?: (string | number)[]): string }

const STATE_BADGES: Record<FileState, ValueState> = {
    read: ValueState.Information,
    modified: ValueState.Warning,
    new: ValueState.Success
};

const STATE_KEYS: Record<FileState, string> = {
    read: "fileStateRead",
    modified: "fileStateModified",
    new: "fileStateNew"
};

const STAGE_KEYS: Record<Stage, string> = {
    chat: "stageChat",
    design: "stageDesign",
    plan: "stagePlan",
    propose: "stagePropose",
    review: "stageReview",
    done: "stageDone",
    investigate: "stageInvestigate"
};

const FINDING_KIND_KEYS: Record<FindingKind, string> = {
    dump: "findingKindDump",
    trace: "findingKindTrace",
    gateway_error: "findingKindGatewayError",
    auth_check: "findingKindAuthCheck",
    odata_call: "findingKindOdataCall"
};

const FINDING_ICONS: Record<FindingKind, string> = {
    dump: "sap-icon://error",
    trace: "sap-icon://performance",
    gateway_error: "sap-icon://chain-link",
    auth_check: "sap-icon://locked",
    odata_call: "sap-icon://cloud"
};

/** A finding's time for display: the locale's short date and time, or the raw value when it is no date. */
function findingTime(value: string | null | undefined): string {
    if (!value) {
        return "";
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : DateFormat.getDateTimeInstance({ style: "short" }).format(date);
}

/**
 * The i18n bundle a formatter reads its texts from. A `'.formatter.x'`
 * binding in an XML view runs with `this` bound to the view's controller,
 * whose getModel("i18n") (BaseController) resolves through the view. The
 * manifest's i18n model is synchronous, so getResourceBundle() returns the
 * bundle, not a promise.
 */
function bundleOf(host: I18nHost | undefined): Bundle | undefined {
    const model = host?.getModel?.("i18n") as { getResourceBundle?: () => unknown } | undefined;
    const bundle = model?.getResourceBundle?.() as Bundle | undefined;
    return bundle && typeof bundle.getText === "function" ? bundle : undefined;
}

function translate(host: I18nHost | undefined, keys: Record<string, string>, value: string | null | undefined): string {
    if (!value) {
        return "";
    }
    const key = Object.prototype.hasOwnProperty.call(keys, value) ? keys[value] : undefined;
    const bundle = bundleOf(host);
    return key && bundle ? bundle.getText(key) : value;
}

export default {
    /** A workspace file's badge colour: read, modified, new. Folders get none. */
    stateBadge(state: FileState | null | undefined): ValueState {
        return (state && STATE_BADGES[state]) || ValueState.None;
    },

    /** A workspace file's badge text from i18n ("read", "modified", "new"). */
    stateText(this: I18nHost | undefined, state: FileState | null | undefined): string {
        return translate(this, STATE_KEYS, state);
    },

    /** A session stage's display name from i18n ("Chat", "Design", ...). */
    stageText(this: I18nHost | undefined, stage: Stage | null | undefined): string {
        return translate(this, STAGE_KEYS, stage);
    },

    /** A finished session reads as Success; any open stage as Information. */
    stageState(stage: Stage | null | undefined): ValueState {
        return stage === "done" ? ValueState.Success : ValueState.Information;
    },

    /** A title with a count: `pattern` is an i18n text with `{0}`. */
    countTitle(pattern: string | null | undefined, count: number | null | undefined): string {
        return formatMessage(pattern ?? "", [count ?? 0]);
    },

    /**
     * Where and when a diagnose finding happened: "program · include · line N · time",
     * leaving out what the finding does not have.
     */
    formatFindingWhere(
        this: I18nHost | undefined, program: string | null | undefined, include: string | null | undefined,
        line: number | null | undefined, occurredAt?: string | null
    ): string {
        const bundle = bundleOf(this);
        const parts: string[] = [];
        if (program) {
            parts.push(program);
        }
        if (include && include !== program) {
            parts.push(include);
        }
        if (line !== null && line !== undefined) {
            parts.push(bundle ? bundle.getText("findingLine", [line]) : String(line));
        }
        const time = findingTime(occurredAt);
        if (time) {
            parts.push(time);
        }
        if (!parts.length) {
            return bundle ? bundle.getText("findingWhereUnknown") : "";
        }
        return parts.join(" \u00b7 ");
    },

    /** A finding kind's display name from i18n ("Dump", "Trace", ...). */
    findingKindText(this: I18nHost | undefined, kind: FindingKind | null | undefined): string {
        return translate(this, FINDING_KIND_KEYS, kind);
    },

    /**
     * Whether SAP can be asked for a finding's text: an authorization check
     * and an OData call are rows of a list, with nothing more to read (the
     * detail route answers 422 `no_detail` for them).
     */
    findingHasDetail(kind: FindingKind | null | undefined): boolean {
        return kind === "dump" || kind === "trace" || kind === "gateway_error";
    },

    /** A finding kind's icon (decorative: the kind is also shown as text). */
    findingIcon(kind: FindingKind | null | undefined): string {
        return (kind && Object.prototype.hasOwnProperty.call(FINDING_ICONS, kind) && FINDING_ICONS[kind]) || "sap-icon://inspection";
    }
};
