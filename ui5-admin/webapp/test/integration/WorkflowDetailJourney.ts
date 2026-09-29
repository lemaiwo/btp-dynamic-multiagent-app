import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import { MAIN_LINE_KEY } from "com/agent/admin/model/NullableKey";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Button from "sap/m/Button";
import type HBox from "sap/m/HBox";
import type Input from "sap/m/Input";
import type Link from "sap/m/Link";
import type Select from "sap/m/Select";
import type Dialog from "sap/m/Dialog";
import type Text from "sap/m/Text";
import type TextArea from "sap/m/TextArea";
import type Title from "sap/m/Title";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type ProcessFlow from "sap/suite/ui/commons/ProcessFlow";
import type { WorkflowStep } from "com/agent/admin/service/types";
import Common, { backend } from "./pages/Common";

/** The subset of a step-row model entry this file manipulates directly
 * (arrange only -- never asserted on) to set a freshly `onAddStep`'d row's
 * branch/agent without driving the editor's `<Select>` controls, which do
 * not write back through their two-way binding when set programmatically
 * (only a genuine `change` event does). */
type StepRow = WorkflowStep & { agentOptions: unknown; branchOptions: unknown };

/** Cells of a row in the Steps list (WorkflowDetail.view.xml): number,
 * branch, kind, summary, fan-out badge, timeout, actions. The list is
 * read-mostly since the master-detail rework; a step's fields are edited in
 * the `stepEditor` panel, which shows the selected row. */
const CELL_BRANCH = 1;
const CELL_SUMMARY = 3;
const CELL_ACTIONS = 6;

/** The delete button of a step row: the Actions cell holds move-up /
 * move-down / delete, so it is the last item inside it. */
function deleteButtonOf(row: ColumnListItem): Control {
    return (row.getCells()[CELL_ACTIONS] as HBox).getItems()[2];
}

/** The 0-based index of the step list's selected row, -1 for none. */
function selectedIndexOf(table: Table): number {
    const item = table.getSelectedItem();
    return item ? table.getItems().indexOf(item) : -1;
}

/** Matcher for a control inside an open dialog by the tail of its id --
 * the text dialog is a dependent of the WorkflowDetail page, so its
 * controls carry the view prefix, and `searchOpenDialogs` ignores
 * `viewName`. */
function idEndsWith(suffix: string): (element: UI5Element) => boolean {
    return (element: UI5Element) => element.getId().endsWith(`--${suffix}`);
}

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Workflow detail journey");

// FakeBackend.reset() creates the two seeded agents (ids 100/101) before the
// one seeded workflow, so the workflow always lands on id 102 -- see
// FakeBackend.ts's reset() and WorkflowJourney.ts's own copy of this constant.
const WORKFLOW_ID = 102;

/** The step row carrying `needle` in its instructions. The steps list groups
 * main-line rows ahead of branch rows, so a row's position in the list no
 * longer follows the order steps were appended in -- find rows by content. */
