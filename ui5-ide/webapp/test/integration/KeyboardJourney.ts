import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import Theming from "sap/ui/core/Theming";
import CoreElement from "sap/ui/core/Element";
import type UI5Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type Dialog from "sap/m/Dialog";
import type Popover from "sap/m/Popover";
import type Switch from "sap/m/Switch";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS } from "./pages/Session";
import { announced, recordAnnouncements } from "./pages/Shared";
import { APP, D } from "./ConventionsJourney";
import { X, active, block, diff, newRow, seedChanges, seedDoc, win } from "./ReviewFollowupsJourney";
import type FakeBackend from "./FakeBackend";

/**
 * Task U17: the whole flow with the keyboard only, and the high-contrast
 * themes. Every action is a native key event (keydown, and keyup unless a
 * held key is meant) dispatched on the element that has the focus, as a
 * browser sends it; text goes in through EnterText. The steps assert where
 * the focus lands, that it returns after every popover and dialog, that
 * states are text, and what is announced.
 *
 * What this cannot show: how a real screen reader reads the names and
 * descriptions asserted here (no assistive technology runs in karma).
 */
/**
 * The high-contrast tests switch the theme of the shared karma window: whatever a test did
 * (one that fails midway never reaches its own switch back), sap_horizon is applied again
 * before the next test starts.
 */
function restoreTheme(): Promise<void> {
    if (Theming.getTheme() === "sap_horizon") {
        return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 15000);
        const done = (): void => {
            if (Theming.getTheme() === "sap_horizon") {
                clearTimeout(timer);
                Theming.detachApplied(done);
                resolve();
            }
        };
        Theming.setTheme("sap_horizon");
        Theming.attachApplied(done);
    });
}

QUnit.module("Keyboard and high contrast", {
    afterEach: restoreTheme
});

const KEY_CODES: Record<string, number> = {
    Enter: 13, Escape: 27, " ": 32, ArrowUp: 38, ArrowDown: 40, F2: 113, Tab: 9, c: 67, n: 78
};

interface KeyOptions { shift?: boolean; ctrl?: boolean; repeat?: boolean; noUp?: boolean }

/** A key as a browser sends it to `el` (UI5 reads keyCode/which, the app's own handlers `key`). */
function key(el: Element, name: string, options: KeyOptions = {}): void {
    const w = win() as unknown as { KeyboardEvent: typeof KeyboardEvent };
    const send = (type: string): void => {
        const event = new w.KeyboardEvent(type, {
            key: name, code: name === " " ? "Space" : name, bubbles: true, cancelable: true,
            shiftKey: !!options.shift, ctrlKey: !!options.ctrl, repeat: !!options.repeat
        });
        const code = KEY_CODES[name] ?? name.toUpperCase().charCodeAt(0);
        Object.defineProperty(event, "keyCode", { get: () => code });
        Object.defineProperty(event, "which", { get: () => code });
        el.dispatchEvent(event);
    };
    send("keydown");
    if (!options.noUp) {
        send("keyup");
    }
}

/** Presses `name` on whatever has the focus once `ready()` holds. */
function iPressKey(When: Common, name: string, ready: () => boolean = () => true, options: KeyOptions = {}): void {
    When.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!active() && ready(),
        success: () => key(active()!, name, options),
        errorMessage: `Cannot press ${name} (focus on ${describe(active())})`
    });
}

/** Moves the focus to `find()` (what Tab or an arrow key would do; the order itself is asserted separately). */
function iFocus(When: Common, find: () => HTMLElement | null | undefined, what: string): void {
    When.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!find(),
        success: () => {
            find()!.focus();
            Opa5.assert.strictEqual(active(), find(), `the focus is on ${what}`);
        },
        errorMessage: `No ${what} to focus`
    });
}

function describe(el: Element | null | undefined): string {
    return el ? `${el.tagName}#${el.id}.${(el as HTMLElement).className}` : "nothing";
}

/** The focus is on what `find()` answers (waits for it). */
function theFocusIsOn(Then: Common, find: () => Element | null | undefined, what: string): void {
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!find() && active() === find(),
        success: () => Opa5.assert.ok(true, `the focus is on ${what}`),
        error: () => Opa5.assert.ok(false, `the focus is not on ${what} but on ${describe(active())}`),
        errorMessage: `The focus is not on ${what}`
    });
}

