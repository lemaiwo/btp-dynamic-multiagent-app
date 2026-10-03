import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import type Dialog from "sap/m/Dialog";
import type ScrollContainer from "sap/m/ScrollContainer";
import type TextArea from "sap/m/TextArea";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import type Tree from "sap/m/Tree";
import type ListItemBase from "sap/m/ListItemBase";
import type UI5Event from "sap/ui/base/Event";
import { InvisibleMessageMode, ValueState } from "sap/ui/core/library";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import { buildTree, OBJECT_TYPES } from "../model/workspaceTree";
import IdeService from "../service/IdeService";
import type {
    ArtifactKind, ArtifactEventData, FileSummary, ObjectHit, SessionDetail, SessionSummary, SseEvent, Stage
} from "../service/types";
import { canApprove, canRevise, canSend, nextStage, stageTokens, type Gate } from "../model/stageGate";
import { activeTool, newRun, reduceRun, toChatItem, type ChatItem, type RunState } from "../model/chatRun";
import ActivityState, { eventRows, lastActivity, todoView } from "../model/activity";
import { gateErrorText, runErrorText } from "../model/errorText";
import RunWatch from "../model/runWatch";
import { IdeError } from "../service/IdeService";
import type TabContainer from "sap/m/TabContainer";
import type TabContainerItem from "sap/m/TabContainerItem";
import type CodeEditor from "sap/ui/codeeditor/CodeEditor";
import type Control from "sap/ui/core/Control";
import { ensureDiff, ensureMarkdown } from "../model/vendor";
import { applyThemeVars } from "../model/themeVars";
import { renderMarkdown } from "../model/markdown";
import { toAceAnnotations, type AceAnnotation } from "../model/lintAnnotations";
import type { DiffLabels } from "../model/diffModel";
import {
    docKey, fileKey, newDocTab, newFileTab, showsLintedText, withFile, withView,
    type DocVersion, type EditorTab
} from "../model/editorTabs";

type ItemEvent = UI5Event<{ listItem?: ListItemBase }>;
type SearchEvent = UI5Event<{ query?: string; clearButtonPressed?: boolean }>;
type TabEvent = UI5Event<{ item?: TabContainerItem }>;
type MenuEvent = UI5Event<{ item?: Control }>;
/** The fields of the jQuery event UI5 hands to an event delegate's onkeydown. */
interface KeyDownEvent { ctrlKey: boolean; metaKey: boolean; key?: string; preventDefault(): void }
/** The operations on an editor tab; each has its own request token. */
type TabOp = "open" | "lint" | "refresh";

/** The slice of the Ace editor behind sap.ui.codeeditor.CodeEditor that the lint markers use. */
interface AceEditor { getSession?(): { setAnnotations(annotations: AceAnnotation[]): void } | undefined }

const DOC_KINDS: ArtifactKind[] = ["design", "plan", "note", "review"];
const DOC_KIND_KEYS: Record<ArtifactKind, string> = {
    design: "docKindDesign", plan: "docKindPlan", note: "docKindNote", review: "docKindReview"
};
const STAGE_KEYS: Record<Stage, string> = {
    chat: "stageChat", design: "stageDesign", plan: "stagePlan",
    propose: "stagePropose", review: "stageReview", done: "stageDone"
};
const TOKEN_KEYS = { done: "stageTokenDone", current: "stageTokenCurrent", upcoming: "stageTokenUpcoming" };
/** The document a stage's run delivers (its Approve gate needs it); a run brings that tab to the front. */
const STAGE_DELIVERABLE: Partial<Record<Stage, ArtifactKind>> = { design: "design", plan: "plan", review: "review" };
/** At most one re-parse of the streamed markdown per this many ms (it re-parses the whole answer). */
const RENDER_INTERVAL_MS = 100;

/**
 * The workbench: explorer, editor and assistant panes side by side.
 *
 * The explorer (view/Explorer.fragment.xml) lists the caller's sessions,
 * shows the selected session's workspace as a tree and opens ABAP objects
 * into it. The editor (view/Editor.fragment.xml) opens a workspace file as a
 * read-only tab with Source / Proposed / Diff and lint markers, and the
 * session's documents (design, plan, note, review) as rendered markdown.
 * The assistant (view/Assistant.fragment.xml) shows the stage bar with
 * Approve / Revise / Stop and the chat, whose runs stream over SSE
 * (model/chatRun folds the events; model/stageGate mirrors the gates).
 * Everything they show lives in the view-local `ide` model; the shell's
 * `appView` model gets the open session's title and target.
 *
 * @namespace com.agent.ide.controller
 */
export default class Ide extends BaseController {

    public readonly formatter = formatter;

    private readonly service = new IdeService();
    private newSessionDialog?: Promise<Dialog>;
    private openObjectDialog?: Promise<Dialog>;
    /**
     * Bumped on every session switch. An answer that arrives after the
     * selection changed belongs to another session and is dropped.
     */
    private loadSeq = 0;
    /** The same guard for the session list (create and delete both reload it). */
    private sessionsSeq = 0;
    /**
     * The latest request per editor tab key and operation (`<key>#<op>`).
     * Keys repeat across sessions (`file:<path>`, `doc:<kind>`) and a
     * document can be asked for twice in a row (version switches), so an
     * answer is applied only by the request that is still the latest of its
     * kind for its tab in the same session. Operations have separate tokens:
     * a Lint does not make a Refresh in flight stale. A tab is busy while
     * any of its operations is pending.
     */
    private tabRequests = new Map<string, number>();
    private tabRequestSeq = 0;

