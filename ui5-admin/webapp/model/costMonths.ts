import NumberFormat from "sap/ui/core/format/NumberFormat";
import DateFormat from "sap/ui/core/format/DateFormat";
import type { CostActual, CostEstimate, CostMonth, CostMonths } from "../service/costTypes";

/**
 * The "AI Core cost" panel of the Settings page as pure functions: the
 * answer of `GET cost/months` to table rows and to the one line that says
 * why the bill is missing. Texts come from the i18n bundle through `TextOf`,
 * so this module holds no wording; amounts go through `NumberFormat`.
 *
 * Rules: EUR per capacity unit only when there are units; the difference
 * (billed minus estimated) only when both figures exist, are in the same
 * currency and the estimate is fully priced (a partly priced one is a floor);
 * an amount above 0 that would show as 0.00 is "< € 0.01", as in the run
 * views; a status code is shown only when it has the form of an
 * identifier; a value that is not the promised number is a dash.
 */

/** The i18n lookup of the page (`BaseController#text`). */
export type TextOf = (key: string, args?: string[]) => string;

/** One row of the table, every cell a ready text. */
export interface CostMonthRow {
    /** `YYYY-MM` of the answer, for tests and keys. */
    key: string;
    /** The month in the locale, "November 2026". */
    month: string;
    /** The month is still open: its figures are not final. */
    open: boolean;
    /** The line under the month: "not final", or why the bill is missing. */
    note: string;
    billed: string;
    units: string;
    perUnit: string;
    estimated: string;
    difference: string;
}

/** The panel's line about the usage service; `null` when it answered. */
export interface ServiceLine {
    type: "Information" | "Warning";
    text: string;
}

/** The months the panel offers; 6 is the default (the route's too). */
export const MONTH_CHOICES = [3, 6, 12];
export const DEFAULT_MONTHS = 6;

const LABEL_RE = /^([0-9]{4})-([0-9]{2})$/;
const CODE_RE = /^[a-z][a-z0-9_]{0,39}$/;

/** The key of the month choice to a number of months; anything else is the default. */
export function monthsOf(key: unknown): number {
    const value = Number(key);
    return MONTH_CHOICES.indexOf(value) === -1 ? DEFAULT_MONTHS : value;
}

/** A status code as shown: itself when it looks like one, else "unknown". */
function code(value: unknown): string {
    return typeof value === "string" && CODE_RE.test(value) ? value : "unknown";
}

function num(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readActual(value: unknown): CostActual | null {
    const actual = value as CostActual | null;
    if (!actual || typeof actual !== "object" || num(actual.amount) === null
        || typeof actual.currency !== "string" || !actual.currency) {
        return null;
    }
    return actual;
}

function readEstimate(value: unknown): CostEstimate | null {
    const estimate = value as CostEstimate | null;
    if (!estimate || typeof estimate !== "object" || num(estimate.amount) === null
        || typeof estimate.currency !== "string" || !estimate.currency) {
        return null;
    }
    return estimate;
}

const money = NumberFormat.getFloatInstance({ minFractionDigits: 2, maxFractionDigits: 2, groupingEnabled: true });
const unitPrice = NumberFormat.getFloatInstance({ minFractionDigits: 2, maxFractionDigits: 4, groupingEnabled: true });
const unitCount = NumberFormat.getFloatInstance({ minFractionDigits: 0, maxFractionDigits: 2, groupingEnabled: true });
const monthName = DateFormat.getDateInstance({ format: "yMMMM", UTC: true });

/** Below this an amount would show as 0.00 although it is not zero (the run views' rule). */
const SMALLEST_SHOWN = 0.005;

function isSmall(amount: number): boolean {
    return amount > 0 && amount < SMALLEST_SHOWN;
}

/** An amount with its currency: "€ 12.34" for EUR, "USD 12.34" otherwise;
 * "< € 0.01" for an amount that two decimals would show as zero. */
function moneyText(text: TextOf, amount: number, currency: string, format = money): string {
    if (format === money && isSmall(amount)) {
        return text("cmLessThan", [moneyText(text, 0.01, currency)]);
    }
    const shown = format.format(amount);
    return currency === "EUR" ? text("cmMoney", [shown]) : text("cmMoneyOther", [currency, shown]);
}

function monthText(label: string): string {
    const match = LABEL_RE.exec(label);
    if (!match) {
        return label;
    }
    return monthName.format(new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1)));
}

