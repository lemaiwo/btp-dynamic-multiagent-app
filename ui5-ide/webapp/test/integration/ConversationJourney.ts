import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type List from "sap/m/List";
import type ListItemBase from "sap/m/ListItemBase";
import type Panel from "sap/m/Panel";
import type TextArea from "sap/m/TextArea";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { closeMessageBox } from "./pages/Shared";
import { SOPTS, iPress, iSend, longConversation, scroller, thePrimaryActionIs } from "./pages/Session";
import type { Message } from "./FakeBackend";

/** Task U8: the conversation column of the session page. */
QUnit.module("Conversation journey");

const LONG_ANSWER = Array.from({ length: 40 }, (_, i) => `Line ${i + 1} of a long answer.`).join("\n\n");

function items(control: UI5Element): ListItemBase[] {
    return (control as List).getItems();
}

/** What each row is: `user`, `assistant`, `card` (pending approval) or `line` (decided approval). */
function shape(control: UI5Element): string[] {
    return items(control).map((item) => {
        const ctx = item.getBindingContext("s");
        if (ctx?.getProperty("kind") === "approval") {
            return ctx.getProperty("approval/pending") ? "card" : "line";
        }
        return ctx?.getProperty("isUser") ? "user" : "assistant";
    });
}

function textOf(item: ListItemBase): string {
    return item.getDomRef()?.textContent ?? "";
}

function atBottom(el: HTMLElement): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 30;
}

/** A stored message; `stage` follows the session type (`investigate` in diagnose, a change stage otherwise). */
function seeded(
    id: string, role: Message["role"], created_at: string, content: string, activity?: Message["activity"],
    stage: Message["stage"] = "investigate"
): Message {
    return { id, role, stage, created_at, content, ...(activity ? { activity } : {}) };
}

opaTest("the answer streams in; a reader who scrolled up stays put and is offered Jump to latest", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        longConversation(fake, "s-1");
        fake.answer = LONG_ANSWER;
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => items(control).length === 30,
        success: function () {
            Opa5.assert.ok(true, "the stored conversation is shown");
            release = backend.pauseStream();
        }
    });
    Then.waitFor({
        id: "jumpToLatest",
        ...SOPTS,
        visible: false,
        success: function (control: UI5Element) {
            Opa5.assert.notOk((control as Control).getVisible(), "at the end: no Jump to latest");
        }
    });
    iSend(When, "Why two decimals?");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: function (control: UI5Element) {
            const list = items(control);
            return list.length === 32 && textOf(list[31]).includes("Line 1");
        },
        success: function () {
            Opa5.assert.ok(true, "the answer appears while it is streamed");
        },
        errorMessage: "No streamed text"
    });
    scroller(When, (el) => {
        el.scrollTop = 0;
        el.dispatchEvent(new Event("scroll"));
    });
    Then.waitFor({
        id: "jumpToLatest",
        ...SOPTS,
        success: function () {
            Opa5.assert.ok(true, "scrolled up: Jump to latest is offered");
            release();
        },
        errorMessage: "No Jump to latest after scrolling up"
    });
    thePrimaryActionIs(Then, "Start design", true, "the run is over");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => textOf(items(control)[items(control).length - 1]).includes("Line 40"),
        success: function () {
            Opa5.assert.ok(true, "the whole answer arrived");
        }
    });
    Then.waitFor({
        id: "conversationScroll",
        ...SOPTS,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control.getDomRef() as HTMLElement).scrollTop, 0, "the reader stayed where they were");
        }
    });
    iPress(When, "jumpToLatest");
    Then.waitFor({
        id: "conversationScroll",
        ...SOPTS,
        check: (control: UI5Element) => atBottom(control.getDomRef() as HTMLElement),
        success: function () {
            Opa5.assert.ok(true, "Jump to latest goes to the end");
        },
        errorMessage: "Jump to latest did not scroll"
    });
    Then.waitFor({
        id: "jumpToLatest",
        ...SOPTS,
        visible: false,
        check: (control: UI5Element) => !(control as Control).getVisible(),
        success: function () {
            const input = Opa5.getWindow().document.querySelector("[id$='--chatInput']");
            Opa5.assert.ok(input?.contains(Opa5.getWindow().document.activeElement), "the focus goes to the message input");
        }
    });
    Then.iStopTheApp();
});

