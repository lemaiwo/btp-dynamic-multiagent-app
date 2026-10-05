import Press from "sap/ui/test/actions/Press";
import type Input from "sap/m/Input";
import type ComboBox from "sap/m/ComboBox";
import type ListItem from "sap/ui/core/ListItem";
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
        destination: control<ComboBox>(element, DESTINATION).getValue(),
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

// --- the destination field ------------------------------------------------------

/** The destination field: a combo box that also takes a typed name. */
export const DESTINATION = "odataDestination";

/** What the destination field shows. */
export interface DestinationField {
    value: string;
    state: string;
    stateText: string;
    /** What the dropdown offers: [name, the line that describes it]. */
    choices: string[][];
    /** The line under the field, "" when there is none. */
    hint: string;
    /** Whether a screen reader is given the hint as the field's description. */
    hintDescribes: boolean;
    /** The text of the element the description points to, as it is in the page. */
    describedText: string;
    /** The text of the label whose `for` is the field's input. */
    label: string;
    /** What the input shows right now (while typing, the combo box's completion included). */
    shown: string;
}

function destinationInput(element: UI5Element): HTMLInputElement {
    return control<ComboBox>(element, DESTINATION).getFocusDomRef() as HTMLInputElement;
}

/**
 * Types `text` into the destination field key by key, the way keystrokes
 * arrive: each character replaces what is selected (the tail the combo box
 * completed the last key to) and an `input` event is fired, so the combo
 * box completes again as it does for a user. (OPA's EnterText sets the
 * value and leaves the field in one go; it never shows what a half-typed
 * name is completed to.) The field is emptied first.
 */
export function typeDestination(element: UI5Element, text: string): void {
    const input = destinationInput(element);
    input.focus();
    input.setSelectionRange(0, input.value.length);
    text.split("").forEach((key) => {
        const start = input.selectionStart ?? input.value.length;
        const end = input.selectionEnd ?? start;
        input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
        input.value = input.value.slice(0, start) + key + input.value.slice(end);
        input.setSelectionRange(start + 1, start + 1);
        input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: key }));
    });
}

/** A key that is not a character, pressed in the destination field. */
export function keyInDestination(element: UI5Element, key: "Enter" | "ArrowDown"): void {
    const keyCode = key === "Enter" ? 13 : 40;
    destinationInput(element).dispatchEvent(new KeyboardEvent("keydown", {
        key, code: key, keyCode, which: keyCode, bubbles: true, cancelable: true
    } as KeyboardEventInit));
}

/** Leaves the destination field for the one above it, as a click there does. */
export function leaveDestination(element: UI5Element): void {
    control<Input>(element, "odataNotFor").focus();
}