// A form field sits in a grid cell whose id also ends in the field's id ("...-wrapperfor-<id>"): the control, not the cell.
const byId = (suffix: string): HTMLElement | null => win().document.querySelector<HTMLElement>(`[id$='--${suffix}']:not([id*='-wrapperfor-'])`);
const inner = (suffix: string, tag: string): HTMLElement | null => byId(suffix)?.querySelector<HTMLElement>(tag) ?? null;
const hash = (): string => HashChanger.getInstance().getHash();
const count = (key: string): number => backend.requests.filter((k) => k === key).length;

function theHashMatches(Then: Common, pattern: RegExp, what: string): void {
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => pattern.test(hash()),
        success: () => Opa5.assert.ok(true, what),
        errorMessage: `The hash is ${hash()}, not ${String(pattern)}`
    });
}

function wasAnnounced(Then: Common, text: string): void {
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false,
        check: () => announced.includes(text),
        success: () => Opa5.assert.ok(true, `announced: "${text}"`),
        error: () => Opa5.assert.ok(false, `"${text}" was not announced; announced: ${JSON.stringify(announced)}`),
        errorMessage: `"${text}" was not announced`
    });
}

function waitMs(Then: Common, ms: number): void {
    let start = 0;
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false,
        check: () => { start ||= Date.now(); return Date.now() - start >= ms; },
        success: () => Opa5.assert.ok(true, `${ms} ms later`)
    });
}

/** The session is in propose with one proposed class (revision 1) and nothing else in the workspace. */
function seedFlow(fake: FakeBackend): void {
    fake.dataOf(fake.sessions[0].session.id)!.files.length = 0;
    seedChanges(fake, 1);
}

const popover = (): HTMLElement | null => byId("commentPopover");
const popoverOpen = (): boolean => !!popover() && popover()!.offsetParent !== null;
const draft = (): HTMLElement | null => inner("commentDraft", "textarea");

/** The comment popover is open with the focus in its draft; it is a named dialog. */
function thePopoverHasTheFocus(Then: Common, title: string): void {
    theFocusIsOn(Then, draft, `the draft of "${title}"`);
    Then.waitFor({
        id: "commentPopover", ...SOPTS, searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Popover).getTitle() === title,
        success: (c: UI5Element) => {
            const root = (c as Popover).getDomRef()!;
            const name = (root.getAttribute("aria-labelledby") ?? "").split(" ")
                .map((id) => win().document.getElementById(id)?.textContent ?? "").join(" ").trim();
            Opa5.assert.strictEqual(root.getAttribute("role"), "dialog", "the popover is a dialog");
            Opa5.assert.strictEqual(name, title, "named by its title (also when the focus is parked on its root while saving)");
            Opa5.assert.strictEqual(root.getAttribute("tabindex"), "-1", "its root can hold the focus");
        },
        errorMessage: `The popover is not "${title}"`
    });
}

function iTypeInto(When: Common, id: string, text: string): void {
    When.waitFor({ id, ...SOPTS, searchOpenDialogs: id !== "chatInput", actions: new EnterText({ text, keepFocus: true }), errorMessage: `No ${id}` });
}

// --- The change flow ------------------------------------------------------------------

