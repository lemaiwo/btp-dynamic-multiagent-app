import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import ElementRegistry from "sap/ui/core/ElementRegistry";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Popover from "sap/m/Popover";
import type TextArea from "sap/m/TextArea";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iSend, thePrimaryActionIs } from "./pages/Session";
import { closeMessageBox } from "./pages/Shared";
import { seedDoc, theBlocksAreShown, win } from "./ReviewFollowupsJourney";

/**
 * Review follow-ups 4: the comment popover when its own marker is pressed
 * again (B-1, B-5), a save that ends after the popover moved to another
 * block (B-2), a Save on a popover that is closing for a queued open (B-4),
 * and Stop refused because the run is on another instance (E1).
 */
QUnit.module("Review follow-ups 4");

const DOC_HASH = "sessions/s-1?view=document&kind=design&version=1";
const POST_COMMENT = "POST sessions/s-1/comments";

const commentPosts = (): Record<string, unknown>[] =>
    backend.bodies.filter((b) => b.key === POST_COMMENT).map((b) => b.body as Record<string, unknown>);

/** Presses the comment marker of block `index` (0-based), focused first as a click does. */
function iPressMarker(When: Common, index: number, before?: () => void): void {
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--docMarker-") && c.getBindingContext("s")?.getPath() === `/artifact/doc/blocks/${index}`,
        success: function (list: UI5Element[]) {
            before?.();
            const marker = list[0] as Button;
            (marker.getFocusDomRef() as HTMLElement).focus();
            marker.firePress();
        },
        errorMessage: `No marker on block ${index + 1}`
    });
}

interface Watch { popover?: Popover; opens: number; closes: number }

/** The popover is open with `title`; from the first call on its opens and closes are counted in `watch`. */
function thePopoverIsOpen(Then: Common, title: string, watch: Watch, opens?: number): void {
    Then.waitFor({
        id: "commentPopover", ...SOPTS, searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Popover).isOpen() && (c as Popover).getTitle() === title
            && (opens === undefined || watch.opens === opens),
        success: (c: UI5Element) => {
            if (!watch.popover) {
                watch.popover = c as Popover;
                watch.popover.attachAfterOpen(() => { watch.opens++; });
                watch.popover.attachAfterClose(() => { watch.closes++; });
            }
            Opa5.assert.ok(true, `the popover is open: ${title}`);
        },
        errorMessage: `The popover is not open with "${title}" (opens ${watch.opens})`
    });
}

/** Waits `ms` without OPA's autoWait (a popover reopening would otherwise be waited for). */
function iWait(Then: Common, ms: number): void {
    let start = 0;
    Then.waitFor({
        autoWait: false,
        check: () => {
            start ||= Date.now();
            return Date.now() - start >= ms;
        },
        success: () => Opa5.assert.ok(true, `${ms} ms later`)
    });
}

// --- B-1 / B-5: the marker of the open popover pressed again ---------------------

opaTest("B-1: the marker of the open popover pressed again, then Escape: the popover stays closed, the focus is on the marker", function (Given: Common, When: Common, Then: Common) {
    const watch: Watch = { opens: 0, closes: 0 };
    Given.iStartTheApp(DOC_HASH, undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    iPressMarker(When, 0);
    thePopoverIsOpen(Then, "Comments on block 1", watch);
    iPressMarker(When, 0);
    // Pressing the opener of the open popover changes nothing on screen.
    thePopoverIsOpen(Then, "Comments on block 1", watch, 0);
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        success: function (draft: UI5Element) {
            const dom = (draft as TextArea).getFocusDomRef() as HTMLElement;
            dom.focus();
            QUnitUtils.triggerKeydown(dom, "ESCAPE");
            QUnitUtils.triggerKeyup(dom, "ESCAPE");
        },
        errorMessage: "No draft to press Escape in"
    });
    Then.waitFor({
        autoWait: false,
        check: () => watch.closes === 1,
        success: () => Opa5.assert.ok(true, "Escape closed the popover"),
        errorMessage: "Escape did not close the popover"
    });
    iWait(Then, 1200);
    Then.waitFor({
        autoWait: false,
        success: function () {
            const marker = win().document.activeElement as HTMLElement | null;
            Opa5.assert.strictEqual(watch.opens, 0, "the popover did not open again");
            Opa5.assert.notOk(watch.popover!.isOpen(), "the popover is closed");
            Opa5.assert.ok(!!marker && marker.id.includes("--docMarker-"), `the focus is back on the marker (${marker?.id ?? "-"})`);
        }
    });
    Then.iStopTheApp();
});

