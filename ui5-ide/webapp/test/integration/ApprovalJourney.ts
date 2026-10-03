import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import ElementRegistry from "sap/ui/core/ElementRegistry";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Panel from "sap/m/Panel";
import type ObjectStatus from "sap/m/ObjectStatus";
import type ObjectAttribute from "sap/m/ObjectAttribute";
import type Text from "sap/m/Text";
import type Dialog from "sap/m/Dialog";
import type List from "sap/m/List";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { OPTS, announced, answerShown, closeMessageBox, pressSession, recordAnnouncements, send } from "./pages/Assistant";
import type FakeBackend from "./FakeBackend";
import { DEFAULT_TRACE_PARAMS } from "./FakeBackend";

QUnit.module("Approval journey (trace approvals, variant B)");

let sid = "";
const decideKey = (): string => `POST sessions/${sid}/approvals/`;
const decisions = (): string[] => backend.requests.filter((r) => r.startsWith(decideKey()));

/** A diagnose session on a non-production target (newest, so selected). */
function diagnose(extra?: (fake: FakeBackend) => void) {
    return (fake: FakeBackend): void => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
        extra?.(fake);
    };
}

/** Inside a row of the approvals list (the cards are clones of one template). */
export function inApprovals(control: UI5Element): boolean {
    return (control.getBindingContext("ide")?.getPath() ?? "").startsWith("/approvalRows/");
}

/** The buttons of the pending cards, as rendered: [text, type, enabled]. */
export function cardButtons(): [string, string, boolean][] {
    return Array.from(document.querySelectorAll(".ideApprovalCard button")).map((dom) => {
        const button = ElementRegistry.get(dom.id) as Button;
        return [button.getText(), button.getType(), button.getEnabled()];
    });
}

/** Waits for the pending card and hands its panel to `success`. */
export function cardShown(Then: Common, success: (panel: Panel) => void, message = "No approval card"): void {
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...OPTS,
        matchers: [inApprovals, new PropertyStrictEquals({ name: "visible", value: true })],
        success: function (panels: UI5Element[]) {
            Opa5.assert.strictEqual(panels.length, 1, "one pending card");
            success(panels[0] as Panel);
        },
        errorMessage: message
    });
}

export function pressCard(When: Common, text: string): void {
    When.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        matchers: [inApprovals, new PropertyStrictEquals({ name: "text", value: text })],
        actions: new Press(),
        errorMessage: `The approval card has no '${text}' button`
    });
}

/** Waits for the decided line matching `pattern`; the card with its buttons must be gone. */
export function decidedLine(Then: Common, pattern: RegExp, state: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...OPTS,
        matchers: [inApprovals, (control: UI5Element) => pattern.test((control as ObjectStatus).getText())],
        success: function (lines: UI5Element[]) {
            const line = lines[0] as ObjectStatus;
            Opa5.assert.strictEqual(line.getState(), state, `the line reads as ${state}`);
            Opa5.assert.ok(line.getIcon(), "with an icon, so its meaning does not rest on colour");
            Opa5.assert.deepEqual(cardButtons(), [], "a decided approval is read-only: no Approve, no Reject");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `No decided line matching ${String(pattern)}`
    });
}

/** Waits for a decided line matching `pattern` next to a card that is still pending. */
function decidedLine2(Then: Common, pattern: RegExp, message: string): void {
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...OPTS,
        matchers: [inApprovals, (control: UI5Element) => pattern.test((control as ObjectStatus).getText())],
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `No decided line matching ${String(pattern)}`
    });
}

function proposeTrace(When: Common, Then: Common): void {
    send(When, "Trace the slow order call");
    answerShown(Then, 2, "investigate", "the run that proposed the trace finished");
}

