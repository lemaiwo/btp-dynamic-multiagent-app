import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import Element from "sap/ui/core/Element";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Dialog from "sap/m/Dialog";
import type Input from "sap/m/Input";
import type MessageStrip from "sap/m/MessageStrip";
import type UI5Element from "sap/ui/core/Element";
import type { ModelProviderName } from "com/agent/admin/service/modelProviderTypes";
import Common, { backend } from "./pages/Common";
import { buttonsOf, messageOf } from "./pages/ODataList";
import { toastText } from "./pages/Embeddings";
import { viewOf } from "./pages/InaQueryDetail";
import { INFO, PANEL, VIEW, blockOf, idOf, type BlockTexts } from "./pages/ModelProviders";
import { BAD_BASE_URL } from "./FakeModelProviders";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Model providers journey");

/** The control a label is rendered for (its `labelFor`, or the field a form ties it to). */
interface LabelFor { getLabelForRendering(): string }

const STATUS = "GET model-providers";
const SAVE_ANTHROPIC = "PUT model-providers/anthropic";
const SAVE_OPENAI = "PUT model-providers/openai";

/** Waits for the panel to show the providers, then hands a block over. */
function iSeeTheBlock(Then: Common, provider: ModelProviderName, what: string, assert: (block: BlockTexts) => void,
    ready: (block: BlockTexts) => boolean = (block) => block.title !== ""): void {
    Then.waitFor({
        id: PANEL,
        viewName: VIEW,
        check: (panel: UI5Element) => ready(blockOf(panel, provider)),
        success: (panel: UI5Element) => assert(blockOf(panel, provider)),
        errorMessage: `The ${provider} block does not show: ${what}`
    });
}

function isMessage(dialog: UI5Element): boolean {
    return (dialog as Dialog).getType() === "Message";
}

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

function iSeeAToast(Then: Common, text: string): void {
    Then.waitFor({
        check: () => toastText().indexOf(text) !== -1,
        timeout: 25,
        success: () => Opa5.assert.ok(true, `the toast says: ${text}`),
        errorMessage: `No toast saying: ${text}`
    });
}

function iType(When: Common, provider: ModelProviderName, control: string, text: string): void {
    When.waitFor({ id: idOf(provider, control), viewName: VIEW, actions: new EnterText({ text, clearTextFirst: true }) });
}

function iPress(When: Common, provider: ModelProviderName, control: string): void {
    When.waitFor({ id: idOf(provider, control), viewName: VIEW, actions: new Press() });
}

opaTest("the panel shows both providers, the key never, and where the key comes from", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");

    iSeeTheBlock(Then, "anthropic", "the stored setting", (block) => {
        Opa5.assert.strictEqual(block.title, "Anthropic");
        Opa5.assert.strictEqual(block.enabled, true);
        Opa5.assert.strictEqual(block.key, "", "the key field is empty");
        Opa5.assert.strictEqual(block.keyType, "Password");
        Opa5.assert.strictEqual(block.keyPlaceholder, "A key is stored; type a new one to replace it");
        Opa5.assert.strictEqual(block.keySource, "Set here");
        Opa5.assert.strictEqual(block.canRemoveKey, true);
        Opa5.assert.strictEqual(block.models, "claude-sonnet-4-5");
        Opa5.assert.strictEqual(block.effective, "anthropic:claude-sonnet-4-5");
        Opa5.assert.strictEqual(block.canTest, true);
        Opa5.assert.strictEqual(block.saveFirst, false);
        Opa5.assert.strictEqual(backend.countRequests(STATUS), 1);
    });
    iSeeTheBlock(Then, "openai", "nothing stored", (block) => {
        Opa5.assert.strictEqual(block.title, "OpenAI");
        Opa5.assert.strictEqual(block.enabled, false);
        Opa5.assert.strictEqual(block.keyPlaceholder, "No key is stored; type one to store it");
        Opa5.assert.strictEqual(block.keySource, "No key");
        Opa5.assert.strictEqual(block.canRemoveKey, false, "no stored key to remove");
        Opa5.assert.strictEqual(block.effective, "None");
    });
    Then.waitFor({
        id: INFO,
        viewName: VIEW,
        success: (strip: UI5Element) => {
            const text = (strip as MessageStrip).getText();
            Opa5.assert.ok(text.indexOf("anthropic:<model> and openai:<model>") !== -1, text);
            Opa5.assert.ok(text.indexOf("takes precedence over the environment variables") !== -1, text);
            Opa5.assert.strictEqual(text.indexOf("SAP AI Core is not configured"), -1, "AI Core is configured");
        }
    });
    Then.waitFor({
        id: idOf("anthropic", "apiKey"),
        viewName: VIEW,
        success: (input: UI5Element) => {
            const dom = (input as Input).getFocusDomRef() as HTMLInputElement;
            Opa5.assert.strictEqual(dom.getAttribute("autocomplete"), "off", "the browser does not fill the key");
            Opa5.assert.strictEqual(dom.type, "password");
            const label = viewOf(input).byId(idOf("anthropic", "apiKeyLabel")) as unknown as LabelFor;
            Opa5.assert.strictEqual(label.getLabelForRendering(), input.getId(), "the key field has its label");
            ["enabled", "baseUrl", "models"].forEach((control) => {
                const field = viewOf(input).byId(idOf("anthropic", control))!;
                const fieldLabel = viewOf(input).byId(idOf("anthropic", `${control}Label`)) as unknown as LabelFor;
                Opa5.assert.strictEqual(fieldLabel.getLabelForRendering(), field.getId(), `${control} has its label`);
            });
        }
    });

    Then.iStopTheApp();
});

