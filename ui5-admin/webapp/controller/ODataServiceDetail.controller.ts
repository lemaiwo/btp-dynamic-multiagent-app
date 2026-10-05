import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import HashChanger from "sap/ui/core/routing/HashChanger";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { InvisibleMessageMode, ValueState } from "sap/ui/core/library";
import ODataController from "./odata/ODataController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import formatter from "../model/formatter";
import odataCatalog, {
    type ODataEntityRow, type ODataErrorField, type ODataErrors, type ODataNewWrite
} from "../model/odataCatalog";
import { canonical } from "../model/runsPanel";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type CheckBox from "sap/m/CheckBox";
import type SearchField from "sap/m/SearchField";
import type SegmentedButton from "sap/m/SegmentedButton";
import type ListBinding from "sap/ui/model/ListBinding";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";
import type {
    ODataDefinition, ODataEntityOp, ODataService, ODataServiceInput, ODataServiceUpdate, ODataUsedBy
} from "../service/types";

const ROUTE = "odataServiceDetail";

/** The route's name for a service that does not exist yet. */
const NEW = "new";

/** The payload fields that have a text field in the General section, in the
 *  order of the form, with the id of that field. */
const FORM_FIELDS: readonly { field: ODataErrorField; id: string }[] = [
    { field: "title", id: "odataTitle" },
    { field: "name", id: "odataName" },
    { field: "purpose", id: "odataPurpose" },
    { field: "not_for", id: "odataNotFor" },
    { field: "destination", id: "odataDestination" },
    { field: "service_path", id: "odataServicePath" }
];

/** What the duplicate dialog holds. */
interface DuplicateState {
    name: string;
    destination: string;
    user_context: boolean;
    /** Field -> message. */
    errors: Record<string, string>;
    /** A refusal that names neither field. */
    error: string;
    /** The form has unsaved changes, which are not part of the copy. */
    unsaved: boolean;
}

/** The i18n key of each entity-set operation's name. */
const OP_TEXT: Record<ODataEntityOp, string> = {
    list: "odataOpList", get: "odataOpGet", create: "odataOpCreate", update: "odataOpUpdate", delete: "odataOpDelete"
};

/** A question to ask before a save. */
interface SaveQuestion {
    title: string;
    text: string;
}

/**
 * One OData service of the catalogue: its header actions, the General
 * section and the entity sets with their operation switches. The definition
 * is loaded and sent back whole with every save; the operations section
 * comes with a later task.
 *
 * @namespace com.agent.admin.controller
 */
export default class ODataServiceDetail extends ODataController {

    /** The name in the route: a service's, or undefined for a new one. */
    private serviceName?: string;

    /** Counts the loads, so that an answer that comes back after the page
     *  moved on to another service is dropped. */
    private loadCount = 0;

    /** A save, duplicate or delete is on its way. */
    private working = false;

    /** The service a save just created: the navigation to its own route
     *  finds it already on the page and does not read it again. */
    private justCreated?: string;

    private duplicateDialog?: Dialog;

    /** The hash this page is shown under, to come back to when the user
     *  leaves by the browser (back button, edited address) with unsaved
     *  changes: the router cannot refuse a hash, only undo it. */
    private shownHash = "";

    /** The hash was just put back to `shownHash`: the route that matches
     *  next is this page again and must not load anything. */
    private restoring = false;

    /** Where the user was going when the hash was put back. */
    private wantedHash = "";

    private readonly onBeforeUnload = (event: BeforeUnloadEvent): void => {
        if (this.isDirty()) {
            // The browser's own "leave site?" question; its text is not ours.
            event.preventDefault();
            event.returnValue = "";
        }
    };

