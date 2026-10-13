import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type HBox from "sap/m/HBox";
import type ObjectStatus from "sap/m/ObjectStatus";
import type List from "sap/m/List";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { closeMessageBox } from "./pages/Shared";
import { OPTS as WOPTS, iSeeRows, theHashIs } from "./pages/Worklist";
import {
    SOPTS, designSession, iPress, iSend, longConversation, openComment, scroller, theReasonIs, thePrimaryActionIs
} from "./pages/Session";

QUnit.module("Session page journey");

const LONG_ANSWER = Array.from({ length: 40 }, (_, i) => `Line ${i + 1} of a long answer.`).join("\n\n");

function tokens(control: UI5Element): ObjectStatus[] {
    return (control as HBox).getItems().map((item) =>
        ((item as unknown as { getItems?(): UI5Element[] }).getItems?.()[0] ?? item) as ObjectStatus);
}

opaTest("a worklist row opens the session page; the timeline marks the current stage; back returns", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions");
    iSeeRows(Then, 1, "the seeded session is listed");
    When.waitFor({
        controlType: "sap.m.ColumnListItem",
        ...WOPTS,
        actions: new Press(),
        errorMessage: "No worklist row"
    });
    theHashIs(Then, /^sessions\/s-1$/, "the row opened its session route");
    Then.waitFor({
        id: "sessionTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Explain the order class" }),
        success: function () {
            Opa5.assert.ok(true, "the page shows the session title");
        },
        errorMessage: "The session page did not open"
    });
    Then.waitFor({
        id: "sessionType",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Change" }),
        success: function () {
            Opa5.assert.ok(true, "the type is shown as a status");
        }
    });
    Then.waitFor({
        id: "sessionTarget",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "dev-system" }),
        success: function () {
            Opa5.assert.ok(true, "and the target system");
        }
    });
    Then.waitFor({
        id: "stageTimeline",
        ...SOPTS,
        check: function (control: UI5Element) {
            return tokens(control).length === 5 && !!control.getDomRef()?.querySelector("[aria-current='step']");
        },
        success: function (control: UI5Element) {
            const list = tokens(control);
            Opa5.assert.deepEqual(list.map((t) => t.getText()), ["Chat", "Design", "Plan", "Changes", "Review"],
                "five stages, in walking order");
            const current = control.getDomRef()!.querySelectorAll("[aria-current='step']");
            Opa5.assert.strictEqual(current.length, 1, "exactly one token is the current step");
            Opa5.assert.ok(current[0].textContent?.includes("Chat"), "and it is Chat");
            Opa5.assert.ok(list[0].getIcon(), "the current stage is marked by an icon too, not only a colour");
        },
        errorMessage: "No stage timeline with a current step"
    });
    thePrimaryActionIs(Then, "Start design", true, "in chat the primary action starts the design");
    Then.waitFor({
        id: "primaryReason",
        ...SOPTS,
        visible: false,
        success: function (control: UI5Element) {
            Opa5.assert.notOk(control.getDomRef(), "no reason while the action is allowed");
        }
    });

    iPress(When, "backToWorklist", "No way back to the worklist");
    theHashIs(Then, /^sessions$/, "back opens the worklist");
    iSeeRows(Then, 1, "the worklist is shown again");

    Then.iStopTheApp();
});

opaTest("deep link: the primary action names the version it approves; usage survives a reload", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake); });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", true, "the deep link opens the session in design");
    Then.waitFor({
        id: "usageText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Requests used: 38 / 1000" }),
        success: function () {
            Opa5.assert.ok(true, "the requests used come from the session JSON");
        }
    });
    Then.iTeardownMyUIComponent();
    Given.iStartMyUIComponent({ componentConfig: { name: "com.agent.ide", async: true }, hash: "sessions/s-1" });
    Then.waitFor({
        id: "usageText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Requests used: 38 / 1000" }),
        success: function () {
            Opa5.assert.ok(true, "after a reload the numbers are still the session's");
        }
    });
    Then.iStopTheApp();
});

opaTest("without a design the primary action is disabled and says why", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake, 0); });
    thePrimaryActionIs(Then, "Approve design and plan", false, "nothing to approve yet");
    theReasonIs(Then, "Ask the assistant for a design first: Approve needs a design document.",
        "the reason is visible text, not only a tooltip");
    Then.iStopTheApp();
});

