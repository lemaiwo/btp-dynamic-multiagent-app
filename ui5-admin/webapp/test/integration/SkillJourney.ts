import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Table from "sap/m/Table";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Skill journey");

opaTest("editing a skill's description saves it and updates the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("skills");

    // Only one skill is seeded by FakeBackend.reset(), so the single row is
    // the one to open. Picking it off the table directly (rather than
    // searching for a bare "sap.m.ColumnListItem" and matching it back to
    // the view by ancestor) sidesteps a flaky Ancestor check against the
    // table's item aggregation.
    When.waitFor({
        id: "skillsTable",
        viewName: "Skills",
        matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
        actions: new Press()
    });

    When.waitFor({
        id: "skillDescription",
        viewName: "SkillDetail",
        actions: new EnterText({ text: "Updated description" })
    });
    When.waitFor({ id: "saveSkillButton", viewName: "SkillDetail", actions: new Press() });

    Then.waitFor({
        id: "skillsTable",
        viewName: "Skills",
        success: function (element: UI5Element) {
            const table = element as Table;
            Opa5.assert.strictEqual(
                backend.skills[0].description, "Updated description",
                "the edit reached the backend"
            );
            const model = table.getModel("skills") as JSONModel;
            Opa5.assert.strictEqual(
                model.getProperty("/items/0/description"), "Updated description",
                "the list row shows the new description"
            );
        }
    });

    Then.iStopTheApp();
});
