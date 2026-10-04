import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import ElementRegistry from "sap/ui/core/ElementRegistry";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { closeMessageBox } from "./pages/Shared";
import { OPTS as WOPTS, iSeeRows, theHashIs } from "./pages/Worklist";
import { SOPTS, designSession, iPress, iSend, thePrimaryActionIs } from "./pages/Session";

/**
 * U7 review fix round: focus of the artifact column, leaving the page during
 * a run, no reload or busy overlay for an artifact, stale artifact answers,
 * a session that is not there, visible reasons for disabled actions, approve
 * sends what was shown, persistent run errors, the document title.
 */
QUnit.module("Session page states journey");

const PATH = "src/CLAS/zcl_price_calc.clas.abap";

function activeElement(): Element | null {
    return Opa5.getWindow().document.activeElement;
}

function domOf(id: string): HTMLElement | null {
    return Opa5.getWindow().document.querySelector(`[id$='--${id}']`);
}

/** No message box or toast is on screen. */
function nothingShown(): boolean {
    const dialogs = ElementRegistry.filter((e) => e.isA("sap.m.Dialog") && (e as Dialog).isOpen());
    return dialogs.length === 0 && !Opa5.getWindow().document.querySelector(".sapMMessageToast");
}

/** Waits `ms` from the moment this step runs. */
function iWait(Then: Common, ms: number): void {
    let start = 0;
    Then.waitFor({
        success: function () {
            start = Date.now();
        }
    });
    Then.waitFor({
        check: function () {
            return Date.now() - start >= ms;
        },
        success: function () {
            Opa5.assert.ok(true, `waited ${ms} ms`);
        }
    });
}

function theTokenNamed(When: Common, text: string): void {
    When.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: [new PropertyStrictEquals({ name: "text", value: text }), new PropertyStrictEquals({ name: "active", value: true })],
        actions: new Press(),
        errorMessage: `No active token "${text}"`
    });
}

opaTest("the artifact column is a labelled region; opening moves the focus to its title, Close returns it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake, 2); });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", true, "the session is loaded");
    theTokenNamed(When, "Design");
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design v2" }),
        check: function (control: UI5Element) {
            return activeElement() === control.getDomRef();
        },
        success: function (control: UI5Element) {
            Opa5.assert.ok(true, "the focus is on the document's title");
            Opa5.assert.strictEqual(control.getDomRef()?.getAttribute("tabindex"), "-1", "the title takes the focus, but not a tab stop");
            const host = domOf("artifactHost");
            Opa5.assert.strictEqual(host?.getAttribute("role"), "region", "the column is a region");
            Opa5.assert.strictEqual(host?.getAttribute("aria-labelledby"), control.getId(), "named by its title");
        },
        errorMessage: "The focus did not move to the artifact title"
    });
    iPress(When, "closeArtifact");
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design" }),
        check: function (controls: UI5Element[]) {
            const dom = (controls[0] as Control).getDomRef();
            return !!dom && !!activeElement() && dom.contains(activeElement());
        },
        success: function () {
            Opa5.assert.ok(true, "Close gives the focus back to the token that opened the column");
        },
        errorMessage: "The focus did not return to the opener"
    });
    Then.iStopTheApp();
});

opaTest("a deep link to a document focuses its title; no reload of the session and no busy overlay for an artifact", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    let design = "";
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        designSession(fake, 0);
        design = fake.addArtifact("s-1", "design", "# Design one\n\nText.").id;
    });
    thePrimaryActionIs(Then, "Approve design (v1) and plan", true, "loaded once");
    Then.waitFor({
        success: function () {
            release = backend.hold(`GET sessions/s-1/artifacts/${design}`);
            HashChanger.getInstance().setHash("sessions/s-1?view=document&kind=design&version=1");
        }
    });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design v1" }),
        success: function () {
            Opa5.assert.ok(backend.requests.includes(`GET sessions/s-1/artifacts/${design}`), "the document is being read");
        }
    });
    Then.waitFor({
        id: "fcl",
        ...SOPTS,
        success: function (control: UI5Element) {
            Opa5.assert.notOk((control as Control).getBusy(), "no busy overlay over the page while a document loads");
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === "GET sessions/s-1").length, 1,
                "the session is not read again for a query change");
            release();
        }
    });
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: function (control: UI5Element) {
            return !!control.getDomRef()?.textContent?.includes("Design one");
        },
        success: function () {
            Opa5.assert.ok(true, "the document is shown");
        }
    });
    iPress(When, "closeArtifact");
    Then.waitFor({
        id: "fcl",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "layout", value: "OneColumn" }),
        success: function () {
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === "GET sessions/s-1").length, 1, "nor for closing it");
            Opa5.assert.ok(domOf("chatInput")?.contains(activeElement()), "without an opener the focus goes to the message input");
        }
    });
    Then.iStopTheApp();

    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { designSession(fake, 1); });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        check: function (control: UI5Element) {
            return activeElement() === control.getDomRef();
        },
        success: function () {
            Opa5.assert.ok(true, "a deep link puts the focus on the document title");
        },
        errorMessage: "The deep link did not focus the title"
    });
    Then.iStopTheApp();
});

