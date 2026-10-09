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

    /** Between two polls for finished runs. Static, so a test can shorten it. */
    public static notificationPollMs = 15000;
    /**
     * Without pointer or key input for this long, the polling stops until
     * the next input. A poll is a request with the user's session: an
     * unattended screen that kept asking would keep a session alive that is
     * meant to end when nobody uses it.
     */
    public static notificationIdleMs = 600000;

    /** False once the shell is gone (and inside a host shell, where there
     *  is no bell): an answer that arrives then is dropped. */
    private notificationsActive = false;
    private notificationTimer?: ReturnType<typeof setTimeout>;
    /** A poll of the chain is out; the next timer is armed when it answers. */
    private chainPolling = false;
    /** The last poll was answered 401 or 403: no timer until the next input. */
    private pollRefused = false;
    private lastInputAt = 0;
    private lastPollAt = 0;
    /** Counts the polls, so that only the answer of the latest one is shown. */
    private pollSeq = 0;
    /** The runs of the last poll that was answered; `null` until one is,
     *  so the first successful poll announces nothing. */
    private knownKeys: Set<string> | null = null;
    private lastList?: NotificationList;
    /** The runs that were unread when the list was opened: they keep their
     *  mark while it is open, although opening it marked them read. */
    private openUnreadKeys = new Set<string>();
    private notificationsPopover?: Promise<ResponsivePopover>;

    /** A tab that is hidden does not poll; one that is shown again does at once. */
    private readonly onVisibilityChange = (): void => {
        if (document.visibilityState === "hidden") {
            this.clearNotificationTimer();
        } else {
            this.resumeNotifications();
        }
    };

    /** Pointer or key input: the page is in use, so a chain that stopped
     *  (idle, or refused) starts again. */
    private readonly onUserInput = (): void => {
        const now = Date.now();
        this.lastInputAt = now;
        if (this.pollRefused) {
            // Not one request per key press while the session is gone.
            if (now - this.lastPollAt < App.notificationPollMs) {
                return;
            }
            this.pollRefused = false;
        }
        this.resumeNotifications();
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
            // The finished runs behind the bell, the text of its badge
            // ("" = no badge) and its name, which carries the count.
            notifications: [],
            notificationBadge: "",
            notificationTooltip: this.text("notificationsTooltip")
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
        this.clearNotificationTimer();
        document.removeEventListener("visibilitychange", this.onVisibilityChange);
        document.removeEventListener("pointerdown", this.onUserInput, true);
        document.removeEventListener("keydown", this.onUserInput, true);
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

    /**
     * One poll now, then one every `notificationPollMs` for as long as the
     * page is visible, in use and not refused (`mayPoll`). Whatever stops
     * the chain, `resumeNotifications` starts it again with one poll.
     */
    private startNotifications(): void {
        this.notificationsActive = true;
        this.lastInputAt = Date.now();
        document.addEventListener("visibilitychange", this.onVisibilityChange);
        // Capturing and passive: only the time of the input is noted, and a
        // control that stops the event must not hide it.
        document.addEventListener("pointerdown", this.onUserInput, { capture: true, passive: true });
        document.addEventListener("keydown", this.onUserInput, { capture: true, passive: true });
        this.resumeNotifications();
    }

    /** The one rule for "should the chain run". */
    private mayPoll(): boolean {
        return this.notificationsActive
            && !this.pollRefused
            && document.visibilityState !== "hidden"
            && Date.now() - this.lastInputAt < App.notificationIdleMs;
    }

    /** Starts a chain that is stopped: one poll at once, then the timer.
     *  Does nothing while a timer is armed or a chain poll is out. */
    private resumeNotifications(): void {
        if (this.notificationTimer !== undefined || this.chainPolling || !this.mayPoll()) {
            return;
        }
        void this.pollInChain();
    }

    /** One poll of the chain; the next timer is armed only after it has
     *  answered, so a slow backend never has two chain polls out. */
    private async pollInChain(): Promise<void> {
        this.chainPolling = true;
        await this.pollNotifications();
        this.chainPolling = false;
        this.armNotificationTimer();
    }

    /** Arms the next poll, or none: a stopped chain has no timer at all. */
    private armNotificationTimer(): void {
        this.clearNotificationTimer();
        if (!this.mayPoll()) {
            return;
        }
        this.notificationTimer = setTimeout(() => {
            this.notificationTimer = undefined;
            // The idle limit may have passed while the timer ran.
            if (this.mayPoll()) {
                void this.pollInChain();
            }
        }, App.notificationPollMs);
    }

    private clearNotificationTimer(): void {
        if (this.notificationTimer !== undefined) {
            clearTimeout(this.notificationTimer);
            this.notificationTimer = undefined;
        }
    }

    /** 401 or 403: the session is gone, or the user may not read the list.
     *  Asking again every 15 s would change nothing, so the chain stops. */
    private notePollFailure(error: unknown): void {
        const status = (error as { status?: unknown } | null)?.status;
        if (status === 401 || status === 403) {
            this.pollRefused = true;
            this.clearNotificationTimer();
        }
    }

    /**
     * Reads the finished runs, shows them and the unread count, and
     * announces the runs that were not in the previous answer. A poll that
     * fails is silent: it runs in the background, and the next one will do
     * (after a 401 or 403 there is no next one until the page is used
     * again). Never rejects.
     */
    public async pollNotifications(): Promise<void> {
        const seq = ++this.pollSeq;
        this.lastPollAt = Date.now();
        let list: NotificationList;
        try {
            list = await this.getAdminService().getNotifications();
        } catch (error) {
            this.notePollFailure(error);
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
        const badge = badgeText(list.unread_count);
        model.setProperty("/notificationBadge", badge);
        // The badge's own count does not reliably reach a screen reader
        // (the button is not re-described when the badge arrives after it
        // was rendered), so the bell's name carries it.
        model.setProperty("/notificationTooltip", badge
            ? this.text("notificationsTooltipUnread", [badge])
            : this.text("notificationsTooltip"));
    }

    /**
     * Opens the list of finished runs (or closes it when it is open).
     * Opening is what marks read what the list shows: it is read fresh
     * first, so a run that finished since the last poll is in it and is
     * marked too. A run that arrives while the list is open is not.
     */
    public async onOpenNotifications(): Promise<void> {
        let popover: ResponsivePopover;
        try {
            popover = await this.getNotificationsPopover();
        } catch {
            // The list could not be loaded; the next press tries again.
            return;
        }
        if (popover.isOpen()) {
            popover.close();
            return;
        }
        this.openUnreadKeys = new Set();
        this.keepUnreadMarks();
        popover.openBy(this.byId("notificationBell") as Button);

        await this.pollNotifications();
        // The fresh list, or the one that was already shown when the poll
        // failed or the popover was closed again meanwhile.
        const list = this.lastList;
        if (!list || !popover.isOpen()) {
            return;
        }
        this.keepUnreadMarks();
        const upTo = newestFinishedAt(list.items);
        if (upTo !== null && (list.unread_count > 0 || list.items.some((item) => item.unread))) {
            await this.markNotificationsSeen(upTo);
        }
    }

    /** The unread runs of the list that is shown keep their mark until the
     *  list is closed. */
    private keepUnreadMarks(): void {
        (this.lastList?.items ?? []).filter((item) => item.unread)
            .forEach((item) => this.openUnreadKeys.add(itemKey(item)));
    }

    /** Moves the read marker, then reads the list again for the count that
     *  is left. Silent on failure, as a poll is: the badge then stays. */
    private async markNotificationsSeen(upTo: string): Promise<void> {
        // An answer that is on its way was made before the marker moved.
        this.pollSeq++;
        try {
            // Exactly the string the server sent: it accepts nothing else.
            await this.getAdminService().markNotificationsSeen(upTo);
        } catch (error) {
            this.notePollFailure(error);
            return;
        }
        await this.pollNotifications();
    }

    private getNotificationsPopover(): Promise<ResponsivePopover> {
        if (!this.notificationsPopover) {
            // loadFragment makes it a dependent of the view: it gets the
            // view's models and is destroyed with it.
            const loading = (this.loadFragment({
                name: "com.agent.admin.fragment.NotificationsPopover"
            }) as Promise<ResponsivePopover>).catch((error: unknown) => {
                // Not kept: a failed load must not leave the bell dead.
                if (this.notificationsPopover === loading) {
                    this.notificationsPopover = undefined;
                }
                throw error;
            });
            this.notificationsPopover = loading;
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
        try {
            (await this.getNotificationsPopover()).close();
        } catch {
            // No list to close.
        }
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