opaTest("assistant text is sanitised markdown; user text stays text", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.dataOf("s-1")!.messages.push(
            seeded("m-u", "user", "2026-10-03T09:00:00", "Is <b>this</b> bold?", undefined, "chat"),
            seeded("m-a", "assistant", "2026-10-03T09:01:00",
                "**Yes.** <script>window.__ideXss = 1</script><img src=x onerror=\"window.__ideXss = 2\"> [link](javascript:alert(1))",
                undefined, "chat")
        );
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => items(control).length === 2 && !!items(control)[1].getDomRef()?.querySelector("strong"),
        success: function (control: UI5Element) {
            const [user, answer] = items(control).map((i) => i.getDomRef() as HTMLElement);
            Opa5.assert.ok(user.textContent?.includes("Is <b>this</b> bold?"), "the user's markup is shown as text");
            Opa5.assert.notOk(user.querySelector("b"), "and not rendered");
            Opa5.assert.notOk(answer.querySelector("script, img[onerror]"), "no script, no event handler");
            Opa5.assert.notOk(answer.querySelector("a[href^='javascript']"), "no javascript: link");
            Opa5.assert.notOk((Opa5.getWindow() as unknown as { __ideXss?: number }).__ideXss, "nothing ran");
        },
        errorMessage: "The messages are not rendered"
    });
    Then.iStopTheApp();
});

opaTest("approval cards sit at their place in time; Approve and Reject decide them", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
        fake.dataOf(sid)!.messages.push(
            seeded("m-1", "user", "2026-10-03T09:00:00", "The order list takes 9 seconds. Why?"),
            seeded("m-2", "assistant", "2026-10-03T09:01:00", "I need a trace of one call."),
            seeded("m-3", "assistant", "2026-10-03T09:10:00", "After you approve, open the list once.")
        );
        fake.addApproval(sid, { created_at: "2026-10-03T09:05:00" });
        fake.addApproval(sid, { created_at: "2026-10-03T09:20:00" });
    });
    Then.waitFor({
        success: function () {
            HashChanger.getInstance().setHash(`sessions/${sid}`);
        }
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control).length === 5,
        success: function (control: UI5Element) {
            Opa5.assert.deepEqual(shape(control), ["user", "assistant", "card", "assistant", "card"],
                "each card between the messages it came between");
            Opa5.assert.ok(textOf(items(control)[2]).includes("Arm a profiler trace on dev-system?"), "the card asks its question");
        },
        errorMessage: "The approvals are not in the conversation"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: "Approve trace" }),
            (button: UI5Element) => button.getBindingContext("s")?.getPath() === "/messages/2"
        ],
        actions: new Press(),
        success: function () {
            Opa5.assert.ok(true, "Approve pressed on the first card");
        }
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control)[2] === "line",
        success: function (control: UI5Element) {
            Opa5.assert.ok(textOf(items(control)[2]).startsWith("Trace armed"), "the card became the armed line, in place");
            Opa5.assert.strictEqual(backend.requests.filter((r) => /^POST sessions\/[^/]+\/approvals\//.test(r)).length, 1, "one decision sent");
        },
        errorMessage: "The approval was not decided"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: "Reject" }),
            (button: UI5Element) => button.getBindingContext("s")?.getPath() === "/messages/4"
        ],
        actions: new Press(),
        errorMessage: "No Reject on the second card"
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control)[4] === "line",
        success: function (control: UI5Element) {
            Opa5.assert.ok(textOf(items(control)[4]).includes("Request rejected"), "Reject: nothing armed");
        },
        errorMessage: "The rejection is not shown"
    });

    // A run proposes a trace: its card follows the question that led to it.
    Then.waitFor({
        success: function () {
            backend.scriptRun({ approval: {} });
        }
    });
    iSend(When, "And the second call?");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control).length === 8 && !backend.dataOf(sid)!.session.status.startsWith("run"),
        success: function (control: UI5Element) {
            Opa5.assert.deepEqual(shape(control).slice(5), ["user", "card", "assistant"],
                "the proposal sits between the question and the answer, where it happened");
        },
        errorMessage: "The proposed trace is not in the conversation"
    });
    Then.iStopTheApp();
});

