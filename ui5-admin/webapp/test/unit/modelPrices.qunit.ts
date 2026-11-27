import {
    MAX_MODEL_CHARS, fromAnswer, isAmount, isDirty, newRow, refusedAt, sentRows, toPayload, unpricedRows, validate
} from "com/agent/admin/model/modelPrices";
import type { PriceRowForm, PricesForm } from "com/agent/admin/model/modelPrices";
import AdminService, { AdminError } from "com/agent/admin/service/AdminService";
import type { ModelPrices } from "com/agent/admin/service/costTypes";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";
import { AMOUNT_RULE, DUPLICATE_MODEL, STALE } from "com/agent/admin/test/integration/FakeModelPrices";

// The logic of the "Model prices" panel of the Settings page, the two calls
// of AdminService, and the fake's routes.

function answer(extra: Partial<ModelPrices> = {}): ModelPrices {
    return {
        currency: "EUR",
        capacity_unit_price: "7.5",
        prices: [{ model: "claude-sonnet-4-5", input_rate: "0.25", output_rate: "1.2", cache_read_rate: null }],
        unpriced_models: ["gpt-4o"],
        updated_at: "2026-11-20T10:00:00+00:00",
        ...extra
    };
}

function row(extra: Partial<PriceRowForm> = {}): PriceRowForm {
    return { model: "m", input: "1", cached: "", output: "2", unpriced: false, ...extra };
}

function form(rows: PriceRowForm[], cuPrice = "7.5", stamp: string | null = "2026-11-20T10:00:00+00:00"): PricesForm {
    return { cuPrice, rows, stamp };
}

QUnit.module("modelPrices: form and payload");

QUnit.test("fromAnswer: the stored rows, then one empty row per model without a price", function (assert) {
    const result = fromAnswer(answer({
        prices: [{ model: "a", input_rate: "0.1", output_rate: "0.2", cache_read_rate: "0.05" }],
        unpriced_models: ["b", "c"]
    }));
    assert.strictEqual(result.cuPrice, "7.5");
    assert.strictEqual(result.stamp, "2026-11-20T10:00:00+00:00");
    assert.deepEqual(result.rows, [
        { model: "a", input: "0.1", cached: "0.05", output: "0.2", unpriced: false },
        { model: "b", input: "", cached: "", output: "", unpriced: true },
        { model: "c", input: "", cached: "", output: "", unpriced: true }
    ]);
    assert.deepEqual(fromAnswer(answer({ capacity_unit_price: null, updated_at: null, prices: [], unpriced_models: [] })),
        { cuPrice: "", rows: [], stamp: null }, "nothing stored: blank price, no stamp");
});

QUnit.test("unpricedRows: a model of the ledger that has a stored rate is not listed twice", function (assert) {
    assert.deepEqual(unpricedRows(answer({ unpriced_models: ["gpt-4o", "claude-sonnet-4-5"] })).map((r) => r.model), ["gpt-4o"]);
    assert.deepEqual(unpricedRows(null), []);
    assert.ok(unpricedRows(answer()).every((r) => r.unpriced && r.input === "" && r.output === ""));
});

QUnit.test("toPayload: rows with both required rates, values as typed, cached and CU price null when blank", function (assert) {
    const body = toPayload(form([
        row({ model: "a", input: "0.250", output: "1", cached: "" }),
        row({ model: "b", input: "", output: "", unpriced: true }),
        row({ model: "c", input: "3", output: "4", cached: "0.5" })
    ], ""));
    assert.deepEqual(body, {
        expected_updated_at: "2026-11-20T10:00:00+00:00",
        capacity_unit_price: null,
        prices: [
            { model: "a", input_rate: "0.250", output_rate: "1", cache_read_rate: null },
            { model: "c", input_rate: "3", output_rate: "4", cache_read_rate: "0.5" }
        ]
    });
    assert.strictEqual(typeof body.prices[0].input_rate, "string", "a rate stays a string");
    assert.deepEqual(Object.keys(body), ["expected_updated_at", "capacity_unit_price", "prices"], "exactly the three keys");
    const first = toPayload(form([], "12.000000001", null));
    assert.strictEqual(first.expected_updated_at, null, "a table never saved sends null");
    assert.strictEqual(first.capacity_unit_price, "12.000000001", "the CU price as typed");
    assert.deepEqual(sentRows(form([row(), row({ input: "", output: "" , unpriced: true }), row()])), [0, 2]);
});

QUnit.test("isDirty: a change of what would be sent; an empty new row is none", function (assert) {
    const stored = fromAnswer(answer());
    assert.notOk(isDirty(fromAnswer(answer()), stored));
    const added = fromAnswer(answer());
    added.rows.push(newRow());
    assert.notOk(isDirty(added, stored), "an empty row sends nothing");
    const priced = fromAnswer(answer());
    priced.rows[1].input = "1";
    priced.rows[1].output = "2";
    assert.ok(isDirty(priced, stored), "the unpriced model got rates");
    const cu = fromAnswer(answer());
    cu.cuPrice = "8";
    assert.ok(isDirty(cu, stored));
});

QUnit.module("modelPrices: validation");

QUnit.test("isAmount: the server's plain decimal, 0 to 1000000, at most 9 decimals", function (assert) {
    ["0", "0.5", "1000000", "1000000.000000000", "0000001.5", "12.123456789"].forEach((value) => {
        assert.ok(isAmount(value), `accepted: ${value}`);
    });
    ["", " 1", "1 ", "-1", "+1", "1e-5", "1,5", ".5", "5.", "1000000.1", "1000001", "12345678",
        "1.1234567891", "１", "NaN"].forEach((value) => {
        assert.notOk(isAmount(value), `refused: ${value}`);
    });
});

