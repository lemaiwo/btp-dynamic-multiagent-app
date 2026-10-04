import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Link from "sap/m/Link";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Popover from "sap/m/Popover";
import type Select from "sap/m/Select";
import type Text from "sap/m/Text";
import type Title from "sap/m/Title";
import type View from "sap/ui/core/mvc/View";
import type Controller from "sap/ui/core/mvc/Controller";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, thePrimaryActionIs, theReasonIs } from "./pages/Session";
import { announced, closeMessageBox, recordAnnouncements } from "./pages/Shared";
import type FakeBackend from "./FakeBackend";
import type { Comment } from "../../service/types";

/**
 * Task U10: the changes view. Proposed objects with their base, SAP version,
 * lint and syntax states; stacked diffs; next/previous change; line
 * comments by keyboard and mouse; Request changes and the next revision;
 * Check syntax; Open in ADT; the source view at a line; the approve gate.
 */
QUnit.module("Changes review");

const X = "src/CLAS/zcl_x.clas.abap";
const I = "src/INTF/zif_x.intf.abap";
const R = "src/PROG/zx_report.prog.abap";
const HOSTILE = "<img src=x onerror=alert(1)> {/session/title}";

const OLD_X = Array.from({ length: 40 }, (_, i) => {
    const n = i + 1;
    if (n === 1) {
        return "CLASS zcl_x IMPLEMENTATION.";
    }
    if (n === 11) {
        return "  METHOD round.";
    }
    if (n === 12) {
        return "    rv_amount = round( val = iv_amount dec = 2 ).";
    }
    if (n === 13) {
        return "  ENDMETHOD.";
    }
    if (n === 30) {
        return "    lv_total = lv_total + 1.";
    }
    if (n === 40) {
        return "ENDCLASS.";
    }
    return `    " step ${n}`;
}).join("\n");

const NEW_X = OLD_X
    .replace("    rv_amount = round( val = iv_amount dec = 2 ).",
        "    DATA(lv_decimals) = cl_exchange_rates=>get_currency_decimals( iv_currency ).\n"
        + "    rv_amount = round( val = iv_amount dec = lv_decimals ).")
    .replace("    lv_total = lv_total + 1.", `    lv_total = lv_total + 2. " ${HOSTILE}`);

const NEW_I = "INTERFACE zif_x PUBLIC.\n  METHODS get RETURNING VALUE(r) TYPE i.\nENDINTERFACE.";
const NEW_R = "REPORT zx_report.\nWRITE 'x'.";

interface SeedOptions {
    comments?: Comment[];
    /** Extra proposed file (path, origin, proposal). */
    extra?: { path: string; type: string; name: string; origin: string; proposal: string };
}

/** A propose-stage session on target DEMO with a changed, a new and an unchecked object. */
function seed(fake: FakeBackend, options: SeedOptions = {}): string {
    const s = fake.sessions[0].session;
    s.stage = "propose";
    s.target = "DEMO";
    s.request_cap = 1000;
    const data = fake.dataOf(s.id)!;
    data.files.push(
        { path: X, state: "modified", object_type: "CLAS", object_name: "ZCL_X", origin_source: OLD_X, proposed_source: "", origin_version: "00042" },
        { path: I, state: "new", object_type: "INTF", object_name: "ZIF_X", origin_source: "", proposed_source: "" },
        { path: R, state: "new", object_type: "PROG", object_name: "ZX_REPORT", origin_source: "", proposed_source: "", base_status: "unknown" }
    );
    if (options.extra) {
        const e = options.extra;
        data.files.push({ path: e.path, state: "modified", object_type: e.type, object_name: e.name, origin_source: e.origin, proposed_source: "" });
    }
    fake.addRevision(s.id, X, NEW_X);
    fake.addRevision(s.id, I, NEW_I);
    fake.addRevision(s.id, R, NEW_R);
    if (options.extra) {
        fake.addRevision(s.id, options.extra.path, options.extra.proposal);
    }
    const rev = (path: string) => data.files.find((f) => f.path === path)!.revisions![0];
    Object.assign(rev(X), {
        syntax_status: "errors", checked_at: "2026-10-03T10:00:00",
        syntax: [
            { line: 13, message: `Field "LV_DECIMALS" is unknown. ${HOSTILE}`, severity: "error" },
            { line: 31, message: "Statement is not well-formed.", severity: "error" }
        ]
    });
    Object.assign(rev(I), { syntax_status: "ok", syntax: [], checked_at: "2026-10-03T10:00:00" });
    Object.assign(rev(R), { syntax_status: "unavailable", syntax: [], checked_at: "2026-10-03T10:00:00" });
    data.comments.push(...(options.comments ?? []));
    return s.id;
}

function lineComment(id: string, start: number, end: number, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "file", path: X, revision: 1, line_start: start, line_end: end,
        kind: null, version: null, paragraph: null, body: `Body ${id}`, state: "open", answer: null, quote: null,
        created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00", ...extra
    };
}

function doc(): Document {
    return Opa5.getWindow().document;
}

function diff(path: string): HTMLElement | null {
    return doc().querySelector<HTMLElement>(`.ideUnified[data-path='${path}']`);
}

