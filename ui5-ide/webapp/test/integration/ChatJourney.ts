import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type List from "sap/m/List";
import type HTML from "sap/ui/core/HTML";
import type TextArea from "sap/m/TextArea";
import type MessageStrip from "sap/m/MessageStrip";
import type SegmentedButton from "sap/m/SegmentedButton";
import type ObjectListItem from "sap/m/ObjectListItem";
import type Text from "sap/m/Text";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import type { Artifact, WorkspaceFile } from "./FakeBackend";
import { OPTS, answerShown, approveEnabled, currentStage, designHeading, send, sessionIn } from "./pages/Assistant";

QUnit.module("Assistant journey");

opaTest("Ctrl+Enter sends a message; the answer streams in and is rendered as markdown", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    Then.waitFor({
        id: "assistantTitle",
        ...OPTS,
        success: function () {
            Opa5.assert.ok(true, "the assistant pane has a heading");
        }
    });
    currentStage(Then, "chat", "a new session is in Chat");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "editable", value: true }),
        actions: new EnterText({ text: "Explain <b>ZCL_DEMO</b>" }),
        success: function (control: UI5Element) {
            const area = control as TextArea;
            Opa5.assert.ok(/Ctrl\+Enter/.test(area.getPlaceholder()), "the placeholder documents Ctrl+Enter");
            const dom = area.getFocusDomRef() as HTMLElement;
            dom.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", ctrlKey: true, bubbles: true }));
        }
    });
    answerShown(Then, 2, "<strong>chat</strong>", "the streamed answer is rendered as markdown");
    Then.waitFor({
        id: "chatList",
        ...OPTS,
        success: function (control: UI5Element) {
            const first = (control as List).getItems()[0].getDomRef() as HTMLElement;
            Opa5.assert.ok((first.textContent ?? "").includes("Explain <b>ZCL_DEMO</b>"), "the user text is shown as typed");
            Opa5.assert.strictEqual(first.querySelectorAll("b").length, 0, "user text is never rendered as HTML");
            Opa5.assert.ok(backend.requests.includes("POST sessions/s-1/messages"), "sent through the API");
            Opa5.assert.ok(backend.requests.includes("GET sessions/s-1/messages"), "the stored messages were reloaded");
        }
    });
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "value", value: "" }),
        success: function () {
            Opa5.assert.ok(true, "the input is cleared");
        }
    });
    Then.waitFor({
        id: "usageStatus",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "text", value: "1 / 200 requests" }),
        success: function () {
            Opa5.assert.ok(true, "the usage is shown");
        }
    });

    Then.iStopTheApp();
});

opaTest("approve Chat, Design needs a design, a design run opens it, approve moves to Plan", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    approveEnabled(Then, true, "Chat can always be approved");
    When.waitFor({ id: "approveButton", ...OPTS, actions: new Press() });
    currentStage(Then, "design", "the stage bar shows Design as current");
    approveEnabled(Then, false, "Design without a design document cannot be approved");
    Then.waitFor({
        id: "sessionList",
        ...OPTS,
        check: function (control: UI5Element) {
            const item = (control as List).getSelectedItem() as ObjectListItem | null;
            return item?.getFirstStatus()?.getText() === "Design";
        },
        success: function () {
            Opa5.assert.ok(true, "the session list shows the new stage");
        }
    });

    send(When, "Design a guard for the order check");
    answerShown(Then, 2, "<strong>design</strong>", "the design answer arrived");
    designHeading(Then, "design v1", "the new design opened as a document tab");
    approveEnabled(Then, true, "with a design, Design can be approved");
    When.waitFor({ id: "approveButton", ...OPTS, actions: new Press() });
    currentStage(Then, "plan", "Approve moved the session to Plan");
    approveEnabled(Then, false, "Plan needs a plan first");

    Then.iStopTheApp();
});

opaTest("Revise opens a dialog that requires feedback, then reruns the stage", function (Given: Common, When: Common, Then: Common) {
    const design: Artifact = { id: "a-d1", kind: "design", version: 1, content: "# design v1", created_at: "2026-10-03T09:00:00" };
    Given.iStartTheApp("", undefined, sessionIn("design", [design]));

    currentStage(Then, "design", "the session is in Design");
    When.waitFor({
        id: "reviseButton",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        actions: new Press()
    });
    When.waitFor({ id: "reviseSendButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "valueState", value: "Error" }),
        success: function (control: UI5Element) {
            Opa5.assert.ok((control as TextArea).getRequired(), "the feedback is required");
            Opa5.assert.notOk(backend.requests.some((r) => r.endsWith("/revise")), "nothing was sent");
        },
        errorMessage: "Empty feedback was not refused"
    });
    When.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Also cover the cancel path" })
    });
    When.waitFor({ id: "reviseSendButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    answerShown(Then, 2, "<strong>design</strong>", "the revised answer arrived");
    designHeading(Then, "design v2", "the design tab shows the new version", function () {
        Opa5.assert.ok(backend.requests.includes("POST sessions/s-2/revise"), "revised through the API");
    });

    Then.iStopTheApp();
});

