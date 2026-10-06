import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import MessageBox from "sap/m/MessageBox";
import Device from "sap/ui/Device";
import { InvisibleMessageMode, ValueState } from "sap/ui/core/library";
import odataCatalog, {
    type ODataImportItem, type ODataImportPlan, type ODataImportStatus, type ODataImportSummary
} from "../../model/odataCatalog";
import { AdminError } from "../../service/AdminService";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type View from "sap/ui/core/mvc/View";
import type Dialog from "sap/m/Dialog";
import type SearchField from "sap/m/SearchField";
import type Select from "sap/m/Select";
import type {
    ODataDefinition, ODataEntityOp, ODataMetadataPreview, ODataMetadataRequest, ODataSkippedElement
} from "../../service/types";

/** What the dialog needs from the page that opens it. */
export interface ImportDialogContext {
    /** The definition of the form: what the document is compared with, and
     *  what Apply merges into (a copy; the form itself is the page's). */
    definition: ODataDefinition;
    /** Where the form says the service is, read when "Read metadata" is
     *  pressed; nothing when destination or service path cannot be sent. */
    request: () => ODataMetadataRequest | undefined;
    read: (body: ODataMetadataRequest) => Promise<ODataMetadataPreview>;
    /** A failure the app answers centrally (a lapsed session, a missing
     *  scope): true when it took it. */
    handled: (error: unknown) => boolean;
    text: (key: string, args?: (string | number)[]) => string;
}

/** What Apply answers: the merged definition and when the document was
 *  read. Cancel answers nothing. */
export interface ImportDialogResult {
    definition: ODataDefinition;
    metadata_fetched_at: string;
}

/** A row of the list: a plan item with what the dialog keeps on it. */
type Row = ODataImportItem & { selected?: boolean; expanded?: boolean };

const OP_TEXT: Record<ODataEntityOp, string> = {
    list: "odataOpList", get: "odataOpGet", create: "odataOpCreate", update: "odataOpUpdate", delete: "odataOpDelete"
};
const KIND_TEXT: Record<string, string> = {
    function_import: "odataKindFunctionImport", action: "odataKindAction", function: "odataKindFunction"
};
const STATUS_TEXT: Record<ODataImportStatus, string> = {
    new: "odataImportNew", changed: "odataImportChanged", in_service: "odataImportInService", removed: "odataImportRemoved"
};
const DECLARED_TEXT: Record<string, string> = {
    creatable: "odataImportDeclCreatable", updatable: "odataImportDeclUpdatable", deletable: "odataImportDeclDeletable",
    filterable: "odataImportDeclFilterable"
};
/** A refusal of the metadata route by its stable code (`X-OData-Error`). */
const ERROR_TEXT: Record<string, string> = {
    user_token_required: "odataImportErrUser", proxy_refused: "odataImportErrProxy", unreachable: "odataImportErrUnreachable",
    redirect: "odataImportErrRedirect", not_xml: "odataImportErrNotXml", too_large: "odataImportErrTooLarge",
    timeout: "odataImportErrTimeout", busy: "odataImportErrBusy", preview_failed: "odataImportErrFailed"
};
/** Codes whose text is SAP's or the parser's own: shown after our words. */
const ERROR_WITH_DETAIL: Record<string, string> = {
    sap_error: "odataImportErrSap", invalid_metadata: "odataImportErrInvalid", invalid_path: "odataImportErrPath"
};
const WARNING_TEXT: Record<string, string> = {
    metadata_incomplete: "odataImportWarnIncomplete"
};
/** Why Apply is not taken, by the key `odataCatalog.importSummary` gives. */
const BLOCK_TEXT: Record<string, string> = {
    odataRemoveEntityBound: "odataImportBlockedBound", odataRemoveEntityReturned: "odataImportBlockedReturned"
};
/** With at most this many rows under entity sets, they start opened up. */
const OPEN_UP_TO = 60;
const SKIPPED_SHOWN = 200;

