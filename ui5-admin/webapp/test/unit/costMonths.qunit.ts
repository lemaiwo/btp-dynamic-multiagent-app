import ResourceBundle from "sap/base/i18n/ResourceBundle";
import { MONTH_CHOICES, monthsOf, rows, serviceLine } from "com/agent/admin/model/costMonths";
import type { TextOf } from "com/agent/admin/model/costMonths";
import AdminService from "com/agent/admin/service/AdminService";
import type { CostMonth, CostMonths } from "com/agent/admin/service/costTypes";
import FakeBackend from "com/agent/admin/test/integration/FakeBackend";

// The logic of the "AI Core cost" panel of the Settings page, the call of
// AdminService and the fake's route.

async function bundle(): Promise<ResourceBundle> {
    return await ResourceBundle.create({
        url: sap.ui.require.toUrl("com/agent/admin/i18n/i18n.properties"), async: true
    }) as ResourceBundle;
}

/** Texts through the real bundle, as the panel gets them. */
async function textOf(): Promise<TextOf> {
    const b = await bundle();
    return (key, args) => b.getText(key, args) ?? "";
}

function month(extra: Partial<CostMonth> = {}): CostMonth {
    return {
        month: "2026-10",
        actual: { amount: 12.34, currency: "EUR", capacity_units: 8, estimated: false },
        actual_status: "ok",
        estimated: { amount: 10.02, currency: "EUR", complete: true },
        by_source: { job: 10.02, workflow_step: null, chat: null },
        by_model: [],
        ...extra
    };
}

function answer(months: CostMonth[], usage = "ok"): CostMonths {
    return { usage_service: usage, currency: "EUR", months };
}

QUnit.module("costMonths: rows");

QUnit.test("a billed month: month, billed, units, EUR per unit, estimate and difference", async function (assert) {
    const text = await textOf();
    const [row] = rows(answer([month()]), text);
    assert.strictEqual(row.month, "October 2026");
    assert.strictEqual(row.billed, "€ 12.34");
    assert.strictEqual(row.units, "8");
    assert.strictEqual(row.perUnit, "€ 1.5425", "amount / units, up to four decimals");
    assert.strictEqual(row.estimated, "≈ € 10.02");
    assert.strictEqual(row.difference, "+ € 2.32", "billed minus estimated");
    assert.strictEqual(row.note, "");
    assert.notOk(row.open);
});

QUnit.test("rows keep the answer's order (newest first) and mark the open month not final", async function (assert) {
    const text = await textOf();
    const result = rows(answer([
        month({ month: "2026-11", actual: { amount: 1, currency: "EUR", capacity_units: 2, estimated: true } }),
        month({ month: "2026-10" })
    ]), text);
    assert.deepEqual(result.map((r) => r.month), ["November 2026", "October 2026"]);
    assert.ok(result[0].open, "SAP says the month is estimated");
    assert.strictEqual(result[0].note, "not final");
    assert.notOk(result[1].open);
    assert.strictEqual(result[0].perUnit, "€ 0.50");
});

QUnit.test("without the bill, the current month (the first) is still the open one", async function (assert) {
    const text = await textOf();
    const result = rows(answer([
        month({ month: "2026-11", actual: null, actual_status: "off" }),
        month({ month: "2026-10", actual: null, actual_status: "off" })
    ], "off"), text);
    assert.ok(result[0].open);
    assert.strictEqual(result[0].note, "not final");
    assert.notOk(result[1].open);
    assert.strictEqual(result[1].note, "", "the panel's line says why there is no bill, not every row");
    assert.strictEqual(result[1].billed, "—");
    assert.strictEqual(result[1].units, "—");
    assert.strictEqual(result[1].perUnit, "—");
    assert.strictEqual(result[1].estimated, "≈ € 10.02", "estimates only");
    assert.strictEqual(result[1].difference, "—", "no difference without the bill");
});

