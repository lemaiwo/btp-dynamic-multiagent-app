import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import Poller from "com/agent/admin/model/autoRefresh";
import type List from "sap/m/List";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Text from "sap/m/Text";
import type ToggleButton from "sap/m/ToggleButton";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

/**
 * The poll interval these journeys run with. Above 1000 ms on purpose:
 * OPA5's autoWait treats a pending timer of up to 1000 ms as execution flow
 * it has to wait for, so a faster poll chain would leave every waitFor
 * blocked until the run finished.
 */
const TEST_INTERVAL_MS = 1100;

QUnit.module("Run auto-refresh journey", {
    beforeEach: function () {
        Poller.intervalMs = TEST_INTERVAL_MS;
        Poller.slowIntervalMs = TEST_INTERVAL_MS * 2;
    },
    afterEach: function () {
        Poller.intervalMs = 3000;
        Poller.slowIntervalMs = 10000;
    }
});

// FakeBackend.reset() seeds run-2 (a job run) and wf-run-2 (a workflow run)
// as still running; finishRun / finishWorkflowRun flip them between two
// polls the way the runner would.

opaTest("a live job run auto-refreshes until it finishes, then stops", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("runs/run-2");

    Then.waitFor({
        id: "autoRefreshRunToggle",
        viewName: "RunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.ok((element as ToggleButton).getPressed(), "auto-refresh starts on");
        }
    });
    Then.waitFor({
        id: "runAutoRefreshStatus",
        viewName: "RunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual(
                (element as Text).getText(false), "Auto-refreshing every 1.1 s",
                "the status line names the interval while the run is live"
            );
        }
    });

    When.waitFor({
        id: "runStatus",
        viewName: "RunDetail",
        success: function () { backend.finishRun("run-2"); }
    });

    Then.waitFor({
        id: "runStatus",
        viewName: "RunDetail",
        check: function (element: UI5Element) { return (element as ObjectStatus).getText() === "success"; },
        success: function () {
            Opa5.assert.ok(true, "the page picked up the finished status without anyone pressing Refresh");
        },
        errorMessage: "the run detail never refreshed itself"
    });
    Then.waitFor({
        id: "runAutoRefreshStatus",
        viewName: "RunDetail",
        check: function (element: UI5Element) {
            return (element as Text).getText(false).indexOf("Finished, auto-refresh stopped") === 0;
        },
        success: function (element: UI5Element) {
            Opa5.assert.ok(
                /Last refreshed \d\d:\d\d:\d\d$/.test((element as Text).getText(false)),
                "and says when it last refreshed"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("with auto-refresh off, the Refresh button still reloads the run", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("runs/run-2");

    When.waitFor({ id: "autoRefreshRunToggle", viewName: "RunDetail", actions: new Press() });

    Then.waitFor({
        id: "runAutoRefreshStatus",
        viewName: "RunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.ok(
                (element as Text).getText(false).indexOf("Auto-refresh off") === 0,
                "the status line says it is off"
            );
        }
    });

    When.waitFor({
        id: "runStatus",
        viewName: "RunDetail",
        success: function () { backend.finishRun("run-2"); }
    });
    When.waitFor({ id: "refreshRunDetailButton", viewName: "RunDetail", actions: new Press() });

    Then.waitFor({
        id: "runStatus",
        viewName: "RunDetail",
        check: function (element: UI5Element) { return (element as ObjectStatus).getText() === "success"; },
        success: function () {
            Opa5.assert.ok(true, "a manual refresh fetched the new status");
        }
    });
    Then.waitFor({
        id: "autoRefreshRunToggle",
        viewName: "RunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.notOk((element as ToggleButton).getPressed(), "a manual refresh does not switch auto-refresh back on");
        }
    });

    Then.iStopTheApp();
});

opaTest("navigating back from a live run stops the polling", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("runs/run-2");

    Then.waitFor({
        id: "runAutoRefreshStatus",
        viewName: "RunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.ok((element as Text).getText(false).indexOf("Auto-refreshing") === 0, "polling while on the page");
        }
    });

    // The Page's own back button.
    When.waitFor({ id: "runDetailPage-navButton", viewName: "RunDetail", actions: new Press() });

    let requestsAfterLeaving = -1;
    let leftAt = 0;
    Then.waitFor({
        id: "runsTable",
        viewName: "Runs",
        success: function () {
            requestsAfterLeaving = backend.countRequests("GET runs/run-2");
            leftAt = Date.now();
        }
    });
    // Wait past two intervals, then check nothing more was fetched.
    Then.waitFor({
        check: function () { return Date.now() - leftAt > TEST_INTERVAL_MS * 2 + 200; },
        success: function () {
            Opa5.assert.strictEqual(
                backend.countRequests("GET runs/run-2"), requestsAfterLeaving,
                "no further poll of the run after leaving the page"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("a live workflow run auto-refreshes its status, items and steps until it finishes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("workflow-runs/wf-run-2");

    Then.waitFor({
        id: "autoRefreshWorkflowRunToggle",
        viewName: "WorkflowRunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.ok((element as ToggleButton).getPressed(), "auto-refresh starts on");
        }
    });
    Then.waitFor({
        id: "workflowRunAutoRefreshStatus",
        viewName: "WorkflowRunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as Text).getText(false), "Auto-refreshing every 1.1 s");
        }
    });
    Then.waitFor({
        id: "itemsList",
        viewName: "WorkflowRunDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as List).getItems().length, 0, "no items yet while the fan-out step runs");
        }
    });

    When.waitFor({
        id: "runStatus",
        viewName: "WorkflowRunDetail",
        success: function () { backend.finishWorkflowRun("wf-run-2"); }
    });

    Then.waitFor({
        id: "runStatus",
        viewName: "WorkflowRunDetail",
        check: function (element: UI5Element) { return (element as ObjectStatus).getText() === "success"; },
        success: function () {
            Opa5.assert.ok(true, "the page picked up the finished status");
        },
        errorMessage: "the workflow run detail never refreshed itself"
    });
    Then.waitFor({
        id: "itemsList",
        viewName: "WorkflowRunDetail",
        success: function (element: UI5Element) {
            const list = element as List;
            Opa5.assert.strictEqual(list.getItems().length, 1, "the item produced since the last poll is listed");
            const steps = (list.getItems()[0].getBindingContext("run")?.getObject() as { steps: unknown[] }).steps;
            Opa5.assert.strictEqual(steps.length, 1, "with its step run nested under it");
        }
    });
    Then.waitFor({
        id: "workflowRunAutoRefreshStatus",
        viewName: "WorkflowRunDetail",
        check: function (element: UI5Element) {
            return (element as Text).getText(false).indexOf("Finished, auto-refresh stopped") === 0;
        },
        success: function () {
            Opa5.assert.ok(true, "and the status line says it stopped");
        }
    });

    Then.iStopTheApp();
});
