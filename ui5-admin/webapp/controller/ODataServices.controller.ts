import JSONModel from "sap/ui/model/json/JSONModel";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import ODataController from "./odata/ODataController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import odataCatalog from "../model/odataCatalog";
import type Event from "sap/ui/base/Event";
import type ColumnListItem from "sap/m/ColumnListItem";
import type SearchField from "sap/m/SearchField";
import type Table from "sap/m/Table";
import type Control from "sap/ui/core/Control";
import type ListBinding from "sap/ui/model/ListBinding";
import type { ODataServiceInput, ODataServiceSummary, ODataUsedBy } from "../service/types";

/** The fields of an exported service (`to_export()`), which are exactly what
 *  a create accepts. Anything else in a file (an id, counts, `used_by` of a
 *  copied API answer) is left out, because the server refuses unknown keys. */
const CONFIG_FIELDS: readonly (keyof ODataServiceInput)[] = [
    "name", "title", "purpose", "not_for", "destination", "user_context", "odata_version",
    "service_path", "enabled", "definition", "metadata_fetched_at"
];

/** The fields `odataCatalog.validate` reads as text. */
const TEXT_FIELDS: readonly (keyof ODataServiceInput)[] = [
    "name", "title", "purpose", "not_for", "destination", "service_path"
];

/** The largest file the import reads. A stored definition is at most
 *  2,000,000 bytes (`MAX_DEFINITION_BYTES`), so nothing bigger can be a
 *  service. The text `odataConfigTooLarge` states this limit. */
const MAX_CONFIG_BYTES = 2 * 1024 * 1024;

/** What the import needs of a picked file. */
interface ConfigFile {
    size: number;
    text(): Promise<string>;
}

/**
 * The list of OData services in the catalogue.
 *
 * @namespace com.agent.admin.controller
 */
export default class ODataServices extends ODataController {

    /** A delete is on its way: no second one until it has answered. */
    private deleting = false;

    /** A picked file is being read or created. */
    private importing = false;

