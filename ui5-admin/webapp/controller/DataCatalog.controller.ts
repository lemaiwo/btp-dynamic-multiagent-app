import JSONModel from "sap/ui/model/json/JSONModel";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import formatter from "../model/formatter";
import dataCatalog from "../model/dataCatalog";
import type Event from "sap/ui/base/Event";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Control from "sap/ui/core/Control";
import type Popover from "sap/m/Popover";
import type SearchField from "sap/m/SearchField";
import type Table from "sap/m/Table";
import type ListBinding from "sap/ui/model/ListBinding";
import type { DataRebuildResult, DataSourceSummary } from "../service/dataCatalogTypes";

/**
 * The list of data catalogue sources: what is imported, as whom it runs,
 * who uses it and how much of it carries hints.
 *
 * @namespace com.agent.admin.controller
 */
export default class DataCatalog extends BaseController {

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "dc");
        // selected: a row is picked, so "Rebuild selected" applies to it.
        // busy: a rebuild is running. kind: the kind filter, "" for all.
        this.setModel(new JSONModel({
            loadFailed: false, loadError: "", query: "", kind: "", selected: false, busy: false
        }), "view");
        this.setModel(new JSONModel({ names: [] }), "usedBy");
        this.getRouter().getRoute("dataCatalog")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    /** Reads the list. A failure empties the table and says so with a retry. */
    private load(): Promise<void> {
        const view = this.getModel("view") as JSONModel;
        return this.withBusy(async () => {
            try {
                const sources = await this.getAdminService().listDataSources();
                (this.getModel("dc") as JSONModel).setProperty("/items", sources);
                view.setProperty("/loadFailed", false);
                view.setProperty("/loadError", "");
            } catch (error) {
                (this.getModel("dc") as JSONModel).setProperty("/items", []);
                const reason = ErrorHandler.messageFor(error, "");
                view.setProperty("/loadError", reason
                    ? this.text("dcLoadFailedReason", [reason]) : this.text("dcLoadFailed"));
                view.setProperty("/loadFailed", true);
                const kind = ErrorHandler.classify(error);
                if (kind === "session" || kind === "forbidden") {
                    ErrorHandler.handle(error);
                }
            }
            (this.byId("dcSourcesTable") as Table).removeSelections(true);
            view.setProperty("/selected", false);
        });
    }

    public onRetry(): void {
        void this.load();
    }

    // --- formatters ---------------------------------------------------------

    public formatKind(kind: string): string {
        return this.text(`dcKind_${kind}`);
    }

    public formatRunsAs(userContext: boolean | undefined): string {
        return this.text(userContext ? "odataRunsAsUser" : "odataRunsAsTechnical");
    }

    public formatUsedBy(usedBy: string[] | undefined | null): string {
        const count = usedBy?.length ?? 0;
        if (count === 0) {
            return this.text("odataUsedByNone");
        }
        return count === 1 ? this.text("odataUsedByOne") : this.text("odataUsedByMany", [count]);
    }

    public formatDate(iso: string | null): string {
        return iso ? formatter.timestamp(iso) : this.text("dcNeverImported");
    }

    public formatPercent(value: number): number {
        return dataCatalog.completenessPercent(value);
    }

    public formatPercentText(value: number): string {
        return dataCatalog.completenessText(value);
    }

    public formatPercentState(value: number): string {
        return dataCatalog.completenessState(value);
    }

    public formatNoData(loadFailed: boolean | undefined, query: string | undefined, kind?: string): string {
        if (loadFailed) {
            return this.text("dcLoadFailedNoData");
        }
        return this.text(query || kind ? "dcNoMatches" : "dcNoSources");
    }

    // --- interaction --------------------------------------------------------

    public onSearch(event: Event): void {
        (this.getModel("view") as JSONModel).setProperty("/query", (event.getSource() as SearchField).getValue().trim());
        this.applyFilters();
    }

    public onKindFilter(): void {
        this.applyFilters();
    }

    /** The search text (title, name or destination) and the kind, together. */
    private applyFilters(): void {
        const view = this.getModel("view") as JSONModel;
        const query = String(view.getProperty("/query") ?? "");
        const kind = String(view.getProperty("/kind") ?? "");
        const filters: Filter[] = [];
        if (query) {
            filters.push(new Filter({
                filters: [
                    new Filter("title", FilterOperator.Contains, query),
                    new Filter("name", FilterOperator.Contains, query),
                    new Filter("destination", FilterOperator.Contains, query)
                ],
                and: false
            }));
        }
        if (kind) {
            filters.push(new Filter("kind", FilterOperator.EQ, kind));
        }
        // Both must hold: said, not left to the binding's grouping by path.
        ((this.byId("dcSourcesTable") as Table).getBinding("items") as ListBinding)
            .filter(filters.length ? [new Filter({ filters, and: true })] : []);
    }

    public onSelectionChange(): void {
        const selected = (this.byId("dcSourcesTable") as Table).getSelectedItem();
        (this.getModel("view") as JSONModel).setProperty("/selected", !!selected);
    }

    public onOpen(event: Event): void {
        const source = (event.getSource() as ColumnListItem)
            .getBindingContext("dc")?.getObject() as DataSourceSummary;
        this.getRouter().navTo("dataCatalogDetail", { kind: source.kind, name: source.name });
    }

    public onShowGraph(): void {
        this.getRouter().navTo("dataCatalogGraph");
    }

    /** Opens the names behind a "used by" count next to it. */
    public onUsedByPress(event: Event): void {
        const link = event.getSource() as Control;
        const source = link.getBindingContext("dc")?.getObject() as DataSourceSummary;
        (this.getModel("usedBy") as JSONModel).setProperty("/names", (source.used_by ?? []).slice());
        (this.byId("dcUsedByPopover") as Popover).openBy(link);
    }

    // --- rebuild ------------------------------------------------------------

    public onRebuildAll(): void {
        void this.rebuild();
    }

    public onRebuildSelected(): void {
        const item = (this.byId("dcSourcesTable") as Table).getSelectedItem();
        const source = item?.getBindingContext("dc")?.getObject() as DataSourceSummary | undefined;
        if (source) {
            void this.rebuild({ kind: source.kind, name: source.name });
        }
    }

    /** Imports again (one source or all) and says what changed; resolves whether it worked. */
    public async rebuild(source?: { kind: string; name: string }): Promise<boolean> {
        const view = this.getModel("view") as JSONModel;
        if (view.getProperty("/busy")) {
            return false;
        }
        view.setProperty("/busy", true);
        let result: DataRebuildResult | undefined;
        try {
            result = await this.run(
                this.withBusy(() => this.getAdminService().rebuildDataCatalog(source)),
                this.text("dcRebuildFailed"));
        } finally {
            view.setProperty("/busy", false);
        }
        if (!result) {
            return false;
        }
        MessageToast.show(this.text("dcRebuilt", [result.imported, result.removed, result.vectors]));
        await this.load();
        return true;
    }
}
