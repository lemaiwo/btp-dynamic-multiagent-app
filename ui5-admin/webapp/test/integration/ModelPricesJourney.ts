import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Dialog from "sap/m/Dialog";
import type Input from "sap/m/Input";
import type Table from "sap/m/Table";
import type ObjectStatus from "sap/m/ObjectStatus";
import type ColumnListItem from "sap/m/ColumnListItem";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import Common, { backend } from "./pages/Common";
import { toastText } from "./pages/Embeddings";
import { AMOUNT_RULE } from "./FakeModelPrices";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Model prices journey");

const VIEW = "Settings";
const TABLE = "mpcTable";
const READ = "GET model-prices";
const SAVE = "PUT model-prices";

/** The cell controls of a row: model, input, cached, output, status, remove. */
function cellsOf(table: Table, row: number): Control[] {
    const item = table.getItems()[row] as ColumnListItem | undefined;
    return item ? item.getCells() as Control[] : [];
}

const FIELD_CELL: Record<string, number> = { model: 0, input: 1, cached: 2, output: 3 };

/** The model column of each row, in order. */
function models(table: Table): string[] {
    return table.getItems().map((item) => ((item as ColumnListItem).getCells()[0] as Input).getValue());
}

function noPrice(table: Table, row: number): boolean {
    const status = cellsOf(table, row)[4] as ObjectStatus | undefined;
    return !!status && status.getVisible() && status.getText() === "No price";
}

function iSeeTheTable(Then: Common, what: string, ready: (table: Table) => boolean, assert: (table: Table) => void): void {
    Then.waitFor({
        id: TABLE,
        viewName: VIEW,
        check: (table: UI5Element) => ready(table as Table),
        success: (table: UI5Element) => assert(table as Table),
        errorMessage: `The model prices table does not show: ${what}`
    });
}

function iTypeInRow(When: Common, row: number, field: string, text: string): void {
    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        check: (table: UI5Element) => !!cellsOf(table as Table, row)[FIELD_CELL[field]],
        actions: (table: UI5Element | null) => {
            new EnterText({ text, clearTextFirst: true }).executeOn(cellsOf(table as Table, row)[FIELD_CELL[field]]);
        },
        errorMessage: `No ${field} field in row ${row}`
    });
}

function iTypeTheCuPrice(When: Common, text: string): void {
    When.waitFor({ id: "mpcCuPrice", viewName: VIEW, actions: new EnterText({ text, clearTextFirst: true }) });
}

function iPress(When: Common, id: string): void {
    When.waitFor({ id, viewName: VIEW, actions: new Press() });
}

function isMessage(dialog: UI5Element): boolean {
    return (dialog as Dialog).getType() === "Message";
}

function iSeeAMessage(Then: Common, what: string, assert: (text: string, dialog: Dialog) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, `one message box is open: ${what}`);
            const dialog = dialogs[0] as Dialog;
            assert(dialog.getDomRef()?.textContent ?? "", dialog);
        },
        errorMessage: `No message box: ${what}`
    });
}

function iCloseTheMessage(When: Common): void {
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        actions: function (element: UI5Element | null) {
            new Press().executeOn((element as Dialog).getButtons()[0]);
        },
        errorMessage: "No message box to close"
    });
}

/** The field state of a row's cell. */
function stateOf(table: Table, row: number, field: string): { state: string; text: string } {
    const input = cellsOf(table, row)[FIELD_CELL[field]] as Input;
    return { state: String(input.getValueState()), text: input.getValueStateText() };
}

opaTest("the panel shows the CU price, the stored rate and the model without a price", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    iSeeTheTable(Then, "two rows", (table) => table.getItems().length === 2, (table) => {
        Opa5.assert.deepEqual(models(table), ["claude-sonnet-4-5", "gpt-4o"]);
        const stored = cellsOf(table, 0).slice(1, 4).map((cell) => (cell as Input).getValue());
        Opa5.assert.deepEqual(stored, ["0.25", "", "1.2"], "input, cached (blank = as input), output");
        Opa5.assert.notOk(noPrice(table, 0), "the stored row has a price");
        Opa5.assert.ok(noPrice(table, 1), "gpt-4o: No price");
        Opa5.assert.deepEqual(cellsOf(table, 1).slice(1, 4).map((cell) => (cell as Input).getValue()), ["", "", ""]);
        Opa5.assert.strictEqual(backend.countRequests(READ), 1);
    });
    Then.waitFor({
        id: "mpcCuPrice",
        viewName: VIEW,
        success: (input: UI5Element) => Opa5.assert.strictEqual((input as Input).getValue(), "7.5")
    });

    Then.iStopTheApp();
});

