import JSONModel from "sap/ui/model/json/JSONModel";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { InvisibleMessageMode, ValueState } from "sap/ui/core/library";
import BaseController from "./BaseController";
import DestinationField from "./DestinationField";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import formatter from "../model/formatter";
import inaQueries, { DESTINATION_TEXTS, type InaField, type InaQueryForm } from "../model/inaQueries";
import odataCatalog from "../model/odataCatalog";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type Input from "sap/m/Input";
import type Page from "sap/m/Page";
import type SegmentedButton from "sap/m/SegmentedButton";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { InaDefinition, InaDescribeResult, InaQuery, InaTestResult } from "../service/inaTypes";

const ROUTE = "inaQueryDetail";

/** The fields of the form that a refusal can name, in its order, with their ids. */
const FORM_FIELDS: readonly { field: InaField; id: string }[] = [
    { field: "title", id: "inaDetailTitle" },
    { field: "purpose", id: "inaDetailPurpose" },
    { field: "not_for", id: "inaDetailNotFor" },
    { field: "destination", id: "inaDetailDestination" },
    { field: "query", id: "inaDetailQuery" },
    { field: "package", id: "inaDetailPackage" }
];

/** The words of a variable's type (`InaVariableType`). */
const VARIABLE_TYPES: Record<string, string> = {
    single: "inaVariableSingle", interval: "inaVariableInterval", multiple: "inaVariableMultiple"
};

type ResultType = "Success" | "Information" | "Warning" | "Error";

/** The model of the page (`qry`). */
interface DetailState {
    loaded: boolean;
    /** A stored query is shown (false while loading and after a failed load). */
    exists: boolean;
    /** A save, describe, test or delete is on its way. */
    busy: boolean;
    /** A question is open: nothing is saved or sent until it is answered. */
    asking: boolean;
    title: string;
    /** What the form holds. */
    data: InaQueryForm;
    /** The query as it was loaded: the tags, the dirty check, the identity question. */
    stored: InaQueryForm;
    /** What a save sends back as `expected_updated_at`. */
    updated_at: string | null;
    usedBy: string[];
    definition: InaDefinition;
    describedAt: string | null;
    describedText: string;
    errors: Partial<Record<InaField, string>>;
    saveError: string;
    loadFailed: boolean;
    loadError: string;
    changedElsewhere: boolean;
    deletedElsewhere: boolean;
    /** The strip under the toolbar: what Test answered, or what Describe or Test has to say. */
    test: { type: ResultType; text: string } | null;
}

/**
 * One analytical query (InA): its identity (destination, "Runs as"), the
 * texts an agent reads, and what `Describe` read from SAP, read-only. Test
 * reads the first measure once through the saved query.
 *
 * Mirrors the OData service page (`ODataServiceDetail`): a save is a GET
 * and a PUT with `expected_updated_at`, so a query saved elsewhere is never
 * overwritten; a change of whom or what the agents of a query in use call
 * is asked about first.
 *
 * @namespace com.agent.admin.controller
 */
export default class InaQueryDetail extends BaseController {

    public formatDestinationState = DestinationField.formatState;
    public formatDestinationStateText = DestinationField.formatStateText;

    /** The name in the route. */
    private queryName?: string;
    /** Counts the loads, so that a late answer for another query is dropped. */
    private loadCount = 0;
    private destinationField!: DestinationField;

