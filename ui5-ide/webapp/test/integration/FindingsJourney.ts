import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type List from "sap/m/List";
import type CustomListItem from "sap/m/CustomListItem";
import type Text from "sap/m/Text";
import type Icon from "sap/ui/core/Icon";
import type Dialog from "sap/m/Dialog";
import type MessageStrip from "sap/m/MessageStrip";
import type TabContainer from "sap/m/TabContainer";
import type CodeEditor from "sap/ui/codeeditor/CodeEditor";
import type JSONModel from "sap/ui/model/json/JSONModel";
import Press from "sap/ui/test/actions/Press";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { OPTS, announced, answerShown, closeMessageBox, inTab, pressSession, recordAnnouncements, send } from "./pages/Assistant";
import type FakeBackend from "./FakeBackend";

QUnit.module("Findings journey");

/** A diagnose session (newest, so selected) with two findings, newest first. */
function diagnoseWithFindings(fake: FakeBackend): void {
    fake.allowDiagnose();
    const session = fake.addSession("Why does the order dump?", [], [], "diagnose");
    fake.dataOf(session.id)?.findings.push(
        {
            id: "f-b", kind: "trace", ref_id: "TRC-7", title: "Slow order list", program: null, include: null,
            line: null, occurred_at: null, created_at: "2026-10-03T09:00:00Z"
        },
        {
            id: "f-a", kind: "dump", ref_id: "DUMP-1", title: "CX_SY_ZERODIVIDE", program: "ZCL_ORDER=====CP",
            include: "ZCL_ORDER=====CM003", line: 42, occurred_at: null, created_at: "2026-10-03T08:00:00Z"
        }
    );
}

/** What one findings row shows: title, position, kind and icon. */
interface Row { getTitle(): string; getDescription(): string; getInfo(): string; getIcon(): string; getType(): string }

function rows(control: UI5Element): Row[] {
    return ((control as List).getItems() as CustomListItem[]).map((item) => {
        const texts = item.findAggregatedObjects(true, (c) => c.isA("sap.m.Text")) as Text[];
        const icon = item.findAggregatedObjects(true, (c) => c.isA("sap.ui.core.Icon"))[0] as Icon;
        return {
            getTitle: () => texts[0].getText(false),
            getDescription: () => texts[1].getText(false),
            getInfo: () => texts[2].getText(false),
            getIcon: () => icon.getSrc(),
            getType: () => item.getType()
        };
    });
}

/** A third finding, in the main source of a class: its line maps to the opened file. */
const CALC = "src/CLAS/zcl_demo_calc.clas.abap";
function withExactLine(fake: FakeBackend): void {
    diagnoseWithFindings(fake);
    fake.sessions[fake.sessions.length - 1].findings.unshift({
        id: "f-c", kind: "dump", ref_id: "DUMP-2", title: "COMPUTE_INT_ZERODIVIDE", program: "ZCL_DEMO_CALC=================CP",
        include: null, line: 12, occurred_at: null, created_at: "2026-10-03T09:30:00Z"
    });
}

function ofFinding(fid: string) {
    return (control: UI5Element): boolean => control.getBindingContext("ide")?.getProperty("id") === fid;
}

function pressFinding(When: Common, fid: string): void {
    When.waitFor({
        controlType: "sap.m.CustomListItem",
        ...OPTS,
        matchers: ofFinding(fid),
        actions: new Press(),
        errorMessage: `No finding ${fid} in the list`
    });
}

/** The rows Ace highlights as a finding's line in the editor of the tab `key`. */
function markedRows(editor: UI5Element): number[] {
    interface Marker { clazz: string; range: { start: { row: number } } }
    const ace = (editor as unknown as {
        getInternalEditorInstance(): { getSession(): { getMarkers(front: boolean): Record<string, Marker> } };
    }).getInternalEditorInstance();
    return Object.values(ace.getSession().getMarkers(false)).filter((m) => m.clazz === "ideFindingLine").map((m) => m.range.start.row);
}

