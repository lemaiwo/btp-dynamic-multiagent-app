import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import Device from "sap/ui/Device";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import dataCatalogLinks, { LINK_KINDS, type LinkCandidate, type LinkStart } from "../../model/dataCatalogLinks";
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
    /** The nodes of that source a link can start at (`dataCatalogLinks.linkStarts`). */
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
 * since the search) is said in the dialog, which stays open. While Add is
 * on its way, Cancel and Escape wait; a navigation that closes the dialog
 * meanwhile does not lose the answer: `open` answers once the server has.
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
    /** Add is waiting for the server. */
    private sending = false;
    /** The answer of an `open` whose dialog closed while Add was waiting
     *  (a navigation): that Add gives it, whatever the dialog shows by then. */
    private pending?: (edge: DataEdge | undefined) => void;

    public async open(view: View, context: LinkDialogContext): Promise<DataEdge | undefined> {
        this.view = view;
        this.context = context;
        if (!this.dialog) {
            this.dialog = await Fragment.load({
                id: view.getId(), name: "com.agent.admin.fragment.DataCatalogLinkDialog", controller: this
            }) as Dialog;
            this.dialog.setModel(this.model, "link");
            // Escape is Cancel, which waits while Add is on its way.
            this.dialog.setEscapeHandler((escape) => {
                (escape.reject as () => void)();
                this.onCancel();
            });
            // Answered when the dialog has closed, whoever closed it; closed
            // under a pending Add (a navigation), that Add answers instead.
            this.dialog.attachAfterClose(() => {
                if (this.sending) {
                    this.pending = this.resolve;
                    this.resolve = undefined;
                } else {
                    this.settle();
                }
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
            kinds: LINK_KINDS.map((kind) => ({ key: kind, text: text(dataCatalogLinks.relationKindKey(kind)) })),
            kind: LINK_KINDS[0],
            // Opened again while an earlier Add is on its way: it waits for that one.
            q: "", candidates: [], to: "", searching: false, searched: false, capped: false, error: "", busy: this.sending
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

    /** Answers `open` with what the dialog stored, once. */
    private settle(): void {
        const resolve = this.resolve;
        const result = this.result;
        this.resolve = undefined;
        this.result = undefined;
        resolve?.(result);
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
            const graph = await this.context.service.getDataGraph(dataCatalogLinks.linkQuery(q));
            candidates = dataCatalogLinks.linkCandidates(graph, this.context.source);
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
        const dialog = this.dialog;
        if (this.sending || !dialog?.isOpen() || !from || !to) {
            return false;
        }
        this.sending = true;
        this.model.setProperty("/busy", true);
        this.model.setProperty("/error", "");
        let edge: DataEdge | undefined;
        try {
            edge = await this.context.service.createDataEdge({ from_id: from, to_id: to, kind });
        } catch (error) {
            // The route's refusals are fixed texts that name the field and
            // the rule, never a value: shown as they are, in the dialog they
            // were made in (not in one opened since).
            if (error instanceof AdminError && (error.status === 409 || error.status === 422) && error.detail) {
                if (!this.pending) {
                    this.model.setProperty("/error", this.context.text("dcLinkRefused", [error.detail]));
                }
            } else {
                ErrorHandler.handle(error, this.context.text("dcLinkAddFailed"));
            }
        } finally {
            this.sending = false;
            this.model.setProperty("/busy", false);
        }
        const pending = this.pending;
        if (pending) {
            // Closed while the server was asked: what it stored is answered all the same.
            this.pending = undefined;
            pending(edge);
        } else if (edge) {
            this.result = edge;
            dialog.close();
        }
        return edge !== undefined;
    }

    /** Closes without a link; not while Add is on its way (its link would be stored unseen). */
    public onCancel(): void {
        if (!this.sending) {
            this.dialog?.close();
        }
    }
}
