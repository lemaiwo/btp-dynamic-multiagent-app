import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import type UI5Element from "sap/ui/core/Element";
import type Button from "sap/m/Button";
import type Popover from "sap/m/Popover";
import type TextArea from "sap/m/TextArea";
import type List from "sap/m/List";
import type Controller from "sap/ui/core/mvc/Controller";
import type View from "sap/ui/core/mvc/View";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress } from "./pages/Session";
import type FakeBackend from "./FakeBackend";
import type { Comment } from "../../service/types";

/** U9 review fix round: guards, size limits, one tab stop, quotes, a new version while commenting. */
QUnit.module("Document review fixes");

function doc(blocks: number): string {
    return Array.from({ length: blocks }, (_, i) => `Block number ${i + 1} of the design.`).join("\n\n");
}

function seed(fake: FakeBackend, blocks = 4, stage: "design" = "design"): string {
    const s = fake.sessions[0].session;
    s.stage = stage;
    s.request_cap = 1000;
    fake.addArtifact(s.id, "design", doc(blocks));
    return s.id;
}

function comment(id: string, paragraph: number, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 1, paragraph, body: `Body ${id}`, state: "open", answer: null,
        created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00", ...extra
    };
}

function content(): HTMLElement | null {
    return Opa5.getWindow().document.querySelector("[id$='--artifactContent']");
}

function block(i: number): HTMLElement | null {
    return content()?.querySelector(`.ideDocBlock[data-para='${i}']`) ?? null;
}

function active(): HTMLElement | null {
    return Opa5.getWindow().document.activeElement as HTMLElement | null;
}

function theBlocksAreShown(Then: Common, count: number): void {
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => content()?.querySelectorAll(".ideDocBlock").length === count,
        success: function () {
            Opa5.assert.ok(true, `all ${count} blocks are rendered`);
        },
        errorMessage: `Not ${count} blocks`
    });
}

function iPressKeyOn(When: Common, find: () => HTMLElement | null, key: string, message: string): void {
    When.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => !!find(),
        success: function () {
            const el = find()!;
            el.focus();
            QUnitUtils.triggerKeydown(el, key);
        },
        errorMessage: message
    });
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

function iType(When: Common, text: string): void {
    When.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text, keepFocus: true }),
        errorMessage: "No comment text area"
    });
}

function posts(key: string): number {
    return backend.requests.filter((k) => k === key).length;
}

function controllerOf(control: UI5Element): Controller {
    let el = control as UI5Element | null;
    while (el && !el.isA("sap.ui.core.mvc.View")) {
        el = (el as UI5Element).getParent() as UI5Element | null;
    }
    return (el as unknown as View).getController();
}

opaTest("Request changes: a double press of Send starts one run; a refused run keeps the note", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = seed(fake);
        fake.dataOf(sid)!.comments.push(comment("c-1", 1));
    });
    theBlocksAreShown(Then, 4);
    iPress(When, "requestChangesButton");
    When.waitFor({
        id: "requestChangesSend",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (button: UI5Element) {
            (button as Button).firePress();
            (button as Button).firePress();
        },
        errorMessage: "No Send"
    });
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Request changes" }),
        success: function () {
            Opa5.assert.strictEqual(posts("POST sessions/s-1/request-changes"), 1, "one request-changes POST");
        },
        errorMessage: "The run did not end"
    });
    // A refused run: the note comes back into the dialog.
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
        id: "requestChangesNote",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Keep the old signature.", keepFocus: true }),
        errorMessage: "No note"
    });
    When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Send is off" });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: (dialogs: UI5Element[]) => dialogs.some((d) => (d.getDomRef()?.textContent ?? "").includes("still working")),
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.ok(true, "run_in_progress is worded");
            const box = dialogs.find((d) => (d.getDomRef()?.textContent ?? "").includes("still working"))!;
            (box as unknown as { close(): void }).close();
        },
        errorMessage: "No run_in_progress message"
    });
    iPress(When, "requestChangesButton");
    Then.waitFor({
        id: "requestChangesNote",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => (c as TextArea).getValue() === "Keep the old signature.",
        success: function () {
            Opa5.assert.ok(true, "the refused note is restored");
        },
        errorMessage: "The note was lost"
    });
    When.waitFor({ id: "requestChangesCancel", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.iStopTheApp();
});

