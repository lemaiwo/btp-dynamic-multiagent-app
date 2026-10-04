import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type CustomListItem from "sap/m/CustomListItem";
import type Link from "sap/m/Link";
import type Popover from "sap/m/Popover";
import type Select from "sap/m/Select";
import type Text from "sap/m/Text";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, thePrimaryActionIs, theReasonIs } from "./pages/Session";
import { announced, closeMessageBox, recordAnnouncements } from "./pages/Shared";
import {
    X, block, closePopover, iPressKeyOn, posts, seedChanges, seedDoc, switchRevision,
    theBlocksAreShown, theDiffIsShown, thePopoverDraftIs, win, withPage
} from "./ReviewFollowupsJourney";
import type { Page } from "./ReviewFollowupsJourney";
import type FakeBackend from "./FakeBackend";
import type { DiagnoseFinding } from "../../service/types";

/**
 * Review follow-ups 2 (after U11): the key buffer while a comment popover
 * opens (A1, A2), picks and approve on the changes view (A3, B2, B3), folds
 * (B4 in the unit tests), the source view's proposal rule (B1), and the
 * diagnose page (C1-C3).
 */
QUnit.module("Review follow-ups 2");

type Loader = (options: unknown) => Promise<unknown>;
type Page2 = Page & { loadFragment: Loader; onArtifactStored(kind: string, version: number): Promise<void> };

function page2(fn: (page: Page2) => void): (page: Page) => void {
    return (page) => fn(page as Page2);
}

/** What happened to a key: whether its default was prevented and whether it bubbled up to the document. */
function sendKey(el: Element, key: string, init: KeyboardEventInit = {}): { prevented: boolean; reached: boolean } {
    const w = win() as unknown as { KeyboardEvent: typeof KeyboardEvent };
    let reached = false;
    const seen = (): void => { reached = true; };
    const docu = win().document;
    docu.addEventListener("keydown", seen);
    const event = new w.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
    el.dispatchEvent(event);
    docu.removeEventListener("keydown", seen);
    return { prevented: event.defaultPrevented, reached };
}

function composer(): HTMLTextAreaElement | null {
    return win().document.querySelector<HTMLTextAreaElement>("[id$='--chatInput'] textarea");
}

/** The composer takes a key as usual: nothing prevents it or stops it on its way up. */
function theComposerTakes(key: string, message: string): void {
    const area = composer();
    Opa5.assert.ok(area, "the composer is there");
    const r = sendKey(area!, key);
    Opa5.assert.ok(!r.prevented && r.reached, `${message} (prevented ${String(r.prevented)}, reached the document ${String(r.reached)})`);
}

/** The next comment popover load waits until the returned function runs (then loads the real fragment). */
function holdPopoverLoad(page: Page2): () => void {
    const real = page.loadFragment.bind(page) as Loader;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    page.loadFragment = (options: unknown) => gate.then(() => real(options));
    return () => release();
}

/** The comment popover loads for real, then `patch` changes it (openBy throwing, an open that does nothing). */
function patchPopover(page: Page2, patch: (popover: Popover) => void): void {
    const real = page.loadFragment.bind(page) as Loader;
    page.loadFragment = (options: unknown) => real(options).then((p) => {
        patch(p as Popover);
        return p;
    });
}

// --- A1 / A2: the key buffer while the popover opens ------------------------------

opaTest("A1: openBy throws: the buffer ends at once, the composer takes keys and the error is shown", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    withPage(When, page2((page) => {
        patchPopover(page, (p) => {
            p.openBy = () => { throw new Error("The popover could not be opened"); };
        });
    }));
    iPressKeyOn(When, () => block(0), "ENTER");
    closeMessageBox(When);
    Then.waitFor({
        success: function () {
            theComposerTakes("x", "a key typed in the composer is not taken into the draft");
            const r = sendKey(block(1)!, "y");
            Opa5.assert.notOk(r.prevented, "nor a key on a block");
        }
    });
    Then.iStopTheApp();
});

