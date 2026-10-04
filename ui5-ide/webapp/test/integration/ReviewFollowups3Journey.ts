import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Dialog from "sap/m/Dialog";
import type Popover from "sap/m/Popover";
import type Select from "sap/m/Select";
import type TextArea from "sap/m/TextArea";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, thePrimaryActionIs } from "./pages/Session";
import { announced, recordAnnouncements } from "./pages/Shared";
import {
    active, block, iPressKeyOn, posts, seedChanges, seedDoc, theBlocksAreShown, theDiffIsShown, win
} from "./ReviewFollowupsJourney";
import {
    APP, D, DEV, buttonsOf, closeDialog, enter, iAnswer, openDialog, press, property, textOf, theConfirmation
} from "./ConventionsJourney";

/**
 * Review follow-ups 3 (after U12): the approve settle window explains itself
 * and survives lost key/pointer ends and a hidden tab (B1), a failed comment
 * save keeps its popover (14-a), a deferred popover open keeps Save working
 * (A1-a), and the conventions dialog does not drop unsaved edits (D1).
 */
QUnit.module("Review follow-ups 3");

const SETTLING = "Look at the changes first. Approve is available in a moment.";
const IGNORED = "Not approved: the changes have only just appeared. Look at them first, then press Approve changes again.";
const APPROVE = "Approve changes and review";

/** A mouse event as the browser sends it on the button's DOM. */
function mouse(el: Element, type: string): void {
    const W = win() as unknown as { MouseEvent: typeof MouseEvent };
    el.dispatchEvent(new W.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: type === "mousedown" ? 1 : 0, view: win() }));
}

/** A pointer event (pointerdown, pointercancel) on `el`. */
function pointer(el: Element, type: string): void {
    const W = win() as unknown as { PointerEvent: typeof PointerEvent };
    el.dispatchEvent(new W.PointerEvent(type, { bubbles: true, cancelable: true, button: 0, pointerId: 1, pointerType: "mouse" }));
}

/** An Enter keydown on `el`, as the first stroke of a key that goes down. */
function enterDown(el: Element): void {
    const W = win() as unknown as { KeyboardEvent: typeof KeyboardEvent };
    const ev = new W.KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, cancelable: true });
    Object.defineProperty(ev, "keyCode", { get: () => 13 });
    Object.defineProperty(ev, "which", { get: () => 13 });
    el.dispatchEvent(ev);
}

function primaryDom(control: UI5Element): HTMLElement {
    return (control as Control).getDomRef() as HTMLElement;
}

/** Waits `ms`, then: no approve was sent. */
function noApproveAfter(Then: Common, ms: number, message: string): void {
    let start = 0;
    Then.waitFor({
        check: () => {
            start ||= Date.now();
            return Date.now() - start >= ms;
        },
        success: () => Opa5.assert.strictEqual(posts("POST sessions/s-1/approve"), 0, message)
    });
}

function theApproveIsSent(Then: Common, message: string): void {
    Then.waitFor({
        check: () => posts("POST sessions/s-1/approve") === 1,
        success: () => Opa5.assert.ok(true, message),
        errorMessage: "No approve"
    });
}

/**
 * Review changes while the file reads are held; `during(button)` runs while
 * the cards load (before they are on screen), then the reads are released.
 */
function reviewWithCardsHeld(When: Common, Then: Common, during: (button: HTMLElement) => void): void {
    let release!: () => void;
    thePrimaryActionIs(Then, "Review changes", true, "without the cards the action reviews");
    When.waitFor({ success: () => { release = backend.hold("GET sessions/s-1/file"); } });
    iPress(When, "primaryAction");
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        check: () => backend.requests.includes("GET sessions/s-1/file"),
        success: function (button: UI5Element) {
            during(primaryDom(button));
            release();
        }
    });
    theDiffIsShown(Then, 1);
}

// --- B1: the approve settle window ------------------------------------------------

