import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type Dialog from "sap/m/Dialog";
import type Popover from "sap/m/Popover";
import type Select from "sap/m/Select";
import type TextArea from "sap/m/TextArea";
import type Controller from "sap/ui/core/mvc/Controller";
import type View from "sap/ui/core/mvc/View";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, iSend, thePrimaryActionIs, theReasonIs } from "./pages/Session";
import { announced, closeMessageBox, recordAnnouncements } from "./pages/Shared";
import type FakeBackend from "./FakeBackend";
import type { Comment } from "../../service/types";

/**
 * Review follow-ups after U10 (F1-F7, nits, G1-G3): what the document view
 * and the changes view must do that the earlier journeys did not pin down.
 */
QUnit.module("Review follow-ups");

// --- Shared helpers ------------------------------------------------------------

/** The session page's controller, for state no control shows (private on purpose: read, not driven). */
export type Page = Record<string, unknown> & {
    allActivities: Set<string>;
    openOutputs: Set<string>;
    detail: { status: string } | null;
    startRun(kind: string, text: string): void;
    reloadComments(): Promise<void>;
    reloadDetail(): Promise<void>;
    syncChanges(): Promise<void>;
    onArtifactStored(kind: string, version: number): Promise<void>;
    loadFragment: unknown;
};

export function controllerOf(control: UI5Element): Page {
    let el = control as UI5Element | null;
    while (el && !el.isA("sap.ui.core.mvc.View")) {
        el = (el as UI5Element).getParent() as UI5Element | null;
    }
    return (el as unknown as View).getController() as Controller as unknown as Page;
}

/** Runs `fn` with the session page's controller once the page is there. */
export function withPage(When: Common, fn: (page: Page) => void): void {
    When.waitFor({
        id: "sessionPage",
        ...SOPTS,
        visible: false,
        success: (c: UI5Element) => fn(controllerOf(c)),
        errorMessage: "No session page"
    });
}

export function win(): Window {
    return Opa5.getWindow() as unknown as Window;
}

export function active(): HTMLElement | null {
    return win().document.activeElement as HTMLElement | null;
}

export function posts(key: string): number {
    return backend.requests.filter((k) => k === key).length;
}

/**
 * On the first frame the comment popover is shown (still animating in), a
 * key is typed on whatever has the focus then: in a real browser that is
 * where fast typing lands while the popover opens.
 */
function typeWhenPopoverShows(key: string): { done: boolean; on: string } {
    const seen = { done: false, on: "" };
    let frames = 0;
    const tick = (): void => {
        const pop = win().document.querySelector<HTMLElement>("[id$='--commentPopover']");
        if (pop && pop.offsetParent !== null) {
            const target = active() && active() !== win().document.body ? active()! : pop;
            seen.on = `${target.tagName}.${target.className}`;
            typeKey(target, key);
            seen.done = true;
        } else if (++frames < 600) {
            win().requestAnimationFrame(tick);
        }
    };
    win().requestAnimationFrame(tick);
    return seen;
}

/** A printable key as a browser sends it (the event a UI5 delegate sees bubbles up from `el`). */
export function typeKey(el: HTMLElement, key: string): void {
    el.dispatchEvent(new (win() as unknown as { KeyboardEvent: typeof KeyboardEvent }).KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
}

export function thePopoverDraftIs(Then: Common, value: string, message: string): void {
    Then.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => {
            const area = c as TextArea;
            const dom = area.getFocusDomRef() as HTMLTextAreaElement | null;
            return area.getValue() === value && !!dom && active() === dom && dom.selectionStart === value.length;
        },
        success: () => Opa5.assert.ok(true, message),
        error: function () {
            const area = win().document.querySelector<HTMLTextAreaElement>("[id$='--commentDraft'] textarea");
            Opa5.assert.ok(false, `The draft is ${JSON.stringify(area?.value)} (caret ${String(area?.selectionStart)}, focus ${active() === area ? "in it" : `on ${active()?.tagName ?? "nothing"}`}), not "${value}"`);
        },
        errorMessage: `The draft is not "${value}" with the focus at its end`
    });
}

export function closePopover(When: Common): void {
    When.waitFor({ id: "commentPopover", ...SOPTS, searchOpenDialogs: true, success: (p: UI5Element) => (p as Popover).close() });
}

// --- Document view ---------------------------------------------------------------