/** The line under a month when the service answered but this month has no bill. */
function monthNote(text: TextOf, usage: string, status: string): string {
    if (usage !== "ok" || status === "ok") {
        return "";
    }
    if (status === "no_data") {
        return text("cmMonthNoData");
    }
    if (status === "unreadable") {
        return text("cmMonthUnreadable");
    }
    return text("cmMonthFailed", [code(status)]);
}

function differenceText(text: TextOf, actual: CostActual | null, estimate: CostEstimate | null): string {
    if (!actual || !estimate || actual.currency !== estimate.currency || estimate.complete !== true) {
        // A partly priced estimate is a floor: billed minus a floor says nothing.
        return text("cmNone");
    }
    // In cents, so 10.02 - 10.02 is zero and not a sign of a rounding error.
    const cents = Math.round(actual.amount * 100) - Math.round(estimate.amount * 100);
    const shown = moneyText(text, Math.abs(cents) / 100, actual.currency);
    if (cents > 0) {
        return text("cmDiffMore", [shown]);
    }
    return cents < 0 ? text("cmDiffLess", [shown]) : shown;
}

function row(text: TextOf, usage: string, month: CostMonth, index: number): CostMonthRow {
    const actual = readActual(month?.actual);
    const estimate = readEstimate(month?.estimated);
    const units = actual ? num(actual.capacity_units) : null;
    // SAP says whether its figure is final; without one, the newest month
    // (the current UTC month, first in the answer) is the open one.
    const open = actual ? actual.estimated === true : index === 0;
    const status = typeof month?.actual_status === "string" ? month.actual_status : "";
    const note = [open ? text("cmNotFinal") : "", monthNote(text, usage, status)].filter(Boolean).join(" · ");
    const none = text("cmNone");
    let estimated = none;
    if (estimate) {
        const shown = moneyText(text, estimate.amount, estimate.currency);
        // "< € 0.01" stands alone, as in the run views.
        estimated = isSmall(estimate.amount) ? shown : text("cmEstimate", [shown]);
        if (estimate.complete !== true) {
            estimated = text("cmPartly", [estimated]);
        }
    }
    return {
        key: String(month?.month ?? ""),
        month: monthText(String(month?.month ?? "")),
        open,
        note,
        billed: actual ? moneyText(text, actual.amount, actual.currency) : none,
        units: units === null ? none : unitCount.format(units),
        perUnit: actual && units !== null && units > 0
            ? moneyText(text, actual.amount / units, actual.currency, unitPrice) : none,
        estimated,
        difference: differenceText(text, actual, estimate)
    };
}

/** The table rows, in the answer's order (newest first). */
export function rows(answer: CostMonths | null | undefined, text: TextOf): CostMonthRow[] {
    if (!answer || !Array.isArray(answer.months)) {
        return [];
    }
    const usage = typeof answer.usage_service === "string" ? answer.usage_service : "";
    return answer.months.map((month, index) => row(text, usage, month, index));
}

/** Why the bill is missing for every month, or `null` when the service answered. */
export function serviceLine(answer: CostMonths | null | undefined, text: TextOf): ServiceLine | null {
    const usage = answer?.usage_service;
    if (!answer || usage === "ok") {
        return null;
    }
    switch (usage) {
        case "off":
            return { type: "Information", text: text("cmServiceOff") };
        case "no_subaccount":
            return { type: "Warning", text: text("cmServiceNoSubaccount") };
        case "usage_unavailable":
            return { type: "Warning", text: text("cmServiceUnavailable") };
        case "unauthorized":
            return { type: "Warning", text: text("cmServiceUnauthorized") };
        default:
            return { type: "Warning", text: text("cmServiceFailed", [code(usage)]) };
    }
}

export default { MONTH_CHOICES, DEFAULT_MONTHS, monthsOf, rows, serviceLine };
