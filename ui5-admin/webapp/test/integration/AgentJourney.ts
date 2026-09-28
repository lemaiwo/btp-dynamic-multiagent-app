import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Button from "sap/m/Button";
import type ColumnListItem from "sap/m/ColumnListItem";
import type CustomListItem from "sap/m/CustomListItem";
import type HBox from "sap/m/HBox";
import type Input from "sap/m/Input";
import type Link from "sap/m/Link";
import type List from "sap/m/List";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type MultiComboBox from "sap/m/MultiComboBox";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Select from "sap/m/Select";
import type Table from "sap/m/Table";
import type Text from "sap/m/Text";
import type UI5Element from "sap/ui/core/Element";
import type StepInput from "sap/m/StepInput";
import type Switch from "sap/m/Switch";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Agent journey");

opaTest("creating an agent with two toolsets adds it to the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");

    When.waitFor({ id: "addAgentButton", viewName: "Agents", actions: new Press() });

    When.waitFor({ id: "agentName", viewName: "AgentDetail", actions: new EnterText({ text: "new-agent" }) });
    When.waitFor({ id: "agentDescription", viewName: "AgentDetail", actions: new EnterText({ text: "A new agent" }) });
    When.waitFor({ id: "agentInstructions", viewName: "AgentDetail", actions: new EnterText({ text: "Do the thing." }) });

    // First toolset. The MCP server dialog is a Fragment added as a
    // *dependent* of the AgentDetail view (not a named view of its own), so
    // OPA5's "searchOpenDialogs" id matcher can only strip the view-id prefix
    // (and so match a bare id like "serverUrl") when it is also told which
    // view to walk up the dialog's parent chain to -- without viewName it
    // falls back to comparing the *full* prefixed id, which never matches.
    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({ id: "serverUrl", viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text: "https://a.hana.ondemand.com/mcp" }) });
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });

    // Second toolset: a built-in typed by hand. sapnotes is the one that
    // needs no credential -- the catalog narrows every built-in to the modes
    // its factory can build with, so gmail here would flip to oauth2 and OK
    // would (correctly) refuse the dialog for want of a client id.
    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({ id: "serverUrl", viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text: "builtin:sapnotes" }) });
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });

    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            Opa5.assert.ok(
                backend.agents.some((a) => a.name === "new-agent" && a.mcp_servers.length === 2),
                "the agent reached the backend with both toolsets"
            );
        }
    });

    Then.iStopTheApp();
});

// FakeBackend.reset() fixtures btp-agent (id 100) with peers ["gmail-agent"]
// and model_name "gpt-4o" (present in the fake GET /model list), and
// gmail-agent (id 101) with model_name "retired-model" (deliberately absent
// from that list) -- see FakeBackend.ts.