opaTest("B1-a: while Approve settles it says why; a press that began before the cards is announced, not silent", function (Given: Common, When: Common, Then: Common) {
    // Sampled every frame from the moment the reads are released: OPA's autoWait would wait for the settle timer itself.
    const seen = { reason: false, described: false, enabled: false };
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    Then.waitFor({ id: "primaryAction", ...SOPTS, success: recordAnnouncements });
    reviewWithCardsHeld(When, Then, (button) => {
        // The pointer goes down while the cards load, and stays down past the settle window.
        mouse(button, "mousedown");
        let frames = 0;
        const tick = (): void => {
            const reason = win().document.querySelector<HTMLElement>("[id$='--primaryReason']");
            const action = win().document.querySelector<HTMLElement>("[id$='--primaryAction']");
            if (reason && reason.offsetParent && reason.textContent === SETTLING) {
                seen.reason = true;
                seen.described = !!action?.getAttribute("aria-describedby")?.split(" ").includes(reason.id);
                seen.enabled = !!action && !action.hasAttribute("disabled") && action.getAttribute("aria-disabled") !== "true";
            } else if (++frames < 600) {
                win().requestAnimationFrame(tick);
            }
        };
        win().requestAnimationFrame(tick);
    });
    Then.waitFor({
        check: () => seen.reason,
        success: function () {
            Opa5.assert.ok(true, `while the cards settle the reason is shown: "${SETTLING}"`);
            Opa5.assert.ok(seen.described, "the button's description is the reason");
            Opa5.assert.notOk(seen.enabled, "and the button is off meanwhile");
        },
        errorMessage: `The settle reason "${SETTLING}" was not shown`
    });
    thePrimaryActionIs(Then, APPROVE, true, "settled: Approve is on");
    Then.waitFor({
        id: "primaryReason",
        ...SOPTS,
        visible: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: () => Opa5.assert.ok(true, "and the reason is gone")
    });
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        success: function (button: UI5Element) {
            announced.length = 0;
            // The pointer that went down before the cards comes up now: that click is no approve of them.
            mouse(primaryDom(button), "mouseup");
            mouse(primaryDom(button), "click");
        }
    });
    noApproveAfter(Then, 500, "the press that began before the cards approved nothing");
    Then.waitFor({
        check: () => announced.includes(IGNORED),
        success: () => Opa5.assert.ok(true, "and it was announced, not ignored silently"),
        error: () => Opa5.assert.ok(false, `Not announced: ${announced.join(" | ")}`)
    });
    iPress(When, "primaryAction");
    theApproveIsSent(Then, "a deliberate press approves");
    Then.iStopTheApp();
});

opaTest("B1-b: a key whose keyup was lost (the window lost the focus) does not block a later approve", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    reviewWithCardsHeld(When, Then, (button) => {
        enterDown(button);
        // The window loses the focus with the key down: its keyup never reaches the page.
        win().dispatchEvent(new Event("blur"));
    });
    thePrimaryActionIs(Then, APPROVE, true, "the cards are visible");
    iPress(When, "primaryAction");
    theApproveIsSent(Then, "a later deliberate press approves");
    Then.iStopTheApp();
});

opaTest("B1-b: a pointer that was cancelled (no pointerup) does not block a later approve", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    reviewWithCardsHeld(When, Then, (button) => {
        pointer(button, "pointerdown");
        pointer(button, "pointercancel");
    });
    thePrimaryActionIs(Then, APPROVE, true, "the cards are visible");
    iPress(When, "primaryAction");
    theApproveIsSent(Then, "a later deliberate press approves");
    Then.iStopTheApp();
});

