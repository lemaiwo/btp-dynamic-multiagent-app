import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import QUnitUtils from "sap/ui/qunit/QUnitUtils";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Button from "sap/m/Button";
import type List from "sap/m/List";
import type Popover from "sap/m/Popover";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, theReasonIs, thePrimaryActionIs } from "./pages/Session";
import type FakeBackend from "./FakeBackend";
import type { Comment } from "../../service/types";

/** Task U9: the document view, paragraph comments and Request changes. */
QUnit.module("Document review journey");

const DESIGN = "# Design\n\nFirst paragraph.\n\nSecond paragraph.\n\nThird paragraph.";
const HOSTILE = "<img src=x onerror=alert(1)> {/session/title} {= 'x' }";

/** A change session in `design` with design v1 (four blocks: the heading and three paragraphs). */
function designDoc(fake: FakeBackend): string {
    const s = fake.sessions[0].session;
    s.stage = "design";
    s.requests_used = 3;
    s.request_cap = 1000;
    fake.addArtifact(s.id, "design", DESIGN);
    return s.id;
}

function docComment(id: string, paragraph: number, body: string, extra: Partial<Comment> = {}): Comment {
    return {
        id, anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 1, paragraph, body, state: "open", answer: null,
        created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00", ...extra
    };
}

function domOf(id: string): HTMLElement | null {
    return Opa5.getWindow().document.querySelector(`[id$='--${id}']`);
}

function paragraph(index: number): HTMLElement | null {
    return domOf("artifactContent")?.querySelector(`.ideDocBlock[data-para='${index}']`) ?? null;
}

function activePara(): string | null {
    return (Opa5.getWindow().document.activeElement as HTMLElement | null)?.getAttribute("data-para") ?? null;
}

function bindingPath(path: string) {
    return (control: UI5Element): boolean => control.getBindingContext("s")?.getPath() === path;
}

function theDocumentShows(Then: Common, title: string, message: string): void {
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: title }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The artifact column does not show ${title}`
    });
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => !!paragraph(0),
        success: function () {
            Opa5.assert.ok(true, "its paragraphs are rendered");
        }
    });
}

function theVersionsAre(Then: Common, texts: string[], selected: string, message: string): void {
    Then.waitFor({
        id: "docVersions",
        ...SOPTS,
        check: function (control: UI5Element) {
            const sb = control as SegmentedButton;
            return JSON.stringify(sb.getItems().map((i) => i.getText())) === JSON.stringify(texts)
                && sb.getSelectedKey() === selected;
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The version switcher is not ${texts.join(" · ")} with ${selected} selected`
    });
}

function iPressKey(When: Common, index: number, key: string): void {
    When.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => !!paragraph(index),
        success: function () {
            const el = paragraph(index)!;
            el.focus();
            QUnitUtils.triggerKeydown(el, key);
        },
        errorMessage: `No paragraph ${index}`
    });
}

function thePopoverIsOpen(Then: Common, title: string): void {
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (control: UI5Element) => (control as Popover).isOpen() && (control as Popover).getTitle() === title,
        success: function () {
            Opa5.assert.ok(true, `the popover "${title}" is open`);
        },
        errorMessage: `The comment popover "${title}" is not open`
    });
}

function iWriteAComment(When: Common, text: string): void {
    When.waitFor({
        id: "commentDraft",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text, keepFocus: true }),
        errorMessage: "No comment text area"
    });
    When.waitFor({
        id: "commentSave",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new Press(),
        errorMessage: "Save is not enabled"
    });
}

function theMarkerIs(Then: Common, index: number, text: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: [bindingPath(`/artifact/doc/blocks/${index}`), (c: UI5Element) => c.getId().includes("docMarker")],
        check: (buttons: UI5Element[]) => (buttons[0] as Button).getText() === text,
        success: function (buttons: UI5Element[]) {
            const dom = buttons[0].getDomRef()!;
            const labelIds = (dom.getAttribute("aria-labelledby") ?? "").split(" ");
            const label = labelIds.map((id) => Opa5.getWindow().document.getElementById(id)?.textContent ?? "").join(" ");
            Opa5.assert.ok(label.includes(`Block ${index + 1}`), `the marker is named by its paragraph: ${label}`);
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The marker of paragraph ${index + 1} does not read "${text}"`
    });
}

function iPressMarker(When: Common, index: number): void {
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: [bindingPath(`/artifact/doc/blocks/${index}`), (c: UI5Element) => c.getId().includes("docMarker")],
        actions: new Press(),
        errorMessage: `No marker on paragraph ${index + 1}`
    });
}

/** The inline state of the first comment under paragraph `index`. */
function theInlineStateIs(Then: Common, index: number, text: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: [bindingPath(`/artifact/doc/blocks/${index}/comments/0`)],
        check: (list: UI5Element[]) => list.some((c) => (c as unknown as { getText(): string }).getText() === text),
        success: function (list: UI5Element[]) {
            Opa5.assert.ok((list[0] as unknown as { getIcon(): string }).getIcon(), "the state has an icon too, not only a colour");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The comment under paragraph ${index + 1} is not "${text}"`
    });
}

