import JSONModel from "sap/ui/model/json/JSONModel";
import costMonths, { DEFAULT_MONTHS } from "../../model/costMonths";
import ErrorHandler from "../../service/ErrorHandler";
import type AdminService from "../../service/AdminService";

/** What the panel needs of the Settings page. */
export interface CostMonthsHost {
    service(): AdminService;
    text(key: string, args?: string[]): string;
}

/**
 * The "AI Core cost" panel of the Settings page: per month what SAP billed
 * for AI Core in this subaccount, the capacity units, the EUR per unit, this
 * app's estimate and the difference. The logic is the pure
 * `model/costMonths.ts`; this class makes the call and keeps the `cost`
 * model the panel binds to.
 *
 * Read only. A failed read is a strip in the panel with a retry link, never
 * a box (only an expired session or a missing role goes to `ErrorHandler`).
 * An answer that arrives after a newer request was sent is dropped.
 */
export default class CostMonths {

    /** The panel's state, bound as `cost`. */
    public readonly model = new JSONModel({
        loading: false, loaded: false, loadError: "", months: String(DEFAULT_MONTHS),
        line: "", lineType: "Information", rows: []
    });

    private sequence = 0;

    public constructor(private readonly host: CostMonthsHost) {}

    /** Read the months currently chosen. */
    public async load(): Promise<void> {
        const months = costMonths.monthsOf(this.model.getProperty("/months"));
        const mine = ++this.sequence;
        this.model.setProperty("/loading", true);
        try {
            const answer = await this.host.service().getCostMonths(months);
            if (mine !== this.sequence) {
                return;
            }
            const text = (key: string, args?: string[]) => this.host.text(key, args);
            const line = costMonths.serviceLine(answer, text);
            this.model.setProperty("/rows", costMonths.rows(answer, text));
            this.model.setProperty("/line", line ? line.text : "");
            this.model.setProperty("/lineType", line ? line.type : "Information");
            this.model.setProperty("/loadError", "");
            this.model.setProperty("/loaded", true);
        } catch (error) {
            if (mine !== this.sequence) {
                return;
            }
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
                return;
            }
            const reason = ErrorHandler.messageFor(error, "");
            this.model.setProperty("/loadError", reason
                ? this.host.text("cmLoadFailed", [reason]) : this.host.text("cmLoadFailedPlain"));
        } finally {
            if (mine === this.sequence) {
                this.model.setProperty("/loading", false);
            }
        }
    }

    /** Another number of months was chosen: read again. */
    public choose(key: string): void {
        this.model.setProperty("/months", String(costMonths.monthsOf(key)));
        void this.load();
    }
}
