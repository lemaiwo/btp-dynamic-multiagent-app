import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type HTML from "sap/ui/core/HTML";
import type TabContainer from "sap/m/TabContainer";
import type TextArea from "sap/m/TextArea";
import type Text from "sap/m/Text";
import type Menu from "sap/m/Menu";
import type Select from "sap/m/Select";
import type MessageStrip from "sap/m/MessageStrip";
import type JSONModel from "sap/ui/model/json/JSONModel";
import RunWatch from "com/agent/ide/model/runWatch";
import type { EditorTab } from "com/agent/ide/model/editorTabs";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { USER_TOKEN_REQUIRED, type Artifact, type WorkspaceFile } from "./FakeBackend";
import {
    OPTS, announced, answerShown, closeMessageBox, inTab, pressSession, recordAnnouncements, send, sessionIn
} from "./pages/Assistant";

QUnit.module("Assistant run journey");

function file(name: string): WorkspaceFile {
    return {
        path: `src/CLAS/${name}.clas.abap`, state: "read", object_type: "CLAS", object_name: name.toUpperCase(),
        origin_source: `CLASS ${name} DEFINITION.`, proposed_source: ""
    };
}

function ideModel(control: UI5Element): JSONModel {
    return control.getModel("ide") as JSONModel;
}

function tabs(control: UI5Element): EditorTab[] {
    return ideModel(control).getProperty("/tabs") as EditorTab[];
}

function selectedTabKey(control: UI5Element): string {
    const id = (control as TabContainer).getSelectedItem() as unknown as string | undefined;
    const item = id ? UI5Element.getElementById(id) as unknown as { getKey(): string } | undefined : undefined;
    return item?.getKey() ?? "";
}

function openFileTab(When: Common, path: string): void {
    When.waitFor({
        controlType: "sap.m.CustomTreeItem",
        ...OPTS,
        matchers: function (item: UI5Element) {
            return item.getBindingContext("ide")?.getProperty("path") === path;
        },
        actions: new Press(),
        errorMessage: `No tree item for ${path}`
    });
    tabLoaded(When, `file:${path}`);
}

function tabLoaded(Then: Common, key: string): void {
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        check: function (control: UI5Element) {
            const tab = tabs(control).find((t) => t.key === key);
            return !!tab && !tab.busy;
        },
        errorMessage: `The tab ${key} did not load`
    });
}

/** Waits until `predicate` holds, then runs `then` (an OPA step on any control). */
function when(Then: Common, predicate: () => boolean, then: () => void, errorMessage: string): void {
    Then.waitFor({ check: predicate, success: then, errorMessage });
}

function messageBoxSays(Then: Common, pattern: RegExp, message: string): void {
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: function (control: UI5Element) {
            return pattern.test(String((control as Text).getProperty("text")));
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `No message box matching ${pattern}`
    });
}

