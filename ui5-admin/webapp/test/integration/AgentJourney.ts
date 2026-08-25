import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import type Input from "sap/m/Input";
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