/**
 * The import dialog: reads the `$metadata` document of the service the
 * form describes, lists what it declares against what the form holds, and
 * hands back the definition with what the admin ticked.
 *
 * The dialog sends one request (the read) and stores nothing. What it adds
 * arrives switched off -- no operation enabled, no field ticked, every
 * operation disabled (`odataCatalog.mergeImport`); what the admin wrote is
 * kept; something the document no longer has goes only when its row is
 * ticked. `open` answers the merged definition (Apply) or nothing (Cancel,
 * Escape, a page that was left: `dismiss`); the page writes it into its
 * form, and its Save stores it.
 *
 * Every name, label and message comes from a remote document or from SAP:
 * all of it is bound as text.
 */
export default class ImportDialog {

    private dialog?: Dialog;
    private readonly model = new JSONModel();
    private context!: ImportDialogContext;
    private resolve?: (result: ImportDialogResult | undefined) => void;
    private result?: ImportDialogResult;
    /** The page was left while the dialog was on its way. */
    private dismissed = false;
    /** Counts the reads: an answer for an earlier one, or for a dialog
     *  that was closed meanwhile, is dropped. */
    private readCount = 0;
    private preview?: ODataMetadataPreview;
    private plan?: ODataImportPlan;
    private expanded: Record<string, boolean> = {};

    public async open(view: View, context: ImportDialogContext): Promise<ImportDialogResult | undefined> {
        this.context = context;
        this.dismissed = false;
        if (!this.dialog) {
            const dialog = await Fragment.load({
                id: view.getId(), name: "com.agent.admin.fragment.ODataImportDialog", controller: this
            }) as Dialog;
            // A document can hold 20,000 fields; the list shows them 50 at a time.
            this.model.setSizeLimit(25000);
            dialog.setModel(this.model, "imp");
            dialog.setEscapeHandler((escape) => {
                (escape.reject as () => void)();
                this.onCancel();
            });
            dialog.attachAfterClose(() => {
                const resolve = this.resolve;
                const result = this.result;
                this.resolve = undefined;
                this.result = undefined;
                this.readCount++;
                resolve?.(result);
            });
            view.addDependent(dialog);
            this.dialog = dialog;
        }
        if (this.dismissed) {
            // The page was left during the first load: nothing opens over
            // whatever is shown now.
            return undefined;
        }
        this.result = undefined;
        this.preview = undefined;
        this.plan = undefined;
        this.expanded = {};
        this.model.setData({
            ...this.blank(), where: this.where(context.request())
        });
        this.dialog.setStretch(Device.system.phone === true);
        this.dialog.setInitialFocus(view.byId("importReadButton") as Control);
        this.dialog.open();
        return new Promise((resolve) => {
            this.resolve = resolve;
        });
    }

    /** The dialog before a document is read. */
    private blank(): Record<string, unknown> {
        return {
            reading: false, read: false, error: "", summary: "", truncated: "", removalsUnknown: false,
            warnings: [], skippedTitle: "", skipped: [], skippedMore: "", rows: [], query: "", filter: "all",
            removable: 0, selection: this.context.text("odataImportNothing"), canApply: false, blocked: [],
            noRows: "", where: {}
        };
    }

    /** Where the read goes, as the form has it: shown, not edited here. */
    private where(request: ODataMetadataRequest | undefined): Record<string, unknown> {
        const text = this.context.text;
        return {
            ok: !!request,
            destination: request?.destination ?? "",
            path: request?.service_path ?? "",
            version: request ? text(request.odata_version === "v4" ? "odataVersionV4" : "odataVersionV2") : "",
            identity: request ? text(request.user_context === true ? "odataImportAsUser" : "odataImportAsTechnical") : ""
        };
    }

    private close(result: ImportDialogResult | undefined): void {
        this.result = result;
        this.dialog?.close();
    }