function hookLine(): string | null | undefined {
    return document.querySelector(".ideEditorTab")?.getAttribute("data-finding-line");
}
opaTest("a diagnose session lists its findings with kind, position and icon; a change session has no list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnoseWithFindings);

    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function (control: UI5Element) {
            const [trace, dump] = rows(control);
            Opa5.assert.strictEqual(trace.getTitle(), "Slow order list", "newest first");
            Opa5.assert.strictEqual(trace.getInfo(), "Trace", "the kind is shown as text");
            Opa5.assert.strictEqual(trace.getIcon(), "sap-icon://performance");
            Opa5.assert.strictEqual(trace.getDescription(), "No source position", "a finding without a program says so");
            Opa5.assert.strictEqual(dump.getTitle(), "CX_SY_ZERODIVIDE");
            Opa5.assert.strictEqual(dump.getInfo(), "Dump");
            Opa5.assert.strictEqual(dump.getIcon(), "sap-icon://error");
            Opa5.assert.strictEqual(dump.getDescription(), "ZCL_ORDER=====CP · ZCL_ORDER=====CM003 · line 42");
            Opa5.assert.strictEqual(dump.getType(), "Navigation", "a finding leads to its source");
            const labelled = (control as List).getAriaLabelledBy();
            Opa5.assert.ok(labelled.some((id) => id.endsWith("findingsTitle")), "the list is labelled by its title");
        },
        errorMessage: "The findings list does not show the two findings"
    });
    Then.waitFor({
        id: "findingsTitle",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Findings (2)" }),
        success: function () {
            Opa5.assert.ok(true, "the title counts the findings");
        },
        errorMessage: "The findings title does not count 2"
    });

    pressSession(When, "Explain the order class");
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual(rows(control).length, 0, "and the other session's findings are gone");
            Opa5.assert.ok(!backend.requests.includes("GET sessions/s-1/findings"), "a change session does not ask for findings");
        },
        errorMessage: "The findings list is shown for a change session"
    });
    Then.iStopTheApp();
});

opaTest("a run's finding frames add and update rows, and the list is reloaded when the run ends", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        diagnoseWithFindings(fake);
        fake.scriptRun({
            findings: [
                { kind: "gateway_error", ref_id: "GW-3", title: "Order service 500", program: "ZCL_ORDER_DPC_EXT=CP", line: 9 },
                { kind: "dump", ref_id: "DUMP-1", title: "CX_SY_ZERODIVIDE (3 times)" }
            ]
        });
    });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function () {
            Opa5.assert.ok(true, "two findings to start with");
        }
    });
    send(When, "Check the gateway errors too");
    answerShown(Then, 2, "investigate", "the run finished");
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 3 }),
        success: function (control: UI5Element) {
            const [gateway, trace, dump] = rows(control);
            Opa5.assert.strictEqual(gateway.getTitle(), "Order service 500", "the new finding is on top");
            Opa5.assert.strictEqual(gateway.getInfo(), "Gateway error");
            Opa5.assert.strictEqual(gateway.getIcon(), "sap-icon://chain-link");
            Opa5.assert.strictEqual(gateway.getDescription(), "ZCL_ORDER_DPC_EXT=CP · line 9");
            Opa5.assert.strictEqual(trace.getTitle(), "Slow order list", "the others keep their place");
            Opa5.assert.strictEqual(dump.getTitle(), "CX_SY_ZERODIVIDE (3 times)", "a known finding is updated in place");
            const loads = backend.requests.filter((r) => /^GET sessions\/[^/]+\/findings$/.test(r));
            Opa5.assert.strictEqual(loads.length, 2, "loaded on select and again when the run ended");
        },
        errorMessage: "The finding frames did not reach the list"
    });
    Then.iStopTheApp();
});

