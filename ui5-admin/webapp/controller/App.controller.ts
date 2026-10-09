import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import {
    badgeText, itemKey, newestFinishedAt, newlyFinished, statusIcon, statusState, toastKey
} from "../model/notifications";
import type { NotificationItem, NotificationList } from "../service/types";
import type Button from "sap/m/Button";
import type ResponsivePopover from "sap/m/ResponsivePopover";
import type { ListBase$ItemPressEvent } from "sap/m/ListBase";
import type NavigationListItem from "sap/tnt/NavigationListItem";
import type { Router$RouteMatchedEvent, Router$BypassedEvent } from "sap/ui/core/routing/Router";
import type SideNavigation from "sap/tnt/SideNavigation";
import type { SideNavigation$ItemSelectEvent } from "sap/tnt/SideNavigation";

/** Which side-nav item is highlighted for each route. Detail routes keep
 *  their list's item selected. Anything unlisted (including the bypassed
 *  notFound target) selects nothing. */
const NAV_KEY_BY_ROUTE: Record<string, string> = {
    root: "agents",
    agents: "agents",
    agentDetail: "agents",
    skills: "skills",
    skillDetail: "skills",
    odataServices: "odataServices",
    odataServiceDetail: "odataServices",
    runs: "runs",
    runDetail: "runs",
    workflows: "workflows",
    workflowDetail: "workflows",
    workflowRuns: "workflowRuns",
    workflowRunDetail: "workflowRuns",
    settings: "settings"
};

/**
 * @namespace com.agent.admin.controller
 */
export default class App extends BaseController {

    /** The navigation key of the route that is shown. Kept apart from the
     *  model's `selectedKey`, which the side navigation overwrites with
     *  whatever item was pressed, before anyone agreed to leave. */
    private shownKey = "";

    /** For the bindings of the notification list. */
    public formatter = formatter;
    public notificationFormat = { statusState, statusIcon };

    /** Between two polls for finished runs. Static, so a test can shorten
     *  it; the journeys call `pollNotifications()` instead of waiting. */
    public static notificationPollMs = 15000;

    /** False once the shell is gone (and inside a host shell, where there
     *  is no bell): an answer that arrives then is dropped. */
    private notificationsActive = false;
    private notificationTimer?: ReturnType<typeof setTimeout>;
    /** Counts the polls, so that only the answer of the latest one is shown. */
    private pollSeq = 0;
    /** The runs of the last poll; `null` before the first one, which
     *  therefore announces nothing. */
    private knownKeys: Set<string> | null = null;
    private lastList?: NotificationList;
    /** The runs that were unread when the list was opened: they keep their
     *  mark while it is open, although opening it marked them read. */
    private openUnreadKeys = new Set<string>();
    private notificationsPopover?: Promise<ResponsivePopover>;

    private readonly onVisibilityChange = (): void => {
        if (this.notificationsActive && document.visibilityState === "visible") {
            void this.pollNotifications();
        }
    };