    /** Closes the dialog as Cancel does, without a question: the page it
     *  belongs to is left. Also keeps a dialog that is still being loaded
     *  from opening. */
    public dismiss(): void {
        this.dismissed = true;
        if (this.resolve && this.dialog && !this.dialog.isDestroyed() && this.dialog.isOpen()) {
            this.close(undefined);
        }
    }

    // --- reading ------------------------------------------------------------

    /**
     * Reads the document once. The destination, path, version and identity
     * are the form's at this moment (`context.request`); a refusal is said
     * inside the dialog by its code, and what was listed before stays away:
     * it was about another read.
     */
    public async onRead(): Promise<void> {
        const model = this.model;
        if (model.getProperty("/reading") === true) {
            return;
        }
        const text = this.context.text;
        const request = this.context.request();
        this.preview = undefined;
        this.plan = undefined;
        this.expanded = {};
        model.setData({ ...this.blank(), where: this.where(request) });
        if (!request) {
            this.say("/error", text("odataImportFormIncomplete"));
            return;
        }
        const count = ++this.readCount;
        model.setProperty("/reading", true);
        let preview: ODataMetadataPreview;
        try {
            preview = await this.context.read(request);
        } catch (error) {
            if (count !== this.readCount) {
                return;
            }
            model.setProperty("/reading", false);
            if (!this.context.handled(error)) {
                this.say("/error", this.errorText(error));
            }
            return;
        }
        if (count !== this.readCount) {
            return;
        }
        model.setProperty("/reading", false);
        this.show(preview);
    }

    private say(path: string, message: string): void {
        this.model.setProperty(path, message);
        InvisibleMessage.getInstance().announce(message, InvisibleMessageMode.Assertive);
    }

    /** Why the document was not read: by the stable code of the refusal,
     *  never by its HTTP status (a proxy on the way answers with those too). */
    private errorText(error: unknown): string {
        const text = this.context.text;
        if (!(error instanceof AdminError)) {
            return text("odataImportErrPlain");
        }
        const detail = (error.detail ?? "").trim();
        if (error.code === "destination_error") {
            // The server says what is wrong with an on-premise destination
            // in a fixed text of its own: shown as it is, as text.
            return detail ? text("odataImportErrDestinationSaid", [detail]) : text("odataImportErrDestination");
        }
        if (ERROR_TEXT[error.code]) {
            return text(ERROR_TEXT[error.code]);
        }
        if (ERROR_WITH_DETAIL[error.code]) {
            return text(ERROR_WITH_DETAIL[error.code], [detail]);
        }
        if (error.status === 404) {
            return text("odataImportErrGone");
        }
        return detail ? text("odataImportErrOther", [detail]) : text("odataImportErrPlain");
    }