opaTest("a finding the run stored without a frame shows up when the run ends", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    Given.iStartTheApp("", undefined, (fake) => {
        diagnoseWithFindings(fake);
        sid = fake.sessions[fake.sessions.length - 1].session.id;
        fake.scriptRun({});
    });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function () {
            // What a stopped or broken stream leaves behind: the row is stored, no frame was seen.
            backend.dataOf(sid)?.findings.unshift({
                id: "f-c", kind: "auth_check", ref_id: "AUTH-1", title: "S_TCODE missing", program: null, include: null,
                line: null, occurred_at: null, created_at: "2026-10-03T10:00:00Z"
            });
        }
    });
    send(When, "Anything about authorizations?");
    answerShown(Then, 2, "investigate", "the run finished");
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 3 }),
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual(rows(control)[0].getTitle(), "S_TCODE missing", "the stored finding is listed, newest first");
        },
        errorMessage: "The findings were not reloaded after the run"
    });
    Then.iStopTheApp();
});

opaTest("a slow findings answer of the session left behind never reaches the next session", function (Given: Common, When: Common, Then: Common) {
    let slow = "";
    let other = "";
    let release: () => void = () => undefined;
    Given.iStartTheApp("", undefined, (fake) => {
        fake.allowDiagnose();
        other = `GET sessions/${fake.addSession("Slow reports", [], [], "diagnose").id}/findings`;
        diagnoseWithFindings(fake);
        slow = `GET sessions/${fake.sessions[fake.sessions.length - 1].session.id}/findings`;
        release = fake.hold(slow);
    });
    pressSession(When, "Slow reports");
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        autoWait: false,
        check: function () {
            return backend.responses.includes(other) && backend.requests.includes(slow);
        },
        success: function () {
            Opa5.assert.notOk(backend.responses.includes(slow), "the first session's findings are still in flight");
            release();
        },
        errorMessage: "The second session's findings were not loaded"
    });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        autoWait: false,
        check: function () {
            return backend.responses.includes(slow);
        },
        success: function () {
            Opa5.assert.ok(true, "the late answer arrived");
        }
    });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual(rows(control).length, 0, "and the open session still has no findings");
            Opa5.assert.strictEqual((control as List).getBusy(), false, "nor a busy list");
        },
        errorMessage: "The other session's findings leaked into the list"
    });
    Then.iStopTheApp();
});

opaTest("a finding opens its source in a tab with the line highlighted; a method include shows a hint instead", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withExactLine);
    Then.waitFor({
        id: "workspaceTree",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function () {
            Opa5.assert.ok(true, "a diagnose session has findings instead of the workspace tree");
        },
        errorMessage: "The workspace tree is shown for a diagnose session"
    });
    Then.waitFor({
        id: "openObjectSearch",
        ...OPTS,
        success: function () {
            Opa5.assert.ok(true, "Open object stays, to read source into a tab");
        }
    });

    pressFinding(When, "f-c");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${CALC}`),
        check: function (editors: UI5Element[]) {
            return markedRows(editors[0]).length === 1 && hookLine() === "12";
        },
        success: function (editors: UI5Element[]) {
            const editor = editors[0] as CodeEditor;
            Opa5.assert.deepEqual(markedRows(editor), [11], "Ace marks row 11, the finding's line 12");
            Opa5.assert.strictEqual(hookLine(), "12", "and the tab says which line is marked");
            Opa5.assert.ok(editor.getValue().includes("ZCL_DEMO_CALC line 12"), "the tab shows the class source");
            Opa5.assert.strictEqual(editor.getEditable(), false, "read-only");
            Opa5.assert.ok(backend.requests.some((r) => /^POST sessions\/[^/]+\/findings\/f-c\/open$/.test(r)), "the server opened the source");
        },
        errorMessage: "The finding's line is not highlighted"
    });
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        success: function (control: UI5Element) {
            const container = control as TabContainer;
            Opa5.assert.strictEqual(container.getItems().length, 1, "one tab");
            Opa5.assert.strictEqual(container.getItems()[0].getKey(), `file:${CALC}`, "for the class file");
        }
    });

    // A second press selects the tab again and leaves one marker.
    pressFinding(When, "f-c");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${CALC}`),
        check: function () {
            return backend.responses.filter((r) => r.endsWith("/findings/f-c/open")).length === 2;
        },
        success: function (editors: UI5Element[]) {
            Opa5.assert.deepEqual(markedRows(editors[0]), [11], "still one marker");
        }
    });
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control as TabContainer).getItems().length, 1, "and still one tab");
        }
    });

    pressFinding(When, "f-a");
    Then.waitFor({
        controlType: "sap.m.MessageStrip",
        ...OPTS,
        matchers: [inTab("file:src/CLAS/zcl_order.clas.abap"), new PropertyStrictEquals({ name: "visible", value: true })],
        success: function (strips: UI5Element[]) {
            const text = (strips[0] as MessageStrip).getText();
            Opa5.assert.ok(text.includes("ZCL_ORDER=====CM003") && text.includes("42"), `the hint names the include and its line: ${text}`);
        },
        errorMessage: "A method-include finding shows no hint"
    });
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab("file:src/CLAS/zcl_order.clas.abap"),
        check: function (editors: UI5Element[]) {
            return (editors[0] as CodeEditor).getValue().includes("ZCL_ORDER line 1");
        },
        success: function (editors: UI5Element[]) {
            Opa5.assert.deepEqual(markedRows(editors[0]), [], "no line is highlighted: the include line does not map to the class");
            Opa5.assert.strictEqual(hookLine(), "", "and the tab marks none");
        },
        errorMessage: "The class of the method include did not open"
    });
    Then.iStopTheApp();
});

