import { ValueState } from "sap/ui/core/library";
import type {
    EmbeddingDeployResult, EmbeddingDeployment, EmbeddingSetting, EmbeddingsStatus
} from "../service/dataCatalogTypes";
import formatter from "./formatter";

/**
 * The logic of the "Data catalogue embeddings" panel of the Settings page
 * (design addendum (l), point 4), without a view: the status lines, the
 * options of the Model select, the form and the body of its save, when
 * "Compute embeddings now" may run, and the texts of the deploy
 * confirmation and of the toast after a run.
 *
 * Texts are i18n keys with their arguments, never a sentence built here.
 * The flags of an answer count only as the boolean `true`. The server stays
 * the gate: what it refuses is a 422 that names the field (`refusedField`).
 */

/**
 * The key of the "Automatic" option of the Model select, which saves the
 * model as `null`. It cannot be `""`: `sap.m.Select` reads an empty
 * `selectedKey` as "no selection" (see `MAIN_LINE_KEY` in NullableKey.ts).
 */
export const AUTOMATIC_KEY = "__automatic__";

/** How often the status is read again while a run is in flight. */
export const POLL_INTERVAL_MS = 2000;

/** A text of the panel as an i18n key and its arguments. */
export interface EmbeddingsText {
    key: string;
    args?: string[];
}

/** One status line: the i18n key of its label, its value, its colour. */
export interface StatusLine {
    label: string;
    text: EmbeddingsText;
    state: ValueState;
}

/** An item of the Model select. */
export interface ModelOption {
    key: string;
    text: EmbeddingsText;
}

/** What the panel edits: the switch and the selected key of the Model select. */
export interface EmbeddingsForm {
    enabled: boolean;
    modelKey: string;
}

/** A row of the deploy dialog. */
export interface DeployOption {
    model: string;
    /** The versions AI Core offers, joined for display. */
    versions: string;
}

function count(value: unknown): string {
    return String(typeof value === "number" && isFinite(value) ? value : 0);
}

function nameOf(value: unknown): string {
    return typeof value === "string" ? value : "";
}

/** AI Core could not be read: the deployments and offers are unknown, not empty. */
function aicoreUnread(status: EmbeddingsStatus | null | undefined): boolean {
    return !!status && typeof status.aicore_error === "string" && status.aicore_error !== "";
}

function settingOf(status: EmbeddingsStatus): { enabled: boolean; model: string } {
    const setting = status.setting as Partial<EmbeddingSetting> | undefined;
    return { enabled: setting?.enabled === true, model: nameOf(setting?.model) };
}

/** The model line: which model is in effect and where it comes from, or why there is none. */
function modelLine(status: EmbeddingsStatus): StatusLine {
    const line = (text: EmbeddingsText, state: ValueState = ValueState.None): StatusLine => (
        { label: "embLineModel", text, state });
    const effective = nameOf(status.effective_model);
    const setting = settingOf(status);
    if (effective) {
        const key = status.source === "setting" ? "embModelFromSetting"
            : status.source === "env" ? "embModelFromEnv" : "embModelFromDiscovery";
        return line({ key, args: [effective] });
    }
    if (!setting.enabled) {
        return line({ key: "embModelOff" });
    }
    const unread = aicoreUnread(status);
    if (status.source === "setting" && setting.model) {
        // A model chosen here without a RUNNING deployment (or none AI Core
        // could confirm): nothing is embedded.
        return unread
            ? line({ key: "embModelUnknownState", args: [setting.model] }, ValueState.Warning)
            : line({ key: "embModelNotRunning", args: [setting.model] }, ValueState.Warning);
    }
    return unread ? line({ key: "embModelNoneUnknown" }, ValueState.Warning) : line({ key: "embModelNone" });
}

