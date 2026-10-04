import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import type UI5Element from "sap/ui/core/Element";
import type Dialog from "sap/m/Dialog";
import type Button from "sap/m/Button";
import type Input from "sap/m/Input";
import type Select from "sap/m/Select";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { OPTS as WL } from "./pages/Worklist";

/**
 * Task U12: the conventions dialog. An admin creates a target, edits and
 * explicitly clears fields, and sets or clears the `non_production` flag only
 * through a confirmation that names the consequence (Cancel and Enter change
 * nothing); the dialog then shows what the server saved. A developer sees the
 * conventions read-only. The journeys start on the worklist so the
 * new-session dialog can show what the flag changed.
 */
QUnit.module("Conventions journey");

export const APP = { viewName: "App" };
export const D = { ...APP, searchOpenDialogs: true };
export const DEV = "dev-system";
const ON_TITLE = "Mark as Non-Production System";
const OFF_TITLE = "Remove Non-Production Flag";
const ON_ACTION = "Mark as Non-Production";
const OFF_ACTION = "Remove Flag";

export function openDialog(When: Common): void {
    When.waitFor({ id: "conventionsButton", ...APP, actions: new Press() });
    When.waitFor({
        id: "convTarget", ...D,
        check: (c: UI5Element) => !!(c as Select).getSelectedKey(),
        errorMessage: "The conventions are not loaded"
    });
}

export function closeDialog(When: Common): void {
    When.waitFor({ id: "convCloseButton", ...D, actions: new Press() });
    When.waitFor({
        id: "conventionsDialog", ...APP, visible: false,
        check: (c: UI5Element) => !(c as Dialog).isOpen(),
        errorMessage: "The conventions dialog did not close"
    });
}

/** Asserts a property; hidden and disabled controls included (OPA's defaults would skip them). */
export function property(Then: Common, id: string, name: string, value: unknown, message: string): void {
    Then.waitFor({
        // By id in the view (the fragment is the view's): no DOM needed, so hidden controls are found too.
        id, ...APP, visible: false, enabled: false, autoWait: false,
        matchers: new PropertyStrictEquals({ name, value }),
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `${id}.${name} is not ${String(value)} (${message})`
    });
}

export function enter(When: Common, id: string, text: string): void {
    When.waitFor({ id, ...D, actions: new EnterText({ text, clearTextFirst: true }) });
}

export function press(When: Common, id: string): void {
    When.waitFor({ id, ...D, actions: new Press(), errorMessage: `Cannot press ${id}` });
}

const puts = (target = DEV) => backend.bodies.filter((b) => b.key === `PUT conventions/${target}`);
const posts = () => backend.bodies.filter((b) => b.key === "POST conventions");
const stored = (target = DEV) => backend.conventions.find((c) => c.target === target);

/** The open confirmation (a MessageBox) titled `title`, and its buttons. */
export function theConfirmation(Then: Common, title: string, check: (dialog: Dialog) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "title", value: title }),
        success: function (dialogs: UI5Element[]) {
            check(dialogs[0] as Dialog);
        },
        errorMessage: `No confirmation "${title}"`
    });
}

export function textOf(dialog: Dialog): string {
    return dialog.getContent().map((c) => (c as unknown as { getText?(): string }).getText?.() ?? "").join(" ")
        + " " + (dialog.getDomRef()?.textContent ?? "");
}

export function buttonsOf(dialog: Dialog): Button[] {
    return dialog.getButtons() as Button[];
}

export function iAnswer(When: Common, title: string, action: string): void {
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: action }),
            (b: UI5Element) => (b.getParent() as unknown as { getTitle?(): string } | null)?.getTitle?.() === title
        ],
        actions: new Press(),
        errorMessage: `No "${action}" in "${title}"`
    });
}

/** Closes the open message box (not the conventions dialog, whose end button is "Close" too). */
function closeMessage(When: Common): void {
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: "Close" }),
            (b: UI5Element) => (b.getParent() as unknown as { getType?(): string } | null)?.getType?.() === "Message"
        ],
        actions: new Press(),
        errorMessage: "No message box to close"
    });
}

function noConfirmationOpen(Then: Common, title: string, message: string): void {
    Then.waitFor({
        searchOpenDialogs: true,
        check: () => !document.querySelector(".sapMMessageBox") ||
            !Array.from(document.querySelectorAll(".sapMMessageBox .sapMTitle")).some((t) => t.textContent === title),
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `"${title}" is still open`
    });
}