function iPressInPopover(When: Common, idPart: string, row = 0): void {
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: [bindingPath(`/pop/comments/${row}`), (c: UI5Element) => c.getId().includes(idPart)],
        actions: new Press(),
        errorMessage: `No ${idPart} button on comment ${row}`
    });
}

function thePopoverStateIs(Then: Common, text: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: [bindingPath("/pop/comments/0")],
        check: (list: UI5Element[]) => list.some((c) => (c as unknown as { getText(): string }).getText() === text),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The comment in the popover is not "${text}"`
    });
}

opaTest("a plan shows its versions and the design it is based on; the pinned design version is marked approved", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=plan&version=2", undefined, (fake) => {
        const s = fake.sessions[0].session;
        s.stage = "plan";
        fake.addArtifact(s.id, "design", "# Design v1");
        fake.addArtifact(s.id, "design", "# Design v2");
        fake.dataOf(s.id)!.pins.design = 2;
        fake.addArtifact(s.id, "plan", "# Plan v1");
        fake.addArtifact(s.id, "plan", "# Plan v2\n\nTest first.");
    });
    theDocumentShows(Then, "Plan v2", "the deep link opens plan v2");
    theVersionsAre(Then, ["v1", "v2"], "2", "the switcher lists v1 · v2 with v2 selected");
    Then.waitFor({
        id: "docBasedOn",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "based on Design v2" }),
        success: function () {
            Opa5.assert.ok(true, "the plan names the design version it was written against");
        }
    });
    iPress(When, "docBasedOn");
    theDocumentShows(Then, "Design v2", "based on opens the design version");
    theVersionsAre(Then, ["v1", "v2 (approved)"], "2", "the pinned design version says approved");
    Then.waitFor({
        success: function () {
            Opa5.assert.ok(/kind=design&version=2/.test(HashChanger.getInstance().getHash()), "the hash names it (deep link)");
        }
    });
    When.waitFor({
        controlType: "sap.m.SegmentedButtonItem",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "v1" }),
        actions: new Press(),
        errorMessage: "No v1 in the switcher"
    });
    theDocumentShows(Then, "Design v1", "the switcher opens another version");
    Then.iStopTheApp();
});

opaTest("keyboard: Enter or C on a paragraph opens the comment popover; Esc closes it and the focus returns", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, designDoc);
    theDocumentShows(Then, "Design v1", "design v1 is shown");
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        success: function () {
            const blocks = domOf("artifactContent")!.querySelectorAll(".ideDocBlock");
            Opa5.assert.strictEqual(blocks.length, 4, "four paragraphs");
            Opa5.assert.deepEqual(Array.from(blocks).map((b) => b.getAttribute("tabindex")), ["0", "-1", "-1", "-1"],
                "one tab stop; the arrow keys reach the others");
        }
    });
    iPressKey(When, 2, "ENTER");
    thePopoverIsOpen(Then, "Comments on block 3");
    When.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            const dom = control.getDomRef() as HTMLElement;
            QUnitUtils.triggerKeydown(dom, "ESCAPE");
            QUnitUtils.triggerKeyup(dom, "ESCAPE");
        }
    });
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        visible: false,
        check: (control: UI5Element) => !(control as Popover).isOpen() && activePara() === "2",
        success: function () {
            Opa5.assert.ok(true, "Esc closed the popover and the paragraph has the focus again");
        },
        errorMessage: "The popover did not close back onto the paragraph"
    });
    iPressKey(When, 1, "C");
    thePopoverIsOpen(Then, "Comments on block 2");
    iWriteAComment(When, "Name the currency table.");
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => activePara() === "1",
        success: function () {
            Opa5.assert.ok(true, "after Save the focus is back on the paragraph");
        },
        errorMessage: "The focus did not return to the paragraph"
    });
    theMarkerIs(Then, 1, "1", "the marker counts the new comment");
    Then.waitFor({
        success: function () {
            const body = backend.bodies.find((b) => b.key === "POST sessions/s-1/comments")?.body;
            Opa5.assert.deepEqual(body, { anchor: "document", kind: "design", version: 1, paragraph: 1, body: "Name the currency table.", quote: "First paragraph." },
                "the comment is anchored to design v1, block 1");
        }
    });
    Then.iStopTheApp();
});

opaTest("comment -> Request changes (1) -> dialog -> sent -> addressed with the answer -> v2 selected -> reopen / dismiss gate", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, designDoc);
    theDocumentShows(Then, "Design v1", "design v1 is shown");
    iPressKey(When, 2, "ENTER");
    thePopoverIsOpen(Then, "Comments on block 3");
    iWriteAComment(When, "Explain the rounding.");
    theMarkerIs(Then, 2, "1", "paragraph 3 has one comment");
    theInlineStateIs(Then, 2, "Open", "the comment is shown under its paragraph as Open");
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: [new PropertyStrictEquals({ name: "text", value: "Request changes (1)" }),
            new PropertyStrictEquals({ name: "enabled", value: true })],
        success: function () {
            Opa5.assert.ok(true, "the header counts the open comment");
        }
    });
    theReasonIs(Then, "Resolve or dismiss the 1 open review comment first.", "the open comment blocks the primary action");
    iPress(When, "requestChangesButton");
    Then.waitFor({
        id: "requestChangesList",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (control: UI5Element) => (control as List).getItems().length === 1,
        success: function (control: UI5Element) {
            const text = (control as List).getItems()[0].getDomRef()?.textContent ?? "";
            Opa5.assert.ok(text.includes("Design v1, block 3"), "the dialog lists the comment by its anchor");
            Opa5.assert.ok(text.includes("Explain the rounding."), "with its text");
        },
        errorMessage: "The dialog does not list the open comment"
    });
    When.waitFor({
        id: "requestChangesNote",
        ...SOPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Keep it short." }),
        success: function () {
            release = backend.pauseStream();
        }
    });
    When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "No Send" });
    theInlineStateIs(Then, 2, "Sent", "while the run works the comment is Sent");
    Then.waitFor({
        success: function () {
            const body = backend.bodies.find((b) => b.key === "POST sessions/s-1/request-changes")?.body;
            Opa5.assert.deepEqual(body, { note: "Keep it short." }, "the run got the note");
            release();
        }
    });
    theDocumentShows(Then, "Design v2", "the reworked version is shown when it is stored");
    theVersionsAre(Then, ["v1", "v2"], "2", "and selected in the switcher");
    Then.waitFor({
        id: "docOtherComments",
        ...SOPTS,
        check: (control: UI5Element) => {
            const text = control.getDomRef()?.textContent ?? "";
            return text.includes("Addressed") && text.includes("Assistant: Addressed: Explain the rounding.");
        },
        success: function () {
            Opa5.assert.ok(true, "the comment on v1 is listed as Addressed with the agent's answer");
        },
        errorMessage: "No addressed comment with its answer"
    });
    thePrimaryActionIs(Then, "Approve design (v2) and plan", true, "an addressed comment does not block");
    When.waitFor({
        controlType: "sap.m.Link",
        ...SOPTS,
        matchers: [bindingPath("/artifact/doc/others/0")],
        actions: new Press(),
        errorMessage: "No link to the commented version"
    });
    theDocumentShows(Then, "Design v1", "the link opens the version the comment is on");
    theInlineStateIs(Then, 2, "Addressed", "the comment is Addressed under its paragraph");
    iPressMarker(When, 2);
    thePopoverIsOpen(Then, "Comments on block 3");
    iPressInPopover(When, "commentReopen");
    thePopoverStateIs(Then, "Open", "reopened");
    Then.waitFor({
        controlType: "sap.m.CustomListItem",
        ...SOPTS,
        searchOpenDialogs: true,
        matchers: [bindingPath("/pop/comments/0")],
        check: (items: UI5Element[]) => !!items[0].getDomRef()?.contains(Opa5.getWindow().document.activeElement),
        success: function () {
            Opa5.assert.ok(true, "the focus stays on the comment acted on");
        },
        errorMessage: "The focus left the comment"
    });
    theReasonIs(Then, "Resolve or dismiss the 1 open review comment first.", "a reopened comment blocks again");
    iPressInPopover(When, "commentDismiss");
    thePopoverStateIs(Then, "Dismissed", "dismissed");
    // Final review m1: the comment no longer blocks, but v1 is open: approve waits for v2 to be shown.
    thePrimaryActionIs(Then, "Approve design (v2) and plan", false, "dismissing it lifts the comment gate; v1 is still open");
    theReasonIs(Then, "You are looking at v1 \u2014 open v2 to approve", "and the reason says which version to open");
    Then.iStopTheApp();
});

opaTest("a hostile comment and answer are shown literally, never as markup or bindings", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = designDoc(fake);
        fake.dataOf(sid)!.comments.push(docComment("c-x", 1, HOSTILE, { state: "addressed", answer: "{/session/title} <b>bold</b>" }));
    });
    theDocumentShows(Then, "Design v1", "design v1 is shown");
    theInlineStateIs(Then, 1, "Addressed", "the seeded comment is shown");
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        success: function (control: UI5Element) {
            const dom = control.getDomRef()!;
            Opa5.assert.ok(dom.textContent!.includes(HOSTILE), "the body is the literal text");
            Opa5.assert.ok(dom.textContent!.includes("Assistant: {/session/title} <b>bold</b>"), "and so is the answer");
            Opa5.assert.strictEqual(dom.querySelectorAll("img, b").length, 0, "no element was made from them");
        }
    });
    iPressMarker(When, 1);
    thePopoverIsOpen(Then, "Comments on block 2");
    iPressInPopover(When, "commentReopen");
    thePopoverStateIs(Then, "Open", "reopened");
    Then.waitFor({
        id: "commentPopover",
        ...SOPTS,
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            const dom = control.getDomRef()!;
            Opa5.assert.ok(dom.textContent!.includes(HOSTILE), "the popover shows it literally");
            Opa5.assert.strictEqual(dom.querySelectorAll("img").length, 0, "without an image");
            (control as Popover).close();
        }
    });
    iPress(When, "requestChangesButton");
    Then.waitFor({
        id: "requestChangesList",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (control: UI5Element) => (control as List).getItems().length === 1,
        success: function (control: UI5Element) {
            const dom = control.getDomRef()!;
            Opa5.assert.ok(dom.textContent!.includes(HOSTILE), "the dialog shows it literally too");
            Opa5.assert.strictEqual(dom.querySelectorAll("img").length, 0, "without an image");
        }
    });
    When.waitFor({ id: "requestChangesCancel", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.iStopTheApp();
});

opaTest("comments held back by the server's cap are announced as left for the next round", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1?view=document&kind=design&version=1", undefined, (fake) => {
        const sid = designDoc(fake);
        fake.sendCap = 1;
        fake.resolveNoComments = false;
        fake.dataOf(sid)!.comments.push(docComment("c-1", 1, "First."), docComment("c-2", 2, "Second."));
    });
    theDocumentShows(Then, "Design v1", "design v1 is shown");
    iPress(When, "requestChangesButton");
    When.waitFor({ id: "requestChangesSend", ...SOPTS, searchOpenDialogs: true, actions: new Press(), errorMessage: "No Send" });
    Then.waitFor({
        id: "runErrorStrip",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "1 more open comment will be sent in the next round." }),
        success: function () {
            Opa5.assert.ok(true, "the held-back comment is announced");
        },
        errorMessage: "No note on the comment left for the next round"
    });
    Then.waitFor({
        id: "requestChangesButton",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Request changes (1)" }),
        success: function () {
            Opa5.assert.ok(true, "and it is still open for the next round");
        }
    });
    Then.iStopTheApp();
});

opaTest("a diagnose report has no comment markers", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    Given.iStartTheApp("sessions", undefined, (fake) => {
        fake.allowDiagnose();
        sid = fake.addSession("Why is the order list slow?", [], [], "diagnose").id;
        fake.addArtifact(sid, "report", "# Report\n\nThe index is missing.");
    });
    Then.waitFor({
        success: function () {
            HashChanger.getInstance().setHash(`sessions/${sid}?view=document&kind=report&version=1`);
        }
    });
    theDocumentShows(Then, "Report v1", "the report is shown");
    Then.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        visible: false,
        matchers: [bindingPath("/artifact/doc/blocks/0"), (c: UI5Element) => c.getId().includes("docMarker")],
        success: function (buttons: UI5Element[]) {
            Opa5.assert.notOk((buttons[0] as Control).getVisible(), "no comments on a diagnose report");
        }
    });
    Then.iStopTheApp();
});