QUnit.test("validate: fixed texts that never quote a value", function (assert) {
    const problems = validate(form([
        row({ model: "dup", input: "0.1", output: "0.2" }),
        row({ model: "dup", input: "abc", output: "0.2", cached: "x" }),
        row({ model: "", input: "1", output: "2" }),
        row({ model: " padded", input: "1", output: "" }),
        row({ model: "only-model", input: "", output: "" }),
        row({ model: "x".repeat(MAX_MODEL_CHARS + 1), input: "1", output: "2" }),
        row({ model: "tab\tname", input: "1", output: "2" }),
        row({ model: "gpt-4o", input: "", output: "", unpriced: true }),
        newRow()
    ], "-3"));
    const seen = problems.map((p) => `${p.row}:${p.field}:${p.text.key}`);
    assert.deepEqual(seen, [
        "null:cuPrice:mpcErrAmount",
        "1:model:mpcErrModelDuplicate",
        "1:input:mpcErrAmount",
        "1:cached:mpcErrAmount",
        "2:model:mpcErrModelBlank",
        "3:model:mpcErrModelEdge",
        "3:output:mpcErrRequired",
        "4:input:mpcErrRequired",
        "4:output:mpcErrRequired",
        "5:model:mpcErrModelText",
        "6:model:mpcErrModelText"
    ], "the unpriced row and the empty new row are left out, not refused");
    problems.forEach((p) => assert.notOk(p.text.args && p.text.args.length, "no argument: nothing of the input"));
    assert.deepEqual(validate(fromAnswer(answer())), [], "the stored table passes");
    assert.deepEqual(validate(form([], "")), [], "no CU price is allowed");
});

QUnit.test("refusedAt: the row and field of a 422, through the rows that were sent", function (assert) {
    const sent = [0, 2, 5];
    assert.deepEqual(refusedAt("prices[1].output_rate: a number from 0 to 1000000 with at most 9 decimals", sent),
        { row: 2, field: "output" });
    assert.deepEqual(refusedAt("prices[2].model: a model of an earlier row", sent), { row: 5, field: "model" });
    assert.deepEqual(refusedAt("models[0].model: no blanks", sent), { row: 0, field: "model" });
    assert.deepEqual(refusedAt("prices[0].input_rate: x", sent), { row: 0, field: "input" });
    assert.deepEqual(refusedAt("prices[0].cache_read_rate: x", sent), { row: 0, field: "cached" });
    assert.deepEqual(refusedAt("prices[1]: unknown key", sent), { row: 2, field: "model" });
    assert.deepEqual(refusedAt("capacity_unit_price: a number", sent), { row: null, field: "cuPrice" });
    assert.strictEqual(refusedAt("prices[9].model: x", sent), null, "an index that was not sent");
    assert.strictEqual(refusedAt("prices: a list of at most 200 rows", sent), null);
    assert.strictEqual(refusedAt("", sent), null);
    assert.strictEqual(refusedAt(null, sent), null);
});

QUnit.module("AdminService and FakeBackend: model prices", {
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

QUnit.test("read, save with the stamp, the new stamp; a stale stamp is a 409", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const first = await service.getModelPrices();
    assert.strictEqual(first.currency, "EUR");
    assert.deepEqual(first.unpriced_models, ["gpt-4o"]);
    const body = toPayload(fromAnswer(first));
    body.prices.push({ model: "gpt-4o", input_rate: "0.5", output_rate: "1.5", cache_read_rate: null });
    const saved = await service.setModelPrices(body);
    assert.deepEqual(this.backend.bodies["PUT model-prices"], body as unknown as Record<string, unknown>, "the body as built");
    assert.deepEqual(saved.unpriced_models, [], "gpt-4o has a rate now");
    assert.notStrictEqual(saved.updated_at, first.updated_at, "a new stamp");
    const stale = await refusal(() => service.setModelPrices(body));
    assert.strictEqual(stale.status, 409);
    assert.strictEqual(stale.detail, STALE);
});

QUnit.test("the refusals name the field and never the value", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const stamp = (await service.getModelPrices()).updated_at;
    const duplicate = await refusal(() => service.setModelPrices({ expected_updated_at: stamp, capacity_unit_price: null,
        prices: [{ model: "a", input_rate: "1", output_rate: "1", cache_read_rate: null },
            { model: "a", input_rate: "1", output_rate: "1", cache_read_rate: null }] }));
    assert.strictEqual(duplicate.status, 422);
    assert.strictEqual(duplicate.detail, DUPLICATE_MODEL);
    const amount = await refusal(() => service.setModelPrices({ expected_updated_at: stamp, capacity_unit_price: "1",
        prices: [{ model: "a", input_rate: "1", output_rate: "secret-ish", cache_read_rate: null }] }));
    assert.strictEqual(amount.detail, `prices[0].output_rate: ${AMOUNT_RULE}`);
    assert.strictEqual(amount.detail.indexOf("secret-ish"), -1);
    const first = await refusal(() => service.setModelPrices({ expected_updated_at: null, capacity_unit_price: null,
        prices: [] }));
    assert.strictEqual(first.status, 409, "null on a table that was saved is stale");
});
