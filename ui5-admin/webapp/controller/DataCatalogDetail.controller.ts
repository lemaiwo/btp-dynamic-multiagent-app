import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import LinkDialog from "./dataCatalog/LinkDialog";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import dataCatalog, { type EntityRow, type HintsForm, type LinkStart, type RelationRow } from "../model/dataCatalog";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type MultiInput from "sap/m/MultiInput";
import type { MultiInput$TokenUpdateEvent } from "sap/m/MultiInput";
import type { ListBase$DeleteEvent } from "sap/m/ListBase";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { DataEdge, DataHintsUpdate, DataNode, DataSourceDetail } from "../service/dataCatalogTypes";

/** The editor's copy of the source hints: examples as rows, so each one can be bound to an input. */
interface HintsEditor {
    label: string;
    purpose: string;
    not_for: string;
    synonyms: string[];
    exampleRows: { text: string }[];
}

/**
 * One source of the data catalogue: its hints (edited inline), entities and
 * fields (for an InA source: its query, dimensions and measures), its links
 * (Add link, and a remove on the ones an admin drew), the agents that use it
 * and a test of the retrieval.
 *
 * @namespace com.agent.admin.controller
 */
export default class DataCatalogDetail extends BaseController {

    /** The source hints as stored, to tell what an edit changes. */
    private saved?: HintsForm;
    /** The answer the page shows; a link that is added or removed changes its edges. */
    private detail?: DataSourceDetail;
    private linkDialog?: LinkDialog;