function newRow(path: string, line: number): HTMLElement | null {
    return diff(path)?.querySelector<HTMLElement>(`tr[data-side='new'][data-line='${line}']`) ?? null;
}

function active(): HTMLElement | null {
    return doc().activeElement as HTMLElement | null;
}

/** The control with template id `id` in object card `index` of the changes list. */
function inObject(index: number, id: string) {
    return function (control: UI5Element): boolean {
        const ctx = control.getBindingContext("s");
        return control.getId().includes(`--${id}-`) && !!ctx && ctx.getPath() === `/artifact/changes/objects/${index}`;
    };
}

function objectControl(When: Common, index: number, id: string, type: string, success: (c: UI5Element) => void, check?: (c: UI5Element) => boolean): void {
    When.waitFor({
        controlType: type,
        ...SOPTS,
        visible: false,
        matchers: inObject(index, id),
        check: check ? (list: UI5Element[]) => list.length > 0 && check(list[0]) : undefined,
        success: (list: UI5Element[]) => success(list[0]),
        errorMessage: `No ${id} in object ${index}`
    });
}

function theStatusIs(Then: Common, index: number, id: string, text: string, state: string): void {
    objectControl(Then, index, id, "sap.m.ObjectStatus", (c) => {
        const status = c as ObjectStatus;
        Opa5.assert.strictEqual(status.getText(), text, `${id} of object ${index}: "${text}"`);
        Opa5.assert.strictEqual(status.getState(), state, `state ${state}`);
        Opa5.assert.ok(!!status.getIcon(), "with an icon, not colour alone");
    }, (c) => (c as ObjectStatus).getText() === text);
}

function theDiffsAreShown(Then: Common, count: number): void {
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => doc().querySelectorAll(".ideUnified[data-path]").length === count
            && !!diff(X)?.querySelector("[tabindex='0']"),
        success: function () {
            Opa5.assert.ok(true, `${count} diffs are shown`);
        },
        errorMessage: `Not ${count} diffs`
    });
}

function iPressKeyOn(When: Common, find: () => HTMLElement | null, key: string, shift = false): void {
    When.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!find(),
        success: function () {
            const el = find()!;
            el.focus();
            QUnitUtils.triggerKeydown(el, key, shift, false, false);
        },
        errorMessage: `Nothing to press ${key} on`
    });
}

/** Keys on whatever has the focus now (the previous step put it there). */
function iPressKey(When: Common, key: string, shift = false): void {
    iPressKeyOn(When, active, key, shift);
}

function theFocusIs(Then: Common, check: (el: HTMLElement | null) => boolean, message: string): void {
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => check(active()),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `Focus: ${message}`
    });
}

function isRow(path: string, side: "new" | "old", line: number) {
    return (el: HTMLElement | null): boolean => !!el && el.closest(".ideUnified")?.getAttribute("data-path") === path
        && el.getAttribute("data-side") === side && el.getAttribute("data-line") === String(line);
}

function thePopoverIsOpen(Then: Common, title: string): void {
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Popover).isOpen() && (c as Popover).getTitle() === title,
        success: function () {
            Opa5.assert.ok(true, `"${title}" is open`);
        },
        errorMessage: `"${title}" is not open`
    });
}

/** A real click (UI5 dispatches it from its root listener like a user's). */
function click(el: Element, shiftKey = false): void {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, shiftKey }));
}

function lastBody(key: string): Record<string, unknown> | undefined {
    const found = backend.bodies.filter((b) => b.key === key);
    return found[found.length - 1]?.body as Record<string, unknown> | undefined;
}

