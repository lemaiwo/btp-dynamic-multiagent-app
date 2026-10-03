import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { OPTS, announced, answerShown, pressSession, recordAnnouncements } from "./pages/Assistant";
import { cardButtons, cardShown, decidedLine, pressCard } from "./ApprovalJourney";
import type FakeBackend from "./FakeBackend";

QUnit.module("Approval guard journey (final review S8)");

let sid = "";
const decisions = (): string[] => backend.requests.filter((r) => r.startsWith(`POST sessions/${sid}/approvals/`));

function diagnose(extra?: (fake: FakeBackend) => void) {
    return (fake: FakeBackend): void => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
        extra?.(fake);
    };
}

/** A full Enter key stroke on `dom`, as the browser sends it; `ctrl` for Ctrl+Enter. */
function pressEnter(dom: Element | null | undefined, ctrl = false): void {
    for (const type of ["keydown", "keypress", "keyup"]) {
        dom?.dispatchEvent(new KeyboardEvent(type, {
            key: "Enter", code: "Enter", keyCode: 13, which: 13, ctrlKey: ctrl, bubbles: true, cancelable: true
        } as KeyboardEventInit));
    }
}

opaTest("Enter never approves: not from the chat input, not on the card, not with Ctrl", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.addApproval(sid);
    }));
    cardShown(Then, (panel) => {
        const input = document.querySelector("textarea[id$='chatInput-inner']");
        Opa5.assert.ok(input, "the chat input is there");
        Opa5.assert.notOk(document.activeElement?.closest(".ideApprovalCard"), "the focus is not on the card");
        pressEnter(input);
        pressEnter(input, true);
        pressEnter(panel.getDomRef());
        pressEnter(panel.getDomRef(), true);
    });
    // The same synthetic key stroke does reach the app: with a draft, Ctrl+Enter sends the message.
    When.waitFor({ id: "chatInput", ...OPTS, actions: new EnterText({ text: "Is it still slow?" }) });
    When.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            pressEnter(document.querySelector("textarea[id$='chatInput-inner']"), true);
        }
    });
    answerShown(Then, 2, "investigate", "Ctrl+Enter sent the message, so the key strokes above were real ones");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.strictEqual(decisions().length, 0, "no decision was sent by any Enter");
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].status, "pending", "the request is still pending");
            Opa5.assert.deepEqual(cardButtons(), [["Approve trace", "Accept", true], ["Reject", "Reject", true]],
                "the card is unchanged: Approve takes a press of its own button");
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "and nothing else opened");
        }
    });
    Then.iStopTheApp();
});

opaTest("a session switch while a decision is in flight: its answer never reaches the other session, and the first one shows the server's state afterwards", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    let key = "";
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        const approval = fake.addApproval(sid);
        key = `POST sessions/${sid}/approvals/${approval.id}`;
        release = fake.hold(key);
        // The arming fails: a failure handled in the wrong session would be an error box there.
        fake.failArming = "arc1_timeout_unknown";
    }));
    Then.waitFor({ id: "chatInput", ...OPTS, success: recordAnnouncements });
    pressCard(When, "Approve trace");
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...OPTS,
        autoWait: false,
        check: function () {
            return decisions().length === 1 && cardButtons().length === 2 && cardButtons().every((b) => !b[2]);
        },
        success: function () {
            Opa5.assert.ok(true, "the decision is in flight: both buttons are off");
        },
        errorMessage: "The decision was not sent"
    });
    // autoWait is off: the card is busy while the decision is held.
    When.waitFor({
        controlType: "sap.m.ObjectListItem",
        ...OPTS,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "title", value: "Explain the order class" }),
        actions: new Press(),
        errorMessage: "No change session in the list"
    });
    Then.waitFor({
        id: "approvalList",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        check: function () {
            return backend.responses.includes("GET sessions/s-1/messages");
        },
        success: function () {
            Opa5.assert.strictEqual(document.querySelectorAll(".ideApprovalCard").length, 0, "the change session shows no card");
            release();
        },
        errorMessage: "The change session did not open"
    });
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        check: function () {
            return backend.responses.includes(key);
        },
        success: function () {
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].status, "failed", "the server decided: the arming failed");
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMMessageBoxError").length, 0,
                "the late answer opens no error box in the other session");
            Opa5.assert.strictEqual(document.querySelectorAll(".ideApprovals .sapMObjStatus").length, 0, "and no status line");
            Opa5.assert.notOk(announced.some((x) => /^Failed|^Trace armed/.test(x)), `nor an announcement: ${announced.join(" | ")}`);
        },
        errorMessage: "The held decision was not answered"
    });
    pressSession(When, "Why is the order list slow?");
    decidedLine(Then, /^Failed: SAP did not answer in time, so the trace may have been armed/, "Error",
        "back in the diagnose session the list shows what the server stored, read-only");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.strictEqual(decisions().length, 1, "one decision in all");
        }
    });
    Then.iStopTheApp();
});