opaTest("Request changes is enabled without comments; Send needs a non-blank note; nothing_to_send is worded", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seed(fake); });
    theBlocksAreShown(Then, 4);
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        success: function () {
            Opa5.assert.ok(true, "enabled in a revisable stage without comments");
        }
    });
    iPress(When, "requestChangesButton");
    When.waitFor({
        id: "requestChangesNote",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "   ", keepFocus: true })
    });
    Then.waitFor({
        id: "requestChangesSend",
        ...SOPTS,
        searchOpenDialogs: true,
        enabled: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        success: function () {
            Opa5.assert.ok(true, "a blank note sends nothing");
        }
    });
    When.waitFor({
        id: "requestChangesNote",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Round per currency.", keepFocus: true })
    });
    Then.waitFor({
        success: function () {
            backend.failNext = {
                path: "sessions/s-1/request-changes", status: 409,
                body: { detail: "Write a comment or a note first.", code: "nothing_to_send" }
            };
        }
    });
    When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "Send is off" });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        check: (dialogs: UI5Element[]) => dialogs.some((d) => /comment|note/i.test(d.getDomRef()?.textContent ?? "")
            && (d.getDomRef()?.className ?? "").includes("sapMMessageBox")),
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.ok(true, "nothing_to_send is worded");
            dialogs.filter((d) => (d.getDomRef()?.className ?? "").includes("sapMMessageBox"))
                .forEach((d) => (d as unknown as { close(): void }).close());
        },
        errorMessage: "No nothing_to_send message"
    });
    Then.iStopTheApp();
});

opaTest("long lists are not cut at 100: a 120-block document and 105 messages", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = seed(fake, 120);
        const data = fake.dataOf(sid)!;
        for (let i = 0; i < 105; i++) {
            data.messages.push({ id: `m-${i}`, role: i % 2 ? "assistant" : "user", stage: "design", created_at: "2026-10-03T09:00:00", content: `Message ${i}` });
        }
    });
    theBlocksAreShown(Then, 120);
    Then.waitFor({
        id: "messageList",
        ...SOPTS,
        check: (c: UI5Element) => (c as List).getItems().length === 105,
        success: function () {
            Opa5.assert.ok(true, "every message is listed");
        },
        errorMessage: "The conversation is cut"
    });
    Then.iStopTheApp();
});

opaTest("one tab stop for a 60-block document: arrows, Home and End move between blocks; markers are not tab stops", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seed(fake, 60); });
    theBlocksAreShown(Then, 60);
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => Array.from(content()!.querySelectorAll("button")).every((b) => b.getAttribute("tabindex") === "-1"),
        success: function () {
            const root = content()!;
            const stops = Array.from(root.querySelectorAll<HTMLElement>("[tabindex], button, a[href]"))
                .filter((el) => el.tabIndex >= 0 && el.getAttribute("tabindex") !== "-1");
            Opa5.assert.strictEqual(stops.length, 1, "one tab stop in the whole document");
            Opa5.assert.strictEqual(stops[0], block(0), "the first block");
        },
        errorMessage: "The markers are tab stops"
    });
    iPressKeyOn(When, () => block(0), "ARROW_DOWN", "No block 0");
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => active() === block(1),
        success: function () {
            Opa5.assert.strictEqual(block(1)!.getAttribute("tabindex"), "0", "the tab stop follows the focus");
            Opa5.assert.strictEqual(block(0)!.getAttribute("tabindex"), "-1");
        },
        errorMessage: "Arrow down did not move to block 2"
    });
    iPressKeyOn(When, () => block(1), "END", "No block 1");
    Then.waitFor({ id: "artifactContent", ...SOPTS, check: () => active() === block(59), success: () => Opa5.assert.ok(true, "End: the last block") });
    iPressKeyOn(When, () => block(59), "HOME", "No block 59");
    Then.waitFor({ id: "artifactContent", ...SOPTS, check: () => active() === block(0), success: () => Opa5.assert.ok(true, "Home: the first block") });
    iPressKeyOn(When, () => block(0), "ARROW_UP", "No block 0");
    Then.waitFor({ id: "artifactContent", ...SOPTS, check: () => active() === block(0), success: () => Opa5.assert.ok(true, "Up at the top stays") });
    Then.iStopTheApp();
});

