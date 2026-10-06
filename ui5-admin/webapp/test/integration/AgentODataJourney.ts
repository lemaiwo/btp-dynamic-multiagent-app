import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type CheckBox from "sap/m/CheckBox";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type HBox from "sap/m/HBox";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type List from "sap/m/List";
import type ListItem from "sap/ui/core/ListItem";
import type MessageStrip from "sap/m/MessageStrip";
import type MultiComboBox from "sap/m/MultiComboBox";
import type Select from "sap/m/Select";
import type StandardListItem from "sap/m/StandardListItem";
import type Table from "sap/m/Table";
import type Text from "sap/m/Text";
import type UI5Element from "sap/ui/core/Element";
import type { AgentInput, McpServer } from "../../service/types";
import Common, { backend } from "./pages/Common";
import { buttonsOf, messageOf } from "./pages/ODataList";

// The agent's one "OData services" entry in the server dialog, and the
// question of the agent's Save when it newly gives writes. Fixtures
// (FakeBackend.reset): btp-agent (100) has a run endpoint, gmail-agent (101)
// has none and two other servers. The catalogue: "purchase-requisitions-jobs"
// (technical user, with writes), "business-partners" and
// "purchase-requisitions" (signed-in user, read only),
// "purchase-requisitions-v4" (disabled).

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Agent journey: OData services entry");

const VIEW = "AgentDetail";
const ODATA = "builtin:odata";
const JOBS = "purchase-requisitions-jobs";
const PARTNERS = "business-partners";
const V4 = "purchase-requisitions-v4";
const MARKUP = "<b>x</b> {y}";
const PUT_101 = "PUT agents/101";

const JOBS_LINE = "Purchase requisitions (jobs): Requisition item (update), Item text (create, update), Release item";
const OPENS_ON = "Opens the write operations that are enabled in the catalogue for these services:";
const OPENS_OFF = "Off: this agent can only read. Ticking it would open:";
const LATER = "Whatever is enabled in the catalogue for these services later opens too, without another question. "
    + "Every write call is audited.";
const SAVE_LATER = "Whatever is enabled in the catalogue for these services later opens too, without another question.";
const AUDITED = "Every write call is audited.";
const SAVE_TITLE = "Allow writes for this agent?";

/** What the entry shows, read from its controls. */
interface Entry {
    keys: string[];
    options: string[][];
    rows: string[][];
    ticked: boolean;
    writeText: string;
    warning: string;
    missing: string;
    disabled: string;
    duplicate: string;
    visible: (id: string) => boolean;
    /** The text of a control as the browser shows it. */
    rendered: (id: string) => string;
    dom: HTMLElement;
}

function entryOf(element: UI5Element): Entry {
    const box = element as MultiComboBox;
    const sibling = <T extends UI5Element>(id: string): T => (
        Element.getElementById(box.getId().replace(/odataEntryServices$/, id)) as T
    );
    const strip = (id: string): string => {
        const control = sibling<MessageStrip>(id);
        return control.getVisible() ? control.getText() : "";
    };
    let dialog = box as unknown as { getParent(): unknown; getMetadata(): { getName(): string } } | null;
    while (dialog && dialog.getMetadata().getName() !== "sap.m.Dialog") {
        dialog = dialog.getParent() as typeof dialog;
    }
    return {
        keys: box.getSelectedKeys(),
        options: box.getItems().map((item) => [item.getKey(), item.getText(), (item as ListItem).getAdditionalText()]),
        rows: (sibling<List>("odataEntrySelected").getItems() as StandardListItem[])
            .map((item) => [item.getTitle(), item.getDescription(), item.getInfo(), String(item.getInfoState())]),
        ticked: sibling<CheckBox>("odataEntryAllowWrite").getSelected(),
        writeText: sibling<Text>("odataEntryAllowWriteText").getText(false),
        warning: strip("odataEntryWarning"),
        missing: strip("odataEntryMissing"),
        disabled: strip("odataEntryDisabled"),
        duplicate: strip("odataEntryDuplicate"),
        visible: (id: string) => sibling<Control>(id).getVisible(),
        rendered: (id: string) => (sibling<Control>(id).getDomRef()?.textContent ?? "").trim(),
        dom: (dialog as unknown as Dialog).getDomRef() as HTMLElement
    };
}

/** Waits until the entry is `ready`, then hands it to `assert`. */
function iSeeTheEntry(Then: Common, what: string, ready: (entry: Entry) => boolean, assert: (entry: Entry) => void): void {
    Then.waitFor({
        id: "odataEntryServices",
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

/** Gives the stored agent an OData services entry, as the server stores it. */
function storeEntry(id: number, services: string[], allowWrite: boolean): void {
    const agent = backend.agents.filter((a) => a.id === id)[0];
    agent.mcp_servers = agent.mcp_servers.concat([{
        url: ODATA, auth_mode: "destination",
        oauth: { services, ...(allowWrite ? { allow_write: true } : {}), has_client_secret: false }
    }]);
}

function iAddAServer(When: Common): void {
    When.waitFor({ id: "addServerButton", viewName: VIEW, actions: new Press() });
}

function iEditServer(When: Common, row: number, rows: number): void {
    When.waitFor({
        id: "serversTable",
        viewName: VIEW,
        matchers: function (element: UI5Element) { return (element as Table).getItems().length === rows; },
        actions: function (element: UI5Element | null) {
            const item = (element as Table).getItems()[row] as ColumnListItem;
            ((item.getCells()[2] as HBox).getItems()[0] as Button).firePress();
        },
        errorMessage: `Not seen: ${rows} toolsets`
    });
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
        id: "odataEntryServices",
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

/** Waits for the one open message box (the server dialog may be open under it). */
function iSeeAMessage(Then: Common, what: string, assert: (dialog: UI5Element) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, `one question is open: ${what}`);
            assert(dialogs[0]);
        },
        errorMessage: `No question appeared: ${what}`
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
        errorMessage: `No question with a button "${text}"`
    });
}