opaTest("an open comment blocks the primary action; dismissing it enables it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        const sid = designSession(fake, 1);
        openComment(fake, sid);
    });
    thePrimaryActionIs(Then, "Approve design (v1) and plan", false, "blocked by the open comment");
    theReasonIs(Then, "Resolve or dismiss the 1 open review comment first.", "the reason names the open comment");
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Request changes (1)" }),
        success: function () {
            Opa5.assert.ok(true, "Request changes counts the open comments");
        }
    });
    Then.waitFor({
        success: function () {
            backend.setCommentState("s-1", "c-open", "dismissed");
            // Leaving the page and opening the session again reads it again (a query change alone does not).
            HashChanger.getInstance().setHash("sessions");
        }
    });
    iSeeRows(Then, 1, "the worklist");
    Then.waitFor({
        success: function () {
            HashChanger.getInstance().setHash("sessions/s-1?view=document&kind=design&version=1");
        }
    });
    thePrimaryActionIs(Then, "Approve design (v1) and plan", true, "the dismissed comment no longer blocks");
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: [new PropertyStrictEquals({ name: "text", value: "Request changes" }),
            new PropertyStrictEquals({ name: "enabled", value: true })],
        success: function () {
            Opa5.assert.ok(true, "no count without open comments; still enabled (a note alone may be sent)");
        }
    });
    Then.iStopTheApp();
});

opaTest("a 409 open_comments from the server shows the same reason and reloads", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake, 1); });
    thePrimaryActionIs(Then, "Approve design (v1) and plan", true, "allowed as far as the page knows");
    Then.waitFor({
        success: function () {
            openComment(backend, "s-1");   // written in another tab meanwhile
        }
    });
    iPress(When, "primaryAction");
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Resolve or dismiss the open review comments first." }),
        success: function () {
            Opa5.assert.ok(true, "the server's refusal is shown when it arrives, not only after the reload");
        },
        errorMessage: "The open_comments refusal was swallowed"
    });
    closeMessageBox(When);
    theReasonIs(Then, "Resolve or dismiss the 1 open review comment first.", "the server's refusal shows the gate reason");
    thePrimaryActionIs(Then, "Approve design (v1) and plan", false, "and the reloaded session keeps it disabled");
    Then.iStopTheApp();
});

opaTest("approve sends the version it shows and the pin appears in the timeline", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { designSession(fake, 2); });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", true, "design v2 can be approved");
    iPress(When, "primaryAction");
    thePrimaryActionIs(Then, "Approve plan and propose changes", false, "the session moved to plan");
    Then.waitFor({
        id: "stageTimeline",
        ...SOPTS,
        check: function (control: UI5Element) {
            return tokens(control)[1]?.getTooltip() === "Design: v2 approved";
        },
        success: function (control: UI5Element) {
            const body = backend.bodies.find((b) => b.key === "POST sessions/s-1/approve")?.body;
            Opa5.assert.deepEqual(body, { version: 2 }, "approve sent the version on screen");
            Opa5.assert.ok(control.getDomRef()?.textContent?.includes("v2 approved"), "the design token shows its pin");
        },
        errorMessage: "The design pin is not shown"
    });
    Then.iStopTheApp();
});

opaTest("?view=document&kind=design&version=1 opens the artifact column; closing it returns to one column", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { designSession(fake, 2); });
    Then.waitFor({
        id: "fcl",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "layout", value: "TwoColumnsMidExpanded" }),
        success: function () {
            Opa5.assert.ok(true, "the deep link restores the artifact column");
        },
        errorMessage: "The artifact column is not open"
    });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design v1" }),
        success: function () {
            Opa5.assert.ok(true, "it shows the requested version, not the latest");
        }
    });
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: function (control: UI5Element) {
            return !!control.getDomRef()?.textContent?.includes("Design v1");
        },
        success: function () {
            Opa5.assert.ok(true, "the document is rendered");
        }
    });
    iPress(When, "closeArtifact", "No close button on the artifact column");
    Then.waitFor({
        id: "fcl",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "layout", value: "OneColumn" }),
        success: function () {
            Opa5.assert.strictEqual(HashChanger.getInstance().getHash(), "sessions/s-1", "the query is gone from the hash");
        }
    });
    Then.iStopTheApp();
});

