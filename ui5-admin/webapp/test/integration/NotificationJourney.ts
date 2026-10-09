import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type Button from "sap/m/Button";
import type ObjectListItem from "sap/m/ObjectListItem";
import type ResponsivePopover from "sap/m/ResponsivePopover";
import type UI5Element from "sap/ui/core/Element";
import MessageToast from "sap/m/MessageToast";
import App from "com/agent/admin/controller/App.controller";
import Common, { backend } from "./pages/Common";
import {
    controllerOf, iPollAfter, iSeeNoPollFor, iSeeTheBadge, iSeeTheItems, iStartTheAppWithNotifications, iWait,
    polls, restoreVisibility, setVisibility, textsOf, toastShows, userInput,
    type StoredNotification
} from "./pages/Notifications";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Notification journey");

// The read marker sits between the second and the third run: two unread.
const SEEN_AT = "2026-08-24T09:30:00+00:00";
const NEWEST = "2026-08-24T10:05:00+00:00";

// run-1 and wf-run-1 are runs the fake backend can show a detail page for.
function finishedRuns(): StoredNotification[] {
    return [
        {
            kind: "workflow", run_id: "wf-run-1", name: "triage-inbox", status: "partial",
            trigger: "schedule", created_by: null, finished_at: NEWEST
        },
        {
            kind: "agent", run_id: "run-1", name: "btp-agent", status: "success",
            trigger: "manual", created_by: "tester", finished_at: "2026-08-24T10:01:30+00:00"
        },
        {
            kind: "agent", run_id: "run-3", name: "gmail-agent", status: "failed",
            trigger: "manual", created_by: "tester", finished_at: "2026-08-23T10:00:10+00:00"
        }
    ];
}

function iOpenTheBell(When: Common): void {
    When.waitFor({ id: "notificationBell", viewName: "App", actions: new Press() });
}

function iPressTheItem(When: Common, title: string): void {
    When.waitFor({
        controlType: "sap.m.ObjectListItem",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "title", value: title }),
        actions: new Press(),
        errorMessage: `No notification "${title}" in the open list`
    });
}

opaTest("the bell shows the unread count; opening it lists the runs and marks them read", function (Given: Common, When: Common, Then: Common) {
    iStartTheAppWithNotifications(Given, finishedRuns(), SEEN_AT);

    iSeeTheBadge(Then, "2", "the badge counts the two runs that finished after the read marker");
    Then.waitFor({
        id: "notificationBell",
        viewName: "App",
        success: function (bell: UI5Element) {
            Opa5.assert.strictEqual(
                (bell as Button).getTooltip(), "Notifications, 2 unread",
                "the bell's name carries the count for a screen reader"
            );
            Opa5.assert.strictEqual(
                document.querySelectorAll(".sapMMessageToast").length, 0,
                "the first poll shows no toast: these runs finished before the app was opened"
            );
        }
    });

    iOpenTheBell(When);

    iSeeTheItems(Then, 3, function (items: ObjectListItem[]) {
        Opa5.assert.deepEqual(textsOf(items[0]), {
            title: "triage-inbox",
            attributes: ["Workflow run", "schedule", new Date(NEWEST).toLocaleString()],
            status: "partial", statusState: "Warning", unread: true
        }, "the newest run comes first: a workflow run, with trigger, finish time and status");
        Opa5.assert.deepEqual(textsOf(items[1]), {
            title: "btp-agent",
            attributes: ["Agent run", "manual", new Date("2026-08-24T10:01:30+00:00").toLocaleString()],
            status: "success", statusState: "Success", unread: true
        }, "then the agent run, also unread");
        Opa5.assert.deepEqual(
            [textsOf(items[2]).title, textsOf(items[2]).statusState, textsOf(items[2]).unread],
            ["gmail-agent", "Error", false],
            "the run that finished before the marker is listed as read"
        );
    });

    // Opening the list is what marks it read.
    Then.waitFor({
        check: function () { return backend.requests.indexOf("POST notifications/seen") !== -1; },
        success: function () {
            Opa5.assert.deepEqual(
                backend.bodies["POST notifications/seen"], { up_to: NEWEST },
                "seen is sent with the newest finished_at, unchanged, and no other key"
            );
            Opa5.assert.strictEqual(backend.notificationsSeenAt, NEWEST, "and the backend accepted it");
        },
        errorMessage: "Opening the bell never sent notifications/seen"
    });
    iSeeTheBadge(Then, "", "after that the badge is gone");
    Then.waitFor({
        id: "notificationBell",
        viewName: "App",
        success: function (bell: UI5Element) {
            Opa5.assert.strictEqual((bell as Button).getTooltip(), "Notifications", "and so is the count in its name");
        }
    });
    iSeeTheItems(Then, 3, function (items: ObjectListItem[]) {
        Opa5.assert.deepEqual(
            items.map((item) => textsOf(item).unread), [true, true, false],
            "the list that is open still shows which runs were new"
        );
    });

    Then.iStopTheApp();
});

