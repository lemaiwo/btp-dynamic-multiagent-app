import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import UI5Element from "sap/ui/core/Element";
import type List from "sap/m/List";
import type ObjectListItem from "sap/m/ObjectListItem";
import type Tree from "sap/m/Tree";
import type Text from "sap/m/Text";
import type SearchField from "sap/m/SearchField";
import type InvisibleText from "sap/ui/core/InvisibleText";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { READONLY_REFUSED, SAP_AUTH_FAILED, USER_TOKEN_REQUIRED, type WorkspaceFile } from "./FakeBackend";

QUnit.module("Explorer journey");

const DEMO: WorkspaceFile = {
    path: "src/CLAS/zcl_other.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_OTHER",
    origin_source: "CLASS zcl_other DEFINITION PUBLIC.\nENDCLASS.", proposed_source: ""
};

/** Two sessions: the seeded "Explain the order class" (s-1, one file) and "Second session" (newest, `files`). */
function twoSessions(files: WorkspaceFile[] = []): (fake: typeof backend) => void {
    return (fake) => {
        fake.addSession("Second session", files);
    };
}

function pressSession(Then: Common, title: string): void {
    Then.waitFor({
        controlType: "sap.m.ObjectListItem",
        viewName: "Ide",
        matchers: new PropertyStrictEquals({ name: "title", value: title }),
        actions: new Press(),
        errorMessage: `No session '${title}' in the list`
    });
}

function treeHasFiles(Then: Common, count: number, message: string): void {
    Then.waitFor({
        id: "workspaceTree",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: count === 0 ? 0 : count + 2 }),
        success: function () {
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The tree does not show ${count} file(s)`
    });
}

function openDemoObject(When: Common, name: string): void {
    When.waitFor({
        id: "openObjectSearch",
        viewName: "Ide",
        actions: new EnterText({ text: name, pressEnterKey: true })
    });
    When.waitFor({
        id: "openObjectResults",
        viewName: "Ide",
        searchOpenDialogs: true,
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (control: UI5Element) {
            new Press().executeOn((control as List).getItems()[0]);
        },
        errorMessage: "The object search found nothing"
    });
}

function expectMessageBox(Then: Common, pattern: RegExp, message: string): void {
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

opaTest("switching sessions swaps the workspace and the header title", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, twoSessions([DEMO]));

    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function (control: UI5Element) {
            const item = (control as List).getSelectedItem() as ObjectListItem | null;
            Opa5.assert.strictEqual(item?.getTitle(), "Second session", "the newest session is selected");
        }
    });
    treeHasFiles(Then, 1, "the second session's workspace shows one file");

    pressSession(When, "Explain the order class");
    Then.waitFor({
        id: "sessionTitle",
        viewName: "App",
        matchers: new PropertyStrictEquals({ name: "text", value: "Explain the order class" }),
        success: function () {
            Opa5.assert.ok(true, "the header shows the newly selected session");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        viewName: "Ide",
        matchers: new PropertyStrictEquals({ name: "text", value: "zcl_demo.clas.abap" }),
        success: function () {
            Opa5.assert.ok(true, "the tree shows the first session's file");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        viewName: "Ide",
        success: function (controls: UI5Element[]) {
            const names = (controls as Text[]).map((t) => String(t.getProperty("text")));
            Opa5.assert.notOk(names.includes("zcl_other.clas.abap"), "the second session's file is gone");
        }
    });

    Then.waitFor({
        id: "openObjectSearch",
        viewName: "Ide",
        success: function (control: UI5Element) {
            const field = control as SearchField;
            const label = UI5Element.getElementById(field.getAriaLabelledBy()[0]) as InvisibleText;
            Opa5.assert.strictEqual(label?.getText(), "Open object", "the search field has its own accessible name");
        }
    });

    Then.iStopTheApp();
});

opaTest("deleting the selected session selects the next one", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, twoSessions());

    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function () {
            Opa5.assert.ok(true, "two sessions are listed");
        }
    });
    When.waitFor({ id: "deleteSessionButton", viewName: "Ide", actions: new Press() });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "OK" }),
        actions: new Press(),
        errorMessage: "No confirmation dialog"
    });
    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (control: UI5Element) {
            const item = (control as List).getSelectedItem() as ObjectListItem | null;
            Opa5.assert.strictEqual(item?.getTitle(), "Explain the order class", "the remaining session is selected");
            Opa5.assert.ok(backend.requests.includes("DELETE sessions/s-2"), "deleted through the API");
        }
    });

    Then.iStopTheApp();
});

opaTest("a slow open in session A does not overwrite session B's workspace", function (Given: Common, When: Common, Then: Common) {
    let release: () => void = () => undefined;
    Given.iStartTheApp("", undefined, twoSessions());

    pressSession(When, "Explain the order class");
    treeHasFiles(Then, 1, "session A shows its file");
    When.waitFor({
        success: function () {
            release = backend.hold("GET sessions/s-1/files");
        }
    });
    openDemoObject(When, "ZI_DEMO");
    // The open answered and the workspace reload is in flight: switch to B.
    When.waitFor({
        check: function () {
            return backend.requests.includes("GET sessions/s-1/files");
        },
        success: function () {
            Opa5.assert.ok(true, "session A's workspace reload is pending");
        }
    });
    pressSession(When, "Second session");
    treeHasFiles(Then, 0, "session B's workspace is empty");
    When.waitFor({
        success: function () {
            release();
        }
    });
    Then.waitFor({
        check: function () {
            return backend.responses.includes("GET sessions/s-1/files");
        },
        success: function () {
            Opa5.assert.ok(true, "session A's reload has answered");
        }
    });
    Then.waitFor({
        id: "workspaceTree",
        viewName: "Ide",
        success: function (control: UI5Element) {
            const ide = (control as Tree).getModel("ide") as JSONModel;
            Opa5.assert.strictEqual((control as Tree).getItems().length, 0, "B's tree is unchanged");
            Opa5.assert.strictEqual(ide.getProperty("/selectedId"), "s-2", "B stays selected");
            Opa5.assert.strictEqual(ide.getProperty("/selectedPath"), "", "A's file is not selected in B");
        }
    });

    Then.iStopTheApp();
});

opaTest("a missing user token on open shows the reload message", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", { path: "sessions/s-1/open", ...USER_TOKEN_REQUIRED });

    treeHasFiles(Then, 1, "the session has loaded");
    openDemoObject(When, "ZI_DEMO");
    expectMessageBox(Then, /SAP user token is missing.*reload/i, "the 424 maps to the user-token message");

    Then.iStopTheApp();
});

opaTest("a read-only guard refusal on open says so, not missing role", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", { path: "sessions/s-1/open", ...READONLY_REFUSED });

    treeHasFiles(Then, 1, "the session has loaded");
    openDemoObject(When, "ZI_DEMO");
    expectMessageBox(Then, /not allowed in the read-only IDE/, "the 403 readonly_refused maps to the read-only message");

    Then.iStopTheApp();
});

opaTest("an ARC-1 logon failure shows SAP's message and the user-mapping hint", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", { path: "objects/search", ...SAP_AUTH_FAILED });

    treeHasFiles(Then, 1, "the session has loaded");
    When.waitFor({
        id: "openObjectSearch",
        viewName: "Ide",
        actions: new EnterText({ text: "ZI_DEMO", pressEnterKey: true })
    });
    expectMessageBox(Then, /SAP logon failed for the mapped user.*user mapping/i, "the 502 carries SAP's message and the hint");

    Then.iStopTheApp();
});
