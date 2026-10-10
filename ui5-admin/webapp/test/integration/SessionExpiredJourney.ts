import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import type Dialog from "sap/m/Dialog";
import type UI5Element from "sap/ui/core/Element";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Session expired journey");

function thenADialogTitled(Then: Common, title: string): void {
    // ErrorHandler routes a 401 and a 403 to MessageBox.error() with a fixed
    // title each (service/ErrorHandler.ts), which sap.m.MessageBox sets as
    // the real Dialog#title -- asserted on the control itself, not on page
    // text. A body-text search is vacuous here: QUnit's own test-runner page
    // renders a "Module:" filter dropdown listing every QUnit.module() name,
    // and this file's module is literally named "Session expired journey",
    // so that substring is present from page load regardless of what the app
    // does. (Common.iStopTheApp() destroys the dialog during teardown -- the
    // session-expired one cannot be dismissed in-test, since its only action
    // reloads the page.)
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) {
            return (element as Dialog).getTitle() === title;
        },
        success: function () {
            Opa5.assert.ok(true, `the '${title}' dialog is shown`);
        },
        errorMessage: `No dialog titled '${title}' appeared`
    });
}

opaTest("a 401 loading the agent list shows the session-expired dialog", function (Given: Common, When: Common, Then: Common) {
    // A 401 on a GET is what the approuter sends for an expired session only
    // because AdminService marks every request `X-Requested-With:
    // XMLHttpRequest` (asserted in test/unit/AdminService.qunit.ts); a GET
    // without it would get a 302 to the identity provider instead, which
    // fetch follows cross-origin and fails as "Failed to fetch". The fake
    // backend does not check the header, so this journey covers the dialog,
    // the unit test the header.
    Given.iStartTheApp("agents", { path: "agents", status: 401, body: { detail: "no token" } });
    thenADialogTitled(Then, "Session expired");
    Then.iStopTheApp();
});

opaTest("a 403 loading the agent list explains the missing role instead of reloading", function (Given: Common, When: Common, Then: Common) {
    // A 403 is a signed-in user without the admin scope. Reloading would only
    // sign them in again to the same 403, so the dialog explains and stays.
    Given.iStartTheApp("agents", { path: "agents", status: 403, body: { detail: "forbidden" } });
    thenADialogTitled(Then, "Access denied");
    Then.iStopTheApp();
});