export function doc(blocks: number): string {
    return Array.from({ length: blocks }, (_, i) => `Block number ${i + 1} of the design.`).join("\n\n");
}

export function seedDoc(fake: FakeBackend, blocks = 4): string {
    const s = fake.sessions[0].session;
    s.stage = "design";
    s.request_cap = 1000;
    fake.addArtifact(s.id, "design", doc(blocks));
    return s.id;
}

export function docComment(id: string, paragraph: number, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 1, paragraph, body: `Body ${id}`, state: "open", answer: null, quote: null,
        created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00", ...extra
    };
}

export function content(): HTMLElement | null {
    return win().document.querySelector("[id$='--artifactContent']");
}

export function block(i: number): HTMLElement | null {
    return content()?.querySelector(`.ideDocBlock[data-para='${i}']`) ?? null;
}

export function theBlocksAreShown(Then: Common, count: number): void {
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => content()?.querySelectorAll(".ideDocBlock").length === count,
        success: () => Opa5.assert.ok(true, `all ${count} blocks are rendered`),
        errorMessage: `Not ${count} blocks`
    });
}

export function iPressKeyOn(When: Common, find: () => HTMLElement | null, key: string, shift = false, after?: (el: HTMLElement) => void): void {
    When.waitFor({
        id: "sessionPage",
        ...SOPTS,
        visible: false,
        check: () => !!find(),
        success: function () {
            const el = find()!;
            el.focus();
            QUnitUtils.triggerKeydown(el, key, shift, false, false);
            after?.(el);
        },
        errorMessage: `Nothing to press ${key} on`
    });
}

function theMarkerOfBlockIs(Then: Common, index: number, text: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        visible: false,
        matchers: (c: UI5Element) => c.getId().includes("--docMarker-") && c.getBindingContext("s")?.getPath() === `/artifact/doc/blocks/${index}`,
        check: (list: UI5Element[]) => (list[0] as Button).getText() === text,
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `The marker of block ${index + 1} is not "${text}"`
    });
}

opaTest("F1: a refused request-changes note is not offered in another session, nor after a successful run", function (Given: Common, When: Common, Then: Common) {
    let other = "";
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        seedDoc(fake);
        const b = fake.addSession("Other session");
        b.stage = "design";
        b.request_cap = 1000;
        fake.addArtifact(b.id, "design", doc(2));
        other = b.id;
    });
    theBlocksAreShown(Then, 4);
    const refuse = (): void => {
        Then.waitFor({
            success: function () {
                backend.failNext = {
                    path: "sessions/s-1/request-changes", status: 409,
                    body: { detail: "A run is already in progress.", code: "run_in_progress" }
                };
            }
        });
        iPress(When, "requestChangesButton");
        When.waitFor({
            id: "requestChangesNote", ...SOPTS, searchOpenDialogs: true,
            actions: new EnterText({ text: "Keep the old signature.", keepFocus: true }), errorMessage: "No note"
        });
        When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Send is off" });
        closeMessageBox(When);
    };
    const theNoteIs = (value: string, message: string): void => {
        iPress(When, "requestChangesButton");
        Then.waitFor({
            id: "requestChangesNote", ...SOPTS, searchOpenDialogs: true,
            check: (c: UI5Element) => (c as TextArea).getValue() === value,
            success: () => Opa5.assert.ok(true, message),
            errorMessage: `The note is not "${value}"`
        });
        When.waitFor({ id: "requestChangesCancel", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    };
    refuse();
    theNoteIs("Keep the old signature.", "the same session offers the refused note again");
    // Another session: nothing of the first one's note.
    Then.waitFor({ success: () => HashChanger.getInstance().setHash(`sessions/${other}?view=document&kind=design&version=1`) });
    theBlocksAreShown(Then, 2);
    theNoteIs("", "session B does not see session A's refused note");
    // Back in session A: refused again, then a run that succeeds clears it.
    Then.waitFor({ success: () => HashChanger.getInstance().setHash("sessions/s-1?view=document&kind=design&version=1") });
    theBlocksAreShown(Then, 4);
    refuse();
    iSend(When, "Go on.");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (c: UI5Element) => posts("POST sessions/s-1/messages") === 1 && (c.getModel("s") as JSONModel).getProperty("/running") === false
            && backend.dataOf("s-1")!.messages.some((m) => m.role === "assistant"),
        success: () => Opa5.assert.ok(true, "the message run ended"),
        errorMessage: "The message run did not end"
    });
    theNoteIs("", "after a successful run the refused note is gone");
    Then.iStopTheApp();
});