/** The worklist's new-session dialog: whether Diagnose is offered and with which targets. */
function theNewSessionDialogOffersDiagnose(When: Common, Then: Common, offered: boolean, target: string): void {
    When.waitFor({ id: "worklistNewButton", ...WL, actions: new Press(), errorMessage: "No New Session button" });
    Then.waitFor({
        id: "newSessionTypeDiagnose", ...WL, visible: false, enabled: false, autoWait: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: offered }),
        success: () => Opa5.assert.ok(true, `Diagnose is ${offered ? "" : "not "}offered`),
        errorMessage: `Diagnose is ${offered ? "not " : ""}offered`
    });
    if (offered) {
        When.waitFor({
            id: "newSessionType", ...WL, searchOpenDialogs: true,
            success: function (control: UI5Element) {
                // A SegmentedButtonItem has no DOM of its own: press it as DiagnoseSessionJourney does.
                const diagnose = (control as SegmentedButton).getItems().find((i) => i.getKey() === "diagnose");
                new Press().executeOn(diagnose as unknown as Button);
            },
            errorMessage: "Cannot choose Diagnose"
        });
        Then.waitFor({
            id: "newSessionTarget", ...WL, searchOpenDialogs: true,
            check: (c: UI5Element) => (c as Select).getItems().some((i) => i.getKey() === target),
            success: () => Opa5.assert.ok(true, `${target} is a diagnose target`),
            errorMessage: `${target} is not offered for diagnose`
        });
    }
    When.waitFor({ id: "newSessionCancel", ...WL, searchOpenDialogs: true, actions: new Press(), errorMessage: "Cannot cancel the new-session dialog" });
    Then.waitFor({
        id: "newSessionDialog", ...WL, visible: false,
        check: (c: UI5Element) => !(c as Dialog).isOpen(),
        errorMessage: "The new-session dialog did not close"
    });
}

// --- developer --------------------------------------------------------------

opaTest("a developer sees everything read-only: no Save, no New target, no clear buttons, the flag disabled", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.conventions[0].free_text = "{i18n>appTitle} and {= ${x} }";
    });
    openDialog(When);
    for (const id of ["convLabel", "convNamespace", "convPackage", "convAtc", "convDestination", "convFreeText", "convCleanCore"]) {
        property(Then, id, "editable", false, `${id} is read-only`);
    }
    property(Then, "convPackage", "value", "ZLOCAL", "the target's package is loaded");
    property(Then, "convFreeText", "value", "{i18n>appTitle} and {= ${x} }", "the free text is shown as text, not as a binding");
    property(Then, "convNonProduction", "enabled", false, "the flag cannot be switched");
    property(Then, "convSaveButton", "visible", false, "no Save");
    property(Then, "convNewTargetButton", "visible", false, "no New target");
    property(Then, "convPackageClear", "visible", false, "no clear buttons");
    property(Then, "conventionsReadOnly", "visible", true, "the read-only note is shown");
    Then.waitFor({
        id: "conventionsDialog", ...APP,
        success: function (dialog: UI5Element) {
            Opa5.assert.ok(((dialog as Dialog).getBeginButton()).getId().endsWith("convSaveButton"), "the begin button is Save (hidden)");
        }
    });

    Then.iStopTheApp();
});

// --- create -----------------------------------------------------------------

