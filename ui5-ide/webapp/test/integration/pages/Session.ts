import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type Common from "./Common";
import { backend } from "./Common";

/** Session-page steps (Task U7). The page is the `session` route. */

export const SOPTS = { viewName: "Session" };

/** A seeded change session in `stage` with `designs` design versions; answers its id. */
export function designSession(fake: typeof backend, designs = 2, extra: { used?: number; cap?: number } = {}): string {
    const s = fake.sessions[0].session;
    s.stage = "design";
    s.requests_used = extra.used ?? 38;
    s.request_cap = extra.cap ?? 1000;
    for (let i = 0; i < designs; i++) {
        fake.addArtifact(s.id, "design", `# Design v${i + 1}\n\nRound by the currency's decimals.`);
    }
    return s.id;
}

/** Adds an open document comment on design v1 of `sid`. */
export function openComment(fake: typeof backend, sid: string, id = "c-open"): void {
    fake.dataOf(sid)!.comments.push({
        id, anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 1, paragraph: 0, body: "Use the released API.", state: "open",
        answer: null, created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00"
    });
}

/** `count` stored messages, alternating user and assistant, so the conversation scrolls. */
export function longConversation(fake: typeof backend, sid: string, count = 30): void {
    const data = fake.dataOf(sid)!;
    for (let i = 0; i < count; i++) {
        data.messages.push({
            id: `m-old-${i}`, role: i % 2 ? "assistant" : "user", stage: data.session.stage,
            created_at: "2026-10-03T09:00:00",
            content: i % 2 ? `Answer ${i}: the class reads the decimals from the currency table.` : `Question ${i}?`
        });
    }
}

export function thePrimaryActionIs(Then: Common, text: string, enabled: boolean, message: string): void {
    Then.waitFor({
        id: "primaryAction",
        ...SOPTS,
        enabled: false,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: text }),
            new PropertyStrictEquals({ name: "enabled", value: enabled })
        ],
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The primary action is not "${text}" (${enabled ? "enabled" : "disabled"})`
    });
}

/** The disabled reason is a visible Text that the button names as its description. */
export function theReasonIs(Then: Common, text: string, message: string): void {
    Then.waitFor({
        id: "primaryReason",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: text }),
        success: function (reason: UI5Element) {
            const button = Opa5.getWindow().document.getElementById(
                reason.getId().replace(/primaryReason$/, "primaryAction"));
            Opa5.assert.ok(button?.getAttribute("aria-describedby")?.split(" ").includes(reason.getId()),
                "the button's description is the reason");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The reason "${text}" is not shown`
    });
}

export function iPress(When: Common, id: string, message = `No ${id}`): void {
    When.waitFor({ id, ...SOPTS, actions: new Press(), errorMessage: message });
}

export function iSend(When: Common, text: string): void {
    When.waitFor({
        id: "chatInput",
        ...SOPTS,
        actions: new EnterText({ text, keepFocus: true }),
        errorMessage: "No message input"
    });
    iPress(When, "sendButton", "Send is not enabled");
}

/** The scrolling element of the conversation. */
export function scroller(When: Common, fn: (el: HTMLElement) => void): void {
    When.waitFor({
        id: "conversationScroll",
        ...SOPTS,
        success: function (control: UI5Element) {
            fn(control.getDomRef() as HTMLElement);
        }
    });
}

export function pressedButtonText(control: UI5Element): string {
    return (control as Button).getText();
}
