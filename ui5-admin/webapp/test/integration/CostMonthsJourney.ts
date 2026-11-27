import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import InstanceManager from "sap/m/InstanceManager";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type ObjectIdentifier from "sap/m/ObjectIdentifier";
import type MessageStrip from "sap/m/MessageStrip";
import type Text from "sap/m/Text";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import { pressSegment } from "./pages/ODataDetail";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("AI Core cost journey");

const VIEW = "Settings";
const TABLE = "cmTable";
const READ = "GET cost/months";

/** A row as the admin reads it: month, note, then the five figures. */
function rowOf(table: Table, index: number): string[] {
    const item = table.getItems()[index] as ColumnListItem | undefined;
    if (!item) {
        return [];
    }
    const cells = item.getCells();
    const month = cells[0] as ObjectIdentifier;
    return [month.getTitle(), month.getText(), ...cells.slice(1).map((cell) => (cell as Text).getText(false))];
}

function iSeeTheTable(Then: Common, what: string, ready: (table: Table) => boolean, assert: (table: Table) => void): void {
    Then.waitFor({
        id: TABLE,
        viewName: VIEW,
        check: (table: UI5Element) => ready(table as Table),
        success: (table: UI5Element) => assert(table as Table),
        errorMessage: `The AI Core cost table does not show: ${what}`
    });
}

function iSeeTheStrip(Then: Common, id: string, what: string, assert: (strip: MessageStrip) => void): void {
    Then.waitFor({
        id,
        viewName: VIEW,
        check: (strip: UI5Element) => (strip as MessageStrip).getVisible(),
        success: (strip: UI5Element) => assert(strip as MessageStrip),
        errorMessage: `No strip: ${what}`
    });
}

function iPress(When: Common, id: string): void {
    When.waitFor({ id, viewName: VIEW, actions: new Press() });
}

/** No message box or other dialog is open. */
function iSeeNoDialog(Then: Common): void {
    Then.waitFor({
        success: () => {
            const open = InstanceManager.getOpenDialogs().length;
            Opa5.assert.strictEqual(open, 0, "no dialog: the failure stays in the panel");
        }
    });
}

opaTest("with the bill: month, billed, units, EUR per unit, estimate and difference; the open month is not final", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    iSeeTheTable(Then, "six months", (table) => table.getItems().length === 6, (table) => {
        Opa5.assert.deepEqual(rowOf(table, 0),
            ["November 2026", "not final", "€ 12.00", "8", "€ 1.50", "≈ € 10.02", "+ € 1.98"]);
        Opa5.assert.deepEqual(rowOf(table, 1),
            ["October 2026", "", "€ 7.50", "5", "€ 1.50", "≈ € 6.00 (partly priced)", "—"]);
        Opa5.assert.deepEqual(rowOf(table, 2),
            ["September 2026", "No AI Core charge reported for this month", "—", "—", "—", "—", "—"]);
        Opa5.assert.deepEqual(rowOf(table, 3).slice(0, 7),
            ["August 2026", "", "€ 3.00", "2", "€ 1.50", "—", "—"], "no estimate, no difference");
        Opa5.assert.strictEqual(backend.costMonths.lastMonths, 6, "six months by default");
        Opa5.assert.strictEqual(backend.countRequests(READ), 1);
    });
    Then.waitFor({
        id: "cmServiceLine",
        viewName: VIEW,
        visible: false,
        success: (strip: UI5Element) => Opa5.assert.notOk((strip as MessageStrip).getVisible(), "no service line when it answered")
    });
    Then.waitFor({
        id: "cmNoCuPrice",
        viewName: VIEW,
        visible: false,
        success: (strip: UI5Element) => Opa5.assert.notOk((strip as MessageStrip).getVisible(), "a CU price is set: no hint")
    });
    Then.waitFor({
        id: "cmHint",
        viewName: VIEW,
        success: (hint: UI5Element) => Opa5.assert.ok((hint as Text).getText(false).indexOf("other consumers of the AI Core instance") !== -1)
    });
    Then.waitFor({
        id: "mpcCuPriceHint",
        viewName: VIEW,
        success: (hint: UI5Element) => Opa5.assert.ok((hint as Text).getText(false).indexOf("EUR per capacity unit") !== -1,
            "the model prices panel points to the column")
    });

    Then.iStopTheApp();
});