function stepRowMatching(table: Table, needle: string): ColumnListItem {
    return (table.getItems() as ColumnListItem[]).filter((row) =>
        String(row.getBindingContext("workflow")?.getProperty("instructions") ?? "")
            .indexOf(needle) !== -1
    )[0];
}

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

                Opa5.assert.strictEqual(
                    (rows[0].getCells()[CELL_BRANCH] as Text).getText(false), "Main line",
                    "the fan-out step's row names the main line (branch_key null)"
                );
                Opa5.assert.strictEqual(
                    (rows[0].getCells()[CELL_SUMMARY] as Text).getText(false), "Read new mail.",
                    "an agent step's row summarises its instructions"
                );
                Opa5.assert.strictEqual(
                    (rows[1].getCells()[CELL_BRANCH] as Text).getText(false), "billing",
                    "a branch step's row names its branch"
                );
                Opa5.assert.strictEqual(selectedIndexOf(table), 0, "the first step opens in the editor");
            }
        });

        // The editor opened on the first (main-line, fan-out) step.
        Then.waitFor({
            id: "stepBranch",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const select = element as Select;
                // MAIN_LINE_KEY rather than "": with an empty selectedKey the
                // control renders blank even though a "" item exists, which is
                // exactly the bug the NullableKey binding type closes.
                Opa5.assert.strictEqual(
                    select.getSelectedKey(), MAIN_LINE_KEY,
                    "the fan-out step's branch select preselects the main-line key (branch_key null)"
                );
                Opa5.assert.strictEqual(
                    select.getSelectedItem()?.getText(), "Main line",
                    "and the control visibly reads 'Main line', not blank"
                );
            }
        });
        Then.waitFor({
            id: "stepAgent",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Select).getSelectedKey(), "gmail-agent",
                    "the fan-out step's agent is preselected"
                );
            }
        });

        // Selecting the billing row switches the editor to that step.
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) { return (element as Table).getItems()[1]; },
            actions: new Press()
        });

        Then.waitFor({
            id: "stepBranch",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) { return (element as Select).getSelectedKey() === "billing"; },
            success: function () {
                Opa5.assert.ok(true, "a branch step's branch is preselected");
            },
            errorMessage: "the editor did not switch to the billing step"
        });
        Then.waitFor({
            id: "stepAgent",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Select).getSelectedKey(), "btp-agent",
                    "a branch step's agent is preselected"
                );
            }
        });
        Then.waitFor({
            id: "stepInstructions",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as TextArea).getValue(), "Draft a billing reply.",
                    "selecting a row shows its instructions in the editor"
                );
            }
        });
        Then.waitFor({
            id: "stepEditorTitle",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Title).getText(), "Step 2", "the editor is titled by the row number");
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "the flow preview draws the saved workflow and redraws as steps are added",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        Then.waitFor({
            id: "workflowFlow",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const nodes = (element as ProcessFlow).getNodes();
                Opa5.assert.strictEqual(nodes.length, 4, "one node per seeded step");
                const fanOut = nodes.find((n) => n.getNodeId() === "main-0");
                Opa5.assert.strictEqual(fanOut?.getTitle(), "gmail-agent", "the fan-out node is titled by its agent");
                Opa5.assert.deepEqual(
                    fanOut?.getChildren(), ["billing-0", "support-0"],
                    "the fan-out node forks to the head of each declared branch"
                );
                Opa5.assert.deepEqual(
                    nodes.find((n) => n.getNodeId() === "support-0")?.getChildren(), ["support-1"],
                    "the two-step branch chains internally"
                );
                Opa5.assert.deepEqual(
                    nodes.find((n) => n.getNodeId() === "billing-0")?.getChildren(), [],
                    "with no step after the fan-out there is nothing to join back into yet"
                );
            }
        });

        When.waitFor({
            id: "addStepButton",
            viewName: "WorkflowDetail",
            actions: new Press()
        });

        Then.waitFor({
            id: "workflowFlow",
            viewName: "WorkflowDetail",
            // Polled, because the preview is redrawn from the model change the
            // press causes rather than by the press itself.
            check: function (element: UI5Element) {
                return (element as ProcessFlow).getNodes().length === 5;
            },
            success: function (element: UI5Element) {
                const nodes = (element as ProcessFlow).getNodes();
                const added = nodes.find((n) => n.getNodeId() === "main-1");
                Opa5.assert.ok(added, "the new row appears in the preview immediately, unsaved");
                Opa5.assert.strictEqual(
                    added?.getState(), "Planned",
                    "a step with no agent chosen yet is drawn as planned rather than dropped"
                );
                Opa5.assert.deepEqual(
                    nodes.find((n) => n.getNodeId() === "billing-0")?.getChildren(), ["main-1"],
                    "adding a main-line step after the fan-out gives the branches a join to reconnect to"
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
                            instructions: "Read new mail.", fan_out: true, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        },
                        {
                            branch_key: "billing", position: 1, agent_name: "btp-agent",
                            instructions: "Draft a billing reply.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        },
                        {
                            branch_key: "support", position: 1, agent_name: "btp-agent",
                            instructions: "Draft a support reply.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        },
                        {
                            branch_key: "support", position: 2, agent_name: "btp-agent",
                            instructions: "Send the reply.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        }
                    ],
                    "every step's branch, agent, instructions and per-group position survive the save, unchanged (a stored step without a kind is submitted as an agent step)"
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

        // Open the ghost step in the editor.
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) {
                return stepRowMatching(element as Table, "Whoever last ran this agent");
            },
            actions: new Press()
        });

        Then.waitFor({
            id: "stepAgent",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) { return (element as Select).getSelectedKey() === "ghost-agent"; },
            success: function (element: UI5Element) {
                const agentSelect = element as Select;
                Opa5.assert.strictEqual(
                    agentSelect.getSelectedKey(), "ghost-agent",
                    "the missing agent's name stays selected instead of the browser defaulting to the first real agent"
                );
                const selectedItem = agentSelect.getSelectedItem();
                Opa5.assert.strictEqual(
                    selectedItem?.getEnabled(), false,
                    "the placeholder carrying the missing name cannot itself be (re-)selected, so a save is forced to fail rather than quietly keep it"
                );
            },
            errorMessage: "the editor did not open on the ghost step with its stored agent name selected"
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
                const ghostRow = stepRowMatching(table, "Whoever last ran this agent");
                Opa5.assert.strictEqual(
                    ghostRow.getBindingContext("workflow")?.getProperty("agent_name"), "ghost-agent",
                    "after the rejected save, the step still names the missing agent -- it was never silently substituted"
                );
                Opa5.assert.strictEqual(table.getSelectedItem(), ghostRow, "the ghost step is still the one in the editor");
            }
        });
        Then.waitFor({
            id: "stepAgent",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Select).getSelectedKey(), "ghost-agent",
                    "and the editor still shows that name"
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
                        instructions: "", fan_out: false, step_timeout_seconds: 600,
                        kind: "agent", config: {}
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
                return deleteButtonOf((element as Table).getItems()[0] as ColumnListItem);
            },
            actions: new Press()
        });

        // The deleted row was the one in the editor; its neighbour (the row
        // that took its place) is selected instead of the editor going blank.
        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            check: function (element: UI5Element) { return (element as Table).getItems().length === 3; },
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(selectedIndexOf(element as Table), 0, "deleting the selected row selects its neighbour");
            }
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
                // Grouped ahead of the branch rows, so not steps[length-1]:
                // the new row is the one still carrying no agent.
                const newStep = steps.filter((row) => !row.agent_name)[0];
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
                            instructions: "Draft a billing reply.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        },
                        {
                            branch_key: "support", position: 1, agent_name: "btp-agent",
                            instructions: "Draft a support reply.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        },
                        {
                            branch_key: "support", position: 2, agent_name: "btp-agent",
                            instructions: "Send the reply.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        },
                        {
                            branch_key: "support", position: 3, agent_name: "btp-agent",
                            instructions: "Escalate to a human.", fan_out: false, step_timeout_seconds: 600,
                            kind: "agent", config: {}
                        }
                    ],
                    "the main line is gone with no gap left behind, and 'support' is numbered 1..3 on its own, not 2..4 following 'billing'"
                );
            }
        });

        Then.iStopTheApp();
    }
);

