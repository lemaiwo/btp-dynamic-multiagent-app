import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Table from "sap/m/Table";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type Button from "sap/m/Button";
import type ColumnListItem from "sap/m/ColumnListItem";
import Common, { backend } from "./pages/Common";
import { buttonsOf, messageOf } from "./pages/ODataList";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

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

const NOT_LIVE = ", but the running agents could not be reloaded: they still use the previous configuration. "
    + "Go to Settings and press Reload to make the change active.";

function isMessage(dialog: UI5Element): boolean {
    return (dialog as Dialog).getType() === "Message";
}

/** Waits for the one message box, checks it, and closes it with OK. */
function iSeeTheNotLiveBox(Then: Common, When: Common, lead: string, extra?: () => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, "one message is open");
            Opa5.assert.strictEqual(messageOf(dialogs[0]), `${lead}${NOT_LIVE}`);
            Opa5.assert.deepEqual(buttonsOf(dialogs[0]), ["OK"], "it stays until it is closed");
            extra?.();
        },
        errorMessage: "No box about the failed reload"
    });
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        actions: function (element: UI5Element | null) {
            new Press().executeOn((element as Dialog).getButtons()[0]);
        }
    });
}

opaTest("a skill save the running agents did not get says so in a box that stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("skills");
    When.waitFor({
        id: "skillsTable",
        viewName: "Skills",
        matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
        actions: function (element: UI5Element | null) {
            backend.reloadOutcome = "failed";
            new Press().executeOn(element as Control);
        }
    });
    When.waitFor({
        id: "skillDescription",
        viewName: "SkillDetail",
        actions: new EnterText({ text: "Stored, not live" })
    });
    When.waitFor({ id: "saveSkillButton", viewName: "SkillDetail", actions: new Press() });
    iSeeTheNotLiveBox(Then, When, "The skill was saved", function () {
        Opa5.assert.strictEqual(backend.skills[0].description, "Stored, not live", "the skill is stored");
    });
    Then.iStopTheApp();
});

opaTest("a skill delete the running agents did not get says so in a box that stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("skills");
    When.waitFor({
        id: "skillsTable",
        viewName: "Skills",
        matchers: function (element: UI5Element) { return (element as Table).getItems().length > 0; },
        actions: function (element: UI5Element | null) {
            backend.reloadOutcome = "failed";
            const item = (element as Table).getItems()[0] as ColumnListItem;
            const button = item.findAggregatedObjects(true, (child) => (
                child.isA("sap.m.Button") && (child as Button).getIcon() === "sap-icon://delete"
            ))[0] as Button;
            button.firePress();
        }
    });
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        actions: function (element: UI5Element | null) {
            const ok = (element as Dialog).getButtons().filter((b) => b.getText() === "OK")[0];
            new Press().executeOn(ok);
        }
    });
    iSeeTheNotLiveBox(Then, When, "The skill was deleted", function () {
        Opa5.assert.strictEqual(backend.skills.length, 0, "the skill is deleted");
    });
    Then.iStopTheApp();
});
