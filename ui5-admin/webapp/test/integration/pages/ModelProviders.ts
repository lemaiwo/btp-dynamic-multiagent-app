import type Input from "sap/m/Input";
import type Button from "sap/m/Button";
import type Switch from "sap/m/Switch";
import type Text from "sap/m/Text";
import type ObjectStatus from "sap/m/ObjectStatus";
import type MessageStrip from "sap/m/MessageStrip";
import type SimpleForm from "sap/ui/layout/form/SimpleForm";
import type UI5Element from "sap/ui/core/Element";
import type { ModelProviderName } from "com/agent/admin/service/modelProviderTypes";
import { viewOf } from "./InaQueryDetail";

/**
 * What the model providers journey reads off the "Model providers" panel of
 * the Settings page: the rendered controls, never the model behind them.
 */

export const VIEW = "Settings";

export const PANEL = "modelProvidersPanel";
export const INFO = "mpInfoStrip";

/** The id of a control of a provider's block (`mpAnthropic--apiKey`). */
export function idOf(provider: ModelProviderName, control: string): string {
    return `${provider === "anthropic" ? "mpAnthropic" : "mpOpenai"}--${control}`;
}

/** What one provider's block shows. */
export interface BlockTexts {
    title: string;
    enabled: boolean;
    key: string;
    keyType: string;
    keyPlaceholder: string;
    keySource: string;
    canRemoveKey: boolean;
    baseUrl: string;
    models: string;
    effective: string;
    canTest: boolean;
    testTooltip: string;
    saveFirst: boolean;
    /** The test strip's text when it is visible, else "". */
    test: string;
    testType: string;
}

/** The block of `provider` in the view that holds `control`. */
export function blockOf(control: UI5Element, provider: ModelProviderName): BlockTexts {
    const view = viewOf(control);
    const get = <T>(id: string) => view.byId(idOf(provider, id)) as unknown as T;
    const key = get<Input>("apiKey");
    const strip = get<MessageStrip>("testResult");
    const test = get<Button>("test");
    return {
        title: get<SimpleForm>("form").getTitle() as string,
        enabled: get<Switch>("enabled").getState(),
        key: key.getValue(),
        keyType: String(key.getType()),
        keyPlaceholder: key.getPlaceholder(),
        keySource: get<ObjectStatus>("keySource").getText(),
        canRemoveKey: get<Button>("removeKey").getEnabled(),
        baseUrl: get<Input>("baseUrl").getValue(),
        models: get<Input>("models").getValue(),
        effective: get<Text>("effective").getText(false),
        canTest: test.getEnabled(),
        testTooltip: String(test.getTooltip() ?? ""),
        saveFirst: get<Text>("saveFirst").getVisible(),
        test: strip.getVisible() ? strip.getText() : "",
        testType: String(strip.getType())
    };
}
