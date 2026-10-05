import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type CustomListItem from "sap/m/CustomListItem";
import type Link from "sap/m/Link";
import type MessageStrip from "sap/m/MessageStrip";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type View from "sap/ui/core/mvc/View";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, thePrimaryActionIs } from "./pages/Session";
import { closeMessageBox } from "./pages/Shared";
import { posts, seedChanges, theDiffIsShown, win } from "./ReviewFollowupsJourney";
import type FakeBackend from "./FakeBackend";
import type { DiagnoseFinding } from "../../service/types";

/**
 * Fix round 1 of the U11 review (diagnose on the session page): Review
 * changes cannot turn into an approve of cards nobody saw, the source view's
 * ADT link and line rules, the details editor's name, the banner's retention
 * from `/me`, rows without a program, and the handover's double-submit guard.
 */
QUnit.module("Diagnose page review fixes (U11 fix round 1)");

const SID = "s-2";
const POOL = "ZCL_ORDER_QUERY".padEnd(30, "=") + "CP";
const BANNER_START = "Diagnose session on a non-production system: dumps and traces are sent to the AI model as they are and ";

function hash(): string {
    return HashChanger.getInstance().getHash();
}

function sModel(control: UI5Element): JSONModel {
    let el = control as UI5Element | null;
    while (el && !el.isA("sap.ui.core.mvc.View")) {
        el = (el as UI5Element).getParent() as UI5Element | null;
    }
    return (el as unknown as View).getModel("s") as JSONModel;
}

/** A mouse event as the browser sends it on the button's DOM (UI5 turns it into touchstart/touchend/tap). */
function mouse(el: Element, type: string): void {
    const W = win() as unknown as { MouseEvent: typeof MouseEvent };
    el.dispatchEvent(new W.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: type === "mousedown" ? 1 : 0, view: win() }));
}

/** An Enter keydown on `el` (`repeat` as a held key sends it), or the keyup that ends it. */
function enter(el: Element, repeat: boolean, type = "keydown"): void {
    const W = win() as unknown as { KeyboardEvent: typeof KeyboardEvent };
    const ev = new W.KeyboardEvent(type, { key: "Enter", code: "Enter", repeat, bubbles: true, cancelable: true });
    Object.defineProperty(ev, "keyCode", { get: () => 13 });
    Object.defineProperty(ev, "which", { get: () => 13 });
    el.dispatchEvent(ev);
}

/** Waits `ms`, then: no approve was sent. */
function noApproveAfter(Then: Common, ms: number, message: string): void {
    let start = 0;
    Then.waitFor({
        check: () => {
            start ||= Date.now();
            return Date.now() - start >= ms;
        },
        success: () => Opa5.assert.strictEqual(posts("POST sessions/s-1/approve"), 0, message)
    });
}

function theApproveIsSent(Then: Common): void {
    Then.waitFor({
        check: () => posts("POST sessions/s-1/approve") === 1,
        success: () => Opa5.assert.ok(true, "a deliberate press after the cards are visible approves"),
        errorMessage: "No approve"
    });
}

// --- #1: Review changes -> Approve changes flip ------------------------------------------

opaTest("#1 a double click on Review changes approves nothing; a deliberate press once the cards show does", function (Given: Common, When: Common, Then: Common) {
    const second = { pressed: false };
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    thePrimaryActionIs(Then, "Review changes", true, "without the cards the action reviews");
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        success: function (button: UI5Element) {
            const model = sModel(button);
            new Press().executeOn(button as Control);
            // The second click of a double click lands the moment the cards are on screen.
            const tick = (): void => {
                if (model.getProperty("/artifact/changes/loaded")) {
                    new Press().executeOn(button as Control);
                    second.pressed = true;
                } else {
                    win().requestAnimationFrame(tick);
                }
            };
            win().requestAnimationFrame(tick);
        }
    });
    theDiffIsShown(Then, 1);
    Then.waitFor({
        check: () => second.pressed,
        success: () => Opa5.assert.ok(/view=changes/.test(hash()), "the changes view is open")
    });
    noApproveAfter(Then, 1500, "the second click approved nothing");
    thePrimaryActionIs(Then, "Approve changes and review", true, "the cards are visible: Approve is on");
    iPress(When, "primaryAction");
    theApproveIsSent(Then);
    Then.iStopTheApp();
});

opaTest("#1 a press that started before the cards appeared (pointer down, a held Enter) approves nothing", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp("sessions/s-1", undefined, (fake) => { seedChanges(fake); });
    thePrimaryActionIs(Then, "Review changes", true, "without the cards the action reviews");
    When.waitFor({ success: () => { release = backend.hold("GET sessions/s-1/file"); } });
    iPress(When, "primaryAction");
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        check: () => backend.requests.includes("GET sessions/s-1/file"),
        success: function (button: UI5Element) {
            // While the cards load: the pointer goes down, an Enter starts (its own press is another Review).
            mouse((button as Control).getDomRef()!, "mousedown");
            enter((button as Control).getDomRef()!, false);
            release();
        }
    });
    theDiffIsShown(Then, 1);
    thePrimaryActionIs(Then, "Approve changes and review", true, "the cards are visible");
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        success: function (button: UI5Element) {
            const dom = (button as Control).getDomRef()!;
            // The pointer comes up and the held Enter repeats, both after the cards appeared.
            mouse(dom, "mouseup");
            mouse(dom, "click");
            enter(dom, true);
            enter(dom, false, "keyup");
        }
    });
    noApproveAfter(Then, 1000, "presses that began before the cards approved nothing");
    iPress(When, "primaryAction");
    theApproveIsSent(Then);
    Then.iStopTheApp();
});

