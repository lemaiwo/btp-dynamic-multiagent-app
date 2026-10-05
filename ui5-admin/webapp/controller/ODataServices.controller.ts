import JSONModel from "sap/ui/model/json/JSONModel";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
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

/**
 * The list of OData services in the catalogue.
 *
 * @namespace com.agent.admin.controller
 */
export default class ODataServices extends BaseController {

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "odata");
        this.getRouter().getRoute("odataServices")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private load(): Promise<void> {
        return this.withBusy(async () => {
            const services = await this.run(
                this.getAdminService().listODataServices(),
                "Could not load the OData services."
            );
            if (services) {
                (this.getModel("odata") as JSONModel).setProperty("/items", services);
            }
        });
    }

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

    public onDelete(event: Event): void {
        const service = (event.getSource() as Control)
            .getBindingContext("odata")?.getObject() as ODataServiceSummary;

        MessageBox.confirm(this.text("odataDeleteConfirm", [service.title]), {
            title: this.text("delete"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.doDelete(service);
                }
            }
        });
    }

    private async doDelete(service: ODataServiceSummary): Promise<void> {
        try {
            await this.getAdminService().deleteODataService(service.name);
        } catch (error) {
            // The central policy shows a 409 as a passing toast ("a run is
            // already in flight"). Here it means the delete was refused, and
            // the admin has to detach the service from the agents first, so
            // it stays on screen and names them.
            if (error instanceof AdminError && error.status === 409) {
                const agents = this.formatUsedByNames(service.used_by);
                MessageBox.error(agents ? this.text("odataDeleteInUse", [agents]) : error.detail);
                // The row may be older than the refusal: show who uses it now.
                void this.load();
            } else {
                ErrorHandler.handle(error, `Could not delete the OData service "${service.title}".`);
            }
            return;
        }
        MessageToast.show(this.text("odataDeleted"));
        void this.load();
    }

    /** Lets the admin pick an exported service file and creates it. */
    public onImportConfiguration(): void {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".json,application/json";
        input.addEventListener("change", () => {
            const file = input.files?.[0];
            if (file) {
                void file.text().then((text) => this.importConfiguration(text));
            }
        });
        input.click();
    }

    /**
     * Creates a service from the text of an exported service file.
     *
     * Resolves whether a service was created. A file that is not JSON, or
     * whose general fields the server would refuse anyway, is reported
     * without a call; the definition's own rules are the server's.
     */
    public async importConfiguration(text: string): Promise<boolean> {
        let parsed: unknown;
        try {
            parsed = JSON.parse(text);
        } catch (error) {
            MessageBox.error(this.text("odataConfigInvalid", [(error as Error).message]));
            return false;
        }

        const input = ODataServices.toInput(parsed);
        const invalid = Object.keys(odataCatalog.validate(input));
        if (input.odata_version !== "v2" && input.odata_version !== "v4") {
            invalid.push("odata_version");
        }
        if (invalid.length) {
            MessageBox.error(this.text("odataConfigInvalid", [invalid.join(", ")]));
            return false;
        }

        const created = await this.run(
            this.getAdminService().createODataService(input),
            "Could not create the OData service from the file."
        );
        if (!created) {
            return false;
        }
        MessageToast.show(this.text("odataConfigImported", [created.title]));
        await this.load();
        return true;
    }

    /** The payload fields of `parsed`, on top of an empty service. Anything
     *  that is not an object yields the empty service, which fails validation
     *  on every required field. */
    private static toInput(parsed: unknown): ODataServiceInput {
        const input = odataCatalog.emptyService() as unknown as Record<string, unknown>;
        if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
            const source = parsed as Record<string, unknown>;
            // No default for the version: a file that does not say V2 or V4
            // is refused rather than guessed at.
            input.odata_version = undefined;
            CONFIG_FIELDS.forEach((field) => {
                if (source[field] !== undefined) {
                    input[field] = source[field];
                }
            });
        }
        return input as unknown as ODataServiceInput;
    }
}