// --- where used ---
opaTest(
    "a step row's agent link opens that agent",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        // Row 0 is the main-line fan-out step, which runs gmail-agent; it
        // opens in the editor on load, and the link sits in the editor's
        // header.
        Then.waitFor({
            id: "stepAgentLink",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Link).getText(), "Open gmail-agent", "the link names the step's agent");
            }
        });

        When.waitFor({
            id: "stepAgentLink",
            viewName: "WorkflowDetail",
            actions: new Press()
        });

        Then.waitFor({
            id: "agentName",
            viewName: "AgentDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Input).getValue(), "gmail-agent",
                    "the agent detail page opened on the step's agent"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "pressing a flow preview node opens its agent",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        When.waitFor({
            id: "workflowFlow",
            viewName: "WorkflowDetail",
            actions: function (element: UI5Element | null) {
                const flow = element as ProcessFlow;
                const node = flow.getNodes().find((n) => n.getNodeId() === "billing-0");
                // ProcessFlowNode's click handler fires nodePress with the
                // node itself as the parameter object; do the same here.
                (flow as unknown as { fireNodePress(node: unknown): void }).fireNodePress(node);
            }
        });

        Then.waitFor({
            id: "agentName",
            viewName: "AgentDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as Input).getValue(), "btp-agent",
                    "the billing branch's first step runs btp-agent, and that agent opened"
                );
            }
        });

        Then.iStopTheApp();
    }
);

