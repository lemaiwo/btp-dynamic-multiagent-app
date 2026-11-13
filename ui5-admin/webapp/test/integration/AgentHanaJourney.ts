import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import HashChanger from "sap/ui/core/routing/HashChanger";
import Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type Input from "sap/m/Input";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type MessageStrip from "sap/m/MessageStrip";
import type Select from "sap/m/Select";
import type Switch from "sap/m/Switch";
import type Table from "sap/m/Table";
import type UI5Element from "sap/ui/core/Element";
import type { AgentInput, McpServer } from "../../service/types";
import Common, { backend } from "./pages/Common";
import { messageOf } from "./pages/ODataList";

// The agent's one "SAP HANA (monitoring)" entry in the server dialog
// (builtin:hana, agents/hana/config.py, model/hanaEntry.ts). Fixtures
// (FakeBackend.reset): gmail-agent (101) has two other servers.

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Agent journey: SAP HANA (monitoring) entry");

const VIEW = "AgentDetail";
const HANA = "builtin:hana";
const PUT_101 = "PUT agents/101";
const TENANT = { name: "erp_tenant", destination: "hana-erp-tenant" };
const SYSTEMDB = { name: "erp_systemdb", destination: "hana-erp-systemdb" };
const CONFIRM = "This lets the agent run its own SELECT statements on the system and monitoring views "
    + "(SYS, _SYS_STATISTICS, M_*) of \"erp_tenant\" (hana-erp-tenant), \"erp_systemdb\" (hana-erp-systemdb). "
    + "Catalogue data, statement texts with their values and trace file contents come back unmasked. Continue?";

/** What the entry shows, read from its controls. */
interface Entry {
    rows: { name: Input; destination: Input }[];
    allowSelect: boolean;
    strip: (id: string) => string;
    visible: (id: string) => boolean;
}

function entryOf(element: UI5Element): Entry {
    const table = element as Table;
    const sibling = <T extends UI5Element>(id: string): T => (
        Element.getElementById(table.getId().replace(/hanaSystemsTable$/, id)) as T
    );
    return {
        rows: table.getItems().map((item) => {
            const cells = (item as ColumnListItem).getCells();
            return { name: cells[0] as Input, destination: cells[1] as Input };
        }),
        allowSelect: sibling<Switch>("hanaAllowSelect").getState(),
        strip: (id: string) => {
            const control = sibling<MessageStrip>(id);
            return control.getVisible() && (control.getParent() as Control).getVisible() ? control.getText() : "";
        },
        visible: (id: string) => {
            let control = sibling<Control>(id) as Control | null;
            while (control && control.getMetadata().getName() !== "sap.m.Dialog") {
                if (control.getVisible && !control.getVisible()) {
                    return false;
                }
                control = control.getParent() as Control | null;
            }
            return true;
        }
    };
}