opaTest("B1-c: cards rendered while the tab is hidden settle only once the tab is visible", function (Given: Common, When: Common, Then: Common) {
    const docu = (): Document => win().document;
    const setHidden = (hidden: boolean): void => {
        if (hidden) {
            Object.defineProperty(docu(), "visibilityState", { configurable: true, get: () => "hidden" });
            Object.defineProperty(docu(), "hidden", { configurable: true, get: () => true });
        } else {
            delete (docu() as unknown as Record<string, unknown>).visibilityState;
            delete (docu() as unknown as Record<string, unknown>).hidden;
        }
        docu().dispatchEvent(new Event("visibilitychange"));
    };
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    thePrimaryActionIs(Then, "Review changes", true, "without the cards the action reviews");
    When.waitFor({ success: () => setHidden(true) });
    iPress(When, "primaryAction");
    theDiffIsShown(Then, 1);
    let start = 0;
    Then.waitFor({
        id: "primaryAction",
        ...SOPTS,
        enabled: false,
        autoWait: false,
        check: () => {
            start ||= Date.now();
            return Date.now() - start >= 1500;
        },
        success: function (button: UI5Element) {
            Opa5.assert.notOk((button as Button).getEnabled(), "1.5 s later, still hidden: Approve is not on yet");
            Opa5.assert.strictEqual(win().document.querySelector("[id$='--primaryReason']")?.textContent, SETTLING, "and says why");
            Opa5.assert.strictEqual(posts("POST sessions/s-1/approve"), 0, "nothing was approved");
            setHidden(false);
        }
    });
    Then.waitFor({
        id: "primaryAction",
        ...SOPTS,
        enabled: false,
        autoWait: false,
        pollingInterval: 20,
        success: function (button: UI5Element) {
            Opa5.assert.notOk((button as Button).getEnabled(), "right after the tab is visible the window starts: still off");
        }
    });
    thePrimaryActionIs(Then, APPROVE, true, "settled after the tab became visible");
    iPress(When, "primaryAction");
    theApproveIsSent(Then, "then a press approves");
    Then.waitFor({ success: () => Opa5.assert.strictEqual(win().document.visibilityState, "visible", "the tab is visible again") });
    Then.iStopTheApp();
});

// --- 14-a: a failed comment save keeps the popover --------------------------------

function thePopoverIsOpen(Then: Common, title: string): void {
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Popover).isOpen() && (c as Popover).getTitle() === title,
        success: () => Opa5.assert.ok(true, `"${title}" is open`),
        errorMessage: `"${title}" is not open`
    });
}

function iTypeComment(When: Common, text: string): void {
    When.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text, keepFocus: true }),
        errorMessage: "No comment text area"
    });
}

/** Save as a click does it: the button takes the focus, then fires its press. */
function iClickSave(When: Common): void {
    When.waitFor({
        id: "commentSave",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (button: UI5Element) {
            ((button as Button).getFocusDomRef() as HTMLElement).focus();
            (button as Button).firePress();
        },
        errorMessage: "Save is not enabled"
    });
}

opaTest("14-a: a refused save keeps the popover open with the error and the draft; the focus waits on the popover meanwhile", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    iPressKeyOn(When, () => block(0), "ENTER");
    thePopoverIsOpen(Then, "Comments on block 1");
    iTypeComment(When, "Keep this draft.");
    When.waitFor({
        success: function () {
            release = backend.hold("POST sessions/s-1/comments");
            backend.failNext = { path: "sessions/s-1/comments", status: 422, body: { detail: "bad", code: "invalid_anchor" } };
        }
    });
    iClickSave(When);
    let start = 0;
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        visible: false,
        autoWait: false,
        check: () => {
            start ||= Date.now();
            return Date.now() - start >= 600;
        },
        success: function (p: UI5Element) {
            const popover = p as Popover;
            Opa5.assert.ok(popover.isOpen(), "while the save is on its way the popover stays open");
            Opa5.assert.strictEqual(active(), popover.getDomRef(), "the focus waits on the popover itself (it can take it)");
            release();
        }
    });
    Then.waitFor({
        id: "commentPopoverError",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "The comment could not be saved: check its length (max 4000 characters)." }),
        success: () => Opa5.assert.ok(true, "the refusal is shown in the popover"),
        errorMessage: "The error is not shown in an open popover"
    });
    Then.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => active() === (c as TextArea).getFocusDomRef(),
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as TextArea).getValue(), "Keep this draft.", "the draft is intact");
            Opa5.assert.ok(true, "and has the focus again");
        },
        errorMessage: "The draft does not have the focus after the refusal"
    });
    // The popover is still usable: a second Save goes through.
    iClickSave(When);
    Then.waitFor({
        check: () => backend.dataOf("s-1")!.comments.length === 1,
        success: () => Opa5.assert.strictEqual(backend.dataOf("s-1")!.comments[0].body, "Keep this draft.", "saved on the second try"),
        errorMessage: "The second Save did not store the comment"
    });
    Then.iStopTheApp();
});