opaTest("Keyboard only: worklist -> session -> document -> message -> block comment -> request changes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => { seedDoc(fake, 4); });
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => recordAnnouncements() });
    // Worklist: the table is one tab stop; Enter on a row opens the session.
    iFocus(When, () => win().document.querySelector<HTMLElement>("tr[id*='--worklistRow-']"), "the session row");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!win().document.querySelector("tr[id*='--worklistRow-']"),
        success: () => {
            const row = win().document.querySelector("tr[id*='--worklistRow-']")!;
            const name = (row.getAttribute("aria-labelledby") ?? "").split(" ")
                .map((id) => win().document.getElementById(id)?.textContent ?? "").join(" ");
            Opa5.assert.ok(name.includes("Change") && name.includes("Waiting for you") && name.includes("Document to approve"),
                `the row names type, status and reason as text: ${name.slice(0, 160)}`);
            Opa5.assert.notOk(/Indication Color|Warning issued|Entry successfully validated|Informative entry/.test(name),
                "no colour or value-state text is read besides them");
        }
    });
    iPressKey(When, "Enter");
    theHashMatches(Then, /^sessions\/s-1$/, "Enter opened the session");
    theFocusIsOn(Then, () => byId("backToWorklist"), "the breadcrumb at the top of the session page");
    Then.waitFor({
        id: "sessionType", ...SOPTS,
        success: (c: UI5Element) => {
            Opa5.assert.strictEqual(c.getDomRef()!.textContent!.trim(), "Change", "the type badge says its type and nothing else");
        }
    });
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!win().document.querySelector("[id$='--stageTimeline'] [aria-current='step']"),
        success: () => {
            const tokens = Array.from(win().document.querySelectorAll("[id*='--stageToken-'].sapMObjStatus"))
                .map((t) => `${t.querySelector(".sapMObjStatusText")?.textContent ?? ""}: ${t.querySelector("[id$='-state-text']")?.textContent ?? ""}`);
            Opa5.assert.deepEqual(tokens, ["Chat: done", "Design: current stage", "Plan: upcoming", "Changes: upcoming", "Review: upcoming"],
                "each stage token says its state as text (not by colour, icon or tooltip alone)");
        }
    });
    // The current stage's token opens its document (Enter on the one focusable token).
    iFocus(When, () => win().document.querySelector<HTMLElement>("[id*='--stageToken-'].sapMObjStatus[tabindex='0']"), "the Design token");
    iPressKey(When, "Enter");
    theHashMatches(Then, /view=document&kind=design&version=1/, "Enter on the token opened design v1");
    theFocusIsOn(Then, () => byId("artifactTitle"), "the document column's title");
    // A message, sent with Ctrl+Enter from the composer: the new version replaces the shown one and is announced.
    iFocus(When, () => inner("chatInput", "textarea"), "the composer");
    iTypeInto(When, "chatInput", "Name the table in the design.");
    iPressKey(When, "Enter", () => active() === inner("chatInput", "textarea"), { ctrl: true });
    wasAnnounced(Then, "Design v2 is shown.");
    wasAnnounced(Then, "The assistant has answered.");
    theHashMatches(Then, /view=document&kind=design&version=2/, "the new version is shown");
    theFocusIsOn(Then, () => inner("chatInput", "textarea"), "the composer still (the switch did not move the focus)");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!block(1),
        success: () => {
            Opa5.assert.notStrictEqual(active(), win().document.body, `the focus is not lost to the page body (${describe(active())})`);
            const stops = Array.from(win().document.querySelectorAll(".ideDocBlock[tabindex='0']"));
            Opa5.assert.ok(stops.length === 1 && stops[0] === block(0), "the document is one tab stop: its first block");
        }
    });
    // A block comment: Down to block 2, Enter, type, Save with Enter; the focus returns to the block.
    iFocus(When, () => block(0), "block 1");
    iPressKey(When, "ArrowDown");
    theFocusIsOn(Then, () => block(1), "block 2");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        success: () => {
            const s = win().getComputedStyle(block(1)!);
            Opa5.assert.notStrictEqual(s.outlineStyle, "none", `the focused block shows a ring (${s.outlineStyle} ${s.outlineWidth})`);
        }
    });
    iPressKey(When, "Enter");
    thePopoverHasTheFocus(Then, "Comments on block 2");
    iTypeInto(When, "commentDraft", "Say which table.");
    iFocus(When, () => byId("commentSave"), "Save");
    iPressKey(When, "Enter");
    theFocusIsOn(Then, () => block(1), "block 2 again (the popover returned it)");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !popoverOpen() && (block(1)!.getAttribute("aria-label") ?? "").includes("1 comment (Open)"),
        success: () => Opa5.assert.ok(true, "the block's name says it has an open comment"),
        errorMessage: "The block is not named with its comment"
    });
    wasAnnounced(Then, "Comment saved.");
    // Request changes: Escape closes the dialog and returns the focus; Enter on Send starts the run.
    iFocus(When, () => byId("requestChangesButton"), "Request changes (1)");
    iPressKey(When, "Enter");
    theFocusIsOn(Then, () => inner("requestChangesNote", "textarea"), "the note of the dialog");
    Then.waitFor({
        id: "requestChangesDialog", ...SOPTS, searchOpenDialogs: true,
        success: (c: UI5Element) => {
            const root = (c as Dialog).getDomRef()!;
            const described = (root.getAttribute("aria-describedby") ?? "").split(" ")
                .map((id) => win().document.getElementById(id)?.textContent ?? "").join(" ");
            Opa5.assert.ok(described.includes("sent to the assistant"), `the dialog is described by what it sends: "${described}"`);
        }
    });
    iPressKey(When, "Escape");
    theFocusIsOn(Then, () => byId("requestChangesButton"), "Request changes again after Escape");
    iPressKey(When, "Enter");
    theFocusIsOn(Then, () => inner("requestChangesNote", "textarea"), "the note of the dialog");
    iTypeInto(When, "requestChangesNote", "Keep the rounding.");
    iFocus(When, () => byId("requestChangesSend"), "Send");
    iPressKey(When, "Enter");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => backend.dataOf("s-1")!.comments.length === 1 && backend.dataOf("s-1")!.comments.every((c) => c.state === "addressed") && announced.filter((a) => a === "The assistant has answered.").length === 2,
        success: () => {
            Opa5.assert.strictEqual(count("POST sessions/s-1/request-changes"), 1, "one request was sent");
            Opa5.assert.notStrictEqual(active(), win().document.body, `the focus is not lost after the dialog (${describe(active())})`);
        },
        errorMessage: "The request-changes run did not finish"
    });
    Then.iStopTheApp();
});

