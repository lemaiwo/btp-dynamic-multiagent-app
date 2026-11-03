import ResourceBundle from "sap/base/i18n/ResourceBundle";
import { ValueState } from "sap/ui/core/library";
import embeddingsSettings, {
    AUTOMATIC_KEY, POLL_INTERVAL_MS, canRun, deployConfirmText, deployOptions, envStripText, formOf, modelOptions,
    refusedField, runOutcome, settingBody, statusLines
} from "com/agent/admin/model/embeddingsSettings";
import type { EmbeddingsText } from "com/agent/admin/model/embeddingsSettings";
import formatter from "com/agent/admin/model/formatter";
import AdminService, { AdminError } from "com/agent/admin/service/AdminService";
import type { EmbeddingRun, EmbeddingsStatus } from "com/agent/admin/service/dataCatalogTypes";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";
import { MODEL_NOT_DEPLOYED, MODEL_NOT_OFFERED, RUN_IN_FLIGHT } from "com/agent/admin/test/integration/FakeEmbeddings";

// The logic of the "Data catalogue embeddings" panel of the Settings page
// (design addendum (l), point 4), the four calls of AdminService, and the
// fake's scripted run and deployment.

const DONE_RUN: EmbeddingRun = {
    started_at: "2026-11-03T08:00:00+00:00", finished_at: "2026-11-03T08:00:05+00:00",
    model: "text-embedding-3-small", embedded: 20, error_class: null
};

function status(extra: Partial<EmbeddingsStatus> = {}): EmbeddingsStatus {
    return {
        setting: { enabled: true, model: null }, effective_model: "text-embedding-3-small", source: "discovery",
        env_override: false,
        deployed: [{ model: "text-embedding-3-small", status: "RUNNING", deployment_id: "d1" }],
        available: [{ model: "text-embedding-3-large", versions: ["1"] }],
        nodes: 24, vectors: 20, stale: 4, running: false, progress: null, last_run: DONE_RUN,
        resource_group: "rg-agents", ...extra
    };
}

/** The value text of the line labelled `label`. */
function lineOf(value: EmbeddingsStatus, label: string): { text: EmbeddingsText; state: ValueState } {
    const line = statusLines(value).filter((l) => l.label === label)[0];
    if (!line) {
        throw new Error(`no line ${label}`);
    }
    return { text: line.text, state: line.state };
}

QUnit.module("embeddingsSettings: the status lines");

QUnit.test("four lines: the model and where it comes from, vectors, stale, last run", function (assert) {
    const lines = statusLines(status());
    assert.deepEqual(lines.map((l) => l.label), ["embLineModel", "embLineVectors", "embLineStale", "embLineLastRun"]);
    assert.deepEqual(lines[0].text, { key: "embModelFromDiscovery", args: ["text-embedding-3-small"] });
    assert.deepEqual(lines[1].text, { key: "embVectorsOf", args: ["20", "24"] }, "Vectors n of m nodes");
    assert.deepEqual(lines[2].text, { key: "embStaleSome", args: ["4"] });
    assert.strictEqual(lines[2].state, ValueState.Warning, "nodes left to embed are a warning");
    assert.deepEqual(lines[3].text, {
        key: "embRunDone", args: [formatter.timestamp(DONE_RUN.finished_at), "20", "text-embedding-3-small"]
    });
    assert.strictEqual(lines[3].state, ValueState.None);
});

QUnit.test("the model line says where the model comes from", function (assert) {
    assert.deepEqual(lineOf(status({ source: "setting", setting: { enabled: true, model: "m" }, effective_model: "m" }),
        "embLineModel").text, { key: "embModelFromSetting", args: ["m"] });
    assert.deepEqual(lineOf(status({ source: "env", env_override: true, effective_model: "e" }), "embLineModel").text,
        { key: "embModelFromEnv", args: ["e"] });
    const off = lineOf(status({ setting: { enabled: false, model: null }, effective_model: null, source: "none" }),
        "embLineModel");
    assert.deepEqual(off.text, { key: "embModelOff" }, "switched off here");
    assert.strictEqual(off.state, ValueState.None, "a choice, not a problem");
    assert.deepEqual(lineOf(status({ effective_model: null, source: "none" }), "embLineModel").text,
        { key: "embModelNone" }, "on, but nothing deployed (or the landscape switch)");
});

QUnit.test("a chosen model without a running deployment: the status says so", function (assert) {
    const line = lineOf(status({
        setting: { enabled: true, model: "text-embedding-3-large" }, effective_model: null, source: "setting"
    }), "embLineModel");
    assert.deepEqual(line.text, { key: "embModelNotRunning", args: ["text-embedding-3-large"] });
    assert.strictEqual(line.state, ValueState.Warning);
});