opaTest("a proposed trace shows a card with exactly what would be armed; Approve arms it and the card collapses", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => fake.scriptRun({ approval: {} })));
    Then.waitFor({ id: "chatInput", ...OPTS, success: recordAnnouncements });
    proposeTrace(When, Then);
    cardShown(Then, (panel) => {
        Opa5.assert.strictEqual(panel.getHeaderText(), "Arm a profiler trace on dev-system?", "the card names the action and the system");
        const attributes = panel.findAggregatedObjects(true, (c) => c.isA("sap.m.ObjectAttribute")) as ObjectAttribute[];
        Opa5.assert.deepEqual(attributes.map((a) => `${a.getTitle()}: ${a.getText()}`), [
            "Process type: HTTP request",
            "Object type: URL",
            "Maximum executions: 1",
            "Trace expires after: 1 h",
            "SQL trace: Off",
            "Aggregated measurement: On",
            "Description: Trace the slow order call",
            "Traced user: Your own SAP user"
        ], "every parameter that will be armed is listed");
        const texts = (panel.findAggregatedObjects(true, (c) => c.isA("sap.m.Text")) as Text[]).map((x) => x.getText(false));
        Opa5.assert.ok(texts.includes("The trace runs for your own SAP user only and expires automatically."), "it says for whom");
        Opa5.assert.deepEqual(cardButtons(), [["Approve trace", "Accept", true], ["Reject", "Reject", true]],
            "Approve and Reject are two distinct buttons");
        Opa5.assert.notOk(document.activeElement?.closest(".ideApprovalCard"), "the focus is not put on the card: Approve is never the default");
        Opa5.assert.ok(announced.some((x) => x.startsWith("Approval needed: Arm a profiler trace on dev-system?")),
            `the card is announced: ${announced.join(" | ")}`);
        Opa5.assert.strictEqual(decisions().length, 0, "nothing was decided by showing the card");
    });
    let messageReads = 0;
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            messageReads = backend.requests.filter((r) => r === `GET sessions/${sid}/messages`).length;
        }
    });
    pressCard(When, "Approve trace");
    decidedLine(Then, /^Trace armed: request TRC-\d+, expires .+/, "Success", "the card collapsed to the armed line");
    Then.waitFor({
        id: "chatList",
        ...OPTS,
        check: function () {
            // The focus goes back to the input once the decision is handled.
            return backend.responses.some((r) => r.startsWith(decideKey())) && !!document.activeElement?.id.includes("chatInput");
        },
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual(decisions().length, 1, "one decision was sent");
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].status, "approved");
            Opa5.assert.ok(announced.some((x) => /^Trace armed/.test(x)), "the outcome is announced");
            // The server writes no chat message for a decision: the line is the record.
            Opa5.assert.strictEqual((control as List).getItems().length, 2, "the chat is as it was: question and answer");
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === `GET sessions/${sid}/messages`).length, messageReads,
                "and the messages are not read again for a decision");
        },
        errorMessage: "The decision was not answered"
    });
    Then.iStopTheApp();
});

opaTest("Reject denies the request: nothing is armed and the card collapses", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => fake.scriptRun({ approval: {} })));
    proposeTrace(When, Then);
    cardShown(Then, () => undefined);
    pressCard(When, "Reject");
    decidedLine(Then, /^Request rejected: nothing was changed in SAP \(trace "Trace the slow order call"\)$/, "None",
        "the card collapsed to the rejected line, which says which request it was");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            const stored = backend.dataOf(sid)?.approvals[0];
            Opa5.assert.strictEqual(stored?.status, "denied", "the server row is denied");
            Opa5.assert.strictEqual(stored?.result, null, "and nothing was armed");
            Opa5.assert.strictEqual(decisions().length, 1);
        }
    });
    Then.iStopTheApp();
});

