import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type Menu from "sap/m/Menu";
import type List from "sap/m/List";
import type ObjectListItem from "sap/m/ObjectListItem";
import type Dialog from "sap/m/Dialog";
import type HTML from "sap/ui/core/HTML";
import type TabContainer from "sap/m/TabContainer";
import type MessageStrip from "sap/m/MessageStrip";
import type Button from "sap/m/Button";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { OPTS, announced, answerShown, closeMessageBox, currentStage, inTab, recordAnnouncements, send } from "./pages/Assistant";
import type FakeBackend from "./FakeBackend";

QUnit.module("Report and handover journey");

let sid = "";
function diagnose(extra?: (fake: FakeBackend) => void) {
    return (fake: FakeBackend): void => {
        fake.allowDiagnose();
        sid = fake.addSession("Why does the order dump?", [], [], "diagnose").id;
        extra?.(fake);
    };
}

function documentKinds(Then: Common, expected: string[], message: string): void {
    Then.waitFor({
        id: "documentsMenu",
        ...OPTS,
        visible: false,
        autoWait: false,
        check: function (control: UI5Element) {
            return (control as Menu).getItems().length === expected.length;
        },
        success: function (control: UI5Element) {
            Opa5.assert.deepEqual((control as Menu).getItems().map((i) => i.getText()), expected, message);
        },
        errorMessage: `The document menu does not list ${expected.join(", ") || "nothing"}`
    });
}

