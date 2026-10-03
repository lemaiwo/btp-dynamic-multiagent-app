import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type List from "sap/m/List";
import type HBox from "sap/m/HBox";
import type HTML from "sap/ui/core/HTML";
import type Button from "sap/m/Button";
import type Common from "./Common";
import { backend } from "./Common";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import type { InvisibleMessageMode } from "sap/ui/core/library";
import type { Artifact, Stage, WorkspaceFile } from "../FakeBackend";

/** Assistant-pane steps shared by the chat journeys. */

export const OPTS = { viewName: "Ide" };

/** A session in `stage` (newest, so it is selected), with optional artifacts and files. */
export function sessionIn(stage: Stage, artifacts: Artifact[] = [], files: WorkspaceFile[] = []) {
    return function (fake: typeof backend): void {
        fake.addSession(`Session in ${stage}`, files, artifacts).stage = stage;
    };
}

export function currentStage(Then: Common, stage: string, message: string): void {
    Then.waitFor({
        id: "stageBar",
        ...OPTS,
        check: function (control: UI5Element) {
            return (control as HBox).getItems().some((item) => {
                const ctx = item.getBindingContext("ide");
                return ctx?.getProperty("stage") === stage && ctx.getProperty("state") === "current";
            });
        },
        success: function (control: UI5Element) {
            const states = (control as HBox).getItems().map((i) => i.getBindingContext("ide")?.getProperty("state") as string);
            Opa5.assert.strictEqual(states.length, 6, "six stage tokens");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The stage bar does not show ${stage} as current`
    });
}

export function approveEnabled(Then: Common, enabled: boolean, message: string): void {
    Then.waitFor({
        id: "approveButton",
        ...OPTS,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: enabled }),
        success: function (control: UI5Element) {
            const button = control as Button;
            Opa5.assert.strictEqual(button.getType(), "Accept", "Approve is an Accept button");
            Opa5.assert.ok(button.getTooltip_AsString(), "Approve says why (or what next) in its tooltip");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `Approve is not ${enabled ? "enabled" : "disabled"}`
    });
}

export function send(When: Common, text: string): void {
    When.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "editable", value: true }),
        actions: new EnterText({ text })
    });
    When.waitFor({ id: "sendButton", ...OPTS, actions: new Press() });
}

export function answerShown(Then: Common, count: number, includes: string, message: string): void {
    Then.waitFor({
        id: "chatList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: count }),
        check: function (control: UI5Element) {
            const items = (control as List).getItems();
            const dom = items[items.length - 1].getDomRef();
            return !!dom && (dom.querySelector(".ideMarkdown")?.innerHTML ?? "").includes(includes);
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The chat does not end with an answer containing ${includes}`
    });
}


/** The rendered h1 of the open design document tab, or undefined. */
export function designHeading(Then: Common, heading: string, message: string, extra?: () => void): void {
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        ...OPTS,
        matchers: function (control: UI5Element) {
            let parent = control.getParent() as UI5Element | null;
            while (parent && parent.getMetadata().getName() !== "sap.m.TabContainerItem") {
                parent = parent.getParent() as UI5Element | null;
            }
            return !!parent && (parent as unknown as { getKey(): string }).getKey() === "doc:design";
        },
        check: function (controls: UI5Element[]) {
            return (controls[0] as HTML).getDomRef()?.querySelector("h1")?.textContent === heading;
        },
        success: function () {
            extra?.();
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The design tab does not show "${heading}"`
    });
}

/** Presses the session with `title` in the explorer's list. */
export function pressSession(When: Common, title: string): void {
    When.waitFor({
        controlType: "sap.m.ObjectListItem",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "title", value: title }),
        actions: new Press(),
        errorMessage: `No session '${title}' in the list`
    });
}

/** Presses the button that closes the open message box ("Close" for an error, "OK" for a warning). */
export function closeMessageBox(When: Common, button = "Close"): void {
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: button }),
        actions: new Press(),
        errorMessage: `The message box has no ${button} button`
    });
}

/** Matches controls of `type` inside the editor tab `key` (tab content is not in the item's DOM). */
export function inTab(key: string) {
    return function (control: UI5Element): boolean {
        let parent = control.getParent() as UI5Element | null;
        while (parent && parent.getMetadata().getName() !== "sap.m.TabContainerItem") {
            parent = parent.getParent() as UI5Element | null;
        }
        return !!parent && (parent as unknown as { getKey(): string }).getKey() === key;
    };
}

/** What the app announced through InvisibleMessage since the last recordAnnouncements(). */
export const announced: string[] = [];
let recording = false;

/** Starts recording announcements (wraps the singleton once; it still announces). */
export function recordAnnouncements(): void {
    announced.length = 0;
    if (recording) {
        return;
    }
    recording = true;
    const instance = InvisibleMessage.getInstance();
    const announce = instance.announce.bind(instance);
    instance.announce = function (text: string, mode?: InvisibleMessageMode | keyof typeof InvisibleMessageMode): void {
        announced.push(String(text));
        announce(text, mode as InvisibleMessageMode);
    };
}
