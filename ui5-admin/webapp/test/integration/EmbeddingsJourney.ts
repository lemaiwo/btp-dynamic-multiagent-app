import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import HashChanger from "sap/ui/core/routing/HashChanger";
import Element from "sap/ui/core/Element";
import type Dialog from "sap/m/Dialog";
import type Select from "sap/m/Select";
import type Button from "sap/m/Button";
import type MessageStrip from "sap/m/MessageStrip";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import { choose } from "./pages/DataCatalog";
import { buttonsOf, messageOf } from "./pages/ODataList";
import { ID, VIEW, offersOf, panelOf, toastText } from "./pages/Embeddings";
import { MODEL_NOT_DEPLOYED } from "./FakeEmbeddings";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Embeddings journey");

const STATUS = "GET data-catalog/embeddings";
const SAVE = "PUT data-catalog/embeddings";
const RUN = "POST data-catalog/embeddings/run";
const DEPLOY = "POST data-catalog/embeddings/deploy";

/** The status polls every 2 s while a run is in flight; a wait longer than two of them. */
const LONGER_THAN_TWO_POLLS_MS = 4500;

/** Waits for the panel to show an answer of the status, then hands it over. */
function iSeeThePanel(Then: Common, what: string, assert: (panel: UI5Element) => void,
    ready: (panel: UI5Element) => boolean = (panel) => panelOf(panel).model !== ""): void {
    Then.waitFor({
        id: ID.panel,
        viewName: VIEW,
        check: ready,
        success: assert,
        errorMessage: `The panel does not show: ${what}`
    });
}

function isMessage(dialog: UI5Element): boolean {
    return (dialog as Dialog).getType() === "Message";
}

/** Waits for the one message box and hands it over. */
function iSeeAMessage(Then: Common, what: string, assert: (dialog: Dialog) => void): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 1, `one message box is open: ${what}`);
            assert(dialogs[0] as Dialog);
        },
        errorMessage: `No message box: ${what}`
    });
}

function iAnswer(When: Common, text: string): void {
    When.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: isMessage,
        actions: function (element: UI5Element | null) {
            new Press().executeOn((element as Dialog).getButtons().filter((b) => b.getText() === text)[0]);
        },
        errorMessage: `No message box with a button "${text}"`
    });
}

/** Waits `ms` without a timer OPA5 would wait for (it polls `check`). */
function iWait(When: Common, ms: number): void {
    let since = 0;
    When.waitFor({
        check: function () {
            since = since || Date.now();
            return Date.now() - since >= ms;
        },
        timeout: Math.ceil(ms / 1000) + 10
    });
}

/** Waits for a toast whose text holds `text` (a poll interval of 2 s is not waited for by autoWait). */
function iSeeAToast(Then: Common, text: string): void {
    Then.waitFor({
        check: () => toastText().indexOf(text) !== -1,
        timeout: 25,
        success: () => Opa5.assert.ok(true, `the toast says: ${text}`),
        errorMessage: `No toast saying: ${text}`
    });
}

opaTest("the panel shows the status, the switch and the deployed models", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    iSeeThePanel(Then, "the fixture's status", (panel) => {
        const shown = panelOf(panel);
        Opa5.assert.strictEqual(shown.model, "text-embedding-3-small (found among the AI Core deployments)");
        Opa5.assert.strictEqual(shown.vectors, "20 of 24 nodes");
        Opa5.assert.strictEqual(shown.stale, "4 to embed");
        Opa5.assert.ok(shown.lastRun.indexOf(": 20 nodes embedded with text-embedding-3-small") !== -1, shown.lastRun);
        Opa5.assert.strictEqual(shown.enabled, true, "Use embeddings is on");
        Opa5.assert.strictEqual(shown.modelKey, "__automatic__", "Model is Automatic");
        Opa5.assert.deepEqual(shown.options, ["Automatic", "text-embedding-3-small (RUNNING)"]);
        Opa5.assert.strictEqual(shown.envStrip, "", "no strip: the landscape sets no model");
        Opa5.assert.strictEqual(shown.canRun, true, "Compute embeddings now can run");
        Opa5.assert.strictEqual(backend.countRequests(STATUS), 1, "the status was read once");
    });

    Then.iStopTheApp();
});

