import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type List from "sap/m/List";
import type Select from "sap/m/Select";
import type Button from "sap/m/Button";
import type SegmentedButton from "sap/m/SegmentedButton";
import type ObjectListItem from "sap/m/ObjectListItem";
import type HBox from "sap/m/HBox";
import type MessageStrip from "sap/m/MessageStrip";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import { OPTS, answerShown, closeMessageBox, currentStage, pressSession, send } from "./pages/Assistant";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";

QUnit.module("Diagnose session journey");

opaTest("choose Diagnose in the dialog: only diagnose targets are listed; the new session shows the diagnose icon", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        // dev-system stays a change-only target; sandbox-system is non_production.
        fake.allowDiagnose("sandbox-system");
    });

    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function () {
            Opa5.assert.ok(true, "the session list has loaded");
        }
    });
    When.waitFor({ id: "newSessionButton", viewName: "Ide", actions: new Press() });
    When.waitFor({
        id: "newSessionTitle",
        viewName: "Ide",
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Why does the order dump?" })
    });
    Then.waitFor({
        id: "newSessionTarget",
        viewName: "Ide",
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            const keys = (control as Select).getItems().map((i) => i.getKey());
            Opa5.assert.deepEqual(keys, ["dev-system", "sandbox-system"], "a change session offers every target");
        }
    });
    When.waitFor({
        id: "newSessionType",
        viewName: "Ide",
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            const bar = control as SegmentedButton;
            const diagnose = bar.getItems().find((i) => i.getKey() === "diagnose");
            Opa5.assert.ok(diagnose, "the type choice has a Diagnose item");
            new Press().executeOn(diagnose as unknown as Button);
        },
        errorMessage: "No session type choice in the dialog"
    });
    Then.waitFor({
        id: "newSessionTarget",
        viewName: "Ide",
        searchOpenDialogs: true,
        matchers: function (control: UI5Element) {
            return (control as Select).getItems().length === 1;
        },
        success: function (control: UI5Element) {
            const select = control as Select;
            Opa5.assert.deepEqual(select.getItems().map((i) => i.getKey()), ["sandbox-system"], "only the diagnose target is listed");
            Opa5.assert.strictEqual(select.getSelectedKey(), "sandbox-system", "the single diagnose target is preselected");
        },
        errorMessage: "The target list did not switch to the diagnose targets"
    });
    Then.waitFor({
        id: "newSessionDiagnoseHint",
        viewName: "Ide",
        searchOpenDialogs: true,
        success: function () {
            Opa5.assert.ok(true, "the privacy hint is shown for a diagnose session");
        }
    });
    When.waitFor({ id: "newSessionCreate", viewName: "Ide", searchOpenDialogs: true, actions: new Press() });

    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function (control: UI5Element) {
            const item = (control as List).getSelectedItem() as ObjectListItem | null;
            Opa5.assert.strictEqual(item?.getTitle(), "Why does the order dump?", "the new session is selected");
            Opa5.assert.strictEqual(item?.getIcon(), "sap-icon://stethoscope", "the list shows the diagnose icon");
            const sent = backend.sessions.at(-1)?.session;
            Opa5.assert.strictEqual(sent?.type, "diagnose", "the type travelled in the create request");
            Opa5.assert.strictEqual(sent?.target, "sandbox-system");
        },
        errorMessage: "The diagnose session did not appear selected"
    });
    Then.iStopTheApp();
});

opaTest("with no non-production target the Diagnose choice is disabled", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();
    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function () {
            Opa5.assert.ok(true, "loaded");
        }
    });
    When.waitFor({ id: "newSessionButton", viewName: "Ide", actions: new Press() });
    Then.waitFor({
        id: "newSessionType",
        viewName: "Ide",
        searchOpenDialogs: true,
        success: function (control: UI5Element) {
            const diagnose = (control as SegmentedButton).getItems().find((i) => i.getKey() === "diagnose") as unknown as Button;
            Opa5.assert.strictEqual(diagnose.getEnabled(), false, "Diagnose is disabled");
            Opa5.assert.ok(diagnose.getTooltip(), "and says why in its tooltip");
        }
    });
    Then.iStopTheApp();
});

/** Waits for the control `id` to have `visible`; a hidden control is only found with `visible: false`. */
function shown(Then: Common, id: string, visible: boolean, message: string): void {
    Then.waitFor({
        id,
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: visible }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `${id} is not ${visible ? "visible" : "hidden"}`
    });
}

function enabled(Then: Common, id: string, value: boolean, message: string): void {
    Then.waitFor({
        id,
        ...OPTS,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value }),
        success: function (control: UI5Element) {
            Opa5.assert.ok((control as Button).getTooltip_AsString(), `${id} says why (or what it does) in its tooltip`);
            Opa5.assert.ok(true, message);
        },
        errorMessage: `${id} is not ${value ? "enabled" : "disabled"}`
    });
}