opaTest("a message's activity is read only when the reader opens it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.dataOf("s-1")!.messages.push(
            seeded("m-q", "user", "2026-10-03T09:00:00", "Change the rounding.", undefined, "chat"),
            seeded("m-a", "assistant", "2026-10-03T09:01:00", "I proposed the change.", {
                events: [
                    { ts: "t", agent: "abap", kind: "tool", id: "c1", tool: "SAPRead", detail: "ZCL_DEMO", status: "ok", output: "CLASS zcl_demo." },
                    { ts: "t", agent: "ide", kind: "tool", id: "c2", tool: "check_sap_base", detail: "Checking SAP base", status: "ok", output: "" },
                    { ts: "t", agent: "ide", kind: "tool", id: "c3", tool: "check_syntax", detail: "Checking syntax", status: "ok", output: "" }
                ],
                plan: [{ content: "Read the class", status: "completed" }]
            }, "chat"),
            seeded("m-b", "assistant", "2026-10-03T09:02:00", "A reply without tools.", undefined, "chat")
        );
    });
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...SOPTS,
        check: (panels: UI5Element[]) => panels.length === 1,
        success: function (panels: UI5Element[]) {
            const panel = panels[0] as Panel;
            Opa5.assert.strictEqual(panel.getHeaderText(), "Show activity", "one assistant message has an activity to show");
            Opa5.assert.notOk(panel.getExpanded(), "closed");
            Opa5.assert.strictEqual(backend.requests.filter((r) => r.endsWith("/activity")).length, 0, "nothing read before the click");
        },
        errorMessage: "No activity panel"
    });
    When.waitFor({
        controlType: "sap.m.Panel",
        ...SOPTS,
        actions: new Press({ idSuffix: "header" }),
        errorMessage: "The activity panel cannot be opened"
    });
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...SOPTS,
        check: (panels: UI5Element[]) => (panels[0] as Panel).getHeaderText() === "Activity: 3 tool calls · 1 plan step",
        success: function (panels: UI5Element[]) {
            const text = (panels[0] as Panel).getDomRef()?.textContent ?? "";
            Opa5.assert.ok(text.includes("Check of the objects against SAP"), "the base check has a readable name");
            Opa5.assert.ok(text.includes("Syntax check of the proposals"), "so has the syntax check");
            Opa5.assert.ok(text.includes("SAPRead"), "other tools keep their name");
            Opa5.assert.ok(text.includes("Read the class"), "the plan is shown");
            Opa5.assert.deepEqual(backend.requests.filter((r) => r.endsWith("/activity")), ["GET sessions/s-1/messages/m-a/activity"],
                "read once, on request");
        },
        errorMessage: "The activity did not load"
    });
    When.waitFor({ controlType: "sap.m.Panel", ...SOPTS, actions: new Press({ idSuffix: "header" }) });
    When.waitFor({ controlType: "sap.m.Panel", ...SOPTS, actions: new Press({ idSuffix: "header" }) });
    Then.waitFor({
        controlType: "sap.m.Panel",
        ...SOPTS,
        check: (panels: UI5Element[]) => (panels[0] as Panel).getExpanded(),
        success: function () {
            Opa5.assert.strictEqual(backend.requests.filter((r) => r.endsWith("/activity")).length, 1, "opening it again reads nothing");
        }
    });
    Then.iStopTheApp();
});

opaTest("usage after a reload is the session's; a refused send puts the text back", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1");
    iSend(When, "Hello?");
    Then.waitFor({
        id: "usageText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Requests used: 1 / 200" }),
        success: function () {
            Opa5.assert.ok(true, "the run's usage");
        }
    });
    Then.iTeardownMyUIComponent();
    Given.iStartMyUIComponent({ componentConfig: { name: "com.agent.ide", async: true }, hash: "sessions/s-1" });
    Then.waitFor({
        id: "usageText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Requests used: 1 / 200" }),
        success: function () {
            Opa5.assert.ok(true, "after a reload the header shows the session's numbers");
            backend.failNext = {
                path: "sessions/s-1/messages", status: 409, skip: 0,
                body: { detail: "A run is already in progress.", code: "run_in_progress" }
            };
        }
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => items(control).length === 2,
        success: function () {
            Opa5.assert.ok(true, "the stored question and answer");
        }
    });
    iSend(When, "Refused question");
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "The assistant is still working on this session. Wait for it or stop it." }),
        success: function () {
            Opa5.assert.ok(true, "the refusal is said");
        },
        errorMessage: "No refusal message"
    });
    closeMessageBox(When);
    Then.waitFor({
        id: "chatInput",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "value", value: "Refused question" }),
        success: function (control: UI5Element) {
            Opa5.assert.ok((control as TextArea).getValue() === "Refused question", "the text is back in the composer");
        },
        errorMessage: "The refused text is lost"
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual(items(control).length, 2, "and the refused question is not left in the list");
        }
    });
    Then.iStopTheApp();
});

// --- U8 review follow-ups -----------------------------------------------------