QUnit.test("estimate: partly priced, and a dash when there is none", async function (assert) {
    const text = await textOf();
    const [partly, none] = rows(answer([
        month({ estimated: { amount: 10.02, currency: "EUR", complete: false } }),
        month({ estimated: null })
    ]), text);
    assert.strictEqual(partly.estimated, "≈ € 10.02 (partly priced)");
    assert.strictEqual(partly.difference, "+ € 2.32", "both figures exist");
    assert.strictEqual(none.estimated, "—");
    assert.strictEqual(none.difference, "—");
});

QUnit.test("difference: below the estimate, equal, and grouped amounts", async function (assert) {
    const text = await textOf();
    const [less, equal, big] = rows(answer([
        month({ actual: { amount: 8, currency: "EUR", capacity_units: 4, estimated: false } }),
        month({ actual: { amount: 10.02, currency: "EUR", capacity_units: 4, estimated: false } }),
        month({ actual: { amount: 12345.6, currency: "EUR", capacity_units: 1000, estimated: false },
            estimated: { amount: 0.6, currency: "EUR", complete: true } })
    ]), text);
    assert.strictEqual(less.difference, "− € 2.02");
    assert.strictEqual(equal.difference, "€ 0.00");
    assert.strictEqual(big.billed, "€ 12,345.60");
    assert.strictEqual(big.units, "1,000");
    assert.strictEqual(big.difference, "+ € 12,345.00");
});

QUnit.test("EUR per unit only when there are units", async function (assert) {
    const text = await textOf();
    const [zero] = rows(answer([
        month({ actual: { amount: 3, currency: "EUR", capacity_units: 0, estimated: false } })
    ]), text);
    assert.strictEqual(zero.perUnit, "—");
    assert.strictEqual(zero.units, "0");
    assert.strictEqual(zero.billed, "€ 3.00");
});

QUnit.test("a bill in another currency: its code, and no difference", async function (assert) {
    const text = await textOf();
    const [row] = rows(answer([
        month({ actual: { amount: 12.34, currency: "USD", capacity_units: 8, estimated: false } })
    ]), text);
    assert.strictEqual(row.billed, "USD 12.34");
    assert.strictEqual(row.perUnit, "USD 1.5425");
    assert.strictEqual(row.difference, "—", "euros and dollars are not subtracted");
});

QUnit.test("a month the service could not give: no_data, unreadable, another code", async function (assert) {
    const text = await textOf();
    const result = rows(answer([
        month({ month: "2026-11", actual: null, actual_status: "no_data" }),
        month({ month: "2026-10", actual: null, actual_status: "no_data" }),
        month({ month: "2026-09", actual: null, actual_status: "unreadable" }),
        month({ month: "2026-08", actual: null, actual_status: "weird_code" }),
        month({ month: "2026-07", actual: null, actual_status: "<b>not a code</b>" })
    ]), text);
    assert.strictEqual(result[0].note, "not final · No AI Core charge reported for this month", "the open month says both");
    assert.strictEqual(result[1].note, "No AI Core charge reported for this month");
    assert.strictEqual(result[2].note, "This month could not be read from the usage service (two currencies in one month).");
    assert.strictEqual(result[3].note, "The billed cost of this month could not be read (weird_code).");
    assert.strictEqual(result[4].note, "The billed cost of this month could not be read (unknown).", "only an identifier is shown");
    assert.ok(result.every((r) => r.billed === "—" && r.difference === "—"));
});

QUnit.test("values that are not the promised shape are a dash, never NaN", async function (assert) {
    const text = await textOf();
    const odd = { amount: "12", currency: "EUR", capacity_units: Number.NaN, estimated: false } as unknown;
    const [row] = rows(answer([month({ actual: odd as CostMonth["actual"] })]), text);
    assert.strictEqual(row.billed, "—");
    assert.strictEqual(row.perUnit, "—");
    assert.strictEqual(row.difference, "—");
    assert.deepEqual(rows(null, text), []);
    assert.deepEqual(rows({ usage_service: "ok", currency: "EUR", months: "x" } as unknown as CostMonths, text), []);
});

QUnit.module("costMonths: the service line and the month choice");