QUnit.test("stale: none is no warning; without a model nothing is a warning", function (assert) {
    const none = lineOf(status({ stale: 0, vectors: 24 }), "embLineStale");
    assert.deepEqual(none.text, { key: "embStaleNone" });
    assert.strictEqual(none.state, ValueState.None);
    assert.strictEqual(lineOf(status({ effective_model: null, source: "none", stale: 24 }), "embLineStale").state,
        ValueState.None, "nothing can be embedded without a model");
});

QUnit.test("the last run: running, never, failed, unfinished", function (assert) {
    const running = lineOf(status({ running: true, progress: { done: 8, total: 24 } }), "embLineLastRun");
    assert.deepEqual(running.text, { key: "embRunRunningProgress", args: ["8", "24"] });
    assert.strictEqual(running.state, ValueState.Information);
    assert.deepEqual(lineOf(status({ running: true, progress: null }), "embLineLastRun").text, { key: "embRunRunning" },
        "the run has not counted its nodes yet");
    assert.deepEqual(lineOf(status({ last_run: null }), "embLineLastRun").text, { key: "embRunNever" });
    const failed = lineOf(status({ last_run: { ...DONE_RUN, embedded: 3, error_class: "RateLimitError" } }),
        "embLineLastRun");
    assert.deepEqual(failed.text, { key: "embRunFailed", args: [formatter.timestamp(DONE_RUN.finished_at), "RateLimitError"] });
    assert.strictEqual(failed.state, ValueState.Error);
    const unfinished = lineOf(status({ last_run: { ...DONE_RUN, finished_at: null } }), "embLineLastRun");
    assert.deepEqual(unfinished.text, { key: "embRunUnfinished", args: [formatter.timestamp(DONE_RUN.started_at)] });
    assert.strictEqual(unfinished.state, ValueState.Warning);
    assert.deepEqual(lineOf(status({ last_run: { ...DONE_RUN, model: null } }), "embLineLastRun").text,
        { key: "embRunDoneNoModel", args: [formatter.timestamp(DONE_RUN.finished_at), "20"] });
});

QUnit.test("an answer with fields missing gives lines, never an exception", function (assert) {
    assert.deepEqual(statusLines(null), [], "no answer is no lines");
    const lines = statusLines({} as EmbeddingsStatus);
    assert.strictEqual(lines.length, 4);
    assert.deepEqual(lines[0].text, { key: "embModelOff" }, "no setting reads as switched off");
    assert.deepEqual(lines[1].text, { key: "embVectorsOf", args: ["0", "0"] });
    assert.deepEqual(lines[3].text, { key: "embRunNever" });
    assert.deepEqual(lineOf(status({ running: "yes" as unknown as boolean }), "embLineLastRun").text.key, "embRunDone",
        "only the boolean true is a run in flight");
});

QUnit.module("embeddingsSettings: the model select and the form");

QUnit.test("Automatic first, then each deployed model once with its status", function (assert) {
    const options = modelOptions(status({
        deployed: [
            { model: "text-embedding-3-large", status: "PENDING", deployment_id: "d2" },
            { model: "text-embedding-3-small", status: "RUNNING", deployment_id: "d1" },
            { model: "text-embedding-3-large", status: "RUNNING", deployment_id: "d3" }
        ]
    }));
    assert.deepEqual(options, [
        { key: AUTOMATIC_KEY, text: { key: "embModelAutomatic" } },
        { key: "text-embedding-3-large", text: { key: "embModelOption", args: ["text-embedding-3-large", "RUNNING"] } },
        { key: "text-embedding-3-small", text: { key: "embModelOption", args: ["text-embedding-3-small", "RUNNING"] } }
    ], "a model with a running deployment shows RUNNING, whatever its other deployments say");
    assert.notStrictEqual(AUTOMATIC_KEY, "", "sap.m.Select reads an empty key as no selection");
});

QUnit.test("the saved model stays selectable when its deployment is gone", function (assert) {
    const options = modelOptions(status({ setting: { enabled: true, model: "gone-embedding" }, deployed: [] }));
    assert.deepEqual(options, [
        { key: AUTOMATIC_KEY, text: { key: "embModelAutomatic" } },
        { key: "gone-embedding", text: { key: "embModelOptionNotDeployed", args: ["gone-embedding"] } }
    ], "otherwise the Select would show Automatic and a Save would change the setting");
    assert.deepEqual(modelOptions(null), [{ key: AUTOMATIC_KEY, text: { key: "embModelAutomatic" } }]);
});

