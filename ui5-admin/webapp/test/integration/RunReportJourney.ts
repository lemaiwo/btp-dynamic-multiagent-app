import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import type Table from "sap/m/Table";
import type HTML from "sap/ui/core/HTML";
import type ColumnListItem from "sap/m/ColumnListItem";
import type VBox from "sap/m/VBox";
import type Text from "sap/m/Text";
import type UI5Element from "sap/ui/core/Element";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Run report journey");

opaTest("opening a run renders its markdown report", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("runs");

    // run-1 carries token usage, run-2 (still running) none: the Tokens
    // column shows the grouped total for the one and stays empty for the other.
    Then.waitFor({
        id: "runsTable",
        viewName: "Runs",
        check: function (element: UI5Element) {
            return (element as Table).getItems().length >= 2;
        },
        success: function (element: UI5Element) {
            const table = element as Table;
            const tokensCell = (row: number) =>
                ((table.getItems()[row] as ColumnListItem).getCells()[5] as VBox).getItems() as Text[];
            Opa5.assert.strictEqual(tokensCell(0)[0].getText(false), "12,345", "run-1 shows its total tokens");
            Opa5.assert.strictEqual(tokensCell(0)[1].getText(false), "in 10,000 · out 2,345", "and the split below it");
            Opa5.assert.strictEqual(tokensCell(1)[0].getText(false), "", "run-2 has no usage: empty cell");
            Opa5.assert.notOk(tokensCell(1)[1].getVisible(), "and no split line");
        }
    });

    // Only one run is seeded by FakeBackend.reset(), so the single row is
    // the one to open. Picking it off the table directly (rather than
    // searching for a bare "sap.m.ColumnListItem" and matching it back to
    // the view by ancestor) sidesteps a flaky Ancestor check against the
    // growing table's item aggregation.
    When.waitFor({
        id: "runsTable",
        viewName: "Runs",
        matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
        actions: new Press()
    });

    Then.waitFor({
        id: "reportHtml",
        viewName: "RunDetail",
        // Rendering is asynchronous (marked/DOMPurify load, then the markdown
        // is converted), so this polls via `check` rather than asserting once.
        check: function (element: UI5Element) {
            return (element as HTML).getContent().indexOf("<table") > -1;
        },
        success: function () {
            Opa5.assert.ok(true, "the markdown report rendered to a table");
        }
    });

    Then.waitFor({
        id: "runTokens",
        viewName: "RunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual(
                (element as Text).getText(false),
                "12,345 total (10,000 in, 2,345 out, 1,200 cached) · 7 model requests",
                "the detail header shows the run's token line"
            );
        }
    });

    Then.iStopTheApp();
});
