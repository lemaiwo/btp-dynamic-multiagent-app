import Press from "sap/ui/test/actions/Press";
import type Input from "sap/m/Input";
import type TextArea from "sap/m/TextArea";
import type Text from "sap/m/Text";
import type Page from "sap/m/Page";
import type Button from "sap/m/Button";
import type Switch from "sap/m/Switch";
import type ObjectStatus from "sap/m/ObjectStatus";
import type SegmentedButton from "sap/m/SegmentedButton";
import type View from "sap/ui/core/mvc/View";
import type Control from "sap/ui/core/Control";
import type UI5Element from "sap/ui/core/Element";
import type ManagedObject from "sap/ui/base/ManagedObject";

/**
 * What the OData service detail journey reads off the page: the rendered
 * controls, not the model behind them, so a binding or formatter that shows
 * the wrong thing fails a journey.
 */

export const VIEW = "ODataServiceDetail";
export const PAGE = "odataServiceDetailPage";

/** What the General section shows. */
export interface FormTexts {
    title: string;
    name: string;
    nameEditable: boolean;
    purpose: string;
    counter: string;
    notFor: string;
    destination: string;
    runsAs: string;
    runsAsHint: string;
    version: string;
    servicePath: string;
    enabled: boolean;
}

/** The view a control lives in. */
export function viewOf(element: UI5Element): View {
    let current: ManagedObject | null = element;
    while (current && current.getMetadata().getName() !== "sap.ui.core.mvc.XMLView") {
        current = current.getParent();
    }
    return current as View;
}

function control<T extends Control>(element: UI5Element, id: string): T {
    return viewOf(element).byId(id) as T;
}

export function pageTitle(element: UI5Element): string {
    return control<Page>(element, PAGE).getTitle();
}

export function formOf(element: UI5Element): FormTexts {
    return {
        title: control<Input>(element, "odataTitle").getValue(),
        name: control<Input>(element, "odataName").getValue(),
        nameEditable: control<Input>(element, "odataName").getEditable(),
        purpose: control<TextArea>(element, "odataPurpose").getValue(),
        counter: control<Text>(element, "odataPurposeCounter").getText(false),
        notFor: control<Input>(element, "odataNotFor").getValue(),
        destination: control<Input>(element, "odataDestination").getValue(),
        runsAs: control<SegmentedButton>(element, "odataRunsAs").getSelectedKey(),
        runsAsHint: control<Text>(element, "odataRunsAsHint").getText(false),
        version: control<SegmentedButton>(element, "odataVersion").getSelectedKey(),
        servicePath: control<Input>(element, "odataServicePath").getValue(),
        enabled: control<Switch>(element, "odataEnabledSwitch").getState()
    };
}

/** The texts of the header tags that are visible, in order. */
export function tagsOf(element: UI5Element): string[] {
    return ["odataTagVersion", "odataTagRunsAs", "odataTagWrite", "odataTagDisabled"]
        .map((id) => control<ObjectStatus>(element, id))
        .filter((tag) => tag.getVisible())
        .map((tag) => tag.getText());
}

/** The value state of a field and the text that explains it. */
export function stateOf(element: UI5Element, id: string): { state: string; text: string } {
    const field = control<Input>(element, id);
    return { state: field.getValueState(), text: field.getValueStateText() };
}

/** Whether the three header actions can be pressed. */
export function actionsOf(element: UI5Element): { save: boolean; duplicate: boolean; remove: boolean } {
    return {
        save: control<Button>(element, "odataSaveButton").getEnabled(),
        duplicate: control<Button>(element, "odataDuplicateButton").getEnabled(),
        remove: control<Button>(element, "odataDeleteButton").getEnabled()
    };
}

/** The texts of the message toasts on screen. */
export function toasts(): string[] {
    return Array.from(document.querySelectorAll(".sapMMessageToast")).map((toast) => toast.textContent ?? "");
}

/** Matches the control of an open dialog whose id ends with `id` (a
 *  fragment's controls carry the view's id as a prefix). */
export function withId(id: string): (candidate: UI5Element) => boolean {
    return function (candidate: UI5Element): boolean {
        const full = candidate.getId();
        return full === id || full.substring(full.length - id.length - 2) === `--${id}`;
    };
}

/** Presses the segment `key` of a segmented button, as a user does: on the
 *  button the control renders for that item. */
export function pressSegment(segmented: UI5Element, key: string): void {
    const control = segmented as SegmentedButton;
    const index = control.getItems().map((item) => item.getKey()).indexOf(key);
    // `buttons` is the aggregation of rendered buttons behind `items`.
    const buttons = (control as unknown as { getButtons(): Button[] }).getButtons();
    new Press().executeOn(buttons[index]);
}
