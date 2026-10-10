import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type CheckBox from "sap/m/CheckBox";
import type ColumnListItem from "sap/m/ColumnListItem";
import type CustomListItem from "sap/m/CustomListItem";
import type HBox from "sap/m/HBox";
import type Input from "sap/m/Input";
import type Link from "sap/m/Link";
import type List from "sap/m/List";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type MultiComboBox from "sap/m/MultiComboBox";
import type MessageStrip from "sap/m/MessageStrip";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Panel from "sap/m/Panel";
import type Select from "sap/m/Select";
import type Table from "sap/m/Table";
import type Text from "sap/m/Text";
import type TextArea from "sap/m/TextArea";
import type Title from "sap/m/Title";
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

// --- destinations: user_context default ---
opaTest("a new remote destination server starts acting as the signed-in user and posts user_context true", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/101");

    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({ id: "serverUrl", viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text: "https://new.example.com/mcp" }) });
    When.waitFor({
        id: "serverAuthMode",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const select = element as Select;
            select.setSelectedKey("destination");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });
    Then.waitFor({
        id: "oauthUserContext",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as CheckBox).getSelected(), true, "on by default for a new remote server");
        }
    });
    When.waitFor({ id: "oauthDestination", viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text: "NEWDEST" }) });
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });
    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 101);
            Opa5.assert.deepEqual(
                saved?.mcp_servers[2],
                { url: "https://new.example.com/mcp", auth_mode: "destination", oauth: { destination: "NEWDEST", user_context: true } },
                "posted user_context true"
            );
        }
    });
    Then.iStopTheApp();
});

opaTest("an existing remote destination server stored with user_context false stays false", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");
    // reset() runs when the app starts, so change the stored server after it.
    When.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const stored = backend.agents.find((a) => a.id === 101)!.mcp_servers[1];
            (stored.oauth as Record<string, unknown>).user_context = false;
            HashChanger.getInstance().setHash("agents/101");
        }
    });

    When.waitFor({
        id: "serversTable",
        viewName: "AgentDetail",
        matchers: function (element: UI5Element) {
            return ((element as Table).getItems() as ColumnListItem[]).length === 2;
        },
        actions: function (element: UI5Element | null) {
            const row = (element as Table).getItems()[1] as ColumnListItem;
            ((row.getCells()[2] as HBox).getItems()[0] as Button).firePress();
        }
    });
    Then.waitFor({
        id: "oauthUserContext",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as CheckBox).getSelected(), false, "the stored false is kept");
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

// --- run now / refresh / last runs ------------------------------------------
// FakeBackend.reset() seeds run-1 (success) and run-2 (running) for btp-agent
// (id 100, exposed as a job API) and run-3 for gmail-agent (id 101). POST
// agents/{id}/run records the call in backend.runNowCalls and prepends a
// running run, which the panel's delayed reload then picks up.

opaTest("an existing agent shows Run now, Refresh and its own last runs, and a run row opens the run", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    Then.waitFor({
        id: "runAgentNowButton",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const button = element as Button;
            Opa5.assert.ok(button.getVisible(), "Run now is in the header");
            Opa5.assert.ok(button.getEnabled(), "and enabled, since the agent is exposed as a job API");
        }
    });
    Then.waitFor({
        id: "refreshAgentButton",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            Opa5.assert.ok((element as Button).getVisible(), "Refresh is in the header");
        }
    });
    Then.waitFor({
        id: "agentRunsTable",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            const rows = (element as Table).getItems() as ColumnListItem[];
            Opa5.assert.strictEqual(rows.length, 2, "only this agent's runs are listed, not the other agent's");
            Opa5.assert.deepEqual(
                rows.map((row) => (row.getCells()[0] as ObjectStatus).getText()),
                ["success", "running"],
                "newest first, with the status of each"
            );
            Opa5.assert.strictEqual(
                (rows[0].getCells()[3] as Text).getText(false), "1m 30s",
                "a finished run shows its duration"
            );
        }
    });
    Then.waitFor({
        id: "agentRunsCount",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual((element as Title).getText(), "2 runs", "the panel header counts them");
        }
    });

    When.waitFor({
        id: "agentRunsTable",
        viewName: "AgentDetail",
        matchers: function (element: UI5Element) { return (element as Table).getItems()[0]; },
        actions: new Press()
    });
    Then.waitFor({
        check: function () { return HashChanger.getInstance().getHash() === "runs/run-1"; },
        success: function () {
            Opa5.assert.ok(true, "pressing a run row opens that run's detail");
        }
    });

    Then.iStopTheApp();
});

