import JSONModel from "sap/ui/model/json/JSONModel";
import { MODEL_SIZE_LIMIT } from "../model/formatter";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import DateFormat from "sap/ui/core/format/DateFormat";
import { ValueState } from "sap/ui/core/library";
import type Dialog from "sap/m/Dialog";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type UI5Event from "sap/ui/base/Event";
import BaseController from "./BaseController";
import { CONVENTIONS_CHANGED, CONVENTIONS_CHANNEL, type ConventionsChangedData } from "../model/conventionsForm";
import { IdeError } from "../service/IdeService";
import type IdeService from "../service/IdeService";
import type { SessionSummary, SessionType, Stage } from "../service/types";
import {
    changedText, counts, hasFilters, matches, objectLine, sortSessions, statusView, type WorklistFilters
} from "../model/worklist";

/** The stages the stage filter offers, in walking order. */
const STAGES: Stage[] = ["chat", "design", "plan", "propose", "review", "done", "investigate"];

/** A 404: the session is gone (deleted in another tab); the list is read again. */
function isGone(e: unknown): boolean {
    return e instanceof IdeError && e.status === 404;
}

/** One table row: everything a cell shows, worded. */
interface Row {
    id: string;
    title: string;
    target: string;
    type: SessionType;
    typeText: string;
    typeState: string;
    stageText: string;
    statusText: string;
    statusState: string;
    statusInverted: boolean;
    statusIcon: string;
    reason: string;
    objects: string;
    changedText: string;
    updatedText: string;
    updatedTooltip: string;
}

/**
 * The worklist (`sessions` route): the caller's sessions with what each one
 * waits for, searchable and filterable, with New / Rename / Delete. A row
 * opens the `session` route.
 *
 * The object line, the changed-object count and a diagnose session's
 * findings come with every row of `GET /sessions` (`objects`,
 * `objects_total`, `changed_objects`, `findings_count`): no per-row reads.
 *
 * @namespace com.agent.ide.controller
 */
export default class Worklist extends BaseController {

    private sessions: SessionSummary[] = [];
    private loading?: Promise<void>;
    private newSessionDialog?: Promise<Dialog>;
    private renameDialog?: Promise<Dialog>;
    private readonly relative = DateFormat.getDateTimeInstance({ relative: true, relativeScale: "auto", relativeStyle: "wide" });
    private readonly absolute = DateFormat.getDateTimeInstance({ style: "medium" });

    public onInit(): void {
        const wl = new JSONModel({
            busy: false,
            loadFailed: false,
            rows: [] as Row[],
            summary: "",
            query: "",
            type: "all",
            stage: "all",
            waitingOnly: false,
            filtered: false,
            selectedId: "",
            stages: [{ key: "all", text: this.text("worklistStageAll") }]
                .concat(STAGES.map((s) => ({ key: s, text: this.stageText(s) }))),
            rename: { title: "", state: ValueState.None }
        });
        // JSONModel lists stop at 100 entries by default: a long worklist would be cut silently.
        wl.setSizeLimit(MODEL_SIZE_LIMIT);
        this.setModel(wl, "wl");
        // The shared NewSessionDialog fragment binds to `ide>/newSession` and `ide>/diagnoseTargets`.
        const ide = new JSONModel({ targets: [] as string[], diagnoseTargets: [] as string[], newSession: {} });
        ide.setSizeLimit(MODEL_SIZE_LIMIT);
        this.setModel(ide, "ide");
        this.getRouter().getRoute("sessions")?.attachPatternMatched(() => { void this.load(); });
        // A conventions write (App's dialog) changes which targets a new session may use.
        this.getOwnerComponent()?.getEventBus().subscribe(CONVENTIONS_CHANNEL, CONVENTIONS_CHANGED, this.onConventionsChanged, this);
    }

    public onExit(): void {
        this.getOwnerComponent()?.getEventBus().unsubscribe(CONVENTIONS_CHANNEL, CONVENTIONS_CHANGED, this.onConventionsChanged, this);
    }

