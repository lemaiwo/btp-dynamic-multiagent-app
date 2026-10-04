import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, designSession, iPress, thePrimaryActionIs } from "./pages/Session";
import { X, seedChanges, theDiffIsShown, win } from "./ReviewFollowupsJourney";

/**
 * Final review fixes, part 2: a second route match during the first load
 * applies the latest query once the session is there (m2); an approve or a
 * hand over in flight when another session opens leaves nothing behind
 * there (m3).
 */
QUnit.module("Final review fixes 2");

const SID = "s-1";

const hash = (): string => HashChanger.getInstance().getHash();

function waitMs(Then: Common, ms: number): void {
    let start = 0;
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false,
        check: () => { start ||= Date.now(); return Date.now() - start >= ms; },
        success: () => Opa5.assert.ok(true, `${ms} ms later`)
    });
}

function diffOf(path: string): HTMLElement | null {
    return win().document.querySelector<HTMLElement>(`.ideUnified[data-path='${path}']`);
}

// --- m2: a second route match during the first load ------------------------------------------

opaTest("m2: a second route match while the session loads: its query is applied once the load is done", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => {
        seedChanges(fake);
        release = fake.hold(`GET sessions/${SID}`);
    });
    Then.waitFor({
        id: "sessionPage", ...SOPTS, visible: false,
        check: () => backend.requests.includes(`GET sessions/${SID}`),
        success: () => Opa5.assert.ok(true, "the session read is held")
    });
    When.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${SID}?view=changes`) });
    waitMs(Then, 200);
    When.waitFor({ success: () => release() });
    theDiffIsShown(Then, 1);
    Then.waitFor({
        id: "changesView", ...SOPTS,
        success: (c: UI5Element) => {
            Opa5.assert.ok((c as Control).getVisible(), "the changes view is open");
            Opa5.assert.ok(/view=changes/.test(hash()), "as the hash says");
        }
    });
    waitMs(Then, 300);
    Then.waitFor({
        id: "changesView", ...SOPTS,
        success: (c: UI5Element) => Opa5.assert.ok((c as Control).getVisible() && !!diffOf(X), "and stays open: the older query was not applied after it")
    });
    Then.iStopTheApp();
});

// --- m3: approve / hand over in flight vs a session switch ------------------------------------

opaTest("m3: an approve in flight when another session opens: the other session is not busy, and nothing of the approve shows there", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    let other = "";
    Given.iStartTheApp(`sessions/${SID}?view=document&kind=design&version=1`, undefined, (fake) => {
        designSession(fake, 1);
        other = fake.addSession("Other session").id;
    });
    thePrimaryActionIs(Then, "Approve design (v1) and plan", true, "on");
    When.waitFor({ success: () => { release = backend.hold(`POST sessions/${SID}/approve`); } });
    iPress(When, "primaryAction");
    Then.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false, autoWait: false,
        check: () => backend.requests.includes(`POST sessions/${SID}/approve`),
        success: () => Opa5.assert.ok(true, "the approve is in flight")
    });
    When.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${other}`) });
    Then.waitFor({
        id: "sessionTitle", ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Other session" }),
        success: () => Opa5.assert.ok(true, "the other session is open")
    });
    Then.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false, autoWait: false,
        success: (b: UI5Element) => Opa5.assert.notOk((b as Button).getBusy(), "its primary action is not busy with the other session's approve")
    });
    When.waitFor({ success: () => release() });
    waitMs(Then, 500);
    Then.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false,
        success: (b: UI5Element) => {
            Opa5.assert.notOk((b as Button).getBusy(), "still not busy");
            Opa5.assert.notOk(/Moved to/.test(win().document.querySelector(".sapMMessageToast")?.textContent ?? ""),
                "no toast of the other session's approve");
            Opa5.assert.ok(hash().startsWith(`sessions/${other}`), "still on the other session");
        }
    });
    Then.iStopTheApp();
});

opaTest("m3: a hand over in flight when another session opens: no toast, no navigation to the new session", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    let diag = "";
    Given.iStartTheApp("", undefined, (fake) => {
        fake.allowDiagnose("dev-system");
        const s = fake.addSession("Why is the order list slow?", [], [], "diagnose");
        diag = s.id;
        fake.addArtifact(s.id, "report", "# Report v1");
    });
    When.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${diag}`) });
    thePrimaryActionIs(Then, "Hand over to a change", true, "hand over is on");
    When.waitFor({ success: () => { release = backend.hold(`POST sessions/${diag}/handover`); } });
    iPress(When, "primaryAction");
    When.waitFor({
        controlType: "sap.m.Button", searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "OK" }),
        actions: new Press()
    });
    Then.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false, autoWait: false,
        check: () => backend.requests.includes(`POST sessions/${diag}/handover`),
        success: () => Opa5.assert.ok(true, "the hand over is in flight")
    });
    When.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${SID}`) });
    Then.waitFor({
        id: "primaryAction", ...SOPTS, enabled: false, autoWait: false,
        check: (b: UI5Element) => !(b as Button).getBusy() && backend.requests.filter((k) => k === `GET sessions/${SID}`).length > 0,
        success: () => Opa5.assert.ok(true, "the change session's primary action is not busy with the hand over"),
        errorMessage: "The primary action stayed busy"
    });
    When.waitFor({ success: () => release() });
    waitMs(Then, 600);
    Then.waitFor({
        id: "sessionPage", ...SOPTS, visible: false,
        success: () => {
            Opa5.assert.ok(hash().startsWith(`sessions/${SID}`), `no navigation to the handed over session (${hash()})`);
            Opa5.assert.notOk(/started from the report/.test(win().document.querySelector(".sapMMessageToast")?.textContent ?? ""), "no toast");
        }
    });
    Then.iStopTheApp();
});
