import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import type Dialog from "sap/m/Dialog";
import type UI5Element from "sap/ui/core/Element";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Session expired journey");

opaTest("a 403 loading the agent list shows the session-expired dialog", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents", { path: "agents", status: 403, body: { detail: "forbidden" } });

    // ErrorHandler routes a 401/403 to MessageBox.error() with a fixed title
    // (agents/service/ErrorHandler.ts), which sap.m.MessageBox sets as the
    // real Dialog#title -- asserted on the control itself, not on page text.
    // A body-text search for "Session expired" is vacuous here: QUnit's own
    // test-runner page renders a "Module:" filter dropdown listing every
    // QUnit.module() name, and this file's module is literally named
    // "Session expired journey", so the substring is present from page load
    // regardless of what the app does. (Common.iStopTheApp() destroys this
    // dialog during teardown below -- there is no way to dismiss it in-test,
    // since its only action reloads the page.)
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) {
            return (element as Dialog).getTitle() === "Session expired";
        },
        success: function () {
            Opa5.assert.ok(true, "the session-expired dialog is shown");
        },
        errorMessage: "No dialog titled 'Session expired' appeared"
    });

    Then.iStopTheApp();
});