function writes(): number {
    return backend.requests.filter((r) => /^(PUT|POST|DELETE)/.test(r)).length;
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

function iSeeTheList(Then: Common, what: string, assert: () => void, ready: () => boolean): void {
    Then.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        check: ready,
        success: assert,
        errorMessage: `Not seen: ${what}`
    });
}

function sentServers(): McpServer[] {
    return (backend.bodies[PUT_101]?.mcp_servers ?? []) as McpServer[];
}

opaTest("picking OData services shows the services and the write box, and hides destination, auth mode, user context and lookback", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, ODATA);

    iSeeTheEntry(Then, "the catalogue in the box", (entry) => entry.options.length === 4, function (entry) {
        Opa5.assert.deepEqual(entry.options, [
            [PARTNERS, "Business partners", "Signed-in user"],
            ["purchase-requisitions", "Purchase requisitions", "Signed-in user"],
            [JOBS, "Purchase requisitions (jobs)", "Technical user"],
            [V4, "Purchase requisitions (V4)", "Signed-in user, disabled"]
        ], "every catalogue service with who its calls run as");
        Opa5.assert.deepEqual(entry.keys, [], "nothing is selected for a new entry");
        Opa5.assert.strictEqual(entry.ticked, false, "Allow writes starts off");
        Opa5.assert.strictEqual(entry.writeText, "Off or on, this depends on the services: select them first.");
        Opa5.assert.strictEqual(entry.visible("odataEntryAllowWrite"), true, "the write box is shown");
        ["oauthDestination", "serverAuthMode", "oauthUserContext", "oauthLookback", "serverUrl", "oauthAllowSend"].forEach((id) => {
            Opa5.assert.strictEqual(entry.visible(id), false, `${id} is hidden: the entry has no such setting`);
        });
        Opa5.assert.strictEqual(backend.countRequests("GET odata/services"), 1, "the catalogue was read once");
    });

    iSelect(When, [PARTNERS]);
    iSeeTheEntry(Then, "the selected service", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.deepEqual(entry.rows, [
            ["Business partners", "Look up suppliers and their addresses", "Runs as: Signed-in user", "Information"]
        ], "title, purpose and identity of the selected service");
        Opa5.assert.strictEqual(entry.warning, "", "no warning: this agent has no run endpoint");
    });
    Then.iStopTheApp();
});

opaTest("Allow writes says what it opens, by service, before anything is saved", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS, PARTNERS]);

    iSeeTheEntry(Then, "what a tick would open", (entry) => entry.writeText.indexOf("reading") === -1 && entry.rows.length === 2, function (entry) {
        Opa5.assert.strictEqual(
            entry.writeText, `${OPENS_OFF}\n${JOBS_LINE}\nBusiness partners: no write operation enabled`,
            "unticked: read only, and what a tick would open, by service"
        );
    });

    iPressInTheDialog(When, "odataEntryAllowWrite");
    iSeeTheEntry(Then, "what the tick opens", (entry) => entry.ticked, function (entry) {
        Opa5.assert.strictEqual(
            entry.writeText, `${OPENS_ON}\n${JOBS_LINE}\nBusiness partners: no write operation enabled\n${LATER}`,
            "ticked: the enabled writes by service, in the words of the service page"
        );
        Opa5.assert.strictEqual(writes(), 0, "ticking sends nothing");
    });

    iSelect(When, [PARTNERS]);
    iSeeTheEntry(Then, "nothing to open yet", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.strictEqual(
            entry.writeText,
            "None of the selected services has a write operation enabled, so \"Allow writes\" opens nothing yet. "
            + "It will open whatever is enabled in the catalogue for these services later.",
            "with no enabled write it says so, and that later ones open too"
        );
        Opa5.assert.strictEqual(entry.ticked, true, "the tick stays");
    });
    Then.iStopTheApp();
});

