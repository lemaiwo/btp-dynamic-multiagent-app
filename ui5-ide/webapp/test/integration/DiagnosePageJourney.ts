import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type Button from "sap/m/Button";
import type Link from "sap/m/Link";
import type List from "sap/m/List";
import type MessageStrip from "sap/m/MessageStrip";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Text from "sap/m/Text";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SOPTS, iPress, iSend, thePrimaryActionIs, theReasonIs } from "./pages/Session";
import { closeMessageBox } from "./pages/Shared";
import type FakeBackend from "./FakeBackend";
import type { DiagnoseFinding } from "../../service/types";

/**
 * Task U11: diagnose sessions on the session page. The banner (no masking
 * claim), the findings list in the artifact column (live from `finding`
 * frames, again after a reload), a finding's SAP source at its line, the
 * details dialog with a refresh, Open in ADT, the trace approval card,
 * Report into the document view (no comments), Hand over into the new
 * change session, and a session whose target lost its flag.
 */
QUnit.module("Diagnose page journey");

const SID = "s-2";
/** A class pool program name: the class name padded to 30 with "=", then "CP". */
const POOL = "ZCL_ORDER_QUERY".padEnd(30, "=") + "CP";
const PATH = "src/CLAS/zcl_order_query.clas.abap";
/** Data from SAP and the model: shown as text, never as markup or binding syntax. */
const HOSTILE_TITLE = "TSV_TNEW_PAGE_ALLOC_FAILED <img src=x onerror=alert(1)> {/session/title}";
const BANNER = "Diagnose session on a non-production system: dumps and traces are sent to the AI model as they are and kept with this session for 14 days.";
const LOST = "This system is no longer flagged non-production: diagnose reads and runs are blocked. Content already stored stays until the session is deleted or its retention ends.";
const NOT_AVAILABLE = "The target system is not flagged as non-production, so this is not available there. Ask an administrator to check the target's conventions.";
const SAP_SOURCE = Array.from({ length: 30 }, (_, i) => (i === 11 ? "    SELECT * FROM zorder_item APPENDING TABLE @rt_items." : `* sap line ${i + 1}`)).join("\n");

const DUMP: Omit<DiagnoseFinding, "id"> = {
    kind: "dump", ref_id: "DUMP-1", title: HOSTILE_TITLE, program: POOL, include: null, line: 12,
    occurred_at: "2026-10-03T13:52:00", created_at: "2026-10-03T13:53:00"
};

/** A diagnose session on DEMO (flagged), its class already in the workspace with a proposal on top. */
function diagnose(fake: FakeBackend, options: { findings?: boolean; proposal?: boolean } = {}): void {
    fake.allowDiagnose("DEMO");
    const s = fake.addSession("Why is the order list slow?", [], [], "diagnose");
    s.target = "DEMO";
    s.request_cap = 1000;
    s.requests_used = 22;
    const data = fake.dataOf(s.id)!;
    if (options.findings) {
        data.findings.push({ ...DUMP, id: "f-seed" });
    }
    if (options.proposal) {
        // As after a handover went back and forth: a proposal exists, the finding still points at SAP.
        data.files.push({ path: PATH, state: "read", object_type: "CLAS", object_name: "ZCL_ORDER_QUERY", origin_source: SAP_SOURCE, proposed_source: "" });
        fake.addRevision(s.id, PATH, "* PROPOSAL line 1\n* PROPOSAL line 2");
    }
}

function doc(): Document {
    return Opa5.getWindow().document;
}

function hash(): string {
    return HashChanger.getInstance().getHash();
}

/** The template control `id` of findings row `index`. */
function inRow(index: number, id: string) {
    return (control: UI5Element): boolean => control.getId().includes(`--${id}-`)
        && control.getBindingContext("s")?.getPath() === `/artifact/findings/items/${index}`;
}

function theFindingsAre(Then: Common, count: number, message: string, check?: () => boolean): void {
    Then.waitFor({
        id: "findingsList",
        ...SOPTS,
        check: (list: UI5Element) => (list as List).getItems().length === count && (!check || check()),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `Not ${count} findings`
    });
}

function theTitleIs(Then: Common, text: string, message: string): void {
    Then.waitFor({
        id: "artifactTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: text }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The artifact column is not "${text}"`
    });
}

function boxSays(Then: Common, text: string, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: text }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `No message "${text}"`
    });
}