opaTest("AI Core that is not configured is said in the strip", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("agents");
    Then.waitFor({
        id: "agentsPage",
        viewName: "Agents",
        success: () => {
            backend.modelProviders.aicoreConfigured = false;
            HashChanger.getInstance().setHash("settings");
        }
    });
    Then.waitFor({
        id: INFO,
        viewName: VIEW,
        check: (strip: UI5Element) => (strip as MessageStrip).getText().indexOf("SAP AI Core is not configured") !== -1,
        success: () => Opa5.assert.ok(true, "the strip says AI Core is not configured"),
        errorMessage: "The strip does not say AI Core is not configured"
    });

    Then.iStopTheApp();
});

opaTest("a typed key is sent, then the field is empty with the stored placeholder", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "openai", "loaded", () => undefined);

    When.waitFor({ id: idOf("openai", "enabled"), viewName: VIEW, actions: new Press() });
    iType(When, "openai", "apiKey", "sk-openai-new");
    iPress(When, "openai", "save");

    iSeeAToast(Then, "OpenAI saved.");
    iSeeTheBlock(Then, "openai", "the saved key", (block) => {
        Opa5.assert.deepEqual(backend.bodies[SAVE_OPENAI],
            { enabled: true, base_url: null, models: [], api_key: "sk-openai-new" });
        Opa5.assert.strictEqual(block.key, "", "the key field is empty again");
        Opa5.assert.strictEqual(block.keyPlaceholder, "A key is stored; type a new one to replace it");
        Opa5.assert.strictEqual(block.keySource, "Set here");
        Opa5.assert.strictEqual(block.canRemoveKey, true);
        Opa5.assert.strictEqual(block.canTest, true, "nothing unsaved");
        const requests = backend.requests;
        Opa5.assert.ok(requests.lastIndexOf("GET model") > requests.indexOf(SAVE_OPENAI),
            "the LLM model panel reads the models on offer again");
    }, (block) => block.keySource === "Set here");

    Then.iStopTheApp();
});

opaTest("a save without a typed key sends no api_key at all", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "anthropic", "loaded", () => undefined);

    iType(When, "anthropic", "models", "claude-opus-4-1, claude-sonnet-4-5");
    iPress(When, "anthropic", "save");

    iSeeAToast(Then, "Anthropic saved.");
    iSeeTheBlock(Then, "anthropic", "the saved models", (block) => {
        const body = backend.bodies[SAVE_ANTHROPIC] ?? {};
        Opa5.assert.deepEqual(body, { enabled: true, base_url: null, models: ["claude-opus-4-1", "claude-sonnet-4-5"] });
        Opa5.assert.notOk(Object.prototype.hasOwnProperty.call(body, "api_key"), "no api_key property");
        Opa5.assert.strictEqual(block.keySource, "Set here", "the stored key stays");
        Opa5.assert.strictEqual(block.effective,
            "anthropic:claude-opus-4-1, anthropic:claude-sonnet-4-5");
    }, (block) => block.effective.indexOf("claude-opus-4-1") !== -1);

    Then.iStopTheApp();
});

opaTest("Remove stored key asks first, with the focus on Cancel, then sends null", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "anthropic", "loaded", () => undefined);

    iPress(When, "anthropic", "removeKey");
    iSeeAMessage(Then, "the question", (dialog) => {
        Opa5.assert.ok(messageOf(dialog).indexOf("Remove the API key stored for Anthropic?") === 0, messageOf(dialog));
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Remove", "Cancel"]);
        Opa5.assert.strictEqual(
            (Element.getElementById(dialog.getInitialFocus() as string) as unknown as { getText(): string }).getText(),
            "Cancel", "the focus is on Cancel");
    });
    iAnswer(When, "Cancel");
    Then.waitFor({
        id: PANEL,
        viewName: VIEW,
        success: () => Opa5.assert.strictEqual(backend.countRequests(SAVE_ANTHROPIC), 0, "Cancel sends nothing")
    });

    iPress(When, "anthropic", "removeKey");
    iAnswer(When, "Remove");
    iSeeAToast(Then, "The stored key of Anthropic was removed.");
    iSeeTheBlock(Then, "anthropic", "no key", (block) => {
        Opa5.assert.deepEqual(backend.bodies[SAVE_ANTHROPIC],
            { enabled: true, base_url: null, models: ["claude-sonnet-4-5"], api_key: null });
        Opa5.assert.strictEqual(block.keyPlaceholder, "No key is stored; type one to store it");
        Opa5.assert.strictEqual(block.canRemoveKey, false);
    }, (block) => block.keySource === "No key");

    Then.iStopTheApp();
});