    // --- Assistant state (not in the model: rendered from it) -------------
    private reviseDialog?: Promise<Dialog>;
    /** Aborts reading the current run's stream (Stop, or a session switch). */
    private runAbort?: AbortController;
    /** A run this page started is streaming. */
    private localRunning = false;
    private run: RunState = newRun();
    /** The activity panel's data: tool timeline and plan of the last run. */
    private activity = new ActivityState();
    /** Ids of the timeline rows whose output is shown in full. */
    private expandedOutputs = new Set<string>();
    /** The pending animation frame of the throttled stream render. */
    private frame?: number;
    /** The pending wait before that frame, when the last render was too recent. */
    private renderTimer?: ReturnType<typeof setTimeout>;
    /** The cancel call of a Stop; the run's clean-up waits for it. */
    private stopping?: Promise<void>;
    /** Paths named by `file` events, reloaded together. */
    private pendingFiles = new Set<string>();
    /** The loop that reloads `pendingFiles` until none is left (one at a time). */
    private filesRefresh?: Promise<void>;
    /** Polls a session that is running without a stream of this page (see model/runWatch). */
    private runWatch = new RunWatch((sid) => this.pollRun(sid));
    /** The chat draft per session id; the input shows the selected session's. */
    private drafts = new Map<string, string>();
    /** Refused revise feedback per session id, offered again by the next Revise there. */
    private reviseDrafts = new Map<string, string>();
    /** The session whose reload after a run failed; its retries stay silent until one works. */
    private afterRunFailedSid = "";
    /** When the streamed answer was last re-rendered (performance.now()). */
    private lastRender = 0;