function iSeeTheEntry(Then: Common, what: string, ready: (entry: Entry) => boolean, assert: (entry: Entry) => void): void {
    Then.waitFor({
        id: "hanaSystemsTable",
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

/** Gives the stored agent a hana entry, as the server answers it. */
function storeEntry(id: number, oauth: Record<string, unknown>, url = HANA): void {
    const agent = backend.agents.filter((a) => a.id === id)[0];
    agent.mcp_servers = agent.mcp_servers.concat([{
        url, auth_mode: "destination", oauth: { ...oauth, has_client_secret: false } as McpServer["oauth"]
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

function iEditServer(When: Common, index: number): void {
    When.waitFor({
        id: "serversTable",
        viewName: VIEW,
        actions: function (element: UI5Element | null) {
            const item = (element as Table).getItems()[index] as unknown as { getCells(): { getItems(): { firePress(): void }[] }[] };
            item.getCells()[2].getItems()[0].firePress();
        }
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

function iPressInTheDialog(When: Common, id: string): void {
    When.waitFor({ id, viewName: VIEW, searchOpenDialogs: true, actions: new Press() });
}

/** Types into row `row` of the systems table, as typing and leaving the field do. */
function iType(When: Common, row: number, field: "name" | "destination", value: string): void {
    When.waitFor({
        id: "hanaSystemsTable",
        viewName: VIEW,
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) { return (element as Table).getItems().length > row; },
        actions: function (element: UI5Element | null) {
            const input = entryOf(element as UI5Element).rows[row][field];
            input.setValue(value);
            input.fireLiveChange({ value });
            input.fireChange({ value });
        }
    });
}

function iSwitchAllowSelect(When: Common, on: boolean): void {
    When.waitFor({
        id: "hanaAllowSelect",
        viewName: VIEW,
        searchOpenDialogs: true,
        actions: function (element: UI5Element | null) {
            const control = element as Switch;
            control.setState(on);
            control.fireChange({ state: on });
        }
    });
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

function iPressSave(When: Common): void {
    When.waitFor({ id: "saveAgentButton", viewName: VIEW, actions: new Press() });
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

/** Adds the toolset and types the two systems. */
function iAddTwoSystems(When: Common, Then: Common): void {
    iAddAServer(When);
    iPickTheKind(When, HANA);
    iSeeTheEntry(Then, "one empty row", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.strictEqual(entry.rows[0].name.getValue(), "", "a new entry starts with one empty row");
        Opa5.assert.strictEqual(entry.allowSelect, false, "SELECT starts off");
    });
    iType(When, 0, "name", TENANT.name);
    iType(When, 0, "destination", TENANT.destination);
    iPressInTheDialog(When, "hanaAddSystem");
    iType(When, 1, "name", SYSTEMDB.name);
    iType(When, 1, "destination", SYSTEMDB.destination);
}

opaTest("two systems are saved as exactly { systems }; the dialog hides every other setting", function (Given: Common, When: Common, Then: Common) {
    let before: McpServer[] = [];
    iOpenAgent(Given, When, 101, function () {
        before = JSON.parse(JSON.stringify(backend.agents.filter((a) => a.id === 101)[0].mcp_servers)) as McpServer[];
    });
    iAddTwoSystems(When, Then);
    iSeeTheEntry(Then, "the two rows", (entry) => entry.rows.length === 2 && entry.rows[1].destination.getValue() !== "",
        function (entry) {
            ["oauthDestination", "serverAuthMode", "oauthUserContext", "oauthLookback", "serverUrl", "oauthAllowSend",
                "odataEntryServices", "inaEntryQueries"].forEach((id) => {
                Opa5.assert.strictEqual(entry.visible(id), false, `${id} is hidden: the entry has no such setting`);
            });
            Opa5.assert.strictEqual(entry.strip("hanaEntryDuplicate"), "", "the first entry");
        });
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry in the form", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2], { url: HANA, auth_mode: "destination", oauth: { systems: [TENANT, SYSTEMDB] } },
            "OK puts exactly { systems } into the form");
    }, (data) => data.mcp_servers.length === 3);
    iPressSave(When);
    iSeeTheSavedAgent(Then, "the saved agent", function () {
        const sent = sentServers();
        Opa5.assert.deepEqual(sent[2], { url: HANA, auth_mode: "destination", oauth: { systems: [TENANT, SYSTEMDB] } },
            "the PUT carries exactly { url: builtin:hana, auth_mode: destination, oauth: { systems } }");
        Opa5.assert.deepEqual(sent.slice(0, 2), before, "the other servers of the agent are sent unchanged");
    });
    Then.iStopTheApp();
});

opaTest("switching Allow SELECT on asks; Cancel is the default and keeps it off", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddTwoSystems(When, Then);
    iSwitchAllowSelect(When, true);
    iSeeAMessage(Then, "the question", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), CONFIRM, "it names the systems and what the switch opens");
        const focus = Element.getElementById((dialog as Dialog).getInitialFocus()) as Button | undefined;
        Opa5.assert.strictEqual(focus?.getText(), "Cancel", "Cancel has the initial focus");
    });
    iAnswer(When, "Cancel");
    iSeeTheEntry(Then, "the switch back off", (entry) => !entry.allowSelect, function () {
        Opa5.assert.ok(true, "Cancel put the switch back off");
    });
    iSwitchAllowSelect(When, true);
    iAnswer(When, "Allow SELECT");
    iSeeTheEntry(Then, "the switch on", (entry) => entry.allowSelect, function () {
        Opa5.assert.ok(true, "the answer switched it on");
    });
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeTheSavedAgent(Then, "the saved agent", function () {
        Opa5.assert.deepEqual(sentServers()[2], {
            url: HANA, auth_mode: "destination", oauth: { systems: [TENANT, SYSTEMDB], allow_select: true }
        }, "allow_select is sent as true");
    });
    Then.iStopTheApp();
});

opaTest("a second entry is refused in the dialog; a stored entry in another spelling opens with its controls", function (Given: Common, When: Common, Then: Common) {
    const TEXT = "This agent already has a SAP HANA (monitoring) entry (row 3 of its toolsets). An agent has one: edit "
        + "that entry and add the systems there.";
    iOpenAgent(Given, When, 101, () => storeEntry(101, { systems: [TENANT, SYSTEMDB], allow_select: true }, "Builtin:HANA/"));
    iSeeTheServers(When, 3);
    iAddAServer(When);
    iPickTheKind(When, HANA);
    iSeeTheEntry(Then, "the refusal in the dialog", (entry) => !!entry.strip("hanaEntryDuplicate"), function (entry) {
        Opa5.assert.strictEqual(entry.strip("hanaEntryDuplicate"), TEXT, "said as soon as the toolset is picked");
    });
    iType(When, 0, "name", "other");
    iType(When, 0, "destination", "hana-other");
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the refusal of OK", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), TEXT, "OK is refused with the same text");
    });
    iAnswer(When, "Close");
    iPressInTheDialog(When, "serverCancel");
    iSeeTheForm(Then, "the form after Cancel", function (data) {
        Opa5.assert.strictEqual(data.mcp_servers.length, 3, "no second entry in the form");
    });

    iEditServer(When, 2);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.rows.map((row) => [row.name.getValue(), row.destination.getValue()]),
            [[TENANT.name, TENANT.destination], [SYSTEMDB.name, SYSTEMDB.destination]], "both systems as stored");
        Opa5.assert.strictEqual(entry.allowSelect, true, "the stored switch");
        Opa5.assert.strictEqual(entry.strip("hanaEntryDuplicate"), "", "the entry itself is no second one");
    });
    iSwitchAllowSelect(When, false);
    iSwitchAllowSelect(When, true);
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry in its canonical spelling", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2], {
            url: HANA, auth_mode: "destination", oauth: { systems: [TENANT, SYSTEMDB], allow_select: true }
        }, "no question for a switch the entry already had; OK writes the canonical url");
    }, (data) => data.mcp_servers[2]?.url === HANA);
    Then.iStopTheApp();
});