opaTest("F2: blocks are named groups with number, total and comments; the arrow help is on the container once", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        fake.dataOf(seedDoc(fake))!.comments.push(docComment("c-1", 1));
    });
    theBlocksAreShown(Then, 4);
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => block(1)?.getAttribute("aria-label") === "Block 2 of 4, 1 comment (Open)",
        success: function () {
            const root = content()!;
            Opa5.assert.strictEqual(block(0)!.getAttribute("role"), "group", "a block is a group");
            Opa5.assert.strictEqual(block(0)!.getAttribute("aria-label"), "Block 1 of 4", "named with its number and the total");
            Opa5.assert.ok(/--docBlockHint$/.test(block(0)!.getAttribute("aria-describedby") ?? ""), "a short per-block hint");
            Opa5.assert.ok((root.getAttribute("aria-describedby") ?? "").split(" ").some((id) => /--docHint$/.test(id)),
                "the arrow-key help is on the container");
            Opa5.assert.notOk(Array.from(root.querySelectorAll(".ideDocBlock"))
                .some((b) => /--docHint$/.test(b.getAttribute("aria-describedby") ?? "")), "and not repeated on every block");
        },
        errorMessage: "Block 2 is not named with its comment"
    });
    iPressKeyOn(When, () => block(0), "ENTER");
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "Shorter intro.", keepFocus: true }), errorMessage: "No text area"
    });
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => block(0)?.getAttribute("aria-label") === "Block 1 of 4, 1 comment (Open)",
        success: () => Opa5.assert.ok(true, "the name follows a new comment"),
        errorMessage: "The block's name did not follow the comment"
    });
    Then.iStopTheApp();
});

opaTest("F4: a comments read that left before a local save does not overwrite it", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    let reloaded = false;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    withPage(When, (page) => {
        // The server answers now (no comment yet); the answer arrives only later.
        release = backend.holdResponse("GET sessions/s-1/comments");
        void page.reloadComments().then(() => { reloaded = true; });
    });
    iPressKeyOn(When, () => block(0), "ENTER");
    When.waitFor({
        id: "commentDraft", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "Saved meanwhile.", keepFocus: true }), errorMessage: "No text area"
    });
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    theMarkerOfBlockIs(Then, 0, "1", "the saved comment is counted");
    Then.waitFor({ success: () => release() });
    Then.waitFor({
        check: () => reloaded,
        success: () => Opa5.assert.ok(true, "the older read has arrived"),
        errorMessage: "The read never came back"
    });
    theMarkerOfBlockIs(Then, 0, "1", "the older read did not take the comment away");
    Then.iStopTheApp();
});

opaTest("F5: a rendered block fixes only itself; Shift+Arrow is left to the browser", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seedDoc(fake); });
    theBlocksAreShown(Then, 4);
    When.waitFor({
        controlType: "sap.ui.core.HTML",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--docBlock-") && c.getBindingContext("s")?.getPath() === "/artifact/doc/blocks/2",
        success: function (list: UI5Element[]) {
            // A stale marker on another block: a render of block 3 must not walk every block.
            block(3)!.setAttribute("data-probe", "untouched");
            block(3)!.setAttribute("tabindex", "0");
            (list[0] as unknown as { fireAfterRendering(p: object): void }).fireAfterRendering({ isPreservedDOM: false });
            Opa5.assert.strictEqual(block(3)!.getAttribute("tabindex"), "0", "block 4 was not visited");
            Opa5.assert.strictEqual(block(2)!.getAttribute("tabindex"), "-1", "block 3 got its own tab index");
            Opa5.assert.strictEqual(block(2)!.getAttribute("aria-label"), "Block 3 of 4", "and its name");
            block(3)!.setAttribute("tabindex", "-1");
        },
        errorMessage: "No block 3 control"
    });
    iPressKeyOn(When, () => block(1), "ARROW_DOWN", true);
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => active() === block(1),
        success: () => Opa5.assert.ok(true, "Shift+Down does not move to the next block"),
        errorMessage: "Shift+Down moved the focus"
    });
    Then.iStopTheApp();
});