opaTest("Run now on the detail page posts to the run endpoint and the last-runs panel picks up the new run", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    When.waitFor({ id: "runAgentNowButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        check: function () { return backend.runNowCalls.indexOf("agents/100/run") !== -1; },
        success: function () {
            Opa5.assert.deepEqual(backend.runNowCalls, ["agents/100/run"], "exactly one run was requested, for this agent");
        }
    });
    Then.waitFor({
        id: "agentRunsTable",
        viewName: "AgentDetail",
        // The panel reloads itself about a second after the run started;
        // polled rather than asserted once.
        check: function (element: UI5Element) { return (element as Table).getItems().length === 3; },
        success: function (element: UI5Element) {
            const first = (element as Table).getItems()[0] as ColumnListItem;
            Opa5.assert.strictEqual(
                (first.getCells()[0] as ObjectStatus).getText(), "running",
                "the new run appears at the top as running without pressing Refresh"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("Refresh on a dirty agent form asks first, then reloads everything from the server", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/100");

    // Make the form dirty, then change the record behind it: a reload that
    // just restored the snapshot would not show the new description.
    When.waitFor({ id: "agentName", viewName: "AgentDetail", actions: new EnterText({ text: "renamed-locally" }) });
    When.waitFor({
        id: "agentDescription",
        viewName: "AgentDetail",
        success: function () {
            const agent = backend.agents.find((a) => a.id === 100);
            if (agent) {
                agent.description = "changed on the server";
            }
        }
    });
    When.waitFor({ id: "refreshAgentButton", viewName: "AgentDetail", actions: new Press() });

    // The confirm is a MessageBox in the static area.
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: { properties: { text: "OK" } },
        actions: new Press(),
        errorMessage: "no discard confirmation was shown for the dirty form"
    });

    Then.waitFor({
        id: "agentName",
        viewName: "AgentDetail",
        check: function (element: UI5Element) { return (element as Input).getValue() === "btp-agent"; },
        success: function () {
            Opa5.assert.ok(true, "the local rename was discarded");
        }
    });
    Then.waitFor({
        id: "agentDescription",
        viewName: "AgentDetail",
        success: function (element: UI5Element) {
            Opa5.assert.strictEqual(
                (element as TextArea).getValue(), "changed on the server",
                "the form shows what the server has now, not the snapshot it was loaded with"
            );
        }
    });

    Then.iStopTheApp();
});

opaTest("a new agent has neither the header buttons nor the last-runs panel", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/new");

    Then.waitFor({
        id: "runAgentNowButton",
        viewName: "AgentDetail",
        visible: false,
        success: function (element: UI5Element) {
            Opa5.assert.notOk((element as Button).getVisible(), "nothing to run yet");
        }
    });
    Then.waitFor({
        id: "refreshAgentButton",
        viewName: "AgentDetail",
        visible: false,
        success: function (element: UI5Element) {
            Opa5.assert.notOk((element as Button).getVisible(), "nothing to reload yet");
        }
    });
    Then.waitFor({
        id: "agentRunsPanel",
        viewName: "AgentDetail",
        visible: false,
        success: function (element: UI5Element) {
            Opa5.assert.notOk((element as Panel).getVisible(), "no runs to list yet");
        }
    });

    Then.iStopTheApp();
});

// --- destinations: a remote MCP server ---
// gmail-agent's second server (FakeBackend.reset) is a remote MCP url behind a
// user-propagating destination. Opening it and pressing OK must not turn it
// into an app-level destination: that would swap every caller's identity for
// the destination's technical credential without a word.
opaTest("a remote server on a user-context destination keeps acting as the user across an edit", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents/101");

    When.waitFor({
        id: "serversTable",
        viewName: "AgentDetail",
        matchers: function (element: UI5Element) {
            const rows = (element as Table).getItems() as ColumnListItem[];
            return rows.length === 2;
        },
        actions: function (element: UI5Element | null) {
            const row = (element as Table).getItems()[1] as ColumnListItem;
            ((row.getCells()[2] as HBox).getItems()[0] as Button).firePress();
        }
    });

    Then.waitFor({
        id: "oauthUserContext",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const box = element as CheckBox;
            Opa5.assert.ok(box.getVisible(), "the switch is shown for a remote url");
            Opa5.assert.strictEqual(box.getSelected(), true, "and loaded from the stored server");
            // Whatever the dialog shows is kept by cleanOAuth; the window is not.
            const lookback = Element.getElementById(
                box.getId().replace(/oauthUserContext$/, "oauthLookback")) as Input | undefined;
            Opa5.assert.ok(lookback, "the lookback field exists");
            Opa5.assert.notOk(lookback?.getVisible(), "but is hidden for a remote destination");
        }
    });
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 101);
            Opa5.assert.deepEqual(
                saved?.mcp_servers[1],
                {
                    url: "https://arc1.example.com/mcp", auth_mode: "destination",
                    oauth: { destination: "arc1-abap-readonly", user_context: true }
                },
                "posted exactly {destination, user_context}"
            );
        }
    });

    Then.iStopTheApp();
});