opaTest("editing an existing agent shows its peers and model override populated, and excludes itself as a peer", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    Then.waitFor({
        id: "agentPeers",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const multiComboBox = element as MultiComboBox;
            Opa5.assert.deepEqual(
                multiComboBox.getSelectedKeys(), ["gmail-agent"],
                "the stored peer is preselected, not left blank"
            );
            Opa5.assert.ok(
                multiComboBox.getItems().every((item) => item.getKey() !== "btp-agent"),
                "the agent being edited is not offered as its own peer"
            );
        }
    });

    Then.waitFor({
        id: "agentModelName",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const select = element as Select;
            Opa5.assert.strictEqual(
                select.getSelectedKey(), "gpt-4o",
                "the stored model override is preselected"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("saving an untouched agent resends its peers and model override rather than wiping them", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    // Confirms the fields actually round-trip through the save payload: if
    // AgentDetail.controller.ts's load() ever stopped copying peers/model_name
    // into "/data" (the regression this task exists to prevent), the form
    // would still render fine from "/availableAgents" and "/availableModels"
    // but this save would silently blank both on the backend.
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 100);
            Opa5.assert.deepEqual(
                saved?.peers, ["gmail-agent"],
                "the peer survived an unrelated save"
            );
            Opa5.assert.strictEqual(
                saved?.model_name, "gpt-4o",
                "the model override survived an unrelated save"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("a stored model override missing from the available list stays selected and is resent unchanged on save", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/101");

    Then.waitFor({
        id: "agentModelName",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const select = element as Select;
            Opa5.assert.strictEqual(
                select.getSelectedKey(), "retired-model",
                "an override the model fetch didn't return stays selected instead of falling back to blank"
            );
        }
    });

    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 101);
            Opa5.assert.strictEqual(
                saved?.model_name, "retired-model",
                "the unavailable override reached the backend unchanged, not blanked out"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("every OAuth2 server offers sign-in, including one that is already connected", function (Given: Common, When: Common, Then: Common) {
    // The button used to be hidden once a token existed, which left no way to
    // connect as yourself when the run-as identity was someone else, and no way
    // to re-connect a revoked token before it failed a scheduled run.
    Given.iStartTheApp("agents/100");

    Then.waitFor({
        id: "credentialsTable",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const rows = (element as Table).getItems() as ColumnListItem[];
            const signInVisible = rows.map(
                (row) => (row.getCells()[3] as Button).getVisible()
            );
            Opa5.assert.deepEqual(
                signInVisible, [true, true, false],
                "both oauth2 servers offer sign-in; the destination server, which needs no user token, does not"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("the credential column reports validity, not just presence", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    Then.waitFor({
        id: "credentialsTable",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const rows = (element as Table).getItems() as ColumnListItem[];
            const states = rows.map((row) => (row.getCells()[1] as ObjectStatus).getState());
            Opa5.assert.deepEqual(
                states, ["Success", "Error", "None"],
                "a live token reads as success, an expired one as an error, and a server needing none stays neutral"
            );
            Opa5.assert.ok(
                (rows[0].getCells()[2] as Text).getText(false).length > 0,
                "the live token shows when it expires"
            );
            Opa5.assert.strictEqual(
                (rows[2].getCells()[2] as Text).getText(false), "—",
                "a server with no expiry shows an em dash rather than a bogus date"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("picking a built-in from the toolset dropdown fills in its url and auth mode", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");

    When.waitFor({ id: "addAgentButton", viewName: "Agents", actions: new Press() });
    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({
        id: "serverKind",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const select = element as Select;
            select.setSelectedKey("builtin:jira");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });

    Then.waitFor({
        id: "serverAuthMode",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const select = element as Select;
            Opa5.assert.strictEqual(select.getSelectedKey(), "destination", "Jira's only auth mode is selected");
            Opa5.assert.strictEqual(select.getItems().length, 1, "no other auth mode is offered");
            const server = select.getModel("server") as JSONModel;
            Opa5.assert.strictEqual(server.getProperty("/url"), "builtin:jira", "the url is the built-in's");
        }
    });
    Then.iStopTheApp();
});

opaTest("an invalid server url is refused inline and the dialog stays open", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");

    When.waitFor({ id: "addAgentButton", viewName: "Agents", actions: new Press() });
    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({ id: "serverUrl", viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text: "http://insecure.example.com/mcp" }) });
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });

    Then.waitFor({
        id: "serverUrl",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const input = element as Input;
            Opa5.assert.strictEqual(input.getValueState(), "Error", "the url field is flagged");
        }
    });

    Then.iStopTheApp();
});

// --- where used ---
// gmail-agent (id 101) runs triage-inbox's main-line fan-out step and is the
// one peer btp-agent lists; btp-agent (id 100) runs the three branch steps
// and is nobody's peer -- see FakeBackend.reset().

opaTest("the where-used panel lists the workflows and peers that refer to the agent, and its workflow link navigates there", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/101");

    Then.waitFor({
        id: "whereUsedWorkflows",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const items = (element as List).getItems() as CustomListItem[];
            Opa5.assert.strictEqual(items.length, 1, "the one workflow with a step running this agent is listed");
            const row = items[0].getContent()[0] as HBox;
            Opa5.assert.strictEqual((row.getItems()[0] as Link).getText(), "triage-inbox", "named by a link");
            Opa5.assert.strictEqual(
                (row.getItems()[2] as Text).getText(false), "Steps: main #1",
                "with the position of the step that runs it"
            );
        }
    });

    Then.waitFor({
        id: "whereUsedPeers",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const items = (element as List).getItems() as CustomListItem[];
            Opa5.assert.strictEqual(items.length, 1, "the one agent listing this one as a peer is listed");
            const row = items[0].getContent()[0] as HBox;
            Opa5.assert.strictEqual((row.getItems()[0] as Link).getText(), "btp-agent", "named by a link");
        }
    });

    When.waitFor({
        id: "whereUsedWorkflows",
        viewName: "AgentDetail",
        matchers: function (element: UI5Element) {
            return (((element as List).getItems()[0] as CustomListItem).getContent()[0] as HBox).getItems()[0];
        },
        actions: new Press()
    });

    Then.waitFor({
        id: "workflowName",
        viewName: "WorkflowDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as Input).getValue(), "triage-inbox", "the workflow detail page opened on that workflow");
        }
    });

    Then.iStopTheApp();
});

opaTest("the where-used panel lists per-branch step positions and hides an empty peer list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    Then.waitFor({
        id: "whereUsedWorkflows",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const items = (element as List).getItems() as CustomListItem[];
            Opa5.assert.strictEqual(items.length, 1, "triage-inbox is listed once, not once per step");
            const row = items[0].getContent()[0] as HBox;
            Opa5.assert.strictEqual(
                (row.getItems()[2] as Text).getText(false), "Steps: billing #1, support #1, support #2",
                "every step that runs the agent is listed by branch and position"
            );
        }
    });

    Then.waitFor({
        id: "whereUsedPeers",
        viewName: "AgentDetail",
        visible: false,
        success: function (element: UI5Element) {
            Opa5.assert.notOk((element as List).getVisible(), "no agent lists btp-agent as a peer, so the peer list is not shown");
        }
    });

    Then.iStopTheApp();
});