opaTest("Lists the proposed objects with base, version, lint and syntax states; error lines are marked; ADT link", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake); });
    theDiffsAreShown(Then, 3);
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        success: function (title: UI5Element) {
            Opa5.assert.strictEqual((title as Title).getText(), "Changes", "the column is the changes view");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Title",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesObjectName-"),
        check: (list: UI5Element[]) => list.length === 3,
        success: function (list: UI5Element[]) {
            Opa5.assert.deepEqual(list.map((c) => (c as Title).getText()), ["ZCL_X", "ZIF_X", "ZX_REPORT"], "three objects, in order");
        },
        errorMessage: "Not three objects"
    });
    theStatusIs(Then, 0, "changesBase", "Changed vs SAP version 00042", "None");
    theStatusIs(Then, 0, "changesSyntax", "2 syntax errors", "Error");
    theStatusIs(Then, 0, "changesLint", "Lint not run", "None");
    theStatusIs(Then, 1, "changesBase", "New in SAP", "Information");
    theStatusIs(Then, 1, "changesSyntax", "No syntax messages", "None");
    theStatusIs(Then, 2, "changesBase", "Base not checked", "Warning");
    theStatusIs(Then, 2, "changesSyntax", "Syntax not checked", "Warning");
    Then.waitFor({
        id: "changesSummary",
        ...SOPTS,
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as Text).getProperty("text"), "3 objects, +8 −2 lines", "the summary counts lines");
        }
    });
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        success: function () {
            const root = diff(X)!;
            Opa5.assert.ok(root.querySelector("tr[data-side='old'] del"), "removed lines are struck (not colour alone)");
            Opa5.assert.ok(root.querySelector("tr[data-side='new'] ins"), "added lines are marked");
            [13, 31].forEach((line) => {
                const row = newRow(X, line)!;
                Opa5.assert.ok(row.classList.contains("ideDiffSyntaxError"), `line ${line} is marked as a syntax error`);
                Opa5.assert.ok(row.querySelector(".ideDiffSyntaxMark"), "with a gutter glyph");
                // The line's own description first; the short per-line hint follows it.
                const desc = doc().getElementById((row.getAttribute("aria-describedby") ?? "").split(" ")[0]);
                Opa5.assert.ok(desc?.textContent?.startsWith("Syntax error: "), "and a description");
            });
            Opa5.assert.strictEqual(newRow(X, 13)!.getAttribute("title"), `Field "LV_DECIMALS" is unknown. ${HOSTILE}`, "the message is the tooltip, literally");
            Opa5.assert.ok((newRow(X, 31)!.textContent ?? "").includes(HOSTILE), "source shown literally");
            Opa5.assert.strictEqual(doc().querySelectorAll("[id$='--changesView'] img").length, 0, "no markup from source or messages");
            Opa5.assert.strictEqual(root.querySelectorAll("[tabindex='0']").length, 1, "one tab stop per diff");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesSyntaxMessage-"),
        check: (list: UI5Element[]) => list.length === 2,
        success: function (list: UI5Element[]) {
            Opa5.assert.deepEqual(list.map((c) => (c as Text).getProperty("text")), [
                `Error, line 13: Field "LV_DECIMALS" is unknown. ${HOSTILE}`, "Error, line 31: Statement is not well-formed."
            ], "messages as text, by line");
        },
        errorMessage: "No syntax messages listed"
    });
    objectControl(Then, 0, "changesAdt", "sap.m.Link", (c) => {
        const link = c as Link;
        Opa5.assert.strictEqual(link.getHref(), "adt://DEMO/sap/bc/adt/oo/classes/zcl_x/source/main#start=12,0", "ADT opens the SAP line of the first change");
        Opa5.assert.strictEqual(link.getTarget(), "_blank");
        Opa5.assert.ok(link.getVisible(), "shown for a changed object");
    });
    objectControl(Then, 1, "changesAdt", "sap.m.Link", (c) => {
        Opa5.assert.notOk((c as Link).getVisible(), "no ADT link for an object that is not in SAP yet");
    });
    Then.iStopTheApp();
});

opaTest("Next and previous change by keyboard and buttons; arrows move between lines", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake); });
    theDiffsAreShown(Then, 3);
    Then.waitFor({ success: () => recordAnnouncements() });
    iPressKeyOn(When, () => diff(X)?.querySelector<HTMLElement>("[tabindex='0']") ?? null, "N");
    theFocusIs(Then, (el) => el?.getAttribute("data-stop") === "0" && el.closest(".ideUnified") === diff(X), "N: the first change of ZCL_X");
    iPressKey(When, "N");
    theFocusIs(Then, (el) => el?.getAttribute("data-stop") === "1" && el.closest(".ideUnified") === diff(X), "N: hunk 2 of ZCL_X");
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(announced.includes("Change 2 of 4: ZCL_X, line 31"), "the stop is announced");
        }
    });
    iPressKey(When, "N");
    theFocusIs(Then, (el) => el?.getAttribute("data-stop") === "0" && el.closest(".ideUnified") === diff(I), "N: on to ZIF_X");
    iPressKey(When, "P");
    theFocusIs(Then, (el) => el?.getAttribute("data-stop") === "1" && el.closest(".ideUnified") === diff(X), "P: back to hunk 2");
    iPressKey(When, "ARROW_DOWN");
    theFocusIs(Then, isRow(X, "new", 31), "Down: the next line");
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(diff(X)!.querySelectorAll("[tabindex='0']").length, 1, "still one tab stop");
            Opa5.assert.strictEqual(newRow(X, 31)!.getAttribute("tabindex"), "0", "the stop follows the focus");
        }
    });
    iPress(When, "changesNext", "No Next change button");
    theFocusIs(Then, (el) => el?.getAttribute("data-stop") === "0" && el.closest(".ideUnified") === diff(I), "Next change button");
    iPress(When, "changesPrevious", "No Previous change button");
    theFocusIs(Then, (el) => el?.getAttribute("data-stop") === "1" && el.closest(".ideUnified") === diff(X), "Previous change button");
    Then.iStopTheApp();
});

