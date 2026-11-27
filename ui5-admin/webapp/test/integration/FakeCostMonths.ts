import type { CostMonth, CostMonths } from "com/agent/admin/service/costTypes";

type Json = (body: unknown, status?: number, headers?: Record<string, string>) => Promise<Response>;

const MONTHS_RE = /^[0-9]{1,2}$/;

/**
 * The `/admin/api/cost/months` route of the fake backend, apart from
 * `FakeBackend` so neither file grows. Months are counted back from a fixed
 * November 2026 (the open month), newest first, as agents/cost_months.py
 * answers them.
 *
 * The fixture with the service `ok`: November is billed 12.00 EUR for 8
 * capacity units and still estimated by SAP, the app estimates 10.02;
 * October is billed 7.50 for 5 units against an estimate of 6.00 that is
 * partly priced; September has no AI Core row (`no_data`); older months are
 * billed 3.00 for 2 units with no estimate. `usageService` other than `ok`
 * gives every month `actual: null` with that state, as the server does.
 */
export default class FakeCostMonths {

    public usageService = "ok";
    /** The `months` of the last request (`null` before the first). */
    public lastMonths: number | null = null;

    public reset(): void {
        this.usageService = "ok";
        this.lastMonths = null;
    }

    private static label(index: number): string {
        const total = 2026 * 12 + 10 - index; // November 2026 is month index 10 of 2026
        const year = Math.floor(total / 12);
        const month = (total % 12) + 1;
        return `${year}-${String(month).padStart(2, "0")}`;
    }

    private month(index: number): CostMonth {
        const label = FakeCostMonths.label(index);
        const base: CostMonth = {
            month: label, actual: null, actual_status: this.usageService, estimated: null,
            by_source: { job: null, workflow_step: null, chat: null }, by_model: []
        };
        if (index === 0) {
            base.estimated = { amount: 10.02, currency: "EUR", complete: true };
        } else if (index === 1) {
            base.estimated = { amount: 6, currency: "EUR", complete: false };
        }
        if (this.usageService !== "ok") {
            return base;
        }
        if (index === 2) {
            return { ...base, actual_status: "no_data" };
        }
        const actual = index === 0 ? { amount: 12, currency: "EUR", capacity_units: 8, estimated: true }
            : index === 1 ? { amount: 7.5, currency: "EUR", capacity_units: 5, estimated: false }
                : { amount: 3, currency: "EUR", capacity_units: 2, estimated: false };
        return { ...base, actual, actual_status: "ok" };
    }

    public status(months: number): CostMonths {
        const list: CostMonth[] = [];
        for (let i = 0; i < months; i++) {
            list.push(this.month(i));
        }
        return { usage_service: this.usageService, currency: "EUR", months: list };
    }

    /** Answers `GET cost/months`, or `undefined` for any other path. */
    public handle(path: string, method: string, query: URLSearchParams, json: Json): Promise<Response> | undefined {
        if (path !== "cost/months") {
            return undefined;
        }
        if (method !== "GET") {
            return json({ detail: "Method Not Allowed" }, 405);
        }
        const raw = query.get("months") ?? "6";
        const months = MONTHS_RE.test(raw) ? Number(raw) : 0;
        if (months < 1 || months > 12) {
            return json({ detail: [{ loc: ["query", "months"], msg: "Input should be less than or equal to 12",
                type: "less_than_equal" }] }, 422);
        }
        this.lastMonths = months;
        return json(this.status(months));
    }
}
