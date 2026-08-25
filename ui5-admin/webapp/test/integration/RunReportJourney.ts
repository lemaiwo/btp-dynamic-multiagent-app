import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import type Table from "sap/m/Table";
import type HTML from "sap/ui/core/HTML";
import type UI5Element from "sap/ui/core/Element";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Run report journey");

opaTest("opening a run renders its markdown report", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("runs");

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

    Then.iStopTheApp();
});
