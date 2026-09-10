import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Table from "sap/m/Table";
import type List from "sap/m/List";
import type Select from "sap/m/Select";
import type HBox from "sap/m/HBox";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Item from "sap/ui/core/Item";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type UI5Element from "sap/ui/core/Element";
import type ProcessFlow from "sap/suite/ui/commons/ProcessFlow";
import type { WorkflowStepRun } from "com/infrabel/agentadmin/service/types";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Workflow run journey");

// FakeBackend.reset() seeds exactly one workflow run, "wf-run-1", with one
// pre-fan-out step (step-1), one item that ran a step in the "billing"
// branch (item-1 / step-2), and one item skip_seen_items skipped before any
// step ran (item-2, no steps) -- see FakeBackend.ts's reset().
const RUN_ID = "wf-run-1";

opaTest(
    "the workflow runs list renders the seeded run and navigating into it opens the detail",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp("workflow-runs");

        Then.waitFor({
            id: "workflowRunsTable",
            viewName: "WorkflowRuns",
            success: function (element: UI5Element) {
                const table = element as Table;
                Opa5.assert.strictEqual(table.getItems().length, 1, "the seeded run is listed");
                const model = table.getModel("workflowRuns") as JSONModel;
                Opa5.assert.strictEqual(model.getProperty("/items/0/workflow_name"), "triage-inbox", "workflow name is bound");
                Opa5.assert.strictEqual(model.getProperty("/items/0/status"), "success", "status is bound");
                Opa5.assert.strictEqual(model.getProperty("/items/0/items_total"), 2, "item counts are bound");
            }
        });

        When.waitFor({
            id: "workflowRunsTable",
            viewName: "WorkflowRuns",
            matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
            actions: new Press()
        });

        Then.waitFor({
            check: function () { return HashChanger.getInstance().getHash() === `workflow-runs/${RUN_ID}`; },
            success: function () {
                Opa5.assert.ok(true, "the row press navigated to the run detail route");
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "the run detail shows pre-fan-out steps separately and groups the rest under their item",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflow-runs/${RUN_ID}`);

        Then.waitFor({
            id: "preFanOutStepsList",
            viewName: "WorkflowRunDetail",
            success: function (element: UI5Element) {
                const list = element as List;
                Opa5.assert.strictEqual(list.getItems().length, 1, "only the one step that ran before the fan-out is listed here");
                const step = list.getItems()[0].getBindingContext("run")?.getObject() as WorkflowStepRun;
                Opa5.assert.strictEqual(step.id, "step-1", "it is the fan-out step itself, not one attributed to an item");
                Opa5.assert.strictEqual(step.item_run_id, null, "its item_run_id is null, which is why it lands here");
            }
        });

        Then.waitFor({
            id: "itemsList",
            viewName: "WorkflowRunDetail",
            success: function (element: UI5Element) {
                const list = element as List;
                Opa5.assert.strictEqual(list.getItems().length, 2, "both seeded items are listed");
                const model = list.getModel("run") as JSONModel;
                Opa5.assert.strictEqual(model.getProperty("/items/0/item_key"), "msg-1", "item-1 is first, in server order");
                Opa5.assert.strictEqual(model.getProperty("/items/0/steps").length, 1, "item-1 has exactly its own step, step-2");
                Opa5.assert.strictEqual(model.getProperty("/items/0/steps/0/id"), "step-2", "and not the pre-fan-out step");
                Opa5.assert.strictEqual(model.getProperty("/items/1/item_key"), "msg-2", "item-2, which was skipped");
                Opa5.assert.strictEqual(model.getProperty("/items/1/steps").length, 0, "a skipped item ran no steps");
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "a step's output text is actually rendered, not just present in the model",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflow-runs/${RUN_ID}`);

        // Polled via `check` (not a one-shot `success`) because the DOM only
        // carries this text once the async load has both landed and been
        // bound -- see RunReportJourney's identical reasoning for its own
        // `check` function.
        Then.waitFor({
            check: function () {
                return Array.from(document.querySelectorAll(".workflowStepOutput"))
                    .some(function (el) { return (el.textContent ?? "").indexOf("Drafted a billing reply.") !== -1; });
            },
            success: function () {
                Opa5.assert.ok(true, "the per-item step's output text is rendered in the DOM");
            }
        });

        Then.waitFor({
            check: function () {
                return Array.from(document.querySelectorAll(".workflowStepOutput"))
                    .some(function (el) { return (el.textContent ?? "").indexOf("Found 2 items.") !== -1; });
            },
            success: function () {
                Opa5.assert.ok(true, "the pre-fan-out step's output text is rendered in the DOM too");
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "the run flow highlights the path the selected item took, and switching items switches the path",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflow-runs/${RUN_ID}`);

        Then.waitFor({
            id: "runFlow",
            viewName: "WorkflowRunDetail",
            // The flow needs both the run and the workflow definition, which
            // are two round trips -- poll rather than assert on first match.
            check: function (element: UI5Element) {
                return (element as ProcessFlow).getNodes().length > 0;
            },
            success: function (element: UI5Element) {
                const byId = new Map((element as ProcessFlow).getNodes().map((n) => [n.getNodeId(), n]));
                Opa5.assert.strictEqual(byId.size, 4, "every step of the definition is drawn, not only the ones that ran");
                Opa5.assert.strictEqual(
                    byId.get("main-0")?.getState(), "Positive",
                    "the pre-fan-out step took its state from the run-level step run"
                );
                Opa5.assert.strictEqual(
                    byId.get("billing-0")?.getState(), "Positive",
                    "item-1's billing step succeeded"
                );
                Opa5.assert.ok(byId.get("billing-0")?.getHighlighted(), "and is on the highlighted path");
                Opa5.assert.strictEqual(
                    byId.get("support-0")?.getState(), "Planned",
                    "the branch this item never entered stays planned"
                );
                Opa5.assert.notOk(byId.get("support-0")?.getHighlighted(), "and off the highlighted path");
            }
        });

        // Driven through a real change event rather than the dropdown: a
        // programmatic setSelectedKey alone does not write back through the
        // two-way binding -- same reason WorkflowDetailJourney arranges its
        // step rows through the model.
        When.waitFor({
            id: "runItemSelect",
            viewName: "WorkflowRunDetail",
            success: function (element: UI5Element) {
                const select = element as Select;
                select.setSelectedKey("item-2");
                select.fireChange({ selectedItem: select.getSelectedItem() as Item });
            }
        });

        Then.waitFor({
            id: "runFlow",
            viewName: "WorkflowRunDetail",
            check: function (element: UI5Element) {
                const byId = new Map((element as ProcessFlow).getNodes().map((n) => [n.getNodeId(), n]));
                return byId.get("billing-0")?.getState() === "Planned";
            },
            success: function (element: UI5Element) {
                const byId = new Map((element as ProcessFlow).getNodes().map((n) => [n.getNodeId(), n]));
                Opa5.assert.notOk(
                    byId.get("billing-0")?.getHighlighted(),
                    "item-2 was skipped before any branch ran, so no branch is highlighted for it"
                );
                Opa5.assert.strictEqual(
                    byId.get("main-0")?.getState(), "Positive",
                    "the step that ran once for the whole run keeps its state whichever item is selected"
                );
            }
        });

        Then.waitFor({
            id: "runFlowLegend",
            viewName: "WorkflowRunDetail",
            success: function (element: UI5Element) {
                const labels = (element as HBox).getItems()
                    .map((item) => (item as ObjectStatus).getText());
                Opa5.assert.deepEqual(
                    labels, ["Succeeded", "Failed", "Running", "Not run"],
                    "the legend names every state the diagram can draw"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "an item's selected branches are shown",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflow-runs/${RUN_ID}`);

        Then.waitFor({
            check: function () {
                return Array.from(document.querySelectorAll(".workflowItemBranches"))
                    .some(function (el) { return (el.textContent ?? "").indexOf("billing") !== -1; });
            },
            success: function () {
                Opa5.assert.ok(true, "item-1's selected branch, billing, is rendered");
            }
        });

        Then.iStopTheApp();
    }
);