    public onInit(): void {
        const model = new JSONModel({
            sideExpanded: true,
            // Inside a Work Zone / launchpad shell the host already renders a
            // header; showing ours too would stack two title bars.
            showHeader: !App.isInShell(),
            // Left unselected: on a cold load straight to a bad hash, the
            // router's "bypassed" event can fire before this controller's
            // onInit attaches its listener (Component.init() calls
            // getRouter().initialize() before the App view/controller
            // exist), so that miss must default to "nothing highlighted"
            // rather than to a specific item. attachRouteMatched below
            // still fires in time for every real route, cold load included.
            selectedKey: "",
            // The header's user menu always has this one entry, so it carries a
            // placeholder until whoami answers (and keeps it if that call
            // fails) rather than opening onto an empty menu.
            userLabel: this.text("userUnknown"),
            // The finished runs behind the bell and the text of its badge
            // ("" = no badge).
            notifications: [],
            notificationBadge: ""
        });
        this.setModel(model, "appView");

        this.getRouter().attachRouteMatched((event: Router$RouteMatchedEvent) => {
            const name = (event.getParameter("name") as string) || "";
            this.shownKey = NAV_KEY_BY_ROUTE[name] ?? "";
            model.setProperty("/selectedKey", this.shownKey);
        });

        // routeMatched never fires for the bypassed target, so notFound
        // would otherwise leave whichever nav item was selected before it.
        // sap.tnt.SideNavigation#setSelectedKey("") is a no-op for clearing
        // the rendered highlight (it only reacts to keys that match an
        // item), so the control has to be told directly via
        // setSelectedItem(""); the model property is kept in sync too so
        // any future binding-driven reads stay consistent.
        this.getRouter().attachBypassed((_event: Router$BypassedEvent) => {
            this.shownKey = "";
            model.setProperty("/selectedKey", "");
            (this.byId("sideNavigation") as SideNavigation).setSelectedItem("");
        });

        void this.loadUser();

        // The bell is part of the header: without one there is nothing to
        // show a count on and no list to mark read.
        if (model.getProperty("/showHeader")) {
            this.startNotifications();
        }
    }

    public onExit(): void {
        this.notificationsActive = false;
        if (this.notificationTimer !== undefined) {
            clearTimeout(this.notificationTimer);
            this.notificationTimer = undefined;
        }
        document.removeEventListener("visibilitychange", this.onVisibilityChange);
    }

    private static isInShell(): boolean {
        return typeof (window as { sap?: { ushell?: unknown } }).sap?.ushell !== "undefined";
    }

    private async loadUser(): Promise<void> {
        const who = await this.run(
            this.getAdminService().whoami(),
            "Could not read the signed-in user."
        );
        if (who) {
            // Locally there is no XSUAA, so the token carries neither email nor
            // user_name and the label comes back empty; the opaque principal is
            // still better than a blank menu entry.
            const label = who.label || who.principal || this.text("userUnknown");
            (this.getModel("appView") as JSONModel).setProperty("/userLabel", label);
        }
    }

    /** One poll now, then one every `notificationPollMs` while the page is
     *  visible, and one as soon as it becomes visible again. */
    private startNotifications(): void {
        this.notificationsActive = true;
        document.addEventListener("visibilitychange", this.onVisibilityChange);
        void this.pollNotifications();
        this.armNotificationTimer();
    }

    /** A chain of timeouts, armed after the poll has answered, so a slow
     *  backend never has two timer polls out at once. */
    private armNotificationTimer(): void {
        this.notificationTimer = setTimeout(() => {
            this.notificationTimer = undefined;
            const poll = document.visibilityState === "hidden" ? Promise.resolve() : this.pollNotifications();
            void poll.then(() => {
                if (this.notificationsActive) {
                    this.armNotificationTimer();
                }
            });
        }, App.notificationPollMs);
    }

    /**
     * Reads the finished runs, shows them and the unread count, and
     * announces the runs that were not in the previous poll. A poll that
     * fails is silent: it runs in the background, the next one will do.
     * Never rejects.
     */
    public async pollNotifications(): Promise<void> {
        const seq = ++this.pollSeq;
        let list: NotificationList;
        try {
            list = await this.getAdminService().getNotifications();
        } catch {
            return;
        }
        // A newer poll is out (or the list was just marked read, or the
        // shell is gone): this answer is older than what will be shown.
        if (seq !== this.pollSeq || !this.notificationsActive) {
            return;
        }
        const toast = toastKey(newlyFinished(this.knownKeys, list.items));
        this.knownKeys = new Set(list.items.map(itemKey));
        this.lastList = list;
        this.showNotifications();
        if (toast) {
            MessageToast.show(this.text(toast.key, toast.args), { closeOnBrowserNavigation: false });
        }
    }