    public onInit(): void {
        this.setModel(new JSONModel(InaQueryDetail.blank()), "qry");
        // `items` and `hint` of the destination field; `page`: what the list
        // knows about the name in it.
        const dest = new JSONModel({ items: [], hint: "", page: { notice: "", noticeText: "" } });
        this.setModel(dest, "dest");
        this.destinationField = new DestinationField(
            this.getView()!, dest, () => this.getAdminService(),
            (key, args) => this.text(DESTINATION_TEXTS[key] ?? key, args)
        );
        this.destinationField.attach(this.destinationInput(), {
            path: "/page",
            userContext: () => this.data().user_context === true,
            judged: () => this.qry().getProperty("/exists") === true,
            stored: () => this.data().destination,
            hasError: () => !!this.qry().getProperty("/errors/destination"),
            edited: () => this.clearError("destination")
        });
        this.getRouter().getRoute(ROUTE)?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const name = (event.getParameter("arguments") as { name: string }).name;
            void this.destinationField.load();
            void this.load(name);
        });
    }

    private static blank(): DetailState {
        return {
            loaded: false, exists: false, busy: false, asking: false, title: "",
            data: inaQueries.emptyForm(), stored: inaQueries.emptyForm(), updated_at: null, usedBy: [],
            definition: { dimensions: [], measures: [], variables: [] }, describedAt: null, describedText: "",
            errors: {}, saveError: "", loadFailed: false, loadError: "", changedElsewhere: false,
            deletedElsewhere: false, test: null
        };
    }

    private qry(): JSONModel {
        return this.getModel("qry") as JSONModel;
    }

    private data(): InaQueryForm {
        return this.qry().getProperty("/data") as InaQueryForm;
    }

    private destinationInput(): Input {
        return this.byId("inaDetailDestination") as Input;
    }

    private isDirty(): boolean {
        return !inaQueries.sameForm(this.data(), this.qry().getProperty("/stored") as InaQueryForm);
    }

    private setBusy(busy: boolean): void {
        this.qry().setProperty("/busy", busy);
    }

    // --- loading ------------------------------------------------------------

    /**
     * Shows the query `name`. `keep`: the page stays as it is until the
     * answer is there (a read again after Describe), instead of emptying.
     * A failure is said on the page with a retry; a lapsed session or a
     * missing scope additionally gets the central dialog.
     */
    private async load(name: string, keep = false): Promise<void> {
        const count = ++this.loadCount;
        this.queryName = name;
        if (!keep) {
            this.qry().setData(InaQueryDetail.blank());
        }
        let query: InaQuery;
        try {
            query = await this.withBusy(() => this.getAdminService().getInaQuery(name));
        } catch (error) {
            if (count !== this.loadCount) {
                return;
            }
            const reason = ErrorHandler.messageFor(error, "");
            this.qry().setData({
                ...InaQueryDetail.blank(), loadFailed: true,
                loadError: reason ? this.text("inaDetailLoadFailedReason", [name, reason])
                    : this.text("inaDetailLoadFailed", [name])
            });
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
            }
            return;
        }
        if (count === this.loadCount) {
            this.show(query);
        }
    }

    /** Puts `query` on the page as it is stored. */
    private show(query: InaQuery): void {
        const form = inaQueries.formOf(query);
        const described = inaQueries.describedText(query.definition);
        this.qry().setData({
            ...InaQueryDetail.blank(), loaded: true, exists: true, title: query.title,
            data: form, stored: { ...form }, updated_at: query.updated_at ?? null,
            usedBy: inaQueries.usedByNames(query.used_by),
            definition: query.definition ?? { dimensions: [], measures: [], variables: [] },
            describedAt: query.definition ? query.metadata_fetched_at : null,
            describedText: this.text(described.key, described.args)
        });
        this.destinationField.check(this.destinationInput());
    }

    public onRetry(): void {
        if (this.queryName) {
            void this.load(this.queryName);
        }
    }

    /** Reads the query again after it was changed elsewhere; the input is lost by it. */
    public onReload(): void {
        if (this.queryName && this.qry().getProperty("/busy") !== true) {
            void this.load(this.queryName, true);
        }
    }

    public onBack(): void {
        this.getRouter().navTo("inaQueries");
    }

    // --- formatters ---------------------------------------------------------

    public formatRunsAs(userContext: boolean | undefined): string {
        return this.text(userContext ? "odataRunsAsUser" : "odataRunsAsTechnical");
    }

    public formatErrorState(message: string | undefined): ValueState {
        return message ? ValueState.Error : ValueState.None;
    }

    public formatUsedBy(usedBy: string[] | undefined): string {
        return usedBy?.length ? usedBy.join(", ") : this.text("inaUsedByNone");
    }

    public formatDescribedAt(describedAt: string | null | undefined): string {
        return describedAt ? this.text("inaDescribedAtStamp", [formatter.timestamp(describedAt)]) : "";
    }

    /** A type the page does not know is shown as SAP named it. */
    public formatVariableType(type: string | undefined): string {
        const key = type ? VARIABLE_TYPES[type] : undefined;
        return key ? this.text(key) : String(type ?? "");
    }

    // --- editing ------------------------------------------------------------

    private clearError(field: string): void {
        this.qry().setProperty(`/errors/${field}`, "");
        this.qry().setProperty("/saveError", "");
        this.clearResult();
    }

    /** Typing in a field takes its error away, and what Test said about the query before. */
    public onFieldEdit(event: Event): void {
        this.clearError(String((event.getSource() as Control).data("field")));
    }

    public onEdited(): void {
        this.clearResult();
    }

    public onRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.qry().setProperty("/data/user_context", key === "user");
        this.clearResult();
        // Said even when unchanged: the admin wants to know what holds now.
        this.destinationField.check(this.destinationInput(), true, true);
    }

    // --- save ---------------------------------------------------------------

    /**
     * Checks the form, reads the query again (a save is refused over a
     * version that moved), asks when the identity of a query in use
     * changes, and saves.
     */
    public async onSave(): Promise<void> {
        const model = this.qry();
        if (model.getProperty("/busy") === true || model.getProperty("/asking") === true
            || model.getProperty("/exists") !== true) {
            return;
        }
        // A tap on Save that does not move the focus: what the field shows is stored.
        this.destinationField.storeShown(this.destinationInput());
        model.setProperty("/saveError", "");
        const problems = inaQueries.validateInput(this.data());
        const errors: Partial<Record<InaField, string>> = {};
        (Object.keys(problems) as InaField[]).forEach((field) => {
            errors[field] = this.text(problems[field] as string);
        });
        model.setProperty("/errors", errors);
        if (this.focusFirstError(errors)) {
            return;
        }
        if (!model.getProperty("/updated_at")) {
            // Without the version the form was loaded from, a save would be a blind overwrite.
            model.setProperty("/saveError", this.text("inaCannotVerify"));
            return;
        }
        const fresh = await this.freshQuery();
        if (fresh) {
            // Against the query as it is stored NOW: that is what the save replaces.
            this.askAndSave(this.identityQuestion(inaQueries.formOf(fresh), inaQueries.usedByNames(fresh.used_by)));
        }
    }

    private focusFirstError(errors: Partial<Record<InaField, string>>): boolean {
        const first = FORM_FIELDS.filter((entry) => errors[entry.field])[0];
        if (first) {
            (this.byId(first.id) as Input).focus();
        }
        return !!first;
    }

    /** The query as stored, when it is still the version the form was
     *  loaded from; otherwise the page says what happened. */
    private async freshQuery(): Promise<InaQuery | undefined> {
        const model = this.qry();
        const name = this.queryName as string;
        this.setBusy(true);
        let fresh: InaQuery;
        try {
            fresh = await this.withBusy(() => this.getAdminService().getInaQuery(name));
        } catch (error) {
            if (error instanceof AdminError && error.status === 404) {
                model.setProperty("/deletedElsewhere", true);
            } else {
                const kind = ErrorHandler.classify(error);
                if (kind === "session" || kind === "forbidden") {
                    ErrorHandler.handle(error);
                }
                model.setProperty("/saveError", this.text("inaSaveNotRead", [ErrorHandler.messageFor(error, "")]));
            }
            return undefined;
        } finally {
            this.setBusy(false);
        }
        if (name !== this.queryName) {
            return undefined;
        }
        if (!fresh.updated_at) {
            model.setProperty("/saveError", this.text("inaCannotVerify"));
            return undefined;
        }
        if (fresh.updated_at !== model.getProperty("/updated_at")) {
            model.setProperty("/changedElsewhere", true);
            return undefined;
        }
        model.setProperty("/changedElsewhere", false);
        model.setProperty("/usedBy", inaQueries.usedByNames(fresh.used_by));
        return fresh;
    }

    /**
     * What to ask before the save, or undefined: only when agents use the
     * query and the save changes "Runs as", the destination or the InA
     * name, in the words of the OData service page.
     */
    private identityQuestion(stored: InaQueryForm, usedBy: string[]): { title: string; text: string } | undefined {
        const change = inaQueries.identityChange(stored, this.data());
        if (!usedBy.length || (!change.runsAs && !change.destination && !change.query)) {
            return undefined;
        }
        const parts = [usedBy.length === 1
            ? this.text("inaIdentityAgentsOne", [usedBy[0]])
            : this.text("inaIdentityAgentsMany", [usedBy.join(", ")])];
        if (change.runsAs) {
            // No arguments: these two texts carry an apostrophe.
            parts.push(this.text(change.runsAs === "technical" ? "inaIdentityToTechnical" : "inaIdentityToUser"));
        }
        if (change.destination) {
            parts.push(this.text("inaIdentityDestination", [change.destination.from, change.destination.to]));
        }
        if (change.query) {
            parts.push(this.text("inaIdentityQuery", [change.query.from, change.query.to]));
        }
        return { title: this.text("inaIdentityConfirmTitle"), text: parts.join("\n\n") };
    }

    /** Saves, after asking `question` when there is one (Cancel is the default). */
    private askAndSave(question: { title: string; text: string } | undefined): void {
        const model = this.qry();
        if (!question) {
            void this.save();
            return;
        }
        const save = this.text("save");
        model.setProperty("/asking", true);
        MessageBox.warning(question.text, {
            title: question.title,
            actions: [save, MessageBox.Action.CANCEL],
            emphasizedAction: save,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                model.setProperty("/asking", false);
                if (action === save) {
                    void this.save();
                }
            }
        });
    }

    /** The form and the version it was loaded from; never the description,
     *  which the server keeps when it is left out. */
    private async save(): Promise<void> {
        const model = this.qry();
        const name = this.queryName as string;
        const body = inaQueries.toUpdate(this.data(), model.getProperty("/updated_at") as string);
        this.setBusy(true);
        let saved: InaQuery;
        try {
            saved = await this.withBusy(() => this.getAdminService().updateInaQuery(name, body));
        } catch (error) {
            this.setBusy(false);
            this.showRefusal(error);
            return;
        }
        this.setBusy(false);
        this.show(saved);
        if (!this.warnIfNotLive(saved, "inaReloadFailedSaved")) {
            MessageToast.show(this.text("inaSaved"));
        }
    }

    /**
     * Why the server did not save. A 422 goes to the fields it names, the
     * rest above the form; a 404 and a 409 (changed elsewhere) get their
     * strip, and the input stays.
     */
    private showRefusal(error: unknown): void {
        const model = this.qry();
        if (error instanceof AdminError && error.status === 422) {
            const byLoc = odataCatalog.serverErrors(error.detail);
            const errors: Record<string, string> = {};
            let unplaced = Object.keys(byLoc).length === 0;
            Object.keys(byLoc).forEach((loc) => {
                if (FORM_FIELDS.some((entry) => entry.field === loc)) {
                    errors[loc] = byLoc[loc];
                } else {
                    unplaced = true;
                }
            });
            model.setProperty("/errors", errors);
            model.setProperty("/saveError", unplaced
                ? (error.detail ? this.text("inaSaveRefused", [error.detail]) : this.text("inaSaveFailed")) : "");
            this.focusFirstError(errors);
            return;
        }
        if (error instanceof AdminError && error.status === 404) {
            model.setProperty("/deletedElsewhere", true);
            return;
        }
        if (error instanceof AdminError && error.status === 409) {
            model.setProperty("/changedElsewhere", true);
            model.setProperty("/saveError", error.detail
                ? this.text("inaSaveRefused", [error.detail]) : this.text("inaSaveFailed"));
            return;
        }
        ErrorHandler.handle(error, this.text("inaSaveFailed"));
    }

    // --- describe and test --------------------------------------------------

    /** Describe and Test use the saved query: with unsaved input they say so. */
    private canCall(): boolean {
        const model = this.qry();
        if (model.getProperty("/busy") === true || model.getProperty("/asking") === true
            || model.getProperty("/exists") !== true || !this.queryName) {
            return false;
        }
        if (this.isDirty()) {
            this.showResult("Warning", this.text("inaSaveFirst"));
            return false;
        }
        return true;
    }

    /**
     * Reads the description from SAP and stores it, then reads the query
     * again: a Describe moves its `updated_at`. A 409 is a save in between
     * (nothing was stored); every other refusal is the code's text from
     * the server, through the central handler.
     */
    public async onDescribe(): Promise<void> {
        if (!this.canCall()) {
            return;
        }
        const name = this.queryName as string;
        this.clearResult();
        this.setBusy(true);
        let result: InaDescribeResult;
        try {
            result = await this.withBusy(() => this.getAdminService().describeInaQuery(name));
        } catch (error) {
            this.setBusy(false);
            if (error instanceof AdminError && error.status === 404) {
                this.qry().setProperty("/deletedElsewhere", true);
            } else if (error instanceof AdminError && error.status === 409) {
                await this.load(name, true);
                this.showResult("Warning", this.text("inaDescribeMoved"));
            } else {
                ErrorHandler.handle(error, this.text("inaDescribeFailed"));
            }
            return;
        }
        this.setBusy(false);
        await this.load(name, true);
        if (result.truncated === true) {
            this.showResult("Warning", this.text("inaDescribeTruncated"));
        }
        if (!this.warnIfNotLive(result, "inaReloadFailedDescribed")) {
            MessageToast.show(this.text("inaDescribedToast", [result.dimensions, result.measures, result.variables]));
        }
    }

    /** One read of the first measure: outcome, status, duration and the
     *  column names in the strip. A refusal is the outcome too, never a
     *  dialog, except for a lapsed session or a missing scope. */
    public async onTest(): Promise<void> {
        if (!this.canCall()) {
            return;
        }
        const name = this.queryName as string;
        this.setBusy(true);
        let result: InaTestResult;
        try {
            result = await this.withBusy(() => this.getAdminService().testInaQuery(name));
        } catch (error) {
            this.setBusy(false);
            this.showTestRefusal(error);
            return;
        }
        this.setBusy(false);
        if (name !== this.queryName) {
            return;
        }
        const status = typeof result.status === "number" ? String(result.status) : "-";
        const duration = String(result.duration_ms ?? 0);
        if (result.ok === true) {
            const columns = (Array.isArray(result.columns) ? result.columns : []).join(", ");
            this.showResult("Success", this.text("inaTestOk", [status, duration, columns || this.text("inaTestNoColumns")]));
        } else {
            this.showResult("Error", this.text("inaTestFailed", [status, duration]));
        }
    }

    private showTestRefusal(error: unknown): void {
        if (error instanceof AdminError && error.status === 404) {
            this.qry().setProperty("/deletedElsewhere", true);
            return;
        }
        const kind = ErrorHandler.classify(error);
        if (kind === "session" || kind === "forbidden") {
            ErrorHandler.handle(error);
            return;
        }
        const reason = ErrorHandler.messageFor(error, "");
        // By the code of `X-Ina-Error`: a mandatory variable is how the
        // query is, not a failure (Describe is its check).
        const code = error instanceof AdminError ? error.code : "";
        this.showResult(code === "variable_required" ? "Information" : "Error",
            reason ? this.text("inaTestNotRun", [reason]) : this.text("inaTestNotRunPlain"));
    }

    /** Shows the strip under the toolbar, brings it into view and says it. */
    private showResult(type: ResultType, text: string): void {
        this.qry().setProperty("/test", { type, text });
        (this.byId("inaQueryDetailPage") as Page | undefined)?.scrollTo(0);
        InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
    }

    private clearResult(): void {
        if (this.qry().getProperty("/test")) {
            this.qry().setProperty("/test", null);
        }
    }

    // --- delete -------------------------------------------------------------

    /** Asks, naming title and technical name, then deletes and goes back to the list. */
    public onDelete(): void {
        const model = this.qry();
        if (model.getProperty("/busy") === true || model.getProperty("/exists") !== true || !this.queryName) {
            return;
        }
        const stored = model.getProperty("/stored") as InaQueryForm;
        const remove = this.text("delete");
        MessageBox.warning(this.text("inaDeleteConfirm", [stored.title, stored.name]), {
            title: remove,
            actions: [remove, MessageBox.Action.CANCEL],
            emphasizedAction: remove,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === remove) {
                    void this.deleteQuery(stored.name, stored.title);
                }
            }
        });
    }

    private async deleteQuery(name: string, title: string): Promise<void> {
        this.setBusy(true);
        let answer;
        try {
            answer = await this.withBusy(() => this.getAdminService().deleteInaQuery(name));
        } catch (error) {
            this.setBusy(false);
            if (error instanceof AdminError && ErrorHandler.classify(error) === "conflict") {
                // Still used: the server's text names the agents to detach it
                // from, so it stays on screen as it came.
                MessageBox.error(error.detail || this.text("inaDeleteFailed", [title]));
            } else {
                ErrorHandler.handle(error, this.text("inaDeleteFailed", [title]));
            }
            return;
        }
        this.setBusy(false);
        if (!this.warnIfNotLive(answer, "inaReloadFailedDeleted")) {
            MessageToast.show(this.text("inaDeleted"));
        }
        this.getRouter().navTo("inaQueries");
    }
}
