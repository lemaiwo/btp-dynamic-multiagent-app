import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import Common, { backend } from "./pages/Common";
import { buttonsOf, messageOf } from "./pages/ODataList";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Reload journey");

opaTest("reload succeeds and reports success without a session-expired dialog", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    When.waitFor({ id: "reloadButton", viewName: "Settings", actions: new Press() });

    // The success toast is the only feedback onReload() gives; it renders
    // outside the control tree, so it is polled for via `check` rather than
    // matched as a control.
    Then.waitFor({
        check: function () {
            return document.querySelectorAll(".sapMMessageToast").length > 0;
        },
        success: function () {
            Opa5.assert.ok(true, "the reload reported success");
            Opa5.assert.strictEqual(
                document.querySelectorAll(".sapMMessageBox").length, 0,
                "no message box (error or session-expired) opened"
            );
        }
    });

    Then.waitFor({
        id: "settingsPage",
        viewName: "Settings",
        success: function () {
            Opa5.assert.ok(true, "still on the settings page");
        }
    });

    Then.iStopTheApp();
});

opaTest("an orchestrator save the running agents did not get says so in a box that stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    When.waitFor({
        id: "saveOrchestratorButton",
        viewName: "Settings",
        actions: function (element: UI5Element | null) {
            backend.reloadOutcome = "failed";
            new Press().executeOn(element as Control);
        }
    });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: function (dialog: UI5Element) { return (dialog as Dialog).getType() === "Message"; },
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(messageOf(dialogs[0]), "The orchestrator instructions were saved, but the running "
                + "agents could not be reloaded: they still use the previous configuration. Go to Settings and "
                + "press Reload to make the change active.");
            Opa5.assert.deepEqual(buttonsOf(dialogs[0]), ["OK"]);
            new Press().executeOn((dialogs[0] as Dialog).getButtons()[0]);
        }
    });
    Then.iStopTheApp();
});