opaTest("an agent run opens the run page, a workflow run the workflow run page", function (Given: Common, When: Common, Then: Common) {
    iStartTheAppWithNotifications(Given, finishedRuns(), SEEN_AT);

    iOpenTheBell(When);
    iPressTheItem(When, "btp-agent");

    Then.waitFor({
        id: "runStatus",
        viewName: "RunDetail",
        success: function () {
            Opa5.assert.strictEqual(window.location.hash.replace(/^#\/?/, ""), "runs/run-1", "the agent run's detail page is shown");
        },
        errorMessage: "The run detail page did not open"
    });
    Then.waitFor({
        id: "notificationsPopover",
        viewName: "App",
        visible: false,
        check: function (popover: UI5Element) { return !(popover as ResponsivePopover).isOpen(); },
        success: function () { Opa5.assert.ok(true, "and the list is closed"); },
        errorMessage: "The notification list stayed open after an entry was pressed"
    });

    iOpenTheBell(When);
    iSeeTheItems(Then, 3, function (items: ObjectListItem[]) {
        Opa5.assert.deepEqual(
            items.map((item) => textsOf(item).unread), [false, false, false],
            "opened again, nothing is new any more"
        );
    });
    iPressTheItem(When, "triage-inbox");

    Then.waitFor({
        controlType: "sap.m.Page",
        viewName: "WorkflowRunDetail",
        success: function () {
            Opa5.assert.strictEqual(
                window.location.hash.replace(/^#\/?/, ""), "workflow-runs/wf-run-1",
                "the workflow run's detail page is shown"
            );
            Opa5.assert.strictEqual(
                backend.requests.filter((request) => request === "POST notifications/seen").length, 1,
                "opening a list without unread runs sends no second seen"
            );
        },
        errorMessage: "The workflow run detail page did not open"
    });

    Then.iStopTheApp();
});

opaTest("a poll that brings a newly finished run shows a toast and raises the badge", function (Given: Common, When: Common, Then: Common) {
    // Every toast of this test, in order: the DOM only holds the ones that
    // have not faded yet.
    const shown: string[] = [];
    const show = MessageToast.show;
    Given.waitFor({
        success: function () {
            MessageToast.show = function (text: string, options?: object): void {
                shown.push(text);
                show.call(MessageToast, text, options);
            };
        }
    });

    // Everything is read at the start.
    iStartTheAppWithNotifications(Given, finishedRuns(), NEWEST);

    iSeeTheBadge(Then, "", "nothing is unread, so there is no badge");

    iPollAfter(When, function () {
        backend.notifications.unshift({
            kind: "agent", run_id: "run-9", name: "nightly-sync", status: "failed",
            trigger: "schedule", created_by: null, finished_at: "2026-08-24T11:00:00+00:00"
        });
    });

    Then.waitFor({
        check: function () { return toastShows("nightly-sync finished: failed"); },
        success: function () { Opa5.assert.ok(true, "the toast names the run and its status"); },
        errorMessage: "No toast for the run that just finished"
    });
    iSeeTheBadge(Then, "1", "and the badge counts it");

    // The same list again: nothing new, so nothing is announced twice.
    iPollAfter(When, function () { /* unchanged */ });
    iPollAfter(When, function () {
        backend.notifications.unshift({
            kind: "workflow", run_id: "wf-run-8", name: "triage-inbox", status: "success",
            trigger: "manual", created_by: "tester", finished_at: "2026-08-24T11:05:00+00:00"
        }, {
            // The same id as the agent run above: another id space.
            kind: "workflow", run_id: "run-9", name: "triage-inbox", status: "success",
            trigger: "manual", created_by: "tester", finished_at: "2026-08-24T11:04:00+00:00"
        });
        // More unread runs than the capped list holds.
        backend.notificationsUnreadBeyondList = 120;
    });

    Then.waitFor({
        check: function () { return toastShows("2 runs finished"); },
        success: function () {
            MessageToast.show = show;
            Opa5.assert.deepEqual(
                shown, ["nightly-sync finished: failed", "2 runs finished"],
                "three polls, two toasts: several runs in one poll are one toast, and no run is announced twice"
            );
        },
        errorMessage: "No toast for the two runs that finished together"
    });
    iSeeTheBadge(Then, "99+", "the badge follows the server's count, which is larger than the list");

    Then.iStopTheApp();
});

opaTest("a poll that fails is silent", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", { path: "notifications", status: 500, body: { detail: "boom" } });

    Then.waitFor({
        id: "notificationBell",
        viewName: "App",
        check: function () { return backend.requests.indexOf("GET notifications") !== -1; },
        success: function () {
            Opa5.assert.strictEqual(
                document.querySelectorAll(".sapMDialog").length, 0, "no error dialog for a failed poll"
            );
        }
    });
    iSeeTheBadge(Then, "", "and no badge");

    iOpenTheBell(When);
    Then.waitFor({
        controlType: "sap.m.List",
        searchOpenDialogs: true,
        success: function (lists: UI5Element[]) {
            Opa5.assert.strictEqual(
                (lists[0] as unknown as { getNoDataText(): string }).getNoDataText(), "No finished runs in the last 7 days",
                "the empty list says so"
            );
            Opa5.assert.strictEqual(
                backend.requests.indexOf("POST notifications/seen"), -1, "an empty list marks nothing read"
            );
        }
    });

    Then.iStopTheApp();
});

/**
 * The interval the polling tests run with. Above 1000 ms on purpose: OPA5's
 * autoWait waits for a pending timer of up to 1000 ms, so a faster chain
 * would block every waitFor.
 */
const TEST_POLL_MS = 1100;
/** Longer than two test intervals: a chain that still ran would have asked by then. */
const QUIET_MS = 2500;

QUnit.module("Notification polling journey", {
    beforeEach: function () {
        App.notificationPollMs = TEST_POLL_MS;
    },
    afterEach: function () {
        App.notificationPollMs = 15000;
        App.notificationIdleMs = 600000;
        restoreVisibility();
    }
});

opaTest("the list is asked for again after each answer, but not while the tab is hidden", function (Given: Common, When: Common, Then: Common) {
    let before = 0;
    Given.iStartTheApp();

    Then.waitFor({
        check: function () { return polls() >= 3; },
        success: function () { Opa5.assert.ok(true, "the first poll is followed by another one after every answer"); },
        errorMessage: "The poll chain did not re-arm"
    });

    When.waitFor({ success: function () { setVisibility("hidden"); } });
    iSeeNoPollFor(Then, QUIET_MS, "a hidden tab makes no request");

    When.waitFor({
        success: function () {
            before = polls();
            setVisibility("visible");
            Opa5.assert.strictEqual(polls(), before + 1, "becoming visible polls at once");
        }
    });
    iWait(When, 600);
    Then.waitFor({
        success: function () { Opa5.assert.strictEqual(polls(), before + 1, "and only once"); }
    });
    Then.waitFor({
        check: function () { return polls() >= before + 2; },
        success: function () { Opa5.assert.ok(true, "after which the chain runs again"); },
        errorMessage: "The poll chain did not start again after the tab became visible"
    });

    Then.iStopTheApp();
});

opaTest("polling stops when nobody uses the page and starts again with the next input", function (Given: Common, When: Common, Then: Common) {
    let before = 0;
    // Two intervals fit in; the third timer finds the page idle.
    App.notificationIdleMs = 2600;
    Given.iStartTheApp();

    iWait(When, 4500);
    iSeeNoPollFor(Then, QUIET_MS, "without input for longer than the idle limit, nothing is requested any more");
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(polls() >= 2 && polls() <= 4, `it did poll while the page was in use (${polls()} polls)`);
        }
    });

    When.waitFor({
        success: function () {
            before = polls();
            userInput("keydown");
            Opa5.assert.strictEqual(polls(), before + 1, "a key press polls once, at once");
            userInput("keydown");
            userInput("pointerdown");
            Opa5.assert.strictEqual(polls(), before + 1, "further input does not poll again: the chain is running");
        }
    });
    Then.waitFor({
        check: function () { return polls() >= before + 2; },
        success: function () { Opa5.assert.ok(true, "and the chain runs again"); },
        errorMessage: "The poll chain did not start again after input"
    });

    Then.iStopTheApp();
});