opaTest("a finding without a program says there is no source to open", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnoseWithFindings);
    pressFinding(When, "f-b");
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        success: function (dialogs: UI5Element[]) {
            const dialog = dialogs[0] as Dialog;
            Opa5.assert.strictEqual(dialog.getIcon(), "sap-icon://information", "an information, not an error");
            Opa5.assert.ok(dialog.getDomRef()?.textContent?.includes("This finding has no source to open."), "it says why");
        },
        errorMessage: "No message for a finding without source"
    });
    closeMessageBox(When, "OK");
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        visible: false,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control as TabContainer).getItems().length, 0, "no tab was opened");
        }
    });
    Then.iStopTheApp();
});

opaTest("Show details opens the finding's text read-only, refreshes it from SAP and forgets it on close", function (Given: Common, When: Common, Then: Common) {
    function detailShows(part: string, message: string): void {
        Then.waitFor({
            controlType: "sap.ui.codeeditor.CodeEditor",
            searchOpenDialogs: true,
            check: function (editors: UI5Element[]) {
                return (editors[0] as CodeEditor).getValue().includes(part);
            },
            success: function (editors: UI5Element[]) {
                const editor = editors[0] as CodeEditor;
                Opa5.assert.strictEqual(editor.getEditable(), false, "read-only");
                Opa5.assert.strictEqual(editor.getType(), "text", "plain text, nothing is rendered as markup");
                Opa5.assert.ok(true, message);
            },
            errorMessage: `The detail dialog does not show '${part}'`
        });
    }
    Given.iStartTheApp("", undefined, diagnoseWithFindings);
    When.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        matchers: [ofFinding("f-a"), new PropertyStrictEquals({ name: "icon", value: "sap-icon://detail-view" })],
        actions: new Press(),
        success: function (buttons: UI5Element[]) {
            Opa5.assert.strictEqual((buttons[0] as UI5Element).getTooltip_AsString(), "Show details", "the icon button is named");
        },
        errorMessage: "The finding has no Show details button"
    });
    Then.waitFor({
        id: "findingDetailDialog",
        ...OPTS,
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((control as Dialog).getTitle(), "CX_SY_ZERODIVIDE", "titled with the finding");
            Opa5.assert.notOk(backend.requests.some((r) => r.endsWith("/findings/f-a/open")), "the source was not opened");
        },
        errorMessage: "The detail dialog did not open"
    });
    detailShows("raised by DEVUSER01 at line 42", "the stored detail text is shown");

    When.waitFor({ id: "findingDetailRefreshButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    detailShows("(re-read from SAP)", "Refresh from SAP reads the text again");

    // A second refresh that SAP answers with no_detail keeps the text already shown.
    Then.waitFor({
        id: "findingDetailRefreshButton",
        ...OPTS,
        searchOpenDialogs: true,
        success: function () {
            const sid = backend.sessions[backend.sessions.length - 1].session.id;
            backend.failNext = {
                path: `sessions/${sid}/findings/f-a`, status: 422,
                body: { detail: "This finding has no detail that can be read from SAP.", code: "no_detail" }
            };
        }
    });
    When.waitFor({ id: "findingDetailRefreshButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "findingDetailRefreshButton",
        ...OPTS,
        searchOpenDialogs: true,
        autoWait: false,
        visible: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        check: function () {
            return backend.failNext === undefined;
        },
        success: function () {
            Opa5.assert.ok(true, "Refresh is off: SAP has nothing more to read");
        },
        errorMessage: "Refresh stayed on after no_detail"
    });
    detailShows("(re-read from SAP)", "and the text read before is still shown, not replaced by a notice");

    When.waitFor({ id: "findingDetailCloseButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        check: function (control: UI5Element) {
            const model = (control as List).getModel("ide") as JSONModel;
            return model.getProperty("/findingDetail/text") === "" && model.getProperty("/findingDetail/id") === "";
        },
        success: function () {
            Opa5.assert.ok(true, "the text is gone from the model once the dialog is closed");
        },
        errorMessage: "The detail text stayed in the model"
    });
    Then.iStopTheApp();
});

