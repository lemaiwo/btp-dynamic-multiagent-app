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

/**
 * `actual` of one month of `GET cost/months`: what the usage service billed
 * for SAP AI Core in this subaccount. `estimated` is SAP's own flag: the
 * month is still open, so the figure is not final.
 */
export interface CostActual {
    amount: number;
    currency: string;
    capacity_units: number;
    estimated: boolean;
}

/** `estimated` of one month: the ledger priced with the price table. */
export interface CostEstimate {
    amount: number;
    currency: string;
    /** `false`: a model of the month has no rate, so the amount is a floor. */
    complete: boolean;
}

/** One model of a month, tokens summed over all sources. */
export interface CostModelRow {
    model: string;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    requests: number;
    amount: number | null;
}

/** One UTC month of `GET cost/months`. */
export interface CostMonth {
    /** `YYYY-MM`. */
    month: string;
    actual: CostActual | null;
    /** `ok`, `no_data`, `unreadable` or the state of the usage service. */
    actual_status: string;
    estimated: CostEstimate | null;
    by_source: Record<string, number | null>;
    by_model: CostModelRow[];
}

/**
 * `GET cost/months?months=1..12`: newest month first, the current one
 * included. `usage_service` is `ok`, `off` (no destination), `no_subaccount`,
 * `destination_error`, `unauthorized`, `usage_unavailable`, `too_large`,
 * `not_json` or `unreadable`.
 */
export interface CostMonths {
    usage_service: string;
    currency: string;
    months: CostMonth[];
}