function pressRow(When: Common, index: number): void {
    When.waitFor({
        controlType: "sap.m.CustomListItem",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getBindingContext("s")?.getPath() === `/artifact/findings/items/${index}`,
        actions: new Press(),
        errorMessage: `No findings row ${index}`
    });
}

function detailShows(Then: Common, includes: string, message: string): void {
    Then.waitFor({
        id: "findingDetailEditor",
        ...SOPTS,
        searchOpenDialogs: true,
        check: (c: UI5Element) => String((c as unknown as { getValue(): string }).getValue()).includes(includes),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The details do not show "${includes}"`
    });
}

opaTest("banner, a finding live during a run and after a reload, its SAP source at the line, details with refresh, Open in ADT", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => { diagnose(fake, { proposal: true }); });
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: BANNER }),
        success: function (c: UI5Element) {
            Opa5.assert.notOk(/mask/i.test(String((c as Text).getProperty("text"))), "the banner does not claim masking");
        },
        errorMessage: "No diagnose banner"
    });
    Then.waitFor({
        id: "lostFlagStrip",
        ...SOPTS,
        visible: false,
        success: function (c: UI5Element) {
            Opa5.assert.notOk((c as MessageStrip).getVisible(), "no lost-flag strip on a flagged target");
        }
    });
    thePrimaryActionIs(Then, "Create report", true, "Report in place of Approve");
    Then.waitFor({
        id: "diagnoseFindingsLink",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Findings (0)" }),
        actions: new Press(),
        errorMessage: "No findings link in the header"
    });
    theTitleIs(Then, "Findings (0)", "the findings column opens");
    theFindingsAre(Then, 0, "no findings yet");

    // A run finds a dump. The list read at the run's end is held: the row can only come from the frame.
    let release: () => void = () => undefined;
    Then.waitFor({
        success: function () {
            release = backend.hold(`GET sessions/${SID}/findings`);
            backend.scriptRun({ findings: [DUMP] });
        }
    });
    iSend(When, "Why does the order list dump?");
    theFindingsAre(Then, 1, "the finding arrived with the run's frame", () =>
        !backend.responses.includes(`GET sessions/${SID}/findings`) || backend.responses.filter((r) => r === `GET sessions/${SID}/findings`).length === 1);
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: inRow(0, "findingTitle"),
        success: function (texts: UI5Element[]) {
            Opa5.assert.strictEqual(String((texts[0] as Text).getProperty("text")), HOSTILE_TITLE, "the title is shown as text, binding syntax and markup untouched");
            Opa5.assert.notOk(doc().querySelector("img[src='x']"), "no markup from the title");
            Opa5.assert.notOk(backend.responses.filter((r) => r === `GET sessions/${SID}/findings`).length > 1,
                "the list read of the run's end is still held: the row came from the frame");
        },
        errorMessage: "No finding title"
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...SOPTS,
        matchers: inRow(0, "findingKind"),
        success: function (c: UI5Element[]) {
            const status = c[0] as ObjectStatus;
            Opa5.assert.strictEqual(status.getText(), "Dump", "the kind as text");
            Opa5.assert.strictEqual(status.getIcon(), "sap-icon://error", "and as icon");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        matchers: inRow(0, "findingWhere"),
        success: function (c: UI5Element[]) {
            Opa5.assert.ok(String((c[0] as Text).getProperty("text")).startsWith(`${POOL} · line 12`), "program and line");
        }
    });
    Then.waitFor({
        id: "diagnoseFindingsLink",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Findings (1)" }),
        success: function () {
            release();
            Opa5.assert.ok(true, "the header counts it");
        },
        errorMessage: "The header does not count the finding"
    });

    // Reload: leave the session and come back; the finding is read from the server.
    When.waitFor({ success: () => { HashChanger.getInstance().setHash("sessions"); } });
    When.waitFor({ id: "worklistTable", viewName: "Worklist", success: () => { HashChanger.getInstance().setHash(`sessions/${SID}?view=findings`); } });
    theFindingsAre(Then, 1, "after a reload the stored finding is listed");

    // Open in ADT per finding (class pool: the class at the line).
    Then.waitFor({
        controlType: "sap.m.Link",
        ...SOPTS,
        matchers: inRow(0, "findingAdt"),
        success: function (c: UI5Element[]) {
            Opa5.assert.strictEqual((c[0] as Link).getHref(), "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/source/main#start=12,0",
                "Open in ADT from adtUri");
            Opa5.assert.strictEqual((c[0] as Link).getTarget(), "_blank");
        },
        errorMessage: "No ADT link on the finding"
    });

    // The finding opens its SAP source at the line: the proposal is not what the finding points at.
    pressRow(When, 0);
    theTitleIs(Then, "Source of zcl_order_query.clas.abap", "the source view opens");
    Then.waitFor({
        id: "sourceView",
        ...SOPTS,
        check: () => !!doc().querySelector(".ideSource tr[data-line='12'][aria-current='true']"),
        success: function () {
            Opa5.assert.ok(/base=sap/.test(hash()) && /line=12/.test(hash()), "the deep link names the SAP base and the line");
            const text = doc().querySelector(".ideSource")!.textContent ?? "";
            Opa5.assert.ok(text.includes("APPENDING TABLE @rt_items"), "the SAP source is shown");
            Opa5.assert.notOk(text.includes("PROPOSAL"), "not the proposal");
            Opa5.assert.ok(backend.requests.some((r) => /^POST sessions\/s-2\/findings\/[^/]+\/open$/.test(r)), "through the open route");
        },
        errorMessage: "The SAP source is not shown at line 12"
    });
    Then.waitFor({
        id: "sourceMeta",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "SAP source" }),
        success: function () {
            Opa5.assert.ok(true, "it says it is the SAP source");
        }
    });
    Then.waitFor({
        id: "sourceAdt",
        ...SOPTS,
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as Link).getHref(), "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/source/main#start=12,0",
                "the source's ADT link carries the line");
        }
    });
    Then.waitFor({
        id: "sourceFindingNote",
        ...SOPTS,
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual(String((c as Text).getProperty("text")), `Line 12: ${HOSTILE_TITLE}`, "the line says which finding it is, as text");
        }
    });

    // Back to the list; Details shows the stored text, Refresh reads it again from SAP.
    iPress(When, "sourceBackToFindings", "No way back to the findings");
    theFindingsAre(Then, 1, "back on the findings");
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: inRow(0, "findingDetails"),
        actions: new Press(),
        errorMessage: "No Details on the finding"
    });
    detailShows(Then, "Detail of dump DUMP-1", "the stored detail text");
    When.waitFor({ id: "findingDetailRefreshButton", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    detailShows(Then, "(re-read from SAP)", "Refresh read it again from SAP");
    When.waitFor({ id: "findingDetailCloseButton", ...SOPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        visible: false,
        check: (dialogs: UI5Element[]) => !dialogs.some((d) => (d as unknown as { isOpen(): boolean }).isOpen()),
        success: function () {
            Opa5.assert.ok(true, "the dialog closed");
        }
    });
    Then.iStopTheApp();
});

opaTest("a finding without a program has no source; one in a method include opens the class with a hint", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => {
        diagnose(fake);
        fake.dataOf(SID)!.findings.push(
            { ...DUMP, id: "f-auth", kind: "auth_check", ref_id: "AUTH-1", title: "S_TCODE", program: null, line: null },
            { ...DUMP, id: "f-cm", ref_id: "DUMP-2", title: "CX_SY_ZERODIVIDE", include: "ZCL_ORDER_QUERY".padEnd(30, "=") + "CM003", line: 7 }
        );
    });
    theFindingsAre(Then, 2, "two findings");
    Then.waitFor({
        controlType: "sap.m.Link",
        ...SOPTS,
        visible: false,
        matchers: inRow(0, "findingAdt"),
        success: function (c: UI5Element[]) {
            Opa5.assert.notOk((c[0] as Link).getVisible(), "no program: no ADT link");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        visible: false,
        matchers: inRow(0, "findingDetails"),
        success: function (c: UI5Element[]) {
            Opa5.assert.notOk((c[0] as Button).getVisible(), "an authorization check has no text: no Details");
        }
    });
    // No program: the row is no action and says why (fix round U11 #9), so nothing is asked.
    Then.waitFor({
        controlType: "sap.m.CustomListItem",
        ...SOPTS,
        matchers: (c: UI5Element) => c.getBindingContext("s")?.getPath() === "/artifact/findings/items/0",
        success: function (items: UI5Element[]) {
            Opa5.assert.strictEqual((items[0] as unknown as { getType(): string }).getType(), "Inactive", "no program: the row is no action");
            Opa5.assert.notOk(backend.requests.some((r) => r.endsWith("/findings/f-auth/open")), "no open request");
        }
    });
    pressRow(When, 1);
    Then.waitFor({
        id: "sourceHint",
        ...SOPTS,
        check: (c: UI5Element) => (c as MessageStrip).getVisible() && (c as MessageStrip).getText().includes("Method include"),
        success: function () {
            Opa5.assert.notOk(doc().querySelector(".ideSource tr[aria-current='true']"), "no line highlighted in a source the line is not of");
            Opa5.assert.notOk(/line=/.test(hash()), "no line in the link");
        },
        errorMessage: "No hint for the method include"
    });
    Then.iStopTheApp();
});

opaTest("trace approval card: approve and reject; Report shows the report without comments; Hand over opens the change session with it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => {
        diagnose(fake, { findings: true });
        fake.addApproval(SID, { created_at: "2026-10-03T09:05:00" });
    });
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: `${BANNER} 1 trace request is waiting for your decision.` }),
        success: function () {
            Opa5.assert.ok(true, "the banner says a trace request waits");
        },
        errorMessage: "The banner does not count the pending request"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Reject" }),
        actions: new Press(),
        errorMessage: "No Reject on the stored card"
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        check: () => (doc().querySelector("[id$='--messageList']")?.textContent ?? "").includes("Request rejected"),
        success: function () {
            Opa5.assert.ok(true, "Reject: the card became the rejected line");
        },
        errorMessage: "The rejection is not shown"
    });
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: BANNER }),
        success: function () {
            Opa5.assert.ok(true, "nothing waits any more");
        }
    });
    // A run proposes a trace: the card follows the question; Approve arms it.
    Then.waitFor({ success: () => { backend.scriptRun({ approval: {} }); } });
    iSend(When, "Which statement is slow?");
    When.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Approve trace" }),
        actions: new Press(),
        errorMessage: "No Approve on the proposed card"
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        check: () => (doc().querySelector("[id$='--messageList']")?.textContent ?? "").includes("Trace armed"),
        success: function () {
            Opa5.assert.ok(true, "Approve: the trace is armed");
        },
        errorMessage: "The approval is not shown"
    });

    // Report: the run writes report v1 and the document view shows it, without comment markers.
    thePrimaryActionIs(Then, "Create report", true, "no report yet");
    iPress(When, "primaryAction");
    theTitleIs(Then, "Report v1", "the report opens in the document view");
    Then.waitFor({
        id: "artifactContent",
        ...SOPTS,
        check: () => (doc().querySelector("[id$='--artifactContent']")?.textContent ?? "").includes("Diagnose report v1"),
        success: function () {
            const markers = Array.from(doc().querySelectorAll<HTMLElement>("[id*='--docMarker-']")).filter((el) => el.offsetParent);
            Opa5.assert.strictEqual(markers.length, 0, "no comment markers on a report");
            Opa5.assert.strictEqual(doc().querySelectorAll(".ideDocBlock[tabindex]").length, 0, "no block takes comments by keyboard");
            Opa5.assert.ok(/view=document/.test(hash()) && /kind=report/.test(hash()), "the deep link names the report");
        },
        errorMessage: "The report is not shown"
    });
    Then.waitFor({
        id: "diagnoseReportLink",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Report v1" }),
        success: function () {
            Opa5.assert.ok(true, "the header links the report");
        }
    });
    thePrimaryActionIs(Then, "Hand over to a change", true, "with a report the handover is next");

    // Hand over: confirmed first, Cancel sends nothing.
    iPress(When, "primaryAction");
    boxSays(Then, "Start a change session from this report? A new session \"Change: Why is the order list slow?\" is created on DEMO with the report as its first document. This diagnose session stays as it is.",
        "the handover asks first");
    closeMessageBox(When, "Cancel");
    Then.waitFor({
        success: function () {
            Opa5.assert.notOk(backend.requests.includes(`POST sessions/${SID}/handover`), "Cancel sends nothing");
        }
    });
    iPress(When, "primaryAction");
    closeMessageBox(When, "OK");
    Then.waitFor({
        id: "sessionTitle",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Change: Why is the order list slow?" }),
        success: function () {
            Opa5.assert.strictEqual(backend.requests.filter((r) => r === `POST sessions/${SID}/handover`).length, 1, "one handover");
            Opa5.assert.notOk(hash().startsWith(`sessions/${SID}`), "the page shows the new session");
        },
        errorMessage: "The new change session is not shown"
    });
    theTitleIs(Then, "Report v1", "the new change session opens with the report it carries");
    Then.waitFor({
        id: "sessionType",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "Change" }),
        success: function () {
            Opa5.assert.ok(true, "it is a change session");
        }
    });
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        visible: false,
        success: function (c: UI5Element) {
            Opa5.assert.notOk((c as Control).getVisible(), "no diagnose banner on the change session");
        }
    });
    Then.iStopTheApp();
});

opaTest("a session whose target lost its flag: the strip says so, runs/report/details/open are refused; stored data stays visible", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}?view=findings`, undefined, (fake) => {
        diagnose(fake, { findings: true });
        fake.dataOf(SID)!.messages.push({
            id: "m-1", role: "user", stage: "investigate", created_at: "2026-10-03T09:00:00", content: "Why does it dump?"
        }, {
            id: "m-2", role: "assistant", stage: "investigate", created_at: "2026-10-03T09:01:00", content: "Because of the loop."
        });
        fake.addApproval(SID, { created_at: "2026-10-03T09:05:00" });
        fake.conventions.forEach((c) => { c.non_production = false; });
    });
    Then.waitFor({
        id: "lostFlagStrip",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: LOST }),
        success: function (c: UI5Element) {
            Opa5.assert.strictEqual((c as MessageStrip).getType(), "Warning", "a warning strip");
            Opa5.assert.notOk(/mask/i.test((c as MessageStrip).getText()), "it does not promise masking");
        },
        errorMessage: "No lost-flag strip"
    });
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        visible: false,
        success: function (c: UI5Element) {
            Opa5.assert.notOk((c as Control).getVisible(), "the flagged banner is gone");
        }
    });
    thePrimaryActionIs(Then, "Create report", false, "Report is off");
    theReasonIs(Then, NOT_AVAILABLE, "and says why");
    Then.waitFor({
        id: "sendButton",
        ...SOPTS,
        enabled: false,
        success: function (c: UI5Element) {
            Opa5.assert.notOk((c as Button).getEnabled(), "Send is off");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        ...SOPTS,
        check: () => {
            const text = doc().querySelector("[id$='--messageList']")?.textContent ?? "";
            return text.includes("Because of the loop.") && text.includes("Arm a profiler trace");
        },
        success: function () {
            Opa5.assert.ok(true, "stored messages and the approval card are still shown");
        },
        errorMessage: "Stored messages or approvals are gone"
    });
    theFindingsAre(Then, 1, "the stored findings are still listed");
    Then.waitFor({
        controlType: "sap.m.Button",
        ...SOPTS,
        enabled: false,
        matchers: inRow(0, "findingDetails"),
        success: function (c: UI5Element[]) {
            Opa5.assert.notOk((c[0] as Button).getEnabled(), "Details is off");
        },
        errorMessage: "No Details button"
    });
    // Opening the source is a read from SAP: the row is no action then, the reason is shown above the list (UF2 C2).
    Then.waitFor({
        id: "findingsLostReason",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: NOT_AVAILABLE }),
        success: function () {
            Opa5.assert.notOk(backend.requests.some((r) => r.endsWith("/open")), "nothing asks the server to open it");
            Opa5.assert.notOk(/view=source/.test(hash()), "no source view opened");
        },
        errorMessage: "No reason above the findings"
    });
    Then.iStopTheApp();
});

opaTest("a flag removed while the session is open: the first refusal reloads the session and the strip appears", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(`sessions/${SID}`, undefined, (fake) => { diagnose(fake, { findings: true }); });
    Then.waitFor({
        id: "diagnoseBannerText",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: BANNER }),
        success: function () {
            backend.conventions.forEach((c) => { c.non_production = false; });
        }
    });
    iSend(When, "Look at the dumps of today");
    boxSays(Then, NOT_AVAILABLE, "the run is refused");
    closeMessageBox(When);
    Then.waitFor({
        id: "lostFlagStrip",
        ...SOPTS,
        matchers: new PropertyStrictEquals({ name: "visible", value: true }),
        success: function () {
            Opa5.assert.ok(true, "the session was reloaded: the strip says reads and runs are blocked");
        },
        errorMessage: "The strip did not appear"
    });
    Then.iStopTheApp();
});
