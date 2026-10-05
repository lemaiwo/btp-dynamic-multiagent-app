import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type UI5Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type Popover from "sap/m/Popover";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, thePrimaryActionIs } from "./pages/Session";
import { block, iPressKeyOn, seedChanges, seedDoc, theBlocksAreShown, theDiffIsShown, win } from "./ReviewFollowupsJourney";

/**
 * U12 fix round 1, session page: a comment popover requested while the
 * previous one is still closing opens once that one has closed (the latest
 * request wins), and the approve settle reason is shown on the first render
 * of the changes after navigation only (the settle guard stays on every render).
 */
QUnit.module("U12 fix 1: session page");

const SETTLING = "Look at the changes first. Approve is available in a moment.";
const APPROVE = "Approve changes and review";

// --- 11: a popover requested while the previous one closes --------------------------

opaTest("F11: marker B pressed while A's popover is closing opens B's popover; Save stores on B", function (Given: Common, When: Common, Then: Common) {
    let popover: Popover | undefined;
    let opens = 0;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    iPressKeyOn(When, () => block(0), "ENTER");
    Then.waitFor({
        id: "commentPopover", ...SOPTS, searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Popover).isOpen() && (c as Popover).getTitle() === "Comments on block 1",
        success: (c: UI5Element) => {
            popover = c as Popover;
            popover.attachAfterOpen(() => { opens++; });
        },
        errorMessage: "Block 1's popover is not open"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--docMarker-") && c.getBindingContext("s")?.getPath() === "/artifact/doc/blocks/1",
        success: function (list: UI5Element[]) {
            // A's popover starts closing (its animation runs), then marker B is clicked.
            popover!.close();
            const marker = list[0] as Button;
            (marker.getFocusDomRef() as HTMLElement).focus();
            marker.firePress();
        },
        errorMessage: "No marker on block 2"
    });
    Then.waitFor({
        id: "commentPopover", ...SOPTS, searchOpenDialogs: true,
        // isOpen() is true while a popover closes: only its afterOpen proves B's open.
        check: (c: UI5Element) => opens === 1 && (c as Popover).isOpen() && (c as Popover).getTitle() === "Comments on block 2",
        success: () => Opa5.assert.ok(true, "B's popover opened with B's title"),
        error: function () {
            const p = popover;
            Opa5.assert.ok(false, `B's popover did not open (opens ${opens}, open ${String(p?.isOpen())}, title ${p?.getTitle() ?? "-"})`);
        },
        errorMessage: "B's popover did not open"
    });
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "On block two.", keepFocus: true }),
        errorMessage: "No draft"
    });
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Save is not enabled" });
    Then.waitFor({
        check: () => backend.dataOf("s-1")!.comments.length === 1,
        success: function () {
            const saved = backend.dataOf("s-1")!.comments[0];
            Opa5.assert.strictEqual(saved.paragraph, 1, "stored on block 2");
            Opa5.assert.strictEqual(saved.body, "On block two.");
        },
        errorMessage: "Save stored nothing"
    });
    Then.iStopTheApp();
});

// --- 12: the settle reason only on the first render after navigation ---------------------

const Y = "src/CLAS/zcl_y.clas.abap";

opaTest("F12: a re-render of the cards keeps Approve off for a moment but does not flash the settle reason", function (Given: Common, When: Common, Then: Common) {
    const seen = { reason: false, guarded: false, sampled: false, frames: 0, last: "", t0: 0, t2: 0, tReason: 0 };
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    thePrimaryActionIs(Then, "Review changes", true, "without the cards the action reviews");
    iPress(When, "primaryAction");
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, APPROVE, true, "settled: Approve is on");
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        success: function (button: UI5Element) {
            // A new object arrives and the approve is refused as stale: the whole card list is read and rendered again.
            const data = backend.dataOf("s-1")!;
            data.files.push({ path: Y, state: "modified", object_type: "CLAS", object_name: "ZCL_Y", origin_source: "CLASS zcl_y.", proposed_source: "" });
            backend.addRevision("s-1", Y, "CLASS zcl_y. \" new");
            backend.failNext = { path: "sessions/s-1/approve", status: 409, body: { detail: "The proposals changed.", code: "version_changed" } };
            const until = Date.now() + 10000;
            const tick = (): void => {
                const doc = win().document;
                const reason = doc.querySelector<HTMLElement>("[id$='--primaryReason']");
                const action = doc.querySelector<HTMLElement>("[id$='--primaryAction']");
                if (reason && reason.offsetParent && reason.textContent === SETTLING) {
                    seen.reason = true;
                    seen.tReason ||= Date.now() - seen.t0;
                }
                const cards = doc.querySelectorAll(".ideUnified").length;
                seen.frames++;
                if (cards === 2) {
                    seen.t2 ||= Date.now() - seen.t0;
                }
                seen.last = `${cards}/${String(action?.textContent)}/${String(action?.getAttribute("aria-busy"))}/${String(!!action?.querySelector(".sapUiLocalBusyIndicator"))}`;
                if (cards === 2 && action && !seen.sampled && action.textContent?.includes(APPROVE)
                    && action.getAttribute("aria-busy") !== "true" && !action.querySelector(".sapUiLocalBusyIndicator")) {
                    seen.sampled = true;
                    seen.guarded = action.hasAttribute("disabled") || action.getAttribute("aria-disabled") === "true";
                }
                // Past the settle window of the re-render (or a timeout): stop, OPA's autoWait counts frames as animation.
                const settled = seen.t2 > 0 && Date.now() - seen.t0 > seen.t2 + 1200;
                if (Date.now() < until && !settled) {
                    win().requestAnimationFrame(tick);
                }
            };
            seen.t0 = Date.now();
            win().requestAnimationFrame(tick);
            new Press().executeOn(button as Button);
        }
    });
    Then.waitFor({
        autoWait: false,
        check: () => win().document.querySelectorAll(".ideUnified").length === 2 && seen.sampled,
        success: () => Opa5.assert.ok(true, "the cards were rendered again"),
        error: function () {
            const doc = win().document;
            const action = doc.querySelector<HTMLElement>("[id$='--primaryAction']");
            Opa5.assert.ok(false, `The cards were not rendered again (cards ${doc.querySelectorAll(".ideUnified").length}, `
                + `sampled ${String(seen.sampled)} frames ${seen.frames} t2 ${seen.t2} tReason ${seen.tReason} last ${seen.last}, action "${action?.textContent ?? "-"}" busy ${action?.getAttribute("aria-busy") ?? "-"}, `
                + `requests ${backend.requests.filter((r) => r.includes("approve") || r.includes("/file")).join(", ")})`);
        },
        errorMessage: "The cards were not rendered again"
    });
    let start = 0;
    Then.waitFor({
        autoWait: false,
        check: () => {
            start ||= Date.now();
            return Date.now() - start >= 1500;
        },
        success: function () {
            Opa5.assert.notOk(seen.reason, "no settle reason on a re-render");
            Opa5.assert.ok(seen.guarded, "but Approve was off right after the re-render (the guard stays)");
        }
    });
    Then.iStopTheApp();
});