opaTest("a new comment sends the block's quote; a double Save creates one comment; 4000 characters at most", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => { seed(fake); });
    theBlocksAreShown(Then, 4);
    iPressKeyOn(When, () => block(2), "ENTER", "No block 2");
    thePopoverIsOpen(Then, "Comments on block 3");
    Then.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as TextArea).getMaxLength(), 4000, "the text area stops at 4000");
        }
    });
    iType(When, "x".repeat(4000));
    Then.waitFor({
        id: "commentDraftCounter",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "4000 of 4000 characters" }),
        success: () => Opa5.assert.ok(true, "the counter shows the limit")
    });
    iType(When, "Check the rounding.");
    Then.waitFor({
        success: function () {
            release = backend.hold("POST sessions/s-1/comments");
        }
    });
    When.waitFor({
        id: "commentSave",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (button: UI5Element) {
            (button as Button).firePress();
            (button as Button).firePress();
            release();
        }
    });
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Request changes (1)" }),
        success: function () {
            Opa5.assert.strictEqual(posts("POST sessions/s-1/comments"), 1, "one create");
            const sent = backend.bodies.filter((b) => b.key === "POST sessions/s-1/comments").map((b) => b.body as Record<string, unknown>);
            Opa5.assert.strictEqual(sent[0].quote, "Block number 3 of the design.", "the block's text goes along as quote");
            Opa5.assert.strictEqual(backend.dataOf("s-1")!.comments.length, 1, "one comment stored");
        },
        errorMessage: "The comment was not saved once"
    });
    Then.iStopTheApp();
});