opaTest("Keyboard-only comment on lines 12-14: Space, Shift+Space, Enter; marker and comment shown; quote sent", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake); });
    theDiffsAreShown(Then, 3);
    iPressKeyOn(When, () => diff(X)?.querySelector<HTMLElement>("[tabindex='0']") ?? null, "N");
    iPressKey(When, "ARROW_DOWN");
    theFocusIs(Then, isRow(X, "new", 12), "on line 12");
    iPressKey(When, "SPACE");
    iPressKey(When, "ARROW_DOWN");
    iPressKey(When, "ARROW_DOWN");
    theFocusIs(Then, isRow(X, "new", 14), "on line 14");
    iPressKey(When, "SPACE", true);
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => Array.from(diff(X)!.querySelectorAll("tr[aria-selected='true']")).map((r) => r.getAttribute("data-line")).join() === "12,13,14",
        success: function () {
            Opa5.assert.ok(newRow(X, 12)!.classList.contains("ideDiffSelected"), "selected rows carry aria-selected and a non-colour cue");
        },
        errorMessage: "Lines 12-14 are not selected"
    });
    iPressKey(When, "ENTER");
    thePopoverIsOpen(Then, "Comments on lines 12–14");
    When.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: `Buffer the decimals per currency. ${HOSTILE}`, keepFocus: true }),
        errorMessage: "No comment text area"
    });
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Save is off" });
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => newRow(X, 12)?.querySelector(".ideDiffCommentMark")?.textContent === "1",
        success: function () {
            const body = lastBody("POST sessions/s-1/comments")!;
            Opa5.assert.deepEqual(
                [body.anchor, body.path, body.revision, body.line_start, body.line_end],
                ["file", X, 1, 12, 14], "the file anchor of the selected lines");
            Opa5.assert.strictEqual(body.quote,
                "DATA(lv_decimals) = cl_exchange_rates=>get_currency_decimals( iv_currency ).", "quote = the first selected line");
            Opa5.assert.strictEqual(newRow(X, 12)!.querySelector(".ideDiffCommentMark")!.getAttribute("aria-label"),
                "Line 12: 1 comment (Open)", "the marker is named");
        },
        errorMessage: "No marker on line 12"
    });
    theFocusIs(Then, isRow(X, "new", 12), "focus back on the first commented line");
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesCommentBody-"),
        check: (list: UI5Element[]) => list.length === 1,
        success: function (list: UI5Element[]) {
            Opa5.assert.strictEqual((list[0] as Text).getProperty("text"), `Buffer the decimals per currency. ${HOSTILE}`, "the comment as text");
            Opa5.assert.strictEqual(doc().querySelectorAll("[id$='--changesView'] img").length, 0, "no markup");
        },
        errorMessage: "The comment is not listed"
    });
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as Button).getText(), "Request changes (1)", "the header counts it");
        }
    });
    Then.iStopTheApp();
});

opaTest("Mouse: click and shift-click select lines; the comment button and a marker open the popover", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake, { comments: [lineComment("c-1", 12, 14)] }); });
    theDiffsAreShown(Then, 3);
    When.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!newRow(X, 31),
        success: function () {
            click(newRow(X, 30)!.querySelector("td.ideDiffCode")!);
            click(newRow(X, 31)!.querySelector("td.ideDiffCode")!, true);
        }
    });
    objectControl(Then, 0, "changesCommentButton", "sap.m.Button", (c) => {
        Opa5.assert.strictEqual((c as Button).getText(), "Comment on lines 30–31");
        (c as Button).firePress({});
    }, (c) => (c as Button).getVisible() && (c as Button).getText() === "Comment on lines 30–31");
    thePopoverIsOpen(Then, "Comments on lines 30–31");
    When.waitFor({ id: "commentCancel", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    When.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!newRow(X, 12)?.querySelector(".ideDiffCommentMark"),
        success: function () {
            click(newRow(X, 12)!.querySelector(".ideDiffCommentMark")!);
        }
    });
    thePopoverIsOpen(Then, "Comments on lines 12–14");
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: (c: UI5Element) => c.getId().includes("commentPopoverBody"),
        check: (list: UI5Element[]) => list.some((c) => (c as Text).getProperty("text") === "Body c-1"),
        success: function () {
            Opa5.assert.ok(true, "the line's comment is in the popover");
        },
        errorMessage: "The comment is not in the popover"
    });
    When.waitFor({ id: "commentCancel", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.iStopTheApp();
});

opaTest("Request changes: Sent, then Addressed on the old revision while the new revision's diff is shown", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seed(fake, { comments: [lineComment("c-1", 12, 14)] });
        release = fake.pauseStream();
    });
    theDiffsAreShown(Then, 3);
    iPress(When, "requestChangesButton");
    When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Send is off" });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesCommentState-"),
        check: (list: UI5Element[]) => list.some((c) => (c as ObjectStatus).getText() === "Sent"),
        success: function () {
            Opa5.assert.ok(true, "the comment is Sent while the run works");
            release();
        },
        errorMessage: "The comment is not Sent"
    });
    objectControl(Then, 0, "changesRevision", "sap.m.Select", (c) => {
        Opa5.assert.strictEqual((c as Select).getSelectedKey(), "2", "the new revision is shown");
    }, (c) => (c as Select).getSelectedKey() === "2");
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => diff(X)?.getAttribute("data-revision") === "2" && (diff(X)?.textContent ?? "").includes("proposed by the fake"),
        success: function () {
            Opa5.assert.ok(true, "the new revision's diff is shown");
        },
        errorMessage: "The diff is not revision 2"
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesOtherState-"),
        check: (list: UI5Element[]) => list.some((c) => (c as ObjectStatus).getText() === "Addressed"),
        success: function () {
            Opa5.assert.ok(true, "the addressed comment is listed under other revisions");
        },
        errorMessage: "The addressed comment is not shown"
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesOtherAnswer-"),
        check: (list: UI5Element[]) => list.some((c) => (c as Text).getProperty("text") === "Assistant: Addressed: Body c-1"),
        success: function () {
            Opa5.assert.ok(true, "with the assistant's answer");
        },
        errorMessage: "No answer"
    });
    // The old revision on request: its comment is on its lines again.
    objectControl(When, 0, "changesRevision", "sap.m.Select", (c) => {
        const select = c as Select;
        select.setSelectedKey("1");
        select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
    });
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => diff(X)?.getAttribute("data-revision") === "1" && !!newRow(X, 12)?.querySelector(".ideDiffCommentMark"),
        success: function () {
            Opa5.assert.ok(true, "revision 1 with its comment marker");
        },
        errorMessage: "Revision 1 is not shown"
    });
    Then.iStopTheApp();
});