// --- sharepoint ---
// One pinned workbook: site, library, path and the views (typed as JSON).
// Generic placeholders only; a calendar view with a kind label carries `kinds`.
const SHAREPOINT_VIEWS = {
    team: { kind: "table", table: "TeamMembers", columns: ["Name", "Team", "ID"] },
    planning: {
        kind: "calendar", sheet: "{year}", date_row: 8, first_row: 10, first_date_column: "D",
        labels: { member: "A", team: "B", kind: "C" },
        kinds: ["Presence", "Guard"],
        codes: { H: "unavailable", T: "available" }
    }
};

/** Opens the server dialog of gmail-agent and picks the SharePoint toolset. */
function iAddASharePointServer(Given: Common, When: Common): void {
    Given.iStartTheApp("agents/101");
    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({
        id: "serverKind",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const select = element as Select;
            const item = select.getItems().find((i) => i.getText() === "SharePoint workbook (Excel, read-only)");
            Opa5.assert.ok(item, "the toolset is offered under its name");
            select.setSelectedKey("builtin:sharepoint");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });
}

function iTypeInTheServerDialog(When: Common, id: string, text: string): void {
    When.waitFor({ id, viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text }) });
}

/** Sibling control of the server dialog, visible or not (OPA only finds visible ones). */
function dialogControl<T extends UI5Element>(anchor: UI5Element, anchorId: string, id: string): T | undefined {
    return Element.getElementById(anchor.getId().replace(new RegExp(`${anchorId}$`), id)) as T | undefined;
}

opaTest("a SharePoint server stores the pins and the views", function (Given: Common, When: Common, Then: Common) {
    iAddASharePointServer(Given, When);

    Then.waitFor({
        id: "serverAuthMode",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const select = element as Select;
            Opa5.assert.strictEqual(select.getSelectedKey(), "destination", "BTP destination is selected");
            Opa5.assert.deepEqual(
                select.getItems().map((i) => i.getKey()), ["app_only", "destination"],
                "the two modes the built-in runs on are offered");
        }
    });
    iTypeInTheServerDialog(When, "oauthDestination", "GRAPH");
    iTypeInTheServerDialog(When, "oauthSharePointSite", "example.sharepoint.com:/sites/planning");
    iTypeInTheServerDialog(When, "oauthSharePointLibrary", "Documents");
    iTypeInTheServerDialog(When, "oauthSharePointPath", "Planning/Planning.xlsx");
    iTypeInTheServerDialog(When, "oauthSharePointViews", JSON.stringify(SHAREPOINT_VIEWS));
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });

    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 101);
            Opa5.assert.deepEqual(
                saved?.mcp_servers[2],
                {
                    url: "builtin:sharepoint", auth_mode: "destination",
                    oauth: {
                        destination: "GRAPH",
                        site: "example.sharepoint.com:/sites/planning",
                        library: "Documents",
                        path: "Planning/Planning.xlsx",
                        views: SHAREPOINT_VIEWS
                    }
                },
                "posted exactly {destination, site, library, path, views}"
            );
        }
    });
    Then.iStopTheApp();
});

