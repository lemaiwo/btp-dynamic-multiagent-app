import JSONModel from "sap/ui/model/json/JSONModel";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageToast from "sap/m/MessageToast";
import { ValueState } from "sap/ui/core/library";
import BaseController from "./BaseController";
import DestinationField from "./DestinationField";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import formatter from "../model/formatter";
import inaQueries, {
    DESTINATION_TEXTS, type InaField, type InaQueryForm, type InaQueryRow
} from "../model/inaQueries";
import odataCatalog from "../model/odataCatalog";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Dialog from "sap/m/Dialog";
import type Input from "sap/m/Input";
import type SearchField from "sap/m/SearchField";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Table from "sap/m/Table";
import type ListBinding from "sap/ui/model/ListBinding";
import type { InaQuery } from "../service/inaTypes";

/** The fields of the Create dialog, in its order, with the id of each. */
const CREATE_FIELDS: readonly { field: InaField; id: string }[] = [
    { field: "name", id: "inaCreateName" },
    { field: "title", id: "inaCreateTitle" },
    { field: "destination", id: "inaCreateDestination" },
    { field: "query", id: "inaCreateQuery" },
    { field: "package", id: "inaCreatePackage" },
    { field: "purpose", id: "inaCreatePurpose" }
];

/** The model of the Create dialog. */
interface CreateState {
    form: InaQueryForm;
    /** Field -> what is wrong with it, in words. */
    errors: Partial<Record<InaField, string>>;
    /** What the server refused about no field of the dialog. */
    error: string;
    busy: boolean;
}

/**
 * The list of analytical queries (InA, `/admin/api/ina/queries`) and the
 * dialog that registers one. What a query is and does is on its own page
 * (`inaQueryDetail`), which a row and a successful Create open.
 *
 * @namespace com.agent.admin.controller
 */
export default class InaQueries extends BaseController {

    public formatDestinationState = DestinationField.formatState;
    public formatDestinationStateText = DestinationField.formatStateText;