// --- step kinds ---
opaTest(
    "a transform step added between agent steps is saved with its kind and config, and needs no agent",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp("workflows");

        // Injected after FakeBackend.reset() but before the row press below
        // loads the editor, so GET /workflows/102 answers with a stored
        // transform step -- as the server returns one -- already in place.
        When.waitFor({
            success: function () {
                backend.workflows[0].steps.push({
                    branch_key: "billing", position: 2, agent_name: "",
                    instructions: "", fan_out: false, step_timeout_seconds: 60,
                    kind: "transform",
                    config: { extract_json: "", regex: null, template: "Reply: {{text}}", truncate: 300 }
                });
            }
        });

        When.waitFor({
            id: "workflowsTable",
            viewName: "Workflows",
            matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
            actions: new Press()
        });

        // The stored transform step has a row summarising its config;
        // selecting it opens its own form: the kind select says transform,
        // the agent dropdown is hidden and the template field carries the
        // stored value.
        // (The matcher, not `check`, gates on the row count: OPA hands
        // `check` the matcher's result, i.e. the row, not the table.)
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) {
                const table = element as Table;
                if (table.getItems().length !== 5) {
                    return false;
                }
                const row = table.getItems().find((item) => {
                    const step = item.getBindingContext("workflow")?.getObject() as StepRow;
                    return step.kind === "transform";
                }) as ColumnListItem;
                Opa5.assert.ok(row, "the stored transform step has a row");
                Opa5.assert.strictEqual(
                    (row.getCells()[CELL_SUMMARY] as Text).getText(false), "template, ≤300",
                    "its row summarises the stored config"
                );
                return row;
            },
            actions: new Press()
        });
        Then.waitFor({
            id: "stepKind",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) { return (element as Select).getSelectedKey() === "transform"; },
            success: function () {
                Opa5.assert.ok(true, "its kind select shows transform");
            },
            errorMessage: "the editor did not open on the transform step"
        });
        Then.waitFor({
            id: "stepAgent",
            viewName: "WorkflowDetail",
            visible: false,
            success: function (element: UI5Element) {
                Opa5.assert.notOk((element as Select).getVisible(), "the agent dropdown is hidden for a transform step");
            }
        });
        When.waitFor({
            id: "stepTfTemplate",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const template = element as TextArea;
                Opa5.assert.strictEqual(template.getValue(), "Reply: {{text}}", "the template field carries the stored value");
                // Edit through the model, as the other journeys do for bound
                // controls (see the StepRow comment above).
                const model = template.getModel("workflow") as JSONModel;
                const path = template.getBindingContext("workflow")!.getPath();
                model.setProperty(`${path}/cfg/template`, "Billing reply: {{text}}");
                model.setProperty(`${path}/cfg/truncate`, "250");
            }
        });

        // A brand-new row is selected as it is added; switched to `python`
        // it gets its editor and is submitted with code and timeout, no
        // agent.
        When.waitFor({ id: "addStepButton", viewName: "WorkflowDetail", actions: new Press() });
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            check: function (element: UI5Element) { return (element as Table).getItems().length === 6; },
            success: function (element: UI5Element) {
                const table = element as Table;
                Opa5.assert.strictEqual(selectedIndexOf(table), 5, "the added step is selected");
                const model = table.getModel("workflow") as JSONModel;
                model.setProperty("/data/steps/5/kind", "python");
            }
        });
        When.waitFor({
            id: "stepKind",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                // The binding writes `kind` on a genuine change only, so it
                // was set on the model above; fire the handler the way a
                // user's pick would.
                const kindSelect = element as Select;
                kindSelect.fireChange({ selectedItem: kindSelect.getSelectedItem() ?? undefined });
            }
        });
        When.waitFor({
            id: "stepPyCode",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const code = element as TextArea;
                Opa5.assert.ok(code.getVisible(), "switching the kind to python shows the python form");
                Opa5.assert.strictEqual(
                    code.getBindingContext("workflow")?.getPath(), "/data/steps/5",
                    "the editor is bound to the new row"
                );
                const model = code.getModel("workflow") as JSONModel;
                model.setProperty("/data/steps/5/cfg/code", "output = len(text)");
                model.setProperty("/data/steps/5/cfg/py_timeout", 5);
            }
        });

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        Then.waitFor({
            id: "workflowsTable",
            viewName: "Workflows",
            success: function () {
                const saved = backend.workflows.find((w) => w.id === WORKFLOW_ID);
                const transform = saved?.steps.find((s) => s.kind === "transform");
                Opa5.assert.deepEqual(
                    transform,
                    {
                        branch_key: "billing", position: 2, agent_name: "", instructions: "",
                        fan_out: false, step_timeout_seconds: 60, kind: "transform",
                        config: { extract_json: "", regex: null, template: "Billing reply: {{text}}", truncate: 250 }
                    },
                    "the transform step is saved with its kind and the edited config, and no agent"
                );
                const python = saved?.steps.find((s) => s.kind === "python");
                Opa5.assert.deepEqual(
                    python,
                    {
                        branch_key: null, position: 2, agent_name: "", instructions: "",
                        fan_out: false, step_timeout_seconds: 600, kind: "python",
                        config: { code: "output = len(text)", timeout_seconds: 5 }
                    },
                    "the row switched to python is saved with its code and timeout, positioned after the fan-out step on the main line"
                );
                Opa5.assert.ok(
                    saved?.steps.filter((s) => s.kind === "agent").every((s) => s.config && Object.keys(s.config).length === 0),
                    "agent steps carry an empty config"
                );
            }
        });

        Then.iStopTheApp();
    }
);

