/**
 * The logic behind the notification bell: which finished runs are new since
 * the last poll, the badge and toast texts, and how a status looks. Pure, so
 * it is unit-tested; the bell controller only wires it to the view.
 */
import { ValueState } from "sap/ui/core/library";
import type { NotificationItem } from "../service/types";

/** Agent and workflow run ids are separate id spaces, so the kind is part of the key. */
export function itemKey(item: NotificationItem): string {
    return `${item.kind}:${item.run_id}`;
}

/** The items not in `previousKeys`. The first poll (`null`) reports none, so opening the app never toasts. */
export function newlyFinished(previousKeys: Set<string> | null, items: NotificationItem[]): NotificationItem[] {
    if (previousKeys === null) {
        return [];
    }
    return items.filter((item) => !previousKeys.has(itemKey(item)));
}

/** The `finished_at` of the newest item (the list is newest first); `null` when empty. */
export function newestFinishedAt(items: NotificationItem[]): string | null {
    return items.length > 0 ? items[0].finished_at : null;
}

/** The badge on the bell: nothing for 0, the count up to 99, `99+` above. */
export function badgeText(unreadCount: number): string {
    if (unreadCount <= 0) {
        return "";
    }
    return unreadCount > 99 ? "99+" : String(unreadCount);
}

export interface ToastMessage {
    /** An i18n key. */
    key: string;
    args: (string | number)[];
}

/** The toast for the runs that just finished; `null` when there are none. */
export function toastKey(newItems: NotificationItem[]): ToastMessage | null {
    if (newItems.length === 0) {
        return null;
    }
    if (newItems.length === 1) {
        return { key: "notificationOneFinished", args: [newItems[0].name, newItems[0].status] };
    }
    return { key: "notificationManyFinished", args: [newItems.length] };
}

export function statusState(status: string): ValueState {
    switch (status) {
        case "success":
            return ValueState.Success;
        case "partial":
        case "interrupted":
            return ValueState.Warning;
        case "failed":
            return ValueState.Error;
        default:
            return ValueState.None;
    }
}

export function statusIcon(status: string): string {
    switch (status) {
        case "success":
            return "sap-icon://sys-enter-2";
        case "partial":
        case "interrupted":
            return "sap-icon://alert";
        case "failed":
            return "sap-icon://error";
        default:
            return "sap-icon://information";
    }
}
