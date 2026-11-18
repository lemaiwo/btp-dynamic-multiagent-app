import { ValueState } from "sap/ui/core/library";
import type {
    ModelProviderName, ModelProviderSave, ModelProviderStatus, ModelProviderTestResult, ModelProvidersStatus
} from "../service/modelProviderTypes";

/**
 * The logic of the "Model providers" panel of the Settings page, without a
 * view: the form of a provider and the body of its save, whether the form
 * holds unsaved changes, which control a refusal names, and the texts of
 * the key's origin, of the key field's placeholder, of the information
 * strip and of a test's result.
 *
 * The API key is a secret: the server never answers it, so the key field
 * always starts empty, and a save carries `api_key` only when something was
 * typed there (absent keeps the stored key). Texts are i18n keys with their
 * arguments, never a sentence built here. The server stays the gate: what
 * it refuses is a 422 whose `detail` names the field (`refusedField`).
 */

/** The providers in the order the panel shows them. */
export const PROVIDERS: ModelProviderName[] = ["anthropic", "openai"];

/** A text of the panel as an i18n key and its arguments. */
export interface ProviderText {
    key: string;
    args?: string[];
}

/** What the panel edits for one provider. `models` is the comma-separated text. */
export interface ProviderForm {
    enabled: boolean;
    apiKey: string;
    baseUrl: string;
    models: string;
}

/** A control of a provider's block a refusal can name. */
export type ProviderField = "apiKey" | "baseUrl" | "models";

function text(value: unknown): string {
    return typeof value === "string" ? value : "";
}

/** The provider of that name in an answer, or null. */
export function providerOf(status: ModelProvidersStatus | null | undefined,
    name: ModelProviderName): ModelProviderStatus | null {
    const list = Array.isArray(status?.providers) ? status!.providers : [];
    return list.filter((provider) => provider?.provider === name)[0] ?? null;
}

/** The i18n key of a provider's title. */
export function titleKey(name: ModelProviderName): string {
    return name === "anthropic" ? "mpTitleAnthropic" : "mpTitleOpenai";
}

/** The stored setting as the panel edits it: the key field always empty. */
export function toForm(provider: ModelProviderStatus | null | undefined): ProviderForm {
    const models = Array.isArray(provider?.models) ? provider!.models.filter((m) => typeof m === "string") : [];
    return {
        enabled: provider?.enabled === true,
        apiKey: "",
        baseUrl: text(provider?.base_url),
        models: models.join(", ")
    };
}

/** The model names of the Models field: split on commas, trimmed, empties dropped. */
export function parseModels(value: string | null | undefined): string[] {
    return text(value).split(",").map((name) => name.trim()).filter((name) => name !== "");
}

/** What the key field holds as a key: trimmed (a paste often brings a blank); "" is nothing typed. */
export function typedKey(form: ProviderForm): string {
    return text(form.apiKey).trim();
}

/**
 * The body of a save: `enabled`, `base_url` (trimmed, "" as null) and
 * `models` always; `api_key` only when a key was typed, never as "".
 */
export function fromForm(form: ProviderForm): ModelProviderSave {
    const baseUrl = text(form.baseUrl).trim();
    const body: ModelProviderSave = {
        enabled: form.enabled === true,
        base_url: baseUrl === "" ? null : baseUrl,
        models: parseModels(form.models)
    };
    const key = typedKey(form);
    if (key !== "") {
        body.api_key = key;
    }
    return body;
}

/**
 * The body of "Remove stored key": the stored setting of `stored` (the form
 * the provider was last read or saved with) and `api_key: null`, so the
 * removal changes nothing else.
 */
export function removeKeyBody(stored: ProviderForm): ModelProviderSave {
    const body = fromForm({ ...stored, apiKey: "" });
    body.api_key = null;
    return body;
}

/** The form holds changes that are not saved: a typed key, or another
 *  switch, base URL or model list than `stored`. */
export function isDirty(form: ProviderForm, stored: ProviderForm): boolean {
    const now = fromForm(form);
    const then = fromForm({ ...stored, apiKey: "" });
    return "api_key" in now
        || now.enabled !== then.enabled
        || now.base_url !== then.base_url
        || now.models.join(",") !== then.models.join(",");
}

/** The control a refusal of the save names (`api_key: ...`, `base_url: ...`,
 *  `models: ...`, `models[3]: ...`), or "" for any other field. */
export function refusedField(detail: string | null | undefined): ProviderField | "" {
    const value = text(detail);
    if (/^api_key:/.test(value)) {
        return "apiKey";
    }
    if (/^base_url:/.test(value)) {
        return "baseUrl";
    }
    return /^models(\[\d+\])?:/.test(value) ? "models" : "";
}

/** The placeholder of the key field: whether a key is stored. */
export function keyPlaceholder(provider: ModelProviderStatus | null | undefined): ProviderText {
    return { key: provider?.has_stored_key === true ? "mpKeyPlaceholderStored" : "mpKeyPlaceholderNone" };
}

/** Where the key in effect comes from, and its colour. */
export function keySource(provider: ModelProviderStatus | null | undefined): { text: ProviderText; state: ValueState } {
    switch (provider?.key_source) {
        case "setting":
            return { text: { key: "mpKeySourceSetting" }, state: ValueState.Success };
        case "env":
            return { text: { key: "mpKeySourceEnv" }, state: ValueState.Success };
        default:
            // No key while the provider is on is worth a warning: it offers nothing.
            return { text: { key: "mpKeySourceNone" },
                state: provider?.enabled === true ? ValueState.Warning : ValueState.None };
    }
}

/** The models the provider offers now, read-only under the Models field. */
export function effectiveText(provider: ModelProviderStatus | null | undefined): ProviderText {
    const models = Array.isArray(provider?.effective_models)
        ? provider!.effective_models.filter((m) => typeof m === "string" && m !== "") : [];
    return models.length ? { key: "mpEffectiveModels", args: [models.join(", ")] } : { key: "mpEffectiveNone" };
}

/** The information strip: the model names, the setting before the environment,
 *  and that SAP AI Core is not configured when the answer says so. */
export function infoTexts(status: ModelProvidersStatus | null | undefined): ProviderText[] {
    const texts: ProviderText[] = [{ key: "mpInfo" }];
    if (status && status.aicore_configured !== true) {
        texts.push({ key: "mpInfoNoAicore" });
    }
    return texts;
}

/** The strip after a test: a fixed text per case, never the provider's text. */
export function testOutcome(result: ModelProviderTestResult | null | undefined):
    { type: "Success" | "Error"; text: ProviderText } {
    if (result?.ok === true) {
        return { type: "Success", text: { key: "mpTestOk" } };
    }
    if (result?.error === "no_key") {
        return { type: "Error", text: { key: "mpTestNoKey" } };
    }
    if (result?.status === 401 || result?.status === 403) {
        return { type: "Error", text: { key: "mpTestRefused" } };
    }
    // Only a class name (the server never sends the provider's text); anything
    // else is not shown.
    const error = text(result?.error);
    return { type: "Error", text: { key: "mpTestUnreachable", args: [/^[A-Za-z_][A-Za-z0-9_.]{0,99}$/.test(error)
        ? error : "?"] } };
}

export default {
    PROVIDERS, providerOf, titleKey, toForm, parseModels, typedKey, fromForm, removeKeyBody, isDirty, refusedField,
    keyPlaceholder, keySource, effectiveText, infoTexts, testOutcome
};
