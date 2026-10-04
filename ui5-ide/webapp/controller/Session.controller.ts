import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import { InvisibleMessageMode } from "sap/ui/core/library";
import DateFormat from "sap/ui/core/format/DateFormat";
import type UI5Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type Route from "sap/ui/core/routing/Route";
import type ScrollContainer from "sap/m/ScrollContainer";
import type HBox from "sap/m/HBox";
import type TextArea from "sap/m/TextArea";
import type Button from "sap/m/Button";
import type Popover from "sap/m/Popover";
import UI5Element from "sap/ui/core/Element";
import type Dialog from "sap/m/Dialog";
import type List from "sap/m/List";
import BaseController from "./BaseController";
import RunController, { type RunKind } from "../model/RunController";
import StickyScroll from "../model/stickyScroll";
import { isProposal, primaryAction, stageTokens } from "../model/stageGate";
import { activeTool, isRunNote, toChatItem, type RunState } from "../model/chatRun";
import { eventRows, isLongOutput, todoView, visibleRows } from "../model/activity";
import { HtmlCache, activityCounts, interleave } from "../model/conversation";
import { approvalErrorKey, approvalErrorText, approvalRow, paramsText, reduceApprovals, type ApprovalRow } from "../model/approvals";
import { errorText, gateErrorText, runErrorText, runNoteText } from "../model/errorText";
import { ensureMarkdown } from "../model/vendor";
import { applyThemeVars } from "../model/themeVars";
import { renderMarkdown } from "../model/markdown";
import { renderDocument } from "../model/docView";
import { anchorText, quoteOf, stateText } from "../model/comments";
import { MODEL_SIZE_LIMIT } from "../model/formatter";
import {
    basedOnLink, blockHint, blockLabel, blockText, byParagraph, commentRow, commentsLeftText, documentComments, markerLabel,
    otherVersionComments, splitBlocks, versionItems, type CommentRow
} from "../model/docReview";
import {
    CHANGES_LIMITS, baseInfo, baseLineOfFirstChange, changeCounts, commentsInRange, fileComments, lineMarkers, lintInfo,
    otherRevisionComments, proposedObjects, revisionItems, selectionRange, stepStop, syntaxInfo,
    approveRevisions, compareObject, diffSyntax, notCompared, rangeShown, settleLimited, type CompareMode
} from "../model/changesView";
import { decorateDiff, navigable, newLineOf, revealRow, rowFor, rowOfLine, setStop, step } from "../model/diffDecor";
import { DIFF_CONTEXT, changeStops, renderUnifiedHtml, type Hunk, type UnifiedRow } from "../model/diffModel";
import { renderSource, scrollTargetLine } from "../model/sourceView";
import { adtUri, classIncludeOf } from "../model/adtLink";
import { findingRow, pendingApprovalCount, upsertFinding } from "../model/findings";
import { ensureDiff } from "../model/vendor";
import { IdeError } from "../service/IdeService";
import type IdeService from "../service/IdeService";
import type {
    Activity, Approval, ApprovalDecision, ArtifactKind, Comment, DiagnoseFinding, FileDetail, FileRevision, FileSummary, FindingOpen,
    LintFinding, Message,
    SessionDetail, SseEvent, Stage, SyntaxItem, SyntaxStatus, ToolEventData
} from "../service/types";

/** The artifact column views a deep link can name (`?view=`). */
type ArtifactView = "document" | "changes" | "source" | "findings";
const VIEWS: ArtifactView[] = ["document", "changes", "source", "findings"];

/** What a deep link's query may carry (manifest route `session`, `:?query:`). */
interface SessionQuery {
    view?: string;
    path?: string;
    line?: string;
    kind?: string;
    version?: string;
    /** `sap`: the source view shows the SAP source (`origin_source`) even when a proposal exists (a finding's line). */
    base?: string;
}

/** The details dialog of a finding before anything is read. */
const NO_FINDING_DETAIL = { id: "", title: "", text: "", busy: false, canRefresh: true };

/** The finding whose source the source view shows: its line, or the server's hint when the line does not map. */
interface SourceFinding {
    path: string;
    line: number | null;
    hint: string | null;
    title: string;
}

/** The document a stage writes, for the timeline's links and pins. */
const STAGE_KIND: Partial<Record<Stage, ArtifactKind>> = { design: "design", plan: "plan", propose: "note", review: "review" };
/** The stages whose approve pins a document version. */
const PINNED: Partial<Record<Stage, "design" | "plan" | "review">> = { design: "design", plan: "plan", review: "review" };
const REVISABLE: Stage[] = ["design", "plan", "propose", "review"];
const KINDS: ArtifactKind[] = ["design", "plan", "note", "review", "report"];

/** The kinds a change session's developer may comment on (no comments on a diagnose report). */
const COMMENTABLE: ArtifactKind[] = ["design", "plan", "note", "review"];
/** The longest comment body the server stores (contract §1.1). */
const COMMENT_MAX = 4000;
/** The longest a comment popover may take to open before keys typed on its block or line are their own again. */
const BUFFER_MAX_MS = 1500;
/**
 * After the cards of the changes view appear, Approve changes stays off this
 * long: the second click of a double click on "Review changes" (or a
 * repeating Enter) lands on the same button, now an approve of cards nobody
 * has seen yet.
 */
const APPROVE_SETTLE_MS = 800;
/** The events that tell where a press of the primary action starts and ends. */
const PRESS_EVENTS = ["pointerdown", "mousedown", "keydown", "keyup", "pointerup", "mouseup", "pointercancel"];

/** The document the artifact column shows: its kind, version and paragraph blocks (sanitised HTML). */
interface ShownDocument {
    kind: ArtifactKind;
    version: number;
    blocks: string[];
    /** Its text is on screen (the read answered and the blocks are rendered). */
    rendered?: boolean;
}

/** Lines of one revision of a workspace file that a comment popover is about (Task U10). */
interface FileAnchor {
    /** The object card in the changes view. */
    index: number;
    path: string;
    revision: number;
    start: number;
    end: number;
}

/**
 * The open comment popover: which paragraph (or, with `file`, which lines),
 * who opened it (focus goes back there), the comment being edited.
 */
interface PopoverTarget {
    paragraph: number;
    invoker: Control | "paragraph" | "row";
    editingId?: string;
    file?: FileAnchor;
}

/** One proposed object of the changes view: the revision shown and what it was rendered from. */
interface ChangeObject {
    path: string;
    summary: FileSummary;
    detail: FileDetail;
    /** The revision shown; `latest` is the file's newest one. */
    revision: number;
    latest: number;
    revisions: FileRevision[];
    /** `null` when not compared (beyond CHANGES_LIMITS). */
    rows: UnifiedRow[] | null;
    /** The whole diff, its hunks only (a large object), or not compared. */
    mode: CompareMode;
    syntaxStatus: SyntaxStatus | null;
    syntaxItems: SyntaxItem[];
    lint: LintFinding[];
    lintedHere: boolean;
    /** The proposed source of the revision shown. */
    source: string;
    /** The object could not be read: the card shows an error and a Retry. */
    failed?: boolean;
}

/** How many objects the changes view reads at once. */
const CHANGES_CONCURRENCY = 4;

/** The selected lines of one diff: `anchor` is where Shift extends from. */
interface LineSelection {
    index: number;
    anchor: number;
    start: number;
    end: number;
}

/** The longest message the composer sends (the server's limit). */
const DRAFT_MAX = 20000;

/** The id of the row of the answer being streamed. */
const STREAMING = "streaming";

/** One row of the conversation list: a message, or a trace approval of a diagnose session. */
interface MessageRow {
    id: string;
    kind: "message" | "approval";
    isUser: boolean;
    author: string;
    /** Plain text: the user's message, or a status line in place of an answer not written yet. */
    content: string;
    /** Assistant markdown rendered by model/markdown (sanitised); "" for the user. */
    html: string;
    streaming: boolean;
    statusText: string;
    hasActivity: boolean;
    activityOpen: boolean;
    activityBusy: boolean;
    activityTitle: string;
    activityError: string;
    todos: unknown[];
    events: unknown[];
    /** Tool rows beyond the first 50, behind "Show all (n)". */
    hiddenEvents: number;
    showAllText: string;
    approval: ApprovalRow | null;
}

/**
 * The session page (`sessions/{id}:?query:`): header with the stage
 * timeline, the primary action named by its effect and its visible reason
 * when disabled, Request changes, Stop and the requests used; a
 * FlexibleColumnLayout with the conversation (begin column) and the artifact
 * column (mid column) that a deep link's `view` query opens.
 *
 * The run lifecycle is model/RunController's; this controller only maps its
 * callbacks onto the `s` model. The conversation column (U8,
 * fragment/Conversation) interleaves messages and trace approvals by time,
 * reads a message's activity only when it is opened, and follows a streamed
 * answer only for a reader at the end ("Jump to latest" otherwise). Later
 * tasks fill the artifact views (U9-U11).
 *
 * @namespace com.agent.ide.controller
 */
export default class Session extends BaseController {

    private runs!: RunController;
    private sticky!: StickyScroll;
    private sid = "";
    private detail: SessionDetail | null = null;
    /** Bumped per loaded session: an answer for an earlier one is dropped. */
    private loadSeq = 0;
    /** Bumped per artifact the route asks for: a late answer for an earlier one is dropped. */
    private artifactSeq = 0;
    /** The control that opened the artifact column; Close gives the focus back to it. */
    private opener?: Control;
    /** Close was pressed: the next route match without a view returns the focus. */
    private closing = false;
    /** A run this page started is in flight: its end is announced once. */
    private ownRun = false;
    /** An approve is on its way, from the request until the session and the cards are read again. */
    private approving = false;
    /** A hand over is on its way (its own flag: refreshHeader rewrites `/primary`). */
    private handingOver = false;
    /** Bumped when the page leaves a session: an approve or hand over that answers later acts only for its own session. */
    private actionSeq = 0;
    /** While a session's first load runs: the query of the latest route match, applied once it is loaded. */
    private routeQuery?: SessionQuery;
    /**
     * When the cards of the changes view last appeared (performance.now());
     * +Infinity while they were rendered in a hidden tab and nobody has seen them yet.
     */
    private cardsShownAt = Number.NEGATIVE_INFINITY;
    /** Turns Approve changes back on once APPROVE_SETTLE_MS have passed after the cards appeared. */
    private settleTimer?: ReturnType<typeof setTimeout>;
    /** The changes view was navigated to: its first render of the cards explains the settle window. */
    private explainSettle = false;
    /** The current settle window says why Approve waits (only the first render after navigation). */
    private settleExplains = false;
    /** When the pointer went down on the primary action, until that click is over. */
    private primaryPointerAt?: number;
    /** When Enter or Space went down on the primary action (a held key keeps its first down), until it is up. */
    private primaryKeyAt?: number;
    /** The primary action had the focus when it turned off or busy (see keepPrimaryFocus). */
    private primaryFocusParked = false;
    /** Where a press of the primary action starts (capture phase, before any UI5 handler fires press). */
    private readonly onPrimaryInput = (e: Event): void => this.primaryInput(e);
    /** The window lost the focus: a key or pointer that was down will send no up to this page. */
    private readonly onWindowBlur = (): void => this.forgetPressStart();
    /** Cards rendered in a hidden tab start their settle window once the tab is visible. */
    private readonly onVisibility = (): void => this.visibilityChanged();
    /** `GET /me` `diagnose_retention_days` (0 = kept until deleted); undefined until read or when it failed. */
    private retentionDays?: number;
    /** The tab title before a session named it; restored when the page is left. */
    private originalTitle?: string;
    // --- Conversation state (rendered into `s>/messages` by renderConversation) ---
    private messages: Message[] = [];
    private approvals: Approval[] = [];
    /** Approvals a run of this page proposed: shown after its question until the run ends. */
    private liveApprovals = new Set<string>();
    /** The question of the run this page streams, until the stored conversation replaces it. */
    private pendingQuestion?: string;
    private streaming = false;
    private streamHtml = "";
    private streamStatus = "";
    /** Activities read on request, by message id (never re-read). */
    private activities = new Map<string, Activity>();
    private openActivities = new Set<string>();
    private activityErrors = new Map<string, string>();
    private loadingActivities = new Set<string>();
    /** Messages whose activity shows every row ("Show all"). */
    private allActivities = new Set<string>();
    /** Rendered answers by message id and content (no re-sanitising on every re-render). */
    private readonly htmlCache = new HtmlCache();
    /** Approvals whose decision is on its way. */
    private deciding = new Set<string>();
    private readonly dateTime = DateFormat.getDateTimeInstance({ style: "medium" });
    // --- Document view and comments (Task U9) ---
    /** The session's comments, oldest first (`GET comments`). */
    private comments: Comment[] = [];
    /** The query the page shows (the artifact column follows it). */
    private query: SessionQuery = {};
    private doc: ShownDocument | null = null;
    /**
     * The last document version whose text was on screen. While the version
     * asked for is read (or after its read failed) this one is still what the
     * developer saw, so its hold on the approve stays.
     */
    private docOnScreen: { kind: ArtifactKind; version: number } | null = null;
    /** The next applyQuery leaves the focus where it is (a version switch, a new version shown by a run). */
    private keepFocus = false;
    /** The target whose popover is open (set once it opened; never while a load is pending or failed). */
    private pop?: PopoverTarget;
    /** The target whose popover is on its way (loading or opening): keys typed meanwhile go into its draft. */
    private pendingPop?: PopoverTarget;
    /** What `pendingPop` opens by: an open requested while the previous popover was closing is done with it. */
    private pendingAnchor?: Control | HTMLElement;
    /** Between the popover's beforeClose and afterClose: an open now would be ignored (sap.ui.core.Popup). */
    private popoverClosing = false;
    /**
     * `pendingPop` was requested while the popover was closing: its afterClose opens it. Set only
     * there; any other pending open (sap.m.Popover reopening for another opener) opens by itself.
     */
    private queuedOpen = false;
    /** The target of the popover that is closing (beforeClose to afterClose): the focus goes back to its invoker. */
    private closingPop?: PopoverTarget;
    /** The targets whose comment save is on its way: the popover is busy while it shows one of them. */
    private readonly savingPops = new Set<PopoverTarget>();
    /** While a popover is on its way: takes the keys typed on the block or line that opens it (capture phase). */
    private readonly onPendingKey = (e: KeyboardEvent): void => this.pendingKey(e);
    /** The element whose keys are buffered while the popover is on its way (the block, the row, the button). */
    private bufferFrom?: HTMLElement;
    /** The hard stop of the key buffer: it never outlives a popover that does not open. */
    private bufferTimer?: ReturnType<typeof setTimeout>;
    /** The comment popover once loaded (its own root may have the focus while it animates in). */
    private popoverControl?: Popover;
    /** This page put the focus and caret into the text area of the popover on its way (typing went on there). */
    private draftFocused = false;
    /** Bumped per comments read: an older answer never overwrites a newer one. */
    private commentsSeq = 0;
    /** The note of a refused request-changes run: back in the dialog when it opens again. */
    private refusedNote?: string;
    /** Opened tool outputs ("Show full output"), by message id and row index. */
    private openOutputs = new Set<string>();
    /** Doc markers already kept out of the tab order. */
    private readonly tamedMarkers = new WeakSet<object>();
    // --- Changes view (Task U10) ---
    private changes: ChangeObject[] = [];
    /** The global change stop last moved to by Next/Previous (-1 = none yet). */
    private stopCursor = -1;
    private selection?: LineSelection;
    /** Revisions the user picked per path, with the latest revision at the time (a newer one wins). */
    private picked = new Map<string, { revision: number; latest: number }>();
    /** The last revision switch asked for per path (`switchSeq`): only its answer is shown. */
    private switching = new Map<string, number>();
    private switchSeq = 0;
    /** Serialises the reloads of the changes view after `file` events. */
    private changesSync: Promise<void> = Promise.resolve();
    /** The changes view's list read in flight: a `file` event during it waits for it instead of being dropped. */
    private changesLoading: Promise<void> | null = null;
    /** A sync is replacing cards: replaceChange leaves catching up to it. */
    private changesSyncing = false;
    /** The line the source view scrolls to once rendered. */
    private sourceLine = 0;
    // --- Diagnose (Task U11) ---
    /** The session's findings, newest first (`GET findings`, then `finding` frames). */
    private findings: DiagnoseFinding[] = [];
    /** Bumped per findings read: an older answer never overwrites a newer one. */
    private findingsSeq = 0;
    /** The finding the source view was opened for (its hint and title). */
    private sourceFinding?: SourceFinding;
    /** Findings whose source is being opened: a second press waits for the first. */
    private openingFindings = new Set<string>();
    /** The details dialog's own model (the shared fragment binds `ide>/findingDetail`). */
    private findingDetailDialog?: Promise<Dialog>;
    /** Bumped per detail read and on close: a late answer is dropped. */
    private detailSeq = 0;
    private commentPopover?: Promise<Popover>;
    private requestChangesDialog?: Promise<Dialog>;
    private readonly onSessionRoute = (e: UI5Event): void => { void this.onRouteMatched(e); };
    private readonly onAnyRoute = (e: UI5Event): void => {
        if ((e.getParameters() as { name?: string }).name !== "session") {
            this.leave();
        }
    };