opaTest("Check syntax: 409 syntax_check_running is worded, then the new status and its lines are shown", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seed(fake);
        fake.scriptSyntax(R, "errors", [{ line: 2, message: "WRITE needs a period.", severity: "error" }]);
        fake.failNext = {
            path: "sessions/s-1/file/syntax", status: 409,
            body: { detail: "A syntax check of this session is already running", code: "syntax_check_running" }
        };
    });
    theDiffsAreShown(Then, 3);
    objectControl(When, 2, "changesCheckSyntax", "sap.m.Button", (c) => (c as Button).firePress({}));
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: (dialogs: UI5Element[]) => dialogs.some((d) => (d.getDomRef()?.textContent ?? "").includes("being checked right now")),
        success: function () {
            Opa5.assert.ok(true, "syntax_check_running is worded");
        },
        errorMessage: "No syntax_check_running message"
    });
    closeMessageBox(When);
    theStatusIs(Then, 2, "changesSyntax", "Syntax not checked", "Warning");
    objectControl(When, 2, "changesCheckSyntax", "sap.m.Button", (c) => (c as Button).firePress({}));
    theStatusIs(Then, 2, "changesSyntax", "1 syntax error", "Error");
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!newRow(R, 2)?.classList.contains("ideDiffSyntaxError"),
        success: function () {
            const body = backend.requests.filter((k) => k === "POST sessions/s-1/file/syntax").length;
            Opa5.assert.strictEqual(body, 2, "two checks asked");
            Opa5.assert.ok(true, "line 2 is marked");
        },
        errorMessage: "Line 2 is not marked"
    });
    Then.iStopTheApp();
});

opaTest("Source view at a line: highlighted, scrolled into view, text only", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/s-1?view=source&path=${encodeURIComponent(X)}&line=31`, undefined, (fake) => { seed(fake); });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        check: (c: UI5Element) => (c as Title).getText() === "Source of zcl_x.clas.abap",
        success: function () {
            Opa5.assert.ok(true, "the title names the file");
        },
        errorMessage: "No source title"
    });
    Then.waitFor({
        id: "sourceView",
        ...SOPTS,
        check: () => {
            const row = doc().querySelector<HTMLElement>(".ideSource tr[data-line='31']");
            const scroll = doc().querySelector<HTMLElement>("[id$='--artifactScroll']");
            if (!row || !scroll || row.getAttribute("aria-current") !== "true") {
                return false;
            }
            const r = row.getBoundingClientRect();
            const s = scroll.getBoundingClientRect();
            return r.top >= s.top && r.bottom <= s.bottom;
        },
        success: function () {
            const row = doc().querySelector<HTMLElement>(".ideSource tr[data-line='31']")!;
            Opa5.assert.ok(row.classList.contains("ideSourceHighlight"), "the line is highlighted");
            Opa5.assert.ok((row.textContent ?? "").includes(HOSTILE), "the source as text");
            Opa5.assert.strictEqual(doc().querySelectorAll(".ideSource img").length, 0, "no markup");
        },
        errorMessage: "Line 31 is not highlighted in view"
    });
    Then.iStopTheApp();
});

opaTest("A large diff shows its hunks only and a link to its source", function (Given: Common, When: Common, Then: Common) {
    const big = Array.from({ length: 3000 }, (_, i) => `    " line ${i + 1}`).join("\n");
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seed(fake, { extra: { path: "src/CLAS/zcl_big.clas.abap", type: "CLAS", name: "ZCL_BIG", origin: big, proposal: `${big}\n* more` } });
    });
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!diff("src/CLAS/zcl_big.clas.abap")?.querySelector(".ideDiffHunk"),
        success: function () {
            // Final review M2: over 5000 lines the changes are shown with their context, nothing else.
            const root = diff("src/CLAS/zcl_big.clas.abap")!;
            Opa5.assert.notOk(root.querySelector(".ideDiffTooLarge"), "compared, not too large");
            Opa5.assert.strictEqual(root.querySelectorAll("tr[data-line]").length, 4, "the added line and 3 lines before it");
        },
        errorMessage: "No hunks-only diff"
    });
    objectControl(When, 3, "changesShowSource", "sap.m.Link", (c) => (c as Link).firePress({}), (c) => (c as Link).getVisible());
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        check: (c: UI5Element) => (c as Title).getText() === "Source of zcl_big.clas.abap",
        success: function () {
            Opa5.assert.ok(true, "the source view opens");
        },
        errorMessage: "The source view did not open"
    });
    Then.iStopTheApp();
});