opaTest("a poll that is refused as unauthorised stops the polling, silently", function (Given: Common, When: Common, Then: Common) {
    let before = 0;
    Given.iStartTheApp();

    When.waitFor({
        check: function () { return polls() >= 1; },
        success: function () {
            backend.failNext = { path: "notifications", status: 401, body: { detail: "Unauthorized" } };
        }
    });
    Then.waitFor({
        check: function () { return backend.failNext === undefined; },
        success: function () { Opa5.assert.ok(true, "a poll was answered 401"); }
    });
    iSeeNoPollFor(Then, QUIET_MS, "after a 401 nothing is requested any more");
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialog").length, 0, "and no dialog says so");
        }
    });

    // The next input tries once; a 403 stops it again.
    When.waitFor({
        success: function () {
            backend.failNext = { path: "notifications", status: 403, body: { detail: "Forbidden" } };
            before = polls();
            userInput("pointerdown");
            Opa5.assert.strictEqual(polls(), before + 1, "the next input polls once");
        }
    });
    iSeeNoPollFor(Then, QUIET_MS, "a 403 stops the polling as well");
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialog").length, 0, "again without a dialog");
        }
    });

    When.waitFor({
        success: function () {
            before = polls();
            userInput("pointerdown");
        }
    });
    Then.waitFor({
        check: function () { return polls() >= before + 2; },
        success: function () { Opa5.assert.ok(true, "once a poll is answered again, the chain runs again"); },
        errorMessage: "The poll chain did not start again after an authorised answer"
    });

    Then.iStopTheApp();
});

