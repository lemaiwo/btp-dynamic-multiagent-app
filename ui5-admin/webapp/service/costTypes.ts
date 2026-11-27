/**
 * The shapes of the model price routes (`/admin/api/model-prices`,
 * agents/cost_routes.py): the price of one SAP AI Core capacity unit and the
 * rate of each model in capacity units per 1,000 tokens.
 *
 * Every amount is a decimal STRING on the API (at most 9 decimals): the UI
 * never turns one into a JavaScript number, so what is sent is exactly the
 * text that was checked.
 *
 * Kept apart from `types.ts`, which is past the size a file may have.
 */

/** One model's rates, capacity units per 1,000 tokens. */
export interface ModelPriceRow {
    model: string;
    input_rate: string;
    output_rate: string;
    /** `null`: cached input is priced as input. */
    cache_read_rate: string | null;
}

/** `GET model-prices` (and the answer of the `PUT`). */
export interface ModelPrices {
    currency: string;
    /** The price of one capacity unit; `null` when none is set. */
    capacity_unit_price: string | null;
    /** Sorted by model. */
    prices: ModelPriceRow[];
    /** Models the usage ledger has seen without a rate, sorted. */
    unpriced_models: string[];
    /** The stamp of the last save; `null` for a table never saved. */
    updated_at: string | null;
}

/** The body of `PUT model-prices`: exactly these three keys. */
export interface ModelPricesSave {
    /** The `updated_at` the form was read with (`null` for a table never saved). */
    expected_updated_at: string | null;
    capacity_unit_price: string | null;
    prices: ModelPriceRow[];
}
