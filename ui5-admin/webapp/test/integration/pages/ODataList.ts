import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Dialog from "sap/m/Dialog";
import type Text from "sap/m/Text";
import type Button from "sap/m/Button";
import type VBox from "sap/m/VBox";
import type HBox from "sap/m/HBox";
import type ObjectIdentifier from "sap/m/ObjectIdentifier";
import type ObjectStatus from "sap/m/ObjectStatus";
import type View from "sap/ui/core/mvc/View";
import type UI5Element from "sap/ui/core/Element";
import type ManagedObject from "sap/ui/base/ManagedObject";
import type { ODataServiceSummary } from "../../../service/types";

/**
 * What the OData services list journey reads off the page. Everything here
 * comes from the rendered controls, not from the model behind them, so a
 * binding or formatter that shows the wrong thing fails a journey.
 */

/** What one row of the list shows, cell by cell. */
export interface RowTexts {
    title: string;
    name: string;
    purpose: string;
    /** The texts of the tags that are visible, in order. */
    tags: string[];
    destination: string;
    version: string;
    runsAs: string;
    runsAsState: string;
    entitySets: string;
    operations: string;
    usedBy: string;
}

/** What the list controller offers a journey beside its event handlers. */
export interface ListController {
    deleteService(service: ODataServiceSummary): Promise<boolean>;
    importFile(file: { size: number; text(): Promise<string> }): Promise<boolean>;
}

export const VIEW = "ODataServices";
export const TABLE = "odataServicesTable";

export function itemsOf(table: UI5Element): ColumnListItem[] {
    return (table as Table).getItems() as ColumnListItem[];
}

export function nameOf(item: ColumnListItem): string {
    return item.getBindingContext("odata")?.getProperty("name") as string;
}

/** The technical names of the rows the table shows, in order. */
export function namesIn(table: UI5Element): string[] {
    return itemsOf(table).map(nameOf);
}

export function itemOf(table: UI5Element, name: string): ColumnListItem {
    return itemsOf(table).filter((item) => nameOf(item) === name)[0];
}

export function serviceOf(table: UI5Element, name: string): ODataServiceSummary {
    return itemOf(table, name).getBindingContext("odata")?.getObject() as ODataServiceSummary;
}

/** The rendered texts of one row. */
export function rowTexts(item: ColumnListItem): RowTexts {
    const cells = item.getCells();
    const first = (cells[0] as VBox).getItems();
    const identifier = first[0] as ObjectIdentifier;
    const tags = ((first[2] as HBox).getItems() as ObjectStatus[])
        .filter((tag) => tag.getVisible())
        .map((tag) => tag.getText());
    const runsAs = cells[3] as ObjectStatus;
    return {
        title: identifier.getTitle(),
        name: identifier.getText(),
        purpose: (first[1] as Text).getText(false),
        tags,
        destination: (cells[1] as Text).getText(false),
        version: (cells[2] as Text).getText(false),
        runsAs: runsAs.getText(),
        runsAsState: runsAs.getState(),
        entitySets: (cells[4] as Text).getText(false),
        operations: (cells[5] as Text).getText(false),
        usedBy: (cells[6] as Text).getText(false)
    };
}

/** The delete button of one row. */
export function deleteButtonOf(item: ColumnListItem): Button {
    return item.getCells()[7] as Button;
}

/** The message a MessageBox shows. */
export function messageOf(dialog: UI5Element): string {
    return (dialog as Dialog)
        .findAggregatedObjects(true, (child: ManagedObject) => child.isA("sap.m.Text"))
        .map((child: ManagedObject) => (child as unknown as Text).getText(false))
        .join(" ");
}

/** The texts of a dialog's buttons, in order. */
export function buttonsOf(dialog: UI5Element): string[] {
    return (dialog as Dialog).getButtons().map((button) => button.getText());
}

/** The controller of the view a control lives in. */
export function controllerOf(element: UI5Element): ListController {
    let current: ManagedObject | null = element;
    while (current && current.getMetadata().getName() !== "sap.ui.core.mvc.XMLView") {
        current = current.getParent();
    }
    return (current as View).getController() as unknown as ListController;
}