// --- step editor (master-detail) ---
opaTest(
    "the Expand dialog edits the instructions in full: Done writes back, Cancel leaves them as they were",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        // The first step (gmail-agent, "Read new mail.") opens on load.
        Then.waitFor({
            id: "stepInstructions",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as TextArea).getValue(), "Read new mail.", "the editor shows the selected step's instructions");
                Opa5.assert.ok((element as TextArea).getRows() >= 12, "the instructions field is tall enough to read");
            }
        });
        Then.waitFor({
            id: "stepInstructionsCounter",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Text).getText(false), "14 characters · 1 line", "the counter reflects the text");
            }
        });

        When.waitFor({ id: "stepInstructionsExpand", viewName: "WorkflowDetail", actions: new Press() });

        // The dialog opens on the same text; typing replaces it (EnterText
        // clears the field first).
        When.waitFor({
            controlType: "sap.m.TextArea",
            searchOpenDialogs: true,
            matchers: idEndsWith("stepTextDialogArea"),
            success: function (elements: UI5Element[]) {
                Opa5.assert.strictEqual((elements[0] as TextArea).getValue(), "Read new mail.", "the dialog opens on the field's text");
            }
        });
        When.waitFor({
            controlType: "sap.m.TextArea",
            searchOpenDialogs: true,
            matchers: idEndsWith("stepTextDialogArea"),
            actions: new EnterText({ text: "Read new mail.\nSkip newsletters." })
        });
        When.waitFor({
            controlType: "sap.m.Button",
            searchOpenDialogs: true,
            matchers: idEndsWith("stepTextDialogDone"),
            actions: new Press()
        });

        Then.waitFor({
            id: "stepInstructions",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const area = element as TextArea;
                Opa5.assert.strictEqual(area.getValue(), "Read new mail.\nSkip newsletters.", "Done wrote the dialog's text back to the field");
                const model = area.getModel("workflow") as JSONModel;
                Opa5.assert.strictEqual(
                    model.getProperty("/data/steps/0/instructions"), "Read new mail.\nSkip newsletters.",
                    "and to the step in the model, where the save reads it"
                );
            }
        });
        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const row = (element as Table).getItems()[0] as ColumnListItem;
                Opa5.assert.strictEqual(
                    (row.getCells()[CELL_SUMMARY] as Text).getText(false), "Read new mail.",
                    "the row summary stays the first line"
                );
            }
        });

        // Cancel: the field keeps what it had when the dialog opened.
        When.waitFor({ id: "stepInstructionsExpand", viewName: "WorkflowDetail", actions: new Press() });
        When.waitFor({
            controlType: "sap.m.TextArea",
            searchOpenDialogs: true,
            matchers: idEndsWith("stepTextDialogArea"),
            actions: new EnterText({ text: "Discarded draft." })
        });
        When.waitFor({
            controlType: "sap.m.Button",
            searchOpenDialogs: true,
            matchers: idEndsWith("stepTextDialogCancel"),
            actions: new Press()
        });
        Then.waitFor({
            id: "stepInstructions",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(
                    (element as TextArea).getValue(), "Read new mail.\nSkip newsletters.",
                    "Cancel restored the text the dialog opened with"
                );
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "adding a step selects it, and a validation error selects the offending row",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp("workflows/new");

        Then.waitFor({
            id: "stepEditorEmpty",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.ok((element as Control).getVisible(), "with no steps there is nothing to edit: the empty state shows");
            }
        });

        When.waitFor({ id: "workflowName", viewName: "WorkflowDetail", actions: new EnterText({ text: "select-on-error" }) });

        // Step 1: left without an agent -- the row the validation will name.
        When.waitFor({ id: "addStepButton", viewName: "WorkflowDetail", actions: new Press() });
        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual(selectedIndexOf(element as Table), 0, "the first added step is selected");
            }
        });
        Then.waitFor({
            id: "stepEditor",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.ok((element as Control).getVisible(), "the editor replaces the empty state");
            }
        });

        // Step 2: valid, and selected as it is added.
        When.waitFor({ id: "addStepButton", viewName: "WorkflowDetail", actions: new Press() });
        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                const table = element as Table;
                Opa5.assert.strictEqual(selectedIndexOf(table), 1, "adding a second step selects it");
                const model = table.getModel("workflow") as JSONModel;
                const steps = model.getProperty("/data/steps") as StepRow[];
                steps[1].agent_name = "gmail-agent";
                model.setProperty("/data/steps", steps);
            }
        });
        Then.waitFor({
            id: "stepEditorTitle",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Title).getText(), "Step 2", "the editor is on the new row");
            }
        });

        When.waitFor({ id: "saveWorkflowButton", viewName: "WorkflowDetail", actions: new Press() });

        Then.waitFor({
            controlType: "sap.m.Dialog",
            searchOpenDialogs: true,
            matchers: function (element: UI5Element) {
                const dialog = element as Dialog;
                const text = dialog.getDomRef()?.textContent ?? "";
                return dialog.getTitle() === "Error" && text.indexOf("Step 1: An agent step must name an agent.") !== -1;
            },
            success: function () {
                Opa5.assert.ok(true, "the client-side validation names the row without an agent");
            },
            errorMessage: "No 'Error' dialog naming step 1 appeared"
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
                Opa5.assert.strictEqual(selectedIndexOf(element as Table), 0, "the offending row is selected");
            }
        });
        Then.waitFor({
            id: "stepEditorTitle",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Title).getText(), "Step 1", "and the editor shows it, agent field in view");
            }
        });
        Then.waitFor({
            id: "stepAgent",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Select).getSelectedKey(), "", "with no agent chosen yet");
            }
        });

        Then.iStopTheApp();
    }
);

