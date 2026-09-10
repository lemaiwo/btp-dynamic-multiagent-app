import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Button from "sap/m/Button";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Input from "sap/m/Input";
import type MultiComboBox from "sap/m/MultiComboBox";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Select from "sap/m/Select";
import type Table from "sap/m/Table";
import type Text from "sap/m/Text";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.infrabel.agentadmin.view.", autoWait: true });

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

    // Second toolset
    When.waitFor({ id: "addServerButton", viewName: "AgentDetail", actions: new Press() });
    When.waitFor({ id: "serverUrl", viewName: "AgentDetail", searchOpenDialogs: true, actions: new EnterText({ text: "builtin:gmail" }) });
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
