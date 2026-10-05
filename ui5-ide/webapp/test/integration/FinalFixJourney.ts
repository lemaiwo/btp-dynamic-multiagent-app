import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Dialog from "sap/m/Dialog";
import type Text from "sap/m/Text";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, designSession, iPress, thePrimaryActionIs, theReasonIs } from "./pages/Session";
import { X, active, posts, seedChanges, theDiffIsShown, win } from "./ReviewFollowupsJourney";
import type FakeBackend from "./FakeBackend";

/**
 * Final review fixes (the developer can only approve what was on screen):
 * a card replaced in place gets the settle window and Approve waits for the
 * cards to catch up with the session (M1); a large object shows its hunks
 * and one that cannot be compared asks first (M2); an older document
 * version holds its approve (m1).
 */
QUnit.module("Final review fixes");

const APPROVE = "Approve changes and review";
const LOADING = "Loading the latest revisions…";
const SID = "s-1";

function waitMs(Then: Common, ms: number): void {
    let start = 0;
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false,
        check: () => { start ||= Date.now(); return Date.now() - start >= ms; },
        success: () => Opa5.assert.ok(true, `${ms} ms later`)
    });
}

function approveBodies(): unknown[] {
    return backend.bodies.filter((b) => b.key === `POST sessions/${SID}/approve`).map((b) => b.body);
}

function primary(): HTMLElement | null {
    return win().document.querySelector<HTMLElement>("[id$='--primaryAction']");
}

function isOn(el: HTMLElement | null): boolean {
    return !!el && !el.hasAttribute("disabled") && el.getAttribute("aria-disabled") !== "true";
}

/** The primary action's press, whatever its state (as a click that lands on it would fire it). */
function iFirePrimary(When: Common): void {
    When.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false,
        success: (b: UI5Element) => { (b as Button).firePress(); }
    });
}

/** A key as a browser sends it to `el` (keyCode/which for UI5). */
function key(el: Element, name: string): void {
    const w = win() as unknown as { KeyboardEvent: typeof KeyboardEvent };
    const codes: Record<string, number> = { Enter: 13, Escape: 27, " ": 32 };
    ["keydown", "keyup"].forEach((type) => {
        const event = new w.KeyboardEvent(type, { key: name, code: name === " " ? "Space" : name, bubbles: true, cancelable: true });
        Object.defineProperty(event, "keyCode", { get: () => codes[name] });
        Object.defineProperty(event, "which", { get: () => codes[name] });
        el.dispatchEvent(event);
    });
}

// --- M1: a card replaced in place ---------------------------------------------------------

opaTest("M1: a new revision after a watched run holds Approve until the card shows it and has settled; a press meanwhile approves nothing", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    const seen = { shownOff: false, shownAt: 0, pressedOnShow: false };
    Given.iStartTheApp(`sessions/${SID}?view=changes`, undefined, (fake) => {
        seedChanges(fake);
        // A run the page does not stream (another tab): the page watches it.
        fake.sessions[0].session.status = "running";
    });
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, APPROVE, false, "off while the run goes on");
    When.waitFor({
        id: "changesView", ...SOPTS,
        success: () => {
            // The run writes revision 2 of the class and ends; the card's read of it is held.
            backend.addRevision(SID, X, "* written by the watched run");
            release = backend.hold(`GET sessions/${SID}/file`);
            backend.sessions[0].session.status = "idle";
        }
    });
    Then.waitFor({
        id: "primaryReason", ...SOPTS, autoWait: false, timeout: 15,
        check: () => backend.requests.filter((k) => k === `GET sessions/${SID}/file`).length >= 2,
        success: () => Opa5.assert.ok(true, "the watched run ended and the card's new revision is being read")
    });
    thePrimaryActionIs(Then, APPROVE, false, "the session has revision 2, the card still shows 1: Approve is off");
    theReasonIs(Then, LOADING, "and says why");
    iFirePrimary(When);
    waitMs(Then, 300);
    Then.waitFor({
        id: "changesView", ...SOPTS,
        success: () => Opa5.assert.strictEqual(posts(`POST sessions/${SID}/approve`), 0, "a press while the cards are behind approves nothing")
    });
    When.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false,
        success: (b: UI5Element) => {
            const button = b as Button;
            // From the release on, every frame: the moment the card shows revision 2, Approve is off and a press is ignored.
            const tick = (): void => {
                const root = win().document.querySelector(`.ideUnified[data-path='${X}']`);
                if (root?.getAttribute("data-revision") === "2") {
                    seen.shownAt = Date.now();
                    seen.shownOff = !button.getEnabled() && !isOn(primary());
                    button.firePress();
                    seen.pressedOnShow = true;
                    return;
                }
                win().requestAnimationFrame(tick);
            };
            win().requestAnimationFrame(tick);
            release();
        }
    });
    Then.waitFor({
        id: "changesView", ...SOPTS, autoWait: false,
        check: () => seen.pressedOnShow,
        success: () => Opa5.assert.ok(seen.shownOff, "when revision 2 appears, Approve is still off (settle window)"),
        errorMessage: "Revision 2 never appeared"
    });
    waitMs(Then, 300);
    Then.waitFor({
        id: "changesView", ...SOPTS,
        success: () => Opa5.assert.strictEqual(posts(`POST sessions/${SID}/approve`), 0, "a press the moment it appeared approves nothing")
    });
    thePrimaryActionIs(Then, APPROVE, true, "after the settle window Approve is on");
    iPress(When, "primaryAction");
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => approveBodies().length === 1,
        success: () => {
            Opa5.assert.ok(Date.now() - seen.shownAt >= 800, "not before the window passed");
            Opa5.assert.deepEqual(approveBodies()[0], { revisions: { [X]: 2 } }, "it approves revision 2, the one on screen");
        },
        errorMessage: "No approve"
    });
    Then.iStopTheApp();
});