    /** The one file input behind "Import configuration". One element, so a
     *  double click cannot leave two pickers behind. */
    private fileInput?: HTMLInputElement;

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "odata");
        // loadFailed / loadError: the last load did not work, and why.
        // query: the search text, which decides the table's empty text.
        // busy: a delete or an import is running.
        this.setModel(new JSONModel({ loadFailed: false, loadError: "", query: "", busy: false }), "view");
        this.getRouter().getRoute("odataServices")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    /**
     * Reads the list. A failure empties the table and is shown on the page
     * with a retry, so it cannot be mistaken for an empty catalogue; a lapsed
     * session or a missing scope additionally gets the central dialog.
     */
    private load(): Promise<void> {
        const view = this.getModel("view") as JSONModel;
        return this.withBusy(async () => {
            try {
                const services = await this.getAdminService().listODataServices();
                (this.getModel("odata") as JSONModel).setProperty("/items", services);
                view.setProperty("/loadFailed", false);
                view.setProperty("/loadError", "");
            } catch (error) {
                (this.getModel("odata") as JSONModel).setProperty("/items", []);
                const reason = ErrorHandler.messageFor(error, "");
                view.setProperty("/loadError", reason
                    ? this.text("odataLoadFailedReason", [reason]) : this.text("odataLoadFailed"));
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

    /** "not used", "1 agent" or "n agents". */
    public formatUsedBy(usedBy: ODataUsedBy[] | undefined | null): string {
        const count = usedBy?.length ?? 0;
        if (count === 0) {
            return this.text("odataUsedByNone");
        }
        return count === 1 ? this.text("odataUsedByOne") : this.text("odataUsedByMany", [count]);
    }

    /** The agents behind the count, for the tooltip of the "Used by" cell. */
    public formatUsedByNames(usedBy: ODataUsedBy[] | undefined | null): string {
        return (usedBy ?? []).map((used) => used.agent).join(", ");
    }

    /** Why the table is empty: the load failed, the search matches nothing,
     *  or there really is no service yet. */
    public formatNoData(loadFailed: boolean | undefined, query: string | undefined): string {
        if (loadFailed) {
            return this.text("odataLoadFailedNoData");
        }
        return this.text(query ? "odataNoMatches" : "odataNoServices");
    }

    // --- search and navigation ----------------------------------------------

    /** Filters on what the first column shows: title, name and purpose.
     *  Bound to both `liveChange` and `search`, so the field's clear button
     *  and Enter behave like typing. */
    public onSearch(event: Event): void {
        const query = (event.getSource() as SearchField).getValue().trim();
        const filters = query
            ? [new Filter({
                filters: [
                    new Filter("title", FilterOperator.Contains, query),
                    new Filter("name", FilterOperator.Contains, query),
                    new Filter("purpose", FilterOperator.Contains, query)
                ],
                and: false
            })]
            : [];
        (this.getModel("view") as JSONModel).setProperty("/query", query);
        const table = this.byId("odataServicesTable") as Table;
        (table.getBinding("items") as ListBinding).filter(filters);
    }

    public onCreate(): void {
        this.getRouter().navTo("odataServiceDetail", { serviceName: "new" });
    }

    public onOpen(event: Event): void {
        const service = (event.getSource() as ColumnListItem)
            .getBindingContext("odata")?.getObject() as ODataServiceSummary;
        this.getRouter().navTo("odataServiceDetail", { serviceName: service.name });
    }

    // --- delete -------------------------------------------------------------

    private setBusyFlag(): void {
        (this.getModel("view") as JSONModel).setProperty("/busy", this.deleting || this.importing);
    }

    public onDelete(event: Event): void {
        if (this.deleting) {
            return;
        }
        const service = (event.getSource() as Control)
            .getBindingContext("odata")?.getObject() as ODataServiceSummary;
        this.askDelete(service, () => {
            void this.deleteService(service);
        });
    }

    /**
     * Deletes `service`; resolves whether it is gone. While one delete is
     * running a second call does nothing and resolves `false`.
     *
     * Whatever answer the server gives means the row may no longer be true
     * (someone else deleted the service, or attached it to an agent), so the
     * list is read again after every refusal. Only a call that never got an
     * answer leaves the list alone.
     */
    public async deleteService(service: ODataServiceSummary): Promise<boolean> {
        if (this.deleting) {
            return false;
        }
        this.deleting = true;
        this.setBusyFlag();
        let outcome;
        try {
            outcome = await this.removeService(service);
            if (outcome !== "unanswered") {
                await this.load();
            }
        } finally {
            this.deleting = false;
            this.setBusyFlag();
        }
        return outcome === "deleted";
    }

    // --- import -------------------------------------------------------------

    /** Lets the admin pick an exported service file and creates it. */
    public onImportConfiguration(): void {
        if (this.importing) {
            return;
        }
        if (!this.fileInput) {
            const input = document.createElement("input");
            input.type = "file";
            input.accept = ".json,application/json";
            input.addEventListener("change", () => {
                const file = input.files?.[0];
                // Emptied so that picking the same file again fires `change`.
                input.value = "";
                if (file) {
                    void this.importFile(file);
                }
            });
            this.fileInput = input;
        }
        this.fileInput.click();
    }

    /**
     * Reads a picked file and creates the service in it; resolves whether a
     * service was created. A file that is too large is refused unread.
     */
    public async importFile(file: ConfigFile): Promise<boolean> {
        if (this.importing) {
            return false;
        }
        this.importing = true;
        this.setBusyFlag();
        try {
            if (file.size > MAX_CONFIG_BYTES) {
                MessageBox.error(this.text("odataConfigTooLarge"));
                return false;
            }
            let text: string;
            try {
                text = await file.text();
            } catch {
                MessageBox.error(this.text("odataConfigUnreadable"));
                return false;
            }
            return await this.importConfiguration(text);
        } finally {
            this.importing = false;
            this.setBusyFlag();
        }
    }

    /**
     * Creates a service from the text of an exported service file.
     *
     * A file that is not JSON, is not one object, or whose general fields
     * the server would refuse anyway is reported without a call; the
     * definition's own rules are the server's, and its answer is shown as
     * it comes.
     */
    private async importConfiguration(text: string): Promise<boolean> {
        let parsed: unknown;
        try {
            parsed = JSON.parse(text);
        } catch {
            MessageBox.error(this.text("odataConfigNotJson"));
            return false;
        }
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
            MessageBox.error(this.text("odataConfigNotObject"));
            return false;
        }

        const { input, invalid, switchedOff } = ODataServices.toInput(parsed as Record<string, unknown>);
        if (invalid.length) {
            MessageBox.error(this.text("odataConfigInvalid", [invalid.join(", ")]));
            return false;
        }

        // The file is stored as it is, writes included: what it opens is
        // asked about first, the way the Duplicate dialog says it.
        const question = this.importQuestion(input, switchedOff);
        if (question && !(await this.confirmImport(question))) {
            return false;
        }

        let created;
        try {
            created = await this.withBusy(() => this.getAdminService().createODataService(input));
        } catch (error) {
            if (error instanceof AdminError && ErrorHandler.classify(error) === "conflict") {
                // A taken name: a refusal to act on, not a passing toast.
                MessageBox.error(error.detail || this.text("odataConfigCreateFailed"));
            } else {
                ErrorHandler.handle(error, this.text("odataConfigCreateFailed"));
            }
            return false;
        }
        MessageToast.show(this.text(switchedOff ? "odataConfigImportedOff" : "odataConfigImported", [created.title]));
        await this.load();
        return true;
    }

    /**
     * What has to be confirmed before the service of a file is created, or
     * "" when it opens nothing: the writes its definition holds (every one:
     * nothing is stored yet) and the enabled operations it marks as only
     * reading, with the identity and the destination they would run with.
     * A definition the rules cannot read is asked about in general words,
     * never created unseen.
     */
    private importQuestion(input: ODataServiceInput, switchedOff: boolean): string {
        let writes: string;
        let reads: string;
        try {
            const pending = odataCatalog.pendingWrites(undefined, input.definition);
            writes = odataCatalog.hasWrite(input.definition) || odataCatalog.pendingCount(pending) > 0
                ? this.writeList({ ...pending, fields: [] }) : "";
            reads = this.operationList(odataCatalog.pendingReads(undefined, input.definition));
        } catch {
            return this.text("odataConfigAskUnknown");
        }
        if (!writes && !reads) {
            return "";
        }
        return [
            this.text("odataConfigAskIntro", [
                odataCatalog.importLabel(input.title) || input.name, input.name,
                this.formatRunsAs(input.user_context === true), input.destination
            ]),
            writes ? this.text("odataConfigAskWrites", [writes]) : "",
            reads ? this.text("odataConfigAskReads", [reads]) : "",
            this.text(switchedOff ? "odataConfigAskOff" : input.enabled === false
                ? "odataConfigAskStoredOff" : "odataConfigAskOn")
        ].filter(Boolean).join("\n\n");
    }

    /** Asks `question` with Cancel as the default; resolves whether the
     *  admin chose to create the service. */
    private confirmImport(question: string): Promise<boolean> {
        const create = this.text("odataConfigCreate");
        return new Promise((resolve) => {
            MessageBox.warning(question, {
                title: this.text("odataConfigAskTitle"),
                actions: [create, MessageBox.Action.CANCEL],
                emphasizedAction: create,
                initialFocus: MessageBox.Action.CANCEL,
                onClose: (action: string | null) => resolve(action === create)
            });
        });
    }

    /**
     * The payload fields of a parsed file on top of an empty service, the
     * fields that cannot be right, in the order of the form, and whether the
     * service is switched off because the file does not say it is on.
     */
    private static toInput(
        source: Record<string, unknown>
    ): { input: ODataServiceInput; invalid: string[]; switchedOff: boolean } {
        const input = odataCatalog.emptyService() as unknown as Record<string, unknown>;
        const wrongType: string[] = [];
        CONFIG_FIELDS.forEach((field) => {
            const value = source[field];
            if (value === undefined) {
                return;
            }
            if (TEXT_FIELDS.indexOf(field) !== -1 && typeof value !== "string") {
                wrongType.push(field);
                return;
            }
            input[field] = value;
        });
        // A file that does not say, as a boolean, whether the service is on
        // gets no "on" from the defaults of a new service: it is created
        // switched off, and the page says so.
        const switchedOff = typeof source.enabled !== "boolean";
        if (switchedOff) {
            input.enabled = false;
        }
        // No default for the version: a file that does not say V2 or V4 is
        // refused rather than guessed at.
        if (source.odata_version !== "v2" && source.odata_version !== "v4") {
            wrongType.push("odata_version");
        }
        const failed = Object.keys(odataCatalog.validate(input as unknown as ODataServiceInput));
        const invalid = (CONFIG_FIELDS as readonly string[])
            .filter((field) => wrongType.indexOf(field) !== -1 || failed.indexOf(field) !== -1);
        return { input: input as unknown as ODataServiceInput, invalid, switchedOff };
    }
}
