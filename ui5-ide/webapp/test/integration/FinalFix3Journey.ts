import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, designSession, thePrimaryActionIs, theReasonIs } from "./pages/Session";
import { NEW_X, X, seedChanges, switchRevision, theDiffIsShown, win, withPage } from "./ReviewFollowupsJourney";

/**
 * Final review fixes, part 3: a revision-switch read that answers after a
 * sync replaced the card is dropped, so Approve cannot stay on "Loading the
 * latest revisions…" (1); the hold of an older document version is lifted
 * only once the newer version's text is on screen (2).
 */
QUnit.module("Final review fixes 3");

const SID = "s-1";
const APPROVE = "Approve changes and review";
const LOADING = "Loading the latest revisions…";

function waitMs(Then: Common, ms: number): void {
    let start = 0;
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false,
        check: () => { start ||= Date.now(); return Date.now() - start >= ms; },
        success: () => Opa5.assert.ok(true, `${ms} ms later`)
    });
}

function revisionKey(): string | undefined {
    return win().document.querySelector<HTMLElement>(`.ideUnified[data-path='${X}']`)?.getAttribute("data-revision") ?? undefined;
}

// --- 1: a late revision-switch read -----------------------------------------------------------

opaTest("1: a revision switch read that answers after a file event wrote a newer revision is dropped; Approve comes back", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp(`sessions/${SID}?view=changes`, undefined, (fake) => { seedChanges(fake, 2); });
    theDiffIsShown(Then, 2);
    thePrimaryActionIs(Then, APPROVE, true, "approve is on with the latest revision shown");
    // The switch to revision 1 leaves, and is held.
    When.waitFor({ success: () => { release = backend.hold(`GET sessions/${SID}/file`); } });
    switchRevision(When, "1");
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => backend.requests.filter((k) => k === `GET sessions/${SID}/file`).length >= 2,
        success: () => Opa5.assert.ok(true, "the revision switch read is in flight")
    });
    // A `file` event writes revision 3 meanwhile; the cards catch up with it.
    withPage(When, (page) => {
        backend.addRevision(SID, X, `${NEW_X}\n" revision 3`);
        void page.reloadDetail().then(() => page.syncChanges());
    });
    theDiffIsShown(Then, 3);
    When.waitFor({ success: () => release() });
    Then.waitFor({
        id: "changesView", ...SOPTS,
        check: () => backend.responses.filter((k) => k === `GET sessions/${SID}/file`).length >= 3,
        success: () => Opa5.assert.ok(true, "the held read answered")
    });
    waitMs(Then, 300);
    Then.waitFor({
        id: "changesView", ...SOPTS,
        success: () => Opa5.assert.strictEqual(revisionKey(), "3", "the card still shows revision 3: the late answer was dropped")
    });
    // After the settle window Approve is back, with no "Loading the latest revisions…" left.
    thePrimaryActionIs(Then, APPROVE, true, "approve comes back after the settle window");
    Then.waitFor({
        id: "primaryReason", ...SOPTS, visible: false,
        success: (c: UI5Element) => Opa5.assert.notStrictEqual(c.getProperty("text"), LOADING, "it no longer waits for the cards")
    });
    Then.iStopTheApp();
});

// --- 2: the older-document hold is lifted only once the newer text is on screen ---------------

const OLDER = "You are looking at v1 — open v2 to approve";
const APPROVE_DESIGN = "Approve design (v2) and plan";

function documentText(): string {
    return win().document.querySelector<HTMLElement>("[data-sap-ui$='--documentView']")?.textContent ?? "";
}

opaTest("2: from v1 to v2: approve stays held while v2 is read and is on once its text is shown", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    let v2 = "";
    Given.iStartTheApp(`sessions/${SID}?view=document&kind=design&version=1`, undefined, (fake) => {
        designSession(fake, 2);
        v2 = fake.dataOf(SID)!.artifacts.find((a) => a.kind === "design" && a.version === 2)!.id;
    });
    thePrimaryActionIs(Then, APPROVE_DESIGN, false, "held while v1 is shown");
    theReasonIs(Then, OLDER, "and says so");
    When.waitFor({
        success: () => {
            release = backend.hold(`GET sessions/${SID}/artifacts/${v2}`);
            HashChanger.getInstance().setHash(`sessions/${SID}?view=document&kind=design&version=2`);
        }
    });
    Then.waitFor({
        id: "artifactTitle", ...SOPTS,
        check: () => backend.requests.includes(`GET sessions/${SID}/artifacts/${v2}`),
        success: () => Opa5.assert.ok(true, "the v2 read is in flight")
    });
    waitMs(Then, 200);
    thePrimaryActionIs(Then, APPROVE_DESIGN, false, "still held: the v2 text is not on screen yet");
    theReasonIs(Then, OLDER, "with the same reason");
    When.waitFor({ success: () => release() });
    Then.waitFor({
        id: "artifactTitle", ...SOPTS,
        check: () => /Design v2/.test(documentText()),
        success: () => Opa5.assert.ok(true, "the v2 text is shown"),
        errorMessage: "The v2 text is not shown"
    });
    thePrimaryActionIs(Then, APPROVE_DESIGN, true, "and approve is on");
    Then.iStopTheApp();
});

opaTest("2: from v1 to v2 with a failing read: the hold stays and the error is shown", function (Given: Common, When: Common, Then: Common) {
    let v2 = "";
    Given.iStartTheApp(`sessions/${SID}?view=document&kind=design&version=1`, undefined, (fake) => {
        designSession(fake, 2);
        v2 = fake.dataOf(SID)!.artifacts.find((a) => a.kind === "design" && a.version === 2)!.id;
    });
    thePrimaryActionIs(Then, APPROVE_DESIGN, false, "held while v1 is shown");
    When.waitFor({
        success: () => {
            backend.failNext = { path: `sessions/${SID}/artifacts/${v2}`, status: 500, body: { detail: "Internal error." } };
            HashChanger.getInstance().setHash(`sessions/${SID}?view=document&kind=design&version=2`);
        }
    });
    Then.waitFor({
        id: "artifactTitle", ...SOPTS, autoWait: false,
        check: () => !!win().document.querySelector(".sapMMessageBox"),
        success: () => Opa5.assert.ok(true, "the load error is shown"),
        errorMessage: "No load error"
    });
    Then.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false, autoWait: false,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: APPROVE_DESIGN }),
            new PropertyStrictEquals({ name: "enabled", value: false })
        ],
        success: () => Opa5.assert.ok(true, "approve stays held: v2 was never shown")
    });
    Then.waitFor({
        id: "primaryReason", ...SOPTS, visible: false, autoWait: false,
        matchers: new PropertyStrictEquals({ name: "text", value: OLDER }),
        success: () => Opa5.assert.ok(true, "and still says to open v2")
    });
    Then.iStopTheApp();
});