opaTest("rates for the unpriced model and a new CU price are saved as typed; the row loses No price", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "the unpriced row", (table) => noPrice(table, 1), () => Opa5.assert.ok(true));

    iTypeInRow(When, 1, "input", "0.500");
    iTypeInRow(When, 1, "output", "1.000000001");
    iTypeTheCuPrice(When, "8.25");
    Then.waitFor({
        id: "mpcUnsaved",
        viewName: VIEW,
        check: (status: UI5Element) => (status as ObjectStatus).getVisible(),
        success: () => Opa5.assert.ok(true, "unsaved changes are said")
    });
    iPress(When, "mpcSaveButton");

    Then.waitFor({
        check: () => toastText().indexOf("Prices saved.") !== -1,
        timeout: 25,
        success: () => {
            Opa5.assert.deepEqual(backend.bodies[SAVE], {
                expected_updated_at: "2026-11-20T10:00:00+00:00",
                capacity_unit_price: "8.25",
                prices: [
                    { model: "claude-sonnet-4-5", input_rate: "0.25", output_rate: "1.2", cache_read_rate: null },
                    { model: "gpt-4o", input_rate: "0.500", output_rate: "1.000000001", cache_read_rate: null }
                ]
            }, "the stamp it was read with, the rates as typed, as strings");
        },
        errorMessage: "No toast after the save"
    });
    iSeeTheTable(Then, "no unpriced row", (table) => table.getItems().length === 2 && !noPrice(table, 1), (table) => {
        Opa5.assert.deepEqual(models(table), ["claude-sonnet-4-5", "gpt-4o"]);
        Opa5.assert.notOk(noPrice(table, 1), "gpt-4o has a price now");
    });

    Then.iStopTheApp();
});

opaTest("Add model and remove a row; a rate that is no plain decimal is marked and nothing is sent", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "two rows", (table) => table.getItems().length === 2, () => Opa5.assert.ok(true));

    iPress(When, "mpcAddButton");
    iSeeTheTable(Then, "a third row", (table) => table.getItems().length === 3, (table) => {
        Opa5.assert.strictEqual(models(table)[2], "", "an empty new row");
    });
    iTypeInRow(When, 2, "model", "mistral-large");
    iTypeInRow(When, 2, "input", "1,5");
    iTypeInRow(When, 2, "output", "2");
    iPress(When, "mpcSaveButton");

    iSeeAMessage(Then, "the check before saving", (text) => {
        Opa5.assert.ok(text.indexOf("Correct the marked fields") !== -1, text);
    });
    // A message box blocks the page: the marks are read once it is closed.
    iCloseTheMessage(When);
    iSeeTheTable(Then, "the marked rate", () => true, (table) => {
        const mark = stateOf(table, 2, "input");
        Opa5.assert.strictEqual(mark.state, "Error");
        Opa5.assert.strictEqual(mark.text.indexOf("1,5"), -1, "the mark does not quote the value");
        Opa5.assert.ok(mark.text.indexOf("at most 9 decimals") !== -1, mark.text);
        Opa5.assert.strictEqual(backend.countRequests(SAVE), 0, "nothing was sent");
    });

    // Remove the new row: the table is back to what was read.
    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        actions: (table: UI5Element | null) => new Press().executeOn(cellsOf(table as Table, 2)[5])
    });
    iSeeTheTable(Then, "two rows again", (table) => table.getItems().length === 2, (table) => {
        Opa5.assert.deepEqual(models(table), ["claude-sonnet-4-5", "gpt-4o"]);
    });

    Then.iStopTheApp();
});

opaTest("a save after somebody else saved is a 409: the prices are read again and the admin is told", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "two rows", (table) => table.getItems().length === 2, () => {
        backend.modelPrices.bump();
    });
    iTypeTheCuPrice(When, "9");
    iPress(When, "mpcSaveButton");

    iSeeAMessage(Then, "the stale warning", (text) => {
        Opa5.assert.ok(text.indexOf("Somebody else changed the prices meanwhile") !== -1, text);
    });
    iCloseTheMessage(When);
    Then.waitFor({
        id: "mpcCuPrice",
        viewName: VIEW,
        check: () => backend.countRequests(READ) === 2,
        success: (input: UI5Element) => {
            Opa5.assert.strictEqual((input as Input).getValue(), "7.5", "the stored price, read again");
            Opa5.assert.strictEqual(backend.modelPrices.cuPrice, "7.5", "nothing was written");
        },
        errorMessage: "The prices were not read again"
    });

    Then.iStopTheApp();
});

opaTest("a 422 of the server marks the field it names", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "two rows", (table) => table.getItems().length === 2, () => {
        backend.failNext = {
            path: "model-prices", method: "PUT", status: 422,
            body: { detail: `prices[1].output_rate: ${AMOUNT_RULE}` }
        };
    });
    iTypeInRow(When, 1, "input", "1");
    iTypeInRow(When, 1, "output", "2");
    iPress(When, "mpcSaveButton");

    iSeeAMessage(Then, "the refusal", (text) => {
        Opa5.assert.ok(text.indexOf("The prices were not saved") !== -1, text);
    });
    iCloseTheMessage(When);
    iSeeTheTable(Then, "the marked output rate", (table) => stateOf(table, 1, "output").state === "Error", (table) => {
        Opa5.assert.strictEqual(stateOf(table, 1, "output").text, `prices[1].output_rate: ${AMOUNT_RULE}`);
        Opa5.assert.strictEqual(stateOf(table, 1, "input").state, "None", "only the field it names");
        Opa5.assert.strictEqual(stateOf(table, 0, "output").state, "None");
    });
    // An edit of the marked field clears its mark.
    iTypeInRow(When, 1, "output", "3");
    iSeeTheTable(Then, "the mark cleared", (table) => stateOf(table, 1, "output").state === "None",
        () => Opa5.assert.ok(true, "the mark is gone after an edit"));

    Then.iStopTheApp();
});