opaTest("Save asks once when Allow writes is newly given, sends a real boolean and leaves the other servers as they were", function (Given: Common, When: Common, Then: Common) {
    let before: McpServer[] = [];
    iOpenAgent(Given, When, 101, function () {
        before = JSON.parse(JSON.stringify(backend.agents.filter((a) => a.id === 101)[0].mcp_servers)) as McpServer[];
    });
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS, PARTNERS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iPressInTheDialog(When, "serverConfirm");

    iSeeTheForm(Then, "the entry in the form", function (data) {
        Opa5.assert.deepEqual(
            data.mcp_servers[2], { url: ODATA, auth_mode: "destination", oauth: { services: [JOBS, PARTNERS], allow_write: true } },
            "OK puts exactly { services, allow_write } into the form"
        );
        Opa5.assert.strictEqual(writes(), 0, "and stores nothing: that is the agent's Save");
    }, (data) => data.mcp_servers.length === 3);

    iPressSave(When);
    iSeeAMessage(Then, "the write question", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Saving lets the agent \"gmail-agent\" change data in SAP through its OData services.\n\n"
            + `It opens these write operations, enabled in the catalogue:\n${JOBS_LINE}\n\n${SAVE_LATER}\n\n${AUDITED}`,
            "the question names the agent and what is opened; a service without writes is not listed as one"
        );
        Opa5.assert.strictEqual((dialog as Dialog).getTitle(), SAVE_TITLE, "under its own title");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Save", "Cancel"], "Save or Cancel");
        Opa5.assert.strictEqual(writes(), 0, "nothing is sent before the answer");
    });
    // A second press while the question is open must not send or ask again.
    Then.waitFor({
        id: "saveAgentButton",
        viewName: VIEW,
        autoWait: false,
        success: function (element: UI5Element) {
            (element as Button).firePress();
        }
    });
    iSeeAMessage(Then, "still one question after a second press", function () {
        Opa5.assert.strictEqual(writes(), 0, "a second press sends nothing");
    });
    iAnswer(When, "Cancel");
    iSeeTheForm(Then, "the form after Cancel", function (data) {
        Opa5.assert.strictEqual(writes(), 0, "Cancel sends nothing");
        Opa5.assert.strictEqual(data.mcp_servers.length, 3, "and the entry stays in the form");
    });

    iPressSave(When);
    iSeeAMessage(Then, "the question again", function () {
        Opa5.assert.ok(true, "the next Save asks again");
    });
    iAnswer(When, "Save");

    iSeeTheList(Then, "the saved agent", function () {
        const sent = sentServers();
        Opa5.assert.deepEqual(sent[2], { url: ODATA, auth_mode: "destination", oauth: { services: [JOBS, PARTNERS], allow_write: true } },
            "the PUT carries exactly { services, allow_write }");
        Opa5.assert.strictEqual((sent[2].oauth as { allow_write: unknown }).allow_write, true, "allow_write is the boolean true");
        Opa5.assert.deepEqual(sent.slice(0, 2), before, "the other servers of the agent are sent unchanged");
        Opa5.assert.strictEqual(backend.countRequests(PUT_101), 1, "one PUT");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

opaTest("a service with writes added to an entry that already allows writes is asked about, against the stored agent; unticking is not", function (Given: Common, When: Common, Then: Common) {
    const ADDED = "The agent \"gmail-agent\" already has \"Allow writes\". Saving adds services with write operations to it.\n\n"
        + `It opens these write operations, enabled in the catalogue:\n${JOBS_LINE}\n\n${SAVE_LATER}\n\n${AUDITED}`;
    iOpenAgent(Given, When, 101, () => storeEntry(101, [PARTNERS], true));
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [PARTNERS], "the stored selection");
        Opa5.assert.strictEqual(entry.ticked, true, "and the stored tick");
    });
    iSelect(When, [PARTNERS, JOBS]);
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeAMessage(Then, "the question about the added service", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), ADDED, "only the added service with writes is named");
    });
    iAnswer(When, "Cancel");

    // The dialog now opens with the added service already in it: the
    // comparison is with the stored agent, so Save still asks.
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the entry as edited", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [PARTNERS, JOBS], "the edit is still there");
    });
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeAMessage(Then, "the question, again", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), ADDED, "asked again: nothing was stored in between");
        Opa5.assert.strictEqual(writes(), 0, "and nothing sent so far");
    });
    iAnswer(When, "Cancel");

    // Unticking takes writes away: no question.
    iEditServer(When, 2, 3);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeTheList(Then, "the saved agent", function () {
        const oauth = sentServers()[2].oauth as Record<string, unknown>;
        Opa5.assert.deepEqual(oauth, { services: [PARTNERS, JOBS], allow_write: false }, "unticked is sent as false, with the selection");
        Opa5.assert.strictEqual(oauth.allow_write, false, "the boolean false, not a missing key");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no question was asked");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

opaTest("adding a service without writes, or removing one, saves without a question", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, () => storeEntry(101, [JOBS, "purchase-requisitions"], true));
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.strictEqual(entry.ticked, true);
    });
    // One removed, one without writes added.
    iSelect(When, [JOBS, PARTNERS]);
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeTheList(Then, "the saved agent", function () {
        Opa5.assert.deepEqual(sentServers()[2].oauth, { services: [JOBS, PARTNERS], allow_write: true }, "saved as selected");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no question: nothing new can be written");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

opaTest("an agent with a run endpoint is warned about a service that runs as the signed-in user", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 100);
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS]);
    iSeeTheEntry(Then, "a technical-user service", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.strictEqual(entry.warning, "", "a service with a technical user works in scheduled runs: no warning");
    });
    iSelect(When, [JOBS, PARTNERS]);
    iSeeTheEntry(Then, "the warning", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.strictEqual(
            entry.warning,
            "\"Business partners\" runs as the signed-in user. This agent has a run endpoint; scheduled runs have no "
            + "user, so calls to that service are refused there.",
            "the service is named, with why its calls are refused there"
        );
    });
    iSelect(When, [JOBS, PARTNERS, "purchase-requisitions"]);
    iSeeTheEntry(Then, "the warning for two", (entry) => entry.rows.length === 3, function (entry) {
        Opa5.assert.strictEqual(
            entry.warning,
            "These services run as the signed-in user: \"Business partners\", \"Purchase requisitions\". This agent has a run "
            + "endpoint; scheduled runs have no user, so calls to those services are refused there."
        );
    });
    Then.iStopTheApp();
});