opaTest("A1: an open that never happens ends the buffer at once", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    let opened = false;
    withPage(When, page2((page) => {
        patchPopover(page, (p) => {
            p.openBy = () => { opened = true; return p; };
        });
    }));
    iPressKeyOn(When, () => block(0), "ENTER");
    Then.waitFor({
        check: () => opened,
        success: function () {
            theComposerTakes("x", "the composer takes keys");
            Opa5.assert.notOk(sendKey(block(0)!, "y").prevented, "and the block's keys are its own again");
        },
        errorMessage: "The popover was never loaded"
    });
    Then.iStopTheApp();
});

opaTest("A1 + A2: only keys on the invoking block are buffered (AltGr too); Dead/Process end the buffer", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    withPage(When, page2((page) => { release = holdPopoverLoad(page); }));
    iPressKeyOn(When, () => block(0), "ENTER", false, (el) => {
        theComposerTakes("x", "a key on another control during the window is not swallowed");
        Opa5.assert.notOk(sendKey(block(2)!, "q").prevented, "nor a key on another block");
        Opa5.assert.ok(sendKey(el, "y").prevented, "a key on the invoking block is buffered");
        Opa5.assert.ok(sendKey(el, "@", { ctrlKey: true, altKey: true }).prevented, "an AltGr character (Ctrl+Alt) is buffered");
        Opa5.assert.notOk(sendKey(el, "s", { ctrlKey: true }).prevented, "a Ctrl shortcut is not");
        release();
    });
    thePopoverDraftIs(Then, "y@", "the draft holds the block's keys only, the AltGr character included");
    closePopover(When);
    withPage(When, page2((page) => { release = holdPopoverLoad(page); }));
    iPressKeyOn(When, () => block(1), "ENTER", false, (el) => {
        Opa5.assert.notOk(sendKey(el, "Dead").prevented, "a dead key passes");
        Opa5.assert.notOk(sendKey(el, "z").prevented, "and ends the buffering: the next key is not taken");
        release();
    });
    closePopover(When);
    withPage(When, page2((page) => { release = holdPopoverLoad(page); }));
    iPressKeyOn(When, () => block(1), "ENTER", false, (el) => {
        Opa5.assert.notOk(sendKey(el, "Process").prevented, "an IME key passes");
        Opa5.assert.notOk(sendKey(el, "z").prevented, "and ends the buffering");
        release();
    });
    closePopover(When);
    Then.iStopTheApp();
});

opaTest("A1: the buffer stops on its own after a while, even if the popover never opens", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    let pressedAt = 0;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    withPage(When, page2((page) => { release = holdPopoverLoad(page); }));
    iPressKeyOn(When, () => block(0), "ENTER", false, (el) => {
        pressedAt = Date.now();
        Opa5.assert.ok(sendKey(el, "a").prevented, "buffered at first");
    });
    Then.waitFor({
        check: () => Date.now() - pressedAt > 1700,
        success: function () {
            Opa5.assert.notOk(sendKey(block(0)!, "b").prevented, "after the hard stop the block's keys pass");
            theComposerTakes("x", "and so do the composer's");
            release();
        }
    });
    closePopover(When);
    Then.iStopTheApp();
});

// --- A3 / B2 / B3: picks and approve on the changes view ------------------------------

opaTest("A3: a revision that cannot be read leaves no pick behind; picks do not hold Approve back on another view", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seedChanges(fake, 2); });
    theDiffIsShown(Then, 2);
    When.waitFor({ success: () => { backend.failNext = { path: "sessions/s-1/file", status: 500, body: { detail: "SAP did not answer" } }; } });
    switchRevision(When, "1");
    closeMessageBox(When);
    thePrimaryActionIs(Then, "Approve changes and review", true, "the failed read did not count as a pick of revision 1");
    When.waitFor({
        controlType: "sap.m.Select",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesRevision-"),
        success: (list: UI5Element[]) => Opa5.assert.strictEqual((list[0] as Select).getSelectedKey(), "2", "the picker shows the revision on screen"),
        errorMessage: "No revision select"
    });
    switchRevision(When, "1");
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, "Approve changes and review", false, "a real pick of revision 1 holds Approve back");
    When.waitFor({ success: () => HashChanger.getInstance().setHash("sessions/s-1?view=source&path=" + encodeURIComponent(X)) });
    thePrimaryActionIs(Then, "Review changes", true, "on another view the pick does not disable the action");
    Then.waitFor({
        id: "primaryReason",
        ...SOPTS,
        visible: false,
        success: (c: UI5Element) => Opa5.assert.strictEqual((c as Text).getText(true), "", "and no older-revision reason is shown")
    });
    Then.iStopTheApp();
});