opaTest("an admin creates a target: a bad name is refused client-side, a good one is created and offered for new sessions", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    press(When, "convNewTargetButton");
    property(Then, "convNewTarget", "visible", true, "the new-target input is shown");
    property(Then, "convNewTarget", "required", true, "it is required");
    property(Then, "convNonProduction", "enabled", false, "a new target starts as production; the flag is set after creating it");

    enter(When, "convNewTarget", "bad name!");
    press(When, "convSaveButton");
    property(Then, "convNewTarget", "valueState", "Error", "an invalid name is marked");
    Then.waitFor({
        id: "convNewTarget", ...D,
        success: function (input: UI5Element) {
            Opa5.assert.ok((input as Input).getValueStateText().includes("64"), "the value state text names the rule");
            Opa5.assert.strictEqual(posts().length, 0, "nothing was sent");
        }
    });

    enter(When, "convNewTarget", "-DEMO");
    press(When, "convSaveButton");
    property(Then, "convNewTarget", "valueState", "Error", "a name starting with '-' is refused as the server would");

    enter(When, "convNewTarget", "DEMO2");
    property(Then, "convNewTarget", "valueState", "None", "typing a valid name clears the error");
    enter(When, "convPackage", "ZDEMO2");
    press(When, "convSaveButton");
    property(Then, "convTarget", "selectedKey", "DEMO2", "the new target is selected");
    property(Then, "convNewTarget", "visible", false, "back in edit mode");
    property(Then, "convNonProduction", "enabled", true, "now the flag can be set");
    Then.waitFor({
        success: function () {
            Opa5.assert.deepEqual(posts().map((p) => p.body), [{ target: "DEMO2", package: "ZDEMO2" }], "one POST with the target and the filled field");
            Opa5.assert.strictEqual(stored("DEMO2")?.package, "ZDEMO2", "the server has it");
        }
    });
    closeDialog(When);

    When.waitFor({ id: "worklistNewButton", ...WL, actions: new Press() });
    Then.waitFor({
        id: "newSessionTarget", ...WL, searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Select).getItems().some((i) => i.getKey() === "DEMO2"),
        success: () => Opa5.assert.ok(true, "DEMO2 is offered for a new session"),
        errorMessage: "DEMO2 is not in the new-session targets"
    });
    Then.waitFor({
        id: "newSessionTypeDiagnose", ...WL, visible: false, enabled: false, autoWait: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        success: () => Opa5.assert.ok(true, "but not for diagnose: it is not flagged"),
        errorMessage: "Diagnose is offered for an unflagged target"
    });

    Then.iStopTheApp();
});

opaTest("a duplicate target shows the server's refusal on the name; Cancel returns to the selected target", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    press(When, "convNewTargetButton");
    enter(When, "convNewTarget", DEV);
    press(When, "convSaveButton");
    property(Then, "convNewTarget", "valueState", "Error", "the duplicate is marked");
    Then.waitFor({
        id: "convNewTarget", ...D,
        success: function (input: UI5Element) {
            Opa5.assert.strictEqual((input as Input).getValueStateText(), "A target with this name already exists.", "worded from target_exists");
            Opa5.assert.strictEqual(posts().length, 1, "the server was asked once");
            Opa5.assert.strictEqual(backend.conventions.length, 1, "nothing was added");
        }
    });
    press(When, "convCancelNewButton");
    property(Then, "convNewTarget", "visible", false, "the new-target input is gone");
    property(Then, "convTarget", "selectedKey", DEV, "the previous target is shown again");
    property(Then, "convPackage", "value", "ZLOCAL", "with its stored values");

    Then.iStopTheApp();
});

// --- edit and clear ---------------------------------------------------------

opaTest("an admin edits one field and clears another: the PUT carries only the change and clear:[package]", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    property(Then, "convPackageClear", "visible", true, "a clear button per clearable field");
    Then.waitFor({
        id: "convPackageClear", ...D,
        success: function (button: UI5Element) {
            Opa5.assert.strictEqual((button as Button).getTooltip_AsString(), "Clear Package", "the icon button has an accessible name");
            Opa5.assert.strictEqual((button as Button).getIcon(), "sap-icon://decline");
        }
    });
    press(When, "convPackageClear");
    property(Then, "convPackage", "value", "", "the field is empty");
    enter(When, "convLabel", "{i18n>appTitle}");
    press(When, "convSaveButton");
    Then.waitFor({
        check: () => puts().length === 1 && backend.responses.includes(`PUT conventions/${DEV}`),
        success: function () {
            Opa5.assert.deepEqual(puts()[0].body, { label: "{i18n>appTitle}", clear: ["package"] }, "only the change and the clear");
            Opa5.assert.strictEqual(stored()?.package, "", "stored as empty");
            Opa5.assert.strictEqual(stored()?.label, "{i18n>appTitle}", "the label is stored literally");
            Opa5.assert.notOk("non_production" in (puts()[0].body as object), "Save never sends the flag");
        },
        errorMessage: "No PUT"
    });
    property(Then, "convLabel", "value", "{i18n>appTitle}", "the saved label is shown literally");
    property(Then, "convPackage", "value", "", "the saved (empty) package is shown");

    press(When, "convSaveButton");
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(puts().length, 1, "a Save without changes sends nothing");
        }
    });

    Then.iStopTheApp();
});