opaTest("the artifact asked for last wins, whatever answers last", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        const sid = designSession(fake, 0);
        const design = fake.addArtifact(sid, "design", "# The design\n\nSlow.");
        fake.addArtifact(sid, "plan", "# The plan\n\nFast.");
        release = fake.hold(`GET sessions/${sid}/artifacts/${design.id}`);
    });
    thePrimaryActionIs(Then, "Approve design (v1) and plan", true, "loaded");
    Then.waitFor({
        success: function () {
            HashChanger.getInstance().setHash("sessions/s-1?view=document&kind=design&version=1");
        }
    });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design v1" }),
        success: function () {
            const html = String((domOf("artifactContent")?.textContent ?? "")).trim();
            Opa5.assert.strictEqual(html, "", "the column is empty until the document is there");
            HashChanger.getInstance().setHash("sessions/s-1?view=document&kind=plan&version=1");
        }
    });
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: function (control: UI5Element) {
            return !!control.getDomRef()?.textContent?.includes("The plan");
        },
        success: function () {
            release();
        },
        errorMessage: "The plan is not shown"
    });
    iWait(Then, 400);
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control as unknown as { getText(): string }).getText(), "Plan v1", "the later choice keeps its title");
            Opa5.assert.notOk(domOf("artifactContent")?.textContent?.includes("The design"), "the late design answer is dropped");
        }
    });
    Then.iStopTheApp();
});

opaTest("a session that does not exist shows why and a way back; nothing else", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/does-not-exist");
    Then.waitFor({
        id: "loadFailedStrip",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "This session does not exist or belongs to someone else." }),
        success: function () {
            Opa5.assert.ok(nothingShown(), "no message box on top of it");
        },
        errorMessage: "No not-found state"
    });
    Then.waitFor({
        id: "fcl",
        ...SOPTS,
        visible: false,
        success: function (control: UI5Element) {
            Opa5.assert.notOk((control as Control).getVisible(), "no columns");
            Opa5.assert.notOk(domOf("sessionHeaderBox")?.offsetParent, "no header");
        }
    });
    iPress(When, "loadFailedBack", "No link back to the worklist");
    theHashIs(Then, /^sessions$/, "the link leads to the worklist");
    Then.iStopTheApp();
});

opaTest("leaving the page during a run: nothing of that run appears on the worklist", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.errorFrame = { message: "Run r-1 failed.", code: "run_failed" };
    });
    thePrimaryActionIs(Then, "Start design", true, "loaded");
    Then.waitFor({
        success: function () {
            release = backend.pauseStream();
        }
    });
    iSend(When, "What does the class do?");
    Then.waitFor({ id: "stopButton", ...SOPTS, success: function () { Opa5.assert.ok(true, "running"); } });
    iPress(When, "backToWorklist");
    iSeeRows(Then, 1, "the worklist is shown");
    Then.waitFor({
        success: function () {
            release();
        }
    });
    Then.waitFor({
        check: function () {
            return backend.streamsFlushed > 0;
        },
        success: function () {
            Opa5.assert.ok(true, "the run ended with an error frame");
        }
    });
    iWait(Then, 600);
    Then.waitFor({
        id: "worklistTable",
        ...WOPTS,
        success: function () {
            Opa5.assert.ok(nothingShown(), "no message box or toast of the left session's run");
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === "GET sessions/s-1/messages").length, 1,
                "the left page did not reload its conversation");
            Opa5.assert.strictEqual(HashChanger.getInstance().getHash(), "sessions", "still on the worklist");
        }
    });
    Then.iStopTheApp();
});