export function destinationOf(element: UI5Element): DestinationField {
    const field = control<ComboBox>(element, DESTINATION);
    const hint = control<Text>(element, "odataDestinationHint");
    const described = (field.getFocusDomRef()?.getAttribute("aria-describedby") ?? "").split(" ");
    return {
        value: field.getValue(),
        state: field.getValueState(),
        stateText: field.getValueStateText(),
        choices: field.getItems().map((item) => [item.getText(), (item as ListItem).getAdditionalText()]),
        hint: hint.getVisible() ? hint.getText(false) : "",
        hintDescribes: described.indexOf(hint.getId()) !== -1,
        describedText: described.map((id) => (id && document.getElementById(id)?.textContent) || "").join(""),
        label: Array.from(document.querySelectorAll("label"))
            .filter((label) => label.htmlFor === destinationInput(element).id)
            .map((label) => label.textContent ?? "").join("|"),
        shown: destinationInput(element).value
    };
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

/**
 * Whether the control `id` is on screen where the user looks: rendered,
 * wholly inside the window, and not covered by something else.
 */
export function inView(element: UI5Element, id: string): boolean {
    const dom = control<Control>(element, id).getDomRef();
    if (!dom) {
        return false;
    }
    const rect = dom.getBoundingClientRect();
    if (rect.height === 0 || rect.top < 0 || rect.bottom > window.innerHeight) {
        return false;
    }
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return !!top && dom.contains(top);
}

/** What was last announced to screen readers, politely. */
export function announced(): string {
    return document.getElementById("__invisiblemessage-polite")?.textContent ?? "";
}

// --- scrolling ---------------------------------------------------------------

/** The element of the page that scrolls. */
export function scrollerOf(element: UI5Element): HTMLElement {
    return control<Page>(element, PAGE).getDomRef("cont") as HTMLElement;
}

/** Where the row titled `title` is on screen (its top edge, in pixels). */
export function rowTop(element: UI5Element, title: string): number {
    return entityItem(element, title).getDomRef()!.getBoundingClientRect().top;
}

/** Whether the page is scrolled so far that the table's header row is
 *  above the visible part of the page. */
export function tableHeaderScrolledAway(element: UI5Element): boolean {
    const header = control<Table>(element, ENTITY_TABLE).getDomRef()!.querySelector("thead") as HTMLElement;
    return header.getBoundingClientRect().bottom < scrollerOf(element).getBoundingClientRect().top;
}

/** Presses the table's "More" button, as a user does. */
export function pressMore(element: UI5Element): void {
    const table = control<Table>(element, ENTITY_TABLE);
    (table as unknown as { $(suffix: string): { trigger(event: string): void } }).$("trigger").trigger("tap");
}

/** Whether the keyboard focus is on (or in) the control. */
export function hasFocus(candidate: UI5Element): boolean {
    const dom = (candidate as Control).getDomRef();
    return !!dom && !!document.activeElement && dom.contains(document.activeElement);
}

// --- the operations table and "Used by" ----------------------------------------

export const OPERATIONS_TABLE = "odataOperationsTable";

/** What one row of the operations table shows. */
export interface OperationRow {
    title: string;
    technical: string;
    description: string;
    boundTo: string;
    parameters: string;
    changesData: boolean;
    enabled: boolean;
    /** The value state of the Enabled box: "Warning" for an enabled write. */
    enabledState: string;
    /** Why no agent can call it, or "" when the row does not say so. */
    uncallable: string;
    /** Why the last click was not taken, or "". */
    note: string;
}

export function operationItems(element: UI5Element): ColumnListItem[] {
    return control<Table>(element, OPERATIONS_TABLE).getItems() as ColumnListItem[];
}

export function operationItem(element: UI5Element, title: string): ColumnListItem {
    return operationItems(element).filter((item) => identifierOf(item).getTitle() === title)[0];
}

/** The "Changes data" or the "Enabled" box of a row. */
export function operationBox(item: ColumnListItem, which: "changes" | "enabled"): CheckBox {
    return item.getCells()[which === "changes" ? 4 : 5] as CheckBox;
}

export function operationRemove(item: ColumnListItem): Button {
    return item.getCells()[6] as Button;
}

export function operationRow(element: UI5Element, title: string): OperationRow {
    const item = operationItem(element, title);
    const second = (item.getCells()[1] as VBox).getItems();
    const description = second[0] as Text;
    return {
        title: identifierOf(item).getTitle(),
        technical: identifierOf(item).getText(),
        description: description.getVisible() ? description.getText(false) : "",
        boundTo: (item.getCells()[2] as Text).getText(false),
        parameters: (item.getCells()[3] as Text).getText(false),
        changesData: operationBox(item, "changes").getSelected(),
        enabled: operationBox(item, "enabled").getSelected(),
        enabledState: operationBox(item, "enabled").getValueState(),
        uncallable: shown(second[1] as ObjectStatus),
        note: shown(second[2] as ObjectStatus)
    };
}

/** The title of the operations section and what its table says when empty. */
export function operationsHeader(element: UI5Element): { title: string; noData: string; titles: string[] } {
    return {
        title: control<Title>(element, "odataOperationsTitle").getText(),
        noData: control<Table>(element, OPERATIONS_TABLE).getNoDataText(),
        titles: operationItems(element).map((item) => identifierOf(item).getTitle())
    };
}

/** The rows of "Used by": agent, the tag under it ("" when none), whether
 *  it may write (with the state of that tag) and its run endpoint. */
export function usedByRows(element: UI5Element): string[][] {
    return (control<Table>(element, "odataUsedByTable").getItems() as ColumnListItem[]).map((item) => {
        const first = (item.getCells()[0] as VBox).getItems();
        const writes = item.getCells()[1] as ObjectStatus;
        return [
            (first[0] as ObjectIdentifier).getTitle(), shown(first[1] as ObjectStatus),
            `${writes.getText()}/${writes.getState()}`, (item.getCells()[2] as Text).getText(false)
        ];
    });
}

/** The strip of the test call: whether it is shown, its type and text, and
 *  whether what it shows holds an element (it must be text alone). */
export function testStripOf(element: UI5Element): { visible: boolean; type: string; text: string; markup: boolean } {
    const strip = control<MessageStrip>(element, "odataTestStrip");
    const message = strip.getDomRef()?.querySelector(".sapMMsgStripMessage");
    return {
        visible: strip.getVisible(), type: strip.getVisible() ? strip.getType() : "",
        text: strip.getVisible() ? strip.getText() : "",
        markup: !!message && message.querySelector("b, script, img, a") !== null
    };
}