// --- A1-a: a deferred open keeps Save working --------------------------------------

opaTest("A1-a: a popover opened from another block while one is open (closes, then opens) still saves", function (Given: Common, When: Common, Then: Common) {
    let opens = 0;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    iPressKeyOn(When, () => block(0), "ENTER");
    thePopoverIsOpen(Then, "Comments on block 1");
    When.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        success: (p: UI5Element) => { (p as Popover).attachAfterOpen(() => { opens++; }); }
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--docMarker-") && c.getBindingContext("s")?.getPath() === "/artifact/doc/blocks/1",
        success: function (list: UI5Element[]) {
            // A click while the first popover is open: the marker takes the focus, then fires its press.
            // sap.m.Popover closes and opens again (for the marker) after its afterClose.
            const marker = list[0] as Button;
            (marker.getFocusDomRef() as HTMLElement).focus();
            marker.firePress();
        },
        errorMessage: "No marker on block 2"
    });
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (p: UI5Element) => opens === 1 && (p as Popover).isOpen() && (p as Popover).getTitle() === "Comments on block 2",
        success: () => Opa5.assert.ok(true, "the popover closed and opened again for block 2"),
        error: function () {
            const p = win().document.querySelector<HTMLElement>("[id$='--commentPopover']");
            Opa5.assert.ok(false, `The popover did not open again for block 2 (opens ${opens}, shown ${String(!!p?.offsetParent)}, `
                + `title ${p?.querySelector(".sapMTitle")?.textContent ?? "-"}, focus ${active()?.id ?? active()?.tagName ?? "-"})`);
        },
        errorMessage: "The popover did not open again for block 2"
    });
    iTypeComment(When, "From the marker.");
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Save is not enabled" });
    Then.waitFor({
        check: () => backend.dataOf("s-1")!.comments.length === 1,
        success: function () {
            const saved = backend.dataOf("s-1")!.comments[0];
            Opa5.assert.strictEqual(saved.paragraph, 1, "the comment is on block 2, the block the popover was opened for");
            Opa5.assert.strictEqual(saved.body, "From the marker.");
        },
        errorMessage: "Save did nothing after the deferred open"
    });
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        visible: false,
        check: (p: UI5Element) => !(p as Popover).isOpen(),
        success: () => Opa5.assert.ok(true, "and the popover closed after the save"),
        errorMessage: "The popover did not close after the save"
    });
    Then.iStopTheApp();
});

// --- D1: the conventions dialog does not drop unsaved edits ---------------------------

const DISCARD_TITLE = "Unsaved Changes";
const DISCARD = "Discard";
const QA = "qa-system";

function theDiscardQuestion(Then: Common, message: string): void {
    theConfirmation(Then, DISCARD_TITLE, function (dialog: Dialog) {
        Opa5.assert.ok(textOf(dialog).includes("Discard your changes"), message);
        const buttons = buttonsOf(dialog);
        Opa5.assert.deepEqual(buttons.map((b) => b.getText()), [DISCARD, "Cancel"], "Discard and Cancel");
        Opa5.assert.ok(buttons.every((b) => b.getType() !== "Emphasized"), "no action is emphasized");
        Opa5.assert.ok(String(dialog.getInitialFocus()).endsWith(buttons[1].getId()) || dialog.getInitialFocus() === buttons[1].getId(),
            "the focus starts on Cancel");
    });
}

