import Opa5 from "sap/ui/test/Opa5";
import type Button from "sap/m/Button";
import type ObjectListItem from "sap/m/ObjectListItem";
import type UI5Element from "sap/ui/core/Element";
import type ManagedObject from "sap/ui/base/ManagedObject";
import type View from "sap/ui/core/mvc/View";
import type { NotificationItem } from "../../../service/types";
import type Common from "./Common";
import { backend } from "./Common";

/**
 * Steps on the notification bell of the app shell and its list. What they
 * read comes from the rendered controls (the badge the button draws, the
 * items of the open popover), not from the model behind them.
 */

/** A finished run as the fake backend stores it (`unread` follows from the read marker). */
export type StoredNotification = Omit<NotificationItem, "unread">;

/** What the shell controller offers a journey beside its event handlers. */
export interface ShellController {
    pollNotifications(): Promise<void>;
}

/** What one entry of the list shows. */
export interface ItemTexts {
    title: string;
    attributes: string[];
    status: string;
    statusState: string;
    unread: boolean;
}

/**
 * Starts the app with `items` as the finished runs and the read marker at
 * `seenAt`. Seeded in the same queued step as the reset, before the shell's
 * first poll (see `Common.iStartTheApp` on why not by the caller).
 */
export function iStartTheAppWithNotifications(
    Given: Common, items: StoredNotification[], seenAt: string, hash = ""
): void {
    Given.waitFor({
        success: function () {
            backend.reset();
            backend.notifications = items.slice();
            backend.notificationsSeenAt = seenAt;
            backend.install();
        }
    });
    Given.iStartMyUIComponent({
        componentConfig: { name: "com.agent.admin", async: true },
        hash
    });
}

function controllerOf(control: UI5Element): ShellController {
    let parent: ManagedObject | null = control;
    while (parent !== null && !parent.isA("sap.ui.core.mvc.View")) {
        parent = (parent as ManagedObject).getParent();
    }
    return (parent as unknown as View).getController() as unknown as ShellController;
}

/** The badge the bell shows: "" when it shows none. Both what the control
 *  holds and what it rendered must agree. */
export function badgeOf(bell: UI5Element): string {
    const data = (bell as Button).getCustomData().filter((entry) => entry.isA("sap.m.BadgeCustomData"))[0] as unknown as
        { getValue(): string; getVisible(): boolean } | undefined;
    const held = data && data.getVisible() ? String(data.getValue() ?? "") : "";
    const indicator = bell.getDomRef()?.querySelector(".sapMBadgeIndicator");
    const rendered = indicator ? (indicator.getAttribute("data-badge") ?? "") : "";
    return held === rendered ? held : `held "${held}" but rendered "${rendered}"`;
}

/** Waits until the bell's badge reads `expected` ("" = no badge). */
export function iSeeTheBadge(Then: Common, expected: string, message: string): void {
    Then.waitFor({
        id: "notificationBell",
        viewName: "App",
        check: function (bell: UI5Element) { return badgeOf(bell) === expected; },
        success: function () { Opa5.assert.ok(true, message); },
        errorMessage: `The bell's badge never read "${expected}": ${message}`
    });
}

/** Runs one poll, as the shell's timer would, after `change` altered the backend. */
export function iPollAfter(When: Common, change: () => void): void {
    When.waitFor({
        id: "notificationBell",
        viewName: "App",
        success: function (bell: UI5Element) {
            change();
            void controllerOf(bell).pollNotifications();
        }
    });
}

export function textsOf(item: ObjectListItem): ItemTexts {
    const status = item.getFirstStatus();
    return {
        title: item.getTitle(),
        attributes: item.getAttributes().map((attribute) => attribute.getText()),
        status: status ? status.getText() : "",
        statusState: status ? String(status.getState()) : "",
        unread: item.getHighlight() !== "None"
    };
}

/** Waits for the open list to hold `count` entries and hands them to `assert`. */
export function iSeeTheItems(Then: Common, count: number, assert: (items: ObjectListItem[]) => void): void {
    Then.waitFor({
        controlType: "sap.m.ObjectListItem",
        searchOpenDialogs: true,
        check: function (items: UI5Element[]) { return items.length === count; },
        success: function (items: UI5Element[]) { assert(items as ObjectListItem[]); },
        errorMessage: `The notification list never showed ${count} entries`
    });
}

/** Whether a toast with exactly this text is on screen. */
export function toastShows(text: string): boolean {
    return Array.from(document.querySelectorAll(".sapMMessageToast"))
        .some((toast) => (toast.textContent ?? "") === text);
}
