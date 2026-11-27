import type { ModelPriceRow, ModelPrices } from "com/agent/admin/service/costTypes";

type Json = (body: unknown, status?: number, headers?: Record<string, string>) => Promise<Response>;
type Body = Record<string, unknown>;

/** The refusals of agents/cost_routes.py and agents/model_prices.py: fixed texts, never a value. */
export const AMOUNT_RULE = "a number from 0 to 1000000 with at most 9 decimals";
export const DUPLICATE_MODEL_RULE = "a model of an earlier row";
/** The duplicate of the second row of a body, as a test sends it. */
export const DUPLICATE_MODEL = `prices[1].model: ${DUPLICATE_MODEL_RULE}`;
export const STALE = "The prices were changed meanwhile: reload them";

const BODY_KEYS = ["expected_updated_at", "capacity_unit_price", "prices"];
const ROW_KEYS = ["model", "input_rate", "output_rate", "cache_read_rate"];
const AMOUNT_RE = /^([0-9]{1,7})(?:\.([0-9]{1,9}))?$/;
function hasControl(value: string): boolean {
    for (let i = 0; i < value.length; i++) {
        const c = value.charCodeAt(i);
        if (c <= 0x1f || (c >= 0x7f && c <= 0x9f) || c === 0x2028 || c === 0x2029) {
            return true;
        }
    }
    return false;
}

function isObject(value: unknown): value is Body {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** `parse_amount` on a string (the UI sends nothing else): the pattern and at most 1,000,000. */
function isAmount(value: unknown): boolean {
    const match = typeof value === "string" ? AMOUNT_RE.exec(value) : null;
    if (!match) {
        return false;
    }
    const whole = match[1].replace(/^0+(?=\d)/, "");
    return whole.length < 7 || (whole === "1000000" && /^0*$/.test(match[2] ?? ""));
}

/**
 * The `/admin/api/model-prices` routes of the fake backend, apart from
 * `FakeBackend` so neither file grows. Keeps the CU price, the rows and the
 * stamp, and answers 409 when a save's `expected_updated_at` is not the
 * stamp (`null` only for a table never saved), as the server does.
 *
 * The fixture: a CU price of 7.5, one stored rate (`claude-sonnet-4-5`),
 * and the ledger has also seen `gpt-4o`, which has no rate. `bump()` stands
 * in for another admin's save (the stamp moves, nothing else).
 */
export default class FakeModelPrices {

    public cuPrice: string | null = null;
    public rows: ModelPriceRow[] = [];
    /** The models the usage ledger has seen. */
    public seen: string[] = [];
    public stamp: string | null = null;
    private saves = 0;

    public reset(): void {
        this.cuPrice = "7.5";
        this.rows = [{ model: "claude-sonnet-4-5", input_rate: "0.25", output_rate: "1.2", cache_read_rate: null }];
        this.seen = ["claude-sonnet-4-5", "gpt-4o"];
        this.stamp = "2026-11-20T10:00:00+00:00";
        this.saves = 0;
    }

    /** Another admin saved meanwhile: a new stamp. */
    public bump(): void {
        this.saves += 1;
        this.stamp = `2026-11-21T10:00:${String(this.saves).padStart(2, "0")}+00:00`;
    }

    public status(): ModelPrices {
        const priced = this.rows.map((row) => row.model);
        return {
            currency: "EUR",
            capacity_unit_price: this.cuPrice,
            prices: this.rows.slice().sort((a, b) => (a.model < b.model ? -1 : a.model > b.model ? 1 : 0))
                .map((row) => ({ ...row })),
            unpriced_models: this.seen.filter((model) => priced.indexOf(model) === -1).sort(),
            updated_at: this.stamp
        };
    }

    /** Answers a `model-prices` route, or `undefined` for any other path. */
    public handle(path: string, method: string, body: unknown, json: Json): Promise<Response> | undefined {
        if (path !== "model-prices") {
            return undefined;
        }
        if (method === "GET") {
            return json(this.status());
        }
        if (method === "PUT") {
            return this.save(body, json);
        }
        return json({ detail: "Method Not Allowed" }, 405);
    }

    /** The checks of `replace_table`, the first refusal only, then the stamp, then the write. */
    private save(body: unknown, json: Json): Promise<Response> {
        const refuse = (detail: string) => json({ detail }, 422);
        if (!isObject(body)) {
            return refuse("body: a JSON object");
        }
        if (Object.keys(body).some((key) => BODY_KEYS.indexOf(key) === -1)) {
            return refuse("body: unknown key");
        }
        if (!("expected_updated_at" in body)) {
            return refuse("expected_updated_at: required (null for a table never saved)");
        }
        if (!("capacity_unit_price" in body)) {
            return refuse("capacity_unit_price: required (null for no price)");
        }
        if (!Array.isArray(body.prices) || body.prices.length > 200) {
            return refuse("prices: a list of at most 200 rows");
        }
        if (body.capacity_unit_price !== null && !isAmount(body.capacity_unit_price)) {
            return refuse(`capacity_unit_price: ${AMOUNT_RULE}`);
        }
        const rows: ModelPriceRow[] = [];
        for (let i = 0; i < body.prices.length; i++) {
            const row = body.prices[i] as unknown;
            if (!isObject(row)) {
                return refuse(`prices[${i}]: an object`);
            }
            if (Object.keys(row).some((key) => ROW_KEYS.indexOf(key) === -1)) {
                return refuse(`prices[${i}]: unknown key`);
            }
            const model = row.model;
            if (typeof model !== "string" || model.length < 1 || model.length > 128 || hasControl(model)
                || model !== model.trim()) {
                return refuse(`prices[${i}].model: 1 to 128 characters without control characters`);
            }
            if (rows.some((earlier) => earlier.model === model)) {
                return refuse(`prices[${i}].model: ${DUPLICATE_MODEL_RULE}`);
            }
            for (const key of ["input_rate", "output_rate"]) {
                if (!(key in row)) {
                    return refuse(`prices[${i}].${key}: required`);
                }
                if (!isAmount(row[key])) {
                    return refuse(`prices[${i}].${key}: ${AMOUNT_RULE}`);
                }
            }
            const cached = row.cache_read_rate ?? null;
            if (cached !== null && !isAmount(cached)) {
                return refuse(`prices[${i}].cache_read_rate: ${AMOUNT_RULE}`);
            }
            rows.push({ model, input_rate: row.input_rate as string, output_rate: row.output_rate as string,
                cache_read_rate: cached as string | null });
        }
        if (body.expected_updated_at !== this.stamp) {
            return json({ detail: STALE }, 409);
        }
        this.cuPrice = body.capacity_unit_price as string | null;
        this.rows = rows;
        this.bump();
        return json(this.status());
    }
}
