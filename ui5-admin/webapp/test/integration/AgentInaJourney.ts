import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import HashChanger from "sap/ui/core/routing/HashChanger";
import Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type ListItem from "sap/ui/core/ListItem";
import type MessageStrip from "sap/m/MessageStrip";
import type MultiComboBox from "sap/m/MultiComboBox";
import type Select from "sap/m/Select";
import type Table from "sap/m/Table";
import type UI5Element from "sap/ui/core/Element";
import type { AgentInput, McpServer } from "../../service/types";
import Common, { backend } from "./pages/Common";
import { messageOf } from "./pages/ODataList";

// The agent's one "Analytical queries (InA)" entry in the server dialog
// (design addendum (k), part 2 point 3). Fixtures (FakeBackend.reset,
// FakeIna.reset): gmail-agent (101) has no run endpoint and two other
// servers; the registered queries are "flight-revenue" (signed-in user) and
// "cost-centre-actuals" (technical user, disabled).

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Agent journey: analytical queries (InA) entry");

const VIEW = "AgentDetail";
const INA = "builtin:ina";
const REVENUE = "flight-revenue";
const COSTS = "cost-centre-actuals";
const PUT_101 = "PUT agents/101";

/** What the entry shows, read from its controls. */
interface Entry {
    keys: string[];
    options: string[][];
    strip: (id: string) => string;
    visible: (id: string) => boolean;
    dialog: Dialog;
}

function entryOf(element: UI5Element): Entry {
    const box = element as MultiComboBox;
    const sibling = <T extends UI5Element>(id: string): T => (
        Element.getElementById(box.getId().replace(/inaEntryQueries$/, id)) as T
    );
    let dialog = box as unknown as { getParent(): unknown; getMetadata(): { getName(): string } } | null;
    while (dialog && dialog.getMetadata().getName() !== "sap.m.Dialog") {
        dialog = dialog.getParent() as typeof dialog;
    }
    return {
        keys: box.getSelectedKeys(),
        options: box.getItems().map((item) => [item.getKey(), item.getText(), (item as ListItem).getAdditionalText()]),
        strip: (id: string) => {
            const control = sibling<MessageStrip>(id);
            return control.getVisible() ? control.getText() : "";
        },
        visible: (id: string) => sibling<Control>(id).getVisible(),
        dialog: dialog as unknown as Dialog
    };
}

function iSeeTheEntry(Then: Common, what: string, ready: (entry: Entry) => boolean, assert: (entry: Entry) => void): void {
    Then.waitFor({
        id: "inaEntryQueries",
        viewName: VIEW,
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) { return ready(entryOf(element)); },
        success: function (element: UI5Element) { assert(entryOf(element)); },
        errorMessage: `Not seen: ${what}`
    });
}

function iOpenAgent(Given: Common, When: Common, id: number, prepare?: () => void): void {
    Given.iStartTheApp("agents");
    When.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        success: function () {
            if (prepare) {
                prepare();
            }
            HashChanger.getInstance().setHash(`agents/${id}`);
        }
    });
}

/** Gives the stored agent an InA entry, as the server answers it. */
function storeEntry(id: number, queries: string[]): void {
    const agent = backend.agents.filter((a) => a.id === id)[0];
    agent.mcp_servers = agent.mcp_servers.concat([{
        url: INA, auth_mode: "destination", oauth: { queries, has_client_secret: false } as McpServer["oauth"]
    }]);
}

function iSeeTheServers(When: Common, rows: number): void {
    When.waitFor({
        id: "serversTable",
        viewName: VIEW,
        matchers: function (element: UI5Element) { return (element as Table).getItems().length === rows; },
        success: function () { Opa5.assert.ok(true, `the agent has ${rows} toolsets`); },
        errorMessage: `Not seen: ${rows} toolsets`
    });
}

function iAddAServer(When: Common): void {
    When.waitFor({ id: "addServerButton", viewName: VIEW, actions: new Press() });
}

function iPickTheKind(When: Common, key: string): void {
    When.waitFor({
        id: "serverKind",
        viewName: VIEW,
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const select = element as Select;
            select.setSelectedKey(key);
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });
}

