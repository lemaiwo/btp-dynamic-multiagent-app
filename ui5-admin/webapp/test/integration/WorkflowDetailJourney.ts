import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Button from "sap/m/Button";
import type Input from "sap/m/Input";
import type Select from "sap/m/Select";
import type Dialog from "sap/m/Dialog";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Workflow detail journey");

// FakeBackend.reset() creates the two seeded agents (ids 100/101) before the
// one seeded workflow, so the workflow always lands on id 102 -- see
// FakeBackend.ts's reset() and WorkflowJourney.ts's own copy of this constant.
const WORKFLOW_ID = 102;

opaTest(
    "editing an existing workflow shows its branches and steps populated, with each step's branch and agent preselected",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        Then.waitFor({
            id: "branchesTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const table = element as Table;
                Opa5.assert.strictEqual(table.getItems().length, 2, "both seeded branches are listed");
                const rows = table.getItems() as ColumnListItem[];
                const firstKey = (rows[0].getCells()[0] as Input).getValue();
                const secondKey = (rows[1].getCells()[0] as Input).getValue();
                Opa5.assert.deepEqual(
                    [firstKey, secondKey], ["billing", "support"],
                    "the branch keys are bound in the order the server returned them"
                );
            }
        });

        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const table = element as Table;
                Opa5.assert.strictEqual(table.getItems().length, 4, "all four seeded steps are listed");
                const rows = table.getItems() as ColumnListItem[];

                const mainLineRow = rows[0];
                Opa5.assert.strictEqual(
                    (mainLineRow.getCells()[0] as Select).getSelectedKey(), "",
                    "the fan-out step's branch select preselects 'Main line' (branch_key null)"
                );
                Opa5.assert.strictEqual(
                    (mainLineRow.getCells()[1] as Select).getSelectedKey(), "gmail-agent",
                    "the fan-out step's agent is preselected"
                );

                const billingRow = rows[1];
                Opa5.assert.strictEqual(
                    (billingRow.getCells()[0] as Select).getSelectedKey(), "billing",
                    "a branch step's branch is preselected"
                );
                Opa5.assert.strictEqual(
                    (billingRow.getCells()[1] as Select).getSelectedKey(), "btp-agent",
                    "a branch step's agent is preselected"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "a round trip preserves every step's agent, branch and per-group position, and every branch",
    function (Given: Common, When: Common, Then: Common) {
        // The single most valuable assertion in this task: if positions were
        // ever computed globally across the workflow instead of per group
        // (main line and each branch counted separately), this unchanged
        // save would silently reorder or renumber steps -- billing/support
        // would come back numbered 2/3/4 instead of each restarting at 1 --
        // and every later save of this workflow would then be rejected by
        // the server for a reason the operator did nothing to cause.
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        Then.waitFor({
            id: "workflowsTable",
            viewName: "Workflows",
            success: function () {
                const saved = backend.workflows.find((w) => w.id === WORKFLOW_ID);
                Opa5.assert.deepEqual(
                    saved?.steps,
                    [
                        {
                            branch_key: null, position: 1, agent_name: "gmail-agent",
                            instructions: "Read new mail.", fan_out: true, step_timeout_seconds: 600
                        },
                        {
                            branch_key: "billing", position: 1, agent_name: "btp-agent",
                            instructions: "Draft a billing reply.", fan_out: false, step_timeout_seconds: 600
                        },
                        {
                            branch_key: "support", position: 1, agent_name: "btp-agent",
                            instructions: "Draft a support reply.", fan_out: false, step_timeout_seconds: 600
                        },
                        {
                            branch_key: "support", position: 2, agent_name: "btp-agent",
                            instructions: "Send the reply.", fan_out: false, step_timeout_seconds: 600
                        }
                    ],
                    "every step's branch, agent, instructions and per-group position survive an unchanged save"
                );
                Opa5.assert.deepEqual(
                    saved?.branches,
                    [
                        { key: "billing", description: "Billing questions", position: 1 },
                        { key: "support", description: "Support requests", position: 2 }
                    ],
                    "every branch survives an unchanged save"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "a step whose agent no longer exists keeps that name and does not silently become a different agent",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp("workflows");

        // Injected after FakeBackend.reset() (queued inside iStartTheApp) but
        // before the row press below navigates to and loads the editor, so
        // GET /workflows/102 answers with this extra step already in place.
        When.waitFor({
            success: function () {
                backend.workflows[0].steps.push({
                    branch_key: null, position: 2, agent_name: "ghost-agent",
                    instructions: "Whoever last ran this agent has since been deleted.",
                    fan_out: false, step_timeout_seconds: 600
                });
            }
        });

        When.waitFor({
            id: "workflowsTable",
            viewName: "Workflows",
            matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
            actions: new Press()
        });

        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const table = element as Table;
                const rows = table.getItems() as ColumnListItem[];
                const ghostRow = rows[rows.length - 1];
                const agentSelect = ghostRow.getCells()[1] as Select;
                Opa5.assert.strictEqual(
                    agentSelect.getSelectedKey(), "ghost-agent",
                    "the missing agent's name stays selected instead of the browser defaulting to the first real agent"
                );
                const selectedItem = agentSelect.getSelectedItem();
                Opa5.assert.strictEqual(
                    selectedItem?.getEnabled(), false,
                    "the placeholder carrying the missing name cannot itself be (re-)selected, so a save is forced to fail rather than quietly keep it"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "a save rejected by validation shows the message and does not clear the form",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        When.waitFor({
            id: "workflowName",
            viewName: "WorkflowDetail",
            actions: new EnterText({ text: "triage-inbox-renamed" })
        });

        // Set only after the initial GET /workflows/102 (queued by
        // iStartTheApp, which loads the form above) has already gone
        // through: FakeBackend.failNext matches by path alone, regardless of
        // method, so setting it any earlier would fail that GET instead of
        // the PUT this test means to reject.
        When.waitFor({
            success: function () {
                backend.failNext = {
                    path: `workflows/${WORKFLOW_ID}`,
                    status: 400,
                    body: { detail: "Step 1 names agent 'gmail-agent', which does not exist or is disabled." }
                };
            }
        });

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        // AdminService.toError already turns both a plain-string 400 detail
        // (validate_workflow_parts) and a list-shaped 422 body into a single
        // readable AdminError.detail; ErrorHandler renders it via
        // MessageBox.error. Matched on the dialog's own rendered text, not a
        // specific content control, since which control carries the message
        // is MessageBox's implementation detail, not this app's.
        Then.waitFor({
            controlType: "sap.m.Dialog",
            searchOpenDialogs: true,
            matchers: function (element: UI5Element) {
                const dialog = element as Dialog;
                const text = dialog.getDomRef()?.textContent ?? "";
                return dialog.getTitle() === "Error"
                    && text.indexOf("Step 1 names agent 'gmail-agent'") !== -1;
            },
            success: function () {
                Opa5.assert.ok(true, "the validation message reached the operator as readable text");
            },
            errorMessage: "No 'Error' dialog containing the validation message appeared"
        });

        // Dismiss the error dialog: it is a modal blocking layer, so the form
        // underneath is not "Interactable" (and so not matchable) until it
        // closes -- the same reason MessageBox.error's own default action is
        // the only one offered. MessageBox.error renders a single action
        // button (its footer's own overflow-button clone is infrastructure,
        // always present but never visible with only one action), so
        // filtering to the visible one -- without also requiring exact
        // button text, which is resource-bundle text this app does not
        // control -- reaches exactly that button.
        When.waitFor({
            controlType: "sap.m.Button",
            searchOpenDialogs: true,
            matchers: function (element: UI5Element) { return (element as Button).getVisible(); },
            actions: new Press()
        });

        Then.waitFor({
            id: "workflowName",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const input = element as Input;
                Opa5.assert.strictEqual(
                    input.getValue(), "triage-inbox-renamed",
                    "the rejected save left the operator's unsaved edit in place instead of clearing or reloading the form"
                );
            }
        });

        Then.iStopTheApp();
    }
);