    /** Puts a document on the dialog: what it holds, what was cut or left
     *  out, and its rows. */
    private show(preview: ODataMetadataPreview): void {
        const text = this.context.text;
        const model = this.model;
        const plan = odataCatalog.importPlan(this.context.definition, preview);
        this.preview = preview;
        this.plan = plan;
        const children = plan.items.filter((item) => item.parent).length;
        if (children <= OPEN_UP_TO) {
            plan.items.forEach((item) => {
                if (item.children) {
                    this.expanded[item.id] = true;
                    (item as Row).expanded = true;
                }
            });
        }
        const counts = plan.counts;
        const totals = preview.totals ?? { entity_sets: counts.entitySets, operations: counts.operations, skipped: 0 };
        const cut = totals.entity_sets > counts.entitySets || totals.operations > counts.operations;
        const skipped = preview.skipped ?? [];
        const skippedTotal = Math.max(totals.skipped ?? 0, skipped.length);
        const summary = text("odataImportFound", [
            counts.entitySets, counts.operations, counts.isNew, counts.changed, counts.inService, counts.removed
        ]);
        model.setProperty("/read", true);
        model.setProperty("/summary", summary);
        model.setProperty("/truncated", cut
            ? text("odataImportTruncated", [totals.entity_sets, totals.operations, counts.entitySets, counts.operations])
            : preview.truncated === true ? text("odataImportTruncatedSets") : "");
        model.setProperty("/removalsUnknown", !plan.removalsKnown);
        model.setProperty("/warnings", (preview.warnings ?? []).map((warning) => ({
            // Our own words for a code we know; the server's for another one.
            text: WARNING_TEXT[warning.code] ? text(WARNING_TEXT[warning.code]) : String(warning.message ?? warning.code)
        })));
        model.setProperty("/skippedTitle", skippedTotal ? text("odataImportSkipped", [skippedTotal]) : "");
        model.setProperty("/skipped", skipped.slice(0, SKIPPED_SHOWN).map((entry) => ({ text: this.skippedText(entry) })));
        model.setProperty("/skippedMore", skippedTotal > Math.min(skipped.length, SKIPPED_SHOWN)
            ? text("odataImportSkippedMore", [skippedTotal - Math.min(skipped.length, SKIPPED_SHOWN)]) : "");
        model.setProperty("/removable", plan.items.filter((item) => item.status === "removed" && item.selectable).length);
        this.showRows();
        this.showSelection();
        InvisibleMessage.getInstance().announce(summary, InvisibleMessageMode.Polite);
    }

    private skippedText(entry: ODataSkippedElement): string {
        const text = this.context.text;
        const kind = text(`odataImportSkipped_${entry.kind}`);
        return entry.entity_set && entry.kind !== "entity_set"
            ? text("odataImportSkippedIn", [kind, entry.position, entry.entity_set, entry.reason])
            : text("odataImportSkippedAt", [entry.entity_set ? `${kind} ${entry.entity_set}` : kind, entry.position, entry.reason]);
    }

    // --- the list -----------------------------------------------------------

    /** The rows for the search text, the filter and what is opened up. */
    private showRows(): void {
        const model = this.model;
        const query = String(model.getProperty("/query") ?? "");
        const filter = String(model.getProperty("/filter") ?? "all") as ODataImportStatus | "all";
        const rows = this.plan ? odataCatalog.importRows(this.plan.items, query, filter, this.expanded) : [];
        model.setProperty("/rows", rows);
        model.setProperty("/noRows", this.context.text(
            this.plan && this.plan.items.length ? "odataImportNoMatch" : "odataImportEmpty"
        ));
    }

    public onSearch(event: Event): void {
        this.model.setProperty("/query", (event.getSource() as SearchField).getValue());
        this.showRows();
    }

    public onFilter(event: Event): void {
        this.model.setProperty("/filter", (event.getSource() as Select).getSelectedKey());
        this.showRows();
    }

    /** Opens up or closes the rows under an entity set. */
    public onToggle(event: Event): void {
        const row = (event.getSource() as Control).getBindingContext("imp")?.getObject() as Row | undefined;
        if (!row) {
            return;
        }
        row.expanded = !row.expanded;
        this.expanded[row.id] = row.expanded;
        this.showRows();
    }

    private selection(): string[] {
        return (this.plan?.items ?? []).filter((item) => (item as Row).selected === true && item.selectable).map((item) => item.id);
    }

    private summary(): ODataImportSummary {
        return odataCatalog.importSummary(this.context.definition, this.preview as ODataMetadataPreview, this.selection());
    }