QUnit.test("the form and the body of the save", function (assert) {
    assert.deepEqual(formOf(status()), { enabled: true, modelKey: AUTOMATIC_KEY });
    assert.deepEqual(formOf(status({ setting: { enabled: false, model: "m" } })), { enabled: false, modelKey: "m" });
    assert.deepEqual(formOf({} as EmbeddingsStatus), { enabled: false, modelKey: AUTOMATIC_KEY });
    assert.deepEqual(settingBody({ enabled: true, modelKey: AUTOMATIC_KEY }), { enabled: true, model: null },
        "Automatic is null");
    assert.deepEqual(settingBody({ enabled: false, modelKey: "text-embedding-3-small" }),
        { enabled: false, model: "text-embedding-3-small" });
    assert.deepEqual(settingBody({ enabled: "true" as unknown as boolean, modelKey: "" }), { enabled: false, model: null },
        "strict: only the boolean true is on; an empty key is automatic");
});

QUnit.test("a refusal names its field", function (assert) {
    assert.strictEqual(refusedField("model: not a deployed embedding model"), "model");
    assert.strictEqual(refusedField("enabled: Input should be a valid boolean"), "enabled");
    assert.strictEqual(refusedField("body: expected a JSON object"), "");
    assert.strictEqual(refusedField(""), "");
});

QUnit.module("embeddingsSettings: run, deploy, the env strip");

QUnit.test("a run needs embeddings on, a model in effect and no run in flight", function (assert) {
    assert.ok(canRun(status()));
    assert.notOk(canRun(status({ running: true })));
    assert.notOk(canRun(status({ effective_model: null, source: "none" })));
    assert.notOk(canRun(status({ setting: { enabled: false, model: null } })));
    assert.notOk(canRun(status({ setting: { enabled: "true" as unknown as boolean, model: null } })), "strict");
    assert.notOk(canRun(null));
    assert.strictEqual(POLL_INTERVAL_MS, 2000, "the status is polled every 2 s while a run is in flight");
});

QUnit.test("the toast of a finished run: the count or the error class", function (assert) {
    assert.deepEqual(runOutcome(status()), { key: "embRunToastDone", args: ["20"] });
    assert.deepEqual(runOutcome(status({ last_run: { ...DONE_RUN, error_class: "APIConnectionError" } })),
        { key: "embRunToastFailed", args: ["APIConnectionError"] });
    assert.deepEqual(runOutcome(status({ last_run: { ...DONE_RUN, finished_at: null } })), { key: "embRunToastUnfinished" });
    assert.strictEqual(runOutcome(status({ last_run: null })), null);
    assert.strictEqual(runOutcome(status({ running: true })), null, "nothing to say while it runs");
});

QUnit.test("the deploy confirmation names the model, the resource group and the billing", function (assert) {
    assert.deepEqual(deployConfirmText("text-embedding-3-large", "rg-agents"),
        { key: "embDeployConfirm", args: ["text-embedding-3-large", "rg-agents"] });
    assert.deepEqual(deployConfirmText("text-embedding-3-large", ""),
        { key: "embDeployConfirmNoGroup", args: ["text-embedding-3-large"] }, "a server that does not say the group");
    assert.deepEqual(deployConfirmText("m", undefined), { key: "embDeployConfirmNoGroup", args: ["m"] });
});

QUnit.test("the deploy dialog lists what AI Core offers and is not deployed", function (assert) {
    assert.deepEqual(deployOptions(status({
        available: [{ model: "a-embedding", versions: ["1", "2"] }, { model: "b-embedding", versions: [] }]
    })), [{ model: "a-embedding", versions: "1, 2" }, { model: "b-embedding", versions: "" }]);
    assert.deepEqual(deployOptions(null), []);
});

QUnit.test("the env strip: the landscape's variable, in effect or not", function (assert) {
    assert.strictEqual(envStripText(status()), null, "no variable, no strip");
    assert.deepEqual(envStripText(status({ env_override: true, source: "env", effective_model: "e" })),
        { key: "embEnvInEffect", args: ["e"] });
    assert.deepEqual(envStripText(status({
        env_override: true, source: "setting", setting: { enabled: true, model: "m" }, effective_model: "m"
    })), { key: "embEnvSet" }, "a model chosen here wins over the variable");
    assert.deepEqual(envStripText(status({ env_override: true, setting: { enabled: false, model: null },
        effective_model: null, source: "none" })), { key: "embEnvSet" });
});

