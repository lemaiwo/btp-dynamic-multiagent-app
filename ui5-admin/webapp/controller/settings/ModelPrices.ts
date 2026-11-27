import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import type Event from "sap/ui/base/Event";
import type UI5Element from "sap/ui/core/Element";
import type { Model$PropertyChangeEvent } from "sap/ui/model/Model";
import prices, { type PriceField, type PriceRowForm, type PricesForm } from "../../model/modelPrices";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type AdminService from "../../service/AdminService";
import type { ModelPrices as PricesAnswer } from "../../service/costTypes";

/** What the panel needs of the Settings page. */
export interface ModelPricesHost {
    service(): AdminService;
    text(key: string, args?: string[]): string;
}

/** A row as the `prices` model holds it: the form plus the value state of each field. */
interface RowView extends PriceRowForm {
    modelState: string;
    modelStateText: string;
    inputState: string;
    inputStateText: string;
    cachedState: string;
    cachedStateText: string;
    outputState: string;
    outputStateText: string;
}

const FIELDS: PriceField[] = ["model", "input", "cached", "output"];

/**
 * The "Model prices" panel of the Settings page: the price of one SAP AI
 * Core capacity unit and the rate of each model in capacity units per 1,000
 * tokens. The logic is the pure `model/modelPrices.ts`; this class makes the
 * calls and keeps the `prices` model the panel binds to.
 *
 * The models the usage ledger has seen without a rate are rows of the table
 * already, marked "No price": typing their rates prices them. Save sends the
 * whole table with the stamp it was read with; a 409 (somebody else saved)
 * reads it again and says so. Rates stay text from the input to the body.
 */
export default class ModelPrices {

    /** The panel's state, bound as `prices`. */
    public readonly model = new JSONModel(ModelPrices.emptyState());

    /** The form of the last answer, to tell unsaved changes. */
    private stored: PricesForm = { cuPrice: "", rows: [], stamp: null };

    public constructor(private readonly host: ModelPricesHost) {
        // An edit clears the mark of that field and recomputes "unsaved".
        this.model.attachPropertyChange((event: Model$PropertyChangeEvent) => {
            const context = event.getParameter("context");
            const path = String(context ? `${context.getPath()}/${event.getParameter("path")}` : event.getParameter("path"));
            const match = /^\/form\/rows\/(\d+)\/(model|input|cached|output)$/.exec(path);
            if (match) {
                this.clearMark(Number(match[1]), match[2] as PriceField);
            } else if (path === "/form/cuPrice") {
                this.clearMark(null, "cuPrice");
            }
            this.updateDirty();
        });
    }

    private static emptyState(): Record<string, unknown> {
        return {
            loaded: false, loading: false, busy: false, dirty: false, loadError: "", currency: "EUR",
            cuState: "None", cuStateText: "", form: { cuPrice: "", rows: [], stamp: null }
        };
    }

    private static view(row: PriceRowForm): RowView {
        return {
            ...row,
            modelState: "None", modelStateText: "", inputState: "None", inputStateText: "",
            cachedState: "None", cachedStateText: "", outputState: "None", outputStateText: ""
        };
    }

    /** The form as typed, without the value states. */
    private form(): PricesForm {
        const form = this.model.getProperty("/form") as { cuPrice: string; rows: RowView[]; stamp: string | null };
        return {
            cuPrice: form.cuPrice,
            stamp: form.stamp,
            rows: form.rows.map((row) => ({
                model: row.model, input: row.input, cached: row.cached, output: row.output, unpriced: row.unpriced
            }))
        };
    }

