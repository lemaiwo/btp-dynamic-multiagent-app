import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Dialog from "sap/m/Dialog";
import type TextArea from "sap/m/TextArea";
import type UI5Element from "sap/ui/core/Element";
import Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import { buttonsOf, messageOf } from "./pages/ODataList";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

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

    // onConfirmImport() reports invalid JSON via MessageBox.error(), whose
    // default title is the "Error" resource text (sap/m/messagebundle.
    // properties: MSGBOX_TITLE_ERROR). Matching on that title -- rather than
    // just "some sap.m.Dialog is open" -- matters here specifically: the
    // reopened importDialog ("{i18n>importConfig}" titled) is itself already
    // open at this point, so a bare existence check would pass even if
    // MessageBox.error() were never called.
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) {
            return (element as Dialog).getTitle() === "Error";
        },
        success: function () {
            Opa5.assert.ok(true, "an error dialog appeared for invalid JSON");
        },
        errorMessage: "No dialog titled 'Error' appeared for invalid JSON"
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

// --- final review: an import says what it opens and what it changed ------------

const IMPORT = "POST import";

function isMessage(dialog: UI5Element): boolean {
    return (dialog as Dialog).getType() === "Message";
}

/** Pastes `text` into a freshly opened import dialog and presses Import. */
function iImport(When: Common, text: string, replace = false): void {
    When.waitFor({ id: "importButton", viewName: "Settings", actions: new Press() });
    When.waitFor({ id: "importJson", viewName: "Settings", searchOpenDialogs: true, actions: new EnterText({ text }) });
    if (replace) {
        When.waitFor({ id: "importReplace", viewName: "Settings", searchOpenDialogs: true, actions: new Press() });
    }
    When.waitFor({ id: "importConfirm", viewName: "Settings", searchOpenDialogs: true, actions: new Press() });
}

/** Waits for the one message box over the import dialog. */
function iSeeAMessage(Then: Common, what: string, assert: (dialog: Dialog) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, `one message box is open: ${what}`);
            assert(dialogs[0] as Dialog);
        },
        errorMessage: `No question appeared: ${what}`
    });
}

function iAnswer(When: Common, text: string): void {
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        actions: function (element: UI5Element | null) {
            new Press().executeOn((element as Dialog).getButtons().filter((b) => b.getText() === text)[0]);
        },
        errorMessage: `No message box with a button "${text}"`
    });
}

function focusOf(dialog: Dialog): string {
    return (Element.getElementById(dialog.getInitialFocus() as string) as unknown as { getText(): string }).getText();
}

const SERVICE = {
    name: "sales-orders", title: "Sales <b>orders</b>", purpose: "Read sales orders", not_for: "", destination: "S4_ODATA_TECH",
    user_context: false, odata_version: "v2", service_path: "/sap/opu/odata/sap/API_SALES_ORDER_SRV", enabled: true,
    metadata_fetched_at: null,
    definition: {
        entity_sets: [{ name: "A_SalesOrder", title: "Sales order", operations: ["list", "update"] }],
        operations: [{ name: "Release", title: "Release order", enabled: true, http_method: "POST", changes_data: true }]
    }
};
const WRITER = {
    name: "order-agent",
    mcp_servers: [{ url: "builtin:odata", auth_mode: "destination", oauth: { services: ["sales-orders"], allow_write: true } }]
};