// --- Finding view polish (Task 21 review) ------------------------------------

/** The slice of Ace the polish journeys look at. */
interface AceProbe {
    getSession(): { getScrollTop(): number; getLength(): number; $decorations: (string | undefined)[] };
    scrollToLine(line: number, center: boolean, animate: boolean): void;
}
function aceOf(editor: UI5Element): AceProbe {
    return (editor as unknown as { getInternalEditorInstance(): AceProbe }).getInternalEditorInstance();
}
function gutterRows(editor: UI5Element): number[] {
    const out: number[] = [];
    aceOf(editor).getSession().$decorations.forEach((cls, row) => {
        if ((cls ?? "").includes("ideFindingGutter")) {
            out.push(row);
        }
    });
    return out;
}

opaTest("pressing a finding whose tab is open re-reads the source, so the highlighted line matches the text", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withExactLine);
    pressFinding(When, "f-c");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${CALC}`),
        check: function (editors: UI5Element[]) {
            return markedRows(editors[0]).length === 1;
        },
        success: function () {
            // The program changed in SAP meanwhile; the server re-reads it on every open.
            const file = backend.sessions[backend.sessions.length - 1].files.find((f) => f.path === CALC);
            Opa5.assert.ok(file, "the class is in the workspace");
            if (file) {
                file.origin_source = Array.from({ length: 40 }, (_, i) => `* CHANGED line ${i + 1}`).join("\n");
            }
        },
        errorMessage: "The finding did not open"
    });
    pressFinding(When, "f-c");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${CALC}`),
        check: function (editors: UI5Element[]) {
            return (editors[0] as CodeEditor).getValue().includes("CHANGED line 12");
        },
        success: function (editors: UI5Element[]) {
            Opa5.assert.ok(true, "the open tab shows the source as it is now");
            Opa5.assert.deepEqual(markedRows(editors[0]), [11], "with the finding's line marked");
        },
        errorMessage: "The open tab kept the stale source"
    });
    Then.iStopTheApp();
});