/** Sets the selection of the box and tells it so, as a pick or a removed token does. */
function iSelect(When: Common, keys: string[]): void {
    When.waitFor({
        id: "inaEntryQueries",
        viewName: VIEW,
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const box = element as MultiComboBox;
            box.setSelectedKeys(keys);
            box.fireSelectionFinish({ selectedItems: box.getSelectedItems() });
        }
    });
}

function iPressInTheDialog(When: Common, id: string): void {
    When.waitFor({ id, viewName: VIEW, searchOpenDialogs: true, actions: new Press() });
}

function iPressSave(When: Common): void {
    When.waitFor({ id: "saveAgentButton", viewName: VIEW, actions: new Press() });
}

function isMessage(dialog: UI5Element): boolean {
    return (dialog as Dialog).getType() === "Message";
}

function iSeeAMessage(Then: Common, what: string, assert: (dialog: UI5Element) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, `one message is open: ${what}`);
            assert(dialogs[0]);
        },
        errorMessage: `No message appeared: ${what}`
    });
}

function iAnswer(When: Common, text: string): void {
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        actions: function (element: UI5Element | null) {
            const button = (element as Dialog).getButtons().filter((b) => b.getText() === text)[0];
            new Press().executeOn(button);
        },
        errorMessage: `No message with a button "${text}"`
    });
}

/** Waits for the agent page with no dialog open, then hands over the form's data. */
function iSeeTheForm(Then: Common, what: string, assert: (data: AgentInput) => void, ready?: (data: AgentInput) => boolean): void {
    Then.waitFor({
        id: "serversTable",
        viewName: VIEW,
        matchers: function (element: UI5Element) {
            const data = (element.getModel("agent") as JSONModel).getProperty("/data") as AgentInput;
            return document.querySelectorAll(".sapMDialogOpen").length === 0 && (!ready || ready(data));
        },
        success: function (element: UI5Element) {
            assert((element.getModel("agent") as JSONModel).getProperty("/data") as AgentInput);
        },
        errorMessage: `Not seen: ${what}`
    });
}

function iSeeTheSavedAgent(Then: Common, what: string, assert: () => void): void {
    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        check: () => backend.countRequests(PUT_101) === 1,
        success: assert,
        errorMessage: `Not seen: ${what}`
    });
}

function sentServers(): McpServer[] {
    return (backend.bodies[PUT_101]?.mcp_servers ?? []) as McpServer[];
}

opaTest("picking the toolset offers the registered queries with who they run as; two ticked are saved as exactly the queries", function (Given: Common, When: Common, Then: Common) {
    let before: McpServer[] = [];
    iOpenAgent(Given, When, 101, function () {
        before = JSON.parse(JSON.stringify(backend.agents.filter((a) => a.id === 101)[0].mcp_servers)) as McpServer[];
    });
    iAddAServer(When);
    iPickTheKind(When, INA);

    iSeeTheEntry(Then, "the registered queries in the box", (entry) => entry.options.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.options, [
            [COSTS, "Cost centre actuals", "Runs as: Technical user · disabled"],
            [REVENUE, "Flight revenue", "Runs as: Signed-in user"]
        ], "every registered query with who its calls run as");
        Opa5.assert.deepEqual(entry.keys, [], "nothing is selected for a new entry");
        ["oauthDestination", "serverAuthMode", "oauthUserContext", "oauthLookback", "serverUrl", "oauthAllowSend",
            "odataEntryServices"].forEach((id) => {
            Opa5.assert.strictEqual(entry.visible(id), false, `${id} is hidden: the entry has no such setting`);
        });
        Opa5.assert.strictEqual(backend.countRequests("GET ina/queries"), 1, "the queries were read once");
    });

    iSelect(When, [REVENUE, COSTS]);
    iSeeTheEntry(Then, "the note on the disabled query", (entry) => !!entry.strip("inaEntryDisabled"), function (entry) {
        Opa5.assert.strictEqual(entry.strip("inaEntryDisabled"),
            "\"Cost centre actuals\" is disabled under \"Analytical queries\". It can stay selected, but the agent "
            + "does not get it until it is enabled there.", "a disabled query is said to be skipped");
        Opa5.assert.strictEqual(entry.strip("inaEntryWarning"), "", "no warning: this agent has no run endpoint");
        Opa5.assert.strictEqual(entry.strip("inaEntryDuplicate"), "", "the first entry");
    });
    iPressInTheDialog(When, "serverConfirm");

    iSeeTheForm(Then, "the entry in the form", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2], { url: INA, auth_mode: "destination", oauth: { queries: [REVENUE, COSTS] } },
            "OK puts exactly { queries } into the form");
    }, (data) => data.mcp_servers.length === 3);

    iPressSave(When);
    iSeeTheSavedAgent(Then, "the saved agent", function () {
        const sent = sentServers();
        Opa5.assert.deepEqual(sent[2], { url: INA, auth_mode: "destination", oauth: { queries: [REVENUE, COSTS] } },
            "the PUT carries exactly { url: builtin:ina, auth_mode: destination, oauth: { queries } }");
        Opa5.assert.deepEqual(Object.keys(sent[2].oauth ?? {}), ["queries"], "no destination, no user_context, no echo");
        Opa5.assert.deepEqual(sent.slice(0, 2), before, "the other servers of the agent are sent unchanged");
    });
    Then.iStopTheApp();
});

