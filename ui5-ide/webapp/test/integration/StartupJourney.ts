import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type Common from "./pages/Common";

QUnit.module("Startup journey");

opaTest("the app starts on the ide route and shows the three panes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    Then.waitFor({
        id: "appTitle",
        viewName: "App",
        matchers: new PropertyStrictEquals({ name: "text", value: "ABAP Workbench" }),
        success: function () {
            Opa5.assert.ok(true, "the header shows the app title");
        }
    });

    ["explorerPane", "editorPane", "assistantPane"].forEach(function (id) {
        Then.waitFor({
            id,
            viewName: "Ide",
            success: function () {
                Opa5.assert.ok(true, `the ${id} is rendered`);
            }
        });
    });

    Then.iStopTheApp();
});