// --- the non-production flag ------------------------------------------------

opaTest("turning the flag ON asks first: Enter and Cancel change nothing; the confirmed change is saved and shown from the server", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; fake.diagnoseRetentionDays = 14; });
    theNewSessionDialogOffersDiagnose(When, Then, false, DEV);
    openDialog(When);
    property(Then, "convNonProduction", "state", false, "off as stored");

    press(When, "convNonProduction");
    theConfirmation(Then, ON_TITLE, function (dialog) {
        const text = textOf(dialog);
        Opa5.assert.ok(text.includes("dumps and traces"), "names what is sent");
        Opa5.assert.ok(text.includes("user names and data"), "names the personal data");
        Opa5.assert.ok(text.includes("14 days"), "names the retention");
        const turnOn = buttonsOf(dialog).find((b) => b.getText() === ON_ACTION)!;
        Opa5.assert.ok(turnOn, "an explicit action");
        Opa5.assert.notStrictEqual(turnOn.getType(), "Emphasized", "the action is not the emphasized default");
        Opa5.assert.ok(buttonsOf(dialog).every((b) => b.getType() !== "Emphasized"), "no button is emphasized");
    });
    // Enter on whatever has the focus must not confirm.
    When.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "title", value: ON_TITLE }),
        success: function (dialogs: UI5Element[]) {
            const focused = document.activeElement as HTMLElement;
            const cancel = buttonsOf(dialogs[0] as Dialog).find((b) => b.getText() === "Cancel")!;
            Opa5.assert.strictEqual(focused?.id, cancel.getId(), "the focus starts on Cancel");
            QUnitUtils.triggerKeydown(focused, "ENTER");
            QUnitUtils.triggerKeyup(focused, "ENTER");
        },
        errorMessage: "The confirmation is not open for Enter"
    });
    noConfirmationOpen(Then, ON_TITLE, "Enter answered Cancel");
    property(Then, "convNonProduction", "state", false, "the switch is back off");
    Then.waitFor({ success: () => Opa5.assert.strictEqual(puts().length, 0, "nothing was sent") });

    press(When, "convNonProduction");
    iAnswer(When, ON_TITLE, "Cancel");
    property(Then, "convNonProduction", "state", false, "Cancel: back off");
    Then.waitFor({ success: () => Opa5.assert.strictEqual(puts().length, 0, "nothing was sent") });

    press(When, "convNonProduction");
    iAnswer(When, ON_TITLE, ON_ACTION);
    Then.waitFor({
        check: () => backend.responses.includes(`PUT conventions/${DEV}`),
        success: function () {
            Opa5.assert.deepEqual(puts().map((p) => p.body), [{ non_production: true }], "exactly the flag, as a real boolean");
            Opa5.assert.strictEqual(stored()?.non_production, true, "the server has it");
        },
        errorMessage: "The confirmed flag was not sent"
    });
    property(Then, "convNonProduction", "state", true, "on, as saved");
    property(Then, "convNonProductionStrip", "visible", true, "the consequence stays visible while on");
    closeDialog(When);
    theNewSessionDialogOffersDiagnose(When, Then, true, DEV);

    Then.iStopTheApp();
});

opaTest("turning the flag OFF names the effect on existing diagnose sessions; Cancel keeps it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.isAdmin = true;
        fake.diagnoseRetentionDays = 0;
        fake.allowDiagnose(DEV);
    });
    theNewSessionDialogOffersDiagnose(When, Then, true, DEV);
    openDialog(When);
    property(Then, "convNonProduction", "state", true, "on as stored");

    press(When, "convNonProduction");
    theConfirmation(Then, OFF_TITLE, function (dialog) {
        const text = textOf(dialog);
        Opa5.assert.ok(text.includes("Diagnose sessions on dev-system can no longer run"), "names the blocked sessions");
        Opa5.assert.ok(text.includes("until the session is deleted"), "names what happens to stored data (retention 0)");
        Opa5.assert.ok(buttonsOf(dialog).every((b) => b.getType() !== "Emphasized"), "no default action");
    });
    iAnswer(When, OFF_TITLE, "Cancel");
    property(Then, "convNonProduction", "state", true, "Cancel: still on");
    Then.waitFor({ success: () => Opa5.assert.strictEqual(puts().length, 0, "nothing was sent") });

    press(When, "convNonProduction");
    iAnswer(When, OFF_TITLE, OFF_ACTION);
    Then.waitFor({
        check: () => backend.responses.includes(`PUT conventions/${DEV}`),
        success: function () {
            Opa5.assert.deepEqual(puts().map((p) => p.body), [{ non_production: false }], "exactly the flag");
            Opa5.assert.strictEqual(stored()?.non_production, false, "the server has it");
        },
        errorMessage: "The confirmed flag was not sent"
    });
    property(Then, "convNonProduction", "state", false, "off, as saved");
    closeDialog(When);
    theNewSessionDialogOffersDiagnose(When, Then, false, DEV);

    Then.iStopTheApp();
});