opaTest("Keyboard only: changes view -> next change -> line comment -> dismiss -> approve from the changes view", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedFlow(fake); });
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => recordAnnouncements() });
    // The cards are still loading when the keyboard user reaches Approve: an Enter there is not an approve.
    iFocus(When, () => byId("primaryAction"), "Review changes");
    When.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        success: () => { release = backend.hold("GET sessions/s-1/file"); }
    });
    iPressKey(When, "Enter");
    theHashMatches(Then, /view=changes/, "Review changes opened the changes view");
    theFocusIsOn(Then, () => byId("artifactTitle"), "the column title (not the approve button, so a held Enter cannot approve)");
    iPressKey(When, "Enter", () => true, { repeat: true, noUp: true });
    iFocus(When, () => byId("primaryAction"), "the primary action while the cards load");
    iPressKey(When, "Enter");
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => release!() });
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!diff()?.querySelector("[tabindex='0']"),
        success: () => Opa5.assert.strictEqual(count("POST sessions/s-1/approve"), 0, "nothing was approved while the cards loaded"),
        errorMessage: "The diff did not appear"
    });
    // N goes to the first change; Down to the added line; Space selects it; Enter comments.
    iFocus(When, () => diff()!.querySelector<HTMLElement>("[tabindex='0']"), "the diff's one tab stop");
    iPressKey(When, "n");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => active()?.getAttribute("data-line") === "12",
        success: () => Opa5.assert.ok(true, `N moved to the first change (${active()!.getAttribute("aria-label")!})`),
        errorMessage: "N did not move to the first change"
    });
    When.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!active(),
        success: () => { if (active()!.getAttribute("data-side") === "old") { key(active()!, "ArrowDown"); } }
    });
    theFocusIsOn(Then, () => newRow(12), "added line 12");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        success: () => {
            const row = newRow(12)!;
            const s = win().getComputedStyle(row);
            Opa5.assert.notStrictEqual(s.outlineStyle, "none", `the focused line shows a ring (${s.outlineStyle})`);
            Opa5.assert.ok(row.getAttribute("aria-label")!.startsWith("Added line 12 of"), "the line's name says it was added");
            Opa5.assert.ok(!!row.querySelector("ins") && row.querySelector(".ideDiffMark")!.textContent!.includes("+"),
                "added is told by <ins> and a + marker, not by colour");
            const removed = diff()!.querySelector("tr[data-side='old'][data-line='12']")!;
            Opa5.assert.ok(!!removed.querySelector("del") && removed.querySelector(".ideDiffMark")!.textContent!.includes("−"),
                "removed is told by <del> and a − marker");
        }
    });
    iPressKey(When, " ");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => newRow(12)!.getAttribute("aria-selected") === "true",
        success: () => Opa5.assert.ok(true, "Space selected the line (aria-selected, not only a colour)"),
        errorMessage: "Space did not select the line"
    });
    iPressKey(When, "Enter");
    thePopoverHasTheFocus(Then, "Comments on line 12");
    iTypeInto(When, "commentDraft", "Name the constant.");
    iFocus(When, () => byId("commentSave"), "Save");
    iPressKey(When, "Enter");
    theFocusIsOn(Then, () => newRow(12), "line 12 again (the popover returned it)");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => backend.dataOf("s-1")!.comments.some((c) => c.anchor === "file" && c.path === X && c.line_start === 12),
        success: () => Opa5.assert.ok(true, "the line comment is stored")
    });
    // An open comment blocks approve; the reason is visible text that the button names as its description.
    Then.waitFor({
        id: "primaryReason", ...SOPTS,
        check: (c: UI5Element) => !!(c as unknown as { getText(): string }).getText(),
        success: (c: UI5Element) => {
            const button = byId("primaryAction")!;
            Opa5.assert.ok(button.getAttribute("aria-describedby")!.split(" ").includes(c.getId()), "the disabled button is described by the reason");
            Opa5.assert.ok(true, `reason: ${(c as unknown as { getText(): string }).getText()}`);
        },
        errorMessage: "No visible reason while a comment is open"
    });
    // Dismiss it from the popover with the keyboard: Enter on the line, then Enter on Dismiss, then Escape.
    iFocus(When, () => newRow(12), "line 12");
    iPressKey(When, "Enter");
    thePopoverHasTheFocus(Then, "Comments on line 12");
    // The comment is a list row: F2 moves into it (Edit first), Tab then reaches Dismiss.
    iFocus(When, () => win().document.querySelector<HTMLElement>("li[id*='--commentPopoverItem-']"), "the comment's row");
    iPressKey(When, "F2");
    theFocusIsOn(Then, () => win().document.querySelector("button[id*='--commentEdit-']"), "Edit (F2)");
    iFocus(When, () => win().document.querySelector<HTMLElement>("button[id*='--commentDismiss-']"), "Dismiss (Tab within the row)");
    iPressKey(When, "Enter");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => backend.dataOf("s-1")!.comments.every((c) => c.state === "dismissed") && popoverOpen() && !!active() && active() !== win().document.body,
        success: () => Opa5.assert.ok(true, `dismissed; the focus stays in the popover (${describe(active())})`),
        errorMessage: "The comment was not dismissed or the focus was lost"
    });
    iPressKey(When, "Escape", () => active() !== draft());
    iPressKey(When, "Escape", () => true);
    theFocusIsOn(Then, () => newRow(12), "line 12 after Escape");
    // Approve from the changes view with Enter.
    Then.waitFor({
        id: "primaryAction", ...SOPTS,
        check: (c: UI5Element) => (c as Button).getEnabled() && (c as Button).getText() === "Approve changes and review",
        success: () => Opa5.assert.ok(true, "Approve changes is enabled"),
        errorMessage: "Approve changes is not enabled"
    });
    iFocus(When, () => byId("primaryAction"), "Approve changes and review");
    iPressKey(When, "Enter");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => backend.dataOf("s-1")!.session.stage === "review",
        success: () => {
            Opa5.assert.strictEqual(count("POST sessions/s-1/approve"), 1, "one approve, from the changes view");
            Opa5.assert.notStrictEqual(active(), win().document.body, `the focus is not lost (${describe(active())})`);
        },
        errorMessage: "Not approved"
    });
    Then.iStopTheApp();
});