opaTest("send a message: the answer streams in; the page follows only a reader at the bottom", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        longConversation(fake, "s-1");
        fake.answer = LONG_ANSWER;
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: function (control: UI5Element) {
            return (control as List).getItems().length === 30;
        },
        success: function () {
            Opa5.assert.ok(true, "the stored conversation is shown");
        }
    });
    Then.waitFor({
        id: "conversationScroll",
        ...SOPTS,
        check: function (control: UI5Element) {
            const el = control.getDomRef() as HTMLElement;
            return el.scrollHeight > el.clientHeight && el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        },
        success: function () {
            Opa5.assert.ok(true, "opening a session shows its latest message");
        },
        errorMessage: "The conversation does not start at its end"
    });

    // At the bottom: the stream is followed.
    iSend(When, "Why two decimals?");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: function (control: UI5Element) {
            const items = (control as List).getItems();
            return items.length === 32 && !!items[31].getDomRef()?.textContent?.includes("Line 40 of a long answer.");
        },
        success: function () {
            Opa5.assert.ok(true, "the question and the streamed answer are shown");
        },
        errorMessage: "The streamed answer did not arrive"
    });
    Then.waitFor({
        id: "conversationScroll",
        ...SOPTS,
        check: function (control: UI5Element) {
            const el = control.getDomRef() as HTMLElement;
            return el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        },
        success: function () {
            Opa5.assert.ok(true, "a reader at the bottom follows the answer to its end");
        },
        errorMessage: "The page did not follow the answer"
    });
    Then.waitFor({
        id: "usageText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Requests used: 1 / 200" }),
        success: function () {
            Opa5.assert.ok(true, "the usage event updates the header");
        }
    });

    // Scrolled up: the stream does not move the page.
    Then.waitFor({
        success: function () {
            release = backend.pauseStream();
        }
    });
    iSend(When, "And the rounding mode?");
    Then.waitFor({
        id: "stopButton",
        ...SOPTS,
        success: function () {
            Opa5.assert.ok(true, "Stop is offered while the assistant works");
        },
        errorMessage: "No Stop while running"
    });
    thePrimaryActionIs(Then, "Start design", false, "the primary action waits for the run");
    scroller(When, (el) => {
        el.scrollTop = 0;
        el.dispatchEvent(new Event("scroll"));
    });
    Then.waitFor({
        success: function () {
            release();
        }
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: function (control: UI5Element) {
            const items = (control as List).getItems();
            return items.length === 34 && !!items[33].getDomRef()?.textContent?.includes("Line 40 of a long answer.");
        },
        success: function () {
            Opa5.assert.ok(true, "the second answer arrived");
        },
        errorMessage: "The second answer did not arrive"
    });
    thePrimaryActionIs(Then, "Start design", true, "the run is over");
    Then.waitFor({
        id: "conversationScroll",
        ...SOPTS,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control.getDomRef() as HTMLElement).scrollTop, 0, "the reader who scrolled up stays where they were");
        }
    });
    Then.iStopTheApp();
});

opaTest("a diagnose session offers Create report, then Hand over to a change", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
    });
    Then.waitFor({
        success: function () {
            HashChanger.getInstance().setHash(`sessions/${sid}`);
        }
    });
    thePrimaryActionIs(Then, "Create report", true, "no report yet: the primary action writes one");
    Then.waitFor({
        id: "stageTimeline",
        ...SOPTS,
        visible: false,
        success: function (control: UI5Element) {
            Opa5.assert.notOk((control as HBox).getVisible(), "a diagnose session has no stage timeline");
        }
    });
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        visible: false,
        success: function (control: UI5Element) {
            Opa5.assert.notOk((control as HBox).getVisible(), "and no Request changes");
        }
    });
    iPress(When, "primaryAction");
    thePrimaryActionIs(Then, "Hand over to a change", true, "once the report exists the handover is the next step");
    Then.waitFor({
        id: "createReportButton",
        ...SOPTS,
        success: function () {
            Opa5.assert.ok(true, "the report can still be written again");
        }
    });
    Then.iStopTheApp();
});