opaTest("the selection survives another toolset and back, an unrelated edit, a refused Save and reopening", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS, PARTNERS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");

    // One click on another toolset, and back.
    iPickTheKind(When, "builtin:jira");
    Then.waitFor({
        id: "oauthProject",
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function () { Opa5.assert.ok(true, "the Jira fields are shown"); }
    });
    iPickTheKind(When, ODATA);
    iSeeTheEntry(Then, "the selection after the round trip", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [JOBS, PARTNERS], "the services are still selected");
        Opa5.assert.strictEqual(entry.ticked, true, "and Allow writes is still ticked");
        Opa5.assert.strictEqual(backend.countRequests("GET odata/services"), 1, "the catalogue is not read again");
    });
    iPressInTheDialog(When, "serverConfirm");

    // An edit of an unrelated field, then a Save the server refuses.
    When.waitFor({ id: "agentDescription", viewName: VIEW, actions: new EnterText({ text: "Changed description" }) });
    When.waitFor({
        id: "saveAgentButton",
        viewName: VIEW,
        actions: function (element: UI5Element | null) {
            backend.failNext = {
                path: "agents/101", method: "PUT", status: 422,
                body: { detail: [{ loc: ["body", "description"], msg: "refused for the test", type: "value_error" }] }
            };
            new Press().executeOn(element as Control);
        }
    });
    iSeeAMessage(Then, "the write question", function () { Opa5.assert.ok(true, "Save asks"); });
    iAnswer(When, "Save");
    iSeeTheForm(Then, "the form after the refusal", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2].oauth, { services: [JOBS, PARTNERS], allow_write: true },
            "the refused Save leaves the entry in the form");
        Opa5.assert.strictEqual(backend.agents.filter((a) => a.id === 101)[0].mcp_servers.length, 2, "and nothing was stored");
    }, () => backend.countRequests(PUT_101) === 1);

    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the reopened entry", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [JOBS, PARTNERS], "reopening shows the selection");
        Opa5.assert.strictEqual(entry.ticked, true, "and the tick");
        Opa5.assert.deepEqual(entry.rows.map((row) => row[0]), ["Purchase requisitions (jobs)", "Business partners"]);
    });
    Then.iStopTheApp();
});

opaTest("a service deleted from the catalogue is shown as missing and is removed only on the admin's answer", function (Given: Common, When: Common, Then: Common) {
    const GONE = "retired-service";
    iOpenAgent(Given, When, 101, () => storeEntry(101, [GONE, PARTNERS], false));
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the missing service", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [GONE, PARTNERS], "it is still selected: opening removes nothing");
        Opa5.assert.deepEqual(entry.rows[0], [GONE, "", "Not in the catalogue", "Error"], "and is shown as missing");
        Opa5.assert.deepEqual(entry.options.filter((option) => option[0] === GONE), [[GONE, GONE, "Not in the catalogue"]]);
        Opa5.assert.strictEqual(
            entry.missing,
            `"${GONE}" is no longer in the catalogue: it was deleted or renamed after this agent was saved. The agent cannot `
            + "be saved while it is selected. Remove it from the selection, or press OK to be asked."
        );
    });

    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the question about the missing service", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `No longer in the catalogue: "${GONE}". The agent cannot be saved with a service that does not exist. `
            + "Remove from the selection of this agent?"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Remove", "Cancel"]);
    });
    iAnswer(When, "Cancel");
    iSeeTheEntry(Then, "the entry after Cancel", (entry) => document.querySelectorAll(".sapMDialogOpen").length === 1 && entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [GONE, PARTNERS], "Cancel removes nothing and the dialog stays open");
    });

    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the question, again", function () { Opa5.assert.ok(true, "OK asks again"); });
    iAnswer(When, "Remove");
    iSeeTheForm(Then, "the form without the missing service", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2].oauth, { services: [PARTNERS], allow_write: false },
            "only the missing service went, on the admin's answer");
        Opa5.assert.strictEqual(writes(), 0, "nothing is stored before the agent's Save");
    }, (data) => ((data.mcp_servers[2]?.oauth as { services?: string[] })?.services ?? []).length === 1);
    Then.iStopTheApp();
});

opaTest("a second OData services entry is refused in the dialog and points to the first", function (Given: Common, When: Common, Then: Common) {
    const TEXT = "This agent already has an OData services entry (row 3 of its toolsets). An agent has one: edit that "
        + "entry and select the services there.";
    iOpenAgent(Given, When, 101, () => storeEntry(101, [PARTNERS], false));
    When.waitFor({
        id: "serversTable",
        viewName: VIEW,
        matchers: function (element: UI5Element) { return (element as Table).getItems().length === 3; },
        success: function () { Opa5.assert.ok(true, "the agent has its entry"); }
    });
    // The existing entry is looked at and changed in the dialog first, then
    // cancelled: the next dialog must not start from what that one showed.
    iEditServer(When, 2, 3);
    iSelect(When, [PARTNERS, JOBS]);
    iSeeTheEntry(Then, "the edit of the existing entry", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [PARTNERS, JOBS]);
    });
    iPressInTheDialog(When, "serverCancel");
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSeeTheEntry(Then, "the refusal in the dialog", (entry) => !!entry.duplicate && entry.options.length === 4, function (entry) {
        Opa5.assert.strictEqual(entry.duplicate, TEXT, "said as soon as the toolset is picked");
        Opa5.assert.deepEqual(entry.keys, [], "a new entry starts with nothing selected, whatever the last dialog showed");
        Opa5.assert.deepEqual(entry.rows, [], "and lists no service");
        Opa5.assert.strictEqual(entry.ticked, false, "and is not ticked");
    });
    iSelect(When, [JOBS]);
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the refusal of OK", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), TEXT, "OK is refused with the same text");
    });
    iAnswer(When, "Close");
    iSeeTheEntry(Then, "the dialog after the refusal", () => document.querySelectorAll(".sapMDialogOpen").length === 1, function (entry) {
        const data = (Element.getElementById(entry.dom.id) as UI5Element).getModel("agent") as JSONModel;
        Opa5.assert.strictEqual((data.getProperty("/data/mcp_servers") as McpServer[]).length, 3, "no second entry in the form");
    });

    // Editing the existing entry is not a second one.
    iPressInTheDialog(When, "serverCancel");
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the existing entry", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.strictEqual(entry.duplicate, "", "the entry being edited is not refused");
    });
    Then.iStopTheApp();
});