opaTest("without the usage service: one line says what to set up, estimates only", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "the first read", (table) => table.getItems().length === 6, () => Opa5.assert.ok(true));

    When.waitFor({
        success: () => {
            backend.costMonths.usageService = "off";
        }
    });
    iPress(When, "cmRefresh");

    iSeeTheStrip(Then, "cmServiceLine", "the service is off", (strip) => {
        Opa5.assert.ok(strip.getText().indexOf("default BTP_USAGE") !== -1, strip.getText());
        Opa5.assert.ok(strip.getText().indexOf("Estimates only") !== -1);
        Opa5.assert.strictEqual(strip.getType(), "Information");
    });
    iSeeTheTable(Then, "estimates only", (table) => rowOf(table, 0)[2] === "—", (table) => {
        Opa5.assert.deepEqual(rowOf(table, 0), ["November 2026", "not final", "—", "—", "—", "≈ € 10.02", "—"]);
        Opa5.assert.deepEqual(rowOf(table, 2).slice(1), ["", "—", "—", "—", "—", "—"], "no line per month");
        Opa5.assert.strictEqual(backend.countRequests(READ), 2, "Refresh read again");
    });

    Then.iStopTheApp();
});

opaTest("12 months asks for 12 and shows them", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "six months", (table) => table.getItems().length === 6, () => Opa5.assert.ok(true));

    When.waitFor({
        id: "cmMonths",
        viewName: VIEW,
        success: (segmented: UI5Element) => pressSegment(segmented, "12")
    });

    iSeeTheTable(Then, "twelve months", (table) => table.getItems().length === 12, (table) => {
        Opa5.assert.strictEqual(backend.costMonths.lastMonths, 12, "months=12 was requested");
        Opa5.assert.strictEqual(rowOf(table, 11)[0], "December 2025", "the oldest month last");
    });

    Then.iStopTheApp();
});

opaTest("a failed read is a strip in the panel with a retry, never a dialog", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings", { path: "cost/months", status: 500, body: { detail: "Internal Server Error" } });

    iSeeTheStrip(Then, "cmLoadError", "the read failed", (strip) => {
        Opa5.assert.ok(strip.getText().indexOf("The AI Core cost could not be read") !== -1, strip.getText());
    });
    iSeeNoDialog(Then);

    iPress(When, "cmRetry");
    iSeeTheTable(Then, "the months after the retry", (table) => table.getVisible() && table.getItems().length === 6, () => {
        Opa5.assert.strictEqual(backend.countRequests(READ), 2);
    });
    Then.waitFor({
        id: "cmLoadError",
        viewName: VIEW,
        visible: false,
        success: (strip: UI5Element) => Opa5.assert.notOk((strip as MessageStrip).getVisible(), "the strip is gone")
    });

    Then.iStopTheApp();
});

opaTest("without a price per capacity unit the panel says where to set one", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheTable(Then, "six months", (table) => table.getItems().length === 6, () => Opa5.assert.ok(true));

    When.waitFor({ id: "mpcCuPrice", viewName: VIEW, actions: new EnterText({ text: "", clearTextFirst: true }) });
    iPress(When, "mpcSaveButton");

    iSeeTheStrip(Then, "cmNoCuPrice", "no CU price", (strip) => {
        Opa5.assert.strictEqual(strip.getText(), "Set a price per capacity unit under Model prices to see estimates.");
        Opa5.assert.strictEqual(strip.getType(), "Information");
        Opa5.assert.strictEqual(backend.modelPrices.cuPrice, null, "saved without a CU price");
    });

    Then.iStopTheApp();
});