// --- B-4: Save on the popover that is closing for a queued open -------------------

opaTest("B-4: Save pressed on the closing popover while B's open is queued stores nothing on A", function (Given: Common, When: Common, Then: Common) {
    const watch: Watch = { opens: 0, closes: 0 };
    let saved = false;
    Given.iStartTheApp(DOC_HASH, undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    iPressMarker(When, 0);
    thePopoverIsOpen(Then, "Comments on block 1", watch);
    // A's popover starts closing, then marker B is pressed: B's open is queued behind the close.
    iPressMarker(When, 1, () => {
        watch.popover!.close();
        // While A still closes: text arrives in the draft and Save on the closing popover is pressed.
        setTimeout(() => {
            const draft = ElementRegistry.filter((e) => /--commentDraft$/.test(e.getId())) as TextArea[];
            const save = ElementRegistry.filter((e) => /--commentSave$/.test(e.getId())) as Button[];
            draft[0].setValue("Typed for block two.");
            draft[0].fireLiveChange({ value: "Typed for block two." });
            save[0].firePress();
            saved = true;
        }, 30);
    });
    Then.waitFor({
        autoWait: false,
        check: () => saved,
        success: () => Opa5.assert.ok(true, "Save was pressed while A was closing"),
        errorMessage: "Save was not pressed"
    });
    iWait(Then, 800);
    Then.waitFor({
        autoWait: false,
        success: function () {
            Opa5.assert.deepEqual(commentPosts().filter((b) => b.paragraph === 0), [], "nothing was stored on block 1");
        }
    });
    thePopoverIsOpen(Then, "Comments on block 2", watch, 1);
    Then.iStopTheApp();
});

// --- B-2: a save that ends after the popover moved to another block -----------------

opaTest("B-2: A's save succeeds after B's popover opened: B's popover stays open, not busy, with its draft", function (Given: Common, When: Common, Then: Common) {
    const watch: Watch = { opens: 0, closes: 0 };
    let release: (() => void) | undefined;
    Given.iStartTheApp(DOC_HASH, undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    // A is below B: A's new comment, shown under A, does not move B's marker (a docked popover
    // closes when its opener moves, sap.m.Popover followOf).
    iPressMarker(When, 2);
    thePopoverIsOpen(Then, "Comments on block 3", watch);
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "On block three.", keepFocus: true }),
        errorMessage: "No draft"
    });
    When.waitFor({
        id: "commentSave", ...SOPTS, searchOpenDialogs: true,
        actions: function (c: UI5Element | null) {
            release = backend.hold(POST_COMMENT);
            new Press().executeOn(c as Control);
        },
        errorMessage: "Save is not enabled"
    });
    iPressMarker(When, 1);
    thePopoverIsOpen(Then, "Comments on block 2", watch, 1);
    Then.waitFor({
        id: "commentSave", ...SOPTS, searchOpenDialogs: true, enabled: false, autoWait: false,
        success: function () {
            const model = watch.popover!.getModel("s");
            Opa5.assert.notOk(model?.getProperty("/pop/busy"), "B's popover is not busy with A's save");
        }
    });
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "Draft on two.", keepFocus: true }),
        success: () => { release!(); },
        errorMessage: "No draft on B"
    });
    Then.waitFor({
        autoWait: false,
        check: () => backend.responses.includes(POST_COMMENT),
        success: () => Opa5.assert.deepEqual(commentPosts().map((b) => b.paragraph), [2], "A's comment was stored on block 3"),
        errorMessage: "A's save did not end"
    });
    iWait(Then, 800);
    Then.waitFor({
        autoWait: false,
        success: function () {
            const model = watch.popover!.getModel("s");
            Opa5.assert.ok(watch.popover!.isOpen(), "B's popover is still open");
            Opa5.assert.strictEqual(watch.closes, 1, "only the move to B closed it");
            Opa5.assert.strictEqual(watch.popover!.getTitle(), "Comments on block 2");
            Opa5.assert.strictEqual(model?.getProperty("/pop/draft"), "Draft on two.", "B's draft is kept");
            Opa5.assert.notOk(model?.getProperty("/pop/busy"), "not busy");
        }
    });
    Then.iStopTheApp();
});