opaTest(
    "moving the selected step keeps it selected, and the editor follows it",
    function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp(`workflows/${WORKFLOW_ID}`);

        // Select "Draft a support reply." (support #1) and move it down.
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) { return stepRowMatching(element as Table, "Draft a support reply."); },
            actions: new Press()
        });
        Then.waitFor({
            id: "stepInstructions",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) { return (element as TextArea).getValue() === "Draft a support reply."; },
            success: function () { Opa5.assert.ok(true, "the support step is in the editor"); }
        });
        When.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            matchers: function (element: UI5Element) {
                const row = stepRowMatching(element as Table, "Draft a support reply.");
                return (row.getCells()[CELL_ACTIONS] as HBox).getItems()[1];
            },
            actions: new Press()
        });

        Then.waitFor({
            id: "stepsTable",
            viewName: "WorkflowDetail",
            check: function (element: UI5Element) { return selectedIndexOf(element as Table) === 3; },
            success: function (element: UI5Element) {
                const table = element as Table;
                Opa5.assert.strictEqual(
                    (table.getSelectedItem() as ColumnListItem).getBindingContext("workflow")?.getProperty("instructions"),
                    "Draft a support reply.",
                    "the moved row is still the selected one, now last"
                );
            }
        });
        Then.waitFor({
            id: "stepEditorTitle",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as Title).getText(), "Step 4", "the editor followed the row to its new number");
            }
        });
        Then.waitFor({
            id: "stepInstructions",
            viewName: "WorkflowDetail",
            success: function (element: UI5Element) {
                Opa5.assert.strictEqual((element as TextArea).getValue(), "Draft a support reply.", "and still shows its instructions");
            }
        });

        Then.iStopTheApp();
    }
);