opaTest("an expired request cannot be approved: 410 turns the card into the expired line", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => fake.scriptRun({ approval: { expiresAt: "2000-01-01T00:00:00Z" } })));
    Then.waitFor({ id: "chatInput", ...OPTS, success: recordAnnouncements });
    proposeTrace(When, Then);
    cardShown(Then, () => undefined);
    pressCard(When, "Approve trace");
    decidedLine(Then, /^Request expired/, "Warning", "the card shows expired");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].result, null, "nothing was armed");
            Opa5.assert.ok(announced.some((x) => /^Request expired/.test(x)), "and it is announced");
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMMessageBoxError").length, 0, "said in the card, not as an error box");
        }
    });
    Then.iStopTheApp();
});

opaTest("a double click sends one decision; both buttons are off while it is in flight", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        const approval = fake.addApproval(sid);
        release = fake.hold(`POST sessions/${sid}/approvals/${approval.id}`);
    }));
    When.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        matchers: [inApprovals, new PropertyStrictEquals({ name: "text", value: "Approve trace" })],
        success: function (buttons: UI5Element[]) {
            const approve = buttons[0] as Button;
            const reject = (approve.getParent() as Control).findAggregatedObjects(false)
                .find((c) => c.isA("sap.m.Button") && (c as Button).getText() === "Reject") as Button;
            approve.firePress();
            approve.firePress();
            reject.firePress();
        },
        errorMessage: "The seeded approval has no card"
    });
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...OPTS,
        autoWait: false,
        matchers: inApprovals,
        check: function () {
            return decisions().length > 0 && cardButtons().every((b) => !b[2]);
        },
        success: function () {
            Opa5.assert.strictEqual(decisions().length, 1, "three presses, one request");
            Opa5.assert.deepEqual(cardButtons().map((b) => b[2]), [false, false], "Approve and Reject are disabled while the decision is in flight");
            release();
        },
        errorMessage: "The buttons were not disabled while the decision was in flight"
    });
    decidedLine(Then, /^Trace armed/, "Success", "the one decision went through");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.strictEqual(decisions().length, 1, "still one request after it answered");
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].status, "approved", "approved, not rejected by the third press");
        }
    });
    Then.iStopTheApp();
});

opaTest("a failed arming is not shown as armed: the line says why; approved without a result is 'outcome unknown'", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.addApproval(sid);
        fake.failArming = "arc1_timeout_unknown";
    }));
    pressCard(When, "Approve trace");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        success: function (dialogs: UI5Element[]) {
            const text = (dialogs[0] as Dialog).getDomRef()?.textContent ?? "";
            Opa5.assert.ok(text.includes("the trace may have been armed") && text.includes("check the active trace requests before proposing again"),
                `the failure is said in a message box: ${text}`);
        },
        errorMessage: "No message for a failed arming"
    });
    closeMessageBox(When);
    decidedLine(Then, /^Failed: SAP did not answer in time, so the trace may have been armed/, "Error", "the card collapsed to the failed line");
    // Outcome unknown: approved, but no result came back.
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            backend.failArming = undefined;
            backend.armingWithoutResult = true;
            backend.addApproval(sid, { created_at: "2026-10-03T12:00:00" });
        }
    });
    pressSession(When, "Explain the order class");
    pressSession(When, "Why is the order list slow?");
    pressCard(When, "Approve trace");
    decidedLine(Then, /^Approved, but the outcome is unknown/, "Warning", "never 'armed' without a result");
    Then.iStopTheApp();
});