opaTest("G2 + F6: keys typed right after Enter land in the draft; the quote leaves out the link host", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = seedDoc(fake, 1);
        fake.dataOf(sid)!.artifacts = [];
        fake.addArtifact(sid, "design", "See [the docs](https://example.com/a).\n\nSecond.");
    });
    theBlocksAreShown(Then, 2);
    let late = { done: false, on: "" };
    iPressKeyOn(When, () => block(0), "ENTER", false, (el) => {
        late = typeWhenPopoverShows("!");
        // Typed before the popover is open: still on the block.
        ["c", "h", "e", "c", "k"].forEach((k) => typeKey(el, k));
    });
    Then.waitFor({
        check: () => late.done,
        success: () => Opa5.assert.ok(true, `a key was typed while the popover animated in (on ${late.on})`),
        errorMessage: "The popover never showed"
    });
    thePopoverDraftIs(Then, "check!", "every key typed after Enter is in the draft, the caret at its end");
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Save is off" });
    Then.waitFor({
        check: () => posts("POST sessions/s-1/comments") === 1,
        success: function () {
            const sent = backend.bodies.find((b) => b.key === "POST sessions/s-1/comments")!.body as Record<string, unknown>;
            Opa5.assert.strictEqual(sent.body, "check!");
            Opa5.assert.strictEqual(sent.quote, "See the docs.", "the host span is not quoted");
        },
        errorMessage: "No comment created"
    });
    Then.iStopTheApp();
});

opaTest("Nits: Send with a run started elsewhere closes the dialog and says so; a failed popover load does not block the new version", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        fake.dataOf(seedDoc(fake))!.comments.push(docComment("c-1", 1));
    });
    theBlocksAreShown(Then, 4);
    iPress(When, "requestChangesButton");
    When.waitFor({
        id: "requestChangesNote", ...SOPTS, searchOpenDialogs: true,
        actions: new EnterText({ text: "Note.", keepFocus: true }), errorMessage: "No note"
    });
    withPage(When, (page) => { page.detail!.status = "running"; });
    When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Send is off" });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: (dialogs: UI5Element[]) => dialogs.some((d) => (d.getDomRef()?.className ?? "").includes("sapMMessageBox")
            && (d.getDomRef()?.textContent ?? "").includes("was not sent")),
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.notOk(dialogs.some((d) => d.getId().endsWith("--requestChangesDialog") && (d as Dialog).isOpen()), "the dialog is closed");
            Opa5.assert.strictEqual(posts("POST sessions/s-1/request-changes"), 0, "nothing was sent");
        },
        errorMessage: "No message about the other run"
    });
    closeMessageBox(When, "OK");
    withPage(When, (page) => {
        page.detail!.status = "idle";
        page.loadFragment = () => Promise.reject(new Error("The popover could not be loaded"));
    });
    iPressKeyOn(When, () => block(0), "ENTER");
    closeMessageBox(When);
    withPage(When, (page) => {
        backend.addArtifact("s-1", "design", "# Design v2");
        void page.onArtifactStored("design", 2);
    });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design v2" }),
        success: () => Opa5.assert.ok(true, "the new version is still selected automatically"),
        errorMessage: "A failed popover load blocked the auto-select"
    });
    Then.iStopTheApp();
});

opaTest("Nit: the conventions dialog lists more than 100 targets", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        for (let i = 0; i < 120; i++) {
            fake.conventions.push({ target: `T${String(i).padStart(3, "0")}` });
        }
    });
    When.waitFor({ id: "conventionsButton", viewName: "App", actions: new Press(), errorMessage: "No Conventions button" });
    Then.waitFor({
        id: "convTarget",
        viewName: "App",
        searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Select).getItems().length >= 120,
        success: () => Opa5.assert.ok(true, "every target is offered"),
        errorMessage: "The target list is cut"
    });
    When.waitFor({ id: "conventionsDialog", viewName: "App", searchOpenDialogs: true, success: (d: UI5Element) => (d as Dialog).close() });
    Then.iStopTheApp();
});

// --- Conversation: activity panel state between runs ------------------------------------