function approveOrReject(When: Common, text: string, path: string, message: string): void {
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: [new PropertyStrictEquals({ name: "text", value: text }), (b: UI5Element) => b.getBindingContext("s")?.getPath() === path],
        actions: new Press(),
        errorMessage: message
    });
}

function decisions(sid: string, aid: string): number {
    return backend.requests.filter((r) => r === `POST sessions/${sid}/approvals/${aid}`).length;
}

opaTest("approval decisions: 410 expires the card, 409 relists, 424 keeps it pending, a double press sends once", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    const ids: string[] = [];
    let release!: () => void;
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
        ids.push(fake.addApproval(sid, { created_at: "2026-10-03T09:01:00", expiresAt: "2020-01-01T00:00:00Z" }).id);
        ids.push(fake.addApproval(sid, { created_at: "2026-10-03T09:02:00" }).id);
        ids.push(fake.addApproval(sid, { created_at: "2026-10-03T09:03:00" }).id);
        ids.push(fake.addApproval(sid, { created_at: "2026-10-03T09:04:00" }).id);
    });
    Then.waitFor({
        success: function () {
            HashChanger.getInstance().setHash(`sessions/${sid}`);
        }
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => JSON.stringify(shape(control)) === JSON.stringify(["card", "card", "card", "card"]),
        success: function () {
            Opa5.assert.ok(true, "four pending cards");
        },
        errorMessage: "The four approvals are not shown"
    });
    // 410: past its expiry on the server.
    approveOrReject(When, "Reject", "/messages/0", "No Reject on the first card");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control)[0] === "line" && textOf(items(control)[0]).includes("expired"),
        success: function () {
            Opa5.assert.ok(true, "410: the card became the expired line");
        },
        errorMessage: "The expired card is not a line"
    });
    // 409 approval_not_pending: decided elsewhere meanwhile.
    Then.waitFor({
        success: function () {
            backend.dataOf(sid)!.approvals.find((a) => a.id === ids[1])!.status = "denied";
        }
    });
    approveOrReject(When, "Reject", "/messages/1", "No Reject on the second card");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control)[1] === "line",
        success: function (control: UI5Element) {
            Opa5.assert.ok(textOf(items(control)[1]).includes("rejected"), "409: the list was read again and shows the server's state");
        },
        errorMessage: "The already decided card was not relisted"
    });
    // 424: no user token; nothing was decided.
    Then.waitFor({
        success: function () {
            backend.failNext = {
                path: `sessions/${sid}/approvals/${ids[2]}`, status: 424,
                body: { detail: "No user token", code: "user_token_required" }
            };
        }
    });
    approveOrReject(When, "Approve trace", "/messages/2", "No Approve on the third card");
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Your SAP user token is missing. Reload the app to sign in again." }),
        success: function () {
            Opa5.assert.ok(true, "424: a message box says why");
        },
        errorMessage: "No message box for the 424"
    });
    closeMessageBox(When);
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control)[2] === "card",
        success: function () {
            Opa5.assert.ok(true, "and the card stays pending");
        }
    });
    // A double press while the decision is on its way sends one decision.
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: [new PropertyStrictEquals({ name: "text", value: "Reject" }), (b: UI5Element) => b.getBindingContext("s")?.getPath() === "/messages/3"],
        success: function (buttons: UI5Element[]) {
            release = backend.hold(`POST sessions/${sid}/approvals/${ids[3]}`);
            const button = buttons[0] as unknown as { firePress(): void };
            button.firePress();
            button.firePress();
            release();
        },
        errorMessage: "No Reject on the fourth card"
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (control: UI5Element) => shape(control)[3] === "line",
        success: function () {
            Opa5.assert.strictEqual(decisions(sid, ids[3]), 1, "one decision sent for two presses");
        },
        errorMessage: "The fourth card was not decided"
    });
    Then.iStopTheApp();
});

