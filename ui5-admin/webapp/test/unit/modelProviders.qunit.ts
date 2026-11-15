import { ValueState } from "sap/ui/core/library";
import {
    PROVIDERS, effectiveText, fromForm, infoTexts, isDirty, keyPlaceholder, keySource, parseModels, providerOf,
    refusedField, removeKeyBody, testOutcome, titleKey, toForm, typedKey
} from "com/agent/admin/model/modelProviders";
import type { ProviderForm } from "com/agent/admin/model/modelProviders";
import AdminService, { AdminError } from "com/agent/admin/service/AdminService";
import type { ModelProviderStatus, ModelProvidersStatus } from "com/agent/admin/service/modelProviderTypes";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";
import { BAD_BASE_URL, BAD_KEY } from "com/agent/admin/test/integration/FakeModelProviders";

// The logic of the "Model providers" panel of the Settings page, the three
// calls of AdminService, and the fake's routes (which never answer a key).

function provider(extra: Partial<ModelProviderStatus> = {}): ModelProviderStatus {
    return {
        provider: "anthropic", stored: true, enabled: true, has_key: true, key_source: "setting", has_stored_key: true,
        base_url: "", models: ["claude-sonnet-4-5"], effective_models: ["anthropic:claude-sonnet-4-5"], ...extra
    };
}

function form(extra: Partial<ProviderForm> = {}): ProviderForm {
    return { enabled: true, apiKey: "", baseUrl: "", models: "claude-sonnet-4-5", ...extra };
}

QUnit.module("modelProviders: form and save body");

QUnit.test("the providers, Anthropic first, and their titles", function (assert) {
    assert.deepEqual(PROVIDERS, ["anthropic", "openai"]);
    assert.strictEqual(titleKey("anthropic"), "mpTitleAnthropic");
    assert.strictEqual(titleKey("openai"), "mpTitleOpenai");
    const status: ModelProvidersStatus = {
        providers: [provider(), provider({ provider: "openai" })], aicore_configured: true, available_models: [],
        default_model: ""
    };
    assert.strictEqual(providerOf(status, "openai")?.provider, "openai");
    assert.strictEqual(providerOf(null, "openai"), null);
});

QUnit.test("toForm: the stored setting, the key field always empty", function (assert) {
    assert.deepEqual(toForm(provider({ base_url: "https://proxy.example.com", models: ["a", "b"] })),
        { enabled: true, apiKey: "", baseUrl: "https://proxy.example.com", models: "a, b" });
    assert.deepEqual(toForm(null), { enabled: false, apiKey: "", baseUrl: "", models: "" });
});

QUnit.test("parseModels: split on commas, trimmed, empties dropped", function (assert) {
    assert.deepEqual(parseModels(" a , ,b,, c "), ["a", "b", "c"]);
    assert.deepEqual(parseModels(""), []);
    assert.deepEqual(parseModels(null), []);
});

QUnit.test("fromForm: enabled, base_url and models always; api_key only when typed", function (assert) {
    const kept = fromForm(form({ baseUrl: "  ", models: "x, y" }));
    assert.deepEqual(kept, { enabled: true, base_url: null, models: ["x", "y"] });
    assert.notOk("api_key" in kept, "no key typed: no api_key property at all");
    assert.notOk("api_key" in fromForm(form({ apiKey: "   " })), "blanks only are nothing typed");
    const typed = fromForm(form({ apiKey: " sk-new ", baseUrl: " https://h.example.com/v1 ", enabled: false }));
    assert.deepEqual(typed, { enabled: false, base_url: "https://h.example.com/v1", models: ["claude-sonnet-4-5"],
        api_key: "sk-new" });
    assert.strictEqual(typedKey(form({ apiKey: " k " })), "k");
});

QUnit.test("removeKeyBody: the stored setting with api_key null", function (assert) {
    assert.deepEqual(removeKeyBody(form({ apiKey: "ignored", baseUrl: "https://p.example.com" })),
        { enabled: true, base_url: "https://p.example.com", models: ["claude-sonnet-4-5"], api_key: null });
});

QUnit.test("isDirty: a typed key, the switch, the base URL or the model list", function (assert) {
    const stored = form();
    assert.notOk(isDirty(form(), stored));
    assert.notOk(isDirty(form({ models: " claude-sonnet-4-5 , " }), stored), "the same list written differently");
    assert.notOk(isDirty(form({ baseUrl: "  " }), stored));
    assert.ok(isDirty(form({ apiKey: "k" }), stored));
    assert.ok(isDirty(form({ enabled: false }), stored));
    assert.ok(isDirty(form({ baseUrl: "https://x.example.com" }), stored));
    assert.ok(isDirty(form({ models: "claude-sonnet-4-5, other" }), stored));
});

QUnit.module("modelProviders: texts");

QUnit.test("refusedField: the control a 422 names", function (assert) {
    assert.strictEqual(refusedField(BAD_KEY), "apiKey");
    assert.strictEqual(refusedField(BAD_BASE_URL), "baseUrl");
    assert.strictEqual(refusedField("models: a list of at most 30 model names"), "models");
    assert.strictEqual(refusedField("models[3]: not a model name"), "models");
    assert.strictEqual(refusedField("enabled: must be a boolean"), "");
    assert.strictEqual(refusedField("body: an unknown key"), "");
    assert.strictEqual(refusedField(""), "");
    assert.strictEqual(refusedField(null), "");
});