opaTest("titles and purposes from the catalogue are shown as text, in the dialog and in the Save question", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 100, function () {
        const jobs = backend.odataServices.filter((s) => s.name === JOBS)[0];
        jobs.title = MARKUP;
        jobs.purpose = MARKUP;
        jobs.user_context = true;
        jobs.definition.entity_sets[0].title = MARKUP;
        backend.agents[0].name = MARKUP;
    });
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iSeeTheEntry(Then, "the markup as text", (entry) => entry.ticked && entry.writeText.indexOf("reading") === -1, function (entry) {
        Opa5.assert.deepEqual(entry.rows, [[MARKUP, MARKUP, "Runs as: Signed-in user", "Information"]], "title and purpose as they are");
        Opa5.assert.deepEqual(entry.options.filter((option) => option[0] === JOBS)[0].slice(0, 2), [JOBS, MARKUP]);
        Opa5.assert.strictEqual(
            entry.writeText, `${OPENS_ON}\n${MARKUP}: ${MARKUP} (update), Item text (create, update), Release item\n${LATER}`
        );
        Opa5.assert.strictEqual(
            entry.warning,
            `"${MARKUP}" runs as the signed-in user. This agent has a run endpoint; scheduled runs have no user, so calls to `
            + "that service are refused there."
        );
        Opa5.assert.strictEqual(entry.dom.querySelectorAll("b").length, 0, "no markup of the title became an element");
        // A strip that took formatted text would drop the tag: compare what is on the screen.
        // (The strip puts its own "Message Strip Warning" for screen readers in front.)
        Opa5.assert.strictEqual(
            entry.rendered("odataEntryWarning").slice(-entry.warning.length), entry.warning, "the warning shows the literal title"
        );
        Opa5.assert.strictEqual(
            entry.rendered("odataEntryAllowWriteText").replace(/\s+/g, " "), entry.writeText.replace(/\s+/g, " "),
            "the write text shows the literal titles"
        );
        Opa5.assert.strictEqual(entry.rendered("odataEntrySelected").split(MARKUP).length - 1, 2, "the list shows title and purpose literally");
    });
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeAMessage(Then, "the write question", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `Saving lets the agent "${MARKUP}" change data in SAP through its OData services.\n\n`
            + `It opens these write operations, enabled in the catalogue:\n${MARKUP}: ${MARKUP} (update), Item text (create, update), `
            + `Release item\n\n${SAVE_LATER}\n\n${AUDITED}`
        );
        const dom = (dialog as Dialog).getDomRef() as HTMLElement;
        Opa5.assert.strictEqual(dom.querySelectorAll("b").length, 0, "as text in the question too");
        Opa5.assert.ok((dom.textContent ?? "").indexOf(`agent "${MARKUP}"`) !== -1, "the literal name is on the screen");
    });
    Then.iStopTheApp();
});

opaTest("Cancel leaves the agent exactly as it was", function (Given: Common, When: Common, Then: Common) {
    let before = "";
    iOpenAgent(Given, When, 101, () => storeEntry(101, [PARTNERS], false));
    iSeeTheForm(Then, "the agent as loaded", function (data) {
        before = JSON.stringify(data);
    }, (data) => data.mcp_servers.length === 3);

    iEditServer(When, 2, 3);
    iSelect(When, [JOBS, V4]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iSeeTheEntry(Then, "the edit in the dialog", (entry) => entry.ticked && entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [JOBS, V4], "the dialog holds the edit");
    });
    iPressInTheDialog(When, "serverCancel");
    iSeeTheForm(Then, "the agent after Cancel", function (data) {
        Opa5.assert.deepEqual(JSON.parse(JSON.stringify(data)), JSON.parse(before), "the form is exactly what it was");
        Opa5.assert.strictEqual(writes(), 0, "and nothing was sent");
    });

    // A new server that is cancelled leaves no trace either.
    iAddAServer(When);
    iPickTheKind(When, "builtin:jira");
    iPressInTheDialog(When, "serverCancel");
    iSeeTheForm(Then, "the agent after the second Cancel", function (data) {
        Opa5.assert.deepEqual(JSON.parse(JSON.stringify(data)), JSON.parse(before), "still exactly what it was");
        Opa5.assert.strictEqual(writes(), 0, "and nothing was sent");
    });
    Then.iStopTheApp();
});

opaTest("reopening a stored entry shows it; a disabled service is said to be skipped; confirming without a service is refused", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, () => storeEntry(101, [JOBS, V4], true));
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [JOBS, V4], "the stored services");
        Opa5.assert.strictEqual(entry.ticked, true, "the stored Allow writes");
        Opa5.assert.deepEqual(entry.rows[1].slice(2), ["Runs as: Signed-in user · disabled", "Warning"]);
        Opa5.assert.strictEqual(
            entry.disabled,
            "\"Purchase requisitions (V4)\" is disabled in the catalogue. It can stay selected, but the agent does not get it "
            + "until it is enabled there."
        );
    });

    iSelect(When, []);
    iPressInTheDialog(When, "serverConfirm");
    iSeeAMessage(Then, "the refusal", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), "Select at least one service.");
    });
    iAnswer(When, "Close");
    iSeeTheEntry(Then, "the dialog after the refusal", () => document.querySelectorAll(".sapMDialogOpen").length === 1, function (entry) {
        Opa5.assert.strictEqual(entry.ticked, true, "the dialog stays open, the tick as it was");
        const agent = (Element.getElementById(entry.dom.id) as UI5Element).getModel("agent") as JSONModel;
        Opa5.assert.deepEqual(
            (agent.getProperty("/data/mcp_servers") as McpServer[])[2].oauth,
            { services: [JOBS, V4], allow_write: true, has_client_secret: false }, "and the form keeps the stored entry"
        );
    });
    Then.iStopTheApp();
});

// --- U8 review: the Save question cannot be skipped, the saved entry is explicit ---

const SWITCHED_ON = "Saving lets the agent \"gmail-agent\" change data in SAP through its OData services.";
const UNKNOWN = "Saving gives the agent \"gmail-agent\" write access through its OData services, or adds services to it. "
    + "What that opens could not be read, so it is not listed here. Save anyway?";