opaTest("An open comment blocks Approve changes; once dismissed, approve sends the revisions", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake, { comments: [lineComment("c-1", 12, 14)] }); });
    theDiffsAreShown(Then, 3);
    thePrimaryActionIs(Then, "Approve changes and review", false, "blocked by the open comment");
    theReasonIs(Then, "Resolve or dismiss the 1 open review comment first.", "the reason is visible");
    When.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!newRow(X, 12)?.querySelector(".ideDiffCommentMark"),
        success: function () {
            click(newRow(X, 12)!.querySelector(".ideDiffCommentMark")!);
        }
    });
    thePopoverIsOpen(Then, "Comments on lines 12–14");
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: (c: UI5Element) => c.getId().includes("commentDismiss") && (c as Control).getVisible(),
        actions: new Press(),
        errorMessage: "No Dismiss"
    });
    thePrimaryActionIs(Then, "Approve changes and review", true, "enabled once nothing is open");
    iPress(When, "primaryAction");
    Then.waitFor({
        check: () => !!lastBody("POST sessions/s-1/approve"),
        success: function () {
            Opa5.assert.deepEqual(lastBody("POST sessions/s-1/approve")!.revisions, { [X]: 1, [I]: 1, [R]: 1 }, "approve sends the revisions shown");
        },
        errorMessage: "No approve"
    });
    Then.iStopTheApp();
});

// --- Lead decision after U10: approve changes only from the changes view ----------------

opaTest("Outside the changes view the primary action opens it (Review changes); approve only with the cards on screen", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seed(fake); });
    thePrimaryActionIs(Then, "Review changes", true, "without the cards the primary action reviews, it does not approve");
    iPress(When, "primaryAction");
    theDiffsAreShown(Then, 3);
    Then.waitFor({
        success: function () {
            Opa5.assert.notOk(backend.bodies.some((b) => b.key === "POST sessions/s-1/approve"), "pressing it approved nothing");
            Opa5.assert.ok(/view=changes/.test(HashChanger.getInstance().getHash()), "it opened the changes view");
        }
    });
    thePrimaryActionIs(Then, "Approve changes and review", true, "with the cards loaded it approves");
    // Closing the view takes the cards away: the action is Review changes again.
    iPress(When, "closeArtifact");
    thePrimaryActionIs(Then, "Review changes", true, "closed: back to Review changes");
    iPress(When, "primaryAction");
    theDiffsAreShown(Then, 3);
    thePrimaryActionIs(Then, "Approve changes and review", true, "opened again");
    iPress(When, "primaryAction");
    Then.waitFor({
        check: () => !!lastBody("POST sessions/s-1/approve"),
        success: function () {
            Opa5.assert.deepEqual(lastBody("POST sessions/s-1/approve")!.revisions, { [X]: 1, [I]: 1, [R]: 1 },
                "approve sends the revisions of the cards on screen");
        },
        errorMessage: "No approve"
    });
    Then.iStopTheApp();
});

opaTest("Another view open (a document): the primary action in propose is Review changes, not an approve", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => {
        const sid = seed(fake);
        fake.addArtifact(sid, "note", "# Note\n\nWhat changed.");
    });
    thePrimaryActionIs(Then, "Review changes", true, "no cards");
    When.waitFor({
        success: function () {
            HashChanger.getInstance().setHash("sessions/s-1?view=document&kind=note&version=1");
        }
    });
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: (c: UI5Element) => (c as Title).getText() !== "",
        success: function () {
            Opa5.assert.ok(true, "the note is shown");
        }
    });
    thePrimaryActionIs(Then, "Review changes", true, "the note is not the changes view");
    iPress(When, "primaryAction");
    theDiffsAreShown(Then, 3);
    Then.waitFor({
        success: function () {
            Opa5.assert.notOk(backend.bodies.some((b) => b.key === "POST sessions/s-1/approve"), "nothing was approved");
        }
    });
    Then.iStopTheApp();
});

// --- Fix round 1 -------------------------------------------------------------------

/** The session page's controller (read, and the file-event path driven as the run controller does). */
type Page = { reloadDetail(): Promise<void>; syncChanges(): Promise<void> };

function withPage(When: Common, fn: (page: Page) => void): void {
    When.waitFor({
        id: "sessionPage",
        ...SOPTS,
        visible: false,
        success: (c: UI5Element) => {
            let el = c as UI5Element | null;
            while (el && !el.isA("sap.ui.core.mvc.View")) {
                el = (el as UI5Element).getParent() as UI5Element | null;
            }
            fn((el as unknown as View).getController() as Controller as unknown as Page);
        },
        errorMessage: "No session page"
    });
}

function approveSent(Then: Common, count: number, revisions: Record<string, number>, message: string): void {
    Then.waitFor({
        check: () => backend.bodies.filter((b) => b.key === "POST sessions/s-1/approve").length === count,
        success: function () {
            Opa5.assert.deepEqual(lastBody("POST sessions/s-1/approve")!.revisions, revisions, message);
        },
        errorMessage: `No approve #${count}`
    });
}