    private showNotifications(): void {
        const list = this.lastList;
        if (!list) {
            return;
        }
        const model = this.getModel("appView") as JSONModel;
        model.setProperty("/notifications", list.items.map((item) => ({
            ...item, unread: item.unread || this.openUnreadKeys.has(itemKey(item))
        })));
        // The server's count, not the number of unread items: the list is
        // capped, the count is not.
        model.setProperty("/notificationBadge", badgeText(list.unread_count));
    }

    /**
     * Opens the list of finished runs (or closes it when it is open).
     * Opening is what marks the runs read: the marker moves to the newest
     * run that is *shown*, so a run that finished since the last poll stays
     * unread.
     */
    public async onOpenNotifications(): Promise<void> {
        const popover = await this.getNotificationsPopover();
        if (popover.isOpen()) {
            popover.close();
            return;
        }
        const items = (this.getModel("appView") as JSONModel).getProperty("/notifications") as NotificationItem[];
        this.openUnreadKeys = new Set(items.filter((item) => item.unread).map(itemKey));
        popover.openBy(this.byId("notificationBell") as Button);

        const upTo = newestFinishedAt(items);
        if (upTo !== null && this.lastList && this.lastList.unread_count > 0) {
            await this.markNotificationsSeen(upTo);
        } else {
            await this.pollNotifications();
        }
    }

    /** Moves the read marker, then reads the list again for the count that
     *  is left. Silent on failure, as a poll is: the badge then stays. */
    private async markNotificationsSeen(upTo: string): Promise<void> {
        // An answer that is on its way was made before the marker moved.
        this.pollSeq++;
        try {
            // Exactly the string the server sent: it accepts nothing else.
            await this.getAdminService().markNotificationsSeen(upTo);
        } catch {
            return;
        }
        await this.pollNotifications();
    }

    private getNotificationsPopover(): Promise<ResponsivePopover> {
        if (!this.notificationsPopover) {
            // loadFragment makes it a dependent of the view: it gets the
            // view's models and is destroyed with it.
            this.notificationsPopover = this.loadFragment({
                name: "com.agent.admin.fragment.NotificationsPopover"
            }) as Promise<ResponsivePopover>;
        }
        return this.notificationsPopover;
    }

    /** The list is closed: what was new when it opened no longer is. */
    public onNotificationsClosed(): void {
        this.openUnreadKeys = new Set();
        this.showNotifications();
    }

    /** Opens the detail page of the pressed run, unless the page that is
     *  shown objects (unsaved changes), as for the side navigation. */
    public async onNotificationPress(event: ListBase$ItemPressEvent): Promise<void> {
        const context = event.getParameter("listItem")?.getBindingContext("appView");
        const item = context?.getObject() as NotificationItem | undefined;
        (await this.getNotificationsPopover()).close();
        if (!item || !(await this.getOwnerComponentTyped().canLeave())) {
            return;
        }
        this.getRouter().navTo(item.kind === "workflow" ? "workflowRunDetail" : "runDetail", { runId: item.run_id });
    }

    public onToggleSideNav(): void {
        const model = this.getModel("appView") as JSONModel;
        model.setProperty("/sideExpanded", !model.getProperty("/sideExpanded"));
    }

    /**
     * Navigates to the pressed item, unless the page that is shown objects
     * (a form with unsaved changes asks first, `Component.canLeave`). The
     * control has already highlighted the pressed item by then, so a "no"
     * puts the highlight back on the page that stays.
     */
    public async onNavItemSelect(event: SideNavigation$ItemSelectEvent): Promise<void> {
        const key = (event.getParameter("item") as NavigationListItem).getKey();
        if (await this.getOwnerComponentTyped().canLeave()) {
            this.getRouter().navTo(key);
            return;
        }
        (this.getModel("appView") as JSONModel).setProperty("/selectedKey", this.shownKey);
        (this.byId("sideNavigation") as SideNavigation).setSelectedKey(this.shownKey);
    }
}