opaTest("Save reads the status, then sends the switch and the model", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeThePanel(Then, "loaded", () => undefined);

    When.waitFor({
        id: ID.select,
        viewName: VIEW,
        success: (select: UI5Element) => choose(select as Select, "text-embedding-3-small")
    });
    When.waitFor({ id: ID.enabled, viewName: VIEW, actions: new Press() });
    When.waitFor({ id: ID.save, viewName: VIEW, actions: new Press() });

    iSeeAToast(Then, "Embeddings setting saved.");
    iSeeThePanel(Then, "the saved setting", (panel) => {
        const shown = panelOf(panel);
        const requests = backend.requests;
        Opa5.assert.deepEqual(backend.bodies[SAVE], { enabled: false, model: "text-embedding-3-small" });
        Opa5.assert.ok(requests.lastIndexOf(STATUS) !== -1 && requests.lastIndexOf(STATUS) < requests.indexOf(SAVE),
            "the status is read right before the save");
        Opa5.assert.strictEqual(shown.model, "None: embeddings are switched off. Agents rank data sources by their words.");
        Opa5.assert.strictEqual(shown.enabled, false);
        Opa5.assert.strictEqual(shown.modelKey, "text-embedding-3-small");
        Opa5.assert.strictEqual(shown.canRun, false, "nothing to compute while switched off");
    }, (panel) => panelOf(panel).enabled === false && panelOf(panel).model.indexOf("None") === 0);

    Then.iStopTheApp();
});

opaTest("a setting changed elsewhere is shown, not overwritten", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeThePanel(Then, "loaded", () => {
        // Another admin switches embeddings off meanwhile.
        backend.embeddings.setting = { enabled: false, model: null };
    });

    When.waitFor({
        id: ID.select,
        viewName: VIEW,
        success: (select: UI5Element) => choose(select as Select, "text-embedding-3-small")
    });
    When.waitFor({ id: ID.save, viewName: VIEW, actions: new Press() });

    iSeeAMessage(Then, "changed elsewhere", (dialog) => {
        Opa5.assert.ok(messageOf(dialog).indexOf("was changed elsewhere") !== -1, messageOf(dialog));
        Opa5.assert.strictEqual(backend.countRequests(SAVE), 0, "nothing was saved");
    });
    iAnswer(When, "OK");
    iSeeThePanel(Then, "the stored setting", (panel) => {
        const shown = panelOf(panel);
        Opa5.assert.strictEqual(shown.enabled, false, "the switch shows what is stored");
        Opa5.assert.strictEqual(shown.modelKey, "__automatic__", "the model shows what is stored");
    });

    Then.iStopTheApp();
});

opaTest("a model the server refuses is marked at the Model select", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeThePanel(Then, "loaded", () => undefined);

    When.waitFor({
        id: ID.select,
        viewName: VIEW,
        success: (select: UI5Element) => {
            choose(select as Select, "text-embedding-3-small");
            // The deployment is gone by the time the save arrives.
            backend.embeddings.deployments = [];
        }
    });
    When.waitFor({ id: ID.save, viewName: VIEW, actions: new Press() });

    Then.waitFor({
        id: ID.select,
        viewName: VIEW,
        check: (select: UI5Element) => (select as Select).getValueState() === "Error",
        success: (select: UI5Element) => {
            Opa5.assert.strictEqual((select as Select).getValueStateText(), MODEL_NOT_DEPLOYED,
                "the server's refusal is shown at the field");
            Opa5.assert.strictEqual(backend.countRequests(SAVE), 1);
        },
        errorMessage: "The refusal is not shown at the Model select"
    });
    When.waitFor({
        id: ID.select,
        viewName: VIEW,
        success: (select: UI5Element) => choose(select as Select, "__automatic__")
    });
    Then.waitFor({
        id: ID.select,
        viewName: VIEW,
        success: (select: UI5Element) => Opa5.assert.strictEqual((select as Select).getValueState(), "None",
            "another choice clears the mark")
    });

    Then.iStopTheApp();
});