opaTest("the finding line is also marked in the gutter and announced", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withExactLine);
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 3 }),
        success: recordAnnouncements
    });
    pressFinding(When, "f-c");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${CALC}`),
        check: function (editors: UI5Element[]) {
            return gutterRows(editors[0]).length === 1 && announced.some((t) => /highlighted/.test(t));
        },
        success: function (editors: UI5Element[]) {
            Opa5.assert.deepEqual(gutterRows(editors[0]), [11], "the gutter of row 11 carries the finding decoration");
            Opa5.assert.deepEqual(announced.filter((t) => /highlighted/.test(t)), ["Line 12 highlighted"], "announced once");
        },
        errorMessage: "No gutter decoration or announcement for the finding line"
    });
    // A method include highlights nothing: no decoration, no announcement.
    pressFinding(When, "f-a");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab("file:src/CLAS/zcl_order.clas.abap"),
        check: function (editors: UI5Element[]) {
            return (editors[0] as CodeEditor).getValue().includes("ZCL_ORDER line 1");
        },
        success: function (editors: UI5Element[]) {
            Opa5.assert.deepEqual(gutterRows(editors[0]), [], "no gutter decoration without a line");
            Opa5.assert.strictEqual(announced.filter((t) => /highlighted/.test(t)).length, 1, "and nothing more announced");
        }
    });
    Then.iStopTheApp();
});

const LONG = "src/PROG/zlong_report.prog.abap";
opaTest("the editor jumps to the finding line once: a later re-rendering keeps the reader's position", function (Given: Common, When: Common, Then: Common) {
    let renders = 0;
    let top = -1;
    Given.iStartTheApp("", undefined, (fake) => {
        diagnoseWithFindings(fake);
        const data = fake.sessions[fake.sessions.length - 1];
        data.files.push({
            path: LONG, state: "read", object_type: "PROG", object_name: "ZLONG_REPORT", proposed_source: "",
            origin_source: Array.from({ length: 400 }, (_, i) => `* ZLONG_REPORT line ${i + 1}`).join("\n")
        });
        data.findings.unshift({
            id: "f-long", kind: "dump", ref_id: "DUMP-9", title: "Far down", program: "ZLONG_REPORT", include: null,
            line: 300, occurred_at: null, created_at: "2026-10-03T09:45:00Z"
        });
    });
    pressFinding(When, "f-long");
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${LONG}`),
        check: function (editors: UI5Element[]) {
            return markedRows(editors[0]).length === 1 && aceOf(editors[0]).getSession().getScrollTop() > 0;
        },
        success: function (editors: UI5Element[]) {
            const editor = editors[0] as CodeEditor;
            const jumped = aceOf(editor).getSession().getScrollTop();
            Opa5.assert.ok(jumped > 0, "the first open scrolls to the finding line");
            // The reader scrolls somewhere else, then the editor is rendered again.
            aceOf(editor).scrollToLine(100, false, false);
            top = aceOf(editor).getSession().getScrollTop();
            Opa5.assert.ok(top > 0 && top !== jumped, "the reader moved away from the finding line");
            editor.addEventDelegate({ onAfterRendering: () => { renders++; } });
            editor.invalidate();
        },
        errorMessage: "The finding in the long report did not open at its line"
    });
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        ...OPTS,
        matchers: inTab(`file:${LONG}`),
        check: function () {
            return renders > 0;
        },
        success: function (editors: UI5Element[]) {
            Opa5.assert.strictEqual(aceOf(editors[0]).getSession().getScrollTop(), top, "the position is kept, not thrown back to the finding line");
            Opa5.assert.deepEqual(markedRows(editors[0]), [299], "the line is still marked");
        },
        errorMessage: "The editor was not rendered again"
    });
    Then.iStopTheApp();
});