opaTest("a server 422 at oauth.systems[1].name marks that row", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, function () {
        storeEntry(101, { systems: [TENANT, SYSTEMDB] });
        backend.hanaRefuseNext = { server: 2, system: 1, field: "name" };
    });
    iSeeTheServers(When, 3);
    iPressSave(When);
    Then.waitFor({
        id: "serversTable",
        viewName: VIEW,
        check: () => backend.countRequests(PUT_101) === 1,
        success: function () { Opa5.assert.ok(true, "the save was refused"); }
    });
    iEditServer(When, 2);
    iSeeTheEntry(Then, "the marked row", (entry) => entry.rows.length === 2 && entry.rows[1].name.getValueState() === "Error",
        function (entry) {
            Opa5.assert.strictEqual(entry.rows[0].name.getValueState(), "None", "the first row is not marked");
            Opa5.assert.ok(entry.rows[1].name.getValueStateText().indexOf("oauth.systems[1].name:") === 0,
                "the second row's name carries the server's text");
        });
    iType(When, 1, "name", "erp_systemdb2");
    iSeeTheEntry(Then, "the mark gone", (entry) => entry.rows[1].name.getValueState() === "None", function () {
        Opa5.assert.ok(true, "typing in the row takes the mark away");
    });
    Then.iStopTheApp();
});

const PROD = { name: "prod_tenant", destination: "hana-prod-tenant" };

/** Waits until no message box is open, so that the next step sees the dialog. */
function iSeeNoMessage(Then: Common, what: string): void {
    Then.waitFor({
        id: "hanaSystemsTable",
        viewName: VIEW,
        searchOpenDialogs: true,
        check: () => document.querySelectorAll(".sapMMessageBox").length === 0,
        success: function () { Opa5.assert.ok(true, what); }
    });
}