opaTest("a diagnose session shows the banner, the single Investigate step and Report / Hand over instead of Approve / Revise", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        fake.allowDiagnose();
        fake.addSession("Why does the order dump?", [], [], "diagnose");
    });

    Then.waitFor({
        id: "diagnoseBanner",
        ...OPTS,
        success: function (control: UI5Element) {
            const strip = control as MessageStrip;
            Opa5.assert.strictEqual(strip.getType(), "Information", "the banner is an information strip");
            Opa5.assert.ok(strip.getShowIcon(), "with an icon, so the type does not rest on colour");
            Opa5.assert.strictEqual(strip.getText(),
                "Diagnose session on a non-production system: dumps and traces are sent to the AI model as they are and kept with this session for 14 days.",
                "it says the data is sent unmasked and kept for 14 days");
            Opa5.assert.notOk(/masked/i.test(strip.getText()), "and does not promise masking");
        },
        errorMessage: "No diagnose banner for a diagnose session"
    });
    Then.waitFor({
        id: "stageBar",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (control: UI5Element) {
            const ctx = (control as HBox).getItems()[0].getBindingContext("ide");
            Opa5.assert.strictEqual(ctx?.getProperty("stage"), "investigate", "the only step is investigate");
            Opa5.assert.strictEqual(ctx?.getProperty("state"), "current", "and it is current");
            Opa5.assert.strictEqual(ctx?.getProperty("label"), "Investigate", "with its i18n name");
        },
        errorMessage: "The stage bar does not show the single Investigate step"
    });
    shown(Then, "approveButton", false, "Approve is hidden");
    shown(Then, "reviseButton", false, "Revise is hidden");
    enabled(Then, "reportButton", true, "Report is enabled when the session is idle");
    enabled(Then, "handoverButton", false, "Hand over waits for a report");

    // The investigate stage accepts a message (it was an unknown stage to the gates before).
    send(When, "Look at the dumps of today");
    answerShown(Then, 2, "investigate", "the diagnose session answers");
    enabled(Then, "reportButton", true, "Report is enabled again after the run");

    // A change session next to it keeps the six stages and has no banner.
    pressSession(When, "Explain the order class");
    currentStage(Then, "chat", "the change session shows six stages");
    shown(Then, "diagnoseBanner", false, "no banner for a change session");
    shown(Then, "reportButton", false, "no Report for a change session");
    shown(Then, "handoverButton", false, "no Hand over for a change session");
    shown(Then, "approveButton", true, "Approve is back");
    shown(Then, "reviseButton", true, "Revise is back");
    Then.iStopTheApp();
});

const BLOCKED_BANNER = "This system is no longer flagged non-production: diagnose reads and runs are blocked. "
    + "Content already stored stays until the session is deleted or its retention ends.";

/** Waits for an open message box whose text contains `includes`. */
function boxSays(Then: Common, includes: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "type", value: "Message" }),
        check: function (dialogs: UI5Element[]) {
            return dialogs.some((d) => (d.getDomRef()?.textContent ?? "").includes(includes));
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `No message box containing '${includes}'`
    });
}

function banner(Then: Common, text: string, type: string, message: string): void {
    Then.waitFor({
        id: "diagnoseBanner",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: text }),
        success: function (control: UI5Element) {
            const strip = control as MessageStrip;
            Opa5.assert.strictEqual(strip.getType(), type, `the banner is a ${type} strip`);
            Opa5.assert.ok(strip.getShowIcon(), "with an icon, so the type does not rest on colour");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The banner does not read '${text}'`
    });
}

const FINDING = {
    id: "f-a", kind: "dump" as const, ref_id: "DUMP-1", title: "CX_SY_ZERODIVIDE", program: "ZDEMO_REPORT",
    include: null, line: 7, occurred_at: null, created_at: "2026-10-03T08:00:00Z"
};

function pressDetails(When: Common): void {
    When.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        matchers: [
            (control: UI5Element) => control.getBindingContext("ide")?.getProperty("id") === "f-a",
            new PropertyStrictEquals({ name: "icon", value: "sap-icon://detail-view" })
        ],
        actions: new Press(),
        errorMessage: "The finding has no Show details button"
    });
}

function detailShows(Then: Common, part: string, message: string): void {
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        searchOpenDialogs: true,
        check: function (editors: UI5Element[]) {
            return (editors[0] as unknown as { getValue(): string }).getValue().includes(part);
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The details dialog does not show '${part}'`
    });
}

const NOT_AVAILABLE = "The target system is not flagged as non-production, so this is not available there.";

