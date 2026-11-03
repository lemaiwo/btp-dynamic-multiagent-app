import MessageToast from "sap/m/MessageToast";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type AdminService from "../../service/AdminService";
import type { Translate } from "../../model/odataDestinations";
import type { ReloadOutcome } from "../../service/types";
import type { InaDescribeResult, InaTestResult } from "../../service/inaTypes";

import type { ResultType } from "../../model/inaDetail";

/** What `DescribeTest` needs of the page it works for. */
export interface DescribeTestPage {
    text: Translate;
    service(): AdminService;
    /** Runs `work` with the page busy (indicator and buttons). */
    busy<T>(work: () => Promise<T>): Promise<T>;
    /** Reads the query again and shows it, without emptying the page first. */
    reload(name: string): Promise<void>;
    /** `BaseController.warnIfNotLive`. */
    warnIfNotLive(outcome: ReloadOutcome | undefined, leadKey: string): boolean;
    /** The strip under the toolbar. */
    showResult(type: ResultType, text: string): void;
    /** The query is gone (404). */
    deletedElsewhere(): void;
    /** The name of the query the page shows now. */
    shownName(): string | undefined;
}

/**
 * Describe and Test of one analytical query (`POST ina/queries/{name}/describe`
 * and `/test`), apart from the detail page so that page stays a page. A
 * plain class: the page decides when a call may be made (saved changes,
 * nothing else running), this one makes it and says how it went.
 *
 * What both say beside their answer: `busy` (429, every call slot taken)
 * is a Warning with a fixed text; a lapsed session or a missing scope gets
 * the central dialog; a query that is gone gets the page's strip.
 */
export default class DescribeTest {

    public constructor(private readonly page: DescribeTestPage) {}

    /**
     * Reads the description from SAP and stores it, then reads the query
     * again: a Describe moves its `updated_at`. A 409 is a save in between
     * (nothing was stored); any other refusal is the code's text from the
     * server, through the central handler.
     */
    public async describe(name: string): Promise<void> {
        const page = this.page;
        let result: InaDescribeResult;
        try {
            result = await page.busy(() => page.service().describeInaQuery(name));
        } catch (error) {
            if (this.saidBesides(error)) {
                return;
            }
            if (error instanceof AdminError && error.status === 409) {
                await page.reload(name);
                page.showResult("Warning", page.text("inaDescribeMoved"));
            } else {
                ErrorHandler.handle(error, page.text("inaDescribeFailed"));
            }
            return;
        }
        await page.reload(name);
        if (result.truncated === true) {
            page.showResult("Warning", page.text("inaDescribeTruncated"));
        }
        if (!page.warnIfNotLive(result, "inaReloadFailedDescribed")) {
            MessageToast.show(page.text("inaDescribedToast", [result.dimensions, result.measures, result.variables]));
        }
    }

    /** One read of the first measure: outcome, status, duration and the
     *  column names in the strip, never a value. A refusal is the outcome
     *  too, never a dialog. */
    public async test(name: string): Promise<void> {
        const page = this.page;
        let result: InaTestResult;
        try {
            result = await page.busy(() => page.service().testInaQuery(name));
        } catch (error) {
            if (this.saidBesides(error)) {
                return;
            }
            const reason = ErrorHandler.messageFor(error, "");
            // By the code of `X-Ina-Error`: a mandatory variable is how the
            // query is, not a failure (Describe is its check).
            const code = error instanceof AdminError ? error.code : "";
            page.showResult(code === "variable_required" ? "Information" : "Error",
                reason ? page.text("inaTestNotRun", [reason]) : page.text("inaTestNotRunPlain"));
            return;
        }
        if (name !== page.shownName()) {
            return;
        }
        const status = typeof result.status === "number" ? String(result.status) : "-";
        const duration = String(result.duration_ms ?? 0);
        if (result.ok === true) {
            const columns = (Array.isArray(result.columns) ? result.columns : []).join(", ");
            page.showResult("Success", page.text("inaTestOk", [status, duration, columns || page.text("inaTestNoColumns")]));
        } else {
            page.showResult("Error", page.text("inaTestFailed", [status, duration]));
        }
    }

    /** What Describe and Test say alike about a refusal; true when said. */
    private saidBesides(error: unknown): boolean {
        const page = this.page;
        if (error instanceof AdminError && error.status === 404) {
            page.deletedElsewhere();
            return true;
        }
        if (error instanceof AdminError && error.code === "busy") {
            page.showResult("Warning", page.text("inaBusy"));
            return true;
        }
        const kind = ErrorHandler.classify(error);
        if (kind === "session" || kind === "forbidden") {
            ErrorHandler.handle(error);
            return true;
        }
        return false;
    }
}