opaTest("a refused flag change shows the error and the server's state, re-read", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    When.waitFor({
        success: function () {
            // The server refuses this flag change (the PUT; the read before the confirmation passes).
            backend.failNext = { path: `conventions/${DEV}`, status: 500, body: { detail: "Database unavailable" }, skip: 1 };
        }
    });
    press(When, "convNonProduction");
    // Asked after the read before the confirmation: another admin changes the package meanwhile, so
    // only a read after the failed PUT can show it.
    theConfirmation(Then, ON_TITLE, () => { stored()!.package = "ZOTHER"; });
    iAnswer(When, ON_TITLE, ON_ACTION);
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: (dialogs: UI5Element[]) => dialogs.some((d) => textOf(d as Dialog).includes("Database unavailable")),
        success: () => Opa5.assert.ok(true, "the refusal is shown"),
        errorMessage: "No error after a refused flag change"
    });
    closeMessage(When);
    property(Then, "convNonProduction", "state", false, "the switch shows the server's (unchanged) state");
    property(Then, "convPackage", "value", "ZOTHER", "the form shows the row read back after the failed PUT");
    Then.waitFor({
        success: function () {
            const gets = backend.requests.filter((r) => r === `GET conventions/${DEV}`).length;
            const put = backend.requests.indexOf(`PUT conventions/${DEV}`);
            Opa5.assert.ok(backend.requests.filter((r) => r === `GET conventions/${DEV}`).length >= 1, "the state was read back from the server");
            Opa5.assert.ok(gets >= 2, `read before the confirmation and again after the failed PUT (${gets} reads)`);
            Opa5.assert.ok(put >= 0, "the PUT was sent");
            Opa5.assert.ok(backend.requests.slice(put + 1).includes(`GET conventions/${DEV}`), "a read follows the failed PUT");
            Opa5.assert.notStrictEqual(stored()?.non_production, true, "the server's flag is unchanged");
        }
    });

    Then.iStopTheApp();
});

opaTest("a refused flag change shows the flag the server has meanwhile (re-read), not the stored one", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    property(Then, "convNonProduction", "state", false, "off as stored");
    When.waitFor({
        success: function () {
            backend.failNext = { path: `conventions/${DEV}`, status: 500, body: { detail: "Database unavailable" }, skip: 1 };
            release = backend.hold(`PUT conventions/${DEV}`);
        }
    });
    press(When, "convNonProduction");
    iAnswer(When, ON_TITLE, ON_ACTION);
    Then.waitFor({
        check: () => backend.requests.includes(`PUT conventions/${DEV}`),
        success: function () {
            // While the refused PUT is on its way, another admin sets the flag.
            stored()!.non_production = true;
            release!();
            Opa5.assert.ok(true, "the PUT is on its way");
        },
        errorMessage: "No PUT"
    });
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: (dialogs: UI5Element[]) => dialogs.some((d) => textOf(d as Dialog).includes("Database unavailable")),
        success: () => Opa5.assert.ok(true, "the refusal is shown"),
        errorMessage: "No error after a refused flag change"
    });
    closeMessage(When);
    property(Then, "convNonProduction", "state", true, "the switch shows the server's flag, read after the failed PUT");
    property(Then, "convNonProductionStrip", "visible", true, "and its consequence");
    Then.waitFor({
        success: function () {
            const put = backend.requests.indexOf(`PUT conventions/${DEV}`);
            Opa5.assert.ok(backend.requests.slice(put + 1).includes(`GET conventions/${DEV}`), "a read follows the failed PUT");
            Opa5.assert.strictEqual(puts().length, 1, "one PUT");
        }
    });

    Then.iStopTheApp();
});