function iPickTarget(When: Common, target: string): void {
    When.waitFor({
        id: "convTarget", ...D,
        success: function (control: UI5Element) {
            // As the Select does on a user's pick: the selection changes, then `change` fires.
            const select = control as Select;
            const item = select.getItems().find((i) => i.getKey() === target)!;
            select.setSelectedItem(item);
            select.fireChange({ selectedItem: item });
        },
        errorMessage: `Cannot pick ${target}`
    });
}

function theDialogIsOpen(Then: Common, open: boolean, message: string): void {
    Then.waitFor({
        id: "conventionsDialog", ...APP, visible: false,
        check: (c: UI5Element) => (c as Dialog).isOpen() === open,
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `The conventions dialog is ${open ? "not " : ""}open`
    });
}

opaTest("D1: switching target, New target, Close and Escape with unsaved edits ask first; Cancel keeps them, Discard drops them", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.isAdmin = true;
        fake.conventions.push({ target: QA, package: "ZQA" });
    });
    openDialog(When);
    property(Then, "convTarget", "selectedKey", DEV, "the first target is shown");
    enter(When, "convPackage", "ZEDITED");

    // Another target.
    iPickTarget(When, QA);
    theDiscardQuestion(Then, "switching target asks");
    iAnswer(When, DISCARD_TITLE, "Cancel");
    property(Then, "convTarget", "selectedKey", DEV, "Cancel: the edited target stays selected");
    property(Then, "convPackage", "value", "ZEDITED", "with the edit");
    iPickTarget(When, QA);
    iAnswer(When, DISCARD_TITLE, DISCARD);
    property(Then, "convTarget", "selectedKey", QA, "Discard: the other target is shown");
    property(Then, "convPackage", "value", "ZQA", "as stored");

    // New target.
    enter(When, "convPackage", "ZQA2");
    press(When, "convNewTargetButton");
    theDiscardQuestion(Then, "New target asks");
    iAnswer(When, DISCARD_TITLE, "Cancel");
    property(Then, "convNewTarget", "visible", false, "Cancel: still editing the target");
    property(Then, "convPackage", "value", "ZQA2", "with the edit");

    // Close and Escape.
    press(When, "convCloseButton");
    theDiscardQuestion(Then, "Close asks");
    iAnswer(When, DISCARD_TITLE, "Cancel");
    theDialogIsOpen(Then, true, "Cancel: the dialog stays open");
    When.waitFor({
        id: "convCloseButton", ...D,
        success: function (button: UI5Element) {
            const dom = (button as Control).getFocusDomRef() as HTMLElement;
            dom.focus();
            QUnitUtils.triggerKeydown(dom, "ESCAPE");
        }
    });
    theDiscardQuestion(Then, "Escape asks");
    iAnswer(When, DISCARD_TITLE, "Cancel");
    theDialogIsOpen(Then, true, "Cancel: the dialog stays open");
    property(Then, "convPackage", "value", "ZQA2", "with the edit");
    press(When, "convCloseButton");
    iAnswer(When, DISCARD_TITLE, DISCARD);
    theDialogIsOpen(Then, false, "Discard: the dialog closes");
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(backend.bodies.filter((b) => b.key.startsWith("PUT conventions/")).length, 0, "nothing was saved");
        }
    });

    // A new target with a name typed: Close asks too.
    openDialog(When);
    press(When, "convNewTargetButton");
    enter(When, "convNewTarget", "NEW1");
    press(When, "convCloseButton");
    theDiscardQuestion(Then, "Close of an unsaved new target asks");
    iAnswer(When, DISCARD_TITLE, DISCARD);
    theDialogIsOpen(Then, false, "Discard: closed");
    Then.waitFor({
        success: () => Opa5.assert.strictEqual(backend.bodies.filter((b) => b.key === "POST conventions").length, 0, "nothing was created")
    });

    // Nothing changed: no question.
    openDialog(When);
    closeDialog(When);
    Then.iStopTheApp();
});
