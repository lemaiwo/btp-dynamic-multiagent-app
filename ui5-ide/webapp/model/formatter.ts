import { ValueState } from "sap/ui/core/library";
import type { FileState, Stage } from "../service/types";

/** What the text formatters need from their `this`: the view's controller. */
interface I18nHost {
    getModel(name?: string): unknown;
}

interface Bundle { getText(key: string): string }

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
    done: "stageDone"
};

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
    }
};