opaTest("B-2: A's save is refused after B's popover opened: A's error is not shown in B's popover", function (Given: Common, When: Common, Then: Common) {
    const watch: Watch = { opens: 0, closes: 0 };
    let release: (() => void) | undefined;
    Given.iStartTheApp(DOC_HASH, undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    // A is below B: A's new comment, shown under A, does not move B's marker (a docked popover
    // closes when its opener moves, sap.m.Popover followOf).
    iPressMarker(When, 2);
    thePopoverIsOpen(Then, "Comments on block 3", watch);
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "On block three.", keepFocus: true }),
        errorMessage: "No draft"
    });
    When.waitFor({
        id: "commentSave", ...SOPTS, searchOpenDialogs: true,
        actions: function (c: UI5Element | null) {
            release = backend.hold(POST_COMMENT);
            backend.failNext = { path: "sessions/s-1/comments", status: 422, body: { detail: "Too long" } };
            new Press().executeOn(c as Control);
        },
        errorMessage: "Save is not enabled"
    });
    iPressMarker(When, 1);
    thePopoverIsOpen(Then, "Comments on block 2", watch, 1);
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "Draft on two.", keepFocus: true }),
        success: () => { release!(); },
        errorMessage: "No draft on B"
    });
    Then.waitFor({
        autoWait: false,
        check: () => backend.responses.includes(POST_COMMENT),
        success: () => Opa5.assert.ok(true, "A's save was answered"),
        errorMessage: "A's save did not end"
    });
    iWait(Then, 800);
    Then.waitFor({
        autoWait: false,
        success: function () {
            const model = watch.popover!.getModel("s");
            Opa5.assert.ok(watch.popover!.isOpen(), "B's popover is still open");
            Opa5.assert.strictEqual(watch.popover!.getTitle(), "Comments on block 2");
            Opa5.assert.strictEqual(model?.getProperty("/pop/error"), "", "A's refusal is not shown in B's popover");
            const toast = win().document.querySelector(".sapMMessageToast");
            Opa5.assert.ok(!!toast && /could not be saved/.test(toast.textContent ?? ""), `A's refusal is said in a toast (${toast?.textContent ?? "-"})`);
            Opa5.assert.strictEqual(model?.getProperty("/pop/draft"), "Draft on two.", "B's draft is kept");
            Opa5.assert.notOk(model?.getProperty("/pop/busy"), "not busy");
        }
    });
    Then.iStopTheApp();
});

// --- E1: Stop refused because the run is on another instance ----------------------

opaTest("E1: after Stop is answered 409 run_on_other_instance the page watches the session and hides Stop once it is idle", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { release = fake.pauseStream(); });
    thePrimaryActionIs(Then, "Start design", true, "loaded");
    iSend(When, "Analyse the impact");
    Then.waitFor({
        id: "stopButton", ...SOPTS,
        success: () => Opa5.assert.ok(true, "Stop is shown while the run goes"),
        errorMessage: "No Stop"
    });
    When.waitFor({
        id: "stopButton",
        ...SOPTS,
        actions: function (control: UI5Element | null) {
            backend.failNext = {
                path: "sessions/s-1/cancel", status: 409,
                body: { detail: "The run is on another instance.", code: "run_on_other_instance" }
            };
            new Press().executeOn(control as Control);
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        check: (texts: UI5Element[]) => texts.some((t) => /another server instance/.test(String(t.getProperty("text")))),
        success: () => Opa5.assert.ok(true, "the 409 is explained"),
        errorMessage: "The 409 is not explained"
    });
    closeMessageBox(When);
    Then.waitFor({
        id: "stopButton", ...SOPTS, visible: false, autoWait: false,
        success: function (c: UI5Element) {
            Opa5.assert.ok((c as Control).getVisible(), "the session still runs elsewhere: Stop stays");
            // The run on the other instance ends.
            release?.();
        }
    });
    Then.waitFor({
        id: "stopButton", ...SOPTS, visible: false,
        timeout: 12,
        check: (c: UI5Element) => !(c as Control).getVisible(),
        success: function () {
            Opa5.assert.ok(backend.requests.filter((r) => r === "GET sessions/s-1").length >= 2, "the session was read again (watched)");
            Opa5.assert.strictEqual(backend.dataOf("s-1")!.session.status, "idle", "the server says idle");
        },
        errorMessage: "Stop is still shown although the session is idle"
    });
    Then.iStopTheApp();
});