opaTest("an import that brings OData services or Allow writes is asked about first; Cancel is the default and sends nothing", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    iImport(When, JSON.stringify({ agents: [WRITER], skills: [], odata_services: [SERVICE, { ...SERVICE, name: "readers", title: "", user_context: true, definition: { entity_sets: [], operations: [] } }] }), true);
    iSeeAMessage(Then, "what the import opens", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "This import changes what agents can do in SAP. Check the list before you import.\n\n"
            + "OData services in this import. A service with the same name is replaced, also when agents use it:\n"
            + "- \"Sales <b>orders</b>\" (sales-orders): runs as Technical user, destination S4_ODATA_TECH. "
            + "Writes: Sales order (update); Release order.\n"
            + "- \"readers\" (readers): runs as Signed-in user, destination S4_ODATA_TECH. No writes.\n\n"
            + "Agents that get \"Allow writes\" in this import, and the services they may write through:\n"
            + "- order-agent: sales-orders\n\n"
            + "Because \"Delete ... not in this import\" is ticked, OData services that are not in this import are deleted from the catalogue.",
            "each service with identity, destination and writes; the agents with Allow writes; what replace deletes"
        );
        Opa5.assert.strictEqual(dialog.getDomRef()?.querySelectorAll("b").length, 0, "a title of the bundle is text, not markup");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Import", "Cancel"]);
        Opa5.assert.strictEqual(focusOf(dialog), "Cancel", "Cancel is the default");
        Opa5.assert.strictEqual(backend.countRequests(IMPORT), 0, "nothing is sent before the answer");
    });
    iAnswer(When, "Cancel");
    Then.waitFor({
        id: "importJson",
        viewName: "Settings",
        searchOpenDialogs: true,
        success: function () {
            Opa5.assert.strictEqual(backend.countRequests(IMPORT), 0, "Cancel sends nothing, and the import dialog stays");
        }
    });
    When.waitFor({ id: "importConfirm", viewName: "Settings", searchOpenDialogs: true, actions: new Press() });
    iAnswer(When, "Import");
    Then.waitFor({
        id: "importDialog",
        viewName: "Settings",
        visible: false,
        check: function () { return backend.countRequests(IMPORT) === 1; },
        success: function () {
            Opa5.assert.strictEqual(backend.bodies[IMPORT]?.replace, true, "sent on the admin's answer, with replace");
        }
    });
    Then.iStopTheApp();
});

opaTest("an import whose content cannot be read is asked about in general words, never sent unseen", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    iImport(When, JSON.stringify({ agents: [], skills: [], odata_services: { "sales-orders": SERVICE } }));
    iSeeAMessage(Then, "the general question", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Could not read what this import opens: its OData services or agents do not have the expected form. It may add or "
            + "replace OData services, change who agents act as in SAP and give agents writes. Import it anyway?"
        );
        Opa5.assert.strictEqual(focusOf(dialog), "Cancel", "Cancel is the default");
        Opa5.assert.strictEqual(backend.countRequests(IMPORT), 0, "nothing is sent");
    });
    iAnswer(When, "Cancel");
    Then.waitFor({
        id: "importJson",
        viewName: "Settings",
        searchOpenDialogs: true,
        success: function () { Opa5.assert.strictEqual(backend.countRequests(IMPORT), 0, "Cancel sends nothing"); }
    });
    Then.iStopTheApp();
});

opaTest("after an import, what it changed for agents in use, the server's warnings and a failed reload stay on screen", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    When.waitFor({
        success: function () {
            backend.reloadOutcome = "failed";
            backend.importAnswer = {
                removed_odata_service_names: ["old-one"],
                odata_identity_changes: [{ service: "sales-orders", changed: ["destination", "user_context"], agents: ["btp-agent", "<i>x</i>"] }],
                warnings: [
                    "OData service(s) removed by replace: 'old-one'",
                    "OData service 'sales-orders': destination, user_context changed; used by agent(s) 'btp-agent', '<i>x</i>'",
                    "Agent 'a': model <b>x</b> is not deployed"
                ]
            };
        }
    });
    // No OData service and no Allow writes in the bundle: no question first.
    iImport(When, "{\"agents\":[],\"skills\":[]}");
    iSeeAMessage(Then, "what the import reports", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Configuration imported.\n\n"
            + "OData services deleted from the catalogue: old-one.\n\n"
            + "OData service sales-orders: the destination, Runs as changed. These agents now act differently in SAP: btp-agent, <i>x</i>.\n\n"
            + "The server reported:\nAgent 'a': model <b>x</b> is not deployed\n\n"
            + "The configuration was imported, but the running agents could not be reloaded: they still use the previous "
            + "configuration. Go to Settings and press Reload to make the change active."
        );
        Opa5.assert.strictEqual(dialog.getDomRef()?.querySelectorAll("b, i").length, 0, "the server's texts are text, not markup");
        Opa5.assert.strictEqual(dialog.getTitle(), "Imported: please read");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["OK"], "it stays until it is closed");
        Opa5.assert.strictEqual(backend.countRequests(IMPORT), 1, "the import was sent without a question");
    });
    iAnswer(When, "OK");
    Then.iStopTheApp();
});