/** Presses Save after `prepare` ran, in the same step. */
function iPressSaveAfter(When: Common, prepare: () => void): void {
    When.waitFor({
        id: "saveAgentButton",
        viewName: VIEW,
        actions: function (element: UI5Element | null) {
            prepare();
            new Press().executeOn(element as Control);
        }
    });
}

opaTest("Save still asks when what it opens cannot be worked out, and Cancel sends nothing", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry in the form", function () {
        Opa5.assert.strictEqual(writes(), 0, "nothing sent so far");
    }, (data) => data.mcp_servers.length === 3);

    // The catalogue answers something that is no list: the question cannot be built.
    iPressSaveAfter(When, function () {
        backend.failNext = { path: "odata/services", method: "GET", status: 200, body: { detail: "not a list" } };
    });
    iSeeAMessage(Then, "the question without a list", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), UNKNOWN, "it says that what is opened could not be read, and asks");
        Opa5.assert.strictEqual((dialog as Dialog).getTitle(), SAVE_TITLE, "under the title of the write question");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Save", "Cancel"], "Save or Cancel");
        const focus = Element.getElementById((dialog as Dialog).getInitialFocus() as string) as Button | undefined;
        Opa5.assert.strictEqual(focus?.getText(), "Cancel", "Cancel is where the focus starts");
        Opa5.assert.strictEqual(writes(), 0, "nothing is sent before the answer");
    });
    iAnswer(When, "Cancel");
    iSeeTheForm(Then, "the form after Cancel", function (data) {
        Opa5.assert.strictEqual(writes(), 0, "Cancel sends no PUT");
        Opa5.assert.strictEqual(data.mcp_servers.length, 3, "and the entry stays in the form");
    });

    // The catalogue cannot be read at all: the services are read one by one and the question is asked.
    iPressSaveAfter(When, function () {
        backend.failNext = { path: "odata/services", method: "GET", status: 500, body: { detail: "down" } };
    });
    iSeeAMessage(Then, "the question with the catalogue unread", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog).indexOf(SWITCHED_ON), 0, "the write question is asked");
        Opa5.assert.strictEqual(writes(), 0, "and nothing is sent before the answer");
    });
    iAnswer(When, "Cancel");
    iSeeTheForm(Then, "the form after the second Cancel", function () {
        Opa5.assert.strictEqual(writes(), 0, "Cancel sends no PUT");
    });

    // Answered with Save, the question that names nothing does save.
    iPressSaveAfter(When, function () {
        backend.failNext = { path: "odata/services", method: "GET", status: 200, body: { detail: "not a list" } };
    });
    iSeeAMessage(Then, "the question without a list, again", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), UNKNOWN);
    });
    iAnswer(When, "Save");
    iSeeTheList(Then, "the saved agent", function () {
        Opa5.assert.deepEqual(sentServers()[2].oauth, { services: [JOBS], allow_write: true }, "saved on the admin's answer");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

opaTest("an edit made while the Save question is being read is not saved with it: the PUT is what the question named", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [PARTNERS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry in the form", function () {
        Opa5.assert.ok(true, "a service without writes, with Allow writes");
    }, (data) => data.mcp_servers.length === 3);

    // Save: its catalogue read is kept waiting.
    iPressSaveAfter(When, function () { backend.catalogueHeld = true; });
    Then.waitFor({
        check: function () { return backend.heldCatalogueReads() === 1; },
        success: function () {
            backend.catalogueHeld = false; // only the read of Save waits
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no question yet");
        },
        errorMessage: "Not seen: the catalogue read of Save, waiting"
    });

    // Meanwhile a service with writes is added to the entry.
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the entry, reopened", (entry) => entry.rows.length === 1 && entry.options.length === 4, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [PARTNERS]);
    });
    iSelect(When, [PARTNERS, JOBS]);
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the edited entry in the form", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2].oauth, { services: [PARTNERS, JOBS], allow_write: true }, "the form holds the edit");
        Opa5.assert.strictEqual(writes(), 0, "nothing sent so far");
        backend.releaseCatalogue();
    }, (data) => ((data.mcp_servers[2]?.oauth as { services?: string[] })?.services ?? []).length === 2);

    iSeeAMessage(Then, "the question of the Save that was pressed", function (dialog) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            `${SWITCHED_ON}\n\nNo write operation is enabled in its services yet. Whatever is enabled there later opens for this `
            + `agent without another question.\n\n${AUDITED}`,
            "it names what the form held when Save was pressed"
        );
    });
    iAnswer(When, "Save");
    iSeeTheList(Then, "the saved agent", function () {
        Opa5.assert.deepEqual(sentServers()[2].oauth, { services: [PARTNERS], allow_write: true },
            "the PUT carries what the question was about, not the later edit");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

opaTest("an agent saved without opening its OData entry sends the entry as exactly services and a boolean allow_write", function (Given: Common, When: Common, Then: Common) {
    let before: McpServer[] = [];
    iOpenAgent(Given, When, 101, function () {
        before = JSON.parse(JSON.stringify(backend.agents.filter((a) => a.id === 101)[0].mcp_servers)) as McpServer[];
        storeEntry(101, [PARTNERS], false);
    });
    iSeeTheForm(Then, "the agent as stored", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2].oauth, { services: [PARTNERS], has_client_secret: false },
            "the form holds the entry as the server answered it: no allow_write key");
    }, (data) => data.mcp_servers.length === 3);

    // The description only.
    When.waitFor({ id: "agentDescription", viewName: VIEW, actions: new EnterText({ text: "Changed description" }) });
    iPressSave(When);
    iSeeTheList(Then, "the saved agent", function () {
        const sent = sentServers();
        Opa5.assert.strictEqual(backend.bodies[PUT_101]?.description, "Changed description", "the description was sent");
        Opa5.assert.deepEqual(sent[2], { url: ODATA, auth_mode: "destination", oauth: { services: [PARTNERS], allow_write: false } },
            "the entry is sent as exactly { services, allow_write }");
        Opa5.assert.strictEqual((sent[2].oauth as { allow_write: unknown }).allow_write, false, "allow_write is the boolean false");
        Opa5.assert.deepEqual(sent.slice(0, 2), before, "the other servers are sent unchanged");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no question: nothing new is given");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

opaTest("a tick that was cancelled is not carried into the next dialog, of this agent or another", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101);
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iSeeTheEntry(Then, "the tick", (entry) => entry.ticked && entry.rows.length === 1, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [JOBS], "ticked, with a service");
    });
    iPressInTheDialog(When, "serverCancel");
    iSeeTheForm(Then, "the form after Cancel", function (data) {
        Opa5.assert.strictEqual(data.mcp_servers.length, 2, "no entry was added");
    });

    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSeeTheEntry(Then, "a new entry of the same agent", (entry) => entry.options.length === 4, function (entry) {
        Opa5.assert.strictEqual(entry.ticked, false, "Allow writes is not ticked");
        Opa5.assert.deepEqual(entry.keys, [], "and nothing is selected");
    });
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iSeeTheEntry(Then, "the tick, again", (entry) => entry.ticked, function () { Opa5.assert.ok(true, "ticked again"); });
    iPressInTheDialog(When, "serverCancel");

    iSeeTheForm(Then, "the first agent", function () {
        HashChanger.getInstance().setHash("agents/100");
    });
    iSeeTheForm(Then, "the other agent", function (data) {
        Opa5.assert.strictEqual(data.name, "btp-agent");
    }, (data) => data.name === "btp-agent");
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSeeTheEntry(Then, "a new entry of another agent", (entry) => entry.options.length === 4, function (entry) {
        Opa5.assert.strictEqual(entry.ticked, false, "Allow writes is not ticked there either");
        Opa5.assert.deepEqual(entry.keys, [], "and nothing is selected");
        Opa5.assert.strictEqual(writes(), 0, "nothing was sent");
    });
    Then.iStopTheApp();
});