    private createDialog?: Dialog;
    private createDialogLoading?: Promise<Dialog>;
    private destinationField!: DestinationField;
    /** The dialog is shown: its destination field is judged. */
    private createShown = false;

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "ina");
        // loadFailed / loadError: the last load did not work, and why.
        // query: the search text, which decides the table's empty text.
        this.setModel(new JSONModel({ loadFailed: false, loadError: "", query: "" }), "view");
        this.setModel(new JSONModel(InaQueries.blankCreate()), "create");
        // `items` and `hint` of the destination field; `create`: what the
        // list knows about the name in the dialog's field.
        const dest = new JSONModel({ items: [], hint: "", create: { notice: "", noticeText: "" } });
        this.setModel(dest, "dest");
        this.destinationField = new DestinationField(
            this.getView()!, dest, () => this.getAdminService(),
            (key, args) => this.text(DESTINATION_TEXTS[key] ?? key, args)
        );
        this.getRouter().getRoute("inaQueries")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private static blankCreate(): CreateState {
        return { form: inaQueries.emptyForm(), errors: {}, error: "", busy: false };
    }

    private create(): JSONModel {
        return this.getModel("create") as JSONModel;
    }

    private form(): InaQueryForm {
        return this.create().getProperty("/form") as InaQueryForm;
    }

    /**
     * Reads the list. A failure empties the table and is shown on the page
     * with a retry, so it cannot be mistaken for an empty list; a lapsed
     * session or a missing scope additionally gets the central dialog.
     */
    private load(): Promise<void> {
        const view = this.getModel("view") as JSONModel;
        return this.withBusy(async () => {
            try {
                const queries = await this.getAdminService().listInaQueries();
                (this.getModel("ina") as JSONModel).setProperty("/items", inaQueries.rowsOf(queries));
                view.setProperty("/loadFailed", false);
                view.setProperty("/loadError", "");
            } catch (error) {
                (this.getModel("ina") as JSONModel).setProperty("/items", []);
                const reason = ErrorHandler.messageFor(error, "");
                view.setProperty("/loadError", reason
                    ? this.text("inaLoadFailedReason", [reason]) : this.text("inaLoadFailed"));
                view.setProperty("/loadFailed", true);
                const kind = ErrorHandler.classify(error);
                if (kind === "session" || kind === "forbidden") {
                    ErrorHandler.handle(error);
                }
            }
        });
    }

    public onRetry(): void {
        void this.load();
    }

    // --- formatters ---------------------------------------------------------

    public formatRunsAs(userContext: boolean | undefined): string {
        return this.text(userContext ? "odataRunsAsUser" : "odataRunsAsTechnical");
    }

    /** "not used", "1 agent" or "n agents". */
    public formatUsedBy(usedBy: string[] | undefined | null): string {
        const count = usedBy?.length ?? 0;
        if (count === 0) {
            return this.text("odataUsedByNone");
        }
        return count === 1 ? this.text("odataUsedByOne") : this.text("odataUsedByMany", [count]);
    }

    /** The agents behind the count, for the tooltip of the "Used by" cell. */
    public formatUsedByNames(usedBy: string[] | undefined | null): string {
        return (usedBy ?? []).join(", ");
    }

    /** The day `Describe` last read SAP, or "Not yet". */
    public formatDescribed(described: boolean | undefined, describedAt: string | null | undefined): string {
        if (!described) {
            return this.text("inaDescribedNotYet");
        }
        const at = describedAt ? new Date(describedAt) : null;
        return at && !isNaN(at.getTime()) ? at.toLocaleDateString() : this.text("inaDescribedYes");
    }

    /** The whole time of that read, for the tooltip. */
    public formatDescribedAt(described: boolean | undefined, describedAt: string | null | undefined): string {
        return described && describedAt ? formatter.timestamp(describedAt) : "";
    }

    /** Why the table is empty: the load failed, the search matches nothing,
     *  or there really is no query yet. */
    public formatNoData(loadFailed: boolean | undefined, query: string | undefined): string {
        if (loadFailed) {
            return this.text("inaLoadFailedNoData");
        }
        return this.text(query ? "inaNoMatches" : "inaNoQueries");
    }

    public formatErrorState(message: string | undefined): ValueState {
        return message ? ValueState.Error : ValueState.None;
    }

    // --- search and navigation ----------------------------------------------

    /** Filters on title, name and the InA name. Bound to both `liveChange`
     *  and `search`, so the clear button and Enter behave like typing. */
    public onSearch(event: Event): void {
        const query = (event.getSource() as SearchField).getValue().trim();
        const filters = query
            ? [new Filter({
                filters: [
                    new Filter("title", FilterOperator.Contains, query),
                    new Filter("name", FilterOperator.Contains, query),
                    new Filter("query", FilterOperator.Contains, query)
                ],
                and: false
            })]
            : [];
        (this.getModel("view") as JSONModel).setProperty("/query", query);
        const table = this.byId("inaQueriesTable") as Table;
        (table.getBinding("items") as ListBinding).filter(filters);
    }

    public onOpen(event: Event): void {
        const row = (event.getSource() as ColumnListItem).getBindingContext("ina")?.getObject() as InaQueryRow;
        this.getRouter().navTo("inaQueryDetail", { name: row.name });
    }

    // --- create -------------------------------------------------------------

    /** Opens the Create dialog on an empty form: the technical user, enabled. */
    public async onCreate(): Promise<void> {
        this.create().setData(InaQueries.blankCreate());
        const dialog = await this.loadCreateDialog();
        this.createShown = true;
        this.destinationField.clear();
        void this.destinationField.load();
        dialog.open();
    }

    private async loadCreateDialog(): Promise<Dialog> {
        if (this.createDialog) {
            return this.createDialog;
        }
        if (!this.createDialogLoading) {
            // loadFragment makes it a dependent of the view: it is destroyed with it.
            this.createDialogLoading = this.loadFragment({
                name: "com.agent.admin.fragment.InaCreateDialog"
            }) as Promise<Dialog>;
        }
        const dialog = await this.createDialogLoading;
        if (!this.createDialog) {
            this.createDialog = dialog;
            this.destinationField.attach(this.byId("inaCreateDestination") as Input, {
                path: "/create",
                userContext: () => this.form().user_context === true,
                judged: () => this.createShown,
                stored: () => this.form().destination,
                hasError: () => !!this.create().getProperty("/errors/destination"),
                edited: () => this.clearError("destination")
            });
        }
        return this.createDialog;
    }

    /** A field was edited: what the last refused Create said about it is gone. */
    private clearError(field: string): void {
        this.create().setProperty(`/errors/${field}`, "");
        this.create().setProperty("/error", "");
    }

    public onCreateEdit(event: Event): void {
        this.clearError(String((event.getSource() as Control).data("field")));
    }

    public onCreateRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.create().setProperty("/form/user_context", key === "user");
        // Said even when unchanged: the admin wants to know what holds now.
        this.destinationField.check(this.byId("inaCreateDestination") as Input, true, true);
    }

    public onCreateCancel(): void {
        this.createDialog?.close();
    }

    /** What the list said about the dialog's field is cleared when it
     *  opens again (`onCreate`), not here: the field has left the page. */
    public onCreateClosed(): void {
        this.createShown = false;
    }

    /**
     * Checks the form as the server would, creates the query and opens its
     * page. What is refused is said at the field; what the server refuses
     * about no field of the dialog, above the form.
     */
    public async onCreateConfirm(): Promise<void> {
        const model = this.create();
        if (model.getProperty("/busy") === true || !this.createDialog?.isOpen()) {
            return;
        }
        // A tap on Create that does not move the focus: the field may show
        // a name its binding has not stored.
        this.destinationField.storeShown(this.byId("inaCreateDestination") as Input);
        const form = this.form();
        const problems = inaQueries.validateInput(form);
        const errors: Partial<Record<InaField, string>> = {};
        (Object.keys(problems) as InaField[]).forEach((field) => {
            errors[field] = this.text(problems[field] as string);
        });
        model.setProperty("/errors", errors);
        model.setProperty("/error", "");
        const first = CREATE_FIELDS.filter((entry) => errors[entry.field])[0];
        if (first) {
            (this.byId(first.id) as Input).focus();
            return;
        }

        model.setProperty("/busy", true);
        let created: InaQuery;
        try {
            created = await this.withBusy(() => this.getAdminService().createInaQuery(inaQueries.toInput(form)));
        } catch (error) {
            model.setProperty("/busy", false);
            this.showCreateRefusal(error);
            return;
        }
        model.setProperty("/busy", false);
        this.createDialog.close();
        MessageToast.show(this.text("inaCreated", [created.title]));
        this.getRouter().navTo("inaQueryDetail", { name: created.name });
    }

    /** A 409 of a create is a taken name; a 422 names its fields. */
    private showCreateRefusal(error: unknown): void {
        const model = this.create();
        if (error instanceof AdminError && error.status === 409 && error.detail) {
            model.setProperty("/errors", { name: error.detail });
            (this.byId("inaCreateName") as Input).focus();
            return;
        }
        if (error instanceof AdminError && error.status === 422) {
            const byLoc = odataCatalog.serverErrors(error.detail);
            const errors: Record<string, string> = {};
            let unplaced = Object.keys(byLoc).length === 0;
            Object.keys(byLoc).forEach((loc) => {
                if (CREATE_FIELDS.some((entry) => entry.field === loc)) {
                    errors[loc] = byLoc[loc];
                } else {
                    unplaced = true;
                }
            });
            model.setProperty("/errors", errors);
            model.setProperty("/error", unplaced ? (error.detail || this.text("inaCreateFailed")) : "");
            return;
        }
        ErrorHandler.handle(error, this.text("inaCreateFailed"));
    }
}