opaTest("a diagnose session whose target lost its non-production flag says reads and runs are blocked; Send, Report, a finding's source and details say why", function (Given: Common, When: Common, Then: Common) {
    let sid = "";
    Given.iStartTheApp("", undefined, (fake) => {
        fake.allowDiagnose();
        sid = fake.addSession("Flag removed", [], [], "diagnose").id;
        fake.dataOf(sid)?.findings.push({ ...FINDING });
        // An admin removed the flag after the session was created: the session JSON says masked.
        fake.conventions.forEach((c) => { c.non_production = false; });
    });
    banner(Then, BLOCKED_BANNER, "Warning", "the banner says what is blocked and what stays");
    Then.waitFor({
        id: "diagnoseBanner",
        ...OPTS,
        success: function (control: UI5Element) {
            const text = (control as MessageStrip).getText();
            Opa5.assert.notOk(/masked/i.test(text), "it does not promise masking: nothing is read or sent at all");
            Opa5.assert.notOk(text.includes("as they are"), "nor that dumps are sent to the model");
        }
    });

    // Send: refused before the stream, the text goes back to the input.
    send(When, "Look at the dumps of today");
    boxSays(Then, NOT_AVAILABLE, "Send shows the mapped 409 text");
    closeMessageBox(When);
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "value", value: "Look at the dumps of today" }),
        success: function () {
            Opa5.assert.strictEqual(backend.dataOf(sid)?.messages.length, 0, "nothing was run");
        },
        errorMessage: "The refused message did not go back to the input"
    });

    // Report: the same refusal.
    When.waitFor({ id: "reportButton", ...OPTS, actions: new Press() });
    boxSays(Then, NOT_AVAILABLE, "Report shows the mapped 409 text");
    closeMessageBox(When);
    Then.waitFor({
        id: "reportButton",
        ...OPTS,
        success: function () {
            Opa5.assert.notOk(backend.dataOf(sid)?.artifacts.length, "no report was written");
        }
    });

    // A finding's source is a live read: refused.
    When.waitFor({
        controlType: "sap.m.CustomListItem",
        ...OPTS,
        matchers: (control: UI5Element) => control.getBindingContext("ide")?.getProperty("id") === "f-a",
        actions: new Press(),
        errorMessage: "No finding in the list"
    });
    boxSays(Then, NOT_AVAILABLE, "opening the finding's source shows the mapped 409 text");
    closeMessageBox(When);

    // The detail text is refused too, stored or not: the dialog says why and offers no Refresh.
    pressDetails(When);
    detailShows(Then, NOT_AVAILABLE, "the details dialog says why there is no text");
    Then.waitFor({
        id: "findingDetailRefreshButton",
        ...OPTS,
        searchOpenDialogs: true,
        autoWait: false,
        visible: false,
        matchers: new PropertyStrictEquals({ name: "enabled", value: false }),
        success: function () {
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMMessageBoxError").length, 0, "said in the dialog, not as a second box");
            Opa5.assert.ok(backend.responses.includes(`GET sessions/${sid}/findings/f-a`), "the server was asked and refused");
        },
        errorMessage: "Refresh is offered although nothing can be read"
    });
    Then.iStopTheApp();
});

opaTest("a flag removed while the session is open: the first refusal reloads the session and the banner changes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        fake.allowDiagnose();
        const sid = fake.addSession("Why does the order dump?", [], [], "diagnose").id;
        fake.dataOf(sid)?.findings.push({ ...FINDING });
    });
    banner(Then,
        "Diagnose session on a non-production system: dumps and traces are sent to the AI model as they are and kept with this session for 14 days.",
        "Information", "flagged: the usual banner");
    pressDetails(When);
    detailShows(Then, "Detail of dump DUMP-1", "flagged: the stored detail is shown");
    Then.waitFor({
        id: "findingDetailRefreshButton",
        ...OPTS,
        searchOpenDialogs: true,
        success: function () {
            backend.conventions.forEach((c) => { c.non_production = false; });
        }
    });
    // Refresh from SAP with the dialog open: refused in a box, and the text already shown stays.
    When.waitFor({ id: "findingDetailRefreshButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    boxSays(Then, NOT_AVAILABLE, "Refresh from SAP shows the mapped 409 text");
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: [
            new PropertyStrictEquals({ name: "text", value: "Close" }),
            (control: UI5Element) => !!control.getDomRef()?.closest(".sapMMessageBox")
        ],
        actions: new Press(),
        errorMessage: "The message box has no Close button"
    });
    detailShows(Then, "Detail of dump DUMP-1", "and the text already shown stays");
    When.waitFor({ id: "findingDetailCloseButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    send(When, "Look at the dumps of today");
    boxSays(Then, NOT_AVAILABLE, "the run is refused");
    closeMessageBox(When);
    banner(Then, BLOCKED_BANNER, "Warning", "the session was reloaded: the banner now says reads and runs are blocked");
    Then.iStopTheApp();
});