opaTest("another toolset and back on a stored entry keeps its services and the stored Allow writes", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, () => storeEntry(101, [JOBS, PARTNERS], true));
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.strictEqual(entry.ticked, true, "the stored tick");
    });
    iPickTheKind(When, "builtin:jira");
    Then.waitFor({
        id: "oauthProject",
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function () { Opa5.assert.ok(true, "the Jira fields are shown"); }
    });
    iPickTheKind(When, ODATA);
    iSeeTheEntry(Then, "the entry after the round trip", (entry) => entry.rows.length === 2, function (entry) {
        Opa5.assert.deepEqual(entry.keys, [JOBS, PARTNERS], "the stored services are still selected");
        Opa5.assert.strictEqual(entry.ticked, true, "and Allow writes is still ticked");
    });
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry in the form", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[2], { url: ODATA, auth_mode: "destination", oauth: { services: [JOBS, PARTNERS], allow_write: true } },
            "OK puts the stored services and the stored switch back, and nothing of the other toolset");
    }, (data) => data.mcp_servers.length === 3 && !("has_client_secret" in (data.mcp_servers[2].oauth as object)));

    iPressSave(When);
    iSeeTheList(Then, "the saved agent", function () {
        Opa5.assert.deepEqual(sentServers()[2].oauth, { services: [JOBS, PARTNERS], allow_write: true }, "saved as it was stored");
        Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "no question: the stored agent already had it");
    }, () => backend.countRequests(PUT_101) === 1);
    Then.iStopTheApp();
});

/** Names the new agent and gives it an OData services entry with Allow writes. */
function iFillANewAgentWithWrites(When: Common, Then: Common): void {
    When.waitFor({ id: "agentName", viewName: VIEW, actions: new EnterText({ text: "new-agent" }) });
    When.waitFor({ id: "agentDescription", viewName: VIEW, actions: new EnterText({ text: "A new agent" }) });
    When.waitFor({ id: "agentInstructions", viewName: VIEW, actions: new EnterText({ text: "Do the thing." }) });
    iAddAServer(When);
    iPickTheKind(When, ODATA);
    iSelect(When, [JOBS]);
    iPressInTheDialog(When, "odataEntryAllowWrite");
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry of the new agent", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers, [{ url: ODATA, auth_mode: "destination", oauth: { services: [JOBS], allow_write: true } }]);
    }, (data) => data.mcp_servers.length === 1);
}

const NEW_QUESTION = "Saving lets the agent \"new-agent\" change data in SAP through its OData services.\n\n"
    + `It opens these write operations, enabled in the catalogue:\n${JOBS_LINE}\n\n${SAVE_LATER}\n\n${AUDITED}`;

opaTest("a new agent with Allow writes is asked about before it is created", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");
    When.waitFor({ id: "addAgentButton", viewName: "Agents", actions: new Press() });
    iFillANewAgentWithWrites(When, Then);

    iPressSave(When);
    iSeeAMessage(Then, "the write question for a new agent", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), NEW_QUESTION, "the question names the new agent and what is opened");
        Opa5.assert.strictEqual(writes(), 0, "nothing is sent before the answer");
    });
    iAnswer(When, "Cancel");
    iSeeTheForm(Then, "the form after Cancel", function () {
        Opa5.assert.strictEqual(writes(), 0, "Cancel creates nothing");
    });

    iPressSave(When);
    iSeeAMessage(Then, "the question again", function () { Opa5.assert.ok(true, "asked again"); });
    iAnswer(When, "Save");
    iSeeTheList(Then, "the created agent", function () {
        Opa5.assert.deepEqual(backend.bodies["POST agents"]?.mcp_servers,
            [{ url: ODATA, auth_mode: "destination", oauth: { services: [JOBS], allow_write: true } }],
            "the POST carries exactly { services, allow_write }");
        Opa5.assert.strictEqual(backend.countRequests("POST agents"), 1, "one POST");
    }, () => backend.countRequests("POST agents") === 1);
    Then.iStopTheApp();
});