function reportTab(Then: Common, heading: string, message: string): void {
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        ...OPTS,
        matchers: inTab("doc:report"),
        check: function (controls: UI5Element[]) {
            return (controls[0] as HTML).getDomRef()?.querySelector("h1")?.textContent === heading;
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The report tab does not show "${heading}"`
    });
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        success: function (control: UI5Element) {
            const container = control as TabContainer;
            const selected = container.getItems().find((i) => i.getId() === container.getSelectedItem());
            Opa5.assert.strictEqual(selected?.getKey(), "doc:report", "the report tab is in front");
            Opa5.assert.strictEqual(selected?.getName(), "Report");
        }
    });
}

function pressButton(When: Common, id: string): void {
    When.waitFor({
        id, ...OPTS, matchers: new PropertyStrictEquals({ name: "enabled", value: true }), actions: new Press(),
        errorMessage: `${id} is not enabled`
    });
}

function confirmShown(Then: Common, includes: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        success: function (dialogs: UI5Element[]) {
            const text = (dialogs[0] as Dialog).getDomRef()?.textContent ?? "";
            Opa5.assert.ok(text.includes(includes), `${message}: ${text}`);
        },
        errorMessage: "No message box"
    });
}

opaTest("Report writes the report and opens it in front; Hand over starts a change session that has the report", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose());
    documentKinds(Then, [], "no documents before the report");
    Then.waitFor({
        id: "handoverButton",
        ...OPTS,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        success: function () {
            Opa5.assert.ok(true, "Hand over waits for a report");
        }
    });
    pressButton(When, "reportButton");
    answerShown(Then, 1, "Report written.", "the report run streamed its answer (no user message for it)");
    reportTab(Then, "Diagnose report v1", "the report is rendered in a document tab");
    documentKinds(Then, ["Report"], "the document menu lists the report");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function () {
            Opa5.assert.ok(backend.requests.includes(`POST sessions/${sid}/report`), "through the report route");
        }
    });

    // Cancel in the confirmation: nothing happens.
    pressButton(When, "handoverButton");
    confirmShown(Then, "Start a change session from this report?", "the handover asks first");
    closeMessageBox(When, "Cancel");
    Then.waitFor({
        id: "sessionList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function () {
            Opa5.assert.notOk(backend.requests.includes(`POST sessions/${sid}/handover`), "Cancel sends nothing");
        }
    });

    pressButton(When, "handoverButton");
    closeMessageBox(When, "OK");
    Then.waitFor({
        id: "sessionList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 3 }),
        check: function (control: UI5Element) {
            return ((control as List).getSelectedItem() as ObjectListItem | null)?.getTitle() === "Change: Why does the order dump?";
        },
        success: function () {
            Opa5.assert.ok(true, "the new change session is selected");
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === `POST sessions/${sid}/handover`).length, 1, "one handover");
        },
        errorMessage: "The new change session is not selected"
    });
    currentStage(Then, "chat", "it is a change session in its first stage");
    documentKinds(Then, ["Report"], "its document menu shows the handed-over report");
    reportTab(Then, "Diagnose report v1", "and the report is open, ready to work from");
    Then.waitFor({
        id: "diagnoseBanner",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function () {
            Opa5.assert.ok(true, "no diagnose banner in the change session");
        }
    });
    Then.iStopTheApp();
});

opaTest("a refused handover says why and stays in the diagnose session", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.dataOf(sid)?.artifacts.push({ id: "a-r1", kind: "report", version: 1, content: "# Stored report", created_at: "2026-10-03T09:00:00" });
    }));
    documentKinds(Then, ["Report"], "a stored report is listed");
    Then.waitFor({
        id: "handoverButton",
        ...OPTS,
        success: function () {
            backend.conventions.forEach((c) => { c.non_production = false; });
        }
    });
    pressButton(When, "handoverButton");
    closeMessageBox(When, "OK");
    confirmShown(Then, "not flagged as non-production", "the refusal is explained");
    closeMessageBox(When);
    Then.waitFor({
        id: "sessionList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual(((control as List).getSelectedItem() as ObjectListItem).getTitle(), "Why does the order dump?",
                "still in the diagnose session");
            backend.allowDiagnose();
        }
    });
    Then.iStopTheApp();
});

opaTest("diagnose sessions: own empty-chat hint, non-fatal notes as a warning strip, no Source/Proposed/Diff or Lint on files", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.errorFrame = {
            code: "no_diagnose_server",
            message: "The diagnose agent has no connection to this session's system, so dumps and traces cannot be read."
        };
        fake.dataOf(sid)?.files.push({
            path: "src/CLAS/zcl_demo.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_DEMO",
            origin_source: "CLASS zcl_demo DEFINITION PUBLIC.\nENDCLASS.", proposed_source: ""
        });
    }));
    Then.waitFor({
        id: "chatList",
        ...OPTS,
        matchers: new PropertyStrictEquals({
            name: "noDataText", value: "No messages yet. Ask why something dumps, runs slowly or fails an authorization check."
        }),
        success: function () {
            Opa5.assert.ok(true, "the empty chat speaks about diagnosing, not about changes");
        },
        errorMessage: "The empty-chat hint is not the diagnose one"
    });
    send(When, "Look at the dumps");
    answerShown(Then, 2, "investigate", "the run finished: the note did not fail it");
    Then.waitFor({
        id: "runWarning",
        ...OPTS,
        success: function (control: UI5Element) {
            const strip = control as MessageStrip;
            Opa5.assert.strictEqual(strip.getType(), "Warning", "a warning strip");
            Opa5.assert.ok(strip.getShowIcon(), "with an icon");
            Opa5.assert.strictEqual(strip.getText(),
                "No ARC-1 server of this agent matches the session target — diagnostics tools are unavailable.");
        },
        errorMessage: "No warning strip for no_diagnose_server"
    });
    Then.waitFor({
        id: "runError",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function () {
            Opa5.assert.ok(true, "and no error strip: the run did not fail");
        },
        errorMessage: "The note is shown as a failed run"
    });

    // A file opened in a diagnose session: nothing is proposed there.
    When.waitFor({
        id: "openObjectSearch",
        ...OPTS,
        success: function (control: UI5Element) {
            interface Host { getController?(): { openFile(path: string): Promise<void> } }
            let parent = control.getParent() as (UI5Element & Host) | null;
            while (parent && !parent.getController) {
                parent = parent.getParent() as (UI5Element & Host) | null;
            }
            void parent?.getController?.()
                .openFile("src/CLAS/zcl_demo.clas.abap");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        matchers: [inTab("file:src/CLAS/zcl_demo.clas.abap"), new PropertyStrictEquals({ name: "text", value: "Refresh from SAP" })],
        success: function (buttons: UI5Element[]) {
            Opa5.assert.strictEqual((buttons[0] as Button).getVisible(), true, "Refresh from SAP stays");
        },
        errorMessage: "The file tab has no Refresh from SAP"
    });
    Then.waitFor({
        controlType: "sap.m.SegmentedButton",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: inTab("file:src/CLAS/zcl_demo.clas.abap"),
        success: function (controls: UI5Element[]) {
            Opa5.assert.strictEqual((controls[0] as SegmentedButton).getVisible(), false, "no Source / Proposed / Diff toggle");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: [inTab("file:src/CLAS/zcl_demo.clas.abap"), new PropertyStrictEquals({ name: "icon", value: "sap-icon://quality-issue" })],
        success: function (buttons: UI5Element[]) {
            Opa5.assert.strictEqual((buttons[0] as Button).getVisible(), false, "no Lint button");
        }
    });
    Then.iStopTheApp();
});

opaTest("conventions_unavailable is a warning strip too: the run goes on, and the strip is announced", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, diagnose((fake) => {
        fake.errorFrame = { code: "conventions_unavailable", message: "The conventions of dev-system could not be loaded." };
    }));
    Then.waitFor({ id: "chatInput", ...OPTS, success: recordAnnouncements });
    send(When, "Look at the dumps");
    answerShown(Then, 2, "investigate", "the run finished: the note did not fail it");
    const expected = "The conventions of the target system could not be loaded — diagnostics tools are unavailable. "
        + "Ask an administrator to check the target's destination.";
    Then.waitFor({
        id: "runWarning",
        ...OPTS,
        success: function (control: UI5Element) {
            const strip = control as MessageStrip;
            Opa5.assert.strictEqual(strip.getType(), "Warning", "a warning strip");
            Opa5.assert.ok(strip.getShowIcon(), "with an icon");
            Opa5.assert.strictEqual(strip.getText(), expected, "with the i18n sentence, not the server's");
            Opa5.assert.ok(announced.includes(expected), `and it is announced: ${announced.join(" | ")}`);
        },
        errorMessage: "No warning strip for conventions_unavailable"
    });
    Then.waitFor({
        id: "runError",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function () {
            Opa5.assert.ok(true, "and no error strip: the run did not fail");
        },
        errorMessage: "The note is shown as a failed run"
    });
    Then.iStopTheApp();
});