opaTest("403 target_not_non_production and 409 approval_not_pending: a message, respectively a reload", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.addApproval(sid);
    }));
    cardShown(Then, () => {
        backend.conventions.forEach((c) => { c.non_production = false; });
    });
    pressCard(When, "Approve trace");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.ok((dialogs[0] as Dialog).getDomRef()?.textContent?.includes("no longer flagged as non-production"),
                "the refusal is explained");
        },
        errorMessage: "No message for target_not_non_production"
    });
    closeMessageBox(When);
    cardShown(Then, () => {
        Opa5.assert.deepEqual(cardButtons().map((b) => b[2]), [true, true], "the request is still pending and can be decided");
        // A second request: rejecting sends nothing to SAP, so it works without the flag.
        backend.addApproval(sid, { created_at: "2026-10-03T07:00:00", params: { ...DEFAULT_TRACE_PARAMS, description: "Older request" } });
    });
    pressSession(When, "Explain the order class");
    pressSession(When, "Why is the order list slow?");
    When.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        matchers: [inApprovals, new PropertyStrictEquals({ name: "text", value: "Reject" })],
        check: function (buttons: UI5Element[]) {
            return buttons.length === 2;
        },
        success: function (buttons: UI5Element[]) {
            const older = buttons.find((b) => b.getBindingContext("ide")?.getProperty("id") !== backend.dataOf(sid)?.approvals[0].id);
            (older as Button).firePress();
        },
        errorMessage: "The two pending requests are not shown as cards"
    });
    decidedLine2(Then, /^Request rejected/, "Reject is not refused on a target that lost its flag");
    cardShown(Then, () => {
        Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[1].status, "denied", "the server row is denied");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMMessageBoxError").length, 0, "without an error box");
        // Meanwhile the first one is rejected in another tab.
        backend.allowDiagnose();
        const stored = backend.dataOf(sid)?.approvals[0];
        if (stored) {
            stored.status = "denied";
            stored.decided_at = "2026-10-03T10:05:00";
        }
    });
    pressCard(When, "Approve trace");
    decidedLine(Then, /^Request rejected/, "None", "the list was reloaded and shows the decision taken elsewhere");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].result, null, "nothing was armed");
        }
    });
    Then.iStopTheApp();
});

opaTest("a request whose parameters are not valid shows no Approve: it says so and can only be rejected", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.addApproval(sid, { params: {} as never });
    }));
    cardShown(Then, (panel) => {
        Opa5.assert.deepEqual(cardButtons().map((b) => b[0]), ["Reject"], "Reject is the only button: nothing can be approved");
        const attributes = panel.findAggregatedObjects(true, (c) => c.isA("sap.m.ObjectAttribute"));
        Opa5.assert.strictEqual(attributes.length, 0, "no empty parameter list");
        const text = panel.getDomRef()?.textContent ?? "";
        Opa5.assert.ok(text.includes("This request is not valid and cannot be approved"), `the card says why: ${text}`);
        Opa5.assert.notOk(text.includes("Approving changes a runtime setting"), "and does not ask for consent");
    });
    pressCard(When, "Reject");
    decidedLine(Then, /^Request rejected: nothing was changed in SAP \(request ap-\d+\)$/, "None",
        "it can be rejected, and the line names the request");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.deepEqual(decisions().length, 1, "one decision: the deny");
            Opa5.assert.strictEqual(backend.dataOf(sid)?.approvals[0].status, "denied");
        }
    });
    Then.iStopTheApp();
});

