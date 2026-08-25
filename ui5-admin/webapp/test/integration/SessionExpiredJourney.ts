import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Session expired journey");

opaTest("a 403 loading the agent list shows the session-expired dialog", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents", { path: "agents", status: 403, body: { detail: "forbidden" } });

    // ErrorHandler routes a 401/403 to MessageBox.error() with a fixed title
    // and message (agents/service/ErrorHandler.ts). sap.m.MessageBox does set
    // Dialog#title, but this checks the rendered text directly: it is proven
    // reliable, whereas a control-based getTitle() match here was flaky.
    // (Common.iStopTheApp() destroys this dialog during teardown below --
    // there is no way to dismiss it in-test, since its only action reloads
    // the page.)
    Then.waitFor({
        check: function () {
            return document.querySelectorAll(".sapMDialog").length > 0
                && (document.body.textContent ?? "").indexOf("Session expired") > -1;
        },
        success: function () {
            Opa5.assert.ok(true, "the session-expired dialog is showing");
        }
    });

    Then.iStopTheApp();
});