QUnit.test("one line per state of the usage service; none when it answered", async function (assert) {
    const text = await textOf();
    assert.strictEqual(serviceLine(answer([], "ok"), text), null);
    const off = serviceLine(answer([], "off"), text);
    assert.strictEqual(off?.type, "Information");
    assert.ok(off?.text.indexOf("BTP_USAGE") !== -1, "says which destination to create");
    assert.ok(off?.text.indexOf("Estimates only") !== -1);
    assert.ok(serviceLine(answer([], "no_subaccount"), text)?.text.indexOf("USAGE_SUBACCOUNT_ID") !== -1);
    assert.ok(serviceLine(answer([], "usage_unavailable"), text)?.text.indexOf("not available") !== -1);
    assert.ok(serviceLine(answer([], "unauthorized"), text)?.text.indexOf("refused") !== -1);
    assert.strictEqual(serviceLine(answer([], "not_json"), text)?.text,
        "The billed cost could not be read (not_json). Estimates only.");
    assert.strictEqual(serviceLine(answer([], "not_json"), text)?.type, "Warning");
    assert.strictEqual(serviceLine(answer([], "a b"), text)?.text,
        "The billed cost could not be read (unknown). Estimates only.");
    assert.strictEqual(serviceLine(null, text), null);
});

QUnit.test("every text the panel can show is in the i18n bundle", async function (assert) {
    const b = await bundle();
    const keys = new Set<string>();
    const recording: TextOf = (key, args) => {
        keys.add(key);
        return b.getText(key, args) ?? "";
    };
    rows(answer([
        month({ month: "2026-11", actual: { amount: 1, currency: "EUR", capacity_units: 2, estimated: true } }),
        month({ estimated: { amount: 1, currency: "EUR", complete: false } }),
        month({ actual: { amount: 1, currency: "EUR", capacity_units: 2, estimated: false } }),
        month({ actual: { amount: 10.02, currency: "EUR", capacity_units: 0, estimated: false } }),
        month({ actual: { amount: 1, currency: "USD", capacity_units: 1, estimated: false } }),
        month({ actual: null, actual_status: "no_data", estimated: null }),
        month({ actual: null, actual_status: "unreadable" }),
        month({ actual: null, actual_status: "x" })
    ]), recording);
    ["ok", "off", "no_subaccount", "usage_unavailable", "unauthorized", "x"].forEach((state) =>
        serviceLine(answer([], state), recording));
    keys.forEach((key) => assert.ok(b.hasText(key), `${key} is in the bundle`));
    assert.ok(keys.size >= 14, "the rows and the lines use the bundle");
});

QUnit.test("the month choice: 3, 6 or 12, anything else is 6", function (assert) {
    assert.deepEqual(MONTH_CHOICES, [3, 6, 12]);
    assert.strictEqual(monthsOf("12"), 12);
    assert.strictEqual(monthsOf("3"), 3);
    assert.strictEqual(monthsOf("7"), 6);
    assert.strictEqual(monthsOf(undefined), 6);
});

QUnit.module("AdminService and FakeBackend: cost months", {
    beforeEach: function (this: { backend: FakeBackend }) {
        this.backend = new FakeBackend();
        this.backend.reset();
        this.backend.install();
    },
    afterEach: function (this: { backend: FakeBackend }) {
        this.backend.restore();
    }
});

QUnit.test("getCostMonths asks for the number of months; the fake answers that many, newest first", async function (this: { backend: FakeBackend }, assert) {
    const service = new AdminService();
    const six = await service.getCostMonths();
    assert.strictEqual(this.backend.costMonths.lastMonths, 6, "6 by default");
    assert.strictEqual(six.months.length, 6);
    assert.strictEqual(six.usage_service, "ok");
    const twelve = await service.getCostMonths(12);
    assert.strictEqual(this.backend.costMonths.lastMonths, 12);
    assert.strictEqual(twelve.months.length, 12);
    assert.ok(twelve.months[0].month > twelve.months[1].month, "newest first");
    assert.strictEqual(this.backend.countRequests("GET cost/months"), 2);
});