opaTest("a refused base URL is marked at its field", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "openai", "loaded", () => undefined);

    iType(When, "openai", "baseUrl", "http://proxy.example.com");
    iPress(When, "openai", "save");

    Then.waitFor({
        id: idOf("openai", "baseUrl"),
        viewName: VIEW,
        check: (input: UI5Element) => (input as Input).getValueState() === "Error",
        success: (input: UI5Element) => {
            Opa5.assert.strictEqual((input as Input).getValueStateText(), BAD_BASE_URL, "the server's refusal at the field");
            Opa5.assert.strictEqual(backend.countRequests(SAVE_OPENAI), 1);
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMMessageBox").length, 0, "no message box");
        },
        errorMessage: "The refusal is not shown at the base URL"
    });
    iType(When, "openai", "baseUrl", "https://proxy.example.com");
    Then.waitFor({
        id: idOf("openai", "baseUrl"),
        viewName: VIEW,
        success: (input: UI5Element) => Opa5.assert.strictEqual((input as Input).getValueState(), "None",
            "an edit clears the mark")
    });

    Then.iStopTheApp();
});

opaTest("Test says whether the key was accepted or refused", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "anthropic", "loaded", () => {
        // OpenAI gets a key from the environment that the provider refuses.
        backend.modelProviders.envKey.openai = "sk-bad-from-env";
    });

    iPress(When, "anthropic", "test");
    iSeeTheBlock(Then, "anthropic", "accepted", (block) => {
        Opa5.assert.strictEqual(block.test, "The key was accepted");
        Opa5.assert.strictEqual(block.testType, "Success");
    }, (block) => block.test !== "");

    iPress(When, "openai", "test");
    iSeeTheBlock(Then, "openai", "refused", (block) => {
        Opa5.assert.strictEqual(block.test, "The provider refused the key");
        Opa5.assert.strictEqual(block.testType, "Error");
        Opa5.assert.strictEqual(backend.countRequests("POST model-providers/openai/test"), 1);
    }, (block) => block.test !== "");

    Then.iStopTheApp();
});

opaTest("Test is off while the block holds unsaved changes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "anthropic", "loaded", () => undefined);

    iType(When, "anthropic", "apiKey", "sk-ant-other");
    iSeeTheBlock(Then, "anthropic", "unsaved", (block) => {
        Opa5.assert.strictEqual(block.canTest, false, "Test is off");
        Opa5.assert.strictEqual(block.testTooltip, "Save first: Test checks what is stored");
        Opa5.assert.strictEqual(block.saveFirst, true, "the hint shows");
    }, (block) => !block.canTest);
    iSeeTheBlock(Then, "openai", "untouched", (block) => {
        Opa5.assert.strictEqual(block.canTest, true, "the other provider is not affected");
    });

    When.waitFor({
        id: idOf("anthropic", "apiKey"),
        viewName: VIEW,
        actions: (input: UI5Element | null) => {
            (input as Input).setValue("");
        }
    });
    iSeeTheBlock(Then, "anthropic", "back to the stored state", (block) => {
        Opa5.assert.strictEqual(block.canTest, true, "Test is on again");
        Opa5.assert.strictEqual(block.saveFirst, false);
        Opa5.assert.strictEqual(backend.countRequests("POST model-providers/anthropic/test"), 0);
    }, (block) => block.canTest);

    Then.iStopTheApp();
});

opaTest("a save whose reload failed says so in a box that stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("settings");
    iSeeTheBlock(Then, "anthropic", "loaded", () => {
        backend.reloadOutcome = "failed";
    });

    When.waitFor({ id: idOf("anthropic", "enabled"), viewName: VIEW, actions: new Press() });
    iPress(When, "anthropic", "save");
    iSeeAMessage(Then, "the reload warning", (dialog) => {
        Opa5.assert.ok(messageOf(dialog).indexOf("The model provider was saved, but the running agents could not be reloaded") === 0,
            messageOf(dialog));
        Opa5.assert.strictEqual(toastText().indexOf("Anthropic saved."), -1, "no saved toast");
    });
    iAnswer(When, "OK");

    Then.iStopTheApp();
});