opaTest("B2: while a card could not be loaded, Approve is off with a reason; after Retry it is on", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seedChanges(fake);
        fake.failNext = { path: "sessions/s-1/file", status: 500, body: { detail: "SAP did not answer" } };
    });
    Then.waitFor({
        controlType: "sap.m.MessageStrip",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesFailedText-") && (c as Control).getVisible(),
        success: () => Opa5.assert.ok(true, "the card failed"),
        errorMessage: "No failed card"
    });
    thePrimaryActionIs(Then, "Approve changes and review", false, "Approve is off while a card is missing");
    theReasonIs(Then, "An object could not be loaded — retry it first", "and says why");
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesRetry-") && (c as Control).getVisible(),
        actions: new Press(),
        errorMessage: "No Retry"
    });
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, "Approve changes and review", true, "after Retry Approve is on");
    Then.waitFor({
        success: () => Opa5.assert.strictEqual(posts("POST sessions/s-1/approve"), 0, "nothing was approved meanwhile")
    });
    Then.iStopTheApp();
});

opaTest("B3: Approve stays busy until the session reload and the card sync are done", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seedChanges(fake); });
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, "Approve changes and review", true, "ready to approve");
    When.waitFor({ success: () => { release = backend.hold("GET sessions/s-1"); } });
    iPress(When, "primaryAction");
    Then.waitFor({
        id: "primaryAction",
        ...SOPTS,
        // A busy control is not "interactable": found without that filter.
        visible: false,
        check: () => backend.responses.includes("POST sessions/s-1/approve"),
        success: function (c: UI5Element) {
            Opa5.assert.ok((c as Button).getBusy(), "busy while the session is read again");
            release();
        },
        errorMessage: "The approve was not answered"
    });
    Then.waitFor({
        id: "primaryAction",
        ...SOPTS,
        visible: false,
        check: (c: UI5Element) => !(c as Button).getBusy() && backend.responses.includes("GET sessions/s-1"),
        success: () => Opa5.assert.ok(true, "no longer busy once the reload is done"),
        errorMessage: "The action stayed busy"
    });
    Then.iStopTheApp();
});

// --- B1: the source view's proposal rule -------------------------------------------------

opaTest("B1: the source view shows a proposal only for a file in state modified/new", function (Given: Common, When: Common, Then: Common) {
    const path = "src/CLAS/zcl_read.clas.abap";
    Given.iStartTheApp(`sessions/s-1?view=source&path=${encodeURIComponent(path)}`, undefined, (fake) => {
        const s = fake.sessions[0].session;
        s.target = "DEMO";
        const data = fake.dataOf(s.id)!;
        // A dropped proposal: equal to SAP again, state read, its revisions kept.
        data.files.push({ path, state: "read", object_type: "CLAS", object_name: "ZCL_READ", origin_source: "* sap line 1", proposed_source: "" });
        fake.addRevision(s.id, path, "* dropped proposal line 1");
        data.files.find((f) => f.path === path)!.state = "read";
    });
    Then.waitFor({
        id: "sourceView",
        ...SOPTS,
        check: () => (win().document.querySelector("[id$='--sourceView']")?.textContent ?? "").includes("sap line 1"),
        success: function () {
            Opa5.assert.notOk((win().document.querySelector("[id$='--sourceView']")?.textContent ?? "").includes("dropped proposal"),
                "the SAP source, not the dropped proposal");
        },
        errorMessage: "The SAP source is not shown"
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "SAP source" }),
        success: () => Opa5.assert.ok(true, "the meta line says SAP source"),
        errorMessage: "The meta line does not say SAP source"
    });
    Then.iStopTheApp();
});

// --- C1-C3: the diagnose page ---------------------------------------------------------------