// --- deep agents -------------------------------------------------------------
// FakeBackend.reset() gives btp-agent (id 100) deep = { enabled, scratchpad
// off, 3 sub-agents, depth 2, "Be brief." }; gmail-agent (id 101) has the
// defaults (disabled).

opaTest("the deep agent panel shows the stored config and resends it changed on save", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    Then.waitFor({
        id: "agentDeepEnabled",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as Switch).getState(), true, "the stored config is shown as enabled");
        }
    });
    Then.waitFor({
        id: "agentDeepMaxSubagents",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as StepInput).getValue(), 3, "the stored sub-agent cap is shown");
        }
    });

    // Raise the cap through the control, then save.
    When.waitFor({
        id: "agentDeepMaxSubagents",
        viewName: "AgentDetail",
        actions: function (element: UI5Element | null) {
            const input = element as StepInput;
            input.setValue(4);
            input.fireChange({ value: "4" });
        }
    });
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 100);
            Opa5.assert.deepEqual(
                saved?.deep,
                {
                    enabled: true, planning: true, scratchpad: false, subagents: true,
                    max_subagents: 4, subagent_max_depth: 2, subagent_instructions: "Be brief."
                },
                "the whole deep config reached the backend, with only the cap changed"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("an agent without a deep config saves the defaults, not nothing", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/101");

    Then.waitFor({
        id: "agentDeepEnabled",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as Switch).getState(), false, "the panel starts disabled");
        }
    });
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 101);
            Opa5.assert.deepEqual(
                saved?.deep,
                {
                    enabled: false, planning: true, scratchpad: true, subagents: true,
                    max_subagents: 5, subagent_max_depth: 1, subagent_instructions: ""
                },
                "the defaults are sent explicitly, so the backend can tell 'off' from 'not sent'"
            );
        }
    });

    Then.iStopTheApp();
});
