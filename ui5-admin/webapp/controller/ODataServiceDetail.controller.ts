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
import EntitySetDialog from "./odata/EntitySetDialog";
import OperationDialog from "./odata/OperationDialog";
import ImportDialog from "./odata/ImportDialog";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import formatter from "../model/formatter";
import odataCatalog, {
    type ODataEntityRow, type ODataErrorField, type ODataErrors, type ODataNewOperation, type ODataOperationRow,
    type ODataPending
} from "../model/odataCatalog";
import odataDestinations, { type DestinationNotice, type DestinationsState } from "../model/odataDestinations";
import { canonical } from "../model/runsPanel";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type Input from "sap/m/Input";
import type { Input$LiveChangeEvent } from "sap/m/Input";
import type Item from "sap/ui/core/Item";
import type SelectDialog from "sap/m/SelectDialog";
import type { SelectDialog$ConfirmEvent, SelectDialog$LiveChangeEvent } from "sap/m/SelectDialog";
import type CheckBox from "sap/m/CheckBox";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Page from "sap/m/Page";
import type Table from "sap/m/Table";
import type SearchField from "sap/m/SearchField";
import type SegmentedButton from "sap/m/SegmentedButton";
import type ListBinding from "sap/ui/model/ListBinding";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";
import type {
    ODataDefinition, ODataEntityOp, ODataEntitySet, ODataMetadataRequest, ODataOperation, ODataService,
    ODataServiceInput, ODataServiceUpdate, ODataTestResult, ODataUsedBy
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
    /** The writes the copy gets, as the dialog lists them, or "". */
    writes: string;
}

/** How many entity sets and operations the strip next to Save names; the
 *  Save question lists them all. */
const STRIP_CAP = 3;
/** What the entity set dialog edits of an entity set: Apply writes these
 *  back and leaves the rest (keys, path, entity type, operations) as the
 *  form holds it. */
const DIALOG_EDITS = ["name", "title", "description", "fields", "navigations", "examples"] as const;

/** A question to ask before a save. */
interface SaveQuestion {
    title: string;
    text: string;
}

