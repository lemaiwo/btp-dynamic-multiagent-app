import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import Device from "sap/ui/Device";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import dataCatalog, { LINK_KINDS, type LinkCandidate, type LinkStart } from "../../model/dataCatalog";
import type AdminService from "../../service/AdminService";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type View from "sap/ui/core/mvc/View";
import type Dialog from "sap/m/Dialog";
import type List from "sap/m/List";
import type SearchField from "sap/m/SearchField";
import type { ListBase$SelectionChangeEvent } from "sap/m/ListBase";
import type { DataEdge, DataLinkKind } from "../../service/dataCatalogTypes";

/** What the dialog needs from the detail page that opens it. */
export interface LinkDialogContext {
    /** The source the page shows: its nodes are no candidate. */
    source: { kind: string; name: string };
    /** The nodes of that source a link can start at (`dataCatalog.linkStarts`). */
    starts: LinkStart[];
    service: AdminService;
    text: (key: string, args?: (string | number)[]) => string;
}

/**
 * The Add link dialog of the data catalogue detail: a node of this source
 * (From), the kind of link, and a node of another source found by a search
 * over the graph route (entities, InA queries and functions only). Add sends
 * `POST data-catalog/edges`; `open` answers the stored edge, or nothing when
 * the dialog was closed without one (Cancel, Escape, a navigation). A refusal
 * of the server (the pair is already linked by that kind, a node is gone
 * since the search) is said in the dialog, which stays open.
 *
 * A plain class, not a controller: it is the controller object of its
 * fragment only.
 */
export default class LinkDialog {

    private dialog?: Dialog;
    private view!: View;
    private readonly model = new JSONModel();
    private context!: LinkDialogContext;
    private resolve?: (edge: DataEdge | undefined) => void;
    /** What `open` answers once the dialog has closed. */
    private result?: DataEdge;
    /** Counts the searches: the answer of an earlier one is dropped. */
    private searches = 0;

    public async open(view: View, context: LinkDialogContext): Promise<DataEdge | undefined> {
        this.view = view;
        this.context = context;
        if (!this.dialog) {
            this.dialog = await Fragment.load({
                id: view.getId(), name: "com.agent.admin.fragment.DataCatalogLinkDialog", controller: this
            }) as Dialog;
            this.dialog.setModel(this.model, "link");
            // Answered when the dialog has closed, whoever closed it.
            this.dialog.attachAfterClose(() => {
                const resolve = this.resolve;
                const result = this.result;
                this.resolve = undefined;
                this.result = undefined;
                resolve?.(result);
            });
            // A dependent of the view: destroyed with it.
            view.addDependent(this.dialog);
        }
        const text = context.text;
        this.result = undefined;
        this.model.setData({
            starts: context.starts.map((start) => ({
                ...start, kindText: text(`dcNodeKind_${start.kind}`)
            })),
            from: context.starts.length ? context.starts[0].key : "",
            kinds: LINK_KINDS.map((kind) => ({ key: kind, text: text(dataCatalog.relationKindKey(kind)) })),
            kind: LINK_KINDS[0],
            q: "", candidates: [], to: "", searching: false, searched: false, capped: false, error: "", busy: false
        });
        this.candidateList().removeSelections(true);
        this.dialog.setStretch(Device.system.phone === true);
        this.dialog.setInitialFocus(view.byId("dcLinkSearch") as Control);
        this.dialog.open();
        // Without a text: every node of another source that can be linked.
        void this.search("");
        return new Promise((resolve) => {
            this.resolve = resolve;
        });
    }

    private candidateList(): List {
        return this.view.byId("dcLinkCandidates") as List;
    }

    /** Asks the graph route for candidates matching `q`; resolves when the answer is shown or dropped. */
    public async search(q: string): Promise<void> {
        const mine = ++this.searches;
        const text = this.context.text;
        this.model.setProperty("/searching", true);
        this.model.setProperty("/error", "");
        let candidates: LinkCandidate[] = [];
        let capped = false;
        try {
            const graph = await this.context.service.getDataGraph(dataCatalog.linkQuery(q));
            candidates = dataCatalog.linkCandidates(graph, this.context.source);
            capped = graph.truncated === true;
        } catch (error) {
            if (mine !== this.searches) {
                return;
            }
            const reason = ErrorHandler.messageFor(error, "");
            this.model.setProperty("/error", reason
                ? text("dcLinkSearchFailedReason", [reason]) : text("dcLinkSearchFailed"));
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
            }
        }
        if (mine !== this.searches) {
            return;
        }
        this.model.setProperty("/candidates", candidates);
        this.model.setProperty("/capped", capped);
        this.model.setProperty("/searching", false);
        this.model.setProperty("/searched", true);
        // A list item keeps its selected state when the binding hands it
        // another row: nothing stays picked across a new answer.
        this.model.setProperty("/to", "");
        this.candidateList().removeSelections(true);
    }

    public onSearch(event: Event): void {
        void this.search((event.getSource() as SearchField).getValue());
    }

    public onCandidateSelect(event: ListBase$SelectionChangeEvent): void {
        const candidate = event.getParameter("listItem")?.getBindingContext("link")?.getObject() as LinkCandidate | undefined;
        this.model.setProperty("/to", candidate ? String(candidate.id) : "");
        this.model.setProperty("/error", "");
    }

    public onAdd(): void {
        void this.add();
    }

    /** Sends the link; resolves whether it was stored (the dialog then closes). */
    public async add(): Promise<boolean> {
        const from = Number(this.model.getProperty("/from"));
        const to = Number(this.model.getProperty("/to"));
        const kind = this.model.getProperty("/kind") as DataLinkKind;
        if (this.model.getProperty("/busy") === true || !this.dialog?.isOpen() || !from || !to) {
            return false;
        }
        this.model.setProperty("/busy", true);
        this.model.setProperty("/error", "");
        try {
            const edge = await this.context.service.createDataEdge({ from_id: from, to_id: to, kind });
            this.result = edge;
            this.dialog.close();
            return true;
        } catch (error) {
            // The route's refusals are fixed texts that name the field and
            // the rule, never a value: shown as they are.
            if (error instanceof AdminError && (error.status === 409 || error.status === 422) && error.detail) {
                this.model.setProperty("/error", this.context.text("dcLinkRefused", [error.detail]));
            } else {
                ErrorHandler.handle(error, this.context.text("dcLinkAddFailed"));
            }
            return false;
        } finally {
            this.model.setProperty("/busy", false);
        }
    }

    public onCancel(): void {
        this.dialog?.close();
    }
}