opaTest("invalid JSON in the SharePoint views field is refused inline and the dialog stays open", function (Given: Common, When: Common, Then: Common) {
    iAddASharePointServer(Given, When);

    iTypeInTheServerDialog(When, "oauthDestination", "GRAPH");
    iTypeInTheServerDialog(When, "oauthSharePointSite", "example.sharepoint.com:/sites/planning");
    iTypeInTheServerDialog(When, "oauthSharePointLibrary", "Documents");
    iTypeInTheServerDialog(When, "oauthSharePointPath", "Planning/Planning.xlsx");
    // No message may echo what the admin typed: the text carries a marker.
    iTypeInTheServerDialog(When, "oauthSharePointViews", "{ team: SECRETVALUE }");
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });

    Then.waitFor({
        id: "oauthSharePointViews",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const area = element as TextArea;
            Opa5.assert.strictEqual(area.getValueState(), "Error", "the views field is flagged");
            Opa5.assert.strictEqual(area.getValueStateText(), "Views is not valid JSON.", "and says why, in a fixed text");
            Opa5.assert.strictEqual(area.getValueStateText().indexOf("SECRETVALUE"), -1, "the typed text is not echoed");
            const errors = JSON.stringify((area.getModel("server") as JSONModel).getProperty("/errors"));
            Opa5.assert.strictEqual(errors.indexOf("SECRETVALUE"), -1, "nor held in the dialog's errors");
            const agent = area.getModel("agent") as JSONModel;
            Opa5.assert.strictEqual(
                (agent.getProperty("/data/mcp_servers") as unknown[]).length, 2, "no server was added");
        }
    });
    Then.iStopTheApp();
});

opaTest("the SharePoint server dialog shows its four fields and no mail or signed-in-user control, in both auth modes", function (Given: Common, When: Common, Then: Common) {
    iAddASharePointServer(Given, When);

    const HIDDEN = ["oauthMailbox", "oauthLookback", "oauthAllowSend", "oauthUserContext", "oauthTeam", "oauthMailTheme"];
    const SHOWN = ["oauthSharePointSite", "oauthSharePointLibrary", "oauthSharePointPath", "oauthSharePointViews"];
    const check = function (mode: string, alsoShown: string[], alsoHidden: string[]): void {
        Then.waitFor({
            id: "serverAuthMode",
            viewName: "AgentDetail",
            searchOpenDialogs: true,
            matchers: function (element: UI5Element) {
                return (element as Select).getSelectedKey() === mode;
            },
            success: function (element: UI5Element) {
                SHOWN.concat(alsoShown).forEach((id) => {
                    Opa5.assert.ok(
                        dialogControl<Input>(element, "serverAuthMode", id)?.getVisible(), `${mode}: ${id} is shown`);
                });
                HIDDEN.concat(alsoHidden).forEach((id) => {
                    const control = dialogControl<Input>(element, "serverAuthMode", id);
                    Opa5.assert.ok(control, `${id} exists`);
                    Opa5.assert.notOk(control?.getVisible(), `${mode}: ${id} is hidden`);
                });
            }
        });
    };

    check("destination", ["oauthDestination"], ["oauthClientId", "oauthClientSecret"]);
    When.waitFor({
        id: "serverAuthMode",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const select = element as Select;
            select.setSelectedKey("app_only");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });
    check("app_only", ["oauthClientId", "oauthClientSecret", "oauthScope"], ["oauthDestination"]);
    Then.iStopTheApp();
});

