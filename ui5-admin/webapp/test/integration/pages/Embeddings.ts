import type ObjectStatus from "sap/m/ObjectStatus";
import type Select from "sap/m/Select";
import type Switch from "sap/m/Switch";
import type Button from "sap/m/Button";
import type List from "sap/m/List";
import type StandardListItem from "sap/m/StandardListItem";
import type MessageStrip from "sap/m/MessageStrip";
import type UI5Element from "sap/ui/core/Element";
import { viewOf } from "./InaQueryDetail";

/**
 * What the embeddings journey reads off the "Data catalogue embeddings"
 * panel of the Settings page and its deploy dialog: the rendered controls,
 * never the model behind them.
 */

export const VIEW = "Settings";

/** The ids of the panel's controls and of the deploy dialog's. */
export const ID = {
    panel: "embeddingsPanel",
    model: "embModelLine",
    vectors: "embVectorsLine",
    stale: "embStaleLine",
    lastRun: "embLastRunLine",
    enabled: "embEnabledSwitch",
    select: "embModelSelect",
    save: "embSaveButton",
    run: "embRunButton",
    deploy: "embDeployButton",
    envStrip: "embEnvStrip",
    loadError: "embLoadError",
    dialog: "embDeployDialog",
    offers: "embDeployList",
    dialogDeploy: "embDeployContinue",
    dialogCancel: "embDeployCancel"
};

/** What the panel shows. */
export interface PanelTexts {
    model: string;
    modelState: string;
    vectors: string;
    stale: string;
    lastRun: string;
    enabled: boolean;
    modelKey: string;
    /** The texts of the Model select's items, in order. */
    options: string[];
    /** The strip's text when it is visible, else "". */
    envStrip: string;
    canRun: boolean;
}

/** The panel of the view that holds `control`. */
export function panelOf(control: UI5Element): PanelTexts {
    const view = viewOf(control);
    const status = (id: string) => view.byId(id) as ObjectStatus;
    const select = view.byId(ID.select) as Select;
    const strip = view.byId(ID.envStrip) as MessageStrip;
    return {
        model: status(ID.model).getText(),
        modelState: String(status(ID.model).getState()),
        vectors: status(ID.vectors).getText(),
        stale: status(ID.stale).getText(),
        lastRun: status(ID.lastRun).getText(),
        enabled: (view.byId(ID.enabled) as Switch).getState(),
        modelKey: select.getSelectedKey(),
        options: select.getItems().map((item) => item.getText()),
        envStrip: strip.getVisible() ? strip.getText() : "",
        canRun: (view.byId(ID.run) as Button).getEnabled()
    };
}

/** The models the deploy dialog offers, as listed. */
export function offersOf(list: UI5Element): string[] {
    return (list as List).getItems().map((item) => (item as StandardListItem).getTitle());
}

/** The text of the message toast on screen, "" when there is none. */
export function toastText(): string {
    return Array.prototype.map.call(document.querySelectorAll(".sapMMessageToast"),
        (toast: Element) => toast.textContent ?? "").join(" ");
}