opaTest("Keyboard only: an approve pressed while the cards load is not sent and is announced", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedFlow(fake); });
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => recordAnnouncements() });
    iFocus(When, () => byId("primaryAction"), "Review changes");
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => { release = backend.hold("GET sessions/s-1/file"); } });
    iPressKey(When, "Enter");
    theHashMatches(Then, /view=changes/, "the changes view opens; the cards wait for their file");
    iFocus(When, () => byId("primaryAction"), "the primary action while the cards load");
    // Enter goes down while the cards are not there; it is still held when they appear.
    iPressKey(When, "Enter", () => (byId("primaryAction") as HTMLButtonElement).disabled === false, { noUp: true });
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => release!() });
    waitMs(Then, 1000);
    iPressKey(When, "Enter", () => true, { repeat: true, noUp: true });
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false,
        check: () => announced.some((a) => a.startsWith("Not approved: the changes have only just appeared")) || count("POST sessions/s-1/approve") > 0,
        success: () => {
            Opa5.assert.strictEqual(count("POST sessions/s-1/approve"), 0, "the held key did not approve");
            Opa5.assert.ok(true, "the ignored press was announced");
        },
        errorMessage: "The ignored press was not announced"
    });
    Then.iStopTheApp();
});

// --- Diagnose ------------------------------------------------------------------------

const POOL = "ZCL_ORDER_QUERY".padEnd(30, "=") + "CP";
const SAP_SOURCE = Array.from({ length: 30 }, (_, i) => `* sap line ${i + 1}`).join("\n");

function seedDiagnose(fake: FakeBackend): string {
    fake.allowDiagnose("DEMO");
    const s = fake.addSession("Why is the order list slow?", [], [], "diagnose");
    s.target = "DEMO";
    s.request_cap = 1000;
    const data = fake.dataOf(s.id)!;
    data.findings.push({
        id: "f-1", kind: "dump", ref_id: "DUMP-1", title: "TSV_TNEW_PAGE_ALLOC_FAILED", program: POOL, include: null, line: 12,
        occurred_at: "2026-10-03T13:52:00", created_at: "2026-10-03T13:53:00"
    });
    data.files.push({ path: "src/CLAS/zcl_order_query.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_ORDER_QUERY", origin_source: SAP_SOURCE, proposed_source: "" });
    fake.addApproval(s.id, {});
    return s.id;
}