opaTest("F3: Show all and opened outputs of a streamed answer do not leak into the next run; a start during a run changes nothing", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        fake.sessions[0].session.request_cap = 1000;
        release = fake.pauseStream();
    });
    iSend(When, "First question.");
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (c: UI5Element) => (c.getModel("s") as JSONModel).getProperty("/running") === true,
        success: () => Opa5.assert.ok(true, "the run streams"),
        errorMessage: "No run"
    });
    withPage(When, (page) => {
        page.allActivities.add("streaming");
        page.openOutputs.add("streaming#0");
        page.startRun("message", "A second start while the first runs.");
        Opa5.assert.ok(page.allActivities.has("streaming"), "a refused start leaves Show all alone");
        Opa5.assert.ok(page.openOutputs.has("streaming#0"), "and the opened output");
        Opa5.assert.strictEqual(posts("POST sessions/s-1/messages"), 1, "and starts nothing");
        release();
    });
    thePrimaryActionIs(Then, "Start design", true, "the run is over");
    withPage(When, (page) => {
        Opa5.assert.notOk(page.allActivities.has("streaming"), "the streamed answer's Show all is gone at the end");
        Opa5.assert.notOk(Array.from(page.openOutputs).some((k) => k.startsWith("streaming#")), "and its opened outputs");
    });
    Then.iStopTheApp();
});

// --- Changes view ------------------------------------------------------------------

export const X = "src/CLAS/zcl_x.clas.abap";
export const OLD_X = Array.from({ length: 30 }, (_, i) => (i === 11 ? "    rv_amount = round( val = iv_amount dec = 2 )." : `    " step ${i + 1}`)).join("\n");
export const NEW_X = OLD_X.replace("    rv_amount = round( val = iv_amount dec = 2 ).",
    "    DATA(lv_decimals) = get_decimals( iv_currency ).\n    rv_amount = round( val = iv_amount dec = lv_decimals ).");

export function seedChanges(fake: FakeBackend, revisions = 1, comments: Comment[] = []): string {
    const s = fake.sessions[0].session;
    s.stage = "propose";
    s.target = "DEMO";
    s.request_cap = 1000;
    const data = fake.dataOf(s.id)!;
    data.files.push({ path: X, state: "modified", object_type: "CLAS", object_name: "ZCL_X", origin_source: OLD_X, proposed_source: "", origin_version: "00042" });
    fake.addRevision(s.id, X, NEW_X);
    for (let r = 2; r <= revisions; r++) {
        fake.addRevision(s.id, X, `${NEW_X}\n" revision ${r}`);
    }
    data.comments.push(...comments);
    return s.id;
}

export function diff(): HTMLElement | null {
    return win().document.querySelector<HTMLElement>(`.ideUnified[data-path='${X}']`);
}

export function newRow(line: number): HTMLElement | null {
    return diff()?.querySelector<HTMLElement>(`tr[data-side='new'][data-line='${line}']`) ?? null;
}

export function theDiffIsShown(Then: Common, revision: number): void {
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => diff()?.getAttribute("data-revision") === String(revision) && !!diff()?.querySelector("[tabindex='0']"),
        success: () => Opa5.assert.ok(true, `revision ${revision} is shown`),
        errorMessage: `Revision ${revision} is not shown`
    });
}

export function switchRevision(When: Common, key: string): void {
    When.waitFor({
        controlType: "sap.m.Select",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesRevision-"),
        success: function (list: UI5Element[]) {
            const select = list[0] as Select;
            select.setSelectedKey(key);
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        },
        errorMessage: "No revision select"
    });
}

export function theSelectionIs(Then: Common, lines: string[], message: string): void {
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => JSON.stringify(Array.from(diff()?.querySelectorAll("tr[aria-selected='true']") ?? [])
            .map((r) => r.getAttribute("data-line"))) === JSON.stringify(lines),
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `The selection is not ${lines.join(", ") || "empty"}`
    });
}