opaTest("Stop cancels the running answer", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("", undefined, (fake) => { release = fake.pauseStream(); });

    send(When, "Analyse the impact");
    Then.waitFor({
        id: "streamHtml",
        ...OPTS,
        check: function (control: UI5Element) {
            return ((control as HTML).getDomRef()?.textContent ?? "").includes("Fake ans");
        },
        success: function () {
            Opa5.assert.ok(true, "the first delta is shown while the run goes on");
        },
        errorMessage: "No streamed text"
    });
    approveEnabled(Then, false, "Approve is off while the assistant runs");
    When.waitFor({
        id: "stopButton",
        ...OPTS,
        actions: new Press(),
        errorMessage: "No visible Stop button while running"
    });
    Then.waitFor({
        id: "chatList",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        check: function (control: UI5Element) {
            const last = (control as List).getItems()[1];
            return last.getBindingContext("ide")?.getProperty("cancelled") === true;
        },
        success: function () {
            Opa5.assert.ok(backend.requests.includes("POST sessions/s-1/cancel"), "the run was cancelled on the server");
            release?.();
        },
        errorMessage: "The stopped answer is not marked"
    });
    Then.waitFor({
        id: "stopButton",
        ...OPTS,
        visible: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: false }),
        success: function () {
            Opa5.assert.ok(true, "Stop is hidden again");
        }
    });
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "editable", value: true }),
        success: function () {
            Opa5.assert.ok(true, "a new message can be sent");
        }
    });

    Then.iStopTheApp();
});

opaTest("a refused approve shows the gate's text", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", { path: "sessions/s-1/approve", status: 409, body: { detail: "busy", code: "run_in_progress" } });

    approveEnabled(Then, true, "the client gate allows it");
    When.waitFor({ id: "approveButton", ...OPTS, actions: new Press() });
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: function (control: UI5Element) {
            return /still working on this session/.test(String((control as Text).getProperty("text")));
        },
        success: function () {
            Opa5.assert.ok(true, "the 409 code is shown with its i18n text");
        },
        errorMessage: "No gate message"
    });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "Close" }),
        actions: new Press(),
        errorMessage: "The message box has no Close button"
    });
    currentStage(Then, "chat", "the stage did not change");

    Then.iStopTheApp();
});

opaTest("a stream that ends without done shows an error strip in the chat", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => { fake.omitDone = true; });

    send(When, "Hello");
    Then.waitFor({
        id: "runError",
        ...OPTS,
        success: function (control: UI5Element) {
            const strip = control as MessageStrip;
            Opa5.assert.ok(/connection ended/.test(strip.getText()), "the incomplete stream is explained");
            Opa5.assert.strictEqual(strip.getType(), "Error");
        },
        errorMessage: "No error strip"
    });

    Then.iStopTheApp();
});

opaTest("a propose run reloads the tree and the open file tab", function (Given: Common, When: Common, Then: Common) {
    const file: WorkspaceFile = {
        path: "src/CLAS/zcl_prop.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_PROP",
        origin_source: "CLASS zcl_prop DEFINITION.", proposed_source: ""
    };
    Given.iStartTheApp("", undefined, sessionIn("propose", [], [file]));

    approveEnabled(Then, false, "Propose without a proposed file cannot be approved");
    When.waitFor({
        controlType: "sap.m.CustomTreeItem",
        ...OPTS,
        matchers: function (item: UI5Element) {
            return item.getBindingContext("ide")?.getProperty("path") === file.path;
        },
        actions: new Press()
    });
    Then.waitFor({
        controlType: "sap.m.SegmentedButton",
        ...OPTS,
        success: function (controls: UI5Element[]) {
            const item = (controls[0] as SegmentedButton).getItems().find((i) => i.getKey() === "proposed");
            Opa5.assert.strictEqual(item?.getEnabled(), false, "no proposal yet");
        }
    });
    send(When, "Propose the fix");
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        ...OPTS,
        matchers: function (status: UI5Element) {
            return status.getBindingContext("ide")?.getProperty("path") === file.path
                && (status as unknown as { getText(): string }).getText() === "modified";
        },
        success: function () {
            Opa5.assert.ok(true, "the tree shows the file as modified");
        },
        errorMessage: "The tree was not reloaded"
    });
    // The run's note opened as a tab in the background: the file tab stays in front.
    Then.waitFor({
        controlType: "sap.m.SegmentedButton",
        ...OPTS,
        visible: false,
        check: function (controls: UI5Element[]) {
            const item = (controls[0] as SegmentedButton).getItems().find((i) => i.getKey() === "proposed");
            return item?.getEnabled() === true;
        },
        success: function () {
            Opa5.assert.ok(true, "the open tab now offers the proposal");
        },
        errorMessage: "The open tab was not refreshed"
    });
    approveEnabled(Then, true, "with a modified file, Propose can be approved");

    Then.iStopTheApp();
});