opaTest("Keyboard only (diagnose): finding -> source -> details -> trace approval decided with no default action", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    const findingRow = (): HTMLElement | null => win().document.querySelector<HTMLElement>("li[id*='--findingItem-']");
    const card = (): HTMLElement | null => win().document.querySelector<HTMLElement>("li[id*='--messageItem-']");
    Given.iStartTheApp("", undefined, (fake) => { sid = seedDiagnose(fake); });
    When.waitFor({ id: "toolPage", viewName: "App", visible: false, success: () => { recordAnnouncements(); HashChanger.getInstance().setHash(`sessions/${sid}?view=findings`); } });
    Then.waitFor({
        id: "sessionType", ...SOPTS,
        check: (c: UI5Element) => !!c.getDomRef() && c.getDomRef()!.textContent!.includes("Diagnose"),
        success: (c: UI5Element) => Opa5.assert.strictEqual(c.getDomRef()!.textContent!.trim(), "Diagnose",
            "the type badge reads \"Diagnose\", not \"Diagnose Indication Color 3\""),
        errorMessage: "No diagnose badge"
    });
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!findingRow(),
        success: () => {
            const text = findingRow()!.textContent!;
            Opa5.assert.ok(text.includes("Dump") && !/Indication Color/.test(text), "the finding's kind is its text, no colour name");
            const list = byId("findingsList")!;
            const name = (list.querySelector("[role='list'], ul")?.getAttribute("aria-labelledby") ?? list.getAttribute("aria-labelledby") ?? "")
                .split(" ").map((id) => win().document.getElementById(id)?.textContent ?? "").join(" ");
            Opa5.assert.ok(name.includes("F2"), `the list names the F2 pattern: "${name}"`);
            Opa5.assert.ok(byId("findingsKeyHint")!.offsetParent !== null, "and shows it as visible text");
        },
        errorMessage: "No finding row"
    });
    // Enter on the row opens the SAP source at the line.
    iFocus(When, findingRow, "the finding row");
    iPressKey(When, "Enter");
    theHashMatches(Then, /view=source.*line=12/, "Enter opened the source at line 12");
    theFocusIsOn(Then, () => byId("artifactTitle"), "the source column's title");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!win().document.querySelector(".ideSourceHighlight[data-line='12'], .ideSourceHighlight"),
        success: () => {
            const row = win().document.querySelector<HTMLElement>(".ideSourceHighlight")!;
            const code = row.querySelector(".ideSourceCode")!;
            Opa5.assert.notStrictEqual(win().getComputedStyle(code).boxShadow, "none", "the line is marked by a bar, not only a background");
        },
        errorMessage: "No highlighted line"
    });
    // Back to the findings; F2 moves into the row, Enter opens the details, Escape returns the focus to Details.
    iFocus(When, () => byId("sourceBackToFindings"), "All findings");
    iPressKey(When, "Enter");
    theHashMatches(Then, /view=findings/, "back on the findings");
    iFocus(When, findingRow, "the finding row");
    iPressKey(When, "F2");
    theFocusIsOn(Then, () => win().document.querySelector("button[id*='--findingDetails-']"), "Details (F2)");
    iPressKey(When, "Enter");
    theFocusIsOn(Then, () => byId("findingDetailCloseButton"), "Close in the details dialog");
    iPressKey(When, "Escape");
    theFocusIsOn(Then, () => win().document.querySelector("button[id*='--findingDetails-']"), "Details again after Escape");
    // The trace approval: the card says how to reach its buttons; Enter on the row decides nothing; F2 reaches them.
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => !!card() && !!win().document.querySelector("button[id*='--approveTraceButton-']"),
        success: () => {
            const approve = win().document.querySelector<HTMLElement>("button[id*='--approveTraceButton-']")!;
            const deny = win().document.querySelector<HTMLElement>("button[id*='--denyTraceButton-']")!;
            const buttons = [approve, deny].map((b) => (CoreElement.closestTo(b) as unknown as Button).getType());
            Opa5.assert.notOk(buttons.includes("Emphasized"), `no button is the default (${buttons.join(", ")})`);
            Opa5.assert.ok(card()!.textContent!.includes("F2"), "the card says that F2 reaches Approve and Reject");
        },
        errorMessage: "No pending approval card"
    });
    iFocus(When, card, "the approval card's row");
    iPressKey(When, "Enter");
    iPressKey(When, " ");
    waitMs(Then, 300);
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        success: () => Opa5.assert.strictEqual(backend.requests.filter((k) => k.startsWith(`POST sessions/${sid}/approvals/`)).length, 0,
            "Enter and Space on the card decide nothing")
    });
    iPressKey(When, "F2");
    theFocusIsOn(Then, () => win().document.querySelector("button[id*='--approveTraceButton-']"), "Approve trace (F2)");
    iFocus(When, () => win().document.querySelector<HTMLElement>("button[id*='--denyTraceButton-']"), "Reject (the next Tab stop)");
    iPressKey(When, "Enter");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => backend.dataOf(sid)!.approvals[0].status === "denied",
        success: () => {
            Opa5.assert.notStrictEqual(active(), win().document.body, `the focus is not lost after the decision (${describe(active())})`);
            Opa5.assert.ok(announced.some((a) => a.startsWith("Request rejected")), "the decision is announced");
            const line = win().document.querySelector("[id*='--approvalDecided-']:not([style*='display: none'])");
            Opa5.assert.ok(!!line && !/Indication Color|Warning issued|Entry successfully|Invalid entry|Informative entry/.test(line.textContent!),
                `the decided line says its state as text only: ${line?.textContent ?? "-"}`);
        },
        errorMessage: "The approval was not rejected"
    });
    Then.iStopTheApp();
});