function theStageIs(Then: Common, stage: string, message: string): void {
    Then.waitFor({
        check: () => backend.sessions[0].session.stage === stage,
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The stage is not ${stage}`
    });
}

function theRevisionShownIs(Then: Common, index: number, path: string, revision: string, message: string): void {
    objectControl(Then, index, "changesRevision", "sap.m.Select", () => {
        Opa5.assert.ok(true, message);
    }, (c) => (c as Select).getSelectedKey() === revision && diff(path)?.getAttribute("data-revision") === revision);
}

const D = "src/CLAS/zcl_dropped.clas.abap";

opaTest("A dropped proposal (read, revisions kept) is not listed and approve does not pin it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        const sid = seed(fake);
        const data = fake.dataOf(sid)!;
        data.files.push({ path: D, state: "modified", object_type: "CLAS", object_name: "ZCL_DROPPED", origin_source: "CLASS zcl_dropped.", proposed_source: "" });
        fake.addRevision(sid, D, "CLASS zcl_dropped. \" was");
        // The server sets this when a proposal becomes byte-equal to SAP again.
        data.files.find((f) => f.path === D)!.state = "read";
    });
    theDiffsAreShown(Then, 3);
    Then.waitFor({
        id: "changesSummary",
        ...SOPTS,
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as Text).getProperty("text"), "3 objects, +8 −2 lines", "three objects, the read file is not one");
            Opa5.assert.notOk(diff(D), "no card for the dropped proposal");
        }
    });
    thePrimaryActionIs(Then, "Approve changes and review", true, "approve is offered");
    iPress(When, "primaryAction");
    approveSent(Then, 1, { [X]: 1, [I]: 1, [R]: 1 }, "the read file is not sent");
    theStageIs(Then, "review", "approve succeeded (no version_changed)");
    Then.iStopTheApp();
});

opaTest("After version_changed the cards show the new revision and the second approve sends exactly those", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake); });
    theDiffsAreShown(Then, 3);
    // A newer revision is written without a file event reaching the page.
    When.waitFor({ success: () => { backend.addRevision("s-1", X, `${NEW_X}\n* second revision`); } });
    iPress(When, "primaryAction");
    approveSent(Then, 1, { [X]: 1, [I]: 1, [R]: 1 }, "the first approve sends what the cards showed");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: (dialogs: UI5Element[]) => dialogs.some((d) => (d.getDomRef()?.textContent ?? "").length > 0),
        success: function () {
            Opa5.assert.ok(true, "version_changed is shown");
        },
        errorMessage: "No version_changed message"
    });
    closeMessageBox(When);
    theRevisionShownIs(Then, 0, X, "2", "the card reloaded to revision 2");
    thePrimaryActionIs(Then, "Approve changes and review", true, "approve is offered again");
    iPress(When, "primaryAction");
    approveSent(Then, 2, { [X]: 2, [I]: 1, [R]: 1 }, "the second approve sends the revisions the cards show");
    theStageIs(Then, "review", "and succeeds");
    Then.iStopTheApp();
});

opaTest("Open in ADT on a class include opens that include at the line of its own diff", function (Given: Common, When: Common, Then: Common) {
    const T = "src/CLAS/zcl_x.clas.testclasses.abap";
    const origin = "CLASS ltc DEFINITION FOR TESTING.\nENDCLASS.\nCLASS ltc IMPLEMENTATION.\n  METHOD a.\n  ENDMETHOD.\nENDCLASS.";
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seed(fake, { extra: { path: T, type: "CLAS", name: "ZCL_X", origin, proposal: origin.replace("  METHOD a.", "  METHOD b.") } });
    });
    theDiffsAreShown(Then, 4);
    objectControl(Then, 3, "changesAdt", "sap.m.Link", (c) => {
        Opa5.assert.strictEqual((c as Link).getHref(), "adt://DEMO/sap/bc/adt/oo/classes/zcl_x/includes/testclasses#start=4,0",
            "the include's URI and the line of its first change");
    }, (c) => !!(c as Link).getHref());
    objectControl(Then, 0, "changesAdt", "sap.m.Link", (c) => {
        Opa5.assert.strictEqual((c as Link).getHref(), "adt://DEMO/sap/bc/adt/oo/classes/zcl_x/source/main#start=12,0", "the main source unchanged");
    });
    Then.iStopTheApp();
});

opaTest("Lint belongs to the latest revision: an older one says so and Run lint is off; pending syntax marks no line", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        const sid = seed(fake);
        fake.addRevision(sid, X, `${NEW_X}\n* second revision`);
        const file = fake.dataOf(sid)!.files.find((f) => f.path === X)!;
        // A check not finished yet: stored items do not mark lines.
        Object.assign(file.revisions![1], { syntax_status: null, syntax: [{ line: 1, message: "stale", severity: "error" }] });
    });
    theDiffsAreShown(Then, 3);
    theRevisionShownIs(Then, 0, X, "2", "the latest revision");
    theStatusIs(Then, 0, "changesLint", "Lint not run", "None");
    objectControl(Then, 0, "changesRunLint", "sap.m.Button", (c) => {
        Opa5.assert.ok((c as Button).getEnabled(), "Run lint on the latest");
    });
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!newRow(X, 1),
        success: function () {
            Opa5.assert.notOk(newRow(X, 1)!.classList.contains("ideDiffSyntaxError"), "a pending check marks no line");
        }
    });
    objectControl(When, 0, "changesRevision", "sap.m.Select", (c) => {
        const select = c as Select;
        select.setSelectedKey("1");
        select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
    });
    theRevisionShownIs(Then, 0, X, "1", "revision 1");
    theStatusIs(Then, 0, "changesLint", "Lint applies to the latest revision", "None");
    objectControl(Then, 0, "changesRunLint", "sap.m.Button", (c) => {
        Opa5.assert.notOk((c as Button).getEnabled(), "Run lint is off on an older revision");
    }, (c) => !(c as Button).getEnabled());
    Then.iStopTheApp();
});

opaTest("A folded line with a comment opens its fold; an anchor link opens a closed fold; the diff is a grid", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake, { comments: [lineComment("c-1", 3, 3)] }); });
    theDiffsAreShown(Then, 3);
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => !!newRow(X, 3)?.querySelector(".ideDiffCommentMark"),
        success: function () {
            const row = newRow(X, 3)!;
            Opa5.assert.ok(row.closest("details")?.open, "the fold holding the marker is open");
            Opa5.assert.notOk(newRow(X, 20)!.closest("details")?.open, "a fold with nothing in it stays closed");
            const grid = diff(X)!.querySelector("table.ideDiffTable")!;
            Opa5.assert.strictEqual(grid.getAttribute("role"), "grid", "the diff is a grid");
            Opa5.assert.strictEqual(grid.getAttribute("aria-multiselectable"), "true");
            Opa5.assert.strictEqual(newRow(X, 12)!.getAttribute("aria-selected"), "false", "selectable rows say they are not selected");
            row.closest("details")!.open = false;
        }
    });
    When.waitFor({
        controlType: "sap.m.Link",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesCommentAnchor-"),
        actions: new Press(),
        errorMessage: "No anchor link"
    });
    theFocusIs(Then, isRow(X, "new", 3), "the anchor's line takes the focus");
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(newRow(X, 3)!.closest("details")?.open, "its fold is open again");
        }
    });
    Then.iStopTheApp();
});

opaTest("An object that cannot be read is a card with an error and Retry; the others load", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seed(fake);
        fake.failNext = { path: "sessions/s-1/file", status: 500, body: { detail: "SAP did not answer" } };
    });
    Then.waitFor({
        id: "changesView",
        ...SOPTS,
        check: () => doc().querySelectorAll(".ideUnified[data-path]").length === 2,
        success: function () {
            Opa5.assert.ok(true, "the other two objects are shown");
        },
        errorMessage: "The other objects did not load"
    });
    Then.waitFor({
        controlType: "sap.m.MessageStrip",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesFailedText-") && (c as Control).getVisible(),
        check: (list: UI5Element[]) => list.length === 1,
        success: function (list: UI5Element[]) {
            Opa5.assert.strictEqual(list[0].getProperty("text"), "ZCL_X could not be loaded.", "the card says which object failed");
        },
        errorMessage: "No per-card error"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--changesRetry-") && (c as Control).getVisible(),
        actions: new Press(),
        errorMessage: "No Retry"
    });
    theDiffsAreShown(Then, 3);
    Then.iStopTheApp();
});

opaTest("A file event during the first load is applied after it, not dropped", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => {
        seed(fake);
        release = fake.hold("GET sessions/s-1/file");
    });
    Then.waitFor({ check: () => backend.requests.includes("GET sessions/s-1/file"), success: () => Opa5.assert.ok(true, "the first load is in flight") });
    withPage(When, (page) => {
        backend.addRevision("s-1", X, `${NEW_X}\n* second revision`);
        // What a `file` event does (RunController.onFilesChanged).
        void page.reloadDetail().then(() => page.syncChanges()).then(() => undefined);
        setTimeout(release, 50);
    });
    theRevisionShownIs(Then, 0, X, "2", "the card shows the revision of the event");
    Then.iStopTheApp();
});

opaTest("409 run_in_progress on Check syntax and Run lint is worded", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=changes", undefined, (fake) => { seed(fake); });
    theDiffsAreShown(Then, 3);
    const refusal = { detail: "A run is in progress for this session.", code: "run_in_progress" };
    const worded = (Then2: Common, what: string): void => {
        Then2.waitFor({
            controlType: "sap.m.Dialog",
            searchOpenDialogs: true,
            check: (dialogs: UI5Element[]) => dialogs.some((d) => (d.getDomRef()?.textContent ?? "")
                .includes("The assistant is still working on this session. Wait for it or stop it.")),
            success: function () {
                Opa5.assert.ok(true, `${what}: run_in_progress is worded`);
            },
            errorMessage: `${what}: no run_in_progress text`
        });
    };
    When.waitFor({ success: () => { backend.failNext = { path: "sessions/s-1/file/syntax", status: 409, body: refusal }; } });
    objectControl(When, 0, "changesCheckSyntax", "sap.m.Button", (c) => (c as Button).firePress({}));
    worded(Then, "Check syntax");
    closeMessageBox(When);
    When.waitFor({ success: () => { backend.failNext = { path: "sessions/s-1/file/lint", status: 409, body: refusal }; } });
    objectControl(When, 0, "changesRunLint", "sap.m.Button", (c) => (c as Button).firePress({}));
    worded(Then, "Run lint");
    closeMessageBox(When);
    theStatusIs(Then, 0, "changesLint", "Lint not run", "None");
    Then.iStopTheApp();
});