opaTest("the composer takes up to 20000 characters; above that Send is off and says why", function (Given: Common, When: Common, Then: Common) {
    const long = "x".repeat(20001);
    Given.iStartTheApp("sessions/s-1");
    When.waitFor({
        id: "chatInput",
        ...SOPTS,
        success: function (control: UI5Element) {
            const area = control as TextArea;
            Opa5.assert.strictEqual(area.getMaxLength(), 20000, "maxLength is the server's limit");
            Opa5.assert.ok(area.getShowExceededText(), "the counter shows what is over");
            area.setValue(long);
            area.fireLiveChange({ value: long });
        }
    });
    Then.waitFor({
        id: "sendButton",
        ...SOPTS,
        enabled: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        success: function (control: UI5Element) {
            const reason = Opa5.getWindow().document.querySelector("[id$='--draftTooLong']") as HTMLElement | null;
            Opa5.assert.strictEqual(reason?.textContent, "The message is too long: at most 20000 characters.", "the reason is visible");
            Opa5.assert.ok(control.getDomRef()?.getAttribute("aria-describedby")?.includes("draftTooLong"), "and describes Send");
        },
        errorMessage: "Send is not off for an over-long message"
    });
    When.waitFor({
        id: "chatInput",
        ...SOPTS,
        success: function (control: UI5Element) {
            (control as TextArea).setValue("short");
            (control as TextArea).fireLiveChange({ value: "short" });
        }
    });
    Then.waitFor({
        id: "sendButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        success: function () {
            Opa5.assert.ok(true, "back under the limit Send is on again");
        }
    });
    Then.iStopTheApp();
});

opaTest("a refused send keeps newer typing and puts the refused text in front of it", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.failNext = { path: "sessions/s-1/messages", status: 409, skip: 1, body: { detail: "A run is already in progress.", code: "run_in_progress" } };
    });
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        success: function () {
            release = backend.hold("POST sessions/s-1/messages");
        }
    });
    iSend(When, "Refused question");
    When.waitFor({
        id: "chatInput",
        ...SOPTS,
        success: function (control: UI5Element) {
            (control as TextArea).setValue("Newer text");
            release();
        }
    });
    closeMessageBox(When);
    Then.waitFor({
        id: "chatInput",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "value", value: "Refused question\n\nNewer text" }),
        success: function () {
            Opa5.assert.ok(true, "nothing typed meanwhile is lost; the refused text comes first");
        },
        errorMessage: "The refused text overwrote the newer typing"
    });
    Then.iStopTheApp();
});

opaTest("a long activity shows 50 rows and Show all; long tool output opens in full; the status is text", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        const events = Array.from({ length: 60 }, (_, i) => ({
            ts: "t", agent: "abap", kind: "tool", id: `c${i}`, tool: "SAPRead", detail: `object ${i}`, status: "ok",
            output: i === 0 ? "line 1\nline 2\nline 3\nline 4\nline 5" : ""
        }));
        fake.dataOf("s-1")!.messages.push(
            seeded("m-q", "user", "2026-10-03T09:00:00", "Read everything.", undefined, "chat"),
            seeded("m-a", "assistant", "2026-10-03T09:01:00", "Done.", { events, plan: [] }, "chat")
        );
    });
    When.waitFor({
        controlType: "sap.m.Panel",
        ...SOPTS,
        matchers: (p: UI5Element) => (p as Panel).getHeaderText() === "Show activity",
        actions: new Press({ idSuffix: "header" }),
        errorMessage: "No activity panel"
    });
    Then.waitFor({
        controlType: "sap.m.VBox",
        ...SOPTS,
        matchers: (c: UI5Element) => /--activityTools-/.test(c.getId()),
        check: (list: UI5Element[]) => ((list[0] as unknown as { getItems(): UI5Element[] }).getItems().length === 50),
        success: function (list: UI5Element[]) {
            const text = list[0].getDomRef()?.textContent ?? "";
            Opa5.assert.ok(text.includes("Done"), "each tool's status is text, not only a tooltip");
        },
        errorMessage: "Not 50 rows"
    });
    When.waitFor({
        controlType: "sap.m.Link",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Show full output" }),
        actions: new Press(),
        errorMessage: "No Show full output on the clipped output"
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: (t: UI5Element) => t.getId().includes("activityToolOutput") && (t as unknown as { getText(): string }).getText().startsWith("line 1"),
        check: (texts: UI5Element[]) => (texts[0] as unknown as { getMaxLines(): number }).getMaxLines() === 0,
        success: function () {
            Opa5.assert.ok(true, "the whole output is readable");
        },
        errorMessage: "The output stayed clipped"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Show all (60)" }),
        actions: new Press(),
        errorMessage: "No Show all (60)"
    });
    Then.waitFor({
        controlType: "sap.m.VBox",
        ...SOPTS,
        matchers: (c: UI5Element) => /--activityTools-/.test(c.getId()),
        check: (list: UI5Element[]) => ((list[0] as unknown as { getItems(): UI5Element[] }).getItems().length === 60),
        success: function () {
            Opa5.assert.ok(true, "all 60 rows after Show all");
        },
        errorMessage: "Show all did not show every row"
    });
    Then.iStopTheApp();
});