// --- Conventions: the non-production flag ----------------------------------------------

opaTest("Keyboard only (conventions): the flag's confirmation starts on Cancel, Enter cancels, the focus returns to the switch", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => { fake.isAdmin = true; });
    iFocus(When, () => byId("conventionsButton"), "Conventions");
    iPressKey(When, "Enter");
    When.waitFor({ id: "convNonProduction", ...D, success: () => Opa5.assert.ok(true, "the dialog is open") });
    iFocus(When, () => byId("convNonProduction"), "the non-production switch");
    iPressKey(When, " ");
    Then.waitFor({
        controlType: "sap.m.Dialog", searchOpenDialogs: true,
        check: (list: UI5Element[]) => list.some((d) => (d as Dialog).getTitle() === "Mark as Non-Production System"),
        success: (list: UI5Element[]) => {
            const box = list.find((d) => (d as Dialog).getTitle() === "Mark as Non-Production System") as Dialog;
            const buttons = box.getButtons() as Button[];
            Opa5.assert.notOk(buttons.some((b) => b.getType() === "Emphasized"), "no emphasized (default) action");
            Opa5.assert.strictEqual(active(), (buttons.find((b) => b.getText() === "Cancel")!).getFocusDomRef(), "the focus starts on Cancel");
        },
        errorMessage: "No confirmation"
    });
    iPressKey(When, "Enter");
    theFocusIsOn(Then, () => byId("convNonProduction"), "the switch again");
    Then.waitFor({
        id: "convNonProduction", ...APP, searchOpenDialogs: true,
        success: (c: UI5Element) => {
            Opa5.assert.strictEqual((c as Switch).getState(), false, "the switch is back off");
            Opa5.assert.strictEqual(backend.requests.filter((k) => k.startsWith("PUT conventions")).length, 0, "nothing was sent");
            Opa5.assert.notOk(backend.conventions.find((c2) => c2.target === "dev-system")!.non_production, "the flag is unchanged");
        }
    });
    Then.iStopTheApp();
});

// --- High contrast ---------------------------------------------------------------------

opaTest("Theme parameters are published when the app starts on the worklist (not only once a session was opened)", function (Given: Common, When: Common, Then: Common) {
    When.waitFor({
        success: () => {
            const style = win().document.documentElement.style;
            ["sapContent_LabelColor", "sapFontSmallSize"].forEach((name) => style.removeProperty(`--${name}`));
        }
    });
    Given.iStartTheApp("");
    Then.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        check: () => win().document.documentElement.style.getPropertyValue("--sapContent_LabelColor") !== "",
        success: () => Opa5.assert.ok(true, "the worklist's reason line gets the theme's label colour"),
        errorMessage: "--sapContent_LabelColor is not set on the worklist"
    });
    Then.iStopTheApp();
});

function iApplyTheme(When: Common, theme: string): void {
    let applied = false;
    When.waitFor({
        id: "toolPage", viewName: "App", visible: false,
        success: () => {
            const done = (): void => { applied = Theming.getTheme() === theme; };
            Theming.attachApplied(done);
            Theming.setTheme(theme);
        }
    });
    When.waitFor({
        id: "toolPage", viewName: "App", visible: false, autoWait: false, timeout: 30,
        check: () => applied && win().getComputedStyle(win().document.documentElement).getPropertyValue("--sapContent_FocusColor").trim() !== "",
        success: () => Opa5.assert.ok(true, `${theme} is applied`),
        errorMessage: `${theme} was not applied`
    });
}