opaTest("Scenario A: a system added after Allow SELECT was confirmed is asked about at OK; Cancel keeps the dialog open", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, HANA);
    iType(When, 0, "name", "dev");
    iType(When, 0, "destination", "hana-dev");
    iSwitchAllowSelect(When, true);
    iSeeAMessage(Then, "the question at the switch", function (dialog) {
        Opa5.assert.ok(messageOf(dialog).indexOf("(SYS, _SYS_STATISTICS, M_*) of \"dev\" (hana-dev).") > -1, "it names dev only, with its destination");
    });
    iAnswer(When, "Allow SELECT");
    iSeeNoMessage(Then, "the switch is on");
    iPressInTheDialog(When, "hanaAddSystem");
    iType(When, 1, "name", PROD.name);
    iType(When, 1, "destination", PROD.destination);
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the question again at OK", function (dialog) {
        Opa5.assert.ok(messageOf(dialog).indexOf("(SYS, _SYS_STATISTICS, M_*) of \"dev\" (hana-dev), \"prod_tenant\" (hana-prod-tenant).") > -1,
            "it names every system of the entry now");
        const focus = Element.getElementById((dialog as Dialog).getInitialFocus()) as Button | undefined;
        Opa5.assert.strictEqual(focus?.getText(), "Cancel", "Cancel has the initial focus");
    });
    iAnswer(When, "Cancel");
    iSeeTheEntry(Then, "the dialog still open, unchanged", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.strictEqual(entry.allowSelect, true, "the switch stays as it is");
        Opa5.assert.strictEqual(entry.rows[1].name.getValue(), PROD.name, "the system stays");
        Opa5.assert.strictEqual(backend.countRequests(PUT_101), 0, "nothing was saved");
    });
    iPressInTheDialog(When, "serverConfirm");
    iAnswer(When, "Allow SELECT");
    iSeeTheForm(Then, "the entry with both systems", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2], {
            url: HANA, auth_mode: "destination",
            oauth: { systems: [{ name: "dev", destination: "hana-dev" }, PROD], allow_select: true }
        }, "the answer goes on with OK");
    }, (data) => data.mcp_servers.length === 3);
    Then.iStopTheApp();
});

opaTest("Scenario B: a stored entry that allows SELECT asks at OK when its systems were replaced", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, () => storeEntry(101, { systems: [TENANT], allow_select: true }));
    iSeeTheServers(When, 3);
    iEditServer(When, 2);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 1 && entry.allowSelect, function () {
        Opa5.assert.ok(true, "one system, SELECT on");
    });
    iType(When, 0, "name", PROD.name);
    iType(When, 0, "destination", PROD.destination);
    iPressInTheDialog(When, "hanaAddSystem");
    iType(When, 1, "name", SYSTEMDB.name);
    iType(When, 1, "destination", SYSTEMDB.destination);
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the question at OK", function (dialog) {
        Opa5.assert.ok(messageOf(dialog).indexOf("(SYS, _SYS_STATISTICS, M_*) of \"prod_tenant\" (hana-prod-tenant), \"erp_systemdb\" (hana-erp-systemdb).") > -1,
            "it names the systems now in the entry");
    });
    iAnswer(When, "Cancel");
    iSeeNoMessage(Then, "the dialog stays open");
    iPressInTheDialog(When, "serverCancel");
    iSeeTheForm(Then, "the stored entry untouched", function (data) {
        Opa5.assert.deepEqual((data.mcp_servers[2].oauth as Record<string, unknown>).systems, [TENANT],
            "Cancel of the dialog kept the stored entry");
    });
    Then.iStopTheApp();
});

opaTest("a confirmed system whose destination changed is asked about at OK, naming the new destination", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, () => storeEntry(101, { systems: [TENANT], allow_select: true }));
    iSeeTheServers(When, 3);
    iEditServer(When, 2);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 1 && entry.allowSelect, function () {
        Opa5.assert.ok(true, "one confirmed system, SELECT on");
    });
    iType(When, 0, "destination", "hana-erp-other");
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the question at OK", function (dialog) {
        Opa5.assert.ok(messageOf(dialog).indexOf("(SYS, _SYS_STATISTICS, M_*) of \"erp_tenant\" (hana-erp-other).") > -1,
            "the question shows the new destination");
    });
    iAnswer(When, "Allow SELECT");
    iSeeTheForm(Then, "the entry on its new destination", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2].oauth, {
            systems: [{ name: TENANT.name, destination: "hana-erp-other" }], allow_select: true
        }, "the answer goes on with OK");
    }, (data) => JSON.stringify(data.mcp_servers[2]?.oauth ?? {}).indexOf("hana-erp-other") > -1);
    Then.iStopTheApp();
});