    /** What Apply will do with the ticks, on its button and beside it, and
     *  what keeps it from being applied. */
    private showSelection(): void {
        const text = this.context.text;
        const model = this.model;
        if (!this.preview) {
            model.setProperty("/selection", text("odataImportNothing"));
            model.setProperty("/canApply", false);
            model.setProperty("/blocked", []);
            return;
        }
        const summary = this.summary();
        // "Add 1 item(s)" for the plain case; every count when something
        // is changed or removed too.
        model.setProperty("/selection", !summary.total ? text("odataImportNothing")
            : summary.change || summary.remove
                ? text("odataImportSelection", [summary.add, summary.change, summary.remove])
                : text("odataImportAdd", [summary.add]));
        model.setProperty("/blocked", summary.blocked.map((block) => ({
            text: text(BLOCK_TEXT[block.key] ?? block.key, block.args)
        })));
        model.setProperty("/canApply", summary.total > 0 && summary.blocked.length === 0);
    }

    public onSelect(): void {
        this.showSelection();
    }

    /** Ticks every row of something the document no longer has. Nothing is
     *  removed by that: Apply does it, and Save stores it. */
    public onSelectRemoved(): void {
        (this.plan?.items ?? []).forEach((item) => {
            if (item.status === "removed" && item.selectable) {
                (item as Row).selected = true;
            }
        });
        this.model.refresh(true);
        this.showSelection();
    }

    // --- formatters: a row is worded when it is shown -----------------------

    public formatName(row: Row | undefined): string {
        if (!row) {
            return "";
        }
        if (row.kind === "labels") {
            return this.context.text("odataImportLabels", [row.count]);
        }
        if (row.kind === "key_change") {
            return this.context.text("odataImportKeyRow");
        }
        if (row.kind === "entity_type_change") {
            return this.context.text("odataImportEntityTypeRow");
        }
        return row.name;
    }

    /** "Purchase requisition item · 89 fields · 4 navigations". */
    public formatHint(row: Row | undefined): string {
        if (!row) {
            return "";
        }
        const text = this.context.text;
        const none = text("odataImportNone");
        const declared = row.declared.length
            ? text("odataImportDeclared", [row.declared.map((key) => text(DECLARED_TEXT[key] ?? key)).join(", ")]) : "";
        let parts: string[] = [];
        switch (row.kind) {
            case "entity_set":
            case "removed_entity_set":
                parts = [
                    row.label,
                    row.truncated && row.fieldsTotal > row.fields
                        ? text("odataImportFieldsOf", [row.fields, row.fieldsTotal]) : text("odataImportFields", [row.fields]),
                    row.navigations ? text("odataImportNavigations", [row.navigations]) : "", declared
                ];
                break;
            case "operation":
            case "removed_operation":
                parts = [
                    row.label, text(KIND_TEXT[row.operationKind] ?? "odataReturnsUnknown"), row.method,
                    text("odataImportParameters", [row.parameters]),
                    row.boundTo ? text("odataImportBound", [row.boundTo]) : ""
                ];
                break;
            case "field":
            case "removed_field":
                parts = [row.label, row.type, declared];
                break;
            case "type_change":
                parts = [row.label, text("odataImportTypeChange", [row.was || none, row.now || none])];
                break;
            case "key_change":
                parts = [text("odataImportKeyChange", [row.was || none, row.now || none])];
                break;
            case "entity_type_change":
                parts = [text("odataImportEntityTypeChange", [row.was || none, row.now || none])];
                break;
            default:
                parts = [text("odataImportLabelsHint")];
        }
        return parts.filter(Boolean).join(" · ");
    }

