import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type Dialog from "sap/m/Dialog";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { OPTS as WL } from "./pages/Worklist";
import {
    APP, D, DEV, enter, iAnswer, openDialog, press, property, textOf, theConfirmation
} from "./ConventionsJourney";
import { win } from "./ReviewFollowupsJourney";

/**
 * U12 fix round 1: the conventions dialog diffs a Save against what it
 * loaded (no lost update after a flag write), shows the stored flag while a
 * confirmed change is on its way, re-reads the flag before asking, merges a
 * write into the worklist's targets, words a create 422 by its field and
 * keeps Save off without a target.
 */
QUnit.module("U12 fix 1");

const ON_TITLE = "Mark as Non-Production System";
const ON_ACTION = "Mark as Non-Production";

const puts = (target = DEV) => backend.bodies.filter((b) => b.key === `PUT conventions/${target}`);
const stored = (target = DEV) => backend.conventions.find((c) => c.target === target);
const answered = (key: string) => backend.responses.filter((r) => r === key).length;

/** Closes the open message box only (the conventions dialog's end button is "Close" too). */
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

function theMessageSays(Then: Common, text: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: (dialogs: UI5Element[]) => dialogs.some((d) => textOf(d as Dialog).includes(text)),
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `No message saying "${text}"`
    });
}

// --- 1, 8: no lost update ------------------------------------------------------

opaTest("F1: the stored row changes between open and Save: the PUT carries only the edited field", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    enter(When, "convLabel", "Mine");
    // Another admin changes the package meanwhile.
    When.waitFor({ success: () => { stored()!.package = "ZOTHER"; } });
    press(When, "convSaveButton");
    Then.waitFor({
        check: () => answered(`PUT conventions/${DEV}`) === 1,
        success: function () {
            Opa5.assert.deepEqual(puts().map((p) => p.body), [{ label: "Mine" }], "only the edited field");
            Opa5.assert.strictEqual(stored()?.package, "ZOTHER", "the other admin's change stays");
        },
        errorMessage: "No PUT"
    });
    property(Then, "convPackage", "value", "ZOTHER", "the form shows the saved row");
    Then.iStopTheApp();
});

opaTest("F1/F8: a flag change after an unsaved edit, then Save: the untouched field follows the server, Save sends only the edit", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    enter(When, "convLabel", "Mine");
    When.waitFor({ success: () => { stored()!.package = "ZOTHER"; } });
    press(When, "convNonProduction");
    iAnswer(When, ON_TITLE, ON_ACTION);
    Then.waitFor({
        check: () => answered(`PUT conventions/${DEV}`) === 1,
        success: () => Opa5.assert.deepEqual(puts().map((p) => p.body), [{ non_production: true }], "the flag alone"),
        errorMessage: "The confirmed flag was not sent"
    });
    property(Then, "convNonProduction", "state", true, "the flag as saved");
    property(Then, "convPackage", "value", "ZOTHER", "the untouched package shows the server's value");
    property(Then, "convLabel", "value", "Mine", "the unsaved edit is kept");
    press(When, "convSaveButton");
    Then.waitFor({
        check: () => answered(`PUT conventions/${DEV}`) === 2,
        success: function () {
            Opa5.assert.deepEqual(puts()[1].body, { label: "Mine" }, "Save sends only the edit, never the package it did not touch");
            Opa5.assert.strictEqual(stored()?.package, "ZOTHER", "the other admin's package is not reverted");
            Opa5.assert.strictEqual(stored()?.non_production, true, "and Save did not touch the flag");
        },
        errorMessage: "No second PUT"
    });
    Then.iStopTheApp();
});

// --- 2, 8: the switch while the flag is on its way; double confirm -------------

opaTest("F2/F8: while the confirmed flag is on its way the switch shows the stored state; a double confirm sends one request", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    press(When, "convNonProduction");
    theConfirmation(Then, ON_TITLE, () => Opa5.assert.ok(true, "asked"));
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: ON_ACTION }),
            (b: UI5Element) => (b.getParent() as unknown as { getTitle?(): string } | null)?.getTitle?.() === ON_TITLE
        ],
        success: function (buttons: UI5Element[]) {
            release = backend.hold(`PUT conventions/${DEV}`);
            (buttons[0] as Button).firePress();
            (buttons[0] as Button).firePress();
        },
        errorMessage: "No confirm action"
    });
    Then.waitFor({
        check: () => backend.requests.includes(`PUT conventions/${DEV}`),
        success: () => Opa5.assert.ok(true, "the PUT is on its way"),
        errorMessage: "No PUT left"
    });
    property(Then, "convNonProduction", "state", false, "while on its way: the stored state, not the wished one");
    When.waitFor({ success: () => release() });
    Then.waitFor({
        check: () => answered(`PUT conventions/${DEV}`) >= 1,
        success: () => Opa5.assert.ok(true, "answered"),
        errorMessage: "The PUT was not answered"
    });
    property(Then, "convNonProduction", "state", true, "the answer sets it");
    Then.waitFor({
        success: () => Opa5.assert.deepEqual(puts().map((p) => p.body), [{ non_production: true }], "one request, the flag alone")
    });
    Then.iStopTheApp();
});