opaTest("coming from an agent that has writes, a new agent with Allow writes is still asked about", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 100, () => storeEntry(100, [JOBS], true));
    iSeeTheForm(Then, "the agent that has writes", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers[data.mcp_servers.length - 1].oauth,
            { services: [JOBS], allow_write: true, has_client_secret: false }, "the stored agent allows writes through the same service");
        HashChanger.getInstance().setHash("agents/new");
    }, (data) => data.name === "btp-agent" && data.mcp_servers.some((server) => server.url === ODATA));
    iSeeTheForm(Then, "the empty form of a new agent", function (data) {
        Opa5.assert.deepEqual(data.mcp_servers, [], "a new agent has no toolsets");
    }, (data) => data.name === "" && data.mcp_servers.length === 0);
    iFillANewAgentWithWrites(When, Then);

    iPressSave(When);
    iSeeAMessage(Then, "the write question for the new agent", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), NEW_QUESTION, "compared with nothing stored, not with the agent seen before");
        Opa5.assert.strictEqual(writes(), 0, "nothing is sent before the answer");
    });
    iAnswer(When, "Cancel");
    iSeeTheForm(Then, "the form after Cancel", function () {
        Opa5.assert.strictEqual(writes(), 0, "Cancel creates nothing");
    });
    Then.iStopTheApp();
});

// --- final review -------------------------------------------------------------

const NOT_LIVE = ", but the running agents could not be reloaded: they still use the previous configuration. Go to Settings and press Reload to make the change active.";

opaTest("an entry stored in another spelling of the url opens with its services and Allow writes on screen", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, function () {
        storeEntry(101, [JOBS], true);
        const servers = backend.agents.filter((a) => a.id === 101)[0].mcp_servers;
        servers[servers.length - 1].url = "Builtin:OData";
    });
    iEditServer(When, 2, 3);
    iSeeTheEntry(Then, "the stored entry", (entry) => entry.rows.length === 1, function (entry) {
        Opa5.assert.strictEqual(entry.visible("odataEntryServices"), true, "the services box is shown");
        Opa5.assert.strictEqual(entry.visible("odataEntryAllowWrite"), true, "and so is Allow writes");
        Opa5.assert.strictEqual(entry.ticked, true, "with the stored tick: nothing is written back unseen");
        Opa5.assert.deepEqual(entry.keys, [JOBS]);
    });
    iPressInTheDialog(When, "serverConfirm");
    iSeeTheForm(Then, "the entry after OK", function (data) {
        Opa5.assert.strictEqual(data.mcp_servers[2].url, ODATA, "held in the spelling the server stores");
    });
    Then.iStopTheApp();
});

opaTest("an agent save the running agents did not get says so in a box that stays, and no outcome key is ever sent back", function (Given: Common, When: Common, Then: Common) {
    iOpenAgent(Given, When, 101, function () {
        storeEntry(101, [PARTNERS], true);
        // As if an answer of a save had been kept as the form's data.
        Object.assign(backend.agents.filter((a) => a.id === 101)[0], { reloaded: false, reload_failed: true });
        backend.reloadOutcome = "failed";
    });
    iSeeTheForm(Then, "the agent as stored", function () { /* loaded */ }, (data) => data.mcp_servers.length === 3);
    // Unticking Allow writes is a narrowing: no question, but it must be live.
    iEditServer(When, 2, 3);
    When.waitFor({ id: "odataEntryAllowWrite", viewName: VIEW, searchOpenDialogs: true, actions: new Press() });
    iPressInTheDialog(When, "serverConfirm");
    iPressSave(When);
    iSeeAMessage(Then, "the failed reload", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), `The agent was saved${NOT_LIVE}`);
        Opa5.assert.deepEqual(buttonsOf(dialog), ["OK"], "it stays until it is closed");
        const body = backend.bodies[PUT_101] ?? {};
        Opa5.assert.strictEqual((sentServers()[2].oauth as { allow_write: unknown }).allow_write, false, "the narrowing was sent");
        Opa5.assert.notOk("reloaded" in body, "the body carries no reloaded");
        Opa5.assert.notOk("reload_failed" in body, "and no reload_failed");
    });
    iAnswer(When, "OK");
    Then.iStopTheApp();
});

opaTest("an agent delete the running agents did not get says so in a box that stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");
    When.waitFor({
        id: "agentsTable",
        viewName: "Agents",
        matchers: function (element: UI5Element) { return (element as Table).getItems().length > 0; },
        actions: function (element: UI5Element | null) {
            backend.reloadOutcome = "failed";
            const item = (element as Table).getItems()
                .filter((row) => (row.getBindingContext("agents")?.getObject() as { id: number }).id === 101)[0] as ColumnListItem;
            const button = item.findAggregatedObjects(true, (child) => (
                child.isA("sap.m.Button") && (child as Button).getIcon() === "sap-icon://delete"
            ))[0] as Button;
            button.firePress();
        }
    });
    iAnswer(When, "OK");
    iSeeAMessage(Then, "the failed reload", function (dialog) {
        Opa5.assert.strictEqual(messageOf(dialog), `The agent was deleted${NOT_LIVE}`);
        Opa5.assert.strictEqual(backend.agents.filter((a) => a.id === 101).length, 0, "the agent is deleted");
        Opa5.assert.strictEqual(backend.countRequests("DELETE agents/101"), 1);
    });
    iAnswer(When, "OK");
    Then.iStopTheApp();
});
