import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";

QUnit.module("Conventions journey");

const APP = { viewName: "App" };

function openDialog(When: Common): void {
    When.waitFor({ id: "conventionsButton", ...APP, actions: new Press() });
}

function field(Then: Common, id: string, editable: boolean, extra?: (c: Control) => void): void {
    Then.waitFor({
        id,
        ...APP,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "editable", value: editable }),
        success: function (control: UI5Element) {
            extra?.(control as Control);
            Opa5.assert.ok(true, `${id} is ${editable ? "editable" : "read-only"}`);
        },
        errorMessage: `${id} is not ${editable ? "editable" : "read-only"}`
    });
}

opaTest("a developer sees the conventions read-only and cannot save", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();
    openDialog(When);
    for (const id of ["convLabel", "convNamespace", "convPackage", "convAtc", "convDestination", "convFreeText"]) {
        field(Then, id, false);
    }
    field(Then, "convCleanCore", false);
    Then.waitFor({
        id: "convPackage",
        ...APP,
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control as unknown as { getValue(): string }).getValue(), "ZLOCAL", "the target's package is loaded");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        success: function (dialogs: UI5Element[]) {
            const save = (dialogs[0] as Dialog).getBeginButton();
            Opa5.assert.strictEqual(save.getId().endsWith("convSaveButton"), true, "the dialog's begin button is Save");
            Opa5.assert.strictEqual(save.getVisible(), false, "Save is hidden for a developer");
        },
        errorMessage: "The conventions dialog is not open"
    });

    Then.iStopTheApp();
});

opaTest("an admin edits the conventions and Save sends a PUT", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    field(Then, "convPackage", true);
    When.waitFor({
        id: "convPackage",
        ...APP,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "ZNEW" })
    });
    When.waitFor({ id: "convSaveButton", ...APP, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        success: function () {
            const put = backend.requests.find((r) => r.startsWith("PUT") && r.includes("conventions/dev-system"));
            Opa5.assert.ok(put, "Save sent PUT conventions/dev-system");
            Opa5.assert.strictEqual(backend.conventions.find((c) => c.target === "dev-system")?.package, "ZNEW", "the backend stored the new package");
        }
    });

    Then.iStopTheApp();
});

opaTest("a 403 on Save shows a message and keeps the dialog open", function (Given: Common, When: Common, Then: Common) {
    // The caller looks like an admin to the page, but the server refuses.
    Given.iStartTheApp("", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    field(Then, "convPackage", true);
    When.waitFor({
        success: function () {
            backend.isAdmin = false;
        }
    });
    When.waitFor({ id: "convSaveButton", ...APP, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        success: function () {
            Opa5.assert.ok(true, "a message box is shown");
        },
        errorMessage: "No message after a refused save"
    });

    Then.iStopTheApp();
});