QUnit.test("every text the panel can show is in the i18n bundle", async function (assert) {
    const bundle = await ResourceBundle.create({
        url: sap.ui.require.toUrl("com/agent/admin/i18n/i18n.properties"), async: true
    }) as ResourceBundle;
    const keys = new Set<string>();
    const statuses: EmbeddingsStatus[] = [
        status(), status({ source: "setting", effective_model: "m" }), status({ source: "env", effective_model: "e" }),
        status({ setting: { enabled: false, model: null }, effective_model: null, source: "none" }),
        status({ setting: { enabled: true, model: "x" }, effective_model: null, source: "setting", deployed: [] }),
        status({ effective_model: null, source: "none" }), status({ stale: 0 }),
        status({ running: true, progress: { done: 1, total: 2 } }), status({ running: true }), status({ last_run: null }),
        status({ last_run: { ...DONE_RUN, error_class: "E" } }), status({ last_run: { ...DONE_RUN, finished_at: null } }),
        status({ last_run: { ...DONE_RUN, model: null } }), status({ env_override: true, source: "env", effective_model: "e" }),
        status({ env_override: true })
    ];
    statuses.forEach((value) => {
        statusLines(value).forEach((line) => { keys.add(line.label); keys.add(line.text.key); });
        modelOptions(value).forEach((option) => keys.add(option.text.key));
        const outcome = runOutcome(value);
        const strip = envStripText(value);
        [outcome, strip].forEach((text) => text && keys.add(text.key));
    });
    keys.add(deployConfirmText("m", "rg").key);
    keys.add(deployConfirmText("m", "").key);
    keys.forEach((key) => assert.ok(bundle.hasText(key), key));
    assert.ok(keys.size >= 25, `${keys.size} texts checked`);
    assert.strictEqual(typeof embeddingsSettings.statusLines, "function", "the default export holds the helpers");
});

QUnit.module("AdminService: data catalogue embeddings", {
    beforeEach: function (this: { originalFetch: typeof fetch }) {
        this.originalFetch = window.fetch;
    },
    afterEach: function (this: { originalFetch: typeof fetch }) {
        window.fetch = this.originalFetch;
    }
});

QUnit.test("the four calls use the documented paths, methods and bodies", async function (assert) {
    const calls: string[][] = [];
    window.fetch = ((url: string, init?: RequestInit) => {
        calls.push([`${init?.method ?? "GET"} ${url}`, (init?.body as string) ?? ""]);
        return Promise.resolve(new Response(JSON.stringify({}), {
            status: 202, headers: { "Content-Type": "application/json" }
        }));
    }) as unknown as typeof fetch;
    const service = new AdminService();

    await service.getEmbeddings();
    await service.setEmbeddings({ enabled: true, model: null });
    await service.runEmbeddings();
    await service.deployEmbeddingModel("text-embedding-3-large");

    assert.deepEqual(calls, [
        ["GET backend/data-catalog/embeddings", ""],
        ["PUT backend/data-catalog/embeddings", "{\"enabled\":true,\"model\":null}"],
        ["POST backend/data-catalog/embeddings/run", "{}"],
        ["POST backend/data-catalog/embeddings/deploy", "{\"model\":\"text-embedding-3-large\"}"]
    ]);
});

QUnit.module("FakeBackend: the embeddings routes", {
    beforeEach: function (this: { backend: FakeBackend }) {
        this.backend = new FakeBackend();
        this.backend.reset();
        this.backend.install();
    },
    afterEach: function (this: { backend: FakeBackend }) {
        this.backend.restore();
    }
});

async function refusal(call: () => Promise<unknown>): Promise<AdminError> {
    try {
        await call();
    } catch (e) {
        return e as AdminError;
    }
    throw new Error("should have been refused");
}

QUnit.test("a run answers running for two polls, then done with the vectors", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const before = await service.getEmbeddings();
    assert.strictEqual(before.effective_model, "text-embedding-3-small", "discovery: the first running *embedding*");
    assert.strictEqual(before.source, "discovery");
    assert.deepEqual([before.vectors, before.nodes, before.stale], [20, 24, 4]);
    assert.ok(canRun(before));

    assert.deepEqual(await service.runEmbeddings(), { running: true });
    const first = await service.getEmbeddings();
    assert.ok(first.running);
    assert.deepEqual(first.progress, { done: 1, total: 4 });
    assert.notOk(canRun(first));
    const again = await refusal(() => service.runEmbeddings());
    assert.strictEqual(again.status, 409, "one run at a time");
    assert.strictEqual(again.detail, RUN_IN_FLIGHT);
    const second = await service.getEmbeddings();
    assert.ok(second.running);
    assert.deepEqual(second.progress, { done: 2, total: 4 });
    const done = await service.getEmbeddings();
    assert.notOk(done.running);
    assert.strictEqual(done.progress, null);
    assert.deepEqual([done.vectors, done.stale], [24, 0]);
    assert.strictEqual(done.last_run?.embedded, 4);
    assert.strictEqual(done.last_run?.error_class, null);
    assert.deepEqual(runOutcome(done), { key: "embRunToastDone", args: ["4"] });
});

