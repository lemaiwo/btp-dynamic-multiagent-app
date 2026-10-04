import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type MessageStrip from "sap/m/MessageStrip";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { closeMessageBox } from "./pages/Shared";
import { SOPTS, iSend, thePrimaryActionIs } from "./pages/Session";

/**
 * Run lifecycle and approval-card guards on the session page, ported from
 * the journeys of the removed three-pane view (U14): Stop, Stop on another
 * instance, a stream without `done`, a run note as a warning, and Enter
 * never deciding a trace request.
 */
QUnit.module("Run lifecycle journey");

function stopVisible(Then: Common, visible: boolean, message: string): void {
    Then.waitFor({
        id: "stopButton",
        ...SOPTS,
        visible: false,
        check: (control: UI5Element) => (control as Control).getVisible() === visible,
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `Stop is not ${visible ? "shown" : "hidden"}`
    });
}

function theStripIs(Then: Common, pattern: RegExp, type: string, message: string): void {
    Then.waitFor({
        id: "runErrorStrip",
        ...SOPTS,
        check: (control: UI5Element) => pattern.test((control as MessageStrip).getText()),
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control as MessageStrip).getType(), type, message);
        },
        errorMessage: `No run strip matching ${String(pattern)}`
    });
}

opaTest("Stop cancels the running answer", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { release = fake.pauseStream(); });
    thePrimaryActionIs(Then, "Start design", true, "loaded");

    iSend(When, "Analyse the impact");
    stopVisible(Then, true, "Stop is offered while the assistant works");
    When.waitFor({ id: "stopButton", ...SOPTS, actions: new Press(), errorMessage: "No Stop button" });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Stopped" }),
        success: function () {
            Opa5.assert.ok(backend.requests.includes("POST sessions/s-1/cancel"), "the run was cancelled on the server");
            release?.();
        },
        errorMessage: "The stopped answer is not marked"
    });
    stopVisible(Then, false, "Stop is hidden again");
    Then.waitFor({
        id: "chatInput",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "editable", value: true }),
        success: function () {
            Opa5.assert.ok(true, "a new message can be written");
        }
    });

    Then.iStopTheApp();
});

opaTest("Stop answered 409 run_on_other_instance says the run is elsewhere", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { release = fake.pauseStream(); });
    thePrimaryActionIs(Then, "Start design", true, "loaded");

    iSend(When, "Analyse the impact");
    stopVisible(Then, true, "the run is going");
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
        check: (texts: UI5Element[]) => texts.some((t) => /another server instance; it will stop or time out/
            .test(String(t.getProperty("text")))),
        success: function () {
            Opa5.assert.ok(true, "the 409 is explained");
        },
        errorMessage: "The 409 is not explained"
    });
    closeMessageBox(When);
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(true, "the message box is closed");
            release?.();
        }
    });

    Then.iStopTheApp();
});

opaTest("a stream that ends without done shows an error strip in the conversation", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { fake.omitDone = true; });
    thePrimaryActionIs(Then, "Start design", true, "loaded");

    iSend(When, "Hello");
    theStripIs(Then, /connection ended/, "Error", "the incomplete stream is explained as an error");

    Then.iStopTheApp();
});

opaTest("a run note (conventions unavailable) is a warning, not an error", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.errorFrame = { message: "The conventions could not be read.", code: "conventions_unavailable" };
    });
    thePrimaryActionIs(Then, "Start design", true, "loaded");

    iSend(When, "Hello");
    theStripIs(Then, /./, "Warning", "the run went on; the note is a warning");

    Then.iStopTheApp();
});

/** A full Enter key stroke on `dom`, as the browser sends it; `ctrl` for Ctrl+Enter. */
function pressEnter(dom: Element | null | undefined, ctrl = false): void {
    for (const type of ["keydown", "keypress", "keyup"]) {
        dom?.dispatchEvent(new KeyboardEvent(type, {
            key: "Enter", code: "Enter", keyCode: 13, which: 13, ctrlKey: ctrl, bubbles: true, cancelable: true
        } as KeyboardEventInit));
    }
}

opaTest("Enter never decides a trace request: not from the chat input, not on the card, not with Ctrl", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
        fake.addApproval(sid);
    });
    Then.waitFor({
        success: function () {
            Opa5.getHashChanger().setHash(`sessions/${sid}`);
        }
    });
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...SOPTS,
        matchers: (panel: UI5Element) => (panel as Control).hasStyleClass("ideApprovalCard") && (panel as Control).getVisible(),
        success: function (cards: UI5Element[]) {
            const card = cards[0];
            const doc = Opa5.getWindow().document;
            const input = doc.querySelector("textarea[id$='chatInput-inner']");
            Opa5.assert.ok(input, "the chat input is there");
            pressEnter(input);
            pressEnter(input, true);
            pressEnter(card.getDomRef());
            pressEnter(card.getDomRef(), true);
        },
        errorMessage: "No approval card"
    });
    When.waitFor({ id: "chatInput", ...SOPTS, actions: new EnterText({ text: "Is it still slow?", keepFocus: true }) });
    When.waitFor({
        id: "chatInput",
        ...SOPTS,
        success: function () {
            pressEnter(Opa5.getWindow().document.querySelector("textarea[id$='chatInput-inner']"), true);
        }
    });
    Then.waitFor({
        check: () => backend.requests.includes(`POST sessions/${sid}/messages`),
        success: function () {
            Opa5.assert.ok(true, "Ctrl+Enter sent the message, so the key strokes above were real ones");
        },
        errorMessage: "Ctrl+Enter did not send"
    });
    Then.waitFor({
        id: "chatInput",
        ...SOPTS,
        success: function () {
            const decisions = backend.requests.filter((r) => r.startsWith(`POST sessions/${sid}/approvals/`));
            Opa5.assert.strictEqual(decisions.length, 0, "no decision was sent by any Enter");
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].status, "pending", "the request is still pending");
        }
    });

    Then.iStopTheApp();
});