    public onInit(): void {
        this.setModel(new JSONModel({
            targets: [] as string[],
            sessions: [] as SessionSummary[],
            sessionsBusy: false,
            selectedId: "",
            session: null as SessionDetail | null,
            fileList: [] as FileSummary[],
            files: [],
            filesBusy: false,
            selectedPath: "",
            objectTypes: [...OBJECT_TYPES],
            newSession: { title: "", target: "", titleState: ValueState.None, targetState: ValueState.None },
            openObject: { type: "CLAS", name: "", results: [] as ObjectHit[], searching: false },
            tabs: [] as EditorTab[],
            docKinds: [] as { kind: ArtifactKind; label: string }[],
            messages: [] as ChatItem[],
            draft: "",
            activity: { title: "", todos: [], events: [] },
            run: { html: "", status: "", error: "", usageText: "" },
            assistant: {
                stages: [], running: false,
                canApprove: false, approveTooltip: "", canRevise: false, reviseTooltip: "", canSend: false
            },
            revise: { title: "", feedback: "", state: ValueState.None, stateText: "" }
        }), "ide");
        applyThemeVars();
        this.updateAssistant();
        this.renderActivity();
        // Ctrl+Enter (Cmd+Enter on a Mac) sends; the placeholder says so.
        this.byId("chatInput")?.addEventDelegate({
            onkeydown: (event: KeyDownEvent) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                    event.preventDefault();
                    this.onSend();
                }
            }
        });
        // An HBox has no ariaLabelledBy: name the stage bar as a group on its DOM.
        const stageBar = this.byId("stageBar");
        stageBar?.addEventDelegate({
            onAfterRendering: () => {
                const dom = stageBar.getDomRef();
                dom?.setAttribute("role", "group");
                dom?.setAttribute("aria-labelledby", this.createId("stageBarLabel") ?? "stageBarLabel");
            }
        });
        void this.loadInitial();
    }

    public onExit(): void {
        // No model writes here: stop the poll, the stream read and the render timers only.
        this.runWatch.stop();
        this.runAbort?.abort();
        this.cancelRender();
    }

    // A method, not a getter: the UI5 class transform copies members into
    // Controller.extend(), which would evaluate a getter at module load.
    private ide(): JSONModel {
        return this.getModel("ide") as JSONModel;
    }

    // --- Loading -------------------------------------------------------------

    private async loadInitial(): Promise<void> {
        this.ide().setProperty("/sessionsBusy", true);
        try {
            const [me, sessions] = await Promise.all([this.service.getMe(), this.service.listSessions()]);
            this.ide().setProperty("/targets", me.targets);
            this.ide().setProperty("/sessions", sessions);
            if (sessions.length) {
                await this.selectSession(sessions[0].id);
            }
        } catch (e) {
            this.showError(e);
        } finally {
            this.ide().setProperty("/sessionsBusy", false);
        }
    }

    private async reloadSessions(): Promise<void> {
        const seq = ++this.sessionsSeq;
        const sessions = await this.service.listSessions();
        if (seq === this.sessionsSeq) {
            this.ide().setProperty("/sessions", sessions);
        }
    }

    /** Selects a session (or none, for "") and loads its detail and workspace. */
    private async selectSession(sid: string): Promise<void> {
        const seq = ++this.loadSeq;
        this.afterRunFailedSid = "";
        this.keepDraft();
        this.ide().setProperty("/selectedId", sid);
        this.ide().setProperty("/draft", this.drafts.get(sid) ?? "");
        this.ide().setProperty("/selectedPath", "");
        // Nothing of the previous session stays on screen while the new one loads.
        this.applyDetail(null);
        this.resetOpenObject();
        this.ide().setProperty("/tabs", []);
        this.tabRequests.clear();
        this.resetChat();
        if (!sid) {
            return;
        }
        this.ide().setProperty("/filesBusy", true);
        try {
            const detail = await this.service.getSession(sid);
            if (seq === this.loadSeq) {
                this.applyDetail(detail);
                void this.loadMessages(sid, seq);
            }
        } catch (e) {
            if (seq === this.loadSeq) {
                this.showError(e);
            }
        } finally {
            if (seq === this.loadSeq) {
                this.ide().setProperty("/filesBusy", false);
            }
        }
    }

    private applyDetail(detail: SessionDetail | null): void {
        this.ide().setProperty("/session", detail);
        const kinds = new Set((detail?.artifacts ?? []).map((a) => a.kind));
        this.ide().setProperty("/docKinds", DOC_KINDS.filter((k) => kinds.has(k)).map((kind) => ({
            kind, label: this.text(DOC_KIND_KEYS[kind])
        })));
        this.setFiles(detail?.files ?? []);
        const appView = this.getView()?.getModel("appView") as JSONModel | undefined;
        appView?.setProperty("/sessionTitle", detail?.title ?? "");
        appView?.setProperty("/target", detail?.target ?? "");
        this.watchRun(detail);
        this.updateAssistant();
    }

    private setFiles(files: FileSummary[]): void {
        this.ide().setProperty("/fileList", files);
        this.ide().setProperty("/files", buildTree(files));
        // A workspace is a few levels deep (src > TYPE > file); show it whole.
        (this.byId("workspaceTree") as Tree | undefined)?.expandToLevel(10);
    }

    /** Reloads the workspace of `sid`, unless the selection moved on meanwhile. */
    private async reloadFiles(sid: string, seq: number): Promise<boolean> {
        const files = await this.service.listFiles(sid);
        if (seq !== this.loadSeq) {
            return false;
        }
        this.setFiles(files);
        return true;
    }

    private resetOpenObject(): void {
        this.ide().setProperty("/openObject", {
            type: "CLAS", name: "", results: [] as ObjectHit[], searching: false
        });
    }

    // --- Session list --------------------------------------------------------

    public onSessionSelect(event: ItemEvent): void {
        const ctx = event.getParameter("listItem")?.getBindingContext("ide");
        const sid = ctx ? ctx.getProperty("id") as string : "";
        if (sid && sid !== this.ide().getProperty("/selectedId")) {
            void this.selectSession(sid);
        }
    }

    public formatTarget(target: string | null | undefined): string {
        return target ? this.text("sessionTarget", [target]) : "";
    }

    public onNewSession(): void {
        const targets = this.ide().getProperty("/targets") as string[];
        this.ide().setProperty("/newSession", {
            title: "",
            // One target is the common case; preselect it.
            target: targets.length === 1 ? targets[0] : "",
            titleState: ValueState.None,
            targetState: ValueState.None
        });
        void this.dialog("newSession").then((dialog) => dialog.open());
    }

    public async onNewSessionCreate(): Promise<void> {
        const title = String(this.ide().getProperty("/newSession/title") ?? "").trim();
        const target = String(this.ide().getProperty("/newSession/target") ?? "");
        this.ide().setProperty("/newSession/titleState", title ? ValueState.None : ValueState.Error);
        this.ide().setProperty("/newSession/targetState", target ? ValueState.None : ValueState.Error);
        if (!title || !target) {
            return;
        }
        const dialog = await this.dialog("newSession");
        dialog.setBusy(true);
        try {
            const session = await this.service.createSession(title, target);
            dialog.close();
            await this.reloadSessions();
            await this.selectSession(session.id);
        } catch (e) {
            this.showError(e);
        } finally {
            dialog.setBusy(false);
        }
    }

    public onNewSessionCancel(): void {
        void this.dialog("newSession").then((dialog) => dialog.close());
    }

    public onNewSessionAfterClose(): void {
        this.ide().setProperty("/newSession/titleState", ValueState.None);
        this.ide().setProperty("/newSession/targetState", ValueState.None);
    }

    public onDeleteSession(): void {
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        const sid = this.ide().getProperty("/selectedId") as string;
        if (!sid) {
            return;
        }
        MessageBox.confirm(this.text("deleteSessionConfirm", [session?.title ?? sid]), {
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.deleteSession(sid);
                }
            }
        });
    }

    private async deleteSession(sid: string): Promise<void> {
        this.ide().setProperty("/sessionsBusy", true);
        try {
            await this.service.deleteSession(sid);
            this.drafts.delete(sid);
            this.reviseDrafts.delete(sid);
            if (this.ide().getProperty("/selectedId") === sid) {
                this.ide().setProperty("/draft", "");
            }
            await this.reloadSessions();
            const sessions = this.ide().getProperty("/sessions") as SessionSummary[];
            await this.selectSession(sessions[0]?.id ?? "");
            MessageToast.show(this.text("sessionDeleted"));
        } catch (e) {
            this.showError(e);
        } finally {
            this.ide().setProperty("/sessionsBusy", false);
        }
    }

    // --- Workspace tree ------------------------------------------------------

    public onFileSelect(event: ItemEvent): void {
        const ctx = event.getParameter("listItem")?.getBindingContext("ide");
        if (ctx && !ctx.getProperty("folder")) {
            const path = ctx.getProperty("path") as string;
            this.ide().setProperty("/selectedPath", path);
            void this.openFile(path);
        }
    }

    // --- Open object ---------------------------------------------------------

    public onOpenObjectSearch(event: SearchEvent): void {
        if (event.getParameter("clearButtonPressed") || !this.ide().getProperty("/selectedId")) {
            return;
        }
        const query = String(event.getParameter("query") ?? "").trim();
        this.ide().setProperty("/openObject/name", query.toUpperCase());
        this.ide().setProperty("/openObject/results", []);
        void this.dialog("openObject").then((dialog) => {
            dialog.open();
            if (query) {
                void this.onObjectSearch();
            }
        });
    }

    public async onObjectSearch(): Promise<void> {
        const q = String(this.ide().getProperty("/openObject/name") ?? "").trim();
        const target = (this.ide().getProperty("/session") as SessionDetail | null)?.target;
        if (!q || !target) {
            return;
        }
        const seq = this.loadSeq;
        this.ide().setProperty("/openObject/searching", true);
        try {
            const results = await this.service.searchObjects(target, q);
            if (seq === this.loadSeq) {
                this.ide().setProperty("/openObject/results", results);
            }
        } catch (e) {
            if (seq === this.loadSeq) {
                this.showError(e);
            }
        } finally {
            this.ide().setProperty("/openObject/searching", false);
        }
    }

    public formatHitInfo(type: string, pkg: string): string {
        return pkg ? this.text("objectHitInfo", [type, pkg]) : type;
    }

    public async onObjectHitPress(event: ItemEvent): Promise<void> {
        const hit = event.getParameter("listItem")?.getBindingContext("ide")?.getObject() as ObjectHit | undefined;
        if (!hit) {
            return;
        }
        this.ide().setProperty("/openObject/type", hit.type);
        this.ide().setProperty("/openObject/name", hit.name);
        await this.onOpenObject();
    }

    public async onOpenObject(): Promise<void> {
        const sid = this.ide().getProperty("/selectedId") as string;
        const type = String(this.ide().getProperty("/openObject/type") ?? "");
        const name = String(this.ide().getProperty("/openObject/name") ?? "").trim();
        if (!sid || !type || !name) {
            return;
        }
        const seq = this.loadSeq;
        const dialog = await this.dialog("openObject");
        dialog.setBusy(true);
        try {
            const file = await this.service.openObject(sid, type, name);
            dialog.close();
            if (seq !== this.loadSeq || !await this.reloadFiles(sid, seq)) {
                return;
            }
            this.ide().setProperty("/selectedPath", file.path);
            void this.openFile(file.path);
            MessageToast.show(this.text("objectOpened", [file.path]));
        } catch (e) {
            if (seq === this.loadSeq) {
                this.showError(e);
            }
        } finally {
            dialog.setBusy(false);
        }
    }

    public onOpenObjectCancel(): void {
        void this.dialog("openObject").then((dialog) => dialog.close());
    }


    // --- Editor --------------------------------------------------------------

    private tabs(): EditorTab[] {
        return this.ide().getProperty("/tabs") as EditorTab[];
    }

    private tab(key: string): EditorTab | undefined {
        return this.tabs().find((t) => t.key === key);
    }

    /**
     * Starts an `op` request for the tab `key`. `current()` is true while no
     * later request of the same operation for that tab (or a session
     * switch) superseded it; `finish()` ends it and clears the tab's busy
     * flag once none of its operations is pending.
     */
    private tabRequest(key: string, op: TabOp): { current: () => boolean; finish: () => void } {
        const id = `${key}#${op}`;
        const token = ++this.tabRequestSeq;
        const seq = this.loadSeq;
        this.tabRequests.set(id, token);
        const current = (): boolean => seq === this.loadSeq && this.tabRequests.get(id) === token;
        return {
            current,
            finish: () => {
                if (current()) {
                    this.tabRequests.delete(id);
                    this.patchTab(key, { busy: [...this.tabRequests.keys()].some((k) => k.startsWith(`${key}#`)) });
                }
            }
        };
    }

    /** Replaces the tab `key` with `patch` applied; a tab closed meanwhile stays closed. */
    private patchTab(key: string, patch: Partial<EditorTab>): EditorTab | undefined {
        const index = this.tabs().findIndex((t) => t.key === key);
        if (index < 0) {
            return undefined;
        }
        const next = { ...this.tabs()[index], ...patch };
        this.ide().setProperty(`/tabs/${index}`, next);
        return next;
    }

    private addTab(tab: EditorTab, select = true): void {
        this.ide().setProperty("/tabs", [...this.tabs(), tab]);
        if (select) {
            this.selectTab(tab.key);
        }
    }

    private tabItem(key: string): TabContainerItem | undefined {
        const container = this.byId("editorTabs") as TabContainer | undefined;
        return container?.getItems().find((item) => item.getKey() === key);
    }

    private selectTab(key: string): void {
        const item = this.tabItem(key);
        if (item) {
            (this.byId("editorTabs") as TabContainer).setSelectedItem(item);
        }
    }

    /** The key of the tab a control inside a tab belongs to. */
    private tabKeyOf(control: Control | undefined): string {
        return (control?.getBindingContext("ide")?.getProperty("key") as string | undefined) ?? "";
    }

    private diffLabels(tab: EditorTab): DiffLabels {
        return {
            left: this.text("diffSourceHeader"),
            right: this.text("diffProposedHeader"),
            caption: this.text("diffCaption", [tab.path]),
            markHeader: this.text("diffMarkHeader"),
            added: this.text("diffAdded"),
            removed: this.text("diffRemoved"),
            changed: this.text("diffChanged"),
            unchanged: (count: number) => this.text("diffUnchanged", [count]),
            tooLarge: this.text("diffTooLarge")
        };
    }

    /** Recomputes the editor text or the diff for the tab's mode and refreshes its lint markers. */
    private async showTab(key: string): Promise<void> {
        const tab = this.tab(key);
        if (!tab || tab.kind !== "file") {
            return;
        }
        if (tab.mode === "diff") {
            await ensureDiff();
        }
        const current = this.tab(key);
        if (current) {
            this.patchTab(key, withView(current, this.diffLabels(current)));
            this.applyAnnotations(key);
        }
    }

    /**
     * Sets the tab's SAPLint findings as Ace gutter markers, only while the
     * editor shows the text that was linted.
     */
    private applyAnnotations(key: string): void {
        const tab = this.tab(key);
        const editor = this.tabItem(key)?.findAggregatedObjects(true, (c) => c.isA("sap.ui.codeeditor.CodeEditor"))[0] as
            CodeEditor | undefined;
        if (!tab || !editor) {
            return;
        }
        // Not there yet while the CodeEditor has not created Ace (or failed to load it).
        const ace = (editor as unknown as { getInternalEditorInstance(): AceEditor | undefined }).getInternalEditorInstance();
        ace?.getSession?.()?.setAnnotations(showsLintedText(tab) ? toAceAnnotations(tab.lint) : []);
    }

    private lintPatch(lint: EditorTab["lint"]): Partial<EditorTab> {
        const errors = lint.some((f) => (f.severity || "").toLowerCase().startsWith("e"));
        return {
            lint,
            lintSummary: lint.length ? this.text("lintFindings", [lint.length]) : this.text("lintClean"),
            lintState: !lint.length ? ValueState.Success : errors ? ValueState.Error : ValueState.Warning
        };
    }

    /** Opens a workspace file as a tab, or selects its tab when it is open. */
    public async openFile(path: string): Promise<void> {
        const sid = this.ide().getProperty("/selectedId") as string;
        const key = fileKey(path);
        if (!sid) {
            return;
        }
        if (this.tab(key)) {
            this.selectTab(key);
            return;
        }
        const request = this.tabRequest(key, "open");
        this.addTab(newFileTab(path));
        try {
            const file = await this.service.getFile(sid, path);
            const tab = this.tab(key);
            if (!request.current() || !tab) {
                return;
            }
            this.patchTab(key, withFile(tab, file));
            await this.showTab(key);
        } catch (e) {
            if (request.current()) {
                this.showError(e);
            }
        } finally {
            request.finish();
        }
    }

    public onEditorModeChange(event: UI5Event): void {
        void this.showTab(this.tabKeyOf(event.getSource() as Control));
    }

    public onEditorTabSelect(event: TabEvent): void {
        const key = event.getParameter("item")?.getKey();
        if (key) {
            this.applyAnnotations(key);
        }
    }

    public onEditorTabClose(event: TabEvent): void {
        // The items are bound: remove the tab from the model, not the control.
        event.preventDefault();
        const key = event.getParameter("item")?.getKey();
        this.ide().setProperty("/tabs", this.tabs().filter((t) => t.key !== key));
    }

    public async onLintFile(event: UI5Event): Promise<void> {
        const key = this.tabKeyOf(event.getSource() as Control);
        const tab = this.tab(key);
        const sid = this.ide().getProperty("/selectedId") as string;
        if (!tab || !sid) {
            return;
        }
        const request = this.tabRequest(key, "lint");
        this.patchTab(key, { busy: true });
        try {
            const findings = await this.service.lintFile(sid, tab.path);
            if (request.current()) {
                this.patchTab(key, this.lintPatch(findings));
                this.applyAnnotations(key);
            }
        } catch (e) {
            if (request.current()) {
                this.showError(e);
            }
        } finally {
            request.finish();
        }
    }

    public async onRefreshFile(event: UI5Event): Promise<void> {
        const key = this.tabKeyOf(event.getSource() as Control);
        const tab = this.tab(key);
        const sid = this.ide().getProperty("/selectedId") as string;
        if (!tab || !sid) {
            return;
        }
        const request = this.tabRequest(key, "refresh");
        this.patchTab(key, { busy: true });
        try {
            const file = await this.service.refreshFile(sid, tab.path);
            const now = this.tab(key);
            if (request.current() && now) {
                this.patchTab(key, withFile(now, file, true));
                await this.showTab(key);
                MessageToast.show(this.text("fileRefreshed", [tab.path]));
            }
        } catch (e) {
            if (request.current()) {
                this.showError(e);
            }
        } finally {
            request.finish();
        }
    }

    public onDocumentMenuSelect(event: MenuEvent): void {
        const kind = event.getParameter("item")?.getBindingContext("ide")?.getProperty("kind") as ArtifactKind | undefined;
        if (kind) {
            void this.openDocument(kind);
        }
    }

    /**
     * Opens the session's documents of one kind as a tab on the latest
     * version; `select: false` opens it behind the tab the user looks at.
     */
    public async openDocument(kind: ArtifactKind, select = true): Promise<void> {
        const key = docKey(kind);
        if (this.tab(key)) {
            if (select) {
                this.selectTab(key);
            }
            return;
        }
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        const versions = this.docVersions(kind);
        if (!session || !versions.length) {
            return;
        }
        this.addTab(newDocTab(kind, this.text(DOC_KIND_KEYS[kind]), versions), select);
        await this.loadDocument(key, versions[0].id);
    }

    /** The session's versions of a document kind, latest first. */
    private docVersions(kind: ArtifactKind): DocVersion[] {
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        return (session?.artifacts ?? [])
            .filter((a) => a.kind === kind)
            .sort((a, b) => b.version - a.version)
            .map((a) => ({ id: a.id, label: this.text("artifactVersion", [a.version]) }));
    }

    public onDocVersionChange(event: UI5Event<{ selectedItem?: Control }>): void {
        const key = this.tabKeyOf(event.getSource() as Control);
        const aid = (event.getParameter("selectedItem") as unknown as { getKey(): string } | undefined)?.getKey();
        if (key && aid) {
            void this.loadDocument(key, aid);
        }
    }

    private async loadDocument(key: string, aid: string): Promise<void> {
        const sid = this.ide().getProperty("/selectedId") as string;
        const request = this.tabRequest(key, "open");
        this.patchTab(key, { busy: true, artifactId: aid });
        try {
            const [artifact] = await Promise.all([this.service.getArtifact(sid, aid), ensureMarkdown()]);
            if (request.current()) {
                this.patchTab(key, { html: renderMarkdown(artifact.content) });
            }
        } catch (e) {
            if (request.current()) {
                this.showError(e);
            }
        } finally {
            request.finish();
        }
    }

    // --- Assistant: stage bar ------------------------------------------------

    private stageLabel(stage: Stage | null | undefined): string {
        return stage && STAGE_KEYS[stage] ? this.text(STAGE_KEYS[stage]) : String(stage ?? "");
    }

    /** True while a run of the open session is going on, here or elsewhere. */
    private running(): boolean {
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        return this.localRunning || session?.status === "running";
    }

    /** Recomputes the stage bar and the Approve / Revise / Send gates (model/stageGate). */
    private updateAssistant(): void {
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        const gated = session ? { stage: session.stage, status: this.running() ? "running" as const : session.status } : null;
        const reason = (gate: Gate, okText: string): string => (gate.ok ? okText : this.text(gate.reasonKey ?? "gateNoSession"));
        const approve = canApprove(gated, session?.artifacts ?? [], session?.files ?? []);
        const revise = canRevise(gated);
        const next = session ? nextStage(session.stage) : null;
        this.ide().setProperty("/assistant", {
            stages: stageTokens(session?.stage).map((t) => ({
                stage: t.stage,
                state: t.state,
                label: this.stageLabel(t.stage),
                tooltip: this.text(TOKEN_KEYS[t.state], [this.stageLabel(t.stage)])
            })),
            running: this.running(),
            canApprove: approve.ok,
            approveTooltip: reason(approve, this.text("approveTooltip", [this.stageLabel(next)])),
            canRevise: revise.ok,
            reviseTooltip: reason(revise, this.text("reviseTooltip")),
            canSend: canSend(gated).ok
        });
    }

    public async onApprove(): Promise<void> {
        const sid = this.ide().getProperty("/selectedId") as string;
        if (!sid) {
            return;
        }
        const seq = this.loadSeq;
        this.ide().setProperty("/assistant/canApprove", false);
        try {
            const session = await this.service.approve(sid);
            if (seq !== this.loadSeq) {
                return;
            }
            await Promise.all([this.refreshSession(sid, seq), this.reloadSessions()]);
            MessageToast.show(this.text("stageApproved", [this.stageLabel(session.stage)]));
        } catch (e) {
            if (seq === this.loadSeq) {
                this.showGateError(e, sid, seq);
            }
        } finally {
            if (seq === this.loadSeq) {
                this.updateAssistant();
            }
        }
    }

    /** A refused action: the gate's text in a message box; a stale view is reloaded. */
    private showGateError(e: unknown, sid: string, seq: number, onClose?: () => void): void {
        MessageBox.error(gateErrorText(e, (key, args) => this.text(key, args)), { onClose });
        if (e instanceof IdeError && e.status === 409) {
            void this.refreshSession(sid, seq).catch(() => undefined);
        }
    }

    public onRevise(): void {
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        const sid = this.ide().getProperty("/selectedId") as string;
        const feedback = this.reviseDrafts.get(sid) ?? "";
        this.reviseDrafts.delete(sid);
        this.ide().setProperty("/revise", {
            title: this.text("reviseDialogTitle", [this.stageLabel(session?.stage)]),
            feedback,
            state: ValueState.None,
            stateText: ""
        });
        void this.dialog("revise").then((dialog) => dialog.open());
    }

    public async onReviseSend(): Promise<void> {
        const feedback = String(this.ide().getProperty("/revise/feedback") ?? "").trim();
        if (!feedback) {
            this.reviseInvalid(this.text("reviseFeedbackRequired"));
            return;
        }
        if (this.running()) {
            // A run started while the dialog was open: keep the feedback here.
            this.reviseInvalid(this.text("gateRunInProgress"));
            return;
        }
        (await this.dialog("revise")).close();
        await this.startRun("revise", feedback);
    }

    private reviseInvalid(stateText: string): void {
        this.ide().setProperty("/revise/state", ValueState.Error);
        this.ide().setProperty("/revise/stateText", stateText);
    }

    /**
     * Opens the revise dialog again with the feedback the server refused,
     * unless the user has moved to another session meanwhile (the message
     * box closes later): then the feedback waits for the next Revise in its
     * own session instead of opening over another one.
     */
    private reopenRevise(sid: string, seq: number, feedback: string): void {
        if (seq !== this.loadSeq) {
            this.reviseDrafts.set(sid, feedback);
            this.ide().setProperty("/revise/feedback", "");
            return;
        }
        this.ide().setProperty("/revise/feedback", feedback);
        void this.dialog("revise").then((dialog) => dialog.open());
    }

    public onReviseCancel(): void {
        void this.dialog("revise").then((dialog) => dialog.close());
    }

    public onReviseAfterClose(): void {
        this.ide().setProperty("/revise/state", ValueState.None);
    }

    /**
     * Stops the run: stops reading the stream, then asks the server to
     * cancel it (aborting alone leaves the run going). The server saves the
     * partial answer with a "(cancelled)" tail and ends the stream with
     * done.status "idle"; a run on another instance answers 409
     * run_on_other_instance.
     */
    public onStop(): void {
        const sid = this.ide().getProperty("/selectedId") as string;
        if (!sid || this.stopping) {
            return;
        }
        const seq = this.loadSeq;
        const hadStream = !!this.runAbort;
        this.stopping = (async () => {
            try {
                await this.service.cancel(sid);
                if (seq === this.loadSeq) {
                    MessageToast.show(this.text("runStopped"));
                }
            } catch (e) {
                if (seq === this.loadSeq) {
                    MessageBox.warning(gateErrorText(e, (key, args) => this.text(key, args)));
                }
            }
        })();
        this.runAbort?.abort();
        if (!hadStream) {
            // A run started elsewhere (another tab, before a reload): no
            // stream of ours ends, so reload once the cancel answered.
            void this.stopping.then(() => this.afterRun(sid, seq));
        }
    }

    // --- Assistant: chat -----------------------------------------------------

    public formatCanSubmit(canSendNow: boolean, draft: string | null | undefined): boolean {
        return !!canSendNow && !!String(draft ?? "").trim();
    }

    public onSend(): void {
        const text = String(this.ide().getProperty("/draft") ?? "").trim();
        if (!text || !this.ide().getProperty("/assistant/canSend")) {
            return;
        }
        this.ide().setProperty("/draft", "");
        void this.startRun("message", text);
    }

    /** Remembers the selected session's draft before the selection changes. */
    private keepDraft(): void {
        const sid = this.ide().getProperty("/selectedId") as string;
        const draft = String(this.ide().getProperty("/draft") ?? "");
        if (sid && draft) {
            this.drafts.set(sid, draft);
        } else if (sid) {
            this.drafts.delete(sid);
        }
    }

    public onRunErrorClose(): void {
        this.ide().setProperty("/run/error", "");
    }

    private resetChat(): void {
        // Stop reading the old session's stream; its run goes on server-side.
        this.runAbort?.abort();
        this.runAbort = undefined;
        this.localRunning = false;
        this.runWatch.stop();
        this.run = newRun();
        this.activity = new ActivityState();
        this.expandedOutputs.clear();
        this.renderActivity();
        this.pendingFiles.clear();
        // The old session's reload loop checks loadSeq and ends by itself;
        // the new session's first file event starts its own.
        this.filesRefresh = undefined;
        this.cancelRender();
        this.ide().setProperty("/messages", []);
        this.ide().setProperty("/run", { html: "", status: "", error: "", usageText: "" });
    }

    private async loadMessages(sid: string, seq: number): Promise<void> {
        try {
            const [messages] = await Promise.all([this.service.listMessages(sid), ensureMarkdown()]);
            if (seq === this.loadSeq) {
                this.ide().setProperty("/messages", messages.map((m) => toChatItem(m, renderMarkdown)));
                const stored = lastActivity(messages);
                if (stored) {
                    this.activity.load(stored);
                    this.renderActivity();
                }
                this.scrollChat();
            }
        } catch (e) {
            if (seq === this.loadSeq) {
                this.showError(e);
            }
        }
    }

    /** Reloads the open session (stage, status, artifacts, files) without touching the tabs. */
    private async refreshSession(sid: string, seq: number): Promise<void> {
        const detail = await this.service.getSession(sid);
        if (seq === this.loadSeq) {
            this.applyDetail(detail);
        }
    }

    /**
     * Sends a message or a revise and follows its stream (plan §1.3). A
     * refusal before the stream gives the text back: a message to the
     * input, a revise to its dialog.
     */
    private async startRun(kind: "message" | "revise", text: string): Promise<void> {
        const sid = this.ide().getProperty("/selectedId") as string;
        if (!sid || this.running()) {
            return;
        }
        this.runWatch.stop();
        this.afterRunFailedSid = "";
        const seq = this.loadSeq;
        const local: ChatItem = {
            id: `local-${Date.now()}`, role: "user", isUser: true, content: text, html: "", cancelled: false
        };
        this.ide().setProperty("/messages", [...this.ide().getProperty("/messages") as ChatItem[], local]);
        this.run = newRun();
        this.activity = new ActivityState();
        this.expandedOutputs.clear();
        this.renderActivity();
        this.localRunning = true;
        this.ide().setProperty("/run", {
            html: "", status: this.text("assistantThinking"), error: "",
            usageText: this.ide().getProperty("/run/usageText")
        });
        this.updateAssistant();
        this.scrollChat();
        this.announce(this.text("announceAnswering"));
        const abort = new AbortController();
        this.runAbort = abort;
        let refused = false;
        try {
            await ensureMarkdown();
            const onEvent = (event: SseEvent): void => this.onRunEvent(sid, seq, event);
            if (kind === "message") {
                await this.service.streamMessage(sid, text, onEvent, abort.signal);
            } else {
                await this.service.streamRevise(sid, text, onEvent, abort.signal);
            }
        } catch (e) {
            if (this.run.runId) {
                // The run had started; the stream broke. Say so in the chat.
                if (seq === this.loadSeq) {
                    this.showRunError(gateErrorText(e, (key, args) => this.text(key, args)));
                }
                return;
            }
            refused = true;
            if (seq === this.loadSeq) {
                // Refused before the stream started (409/429/404/auth): take
                // the message back so it can be sent again.
                this.ide().setProperty("/messages",
                    (this.ide().getProperty("/messages") as ChatItem[]).filter((m) => m !== local && m.id !== local.id));
                if (kind === "message" && !this.ide().getProperty("/draft")) {
                    this.ide().setProperty("/draft", text);
                }
                this.announce(gateErrorText(e, (key, args) => this.text(key, args)));
                this.showGateError(e, sid, seq, kind === "revise" ? () => this.reopenRevise(sid, seq, text) : () => this.focusInput());
            }
        } finally {
            if (this.runAbort === abort) {
                this.runAbort = undefined;
            }
            if (seq === this.loadSeq) {
                this.localRunning = false;
                if (!refused || this.stopping) {
                    await this.afterRun(sid, seq);
                    if (seq === this.loadSeq) {
                        this.focusInput();
                    }
                } else {
                    this.updateAssistant();
                }
            }
        }
    }

    /** Puts the focus back into the chat input (it was on Send, or in the dialog). */
    private focusInput(): void {
        (this.byId("chatInput") as TextArea | undefined)?.focus();
    }

    /** Tells a screen reader user what the assistant does (one polite live region, sap/ui/core/InvisibleMessage). */
    private announce(text: string): void {
        if (text) {
            InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
        }
    }

    private showRunError(text: string): void {
        this.ide().setProperty("/run/error", text);
        this.announce(text);
        this.scrollChat();
    }

    /**
     * After a run (done, stopped or broken): the stored answer replaces the
     * streamed one. True when the reload worked. When it failed, the session
     * is watched (model/runWatch) so the next poll reloads it again, instead
     * of the view staying on a run the model may still call running; only the
     * first failure of a session shows a message.
     */
    private async afterRun(sid: string, seq: number): Promise<boolean> {
        if (this.stopping) {
            await this.stopping;
            this.stopping = undefined;
        }
        this.cancelRender();
        if (seq !== this.loadSeq) {
            return false;
        }
        try {
            // Settle all three before deciding: a late session refresh must not
            // stop the watch that a failure below re-arms.
            const results = await Promise.allSettled(
                [this.refreshSession(sid, seq), this.loadMessages(sid, seq), this.reloadSessions()]);
            const failed = results.find((r): r is PromiseRejectedResult => r.status === "rejected");
            if (failed) {
                throw failed.reason;
            }
            if (this.afterRunFailedSid === sid) {
                this.afterRunFailedSid = "";
            }
            await this.filesRefresh;
            if (seq === this.loadSeq && !this.ide().getProperty("/run/error")) {
                const messages = this.ide().getProperty("/messages") as ChatItem[];
                const last = messages[messages.length - 1];
                this.announce(this.text(last?.cancelled ? "announceAnswerStopped" : "announceAnswerComplete"));
            }
            return true;
        } catch (e) {
            if (seq === this.loadSeq) {
                if (this.afterRunFailedSid !== sid) {
                    this.afterRunFailedSid = sid;
                    this.showError(e);
                }
                this.runWatch.watch(sid);
            }
            return false;
        } finally {
            if (seq === this.loadSeq) {
                this.ide().setProperty("/run/html", "");
                this.ide().setProperty("/run/status", "");
                this.updateAssistant();
            }
        }
    }

    private onRunEvent(sid: string, seq: number, event: SseEvent): void {
        if (seq !== this.loadSeq) {
            return;
        }
        this.run = reduceRun(this.run, event);
        switch (event.type) {
            case "tool":
            case "plan":
                this.activity.apply(event);
                this.renderActivity();
                this.scheduleRender();
                break;
            case "text":
                this.scheduleRender();
                break;
            case "usage":
                this.ide().setProperty("/run/usageText",
                    this.text("usageText", [event.data.requests_used, event.data.request_cap]));
                break;
            case "error":
                this.showRunError(runErrorText(event.data, (key, args) => this.text(key, args)));
                break;
            case "artifact":
                void this.showArtifact(sid, seq, event.data);
                break;
            case "file":
                this.pendingFiles.add(event.data.path);
                this.filesRefresh ??= this.drainChangedFiles(sid, seq);
                break;
            case "done":
                this.renderStream();
                break;
            default:
                break;
        }
    }

    /**
     * Re-renders the streamed answer at most once per animation frame and
     * per RENDER_INTERVAL_MS: each render re-parses the whole answer.
     */
    private scheduleRender(): void {
        if (this.frame !== undefined || this.renderTimer !== undefined) {
            return;
        }
        const wait = this.lastRender + RENDER_INTERVAL_MS - performance.now();
        const frame = (): void => {
            this.frame = requestAnimationFrame(() => {
                this.frame = undefined;
                this.renderStream();
            });
        };
        if (wait > 0) {
            this.renderTimer = setTimeout(() => {
                this.renderTimer = undefined;
                frame();
            }, wait);
        } else {
            frame();
        }
    }

    private cancelRender(): void {
        if (this.frame !== undefined) {
            cancelAnimationFrame(this.frame);
            this.frame = undefined;
        }
        if (this.renderTimer !== undefined) {
            clearTimeout(this.renderTimer);
            this.renderTimer = undefined;
        }
    }

    private renderStream(): void {
        this.lastRender = performance.now();
        const tool = activeTool(this.run);
        this.ide().setProperty("/run/html", this.run.text ? renderMarkdown(this.run.text) : "");
        this.ide().setProperty("/run/status", tool
            ? this.text("assistantUsingTool", [tool.tool])
            : this.run.text ? "" : this.text("assistantThinking"));
        this.scrollChat();
    }

    /** Pushes the activity (plan and tool timeline) into the model the panel binds to. */
    private renderActivity(): void {
        const todos = todoView(this.activity.todos);
        const events = eventRows(this.activity.events).map((row) => ({
            ...row, expanded: this.expandedOutputs.has(row.id)
        }));
        const done = this.activity.todos.filter((t) => t.status === "completed").length;
        this.ide().setProperty("/activity", {
            title: this.text("activityTitle", [events.length, done, todos.length]),
            todos, events
        });
    }

    /** Shows one tool call's output in full, or folds it back to a few lines. */
    public onActivityToggle(event: UI5Event): void {
        const id = (event.getSource() as Control).getBindingContext("ide")?.getProperty("id") as string | undefined;
        if (id) {
            if (!this.expandedOutputs.delete(id)) {
                this.expandedOutputs.add(id);
            }
            this.renderActivity();
        }
    }

    private scrollChat(): void {
        requestAnimationFrame(() => {
            (this.byId("chatScroll") as ScrollContainer | undefined)?.scrollTo(0, 1e7, 0);
        });
    }

    /**
     * A new design / plan / note / review version. The current stage's own
     * deliverable (design, plan, review: what Approve needs) comes to the
     * front; anything else (a propose run's note) opens or refreshes behind
     * the tab the user looks at, with a toast. An open tab moves to the new
     * version only when it showed the latest one: a version the user chose
     * stays.
     */
    private async showArtifact(sid: string, seq: number, artifact: ArtifactEventData): Promise<void> {
        try {
            await this.refreshSession(sid, seq);
        } catch (e) {
            if (seq === this.loadSeq) {
                this.showError(e);
            }
            return;
        }
        if (seq !== this.loadSeq) {
            return;
        }
        const session = this.ide().getProperty("/session") as SessionDetail | null;
        const front = !!session && STAGE_DELIVERABLE[session.stage] === artifact.kind;
        const label = this.text(DOC_KIND_KEYS[artifact.kind]);
        const key = docKey(artifact.kind);
        const tab = this.tab(key);
        if (!tab) {
            if (!front) {
                MessageToast.show(this.text("docReadyBehind", [label, artifact.version]));
            }
            await this.openDocument(artifact.kind, front);
            return;
        }
        const onLatest = !tab.artifactId || tab.artifactId === tab.versions[0]?.id;
        this.patchTab(key, { versions: this.docVersions(artifact.kind) });
        if (!onLatest) {
            MessageToast.show(this.text("docNewerVersion", [label, artifact.version]));
            return;
        }
        if (front) {
            this.selectTab(key);
        } else {
            MessageToast.show(this.text("docReadyBehind", [label, artifact.version]));
        }
        await this.loadDocument(key, artifact.id);
    }

    /**
     * The files the run changed: reload the tree, and re-read every open tab
     * of them, keeping each tab's mode (a diff stays a diff).
     */
    private async refreshChangedFiles(sid: string, seq: number): Promise<void> {
        const paths = [...this.pendingFiles];
        this.pendingFiles.clear();
        if (!await this.reloadFiles(sid, seq)) {
            return;
        }
        await Promise.all(paths.filter((path) => this.tab(fileKey(path))).map(async (path) => {
            const key = fileKey(path);
            const request = this.tabRequest(key, "refresh");
            try {
                const file = await this.service.getFile(sid, path);
                const tab = this.tab(key);
                if (request.current() && tab) {
                    this.patchTab(key, withFile(tab, file, true));
                    await this.showTab(key);
                }
            } finally {
                request.finish();
            }
        }));
    }

    /**
     * Reloads the paths of `file` events until none is left: a path that
     * arrives while a reload is in flight is picked up by the next round
     * instead of being lost. A failed round shows its error and the loop goes
     * on with the paths that arrived meanwhile (the failed round's own paths
     * are not retried, so a lasting failure cannot spin). Clears
     * `filesRefresh` in the same turn as the last empty check, so the next
     * event always starts a new loop.
     */
    private drainChangedFiles(sid: string, seq: number): Promise<void> {
        let loop: Promise<void> | undefined;
        loop = (async () => {
            // Events of one chunk arrive together: let them all land first.
            await Promise.resolve();
            try {
                while (this.pendingFiles.size && seq === this.loadSeq) {
                    try {
                        await this.refreshChangedFiles(sid, seq);
                    } catch (e) {
                        if (seq === this.loadSeq) {
                            this.showError(e);
                        }
                    }
                }
            } finally {
                if (this.filesRefresh === loop) {
                    this.filesRefresh = undefined;
                }
            }
        })();
        return loop;
    }

    // --- Assistant: a run without a stream of ours --------------------------

    /**
     * Watches the open session while the server says it is running and no
     * stream of this page follows it (left by a session switch, started
     * before a reload or in another tab); stops otherwise.
     */
    private watchRun(detail: SessionDetail | null): void {
        if (detail?.status === "running" && !this.localRunning && !this.runAbort) {
            this.runWatch.watch(detail.id);
        } else {
            this.runWatch.stop();
        }
    }

    /** One poll of {@link watchRun}: true while the run goes on; its end reloads like a run of ours. */
    private async pollRun(sid: string): Promise<boolean> {
        const seq = this.loadSeq;
        const detail = await this.service.getSession(sid);
        if (seq !== this.loadSeq || this.localRunning || this.runAbort) {
            return false;
        }
        if (detail.status === "running") {
            return true;
        }
        // A failed reload keeps watching (afterRun re-arms it): the next poll retries.
        return !await this.afterRun(sid, seq) && seq === this.loadSeq;
    }

    // --- Helpers -------------------------------------------------------------

    /** Loads a dialog fragment once; loadFragment prefixes ids and adds it as a dependent. */
    private dialog(which: "newSession" | "openObject" | "revise"): Promise<Dialog> {
        if (which === "revise") {
            this.reviseDialog ??= this.loadFragment({
                name: "com.agent.ide.fragment.ReviseDialog"
            }) as Promise<Dialog>;
            return this.reviseDialog;
        }
        if (which === "newSession") {
            this.newSessionDialog ??= this.loadFragment({
                name: "com.agent.ide.fragment.NewSessionDialog"
            }) as Promise<Dialog>;
            return this.newSessionDialog;
        }
        this.openObjectDialog ??= this.loadFragment({
            name: "com.agent.ide.fragment.OpenObjectDialog"
        }) as Promise<Dialog>;
        return this.openObjectDialog;
    }

}