opaTest("Compute embeddings now polls the status while the run is in flight, then says what it did", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeThePanel(Then, "loaded", () => undefined);

    When.waitFor({ id: ID.run, viewName: VIEW, actions: new Press() });

    iSeeThePanel(Then, "the run in flight", (panel) => {
        Opa5.assert.ok(panelOf(panel).lastRun.indexOf("Running") === 0, panelOf(panel).lastRun);
        Opa5.assert.strictEqual(panelOf(panel).canRun, false, "no second run while one is in flight");
        Opa5.assert.strictEqual(backend.countRequests(RUN), 1);
    }, (panel) => panelOf(panel).lastRun.indexOf("Running") === 0);

    iSeeAToast(Then, "Embeddings computed: 4 nodes embedded.");
    let polls = 0;
    iSeeThePanel(Then, "the finished run", (panel) => {
        const shown = panelOf(panel);
        polls = backend.countRequests(STATUS);
        Opa5.assert.strictEqual(shown.vectors, "24 of 24 nodes");
        Opa5.assert.strictEqual(shown.stale, "None");
        Opa5.assert.ok(shown.lastRun.indexOf(": 4 nodes embedded with text-embedding-3-small") !== -1, shown.lastRun);
        Opa5.assert.strictEqual(shown.canRun, true);
        // The load, the read right after the start, and two polls: the third
        // read is the one that finds the run done.
        Opa5.assert.strictEqual(polls, 4, "the status was read until the run was done");
    }, (panel) => panelOf(panel).vectors === "24 of 24 nodes");

    iWait(When, LONGER_THAN_TWO_POLLS_MS);
    Then.waitFor({
        success: () => Opa5.assert.strictEqual(backend.countRequests(STATUS), polls, "no poll after the run")
    });

    Then.iStopTheApp();
});

opaTest("leaving the Settings page stops the polling, coming back resumes it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeThePanel(Then, "loaded", () => undefined);
    let reads = 0;

    When.waitFor({ id: ID.run, viewName: VIEW, actions: new Press() });
    iSeeThePanel(Then, "the run in flight", () => {
        // A page that matches no route (the router's `bypassed`).
        HashChanger.getInstance().setHash("no-such-page");
        reads = backend.countRequests(STATUS);
    }, (panel) => panelOf(panel).lastRun.indexOf("Running") === 0);
    iWait(When, LONGER_THAN_TWO_POLLS_MS);
    Then.waitFor({
        success: () => {
            Opa5.assert.strictEqual(backend.countRequests(STATUS), reads, "no poll behind the not-found page");
            HashChanger.getInstance().setHash("settings");
        }
    });
    iSeeAToast(Then, "Embeddings computed: 4 nodes embedded.");

    // A second run, and leaving by a route this time.
    When.waitFor({ id: ID.run, viewName: VIEW, actions: new Press() });
    iSeeThePanel(Then, "the second run in flight", () => {
        HashChanger.getInstance().setHash("agents");
        reads = backend.countRequests(STATUS);
    }, (panel) => panelOf(panel).lastRun.indexOf("Running") === 0);
    iWait(When, LONGER_THAN_TWO_POLLS_MS);
    Then.waitFor({
        success: () => Opa5.assert.strictEqual(backend.countRequests(STATUS), reads, "no poll behind the agents page")
    });

    Then.iStopTheApp();
});

