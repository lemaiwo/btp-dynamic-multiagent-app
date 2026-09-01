import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import HashChanger from "sap/ui/core/routing/HashChanger";
import MessageBox from "sap/m/MessageBox";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type HBox from "sap/m/HBox";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

QUnit.module("Workflow journey");

/** Row cell index of the run-now/delete HBox in Workflows.view.xml. Neither
 *  button carries an id -- like Agents.view.xml/Skills.view.xml, a template
 *  control cloned once per row can't be given a stable one -- so tests
 *  reach them the same way SkillJourney reaches its row: by drilling into
 *  the bound control tree from the table's own id. */
const ACTIONS_CELL = 6;

/** FakeBackend.reset() creates the two seeded agents (ids 100/101) before
 *  the one seeded workflow, so the workflow always lands on id 102 -- see
 *  FakeBackend.ts's reset(). */
const WORKFLOW_ID = 102;

function actionsBox(element: UI5Element): HBox {
    const table = element as Table;
    const row = table.getItems()[0] as ColumnListItem;
    return row.getCells()[ACTIONS_CELL] as HBox;
}

opaTest("the workflow list renders the seeded workflow's operational settings", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("workflows");

    Then.waitFor({
        id: "workflowsTable",
        viewName: "Workflows",
        success: function (element: UI5Element) {
            const table = element as Table;
            Opa5.assert.strictEqual(table.getItems().length, 1, "the seeded workflow is listed");
            const model = table.getModel("workflows") as JSONModel;
            Opa5.assert.strictEqual(model.getProperty("/items/0/name"), "triage-inbox", "the name is bound");
            Opa5.assert.strictEqual(model.getProperty("/items/0/api_slug"), "", "the api slug is bound");
            Opa5.assert.strictEqual(model.getProperty("/items/0/enabled"), true, "the enabled flag is bound");
            Opa5.assert.strictEqual(model.getProperty("/items/0/max_parallel_items"), 1, "max_parallel_items is bound");
            Opa5.assert.strictEqual(model.getProperty("/items/0/skip_seen_items"), true, "skip_seen_items is bound");
            Opa5.assert.strictEqual(model.getProperty("/items/0/on_unknown_branch"), "fail", "on_unknown_branch is bound");
        }
    });

    Then.iStopTheApp();
});

opaTest("pressing a workflow row navigates to the editor route", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("workflows");

    When.waitFor({
        id: "workflowsTable",
        viewName: "Workflows",
        matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
        actions: new Press()
    });

    // The editor view (WorkflowDetail) is built by a later task and does not
    // exist yet, so this asserts via the hash rather than by waiting for a
    // control in a view this task must not touch.
    Then.waitFor({
        check: function () { return HashChanger.getInstance().getHash() === `workflows/${WORKFLOW_ID}`; },
        success: function () {
            Opa5.assert.ok(true, "the row press navigated to the workflow editor route");
        }
    });

    Then.iStopTheApp();
});

opaTest("New workflow navigates to the editor with a new id", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("workflows");

    When.waitFor({ id: "addWorkflowButton", viewName: "Workflows", actions: new Press() });

    Then.waitFor({
        check: function () { return HashChanger.getInstance().getHash() === "workflows/new"; },
        success: function () {
            Opa5.assert.ok(true, "New workflow navigated to the editor with a new id");
        }
    });

    Then.iStopTheApp();
});

opaTest("Run now starts a run and toasts the backend's run id", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("workflows");

    When.waitFor({
        id: "workflowsTable",
        viewName: "Workflows",
        matchers: function (element: UI5Element) { return actionsBox(element).getItems()[0]; },
        actions: new Press()
    });

    // The toast renders in the static area, outside the view's control tree
    // -- ReloadJourney already polls for it the same way via `check`.
    Then.waitFor({
        check: function () { return document.querySelectorAll(".sapMMessageToast").length > 0; },
        success: function () {
            const toast = document.querySelector(".sapMMessageToast");
            Opa5.assert.ok(
                (toast?.textContent ?? "").indexOf("wf-run-") !== -1,
                "the backend's run id is shown in the toast"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("a run already in progress surfaces the server's detail verbatim", function (Given: Common, When: Common, Then: Common) {
    // FakeBackend's /workflows/{id}/run always answers 200 -- it models no
    // overlap lock -- so the 409 path is exercised via failNext instead.
    Given.iStartTheApp("workflows", {
        path: `workflows/${WORKFLOW_ID}/run`,
        status: 409,
        body: { detail: "A run for this workflow is already in progress." }
    });

    When.waitFor({
        id: "workflowsTable",
        viewName: "Workflows",
        matchers: function (element: UI5Element) { return actionsBox(element).getItems()[0]; },
        actions: new Press()
    });

    Then.waitFor({
        check: function () { return document.querySelectorAll(".sapMMessageToast").length > 0; },
        success: function () {
            const toast = document.querySelector(".sapMMessageToast");
            Opa5.assert.strictEqual(
                toast?.textContent, "A run for this workflow is already in progress.",
                "the 409 detail reaches the user unchanged, per ErrorHandler's conflict policy"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("deleting a workflow removes it from the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("workflows");

    // MessageBox.confirm opens outside the OPA5 control tree with no id of
    // its own, and no existing journey drives a popover open by clicking it
    // (see AgentJourney/SkillJourney, neither of which tests delete for
    // exactly this reason). Stubbing the same MessageBox module
    // Workflows.controller.ts imports is the standard SAPUI5 way around
    // that, via the sinon build UI5 itself ships at sap/ui/thirdparty/sinon-4.
    type SinonStub = { restore: () => void };
    type SinonLike = { stub: (obj: object, method: string) => { callsFake: (fn: (...a: unknown[]) => void) => SinonStub } };

    // The single-string overload of sap.ui.require is the *synchronous
    // probing* variant (returns immediately, undefined if not already
    // loaded) and never invokes a callback -- the array form is what
    // actually triggers an async load and calls back once it lands.
    let sinonLib: SinonLike | undefined;
    sap.ui.require(["sap/ui/thirdparty/sinon-4"], function (lib: SinonLike) {
        sinonLib = lib;
    });

    let confirmStub: SinonStub | undefined;
    When.waitFor({
        check: function () { return sinonLib !== undefined; },
        success: function () {
            confirmStub = sinonLib!.stub(MessageBox, "confirm").callsFake(function (...args: unknown[]) {
                (args[1] as { onClose: (action: string) => void }).onClose(MessageBox.Action.OK);
            });
        }
    });

    When.waitFor({
        id: "workflowsTable",
        viewName: "Workflows",
        matchers: function (element: UI5Element) { return actionsBox(element).getItems()[1]; },
        actions: new Press()
    });

    Then.waitFor({
        id: "workflowsTable",
        viewName: "Workflows",
        success: function (element: UI5Element) {
            const table = element as Table;
            Opa5.assert.strictEqual(table.getItems().length, 0, "the deleted workflow's row is gone");
            Opa5.assert.strictEqual(backend.workflows.length, 0, "the delete reached the backend");
            confirmStub?.restore();
        }
    });

    Then.iStopTheApp();
});