    /** The page is shown: read the table. */
    public async load(): Promise<void> {
        this.model.setProperty("/loading", true);
        try {
            this.apply(await this.host.service().getModelPrices());
            this.model.setProperty("/loadError", "");
        } catch (error) {
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
                return;
            }
            // In the panel, not in a box: the page's other settings work without it.
            const reason = ErrorHandler.messageFor(error, "");
            this.model.setProperty("/loadError", reason
                ? this.host.text("mpcLoadFailed", [reason]) : this.host.text("mpcLoadFailedPlain"));
        } finally {
            this.model.setProperty("/loading", false);
        }
    }

    private apply(answer: PricesAnswer): void {
        this.stored = prices.fromAnswer(answer);
        const form = prices.fromAnswer(answer);
        this.model.setProperty("/currency", typeof answer?.currency === "string" ? answer.currency : "EUR");
        this.model.setProperty("/cuState", "None");
        this.model.setProperty("/cuStateText", "");
        this.model.setProperty("/form", { cuPrice: form.cuPrice, stamp: form.stamp, rows: form.rows.map(ModelPrices.view) });
        this.model.setProperty("/dirty", false);
        this.model.setProperty("/loaded", true);
    }

    private updateDirty(): void {
        this.model.setProperty("/dirty", prices.isDirty(this.form(), this.stored));
    }

    private mark(row: number | null, field: PriceField | "cuPrice", state: string, text: string): void {
        if (row === null) {
            this.model.setProperty("/cuState", state);
            this.model.setProperty("/cuStateText", text);
            return;
        }
        this.model.setProperty(`/form/rows/${row}/${field}State`, state);
        this.model.setProperty(`/form/rows/${row}/${field}StateText`, text);
    }

    private clearMark(row: number | null, field: PriceField | "cuPrice"): void {
        this.mark(row, field, "None", "");
    }

    private clearMarks(): void {
        this.clearMark(null, "cuPrice");
        const rows = this.model.getProperty("/form/rows") as RowView[];
        rows.forEach((_row, index) => FIELDS.forEach((field) => this.clearMark(index, field)));
    }

    /** "Add model": an empty row at the end. */
    public addRow(): void {
        const rows = (this.model.getProperty("/form/rows") as RowView[]).slice();
        rows.push(ModelPrices.view(prices.newRow()));
        this.model.setProperty("/form/rows", rows);
        this.updateDirty();
    }

    /** The delete button of a row: the row goes (saved only with Save). */
    public removeRow(event: Event): void {
        const source = event.getSource() as UI5Element;
        const path = source?.getBindingContext("prices")?.getPath() ?? "";
        const match = /^\/form\/rows\/(\d+)$/.exec(path);
        if (!match) {
            return;
        }
        const rows = (this.model.getProperty("/form/rows") as RowView[]).slice();
        rows.splice(Number(match[1]), 1);
        this.model.setProperty("/form/rows", rows);
        this.updateDirty();
    }

    /**
     * Save: the checks of what is typed first (each problem marked at its
     * field, one box), then the PUT with the stamp. A 409 reads the table
     * again and says so; a 422 marks the field it names.
     */
    public async save(): Promise<void> {
        if (this.model.getProperty("/busy") === true || this.model.getProperty("/loaded") !== true) {
            return;
        }
        this.clearMarks();
        const form = this.form();
        const problems = prices.validate(form);
        if (problems.length) {
            problems.forEach((problem) => this.mark(problem.row, problem.field, "Error", this.host.text(problem.text.key)));
            MessageBox.error(this.host.text("mpcInvalid"));
            return;
        }
        const sent = prices.sentRows(form);
        this.model.setProperty("/busy", true);
        try {
            const answer = await this.host.service().setModelPrices(prices.toPayload(form));
            this.apply(answer);
            MessageToast.show(this.host.text("mpcSaved"));
        } catch (error) {
            this.refused(error, sent);
        } finally {
            this.model.setProperty("/busy", false);
        }
    }

    private refused(error: unknown, sent: number[]): void {
        if (error instanceof AdminError && error.status === 409) {
            MessageBox.warning(this.host.text("mpcStale"));
            void this.load();
            return;
        }
        if (error instanceof AdminError && error.status === 422) {
            const target = prices.refusedAt(error.detail, sent);
            if (target) {
                this.mark(target.row, target.field, "Error", error.detail);
            }
            MessageBox.error(this.host.text("mpcRefused", [error.detail]));
            return;
        }
        ErrorHandler.handle(error, this.host.text("mpcSaveFailed"));
    }
}