    /**
     * The server's answer to a conventions write: the one target it answered
     * for is merged into what this page read itself (its list may be newer
     * than the dialog's), so the new-session dialog offers what the server holds.
     */
    private onConventionsChanged(_channel: string, _event: string, data: object): void {
        const { target, nonProduction } = data as ConventionsChangedData;
        const targets = (this.ide().getProperty("/targets") as string[] | undefined) ?? [];
        if (!targets.includes(target)) {
            this.ide().setProperty("/targets", [...targets, target]);
        }
        const diagnose = (this.ide().getProperty("/diagnoseTargets") as string[] | undefined) ?? [];
        if (nonProduction && !diagnose.includes(target)) {
            this.ide().setProperty("/diagnoseTargets", [...diagnose, target]);
        } else if (!nonProduction && diagnose.includes(target)) {
            this.ide().setProperty("/diagnoseTargets", diagnose.filter((t) => t !== target));
        }
    }

    private wl(): JSONModel {
        return this.getModel("wl") as JSONModel;
    }

    private ide(): JSONModel {
        return this.getModel("ide") as JSONModel;
    }

    private service(): IdeService {
        return this.getOwnerComponentTyped().getIdeService();
    }

    private t(key: string, args?: (string | number)[]): string {
        return this.text(key, args);
    }

    // --- Loading -------------------------------------------------------------

    /** Reads `/me` and the sessions; a second call while one runs waits for it. */
    private load(): Promise<void> {
        this.loading ??= this.doLoad().finally(() => { this.loading = undefined; });
        return this.loading;
    }

    private async doLoad(): Promise<void> {
        this.wl().setProperty("/busy", true);
        try {
            const [me, sessions] = await Promise.all([this.service().getMe(), this.service().listSessions()]);
            this.ide().setProperty("/targets", me.targets ?? []);
            this.ide().setProperty("/diagnoseTargets", me.diagnose_targets ?? []);
            this.sessions = sortSessions(sessions);
            this.wl().setProperty("/loadFailed", false);
        } catch (e) {
            this.sessions = [];
            // Not "no sessions": the empty state says the load failed and offers "Try again".
            this.wl().setProperty("/loadFailed", true);
            this.showError(e);
        } finally {
            this.refresh();
            this.wl().setProperty("/busy", false);
        }
    }

    /** "Try again" of the failed-load state. */
    public onRetryLoad(): void {
        void this.load();
    }

    /** Rebuilds the rows from the sessions, the search and the filters. */
    private refresh(): void {
        const query = String(this.wl().getProperty("/query") ?? "");
        const filters = this.filters();
        const rows = this.sessions.filter((s) => matches(s, query, filters)).map((s) => this.row(s));
        const c = counts(this.sessions);
        this.wl().setProperty("/rows", rows);
        this.wl().setProperty("/filtered", hasFilters(query, filters));
        this.wl().setProperty("/summary", this.t("worklistSummary", [c.waiting, c.running]));
        const selected = this.wl().getProperty("/selectedId") as string;
        if (selected && !rows.some((r) => r.id === selected)) {
            this.wl().setProperty("/selectedId", "");
        }
    }

    private filters(): WorklistFilters {
        const type = this.wl().getProperty("/type") as string;
        const stage = this.wl().getProperty("/stage") as string;
        return {
            type: type === "change" || type === "diagnose" ? type : null,
            stage: STAGES.includes(stage as Stage) ? stage as Stage : null,
            waiting: this.wl().getProperty("/waitingOnly") ? "any" : null
        };
    }