// --- M2: large objects --------------------------------------------------------------------

const BIG = "src/CLAS/zcl_big.clas.abap";
const HUGE = "src/CLAS/zcl_huge.clas.abap";

function lines(n: number, tag: string): string[] {
    return Array.from({ length: n }, (_, i) => `    " ${tag} ${i + 1}`);
}

/** A propose session whose only proposal (or one of them) is a large class with one changed line. */
function seedLarge(fake: FakeBackend, path: string, name: string, count: number, withSmall: boolean): void {
    if (withSmall) {
        seedChanges(fake);
    }
    const s = fake.sessions[0].session;
    s.stage = "propose";
    s.target = "DEMO";
    s.request_cap = 1000;
    const origin = lines(count, "line");
    const proposal = origin.slice();
    proposal[1499] = "    rv_amount = round( val = iv_amount dec = lv_decimals ). \" <b>bold</b>";
    fake.dataOf(s.id)!.files.push({
        path, state: "modified", object_type: "CLAS", object_name: name, origin_source: origin.join("\n"), proposed_source: "", origin_version: "00007"
    });
    fake.addRevision(s.id, path, proposal.join("\n"));
}

function bigDiff(path: string): HTMLElement | null {
    return win().document.querySelector<HTMLElement>(`.ideUnified[data-path='${path}']`);
}