opaTest("Deploy asks with the model, the resource group and the billing, then the list shows it PENDING", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeThePanel(Then, "loaded", () => undefined);

    When.waitFor({ id: ID.deploy, viewName: VIEW, actions: new Press() });
    Then.waitFor({
        id: ID.offers,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: (list: UI5Element) => {
            Opa5.assert.deepEqual(offersOf(list), ["text-embedding-3-large", "text-embedding-ada-002"],
                "the models AI Core offers that are not deployed");
        }
    });
    Then.waitFor({
        id: ID.dialogDeploy,
        viewName: VIEW,
        searchOpenDialogs: true,
        autoWait: false,
        success: (button: UI5Element) => Opa5.assert.strictEqual((button as Button).getEnabled(), false,
            "nothing to deploy before a model is picked")
    });
    When.waitFor({
        controlType: "sap.m.StandardListItem",
        searchOpenDialogs: true,
        matchers: (item: UI5Element) => (item as unknown as { getTitle(): string }).getTitle() === "text-embedding-3-large",
        actions: new Press()
    });
    When.waitFor({ id: ID.dialogDeploy, viewName: VIEW, searchOpenDialogs: true, actions: new Press() });

    const question = "Deploy text-embedding-3-large in the AI Core resource group rg-agents? AI Core bills for the "
        + "deployment for as long as it runs. It is not stopped from this page: stopping it is an operations step in AI Core.";
    iSeeAMessage(Then, "the deploy question", (dialog) => {
        Opa5.assert.strictEqual(messageOf(dialog), question);
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Deploy", "Cancel"]);
        Opa5.assert.strictEqual(
            (Element.getElementById(dialog.getInitialFocus() as string) as unknown as { getText(): string }).getText(),
            "Cancel", "the focus is on Cancel");
    });
    iAnswer(When, "Cancel");
    Then.waitFor({
        id: ID.dialog,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: () => Opa5.assert.strictEqual(backend.countRequests(DEPLOY), 0, "Cancel deploys nothing")
    });

    When.waitFor({ id: ID.dialogDeploy, viewName: VIEW, searchOpenDialogs: true, actions: new Press() });
    iAnswer(When, "Deploy");

    iSeeAToast(Then, "Deployment of text-embedding-3-large requested (PENDING).");
    iSeeThePanel(Then, "the PENDING deployment", (panel) => {
        Opa5.assert.deepEqual(backend.bodies[DEPLOY], { model: "text-embedding-3-large" });
        Opa5.assert.deepEqual(panelOf(panel).options,
            ["Automatic", "text-embedding-3-small (RUNNING)", "text-embedding-3-large (PENDING)"]);
    }, (panel) => panelOf(panel).options.length === 3);
    Then.waitFor({
        id: ID.dialog,
        viewName: VIEW,
        visible: false,
        autoWait: false,
        success: (dialog: UI5Element) => Opa5.assert.strictEqual((dialog as Dialog).isOpen(), false, "the dialog is closed")
    });

    Then.iStopTheApp();
});

opaTest("the landscape's AICORE_EMBEDDING_MODEL is shown as a strip", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");
    Then.waitFor({
        id: "agentsPage",
        viewName: "Agents",
        success: () => {
            backend.embeddings.envModel = "text-embedding-3-large";
            HashChanger.getInstance().setHash("settings");
        }
    });

    iSeeThePanel(Then, "the variable in effect", (panel) => {
        const shown = panelOf(panel);
        Opa5.assert.strictEqual(shown.envStrip, "The model text-embedding-3-large is set by AICORE_EMBEDDING_MODEL "
            + "on this landscape. A model chosen here takes its place.");
        Opa5.assert.strictEqual(shown.model, "text-embedding-3-large (set by AICORE_EMBEDDING_MODEL on this landscape)");
        Opa5.assert.strictEqual(shown.vectors, "0 of 24 nodes");
    }, (panel) => panelOf(panel).envStrip !== "");

    Then.iStopTheApp();
});

opaTest("a status that cannot be read is said in the panel, not in a box", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings", { path: "data-catalog/embeddings", status: 502, body: { detail: "AI Core is not reachable" } });

    Then.waitFor({
        id: ID.loadError,
        viewName: VIEW,
        check: (strip: UI5Element) => (strip as MessageStrip).getVisible(),
        success: (strip: UI5Element) => {
            Opa5.assert.strictEqual((strip as MessageStrip).getText(),
                "The embeddings status could not be read: AI Core is not reachable");
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMMessageBox").length, 0, "no message box");
        },
        errorMessage: "The failure is not shown in the panel"
    });

    Then.iStopTheApp();
});