opaTest("a failed run leaves a message in the conversation, not a passing toast; one announcement per answer", function (Given: Common, When: Common, Then: Common) {
    const said: string[] = [];
    let original: ((text: string, mode?: string) => void) | undefined;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        const im = InvisibleMessage.getInstance() as unknown as { announce(text: string, mode?: string): void };
        original = im.announce.bind(im);
        im.announce = (text: string, mode?: string): void => { said.push(text); original!(text, mode); };
        fake.answer = "A long answer streamed in many small parts so that a per-delta announcement would show.";
    });
    thePrimaryActionIs(Then, "Start design", true, "loaded");
    iSend(When, "What does it do?");
    thePrimaryActionIs(Then, "Start design", true, "the run is over");
    Then.waitFor({
        success: function () {
            // UI5 itself clears the live region with "" now and then; only spoken texts count.
            Opa5.assert.deepEqual(said.filter(Boolean), ["The assistant has answered."], "one polite announcement, when the answer is complete");
            backend.errorFrame = { message: "Run r-9 failed.", code: "run_failed" };
        }
    });
    iSend(When, "Again?");
    Then.waitFor({
        id: "runErrorStrip",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "The run failed. Run r-9 failed." }),
        success: function () {
            Opa5.assert.ok(true, "the failure stays in the conversation");
        },
        errorMessage: "No persistent run error"
    });
    iWait(Then, 3600);
    Then.waitFor({
        id: "runErrorStrip",
        ...SOPTS,
        success: function () {
            Opa5.assert.ok(true, "still there after a toast would have gone");
            backend.errorFrame = undefined;
        }
    });
    iSend(When, "Third try");
    Then.waitFor({
        id: "runErrorStrip",
        ...SOPTS,
        visible: false,
        check: function (control: UI5Element) {
            return !(control as Control).getVisible();
        },
        success: function () {
            Opa5.assert.ok(true, "a new message clears it");
            const im = InvisibleMessage.getInstance() as unknown as { announce(text: string, mode?: string): void };
            if (original) {
                im.announce = original;
            }
        }
    });
    Then.iStopTheApp();
});

opaTest("disabled sending says why: usage used up, session done; the tab shows the session", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake, 1, { used: 200, cap: 200 }); });
    Then.waitFor({
        id: "sendReason",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "This session has used its 200 model requests." }),
        success: function (reason: UI5Element) {
            const send = domOf("sendButton");
            Opa5.assert.ok(send?.getAttribute("aria-describedby")?.split(" ").includes(reason.getId()), "Send is described by the reason");
            Opa5.assert.strictEqual(Opa5.getWindow().document.title, "Explain the order class - ABAP Assistant", "the tab names the session");
        },
        errorMessage: "No reason for the disabled Send"
    });
    Then.waitFor({
        id: "chatInput",
        ...SOPTS,
        enabled: false,
        success: function (control: UI5Element) {
            Opa5.assert.notOk((control as unknown as { getEnabled(): boolean }).getEnabled(), "the composer is off");
        }
    });
    Then.waitFor({
        id: "requestChangesReason",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Request changes: this session has used its 200 model requests." }),
        success: function () {
            Opa5.assert.ok(true, "Request changes says why it is off");
        },
        errorMessage: "No reason for the disabled Request changes"
    });
    Then.iStopTheApp();

    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { fake.sessions[0].session.stage = "done"; });
    Then.waitFor({
        id: "sendReason",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "This session is done. Start a new session for further work." }),
        success: function () {
            Opa5.assert.ok(true, "a finished session says why it takes no message");
        },
        errorMessage: "No reason in a done session"
    });
    Then.iStopTheApp();
});

opaTest("approve refusals are worded per code; propose sends the revisions on screen", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake, 2); });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", true, "v2 is shown");
    Then.waitFor({
        success: function () {
            backend.addArtifact("s-1", "design");   // v3, written meanwhile
        }
    });
    iPress(When, "primaryAction");
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({
            name: "text", value: "A newer version was written in the meantime. Look at it, then approve again."
        }),
        success: function () {
            Opa5.assert.ok(true, "version_changed has its own sentence");
        },
        errorMessage: "No version_changed text"
    });
    closeMessageBox(When);
    thePrimaryActionIs(Then, "Approve design (v3) and plan", true, "the reloaded session names the new version");
    Then.iStopTheApp();

    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.sessions[0].session.stage = "propose";
        fake.addRevision("s-1", PATH, "CLASS zcl_price_calc DEFINITION.\nENDCLASS.");
    });
    // Approve changes only with the cards on screen: the primary action opens them first.
    thePrimaryActionIs(Then, "Review changes", true, "outside the changes view the action reviews");
    iPress(When, "primaryAction");
    thePrimaryActionIs(Then, "Approve changes and review", true, "revision 1 is shown");
    Then.waitFor({
        success: function () {
            backend.addRevision("s-1", PATH, "* revision 2");
        }
    });
    iPress(When, "primaryAction");
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({
            name: "text", value: "A newer version was written in the meantime. Look at it, then approve again."
        }),
        success: function () {
            const body = backend.bodies.filter((b) => b.key === "POST sessions/s-1/approve").pop()?.body;
            Opa5.assert.deepEqual(body, { revisions: { [PATH]: 1 } }, "approve sent the revision on screen");
        },
        errorMessage: "A revision written meanwhile was approved blindly"
    });
    closeMessageBox(When);
    iPress(When, "primaryAction");
    thePrimaryActionIs(Then, "Finish session", false, "the second approve, with the reloaded revisions, moved on");
    Then.waitFor({
        success: function () {
            const body = backend.bodies.filter((b) => b.key === "POST sessions/s-1/approve").pop()?.body;
            Opa5.assert.deepEqual(body, { revisions: { [PATH]: 2 } }, "and sent revision 2");
        }
    });
    Then.iStopTheApp();
});