/** The last run, or the one in flight. */
function runLine(status: EmbeddingsStatus): StatusLine {
    const line = (text: EmbeddingsText, state: ValueState = ValueState.None): StatusLine => (
        { label: "embLineLastRun", text, state });
    if (status.running === true) {
        const progress = status.progress;
        // Before the run has counted its nodes the server says `{0, 0}`.
        return progress && typeof progress.total === "number" && progress.total > 0
            ? line({ key: "embRunRunningProgress", args: [count(progress.done), count(progress.total)] }, ValueState.Information)
            : line({ key: "embRunRunning" }, ValueState.Information);
    }
    const run = status.last_run;
    if (!run) {
        return line({ key: "embRunNever" });
    }
    if (!run.finished_at) {
        return line({ key: "embRunUnfinished", args: [formatter.timestamp(run.started_at ?? null)] }, ValueState.Warning);
    }
    const at = formatter.timestamp(run.finished_at);
    if (run.error_class) {
        return line({ key: "embRunFailed", args: [at, String(run.error_class)] }, ValueState.Error);
    }
    const model = nameOf(run.model);
    return model
        ? line({ key: "embRunDone", args: [at, count(run.embedded), model] })
        : line({ key: "embRunDoneNoModel", args: [at, count(run.embedded)] });
}

/** The status lines, in their order: Effective model, Vectors n of m nodes,
 *  Stale, Last run. None without an answer. */
export function statusLines(status: EmbeddingsStatus | null | undefined): StatusLine[] {
    if (!status) {
        return [];
    }
    const stale = typeof status.stale === "number" ? status.stale : 0;
    // Nodes left to embed are worth a warning only when a model could embed them.
    const staleState = stale > 0 && nameOf(status.effective_model) ? ValueState.Warning : ValueState.None;
    return [
        modelLine(status),
        { label: "embLineVectors", text: { key: "embVectorsOf", args: [count(status.vectors), count(status.nodes)] },
            state: ValueState.None },
        { label: "embLineStale", text: stale > 0 ? { key: "embStaleSome", args: [count(stale)] } : { key: "embStaleNone" },
            state: staleState },
        runLine(status)
    ];
}

/**
 * The items of the Model select: Automatic, then each deployed embedding
 * model once with its AI Core status (RUNNING when any of its deployments
 * runs, else the first one's). A saved model that is no longer deployed
 * stays an item, marked so: without it the select would show Automatic and
 * a save would change the setting.
 */
export function modelOptions(status: EmbeddingsStatus | null | undefined): ModelOption[] {
    const options: ModelOption[] = [{ key: AUTOMATIC_KEY, text: { key: "embModelAutomatic" } }];
    const byModel: Record<string, string> = {};
    const order: string[] = [];
    (Array.isArray(status?.deployed) ? status!.deployed : []).forEach((deployment: EmbeddingDeployment) => {
        const model = nameOf(deployment?.model);
        if (!model || model === AUTOMATIC_KEY) {
            return;
        }
        if (!(model in byModel)) {
            order.push(model);
            byModel[model] = nameOf(deployment.status);
        } else if (deployment.status === "RUNNING") {
            byModel[model] = "RUNNING";
        }
    });
    order.forEach((model) => options.push({ key: model, text: { key: "embModelOption", args: [model, byModel[model]] } }));
    const saved = status ? settingOf(status).model : "";
    if (saved && saved !== AUTOMATIC_KEY && !(saved in byModel)) {
        options.push({ key: saved, text: {
            key: aicoreUnread(status) ? "embModelOptionUnknown" : "embModelOptionNotDeployed", args: [saved]
        } });
    }
    return options;
}

/** The stored setting as the panel edits it. */
export function formOf(status: EmbeddingsStatus | null | undefined): EmbeddingsForm {
    const setting = status ? settingOf(status) : { enabled: false, model: "" };
    return { enabled: setting.enabled, modelKey: setting.model || AUTOMATIC_KEY };
}

