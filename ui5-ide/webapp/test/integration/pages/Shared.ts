import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import type { InvisibleMessageMode } from "sap/ui/core/library";
import type UI5Element from "sap/ui/core/Element";
import type Common from "./Common";

/** Steps every page's journeys share. */

/** True when `control` sits inside an open sap.m.MessageBox (not any other dialog). */
function inMessageBox(control: UI5Element): boolean {
    let parent = control.getParent() as UI5Element | null;
    while (parent && !(parent as UI5Element).isA("sap.m.Dialog")) {
        parent = (parent as UI5Element).getParent() as UI5Element | null;
    }
    return !!parent && (parent as unknown as { hasStyleClass(c: string): boolean }).hasStyleClass("sapMMessageBox");
}

/**
 * Presses the button that closes the open message box ("Close" for an error,
 * "OK" for a warning). Only a message box's button: a dialog behind it with a
 * button of the same text is left alone.
 */
export function closeMessageBox(When: Common, button = "Close"): void {
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: [new PropertyStrictEquals({ name: "text", value: button }), inMessageBox],
        actions: new Press(),
        errorMessage: `The message box has no ${button} button`
    });
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