opaTest("approvals are loaded with the session: pending as a card, decided as lines; a change session has none", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.addApproval(sid, { status: "expired", created_at: "2026-10-03T08:00:00" });
        fake.addApproval(sid, {
            status: "approved", created_at: "2026-10-03T09:00:00",
            result: { trace_request_id: "TRC-7", expires_at: "2026-10-03T10:00:00" }
        });
        fake.addApproval(sid, { action: "trace_cancel", params: { id: "TRC-7" }, created_at: "2026-10-03T09:30:00" });
    }));
    cardShown(Then, (panel) => {
        Opa5.assert.strictEqual(panel.getHeaderText(), "Cancel a trace request on dev-system?", "the pending cancel is a card");
        const attributes = panel.findAggregatedObjects(true, (c) => c.isA("sap.m.ObjectAttribute")) as ObjectAttribute[];
        Opa5.assert.deepEqual(attributes.map((a) => `${a.getTitle()}: ${a.getText()}`), ["Trace request: TRC-7"]);
        Opa5.assert.deepEqual(cardButtons().map((b) => b[0]), ["Approve cancellation", "Reject"]);
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...OPTS,
        matchers: [inApprovals, new PropertyStrictEquals({ name: "visible", value: true })],
        success: function (lines: UI5Element[]) {
            const texts = (lines as ObjectStatus[]).map((l) => l.getText());
            Opa5.assert.strictEqual(texts.length, 2, "the two decided approvals are lines");
            Opa5.assert.ok(/^Trace armed: request TRC-7, expires /.test(texts[0]), texts[0]);
            Opa5.assert.ok(/^Request expired/.test(texts[1]), texts[1]);
            Opa5.assert.strictEqual(cardButtons().length, 2, "only the pending card has buttons");
        },
        errorMessage: "The decided approvals are not listed"
    });
    pressSession(When, "Explain the order class");
    Then.waitFor({
        id: "approvalList",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function () {
            Opa5.assert.notOk(backend.requests.includes("GET sessions/s-1/approvals"), "a change session does not ask for approvals");
            Opa5.assert.strictEqual(document.querySelectorAll(".ideApprovalCard").length, 0, "and shows no card of the other session");
        },
        errorMessage: "The approvals of the diagnose session stayed on screen"
    });
    Then.iStopTheApp();
});

opaTest("the approvals list has an accessible name, and a pending request found when the session loads is announced once", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        recordAnnouncements();
        fake.addApproval(sid, { status: "denied", created_at: "2026-10-03T08:00:00" });
        fake.addApproval(sid);
    }));
    const needed = (): string[] => announced.filter((x) => x.startsWith("Approval needed:"));
    cardShown(Then, () => {
        const list = document.querySelector(".ideApprovals");
        Opa5.assert.strictEqual(list?.getAttribute("role"), "group", "the list of approvals is a group");
        const label = document.getElementById(list?.getAttribute("aria-labelledby") ?? "");
        Opa5.assert.strictEqual(label?.textContent, "Trace approvals", "named by its label");
        Opa5.assert.deepEqual(needed(), [
            "Approval needed: Arm a profiler trace on dev-system? HTTP request · max 1 execution(s) · expires in 1 h"
        ], `the pending card is announced, the decided one is not: ${announced.join(" | ")}`);
        Opa5.assert.notOk(document.activeElement?.closest(".ideApprovalCard"), "and the focus is not put on the card");
        // An approve that is refused reloads the list: the card is still the same one.
        backend.conventions.forEach((c) => { c.non_production = false; });
    });
    pressCard(When, "Approve trace");
    closeMessageBox(When);
    cardShown(Then, () => {
        Opa5.assert.ok(backend.requests.filter((r) => r === `GET sessions/${sid}/approvals`).length >= 2, "the list was reloaded");
        Opa5.assert.strictEqual(needed().length, 1, "a card that was already there is not announced again");
        backend.allowDiagnose();
    });
    Then.iStopTheApp();
});

opaTest("a proposal the server did not store shows in the activity panel as refused, with the reason", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => fake.scriptRun({ proposalRefused: "too_many_pending" })));
    proposeTrace(When, Then);
    Then.waitFor({
        id: "activityTimeline",
        ...OPTS,
        visible: false,
        autoWait: false,
        check: function (control: UI5Element) {
            return (control as List).getItems().length > 0;
        },
        success: function (control: UI5Element) {
            const row = (control as List).getItems().map((i) => i.getBindingContext("ide")?.getObject() as { refused: boolean; refusedText: string; state: string })
                .find((r) => r.refused);
            Opa5.assert.strictEqual(row?.refusedText, "not proposed (too many open requests)", "the refusal has its own label");
            Opa5.assert.strictEqual(row?.state, "Warning", "a refusal, not a failing tool");
            Opa5.assert.strictEqual(document.querySelectorAll(".ideApprovalCard").length, 0, "and there is no card: nothing was proposed");
        },
        errorMessage: "The refused proposal is not in the activity timeline"
    });
    Then.iStopTheApp();
});