    public onInit(): void {
        this.setModel(new JSONModel(this.blankState("")), "svc");
        this.getRouter().getRoute(ROUTE)?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const name = (event.getParameter("arguments") as { serviceName: string }).serviceName;
            if (this.restoring) {
                // Back on the page the user tried to leave; it is as it was.
                this.restoring = false;
                this.getOwnerComponentTyped().setLeaveGuard(() => this.confirmLeave());
                this.askAboutLeaving();
                return;
            }
            if (this.keepUnsaved()) {
                return;
            }
            this.shownHash = HashChanger.getInstance().getHash();
            this.getOwnerComponentTyped().setLeaveGuard(() => this.confirmLeave());
            if (name === this.justCreated) {
                this.justCreated = undefined;
                return;
            }
            this.justCreated = undefined;
            void this.load(name);
        });
        this.getRouter().attachRouteMatched(this.onAnyRouteMatched, this);
        // A hash that matches no route fires no routeMatched: without this
        // the form and its guard would live on behind the not-found page.
        this.getRouter().attachBypassed(this.onLeft, this);
        window.addEventListener("beforeunload", this.onBeforeUnload);
    }

    public onExit(): void {
        window.removeEventListener("beforeunload", this.onBeforeUnload);
        this.getRouter().detachRouteMatched(this.onAnyRouteMatched, this);
        this.getRouter().detachBypassed(this.onLeft, this);
        this.getOwnerComponentTyped().setLeaveGuard(undefined);
    }

    private onAnyRouteMatched(event: Router$RouteMatchedEvent): void {
        if (event.getParameter("name") !== ROUTE) {
            this.onLeft();
        }
    }

    /** Another page is shown (or the not-found page): this one no longer
     *  has a say about leaving, and whatever it held is not a form any
     *  more -- unless it held unsaved changes, then it comes back and asks. */
    private onLeft(): void {
        if (this.keepUnsaved()) {
            return;
        }
        this.restoring = false;
        this.justCreated = undefined;
        this.getOwnerComponentTyped().setLeaveGuard(undefined);
        this.loadCount++;
        this.serviceName = undefined;
        this.shownHash = "";
        this.svc().setData(this.blankState(""));
    }

    /**
     * The address changed under a form with unsaved changes (the browser's
     * back button, an edited hash); the page's own ways out have asked
     * before they navigate and never get here with changes.
     *
     * The router cannot refuse a hash, so the old one is put back, which
     * shows this page again with its input, and the question is asked from
     * there. "Discard" then goes where the user wanted to go. Returns
     * whether it took over.
     */
    private keepUnsaved(): boolean {
        const hashChanger = HashChanger.getInstance();
        const wanted = hashChanger.getHash();
        if (!this.shownHash || wanted === this.shownHash || !this.isDirty()) {
            return false;
        }
        this.restoring = true;
        this.wantedHash = wanted;
        hashChanger.replaceHash(this.shownHash);
        return true;
    }

    /**
     * Asks the question once this page is shown again. Not earlier, and not
     * in the same tick: the router closes every open dialog when it
     * navigates, and it is still navigating back here.
     */
    private askAboutLeaving(): void {
        const wanted = this.wantedHash;
        setTimeout(() => {
            void this.confirmLeave().then((leave) => {
                if (leave) {
                    HashChanger.getInstance().setHash(wanted);
                }
            });
        }, 0);
    }

    // --- state --------------------------------------------------------------

    private svc(): JSONModel {
        return this.getModel("svc") as JSONModel;
    }

    /**
     * The page before anything is loaded: no form, no tags, nothing to save.
     *
     * `loaded`: the form shows a service (or a new one) and can be saved.
     * `exists`: that service is stored, so it has tags and can be duplicated
     * and deleted. `original`: the service as stored, for the tags, the
     * unsaved-changes check and the question about a changed identity.
     */
    private blankState(title: string): Record<string, unknown> {
        return {
            title: title || this.text("odataServiceTitle"),
            isNew: false, loaded: false, exists: false, busy: false,
            loadFailed: false, loadError: "",
            data: odataCatalog.emptyService(), original: odataCatalog.emptyService(),
            errors: {}, saveError: "", used_by: [], has_write: false,
            // `rows`: what the entity sets table shows, one flat row per
            // entity set of `data.definition` (`odataCatalog.entitySetRow`).
            // `pendingWrites`: what the ticked, unsaved writes will allow.
            // `asking`: a question about the save is open.
            rows: [], entitySearch: "", pendingWrites: "", asking: false,
            // `updated_at`: the version of the service the form was loaded
            // from; a save is only made on top of that one.
            // `changedElsewhere` / `deletedElsewhere`: it is not the stored
            // version any more, or the service is gone.
            updated_at: null, changedElsewhere: false, deletedElsewhere: false,
            // U6 fills this with the result of "Test call".
            test: null,
            duplicate: { name: "", destination: "", user_context: false, errors: {}, error: "", unsaved: false }
        };
    }

    /**
     * Puts a stored service on the page, as loaded or as just saved.
     * `keepSearch`: the entity sets stay filtered as they were (after a
     * save the admin is still working on the same rows).
     */
    private show(service: ODataService, keepSearch = false): void {
        const search = keepSearch ? this.svc().getProperty("/entitySearch") as string : "";
        this.serviceName = service.name;
        this.svc().setData({
            ...this.blankState(service.title),
            loaded: true, exists: true,
            data: odataCatalog.payloadOf(service), original: odataCatalog.payloadOf(service),
            used_by: service.used_by ?? [], has_write: service.has_write === true,
            updated_at: service.updated_at ?? null,
            rows: odataCatalog.entitySetRows(service.definition),
            entitySearch: search
        });
        this.filterEntitySets(search);
    }

    private data(): ODataServiceInput {
        return this.svc().getProperty("/data") as ODataServiceInput;
    }

    private original(): ODataServiceInput {
        return this.svc().getProperty("/original") as ODataServiceInput;
    }

    private isDirty(): boolean {
        return this.svc().getProperty("/loaded") === true && canonical(this.data()) !== canonical(this.original());
    }

    private setWorking(working: boolean): void {
        this.working = working;
        this.svc().setProperty("/busy", working);
    }

    // --- load ---------------------------------------------------------------

    /**
     * Shows the service `name`, or an empty form for `new`.
     *
     * A service that cannot be read leaves the page without a form and says
     * so with a retry; a lapsed session or a missing scope additionally gets
     * the central dialog.
     */
    private async load(name: string): Promise<void> {
        const count = ++this.loadCount;
        const model = this.svc();

        if (name === NEW) {
            this.serviceName = undefined;
            model.setData({ ...this.blankState(this.text("odataNewService")), isNew: true, loaded: true });
            this.filterEntitySets("");
            return;
        }

        this.serviceName = name;
        model.setData(this.blankState(""));
        try {
            const service = await this.withBusy(() => this.getAdminService().getODataService(name));
            if (count === this.loadCount) {
                this.show(service);
            }
        } catch (error) {
            if (count !== this.loadCount) {
                return;
            }
            const reason = ErrorHandler.messageFor(error, "");
            model.setProperty("/loadError", reason
                ? this.text("odataDetailLoadFailedReason", [name, reason])
                : this.text("odataDetailLoadFailed", [name]));
            model.setProperty("/loadFailed", true);
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
            }
        }
    }

    public onRetry(): void {
        if (this.serviceName) {
            void this.load(this.serviceName);
        }
    }

    /** Reads the service again after it was changed elsewhere. The strip
     *  that offers this says that the input on the page is lost by it. */
    public onReload(): void {
        if (this.serviceName && !this.working) {
            void this.load(this.serviceName);
        }
    }

    /**
     * The service was deleted elsewhere: turns the page into the form of a
     * new service that holds the same input, so that Save creates it again
     * (the name can be changed, too).
     */
    public onSaveAsNew(): void {
        const model = this.svc();
        model.setProperty("/deletedElsewhere", false);
        model.setProperty("/exists", false);
        model.setProperty("/isNew", true);
        model.setProperty("/used_by", []);
        model.setProperty("/updated_at", null);
        model.setProperty("/original", odataCatalog.emptyService());
        model.setProperty("/title", this.text("odataNewService"));
        this.showPendingWrites();
    }

    // --- formatters ---------------------------------------------------------

    /** A field with a message is in error. */
    public formatErrorState(message: string | undefined): ValueState {
        return message ? ValueState.Error : ValueState.None;
    }

    /** "44 / 200", counting what the server counts. */
    public formatPurposeCounter(purpose: string | undefined): string {
        return this.text("odataPurposeCounter", [odataCatalog.purposeLength(purpose)]);
    }

    /** Over the limit, the counter says so before Save does. */
    public formatPurposeCounterState(purpose: string | undefined): ValueState {
        return odataCatalog.purposeLength(purpose) > odataCatalog.MAX_PURPOSE ? ValueState.Error : ValueState.None;
    }

    public formatRunsAsKey(userContext: boolean | undefined): string {
        return userContext ? "user" : "technical";
    }

    /** What the chosen identity means for who sees what, and where it works. */
    public formatRunsAsHint(userContext: boolean | undefined): string {
        return this.text(userContext ? "odataRunsAsUserHint" : "odataRunsAsTechnicalHint");
    }

    /** Under the name: how to form it, or why it can no longer be changed. */
    public formatNameHint(isNew: boolean | undefined): string {
        return this.text(isNew ? "odataNameHint" : "odataNameImmutable");
    }

    /** "Entity sets (5)". */
    public formatEntitySetsTitle(count: number | undefined): string {
        return this.text("odataEntitySets", [count ?? 0]);
    }

    /** When the metadata was last read from SAP, or that it never was. */
    public formatMetadataInfo(fetchedAt: string | null | undefined): string {
        return fetchedAt
            ? this.text("odataMetadataRead", [formatter.timestamp(fetchedAt)])
            : this.text("odataMetadataNever");
    }

    /** The technical name, with the URL segment when that is another one. */
    public formatEntityTechnical(name: string | undefined, path: string | undefined): string {
        return path ? this.text("odataEntityPath", [name ?? "", path]) : (name ?? "");
    }

    /** "Update Requisition item": what a checkbox switches. */
    public formatOperationOf(operation: string | undefined, title: string | undefined): string {
        return this.text("odataOperationOf", [operation ?? "", title ?? ""]);
    }

    /** The same for a write column, which says that it is one. */
    public formatWriteOperationOf(operation: string | undefined, title: string | undefined): string {
        return this.text("odataWriteOperationOf", [operation ?? "", title ?? ""]);
    }

    /** A ticked write stands out; an unticked one is a plain box. */
    public formatWriteState(enabled: boolean | undefined): ValueState {
        return enabled ? ValueState.Warning : ValueState.None;
    }

    /** No description is a warning once agents can find the entity set. */
    public formatNoDescriptionState(anyOperation: boolean | undefined): ValueState {
        return anyOperation ? ValueState.Warning : ValueState.None;
    }

    /** "24 of 89": readable fields of all. */
    public formatFieldsCount(selectable: number | undefined, total: number | undefined): string {
        return this.text("odataFieldsCount", [selectable ?? 0, total ?? 0]);
    }

    public formatFieldsCountTooltip(selectable: number | undefined, total: number | undefined): string {
        return this.text("odataFieldsCountTooltip", [selectable ?? 0, total ?? 0]);
    }

    /** Why the table is empty: nothing there, or nothing that matches. */
    public formatNoEntitySets(search: string | undefined): string {
        return this.text((search ?? "").trim() ? "odataNoEntityMatches" : "odataNoEntitySets");
    }

    /** "Add" until the definition is as large as the server takes it. */
    public formatCanAddEntitySet(busy: boolean | undefined, count: number | undefined): boolean {
        return !busy && (count ?? 0) < odataCatalog.MAX_ENTITY_SETS;
    }

    // --- entity sets --------------------------------------------------------

    private definition(): ODataDefinition {
        return this.data().definition;
    }

    private rows(): ODataEntityRow[] {
        return this.svc().getProperty("/rows") as ODataEntityRow[];
    }

    /** Works the table rows out again from the definition, all of them. */
    private showEntitySets(): void {
        this.svc().setProperty("/rows", odataCatalog.entitySetRows(this.definition()));
        this.showPendingWrites();
    }

    /** "Update on "Requisition item" (A_PurchaseRequisitionItem); ..." */
    private writeList(writes: ODataNewWrite[]): string {
        return writes.map((write) => this.text("odataWriteItem", [
            write.operations.map((op) => this.text(OP_TEXT[op])).join(", "), write.title, write.name
        ])).join("; ");
    }

    /**
     * The write operations saving the form over `stored` would newly open.
     * Switching a stored, disabled service on opens all of its writes.
     */
    private newWrites(stored: ODataServiceInput): ODataNewWrite[] {
        return odataCatalog.newWrites(stored.definition, this.definition(), this.switchesOn(stored));
    }

    /** Whether saving the form switches the stored, disabled service on. */
    private switchesOn(stored: ODataServiceInput): boolean {
        return stored.enabled === false && this.data().enabled !== false
            && this.svc().getProperty("/isNew") !== true;
    }

    /**
     * Says what the ticked, unsaved writes will allow, as long as there are
     * any: in the strip that stays in view next to Save, and -- when it
     * changes because of what the admin just did (`announce`) -- to a
     * screen reader. So an admin learns what a tick means when it is set,
     * wherever in the table that was; Save asks about it again.
     */
    private showPendingWrites(announce = false): void {
        const model = this.svc();
        const writes = model.getProperty("/loaded") === true ? this.newWrites(this.original()) : [];
        const text = writes.length ? this.text("odataPendingWrites", [this.writeList(writes)]) : "";
        const changed = text !== model.getProperty("/pendingWrites");
        model.setProperty("/pendingWrites", text);
        if (announce && changed && text) {
            InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
        }
    }

    /**
     * Puts a checkbox back to what its row holds. The boxes are bound one
     * way, so a click that is not taken would otherwise stay on screen: an
     * unticked box over an operation that is still on, and saved as on.
     */
    private resetBox(box: CheckBox, op: ODataEntityOp): void {
        const row = box.getBindingContext("svc")?.getObject() as ODataEntityRow | undefined;
        box.setSelected(row ? row[op] === true : false);
    }

    /**
     * A checkbox of the table was clicked: switches that operation of that
     * entity set on or off in the form. Nothing is sent; Save does that.
     *
     * Switching ON is refused, with the reason on the row, when the entity
     * set cannot carry the operation (no key, no readable or no writable
     * field): the server would refuse the whole save for it. Switching off
     * is always taken. Whenever the click is not taken, the box is put back.
     */
    public onToggleOperation(event: Event): void {
        const box = event.getSource() as CheckBox;
        const op = box.data("op") as ODataEntityOp;
        const enable = box.getSelected();
        const model = this.svc();
        const row = box.getBindingContext("svc")?.getObject() as ODataEntityRow | undefined;
        if (!row || odataCatalog.ENTITY_OPS.indexOf(op) === -1
            || this.working || model.getProperty("/asking") === true) {
            // Not while a save is on its way or being asked about: what was
            // checked and confirmed must be what is sent.
            this.resetBox(box, op);
            return;
        }
        const entitySet = this.definition().entity_sets[row.index];
        if (!entitySet || entitySet.name !== row.name) {
            // The row is not (or no longer) the entity set at its position:
            // the switch would land on another one. Nothing changes; the
            // table is worked out again and the box shows what is there.
            this.showEntitySets();
            this.resetBox(box, op);
            return;
        }
        const path = `/rows/${row.index}`;
        const refusal = enable ? odataCatalog.operationRefusal(entitySet, op) : "";
        if (refusal) {
            const reason = this.text(refusal);
            model.setProperty(`${path}/note`, reason);
            this.resetBox(box, op);
            InvisibleMessage.getInstance().announce(reason, InvisibleMessageMode.Assertive);
            return;
        }
        entitySet.operations = odataCatalog.toggleOperation(entitySet.operations ?? [], op, enable);
        // The row anew: its note and what a refused save said are about
        // the entity set as it was.
        model.setProperty(path, odataCatalog.entitySetRow(entitySet, row.index));
        model.setProperty("/saveError", "");
        this.showPendingWrites(true);
    }

    /** Shows all entity sets again and empties the search field. */
    private clearEntitySearch(): void {
        this.svc().setProperty("/entitySearch", "");
        this.filterEntitySets("");
    }

    /**
     * Shows the entity sets whose title, technical name or description
     * holds `query`. A filter on the binding: no row is worked out again,
     * and only the rows on screen are rendered.
     */
    private filterEntitySets(query: string): void {
        const binding = this.byId("odataEntityTable")?.getBinding("items") as ListBinding | undefined;
        const text = query.trim();
        binding?.filter(text ? new Filter({
            filters: ["title", "name", "description"].map((path) => new Filter(path, FilterOperator.Contains, text)),
            and: false
        }) : []);
    }

    public onEntitySearch(event: Event): void {
        this.filterEntitySets((event.getSource() as SearchField).getValue());
    }

    /**
     * Adds an entity set by hand, with nothing enabled, and opens it.
     * It gets a free technical name; the dialog is where that is changed.
     */
    public onAddEntitySet(): void {
        const model = this.svc();
        const entitySets = this.definition().entity_sets;
        if (this.working || model.getProperty("/asking") === true
            || entitySets.length >= odataCatalog.MAX_ENTITY_SETS) {
            return;
        }
        entitySets.push(odataCatalog.emptyEntitySet(
            odataCatalog.newEntitySetName(entitySets.map((entitySet) => entitySet.name))
        ));
        // Positions changed meaning for nobody, but what a refused save
        // said was about another list: all rows anew, and none hidden.
        this.clearEntitySearch();
        this.showEntitySets();
        model.setProperty("/saveError", "");
        this.openEntitySet(entitySets.length - 1);
    }

    /** A row of the table was pressed. */
    public onOpenEntitySet(event: Event): void {
        const row = (event.getSource() as Control).getBindingContext("svc")?.getObject() as ODataEntityRow | undefined;
        if (row) {
            this.openEntitySet(row.index);
        }
    }

    /**
     * U5 HOOK -- the entity set dialog (fields, keys, navigations, example
     * queries, title and description) opens from here; it is not built yet,
     * so pressing a row does nothing.
     *
     * `index` is the position in `data.definition.entity_sets`. The dialog
     * edits a copy and, on Apply, writes it back there and calls
     * `showEntitySets()`: the rows, the pending-writes strip and the
     * unsaved-changes check all follow from the definition.
     */
    private openEntitySet(index: number): void {
        void index;
    }

    // --- editing ------------------------------------------------------------

    /** Typing in a field takes its error away, and with it what the last
     *  refused save said above the form; the next save checks again. */
    public onEdit(event: Event): void {
        const field = (event.getSource() as Control).data("field") as string;
        this.svc().setProperty(`/errors/${field}`, "");
        this.svc().setProperty("/saveError", "");
    }

    public onRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/data/user_context", key === "user");
        this.svc().setProperty("/saveError", "");
    }

    public onVersionChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/data/odata_version", key === "v4" ? "v4" : "v2");
        this.svc().setProperty("/saveError", "");
    }

    public onEnabledChange(): void {
        this.svc().setProperty("/saveError", "");
        // Switching a service on opens the writes it has.
        this.showPendingWrites(true);
    }

    // --- save ---------------------------------------------------------------

    /**
     * Checks the form and saves it.
     *
     * An existing service is read again first. If it was saved elsewhere
     * since the form loaded it, nothing is sent: a PUT replaces the whole
     * service, definition included, and would undo that. And the agents
     * that use it are taken from that fresh answer: a change of identity or
     * destination changes who THEY act as in SAP, and a newly ticked write
     * operation is one THEY can run from then on, so either is confirmed
     * with their names, including an agent that was attached a minute ago.
     */
    public async onSave(): Promise<void> {
        const model = this.svc();
        if (this.working || model.getProperty("/asking") === true || model.getProperty("/loaded") !== true) {
            return;
        }
        model.setProperty("/saveError", "");
        // Both checks run, so that everything wrong is marked at once.
        const general = this.showProblems(odataCatalog.validate(this.data()));
        const entitySets = this.showEntityProblems();
        if (!general || !entitySets) {
            return;
        }
        if (model.getProperty("/isNew") === true) {
            // No agent uses a service that does not exist yet; its write
            // operations are asked about all the same.
            this.askAndSave(this.saveQuestion(this.original(), []));
            return;
        }
        if (!model.getProperty("/updated_at")) {
            // Without the version the form was loaded from, neither this
            // page nor the server can tell that the service was changed
            // elsewhere; saving would be a blind overwrite.
            model.setProperty("/saveError", this.text("odataCannotVerify"));
            return;
        }

        const fresh = await this.freshService();
        if (fresh) {
            // Against the service as it is stored NOW, not as the form
            // loaded it: that is what the save replaces.
            this.askAndSave(this.saveQuestion(odataCatalog.payloadOf(fresh), fresh.used_by ?? []));
        }
    }

    /** Saves, after asking `question` when there is one. */
    private askAndSave(question: SaveQuestion | undefined): void {
        const model = this.svc();
        if (!question) {
            void this.save();
            return;
        }
        const save = this.text("save");
        // Until it is answered there is nothing to save or to tick.
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

    /**
     * Reads the stored service again and answers it -- or nothing, when the
     * save must not go on: the service is gone, was changed elsewhere,
     * carries no version to compare, or could not be read. Each of those is
     * said on the page.
     */
    private async freshService(): Promise<ODataService | undefined> {
        const model = this.svc();
        const name = this.serviceName as string;
        this.setWorking(true);
        let fresh: ODataService;
        try {
            fresh = await this.withBusy(() => this.getAdminService().getODataService(name));
        } catch (error) {
            this.showNotRead(error);
            return undefined;
        } finally {
            this.setWorking(false);
        }
        if (name !== this.serviceName) {
            return undefined;
        }
        if (!fresh.updated_at) {
            // Not "changed elsewhere": nobody knows. The PUT could not be
            // checked by the server either.
            model.setProperty("/saveError", this.text("odataCannotVerify"));
            return undefined;
        }
        if (fresh.updated_at !== model.getProperty("/updated_at")) {
            model.setProperty("/changedElsewhere", true);
            return undefined;
        }
        model.setProperty("/changedElsewhere", false);
        model.setProperty("/used_by", fresh.used_by ?? []);
        return fresh;
    }

    /**
     * The read before a save failed, so nothing was sent. A service that is
     * gone gets its own strip; anything else (a server error, no answer) is
     * said above the form with the reason, and a lapsed session or a
     * missing scope additionally gets the central dialog.
     */
    private showNotRead(error: unknown): void {
        if (error instanceof AdminError && error.status === 404) {
            this.svc().setProperty("/deletedElsewhere", true);
            return;
        }
        const reason = ErrorHandler.messageFor(error, "");
        this.svc().setProperty("/saveError", reason
            ? this.text("odataSaveNotReadReason", [reason]) : this.text("odataSaveNotRead"));
        const kind = ErrorHandler.classify(error);
        if (kind === "session" || kind === "forbidden") {
            ErrorHandler.handle(error);
        }
    }

    /**
     * Marks the entity sets the server would refuse (`definitionProblems`),
     * each on its row and in the user's language, and names them above the
     * form; returns whether there are none. Call after `showProblems`,
     * which sets what is said above the form.
     */
    private showEntityProblems(): boolean {
        const model = this.svc();
        const problems = odataCatalog.definitionProblems(this.definition());
        const byIndex: Record<number, string> = {};
        problems.forEach((problem) => {
            byIndex[problem.index] = this.text(problem.key, problem.args);
        });
        this.showRowErrors(byIndex);
        if (problems.length) {
            // A marked row must be on screen, and named so that it is found.
            this.clearEntitySearch();
            const rows = this.rows();
            const names = problems.map((problem) => odataCatalog.entityLabel(rows[problem.index])).join(", ");
            const above = model.getProperty("/saveError") as string;
            model.setProperty("/saveError", [above, this.text("odataEntityProblems", [names])].filter(Boolean).join(" "));
        }
        return problems.length === 0;
    }

    /** Puts `errors` (row position -> text) on the rows and takes every
     *  other row's error away. Only rows that change are touched. */
    private showRowErrors(errors: Record<number, string>): void {
        const model = this.svc();
        this.rows().forEach((row, index) => {
            const error = errors[index] ?? "";
            if (row.error !== error) {
                model.setProperty(`/rows/${index}/error`, error);
            }
        });
    }

    /**
     * Puts what `validate` found on the form; returns whether it is clean.
     * A problem with a field of its own goes to that field, in the user's
     * language; anything else is listed above the form.
     */
    private showProblems(problems: ODataErrors): boolean {
        const errors: Record<string, string> = {};
        const others: string[] = [];
        (Object.keys(problems) as ODataErrorField[]).forEach((field) => {
            const message = this.text(problems[field] as string);
            if (FORM_FIELDS.some((entry) => entry.field === field)) {
                errors[field] = message;
            } else {
                others.push(`${field}: ${message}`);
            }
        });
        this.svc().setProperty("/errors", errors);
        this.svc().setProperty("/saveError", others.join(" "));
        this.focusFirstError(errors);
        return Object.keys(problems).length === 0;
    }

    private focusFirstError(errors: Record<string, string>): void {
        const first = FORM_FIELDS.filter((entry) => errors[entry.field])[0];
        if (first) {
            (this.byId(first.id) as Control | undefined)?.focus();
        }
    }

    /**
     * What has to be confirmed before saving the form over `stored` (the
     * service as it is stored now; an empty one for a new service), or
     * nothing when the save changes none of it:
     *
     * - who the agents in `usedBy` act as in SAP (identity, destination) --
     *   only when there are such agents;
     * - which write operations the catalogue newly lets agents run -- always,
     *   also when no agent uses the service yet: the next agent attached
     *   with "Allow writes" gets them without this page being opened again.
     *
     * One question for both.
     */
    private saveQuestion(stored: ODataServiceInput, usedBy: ODataUsedBy[]): SaveQuestion | undefined {
        const change = odataCatalog.identityChange(stored, this.data());
        const identity = usedBy.length > 0 && (!!change.runsAs || !!change.destination);
        const writes = this.newWrites(stored);
        if (!identity && !writes.length) {
            return undefined;
        }
        const names = usedBy.map((used) => used.agent);
        const parts: string[] = [];
        if (names.length) {
            parts.push(names.length === 1
                ? this.text("odataIdentityAgentsOne", [names[0]])
                : this.text("odataIdentityAgentsMany", [names.join(", ")]));
        }
        if (identity && change.runsAs) {
            // No arguments: these two texts carry an apostrophe.
            parts.push(this.text(change.runsAs === "technical" ? "odataIdentityToTechnical" : "odataIdentityToUser"));
        }
        if (identity && change.destination) {
            parts.push(this.text("odataIdentityDestination", [change.destination.from, change.destination.to]));
        }
        if (writes.length) {
            if (this.switchesOn(stored)) {
                parts.push(this.text("odataWriteSwitchedOn"));
            }
            parts.push(this.text("odataWriteSaveIntro", [this.writeList(writes)]));
            // Only an agent whose server entry allows writes can run them;
            // the others are named too, so nobody has to guess.
            const { allowed, others } = odataCatalog.writers(usedBy);
            if (!names.length) {
                parts.push(this.text("odataWriteNoAgents"));
            }
            if (allowed.length) {
                parts.push(allowed.length === 1
                    ? this.text("odataWriteAllowedOne", [allowed[0]])
                    : this.text("odataWriteAllowedMany", [allowed.join(", ")]));
            }
            if (others.length) {
                parts.push(others.length === 1
                    ? this.text("odataWriteOthersOne", [others[0]])
                    : this.text("odataWriteOthersMany", [others.join(", ")]));
            }
            parts.push(this.text("odataWriteAudited"));
        }
        const title = identity && writes.length ? "odataSaveConfirmTitle"
            : identity ? "odataIdentityConfirmTitle" : "odataWriteConfirmTitle";
        return { title: this.text(title), text: parts.join("\n\n") };
    }

    /**
     * Sends the whole service: the General fields of the form and the
     * definition as it was loaded. The server replaces everything on a PUT
     * and refuses a payload without a definition.
     */
    private async save(): Promise<void> {
        if (this.working) {
            return;
        }
        const isNew = this.svc().getProperty("/isNew") === true;
        const payload: ODataServiceUpdate = odataCatalog.payloadOf(this.data());
        const loadedAt = this.svc().getProperty("/updated_at") as string | null;
        if (!isNew) {
            if (!loadedAt) {
                // `onSave` refuses this already; never a PUT without it.
                this.svc().setProperty("/saveError", this.text("odataCannotVerify"));
                return;
            }
            // The server refuses (409) when the service is no longer the
            // version this form was loaded from; the check before the
            // question cannot rule out a save in the moment between.
            payload.expected_updated_at = loadedAt;
        }
        // The entity sets in the order they are sent: a refusal names them
        // by position.
        const sentNames = payload.definition.entity_sets.map((entitySet) => entitySet.name);
        this.setWorking(true);
        let saved: ODataService;
        try {
            saved = await this.withBusy(() => (isNew
                ? this.getAdminService().createODataService(payload)
                : this.getAdminService().updateODataService(this.serviceName as string, payload)));
        } catch (error) {
            this.setWorking(false);
            this.showRefusal(error, isNew, sentNames);
            return;
        }
        this.setWorking(false);
        this.show(saved, !isNew);
        MessageToast.show(this.text("odataSaved"));
        if (isNew) {
            // Its own route, in place of "new" in the history.
            this.justCreated = saved.name;
            this.getRouter().navTo(ROUTE, { serviceName: saved.name }, true);
        }
    }

    /**
     * Shows why the server did not save.
     *
     * A 422 of the catalogue routes is one string, "<loc>: <msg>; ...": each
     * message goes to the field its `loc` names, and when a `loc` has no
     * field here (the definition, a flag) or the refusal names none at all,
     * the whole text is shown above the form. A `loc` inside the entity
     * sets -- "definition.entity_sets.1: ..." -- additionally marks the row
     * it names (`sentNames`: the entity sets as they were sent). The texts
     * are the server's, in English, and are shown as text. A 409 on create
     * is about the name.
     *
     * On an existing service, a 404 means it was deleted elsewhere and a
     * 409 that it was changed elsewhere: both get a strip of their own that
     * says what can be done, and the input stays.
     */
    private showRefusal(error: unknown, isNew: boolean, sentNames: string[] = []): void {
        const model = this.svc();
        if (error instanceof AdminError && error.status === 422) {
            const byLoc = odataCatalog.serverErrors(error.detail);
            // Only while the table still lists what was sent, row for row.
            const rows = this.rows();
            const placed = canonical(rows.map((row) => row.name)) === canonical(sentNames)
                ? odataCatalog.rowErrors(byLoc, sentNames) : {};
            this.showRowErrors(placed);
            const marked = Object.keys(placed).length > 0;
            if (marked) {
                // A marked row must be on screen, whatever the search hid.
                this.clearEntitySearch();
            }
            // With a row marked, the text above the form names the entity
            // set in place of its position in the definition.
            const answer = marked ? odataCatalog.refusalLines(byLoc, rows).join("; ") : error.detail;
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
                ? (answer ? this.text("odataSaveRefused", [answer]) : this.text("odataSaveFailed"))
                : "");
            this.focusFirstError(errors);
            return;
        }
        if (error instanceof AdminError && error.status === 409 && isNew && error.detail) {
            model.setProperty("/errors", { name: error.detail });
            this.focusFirstError({ name: error.detail });
            return;
        }
        if (error instanceof AdminError && error.status === 404 && !isNew) {
            model.setProperty("/deletedElsewhere", true);
            return;
        }
        if (error instanceof AdminError && error.status === 409 && !isNew) {
            model.setProperty("/changedElsewhere", true);
            model.setProperty("/saveError", error.detail
                ? this.text("odataSaveRefused", [error.detail]) : this.text("odataSaveFailed"));
            return;
        }
        ErrorHandler.handle(error, this.text("odataSaveFailed"));
    }

    // --- duplicate ----------------------------------------------------------

    /** Opens the dialog that asks what the copy differs in: its name, its
     *  destination and its identity. */
    public async onDuplicate(): Promise<void> {
        const model = this.svc();
        if (this.working || model.getProperty("/exists") !== true) {
            return;
        }
        const stored = this.original();
        const state: DuplicateState = {
            name: "", destination: stored.destination, user_context: stored.user_context,
            errors: {}, error: "", unsaved: this.isDirty()
        };
        model.setProperty("/duplicate", state);

        if (!this.duplicateDialog) {
            this.duplicateDialog = await Fragment.load({
                id: this.getView()!.getId(),
                name: "com.agent.admin.fragment.ODataDuplicateDialog",
                controller: this
            }) as Dialog;
            this.getView()!.addDependent(this.duplicateDialog);
        }
        this.duplicateDialog.open();
    }

    public onDuplicateEdit(event: Event): void {
        const field = (event.getSource() as Control).data("field") as string;
        this.svc().setProperty(`/duplicate/errors/${field}`, "");
    }

    public onDuplicateRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/duplicate/user_context", key === "user");
    }

    public onDuplicateCancel(): void {
        this.duplicateDialog?.close();
    }

    /** Creates the copy and opens it. A refusal stays in the dialog. */
    public async onDuplicateConfirm(): Promise<void> {
        const model = this.svc();
        if (this.working || !this.serviceName) {
            return;
        }
        const state = model.getProperty("/duplicate") as DuplicateState;
        const body = { name: state.name, destination: state.destination, user_context: state.user_context };
        const problems = odataCatalog.validateDuplicate(body);
        const errors: Record<string, string> = {};
        (Object.keys(problems) as ODataErrorField[]).forEach((field) => {
            errors[field] = this.text(problems[field] as string);
        });
        model.setProperty("/duplicate/errors", errors);
        model.setProperty("/duplicate/error", "");
        if (Object.keys(errors).length) {
            return;
        }

        this.setWorking(true);
        let copy: ODataService;
        try {
            copy = await this.withBusy(() => this.getAdminService().duplicateODataService(
                this.serviceName as string, body
            ));
        } catch (error) {
            this.setWorking(false);
            this.showDuplicateRefusal(error);
            return;
        }
        this.setWorking(false);
        this.duplicateDialog?.close();
        MessageToast.show(this.text("odataDuplicated"));
        // The copy is what the page shows next; unsaved changes to the
        // source were announced as lost in the dialog.
        this.show(copy);
        this.justCreated = copy.name;
        this.getRouter().navTo(ROUTE, { serviceName: copy.name });
    }

    /** Like `showRefusal`, for the dialog's two fields. */
    private showDuplicateRefusal(error: unknown): void {
        const model = this.svc();
        if (error instanceof AdminError && error.status === 409 && error.detail) {
            model.setProperty("/duplicate/errors", { name: error.detail });
            return;
        }
        if (error instanceof AdminError && error.status === 422) {
            const byLoc = odataCatalog.serverErrors(error.detail);
            const errors: Record<string, string> = {};
            let unplaced = Object.keys(byLoc).length === 0;
            Object.keys(byLoc).forEach((loc) => {
                if (loc === "name" || loc === "destination") {
                    errors[loc] = byLoc[loc];
                } else {
                    unplaced = true;
                }
            });
            model.setProperty("/duplicate/errors", errors);
            model.setProperty("/duplicate/error", unplaced
                ? (error.detail || this.text("odataDuplicateFailed")) : "");
            return;
        }
        ErrorHandler.handle(error, this.text("odataDuplicateFailed"));
    }

    // --- delete -------------------------------------------------------------

    /** Asks, deletes and goes back to the list; the same question, refusal
     *  and toast as on the list (`ODataController`). */
    public onDelete(): void {
        const model = this.svc();
        if (this.working || model.getProperty("/exists") !== true) {
            return;
        }
        const stored = this.original();
        const service = { name: stored.name, title: stored.title };
        this.askDelete(service, () => {
            void this.deleteService(service);
        });
    }

    private async deleteService(service: { name: string; title: string }): Promise<void> {
        if (this.working) {
            return;
        }
        this.setWorking(true);
        let outcome;
        try {
            outcome = await this.removeService(service);
        } finally {
            this.setWorking(false);
        }
        if (outcome === "deleted") {
            // Nothing left to lose: the form goes with the service.
            this.svc().setData(this.blankState(""));
            this.getRouter().navTo("odataServices");
        } else if (outcome === "refused") {
            // Still in use, or gone already: what the page shows may no
            // longer be true. Unsaved edits are kept only if it still is.
            if (!this.isDirty()) {
                await this.load(service.name);
            }
        }
    }

    // --- leaving ------------------------------------------------------------

    public async onBack(): Promise<void> {
        if (await this.confirmLeave()) {
            this.getRouter().navTo("odataServices");
        }
    }

    /**
     * Whether the page may be left: yes when nothing is unsaved, otherwise
     * what the user answers. "Discard" drops the changes, so nothing asks a
     * second time on the way out.
     */
    private confirmLeave(): Promise<boolean> {
        if (!this.isDirty()) {
            return Promise.resolve(true);
        }
        const discard = this.text("odataDiscard");
        return new Promise<boolean>((resolve) => {
            MessageBox.warning(this.text("odataUnsavedLeave"), {
                title: this.text("odataUnsavedTitle"),
                actions: [discard, MessageBox.Action.CANCEL],
                emphasizedAction: discard,
                initialFocus: MessageBox.Action.CANCEL,
                onClose: (action: string | null) => {
                    if (action === discard) {
                        this.svc().setProperty("/data", odataCatalog.payloadOf(this.original()));
                        this.svc().setProperty("/errors", {});
                        this.svc().setProperty("/saveError", "");
                        this.showEntitySets();
                    }
                    resolve(action === discard);
                }
            });
        });
    }
}
