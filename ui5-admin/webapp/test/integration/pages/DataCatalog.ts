import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type ObjectIdentifier from "sap/m/ObjectIdentifier";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Text from "sap/m/Text";
import type List from "sap/m/List";
import type Select from "sap/m/Select";
import type Button from "sap/m/Button";
import type StandardListItem from "sap/m/StandardListItem";
import type UI5Element from "sap/ui/core/Element";

/**
 * What the data catalogue journeys read off the pages and how they pick in
 * them: the rendered controls, never the model behind them.
 */

export const LIST_VIEW = "DataCatalog";
export const DETAIL_VIEW = "DataCatalogDetail";
export const GRAPH_VIEW = "DataCatalogGraph";

export function rowsOf(table: UI5Element): ColumnListItem[] {
    return (table as Table).getItems().filter((item) => item.isA("sap.m.ColumnListItem")) as ColumnListItem[];
}

/** Picks `key` in a bound Select the way a user does: the selection, then its change event. */
export function choose(select: Select, key: string): void {
    select.setSelectedKey(key);
    select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
}

/** A row of the Links table as the admin reads it: from, relation, to, origin, and whether it offers a remove. */
export function linkOf(row: ColumnListItem): { from: string; relation: string; to: string; origin: string; remove: boolean } {
    const cells = row.getCells();
    return {
        from: (cells[0] as Text).getText(false),
        relation: (cells[1] as ObjectIdentifier).getTitle(),
        to: (cells[2] as Text).getText(false),
        origin: (cells[3] as ObjectStatus).getText(),
        remove: (cells[4] as Button).getVisible()
    };
}

export function linksOf(table: UI5Element): ReturnType<typeof linkOf>[] {
    return rowsOf(table).map(linkOf);
}

/** The titles of the Add link dialog's candidates. */
export function candidatesOf(list: UI5Element): string[] {
    return (list as List).getItems().map((item) => (item as StandardListItem).getTitle());
}
