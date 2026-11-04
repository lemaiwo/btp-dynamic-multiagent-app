import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Dialog from "sap/m/Dialog";
import type Text from "sap/m/Text";
import type VBox from "sap/m/VBox";
import type ObjectIdentifier from "sap/m/ObjectIdentifier";
import type ObjectStatus from "sap/m/ObjectStatus";
import type UI5Element from "sap/ui/core/Element";

/**
 * What the "Analytical queries" journeys read off the page. Everything here
 * comes from the rendered controls, not from the model behind them, so a
 * binding or formatter that shows the wrong thing fails a journey.
 */

/** What one row of the list shows, cell by cell. */
export interface InaRowTexts {
    title: string;
    name: string;
    query: string;
    /** "" when the package line is hidden. */
    package: string;
    destination: string;
    runsAs: string;
    runsAsState: string;
    usedBy: string;
    usedByTooltip: string;
    described: string;
    enabled: string;
}

export const VIEW = "InaQueries";
export const TABLE = "inaQueriesTable";
export const DETAIL_VIEW = "InaQueryDetail";

/** The ids of the Create dialog's controls (in the list view). */
export const CREATE = {
    dialog: "inaCreateDialog",
    name: "inaCreateName",
    title: "inaCreateTitle",
    destination: "inaCreateDestination",
    runsAs: "inaCreateRunsAs",
    query: "inaCreateQuery",
    package: "inaCreatePackage",
    purpose: "inaCreatePurpose",
    error: "inaCreateError",
    confirm: "inaCreateConfirm",
    cancel: "inaCreateCancel"
};

export function itemsOf(table: UI5Element): ColumnListItem[] {
    return (table as Table).getItems() as ColumnListItem[];
}

export function nameOf(item: ColumnListItem): string {
    return item.getBindingContext("ina")?.getProperty("name") as string;
}

/** The technical names of the rows the table shows, in order. */
export function namesIn(table: UI5Element): string[] {
    return itemsOf(table).map(nameOf);
}

export function itemOf(table: UI5Element, name: string): ColumnListItem {
    return itemsOf(table).filter((item) => nameOf(item) === name)[0];
}

/** The rendered texts of one row. */
export function rowTexts(item: ColumnListItem): InaRowTexts {
    const cells = item.getCells();
    const identifier = cells[0] as ObjectIdentifier;
    const query = (cells[1] as VBox).getItems() as Text[];
    const runsAs = cells[3] as ObjectStatus;
    const usedBy = cells[4] as Text;
    return {
        title: identifier.getTitle(),
        name: identifier.getText(),
        query: query[0].getText(false),
        package: query[1].getVisible() ? query[1].getText(false) : "",
        destination: (cells[2] as Text).getText(false),
        runsAs: runsAs.getText(),
        runsAsState: runsAs.getState(),
        usedBy: usedBy.getText(false),
        usedByTooltip: String(usedBy.getTooltip() ?? ""),
        described: (cells[5] as Text).getText(false),
        enabled: (cells[6] as ObjectStatus).getText()
    };
}

/** Whether the dialog with the id `id` (any view prefix) is open. */
export function isOpen(dialog: UI5Element): boolean {
    return (dialog as Dialog).isOpen();
}