// --- sharepoint: fix round 1 --- the edit round trip of a stored app-only entry
opaTest("editing a stored app-only SharePoint server shows pins and views unchanged, keeps the blank secret and refuses a cleared pin", function (Given: Common, When: Common, Then: Common) {
    const stored = {
        client_id: "client-1", has_client_secret: true,
        token_url: "https://login.example.com/tenant/oauth2/v2.0/token",
        scope: "https://graph.microsoft.com/.default",
        site: "example.sharepoint.com:/sites/planning", library: "Documents",
        path: "Planning/Planning.xlsx", views: SHAREPOINT_VIEWS
    };
    Given.iStartTheApp("agents");
    // reset() runs when the app starts, so add the stored server after it.
    When.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const agent = backend.agents.find((a) => a.id === 101)!;
            agent.mcp_servers = agent.mcp_servers.concat([{
                url: "builtin:sharepoint", auth_mode: "app_only",
                oauth: JSON.parse(JSON.stringify(stored)) as typeof stored
            }] as unknown as typeof agent.mcp_servers);
            HashChanger.getInstance().setHash("agents/101");
        }
    });
    When.waitFor({
        id: "serversTable",
        viewName: "AgentDetail",
        matchers: function (element: UI5Element) {
            return ((element as Table).getItems() as ColumnListItem[]).length === 3;
        },
        actions: function (element: UI5Element | null) {
            const row = (element as Table).getItems()[2] as ColumnListItem;
            ((row.getCells()[2] as HBox).getItems()[0] as Button).firePress();
        }
    });

    Then.waitFor({
        id: "oauthSharePointViews",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const value = function (id: string): string | undefined {
                return dialogControl<Input>(element, "oauthSharePointViews", id)?.getValue();
            };
            Opa5.assert.strictEqual(value("oauthSharePointSite"), stored.site, "the site is shown as stored");
            Opa5.assert.strictEqual(value("oauthSharePointLibrary"), stored.library, "the library is shown as stored");
            Opa5.assert.strictEqual(value("oauthSharePointPath"), stored.path, "the path is shown as stored");
            Opa5.assert.strictEqual(
                (element as TextArea).getValue(), JSON.stringify(SHAREPOINT_VIEWS, null, 2), "the views are shown as stored");
            Opa5.assert.deepEqual(
                JSON.parse((element as TextArea).getValue()), SHAREPOINT_VIEWS, "and read back to the stored object");
            const secret = dialogControl<Input>(element, "oauthSharePointViews", "oauthClientSecret");
            Opa5.assert.strictEqual(secret?.getValue(), "", "the secret field is blank");
            Opa5.assert.ok(secret?.getPlaceholder(), "and says a secret is stored");
            Opa5.assert.strictEqual(
                dialogControl<Select>(element, "oauthSharePointViews", "serverAuthMode")?.getSelectedKey(),
                "app_only", "the stored mode is selected");
        }
    });

    // A cleared pin is refused and the dialog stays open.
    When.waitFor({
        id: "oauthSharePointPath",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            (element as Input).setValue("");
        }
    });
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) {
            return element.getProperty("text") === "Path must be the path of an .xlsx file below the library root.";
        },
        success: function () {
            Opa5.assert.ok(true, "the cleared path is refused by the model's rule");
        }
    });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) {
            return (element as Button).getText() === "Close";
        },
        actions: new Press()
    });
    Then.waitFor({
        id: "oauthSharePointPath",
        viewName: "AgentDetail",
        searchOpenDialogs: true,
        success: function (element: UI5Element) {
            const agent = element.getModel("agent") as JSONModel;
            const servers = agent.getProperty("/data/mcp_servers") as { oauth?: { path?: string } }[];
            Opa5.assert.strictEqual(servers.length, 3, "the dialog is still open and no server was added");
        }
    });

    // A changed pin and an untouched secret: saved without client_secret.
    iTypeInTheServerDialog(When, "oauthSharePointPath", "Planning/Other.xlsx");
    When.waitFor({ id: "serverConfirm", viewName: "AgentDetail", searchOpenDialogs: true, actions: new Press() });
    When.waitFor({ id: "saveAgentButton", viewName: "AgentDetail", actions: new Press() });
    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            const saved = backend.agents.find((a) => a.id === 101)?.mcp_servers[2];
            const oauth = (saved?.oauth ?? {}) as Record<string, unknown>;
            Opa5.assert.strictEqual(saved?.url, "builtin:sharepoint");
            Opa5.assert.strictEqual(saved?.auth_mode, "app_only");
            Opa5.assert.notOk("client_secret" in oauth, "no client_secret is posted: the stored one is kept");
            const rest = Object.assign({}, oauth);
            delete rest.has_client_secret;
            Opa5.assert.deepEqual(rest, {
                client_id: stored.client_id, token_url: stored.token_url, scope: stored.scope,
                site: stored.site, library: stored.library, path: "Planning/Other.xlsx",
                views: SHAREPOINT_VIEWS
            }, "everything else is posted as stored, with the changed path");
        }
    });
    Then.iStopTheApp();
});

opaTest("a credential that scheduled runs depend on and that has expired is named in a strip above the list", function (Given: Common, When: Common, Then: Common) {
    Given.waitFor({
        success: function () {
            backend.reset();
            backend.credentialProblems = [{
                agent: "btp-agent", server_key: "https://mcp.example.test/mcp", principal: "uuid-1234",
                token_state: "expired", expires_at: "2026-08-20T08:00:00+00:00"
            }];
            backend.install();
        }
    });
    Given.iStartMyUIComponent({ componentConfig: { name: "com.agent.admin", async: true }, hash: "agents" });
    Then.waitFor({
        id: "credentialAlert",
        viewName: "Agents",
        matchers: function (element: UI5Element) {
            return (element as MessageStrip).getVisible();
        },
        success: function (strip: UI5Element) {
            const text = (strip as MessageStrip).getText();
            Opa5.assert.ok(text.indexOf("\"btp-agent\"") !== -1, "the strip names the agent");
            Opa5.assert.ok(text.indexOf("has expired") !== -1, "and says the credential has expired");
            Opa5.assert.ok(backend.requests.indexOf("GET credential-health") !== -1, "read from credential-health");
        },
        errorMessage: "The credential strip did not show the expired credential"
    });
    Then.iStopTheApp();
});