    public onInit(): void {
        this.setModel(new JSONModel(DataCatalogDetail.emptyState()), "src");
        this.setModel(new JSONModel({
            question: "", agent: "", agents: [], candidates: [], scope: "all", ran: false, running: false
        }), "search");
        this.getRouter().getRoute("dataCatalogDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const args = event.getParameter("arguments") as { kind: string; name: string };
            void this.load(args.kind, args.name);
        });
    }

    private static emptyState(): Record<string, unknown> {
        return {
            loaded: false, imported: false, loadFailed: false, loadError: "", kind: "", name: "", source: {},
            hints: { label: "", purpose: "", not_for: "", synonyms: [], exampleRows: [] },
            errors: {}, dirty: false, saving: false, rows: [], relations: [], linkStarts: [], usedBy: []
        };
    }

    // Methods, not getters: a getter on a controller class is read when
    // UI5's extend copies the class members, before there is a view.
    private src(): JSONModel {
        return this.getModel("src") as JSONModel;
    }

    private search(): JSONModel {
        return this.getModel("search") as JSONModel;
    }

    /** Reads the source and, beside it, the agents the test search can be limited to. */
    public load(kind: string, name: string): Promise<void> {
        this.src().setData({ ...DataCatalogDetail.emptyState(), kind, name });
        this.search().setData({
            ...this.search().getData(), candidates: [], ran: false, running: false, agent: ""
        });
        void this.loadAgents();
        return this.withBusy(async () => {
            try {
                const detail = await this.getAdminService().getDataSource(kind, name);
                this.show(detail);
            } catch (error) {
                const reason = ErrorHandler.messageFor(error, "");
                this.src().setProperty("/loadError", reason
                    ? this.text("dcDetailLoadFailedReason", [reason]) : this.text("dcDetailLoadFailed"));
                this.src().setProperty("/loadFailed", true);
                const errorKind = ErrorHandler.classify(error);
                if (errorKind === "session" || errorKind === "forbidden") {
                    ErrorHandler.handle(error);
                }
            }
        });
    }

    private show(detail: DataSourceDetail): void {
        this.src().setProperty("/usedBy", (detail.used_by ?? []).slice());
        this.src().setProperty("/loaded", true);
        if (!detail.source) {
            // In the catalogue, never imported: nothing to edit yet.
            this.saved = undefined;
            this.detail = undefined;
            this.src().setProperty("/imported", false);
            return;
        }
        this.detail = { ...detail, edges: (detail.edges ?? []).slice() };
        this.src().setProperty("/imported", true);
        this.saved = dataCatalog.hintsOf(detail.source);
        this.src().setProperty("/source", detail.source);
        this.src().setProperty("/hints", DataCatalogDetail.toEditor(this.saved));
        this.src().setProperty("/errors", {});
        this.src().setProperty("/dirty", false);
        this.src().setProperty("/rows", dataCatalog.entityRows(detail));
        this.src().setProperty("/linkStarts", dataCatalog.linkStarts(detail));
        this.showRelations();
    }

    private showRelations(): void {
        this.src().setProperty("/relations", this.detail
            ? dataCatalog.relationRows(this.detail, (key) => this.text(key)) : []);
    }

    /** Imports a source that was never imported, then reads it again. */
    public onImportNow(): void {
        void this.importNow();
    }

    public async importNow(): Promise<boolean> {
        const kind = this.src().getProperty("/kind") as string;
        const name = this.src().getProperty("/name") as string;
        const result = await this.run(
            this.withBusy(() => this.getAdminService().rebuildDataCatalog({ kind, name })),
            this.text("dcRebuildFailed"));
        if (!result) {
            return false;
        }
        MessageToast.show(this.text("dcRebuilt", [result.imported, result.removed, result.vectors]));
        await this.load(kind, name);
        return true;
    }

    /** The agents for the test search's scope; without the list only "All agents" is offered. */
    private async loadAgents(): Promise<void> {
        const all = { key: "", text: this.text("dcSearchAllAgents") };
        let names: string[] = [];
        try {
            names = (await this.getAdminService().listAgents()).map((agent) => agent.name);
        } catch {
            names = [];
        }
        this.search().setProperty("/agents", [all].concat(names.map((name) => ({ key: name, text: name }))));
    }

    private static toEditor(hints: HintsForm): HintsEditor {
        return {
            label: hints.label, purpose: hints.purpose, not_for: hints.not_for,
            synonyms: hints.synonyms.slice(), exampleRows: hints.examples.map((text) => ({ text }))
        };
    }

    private editedHints(): HintsForm {
        const editor = this.src().getProperty("/hints") as HintsEditor;
        return {
            label: editor.label, purpose: editor.purpose, not_for: editor.not_for,
            synonyms: editor.synonyms.slice(), examples: editor.exampleRows.map((row) => row.text),
            readable: this.saved?.readable
        };
    }

    /** What a save would send now. */
    public pendingHints(): DataHintsUpdate {
        return this.saved ? dataCatalog.hintsUpdate(this.saved, this.editedHints()) : {};
    }

    private refreshDirty(): void {
        this.src().setProperty("/dirty", dataCatalog.hasChanges(this.pendingHints()));
    }

    // --- formatters ---------------------------------------------------------

    public formatKind(kind: string): string {
        return kind ? this.text(`dcKind_${kind}`) : "";
    }

    public formatNodeKind(kind: string): string {
        return (dataCatalog.NODE_KINDS as readonly string[]).indexOf(kind) === -1 ? kind ?? "" : this.text(`dcNodeKind_${kind}`);
    }

    /** "Same object"; a kind the catalogue does not name is shown as it is. */
    public formatRelationKind(kind: string): string {
        const key = dataCatalog.relationKindKey(kind);
        return key ? this.text(key) : kind ?? "";
    }

    // --- source hints -------------------------------------------------------

    public onHintChange(): void {
        this.refreshDirty();
    }

    /** A typed synonym becomes a token (a repeat is dropped). */
    public onAddSynonym(event: Event): void {
        const input = event.getSource() as MultiInput;
        const value = input.getValue().trim();
        if (!value) {
            return;
        }
        const synonyms = (this.src().getProperty("/hints/synonyms") as string[]).slice();
        if (synonyms.indexOf(value) === -1) {
            synonyms.push(value);
        }
        this.src().setProperty("/hints/synonyms", synonyms);
        input.setValue("");
        this.refreshDirty();
    }

    public onSynonymTokenUpdate(event: MultiInput$TokenUpdateEvent): void {
        const removed = (event.getParameter("removedTokens") ?? []).map((token) => token.getKey());
        if (!removed.length) {
            return;
        }
        const synonyms = (this.src().getProperty("/hints/synonyms") as string[])
            .filter((value) => removed.indexOf(value) === -1);
        this.src().setProperty("/hints/synonyms", synonyms);
        this.refreshDirty();
    }

    public onAddExample(): void {
        const rows = (this.src().getProperty("/hints/exampleRows") as { text: string }[]).slice();
        rows.push({ text: "" });
        this.src().setProperty("/hints/exampleRows", rows);
    }

    public onDeleteExample(event: ListBase$DeleteEvent): void {
        const item = event.getParameter("listItem");
        const path = item?.getBindingContext("src")?.getPath() ?? "";
        const index = Number(path.split("/").pop());
        const rows = (this.src().getProperty("/hints/exampleRows") as { text: string }[]).slice();
        if (!isNaN(index) && index >= 0 && index < rows.length) {
            rows.splice(index, 1);
            this.src().setProperty("/hints/exampleRows", rows);
            this.refreshDirty();
        }
    }

    public onResetHints(): void {
        if (this.saved) {
            this.src().setProperty("/hints", DataCatalogDetail.toEditor(this.saved));
            this.src().setProperty("/errors", {});
            this.refreshDirty();
        }
    }

    public onSaveHints(): void {
        void this.saveHints();
    }

    /** Sends what changed; resolves whether it was stored. A 422 marks its field. */
    public async saveHints(): Promise<boolean> {
        const body = this.pendingHints();
        const source = this.src().getProperty("/source") as DataNode;
        if (!dataCatalog.hasChanges(body) || this.src().getProperty("/saving")) {
            return false;
        }
        this.src().setProperty("/saving", true);
        this.src().setProperty("/errors", {});
        try {
            const stored = await this.withBusy(() => this.getAdminService().updateDataHints(source.id, body));
            this.saved = dataCatalog.hintsOf(stored);
            this.src().setProperty("/source", stored);
            this.src().setProperty("/hints", DataCatalogDetail.toEditor(this.saved));
            this.src().setProperty("/dirty", false);
            MessageToast.show(this.text("dcHintsSaved"));
            return true;
        } catch (error) {
            const errors = DataCatalogDetail.fieldErrorsOf(error);
            if (Object.keys(errors).length) {
                this.src().setProperty("/errors", errors);
            } else {
                ErrorHandler.handle(error, this.text("dcHintsSaveFailed"));
            }
            return false;
        } finally {
            this.src().setProperty("/saving", false);
        }
    }

    /** A 422's messages by hint field. */
    private static fieldErrorsOf(error: unknown): Record<string, string> {
        return error instanceof AdminError && error.status === 422
            ? dataCatalog.hintErrors(error.fieldErrors, error.detail) : {};
    }

    // --- entity and field rows ----------------------------------------------

    private rowOf(event: Event): { row: EntityRow; path: string } | undefined {
        const context = (event.getSource() as Control).getBindingContext("src");
        return context ? { row: context.getObject() as EntityRow, path: context.getPath() } : undefined;
    }

    /** Saves a row's label when it differs from what is stored. */
    public onRowLabelChange(event: Event): void {
        const target = this.rowOf(event);
        if (target && target.row.label !== target.row.savedLabel) {
            void this.saveRow(target.path, { label: target.row.label });
        }
    }

    public onRowReadableChange(event: Event): void {
        const target = this.rowOf(event);
        if (target) {
            void this.saveRow(target.path, { readable: target.row.readable });
        }
    }

    /**
     * Sends one row's change; resolves whether it was stored. A refused label
     * stays in the input, marked; a refused or failed readable switch goes
     * back to what is stored.
     */
    public async saveRow(path: string, body: DataHintsUpdate): Promise<boolean> {
        const row = this.src().getProperty(path) as EntityRow;
        this.src().setProperty(`${path}/labelState`, "None");
        this.src().setProperty(`${path}/labelError`, "");
        try {
            const stored = await this.getAdminService().updateDataHints(row.id, body);
            this.src().setProperty(`${path}/label`, stored.label ?? "");
            this.src().setProperty(`${path}/savedLabel`, stored.label ?? "");
            this.src().setProperty(`${path}/readable`, stored.readable === true);
            MessageToast.show(this.text("dcHintsSaved"));
            return true;
        } catch (error) {
            const errors = DataCatalogDetail.fieldErrorsOf(error);
            if (body.readable !== undefined) {
                this.src().setProperty(`${path}/readable`, !body.readable);
            }
            if (errors.label) {
                this.src().setProperty(`${path}/labelState`, "Error");
                this.src().setProperty(`${path}/labelError`, errors.label);
            } else {
                ErrorHandler.handle(error, this.text("dcHintsSaveFailed"));
            }
            return false;
        }
    }

    // --- links ----------------------------------------------------------------

    public onAddLink(): void {
        void this.addLink();
    }

    /** Opens Add link; a link it stored is listed at once. Resolves whether one was. */
    public async addLink(): Promise<boolean> {
        const shown = this.detail;
        if (!shown) {
            return false;
        }
        if (!this.linkDialog) {
            this.linkDialog = new LinkDialog();
        }
        const edge = await this.linkDialog.open(this.getView()!, {
            source: { kind: this.src().getProperty("/kind") as string, name: this.src().getProperty("/name") as string },
            starts: this.src().getProperty("/linkStarts") as LinkStart[],
            service: this.getAdminService(),
            text: (key, args) => this.text(key, args)
        });
        if (!edge) {
            return false;
        }
        MessageToast.show(this.text("dcLinkAdded"));
        // The page shows another source by now: its next read lists the link.
        if (this.detail === shown) {
            shown.edges = shown.edges.concat([edge]);
            this.showRelations();
        }
        return true;
    }

    /** Asks before a link an admin drew is removed: no import brings it back. */
    public onRemoveLink(event: Event): void {
        const row = (event.getSource() as Control).getBindingContext("src")?.getObject() as RelationRow | undefined;
        if (!row || !row.curated) {
            return;
        }
        MessageBox.confirm(this.text("dcLinkRemoveConfirm", [row.from, this.formatRelationKind(row.kind), row.to]), {
            title: this.text("dcLinkRemoveTitle"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.removeLink(row.id);
                }
            }
        });
    }

    /** Removes a drawn link; resolves whether it is gone. One already gone
     *  (404) leaves the list too; an imported one is refused by the server. */
    public async removeLink(id: number | string): Promise<boolean> {
        const shown = this.detail;
        let key = "dcLinkRemoved";
        try {
            await this.withBusy(() => this.getAdminService().deleteDataEdge(id));
        } catch (error) {
            if (!(error instanceof AdminError && error.status === 404)) {
                ErrorHandler.handle(error, this.text("dcLinkRemoveFailed"));
                return false;
            }
            key = "dcLinkAlreadyRemoved";
        }
        MessageToast.show(this.text(key));
        if (shown && this.detail === shown) {
            shown.edges = shown.edges.filter((edge: DataEdge) => String(edge.id) !== String(id));
            this.showRelations();
        }
        return true;
    }

    // --- test search --------------------------------------------------------

    public onRunSearch(): void {
        void this.runSearch();
    }

    /** Runs the retrieval for the typed question; resolves whether it answered. */
    public async runSearch(): Promise<boolean> {
        const question = String(this.search().getProperty("/question") ?? "").trim();
        if (!question || this.search().getProperty("/running")) {
            return false;
        }
        const agent = String(this.search().getProperty("/agent") ?? "");
        this.search().setProperty("/running", true);
        try {
            const result = await this.run(
                this.getAdminService().searchDataCatalog(question, agent || undefined),
                this.text("dcSearchFailed"));
            if (!result) {
                return false;
            }
            this.search().setProperty("/candidates", dataCatalog.candidateRows(result.candidates));
            this.search().setProperty("/scope", result.scope);
            this.search().setProperty("/ran", true);
            return true;
        } finally {
            this.search().setProperty("/running", false);
        }
    }

    // --- navigation ---------------------------------------------------------

    public onBack(): void {
        this.getRouter().navTo("dataCatalog");
    }

    public onShowGraph(): void {
        this.getRouter().navTo("dataCatalogGraph");
    }
}