/**
 * One OData service of the catalogue: its header actions (the test call
 * among them), the General section, the entity sets with their operation
 * switches, the operations (function imports, actions, functions) and the
 * agents that use the service. The definition is loaded and sent back
 * whole with every save.
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

    /** The list of destinations as a dialog, for a field without a list
     *  under it (on a phone), and the field it was opened for. */
    private destinationPicker?: SelectDialog;
    private destinationPickerLoading?: Promise<SelectDialog>;
    private pickerField?: Input;
    /** What a destination field held when its list was asked for by the
     *  value help; gone with the next key that changes the text, with the
     *  next `change`, and when the field is left. */
    private heldAtValueHelp?: { field: Input; value: string };

    /** What the destination service lists, for the destination field. */
    private destinations: DestinationsState = "loading";

    /** Counts the reads of that list, so that the answer for an earlier
     *  visit of the page is dropped. */
    private destinationsCount = 0;

    /** The dialog of one entity set; it is open while `entityOpen`. */
    private entityDialog?: EntitySetDialog;
    private entityOpen = false;

    /** The entity set "Add" put into the form for the dialog that is open:
     *  it leaves the form again unless that dialog is applied. */
    private addedStub?: ODataEntitySet;

    /** The open dialog was closed by the page (it is being left), not by
     *  the admin: nothing on the page gets the focus for it. */
    private entityDismissed = false;

    /** The dialog of one operation (its name and description), and whether
     *  it is open or on its way. */
    private operationDialog?: OperationDialog;
    private operationOpen = false;

    /** The import dialog, and whether it is open or on its way. */
    private importDialog?: ImportDialog;
    private importOpen = false;

    /** Watches the height of what the slot of the pending-writes strip holds. */
    private slotObserver?: ResizeObserver;

    /** The slot's height just before something the admin did may change
     *  it (`keepPlace`); undefined when nothing is to be held in place. */
    private slotHeight?: number;

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
        // A field of the form was changed through its binding: what the
        // last test call said is about the service before that.
        this.svc().attachPropertyChange((event) => {
            if (String(event.getParameter("path") ?? "").indexOf("/data/") === 0) {
                this.clearTest();
            }
        });
        // `items`: what a destination field offers; `hint`: the line
        // under it; `notice`: what there is to say about the name in the
        // page's field (a `DestinationNotice`), `noticeText`: that in
        // words; `duplicate`: the same two for the field of the duplicate
        // dialog. Apart from `svc`, which is replaced on every load and save.
        this.setModel(new JSONModel({
            items: [], hint: "", notice: "", noticeText: "", duplicate: { notice: "", noticeText: "" }
        }), "dest");
        this.asDestinationField(this.byId("odataDestination") as Input);
        this.getRouter().getRoute(ROUTE)?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const name = (event.getParameter("arguments") as { serviceName: string }).serviceName;
            if (this.restoring) {
                // Back on the page the user tried to leave; it is as it was.
                this.restoring = false;
                this.getOwnerComponentTyped().setLeaveGuard(() => this.confirmLeave());
                this.askAboutLeaving();
                return;
            }
            // Another service (or this one again) by the address: a dialog
            // of the service that was shown does not stay open over it.
            this.leaveEntityDialog();
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

    /** The view is (again) in the page: the slot's element may be a new one. */
    public onAfterRendering(): void {
        this.slotObserver?.disconnect();
        this.slotObserver = undefined;
        // The box around the strip, not the slot: `holdPlace` may give the
        // slot a height, and an observer must not resize what it observes.
        const inner = this.byId("odataPendingInner")?.getDomRef();
        if (inner && typeof ResizeObserver !== "undefined") {
            this.slotObserver = new ResizeObserver(() => this.holdPlace());
            this.slotObserver.observe(inner);
        }
    }

    /**
     * Call before a change the admin makes may change the strip about
     * pending writes (a tick, the Enabled switch, a save): notes the height
     * of its slot, so that `holdPlace` can keep the page where it is.
     */
    private keepPlace(): void {
        const slot = this.byId("odataPendingSlot")?.getDomRef() as HTMLElement | null | undefined;
        this.slotHeight = slot && slot.offsetParent !== null ? slot.offsetHeight : undefined;
    }

    /**
     * The slot above the page content changed its height: the strip about
     * pending writes appeared, grew, shrank or went. Everything below it
     * would move by that much -- the row under the pointer included, so
     * that a second click lands on its neighbour. The page is scrolled by
     * the same amount instead, before the browser paints, so what is on
     * screen stays where it is (the browser's own scroll anchoring is
     * switched off for this page in style.css: not every browser has it).
     * Only after `keepPlace`: a page that is being loaded is not held.
     *
     * The slot keeps room for a strip of two lines at all times
     * (style.css), so the usual strip changes nothing at all. Two limits:
     * at the very top of the page a strip that needs more room takes it and
     * moves the content down by the difference -- scrolling there would put
     * the top of the form, the Enabled switch included, under the strip;
     * and where the page cannot scroll up as far as the slot shrank, the
     * slot keeps the rest as empty room until the next load.
     */
    private holdPlace(): void {
        const slot = this.byId("odataPendingSlot")?.getDomRef() as HTMLElement | null | undefined;
        const scroller = (this.byId("odataServiceDetailPage") as Page | undefined)?.getDomRef("cont") as
            HTMLElement | null | undefined;
        if (!slot || !scroller || slot.offsetParent === null) {
            // Not on screen (another page is shown): nothing moved.
            return;
        }
        const height = slot.offsetHeight;
        const before = this.slotHeight;
        this.slotHeight = undefined;
        // A row that gets the keyboard focus is scrolled to below the strip.
        scroller.style.scrollPaddingTop = this.svc().getProperty("/pendingWrites") ? `${height}px` : "";
        if (before === undefined || before === height || (height > before && scroller.scrollTop === 0)) {
            return;
        }
        const wanted = scroller.scrollTop + height - before;
        scroller.scrollTop = Math.max(wanted, 0);
        if (wanted < 0) {
            slot.style.minHeight = `${height - wanted}px`;
        }
    }

    /** A service is put on the page anew: the slot is as style.css has it. */
    private releasePlace(): void {
        const slot = this.byId("odataPendingSlot")?.getDomRef() as HTMLElement | null | undefined;
        if (slot) {
            slot.style.minHeight = "";
        }
        this.slotHeight = undefined;
    }

    public onExit(): void {
        this.leaveEntityDialog();
        this.slotObserver?.disconnect();
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
        // A dialog of this page is not left open over another page.
        this.leaveEntityDialog();
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
        this.destinationsCount++;
        this.showDestinations("loading");
    }

    /**
     * The page is being left (or shows another service) while the entity
     * set dialog may be open: the dialog is closed as Cancel does, and an
     * entity set that "Add" put into the form for it is taken out again
     * right away -- before anything asks whether the form has unsaved
     * changes. The dialog itself closes a moment later; by then the stub
     * would have been taken for a change, and the admin asked about a page
     * with nothing unsaved.
     */
    private leaveEntityDialog(): void {
        if (this.operationOpen) {
            // Closed as Cancel does; nothing of it is written into the form.
            this.operationDialog?.dismiss();
        }
        if (this.importOpen) {
            // Likewise: an import that was not applied changes nothing.
            this.importDialog?.dismiss();
        }
        if (!this.entityOpen) {
            return;
        }
        this.entityDismissed = true;
        const stub = this.addedStub;
        this.addedStub = undefined;
        if (stub) {
            this.dropAdded(stub, true);
        }
        this.entityDialog?.dismiss();
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
            errors: {}, saveError: "", used_by: [],
            // `rows`: what the entity sets table shows, one flat row per
            // entity set of `data.definition` (`odataCatalog.entitySetRow`).
            // `pendingWrites`: what the ticked, unsaved writes will allow,
            // as the strip says it; `pending`: the same as data, to tell
            // what one click changed.
            // `problemsOnly`: the table shows the marked rows alone, and
            // this is the sentence that says so.
            // `asking`: a question about the save is open.
            // `pendingReads`: the operations a save newly marks as only
            // reading (callable without "Allow writes", not recorded).
            rows: [], entitySearch: "", pendingWrites: "", asking: false, problemsOnly: "",
            pending: odataCatalog.noPending(), pendingReads: [],
            // `opRows`: what the operations table shows, one flat row per
            // operation of `data.definition` (`odataCatalog.operationRow`).
            // `uncallable`: what the server said about the STORED service's
            // enabled operations that no agent can call (name -> reason);
            // `uncallableText`: the sentence above the table that counts
            // the rows showing such a reason.
            // `usedByWarning`: what the Used-by section warns about.
            opRows: [], uncallable: {}, uncallableText: "", usedByWarning: "",
            // `updated_at`: the version of the service the form was loaded
            // from; a save is only made on top of that one.
            // `changedElsewhere` / `deletedElsewhere`: it is not the stored
            // version any more, or the service is gone.
            updated_at: null, changedElsewhere: false, deletedElsewhere: false,
            // The result of "Test call" as its strip shows it: `{type,
            // text}`, or null. It is about the stored service, so it goes
            // with every load and save.
            test: null,
            duplicate: {
                name: "", destination: "", user_context: false, errors: {}, error: "", unsaved: false, writes: ""
            }
        };
    }

    /**
     * Puts a stored service on the page, as loaded or as just saved.
     * `keepSearch`: the entity sets stay filtered as they were (after a
     * save the admin is still working on the same rows).
     */
    private show(service: ODataService, keepSearch = false): void {
        const search = keepSearch ? this.svc().getProperty("/entitySearch") as string : "";
        if (!keepSearch) {
            this.releasePlace();
        }
        this.serviceName = service.name;
        this.svc().setData({
            ...this.blankState(service.title),
            loaded: true, exists: true,
            data: odataCatalog.payloadOf(service), original: odataCatalog.payloadOf(service),
            used_by: service.used_by ?? [],
            updated_at: service.updated_at ?? null,
            rows: odataCatalog.entitySetRows(service.definition),
            // Read-only, and never part of what Save sends (`payloadOf`).
            uncallable: odataCatalog.uncallableByName(service.uncallable_operations),
            entitySearch: search
        });
        this.showOperations();
        this.showUsedBy();
        this.filterEntitySets(search);
        this.checkDestination();
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
        this.releasePlace();
        // Beside the service, not before it: the form does not wait for it.
        void this.loadDestinations();

        if (name === NEW) {
            this.serviceName = undefined;
            model.setData({ ...this.blankState(this.text("odataNewService")), isNew: true, loaded: true });
            this.filterEntitySets("");
            this.checkDestination();
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

    // --- destinations -------------------------------------------------------

    /**
     * Reads the destinations for the dropdown, once per visit of the page.
     *
     * Quiet whatever happens: without a list the field is a text field
     * with a hint, and a lapsed session is the service load's to report.
     * Only `dest` is written, never the service: a late or failed answer
     * cannot change what is in the field.
     */
    private async loadDestinations(): Promise<void> {
        const count = ++this.destinationsCount;
        this.heldAtValueHelp = undefined;
        this.showDestinations("loading");
        let state: DestinationsState;
        try {
            state = odataDestinations.read(await this.getAdminService().listODataDestinations());
        } catch {
            state = "unavailable";
        }
        if (count === this.destinationsCount) {
            this.showDestinations(state, true);
        }
    }

    /** `announce`: the list just arrived by itself, so what it changes on
     *  the page is said. */
    private showDestinations(state: DestinationsState, announce = false): void {
        const text = (key: string, args?: (string | number)[]) => this.text(key, args);
        const model = this.getModel("dest") as JSONModel;
        this.destinations = state;
        model.setProperty("/items", odataDestinations.choices(state, text));
        model.setProperty("/hint", odataDestinations.hint(state, text));
        if (announce && state === "unavailable") {
            InvisibleMessage.getInstance().announce(odataDestinations.hint(state, text), InvisibleMessageMode.Polite);
        }
        this.checkDestination(announce);
        this.checkDuplicateDestination();
    }

    /**
     * Makes `field` a destination field: an input that never completes
     * what is typed (`autocomplete="false"` in the view), so that it holds
     * what the admin typed or the name the admin picked and nothing else.
     * The page's field and the one of the duplicate dialog are both set up
     * here and share the handlers below.
     *
     * Not on a phone: the listed names that hold the typed text are
     * offered under the field, and one gets into the field only by a click
     * on it or by the arrow keys. On a phone the field is a plain text
     * field (`showSuggestion` is off there: the full-screen dialog the
     * input would use has an input of its own that does complete), and the
     * value help icon opens the list as a dialog to pick from.
     */
    private asDestinationField(field: Input): void {
        field.setFilterFunction((typed: string, item: Item) => odataDestinations.suggests(typed, item.getText()));
        // These run after the input's own handlers.
        field.addEventDelegate({
            // What the field holds when it is left is stored and judged,
            // also when no `change` tells of it (Escape from the list after
            // the arrow keys puts the typed text back without one).
            onfocusout: () => {
                this.heldAtValueHelp = undefined;
                this.storeShownDestination(field);
                this.judgeDestination(field);
            },
            // Enter on the name the field held before it was emptied.
            onsapenter: () => this.storeShownDestination(field),
            onsapescape: () => this.restoreDestinationHeld(field)
        });
    }

    /**
     * Makes what `field` shows the stored destination, where the input
     * itself may not have: with the list under it, the input writes "" to
     * the model the moment the field is emptied, but keeps the name before
     * as its last value. Whatever brings that same name back into the field
     * (Escape, typing or pasting it, picking it from the list) is no
     * `change` to the input, so the model would stay "" under a field that
     * shows the name. Only ever stores what the field shows.
     */
    private storeShownDestination(field: Input): void {
        const shown = field.getValue();
        const stored = this.isDuplicateField(field)
            ? (this.svc().getProperty("/duplicate/destination") as string | undefined) : this.data()?.destination;
        if (shown !== (stored ?? "")) {
            // Through the field: its binding writes the model.
            field.setValue(shown);
        }
    }

    /**
     * Escape in `field`. When its list was opened by the value help and no
     * text was typed since, the input puts back "what was typed", which is
     * nothing (or the part before the selection) and not what the field
     * held: with the text selected, as after Tab into the field, F4, an
     * arrow key and Escape emptied the field and the model, and a second
     * Escape had nothing to give back. The field gets back what it held.
     */
    private restoreDestinationHeld(field: Input): void {
        const held = this.heldAtValueHelp;
        this.heldAtValueHelp = undefined;
        if (held && held.field === field && field.getValue() !== held.value) {
            field.setValue(held.value);
            this.judgeDestination(field);
        }
    }

    private isDuplicateField(field: Input): boolean {
        return field === this.byId("odataDuplicateDestination");
    }

    private judgeDestination(field: Input): void {
        if (this.isDuplicateField(field)) {
            this.checkDuplicateDestination(false, false, field.getValue());
        } else {
            this.checkDestination(false, field.getValue());
        }
    }

    /** The field has another name, or one is being typed: what the last
     *  refused save or copy said about it is gone. */
    private destinationEdited(field: Input): void {
        if (this.isDuplicateField(field)) {
            this.svc().setProperty("/duplicate/errors/destination", "");
        } else {
            this.svc().setProperty("/errors/destination", "");
            this.svc().setProperty("/saveError", "");
        }
    }

    /** The value help icon or F4: the whole list, whatever the field
     *  holds; under the field, or as a dialog where it has no list. */
    public onDestinationValueHelp(event: Event): void {
        const field = event.getSource() as Input;
        if (field.getShowSuggestion()) {
            this.heldAtValueHelp = { field, value: field.getValue() };
            field.showItems(() => true);
        } else {
            void this.openDestinationPicker(field);
        }
    }

    private async openDestinationPicker(field: Input): Promise<void> {
        this.pickerField = field;
        if (this.destinationPickerLoading) {
            // A second tap while the dialog of the first is being built.
            return;
        }
        if (!this.destinationPicker) {
            this.destinationPickerLoading = Fragment.load({
                id: this.getView()!.getId(),
                name: "com.agent.admin.fragment.ODataDestinationPicker",
                controller: this
            }) as Promise<SelectDialog>;
            try {
                this.destinationPicker = await this.destinationPickerLoading;
            } finally {
                this.destinationPickerLoading = undefined;
            }
            this.getView()!.addDependent(this.destinationPicker);
        }
        (this.destinationPicker.getBinding("items") as ListBinding).filter([]);
        this.destinationPicker.open("");
    }

    /** The search field of the dialog: the names that hold the text. */
    public onDestinationPickerSearch(event: SelectDialog$LiveChangeEvent): void {
        const typed = event.getParameter("value") ?? "";
        (event.getParameter("itemsBinding") as ListBinding).filter(typed ? [new Filter({
            path: "name", test: (name: string) => odataDestinations.suggests(typed, name)
        })] : []);
    }

    /** A name was tapped in the dialog: it is the field's name now. */
    public onDestinationPicked(event: SelectDialog$ConfirmEvent): void {
        const item = event.getParameter("selectedItem");
        const field = this.pickerField;
        if (item && field) {
            // Through the field: its binding writes the model.
            field.setValue(item.getTitle());
            this.destinationEdited(field);
            this.judgeDestination(field);
        }
    }

    /**
     * Writes into `dest` at `path` what the list knows about the
     * destination `name` for the identity `userContext`, and says it when
     * asked to (`announce`; `always`: also when it is what was there
     * before). `judged`: false while there is nothing to judge.
     */
    private noteDestination(
        path: string, name: string, userContext: boolean, judged: boolean, announce: boolean, always: boolean
    ): string {
        const model = this.getModel("dest") as JSONModel;
        const notice: DestinationNotice = judged ? odataDestinations.notice(this.destinations, name, userContext) : "";
        const words = odataDestinations.noticeText(
            notice, (key, args) => this.text(key, args), odataDestinations.listedAs(this.destinations, name)
        );
        const changed = notice !== model.getProperty(`${path}/notice`) || words !== model.getProperty(`${path}/noticeText`);
        model.setProperty(`${path}/notice`, notice);
        model.setProperty(`${path}/noticeText`, words);
        return announce && words && (changed || always) ? words : "";
    }

    /**
     * Says what the list knows about the destination in the field, for the
     * identity the service runs as.
     *
     * `announce`: the cause is not the field itself (Runs as changed, the
     * list arrived), so a new notice is said; one that comes from typing
     * in the field is read with the field, like its errors. `always`: said
     * even when it is the notice that was there before (after a change of
     * Runs as the admin wants to know what holds now).
     */
    private checkDestination(announce = false, name = this.data().destination, always = false): void {
        const words = this.noteDestination(
            "", name, this.data().user_context === true, this.svc().getProperty("/loaded") === true, announce, always
        );
        if (words && !this.svc().getProperty("/errors/destination")) {
            InvisibleMessage.getInstance().announce(words, InvisibleMessageMode.Polite);
        }
    }

    /** The same for the field of the duplicate dialog, against the
     *  identity chosen in the dialog. */
    private checkDuplicateDestination(announce = false, always = false, name?: string): void {
        const state = this.svc().getProperty("/duplicate") as DuplicateState;
        const words = this.noteDestination(
            "/duplicate", name ?? state.destination, state.user_context === true,
            this.duplicateDialog?.isOpen() === true, announce, always
        );
        if (words && !state.errors.destination) {
            InvisibleMessage.getInstance().announce(words, InvisibleMessageMode.Polite);
        }
    }

    /**
     * A key in a destination field. While a name is being typed nothing
     * is said about it: neither the last error nor what the list knew
     * about the name before. Escape that puts the field back to the name
     * it held is the exception: that name is the stored one again and no
     * `change` follows, so it is judged here.
     */
    public onDestinationLiveChange(event: Input$LiveChangeEvent): void {
        const field = event.getSource();
        this.destinationEdited(field);
        if (event.getParameter("escPressed")) {
            // The name before is shown again; stored it may not be.
            this.storeShownDestination(field);
            this.judgeDestination(field);
        } else {
            this.heldAtValueHelp = undefined;
            this.noteDestination(this.isDuplicateField(field) ? "/duplicate" : "", "", false, false, false, false);
        }
    }

    /**
     * A destination field was left, or Enter was pressed in it, or a name
     * was picked from the list under it. The field holds what the admin
     * typed or picked and nothing else: the input does not complete typed
     * text. (The binding has written the model already.)
     */
    public onDestinationChange(event: Event): void {
        const field = event.getSource() as Input;
        this.heldAtValueHelp = undefined;
        this.destinationEdited(field);
        this.judgeDestination(field);
    }

    /** An error of the field (a refused save) comes before what the list
     *  says about the name. */
    public formatDestinationState(error: string | undefined, notice: DestinationNotice | undefined): ValueState {
        return error ? ValueState.Error : ValueState[odataDestinations.noticeState(notice ?? "")];
    }

    public formatDestinationStateText(error: string | undefined, words: string | undefined): string {
        return error || words || "";
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
        model.setProperty("/uncallable", {});
        model.setProperty("/test", null);
        model.setProperty("/updated_at", null);
        model.setProperty("/original", odataCatalog.emptyService());
        model.setProperty("/title", this.text("odataNewService"));
        this.showOperations();
        this.showUsedBy();
        this.showPendingWrites();
        this.checkDestination();
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

    /**
     * The Write tag: the stored service lets an agent with "Allow writes"
     * change data. By the rule the Save question goes by
     * (`odataCatalog.hasWrite`), worked out from the definition the page
     * holds as stored.
     */
    public formatWriteTag(exists: boolean | undefined, definition: ODataDefinition | undefined): boolean {
        return exists === true && odataCatalog.hasWrite(definition);
    }

    /** The copy button says when it copies write operations. */
    public formatDuplicateAction(writes: string | undefined): string {
        return this.text(writes ? "odataDuplicateWithWrites" : "odataDuplicate");
    }

    /** What the duplicate dialog says about the writes the copy gets. */
    public formatDuplicateWrites(writes: string | undefined): string {
        return writes ? this.text("odataDuplicateWrites", [writes]) : "";
    }

    /** "Showing only the 3 entity sets with a problem." */
    private problemsOnlyText(count: number): string {
        return count === 1 ? this.text("odataProblemRowsOne") : this.text("odataProblemRows", [count]);
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

    /** "Operations (3)". */
    public formatOperationsTitle(count: number | undefined): string {
        return this.text("odataOperations", [count ?? 0]);
    }

    /** The entity set an operation is bound to, or that it is bound to none. */
    public formatBoundTo(boundTo: string | undefined): string {
        return boundTo || this.text("odataUnbound");
    }

    /** Why no agent can call the operation, in words (`key`: an i18n key). */
    public formatUncallable(key: string | undefined): string {
        return key ? this.text(key) : "";
    }

    /** An enabled write stands out, like a ticked write of an entity set. */
    public formatEnabledWriteState(enabled: boolean | undefined, write: boolean | undefined): ValueState {
        return enabled && write ? ValueState.Warning : ValueState.None;
    }

    /** What the Enabled box of an operation switches, and that it is a write. */
    public formatEnableOperation(title: string | undefined, write: boolean | undefined): string {
        return this.text(write ? "odataEnableWriteOperation" : "odataEnableOperation", [title ?? ""]);
    }

    /** What a press on a row of the operations table opens. */
    public formatOpenOperation(title: string | undefined): string {
        return this.text("odataOpenOperation", [title ?? ""]);
    }

    /** What the Changes data box switches -- or, for an operation that is
     *  sent with POST, why it cannot be switched. */
    public formatChangesDataTooltip(label: string | undefined, title: string | undefined, post: boolean | undefined): string {
        return post === true ? this.text("odataChangesDataPost") : this.formatOperationOf(label, title);
    }

    public formatRemoveOperation(title: string | undefined): string {
        return this.text("odataRemoveOperation", [title ?? ""]);
    }

    public formatYesNo(value: boolean | undefined): string {
        return this.text(value === true ? "odataYes" : "odataNo");
    }

    /** An agent that may write is the one a newly enabled write reaches. */
    public formatWritesAllowedState(allowed: boolean | undefined): ValueState {
        return allowed === true ? ValueState.Warning : ValueState.None;
    }

    /** The slug the job scheduler starts the agent by, or a dash. */
    public formatRunEndpoint(exposed: boolean | undefined, slug: string | undefined): string {
        return exposed === true && slug ? slug : this.text("odataNoRunEndpoint");
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

    /** Works the table rows out again from the definition, all of them.
     *  `announce`: the admin just changed the definition, so a change of
     *  what is pending is said to a screen reader too. */
    private showEntitySets(announce = false): void {
        this.svc().setProperty("/rows", odataCatalog.entitySetRows(this.definition()));
        // An operation row names the entity set it is bound to by its title.
        this.showOperations();
        this.showPendingWrites(announce);
    }

    /**
     * The writes saving the form over `stored` would newly open: ticked
     * entity-set operations, enabled operations (function imports, actions)
     * that are writes, and fields that become writable where Create or
     * Update is on. Switching a stored, disabled service on opens all it has.
     */
    private newWrites(stored: ODataServiceInput): ODataPending {
        return odataCatalog.pendingWrites(stored.definition, this.definition(), this.switchesOn(stored));
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
        const before = model.getProperty("/pending") as ODataPending;
        const readsBefore = (model.getProperty("/pendingReads") ?? []) as ODataNewOperation[];
        if (announce) {
            // The admin just changed the form: the last test is about the
            // service before that.
            this.clearTest();
            this.keepPlace();
        }
        const loaded = model.getProperty("/loaded") === true;
        const pending = loaded ? this.newWrites(this.original()) : odataCatalog.noPending();
        // The other widening: a call that stops being a write.
        const reads = loaded ? this.newReads(this.original()) : [];
        const count = odataCatalog.pendingCount(pending);
        model.setProperty("/pending", pending);
        model.setProperty("/pendingReads", reads);
        // The strip names a few and counts the rest: it stays over the page.
        model.setProperty("/pendingWrites", [
            count ? this.text("odataPendingWrites", [this.writeList(pending, STRIP_CAP)]) : "",
            reads.length ? [
                this.text(reads.length === 1 ? "odataPendingReads" : "odataPendingReadsMany", [
                    this.operationList(reads, STRIP_CAP)
                ]),
                this.readAgents(model.getProperty("/used_by") as ODataUsedBy[], STRIP_CAP)
            ].join(" ") : ""
        ].filter(Boolean).join(" "));
        if (!announce) {
            return;
        }
        // What this click changed, and how many are pending now -- not the
        // whole list again -- and also that none is pending any more.
        const added = odataCatalog.pendingMinus(pending, before);
        const removed = odataCatalog.pendingMinus(before, pending);
        let said = "";
        if (odataCatalog.pendingCount(added)) {
            said = this.text("odataAnnounceAdded", [this.writeList(added, STRIP_CAP), count]);
        } else if (odataCatalog.pendingCount(removed)) {
            // Without "(Save lists them all)": what is gone is not in
            // the Save question.
            said = this.text(count ? "odataAnnounceRemoved" : "odataAnnounceNone", [
                this.writeList(removed, STRIP_CAP, "odataWriteMoreShort"), count
            ]);
        }
        // One announcement for both: a second one would replace the first.
        const readsAdded = odataCatalog.operationsMinus(reads, readsBefore);
        const readsRemoved = odataCatalog.operationsMinus(readsBefore, reads);
        const saidReads = [
            readsAdded.length ? this.text("odataAnnounceReadAdded", [this.operationList(readsAdded, STRIP_CAP)]) : "",
            readsRemoved.length
                ? this.text("odataAnnounceReadRemoved", [this.operationList(readsRemoved, STRIP_CAP, "odataWriteMoreShort")])
                : ""
        ].filter(Boolean).join(" ");
        said = [said, saidReads].filter(Boolean).join(" ");
        if (said) {
            InvisibleMessage.getInstance().announce(said, InvisibleMessageMode.Polite);
        }
    }

    /**
     * The operations saving the form over `stored` newly marks as only
     * reading: every agent that uses the service can then call them, also
     * without "Allow writes", and their calls are no longer recorded
     * (`odataCatalog.pendingReads`).
     */
    private newReads(stored: ODataServiceInput): ODataNewOperation[] {
        return odataCatalog.pendingReads(stored.definition, this.definition());
    }

    /**
     * Whom an operation that is marked as only reading newly reaches: the
     * agents of `usedBy` without "Allow writes", by name (they could not
     * call it while it was a write) -- or that there is no such agent.
     */
    private readAgents(usedBy: readonly ODataUsedBy[] | undefined | null, cap = Infinity): string {
        const all = usedBy ?? [];
        if (!all.length) {
            return this.text("odataReadAgentsNone");
        }
        const others = odataCatalog.writers(all).others;
        if (!others.length) {
            return this.text("odataReadAgentsAllAllowed");
        }
        return this.text("odataReadAgentsWithout", [others.length <= cap
            ? others.join(", ")
            : this.text("odataWriteFieldsMore", [others.slice(0, cap).join(", "), others.length - cap])]);
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
            this.sayListRefreshed(row.name);
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

    /**
     * A click was not applied because the table was not showing the entity
     * sets as they are. Said on the row that was clicked (when the entity
     * set is still there) and to a screen reader.
     */
    private sayListRefreshed(name: string): void {
        const text = this.text("odataListRefreshed");
        const index = this.rows().map((row) => row.name).indexOf(name);
        if (index !== -1) {
            this.svc().setProperty(`/rows/${index}/note`, text);
        }
        InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
    }

    /** Shows all entity sets again and empties the search field. */
    private clearEntitySearch(): void {
        this.svc().setProperty("/entitySearch", "");
        this.filterEntitySets("");
    }

    /** The way back from the marked rows alone to all entity sets. */
    public onShowAllEntitySets(): void {
        this.clearEntitySearch();
        // The link that was pressed goes away with its strip.
        (this.byId("odataEntityTable") as Control | undefined)?.focus();
    }

    /** The positions (in the definition) of the rows the table renders now. */
    private renderedRows(): number[] {
        const table = this.byId("odataEntityTable") as Table | undefined;
        return (table?.getItems() ?? []).map((item) => (
            (item.getBindingContext("svc")?.getObject() as ODataEntityRow | undefined)?.index ?? -1
        ));
    }

    /**
     * Rows were marked (`marked`: their positions): makes sure they can be
     * seen, and says so.
     *
     * When every marked row is among the rendered ones, the table and the
     * search stay as they are. Otherwise -- the search hides one, or it is
     * further down than the table has grown -- the table shows the marked
     * rows alone, says that it does, and offers the way back. `focus`: the
     * first marked row then gets the focus, which also scrolls it into
     * view; not when a field of the form already has it. What is said above
     * the form is announced, since the admin may be far from it.
     */
    private revealMarked(marked: number[], focus: boolean): void {
        const model = this.svc();
        const table = this.byId("odataEntityTable") as Table | undefined;
        const toFirst = (): void => {
            const first = (table?.getItems() ?? []).filter((item) => (
                marked.indexOf((item.getBindingContext("svc")?.getObject() as ODataEntityRow).index) !== -1
            ))[0] as ColumnListItem | undefined;
            if (focus) {
                first?.focus();
            }
        };
        const rendered = this.renderedRows();
        // Already showing marked rows alone: that view follows the marks,
        // so that its sentence and its rows are those of this save.
        if (model.getProperty("/problemsOnly") || marked.some((index) => rendered.indexOf(index) === -1)) {
            model.setProperty("/entitySearch", "");
            model.setProperty("/problemsOnly", this.problemsOnlyText(marked.length));
            table?.attachEventOnce("updateFinished", toFirst);
            // The rows as marked now: a row that is repaired stays in the
            // table until the admin leaves this view of it.
            (table?.getBinding("items") as ListBinding | undefined)?.filter(new Filter({
                path: "index", test: (index: number) => marked.indexOf(index) !== -1
            }));
        } else {
            toFirst();
        }
        const said = [model.getProperty("/saveError") as string, model.getProperty("/problemsOnly") as string]
            .filter(Boolean).join(" ");
        if (said) {
            InvisibleMessage.getInstance().announce(said, InvisibleMessageMode.Polite);
        }
    }

    /**
     * Shows the entity sets whose title, technical name or description
     * holds `query`. A filter on the binding: no row is worked out again,
     * and only the rows on screen are rendered. It also ends the view of
     * the marked rows alone.
     */
    private filterEntitySets(query: string): void {
        const binding = this.byId("odataEntityTable")?.getBinding("items") as ListBinding | undefined;
        const text = query.trim();
        this.svc().setProperty("/problemsOnly", "");
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
     * Cancelled there, it is gone again: Cancel leaves the service as it was.
     */
    public onAddEntitySet(): void {
        const model = this.svc();
        const entitySets = this.definition().entity_sets;
        // Not while a dialog is open or on its way (its fragment is still
        // being loaded after a first press): `openEntitySet` would not open
        // a second one, and the entity set pushed here would stay behind
        // without a dialog to cancel it in.
        if (this.entityOpen || this.operationOpen || this.importOpen || this.working
            || model.getProperty("/asking") === true || entitySets.length >= odataCatalog.MAX_ENTITY_SETS) {
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
        this.openEntitySet(entitySets.length - 1, true).catch((error: unknown) => ErrorHandler.handle(error));
    }

    /** A row of the table was pressed. */
    public onOpenEntitySet(event: Event): void {
        const row = (event.getSource() as Control).getBindingContext("svc")?.getObject() as ODataEntityRow | undefined;
        if (!row) {
            return;
        }
        if (this.definition().entity_sets[row.index]?.name !== row.name) {
            // The row is not the entity set at its position: nothing opens.
            this.showEntitySets();
            this.sayListRefreshed(row.name);
            return;
        }
        this.openEntitySet(row.index).catch((error: unknown) => ErrorHandler.handle(error));
    }

    /**
     * Opens the dialog of the entity set at `index` of the definition:
     * fields, keys, navigations, example queries, title and description.
     *
     * The dialog edits a copy and sends nothing. On Apply what the dialog
     * edits of the copy (`DIALOG_EDITS`) is written into the entity set of
     * the form, on Remove the entity set leaves it; either way the rows are
     * worked out again (`showEntitySets`), so the table, the pending-writes
     * strip and the unsaved-changes check follow from the definition, and
     * the page's Save stores it. The entity set is found again by identity
     * when the dialog closes: if the form was loaded anew meanwhile,
     * nothing is written into it.
     *
     * `added`: the entity set was just added for this dialog. Left without
     * Apply (Cancel, or a dialog that could not be shown), it is taken out
     * of the form again.
     *
     * The focus goes back to where the admin was: the row of the entity
     * set, or Add when there is no such row any more.
     */
    private async openEntitySet(index: number, added = false): Promise<void> {
        const model = this.svc();
        const entitySet = this.definition().entity_sets[index];
        if (!entitySet || this.entityOpen || this.operationOpen || this.importOpen || this.working
            || model.getProperty("/asking") === true) {
            return;
        }
        if (!this.entityDialog) {
            this.entityDialog = new EntitySetDialog();
        }
        const stored = this.original().definition?.entity_sets ?? [];
        this.entityOpen = true;
        this.entityDismissed = false;
        this.addedStub = added ? entitySet : undefined;
        let result;
        try {
            result = await this.entityDialog.open(this.getView()!, entitySet, {
                definition: this.definition(),
                version: this.data().odata_version,
                // What SAP calls it stays once the service is saved with it.
                canRename: !stored.some((candidate) => candidate.name === entitySet.name),
                text: (key, args) => this.text(key, args)
            });
        } catch (error) {
            this.dropAdded(entitySet, added);
            throw error;
        } finally {
            this.entityOpen = false;
            this.addedStub = undefined;
        }
        const dismissed = this.entityDismissed;
        this.entityDismissed = false;
        const entitySets = this.definition().entity_sets;
        const at = entitySets.indexOf(entitySet);
        if (!result) {
            const dropped = this.dropAdded(entitySet, added);
            if (dismissed) {
                // The page closed the dialog because it is being left:
                // the focus is not pulled back to it.
                return;
            }
            if (dropped) {
                this.focusEntityRow(-1);
            } else if (at !== -1) {
                this.focusEntityRow(at);
            }
            return;
        }
        if (at === -1) {
            this.showEntitySets();
            this.sayListRefreshed(entitySet.name);
            return;
        }
        if (result.action === "remove") {
            entitySets.splice(at, 1);
            // Every later row moved up: a view of marked rows, which goes
            // by position, would show others. The search stays.
            this.filterEntitySets(model.getProperty("/entitySearch") as string);
        } else {
            // Only what the dialog edits: the rest of its copy is as old
            // as the moment the dialog opened.
            const edited = result.entitySet as unknown as Record<string, unknown>;
            const target = entitySets[at] as unknown as Record<string, unknown>;
            DIALOG_EDITS.forEach((key) => {
                if (edited[key] !== undefined) {
                    target[key] = edited[key];
                }
            });
        }
        model.setProperty("/saveError", "");
        this.showEntitySets(true);
        this.focusEntityRow(result.action === "remove" ? -1 : at);
    }

    /** Takes an entity set that was added for a dialog out of the form
     *  again. Answers whether it did. */
    private dropAdded(entitySet: ODataEntitySet, added: boolean): boolean {
        const entitySets = this.definition().entity_sets;
        const at = added ? entitySets.indexOf(entitySet) : -1;
        if (at === -1) {
            return false;
        }
        entitySets.splice(at, 1);
        this.showEntitySets();
        return true;
    }

    /** Puts the focus on the row of the entity set at `index` of the
     *  definition, or on Add when no such row is listed (-1, a row behind
     *  "More", a row the search hides). */
    private focusEntityRow(index: number): void {
        const item = (this.byId("odataEntityTable") as Table | undefined)?.getItems().filter((candidate) => (
            (candidate.getBindingContext("svc")?.getObject() as ODataEntityRow | undefined)?.index === index
        ))[0];
        ((item ?? this.byId("odataAddEntitySetButton")) as Control | undefined)?.focus();
    }

    // --- import from $metadata ----------------------------------------------

    /**
     * Opens the import dialog. It reads the `$metadata` document of the
     * service as the FORM describes it (destination, service path, version
     * and identity, read when "Read metadata" is pressed) and compares it
     * with the definition of the form.
     *
     * Nothing is stored: on Apply the merged definition and the time of the
     * read are written into the form, and Save stores them. What an import
     * adds arrives switched off (`odataCatalog.mergeImport`), so it opens
     * nothing by itself; whatever the admin enables afterwards goes through
     * the pending strip and the Save question like every other tick.
     */
    public onImportMetadata(): void {
        const model = this.svc();
        if (this.importOpen || this.entityOpen || this.operationOpen || this.working
            || model.getProperty("/asking") === true || model.getProperty("/loaded") !== true) {
            return;
        }
        this.openImport().catch((error: unknown) => ErrorHandler.handle(error));
    }

    /** Where the form says the service is, for the read of the import; or
     *  nothing while destination or service path could not be sent. */
    private metadataRequest(): ODataMetadataRequest | undefined {
        // As Save does: what the destination field shows is what is read.
        this.storeShownDestination(this.byId("odataDestination") as Input);
        const data = this.data();
        const problems = odataCatalog.validate(data);
        if (problems.destination || problems.service_path) {
            return undefined;
        }
        const request: ODataMetadataRequest = {
            destination: data.destination, service_path: data.service_path,
            odata_version: data.odata_version === "v4" ? "v4" : "v2", user_context: data.user_context === true
        };
        if (this.svc().getProperty("/exists") === true && this.serviceName) {
            // The server compares with the stored service: it knows what a
            // long document no longer declares.
            request.service = this.serviceName;
        }
        return request;
    }

    private async openImport(): Promise<void> {
        if (!this.importDialog) {
            this.importDialog = new ImportDialog();
        }
        const definition = this.definition();
        this.importOpen = true;
        let result;
        try {
            result = await this.importDialog.open(this.getView()!, {
                definition,
                request: () => this.metadataRequest(),
                read: (body) => this.getAdminService().readODataMetadata(body),
                handled: (error) => {
                    const kind = ErrorHandler.classify(error);
                    if (kind === "session" || kind === "forbidden") {
                        ErrorHandler.handle(error);
                        return true;
                    }
                    return false;
                },
                text: (key, args) => this.text(key, args)
            });
        } finally {
            this.importOpen = false;
        }
        if (!result) {
            return;
        }
        const model = this.svc();
        if (model.getProperty("/loaded") !== true || this.definition() !== definition) {
            // The form was loaded anew while the dialog was open: what was
            // merged is about another definition.
            InvisibleMessage.getInstance().announce(this.text("odataImportRefreshed"), InvisibleMessageMode.Polite);
            return;
        }
        model.setProperty("/data/definition", result.definition);
        model.setProperty("/data/metadata_fetched_at", result.metadata_fetched_at);
        model.setProperty("/saveError", "");
        // What a refused save said was about another list; no row is hidden.
        this.clearEntitySearch();
        this.filterEntitySets("");
        this.showEntitySets(true);
        MessageToast.show(this.text("odataImportApplied"));
        (this.byId("odataEntityTable") as Control | undefined)?.focus();
    }

    // --- operations ---------------------------------------------------------

    /**
     * Works the rows of the operations table out again from the definition,
     * and counts the rows that say why no agent can call them.
     */
    private showOperations(): void {
        const model = this.svc();
        const rows = model.getProperty("/loaded") === true
            ? odataCatalog.operationRows(
                this.definition(), model.getProperty("/uncallable") as Record<string, string>,
                this.original().definition
            )
            : [];
        model.setProperty("/opRows", rows);
        this.countUncallable();
    }

    private countUncallable(): void {
        const model = this.svc();
        const count = (model.getProperty("/opRows") as ODataOperationRow[]).filter((row) => row.uncallable).length;
        model.setProperty("/uncallableText", count === 0 ? ""
            : count === 1 ? this.text("odataUncallableOne") : this.text("odataUncallableMany", [count]));
    }

    /**
     * The operation a control of the operations table belongs to: its row
     * and the operation at that position of the definition -- or only the
     * row, when that is not (or no longer) the operation the row shows.
     */
    private operationAt(source: Control): { row?: ODataOperationRow; operation?: ODataOperation } {
        const row = source.getBindingContext("svc")?.getObject() as ODataOperationRow | undefined;
        const operation = row ? this.definition().operations[row.index] : undefined;
        return { row, operation: operation && row && operation.name === row.name ? operation : undefined };
    }

    /** The row of `operation` anew: its note is about the click before. */
    private showOperation(operation: ODataOperation, note = ""): void {
        const model = this.svc();
        const index = this.definition().operations.indexOf(operation);
        if (index === -1) {
            this.showOperations();
            return;
        }
        model.setProperty(`/opRows/${index}`, {
            ...odataCatalog.operationRow(
                operation, index, this.definition(), model.getProperty("/uncallable") as Record<string, string>,
                this.original().definition
            ),
            note
        });
        this.countUncallable();
    }

    /** Puts a box of the operations table back to what its row holds (the
     *  boxes are bound one way, like those of the entity sets). */
    private resetOperationBox(box: CheckBox): void {
        const row = box.getBindingContext("svc")?.getObject() as ODataOperationRow | undefined;
        box.setSelected(row ? (box.data("flag") === "enabled" ? row.enabled : row.write) === true : false);
    }

    /**
     * A click on a box of the operations table that cannot be taken as it
     * is: while a save is on its way or asked about, or on a row that is not
     * the operation at its position. Returns the operation when the click
     * can go on.
     */
    private clickedOperation(box: CheckBox): ODataOperation | undefined {
        const model = this.svc();
        const { row, operation } = this.operationAt(box);
        if (!row || this.working || model.getProperty("/asking") === true) {
            this.resetOperationBox(box);
            return undefined;
        }
        if (!operation) {
            this.showOperations();
            this.resetOperationBox(box);
            this.sayOperationsRefreshed(row.name);
            return undefined;
        }
        return operation;
    }

    /** Says that the operations were listed anew and the click was not
     *  taken: on the row of the operation `name`, when there still is one,
     *  and to a screen reader. */
    private sayOperationsRefreshed(name: string): void {
        const model = this.svc();
        const text = this.text("odataOperationsRefreshed");
        const index = (model.getProperty("/opRows") as ODataOperationRow[]).map((r) => r.name).indexOf(name);
        if (index !== -1) {
            model.setProperty(`/opRows/${index}/note`, text);
        }
        InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
    }

    /** A row of the operations table was pressed. */
    public onOpenOperation(event: Event): void {
        const model = this.svc();
        const { row, operation } = this.operationAt(event.getSource() as Control);
        if (!row || this.operationOpen || this.entityOpen || this.importOpen || this.working
            || model.getProperty("/asking") === true) {
            return;
        }
        if (!operation) {
            // The row is not the operation at its position: nothing opens.
            this.showOperations();
            this.sayOperationsRefreshed(row.name);
            return;
        }
        this.openOperation(operation).catch((error: unknown) => ErrorHandler.handle(error));
    }

    /**
     * Opens the dialog of an operation: its business name and description
     * for the agent, and what SAP says about it, read-only.
     *
     * The dialog edits a copy and sends nothing. On Apply the two texts --
     * and nothing else -- are written into the operation of the form, when
     * they differ from what it holds; the page's Save stores them. The
     * operation is found again by identity when the dialog closes: if the
     * form was loaded anew meanwhile, nothing is written into it.
     */
    private async openOperation(operation: ODataOperation): Promise<void> {
        if (!this.operationDialog) {
            this.operationDialog = new OperationDialog();
        }
        this.operationOpen = true;
        let result;
        try {
            result = await this.operationDialog.open(this.getView()!, operation, {
                definition: this.definition(),
                text: (key, args) => this.text(key, args)
            });
        } finally {
            this.operationOpen = false;
        }
        if (!result) {
            return;
        }
        if (this.definition().operations.indexOf(operation) === -1) {
            this.showOperations();
            this.sayOperationsRefreshed(operation.name);
            return;
        }
        let changed = false;
        if (result.title !== (operation.title ?? "")) {
            operation.title = result.title;
            changed = true;
        }
        if (result.description !== (operation.description ?? "")) {
            operation.description = result.description;
            changed = true;
        }
        if (!changed) {
            return;
        }
        this.showOperation(operation);
        this.svc().setProperty("/saveError", "");
        // A pending operation is named by its title.
        this.showPendingWrites(true);
    }

    /**
     * The Enabled box of an operation was clicked: switches the operation
     * on or off in the form. Nothing is sent; Save does that.
     *
     * Enabling an operation that is a write (`odataCatalog.operationIsWrite`)
     * is a pending write like a ticked Create, Update or Delete: the strip
     * next to Save names it at once (`showPendingWrites`), and Save asks
     * with the names of the agents that can then call it.
     */
    public onToggleOperationEnabled(event: Event): void {
        const box = event.getSource() as CheckBox;
        const enable = box.getSelected();
        const operation = this.clickedOperation(box);
        if (!operation) {
            return;
        }
        operation.enabled = enable;
        this.showOperation(operation);
        this.svc().setProperty("/saveError", "");
        this.showPendingWrites(true);
    }

    /**
     * The "Changes data" box of an operation was clicked.
     *
     * Ticking it is always taken: the operation is then a write, and when
     * it is enabled that is a pending write. Unticking it takes the
     * operation out of the write permission and out of the audit, so it is
     * asked about first, with Cancel as the default -- and it is refused on
     * an operation that is sent with POST, which is run as a write whatever
     * this flag says: the box would claim otherwise.
     */
    public onToggleChangesData(event: Event): void {
        const box = event.getSource() as CheckBox;
        const tick = box.getSelected();
        const operation = this.clickedOperation(box);
        if (!operation) {
            return;
        }
        const model = this.svc();
        if (tick) {
            operation.changes_data = true;
            this.showOperation(operation);
            model.setProperty("/saveError", "");
            this.showPendingWrites(true);
            return;
        }
        // Nothing changes before the answer; the box shows what holds.
        this.resetOperationBox(box);
        if (operation.http_method !== "GET") {
            const reason = this.text("odataChangesDataPost");
            this.showOperation(operation, reason);
            InvisibleMessage.getInstance().announce(reason, InvisibleMessageMode.Assertive);
            return;
        }
        const untick = this.text("odataChangesDataOffAction");
        const title = (operation.title ?? "").trim() || operation.name;
        model.setProperty("/asking", true);
        MessageBox.warning(this.text("odataChangesDataOffConfirm"), {
            title: this.text("odataChangesDataOffTitle", [title]),
            actions: [untick, MessageBox.Action.CANCEL],
            emphasizedAction: MessageBox.Action.CANCEL,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                model.setProperty("/asking", false);
                // Only into the form the question was asked about.
                if (action === untick && this.definition().operations.indexOf(operation) !== -1) {
                    operation.changes_data = false;
                    this.showOperation(operation);
                    model.setProperty("/saveError", "");
                    this.showPendingWrites(true);
                }
            }
        });
    }

    /**
     * Takes an operation out of the form, after asking. This is also the
     * way to remove an entity set an operation is bound to or returns.
     * Nothing is sent; Save does that.
     */
    public onRemoveOperation(event: Event): void {
        const model = this.svc();
        const { row, operation } = this.operationAt(event.getSource() as Control);
        if (!row || this.working || model.getProperty("/asking") === true) {
            return;
        }
        if (!operation) {
            this.showOperations();
            InvisibleMessage.getInstance().announce(this.text("odataOperationsRefreshed"), InvisibleMessageMode.Polite);
            return;
        }
        const remove = this.text("odataRemove");
        model.setProperty("/asking", true);
        MessageBox.warning(this.text("odataRemoveOperationConfirm", [row.title, row.name]), {
            title: this.text("odataRemoveOperationTitle"),
            actions: [remove, MessageBox.Action.CANCEL],
            emphasizedAction: remove,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                model.setProperty("/asking", false);
                const operations = this.definition().operations;
                const at = operations.indexOf(operation);
                if (action !== remove || at === -1) {
                    return;
                }
                operations.splice(at, 1);
                model.setProperty("/saveError", "");
                this.showOperations();
                this.showPendingWrites(true);
                // The button that was pressed went with its row.
                (this.byId("odataOperationsTable") as Control | undefined)?.focus();
            }
        });
    }

    // --- used by ------------------------------------------------------------

    /**
     * Warns when the service runs as the signed-in user (as the form has
     * it) and an agent that uses it has a run endpoint: a run started there
     * by the job scheduler has no user, and every call is refused.
     */
    private showUsedBy(): void {
        const model = this.svc();
        const agents = model.getProperty("/loaded") === true && this.data().user_context === true
            ? odataCatalog.jobAgents(model.getProperty("/used_by") as ODataUsedBy[]) : [];
        model.setProperty("/usedByWarning", agents.length === 0 ? ""
            : agents.length === 1 ? this.text("odataUserServiceOnJobAgent", [agents[0]])
                : this.text("odataUserServiceOnJobAgents", [agents.join(", ")]));
    }

    // --- test call ----------------------------------------------------------

    /**
     * Asks the server to read one row through the STORED service and shows
     * how that went: status, destination, duration, rows and identity,
     * never a value. With unsaved changes nothing is called: the result
     * would be about another service than the one on the page.
     */
    public async onTestCall(): Promise<void> {
        const model = this.svc();
        const name = this.serviceName;
        if (this.working || !name || model.getProperty("/exists") !== true || model.getProperty("/asking") === true) {
            return;
        }
        if (this.isDirty()) {
            this.showTest("Warning", this.text("odataTestUnsaved"));
            return;
        }
        const count = this.loadCount;
        this.setWorking(true);
        let result: ODataTestResult;
        try {
            result = await this.withBusy(() => this.getAdminService().testODataService(name));
        } catch (error) {
            this.setWorking(false);
            if (count === this.loadCount) {
                this.showTestRefusal(error);
            }
            return;
        }
        this.setWorking(false);
        if (count !== this.loadCount || name !== this.serviceName) {
            // The page shows another service by now.
            return;
        }
        const warnings = (Array.isArray(result.warnings) ? result.warnings : [])
            .map((warning) => String(warning?.message || warning?.code || "")).filter(Boolean);
        this.showTest(
            result.ok !== true ? "Error" : warnings.length ? "Warning" : "Success",
            [this.testText(result)].concat(warnings).join(" ")
        );
    }

    /** The sentence about a test call that ran (or could be judged). */
    private testText(result: ODataTestResult): string {
        const as = this.text(result.identity === "user" ? "odataTestAsUser"
            : result.identity === "technical" ? "odataTestAsTechnical" : "odataTestAsUnknown");
        const destination = String(result.destination ?? "");
        const status = typeof result.status === "number" ? String(result.status) : "";
        const duration = String(result.duration_ms ?? 0);
        if (result.ok === true) {
            if (result.read === "metadata") {
                return this.text("odataTestOkMetadata", [status, destination, duration, as]);
            }
            return result.rows === 1
                ? this.text("odataTestOkOne", [status, destination, duration, String(result.target ?? ""), as])
                : this.text("odataTestOk", [
                    status, destination, duration, String(result.rows ?? 0), String(result.target ?? ""), as
                ]);
        }
        if (result.code === "proxy_refused") {
            return this.text("odataTestProxyRefused", [destination, as]);
        }
        // The server's words (SAP's code and text among them), as text.
        const said = String(result.message || result.code || "");
        if (status) {
            return this.text("odataTestFailed", [status, destination, as, said]);
        }
        return destination
            ? this.text("odataTestFailedNoStatus", [destination, said]) : this.text("odataTestFailedPlain", [said]);
    }

    /** The test was not made: the route refused it, or could not be reached. */
    private showTestRefusal(error: unknown): void {
        if (error instanceof AdminError && error.status === 404) {
            this.svc().setProperty("/deletedElsewhere", true);
            return;
        }
        const kind = ErrorHandler.classify(error);
        if (kind === "session" || kind === "forbidden") {
            ErrorHandler.handle(error);
            return;
        }
        const reason = ErrorHandler.messageFor(error, "");
        // By the stable code of the refusal (`X-OData-Error`), not by its
        // HTTP status: a proxy on the way answers with those statuses too.
        const code = error instanceof AdminError ? error.code : "";
        this.showTest("Error", code === "user_token_required" ? this.text("odataTestNoUser")
            : code === "busy" ? this.text("odataTestBusy")
                : reason ? this.text("odataTestNotMade", [reason]) : this.text("odataTestNotMadePlain"));
    }

    /** Shows the strip of the test call, brings it into view (the button
     *  stays in the toolbar while the page is scrolled) and announces it. */
    private showTest(type: "Success" | "Warning" | "Error", text: string): void {
        this.svc().setProperty("/test", { type, text });
        (this.byId("odataServiceDetailPage") as Page | undefined)?.scrollTo(0);
        InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
    }

    /** Takes the strip of the test call away: it is about the service
     *  as it was before the form was changed. */
    private clearTest(): void {
        if (this.svc().getProperty("/test")) {
            this.svc().setProperty("/test", null);
        }
    }

    // --- editing ------------------------------------------------------------

    /** Typing in a field takes its error away, and with it what the last
     *  refused save said above the form; the next save checks again. */
    public onEdit(event: Event): void {
        const field = (event.getSource() as Control).data("field") as string;
        this.svc().setProperty(`/errors/${field}`, "");
        this.svc().setProperty("/saveError", "");
        this.clearTest();
    }

    public onRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/data/user_context", key === "user");
        this.svc().setProperty("/saveError", "");
        this.clearTest();
        this.showUsedBy();
        this.checkDestination(true, undefined, true);
    }

    public onVersionChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/data/odata_version", key === "v4" ? "v4" : "v2");
        this.svc().setProperty("/saveError", "");
        this.clearTest();
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
        // A tap on Save that does not move the focus (a touch tablet)
        // leaves the field without its `change`: what it shows is stored.
        this.storeShownDestination(this.byId("odataDestination") as Input);
        model.setProperty("/saveError", "");
        // Both checks run, so that everything wrong is marked at once.
        const general = this.showProblems(odataCatalog.validate(this.data()));
        const entitySets = this.showEntityProblems(general);
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
        this.showUsedBy();
        // The strip names agents, too.
        this.showPendingWrites();
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
     * which sets what is said above the form. `focus`: no field of the form
     * has a problem, so the first marked row gets the focus.
     */
    private showEntityProblems(focus: boolean): boolean {
        const model = this.svc();
        const problems = odataCatalog.definitionProblems(this.definition());
        const byIndex: Record<number, string> = {};
        problems.forEach((problem) => {
            byIndex[problem.index] = this.text(problem.key, problem.args);
        });
        this.showRowErrors(byIndex);
        if (problems.length) {
            // A marked row must be on screen, and named so that it is found.
            const rows = this.rows();
            const names = problems.map((problem) => odataCatalog.entityLabel(rows[problem.index])).join(", ");
            const above = model.getProperty("/saveError") as string;
            model.setProperty("/saveError", [above, this.text("odataEntityProblems", [names])].filter(Boolean).join(" "));
            this.revealMarked(problems.map((problem) => problem.index), focus);
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
     * - who the agents in `usedBy` act as in SAP (identity, destination) and
     *   where their calls go (service path, OData version) -- only when
     *   there are such agents;
     * - which write operations the catalogue newly lets agents run -- always,
     *   also when no agent uses the service yet: the next agent attached
     *   with "Allow writes" gets them without this page being opened again;
     * - which operations are newly marked as only reading -- always, too:
     *   they become callable without "Allow writes" and are no longer
     *   recorded, with the agents that have no "Allow writes" by name.
     *
     * One question for all of it.
     */
    private saveQuestion(stored: ODataServiceInput, usedBy: ODataUsedBy[]): SaveQuestion | undefined {
        const change = odataCatalog.identityChange(stored, this.data());
        const identity = usedBy.length > 0
            && (!!change.runsAs || !!change.destination || !!change.servicePath || !!change.version);
        const writes = this.newWrites(stored);
        const anyWrite = odataCatalog.pendingCount(writes) > 0;
        const reads = this.newReads(stored);
        const anyRead = reads.length > 0;
        if (!identity && !anyWrite && !anyRead) {
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
        // The agents' calls, writes included, would go somewhere else.
        if (identity && change.servicePath) {
            parts.push(this.text("odataIdentityServicePath", [change.servicePath.from, change.servicePath.to]));
        }
        if (identity && change.version) {
            parts.push(this.text("odataIdentityVersion", [
                this.formatVersion(change.version.from), this.formatVersion(change.version.to)
            ]));
        }
        if (anyWrite) {
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
        if (anyRead) {
            // The other way a save opens something: a call that stops
            // being a write reaches the agents WITHOUT "Allow writes", and
            // nobody can look it up afterwards. Every operation is named.
            parts.push(this.text("odataReadSaveIntro", [this.operationList(reads)]));
            parts.push(this.readAgents(usedBy));
        }
        const kinds = [identity, anyWrite, anyRead].filter(Boolean).length;
        const title = kinds > 1 ? "odataSaveConfirmTitle"
            : identity ? "odataIdentityConfirmTitle" : anyWrite ? "odataWriteConfirmTitle" : "odataReadConfirmTitle";
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
        if (!isNew) {
            // The strip goes with the save; the rows stay where they are.
            this.keepPlace();
        }
        this.show(saved, !isNew);
        // Stored, but the running agents still have the service as it was
        // (an unticked Update, a service switched off): a box that stays
        // says so, in place of the toast that would read as "it is in".
        if (!this.warnIfNotLive(saved, "reloadFailedServiceSaved")) {
            MessageToast.show(this.text("odataSaved"));
        }
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
            const refusal = odataCatalog.serverRefusal(error.detail);
            const byLoc = refusal.byLoc;
            // Only while the table still lists what was sent, row for row.
            const rows = this.rows();
            const placed = canonical(rows.map((row) => row.name)) === canonical(sentNames)
                ? odataCatalog.rowErrors(byLoc, sentNames) : {};
            this.showRowErrors(placed);
            const marked = Object.keys(placed).length > 0;
            // With a row marked, the text above the form names the entity
            // set in place of its position in the definition -- and still
            // says all the server said: what came before the first field,
            // and its "and n more".
            const answer = marked
                ? [refusal.lead].concat(odataCatalog.refusalLines(byLoc, rows), [refusal.more]).filter(Boolean).join("; ")
                : error.detail;
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
            if (marked) {
                // A marked row must be seen, whatever the search hid and
                // however far down it is.
                this.revealMarked(Object.keys(placed).map(Number), Object.keys(errors).length === 0);
            }
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

    /** Every write `definition` holds, as the duplicate dialog lists them:
     *  the server copies the definition whole, writes included. */
    private copiedWrites(definition: ODataDefinition): string {
        // The operations, as the dialog's sentence says; which fields
        // they can send is part of the definition that is copied.
        return this.writeList({ ...odataCatalog.pendingWrites(undefined, definition), fields: [] });
    }

    /**
     * Opens the dialog that asks what the copy differs in: its name, its
     * destination and its identity. It also lists the write operations the
     * copy gets and says who can run them; with such a list the focus
     * starts on Cancel and the copy button says that it copies them.
     */
    public async onDuplicate(): Promise<void> {
        const model = this.svc();
        if (this.working || model.getProperty("/exists") !== true) {
            return;
        }
        const stored = this.original();
        const state: DuplicateState = {
            name: "", destination: stored.destination, user_context: stored.user_context,
            errors: {}, error: "", unsaved: this.isDirty(), writes: this.copiedWrites(stored.definition)
        };
        model.setProperty("/duplicate", state);

        if (!this.duplicateDialog) {
            this.duplicateDialog = await Fragment.load({
                id: this.getView()!.getId(),
                name: "com.agent.admin.fragment.ODataDuplicateDialog",
                controller: this
            }) as Dialog;
            this.getView()!.addDependent(this.duplicateDialog);
            this.asDestinationField(this.byId("odataDuplicateDestination") as Input);
            // Open: the field is judged. Closed: nothing of it is left to say.
            this.duplicateDialog.attachAfterOpen(() => this.checkDuplicateDestination());
            this.duplicateDialog.attachAfterClose(() => this.checkDuplicateDestination());
        }
        this.duplicateDialog.setInitialFocus(this.byId(state.writes ? "odataDuplicateCancel" : "odataDuplicateName") as Control);
        this.duplicateDialog.open();
        if (state.writes) {
            // The focus is on Cancel; what the copy carries is said as well.
            InvisibleMessage.getInstance().announce(this.formatDuplicateWrites(state.writes), InvisibleMessageMode.Polite);
        }
    }

    public onDuplicateEdit(event: Event): void {
        const field = (event.getSource() as Control).data("field") as string;
        this.svc().setProperty(`/duplicate/errors/${field}`, "");
    }

    public onDuplicateRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/duplicate/user_context", key === "user");
        this.checkDuplicateDestination(true, true);
    }

    public onDuplicateCancel(): void {
        this.duplicateDialog?.close();
    }

    /**
     * Creates the copy and opens it. A refusal stays in the dialog.
     *
     * The server copies the definition as it is stored at that moment, so
     * the service is read again first: when its writes are not the ones the
     * dialog lists, the list is brought up to date, the dialog says so and
     * nothing is sent -- no write is copied without having been named here.
     */
    public async onDuplicateConfirm(): Promise<void> {
        const model = this.svc();
        if (this.working || !this.serviceName) {
            return;
        }
        // As Save does: a tap on Create that does not move the focus.
        this.storeShownDestination(this.byId("odataDuplicateDestination") as Input);
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
            const name = this.serviceName;
            const fresh = await this.withBusy(() => this.getAdminService().getODataService(name));
            const writes = this.copiedWrites(fresh.definition);
            if (writes !== state.writes) {
                this.setWorking(false);
                // With no write left there is no list to point at.
                const changed = this.text(writes ? "odataDuplicateChanged" : "odataDuplicateChangedNone");
                model.setProperty("/duplicate/writes", writes);
                model.setProperty("/duplicate/error", changed);
                InvisibleMessage.getInstance().announce(
                    [changed, this.formatDuplicateWrites(writes)].filter(Boolean).join(" "),
                    InvisibleMessageMode.Polite
                );
                return;
            }
            copy = await this.withBusy(() => this.getAdminService().duplicateODataService(name, body));
        } catch (error) {
            this.setWorking(false);
            this.showDuplicateRefusal(error);
            return;
        }
        this.setWorking(false);
        this.duplicateDialog?.close();
        if (!this.warnIfNotLive(copy, "reloadFailedServiceSaved")) {
            MessageToast.show(this.text("odataDuplicated"));
        }
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
                        this.showUsedBy();
                    }
                    resolve(action === discard);
                }
            });
        });
    }
}