    public onInit(): void {
        const model = new JSONModel(Session.emptyState());
        // Long documents, conversations and diffs: the default of 100 entries would cut them silently.
        model.setSizeLimit(MODEL_SIZE_LIMIT);
        this.setModel(model, "s");
        // The page's CSS reads theme parameters as var(--sap...): the page may be the first one opened.
        applyThemeVars();
        // Built here, not as a field: the owner component (and its service) exists only from onInit on.
        this.runs = new RunController(this.service(), {
            onEvent: (e) => this.onRunEvent(e),
            onRender: (state) => this.renderStream(state),
            onFilesChanged: () => this.reloadDetail().then(() => this.syncChanges()),
            onRefused: (e, kind, text) => this.onRunRefused(e, kind, text),
            onStreamBroken: (e) => this.showError(e),
            onFinished: (sid) => this.afterRun(sid),
            onBeforeStream: () => ensureMarkdown()
        });
        // When a press of the primary action started: one that began before the cards appeared approves nothing.
        PRESS_EVENTS.forEach((type) => document.addEventListener(type, this.onPrimaryInput, true));
        window.addEventListener("blur", this.onWindowBlur);
        document.addEventListener("visibilitychange", this.onVisibility);
        // "Jump to latest" whenever the reader is not at the end.
        this.sticky = new StickyScroll((atBottom) => this.s().setProperty("/showJump", !atBottom));
        (this.byId("conversationScroll") as ScrollContainer).addEventDelegate({
            onAfterRendering: () => this.attachSticky()
        });
        // The column is a region named by its title; the title takes the focus when the column opens.
        this.byId("artifactTitle")?.addEventDelegate({
            onAfterRendering: () => this.byId("artifactTitle")?.getDomRef()?.setAttribute("tabindex", "-1")
        });
        this.byId("artifactHost")?.addEventDelegate({
            onAfterRendering: () => {
                const dom = this.byId("artifactHost")?.getDomRef();
                dom?.setAttribute("role", "region");
                dom?.setAttribute("aria-labelledby", this.createId("artifactTitle") ?? "artifactTitle");
            }
        });
        // Enter or C on a focused block opens its comments; the arrow keys, Home and End move between blocks.
        this.byId("artifactContent")?.addEventDelegate({
            onkeydown: (e: KeyboardEvent) => this.onDocumentKeydown(e),
            onfocusin: (e: FocusEvent) => this.onDocumentFocus(e),
            // The arrow-key help once, on the container; each block has only the short hint.
            onAfterRendering: () => this.describeDocument()
        });
        // Changes view: N/P, the keys of a diff, clicks on lines and markers, and the roving stop.
        this.byId("changesView")?.addEventDelegate({
            onkeydown: (e: KeyboardEvent) => this.onChangesKeydown(e),
            onclick: (e: MouseEvent) => this.onChangesClick(e),
            onfocusin: (e: FocusEvent) => this.onChangesFocus(e)
        });
        // One tab stop for the document: the markers stay clickable but out of the tab order.
        this.byId("artifactContent")?.getBinding("items")?.attachChange(() => this.tameMarkers());
        (this.byId("stageTimeline") as HBox).addEventDelegate({
            onAfterRendering: () => this.decorateTimeline()
        });
        (this.byId("chatInput") as TextArea).addEventDelegate({
            onkeydown: (e: KeyboardEvent & { originalEvent?: KeyboardEvent }) => {
                // Not while an input method composes (Enter then confirms the composition).
                const composing = e.isComposing || e.originalEvent?.isComposing || e.keyCode === 229;
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !composing) {
                    e.preventDefault();
                    this.onSend();
                }
            }
        });
        this.getRouter().getRoute("session")?.attachPatternMatched(this.onSessionRoute);
        this.getRouter().attachRouteMatched(this.onAnyRoute, this);
    }

    public onExit(): void {
        (this.getRouter().getRoute("session") as Route | undefined)?.detachPatternMatched(this.onSessionRoute);
        this.getRouter().detachRouteMatched(this.onAnyRoute, this);
        PRESS_EVENTS.forEach((type) => document.removeEventListener(type, this.onPrimaryInput, true));
        window.removeEventListener("blur", this.onWindowBlur);
        document.removeEventListener("visibilitychange", this.onVisibility);
        clearTimeout(this.settleTimer);
        this.runs.dispose();
        this.sticky.detach();
        this.endPending();
        // The cached details dialog goes with the page (not only as a dependent of the view).
        void this.findingDetailDialog?.then((dialog) => dialog.destroy(), () => undefined);
        this.findingDetailDialog = undefined;
        this.restoreTitle();
    }

    /**
     * Records where a press of the primary action starts: the pointer going
     * down on it (cleared once that click is over) and the first down of
     * Enter or Space (a held key's repeats keep it; cleared once the key is
     * up and its press handled).
     */
    private primaryInput(e: Event): void {
        const dom = this.byId("primaryAction")?.getDomRef();
        const target = e.target as Node | null;
        const on = !!dom && !!target && dom.contains(target);
        const later = (fn: () => void): void => { setTimeout(fn, 0); };
        switch (e.type) {
            case "pointerdown":
            case "mousedown":
                if (on && (e.type === "pointerdown" || this.primaryPointerAt === undefined)) {
                    this.primaryPointerAt = performance.now();
                }
                break;
            case "pointerup":
            case "mouseup":
                // After the click this up belongs to (dispatched in the same task).
                later(() => { this.primaryPointerAt = undefined; });
                break;
            case "keydown": {
                const key = (e as KeyboardEvent).key;
                if (on && (key === "Enter" || key === " ") && !(e as KeyboardEvent).repeat) {
                    this.primaryKeyAt = performance.now();
                }
                break;
            }
            case "keyup":
                // After Space's press, which fires on key up.
                later(() => { this.primaryKeyAt = undefined; });
                break;
            case "pointercancel":
                // The browser took the pointer over (scroll, gesture): no up and no click follow.
                this.primaryPointerAt = undefined;
                break;
            default:
                break;
        }
    }

    /** No up event will come for a key or pointer that was down (the window lost the focus). */
    private forgetPressStart(): void {
        this.primaryPointerAt = undefined;
        this.primaryKeyAt = undefined;
    }

    /**
     * The settle window of new cards: from now, or, while the tab is hidden,
     * from the moment it becomes visible (the cards have not been seen yet).
     * Every render of the cards gets the window; only the first one after a
     * navigation to the changes view shows its reason, so a re-render does
     * not flash it. `resume` continues a window a hidden tab postponed.
     */
    private startSettle(resume = false): void {
        if (!resume) {
            this.settleExplains = this.explainSettle;
            this.explainSettle = false;
        }
        clearTimeout(this.settleTimer);
        if (document.visibilityState === "hidden") {
            this.cardsShownAt = Number.POSITIVE_INFINITY;
            return;
        }
        this.cardsShownAt = performance.now();
        this.settleTimer = setTimeout(() => this.refreshHeader(), APPROVE_SETTLE_MS + 20);
    }

    private visibilityChanged(): void {
        if (document.visibilityState !== "hidden" && this.cardsShownAt === Number.POSITIVE_INFINITY) {
            this.startSettle(true);
            this.refreshHeader();
        }
    }

    /**
     * Approve changes waits until the cards have been on screen for
     * APPROVE_SETTLE_MS, and a press that began (pointer or key down) before
     * they appeared is not an approve of them.
     */
    private approveTooEarly(): boolean {
        const shown = this.cardsShownAt;
        const now = performance.now();
        const began = [this.primaryPointerAt, this.primaryKeyAt].filter((t): t is number => t !== undefined);
        return now - shown < APPROVE_SETTLE_MS || began.some((t) => t < shown);
    }


    /**
     * Another route took over: the router keeps this view cached and runs no
     * onExit, so the run's stream, the watch and the scroll observers are let
     * go here. Nothing of the left session's run may show up elsewhere.
     */
    private leave(): void {
        if (!this.sid) {
            return;
        }
        this.runs.detach();
        this.sticky.detach();
        this.sid = "";
        this.detail = null;
        // An approve or hand over still in flight belongs to the session left: nothing of it shows on the next one.
        this.actionSeq++;
        this.approving = false;
        this.handingOver = false;
        this.routeQuery = undefined;
        this.ownRun = false;
        this.loadSeq++;
        this.artifactSeq++;
        this.opener = undefined;
        this.setComments([]);
        this.doc = null;
        this.docOnScreen = null;
        this.query = {};
        this.resetChanges();
        this.picked.clear();
        this.closeCommentUi();
        this.findings = [];
        this.findingsSeq++;
        this.sourceFinding = undefined;
        this.openingFindings.clear();
        void this.findingDetailDialog?.then((dialog) => dialog.close());
        this.restoreTitle();
        this.setShellTarget("");
    }

    /** The shell's conventions dialog opens on the open session's target. */
    private setShellTarget(target: string): void {
        (this.getView()?.getModel("appView") as JSONModel | undefined)?.setProperty("/target", target);
    }

    private restoreTitle(): void {
        if (this.originalTitle !== undefined) {
            document.title = this.originalTitle;
            this.originalTitle = undefined;
        }
    }

    /** Follows the conversation's scrolling element (again after the page was left and shown). */
    private attachSticky(): void {
        const el = this.byId("conversationScroll")?.getDomRef() as HTMLElement | null | undefined;
        const content = el?.firstElementChild as HTMLElement | null | undefined;
        if (el && content) {
            this.sticky.attach(el, content);
        }
    }

    private static emptyState(): Record<string, unknown> {
        return {
            busy: false, loadFailed: false, loadFailedText: "",
            session: null, isDiagnose: false, typeText: "", banner: "", lostFlag: false, tokens: [],
            diagnose: { findingsText: "", reportText: "" },
            primary: { text: "", enabled: false, reason: "", busy: false },
            requestChanges: { text: "", visible: false, enabled: false, reason: "" },
            reportAgain: { visible: false, enabled: false },
            usageText: "", running: false, canSend: false, canType: false, sendReason: "",
            draft: "", messages: [] as MessageRow[], runError: { text: "", type: "Error" }, showJump: false,
            layout: "OneColumn", artifact: Session.emptyArtifact(),
            pop: { title: "", comments: [], draft: "", draftLabel: "", counter: "", error: "", canSave: false, busy: false },
            rc: { intro: "", comments: [], note: "", busy: false, canSend: false },
            findingDetail: { ...NO_FINDING_DETAIL }
        };
    }

    private static emptyArtifact(title = "", view = ""): Record<string, unknown> {
        return {
            title, html: "", missing: "", busy: false, view,
            doc: { versions: [], selected: "", basedOn: null, blocks: [], others: [], commentable: false, newer: null },
            changes: { summary: "", objects: [], stops: 0, loaded: false },
            source: { html: "", meta: "", adtHref: "", findingNote: "", hint: "", fromFinding: false },
            findings: { items: [], any: false }
        };
    }

    private s(): JSONModel {
        return this.getModel("s") as JSONModel;
    }

    private service(): IdeService {
        return this.getOwnerComponentTyped().getIdeService();
    }

    // --- Route ---------------------------------------------------------------

    private async onRouteMatched(event: UI5Event): Promise<void> {
        const args = (event.getParameters() as { arguments: { id: string; "?query"?: SessionQuery } }).arguments;
        const id = args.id;
        let query = args["?query"] ?? {};
        if (id !== this.sid) {
            this.leave();
            this.sid = id;
            this.resetConversation();
            this.s().setData(Session.emptyState());
            this.attachSticky();
            this.routeQuery = query;
            await this.load(id);
            if (this.sid !== id) {
                // Another session took over while this one loaded.
                return;
            }
            // A route match during the load (another view of this session): the latest query wins.
            query = this.routeQuery ?? query;
            this.routeQuery = undefined;
        } else if (this.routeQuery !== undefined) {
            // The first load still runs: nothing to show the query on yet; it is applied when the load is done.
            this.routeQuery = query;
            return;
        }
        // The same session with another query only changes the artifact column: no reload.
        if (this.sid === id && this.detail && !this.s().getProperty("/loadFailed")) {
            await this.applyQuery(query);
        }
    }

    /**
     * The first load of a session id: the session and its conversation, under
     * the busy overlay. A session that is not there (404, or 403 for another
     * owner) or a failed read is a state of the page with a way back, not a
     * message box over an empty page.
     */
    private async load(id: string): Promise<void> {
        const seq = ++this.loadSeq;
        this.s().setProperty("/busy", true);
        try {
            const [detail, list] = await Promise.all([
                this.service().getSession(id),
                this.service().listMessages(id),
                ensureMarkdown()
            ]);
            const findingsSeq = ++this.findingsSeq;
            const [approvals, comments, findings, me] = await Promise.all([
                detail.type === "diagnose" ? this.service().listApprovals(id) : Promise.resolve([]),
                detail.type === "change" ? this.service().listComments(id) : Promise.resolve([]),
                detail.type === "diagnose" ? this.service().listFindings(id) : Promise.resolve([]),
                // The banner's retention comes from the server, never a default the UI assumes.
                detail.type === "diagnose" ? this.service().getMe().catch(() => null) : Promise.resolve(null)
            ]);
            if (seq !== this.loadSeq || id !== this.sid) {
                return;
            }
            if (findingsSeq === this.findingsSeq) {
                this.findings = findings;
            } else {
                // A `finding` frame came meanwhile (a run being watched): it is newer than the answer.
                this.findings = this.findings.slice().reverse().reduce((list, f) => upsertFinding(list, f), findings);
            }
            if (me) {
                this.retentionDays = Number.isInteger(me.diagnose_retention_days) && me.diagnose_retention_days >= 0
                    ? me.diagnose_retention_days : undefined;
            }
            this.setComments(comments);
            this.setDetail(detail);
            this.messages = list;
            this.approvals = approvals;
            this.renderConversation();
            this.announceNewApprovals([], approvals);
            this.sticky.toBottom();
            this.runs.watch(detail);
        } catch (e) {
            if (seq === this.loadSeq) {
                const gone = e instanceof IdeError && (e.status === 404 || e.status === 403);
                this.s().setProperty("/loadFailed", true);
                this.s().setProperty("/loadFailedText", gone ? this.text("sessionNotFound") : errorText(e, (k, a) => this.text(k, a)));
            }
        } finally {
            if (seq === this.loadSeq) {
                this.s().setProperty("/busy", false);
            }
        }
    }

    private async reloadDetail(): Promise<void> {
        const id = this.sid;
        const detail = await this.service().getSession(id);
        if (id === this.sid) {
            this.setDetail(detail);
        }
    }

    // --- Header --------------------------------------------------------------

    private setDetail(detail: SessionDetail): void {
        this.detail = detail;
        const isDiagnose = detail.type === "diagnose";
        const m = this.s();
        m.setProperty("/session", detail);
        m.setProperty("/isDiagnose", isDiagnose);
        m.setProperty("/typeText", this.text(isDiagnose ? "sessionTypeDiagnose" : "sessionTypeChange"));
        m.setProperty("/lostFlag", isDiagnose && !detail.target_non_production);
        this.refreshDiagnose();
        m.setProperty("/usageText", this.text("sessionUsage", [detail.requests_used ?? 0, detail.request_cap ?? 0]));
        this.originalTitle ??= document.title;
        document.title = this.text("sessionDocTitle", [detail.title]);
        this.setShellTarget(detail.target);
        this.refreshHeader();
        this.refreshDocMeta();
        if (this.cardsStale(detail)) {
            // A newer session than the cards (read after a syntax check, a run, a refusal): they catch up.
            void this.syncChanges();
        }
    }

    /**
     * The diagnose header: the banner (what is sent and kept, and how many
     * trace requests wait), the findings count and the latest report.
     * Nothing claims masking: on a flagged target nothing is masked.
     */
    private refreshDiagnose(): void {
        const detail = this.detail;
        const m = this.s();
        if (!detail || detail.type !== "diagnose") {
            m.setProperty("/banner", "");
            m.setProperty("/diagnose", { findingsText: "", reportText: "" });
            return;
        }
        const pending = pendingApprovalCount(this.approvals);
        const waiting = pending === 0 ? "" : pending === 1 ? this.text("diagnosePendingOne") : this.text("diagnosePendingMany", [pending]);
        const days = this.retentionDays;
        const banner = days === undefined ? this.text("diagnoseBannerKept")
            : days === 0 ? this.text("diagnoseBannerUntilDeleted")
                : days === 1 ? this.text("diagnoseBannerOneDay") : this.text("diagnoseBannerDays", [days]);
        m.setProperty("/banner", waiting ? `${banner} ${waiting}` : banner);
        const report = this.latestVersion("report");
        m.setProperty("/diagnose", {
            findingsText: this.text("findingsTitle", [this.findings.length]),
            reportText: report === undefined ? "" : this.text("artifactDocumentTitle", [this.kindText("report"), report])
        });
    }

    /** A change session used its model requests: no message and no request-changes run starts. */
    private static exhausted(detail: SessionDetail): boolean {
        const cap = detail.request_cap ?? 0;
        return detail.type === "change" && cap > 0 && (detail.requests_used ?? 0) >= cap;
    }

    /** Timeline, primary action, Request changes and Send, from the session and whether a run is ours. */
    private refreshHeader(): void {
        const detail = this.detail;
        if (!detail) {
            return;
        }
        const running = this.runs.running || detail.status === "running";
        const view = { ...detail, status: running ? "running" as const : detail.status };
        const action = primaryAction(view, detail.artifacts ?? [], detail.files ?? []);
        const m = this.s();
        // Approve sends the revisions on screen: the latest of every object, all of them loaded.
        const older = action.enabled && action.key === "approveChanges" && this.olderRevisionShown(detail);
        // The session has proposals the cards do not show yet (a new revision is being read).
        const stale = action.enabled && action.key === "approveChanges" && !older && this.cardsStale(detail);
        const missing = action.enabled && action.key === "approveChanges" && !older && !stale && this.cardMissing();
        // A document stage approves its latest version: not while an older one is open.
        const olderDoc = action.enabled ? this.olderDocumentShown(detail, action.version) : null;
        // Just appeared (or not seen yet, in a hidden tab): off for a moment, and it says why.
        const settling = action.key === "approveChanges" && performance.now() - this.cardsShownAt < APPROVE_SETTLE_MS;
        m.setProperty("/running", running);
        m.setProperty("/tokens", this.tokens(detail));
        if (this.reviewFirst(action.key, detail)) {
            // Approve changes only with the cards on screen: until then the action opens them.
            m.setProperty("/primary", { key: "reviewChanges", text: this.text("primaryReviewChanges"), enabled: true, reason: "", busy: false });
        } else {
            m.setProperty("/primary", {
                key: action.key,
                text: this.text(action.textKey, action.textArgs),
                enabled: action.enabled && !older && !stale && !missing && !olderDoc && !settling && !this.approving && !this.handingOver,
                reason: older ? this.text("gateOlderRevision") : stale ? this.text("gateCardsLoading")
                    : missing ? this.text("gateObjectNotLoaded")
                    : olderDoc ? this.text("gateOlderDocument", [olderDoc.shown, olderDoc.latest])
                    : action.enabled && settling && this.settleExplains && !this.approving ? this.text("gateApproveSettling")
                        : action.enabled || !action.reasonKey ? "" : this.text(action.reasonKey, action.reasonArgs),
                // An approve keeps it busy until the session and the cards show what it did; so does a hand over.
                busy: this.approving || this.handingOver
            });
        }
        this.keepPrimaryFocus();
        const open = detail.open_comments ?? 0;
        const exhausted = Session.exhausted(detail);
        const rcVisible = detail.type === "change" && REVISABLE.includes(detail.stage);
        // Enabled in every revisable stage: a note alone may be sent (D1); the dialog's Send checks for content.
        const rcEnabled = !running && !exhausted;
        m.setProperty("/requestChanges", {
            text: open > 0 ? this.text("requestChangesCount", [open]) : this.text("requestChanges"),
            visible: rcVisible,
            enabled: rcEnabled,
            // Visible text when it is off for a reason other than a run (Stop says that).
            reason: !rcVisible || rcEnabled || running ? ""
                : this.text("requestChangesUsageExhausted", [detail.request_cap])
        });
        m.setProperty("/reportAgain", {
            visible: action.key === "handover",
            enabled: !running && detail.target_non_production
        });
        const sendReason = detail.stage === "done" ? this.text("gateStageDone")
            : exhausted ? this.text("gateUsageExhausted", [detail.request_cap])
                : detail.type === "diagnose" && !detail.target_non_production ? this.text("targetNotNonProd") : "";
        m.setProperty("/sendReason", sendReason);
        m.setProperty("/canType", !sendReason);
        m.setProperty("/canSend", !running && !sendReason);
    }

    /** The changes view is open and its cards are loaded: what an approve in propose sends is on screen. */
    private changesShown(): boolean {
        return this.query.view === "changes" && !!this.s().getProperty("/artifact/changes/loaded");
    }

    /**
     * In propose with proposals, while the cards are not on screen, the
     * primary action opens the changes view instead of approving (lead
     * decision after U10): the revisions an approve sends always come from
     * what the developer looked at.
     */
    private reviewFirst(key: string, detail: SessionDetail): boolean {
        return key === "approveChanges" && !this.changesShown() && proposedObjects(detail.files).length > 0;
    }

    /**
     * A proposed object is shown at a revision older than its latest (the
     * user picked it). Only the changes view shows picks: on any other view
     * they hold nothing back.
     */
    private olderRevisionShown(detail: SessionDetail): boolean {
        if (!this.changesShown()) {
            return false;
        }
        return proposedObjects(detail.files).some((f) => {
            const pick = this.picked.get(f.path);
            return !!pick && pick.latest === f.revision && pick.revision < f.revision;
        });
    }

    /** A card of the changes view could not be read: an approve would not cover every object (never partial). */
    private cardMissing(): boolean {
        return this.changesShown() && this.changes.some((o) => o.failed);
    }

    /**
     * The cards are behind the session: it lists other proposed objects, or
     * a newer revision of one, than the cards were read for (a run ended, a
     * `file` event, a refusal). Until they caught up, an approve would pin
     * revisions that are not on screen.
     */
    private cardsStale(detail: SessionDetail): boolean {
        if (!this.changesShown()) {
            return false;
        }
        const files = proposedObjects(detail.files);
        return files.length !== this.changes.length
            || files.some((f, i) => f.path !== this.changes[i].path || f.revision !== this.changes[i].latest);
    }

    /** The stage's document is open at a version older than the one its approve pins (`version`). */
    private olderDocumentShown(detail: SessionDetail, version: number | undefined): { shown: number; latest: number } | null {
        const kind = PINNED[detail.stage];
        const doc = this.doc;
        if (detail.type !== "change" || !kind || version === undefined || this.query.view !== "document" || doc?.kind !== kind) {
            return null;
        }
        // Until the version asked for is on screen (still read, or its read failed), the one before it is what was seen.
        const before = this.docOnScreen;
        const shown = !doc.rendered && before?.kind === kind ? Math.min(doc.version, before.version) : doc.version;
        return shown < version ? { shown, latest: version } : null;
    }

    /** What holds an approve back although the session allows it: what is on screen is not what it would pin. */
    private approveHeld(detail: SessionDetail, key: string, version: number | undefined): boolean {
        return (key === "approveChanges" && (this.olderRevisionShown(detail) || this.cardsStale(detail) || this.cardMissing()))
            || !!this.olderDocumentShown(detail, version);
    }

    private tokens(detail: SessionDetail): Record<string, unknown>[] {
        const pins = detail.pins ?? {};
        return stageTokens(detail.stage, detail.type, { withDone: false }).map((t) => {
            const name = t.stage === "propose" ? this.text("stageTokenPropose") : this.stageText(t.stage);
            const pinKind = PINNED[t.stage];
            const pin = t.state === "done" && pinKind && pins[pinKind] !== undefined
                ? this.text("stagePinned", [pins[pinKind]!]) : "";
            const stateText = this.text(t.state === "done" ? "stageStateDone"
                : t.state === "current" ? "stageStateCurrent" : "stageStateUpcoming");
            const kind = STAGE_KIND[t.stage];
            const hasChanges = t.stage === "propose" && proposedObjects(detail.files).length > 0;
            return {
                stage: t.stage,
                text: name,
                pin,
                current: t.state === "current",
                // Not colour alone: an icon per state, and the state in the tooltip.
                icon: t.state === "done" ? "sap-icon://accept" : t.state === "current" ? "sap-icon://process" : "sap-icon://circle-task",
                state: t.state === "done" ? "Success" : t.state === "current" ? "Information" : "None",
                tooltip: this.text("stageTokenTooltip", [name, pin || stateText]),
                // Read after the name ("Design, current stage"): the state as text, not only colour, icon and tooltip.
                stateText,
                active: hasChanges || (!!kind && (detail.artifacts ?? []).some((a) => a.kind === kind))
            };
        });
    }

    private stageText(stage: Stage): string {
        return this.text(`stage${stage.charAt(0).toUpperCase()}${stage.slice(1)}`);
    }

    /** `aria-current="step"` on the current token and list semantics on the timeline (no UI5 property for them). */
    private decorateTimeline(): void {
        const dom = this.byId("stageTimeline")?.getDomRef();
        if (!dom) {
            return;
        }
        dom.setAttribute("role", "list");
        dom.setAttribute("aria-label", this.text("sessionTimelineLabel"));
        dom.querySelectorAll<HTMLElement>("[data-current]").forEach((el) => {
            el.setAttribute("role", "listitem");
            if (el.getAttribute("data-current") === "step") {
                el.setAttribute("aria-current", "step");
            } else {
                el.removeAttribute("aria-current");
            }
        });
    }

    public onStageTokenPress(event: UI5Event): void {
        const source = event.getSource() as Control;
        const stage = source.getBindingContext("s")?.getProperty("stage") as Stage | undefined;
        if (stage === "propose" && proposedObjects(this.detail?.files).length > 0) {
            // The proposals are what this stage made: the changes view, not the note.
            this.opener = source;
            this.getRouter().navTo("session", { id: this.sid, "?query": { view: "changes" } });
            return;
        }
        const kind = stage ? STAGE_KIND[stage] : undefined;
        const version = kind ? this.latestVersion(kind) : undefined;
        if (kind && version !== undefined) {
            this.opener = source;
            this.getRouter().navTo("session", { id: this.sid, "?query": { view: "document", kind, version: String(version) } });
        }
    }

    private latestVersion(kind: ArtifactKind): number | undefined {
        const versions = (this.detail?.artifacts ?? []).filter((a) => a.kind === kind).map((a) => a.version);
        return versions.length ? Math.max(...versions) : undefined;
    }

    // --- Primary action, Request changes, Stop ------------------------------

    public async onPrimaryAction(): Promise<void> {
        const detail = this.detail;
        if (!detail) {
            return;
        }
        const action = primaryAction(detail, detail.artifacts ?? [], detail.files ?? []);
        if (this.reviewFirst(action.key, detail)) {
            this.opener = this.byId("primaryAction") as Control | undefined;
            this.getRouter().navTo("session", { id: this.sid, "?query": { view: "changes" } });
            return;
        }
        const held = this.approveHeld(detail, action.key, action.version);
        const early = action.key === "approveChanges" && !held && this.approveTooEarly();
        if (!action.enabled || this.approving || held || early) {
            // A press that began before the cards appeared is used up: the next one is judged on its own.
            this.primaryPointerAt = undefined;
            if (early && action.enabled && !this.approving) {
                // Never a silent no-op: the button looked pressable.
                this.announce(this.text("approveIgnoredEarly"));
            }
            return;
        }
        if (action.key === "report") {
            this.startRun("report", "");
            return;
        }
        if (action.key === "handover") {
            this.confirmHandover(detail);
            return;
        }
        const unseen = action.key === "approveChanges" ? notCompared(this.changes) : [];
        if (unseen.length) {
            // An object whose diff was not shown is approved only on an explicit answer that names it.
            this.confirmUncompared(unseen);
            return;
        }
        await this.approveStage(detail, action);
    }

    /**
     * Asks before an approve that covers objects the changes view could not
     * compare: each is named, there is no default action and the focus starts
     * on Cancel (Enter cancels). On the confirmed answer the approve goes out
     * only if the cards still show what was asked about and nothing else
     * holds it back.
     */
    private confirmUncompared(unseen: ChangeObject[]): void {
        const sid = this.sid;
        const asked = JSON.stringify(approveRevisions(this.changes));
        const approve = this.text("approveUncomparedAction");
        const lines = unseen.map((o) => this.text("approveUncomparedObject", [this.objectName(o)]));
        MessageBox.warning([...lines, this.text("approveUncomparedQuestion")].join("\n\n"), {
            title: this.text("approveUncomparedTitle"),
            actions: [approve, MessageBox.Action.CANCEL],
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (answer: string | null) => {
                const detail = this.detail;
                if (answer !== approve || sid !== this.sid || !detail) {
                    return;
                }
                const action = primaryAction(detail, detail.artifacts ?? [], detail.files ?? []);
                const settling = performance.now() - this.cardsShownAt < APPROVE_SETTLE_MS;
                if (action.key !== "approveChanges" || !action.enabled || this.approving || settling
                    || this.approveHeld(detail, action.key, action.version) || JSON.stringify(approveRevisions(this.changes)) !== asked) {
                    // The cards changed while the question was open: the header says what holds the approve now.
                    this.refreshHeader();
                    return;
                }
                void this.approveStage(detail, action);
            }
        });
    }

    /** The approve itself: busy until the session and the cards show what it did. */
    private async approveStage(detail: SessionDetail, action: ReturnType<typeof primaryAction>): Promise<void> {
        this.approving = true;
        this.parkPrimaryFocus();
        this.s().setProperty("/primary/busy", true);
        const sid = this.sid;
        const token = this.actionSeq;
        // The page left this session meanwhile (leave reset the flags): the answer is not shown elsewhere.
        const gone = (): boolean => token !== this.actionSeq || sid !== this.sid;
        try {
            // In propose the revisions of the cards on screen (all loaded, see cardMissing); any other stage sends none.
            const revisions = detail.stage === "propose" ? approveRevisions(this.changes) : undefined;
            await this.service().approve(sid, action.version, revisions);
            if (!gone()) {
                MessageToast.show(this.text(action.key === "finish" ? "primaryDone" : "stageApproved",
                    [this.stageText(this.nextStage(detail.stage))]));
            }
        } catch (e) {
            // open_comments and the other gate refusals: the reloaded session shows the reason in the header.
            if (!gone() && !(e instanceof IdeError && e.status === 409 && e.code === "open_comments")) {
                this.showGateError(e);
            }
        } finally {
            if (!gone()) {
                try {
                    // A refusal (version_changed) means the cards are stale: they show what is there now.
                    await this.reloadDetail().catch((e) => this.showError(e));
                    await this.syncChanges();
                } finally {
                    // Busy until the header and the cards show the result: no second press on stale state.
                    if (!gone()) {
                        this.approving = false;
                        this.s().setProperty("/primary/busy", false);
                        this.refreshHeader();
                        this.releasePrimaryFocus();
                    }
                }
            }
        }
    }

    /** Whether the focus has nowhere to be (it fell to the page body). */
    private static focusLost(): boolean {
        return !document.activeElement || document.activeElement === document.body;
    }

    /** The primary action has the focus right now, and is about to turn off or busy under it. */
    private parkPrimaryFocus(): void {
        const dom = this.byId("primaryAction")?.getDomRef();
        if (dom && document.activeElement === dom) {
            this.primaryFocusParked = true;
        }
    }

    /**
     * A focused button that turns disabled or busy (the settle window of new
     * cards, an approve in flight) drops the focus to the page body. The
     * keyboard user's place is kept: once the action can take the focus again
     * it gets it back, unless the focus moved on meanwhile.
     */
    private keepPrimaryFocus(): void {
        const primary = this.s().getProperty("/primary") as { enabled?: boolean; busy?: boolean } | undefined;
        const usable = !!primary?.enabled && !primary.busy;
        if (!usable) {
            this.parkPrimaryFocus();
            return;
        }
        if (this.primaryFocusParked) {
            this.primaryFocusParked = false;
            setTimeout(() => {
                if (Session.focusLost()) {
                    (this.byId("primaryAction") as Control | undefined)?.focus();
                }
            }, 0);
        }
    }

    /**
     * After an approve the next action may stay off (no document of the new
     * stage yet): the focus that sat on the button goes to the composer, where
     * the next request is typed, instead of the page body.
     */
    private releasePrimaryFocus(): void {
        if (!this.primaryFocusParked) {
            return;
        }
        this.primaryFocusParked = false;
        setTimeout(() => {
            if (!Session.focusLost()) {
                return;
            }
            const primary = this.byId("primaryAction") as Button | undefined;
            const input = this.byId("chatInput") as TextArea | undefined;
            if (primary?.getEnabled() && !primary.getBusy()) {
                primary.focus();
            } else if (input?.getEnabled()) {
                input.focus();
            } else {
                (this.byId("artifactTitle") as Control | undefined)?.focus();
            }
        }, 0);
    }

    private nextStage(stage: Stage): Stage {
        const order: Stage[] = ["chat", "design", "plan", "propose", "review", "done"];
        return order[Math.min(order.indexOf(stage) + 1, order.length - 1)];
    }

    /** Request changes: the dialog lists the open comments it sends and takes an optional note. */
    public async onRequestChanges(): Promise<void> {
        const sid = this.sid;
        await this.reloadComments().catch(() => undefined);
        if (sid !== this.sid) {
            return;
        }
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        const open = this.comments.filter((c) => c.state === "open");
        this.s().setProperty("/rc", {
            intro: open.length === 0 ? this.text("requestChangesIntroNone")
                : open.length === 1 ? this.text("requestChangesIntroOne") : this.text("requestChangesIntro", [open.length]),
            comments: open.map((c) => ({ id: c.id, anchorText: anchorText(c, t), body: String(c.body ?? "") })),
            note: this.refusedNote ?? "",
            busy: false,
            canSend: false
        });
        this.onRequestChangesNoteChange();
        this.requestChangesDialog ??= this.loadFragment({ name: "com.agent.ide.fragment.RequestChangesDialog" }) as Promise<Dialog>;
        (await this.requestChangesDialog).open();
    }

    /** Send needs an open comment or a note that is not blank (the event's value: the binding may lag). */
    public onRequestChangesNoteChange(event?: UI5Event): void {
        const typed = (event?.getParameters() as { value?: string } | undefined)?.value;
        const rc = this.s().getProperty("/rc") as { comments: unknown[]; note: string };
        const note = typeof typed === "string" ? typed : String(rc.note ?? "");
        this.s().setProperty("/rc/canSend", rc.comments.length > 0 || !!note.trim());
    }

    /** Send: once (a double press or a run already going sends nothing more); the dialog closes. */
    public async onRequestChangesSend(): Promise<void> {
        const rc = this.s().getProperty("/rc") as { comments: unknown[]; note: string; busy: boolean };
        const note = String(rc.note ?? "").trim();
        if (rc.busy || (!rc.comments.length && !note)) {
            return;
        }
        if (this.runs.running || this.detail?.status === "running") {
            // A run started meanwhile (here or elsewhere): nothing is sent, the note is kept, the dialog says why.
            this.refusedNote = note || undefined;
            (await this.requestChangesDialog)?.close();
            MessageBox.information(this.text("requestChangesRunElsewhere"));
            return;
        }
        this.s().setProperty("/rc/busy", true);
        this.refusedNote = undefined;
        this.startRun("requestChanges", note);
        (await this.requestChangesDialog)?.close();
    }

    public async onRequestChangesCancel(): Promise<void> {
        (await this.requestChangesDialog)?.close();
    }

    public onRequestChangesClosed(): void {
        this.s().setProperty("/rc/note", "");
    }

    public onCreateReport(): void {
        this.startRun("report", "");
    }

    /** Hand over: asked first (a new session is created); Cancel sends nothing. */
    private confirmHandover(detail: SessionDetail): void {
        MessageBox.confirm(this.text("handoverConfirm", [detail.title, detail.target]), {
            actions: [MessageBox.Action.OK, MessageBox.Action.CANCEL],
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: unknown) => {
                if (action === MessageBox.Action.OK && this.sid === detail.id) {
                    void this.handover(detail.id);
                }
            }
        });
    }

    /** The new change session opens on its page with the report it carries (version 1 there). */
    private async handover(sid: string): Promise<void> {
        if (this.handingOver) {
            return;
        }
        this.handingOver = true;
        this.refreshHeader();
        const token = this.actionSeq;
        // The page left this session meanwhile: no toast, no navigation, and the flags are the next session's.
        const gone = (): boolean => token !== this.actionSeq || sid !== this.sid;
        try {
            const created = await this.service().handover(sid);
            if (!gone()) {
                MessageToast.show(this.text("handoverDone", [created.title]));
                this.getRouter().navTo("session", { id: created.id, "?query": { view: "document", kind: "report", version: "1" } });
            }
        } catch (e) {
            if (!gone()) {
                this.showGateError(e);
            }
        } finally {
            if (!gone()) {
                this.handingOver = false;
                this.refreshHeader();
            }
        }
    }

    public onStop(): void {
        this.runs.stop(this.sid).catch((e) => this.showGateError(e));
    }

    private showGateError(e: unknown): void {
        MessageBox.error(gateErrorText(e, (k, a) => this.text(k, a)));
        this.afterLostFlag(e);
    }

    /** The target lost its flag meanwhile: the session is read again, so the strip and the disabled actions show it. */
    private afterLostFlag(e: unknown): void {
        if (e instanceof IdeError && e.code === "target_not_non_production" && this.sid) {
            void this.reloadDetail().catch(() => undefined);
        }
    }

    // --- Conversation (Task U8) -----------------------------------------------

    /** Forgets the conversation of the session left behind. */
    private resetConversation(): void {
        this.messages = [];
        this.approvals = [];
        this.liveApprovals.clear();
        this.pendingQuestion = undefined;
        this.streaming = false;
        this.streamHtml = "";
        this.streamStatus = "";
        this.activities.clear();
        this.openActivities.clear();
        this.activityErrors.clear();
        this.loadingActivities.clear();
        this.allActivities.clear();
        this.openOutputs.clear();
        this.htmlCache.clear();
        this.deciding.clear();
        // A refused request-changes note belongs to the session it was written in.
        this.refusedNote = undefined;
    }

    public onSend(): void {
        const text = String(this.s().getProperty("/draft") ?? "").trim();
        if (!text || text.length > DRAFT_MAX || !this.s().getProperty("/canSend")) {
            return;
        }
        this.s().setProperty("/draft", "");
        this.pendingQuestion = text;
        this.startRun("message", text);
    }

    private startRun(kind: RunKind, text: string): void {
        if (this.runs.running) {
            // One run at a time: a start while ours runs changes nothing (not even the panel state).
            return;
        }
        const sid = this.sid;
        this.s().setProperty("/runError", { text: "", type: "Error" });
        this.ownRun = true;
        this.streaming = true;
        this.streamHtml = "";
        this.streamStatus = this.text("assistantWorking");
        this.openActivities.delete(STREAMING);
        // Nothing of the last streamed answer's panel state carries into this run.
        this.allActivities.delete(STREAMING);
        this.forgetOutputs(STREAMING);
        this.renderConversation();
        // The reader asked for this: the page goes to the end and follows the answer.
        this.sticky.toBottom();
        const started = this.runs.start(sid, kind, text);
        this.refreshHeader();
        void started.then(() => this.refreshHeader());
    }

    public onJumpToLatest(): void {
        this.sticky.toBottom();
        // The button goes away with the press: the focus goes on to the message input.
        (this.byId("chatInput") as TextArea | undefined)?.focus();
    }

    /** The rows of `s>/messages`: stored messages and approvals by time, then what the run of this page adds. */
    private renderConversation(): void {
        // The banner counts the pending trace requests: it follows every change of the approvals.
        this.refreshDiagnose();
        const rows: MessageRow[] = interleave(this.messages, this.approvals.filter((a) => !this.liveApprovals.has(a.id)))
            .map((e) => (e.type === "message" ? this.messageRow(e.message) : this.approvalEntry(e.approval)));
        if (this.pendingQuestion !== undefined) {
            rows.push({ ...this.blankRow(`local-question`), isUser: true, author: this.text("messageYou"), content: this.pendingQuestion });
        }
        // Oldest first, as they came in.
        [...this.approvals].reverse().filter((a) => this.liveApprovals.has(a.id)).forEach((a) => rows.push(this.approvalEntry(a)));
        if (this.streaming) {
            rows.push(this.streamRow());
        }
        this.s().setProperty("/messages", rows);
    }

    private blankRow(id: string): MessageRow {
        return {
            id, kind: "message", isUser: false, author: this.text("messageAssistant"), content: "", html: "",
            streaming: false, statusText: "", hasActivity: false, activityOpen: false, activityBusy: false,
            activityTitle: "", activityError: "", todos: [], events: [], hiddenEvents: 0, showAllText: "", approval: null
        };
    }

    private messageRow(msg: Message): MessageRow {
        // Model text only through the sanitising renderer; the user's text stays text.
        const item = toChatItem(msg, (md) => this.htmlCache.get(msg.id, md, renderMarkdown));
        const loaded = this.activities.get(msg.id);
        return {
            ...this.blankRow(msg.id),
            isUser: item.isUser,
            author: this.text(item.isUser ? "messageYou" : msg.role === "system" ? "messageSystem" : "messageAssistant"),
            content: item.content,
            html: item.html,
            statusText: item.cancelled ? this.text("messageCancelled") : "",
            hasActivity: !item.isUser && !!msg.has_activity,
            activityOpen: this.openActivities.has(msg.id),
            activityBusy: this.loadingActivities.has(msg.id),
            activityError: this.activityErrors.get(msg.id) ?? "",
            ...this.activityView(loaded, msg.id)
        };
    }

    /** The answer being streamed, with the run's live activity. */
    private streamRow(): MessageRow {
        const live = this.runs.activity;
        const activity: Activity = { events: live.events, plan: live.todos, dropped: 0 };
        return {
            ...this.blankRow(STREAMING),
            streaming: true,
            content: this.streamHtml ? "" : this.streamStatus,
            html: this.streamHtml,
            statusText: this.streamHtml ? this.streamStatus : "",
            hasActivity: live.events.length > 0 || live.todos.length > 0,
            activityOpen: this.openActivities.has(STREAMING),
            ...this.activityView(activity, STREAMING)
        };
    }

    /** The panel's title and lists for a loaded activity; "Show activity" until one is. */
    private activityView(
        activity: Activity | undefined, id: string
    ): Pick<MessageRow, "activityTitle" | "todos" | "events" | "hiddenEvents" | "showAllText"> {
        if (!activity) {
            return { activityTitle: this.text("activityShow"), todos: [], events: [], hiddenEvents: 0, showAllText: "" };
        }
        const all = activity.events ?? [];
        const shown = visibleRows(all, this.allActivities.has(id));
        const { tools, steps } = activityCounts(activity);
        const toolText = this.text(tools === 1 ? "activityToolCallsOne" : "activityToolCalls", [tools]);
        return {
            activityTitle: steps
                ? this.text("activityHeader", [toolText, this.text(steps === 1 ? "activityPlanStepsOne" : "activityPlanSteps", [steps])])
                : this.text("activityHeaderToolsOnly", [toolText]),
            todos: todoView(activity.plan ?? []),
            events: this.eventView(shown.rows, id),
            hiddenEvents: shown.hidden,
            showAllText: shown.hidden ? this.text("activityShowAll", [all.length]) : ""
        };
    }

    private eventView(events: ToolEventData[], id = ""): unknown[] {
        return eventRows(events).map((row, index) => ({
            ...row,
            title: row.labelKey ? this.text(row.labelKey) : row.title,
            refusedText: row.refusedKey ? this.text(row.refusedKey) : "",
            statusText: row.statusKey ? this.text(row.statusKey) : "",
            longOutput: isLongOutput(row.output),
            outputOpen: this.openOutputs.has(`${id}#${index}`)
        }));
    }

    private forgetOutputs(id: string): void {
        Array.from(this.openOutputs).filter((k) => k.startsWith(`${id}#`)).forEach((k) => this.openOutputs.delete(k));
    }

    private moveOutputs(from: string, to: string): void {
        Array.from(this.openOutputs).filter((k) => k.startsWith(`${from}#`)).forEach((k) => {
            this.openOutputs.delete(k);
            this.openOutputs.add(`${to}#${k.slice(from.length + 1)}`);
        });
    }

    /** Sets properties of one row in place (no re-render of the whole list, the focus stays). */
    private patchRow(id: string, props: Partial<MessageRow>): void {
        const rows = this.s().getProperty("/messages") as MessageRow[];
        const index = rows.findIndex((r) => r.id === id);
        if (index >= 0) {
            Object.entries(props).forEach(([key, value]) => this.s().setProperty(`/messages/${index}/${key}`, value));
        }
    }

    /** A message's activity panel was opened or closed; the first opening reads it. */
    public onActivityExpand(event: UI5Event): void {
        const id = (event.getSource() as Control).getBindingContext("s")?.getProperty("id") as string | undefined;
        const expand = !!(event.getParameters() as { expand?: boolean }).expand;
        if (!id) {
            return;
        }
        if (!expand) {
            this.openActivities.delete(id);
            return;
        }
        this.openActivities.add(id);
        if (id !== STREAMING && !this.activities.has(id) && !this.loadingActivities.has(id)) {
            void this.loadActivity(id);
        }
    }

    /** "Show all (n)": every tool row of that message's activity. */
    public onActivityShowAll(event: UI5Event): void {
        const id = (event.getSource() as Control).getBindingContext("s")?.getProperty("id") as string | undefined;
        if (!id) {
            return;
        }
        this.allActivities.add(id);
        const live = this.runs.activity;
        const activity = id === STREAMING ? { events: live.events, plan: live.todos, dropped: 0 } : this.activities.get(id);
        this.patchRow(id, this.activityView(activity, id));
    }

    /** "Show more" / "Show less" on a clipped tool output: the row's own flag, in place. */
    public onActivityOutputToggle(event: UI5Event): void {
        const ctx = (event.getSource() as Control).getBindingContext("s");
        const m = /^(\/messages\/\d+)\/events\/(\d+)$/.exec(ctx?.getPath() ?? "");
        if (ctx) {
            const open = !ctx.getProperty("outputOpen");
            this.s().setProperty(`${ctx.getPath()}/outputOpen`, open);
            if (m) {
                // Remembered, so a rebuild of the rows (a new tool frame, "Show all") keeps it open.
                const key = `${String(this.s().getProperty(`${m[1]}/id`))}#${m[2]}`;
                if (open) {
                    this.openOutputs.add(key);
                } else {
                    this.openOutputs.delete(key);
                }
            }
        }
    }

    private async loadActivity(mid: string): Promise<void> {
        const sid = this.sid;
        const seq = this.loadSeq;
        this.loadingActivities.add(mid);
        this.activityErrors.delete(mid);
        this.patchRow(mid, { activityBusy: true, activityError: "" });
        try {
            const activity = await this.service().getActivity(sid, mid);
            if (seq !== this.loadSeq || sid !== this.sid) {
                return;
            }
            this.activities.set(mid, activity);
            this.patchRow(mid, this.activityView(activity, mid));
        } catch (e) {
            if (seq !== this.loadSeq || sid !== this.sid) {
                return;
            }
            const text = e instanceof IdeError && e.code === "no_activity"
                ? this.text("activityNone") : errorText(e, (k, a) => this.text(k, a));
            this.activityErrors.set(mid, text);
            this.patchRow(mid, { activityError: text });
        } finally {
            this.loadingActivities.delete(mid);
            if (seq === this.loadSeq && sid === this.sid) {
                this.patchRow(mid, { activityBusy: false });
            }
        }
    }

    private renderStream(state: RunState): void {
        this.streamHtml = state.text ? renderMarkdown(state.text) : "";
        const tool = activeTool(state);
        const label = tool ? this.eventView([tool])[0] as { title: string } : undefined;
        this.streamStatus = label ? this.text("assistantUsingTool", [label.title]) : state.text ? "" : this.text("assistantWorking");
        // The three text fields only: the activity rows follow their own events (tool / plan).
        this.patchRow(STREAMING, {
            html: this.streamHtml,
            content: this.streamHtml ? "" : this.streamStatus,
            statusText: this.streamHtml ? this.streamStatus : ""
        });
    }

    private onRunEvent(e: SseEvent): void {
        switch (e.type) {
            case "usage":
                this.s().setProperty("/usageText", this.text("sessionUsage", [e.data.requests_used, e.data.request_cap]));
                if (this.detail) {
                    this.detail.requests_used = e.data.requests_used;
                    this.detail.request_cap = e.data.request_cap;
                }
                break;
            case "tool":
            case "plan": {
                const row = this.streamRow();
                this.patchRow(STREAMING, {
                    hasActivity: row.hasActivity, activityTitle: row.activityTitle, todos: row.todos, events: row.events,
                    hiddenEvents: row.hiddenEvents, showAllText: row.showAllText
                });
                break;
            }
            case "approval_required": {
                // A trace proposal: a card after the question, the run does not wait for it.
                const before = this.approvals;
                this.approvals = reduceApprovals(this.approvals, e);
                this.liveApprovals.add(e.data.id);
                this.renderConversation();
                this.announceNewApprovals(before, this.approvals);
                break;
            }
            case "comments": {
                // Comment states changed (sent / addressed): the header counts and the document's comments follow.
                void this.reloadDetail().catch(() => undefined);
                void this.reloadComments().catch(() => undefined);
                const left = e.data.state === "sent" ? commentsLeftText(e.data.left, (k, a) => this.text(k, a)) : "";
                if (left) {
                    this.s().setProperty("/runError", { text: left, type: "Information" });
                    this.announce(left);
                }
                break;
            }
            case "artifact":
                void this.onArtifactStored(e.data.kind, e.data.version);
                break;
            case "finding":
                // A finding of this run: listed at once, a read of the list cannot overwrite it any more.
                this.findingsSeq++;
                this.findings = upsertFinding(this.findings, e.data);
                this.renderFindings();
                break;
            case "error": {
                // In the conversation until the next message, not a passing toast.
                const note = isRunNote(e.data.code);
                const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
                this.s().setProperty("/runError", {
                    text: note ? runNoteText(e.data, t) : runErrorText(e.data, t),
                    type: note ? "Warning" : "Error"
                });
                break;
            }
            default:
                break;
        }
    }

    public onRunErrorClose(): void {
        this.s().setProperty("/runError", { text: "", type: "Error" });
    }

    /** One polite announcement per answer of this page, when it is complete (never per delta). */
    private announceEnd(): void {
        if (!this.ownRun) {
            return;
        }
        this.ownRun = false;
        const strip = this.s().getProperty("/runError") as { text?: string; type?: string };
        const error = strip.type === "Information" ? "" : String(strip.text ?? "");
        this.announce(error || this.text("announceAnswered"));
    }

    private announce(text: string): void {
        if (text) {
            InvisibleMessage.getInstance().announce(text, InvisibleMessageMode.Polite);
        }
    }

    /** A pending approval that was not on screen before is announced once; the focus stays. */
    private announceNewApprovals(before: Approval[], after: Approval[]): void {
        const known = new Set(before.map((a) => a.id));
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        after.filter((a) => a.status === "pending" && !known.has(a.id)).forEach((a) => {
            this.announce(this.text("approvalAnnounce", [this.approvalEntry(a).approval?.title ?? "", paramsText(a.params, t)]));
        });
    }

    private onRunRefused(e: unknown, kind: RunKind, text: string): void {
        this.streaming = false;
        if (kind === "message") {
            // The refused question goes back into the composer, and out of the list.
            this.pendingQuestion = undefined;
            // Whatever was typed meanwhile stays; the refused text goes in front of it.
            const typed = String(this.s().getProperty("/draft") ?? "");
            this.s().setProperty("/draft", typed.trim() ? `${text}\n\n${typed}` : text);
        } else if (kind === "requestChanges" && text) {
            // As the composer: the refused note is not lost, the dialog offers it again.
            this.refusedNote = text;
        }
        this.renderConversation();
        this.ownRun = false;
        this.refreshHeader();
        this.showGateError(e);
    }

    /** After a run: the stored conversation, the approvals and the session (stage, comments, usage, artifacts). */
    private async afterRun(sid: string): Promise<void> {
        if (sid !== this.sid) {
            return;
        }
        const diagnose = this.detail?.type === "diagnose";
        const findingsSeq = ++this.findingsSeq;
        const [detail, messages, approvals, comments, findings] = await Promise.all([
            this.service().getSession(sid),
            this.service().listMessages(sid),
            diagnose ? this.service().listApprovals(sid) : Promise.resolve(this.approvals),
            diagnose ? Promise.resolve(this.comments) : this.service().listComments(sid),
            diagnose ? this.service().listFindings(sid).catch(() => null) : Promise.resolve(null)
        ]);
        if (sid !== this.sid) {
            return;
        }
        if (findings && findingsSeq === this.findingsSeq) {
            // The stored list after the run: also a finding the run stored without a frame.
            this.findings = findings;
            this.renderFindings();
        }
        // The streamed activity becomes the stored answer's: no read when it is opened.
        const doneId = this.runs.state.done?.message_id;
        const live = this.runs.activity;
        if (doneId && (live.events.length || live.todos.length)) {
            this.activities.set(doneId, { events: live.events.slice(), plan: live.todos.slice(), dropped: 0 });
            if (this.openActivities.delete(STREAMING)) {
                this.openActivities.add(doneId);
            }
            // "Show all" and opened outputs stay with the answer they were opened on.
            if (this.allActivities.delete(STREAMING)) {
                this.allActivities.add(doneId);
            }
            this.moveOutputs(STREAMING, doneId);
        }
        // Whatever was not handed on is the ended stream's: nothing of it carries into the next run.
        this.openActivities.delete(STREAMING);
        this.allActivities.delete(STREAMING);
        this.forgetOutputs(STREAMING);
        if (this.ownRun && this.runs.state.done) {
            // A run of ours went through: an earlier refused note is history.
            this.refusedNote = undefined;
        }
        this.messages = messages;
        this.approvals = approvals;
        this.setComments(comments);
        this.renderDocComments();
        this.liveApprovals.clear();
        this.pendingQuestion = undefined;
        this.streaming = false;
        this.renderConversation();
        this.setDetail(detail);
        // Still running (a Stop refused because the run is on another instance): watched until it ends,
        // so Stop goes away once the server says idle.
        this.runs.watch(detail);
        void this.syncChanges();
        this.announceEnd();
    }

    // --- Trace approvals (diagnose) ----------------------------------------------

    /** An API timestamp as the user's date and time; the raw value when it is none. */
    private formatTime(iso: string): string {
        const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
        return Number.isNaN(date.getTime()) ? iso : this.dateTime.format(date);
    }

    private approvalEntry(approval: Approval): MessageRow {
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        return {
            ...this.blankRow(`approval-${approval.id}`),
            kind: "approval",
            approval: {
                ...approvalRow(approval, this.detail?.target ?? "", t, (iso) => this.formatTime(iso)),
                busy: this.deciding.has(approval.id)
            }
        };
    }

    /**
     * Approve or Reject on a card: sent once, both buttons off until the
     * answer is there; the card then becomes the server's line in place.
     * 410 marks it expired, 409 `approval_not_pending` reloads the list, any
     * other refusal is a message box (model/approvals words the codes).
     */
    public onApprovalDecide(event: UI5Event): void {
        const source = event.getSource() as Control;
        const row = source.getBindingContext("s")?.getObject() as MessageRow | undefined;
        const decision = source.data("decision") as ApprovalDecision | undefined;
        if (row?.approval && (decision === "approve" || decision === "deny")) {
            void this.decideApproval(row.approval.id, decision);
        }
    }

    private async decideApproval(aid: string, decision: ApprovalDecision): Promise<void> {
        const sid = this.sid;
        const seq = this.loadSeq;
        const approval = this.approvals.find((a) => a.id === aid);
        if (!approval || approval.status !== "pending" || this.deciding.has(aid)) {
            return;
        }
        if (decision === "approve" && !this.approvalEntry(approval).approval?.canApprove) {
            return;
        }
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        this.deciding.add(aid);
        this.renderConversation();
        let reload = false;
        try {
            const decided = await this.service().decideApproval(sid, aid, decision);
            if (seq !== this.loadSeq) {
                return;
            }
            this.approvals = reduceApprovals(this.approvals, { type: "decided", data: decided });
            this.announce(this.approvalEntry(decided).approval?.statusText ?? "");
            if (decided.status === "failed") {
                MessageBox.error(approvalErrorText(decided.error_code, decided.result?.note, t));
            }
        } catch (e) {
            if (seq !== this.loadSeq) {
                return;
            }
            reload = true;
            const code = e instanceof IdeError ? e.code : undefined;
            if (code === "approval_expired" || (e instanceof IdeError && e.status === 410)) {
                this.approvals = reduceApprovals(this.approvals, {
                    type: "decided", data: { ...approval, status: "expired", error_code: "approval_expired" }
                });
                this.announce(this.text("approvalExpired"));
            } else if (code === "approval_not_pending") {
                MessageToast.show(this.text("approvalAlreadyDecided"));
            } else {
                const key = approvalErrorKey(code);
                MessageBox.error(key ? this.text(key) : gateErrorText(e, t));
            }
        } finally {
            this.deciding.delete(aid);
            if (seq === this.loadSeq) {
                if (reload) {
                    this.approvals = await this.service().listApprovals(sid).catch(() => this.approvals);
                }
                if (seq === this.loadSeq) {
                    this.renderConversation();
                    // The buttons that had the focus are gone: back to the message input.
                    (this.byId("chatInput") as TextArea | undefined)?.focus();
                }
            }
        }
    }

    // --- Artifact column -------------------------------------------------------

    /**
     * Shows what the query names in the artifact column. Each call wins over
     * an earlier one still loading (`artifactSeq`): the column is reset to the
     * new title before the await, and a late answer is dropped. The column's
     * title takes the focus once it is shown; closing it gives the focus back
     * to the opener (or the message input).
     */
    private async applyQuery(query: SessionQuery): Promise<void> {
        const seq = ++this.artifactSeq;
        const keepFocus = this.keepFocus;
        this.keepFocus = false;
        this.query = query;
        const view = VIEWS.includes(query.view as ArtifactView) ? query.view as ArtifactView : undefined;
        const m = this.s();
        this.closeCommentUi();
        this.doc = null;
        if (view !== "document") {
            this.docOnScreen = null;
        }
        this.rovingBlock = 0;
        this.resetChanges();
        if (!view) {
            m.setProperty("/layout", "OneColumn");
            m.setProperty("/artifact", Session.emptyArtifact());
            // No cards on screen any more: in propose the primary action reviews again.
            this.refreshHeader();
            if (this.closing) {
                this.closing = false;
                this.returnFocus();
            }
            return;
        }
        m.setProperty("/layout", "TwoColumnsMidExpanded");
        if (!keepFocus) {
            this.focusArtifactTitle(seq);
        }
        if (view !== "changes") {
            // Whatever replaces the cards: in propose the primary action reviews again.
            m.setProperty("/artifact/changes/loaded", false);
            this.refreshHeader();
        }
        if (view === "changes") {
            m.setProperty("/artifact", Session.emptyArtifact(this.text("artifactChangesTitle"), view));
            // Navigated here: the first render of the cards says why Approve waits a moment.
            this.explainSettle = true;
            this.refreshHeader();
            await this.loadChanges(seq);
            return;
        }
        if (view === "source") {
            await this.loadSource(seq, query);
            return;
        }
        if (view === "findings") {
            m.setProperty("/artifact", Session.emptyArtifact(this.text("findingsTitle", [this.findings.length]), view));
            if (this.detail && this.detail.type !== "diagnose") {
                // Only a diagnose session has findings.
                m.setProperty("/artifact/missing", this.text("artifactNotFound"));
            }
            this.renderFindings();
            return;
        }
        const kind = KINDS.includes(query.kind as ArtifactKind) ? query.kind as ArtifactKind : undefined;
        const version = query.version ? Number(query.version) : kind ? this.latestVersion(kind) : undefined;
        const summary = (this.detail?.artifacts ?? []).find((a) => a.kind === kind && a.version === version);
        const title = kind && version ? this.text("artifactDocumentTitle", [this.kindText(kind), version]) : "";
        m.setProperty("/artifact", Session.emptyArtifact(title, "document"));
        if (!summary || !kind || !version) {
            m.setProperty("/artifact/missing", this.text("artifactNotFound"));
            return;
        }
        this.doc = { kind, version, blocks: [] };
        this.refreshDocMeta();
        // An older version of the stage's document holds its approve back.
        this.refreshHeader();
        const sid = this.sid;
        m.setProperty("/artifact/busy", true);
        try {
            const artifact = await this.service().getArtifact(sid, summary.id);
            if (seq === this.artifactSeq && sid === this.sid) {
                // Model-written markdown: only through the sanitising renderer (docView / markdown), one row per block.
                const rendered = renderDocument(artifact.content);
                const commentable = !!this.s().getProperty("/artifact/doc/commentable");
                this.doc = { kind, version, blocks: splitBlocks(rendered.html, this.createId("docBlockHint"), commentable) };
                m.setProperty("/artifact/busy", false);
                this.renderDoc();
                // Its text is on screen (rendered with the header in the same pass): only now it lifts an older version's hold.
                this.doc.rendered = true;
                this.docOnScreen = { kind, version };
                this.refreshHeader();
            }
        } catch (e) {
            if (seq === this.artifactSeq && sid === this.sid) {
                m.setProperty("/artifact/busy", false);
                this.showError(e);
            }
        }
    }

    // --- Document view and comments (Task U9) ----------------------------------

    /** Versions, the approved pin, "based on" and whether comments are possible, for the document shown. */
    private refreshDocMeta(): void {
        const doc = this.doc;
        const detail = this.detail;
        if (!doc || !detail) {
            return;
        }
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        const pins = (detail.pins ?? {}) as Record<string, unknown>;
        const pin = typeof pins[doc.kind] === "number" ? pins[doc.kind] as number : undefined;
        const artifacts = detail.artifacts ?? [];
        const m = this.s();
        m.setProperty("/artifact/doc/versions", versionItems(artifacts, doc.kind, pin, t));
        m.setProperty("/artifact/doc/selected", String(doc.version));
        m.setProperty("/artifact/doc/basedOn",
            basedOnLink(artifacts.find((a) => a.kind === doc.kind && a.version === doc.version), t));
        m.setProperty("/artifact/doc/commentable", detail.type === "change" && COMMENTABLE.includes(doc.kind));
    }

    /** The rows of the document: each block with its marker and its comments. */
    private renderDoc(): void {
        const doc = this.doc;
        if (!doc) {
            return;
        }
        const groups = byParagraph(documentComments(this.comments, doc.kind, doc.version), doc.blocks.length);
        this.s().setProperty("/artifact/doc/blocks", doc.blocks.map((html, index) => ({
            index, html, ...this.blockComments(index, groups[index] ?? [])
        })));
        this.s().setProperty("/artifact/doc/others", this.otherRows());
    }

    private blockComments(index: number, list: Comment[]): {
        comments: (CommentRow & { hint: string })[]; markerText: string; markerLabel: string; blockLabel: string;
    } {
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        const total = this.doc?.blocks.length ?? 0;
        return {
            comments: list.map((c) => ({ ...commentRow(c, t), hint: blockHint(c, total, t) })),
            markerText: list.length ? String(list.length) : "",
            markerLabel: markerLabel(index, list, t),
            blockLabel: blockLabel(index, total, list, t)
        };
    }

    private otherRows(): Record<string, unknown>[] {
        const doc = this.doc;
        if (!doc) {
            return [];
        }
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        return otherVersionComments(this.comments, doc.kind, doc.version)
            .map((c) => ({ ...commentRow(c, t), anchorText: anchorText(c, t), kind: c.kind, version: c.version }));
    }

    /**
     * The comments changed: each block's marker and comments are set in
     * place (the paragraphs' HTML is not touched, so the focus and the
     * popover's anchor stay), then the open popover's list.
     */
    private renderDocComments(): void {
        const doc = this.doc;
        const m = this.s();
        const blocks = m.getProperty("/artifact/doc/blocks") as unknown[] | undefined;
        if (doc && blocks?.length === doc.blocks.length) {
            const groups = byParagraph(documentComments(this.comments, doc.kind, doc.version), doc.blocks.length);
            groups.forEach((list, index) => {
                const next = this.blockComments(index, list);
                m.setProperty(`/artifact/doc/blocks/${index}/comments`, next.comments);
                m.setProperty(`/artifact/doc/blocks/${index}/markerText`, next.markerText);
                m.setProperty(`/artifact/doc/blocks/${index}/markerLabel`, next.markerLabel);
                m.setProperty(`/artifact/doc/blocks/${index}/blockLabel`, next.blockLabel);
            });
            m.setProperty("/artifact/doc/others", this.otherRows());
            // The blocks' names follow their comments (the block HTML itself is not rendered again).
            this.byId("artifactContent")?.getDomRef()?.querySelectorAll<HTMLElement>(".ideDocBlock").forEach((el) => this.decorateBlock(el));
        }
        this.renderChangeComments();
        this.renderPopover();
    }

    /**
     * Every local change of the list goes through here: the sequence moves
     * on, so a comments read that left before the change cannot undo it.
     */
    private setComments(list: Comment[]): void {
        this.comments = list;
        this.commentsSeq++;
    }

    private async reloadComments(): Promise<void> {
        const sid = this.sid;
        if (!sid || this.detail?.type !== "change") {
            return;
        }
        const seq = ++this.commentsSeq;
        const list = await this.service().listComments(sid);
        // Only the newest read wins: an older answer arriving late never overwrites it.
        if (sid === this.sid && seq === this.commentsSeq) {
            this.setComments(list);
            this.renderDocComments();
        }
    }

    /** A run stored a document: the version list follows, and the column shows it when it shows that kind. */
    private async onArtifactStored(kind: ArtifactKind, version: number): Promise<void> {
        const sid = this.sid;
        // Whether this tab started the run, as the frame arrived (the run may end during the reload).
        const own = this.ownRun;
        await this.reloadDetail().catch(() => undefined);
        const query = this.query;
        if (sid !== this.sid || query.version === String(version) && query.kind === kind) {
            return;
        }
        if (kind === "report" && this.detail?.type === "diagnose") {
            if (own) {
                // The report this tab asked for: the document view shows it.
                this.keepFocus = query.view === "document";
                this.getRouter().navTo("session", { id: sid, "?query": { view: "document", kind, version: String(version) } });
                this.announce(this.text("docNewVersionShown", [this.kindText(kind), version]));
                return;
            }
            if (query.view !== "document" || query.kind !== kind) {
                // Watching a run started elsewhere: offered (the header's Report link), the view stays.
                this.announce(this.text("docVersionAvailable", [this.kindText(kind), version]));
                return;
            }
            const text = this.text("docNewerAvailable", [version]);
            this.s().setProperty("/artifact/doc/newer", { text, kind, version });
            this.announce(text);
            return;
        }
        if (query.view !== "document" || query.kind !== kind) {
            return;
        }
        if (this.pop) {
            // A comment is being written: its popover and draft stay; the new version is offered instead.
            const text = this.text("docNewerAvailable", [version]);
            this.s().setProperty("/artifact/doc/newer", { text, kind, version });
            this.announce(text);
            return;
        }
        // The reader's focus stays where it is; the switch is announced instead.
        this.keepFocus = true;
        this.getRouter().navTo("session", { id: sid, "?query": { view: "document", kind, version: String(version) } });
        this.announce(this.text("docNewVersionShown", [this.kindText(kind), version]));
    }

    public onDocVersionChange(event: UI5Event): void {
        const item = (event.getParameters() as { item?: { getKey(): string } }).item;
        const key = item?.getKey();
        const kind = this.doc?.kind ?? this.query.kind;
        if (key && kind && key !== this.query.version) {
            this.keepFocus = true;
            this.getRouter().navTo("session", { id: this.sid, "?query": { view: "document", kind, version: key } });
        }
    }

    public onDocNewerPress(): void {
        const newer = this.s().getProperty("/artifact/doc/newer") as { kind: ArtifactKind; version: number } | null;
        if (newer) {
            this.getRouter().navTo("session", {
                id: this.sid, "?query": { view: "document", kind: newer.kind, version: String(newer.version) }
            });
        }
    }

    public onBasedOnPress(): void {
        const based = this.s().getProperty("/artifact/doc/basedOn") as { kind: ArtifactKind; version: number } | null;
        if (based) {
            this.getRouter().navTo("session", {
                id: this.sid, "?query": { view: "document", kind: based.kind, version: String(based.version) }
            });
        }
    }

    public onOtherCommentPress(event: UI5Event): void {
        const row = (event.getSource() as Control).getBindingContext("s")?.getObject() as { kind?: string; version?: number } | undefined;
        if (row?.kind && row.version) {
            this.getRouter().navTo("session", {
                id: this.sid, "?query": { view: "document", kind: row.kind, version: String(row.version) }
            });
        }
    }

    /**
     * Enter or C on a focused block opens its comments; Up/Down/Home/End
     * move between blocks (roving tab stop). Only keys on the block itself:
     * a link inside a block stays a tab stop of its own and keeps its keys.
     * Shift+Arrow is left to the browser (text selection).
     */
    private onDocumentKeydown(e: KeyboardEvent): void {
        const target = e.target as HTMLElement | null;
        if (!target?.classList?.contains("ideDocBlock") || e.ctrlKey || e.metaKey || e.altKey
            || !this.s().getProperty("/artifact/doc/commentable")) {
            return;
        }
        const key = String(e.key ?? "").toLowerCase();
        const code = e.which || e.keyCode;
        const move = key === "arrowdown" || code === 40 ? 1 : key === "arrowup" || code === 38 ? -1
            : key === "home" || code === 36 ? -Infinity : key === "end" || code === 35 ? Infinity : 0;
        if (move) {
            if (e.shiftKey) {
                return;
            }
            e.preventDefault();
            const blocks = Array.from(this.byId("artifactContent")?.getDomRef()?.querySelectorAll<HTMLElement>(".ideDocBlock") ?? []);
            const at = blocks.indexOf(target);
            const next = move === -Infinity ? 0 : move === Infinity ? blocks.length - 1 : Math.min(Math.max(at + move, 0), blocks.length - 1);
            blocks[next]?.focus();
            return;
        }
        const enter = key === "enter" || code === 13;
        const c = key === "c" || (!key && code === 67) || (key.length !== 1 && code === 67);
        if (!enter && !c) {
            return;
        }
        const paragraph = Number(target.getAttribute("data-para"));
        if (Number.isInteger(paragraph) && paragraph >= 0) {
            e.preventDefault();
            void this.openCommentPopover(paragraph, "paragraph", target);
        }
    }

    /** Roving tabindex: the focused block is the document's one tab stop. */
    private onDocumentFocus(e: FocusEvent): void {
        const target = e.target as HTMLElement | null;
        if (target?.classList?.contains("ideDocBlock")) {
            this.setRovingBlock(Number(target.getAttribute("data-para")));
        }
    }

    private rovingBlock = 0;

    private setRovingBlock(index: number): void {
        this.rovingBlock = Number.isInteger(index) && index >= 0 ? index : 0;
        this.byId("artifactContent")?.getDomRef()?.querySelectorAll<HTMLElement>(".ideDocBlock[tabindex]").forEach((el) => {
            el.setAttribute("tabindex", Number(el.getAttribute("data-para")) === this.rovingBlock ? "0" : "-1");
        });
    }

    /** A block was (re)rendered from its HTML string: that block (only) gets its tab index and name back. */
    public onDocBlockRendered(event: UI5Event): void {
        const dom = (event.getSource() as Control).getDomRef() as HTMLElement | null;
        if (dom?.classList.contains("ideDocBlock")) {
            this.decorateBlock(dom);
        }
    }

    /** One block's roving tab index and accessible name (number, total, comments), from the model. */
    private decorateBlock(el: HTMLElement): void {
        if (!el.hasAttribute("tabindex")) {
            // Not commentable: not focusable, nothing to name.
            return;
        }
        const index = Number(el.getAttribute("data-para"));
        el.setAttribute("tabindex", index === this.rovingBlock ? "0" : "-1");
        const label = this.s().getProperty(`/artifact/doc/blocks/${index}/blockLabel`) as string | undefined;
        if (label) {
            el.setAttribute("aria-label", label);
        }
    }

    /** The document's blocks as a group named by the column title, with the arrow-key help. */
    private describeDocument(): void {
        const dom = this.byId("artifactContent")?.getDomRef();
        if (!dom) {
            return;
        }
        dom.setAttribute("role", "group");
        dom.setAttribute("aria-labelledby", this.createId("artifactTitle") ?? "artifactTitle");
        dom.setAttribute("aria-describedby", this.createId("docHint") ?? "docHint");
    }

    /** The markers of new rows leave the tab order (Enter or C on the block opens the same popover). */
    private tameMarkers(): void {
        (this.byId("artifactContent") as unknown as { getItems(): Control[] } | undefined)?.getItems().forEach((row) => {
            row.findAggregatedObjects(true, (o) => o.getId().includes("docMarker")).forEach((marker) => {
                if (this.tamedMarkers.has(marker)) {
                    return;
                }
                this.tamedMarkers.add(marker);
                const untab = (): void => { (marker as Control).getDomRef()?.setAttribute("tabindex", "-1"); };
                (marker as Control).addEventDelegate({ onAfterRendering: untab });
                untab();
            });
        });
    }

    public onDocMarkerPress(event: UI5Event): void {
        const source = event.getSource() as Control;
        const index = source.getBindingContext("s")?.getProperty("index") as number | undefined;
        if (typeof index === "number") {
            void this.openCommentPopover(index, source, source);
        }
    }

    /** The quote of a block: its text without the link host span, table cells apart. */
    private blockQuote(index: number): string {
        const el = this.paragraphDom(index);
        return el ? quoteOf(blockText(el)) : "";
    }

    private paragraphDom(index: number): HTMLElement | null {
        return this.byId("artifactContent")?.getDomRef()?.querySelector<HTMLElement>(`.ideDocBlock[data-para='${index}']`) ?? null;
    }

    private async openCommentPopover(
        paragraph: number, invoker: Control | "paragraph" | "row", anchor: Control | HTMLElement, file?: FileAnchor
    ): Promise<void> {
        const target: PopoverTarget = { paragraph, invoker, file };
        this.endPending();
        this.pendingPop = target;
        this.pendingAnchor = anchor;
        this.draftFocused = false;
        this.resetDraft();
        if (!this.popoverControl?.isOpen()) {
            // While the popover is open for another target its new title would re-render it, and its
            // re-rendering pulls the focus back into it just before it closes: beforeOpen renders it then.
            this.renderPopover();
        }
        // Until the popover is open, keys typed on the block or line go into its draft (keys typed after Enter).
        this.startBuffering(anchor instanceof HTMLElement ? anchor : (anchor.getFocusDomRef() ?? anchor.getDomRef()) as HTMLElement | null);
        let popover: Popover;
        try {
            this.commentPopover ??= this.loadFragment({ name: "com.agent.ide.fragment.CommentPopover" }) as Promise<Popover>;
            popover = await this.commentPopover;
        } catch (e) {
            // Not cached: the next attempt loads again. Nothing counts as open.
            this.commentPopover = undefined;
            if (this.pendingPop === target) {
                this.endPending();
                this.showError(e);
            }
            return;
        }
        this.popoverControl = popover;
        if (this.pendingPop !== target) {
            return;
        }
        if (this.popoverClosing) {
            // The previous popover is still closing and would ignore this open: its afterClose opens the
            // pending target (one pending target, the latest request wins). Keys typed meanwhile are buffered.
            // The closing popover no longer stands for its target: a Save on it must not store this draft there.
            this.queuedOpen = true;
            this.pop = undefined;
            return;
        }
        // Open for another opener: sap.m.Popover closes first and opens again after its afterClose.
        const reopening = popover.isOpen();
        this.pop = target;
        try {
            popover.openBy(anchor);
        } catch (e) {
            // Nothing opened: no key may be held back any more.
            this.pop = undefined;
            this.endPending();
            this.showError(e);
            return;
        }
        if (!popover.isOpen()) {
            // The open did not start now (a popover still closing opens later, or never): the page's keys
            // are its own again at once. The target stays, so an open that comes later still works.
            this.stopBuffering();
            return;
        }
        if (reopening && !this.popoverClosing) {
            // Pressed the opener of the open popover: sap.m.Popover does nothing (no close, no afterOpen),
            // so nothing is on its way; a pending open left here would reopen it after the next close.
            this.endPending();
            this.renderPopover();
            this.focusDraftEnd();
            return;
        }
        if (reopening) {
            // The draft on screen belongs to the popover that is closing: focusing it would leave the focus
            // on nothing once it closes, and the new one would close itself (autoclose). afterOpen focuses it.
            return;
        }
        this.focusDraftEnd();
    }

    /** Buffers keys typed on `from` until the popover is open, for at most BUFFER_MAX_MS. */
    private startBuffering(from: HTMLElement | null): void {
        this.stopBuffering();
        this.bufferFrom = from ?? undefined;
        document.addEventListener("keydown", this.onPendingKey, true);
        this.bufferTimer = setTimeout(() => this.stopBuffering(), BUFFER_MAX_MS);
    }

    private stopBuffering(): void {
        document.removeEventListener("keydown", this.onPendingKey, true);
        if (this.bufferTimer !== undefined) {
            clearTimeout(this.bufferTimer);
            this.bufferTimer = undefined;
        }
        this.bufferFrom = undefined;
    }

    /**
     * Whether a key typed on `target` belongs to the popover on its way: the
     * block, line or button that opens it, an element of no control (the
     * body, the popup area's focus trap), or the popover's own frame while
     * it animates in. Never
     * another control, and never the popover's buttons or links.
     */
    private bufferable(target: EventTarget | null): boolean {
        const el = target as HTMLElement | null;
        if (!el || !(el instanceof Element)) {
            return false;
        }
        if (el === this.bufferFrom || !UI5Element.closestTo(el)) {
            // The opener, or no control at all: the body, the popup area's focus trap while the popover animates in.
            return true;
        }
        const popDom = this.popoverControl?.getDomRef();
        return !!popDom && popDom.contains(el)
            && !el.closest("button, a, input, textarea, select, [role='button'], [role='link']");
    }

    /**
     * A key while the popover is on its way. Keys for the text area itself
     * pass; any other (on the block, the row, the popover while it animates
     * in) is taken into the draft, and the text area gets the focus as soon
     * as it can take it.
     */
    private pendingKey(e: KeyboardEvent): void {
        if (!this.pendingPop) {
            this.endPending();
            return;
        }
        const area = (this.byId("commentDraft") as TextArea | undefined)?.getFocusDomRef();
        if (area && e.target === area) {
            return;
        }
        if (e.key === "Process" || e.key === "Dead") {
            // A composition (IME, dead key) cannot be replayed into the draft: buffering ends, the text area takes over.
            this.stopBuffering();
            this.focusDraftEnd();
            return;
        }
        if (!this.bufferable(e.target)) {
            return;
        }
        if (this.bufferKey(e)) {
            // Taken: not also a command (N/P, Space, C, Enter) of the block or line it was typed on.
            e.stopPropagation();
            this.focusDraftEnd();
        }
    }

    private endPending(): void {
        this.pendingPop = undefined;
        this.pendingAnchor = undefined;
        this.queuedOpen = false;
        this.stopBuffering();
    }

    /** The draft's text area has the focus, the caret after what is already typed (once it is shown). */
    private focusDraftEnd(): void {
        const area = this.byId("commentDraft") as TextArea | undefined;
        const dom = area?.getFocusDomRef() as HTMLTextAreaElement | null | undefined;
        if (!dom || !dom.offsetParent) {
            return;
        }
        if (dom.ownerDocument.activeElement !== dom) {
            area!.focus();
        }
        if (typeof dom.setSelectionRange === "function") {
            const end = dom.value.length;
            dom.setSelectionRange(end, end);
        }
        this.draftFocused = true;
    }

    /**
     * Keys typed on the block or line while its popover is still on its way
     * (loading, opening): printable ones go into the draft, Backspace takes
     * one back, Enter is swallowed (it would open the popover again). True
     * when the key was taken.
     */
    private bufferKey(e: KeyboardEvent): boolean {
        // AltGr arrives as AltGraph, or as Ctrl+Alt on Windows: it types a character, it is no shortcut.
        const altGr = (typeof e.getModifierState === "function" && e.getModifierState("AltGraph"))
            || (e.ctrlKey && e.altKey && !e.metaKey);
        if (!this.pendingPop || ((e.ctrlKey || e.metaKey || e.altKey) && !altGr)) {
            return false;
        }
        const key = String(e.key ?? "");
        const draft = String(this.s().getProperty("/pop/draft") ?? "");
        let next: string | undefined;
        if (Array.from(key).length === 1) {
            next = draft + key;
        } else if (key === "Backspace") {
            next = Array.from(draft).slice(0, -1).join("");
        } else if (key !== "Enter") {
            return false;
        }
        e.preventDefault();
        if (next !== undefined && next.length <= COMMENT_MAX) {
            this.s().setProperty("/pop/draft", next);
            this.onCommentDraftChange();
        }
        return true;
    }

    /** The popover shows the target it opens for (a deferred open renders here, see openCommentPopover). */
    public onCommentPopoverBeforeOpen(): void {
        this.renderPopover();
    }

    /**
     * The popover is open: keys reach the text area itself from now on. Its
     * initialFocus put the caret at the start; unless this page already
     * placed it (and typing went on from there), it goes after what was typed.
     */
    public onCommentPopoverOpened(): void {
        if (!this.pop && this.pendingPop) {
            // A deferred open (sap.m.Popover opened from another opener closes first, then opens): the
            // close in between cleared `pop`. The popover is open for the pending target: Save needs it.
            this.pop = this.pendingPop;
        }
        this.endPending();
        const dom = (this.byId("commentDraft") as TextArea | undefined)?.getFocusDomRef();
        if (dom && (!this.draftFocused || dom.ownerDocument.activeElement !== dom)) {
            this.focusDraftEnd();
        }
    }

    private resetDraft(text = "", editing = false): void {
        const m = this.s();
        m.setProperty("/pop/draft", text);
        m.setProperty("/pop/draftLabel", this.text(editing ? "commentEditLabel" : "commentNewLabel"));
        m.setProperty("/pop/error", "");
        this.onCommentDraftChange();
    }

    /** The popover's title and the paragraph's comments with their actions. */
    private renderPopover(): void {
        // The target on its way wins: while it opens, `pop` may still be the one closing.
        const pop = this.pendingPop ?? this.pop;
        const doc = this.doc;
        // Busy belongs to a save of the target shown, never to one of a target the popover has left.
        this.s().setProperty("/pop/busy", !!pop && this.savingPops.has(pop));
        const t = (k: string, a?: (string | number)[]): string => this.text(k, a);
        if (pop?.file) {
            const f = pop.file;
            const list = commentsInRange(fileComments(this.comments, f.path, f.revision), f.start, f.end);
            this.s().setProperty("/pop/title", f.start === f.end
                ? this.text("commentPopoverTitleLine", [f.start]) : this.text("commentPopoverTitleLines", [f.start, f.end]));
            this.s().setProperty("/pop/comments", list.map((c) => commentRow(c, t)));
            return;
        }
        if (!pop || !doc) {
            return;
        }
        const list = byParagraph(documentComments(this.comments, doc.kind, doc.version), doc.blocks.length)[pop.paragraph] ?? [];
        this.s().setProperty("/pop/title", this.text("commentPopoverTitle", [pop.paragraph + 1]));
        this.s().setProperty("/pop/comments", list.map((c) => commentRow(c, t)));
    }

    /** Counter and Save follow every keystroke (the event's value: the binding may update after liveChange). */
    public onCommentDraftChange(event?: UI5Event): void {
        const typed = (event?.getParameters() as { value?: string } | undefined)?.value;
        const draft = typeof typed === "string" ? typed : String(this.s().getProperty("/pop/draft") ?? "");
        this.s().setProperty("/pop/counter", this.text("commentCounter", [draft.length, COMMENT_MAX]));
        this.s().setProperty("/pop/canSave", !!draft.trim() && draft.length <= COMMENT_MAX);
    }

    /** Save: a new comment on the paragraph, or the edited text of an open one; the popover closes. */
    public async onCommentSave(): Promise<void> {
        const pop = this.pop;
        const doc = this.doc;
        const body = String(this.s().getProperty("/pop/draft") ?? "").trim();
        if (!pop || (!doc && !pop.file) || !body || body.length > COMMENT_MAX || this.savingPops.has(pop)) {
            return;
        }
        const sid = this.sid;
        // Busy turns Save off under the focus: the focus would fall to the page, and the popover closes
        // itself when the focus leaves it (autoclose), hiding a refusal. It waits on the popover itself
        // (its root takes the focus: tabindex -1), as in changeComment.
        (this.popoverControl?.getDomRef() as HTMLElement | null | undefined)?.focus();
        this.savingPops.add(pop);
        this.s().setProperty("/pop/busy", true);
        let refused = false;
        try {
            if (pop.editingId) {
                const saved = await this.service().editComment(sid, pop.editingId, body);
                this.setComments(this.comments.map((c) => (c.id === saved.id ? saved : c)));
            } else if (pop.file) {
                const f = pop.file;
                const saved = await this.service().createComment(sid, {
                    anchor: "file", path: f.path, revision: f.revision, line_start: f.start, line_end: f.end, body,
                    quote: quoteOf(this.lineText(f.path, f.revision, f.start))
                });
                this.setComments([...this.comments, saved]);
            } else if (doc) {
                const saved = await this.service().createComment(sid, {
                    anchor: "document", kind: doc.kind, version: doc.version, paragraph: pop.paragraph, body,
                    quote: this.blockQuote(pop.paragraph)
                });
                this.setComments([...this.comments, saved]);
            }
            if (sid !== this.sid) {
                return;
            }
            this.renderDocComments();
            this.announce(this.text("commentSaved"));
            if (this.pop === pop) {
                // Only the popover of this target closes: one opened meanwhile for another target stays.
                (await this.commentPopover)?.close();
            }
            void this.reloadDetail().catch(() => undefined);
        } catch (e) {
            if (sid === this.sid && this.pop !== pop) {
                // The popover moved on to another target meanwhile: the refusal is not shown in it, and a
                // dialog would take the focus and close it: a toast says it.
                MessageToast.show(this.commentErrorText(e));
            } else if (sid === this.sid) {
                refused = true;
                this.s().setProperty("/pop/error", this.commentErrorText(e));
                if (e instanceof IdeError && (e.code === "comment_not_editable" || e.status === 404)) {
                    // The comment changed or went away on the server: show it as it is now.
                    await this.reloadComments().catch(() => undefined);
                    void this.reloadDetail().catch(() => undefined);
                }
            }
        } finally {
            this.savingPops.delete(pop);
            const shown = this.pendingPop ?? this.pop;
            this.s().setProperty("/pop/busy", !!shown && this.savingPops.has(shown));
        }
        if (refused && this.pop === pop) {
            // The draft is kept: the focus goes back into it, to fix it and save again.
            this.focusWhenReady(() => this.byId("commentDraft")?.getFocusDomRef() as HTMLElement | null);
        }
    }

    public async onCommentCancel(): Promise<void> {
        if (this.pop?.editingId) {
            // Cancel an edit: back to a new comment, the popover stays.
            this.pop.editingId = undefined;
            this.resetDraft();
            return;
        }
        (await this.commentPopover)?.close();
    }

    /** The popover closed (Esc, Cancel, Save, outside): the focus goes back to the paragraph or marker that opened it. */
    /** The popover starts closing: an open requested from now on waits for its afterClose. */
    public onCommentPopoverBeforeClose(): void {
        this.popoverClosing = true;
        this.closingPop = this.pop;
    }

    public onCommentPopoverClosed(): void {
        this.popoverClosing = false;
        // A queued open cleared `pop` while this popover closed: the focus still belongs to its invoker.
        const pop = this.pop ?? this.closingPop;
        this.closingPop = undefined;
        this.pop = undefined;
        if (this.pendingPop) {
            // An open is on its way. When it is a deferred one (sap.m.Popover opens again right after this
            // afterClose), its target stays for onCommentPopoverOpened and the focus stays where it goes;
            // only when no open follows, this close ends it as usual.
            setTimeout(() => {
                if (this.popoverControl?.isOpen()) {
                    return;
                }
                // An open requested while this popover was closing (sap.ui.core.Popup ignores an open unless
                // it is closed) is done now. Only the latest request is pending, so it wins. Nothing else reopens.
                const queued = this.queuedOpen ? this.pendingPop : undefined;
                if (queued && this.reopenPending(queued)) {
                    return;
                }
                this.endPending();
                this.focusInvoker(pop);
            }, 0);
            return;
        }
        this.endPending();
        this.focusInvoker(pop);
    }

    /** Opens the popover for `pending` by its recorded anchor; false when that cannot be done (any more). */
    private reopenPending(pending: PopoverTarget): boolean {
        const popover = this.popoverControl;
        const anchor = this.pendingAnchor;
        const shown = anchor instanceof HTMLElement ? anchor.isConnected : !!anchor?.getDomRef();
        if (!popover || !anchor || !shown) {
            return false;
        }
        this.pop = pending;
        try {
            popover.openBy(anchor);
        } catch {
            this.pop = undefined;
            return false;
        }
        if (!popover.isOpen()) {
            this.pop = undefined;
            return false;
        }
        return true;
    }

    /** The focus goes back to the block, line or marker that opened the popover. */
    private focusInvoker(pop: PopoverTarget | undefined): void {
        if (!pop) {
            return;
        }
        if (pop.invoker === "row" && pop.file) {
            const f = pop.file;
            this.focusWhenReady(() => {
                const root = this.diffRoot(f.path);
                const row = root ? rowOfLine(root, f.start) : null;
                if (root && row) {
                    setStop(root, row);
                }
                return row;
            });
            return;
        }
        const invoker = pop.invoker;
        this.focusWhenReady(() => (invoker === "paragraph"
            ? this.paragraphDom(pop.paragraph)
            : invoker === "row" ? null : (invoker.getDomRef() as HTMLElement | null)));
    }

    private commentFromEvent(event: UI5Event): CommentRow | undefined {
        return (event.getSource() as Control).getBindingContext("s")?.getObject() as CommentRow | undefined;
    }

    public onCommentEdit(event: UI5Event): void {
        const row = this.commentFromEvent(event);
        if (row && this.pop) {
            this.pop.editingId = row.id;
            this.resetDraft(row.body, true);
            (this.byId("commentDraft") as TextArea | undefined)?.focus();
        }
    }

    public onCommentDelete(event: UI5Event): void {
        const row = this.commentFromEvent(event);
        if (row) {
            void this.changeComment(row.id, () => this.service().deleteComment(this.sid, row.id).then(() => null));
        }
    }

    public onCommentReopen(event: UI5Event): void {
        const row = this.commentFromEvent(event);
        if (row) {
            void this.changeComment(row.id, () => this.service().setCommentState(this.sid, row.id, "open"));
        }
    }

    public onCommentDismiss(event: UI5Event): void {
        const row = this.commentFromEvent(event);
        if (row) {
            void this.changeComment(row.id, () => this.service().setCommentState(this.sid, row.id, "dismissed"));
        }
    }

    /**
     * Applies one change to a comment (`null` = deleted), then the header
     * counts and the gate follow. The focus stays on the comment acted on
     * (on the text area when it is gone).
     */
    private async changeComment(cid: string, call: () => Promise<Comment | null>): Promise<void> {
        const sid = this.sid;
        if (this.s().getProperty("/pop/busy")) {
            return;
        }
        // Busy re-renders the row, and the answer may remove it (deleted, or gone on a relist): the
        // focus on it would fall to the page, and the popover closes itself when the focus leaves it
        // (autoclose). Until focusComment puts it back, it waits on the popover itself (fix round U11 #14).
        (this.popoverControl?.getDomRef() as HTMLElement | null | undefined)?.focus();
        this.s().setProperty("/pop/busy", true);
        this.s().setProperty("/pop/error", "");
        try {
            const changed = await call();
            if (sid !== this.sid) {
                return;
            }
            this.setComments(changed
                ? this.comments.map((c) => (c.id === cid ? changed : c))
                : this.comments.filter((c) => c.id !== cid));
            if (!changed && this.pop?.editingId === cid) {
                this.pop.editingId = undefined;
                this.resetDraft();
            }
            this.renderDocComments();
            this.announce(changed ? stateText(changed.state, (k, a) => this.text(k, a)) : this.text("commentDeleted"));
            void this.reloadDetail().catch(() => undefined);
        } catch (e) {
            if (sid !== this.sid) {
                return;
            }
            this.s().setProperty("/pop/error", this.commentErrorText(e));
            if (e instanceof IdeError && (e.code === "invalid_transition" || e.code === "comment_not_editable" || e.status === 404)) {
                await this.reloadComments().catch(() => undefined);
                void this.reloadDetail().catch(() => undefined);
            }
        } finally {
            this.s().setProperty("/pop/busy", false);
            this.focusComment(cid);
        }
    }

    private focusComment(cid: string): void {
        this.focusWhenReady(() => {
            const rows = this.s().getProperty("/pop/comments") as CommentRow[] | undefined;
            return rows?.some((r) => r.id === cid) ? this.commentRowDom(cid)
                : this.byId("commentDraft")?.getFocusDomRef() as HTMLElement | null;
        });
    }

    /** The popover's list row of comment `cid` (focusable), or null when it is not listed. */
    private commentRowDom(cid: string): HTMLElement | null {
        const rows = this.s().getProperty("/pop/comments") as CommentRow[] | undefined;
        const index = rows?.findIndex((r) => r.id === cid) ?? -1;
        const item = index < 0 ? undefined : (this.byId("commentPopoverList") as List | undefined)?.getItems()[index];
        return (item?.getDomRef() as HTMLElement | null) ?? null;
    }

    /** Focuses what `find` returns once it is rendered and visible (the popover and lists render asynchronously). */
    private focusWhenReady(find: () => HTMLElement | null | undefined): void {
        let tries = 0;
        const attempt = (): void => {
            const el = find();
            if (el?.isConnected && el.offsetParent) {
                el.focus();
            } else if (++tries < 30) {
                requestAnimationFrame(attempt);
            }
        };
        requestAnimationFrame(attempt);
    }

    private commentErrorText(e: unknown): string {
        if (e instanceof IdeError) {
            if (e.status === 422) {
                return this.text("commentSaveFailed");
            }
            if (e.code === "comment_not_editable") {
                return this.text("commentNotEditable");
            }
            if (e.code === "invalid_transition") {
                return this.text("commentInvalidTransition");
            }
            if (e.code === "comments_not_allowed") {
                return this.text("commentsNotAllowed");
            }
        }
        return gateErrorText(e, (k, a) => this.text(k, a));
    }

    /** Leaving the document (another query, another session): no popover or dialog stays open. */
    private closeCommentUi(): void {
        this.pop = undefined;
        this.closingPop = undefined;
        this.endPending();
        void this.commentPopover?.then((p) => { if (p.isOpen()) { p.close(); } });
        void this.requestChangesDialog?.then((d) => { if (d.isOpen()) { d.close(); } });
    }

    // --- Changes view and source view (Task U10) ---------------------------------

    private resetChanges(): void {
        this.changes = [];
        this.stopCursor = -1;
        this.selection = undefined;
        this.sourceLine = 0;
    }

    private translator(): (k: string, a?: (string | number)[]) => string {
        return (k, a) => this.text(k, a);
    }

    /** The revision a path is shown at: the user's pick while no newer revision came, else the latest. */
    private shownRevision(f: FileSummary): number {
        const pick = this.picked.get(f.path);
        if (pick && pick.latest === f.revision && pick.revision >= 1 && pick.revision <= f.revision) {
            return pick.revision;
        }
        this.picked.delete(f.path);
        return f.revision;
    }

    /** Reads one object at `revision`: its file, its revisions, and the diff rows against its SAP base. */
    private async loadChangeObject(summary: FileSummary, revision: number): Promise<ChangeObject> {
        const sid = this.sid;
        const [detail, revisions] = await Promise.all([
            this.service().getFile(sid, summary.path, revision),
            this.service().listRevisions(sid, summary.path).catch((): FileRevision[] => []),
            ensureDiff()
        ]);
        const source = String(detail.proposed_source ?? "");
        const listed = revisions.find((r) => r.revision === revision);
        const syntaxStatus = listed ? listed.syntax_status : detail.syntax_status ?? null;
        const { rows, mode } = compareObject(detail.origin_source ?? null, source);
        return {
            path: summary.path, summary, detail, revision, latest: summary.revision, revisions, rows, mode,
            syntaxStatus, syntaxItems: detail.syntax ?? [], lint: detail.lint ?? [], lintedHere: false, source
        };
    }

    /** A card for an object that could not be read: its name, an error and Retry. */
    private failedChange(summary: FileSummary, revision: number): ChangeObject {
        return {
            path: summary.path, summary, detail: {} as FileDetail, revision, latest: summary.revision, revisions: [],
            rows: [], mode: "full", syntaxStatus: null, syntaxItems: [], lint: [], lintedHere: false, source: "", failed: true
        };
    }

    /** The changes column: every proposed object, then the cards; a late answer for an earlier query is dropped. */
    private loadChanges(seq: number): Promise<void> {
        const loading = this.doLoadChanges(seq);
        this.changesLoading = loading;
        const clear = (): void => {
            if (this.changesLoading === loading) {
                this.changesLoading = null;
            }
        };
        loading.then(clear, clear);
        return loading;
    }

    private async doLoadChanges(seq: number): Promise<void> {
        const sid = this.sid;
        const files = proposedObjects(this.detail?.files);
        const m = this.s();
        m.setProperty("/artifact/busy", true);
        try {
            // At most a few reads at once; one object that fails is a card with Retry, not a failed view.
            const settled = await settleLimited(files, CHANGES_CONCURRENCY, (f) => this.loadChangeObject(f, this.shownRevision(f)));
            if (seq !== this.artifactSeq || sid !== this.sid) {
                return;
            }
            const loaded = settled.map((r, i) => (r.status === "fulfilled" ? r.value : this.failedChange(files[i], this.shownRevision(files[i]))));
            const sel = this.selection;
            const before = sel ? this.changes[sel.index] : undefined;
            this.changes = loaded;
            this.stopCursor = -1;
            this.selection = undefined;
            if (sel && before) {
                // The same revision still shown (maybe at another place): the selection stays; else it goes, said.
                const at = loaded.findIndex((o) => o.path === before.path);
                if (at >= 0 && loaded[at].revision === before.revision) {
                    this.selection = { ...sel, index: at };
                } else {
                    this.announce(this.text("changesSelectionCleared",
                        [this.objectName(at >= 0 ? loaded[at] : before), at >= 0 ? loaded[at].revision : before.revision]));
                }
            }
            this.renderChanges();
        } catch (e) {
            if (seq === this.artifactSeq && sid === this.sid) {
                this.showError(e);
            }
        } finally {
            if (seq === this.artifactSeq && sid === this.sid) {
                m.setProperty("/artifact/busy", false);
            }
        }
    }

    /**
     * After `file` events and at the end of a run: objects whose revision,
     * syntax or base changed are read again (a pick of an older revision is
     * kept unless the object got a newer one); a new or removed object reads
     * the whole list. Serialised, so overlapping events do not interleave.
     */
    private syncChanges(): Promise<void> {
        this.changesSync = this.changesSync.then(() => this.doSyncChanges()).catch((e) => this.showError(e));
        return this.changesSync;
    }

    private async doSyncChanges(): Promise<void> {
        this.changesSyncing = true;
        try {
            await this.syncChangesNow();
        } finally {
            this.changesSyncing = false;
        }
    }

    private async syncChangesNow(): Promise<void> {
        if (this.changesLoading && this.query.view === "changes") {
            // The first read is still running: this event is compared with what it brings, not dropped.
            await this.changesLoading.catch(() => undefined);
        }
        if (this.query.view !== "changes" || !this.detail || !this.s().getProperty("/artifact/changes/loaded")) {
            return;
        }
        const seq = this.artifactSeq;
        const sid = this.sid;
        const files = proposedObjects(this.detail.files);
        const same = files.length === this.changes.length && files.every((f, i) => f.path === this.changes[i].path);
        if (!same) {
            await this.loadChanges(seq);
            return;
        }
        const stale = files.map((f, i) => ({ f, i, o: this.changes[i] })).filter(({ f, o }) => o.failed || f.revision !== o.latest
            || f.syntax_status !== o.summary.syntax_status || f.base_status !== o.summary.base_status);
        for (const { f, i } of stale) {
            const next = await this.loadChangeObject(f, this.shownRevision(f)).catch(() => this.failedChange(f, this.shownRevision(f)));
            if (seq !== this.artifactSeq || sid !== this.sid) {
                return;
            }
            this.replaceChange(i, next);
        }
    }

    /** Retry on a card that could not be read. */
    public async onChangesRetry(event: UI5Event): Promise<void> {
        const hit = this.objectOf(event);
        if (!hit) {
            return;
        }
        const { index, o } = hit;
        const seq = this.artifactSeq;
        const sid = this.sid;
        this.s().setProperty(`/artifact/changes/objects/${index}/busy`, true);
        const next = await this.loadChangeObject(o.summary, o.revision).catch(() => this.failedChange(o.summary, o.revision));
        if (seq === this.artifactSeq && sid === this.sid && this.changes[index] === o) {
            this.replaceChange(index, next);
            if (next.failed) {
                this.announce(this.text("changesLoadFailed", [this.objectName(next)]));
            }
        }
    }

    /**
     * Replaces one card (a revision switch, a new revision, a new check
     * result): its diff renders again. A line selection on it stays while the
     * same revision is shown; another revision clears it, and that is said.
     */
    private replaceChange(index: number, next: ChangeObject, picked = false): void {
        const prev = this.changes[index];
        this.changes[index] = next;
        if (!prev || prev.revision !== next.revision || prev.source !== next.source || !!prev.failed !== !!next.failed) {
            // Other content on screen (not a syntax or lint update): Approve changes waits until it has been seen.
            // A card the user switched says nothing; one that changed under the user says why Approve waits.
            this.explainSettle = !picked;
            this.startSettle();
        }
        if (this.selection?.index === index && prev?.revision !== next.revision) {
            this.selection = undefined;
            this.announce(this.text("changesSelectionCleared", [this.objectName(next), next.revision]));
        }
        this.stopCursor = -1;
        this.s().setProperty(`/artifact/changes/objects/${index}`, this.objectRow(next, index));
        this.renderChangesSummary();
        this.decorate(index);
        this.redecorateSoon(index);
        // An older revision on screen holds Approve changes back (G1).
        this.refreshHeader();
        if (!this.changesSyncing && this.detail && this.cardsStale(this.detail)) {
            // Backstop: a card read for an older state of the session holds Approve on "loading"; catch up.
            void this.syncChanges();
        }
    }

    private renderChanges(): void {
        const m = this.s();
        m.setProperty("/artifact/changes/objects", this.changes.map((o, i) => this.objectRow(o, i)));
        m.setProperty("/artifact/changes/loaded", true);
        // New cards on screen: Approve changes waits until they have been seen (APPROVE_SETTLE_MS).
        this.startSettle();
        this.renderChangesSummary();
        // The cards are on screen: in propose the primary action approves them.
        this.refreshHeader();
    }

    private renderChangesSummary(): void {
        let added = 0;
        let removed = 0;
        this.changes.forEach((o) => {
            const c = changeCounts(o.rows);
            added += c.added;
            removed += c.removed;
        });
        const n = this.changes.length;
        this.s().setProperty("/artifact/changes/summary", n === 1
            ? this.text("changesSummaryOne", [added, removed]) : this.text("changesSummary", [n, added, removed]));
        this.s().setProperty("/artifact/changes/stops", this.allStops().length);
    }

    private static fileName(path: string): string {
        return path.slice(path.lastIndexOf("/") + 1);
    }

    /** The object type, plus the include for a class include ("CLAS testclasses"): two cards may share a name. */
    private static typeText(o: ChangeObject): string {
        const parts = Session.fileName(o.path).split(".");
        const include = parts.length > 3 ? parts.slice(2, -1).join(".") : "";
        return [o.summary.object_type ?? "", include].filter(Boolean).join(" ");
    }

    private objectName(o: ChangeObject): string {
        return o.summary.object_name || Session.fileName(o.path);
    }

    /** What one card binds: states, actions, the diff HTML (escaped by renderUnifiedHtml) and its comments. */
    private objectRow(o: ChangeObject, index: number): Record<string, unknown> {
        const t = this.translator();
        const name = this.objectName(o);
        const isObject = !!o.summary.object_type;
        const syntax = syntaxInfo(o.syntaxStatus, o.syntaxItems, t);
        const counts = changeCounts(o.rows);
        const base = baseInfo(o.summary, o.detail.origin_version, t);
        const failed = !!o.failed;
        // Lint runs on the latest revision: on an older one it says so and cannot be run.
        const older = o.revision < o.latest;
        // ADT opens the object as it is in SAP: not for an object SAP does not have, nor for a note.
        // A class include opens that include, at the line of its own diff (no link when ADT has no URI for it).
        const href = isObject && !base.isNew && !failed
            ? adtUri(this.detail?.target ?? "", o.summary.object_type ?? "", o.summary.object_name ?? "",
                baseLineOfFirstChange(o.rows) ?? undefined, classIncludeOf(o.path)) ?? "" : "";
        return {
            index, path: o.path, name, isObject,
            typeText: Session.typeText(o),
            base: { text: base.text, state: base.state, icon: base.icon },
            syntax: { text: syntax.text, state: syntax.state, icon: syntax.icon },
            syntaxMessages: syntax.messages.map((text) => ({ text })),
            lint: lintInfo(o.lint, o.lintedHere, t, older),
            lintOlder: older,
            lintTooltip: older ? this.text("changesLintLatestOnly") : "",
            failed,
            failedText: failed ? this.text("changesLoadFailed", [name]) : "",
            countsText: this.text("changesCounts", [counts.added, counts.removed]),
            countsLabel: this.text("changesCountsLabel", [counts.added, counts.removed]),
            revisions: revisionItems(o.revisions, o.latest, t),
            revisionKey: String(o.revision),
            revisionLabel: this.text("changesRevisionLabel", [name]),
            adtHref: href,
            adtTooltip: href ? this.text("changesOpenInAdtTooltip", [name, this.detail?.target ?? ""]) : "",
            html: failed ? "" : this.diffHtml(o, index),
            tooLarge: !failed && o.mode === "none",
            hunksOnly: !failed && o.mode === "hunks",
            hunksNote: !failed && o.mode === "hunks" ? this.text("changesHunksNote", [DIFF_CONTEXT]) : "",
            busy: false, syntaxBusy: false, lintBusy: false,
            selectionText: this.selectionText(index),
            ...this.objectComments(o)
        };
    }

    private diffHtml(o: ChangeObject, index: number): string {
        const name = this.objectName(o);
        return renderUnifiedHtml(o.rows, {
            path: o.path,
            revision: o.revision,
            // A large object: its hunks only, said on the card; "Show the source" has the full text.
            hunksOnly: o.mode === "hunks",
            idPrefix: this.createId(`changesDiff${index}r${o.revision}`),
            // Only a finished check marks lines: pending vouches for nothing, nor does unavailable.
            syntax: diffSyntax(o.syntaxStatus, o.syntaxItems),
            labels: {
                region: this.text("changesRegion", [name, o.revision]),
                markHeader: this.text("changesHeadMark"),
                oldHeader: this.text("changesHeadOld"),
                newHeader: this.text("changesHeadNew"),
                codeHeader: this.text("changesHeadCode"),
                added: (n) => `${this.text("diffAdded")} ${n}`,
                removed: (n) => `${this.text("diffRemoved")} ${n}`,
                sameLine: (n) => this.text("changesSameLine", [n]),
                unchanged: (n) => this.text("diffUnchanged", [n]),
                tooLarge: this.text("changesNotCompared", [name, CHANGES_LIMITS.hunksMaxLines]),
                hunkHeader: (h: Hunk) => this.text("changesHunkHeader", [Session.lineRange(h.newStart, h.newCount),
                    Session.lineRange(h.oldStart, h.oldCount)]),
                syntaxError: (msg) => this.text("changesSyntaxErrorDesc", [msg]),
                syntaxWarning: (msg) => this.text("changesSyntaxWarningDesc", [msg])
            }
        });
    }

    /** "12–18", "12", or "–" for a side without lines. */
    private static lineRange(start: number, count: number): string {
        return count <= 0 ? "\u2013" : count === 1 ? String(start) : `${start}\u2013${start + count - 1}`;
    }

    /** The revision's comments (cards under the diff) and those of other revisions. */
    private objectComments(o: ChangeObject): { comments: Record<string, unknown>[]; others: Record<string, unknown>[] } {
        const t = this.translator();
        return {
            comments: fileComments(this.comments, o.path, o.revision)
                .map((c) => ({ ...commentRow(c, t), anchorText: anchorText(c, t), line: c.line_start })),
            others: otherRevisionComments(this.comments, o.path, o.revision)
                .map((c) => ({ ...commentRow(c, t), anchorText: anchorText(c, t), revision: c.revision }))
        };
    }

    /** The markers of one diff: one per commented first line, named with its count and states. */
    private markersOf(o: ChangeObject): { line: number; text: string; label: string }[] {
        const t = this.translator();
        return Array.from(lineMarkers(fileComments(this.comments, o.path, o.revision)).entries()).map(([line, list]) => {
            const open = list.filter((c) => c.state === "open").length;
            return {
                line,
                text: String(list.length),
                label: list.length === 1
                    ? this.text("changesMarkerOne", [line, stateText(list[0].state, t)])
                    : this.text("changesMarkerMany", [line, list.length, open]),
                comments: list.length === 1
                    ? this.text("changesRowCommentsOne", [stateText(list[0].state, t)])
                    : this.text("changesRowCommentsMany", [list.length, open])
            };
        });
    }

    /** Comments changed: each card's lists and markers in place (no diff re-render, the focus stays). */
    private renderChangeComments(): void {
        const m = this.s();
        const rows = m.getProperty("/artifact/changes/objects") as unknown[] | undefined;
        if (this.query.view !== "changes" || rows?.length !== this.changes.length) {
            return;
        }
        this.changes.forEach((o, i) => {
            const next = this.objectComments(o);
            m.setProperty(`/artifact/changes/objects/${i}/comments`, next.comments);
            m.setProperty(`/artifact/changes/objects/${i}/others`, next.others);
            this.decorate(i);
        });
    }

    /** The rendered diff of `path` (matched by attribute value, never by a selector built from it). */
    private diffRoot(path: string): HTMLElement | null {
        const host = this.byId("changesView")?.getDomRef();
        return Array.from(host?.querySelectorAll<HTMLElement>(".ideUnified") ?? [])
            .find((el) => el.getAttribute("data-path") === path) ?? null;
    }

    /** Selection, markers and the roving stop on one diff. */
    private decorate(index: number): void {
        const o = this.changes[index];
        const root = o ? this.diffRoot(o.path) : null;
        if (!o || !root) {
            return;
        }
        const sel = this.selection?.index === index ? this.selection : undefined;
        const rows = o.rows ?? [];
        const totalNew = rows.filter((r) => r.newNo !== null).length;
        decorateDiff(root, {
            selected: sel ? [sel.start, sel.end] : null,
            markers: this.markersOf(o),
            // Each line by name: kind, number, total, its comments, then the code.
            rowLabel: (r) => {
                const head = r.kind === "del" ? this.text("changesRowRemoved", [r.line])
                    : this.text(r.kind === "add" ? "changesRowAdded" : "changesRowSame", [r.line, totalNew]);
                return r.marker?.comments
                    ? this.text("changesRowLabelComments", [head, r.marker.comments, r.code])
                    : this.text("changesRowLabel", [head, r.code]);
            },
            rowHint: this.createId("changesRowHint")
        });
        root.setAttribute("aria-describedby", this.createId("changesHint") ?? "changesHint");
        root.setAttribute("data-decorated", "true");
    }

    /**
     * A card's diff HTML was replaced: core:HTML may swap its DOM in place
     * without an afterRendering event, so the fresh (undecorated) root is
     * looked for over the next frames and decorated once it is there.
     */
    private redecorateSoon(index: number): void {
        const path = this.changes[index]?.path;
        let tries = 0;
        const attempt = (): void => {
            if (this.changes[index]?.path !== path) {
                return;
            }
            const root = path ? this.diffRoot(path) : null;
            if (root && !root.hasAttribute("data-decorated")) {
                this.decorate(index);
            } else if (++tries < 30) {
                requestAnimationFrame(attempt);
            }
        };
        requestAnimationFrame(attempt);
    }

    /** A diff was (re)rendered from its HTML: it gets its decoration back. */
    public onChangesDiffRendered(event: UI5Event): void {
        const index = (event.getSource() as Control).getBindingContext("s")?.getProperty("index") as number | undefined;
        if (typeof index === "number") {
            this.decorate(index);
        }
    }

    private selectionText(index: number): string {
        const sel = this.selection;
        if (!sel || sel.index !== index) {
            return "";
        }
        return sel.start === sel.end
            ? this.text("changesCommentLine", [sel.start]) : this.text("changesCommentLines", [sel.start, sel.end]);
    }

    /** Selects lines of one diff (`extend`: from the selection's anchor), then decorates and announces. */
    private select(index: number, line: number, extend: boolean): void {
        if (this.detail?.type !== "change") {
            // Lines are selected to comment on them: a diagnose session has no comments.
            return;
        }
        const prev = this.selection;
        let anchor = extend && prev?.index === index ? prev.anchor : line;
        const o = this.changes[index];
        if (o && !rangeShown(o.rows, o.mode, ...selectionRange(anchor, line))) {
            // Hunks only: a selection never spans lines that are not on screen; it starts again here.
            anchor = line;
        }
        const [start, end] = selectionRange(anchor, line);
        this.selection = { index, anchor, start, end };
        if (prev && prev.index !== index) {
            this.s().setProperty(`/artifact/changes/objects/${prev.index}/selectionText`, "");
            this.decorate(prev.index);
        }
        this.s().setProperty(`/artifact/changes/objects/${index}/selectionText`, this.selectionText(index));
        this.decorate(index);
        this.announce(start === end ? this.text("changesSelectedLine", [start]) : this.text("changesSelectedLines", [start, end]));
    }

    private indexOfRoot(root: Element | null): number {
        const path = root?.getAttribute("data-path");
        return path === null || path === undefined ? -1 : this.changes.findIndex((o) => o.path === path);
    }

    /** N / P anywhere in the view; in a diff the arrows, Home, End, Space (Shift extends), Enter or C. */
    private onChangesKeydown(e: KeyboardEvent): void {
        const target = e.target as HTMLElement | null;
        if (!target || e.ctrlKey || e.metaKey || e.altKey
            || target.closest("input, textarea, select, [contenteditable='true'], [role='combobox'], [role='listbox']")) {
            return;
        }
        const key = String(e.key ?? "").toLowerCase();
        const code = e.which || e.keyCode;
        const is = (name: string, keyCode: number): boolean => key === name || ((key.length !== 1 || !key) && code === keyCode);
        if (is("n", 78) || is("p", 80)) {
            e.preventDefault();
            this.goToStop(is("n", 78) ? 1 : -1);
            return;
        }
        const root = target.closest<HTMLElement>(".ideUnified");
        const index = this.indexOfRoot(root);
        if (!root || index < 0) {
            return;
        }
        const here = target === root ? navigable(root)[0] : (rowFor(target) ?? target.closest<HTMLElement>("summary"));
        if (!here) {
            return;
        }
        const move = is("arrowdown", 40) || key === "down" ? 1 : is("arrowup", 38) || key === "up" ? -1
            : is("home", 36) ? -Infinity : is("end", 35) ? Infinity : 0;
        if (move) {
            if (e.shiftKey) {
                // Shift+Arrow is the browser's (text selection); Shift+Space extends a line selection.
                return;
            }
            e.preventDefault();
            const next = target === root ? here : step(root, here, move);
            setStop(root, next);
            next.focus();
            return;
        }
        if (here.tagName === "SUMMARY") {
            // Enter / Space toggle the fold natively.
            return;
        }
        const line = newLineOf(here);
        if (key === " " || key === "spacebar" || code === 32) {
            e.preventDefault();
            if (line === null) {
                this.announce(this.text("changesRemovedLine"));
            } else {
                this.select(index, line, e.shiftKey);
            }
            return;
        }
        if (is("enter", 13) || is("c", 67)) {
            e.preventDefault();
            const sel = this.selection;
            if (sel?.index === index && line !== null && line >= sel.start && line <= sel.end) {
                void this.openLinePopover(index, sel.start, sel.end, "row", here);
            } else if (line !== null) {
                this.select(index, line, false);
                void this.openLinePopover(index, line, line, "row", here);
            } else {
                this.announce(this.text("changesRemovedLine"));
            }
        }
    }

    /** A click on a line selects it (Shift extends); a click on a marker opens that line's comments. */
    private onChangesClick(e: MouseEvent): void {
        const target = e.target as HTMLElement | null;
        const root = target?.closest<HTMLElement>(".ideUnified") ?? null;
        const index = this.indexOfRoot(root);
        if (!target || !root || index < 0) {
            return;
        }
        const mark = target.closest<HTMLElement>(".ideDiffCommentMark");
        const row = rowFor(target);
        if (mark && row) {
            const line = Number(mark.getAttribute("data-line"));
            const list = lineMarkers(fileComments(this.comments, this.changes[index].path, this.changes[index].revision)).get(line) ?? [];
            const end = Math.max(line, ...list.map((c) => c.line_end ?? line));
            setStop(root, row);
            void this.openLinePopover(index, line, end, "row", row);
            return;
        }
        const line = newLineOf(row);
        if (row && line !== null) {
            setStop(root, row);
            row.focus();
            this.select(index, line, e.shiftKey);
        }
    }

    /** The roving stop follows the focus; reaching a hunk's first row moves the change cursor there. */
    private onChangesFocus(e: FocusEvent): void {
        const target = e.target as HTMLElement | null;
        const root = target?.closest<HTMLElement>(".ideUnified") ?? null;
        if (!target || !root || target === root || !(target.matches("tr[data-line]") || target.tagName === "SUMMARY")) {
            return;
        }
        setStop(root, target);
        const stop = target.getAttribute("data-stop");
        const index = this.indexOfRoot(root);
        if (stop !== null && index >= 0) {
            const at = this.allStops().findIndex((s) => s.index === index && String(s.n) === stop);
            if (at >= 0) {
                this.stopCursor = at;
            }
        }
    }

    private allStops(): { index: number; n: number; row: number }[] {
        const list: { index: number; n: number; row: number }[] = [];
        this.changes.forEach((o, index) => {
            changeStops(o.rows ?? []).forEach((row, n) => list.push({ index, n, row }));
        });
        return list;
    }

    /** Next / previous change across all diffs (no wrap): the hunk's first row takes the focus and is announced. */
    private goToStop(dir: 1 | -1): void {
        const stops = this.allStops();
        const at = stepStop(stops.length, this.stopCursor, dir);
        if (at < 0) {
            this.announce(this.text("changesNoStops"));
            return;
        }
        this.stopCursor = at;
        const stop = stops[at];
        const o = this.changes[stop.index];
        const root = this.diffRoot(o.path);
        const row = root?.querySelector<HTMLElement>(`tr[data-stop='${stop.n}']`);
        if (!root || !row) {
            return;
        }
        setStop(root, row);
        row.focus();
        // The revision's line of the hunk (its first added line); the SAP line for a pure removal.
        const rows = o.rows ?? [];
        let line = rows[stop.row]?.oldNo ?? 0;
        for (let i = stop.row; i < rows.length && rows[i].kind !== "same"; i++) {
            if (rows[i].newNo !== null) {
                line = rows[i].newNo as number;
                break;
            }
        }
        this.announce(this.text("changesStopAnnounce", [at + 1, stops.length, this.objectName(o), line]));
    }

    public onNextChange(): void {
        this.goToStop(1);
    }

    public onPreviousChange(): void {
        this.goToStop(-1);
    }

    private lineText(path: string, revision: number, line: number): string {
        const o = this.changes.find((c) => c.path === path && c.revision === revision);
        return o ? (o.source.replace(/\r\n?/g, "\n").split("\n")[line - 1] ?? "") : "";
    }

    private async openLinePopover(
        index: number, start: number, end: number, invoker: Control | "row", anchor: Control | HTMLElement
    ): Promise<void> {
        const o = this.changes[index];
        if (!o || this.detail?.type !== "change") {
            return;
        }
        await this.openCommentPopover(-1, invoker, anchor, { index, path: o.path, revision: o.revision, start, end });
    }

    /** "Comment on lines a-b": the popover for the selection, anchored to the button. */
    public onChangesCommentPress(event: UI5Event): void {
        const source = event.getSource() as Control;
        const sel = this.selection;
        const index = source.getBindingContext("s")?.getProperty("index") as number | undefined;
        if (sel && sel.index === index) {
            void this.openLinePopover(index, sel.start, sel.end, source, source);
        }
    }

    /** A comment's anchor under the diff: its lines are selected and the first one takes the focus. */
    public onChangesCommentAnchorPress(event: UI5Event): void {
        const ctx = (event.getSource() as Control).getBindingContext("s");
        const id = ctx?.getProperty("id") as string | undefined;
        const index = Number(/\/objects\/(\d+)\//.exec(ctx?.getPath() ?? "")?.[1]);
        const c = this.comments.find((x) => x.id === id);
        const o = this.changes[index];
        if (!c || !o || typeof c.line_start !== "number") {
            return;
        }
        this.select(index, c.line_start, false);
        if (typeof c.line_end === "number" && c.line_end !== c.line_start) {
            this.select(index, c.line_end, true);
        }
        const root = this.diffRoot(o.path);
        const row = root ? rowOfLine(root, c.line_start) : null;
        if (root && row) {
            // The target may sit in a folded run of unchanged lines.
            revealRow(row);
            setStop(root, row);
            row.focus();
        }
    }

    /** A comment of another revision: that revision is shown. */
    public onChangesOtherPress(event: UI5Event): void {
        const ctx = (event.getSource() as Control).getBindingContext("s");
        const revision = ctx?.getProperty("revision") as number | undefined;
        const index = Number(/\/objects\/(\d+)\//.exec(ctx?.getPath() ?? "")?.[1]);
        if (typeof revision === "number" && this.changes[index]) {
            void this.showRevision(index, revision);
        }
    }

    public onChangesRevisionChange(event: UI5Event): void {
        const source = event.getSource() as Control;
        const index = source.getBindingContext("s")?.getProperty("index") as number | undefined;
        const item = (event.getParameters() as { selectedItem?: { getKey(): string } }).selectedItem;
        const revision = Number(item?.getKey());
        if (typeof index === "number" && Number.isInteger(revision) && revision > 0) {
            void this.showRevision(index, revision);
        }
    }

    private async showRevision(index: number, revision: number): Promise<void> {
        const o = this.changes[index];
        if (!o) {
            return;
        }
        // The last switch on a card wins: an earlier one still in flight is dropped when it answers.
        const token = ++this.switchSeq;
        this.switching.set(o.path, token);
        if (o.revision === revision) {
            // Back to the revision on screen: nothing to read, and a pending switch is void.
            this.keepShownRevision(index, o);
            return;
        }
        const seq = this.artifactSeq;
        const sid = this.sid;
        this.picked.set(o.path, { revision, latest: o.latest });
        this.s().setProperty(`/artifact/changes/objects/${index}/busy`, true);
        // Only onto the card it was asked on: one a sync replaced meanwhile is newer, and a late answer is dropped.
        const current = (): boolean => seq === this.artifactSeq && sid === this.sid
            && this.changes[index] === o && this.switching.get(o.path) === token;
        try {
            const next = await this.loadChangeObject(o.summary, revision);
            if (current()) {
                this.replaceChange(index, next, true);
            }
        } catch (e) {
            if (current()) {
                this.keepShownRevision(index, o);
                this.showError(e);
            }
        }
    }

    /** Not shown, so not picked: the pick (and the picker) go back to the revision on screen. */
    private keepShownRevision(index: number, o: ChangeObject): void {
        if (o.revision >= o.latest) {
            this.picked.delete(o.path);
        } else {
            this.picked.set(o.path, { revision: o.revision, latest: o.latest });
        }
        this.s().setProperty(`/artifact/changes/objects/${index}/revisionKey`, String(o.revision));
        this.s().setProperty(`/artifact/changes/objects/${index}/busy`, false);
        this.refreshHeader();
    }

    private objectOf(event: UI5Event): { index: number; o: ChangeObject } | undefined {
        const index = (event.getSource() as Control).getBindingContext("s")?.getProperty("index") as number | undefined;
        const o = typeof index === "number" ? this.changes[index] : undefined;
        return o && typeof index === "number" ? { index, o } : undefined;
    }

    /** Check syntax of the revision shown, as the signed-in user; refusals (409) through the gate texts. */
    public async onCheckSyntax(event: UI5Event): Promise<void> {
        const hit = this.objectOf(event);
        const m = this.s();
        if (!hit || m.getProperty(`/artifact/changes/objects/${hit.index}/syntaxBusy`)) {
            return;
        }
        const { index, o } = hit;
        const seq = this.artifactSeq;
        const sid = this.sid;
        m.setProperty(`/artifact/changes/objects/${index}/syntaxBusy`, true);
        try {
            const result = await this.service().checkSyntax(sid, o.path, o.revision);
            if (seq !== this.artifactSeq || sid !== this.sid || this.changes[index] !== o) {
                return;
            }
            o.syntaxStatus = result.status;
            o.syntaxItems = result.items ?? [];
            o.revisions = o.revisions.map((r) => (r.revision === o.revision ? { ...r, syntax_status: result.status } : r));
            const syntax = syntaxInfo(o.syntaxStatus, o.syntaxItems, this.translator());
            m.setProperty(`/artifact/changes/objects/${index}/syntax`, { text: syntax.text, state: syntax.state, icon: syntax.icon });
            m.setProperty(`/artifact/changes/objects/${index}/syntaxMessages`, syntax.messages.map((text) => ({ text })));
            m.setProperty(`/artifact/changes/objects/${index}/html`, this.diffHtml(o, index));
            this.redecorateSoon(index);
            this.announce(this.text("changesSyntaxChecked", [syntax.text]));
            // The header and the worklist follow the stored result; the card already shows it.
            void this.reloadDetail().then(() => {
                const fresh = (this.detail?.files ?? []).find((f) => f.path === o.path);
                if (fresh && this.changes[index] === o) {
                    o.summary = fresh;
                }
            }).catch(() => undefined);
        } catch (e) {
            if (seq === this.artifactSeq && sid === this.sid) {
                this.showGateError(e);
            }
        } finally {
            if (seq === this.artifactSeq && sid === this.sid) {
                m.setProperty(`/artifact/changes/objects/${index}/syntaxBusy`, false);
            }
        }
    }

    /** Run SAPLint on the object; its findings are stored and counted on the card. */
    public async onRunLint(event: UI5Event): Promise<void> {
        const hit = this.objectOf(event);
        const m = this.s();
        if (!hit || m.getProperty(`/artifact/changes/objects/${hit.index}/lintBusy`)) {
            return;
        }
        const { index, o } = hit;
        if (o.revision < o.latest) {
            // Lint reads the latest revision; findings would be shown against another one.
            return;
        }
        const seq = this.artifactSeq;
        const sid = this.sid;
        m.setProperty(`/artifact/changes/objects/${index}/lintBusy`, true);
        try {
            const lint = await this.service().lintFile(sid, o.path);
            if (seq !== this.artifactSeq || sid !== this.sid || this.changes[index] !== o) {
                return;
            }
            o.lint = lint ?? [];
            o.lintedHere = true;
            const info = lintInfo(o.lint, true, this.translator());
            m.setProperty(`/artifact/changes/objects/${index}/lint`, info);
            this.announce(this.text("changesLintDone", [info.text]));
        } catch (e) {
            if (seq === this.artifactSeq && sid === this.sid) {
                this.showGateError(e);
            }
        } finally {
            if (seq === this.artifactSeq && sid === this.sid) {
                m.setProperty(`/artifact/changes/objects/${index}/lintBusy`, false);
            }
        }
    }

    /** A diff too large to show: its source instead. */
    public onChangesShowSource(event: UI5Event): void {
        const hit = this.objectOf(event);
        if (hit) {
            this.opener = event.getSource() as Control;
            // Encoded: the router refuses a "/" in a query value; the route hands it back decoded.
            this.getRouter().navTo("session", { id: this.sid, "?query": { view: "source", path: encodeURIComponent(hit.o.path) } });
        }
    }

    /**
     * `?view=source&path=&line=`: the file's newest proposal (or the SAP
     * source when there is none) through renderSource (escaped), the line
     * highlighted and scrolled into view once rendered.
     */
    private async loadSource(seq: number, query: SessionQuery): Promise<void> {
        const m = this.s();
        const path = Session.queryPath(query.path);
        const summary = (this.detail?.files ?? []).find((f) => f.path === path);
        m.setProperty("/artifact", Session.emptyArtifact(
            path ? this.text("sourceViewTitle", [Session.fileName(path)]) : this.text("artifactSourceTitle"), "source"));
        if (!summary) {
            m.setProperty("/artifact/missing", this.text("sourceNotFound"));
            return;
        }
        const sid = this.sid;
        m.setProperty("/artifact/busy", true);
        try {
            const file = await this.service().getFile(sid, path);
            if (seq !== this.artifactSeq || sid !== this.sid) {
                return;
            }
            // A finding's line is a line of the SAP source: `base=sap` shows it whatever was proposed since.
            // A proposal is a file in state modified/new (as the changes view): a dropped one (read) shows SAP.
            const proposed = query.base !== "sap" && isProposal(file) && (file.revision ?? 0) > 0;
            const text = String((proposed ? file.proposed_source : file.origin_source) ?? "");
            const count = text ? text.replace(/\r\n?/g, "\n").replace(/\n$/, "").split("\n").length : 0;
            const asked = Math.floor(Number(query.line));
            // A line past the end (or any line of an empty source) is not the last line: nothing is highlighted.
            const beyond = !!query.line && Number.isFinite(asked) && asked > count;
            const line = beyond ? 0 : scrollTargetLine(Number(query.line ?? 1), count);
            const t = this.translator();
            this.sourceLine = query.line ? line : 0;
            // ADT opens the SAP object (a class include as that include): with the line only when the
            // SAP source is what is shown and the line is in it.
            const href = file.object_type && file.base_status === "sap"
                ? adtUri(this.detail?.target ?? "", file.object_type, file.object_name ?? "",
                    proposed ? undefined : line || undefined, classIncludeOf(path)) ?? ""
                : "";
            const beyondNote = !beyond ? ""
                : count === 0 ? this.text("sourceEmptyLine", [asked]) : this.text("sourceLineBeyond", [asked, count]);
            m.setProperty("/artifact/source", {
                html: renderSource(text, {
                    highlightLine: query.line && line ? line : null,
                    lint: file.lint,
                    labels: {
                        region: this.text("sourceRegion", [Session.fileName(path)]),
                        lineHeader: this.text("sourceHeadLine"),
                        lintHeader: this.text("sourceHeadLint"),
                        codeHeader: this.text("changesHeadCode"),
                        error: t("changesSeverityError"),
                        warning: t("changesSeverityWarning"),
                        tooLarge: (shown, total) => this.text("sourceTooLarge", [shown, total])
                    }
                }),
                meta: proposed ? this.text("sourceFromRevision", [file.revision]) : this.text("sourceFromSap"),
                adtHref: href,
                ...this.findingSourceTexts(path, query, beyondNote)
            });
        } catch (e) {
            if (seq === this.artifactSeq && sid === this.sid) {
                this.showError(e);
            }
        } finally {
            if (seq === this.artifactSeq && sid === this.sid) {
                m.setProperty("/artifact/busy", false);
            }
        }
    }

    /**
     * What the source view says about the finding it was opened for: which
     * finding the line is (its title, as text), or the server's hint when
     * the line is in an include that does not map to this source.
     */
    private findingSourceTexts(
        path: string, query: SessionQuery, beyondNote: string
    ): { findingNote: string; hint: string; fromFinding: boolean } {
        const f = this.sourceFinding;
        const fromFinding = this.detail?.type === "diagnose";
        if (!f || f.path !== path || query.base !== "sap") {
            return { findingNote: "", hint: beyondNote, fromFinding };
        }
        return {
            // Not when the line is beyond the source: no line is that finding's then.
            findingNote: !beyondNote && f.line && query.line === String(f.line) ? this.text("sourceFindingNote", [f.line, f.title]) : "",
            hint: [f.hint ? this.text("findingLineHint", [f.hint]) : "", beyondNote].filter(Boolean).join(" "),
            fromFinding
        };
    }

    /**
     * The `path` of a query. A link built by navTo carries it URI-encoded (the
     * router refuses a "/" in a query value), a typed deep link plain: an
     * encoded value without a "/" is decoded once.
     */
    private static queryPath(value: string | undefined): string {
        const raw = String(value ?? "");
        if (raw.includes("/") || !/%2f/i.test(raw)) {
            return raw;
        }
        try {
            return decodeURIComponent(raw);
        } catch {
            return raw;
        }
    }

    /** The source was rendered: the line of the query is scrolled into view (once per load). */
    public onSourceRendered(): void {
        const line = this.sourceLine;
        if (!line) {
            return;
        }
        let tries = 0;
        const attempt = (): void => {
            const row = this.byId("sourceView")?.getDomRef()?.querySelector<HTMLElement>(`tr[data-line='${line}']`);
            if (row?.offsetParent) {
                this.sourceLine = 0;
                row.scrollIntoView({ block: "center" });
            } else if (++tries < 60) {
                requestAnimationFrame(attempt);
            }
        };
        requestAnimationFrame(attempt);
    }

    /** Puts the focus on the column's title once it is rendered and shown (the column animates in). */
    private focusArtifactTitle(seq: number): void {
        let tries = 0;
        const attempt = (): void => {
            if (seq !== this.artifactSeq) {
                return;
            }
            const dom = this.byId("artifactTitle")?.getDomRef() as HTMLElement | null | undefined;
            if (dom && dom.offsetParent && dom.getAttribute("tabindex") === "-1") {
                dom.focus();
            } else if (++tries < 60) {
                requestAnimationFrame(attempt);
            }
        };
        requestAnimationFrame(attempt);
    }

    private returnFocus(): void {
        const opener = this.opener;
        this.opener = undefined;
        requestAnimationFrame(() => {
            if (opener?.getDomRef()?.isConnected) {
                opener.focus();
            } else {
                (this.byId("chatInput") as TextArea | undefined)?.focus();
            }
        });
    }

    // --- Findings (Task U11) -----------------------------------------------------

    /** The findings list and the header's count, from `this.findings`. */
    private renderFindings(): void {
        const m = this.s();
        const t = this.translator();
        const target = this.detail?.target ?? "";
        if (this.query.view === "findings") {
            m.setProperty("/artifact/title", this.text("findingsTitle", [this.findings.length]));
            m.setProperty("/artifact/findings/items", this.findings.map((f) => {
                const row = findingRow(f, target, t, (iso) => this.formatTime(iso));
                return { ...row, meta: row.time ? `${row.where} \u00b7 ${row.time}` : row.where };
            }));
            m.setProperty("/artifact/findings/any", this.findings.length > 0);
        }
        this.refreshDiagnose();
    }

    /** The header's Findings link (and the source view's way back). */
    public onShowFindings(event: UI5Event): void {
        this.opener = event.getSource() as Control;
        this.getRouter().navTo("session", { id: this.sid, "?query": { view: "findings" } });
    }

    /** The header's Report link: the latest report in the document view. */
    public onShowReport(event: UI5Event): void {
        const version = this.latestVersion("report");
        if (version !== undefined) {
            this.opener = event.getSource() as Control;
            this.getRouter().navTo("session", { id: this.sid, "?query": { view: "document", kind: "report", version: String(version) } });
        }
    }

    private findingOf(event: UI5Event): DiagnoseFinding | undefined {
        const id = (event.getSource() as Control).getBindingContext("s")?.getProperty("id") as string | undefined;
        return id ? this.findings.find((f) => f.id === id) : undefined;
    }

    /** A row opens the finding's SAP source at its line; without a program there is none (said, nothing asked). */
    public onFindingPress(event: UI5Event): void {
        const finding = this.findingOf(event);
        if (!finding) {
            return;
        }
        if (this.s().getProperty("/lostFlag")) {
            // Opening reads from SAP: off, with the reason shown above the list (the row is not an action then).
            return;
        }
        if (!finding.program) {
            MessageBox.information(this.text("findingNoSource"));
            return;
        }
        this.opener = event.getSource() as Control;
        void this.openFinding(finding);
    }

    /**
     * The server reads the finding's program into the workspace (a read from
     * SAP: refused once the target lost its flag) and answers the line, or a
     * hint when the line is in an include that does not map to the opened
     * source. The source view then shows the SAP source (`base=sap`): the
     * line is a line of SAP, not of a proposal written since.
     */
    private async openFinding(finding: DiagnoseFinding): Promise<void> {
        const sid = this.sid;
        if (this.openingFindings.has(finding.id)) {
            return;
        }
        this.openingFindings.add(finding.id);
        let opened: FindingOpen;
        try {
            opened = await this.service().openFinding(sid, finding.id);
            if (sid !== this.sid || !opened?.file?.path) {
                return;
            }
            // The file list knows the opened file before the source view looks it up.
            await this.reloadDetail();
        } catch (e) {
            if (sid === this.sid) {
                if (e instanceof IdeError && e.code === "no_source") {
                    MessageBox.information(this.text("findingNoSource"));
                } else {
                    this.showGateError(e);
                }
            }
            return;
        } finally {
            this.openingFindings.delete(finding.id);
        }
        if (sid !== this.sid) {
            return;
        }
        const path = opened.file.path;
        const line = typeof opened.line === "number" && opened.line > 0 ? opened.line : null;
        this.sourceFinding = { path, line, hint: opened.hint ?? null, title: String(finding.title ?? "") };
        const query: Record<string, string> = { view: "source", path: encodeURIComponent(path), base: "sap" };
        if (line) {
            query.line = String(line);
        }
        this.getRouter().navTo("session", { id: sid, "?query": query });
        if (!line && opened.hint) {
            this.announce(this.text("findingLineHintToast"));
        }
    }

    /** Details: the finding's text as kept with the session, in the dialog (plain text, read-only). */
    public async onFindingDetails(event: UI5Event): Promise<void> {
        const finding = this.findingOf(event);
        if (!finding || this.s().getProperty("/lostFlag")) {
            return;
        }
        this.s().setProperty("/findingDetail", { ...NO_FINDING_DETAIL, id: finding.id, title: String(finding.title ?? "") });
        try {
            this.findingDetailDialog ??= this.loadFragment({ name: "com.agent.ide.fragment.FindingDetailDialog" }).then((dialog) => {
                // The editor's text input has no name of its own: named after every rendering and on open.
                const editor = this.byId("findingDetailEditor");
                const name = (): void => {
                    editor?.getDomRef()?.querySelector("textarea")?.setAttribute("aria-label", this.text("findingDetailEditorLabel"));
                };
                editor?.addEventDelegate({ onAfterRendering: name });
                (dialog as Dialog).attachAfterOpen(name);
                return dialog as Dialog;
            });
            (await this.findingDetailDialog).open();
        } catch (e) {
            this.findingDetailDialog = undefined;
            this.showError(e);
            return;
        }
        void this.loadFindingDetail(false);
    }

    public onFindingDetailRefresh(): void {
        void this.loadFindingDetail(true);
    }

    /**
     * Reads the text of the finding the dialog shows: as kept with the
     * session, or (`refresh`) again from SAP. Raw SAP text on a flagged
     * target: shown as plain text in a read-only editor, kept in the model
     * only while the dialog is open.
     */
    private async loadFindingDetail(refresh: boolean): Promise<void> {
        const sid = this.sid;
        const fid = this.s().getProperty("/findingDetail/id") as string;
        const token = ++this.detailSeq;
        if (!sid || !fid) {
            return;
        }
        const set = (key: string, value: unknown): void => { this.s().setProperty(`/findingDetail/${key}`, value); };
        set("busy", true);
        try {
            const answer = await this.service().getFinding(sid, fid, refresh);
            if (token === this.detailSeq) {
                set("text", String(answer.detail ?? ""));
                if (refresh) {
                    MessageToast.show(this.text("findingDetailRefreshed"));
                }
            }
        } catch (e) {
            if (token !== this.detailSeq) {
                return;
            }
            if (e instanceof IdeError && e.code === "no_detail") {
                if (refresh && this.s().getProperty("/findingDetail/text")) {
                    MessageToast.show(this.text("findingNoDetail"));
                } else {
                    set("text", this.text("findingNoDetail"));
                }
                set("canRefresh", false);
            } else if (!refresh && e instanceof IdeError && e.code === "target_not_non_production") {
                // Said where the text would be; nothing to refresh. The page learns about the lost flag.
                set("text", errorText(e, (k, a) => this.text(k, a)));
                set("canRefresh", false);
                this.afterLostFlag(e);
            } else {
                // A refused refresh included: the text already shown stays.
                this.showGateError(e);
            }
        } finally {
            if (token === this.detailSeq) {
                set("busy", false);
            }
        }
    }

    public onFindingDetailClose(): void {
        void this.findingDetailDialog?.then((dialog) => dialog.close());
    }

    public onFindingDetailAfterClose(): void {
        // Nothing of the text stays behind, and an answer still on its way is dropped.
        this.detailSeq++;
        this.s().setProperty("/findingDetail", { ...NO_FINDING_DETAIL });
    }

    private kindText(kind: ArtifactKind): string {
        return this.text(`docKind${kind.charAt(0).toUpperCase()}${kind.slice(1)}`);
    }

    public onCloseArtifact(): void {
        this.closing = true;
        this.getRouter().navTo("session", { id: this.sid });
    }

    public onNavBack(): void {
        this.getRouter().navTo("sessions");
    }
}