    /** What ticking the row means, or why it needs attention; "" for a row
     *  that says everything already. */
    public formatNote(row: Row | undefined): string {
        if (!row) {
            return "";
        }
        const text = this.context.text;
        const parts: string[] = [];
        if (row.skippedReason) {
            parts.push(text("odataImportLeftOut", [row.skippedReason]));
        }
        if (row.kind === "entity_set" && row.truncated) {
            parts.push(text("odataImportSetCut", [row.fields, row.fieldsTotal]));
        }
        if (row.needs) {
            parts.push(text("odataImportNeedsSetRow", [row.needs]));
        }
        if (row.kind === "removed_entity_set") {
            parts.push(text("odataImportRemovedSet"));
            if (row.lostOperations.length) {
                parts.push(text("odataImportLostOperations", [row.lostOperations.map((op) => text(OP_TEXT[op])).join(", ")]));
            }
            if (row.lostReadable || row.lostWritable) {
                parts.push(text("odataImportLostFields", [row.lostReadable, row.lostWritable]));
            }
            row.blockers.forEach((blocker) => {
                parts.push(text(BLOCK_TEXT[blocker.key], [row.label || row.name, row.name, blocker.operations.join(", ")]));
            });
        }
        if (row.kind === "removed_field") {
            parts.push(text("odataImportRemovedField"));
            if (row.lostReadable) {
                parts.push(text("odataImportLostFieldRead"));
            }
            if (row.lostWritable) {
                parts.push(text("odataImportLostFieldWrite"));
            }
            if (row.lostKey) {
                parts.push(text("odataImportLostFieldKey"));
            }
        }
        if (row.kind === "removed_operation") {
            parts.push(text("odataImportRemovedOperation"));
            if (row.lostEnabled) {
                parts.push(text("odataImportLostEnabled"));
            }
        }
        return parts.join(" ");
    }

    public formatHasNote(row: Row | undefined): boolean {
        return this.formatNote(row) !== "";
    }

    public formatStatus(row: Row | undefined): string {
        if (!row) {
            return "";
        }
        const text = this.context.text;
        return row.children ? text("odataImportChangedCount", [row.children]) : text(STATUS_TEXT[row.status]);
    }

    public formatStatusState(status: ODataImportStatus | undefined): ValueState {
        return status === "new" ? ValueState.Information
            : status === "changed" ? ValueState.Warning
                : status === "removed" ? ValueState.Error : ValueState.None;
    }

    public formatNoteState(status: ODataImportStatus | undefined): ValueState {
        return status === "removed" ? ValueState.Error : ValueState.Warning;
    }

    public formatToggleIcon(expanded: boolean | undefined): string {
        return expanded ? "sap-icon://navigation-down-arrow" : "sap-icon://navigation-right-arrow";
    }

    public formatToggleTooltip(expanded: boolean | undefined, name: string | undefined, children: number | undefined): string {
        return this.context.text(expanded ? "odataImportHide" : "odataImportShow", [children ?? 0, name ?? ""]);
    }

    /** The accessible name of a row's checkbox: what it is, and its status. */
    public formatTick(row: Row | undefined): string {
        return row ? `${this.formatName(row)} (${this.context.text(STATUS_TEXT[row.status])})` : "";
    }

    public formatRemoveAll(count: number | undefined): string {
        return this.context.text("odataImportSelectRemoved", [count ?? 0]);
    }

    // --- closing ------------------------------------------------------------

    /** Hands the merged definition to the page. Not taken while nothing is
     *  ticked or something blocks it; both are said beside the button. */
    public onApply(): void {
        if (!this.preview || this.model.getProperty("/reading") === true) {
            return;
        }
        const selection = this.selection();
        const summary = this.summary();
        if (!summary.total || summary.blocked.length) {
            this.showSelection();
            return;
        }
        this.close({
            definition: odataCatalog.mergeImport(this.context.definition, this.preview, selection),
            metadata_fetched_at: this.preview.fetched_at
        });
    }

    /** Closes without a result; asks first when rows are ticked. */
    public onCancel(): void {
        const ticked = this.selection().length;
        if (!ticked) {
            this.close(undefined);
            return;
        }
        const discard = this.context.text("odataDiscard");
        const keep = this.context.text("odataKeepEditing");
        MessageBox.warning(this.context.text("odataImportDiscard", [ticked]), {
            title: this.context.text("odataImportTitle"),
            actions: [discard, keep],
            emphasizedAction: keep,
            initialFocus: keep,
            onClose: (action: string | null) => {
                if (action === discard) {
                    this.close(undefined);
                }
            }
        });
    }
}