// --- 3: re-read before asking ----------------------------------------------------

opaTest("F3: the flag is read again before the confirmation; already as wanted: no question, just the server's state", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    property(Then, "convNonProduction", "state", false, "off as loaded");
    // Another admin flags the target meanwhile.
    When.waitFor({ success: () => { stored()!.non_production = true; } });
    press(When, "convNonProduction");
    Then.waitFor({
        check: () => answered(`GET conventions/${DEV}`) >= 1,
        success: () => Opa5.assert.ok(true, "the row was read again before asking"),
        errorMessage: "The flag was not read again"
    });
    property(Then, "convNonProduction", "state", true, "the switch shows the server's state");
    property(Then, "convNonProductionStrip", "visible", true, "and its warning");
    Then.waitFor({
        success: function () {
            const open = Array.from(win().document.querySelectorAll(".sapMMessageBox .sapMTitle")).some((t) => t.textContent === ON_TITLE);
            Opa5.assert.notOk(open, "no confirmation for a change that is already there");
            Opa5.assert.strictEqual(puts().length, 0, "nothing was sent");
        }
    });
    Then.iStopTheApp();
});

// --- 4: the worklist merges the changed target ------------------------------------

function worklistIde(Then: Common, check: (ide: JSONModel) => boolean, message: string): void {
    Then.waitFor({
        id: "worklistNewButton", ...WL, visible: false, enabled: false, autoWait: false,
        check: (c: UI5Element) => check(c.getModel("ide") as JSONModel),
        success: () => Opa5.assert.ok(true, message),
        error: function () {
            Opa5.assert.ok(false, message);
        },
        errorMessage: message
    });
}

opaTest("F4: a conventions write merges its target into the worklist; targets the worklist read since are kept", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    // Another admin adds QAS (flagged); the worklist reads /me again while the dialog is open.
    When.waitFor({ success: () => { backend.conventions.push({ target: "QAS", non_production: true }); } });
    When.waitFor({
        id: "worklistRetry", ...WL, visible: false, enabled: false, autoWait: false,
        success: (b: UI5Element) => (b as Button).firePress()
    });
    worklistIde(Then, (ide) => (ide.getProperty("/targets") as string[]).includes("QAS"), "the worklist knows QAS");
    press(When, "convNonProduction");
    iAnswer(When, ON_TITLE, ON_ACTION);
    Then.waitFor({
        check: () => answered(`PUT conventions/${DEV}`) === 1,
        success: () => Opa5.assert.ok(true, "the flag was saved"),
        errorMessage: "No PUT"
    });
    worklistIde(Then, (ide) => (ide.getProperty("/diagnoseTargets") as string[]).includes(DEV), "the changed target is merged in");
    worklistIde(Then, (ide) => (ide.getProperty("/targets") as string[]).includes("QAS")
        && (ide.getProperty("/diagnoseTargets") as string[]).includes("QAS"), "QAS, unknown to the dialog, is kept");
    Then.iStopTheApp();
});

// --- 5: create 422 by field ---------------------------------------------------------

opaTest("F5: create: a 422 that names the target marks the name; any other 422 shows the server's message", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; });
    openDialog(When);
    press(When, "convNewTargetButton");
    enter(When, "convNewTarget", "DEMO3");
    When.waitFor({
        success: () => {
            backend.failNext = { path: "conventions", status: 422, body: { detail: [{ loc: ["body", "free_text"], msg: "String should have at most 20000 characters" }] } };
        }
    });
    press(When, "convSaveButton");
    theMessageSays(Then, "String should have at most 20000 characters", "the server's message is shown");
    closeMessage(When);
    property(Then, "convNewTarget", "valueState", "None", "the name is not blamed");
    When.waitFor({
        success: () => {
            backend.failNext = { path: "conventions", status: 422, body: { detail: [{ loc: ["body", "target"], msg: "String should match pattern" }] } };
        }
    });
    press(When, "convSaveButton");
    property(Then, "convNewTarget", "valueState", "Error", "a 422 on the target marks the name");
    Then.iStopTheApp();
});

// --- 6: Save without a target ----------------------------------------------------------

opaTest("F6: Save is off while no target is selected in edit mode", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => { fake.isAdmin = true; fake.conventions = []; });
    When.waitFor({ id: "conventionsButton", ...APP, actions: new Press() });
    Then.waitFor({
        id: "conventionsDialog", ...APP,
        check: (d: UI5Element) => (d as Dialog).isOpen() && !(d as Dialog).getBusy(),
        success: () => Opa5.assert.ok(true, "loaded"),
        errorMessage: "The dialog did not load"
    });
    property(Then, "convSaveButton", "enabled", false, "no target: Save is off");
    press(When, "convNewTargetButton");
    property(Then, "convSaveButton", "enabled", true, "create mode: Save (Create) is on");
    When.waitFor({ id: "convCancelNewButton", ...D, actions: new Press() });
    property(Then, "convSaveButton", "enabled", false, "back without a target: off again");
    Then.iStopTheApp();
});
