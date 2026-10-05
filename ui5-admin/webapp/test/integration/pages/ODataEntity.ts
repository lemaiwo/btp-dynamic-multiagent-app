import type Button from "sap/m/Button";
import type CheckBox from "sap/m/CheckBox";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Dialog from "sap/m/Dialog";
import type HBox from "sap/m/HBox";
import type IconTabBar from "sap/m/IconTabBar";
import type IconTabFilter from "sap/m/IconTabFilter";
import type Input from "sap/m/Input";
import type MessageStrip from "sap/m/MessageStrip";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Table from "sap/m/Table";
import type Text from "sap/m/Text";
import type TextArea from "sap/m/TextArea";
import type ToggleButton from "sap/m/ToggleButton";
import type VBox from "sap/m/VBox";
import type Control from "sap/ui/core/Control";
import type UI5Element from "sap/ui/core/Element";
import { viewOf } from "./ODataDetail";

/**
 * What the entity set dialog journey reads off the dialog: the rendered
 * controls, not the model behind them.
 */

export const DIALOG = "odataEntityDialog";

/** A control of the dialog (its fragment carries the view's id). */
export function part<T extends Control>(dialog: UI5Element, id: string): T {
    return viewOf(dialog).byId(id) as T;
}

export type Grant = "read" | "filter" | "write";
const GRANTS: readonly Grant[] = ["read", "filter", "write"];

/** What one row of the fields table shows. */
export interface FieldRow {
    name: string;
    type: string;
    key: boolean;
    label: string;
    read: boolean;
    filter: boolean;
    write: boolean;
    hint: string;
    meanings: string;
    /** The hint about value meanings of a non-Edm type, or "". */
    enumHint: string;
    personal: boolean;
    /** The personal-data tag as shown ("" when the field is not marked) and its state. */
    tag: string;
    tagState: string;
    /** Why the last tick was changed or not taken, or "". */
    note: string;
    /** The question a tick on Read of a personal-data field asks in place, or "". */
    confirm: string;
}

export function header(dialog: UI5Element): {
    title: string; business: string; name: string; nameEditable: boolean; description: string;
    keys: string[]; keysHint: string; keyWarning: string; issues: string; tabs: string[]; showing: string;
} {
    const strip = (id: string): string => {
        const control = part<MessageStrip>(dialog, id);
        return control.getVisible() ? control.getText() : "";
    };
    return {
        title: (dialog as Dialog).getTitle(),
        business: part<Input>(dialog, "entityTitle").getValue(),
        name: part<Input>(dialog, "entityName").getValue(),
        nameEditable: part<Input>(dialog, "entityName").getEditable(),
        description: part<TextArea>(dialog, "entityDescription").getValue(),
        keys: (part<HBox>(dialog, "entityKeys").getItems() as ObjectStatus[]).map((key) => key.getText()),
        keysHint: part<Text>(dialog, "entityKeysHint").getText(false),
        keyWarning: strip("entityKeyWarning"),
        issues: strip("entityIssues"),
        tabs: (part<IconTabBar>(dialog, "entityTabs").getItems() as IconTabFilter[]).map((tab) => tab.getText()),
        showing: part<Text>(dialog, "entityFieldsShowing").getText(false)
    };
}

export function tab(dialog: UI5Element, key: string): IconTabFilter {
    return (part<IconTabBar>(dialog, "entityTabs").getItems() as IconTabFilter[])
        .filter((candidate) => candidate.getKey() === key)[0];
}

/** The rendered rows of the fields table. */
export function fieldItems(dialog: UI5Element): ColumnListItem[] {
    return part<Table>(dialog, "entityFieldsTable").getItems() as ColumnListItem[];
}

function nameOf(item: ColumnListItem): string {
    return ((item.getCells()[0] as VBox).getItems()[0] as Text).getText(false);
}

export function fieldNames(dialog: UI5Element): string[] {
    return fieldItems(dialog).map(nameOf);
}

export function fieldItem(dialog: UI5Element, name: string): ColumnListItem {
    return fieldItems(dialog).filter((item) => nameOf(item) === name)[0];
}

/** The checkbox of `grant` in a row. */
export function box(item: ColumnListItem, grant: Grant): CheckBox {
    return item.getCells()[2 + GRANTS.indexOf(grant)] as CheckBox;
}