opaTest("M2: a 3,000-line class with one changed line shows only its hunks, takes a line selection and is approved without a question", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=changes`, undefined, (fake) => { seedLarge(fake, BIG, "ZCL_BIG", 3000, false); });
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => !!bigDiff(BIG)?.querySelector("tr[data-line]") && !!bigDiff(BIG)?.querySelector("[tabindex='0']"),
        success: () => {
            const root = bigDiff(BIG)!;
            Opa5.assert.notOk(root.querySelector(".ideDiffTooLarge"), "it is compared, not \"too large\"");
            Opa5.assert.strictEqual(root.querySelectorAll("details").length, 0, "no expandable unchanged regions");
            Opa5.assert.strictEqual(root.querySelectorAll("tr[data-line]").length, 8, "the changed line and 3 lines around it, both sides");
            const header = root.querySelector<HTMLElement>("tr.ideDiffHunk");
            Opa5.assert.ok(header && /1497/.test(header.textContent ?? "") && /1503/.test(header.textContent ?? ""),
                `a hunk header with its line numbers: ${header?.textContent ?? "-"}`);
            Opa5.assert.strictEqual(root.querySelector("table")?.getAttribute("role"), "grid", "still a grid");
            Opa5.assert.ok(!root.querySelector("b"), "the source is escaped");
            Opa5.assert.ok((root.querySelector("tr[data-side='new'][data-line='1500']")?.textContent ?? "").includes("<b>bold</b>"), "shown as text");
        },
        errorMessage: "No hunks-only diff"
    });
    Then.waitFor({
        controlType: "sap.m.Text", ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesHunksNote-") && (c as Control).getVisible(),
        success: (list: UI5Element[]) => Opa5.assert.ok(/unchanged lines are not shown/i.test((list[0] as Text).getText(false)),
            `the card says so: ${(list[0] as Text).getText(false)}`),
        errorMessage: "The card does not say that unchanged lines are not shown"
    });
    Then.waitFor({
        controlType: "sap.m.Link", ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesShowSource-") && (c as Control).getVisible(),
        success: () => Opa5.assert.ok(true, "\"Show the source\" stays for the full text"),
        errorMessage: "No Show the source"
    });
    When.waitFor({
        id: "changesView", ...SOPTS,
        success: () => {
            const row = bigDiff(BIG)!.querySelector<HTMLElement>("tr[data-side='new'][data-line='1500']")!;
            row.focus();
            key(row, " ");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Button", ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesCommentButton-") && (c as Button).getVisible(),
        success: (list: UI5Element[]) => {
            Opa5.assert.strictEqual((list[0] as Button).getText(), "Comment on line 1500", "a line comment anchors on the proposal's line number");
            Opa5.assert.strictEqual(bigDiff(BIG)!.querySelector("tr[data-line='1500'][data-side='new']")?.getAttribute("aria-selected"), "true");
        },
        errorMessage: "Line 1500 was not selected"
    });
    thePrimaryActionIs(Then, APPROVE, true, "Approve is on");
    iPress(When, "primaryAction");
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => approveBodies().length === 1,
        success: () => {
            Opa5.assert.deepEqual(approveBodies()[0], { revisions: { [BIG]: 1 } }, "approved without a question: its diff was shown");
            Opa5.assert.strictEqual(backend.sessions[0].session.stage, "review");
        },
        errorMessage: "No approve"
    });
    Then.iStopTheApp();
});

function theConfirmation(Then: Common, check: (dialog: Dialog) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        check: (list: UI5Element[]) => list.some((d) => (d as Dialog).getTitle() === "Approve Without Comparison?"),
        success: (list: UI5Element[]) => check(list.find((d) => (d as Dialog).getTitle() === "Approve Without Comparison?") as Dialog),
        errorMessage: "No confirmation"
    });
}

opaTest("M2: an object that cannot be compared needs a confirmation that names it; Enter cancels; only the confirmed answer approves", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=changes`, undefined, (fake) => { seedLarge(fake, HUGE, "ZCL_HUGE", 21000, true); });
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => !!bigDiff(HUGE)?.querySelector(".ideDiffTooLarge") && !!bigDiff(X)?.querySelector("tr[data-line]"),
        success: () => Opa5.assert.ok(/ZCL_HUGE was not compared here/.test(bigDiff(HUGE)!.textContent ?? ""),
            `the card names it: ${bigDiff(HUGE)!.textContent ?? ""}`),
        errorMessage: "No not-compared card"
    });
    thePrimaryActionIs(Then, APPROVE, true, "Approve is on");
    iPress(When, "primaryAction");
    theConfirmation(Then, (dialog) => {
        const text = dialog.getDomRef()?.textContent ?? "";
        Opa5.assert.ok(text.includes("ZCL_HUGE was not compared here. Review it in ADT before approving."), `it names the object: ${text}`);
        Opa5.assert.notOk(text.includes("ZCL_X"), "and only the objects not compared");
        const buttons = dialog.getButtons() as Button[];
        Opa5.assert.notOk(buttons.some((b) => b.getType() === "Emphasized"), "no emphasized (default) action");
        Opa5.assert.strictEqual(active(), buttons.find((b) => b.getText() === "Cancel")!.getFocusDomRef(), "the focus starts on Cancel");
        Opa5.assert.strictEqual(posts(`POST sessions/${SID}/approve`), 0, "nothing sent yet");
    });
    When.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        check: () => !!active(),
        success: () => key(active()!, "Enter")
    });
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => !win().document.querySelector(".sapMMessageBox"),
        success: () => Opa5.assert.ok(true, "Enter closed it")
    });
    waitMs(Then, 300);
    Then.waitFor({
        id: "changesView", ...SOPTS,
        success: () => Opa5.assert.strictEqual(posts(`POST sessions/${SID}/approve`), 0, "Enter cancelled: nothing approved")
    });
    iPress(When, "primaryAction");
    theConfirmation(Then, () => Opa5.assert.ok(true, "asked again"));
    When.waitFor({
        controlType: "sap.m.Button", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Approve" }),
        actions: new Press(),
        errorMessage: "No Approve in the confirmation"
    });
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => approveBodies().length === 1,
        success: () => Opa5.assert.deepEqual(approveBodies()[0], { revisions: { [X]: 1, [HUGE]: 1 } }, "the confirmed answer approves what is listed"),
        errorMessage: "No approve after the confirmation"
    });
    Then.iStopTheApp();
});

// --- m1: an older document version ----------------------------------------------------------

opaTest("m1: with an older version of the stage's document open, approve is off and says which version to open", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=document&kind=design&version=1`, undefined, (fake) => { designSession(fake, 2); });
    Then.waitFor({ id: "artifactTitle", ...SOPTS, matchers: new PropertyStrictEquals({ name: "text", value: "Design v1" }) });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", false, "off while v1 is shown");
    theReasonIs(Then, "You are looking at v1 — open v2 to approve", "and says which version to open");
    iFirePrimary(When);
    waitMs(Then, 300);
    Then.waitFor({
        id: "artifactTitle", ...SOPTS,
        success: () => Opa5.assert.strictEqual(posts(`POST sessions/${SID}/approve`), 0, "a press approves nothing")
    });
    When.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${SID}?view=document&kind=design&version=2`) });
    Then.waitFor({ id: "artifactTitle", ...SOPTS, matchers: new PropertyStrictEquals({ name: "text", value: "Design v2" }) });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", true, "on with v2 shown");
    Then.waitFor({ id: "primaryReason", ...SOPTS, visible: false, success: (c: UI5Element) => Opa5.assert.strictEqual((c as Text).getText(false), "", "no reason") });
    Then.iStopTheApp();
});
