import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type Button from "sap/m/Button";
import type ObjectListItem from "sap/m/ObjectListItem";
import type ResponsivePopover from "sap/m/ResponsivePopover";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import {
    iPollAfter, iSeeTheBadge, iSeeTheItems, iStartTheAppWithNotifications, textsOf, toastShows,
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
            trigger: "scheduler", created_by: null, finished_at: NEWEST
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
            Opa5.assert.strictEqual((bell as Button).getTooltip(), "Notifications", "the bell is named for a screen reader");
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
            attributes: ["Workflow run", "scheduler", new Date(NEWEST).toLocaleString()],
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
    // Everything is read at the start.
    iStartTheAppWithNotifications(Given, finishedRuns(), NEWEST);

    iSeeTheBadge(Then, "", "nothing is unread, so there is no badge");

    iPollAfter(When, function () {
        backend.notifications.unshift({
            kind: "agent", run_id: "run-9", name: "nightly-sync", status: "failed",
            trigger: "scheduler", created_by: null, finished_at: "2026-08-24T11:00:00+00:00"
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
            Opa5.assert.strictEqual(
                Array.from(document.querySelectorAll(".sapMMessageToast"))
                    .filter((toast) => toast.textContent === "nightly-sync finished: failed").length <= 1,
                true, "several runs in one poll are one toast, and the earlier run is not announced again"
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