opaTest("closing the app stops the polling and removes its listeners", function (Given: Common, When: Common, Then: Common) {
    // Every listener the app puts on the document for the three events it
    // uses, minus the ones it takes away again.
    const types = ["visibilitychange", "pointerdown", "keydown"];
    const added: { type: string; listener: unknown }[] = [];
    const add = document.addEventListener;
    const remove = document.removeEventListener;
    Given.waitFor({
        success: function () {
            document.addEventListener = function (this: Document, type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions): void {
                if (types.indexOf(type) !== -1) {
                    added.push({ type, listener });
                }
                add.call(this, type, listener, options);
            } as typeof document.addEventListener;
            document.removeEventListener = function (this: Document, type: string, listener: EventListenerOrEventListenerObject, options?: boolean | EventListenerOptions): void {
                const index = added.findIndex((entry) => entry.type === type && entry.listener === listener);
                if (index !== -1) {
                    added.splice(index, 1);
                }
                remove.call(this, type, listener, options);
            } as typeof document.removeEventListener;
        }
    });
    Given.iStartTheApp();

    Then.waitFor({
        check: function () { return polls() >= 2; },
        success: function () {
            Opa5.assert.deepEqual(
                added.map((entry) => entry.type).sort(), ["keydown", "pointerdown", "visibilitychange"],
                "while it runs, the shell listens for visibility and for input"
            );
        },
        errorMessage: "The poll chain did not run"
    });

    // Not iStopTheApp(): the fake backend stays installed, so a request
    // after the teardown would still be counted.
    When.iTeardownMyUIComponent();
    Then.waitFor({
        success: function () {
            document.addEventListener = add;
            document.removeEventListener = remove;
            Opa5.assert.deepEqual(added.map((entry) => entry.type), [], "the listeners are gone with the shell");
            setVisibility("hidden");
            setVisibility("visible");
            userInput("pointerdown");
            userInput("keydown");
        }
    });
    iSeeNoPollFor(Then, QUIET_MS, "and nothing is requested any more, whatever happens on the page");
    Then.waitFor({ success: function () { backend.restore(); } });
});

QUnit.module("Notification list journey", {
    afterEach: function () {
        App.notificationPollMs = 15000;
    }
});

