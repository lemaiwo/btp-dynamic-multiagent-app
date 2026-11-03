import type Input from "sap/m/Input";
import type Text from "sap/m/Text";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type MessageStrip from "sap/m/MessageStrip";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Switch from "sap/m/Switch";
import type View from "sap/ui/core/mvc/View";
import type UI5Element from "sap/ui/core/Element";
import type ManagedObject from "sap/ui/base/ManagedObject";
import Opa5 from "sap/ui/test/Opa5";
import ElementRegistry from "sap/ui/core/ElementRegistry";

/**
 * What the analytical query detail journey reads off the page: the
 * rendered controls, never the model behind them.
 */

export const DETAIL_VIEW = "InaQueryDetail";
export const PAGE = "inaQueryDetailPage";

/** The ids of the page's controls. */
export const ID = {
    describe: "inaDescribeButton",
    test: "inaTestButton",
    remove: "inaDeleteButton",
    save: "inaSaveButton",
    title: "inaDetailTitle",
    name: "inaDetailName",
    purpose: "inaDetailPurpose",
    notFor: "inaDetailNotFor",
    destination: "inaDetailDestination",
    runsAs: "inaDetailRunsAs",
    query: "inaDetailQuery",
    package: "inaDetailPackage",
    enabled: "inaEnabledSwitch",
    usedBy: "inaDetailUsedBy",
    described: "inaDescribedText",
    dimensions: "inaDimensionsTable",
    measures: "inaMeasuresTable",
    variables: "inaVariablesTable",
    changedElsewhere: "inaChangedElsewhere",
    reload: "inaReload",
    deletedElsewhere: "inaDeletedElsewhere",
    saveError: "inaSaveError",
    resultStrip: "inaResultStrip"
};

/** What the form shows. */
export interface DetailTexts {
    title: string;
    name: string;
    nameEditable: boolean;
    purpose: string;
    notFor: string;
    destination: string;
    runsAs: string;
    query: string;
    package: string;
    enabled: boolean;
    usedBy: string;
    described: string;
}

/** The view a control lives in. */
export function viewOf(element: UI5Element): View {
    let current: ManagedObject | null = element;
    while (current && current.getMetadata().getName() !== "sap.ui.core.mvc.XMLView") {
        current = current.getParent();
    }
    return current as View;
}

function input(view: View, id: string): Input {
    return view.byId(id) as Input;
}

/** The form of the page that holds `page`. */
export function detailOf(page: UI5Element): DetailTexts {
    const view = viewOf(page);
    return {
        title: input(view, ID.title).getValue(),
        name: input(view, ID.name).getValue(),
        nameEditable: input(view, ID.name).getEditable(),
        purpose: input(view, ID.purpose).getValue(),
        notFor: input(view, ID.notFor).getValue(),
        destination: input(view, ID.destination).getValue(),
        runsAs: (view.byId(ID.runsAs) as SegmentedButton).getSelectedKey(),
        query: input(view, ID.query).getValue(),
        package: input(view, ID.package).getValue(),
        enabled: (view.byId(ID.enabled) as Switch).getState(),
        usedBy: (view.byId(ID.usedBy) as Text).getText(false),
        described: (view.byId(ID.described) as Text).getText(false)
    };
}

/** The cell texts of a read-only table, row by row. */
export function tableRows(table: UI5Element): string[][] {
    return ((table as Table).getItems() as ColumnListItem[]).map((item) => (
        item.getCells().map((cell) => (cell as unknown as { getText(): string }).getText())
    ));
}

/** A strip's text when it is visible, else "". */
export function stripText(strip: UI5Element): string {
    const control = strip as MessageStrip;
    return control.getVisible() ? control.getText() : "";
}

/** What the page says when every Describe or Test slot is taken (`inaBusy`). */
export const BUSY_TEXT_SHOWN = "Not run: other Describe or Test calls are running. Try again in a moment.";

/** Asserts, inside a `success`, that no dialog is open. (A `waitFor` on
 *  `sap.m.Dialog` would wait forever when there is none.) */
export function iSeeNoOpenDialog(): void {
    const open = ElementRegistry.filter((element) => (
        element.isA("sap.m.Dialog") && (element as unknown as { isOpen(): boolean }).isOpen()
    ));
    Opa5.assert.strictEqual(open.length, 0, "no dialog is open");
}