// --- #2 / #4: the source view's ADT link and line ----------------------------------------

const TEST_INCLUDE = "src/CLAS/zcl_x.clas.testclasses.abap";
const MAIN = "src/CLAS/zcl_x.clas.abap";

function sourceFiles(fake: FakeBackend): void {
    const s = fake.sessions[0].session;
    s.target = "DEMO";
    const data = fake.dataOf(s.id)!;
    const five = Array.from({ length: 5 }, (_, i) => `* line ${i + 1}`).join("\n");
    data.files.push(
        { path: TEST_INCLUDE, state: "read", object_type: "CLAS", object_name: "ZCL_X", origin_source: five, proposed_source: "" },
        { path: MAIN, state: "read", object_type: "CLAS", object_name: "ZCL_X", origin_source: five, proposed_source: "" },
        { path: "src/PROG/zempty.prog.abap", state: "read", object_type: "PROG", object_name: "ZEMPTY", origin_source: "", proposed_source: "", base_status: "sap" }
    );
}

function openSource(When: Common, path: string, line: number): void {
    When.waitFor({
        id: "sessionPage",
        ...SOPTS,
        visible: false,
        success: () => HashChanger.getInstance().setHash(`sessions/s-1?view=source&path=${encodeURIComponent(encodeURIComponent(path))}&line=${line}`)
    });
}

function theAdtLinkIs(Then: Common, href: string, message: string): void {
    Then.waitFor({
        id: "sourceAdt",
        ...SOPTS,
        check: (link: UI5Element) => (link as Link).getHref() === href,
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `The ADT link is not ${href}`
    });
}

function theHintIs(Then: Common, text: string, message: string): void {
    Then.waitFor({
        id: "sourceHint",
        ...SOPTS,
        check: (strip: UI5Element) => (strip as MessageStrip).getText() === text,
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `The note is not "${text}"`
    });
}

function theHighlightedLineIs(Then: Common, line: number | null, message: string): void {
    Then.waitFor({
        id: "sourceHtml",
        ...SOPTS,
        visible: false,
        check: () => !!win().document.querySelector("[id$='--sourceHtml'] tr[data-line]")
            || !!win().document.querySelector("[id$='--sourceHtml']"),
        success: function () {
            const current = win().document.querySelector("[id$='--sourceHtml'] tr[aria-current='true']");
            Opa5.assert.strictEqual(current ? Number(current.getAttribute("data-line")) : null, line, message);
        }
    });
}

opaTest("#2 a class include's source links ADT to that include at the line", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, sourceFiles);
    openSource(When, TEST_INCLUDE, 3);
    theAdtLinkIs(Then, "adt://DEMO/sap/bc/adt/oo/classes/zcl_x/includes/testclasses#start=3,0",
        "the test classes include, not the class's main source");
    theHighlightedLineIs(Then, 3, "line 3 is highlighted");
    Then.iStopTheApp();
});

opaTest("#4 a line beyond the source: no highlight on the last line, a note, ADT without a line", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1", undefined, sourceFiles);
    openSource(When, MAIN, 40);
    theHintIs(Then, "Line 40 is beyond the source's last line (5): no line is highlighted.", "the note says why");
    theAdtLinkIs(Then, "adt://DEMO/sap/bc/adt/oo/classes/zcl_x/source/main", "ADT opens the class without a line");
    theHighlightedLineIs(Then, null, "no line is highlighted (not the last one)");
    openSource(When, "src/PROG/zempty.prog.abap", 3);
    theHintIs(Then, "The source is empty: line 3 cannot be highlighted.", "an empty source says so");
    theAdtLinkIs(Then, "adt://DEMO/sap/bc/adt/programs/programs/zempty/source/main", "ADT without a line");
    openSource(When, MAIN, 5);
    Then.waitFor({
        id: "sourceHint",
        ...SOPTS,
        visible: false,
        check: (strip: UI5Element) => !(strip as Control).getVisible(),
        success: () => Opa5.assert.ok(true, "a line inside the source has no note")
    });
    theHighlightedLineIs(Then, 5, "the last line itself is highlighted");
    Then.iStopTheApp();
});

// --- Diagnose session --------------------------------------------------------------------