opaTest("edit, 409 not editable relists, 404 relists, 422 is worded, delete", function (Given: Common, When: Common, Then: Common) {
    let listsBefore = 0;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = seed(fake);
        fake.dataOf(sid)!.comments.push(comment("c-1", 1));
    });
    theBlocksAreShown(Then, 4);
    iPressKeyOn(When, () => block(1), "ENTER", "No block 1");
    thePopoverIsOpen(Then, "Comments on block 2");
    const pressRow = (part: string): void => {
        When.waitFor({
            controlType: "sap.m.Button",
            ...SOPTS,
            searchOpenDialogs: true,
            matchers: (c: UI5Element) => c.getId().includes(part) && c.getBindingContext("s")?.getPath() === "/pop/comments/0",
            actions: new Press(),
            errorMessage: `No ${part}`
        });
    };
    pressRow("commentEdit");
    iType(When, "Edited body.");
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        success: function () {
            Opa5.assert.strictEqual(backend.dataOf("s-1")!.comments[0].body, "Edited body.", "the edit is stored");
        }
    });
    // 422 on a new comment.
    iPressKeyOn(When, () => block(0), "ENTER", "No block 0");
    iType(When, "Another.");
    Then.waitFor({
        success: function () {
            backend.failNext = { path: "sessions/s-1/comments", status: 422, body: { detail: "bad", code: "invalid_anchor" } };
        }
    });
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "commentPopoverError",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "The comment could not be saved: check its length (max 4000 characters)." }),
        success: () => Opa5.assert.ok(true, "422 is worded")
    });
    Then.waitFor({ id: "commentPopover", ...SOPTS, searchOpenDialogs: true, success: (p: UI5Element) => (p as Popover).close(), errorMessage: "CLOSE-A: no open popover after the 422" });
    // 404 on a delete: worded, relisted (the comment is gone on the server).
    iPressKeyOn(When, () => block(1), "ENTER", "No block 1");
    Then.waitFor({
        success: function () {
            backend.dataOf("s-1")!.comments = [];
            listsBefore = posts("GET sessions/s-1/comments");
            backend.failNext = { path: "sessions/s-1/comments/c-1", status: 404, body: { detail: "Comment not found" } };
        }
    });
    pressRow("commentDelete");
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Request changes" }),
        success: function () {
            Opa5.assert.ok(posts("GET sessions/s-1/comments") > listsBefore, "the comments were read again");
        },
        errorMessage: "No relist after 404"
    });
    Then.waitFor({ id: "commentPopover", ...SOPTS, searchOpenDialogs: true, success: (p: UI5Element) => (p as Popover).close(), errorMessage: "CLOSE-B: no open popover after the 404 relist" });
    // 409 comment_not_editable on an edit of a fresh comment the server has moved on: worded, relisted.
    iPressKeyOn(When, () => block(1), "ENTER", "No block 1");
    iType(When, "Fresh.");
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    iPressKeyOn(When, () => block(1), "ENTER", "No block 1");
    pressRow("commentEdit");
    iType(When, "Late edit.");
    Then.waitFor({
        success: function () {
            const fresh = backend.dataOf("s-1")!.comments[0];
            backend.setCommentState("s-1", fresh.id, "sent");
            backend.failNext = {
                path: `sessions/s-1/comments/${fresh.id}`, status: 409,
                body: { detail: "Only an open comment can be edited.", code: "comment_not_editable" }
            };
        }
    });
    When.waitFor({ id: "commentSave", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "commentPopoverError",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Only an open comment can be changed or deleted." }),
        success: function () {
            Opa5.assert.ok(true, "409 is worded");
        }
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: (c: UI5Element) => c.getBindingContext("s")?.getPath() === "/pop/comments/0",
        check: (list: UI5Element[]) => list.some((c) => (c as unknown as { getText(): string }).getText() === "Sent"),
        success: () => Opa5.assert.ok(true, "the list was read again: the comment is Sent"),
        errorMessage: "No relist after 409"
    });
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (p: UI5Element) {
            (p as Popover).close();
        },
        errorMessage: "CLOSE-C: no open popover after the 409 relist"
    });
    Then.iStopTheApp();
});

opaTest("a new version while commenting keeps the draft and offers a link; a comment past the end says where it was", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = seed(fake);
        fake.dataOf(sid)!.comments.push(comment("c-far", 9));
    });
    theBlocksAreShown(Then, 4);
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("docCommentHint"),
        check: (list: UI5Element[]) => list.some((c) => (c as unknown as { getText(): string }).getText() === "Was on block 10 of an earlier layout."),
        success: () => Opa5.assert.ok(true, "the moved comment says where it was"),
        errorMessage: "No hint on the moved comment"
    });
    iPressKeyOn(When, () => block(0), "ENTER", "No block 0");
    iType(When, "Half-written thought");
    When.waitFor({
        id: "artifactContent",
        ...SOPTS,
        success: function (c: UI5Element) {
            backend.addArtifact("s-1", "design", "# Design v2");
            void (controllerOf(c) as unknown as { onArtifactStored(k: string, v: number): Promise<void> }).onArtifactStored("design", 2);
        }
    });
    Then.waitFor({
        id: "docNewer",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "v2 is available" }),
        success: function () {
            Opa5.assert.ok(true, "a link offers the new version");
        },
        errorMessage: "No new-version link"
    });
    Then.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => (c as TextArea).getValue() === "Half-written thought",
        success: () => Opa5.assert.ok(true, "the popover and its draft stay"),
        errorMessage: "The draft was discarded"
    });
    Then.waitFor({ id: "commentPopover", ...SOPTS, searchOpenDialogs: true, success: (p: UI5Element) => (p as Popover).close() });
    iPress(When, "docNewer");
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Design v2" }),
        success: () => Opa5.assert.ok(true, "the link opens v2")
    });
    Then.iStopTheApp();
});