    private row(s: SessionSummary): Row {
        const status = statusView(s, (k, a) => this.t(k, a));
        const updated = s.updated_at ? new Date(s.updated_at) : null;
        const valid = !!updated && !Number.isNaN(updated.getTime());
        return {
            id: s.id,
            title: s.title,
            target: s.target,
            type: s.type,
            typeText: this.t(s.type === "diagnose" ? "sessionTypeDiagnose" : "sessionTypeChange"),
            // Indication colours from the theme: blue for change, orange for diagnose (no hard-coded colour).
            typeState: s.type === "diagnose" ? "Indication03" : "Indication05",
            stageText: this.stageText(s.stage),
            statusText: status.text,
            statusState: status.state,
            statusInverted: status.inverted,
            statusIcon: status.icon,
            reason: status.reason,
            objects: objectLine(s.objects ?? [], (k, a) => this.t(k, a), s.objects_total),
            changedText: changedText(s, (k, a) => this.t(k, a)),
            updatedText: valid ? this.relative.format(updated!) : "",
            updatedTooltip: valid ? this.absolute.format(updated!) : ""
        };
    }

    private stageText(stage: Stage): string {
        return this.t(`stage${stage.charAt(0).toUpperCase()}${stage.slice(1)}`);
    }

    // --- Selection after a rebuild --------------------------------------------

    public onUpdateFinished(): void {
        const table = this.byId("worklistTable") as Table;
        // Rows are rebuilt on every search or filter, so a remembered selection would point at
        // a row index, not a session: select the row of the selected session again, or none.
        const selected = this.wl().getProperty("/selectedId") as string;
        for (const item of table.getItems()) {
            const id = item.getBindingContext("wl")?.getProperty("id") as string | undefined;
            (item as ColumnListItem).setSelected(!!selected && id === selected);
        }
    }

    // --- Search, filters, selection, navigation ------------------------------

    public onSearch(event: UI5Event): void {
        const params = event.getParameters() as { newValue?: string; query?: string };
        this.wl().setProperty("/query", params.newValue ?? params.query ?? "");
        this.refresh();
    }

    public onFilter(): void {
        this.refresh();
    }

    public onSelectionChange(event: UI5Event): void {
        const item = (event.getParameters() as { listItem?: ColumnListItem }).listItem;
        this.wl().setProperty("/selectedId", item?.getSelected() ? item.getBindingContext("wl")?.getProperty("id") as string : "");
    }

    public onRowPress(event: UI5Event): void {
        const id = (event.getSource() as ColumnListItem).getBindingContext("wl")?.getProperty("id") as string | undefined;
        if (id) {
            this.getRouter().navTo("session", { id });
        }
    }

    private selected(): SessionSummary | undefined {
        const id = this.wl().getProperty("/selectedId") as string;
        return this.sessions.find((s) => s.id === id);
    }

    // --- New session (shared NewSessionDialog fragment) ----------------------

    public onNewSession(): void {
        const targets = (this.ide().getProperty("/targets") as string[]) ?? [];
        this.ide().setProperty("/newSession", {
            title: "",
            type: "change",
            targets,
            target: targets.length === 1 ? targets[0] : "",
            titleState: ValueState.None,
            targetState: ValueState.None
        });
        void this.newDialog().then((dialog) => dialog.open());
    }

    /** Switches the target list to the type's targets; keeps a valid choice, preselects a single one. */
    public onNewSessionTypeChange(): void {
        const type = String(this.ide().getProperty("/newSession/type") ?? "change") as SessionType;
        const targets = (this.ide().getProperty(type === "diagnose" ? "/diagnoseTargets" : "/targets") as string[]) ?? [];
        const current = String(this.ide().getProperty("/newSession/target") ?? "");
        this.ide().setProperty("/newSession/targets", targets);
        this.ide().setProperty("/newSession/target", targets.includes(current) ? current : targets.length === 1 ? targets[0] : "");
        this.ide().setProperty("/newSession/targetState", ValueState.None);
    }