export function labelInput(item: ColumnListItem): Input {
    return item.getCells()[1] as Input;
}

export function hintInput(item: ColumnListItem): Input {
    return (item.getCells()[5] as VBox).getItems()[0] as Input;
}

export function meaningsInput(item: ColumnListItem): Input {
    return (item.getCells()[5] as VBox).getItems()[1] as Input;
}

export function personalToggle(item: ColumnListItem): ToggleButton {
    return (item.getCells()[6] as VBox).getItems()[0] as ToggleButton;
}

/** The two buttons of the question a row asks in place: [confirm, keep]. */
export function confirmButtons(item: ColumnListItem): Button[] {
    const question = (item.getCells()[0] as VBox).getItems()[4] as VBox;
    return (question.getItems()[1] as HBox).getItems() as Button[];
}

function shown(status: ObjectStatus): string {
    return status.getVisible() ? status.getText() : "";
}

export function fieldRow(dialog: UI5Element, name: string): FieldRow {
    const item = fieldItem(dialog, name);
    const first = (item.getCells()[0] as VBox).getItems();
    const question = first[4] as VBox;
    const meanings = (item.getCells()[5] as VBox).getItems();
    const tag = (item.getCells()[6] as VBox).getItems()[1] as ObjectStatus;
    return {
        name: nameOf(item),
        type: (first[1] as Text).getText(false),
        key: (first[2] as ObjectStatus).getVisible(),
        label: labelInput(item).getValue(),
        read: box(item, "read").getSelected(),
        filter: box(item, "filter").getSelected(),
        write: box(item, "write").getSelected(),
        hint: hintInput(item).getValue(),
        meanings: meaningsInput(item).getValue(),
        enumHint: shown(meanings[2] as ObjectStatus),
        personal: personalToggle(item).getPressed(),
        tag: shown(tag),
        tagState: tag.getVisible() ? tag.getState() : "",
        note: shown(first[3] as ObjectStatus),
        confirm: question.getVisible() ? (question.getItems()[0] as Text).getText(false) : ""
    };
}

/** What one row of the navigations table shows. */
export function navigationRows(dialog: UI5Element): {
    name: string; target: string; kind: string; description: string; follow: string; state: string;
}[] {
    return (part<Table>(dialog, "entityNavTable").getItems() as ColumnListItem[]).map((item) => {
        const cells = item.getCells();
        return {
            name: (cells[0] as Text).getText(false),
            target: (cells[1] as Text).getText(false),
            kind: (cells[2] as Text).getText(false),
            description: (cells[3] as Input).getValue(),
            follow: (cells[4] as ObjectStatus).getText(),
            state: (cells[4] as ObjectStatus).getState()
        };
    });
}

export function navigationDescription(dialog: UI5Element, index: number): Input {
    return (part<Table>(dialog, "entityNavTable").getItems() as ColumnListItem[])[index].getCells()[3] as Input;
}

export function exampleItems(dialog: UI5Element): ColumnListItem[] {
    return part<Table>(dialog, "entityExamples").getItems() as ColumnListItem[];
}

export type ExamplePart = "description" | "select" | "filter" | "orderby" | "top";
const EXAMPLE_PARTS: readonly ExamplePart[] = ["description", "select", "filter", "orderby", "top"];

/** The input of one part of an example query. */
export function exampleInput(item: ColumnListItem, which: ExamplePart): Input {
    const cell = item.getCells()[EXAMPLE_PARTS.indexOf(which)];
    return (which === "description" ? (cell as VBox).getItems()[0] : cell) as Input;
}

export function exampleRemove(item: ColumnListItem): Button {
    return item.getCells()[5] as Button;
}

/** What one example query shows; `warning` is "" when agents see all of it. */
export function exampleRows(dialog: UI5Element): Record<ExamplePart | "warning", string>[] {
    return exampleItems(dialog).map((item) => ({
        description: exampleInput(item, "description").getValue(),
        select: exampleInput(item, "select").getValue(),
        filter: exampleInput(item, "filter").getValue(),
        orderby: exampleInput(item, "orderby").getValue(),
        top: exampleInput(item, "top").getValue(),
        warning: shown((item.getCells()[0] as VBox).getItems()[1] as ObjectStatus)
    }));
}