function diagnose(fake: FakeBackend, findings: Omit<DiagnoseFinding, "id">[] = []): void {
    fake.allowDiagnose("DEMO");
    const s = fake.addSession("Why is the order list slow?", [], [], "diagnose");
    s.target = "DEMO";
    const data = fake.dataOf(s.id)!;
    findings.forEach((f, i) => data.findings.push({ ...f, id: `f-${i + 1}` }));
}

const DUMP: Omit<DiagnoseFinding, "id"> = {
    kind: "dump", ref_id: "DUMP-1", title: "TSV_TNEW_PAGE_ALLOC_FAILED", program: POOL, include: null, line: 12,
    occurred_at: "2026-10-03T13:52:00", created_at: "2026-10-03T13:53:00"
};

function theBannerIs(Then: Common, text: string, message: string): void {
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: text }),
        success: () => Opa5.assert.ok(true, message),
        errorMessage: `The banner is not "${text}"`
    });
}

opaTest("#7 the banner takes the retention from /me; 0 means kept until the session is deleted", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => {
        diagnose(fake);
        fake.diagnoseRetentionDays = 30;
    });
    theBannerIs(Then, `${BANNER_START}kept with this session for 30 days.`, "30 days, as the server says");
    Then.iStopTheApp();
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => {
        diagnose(fake);
        fake.diagnoseRetentionDays = 0;
    });
    theBannerIs(Then, `${BANNER_START}kept until you delete the session.`, "0: kept until deleted");
    Then.iStopTheApp();
});

opaTest("#3 the details editor's text area has its accessible name", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => { diagnose(fake, [DUMP]); });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--findingDetails-"),
        actions: new Press(),
        errorMessage: "No Details"
    });
    Then.waitFor({
        id: "findingDetailEditor",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => (c as Control).getDomRef()?.querySelector("textarea")?.getAttribute("aria-label") === "Finding details",
        success: () => Opa5.assert.ok(true, "the text area is named \"Finding details\""),
        errorMessage: "The details editor's text area has no name"
    });
    Then.iStopTheApp();
});

opaTest("#9 a finding without a program is not an action and says why", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => {
        diagnose(fake, [{ ...DUMP, ref_id: "DUMP-2", program: null, line: null }, DUMP]);
    });
    Then.waitFor({
        controlType: "sap.m.CustomListItem",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getBindingContext("s")?.getPath() === "/artifact/findings/items/0",
        success: function (items: UI5Element[]) {
            Opa5.assert.strictEqual((items[0] as CustomListItem).getType(), "Inactive", "the row without a program is inactive");
        },
        errorMessage: "No row without a program"
    });
    Then.waitFor({
        controlType: "sap.m.CustomListItem",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getBindingContext("s")?.getPath() === "/artifact/findings/items/1",
        success: function (items: UI5Element[]) {
            Opa5.assert.strictEqual((items[0] as CustomListItem).getType(), "Active", "a row with a program opens its source");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getId().includes("--findingNoSource-")
            && c.getBindingContext("s")?.getPath() === "/artifact/findings/items/0",
        success: function (texts: UI5Element[]) {
            Opa5.assert.strictEqual((texts[0] as unknown as { getText(): string }).getText(), "SAP recorded no program for this finding, so there is no source to open.",
                "the row says why");
        },
        errorMessage: "The row does not say why it cannot be opened"
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        visible: false,
        matchers: (c: UI5Element) => c.getId().includes("--findingNoSource-")
            && c.getBindingContext("s")?.getPath() === "/artifact/findings/items/1",
        success: function (texts: UI5Element[]) {
            Opa5.assert.notOk((texts[0] as Control).getVisible(), "a row with a program has no such text");
        }
    });
    Then.iStopTheApp();
});

opaTest("#12 hand over: a second confirmation while the first is on its way sends nothing more", function (Given: Common, When: Common, Then: Common) {
    let release!: () => void;
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => {
        diagnose(fake, [DUMP]);
        fake.addArtifact(SID, "report", "# Report\n\nRoot cause.");
        release = fake.hold(`POST sessions/${SID}/handover`);
    });
    thePrimaryActionIs(Then, "Hand over to a change", true, "a report exists");
    iPress(When, "primaryAction");
    closeMessageBox(When, "OK");
    When.waitFor({
        check: () => backend.requests.includes(`POST sessions/${SID}/handover`),
        success: () => HashChanger.getInstance().setHash(`sessions/${SID}?view=findings`)
    });
    When.waitFor({
        id: "findingsList",
        ...SOPTS,
        success: () => undefined
    });
    // Whatever the header shows after that refresh, a second hand over is not sent.
    When.waitFor({
        id: "primaryAction",
        ...SOPTS,
        visible: false,
        success: (button: UI5Element) => { (button as Button).firePress(); }
    });
    closeMessageBox(When, "OK");
    When.waitFor({ success: () => release() });
    Then.waitFor({
        id: "sessionTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Change: Why is the order list slow?" }),
        success: () => Opa5.assert.strictEqual(posts(`POST sessions/${SID}/handover`), 1, "one handover"),
        errorMessage: "The new change session is not shown"
    });
    Then.iStopTheApp();
});