opaTest("opening the bell marks read a run that finished since the last poll", function (Given: Common, When: Common, Then: Common) {
    const JUST_NOW = "2026-08-24T11:00:00+00:00";
    // Everything is read, and the list the shell holds says so.
    iStartTheAppWithNotifications(Given, finishedRuns(), NEWEST);
    iSeeTheBadge(Then, "", "nothing is unread");

    // A run finishes; no poll has seen it yet.
    When.waitFor({
        id: "notificationBell",
        viewName: "App",
        success: function () {
            backend.notifications.unshift({
                kind: "agent", run_id: "run-9", name: "nightly-sync", status: "success",
                trigger: "schedule", created_by: null, finished_at: JUST_NOW
            });
        }
    });
    iOpenTheBell(When);

    iSeeTheItems(Then, 4, function (items: ObjectListItem[]) {
        Opa5.assert.deepEqual(
            [textsOf(items[0]).title, textsOf(items[0]).unread], ["nightly-sync", true],
            "the list that opens is fresh: the new run is in it, marked as new"
        );
    });
    Then.waitFor({
        check: function () { return backend.requests.indexOf("POST notifications/seen") !== -1; },
        success: function () {
            Opa5.assert.deepEqual(
                backend.bodies["POST notifications/seen"], { up_to: JUST_NOW },
                "and it is marked read: what the list shows is what opening it marks"
            );
        },
        errorMessage: "A run that was first shown by opening the bell was never marked read"
    });

    iOpenTheBell(When);
    Then.waitFor({
        id: "notificationsPopover",
        viewName: "App",
        visible: false,
        check: function (popover: UI5Element) { return !(popover as ResponsivePopover).isOpen(); },
        success: function () { Opa5.assert.ok(true, "the bell closes the list again"); }
    });
    iSeeTheBadge(Then, "", "no badge is left behind for a run the list has shown");

    Then.iStopTheApp();
});

opaTest("an answer that was on its way when the list was marked read does not bring the badge back", function (Given: Common, When: Common, Then: Common) {
    iStartTheAppWithNotifications(Given, finishedRuns(), SEEN_AT);
    iSeeTheBadge(Then, "2", "two runs are unread");

    // A poll goes out and is not answered yet: its answer says "2 unread".
    iPollAfter(When, function () { backend.holdNextNotifications = true; });
    iOpenTheBell(When);
    Then.waitFor({
        check: function () { return backend.requests.indexOf("POST notifications/seen") !== -1; },
        success: function () { Opa5.assert.ok(true, "meanwhile the list was opened and marked read"); }
    });
    iSeeTheBadge(Then, "", "the badge is gone");

    When.waitFor({ success: function () { backend.releaseNotifications(); } });
    iWait(When, 500);
    iSeeTheBadge(Then, "", "the late answer is dropped: the badge stays away");

    Then.iStopTheApp();
});

opaTest("a list that could not be loaded does not leave the bell dead", function (Given: Common, When: Common, Then: Common) {
    const unhandled: string[] = [];
    const onUnhandled = function (event: PromiseRejectionEvent): void {
        unhandled.push(String(event.reason));
    };
    Given.iStartTheApp();

    // The fragment cannot be loaded (a network failure), once.
    When.waitFor({
        id: "notificationBell",
        viewName: "App",
        success: function (bell: UI5Element) {
            window.addEventListener("unhandledrejection", onUnhandled);
            controllerOf(bell).loadFragment = function (): Promise<unknown> {
                return Promise.reject(new Error("fragment not loaded"));
            };
        }
    });
    iOpenTheBell(When);
    When.waitFor({
        id: "notificationBell",
        viewName: "App",
        success: function (bell: UI5Element) {
            // Back to the controller's own method.
            delete controllerOf(bell).loadFragment;
        }
    });
    iOpenTheBell(When);

    Then.waitFor({
        controlType: "sap.m.List",
        searchOpenDialogs: true,
        success: function () {
            window.removeEventListener("unhandledrejection", onUnhandled);
            Opa5.assert.ok(true, "the next press loads the list and opens it");
            Opa5.assert.deepEqual(unhandled, [], "and the failed load was handled");
        },
        errorMessage: "After a failed load the bell never opened the list"
    });

    Then.iStopTheApp();
});