QUnit.test("a failed run keeps the vectors and reports the error class", async function (this: { backend: FakeBackend }, assert) {
    this.backend.embeddings.runFails = "RateLimitError";
    const service = new AdminService();
    await service.runEmbeddings();
    await service.getEmbeddings();
    await service.getEmbeddings();
    const done = await service.getEmbeddings();
    assert.notOk(done.running);
    assert.strictEqual(done.last_run?.error_class, "RateLimitError");
    assert.deepEqual([done.vectors, done.stale], [20, 4]);
});

QUnit.test("the save: refusals at the field, the setting in effect", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const notDeployed = await refusal(() => service.setEmbeddings({ enabled: true, model: "text-embedding-3-large" }));
    assert.strictEqual(notDeployed.status, 422);
    assert.strictEqual(notDeployed.detail, MODEL_NOT_DEPLOYED);
    assert.strictEqual(refusedField(notDeployed.detail), "model");
    const loose = await refusal(() => service.setEmbeddings({ enabled: "yes", model: null } as never));
    assert.strictEqual(refusedField(loose.detail), "enabled");

    const chosen = await service.setEmbeddings({ enabled: true, model: "text-embedding-3-small" });
    assert.deepEqual(chosen.setting, { enabled: true, model: "text-embedding-3-small" });
    assert.strictEqual(chosen.source, "setting");
    const off = await service.setEmbeddings({ enabled: false, model: null });
    assert.strictEqual(off.effective_model, null);
    assert.strictEqual(off.source, "none");
    assert.notOk(canRun(off));
    assert.deepEqual(this.backend.requests.filter((r) => r.indexOf("embeddings") !== -1), [
        "PUT data-catalog/embeddings", "PUT data-catalog/embeddings", "PUT data-catalog/embeddings",
        "PUT data-catalog/embeddings"
    ]);
});

QUnit.test("a deploy answers PENDING once, and the model stays not running until it starts", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const deployed = await service.deployEmbeddingModel("text-embedding-3-large");
    assert.strictEqual(deployed.status, "PENDING");
    const repeat = await service.deployEmbeddingModel("text-embedding-3-large");
    assert.deepEqual(repeat, deployed, "idempotent: the PENDING deployment is answered, nothing created");
    const unknown = await refusal(() => service.deployEmbeddingModel("gpt-4o"));
    assert.strictEqual(unknown.status, 422);
    assert.strictEqual(unknown.detail, MODEL_NOT_OFFERED);

    const pending = await service.getEmbeddings();
    assert.deepEqual(pending.deployed.filter((d) => d.model === "text-embedding-3-large"),
        [{ model: "text-embedding-3-large", status: "PENDING", deployment_id: deployed.deployment_id }]);
    assert.deepEqual(pending.available.map((a) => a.model), ["text-embedding-ada-002"], "no longer offered to deploy");

    const chosen = await service.setEmbeddings({ enabled: true, model: "text-embedding-3-large" });
    assert.strictEqual(chosen.effective_model, null, "a pending deployment embeds nothing");
    assert.deepEqual(statusLines(chosen)[0].text, { key: "embModelNotRunning", args: ["text-embedding-3-large"] });

    this.backend.embeddings.startDeployments();
    const running = await service.getEmbeddings();
    assert.strictEqual(running.effective_model, "text-embedding-3-large");
    assert.deepEqual([running.vectors, running.stale], [0, 24], "the vectors of another model do not count");
});

QUnit.test("the landscape's variable decides when Model is Automatic", async function (this: { backend: FakeBackend }, assert) {
    this.backend.embeddings.envModel = "text-embedding-ada-002";
    const service = new AdminService();
    const value = await service.getEmbeddings();
    assert.ok(value.env_override);
    assert.strictEqual(value.source, "env");
    assert.strictEqual(value.effective_model, "text-embedding-ada-002");
    assert.deepEqual(envStripText(value), { key: "embEnvInEffect", args: ["text-embedding-ada-002"] });
    assert.strictEqual(value.resource_group, "rg-agents");
});