opaTest("a failing read before the confirmation shows the error, asks nothing, sends nothing and keeps the stored flag", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    property(Then, "convNonProduction", "state", false, "off as stored");
    When.waitFor({
        success: function () {
            backend.failNext = { path: `conventions/${DEV}`, status: 500, body: { detail: "Database unavailable" } };
        }
    });
    press(When, "convNonProduction");
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: (dialogs: UI5Element[]) => dialogs.some((d) => textOf(d as Dialog).includes("Database unavailable")),
        success: () => Opa5.assert.ok(true, "the failed read is shown"),
        errorMessage: "No error after a failed read"
    });
    noConfirmationOpen(Then, ON_TITLE, "nothing is asked");
    closeMessage(When);
    noConfirmationOpen(Then, ON_TITLE, "still nothing asked after the error is closed");
    property(Then, "convNonProduction", "state", false, "the switch is back at the stored state");
    property(Then, "convNonProductionStrip", "visible", false, "no consequence shown");
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(backend.requests.includes(`GET conventions/${DEV}`), "the row was read before asking");
            Opa5.assert.strictEqual(puts().length, 0, "nothing was sent");
            Opa5.assert.notStrictEqual(stored()?.non_production, true, "the server's flag is unchanged");
        }
    });

    Then.iStopTheApp();
});

// --- refusals and guards ------------------------------------------------------

opaTest("a 403 on a forced write is shown cleanly and keeps the dialog and the edit", function (Given: Common, When: Common, Then: Common) {
    // The caller looks like an admin to the page, but the server refuses.
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    When.waitFor({ success: () => { backend.isAdmin = false; } });
    enter(When, "convPackage", "ZFORCED");
    press(When, "convSaveButton");
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: (dialogs: UI5Element[]) => dialogs.some((d) => textOf(d as Dialog).includes("Only administrators can change conventions.")),
        success: () => Opa5.assert.ok(true, "the refusal is worded as a missing admin role"),
        errorMessage: "No message after a refused save"
    });
    closeMessage(When);
    property(Then, "convPackage", "value", "ZFORCED", "the edit is kept");
    Then.waitFor({
        id: "conventionsDialog", ...APP,
        success: function (dialog: UI5Element) {
            Opa5.assert.ok((dialog as Dialog).isOpen(), "the dialog stays open");
            Opa5.assert.strictEqual(stored()?.package, "ZLOCAL", "nothing changed on the server");
        }
    });

    Then.iStopTheApp();
});

opaTest("a double Save sends one request", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.isAdmin = true;
        release = fake.hold(`PUT conventions/${DEV}`);
    });
    openDialog(When);
    enter(When, "convPackage", "ZTWICE");
    When.waitFor({
        id: "convSaveButton", ...D,
        success: function (button: UI5Element) {
            (button as Button).firePress();
            (button as Button).firePress();
            release();
        }
    });
    Then.waitFor({
        check: () => backend.responses.includes(`PUT conventions/${DEV}`),
        success: function () {
            Opa5.assert.strictEqual(puts().length, 1, "one PUT");
            Opa5.assert.strictEqual(stored()?.package, "ZTWICE");
        },
        errorMessage: "The save did not finish"
    });

    Then.iStopTheApp();
});

opaTest("a refused security token is shown as such and the edit can be saved again", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; fake.csrf = true; });
    openDialog(When);
    enter(When, "convPackage", "ZCSRF");
    When.waitFor({ success: () => { backend.refuseCsrf(2); } });
    press(When, "convSaveButton");
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: (dialogs: UI5Element[]) => dialogs.some((d) => textOf(d as Dialog).includes("security check")),
        success: () => Opa5.assert.ok(true, "worded as a security-token refusal, not a missing role"),
        errorMessage: "No security-token message"
    });
    closeMessage(When);
    property(Then, "convPackage", "value", "ZCSRF", "the edit is kept");
    Then.waitFor({ success: () => Opa5.assert.strictEqual(stored()?.package, "ZLOCAL", "nothing was stored") });
    press(When, "convSaveButton");
    Then.waitFor({
        check: () => stored()?.package === "ZCSRF",
        success: () => Opa5.assert.ok(true, "the second Save goes through"),
        errorMessage: "The second Save did not store"
    });

    Then.iStopTheApp();
});