opaTest("F2 (diff lines): each line is named with its kind, number, total, comments and code; the short hint is per line", function (Given: Common, When: Common, Then: Common) {
    const c1: Comment = {
        id: "c-1", anchor: "file", path: X, revision: 1, line_start: 13, line_end: 13, kind: null, version: null, paragraph: null,
        body: "Body", state: "open", answer: null, quote: null, created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00"
    };
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seedChanges(fake, 1, [c1]); });
    theDiffIsShown(Then, 1);
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => newRow(12)?.getAttribute("aria-label") === "Added line 12 of 31: DATA(lv_decimals) = get_decimals( iv_currency ).",
        success: function () {
            Opa5.assert.strictEqual(newRow(13)!.getAttribute("aria-label"),
                "Added line 13 of 31, 1 comment (Open): rv_amount = round( val = iv_amount dec = lv_decimals ).", "with its comment");
            Opa5.assert.strictEqual(diff()!.querySelector("tr[data-side='old'][data-line='12']")!.getAttribute("aria-label"),
                "Removed line 12 of the SAP version: rv_amount = round( val = iv_amount dec = 2 ).");
            Opa5.assert.ok(/--changesRowHint$/.test(newRow(12)!.getAttribute("aria-describedby") ?? ""), "the short per-line hint");
            Opa5.assert.ok(/--changesHint$/.test(diff()!.getAttribute("aria-describedby") ?? ""), "the full help once on the diff");
        },
        errorMessage: "Line 12 is not named"
    });
    iPressKeyOn(When, () => newRow(12), "ARROW_DOWN", true);
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => active() === newRow(12),
        success: () => Opa5.assert.ok(true, "Shift+Down is not taken by the diff"),
        errorMessage: "Shift+Down moved"
    });
    Then.iStopTheApp();
});

opaTest("G2 (diff lines): keys typed right after Enter land in the draft", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seedChanges(fake); });
    theDiffIsShown(Then, 1);
    iPressKeyOn(When, () => newRow(12), "ENTER", false, (el) => {
        ["n", "p", " ", "c"].forEach((k) => typeKey(el, k));
    });
    thePopoverDraftIs(Then, "np c", "N, P, Space and C typed before the popover opened are text, not commands");
    closePopover(When);
    Then.iStopTheApp();
});

opaTest("G1: an older revision on screen disables Approve changes with a visible reason", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seedChanges(fake, 2); });
    theDiffIsShown(Then, 2);
    thePrimaryActionIs(Then, "Approve changes and review", true, "the latest revision is shown");
    switchRevision(When, "1");
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, "Approve changes and review", false, "an older revision is shown");
    theReasonIs(Then, "You are viewing an older revision — switch to the latest to approve", "the reason is visible");
    switchRevision(When, "2");
    theDiffIsShown(Then, 2);
    thePrimaryActionIs(Then, "Approve changes and review", true, "back on the latest");
    Then.iStopTheApp();
});

opaTest("G3: a line selection survives a reload of the same revision; a new revision clears it and says so", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seedChanges(fake); });
    theDiffIsShown(Then, 1);
    iPressKeyOn(When, () => newRow(12), "SPACE");
    iPressKeyOn(When, () => newRow(13), "SPACE", true);
    theSelectionIs(Then, ["12", "13"], "lines 12-13 are selected");
    withPage(When, (page) => {
        recordAnnouncements();
        // The same revision with a new syntax result: the card is read again.
        const rev = backend.dataOf("s-1")!.files.find((f) => f.path === X)!.revisions![0];
        Object.assign(rev, { syntax_status: "ok", syntax: [], checked_at: "2026-10-03T11:00:00" });
        void page.reloadDetail().then(() => page.syncChanges());
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesSyntax-"),
        check: (list: UI5Element[]) => (list[0] as unknown as { getText(): string }).getText() === "No syntax messages",
        success: () => Opa5.assert.ok(true, "the card was reloaded"),
        errorMessage: "The card was not reloaded"
    });
    theSelectionIs(Then, ["12", "13"], "the selection survived the reload");
    Then.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesCommentButton-"),
        check: (list: UI5Element[]) => (list[0] as Button).getText() === "Comment on lines 12–13",
        success: () => Opa5.assert.notOk(announced.some((a) => a.includes("cleared")), "nothing was announced as cleared"),
        errorMessage: "The comment button lost the selection"
    });
    withPage(When, (page) => {
        backend.addRevision("s-1", X, `${NEW_X}\n" two`);
        void page.reloadDetail().then(() => page.syncChanges());
    });
    theDiffIsShown(Then, 2);
    theSelectionIs(Then, [], "a new revision clears the selection");
    Then.waitFor({
        check: () => announced.includes("The line selection was cleared: ZCL_X now shows revision 2."),
        success: () => Opa5.assert.ok(true, "and says so"),
        errorMessage: `Not announced: ${announced.join(" | ")}`
    });
    Then.iStopTheApp();
});