opaTest("a diagnose session titles the object search 'Open object'; findings without detail text have no details button", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        diagnoseWithFindings(fake);
        fake.sessions[fake.sessions.length - 1].findings.push(
            {
                id: "f-auth", kind: "auth_check", ref_id: "AUTH-1", title: "S_TCODE missing", program: null, include: null,
                line: null, occurred_at: null, created_at: "2026-10-03T07:00:00Z"
            },
            {
                id: "f-odata", kind: "odata_call", ref_id: "OD-1", title: "Slow $batch", program: null, include: null,
                line: null, occurred_at: null, created_at: "2026-10-03T06:00:00Z"
            }
        );
    });
    Then.waitFor({
        id: "workspaceTitle",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Open object" }),
        success: function () {
            Opa5.assert.ok(true, "no orphan 'Workspace' heading above the search");
        },
        errorMessage: "The heading above the object search is not 'Open object' in a diagnose session"
    });
    Then.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "icon", value: "sap-icon://detail-view" }),
        check: function (buttons: UI5Element[]) {
            return buttons.length === 4;
        },
        success: function (buttons: UI5Element[]) {
            const shown: Record<string, boolean> = {};
            buttons.forEach((b) => {
                shown[String(b.getBindingContext("ide")?.getProperty("kind"))] = (b as unknown as { getVisible(): boolean }).getVisible();
            });
            Opa5.assert.deepEqual(shown, { trace: true, dump: true, auth_check: false, odata_call: false },
                "details only where SAP has a text to read");
        },
        errorMessage: "The findings rows did not render four details buttons"
    });
    pressSession(When, "Explain the order class");
    Then.waitFor({
        id: "workspaceTitle",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Workspace" }),
        success: function () {
            Opa5.assert.ok(true, "a change session keeps the Workspace heading");
        }
    });
    Then.iStopTheApp();
});

opaTest("the details dialog can be closed while it loads, names its editor, and says when SAP has no detail", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    let sid = "";
    Given.iStartTheApp("", undefined, (fake) => {
        diagnoseWithFindings(fake);
        sid = fake.sessions[fake.sessions.length - 1].session.id;
        release = fake.hold(`GET sessions/${sid}/findings/f-a`);
    });
    function pressDetails(): void {
        When.waitFor({
            controlType: "sap.m.Button",
            ...OPTS,
            matchers: [ofFinding("f-a"), new PropertyStrictEquals({ name: "icon", value: "sap-icon://detail-view" })],
            actions: new Press()
        });
    }
    pressDetails();
    Then.waitFor({
        id: "findingDetailDialog",
        ...OPTS,
        searchOpenDialogs: true,
        autoWait: false,
        check: function (control: UI5Element) {
            return (control as Dialog).isOpen() && backend.requests.includes(`GET sessions/${sid}/findings/f-a`);
        },
        success: function (control: UI5Element) {
            const dialog = control as Dialog;
            Opa5.assert.strictEqual(dialog.getBusy(), false, "the dialog itself is not busy while the text loads");
            const editor = dialog.getContent()[0] as CodeEditor;
            Opa5.assert.strictEqual(editor.getBusy(), true, "only its content is");
            Opa5.assert.strictEqual(
                editor.getDomRef()?.querySelector("textarea")?.getAttribute("aria-label"), "Finding details",
                "the editor has an accessible name");
        },
        errorMessage: "The details dialog did not open while its text loads"
    });
    // Close is usable while the request is in flight.
    When.waitFor({ id: "findingDetailCloseButton", ...OPTS, searchOpenDialogs: true, autoWait: false, actions: new Press() });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        check: function (control: UI5Element) {
            return ((control as List).getModel("ide") as JSONModel).getProperty("/findingDetail/id") === "";
        },
        success: function () {
            Opa5.assert.ok(true, "closed while loading");
            release();
        },
        errorMessage: "Close did not work while the details were loading"
    });
    Then.waitFor({
        id: "findingsList",
        ...OPTS,
        check: function () {
            return backend.responses.includes(`GET sessions/${sid}/findings/f-a`);
        },
        success: function () {
            // The late answer of the closed dialog is dropped; the next read is refused.
            backend.failNext = {
                path: `sessions/${sid}/findings/f-a`, status: 422,
                body: { detail: "This finding has no detail that can be read from SAP.", code: "no_detail" }
            };
        }
    });
    pressDetails();
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        searchOpenDialogs: true,
        check: function (editors: UI5Element[]) {
            return (editors[0] as CodeEditor).getValue() === "This finding has no detail text in SAP.";
        },
        success: function () {
            Opa5.assert.ok(true, "no_detail is said in the dialog, not as an error box");
        },
        errorMessage: "no_detail is not explained in the dialog"
    });
    Then.iStopTheApp();
});
