import type { ModelPriceRow, ModelPrices, ModelPricesSave } from "../service/costTypes";

/**
 * The logic of the "Model prices" panel of the Settings page, without a
 * view: the form of an answer, the body of a save, whether the form holds
 * unsaved changes, the client-side check of what is typed and which row and
 * field a refusal of the server names.
 *
 * Amounts are decimal TEXT end to end. A rate is checked against the
 * server's own rule (`isAmount`) and sent exactly as typed, never through a
 * JavaScript number. Texts are i18n keys without arguments: a refusal never
 * quotes what was typed. The server stays the gate (agents/model_prices.py).
 */

/** The longest model name the server accepts. */
export const MAX_MODEL_CHARS = 128;

/** The server's amount pattern: 1 to 7 digits, optionally a point and 1 to 9 decimals (ASCII only). */
const AMOUNT_RE = /^([0-9]{1,7})(?:\.([0-9]{1,9}))?$/;

/** A control, zero-width, bidi or separator character, which the server refuses in a model name. */
function hasControl(value: string): boolean {
    for (let i = 0; i < value.length; i++) {
        const c = value.charCodeAt(i);
        if (c <= 0x1f || (c >= 0x7f && c <= 0x9f) || (c >= 0x200b && c <= 0x200f) || (c >= 0x2028 && c <= 0x202e)
            || (c >= 0x2060 && c <= 0x2064) || c === 0xfeff) {
            return true;
        }
    }
    return false;
}

/** One row of the table as the panel edits it. */
export interface PriceRowForm {
    model: string;
    input: string;
    /** Blank: cached input is priced as input. */
    cached: string;
    output: string;
    /** A model the usage ledger has seen without a rate ("No price"). */
    unpriced: boolean;
}

/** What the panel edits: the CU price, the rows, and the stamp it was read with. */
export interface PricesForm {
    cuPrice: string;
    rows: PriceRowForm[];
    /** `updated_at` of the answer the form came from (`null`: never saved). */
    stamp: string | null;
}

/** A field of a row a check or a refusal can name. */
export type PriceField = "model" | "input" | "cached" | "output";

/** A text of the panel as an i18n key. */
export interface PriceText {
    key: string;
    args?: string[];
}

/** A problem with what is typed: the row (`null` for the CU price) and the field. */
export interface PriceProblem {
    row: number | null;
    field: PriceField | "cuPrice";
    text: PriceText;
}

/** Where a refusal of the server points. */
export interface PriceTarget {
    row: number | null;
    field: PriceField | "cuPrice";
}

function text(value: unknown): string {
    return typeof value === "string" ? value : "";
}

function blank(value: string): boolean {
    return text(value).trim() === "";
}

/**
 * The server's amount rule on the TEXT: the pattern, then at most 1,000,000.
 * Compared as digits, never parsed into a float.
 */
export function isAmount(value: string | null | undefined): boolean {
    const match = AMOUNT_RE.exec(text(value));
    if (!match) {
        return false;
    }
    const whole = match[1].replace(/^0+(?=\d)/, "");
    const fraction = match[2] ?? "";
    if (whole.length < 7) {
        return true;
    }
    // Seven digits: only 1000000 itself, with no decimal above zero.
    return whole === "1000000" && /^0*$/.test(fraction);
}

/** An empty row for "Add model". */
export function newRow(): PriceRowForm {
    return { model: "", input: "", cached: "", output: "", unpriced: false };
}

/** The models of the ledger without a stored rate, one empty row each, marked. */
export function unpricedRows(answer: ModelPrices | null | undefined): PriceRowForm[] {
    const priced = (Array.isArray(answer?.prices) ? answer!.prices : []).map((p) => p?.model);
    const seen = Array.isArray(answer?.unpriced_models) ? answer!.unpriced_models : [];
    return seen.filter((model) => typeof model === "string" && model !== "" && priced.indexOf(model) === -1)
        .map((model) => ({ ...newRow(), model, unpriced: true }));
}

/** An answer as the panel edits it: the stored rows, then the unpriced models. */
export function fromAnswer(answer: ModelPrices | null | undefined): PricesForm {
    const stored = (Array.isArray(answer?.prices) ? answer!.prices : []).map((p): PriceRowForm => ({
        model: text(p?.model),
        input: text(p?.input_rate),
        cached: text(p?.cache_read_rate),
        output: text(p?.output_rate),
        unpriced: false
    }));
    return {
        cuPrice: text(answer?.capacity_unit_price),
        rows: stored.concat(unpricedRows(answer)),
        stamp: typeof answer?.updated_at === "string" ? answer!.updated_at : null
    };
}

