import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import { ValueState } from "sap/ui/core/library";
import ODataController from "./odata/ODataController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import odataCatalog, { type ODataErrorField, type ODataErrors } from "../model/odataCatalog";
import { canonical } from "../model/runsPanel";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type Dialog from "sap/m/Dialog";
import type SegmentedButton from "sap/m/SegmentedButton";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";
import type { ODataService, ODataServiceInput, ODataUsedBy } from "../service/types";

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

/**
 * One OData service of the catalogue: its header actions and the General
 * section. The definition (entity sets, operations) is loaded and sent back
 * whole with every save; its own sections come with later tasks.
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
            this.getOwnerComponentTyped().setLeaveGuard(() => this.confirmLeave());
            if (name === this.justCreated) {
                this.justCreated = undefined;
                return;
            }
            this.justCreated = undefined;
            void this.load(name);
        });
        this.getRouter().attachRouteMatched(this.onAnyRouteMatched, this);
        window.addEventListener("beforeunload", this.onBeforeUnload);
    }

    public onExit(): void {
        window.removeEventListener("beforeunload", this.onBeforeUnload);
        this.getRouter().detachRouteMatched(this.onAnyRouteMatched, this);
        this.getOwnerComponentTyped().setLeaveGuard(undefined);
    }

    /** Another page is shown: this one no longer has a say about leaving,
     *  and whatever it held is not a form any more. */
    private onAnyRouteMatched(event: Router$RouteMatchedEvent): void {
        if (event.getParameter("name") === ROUTE) {
            return;
        }
        this.getOwnerComponentTyped().setLeaveGuard(undefined);
        this.loadCount++;
        this.serviceName = undefined;
        this.svc().setData(this.blankState(""));
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
            // U6 fills this with the result of "Test call".
            test: null,
            duplicate: { name: "", destination: "", user_context: false, errors: {}, error: "", unsaved: false }
        };
    }

    /** Puts a stored service on the page, as loaded or as just saved. */
    private show(service: ODataService): void {
        this.serviceName = service.name;
        this.svc().setData({
            ...this.blankState(service.title),
            loaded: true, exists: true,
            data: odataCatalog.payloadOf(service), original: odataCatalog.payloadOf(service),
            used_by: service.used_by ?? [], has_write: service.has_write === true
        });
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

    // --- formatters ---------------------------------------------------------

    /** A field with a message is in error. */
    public formatErrorState(message: string | undefined): ValueState {
        return message ? ValueState.Error : ValueState.None;
    }

    /** "44 / 200", counting what the server counts. */
    public formatPurposeCounter(purpose: string | undefined): string {
        return this.text("odataPurposeCounter", [odataCatalog.purposeLength(purpose)]);
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

    // --- editing ------------------------------------------------------------

    /** Typing in a field takes its error away; the next save checks again. */
    public onEdit(event: Event): void {
        const field = (event.getSource() as Control).data("field") as string;
        this.svc().setProperty(`/errors/${field}`, "");
    }

    public onRunsAsChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/data/user_context", key === "user");
    }

    public onVersionChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.svc().setProperty("/data/odata_version", key === "v4" ? "v4" : "v2");
    }

    // --- save ---------------------------------------------------------------

    /**
     * Checks the form and saves it. On a service that agents use, a change
     * of identity or destination is confirmed first: it changes who those
     * agents act as in SAP.
     */
    public onSave(): void {
        const model = this.svc();
        if (this.working || model.getProperty("/loaded") !== true) {
            return;
        }
        model.setProperty("/saveError", "");
        if (!this.showProblems(odataCatalog.validate(this.data()))) {
            return;
        }

        const usedBy = model.getProperty("/used_by") as ODataUsedBy[];
        const question = model.getProperty("/isNew") || !usedBy.length ? "" : this.identityQuestion(usedBy);
        if (!question) {
            void this.save();
            return;
        }
        const save = this.text("save");
        MessageBox.warning(question, {
            title: this.text("odataIdentityConfirmTitle"),
            actions: [save, MessageBox.Action.CANCEL],
            emphasizedAction: save,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === save) {
                    void this.save();
                }
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
     * What saving changes about who the agents in `usedBy` act as, or ""
     * when it changes nothing of the kind.
     */
    private identityQuestion(usedBy: ODataUsedBy[]): string {
        const change = odataCatalog.identityChange(this.original(), this.data());
        if (!change.runsAs && !change.destination) {
            return "";
        }
        const names = usedBy.map((used) => used.agent);
        const parts = [names.length === 1
            ? this.text("odataIdentityAgentsOne", [names[0]])
            : this.text("odataIdentityAgentsMany", [names.join(", ")])];
        if (change.runsAs) {
            // No arguments: these two texts carry an apostrophe.
            parts.push(this.text(change.runsAs === "technical" ? "odataIdentityToTechnical" : "odataIdentityToUser"));
        }
        if (change.destination) {
            parts.push(this.text("odataIdentityDestination", [change.destination.from, change.destination.to]));
        }
        return parts.join("\n\n");
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
        const payload = odataCatalog.payloadOf(this.data());
        this.setWorking(true);
        let saved: ODataService;
        try {
            saved = await this.withBusy(() => (isNew
                ? this.getAdminService().createODataService(payload)
                : this.getAdminService().updateODataService(this.serviceName as string, payload)));
        } catch (error) {
            this.setWorking(false);
            this.showRefusal(error, isNew);
            return;
        }
        this.setWorking(false);
        this.show(saved);
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
     * the whole text is shown above the form. The texts are the server's,
     * in English, and are shown as text. A 409 on create is about the name.
     */
    private showRefusal(error: unknown, isNew: boolean): void {
        const model = this.svc();
        if (error instanceof AdminError && error.status === 422) {
            const byLoc = odataCatalog.serverErrors(error.detail);
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
                ? (error.detail ? this.text("odataSaveRefused", [error.detail]) : this.text("odataSaveFailed"))
                : "");
            this.focusFirstError(errors);
            return;
        }
        if (error instanceof AdminError && error.status === 409 && isNew && error.detail) {
            model.setProperty("/errors", { name: error.detail });
            this.focusFirstError({ name: error.detail });
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
                    }
                    resolve(action === discard);
                }
            });
        });
    }
}
