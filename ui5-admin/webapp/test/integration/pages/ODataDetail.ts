import Press from "sap/ui/test/actions/Press";
import type Input from "sap/m/Input";
import type TextArea from "sap/m/TextArea";
import type Text from "sap/m/Text";
import type Page from "sap/m/Page";
import type Button from "sap/m/Button";
import type CheckBox from "sap/m/CheckBox";
import type ColumnListItem from "sap/m/ColumnListItem";
import type ObjectIdentifier from "sap/m/ObjectIdentifier";
import type Table from "sap/m/Table";
import type Title from "sap/m/Title";
import type VBox from "sap/m/VBox";
import type Switch from "sap/m/Switch";
import type MessageStrip from "sap/m/MessageStrip";
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
        counter: control<ObjectStatus>(element, "odataPurposeCounter").getText(),
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

/** The state of the purpose counter: "Error" once the text is too long. */
export function counterState(element: UI5Element): string {
    return control<ObjectStatus>(element, "odataPurposeCounter").getState();
}

/** Whether a message strip of the page is shown, and what it says. */
export function stripOf(element: UI5Element, id: string): { visible: boolean; text: string } {
    const strip = control<MessageStrip>(element, id);
    return { visible: strip.getVisible(), text: strip.getText() };
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

// --- the entity sets table ---------------------------------------------------

export const ENTITY_TABLE = "odataEntityTable";

/** The operations in the order of their columns. */
export const OPS = ["list", "get", "create", "update", "delete"] as const;
export type Op = typeof OPS[number];

/** What one row of the entity sets table shows. */
export interface EntityRow {
    title: string;
    technical: string;
    /** The description, or "" when the row shows the tag instead. */
    description: string;
    /** The state of the "No description yet" tag, or "" when it is not shown. */
    noDescription: string;
    /** The operations whose checkbox is ticked. */
    ticked: Op[];
    fields: string;
    /** The messages on the row that are shown: navigation hint, refused tick, refused save. */
    hint: string;
    note: string;
    error: string;
}

/** The rendered rows of the entity sets table. */
export function entityItems(element: UI5Element): ColumnListItem[] {
    return control<Table>(element, ENTITY_TABLE).getItems() as ColumnListItem[];
}

function identifierOf(item: ColumnListItem): ObjectIdentifier {
    return (item.getCells()[0] as VBox).getItems()[0] as ObjectIdentifier;
}

/** The titles of the rendered rows, in order. */
export function entityTitles(element: UI5Element): string[] {
    return entityItems(element).map((item) => identifierOf(item).getTitle());
}

/** The row with the business title `title`. */
export function entityItem(element: UI5Element, title: string): ColumnListItem {
    return entityItems(element).filter((item) => identifierOf(item).getTitle() === title)[0];
}

/** The checkbox of `op` in a row. */
export function opBox(item: ColumnListItem, op: Op): CheckBox {
    return item.getCells()[2 + OPS.indexOf(op)] as CheckBox;
}

function shown(status: ObjectStatus): string {
    return status.getVisible() ? status.getText() : "";
}

export function entityRow(element: UI5Element, title: string): EntityRow {
    const item = entityItem(element, title);
    const second = (item.getCells()[1] as VBox).getItems();
    const description = second[0] as Text;
    const tag = second[1] as ObjectStatus;
    return {
        title: identifierOf(item).getTitle(),
        technical: identifierOf(item).getText(),
        description: description.getVisible() ? description.getText(false) : "",
        noDescription: tag.getVisible() ? tag.getState() : "",
        ticked: OPS.filter((op) => opBox(item, op).getSelected()),
        fields: (item.getCells()[7] as Text).getText(false),
        hint: shown(second[2] as ObjectStatus),
        note: shown(second[3] as ObjectStatus),
        error: shown(second[4] as ObjectStatus)
    };
}

/**
 * The accessible name of a checkbox as a screen reader builds it: the texts
 * of the elements its `aria-labelledby` names, in order.
 */
export function accessibleName(box: CheckBox): string {
    const ids = (box.getDomRef()?.getAttribute("aria-labelledby") ?? "").split(" ").filter(Boolean);
    return ids.map((id) => document.getElementById(id)?.textContent ?? "").filter(Boolean).join(" ");
}

/** The title of the entity sets section and the note about the metadata. */
export function entityHeader(element: UI5Element): { title: string; metadata: string; canAdd: boolean } {
    return {
        title: control<Title>(element, "odataEntityTitle").getText(),
        metadata: control<Text>(element, "odataMetadataInfo").getText(false),
        canAdd: control<Button>(element, "odataAddEntitySetButton").getEnabled()
    };
}