/** A row with no rate at all that is left out of a save: an unpriced model, or an empty new row. */
function leftOut(row: PriceRowForm): boolean {
    return blank(row.input) && blank(row.cached) && blank(row.output) && (row.unpriced || blank(row.model));
}

/** The indexes of the rows a save sends, in order: those with both required rates. */
export function sentRows(form: PricesForm): number[] {
    const sent: number[] = [];
    form.rows.forEach((row, index) => {
        if (!blank(row.input) && !blank(row.output)) {
            sent.push(index);
        }
    });
    return sent;
}

/**
 * The body of the PUT: exactly `expected_updated_at`, `capacity_unit_price`
 * and `prices`. Values as typed; a blank cached rate and a blank CU price
 * are `null`.
 */
export function toPayload(form: PricesForm): ModelPricesSave {
    const prices = sentRows(form).map((index): ModelPriceRow => {
        const row = form.rows[index];
        return {
            model: row.model,
            input_rate: row.input,
            output_rate: row.output,
            cache_read_rate: blank(row.cached) ? null : row.cached
        };
    });
    return {
        expected_updated_at: form.stamp,
        capacity_unit_price: blank(form.cuPrice) ? null : form.cuPrice,
        prices
    };
}

/** The form would send something else than `stored`. */
export function isDirty(form: PricesForm, stored: PricesForm): boolean {
    const now = toPayload(form);
    const then = toPayload(stored);
    return now.capacity_unit_price !== then.capacity_unit_price || JSON.stringify(now.prices) !== JSON.stringify(then.prices);
}

function modelProblem(name: string, earlier: string[]): string {
    if (blank(name)) {
        return "mpcErrModelBlank";
    }
    if (name !== name.trim()) {
        return "mpcErrModelEdge";
    }
    if (name.length > MAX_MODEL_CHARS || hasControl(name)) {
        return "mpcErrModelText";
    }
    return earlier.indexOf(name) !== -1 ? "mpcErrModelDuplicate" : "";
}

/**
 * What is typed, checked before anything is sent: the CU price, and per row
 * that is not left out the model (not blank, no edge blanks, length and
 * characters, not a model of an earlier row) and the rates (input and output
 * required, every rate given an amount). In row order, model first.
 */
export function validate(form: PricesForm): PriceProblem[] {
    const problems: PriceProblem[] = [];
    if (!blank(form.cuPrice) && !isAmount(form.cuPrice)) {
        problems.push({ row: null, field: "cuPrice", text: { key: "mpcErrAmount" } });
    }
    const names: string[] = [];
    form.rows.forEach((row, index) => {
        if (leftOut(row)) {
            return;
        }
        const model = modelProblem(text(row.model), names);
        if (model) {
            problems.push({ row: index, field: "model", text: { key: model } });
        }
        names.push(text(row.model));
        (["input", "cached", "output"] as PriceField[]).forEach((field) => {
            const value = text(row[field as "input" | "cached" | "output"]);
            if (blank(value)) {
                if (field !== "cached") {
                    problems.push({ row: index, field, text: { key: "mpcErrRequired" } });
                }
            } else if (!isAmount(value)) {
                problems.push({ row: index, field, text: { key: "mpcErrAmount" } });
            }
        });
    });
    return problems;
}

const SERVER_FIELD: Record<string, PriceField> = {
    model: "model", input_rate: "input", output_rate: "output", cache_read_rate: "cached"
};

/**
 * The row and field a 422 `detail` names (`prices[i].<key>: <rule>`,
 * `prices[i]: <rule>`, `capacity_unit_price: <rule>`), with the payload
 * index `i` turned into the form row through `sent` (`sentRows`). `null`
 * for anything else.
 */
export function refusedAt(detail: string | null | undefined, sent: number[]): PriceTarget | null {
    const value = text(detail);
    if (/^capacity_unit_price:/.test(value)) {
        return { row: null, field: "cuPrice" };
    }
    const match = /^(?:prices|models)\[(\d+)\](?:\.(\w+))?:/.exec(value);
    if (!match) {
        return null;
    }
    const row = sent[Number(match[1])];
    if (row === undefined) {
        return null;
    }
    return { row, field: SERVER_FIELD[match[2] ?? "model"] ?? "model" };
}

export default { MAX_MODEL_CHARS, isAmount, newRow, unpricedRows, fromAnswer, sentRows, toPayload, isDirty, validate, refusedAt };