function inputEditable(Then: Common, editable: boolean, message: string): void {
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "editable", value: editable }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The chat input is not ${editable ? "editable" : "read-only"}`
    });
}

function stopVisible(Then: Common, visible: boolean, message: string): void {
    Then.waitFor({
        id: "stopButton",
        ...OPTS,
        visible: false,
        autoWait: false,
        matchers: new PropertyStrictEquals({ name: "visible", value: visible }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `Stop is not ${visible ? "shown" : "hidden"}`
    });
}

/**
 * Polls faster than the 3 s default, but above OPA's autoWait limit (1 s):
 * a shorter timer counts as pending work and blocks every autoWait step.
 */
function fastPolling(fake: typeof backend): void {
    void fake;
    RunWatch.intervalMs = 1100;
}

function restorePolling(Then: Common): void {
    Then.waitFor({ success: function () { RunWatch.intervalMs = 3000; } });
}

function toastSays(Then: Common, pattern: RegExp, message: string): void {
    Then.waitFor({
        check: function () {
            return [...document.querySelectorAll(".sapMMessageToast")].some((t) => pattern.test(t.textContent ?? ""));
        },
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `No toast matching ${pattern}`
    });
}

opaTest("two file events, the second during the first tree reload: both open tabs are refreshed", function (Given: Common, When: Common, Then: Common) {
    const a = file("zcl_a");
    const b = file("zcl_b");
    let releaseGap: (() => void) | undefined;
    let releaseFiles: (() => void) | undefined;
    Given.iStartTheApp("", undefined, sessionIn("propose", [], [a, b]));

    openFileTab(When, a.path);
    openFileTab(When, b.path);
    when(Then, () => true, function () {
        releaseGap = backend.splitFiles();
        releaseFiles = backend.hold("GET sessions/s-2/files");
    }, "not reached");
    send(When, "Propose the fix");
    // The first file event started the tree reload (held); now the second arrives.
    when(Then, () => backend.requests.includes("GET sessions/s-2/files"), () => releaseGap?.(),
        "The first file event did not reload the tree");
    when(Then, () => backend.streamsFlushed === 1, () => releaseFiles?.(), "The stream did not finish");
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        check: function (control: UI5Element) {
            return tabs(control).filter((t) => t.kind === "file" && t.hasProposal).length === 2;
        },
        success: function (control: UI5Element) {
            Opa5.assert.ok(true, "both open file tabs show their proposal");
            Opa5.assert.ok(backend.requests.includes(`GET sessions/s-2/file`), "the files were re-read");
            Opa5.assert.ok(tabs(control).some((t) => t.key === "doc:note"), "the run's note opened as a tab");
            Opa5.assert.strictEqual(selectedTabKey(control), `file:${b.path}`,
                "the note opened in the background: the file tab the user looks at stays in front");
        },
        errorMessage: "A file changed during the tree reload was never re-read"
    });
    toastSays(Then, /Note version 1/, "a toast says the note is ready");

    Then.iStopTheApp();
});

opaTest("switching sessions mid-stream: the old run's events never reach the new session", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("", undefined, (fake) => {
        sessionIn("design")(fake);
        release = fake.pauseStream();
    });

    send(When, "Design a guard");
    Then.waitFor({
        id: "streamHtml",
        ...OPTS,
        check: function (control: UI5Element) {
            return ((control as HTML).getDomRef()?.textContent ?? "").includes("Fake ans");
        },
        errorMessage: "No streamed text"
    });
    pressSession(When, "Explain the order class");
    stopVisible(Then, false, "the other session shows no run");
    when(Then, () => backend.responses.includes("GET sessions/s-1/messages"), () => release?.(),
        "The other session did not load");
    when(Then, () => backend.streamsFlushed === 1, () => undefined, "The old stream did not finish");
    Then.waitFor({
        id: "chatList",
        ...OPTS,
        success: function (control: UI5Element) {
            const model = ideModel(control);
            Opa5.assert.strictEqual(model.getProperty("/selectedId"), "s-1", "still on the other session");
            Opa5.assert.strictEqual((model.getProperty("/messages") as unknown[]).length, 0, "no message leaked in");
            Opa5.assert.strictEqual(model.getProperty("/run/html"), "", "no streamed text leaked in");
            Opa5.assert.strictEqual(model.getProperty("/run/usageText"), "", "no usage leaked in");
            Opa5.assert.notOk(tabs(control).some((t) => t.key === "doc:design"), "the old run's design did not open here");
        }
    });

    Then.iStopTheApp();
});

opaTest("a run left running by a session switch is polled until it ends", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("", undefined, (fake) => {
        fastPolling(fake);
        sessionIn("design")(fake);
        release = fake.pauseStream();
    });

    send(When, "Design a guard");
    stopVisible(Then, true, "the run is going");
    pressSession(When, "Explain the order class");
    stopVisible(Then, false, "the other session is idle");
    pressSession(When, "Session in design");
    stopVisible(Then, true, "back on the session: it is still running (server status)");
    inputEditable(Then, false, "nothing can be sent while it runs");
    when(Then, () => backend.requests.filter((r) => r === "GET sessions/s-2").length >= 3, () => release?.(),
        "The running session is not polled");
    stopVisible(Then, false, "the poll saw the run end");
    inputEditable(Then, true, "a message can be sent again");
    answerShown(Then, 2, "<strong>design</strong>", "the stored answer is shown");
    restorePolling(Then);

    Then.iStopTheApp();
});

opaTest("Stop answered 409 run_on_other_instance says the run is elsewhere", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("", undefined, (fake) => {
        fastPolling(fake);
        release = fake.pauseStream();
    });

    send(When, "Analyse the impact");
    stopVisible(Then, true, "the run is going");
    When.waitFor({
        id: "stopButton",
        ...OPTS,
        actions: function (control: UI5Element | null) {
            backend.failNext = {
                path: "sessions/s-1/cancel", status: 409,
                body: { detail: "The run is on another instance.", code: "run_on_other_instance" }
            };
            new Press().executeOn(control as Control);
        }
    });
    messageBoxSays(Then, /another server instance; it will stop or time out/, "the 409 is explained");
    closeMessageBox(When, "OK");
    stopVisible(Then, true, "the run still goes on (the server says running)");
    when(Then, () => true, () => release?.(), "not reached");
    stopVisible(Then, false, "it ended on the other instance; the poll noticed");
    restorePolling(Then);

    Then.iStopTheApp();
});

opaTest("a 424 or 429 before the stream: the reason is shown and the text goes back to the input", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    when(Then, () => backend.responses.includes("GET sessions/s-1/messages"), function () {
        backend.failNext = { path: "sessions/s-1/messages", ...USER_TOKEN_REQUIRED };
    }, "The session did not load");
    send(When, "Hello");
    messageBoxSays(Then, /SAP user token is missing/, "the 424 is explained");
    closeMessageBox(When);
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "value", value: "Hello" }),
        success: function (control: UI5Element) {
            Opa5.assert.strictEqual((ideModel(control).getProperty("/messages") as unknown[]).length, 0,
                "the refused message was taken back");
            backend.failNext = {
                path: "sessions/s-1/messages", status: 429,
                body: { detail: "200 of 200 requests used.", code: "usage_exhausted" }
            };
        }
    });
    When.waitFor({ id: "sendButton", ...OPTS, actions: new Press() });
    messageBoxSays(Then, /usage limit of this session is reached: 200 of 200/, "the 429 is explained");
    closeMessageBox(When);
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "value", value: "Hello" }),
        success: function () {
            Opa5.assert.ok(true, "the text is still there to send later");
        }
    });

    Then.iStopTheApp();
});

opaTest("a user_token_required error frame mid-run shows the reload message", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        fake.errorFrame = { code: "user_token_required", message: "No user token for the destination." };
    });

    send(When, "Analyse");
    Then.waitFor({
        id: "runError",
        ...OPTS,
        matchers: function (control: UI5Element) {
            return /SAP user token is missing.*reload/i.test((control as MessageStrip).getText());
        },
        success: function () {
            Opa5.assert.ok(true, "the frame maps to the same message as the 424");
        },
        errorMessage: "No user-token strip"
    });

    Then.iStopTheApp();
});

opaTest("run_timeout and run_failed frames show in the chat and are announced", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => {
        fake.errorFrame = { code: "run_timeout", message: "run r-9 exceeded 600 s" };
    });

    when(Then, () => true, recordAnnouncements, "not reached");
    send(When, "Analyse");
    Then.waitFor({
        id: "runError",
        ...OPTS,
        matchers: function (control: UI5Element) {
            return /took too long and was stopped: run r-9/.test((control as MessageStrip).getText());
        },
        success: function () {
            Opa5.assert.ok(announced.some((t) => /took too long/.test(t)), "the error was announced");
            backend.errorFrame = { code: "run_failed", message: "Reference: run r-10." };
        },
        errorMessage: "No run_timeout strip"
    });
    send(When, "Again");
    Then.waitFor({
        id: "runError",
        ...OPTS,
        matchers: function (control: UI5Element) {
            return /The run failed\. Reference: run r-10\./.test((control as MessageStrip).getText());
        },
        success: function () {
            Opa5.assert.ok(true, "run_failed shows with its reference");
        },
        errorMessage: "No run_failed strip"
    });

    Then.iStopTheApp();
});

opaTest("a new design version does not replace the older version the user chose", function (Given: Common, When: Common, Then: Common) {
    const v1: Artifact = { id: "a-d1", kind: "design", version: 1, content: "# Design v1", created_at: "2026-10-03T09:00:00" };
    const v2: Artifact = { id: "a-d2", kind: "design", version: 2, content: "# Design v2", created_at: "2026-10-03T09:30:00" };
    Given.iStartTheApp("", undefined, sessionIn("design", [v2, v1]));

    When.waitFor({
        id: "documentsMenu",
        ...OPTS,
        visible: false,
        matchers: function (menu: UI5Element) {
            return (menu as Menu).getItems().length > 0;
        },
        success: function (control: UI5Element) {
            const menu = control as Menu;
            menu.fireItemSelected({ item: menu.getItems()[0] });
        }
    });
    tabLoaded(When, "doc:design");
    When.waitFor({
        controlType: "sap.m.Select",
        ...OPTS,
        matchers: inTab("doc:design"),
        success: function (controls: UI5Element[]) {
            const select = controls[0] as Select;
            select.setSelectedKey("a-d1");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });
    tabLoaded(When, "doc:design");
    send(When, "Refine the design");
    answerShown(Then, 2, "<strong>design</strong>", "the answer arrived");
    toastSays(Then, /Design version 3 is available/, "a toast points at the new version");
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        check: function (control: UI5Element) {
            return tabs(control).find((t) => t.key === "doc:design")?.versions.length === 3;
        },
        success: function (control: UI5Element) {
            const tab = tabs(control).find((t) => t.key === "doc:design") as EditorTab;
            Opa5.assert.strictEqual(tab.artifactId, "a-d1", "the chosen version 1 stays shown");
            Opa5.assert.ok(tab.html.includes("Design v1"), "and its content too");
        },
        errorMessage: "The versions were not refreshed"
    });

    Then.iStopTheApp();
});

opaTest("revise feedback survives a refusal and a run that started meanwhile", function (Given: Common, When: Common, Then: Common) {
    const design: Artifact = { id: "a-d1", kind: "design", version: 1, content: "# design v1", created_at: "2026-10-03T09:00:00" };
    Given.iStartTheApp("", undefined, sessionIn("design", [design]));

    When.waitFor({
        id: "reviseButton",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        actions: new Press()
    });
    Then.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        check: function (control: UI5Element) {
            return document.activeElement === (control as TextArea).getFocusDomRef();
        },
        success: function () {
            Opa5.assert.ok(true, "the dialog opens with the focus in the feedback");
        },
        errorMessage: "The feedback does not have the focus"
    });
    When.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Cover the cancel path" }),
        success: function () {
            backend.failNext = {
                path: "sessions/s-2/revise", status: 409,
                body: { detail: "Stage changed.", code: "stage_changed" }
            };
        }
    });
    When.waitFor({ id: "reviseSendButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    messageBoxSays(Then, /./, "the refusal is shown");
    closeMessageBox(When);
    Then.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "value", value: "Cover the cancel path" }),
        success: function (control: UI5Element) {
            Opa5.assert.ok(true, "the dialog is back with the feedback");
            // The session turns running while the dialog is open (another tab started a run).
            ideModel(control).setProperty("/session/status", "running");
        },
        errorMessage: "The refused feedback is lost"
    });
    When.waitFor({ id: "reviseSendButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "valueState", value: "Error" }),
        success: function (control: UI5Element) {
            const area = control as TextArea;
            Opa5.assert.strictEqual(area.getValue(), "Cover the cancel path", "the feedback is kept");
            Opa5.assert.ok(/still working/.test(area.getValueStateText()), "the dialog says why");
            Opa5.assert.strictEqual(backend.requests.filter((r) => r.endsWith("/revise")).length, 1,
                "nothing more was sent");
        },
        errorMessage: "The dialog closed or did not say why"
    });

    Then.iStopTheApp();
});

opaTest("each session keeps its own chat draft", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => { fake.addSession("Second session"); });

    function typeDraft(text: string): void {
        When.waitFor({ id: "chatInput", ...OPTS, actions: new EnterText({ text }) });
    }
    function draftIs(text: string, message: string): void {
        Then.waitFor({
            id: "chatInput",
            ...OPTS,
            matchers: new PropertyStrictEquals({ name: "value", value: text }),
            success: function () {
                Opa5.assert.ok(true, message);
            },
            errorMessage: `The draft is not "${text}"`
        });
    }

    typeDraft("draft for the second");
    pressSession(When, "Explain the order class");
    draftIs("", "the other session starts with an empty draft");
    typeDraft("draft for the first");
    pressSession(When, "Second session");
    draftIs("draft for the second", "the second session's draft is back");
    pressSession(When, "Explain the order class");
    draftIs("draft for the first", "and the first one's");

    Then.iStopTheApp();
});

// The tab is busy (blocked) during the user's own Refresh, so the race is a
// run's re-read of a changed file (not busy) with a Lint pressed meanwhile.
opaTest("a Lint while a changed file is re-read does not drop the re-read", function (Given: Common, When: Common, Then: Common) {
    const f = file("zcl_lint");
    let release: (() => void) | undefined;
    Given.iStartTheApp("", undefined, sessionIn("propose", [], [f]));

    openFileTab(When, f.path);
    when(Then, () => true, function () {
        release = backend.hold("GET sessions/s-2/file");
    }, "not reached");
    send(When, "Propose the fix");
    When.waitFor({
        controlType: "sap.m.Button",
        ...OPTS,
        visible: false,
        matchers: inTab(`file:${f.path}`),
        check: function () {
            return backend.requests.filter((r) => r === "GET sessions/s-2/file").length === 2;
        },
        success: function (controls: UI5Element[]) {
            const lint = controls.find((c) => c.getId().includes("lintButton")) as Control;
            (lint as unknown as { firePress(): void }).firePress();
        },
        errorMessage: "The changed file was not re-read"
    });
    when(Then, () => backend.responses.includes("POST sessions/s-2/file/lint"), () => release?.(),
        "The lint did not answer");
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        check: function (control: UI5Element) {
            const tab = tabs(control).find((t) => t.key === `file:${f.path}`);
            return !!tab && tab.hasProposal && !tab.busy && !!tab.lintSummary;
        },
        success: function () {
            Opa5.assert.ok(true, "both the re-read and the lint result were applied");
        },
        errorMessage: "The re-read was dropped as stale by the lint"
    });

    Then.iStopTheApp();
});

opaTest("a run is announced, the focus comes back to the input, the stage bar is labelled", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    when(Then, () => true, recordAnnouncements, "not reached");
    Then.waitFor({
        id: "stageBar",
        ...OPTS,
        success: function (control: UI5Element) {
            const dom = (control as Control).getDomRef() as HTMLElement;
            const label = (dom.getAttribute("aria-labelledby") ?? "").split(" ")
                .map((id) => document.getElementById(id)?.textContent).join(" ");
            Opa5.assert.strictEqual(dom.getAttribute("role"), "group", "the stage bar is a group");
            Opa5.assert.strictEqual(label, "Stages", "labelled by the stageBarLabel text");
        }
    });
    send(When, "Explain ZCL_DEMO");
    answerShown(Then, 2, "<strong>chat</strong>", "the answer arrived");
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        check: function (control: UI5Element) {
            return announced.includes("Answer complete")
                && document.activeElement === (control as TextArea).getFocusDomRef();
        },
        success: function () {
            Opa5.assert.ok(announced.includes("The assistant is answering…"), "the start was announced");
            Opa5.assert.ok(true, "the end was announced and the focus is back in the input");
        },
        errorMessage: `Announced: ${JSON.stringify(announced)}; focus not restored`
    });

    Then.iStopTheApp();
});

// --- FIX-15: the Task 22 minors ---

/** The Ide view's controller, for a journey that must act while a modal box blocks the UI. */
function ideController(control: UI5Element): { selectSession(sid: string): Promise<void> } {
    let parent: UI5Element | null = control;
    while (parent && parent.getMetadata().getName() !== "sap.ui.core.mvc.XMLView") {
        parent = parent.getParent() as UI5Element | null;
    }
    return (parent as unknown as { getController(): unknown }).getController() as { selectSession(sid: string): Promise<void> };
}

opaTest("a polled run whose end cannot be reloaded is retried by the next poll", function (Given: Common, When: Common, Then: Common) {
    let release: (() => void) | undefined;
    Given.iStartTheApp("", undefined, (fake) => {
        fastPolling(fake);
        sessionIn("design")(fake);
        release = fake.pauseStream();
    });

    send(When, "Design a guard");
    stopVisible(Then, true, "the run is going");
    pressSession(When, "Explain the order class");
    stopVisible(Then, false, "the other session is idle");
    pressSession(When, "Session in design");
    stopVisible(Then, true, "back on the session: it is still running (server status)");
    when(Then, () => backend.requests.filter((r) => r === "GET sessions/s-2").length >= 3, () => {
        // The poll that sees the run end passes; the session reload after it fails once,
        // so the model still says running.
        backend.failNext = { path: "sessions/s-2", status: 500, body: { detail: "session unavailable" }, skip: 1 };
        release?.();
    }, "The running session is not polled");
    messageBoxSays(Then, /session unavailable/, "the failed reload is shown");
    closeMessageBox(When);
    stopVisible(Then, false, "the poll went on and saw the run end");
    inputEditable(Then, true, "a message can be sent again");
    answerShown(Then, 2, "<strong>design</strong>", "the stored answer is shown");
    restorePolling(Then);

    Then.iStopTheApp();
});

opaTest("a failed file re-read does not leave a later changed file stale", function (Given: Common, When: Common, Then: Common) {
    const a = file("zcl_a");
    const b = file("zcl_b");
    let releaseGap: (() => void) | undefined;
    let releaseFiles: (() => void) | undefined;
    Given.iStartTheApp("", undefined, sessionIn("propose", [], [a, b]));

    openFileTab(When, a.path);
    openFileTab(When, b.path);
    when(Then, () => true, function () {
        releaseGap = backend.splitFiles();
        releaseFiles = backend.hold("GET sessions/s-2/files");
        // The first round's re-read of a fails.
        backend.failNext = { path: "sessions/s-2/file", status: 500, body: { detail: "file read failed" } };
    }, "not reached");
    send(When, "Propose the fix");
    when(Then, () => backend.requests.includes("GET sessions/s-2/files"), () => releaseGap?.(),
        "The first file event did not reload the tree");
    when(Then, () => backend.streamsFlushed === 1, () => releaseFiles?.(), "The stream did not finish");
    messageBoxSays(Then, /file read failed/, "the failed re-read is shown");
    closeMessageBox(When);
    Then.waitFor({
        id: "editorTabs",
        ...OPTS,
        check: function (control: UI5Element) {
            return !!tabs(control).find((t) => t.key === `file:${b.path}`)?.hasProposal;
        },
        success: function () {
            Opa5.assert.ok(true, "b, changed during the failed round, was still re-read");
        },
        errorMessage: "A path that arrived during a failed round stayed pending"
    });

    Then.iStopTheApp();
});

opaTest("refused revise feedback does not reopen in another session; it waits for its own", function (Given: Common, When: Common, Then: Common) {
    const design: Artifact = { id: "a-d1", kind: "design", version: 1, content: "# design v1", created_at: "2026-10-03T09:00:00" };
    Given.iStartTheApp("", undefined, sessionIn("design", [design]));

    When.waitFor({
        id: "reviseButton",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        actions: new Press()
    });
    When.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Cover the cancel path" }),
        success: function () {
            backend.failNext = {
                path: "sessions/s-2/revise", status: 409,
                body: { detail: "Stage changed.", code: "stage_changed" }
            };
        }
    });
    When.waitFor({ id: "reviseSendButton", ...OPTS, searchOpenDialogs: true, actions: new Press() });
    messageBoxSays(Then, /./, "the refusal is shown");
    // The session changes while the refusal is still on screen.
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        autoWait: false,
        success: function (control: UI5Element) {
            void ideController(control).selectSession("s-1");
        }
    });
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        autoWait: false,
        check: function (control: UI5Element) {
            return ideModel(control).getProperty("/selectedId") === "s-1";
        },
        errorMessage: "The session did not change"
    });
    closeMessageBox(When);
    Then.waitFor({
        id: "chatInput",
        ...OPTS,
        success: function (control: UI5Element) {
            const dialog = UI5Element.getElementById(control.getId().replace(/chatInput$/, "reviseDialog")) as unknown as
                { isOpen(): boolean } | undefined;
            Opa5.assert.notOk(dialog?.isOpen(), "the revise dialog stays closed in the other session");
            Opa5.assert.strictEqual(ideModel(control).getProperty("/revise/feedback"), "",
                "the other session sees none of the feedback");
        }
    });
    pressSession(When, "Session in design");
    When.waitFor({
        id: "reviseButton",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        actions: new Press()
    });
    Then.waitFor({
        id: "reviseFeedback",
        ...OPTS,
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "value", value: "Cover the cancel path" }),
        success: function () {
            Opa5.assert.ok(true, "back in its own session, the refused feedback is offered again");
        },
        errorMessage: "The refused feedback is lost"
    });

    Then.iStopTheApp();
});