[["sap_horizon_hcb", "#ffffff"], ["sap_horizon_hcw", "#000000"]].forEach(([theme, focus]) => {
    opaTest(`High contrast (${theme}): no information is carried by a colour alone`, function (Given: Common, When: Common, Then: Common) {
        Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
            seedFlow(fake);
            fake.scriptSyntax(X, "errors", [{ line: 13, message: "Field LV_DECIMALS is unknown.", severity: "error" }]);
            fake.dataOf("s-1")!.files[0].revisions![0].syntax_status = "errors";
            fake.dataOf("s-1")!.files[0].revisions![0].syntax = [{ line: 13, message: "Field LV_DECIMALS is unknown.", severity: "error" }];
        });
        iApplyTheme(When, theme);
        Then.waitFor({
            id: "toolPage", viewName: "App", visible: false,
            check: () => !!newRow(12) && !!diff()?.querySelector("[tabindex='0']"),
            success: () => {
                const root = win().getComputedStyle(win().document.documentElement);
                Opa5.assert.strictEqual(root.getPropertyValue("--sapContent_FocusColor").trim().toLowerCase(), focus,
                    "the app's CSS variables follow the high-contrast theme");
                Opa5.assert.notStrictEqual(root.getPropertyValue("--sapSelectedColor").trim(), "", "the selection bar colour is the theme's");
                const removed = diff()!.querySelector("tr[data-side='old'][data-line='12'] del")!;
                Opa5.assert.strictEqual(win().getComputedStyle(removed).textDecorationLine, "line-through", "a removed line is struck through");
                const mark = newRow(12)!.querySelector(".ideDiffMark")!.textContent!;
                Opa5.assert.ok(mark.includes("+"), "an added line has the + marker");
                const syntax = newRow(13)!;
                Opa5.assert.ok((syntax.querySelector(".ideDiffSyntaxMark")?.textContent ?? "").trim() !== "", "a syntax error has a glyph");
                Opa5.assert.notStrictEqual(win().getComputedStyle(syntax.querySelector(".ideDiffCode")!).textDecorationLine, "none",
                    "and an underline");
                Opa5.assert.ok((syntax.getAttribute("aria-describedby") ?? "").split(" ").some((id) => (win().document.getElementById(id)?.textContent ?? "").includes("LV_DECIMALS")),
                    "the message is the line's description, not only a tooltip");
            },
            errorMessage: "The diff is not shown"
        });
        iFocus(When, () => newRow(12), "added line 12");
        iPressKey(When, " ");
        Then.waitFor({
            id: "toolPage", viewName: "App", visible: false,
            check: () => newRow(12)!.getAttribute("aria-selected") === "true",
            success: () => {
                const code = win().getComputedStyle(newRow(12)!.querySelector(".ideDiffCode")!);
                Opa5.assert.notStrictEqual(code.boxShadow, "none", `a selected line has a bar (${code.boxShadow})`);
                const ring = win().getComputedStyle(newRow(12)!);
                Opa5.assert.notStrictEqual(ring.outlineStyle, "none", `the focus ring shows (${ring.outlineStyle} ${ring.outlineColor})`);
                const status = byId("changesSyntax-agentide---session--changesObjects-0") ?? win().document.querySelector("[id*='--changesSyntax-']");
                Opa5.assert.ok(!!status && !/Invalid entry|Warning issued|Indication Color/.test(status.textContent!) && status.textContent!.trim() !== "",
                    `the syntax state is text: ${status?.textContent ?? "-"}`);
            },
            errorMessage: "Space did not select"
        });
        iApplyTheme(When, "sap_horizon");
        Then.iStopTheApp();
    });
});

// --- m5 (final review): a test that leaves a high-contrast theme on does not leak it ---------

opaTest("A test that ends in a high-contrast theme (as a failing one would) leaves it on", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("");
    iApplyTheme(When, "sap_horizon_hcb");
    // No switch back here: the module's afterEach restores the theme, whatever the test did.
    Then.iStopTheApp();
});

opaTest("The next test starts in sap_horizon again (the module restores the theme after each test)", function (Given: Common, When: Common, Then: Common) {
    // Asserted before this test starts the app: it holds even when the test before failed without its teardown.
    Then.waitFor({
        success: () => {
            Opa5.assert.strictEqual(Theming.getTheme(), "sap_horizon", "the theme is sap_horizon");
            Opa5.assert.notStrictEqual(
                win().getComputedStyle(win().document.documentElement).getPropertyValue("--sapContent_FocusColor").trim().toLowerCase(),
                "#ffffff", "and its parameters are applied, not hcb's");
        }
    });
    Given.iStartTheApp("");
    Then.iStopTheApp();
});
