import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type Common from "./Common";

/** Steps on whatever dialog is open, shared by the OData journeys. */

/** Presses the button `text` of the open dialog. */
export function iPressInDialog(When: Common, text: string): void {
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: text }),
        actions: new Press(),
        errorMessage: `No button "${text}" in an open dialog`
    });
}

/** Waits for the one open dialog and hands it to `assert`. */
export function iSeeADialog(Then: Common, assert: (dialog: UI5Element) => void, what: string): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, `one dialog is open: ${what}`);
            assert(dialogs[0]);
        },
        errorMessage: `No dialog appeared: ${what}`
    });
}