opaTest("a second analytical queries entry is refused in the dialog; a stored entry is sent as exactly its queries", function (Given: Common, When: Common, Then: Common) {
    const TEXT = "This agent already has an analytical queries entry (row 3 of its toolsets). An agent has one: edit that "
        + "entry and select the queries there.";
    iOpenAgent(Given, When, 101, () => storeEntry(101, [REVENUE]));
    iSeeTheServers(When, 3);
    iAddAServer(When);
    iPickTheKind(When, INA);
    iSeeTheEntry(Then, "the refusal in the dialog", (entry) => !!entry.strip("inaEntryDuplicate"), function (entry) {
        Opa5.assert.strictEqual(entry.strip("inaEntryDuplicate"), TEXT, "said as soon as the toolset is picked");
        Opa5.assert.deepEqual(entry.keys, [], "a new entry starts with nothing selected");
    });
    iSelect(When, [COSTS]);
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the refusal of OK", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), TEXT, "OK is refused with the same text");
    });
    iAnswer(When, "Close");
    iPressInTheDialog(When, "serverCancel");
    iSeeTheForm(Then, "the form after Cancel", function (data) {
        Opa5.assert.strictEqual(data.mcp_servers.length, 3, "no second entry in the form");
    });

    iPressSave(When);
    iSeeTheSavedAgent(Then, "the saved agent", function () {
        Opa5.assert.deepEqual(sentServers()[2], { url: INA, auth_mode: "destination", oauth: { queries: [REVENUE] } },
            "the stored entry, never opened, is sent without the server's has_client_secret echo");
    });
    Then.iStopTheApp();
});

opaTest("a query that is no longer registered is shown as such and removed only on the admin's answer", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, () => storeEntry(101, ["gone", REVENUE]));
    iSeeTheServers(When, 3);
    When.waitFor({
        id: "serversTable",
        viewName: VIEW,
        actions: function (element: UI5Element | null) {
            const item = (element as Table).getItems()[2] as unknown as { getCells(): { getItems(): { firePress(): void }[] }[] };
            item.getCells()[2].getItems()[0].firePress();
        }
    });
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.keys.length === 2 && !!entry.strip("inaEntryMissing"), function (entry) {
        Opa5.assert.deepEqual(entry.keys, ["gone", REVENUE], "both stored names stay selected");
        Opa5.assert.deepEqual(entry.options[entry.options.length - 1], ["gone", "gone", "Not registered"],
            "the name the list lacks keeps its item, said to be not registered");
        Opa5.assert.ok(entry.strip("inaEntryMissing").indexOf("\"gone\" is no longer registered") === 0, "and a note says so");
    });
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the question", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), "No longer registered: \"gone\". The agent cannot be saved with a query "
            + "that does not exist. Remove from the selection of this agent?");
    });
    iAnswer(When, "Remove");
    iSeeTheForm(Then, "the entry without the query", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2], { url: INA, auth_mode: "destination", oauth: { queries: [REVENUE] } },
            "removed on the answer, and OK went on");
    }, (data) => JSON.stringify(data.mcp_servers[2]?.oauth) === JSON.stringify({ queries: [REVENUE] }));
    Then.iStopTheApp();
});