    public async onNewSessionCreate(): Promise<void> {
        const title = String(this.ide().getProperty("/newSession/title") ?? "").trim();
        const target = String(this.ide().getProperty("/newSession/target") ?? "");
        this.ide().setProperty("/newSession/titleState", title ? ValueState.None : ValueState.Error);
        this.ide().setProperty("/newSession/targetState", target ? ValueState.None : ValueState.Error);
        if (!title || !target) {
            return;
        }
        const dialog = await this.newDialog();
        dialog.setBusy(true);
        try {
            const type = String(this.ide().getProperty("/newSession/type") ?? "change") as SessionType;
            const session = await this.service().createSession(title, target, type);
            this.sessions = sortSessions([session, ...this.sessions.filter((s) => s.id !== session.id)]);
            this.refresh();
            dialog.close();
            this.getRouter().navTo("session", { id: session.id });
        } catch (e) {
            this.showError(e);
        } finally {
            dialog.setBusy(false);
        }
    }

    public onNewSessionCancel(): void {
        void this.newDialog().then((dialog) => dialog.close());
    }

    public onNewSessionAfterClose(): void {
        this.ide().setProperty("/newSession/titleState", ValueState.None);
        this.ide().setProperty("/newSession/targetState", ValueState.None);
    }

    private newDialog(): Promise<Dialog> {
        this.newSessionDialog ??= this.loadFragment({ name: "com.agent.ide.fragment.NewSessionDialog" }) as Promise<Dialog>;
        return this.newSessionDialog;
    }

    // --- Rename ---------------------------------------------------------------

    public onRename(): void {
        const session = this.selected();
        if (!session) {
            return;
        }
        this.wl().setProperty("/rename", { id: session.id, title: session.title, state: ValueState.None });
        void this.renameDialogOf().then((dialog) => dialog.open());
    }

    public async onRenameSave(): Promise<void> {
        const id = this.wl().getProperty("/rename/id") as string;
        const title = String(this.wl().getProperty("/rename/title") ?? "").trim();
        this.wl().setProperty("/rename/state", title ? ValueState.None : ValueState.Error);
        if (!title || !id) {
            return;
        }
        const dialog = await this.renameDialogOf();
        dialog.setBusy(true);
        try {
            const saved = await this.service().renameSession(id, title);
            this.sessions = sortSessions(this.sessions.map((s) => (s.id === id ? saved : s)));
            this.refresh();
            dialog.close();
            MessageToast.show(this.t("sessionRenamed"));
        } catch (e) {
            this.showError(e);
            if (isGone(e)) {
                dialog.close();
                void this.load();
            }
        } finally {
            dialog.setBusy(false);
        }
    }

    public onRenameCancel(): void {
        void this.renameDialogOf().then((dialog) => dialog.close());
    }

    private renameDialogOf(): Promise<Dialog> {
        this.renameDialog ??= this.loadFragment({ name: "com.agent.ide.fragment.RenameSessionDialog" }) as Promise<Dialog>;
        return this.renameDialog;
    }

    // --- Delete ---------------------------------------------------------------

    public onDelete(): void {
        const session = this.selected();
        if (!session) {
            return;
        }
        const deleteAction = this.t("worklistDelete");
        MessageBox.confirm(this.t("deleteSessionConfirm", [session.title]), {
            title: this.t("deleteSessionTitle"),
            actions: [deleteAction, MessageBox.Action.CANCEL],
            emphasizedAction: deleteAction,
            onClose: (action: string | null) => {
                if (action === deleteAction) {
                    void this.deleteSession(session.id);
                }
            }
        });
    }

    private async deleteSession(id: string): Promise<void> {
        this.wl().setProperty("/busy", true);
        let reloading = false;
        try {
            await this.service().deleteSession(id);
            this.sessions = this.sessions.filter((s) => s.id !== id);
            this.wl().setProperty("/selectedId", "");
            (this.byId("worklistTable") as Table).removeSelections(true);
            this.refresh();
            MessageToast.show(this.t("sessionDeleted"));
        } catch (e) {
            this.showError(e);
            if (isGone(e)) {
                this.wl().setProperty("/selectedId", "");
                (this.byId("worklistTable") as Table).removeSelections(true);
                // The reload owns the busy state from here (it clears it when the list is in).
                reloading = true;
                void this.load();
            }
        } finally {
            if (!reloading) {
                this.wl().setProperty("/busy", false);
            }
        }
    }
}
