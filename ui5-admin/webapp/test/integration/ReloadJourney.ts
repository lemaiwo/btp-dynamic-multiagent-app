import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

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