QUnit.test("the key's placeholder and where the key in effect comes from", function (assert) {
    assert.deepEqual(keyPlaceholder(provider()), { key: "mpKeyPlaceholderStored" });
    assert.deepEqual(keyPlaceholder(provider({ has_stored_key: false })), { key: "mpKeyPlaceholderNone" });
    assert.deepEqual(keySource(provider()), { text: { key: "mpKeySourceSetting" }, state: ValueState.Success });
    assert.deepEqual(keySource(provider({ key_source: "env" })), { text: { key: "mpKeySourceEnv" }, state: ValueState.Success });
    assert.deepEqual(keySource(provider({ key_source: "none" })), { text: { key: "mpKeySourceNone" }, state: ValueState.Warning },
        "no key while on is a warning");
    assert.deepEqual(keySource(provider({ key_source: "none", enabled: false })).state, ValueState.None);
});

QUnit.test("the models on offer and the information strip", function (assert) {
    assert.deepEqual(effectiveText(provider()), { key: "mpEffectiveModels", args: ["anthropic:claude-sonnet-4-5"] });
    assert.deepEqual(effectiveText(provider({ effective_models: [] })), { key: "mpEffectiveNone" });
    const status = { providers: [], available_models: [], default_model: "" };
    assert.deepEqual(infoTexts({ ...status, aicore_configured: true }), [{ key: "mpInfo" }]);
    assert.deepEqual(infoTexts({ ...status, aicore_configured: false }), [{ key: "mpInfo" }, { key: "mpInfoNoAicore" }]);
});

QUnit.test("testOutcome: a fixed text per case", function (assert) {
    assert.deepEqual(testOutcome({ ok: true }), { type: "Success", text: { key: "mpTestOk" } });
    assert.deepEqual(testOutcome({ ok: false, error: "no_key" }), { type: "Error", text: { key: "mpTestNoKey" } });
    assert.deepEqual(testOutcome({ ok: false, status: 401, error: "AuthenticationError" }),
        { type: "Error", text: { key: "mpTestRefused" } });
    assert.deepEqual(testOutcome({ ok: false, status: 403, error: "PermissionDeniedError" }),
        { type: "Error", text: { key: "mpTestRefused" } });
    assert.deepEqual(testOutcome({ ok: false, status: null, error: "APIConnectionError" }),
        { type: "Error", text: { key: "mpTestUnreachable", args: ["APIConnectionError"] } });
    assert.deepEqual(testOutcome({ ok: false, error: "a text with sk-secret in it" }),
        { type: "Error", text: { key: "mpTestUnreachable", args: ["?"] } }, "only a class name is shown");
});

QUnit.module("AdminService and FakeBackend: model providers", {
    beforeEach: function (this: { backend: FakeBackend }) {
        this.backend = new FakeBackend();
        this.backend.reset();
        this.backend.install();
    },
    afterEach: function (this: { backend: FakeBackend }) {
        this.backend.restore();
    }
});

async function refusal(work: () => Promise<unknown>): Promise<AdminError> {
    try {
        await work();
    } catch (error) {
        return error as AdminError;
    }
    throw new Error("not refused");
}

QUnit.test("the status never holds a key; a save without api_key keeps the stored one", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const status = await service.getModelProviders();
    assert.deepEqual(status.providers.map((p) => p.provider), ["anthropic", "openai"]);
    assert.strictEqual(JSON.stringify(status).indexOf("sk-ant-stored"), -1, "no key in the answer");
    assert.strictEqual(providerOf(status, "anthropic")?.key_source, "setting");

    const saved = await service.saveModelProvider("anthropic", { enabled: true, base_url: null, models: ["claude-opus-4-1"] });
    assert.notOk("api_key" in (this.backend.bodies["PUT model-providers/anthropic"] ?? {}), "no api_key sent");
    assert.strictEqual(providerOf(saved, "anthropic")?.has_stored_key, true, "the stored key stays");
    assert.deepEqual(providerOf(saved, "anthropic")?.effective_models, ["anthropic:claude-opus-4-1"]);

    const removed = await service.saveModelProvider("anthropic", { enabled: true, base_url: null, models: [], api_key: null });
    assert.strictEqual(this.backend.bodies["PUT model-providers/anthropic"]?.api_key, null, "null is sent");
    assert.strictEqual(providerOf(removed, "anthropic")?.key_source, "none");
    assert.deepEqual(await service.testModelProvider("anthropic"), { ok: false, error: "no_key" });
});

QUnit.test("the refusals name the field", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const url = await refusal(() => service.saveModelProvider("openai", { enabled: true, base_url: "http://x.example.com", models: [] }));
    assert.strictEqual(url.status, 422);
    assert.strictEqual(refusedField(url.detail), "baseUrl");
    const key = await refusal(() => service.saveModelProvider("openai", { enabled: true, base_url: null, models: [], api_key: "a b" }));
    assert.strictEqual(refusedField(key.detail), "apiKey");
    const model = await refusal(() => service.saveModelProvider("openai", { enabled: true, base_url: null, models: ["ok", "no way"] }));
    assert.strictEqual(model.detail, "models[1]: not a model name");
    assert.strictEqual(refusedField(model.detail), "models");
});

QUnit.test("the test: accepted, refused", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    assert.deepEqual(await service.testModelProvider("anthropic"), { ok: true });
    await service.saveModelProvider("openai", { enabled: true, base_url: null, models: [], api_key: "sk-bad-key" });
    const refused = await service.testModelProvider("openai");
    assert.strictEqual(refused.status, 401);
    assert.deepEqual(testOutcome(refused).text, { key: "mpTestRefused" });
});