const SID = "s-2";
const NOT_AVAILABLE = "The target system is not flagged as non-production, so this is not available there. Ask an administrator to check the target's conventions.";
const POOL = "ZCL_ORDER_QUERY".padEnd(30, "=") + "CP";
const DUMP: DiagnoseFinding = {
    id: "f-seed", kind: "dump", ref_id: "DUMP-1", title: "TSV_TNEW_PAGE_ALLOC_FAILED", program: POOL, include: null, line: 12,
    occurred_at: "2026-10-03T13:52:00", created_at: "2026-10-03T13:53:00"
};

function diagnose(fake: FakeBackend): void {
    fake.allowDiagnose("DEMO");
    const s = fake.addSession("Why is the order list slow?", [], [], "diagnose");
    s.target = "DEMO";
    s.request_cap = 1000;
    fake.dataOf(s.id)!.findings.push({ ...DUMP });
}

function hash(): string {
    return HashChanger.getInstance().getHash();
}

opaTest("C1: a report stored by a watched run is offered, not opened", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => { diagnose(fake); });
    Then.waitFor({
        id: "findingsList",
        ...SOPTS,
        success: () => recordAnnouncements(),
        errorMessage: "No findings list"
    });
    withPage(When, page2((page) => {
        backend.addArtifact(SID, "report", "# Diagnose report v1");
        void page.onArtifactStored("report", 1);
    }));
    Then.waitFor({
        id: "diagnoseReportLink",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Report v1" }),
        check: () => announced.includes("Report v1 is available."),
        success: function () {
            Opa5.assert.ok(/view=findings/.test(hash()), "the findings stay on screen");
        },
        errorMessage: `Not offered: ${announced.join(" | ")}`
    });
    When.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${SID}?view=document&kind=report&version=1`) });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Report v1" }),
        success: () => Opa5.assert.ok(true, "report v1 is read")
    });
    withPage(When, page2((page) => {
        backend.addArtifact(SID, "report", "# Diagnose report v2");
        void page.onArtifactStored("report", 2);
    }));
    Then.waitFor({
        id: "docNewer",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "v2 is available" }),
        success: function (c: UI5Element) {
            Opa5.assert.ok((c as Link).getVisible(), "the newer version is a link");
            Opa5.assert.ok(/version=1/.test(hash()), "the version being read stays open");
        },
        errorMessage: "No link to v2"
    });
    Then.iStopTheApp();
});

opaTest("C3: the F2 hint of the findings list is visible text", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => { diagnose(fake); });
    Then.waitFor({
        id: "findingsKeyHint",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Enter opens the source at the line. F2 moves to Details and Open in ADT." }),
        success: function (c: UI5Element) {
            Opa5.assert.ok(c.isA("sap.m.Text"), "the hint is visible text");
            const list = win().document.querySelector("[id$='--findingsList'] [role='list'], [id$='--findingsList'] ul");
            Opa5.assert.ok((list?.getAttribute("aria-labelledby") ?? "").split(" ").includes(c.getId()), "and still names the list");
        },
        errorMessage: "No visible F2 hint"
    });
    Then.iStopTheApp();
});

opaTest("C2: with the flag lost a finding row does not open and says why, as Details", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => {
        diagnose(fake);
        fake.conventions.forEach((c) => { c.non_production = false; });
    });
    Then.waitFor({
        id: "findingsLostReason",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: NOT_AVAILABLE }),
        success: function (c: UI5Element) {
            const details = win().document.querySelector("button[id*='--findingDetails-']");
            Opa5.assert.ok((details?.getAttribute("aria-describedby") ?? "").split(" ").includes(c.getId()), "Details is described by the reason");
        },
        errorMessage: "No visible reason in the findings view"
    });
    When.waitFor({
        controlType: "sap.m.CustomListItem",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getBindingContext("s")?.getPath() === "/artifact/findings/items/0",
        success: function (list: UI5Element[]) {
            Opa5.assert.strictEqual((list[0] as CustomListItem).getType(), "Inactive", "the row is not an open action");
            (list[0] as CustomListItem).firePress();
        },
        errorMessage: "No findings row"
    });
    Then.waitFor({
        success: function () {
            Opa5.assert.notOk(backend.requests.some((r) => r.endsWith("/findings/f-seed/open")), "no open request");
            Opa5.assert.notOk(/view=source/.test(hash()), "no source view");
        }
    });
    Then.iStopTheApp();
});

