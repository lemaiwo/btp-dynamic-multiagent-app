import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Dialog from "sap/m/Dialog";
import type TextArea from "sap/m/TextArea";
import type UI5Element from "sap/ui/core/Element";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Import journey");

// The import dialog is a Fragment added as a *dependent* of the Settings
// view (not a named view of its own), so OPA5's id matchers -- both
// "searchOpenDialogs" and the plain global-id lookup used for a closed,
// invisible control -- can only strip the view-id prefix (and so match a
// bare id like "importJson") when also told which view to walk up the
// dialog's parent chain to; without viewName they compare against the full
// prefixed id, which never matches.
opaTest("importing valid JSON closes the dialog; invalid JSON leaves it open with an error", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    When.waitFor({ id: "importButton", viewName: "Settings", actions: new Press() });
    When.waitFor({
        id: "importJson",
        viewName: "Settings",
        searchOpenDialogs: true,
        actions: new EnterText({ text: "{\"agents\":[],\"skills\":[]}" })
    });
    When.waitFor({ id: "importConfirm", viewName: "Settings", searchOpenDialogs: true, actions: new Press() });

    Then.waitFor({
        id: "importDialog",
        viewName: "Settings",
        // The dialog control still exists once closed (it stays a dependent
        // of the view); "visible: false" tells OPA5 to include it in the
        // search instead of waiting forever for a now-invisible control.
        visible: false,
        success: function (element: UI5Element) {
            const dialog = element as Dialog;
            Opa5.assert.notOk(dialog.isOpen(), "the import dialog closed after a valid import");
        }
    });

    // Second run: reopen and submit text that is not JSON at all.
    When.waitFor({ id: "importButton", viewName: "Settings", actions: new Press() });
    When.waitFor({
        id: "importJson",
        viewName: "Settings",
        searchOpenDialogs: true,
        actions: new EnterText({ text: "not json" })
    });
    When.waitFor({ id: "importConfirm", viewName: "Settings", searchOpenDialogs: true, actions: new Press() });

    // onConfirmImport() reports invalid JSON via MessageBox.error(). The
    // closed importDialog is excluded from this search by OPA5's default
    // "visible" control filter, so only the MessageBox dialog can match.
    // (Common.iStopTheApp() destroys any dialog still open during teardown
    // below, so this does not need to close it itself.)
    Then.waitFor({
        controlType: "sap.m.Dialog",
        success: function (elements: UI5Element[]) {
            Opa5.assert.ok((elements as Dialog[]).length > 0, "an error dialog appeared for invalid JSON");
        }
    });
    Then.waitFor({
        id: "importJson",
        viewName: "Settings",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const input = element as TextArea;
            Opa5.assert.strictEqual(
                input.getValue(), "not json",
                "the import dialog stayed open with the text intact"
            );
        }
    });

    Then.iStopTheApp();
});
