import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Button from "sap/m/Button";
import type Input from "sap/m/Input";
import type Select from "sap/m/Select";
import type Dialog from "sap/m/Dialog";
import type Text from "sap/m/Text";
import type UI5Element from "sap/ui/core/Element";
import type { WorkflowStep } from "com/infrabel/agentadmin/service/types";
import Common, { backend } from "./pages/Common";

/** The subset of a step-row model entry this file manipulates directly
 * (arrange only -- never asserted on) to set a freshly `onAddStep`'d row's
 * branch/agent without driving the row's `<Select>` controls, which do not
 * write back through their two-way binding when set programmatically
 * (only a genuine `change` event does). */
type StepRow = WorkflowStep & { agentOptions: unknown; branchOptions: unknown };

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
    "the API slug field shows a live scheduler-endpoint hint",
    function (Given: Common, When: Common, Then: Common) {
        // The seeded workflow's api_slug is "" (FakeBackend.makeWorkflow's
        // default, never overridden for triage-inbox), so the hint starts on
        // the placeholder rather than a real path -- an operator must not be
        // shown a path that would 404.
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        Then.waitFor({
            id: "workflowApiSlugHint",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Text).getText(false), "The scheduler posts to POST /api/workflows/<slug>/run.",
                    "with no slug entered yet, the hint falls back to a placeholder rather than an empty path"
                );
            }
        });

        When.waitFor({
            id: "workflowApiSlug",
            viewName: "WorkflowDetail",
            actions: new EnterText({ text: "triage-inbox" })
        });

        Then.waitFor({
            id: "workflowApiSlugHint",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Text).getText(false), "The scheduler posts to POST /api/workflows/triage-inbox/run.",
                    "the hint updates live as the slug is typed, not only after the field loses focus"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "a round trip preserves every step's agent, branch and per-group position, every branch, and every scalar field",
    function (Given: Common, When: Common, Then: Common) {
        // The single most valuable assertion in this task: if positions were
        // ever computed globally across the workflow instead of per group
        // (main line and each branch counted separately), this unchanged
        // save would silently reorder or renumber steps -- billing/support
        // would come back numbered 2/3/4 instead of each restarting at 1 --
        // and every later save of this workflow would then be rejected by
        // the server for a reason the operator did nothing to cause.
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        // Every scalar field is set to a value distinguishable from its seed
        // default before saving -- deliberately not re-saved unchanged, only
        // for these fields. Steps and branches are left untouched, so the
        // per-group position assertion below still exercises exactly the
        // "unchanged" path it always has. This matters because FakeBackend's
        // PUT merges `{...existing, ...body}`: a field the controller's
        // payload stopped sending would keep whatever the record already
        // had, and an *unchanged* round trip of that same field could never
        // tell the difference -- it would show the right value either way.
        // Changing it first means a dropped field comes back stale instead.
        //
        // Set directly on the model, the same way the step-row tests in this
        // file set branch_key/agent_name: a <Select>'s or <Switch>'s bound
        // property does not write back through two-way binding from a
        // programmatic control change, only a genuine user gesture does, and
        // onSave() reads only the model, never the controls themselves.
        When.waitFor({
            id: "workflowName",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const model = (element as Input).getModel("workflow") as JSONModel;
                model.setProperty("/data/name", "triage-inbox-edited");
                model.setProperty("/data/description", "Updated description.");
                model.setProperty("/data/api_slug", "triage-v2");
                model.setProperty("/data/run_as_principal", "svc-triage");
                model.setProperty("/data/run_timeout_seconds", 900);
                model.setProperty("/data/skip_seen_items", false);
                model.setProperty("/data/max_parallel_items", 3);
                model.setProperty("/data/on_unknown_branch", "skip");
                model.setProperty("/data/enabled", false);
            }
        });

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        Then.waitFor({
            id: "workflowsTable",
            viewName: "Workflows",
            success: function () {
                const saved = backend.workflows.find((w) => w.id === WORKFLOW_ID);
                Opa5.assert.deepEqual(
                    saved && {
                        name: saved.name,
                        description: saved.description,
                        api_slug: saved.api_slug,
                        run_as_principal: saved.run_as_principal,
                        run_timeout_seconds: saved.run_timeout_seconds,
                        skip_seen_items: saved.skip_seen_items,
                        max_parallel_items: saved.max_parallel_items,
                        on_unknown_branch: saved.on_unknown_branch,
                        enabled: saved.enabled
                    },
                    {
                        name: "triage-inbox-edited",
                        description: "Updated description.",
                        api_slug: "triage-v2",
                        run_as_principal: "svc-triage",
                        run_timeout_seconds: 900,
                        skip_seen_items: false,
                        max_parallel_items: 3,
                        on_unknown_branch: "skip",
                        enabled: false
                    },
                    "every scalar field reaches the server -- one collectPayload() stopped sending would come back with its stale fixture value instead of this one"
                );
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
                    "every step's branch, agent, instructions and per-group position survive the save, unchanged"
                );
                Opa5.assert.deepEqual(
                    saved?.branches,
                    [
                        { key: "billing", description: "Billing questions", position: 1 },
                        { key: "support", description: "Support requests", position: 2 }
                    ],
                    "every branch survives the save, unchanged"
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

        // The end-to-end guarantee that actually matters: the disabled
        // placeholder is what forces the save itself to fail rather than
        // quietly substituting a different agent -- not just that the
        // control renders correctly. FakeBackend doesn't run
        // validate_workflow_parts, so the rejection is modelled the same
        // way the "rejected by validation" test below models it: via
        // failNext carrying the server's own message shape.
        When.waitFor({
            success: function () {
                backend.failNext = {
                    path: `workflows/${WORKFLOW_ID}`,
                    status: 400,
                    body: { detail: "Step 2 names agent 'ghost-agent', which does not exist or is disabled." }
                };
            }
        });

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        Then.waitFor({
            controlType: "sap.m.Dialog",
            searchOpenDialogs: true,
            matchers: function (element: UI5Element) {
                const dialog = element as Dialog;
                const text = dialog.getDomRef()?.textContent ?? "";
                return dialog.getTitle() === "Error" && text.indexOf("ghost-agent") !== -1;
            },
            success: function () {
                Opa5.assert.ok(true, "the save was rejected with the server's message naming the missing agent");
            },
            errorMessage: "No 'Error' dialog naming 'ghost-agent' appeared -- the save was not rejected"
        });

        When.waitFor({
            controlType: "sap.m.Button",
            searchOpenDialogs: true,
            matchers: function (element: UI5Element) { return (element as Button).getVisible(); },
            actions: new Press()
        });

        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const table = element as Table;
                const rows = table.getItems() as ColumnListItem[];
                const ghostRow = rows[rows.length - 1];
                Opa5.assert.strictEqual(
                    (ghostRow.getCells()[1] as Select).getSelectedKey(), "ghost-agent",
                    "after the rejected save, the step still names the missing agent -- it was never silently substituted"
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

opaTest(
    "creating a workflow from 'new', after the same route previously showed an existing one, does not carry over the old id",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        // sap.m.routing.Router caches a target's view/controller instance and
        // reuses it across every match of the same route -- "workflows/102"
        // and "workflows/new" both resolve to the "workflowDetail" target, so
        // this is the SAME WorkflowDetail controller instance load() runs
        // against twice. That is exactly the scenario a stale `workflowId`
        // field left over from the first load would break: if the "new"
        // branch of load() failed to reset it, the save below would PUT over
        // workflow 102 instead of POSTing a genuinely new workflow. Setting
        // the hash directly (rather than clicking back to the list and
        // pressing "New workflow") exercises the same patternMatched
        // re-fire a real navigation would, without depending on the nav-back
        // button's id.
        When.waitFor({
            success: function () { HashChanger.getInstance().setHash("workflows/new"); }
        });

        When.waitFor({
            id: "workflowName", viewName: "WorkflowDetail", actions: new EnterText({ text: "brand-new-flow" })
        });

        When.waitFor({ id: "addBranchButton", viewName: "WorkflowDetail", actions: new Press() });
        When.waitFor({
            id: "branchesTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) {
                return ((element as Table).getItems()[0] as ColumnListItem).getCells()[0];
            },
            actions: new EnterText({ text: "abap" })
        });

        // A second row added and then abandoned without ever naming it --
        // exactly what an operator who adds a row, then changes their mind,
        // leaves behind. Left in the payload it would 400 against
        // validate_workflow_parts' "A branch key must not be empty."; the
        // assertion below instead expects collectBranches() to have dropped
        // it, matching the classic admin's own collect step.
        When.waitFor({ id: "addBranchButton", viewName: "WorkflowDetail", actions: new Press() });

        // The step row's branch/agent are set directly on the model (see the
        // StepRow comment above): EnterText only drives text inputs, and a
        // <Select>'s selectedKey does not write back through its two-way
        // binding when set from code, only from a genuine user "change".
        When.waitFor({ id: "addStepButton", viewName: "WorkflowDetail", actions: new Press() });
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const model = (element as Table).getModel("workflow") as JSONModel;
                const steps = model.getProperty("/data/steps") as StepRow[];
                steps[steps.length - 1].branch_key = "abap";
                steps[steps.length - 1].agent_name = "gmail-agent";
                model.setProperty("/data/steps", steps);
            }
        });

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        Then.waitFor({
            id: "workflowsTable",
            viewName: "Workflows",
            success: function () {
                const original = backend.workflows.find((w) => w.id === WORKFLOW_ID);
                Opa5.assert.strictEqual(
                    original?.name, "triage-inbox",
                    "the previously-viewed workflow (102) was not overwritten"
                );

                const created = backend.workflows.find((w) => w.name === "brand-new-flow");
                Opa5.assert.ok(
                    created !== undefined && created.id !== WORKFLOW_ID,
                    "a genuinely new workflow was POSTed with its own id, not PUT over the stale id 102"
                );
                Opa5.assert.deepEqual(
                    created?.branches, [{ key: "abap", description: "", position: 1 }],
                    "the named branch was submitted, and the second, abandoned blank-key row was dropped rather than sent"
                );
                Opa5.assert.deepEqual(
                    created?.steps,
                    [{
                        branch_key: "abap", position: 1, agent_name: "gmail-agent",
                        instructions: "", fan_out: false, step_timeout_seconds: 600
                    }],
                    "the step added on the 'new' form was submitted"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "adding a step to a branch that already has steps, and removing the fan-out step, submits contiguous per-group positions",
    function (Given: Common, When: Common, Then: Common) {
        // Break-tested: temporarily changing WorkflowDetail.controller.ts's
        // collectSteps() to a single running counter (instead of one counter
        // per branch_key group) makes this fail -- the new support-branch
        // step comes back positioned 5th overall instead of 3rd within
        // "support". See task-3-report.md's Fix round 1 section for the
        // verbatim RED output.
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        // Remove the fan-out step (the sole main-line row, index 0): proves
        // a removed row does not leave a gap in another group's numbering.
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) {
                return ((element as Table).getItems()[0] as ColumnListItem).getCells()[5];
            },
            actions: new Press()
        });

        // Add a third step to "support", which already has two (positions 1
        // and 2 -- "Draft a support reply." / "Send the reply.").
        When.waitFor({ id: "addStepButton", viewName: "WorkflowDetail", actions: new Press() });
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const model = (element as Table).getModel("workflow") as JSONModel;
                const steps = model.getProperty("/data/steps") as StepRow[];
                const newStep = steps[steps.length - 1];
                newStep.branch_key = "support";
                newStep.agent_name = "btp-agent";
                newStep.instructions = "Escalate to a human.";
                model.setProperty("/data/steps", steps);
            }
        });

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
                        },
                        {
                            branch_key: "support", position: 3, agent_name: "btp-agent",
                            instructions: "Escalate to a human.", fan_out: false, step_timeout_seconds: 600
                        }
                    ],
                    "the main line is gone with no gap left behind, and 'support' is numbered 1..3 on its own, not 2..4 following 'billing'"
                );
            }
        });

        Then.iStopTheApp();
    }
);