/** The body of the save: `{enabled, model}`, Automatic (or no key) as `null`. */
export function settingBody(form: EmbeddingsForm): EmbeddingSetting {
    const key = nameOf(form.modelKey);
    return { enabled: form.enabled === true, model: key && key !== AUTOMATIC_KEY ? key : null };
}

/** "model" when a refusal of the save names the model (`model: <rule>`),
 *  else "": the switch is a boolean the panel always sends as one. */
export function refusedField(detail: string | null | undefined): "model" | "" {
    return /^model:/.test(nameOf(detail)) ? "model" : "";
}

/** "Compute embeddings now": embeddings on, a model in effect, no run in flight. */
export function canRun(status: EmbeddingsStatus | null | undefined): boolean {
    return !!status && settingOf(status).enabled && nameOf(status.effective_model) !== "" && status.running !== true;
}

/** The toast after a run: the count, the error class, or that it did not
 *  finish; null while one runs or before the first. */
export function runOutcome(status: EmbeddingsStatus | null | undefined): EmbeddingsText | null {
    const run = status?.last_run;
    if (!status || status.running === true || !run) {
        return null;
    }
    if (!run.finished_at) {
        return { key: "embRunToastUnfinished" };
    }
    return run.error_class
        ? { key: "embRunToastFailed", args: [String(run.error_class)] }
        : { key: "embRunToastDone", args: [count(run.embedded)] };
}

/** The strip of `AICORE_EMBEDDING_MODEL`: in effect (Model is Automatic), or
 *  set and waiting for Automatic; null when the landscape does not set it. */
export function envStripText(status: EmbeddingsStatus | null | undefined): EmbeddingsText | null {
    if (!status || status.env_override !== true) {
        return null;
    }
    const effective = nameOf(status.effective_model);
    return status.source === "env" && effective ? { key: "embEnvInEffect", args: [effective] } : { key: "embEnvSet" };
}

/** The strip when AI Core could not be read: the deployment state is
 *  unknown (the lists are empty for that reason, not because nothing is
 *  deployed or offered). A fixed text: the code is not shown. */
export function aicoreStripText(status: EmbeddingsStatus | null | undefined): EmbeddingsText | null {
    return aicoreUnread(status) ? { key: "embAicoreUnreadable" } : null;
}

/** "Deploy an embedding model…": not while the live state of the deployments is unknown. */
export function canDeploy(status: EmbeddingsStatus | null | undefined): boolean {
    return !!status && !aicoreUnread(status);
}

/** The toast after a deploy: requested, or a live deployment that was already there. */
export function deployOutcome(model: string, result: EmbeddingDeployResult): EmbeddingsText {
    const state = nameOf(result?.status);
    return result?.created === false
        ? { key: "embDeployExisting", args: [model, state] }
        : { key: "embDeployRequested", args: [model, state] };
}

/**
 * The question before a deployment: it names the model, the resource group
 * and that AI Core bills for it. Without the group (a server that does not
 * say it) the text names the resource group of this app instead.
 */
export function deployConfirmText(model: string, resourceGroup: string | null | undefined): EmbeddingsText {
    const group = nameOf(resourceGroup);
    return group
        ? { key: "embDeployConfirm", args: [model, group] }
        : { key: "embDeployConfirmNoGroup", args: [model] };
}

/** The models the deploy dialog offers: what AI Core offers and is not deployed. */
export function deployOptions(status: EmbeddingsStatus | null | undefined): DeployOption[] {
    return (Array.isArray(status?.available) ? status!.available : [])
        .filter((offer) => nameOf(offer?.model) !== "")
        .map((offer) => ({
            model: offer.model,
            versions: (Array.isArray(offer.versions) ? offer.versions : []).map(String).join(", ")
        }));
}

export default {
    statusLines, modelOptions, formOf, settingBody, refusedField, canRun, runOutcome, envStripText, deployConfirmText,
    deployOptions, aicoreStripText, canDeploy, deployOutcome
};
