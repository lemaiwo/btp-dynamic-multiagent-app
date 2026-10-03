import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type Control from "sap/ui/core/Control";
import type TabContainer from "sap/m/TabContainer";
import type TabContainerItem from "sap/m/TabContainerItem";
import type CodeEditor from "sap/ui/codeeditor/CodeEditor";
import type HTML from "sap/ui/core/HTML";
import type Select from "sap/m/Select";
import type Menu from "sap/m/Menu";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Text from "sap/m/Text";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";
import { SAP_AUTH_FAILED, type Artifact, type WorkspaceFile } from "./FakeBackend";

QUnit.module("Editor and diff journey");

const PATH = "src/CLAS/zcl_fix.clas.abap";
const ORIGIN = "CLASS zcl_fix IMPLEMENTATION.\n  METHOD run.\n  ENDMETHOD.\nENDCLASS.";
const PROPOSED = "CLASS zcl_fix IMPLEMENTATION.\n  METHOD run.\n    \" <script>alert(1)</script>\n  ENDMETHOD.\nENDCLASS.";

const FIX: WorkspaceFile = {
    path: PATH, state: "modified", object_type: "CLAS", object_name: "ZCL_FIX",
    origin_source: ORIGIN, proposed_source: PROPOSED,
    lint: [{ line: 3, column: 5, severity: "warning", message: "Comment in method", rule: "comment_check" }]
};

const DESIGNS: Artifact[] = [
    { id: "a-design-2", kind: "design", version: 2, content: "# Design v2\n\nUse a <script>alert(1)</script> guard.", created_at: "2026-10-03T10:00:00" },
    { id: "a-design-1", kind: "design", version: 1, content: "# Design v1", created_at: "2026-10-03T09:00:00" }
];

interface AceLike { getSession(): { getAnnotations(): { row: number; type: string; text: string }[] } }

function withFix(fake: typeof backend): void {
    fake.addSession("Propose a fix", [FIX], DESIGNS.slice());
}

/** The control of `type` inside the editor tab whose key is `key`. */
function inTab(type: string, key: string) {
    return function (control: UI5Element): boolean {
        let parent = control.getParent() as UI5Element | null;
        while (parent && parent.getMetadata().getName() !== "sap.m.TabContainerItem") {
            parent = parent.getParent() as UI5Element | null;
        }
        return control.isA(type) && !!parent && (parent as unknown as TabContainerItem).getKey() === key;
    };
}

function openFix(When: Common): void {
    When.waitFor({
        controlType: "sap.m.CustomTreeItem",
        viewName: "Ide",
        matchers: function (item: UI5Element) {
            return item.getBindingContext("ide")?.getProperty("path") === PATH;
        },
        actions: new Press(),
        errorMessage: `The tree does not show ${PATH}`
    });
}

function pressMode(When: Common, key: "source" | "proposed" | "diff"): void {
    // SegmentedButtonItem is an Element, which OPA does not search for; press
    // its rendered <li> through the SegmentedButton control instead.
    When.waitFor({
        controlType: "sap.m.SegmentedButton",
        viewName: "Ide",
        matchers: inTab("sap.m.SegmentedButton", `file:${PATH}`),
        success: function (controls: UI5Element[]) {
            const segmented = controls[0] as SegmentedButton;
            const item = segmented.getItems().find((i) => i.getKey() === key);
            Opa5.assert.ok(item?.getEnabled(), `the '${key}' toggle is enabled`);
            new Press().executeOn(item as unknown as Control);
        },
        errorMessage: `No mode toggle in the file tab`
    });
}

function editorShows(Then: Common, value: string, message: string): void {
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        viewName: "Ide",
        matchers: [inTab("sap.ui.codeeditor.CodeEditor", `file:${PATH}`), new PropertyStrictEquals({ name: "value", value })],
        success: function (controls: UI5Element[]) {
            const editor = controls[0] as CodeEditor;
            Opa5.assert.strictEqual(editor.getEditable(), false, "the editor is read-only");
            Opa5.assert.strictEqual(editor.getType(), "abap", "an .abap file uses ABAP highlighting");
            Opa5.assert.ok(true, message);
        },
        errorMessage: `The editor does not show the expected text (${message})`
    });
}

opaTest("open a modified file, toggle Source / Proposed / Diff, lint and close it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withFix);

    Then.waitFor({
        id: "editorTitle",
        viewName: "Ide",
        success: function () {
            Opa5.assert.ok(true, "the editor pane has a heading");
        }
    });

    openFix(When);
    Then.waitFor({
        id: "editorTabs",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (control: UI5Element) {
            const tabs = control as TabContainer;
            const item = tabs.getItems()[0];
            Opa5.assert.strictEqual(item.getName(), "zcl_fix.clas.abap", "the tab is named after the file");
            Opa5.assert.strictEqual(tabs.getSelectedItem(), item.getId(), "the new tab is selected");
            Opa5.assert.ok(backend.requests.includes(`GET sessions/s-2/file`), "the file was read through the API");
        },
        errorMessage: "No editor tab opened"
    });
    editorShows(Then, PROPOSED, "a modified file opens on its proposed source");

    pressMode(When, "source");
    editorShows(Then, ORIGIN, "Source shows the diff base");

    pressMode(When, "diff");
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        viewName: "Ide",
        matchers: inTab("sap.ui.core.HTML", `file:${PATH}`),
        check: function (controls: UI5Element[]) {
            const dom = (controls[0] as HTML).getDomRef();
            return !!dom && dom.querySelectorAll("tr.ideDiffAdd").length > 0;
        },
        success: function (controls: UI5Element[]) {
            const dom = (controls[0] as HTML).getDomRef() as HTMLElement;
            const add = dom.querySelector("tr.ideDiffAdd") as HTMLElement;
            Opa5.assert.ok(add.textContent?.includes("<script>alert(1)</script>"), "the added line reads as source text");
            Opa5.assert.strictEqual(dom.querySelectorAll("script").length, 0, "the diff host holds no script element");
            Opa5.assert.ok(dom.querySelector("caption"), "the diff table has an accessible caption");
            Opa5.assert.strictEqual(add.querySelector(".ideDiffMark")?.textContent, "+added line",
                "the added row has a + marker with a text for screen readers");
        },
        errorMessage: "The diff host has no add row"
    });

    pressMode(When, "proposed");
    editorShows(Then, PROPOSED, "Proposed shows the proposal again");
    When.waitFor({
        controlType: "sap.m.Button",
        viewName: "Ide",
        matchers: inTab("sap.m.Button", `file:${PATH}`),
        success: function (controls: UI5Element[]) {
            const lint = controls.find((b) => b.getId().includes("lintButton"));
            Opa5.assert.ok(lint, "the file tab has a Lint button");
            new Press().executeOn(lint as unknown as Control);
        }
    });
    Then.waitFor({
        controlType: "sap.ui.codeeditor.CodeEditor",
        viewName: "Ide",
        matchers: inTab("sap.ui.codeeditor.CodeEditor", `file:${PATH}`),
        check: function (controls: UI5Element[]) {
            const ace = (controls[0] as unknown as { getInternalEditorInstance(): AceLike }).getInternalEditorInstance();
            return ace.getSession().getAnnotations().length === 1;
        },
        success: function (controls: UI5Element[]) {
            const ace = (controls[0] as unknown as { getInternalEditorInstance(): AceLike }).getInternalEditorInstance();
            const [a] = ace.getSession().getAnnotations();
            Opa5.assert.deepEqual([a.row, a.type], [2, "warning"], "the finding marks line 3 as a warning");
            Opa5.assert.ok(a.text.includes("comment_check"), "the marker names the rule");
            Opa5.assert.ok(backend.requests.includes("POST sessions/s-2/file/lint"), "linted through the API");
        },
        errorMessage: "No lint marker in the editor"
    });

    When.waitFor({
        id: "editorTabs",
        viewName: "Ide",
        success: function (control: UI5Element) {
            const tabs = control as TabContainer;
            tabs.fireItemClose({ item: tabs.getItems()[0] });
        }
    });
    Then.waitFor({
        id: "editorTabs",
        viewName: "Ide",
        visible: false,
        matchers: new AggregationLengthEquals({ name: "items", length: 0 }),
        success: function () {
            Opa5.assert.ok(true, "closing the tab removes it");
        }
    });
    Then.waitFor({
        id: "editorEmpty",
        viewName: "Ide",
        success: function () {
            Opa5.assert.ok(true, "the empty-editor hint is back");
        }
    });

    Then.iStopTheApp();
});

opaTest("open the design document, rendered and sanitized, with its versions", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withFix);

    When.waitFor({
        id: "documentsButton",
        viewName: "Ide",
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        actions: new Press(),
        errorMessage: "No enabled Documents button"
    });
    // The menu renders clones of its items in a popover; choosing one fires
    // itemSelected on the Menu with the bound item, as done here.
    When.waitFor({
        id: "documentsMenu",
        viewName: "Ide",
        visible: false,
        success: function (control: UI5Element) {
            const menu = control as Menu;
            const design = menu.getItems().find((i) => i.getText() === "Design");
            Opa5.assert.ok(design, "the Documents menu offers Design");
            Opa5.assert.strictEqual(menu.getItems().length, 1, "only the kinds this session has are offered");
            menu.fireItemSelected({ item: design });
        },
        errorMessage: "No Documents menu"
    });
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        viewName: "Ide",
        matchers: inTab("sap.ui.core.HTML", "doc:design"),
        check: function (controls: UI5Element[]) {
            return !!(controls[0] as HTML).getDomRef()?.querySelector("h1");
        },
        success: function (controls: UI5Element[]) {
            const dom = (controls[0] as HTML).getDomRef() as HTMLElement;
            Opa5.assert.strictEqual(dom.querySelector("h1")?.textContent, "Design v2", "the latest version is shown");
            Opa5.assert.strictEqual(dom.querySelectorAll("script").length, 0, "the script was sanitized away");
        },
        errorMessage: "The design document did not render"
    });
    Then.waitFor({
        controlType: "sap.m.Select",
        viewName: "Ide",
        matchers: inTab("sap.m.Select", "doc:design"),
        success: function (controls: UI5Element[]) {
            const select = controls[0] as Select;
            Opa5.assert.strictEqual(select.getItems().length, 2, "both versions are offered");
            Opa5.assert.strictEqual(select.getSelectedKey(), "a-design-2", "the latest is selected");
        }
    });

    Then.iStopTheApp();
});

opaTest("a stale document answer does not overwrite the version chosen after it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withFix);
    let release: (() => void) | undefined;

    When.waitFor({
        id: "documentsMenu",
        viewName: "Ide",
        visible: false,
        matchers: function (menu: UI5Element) {
            return (menu as Menu).getItems().length > 0;
        },
        success: function (control: UI5Element) {
            const menu = control as Menu;
            menu.fireItemSelected({ item: menu.getItems()[0] });
        }
    });
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        viewName: "Ide",
        matchers: inTab("sap.ui.core.HTML", "doc:design"),
        check: function (controls: UI5Element[]) {
            return (controls[0] as HTML).getDomRef()?.querySelector("h1")?.textContent === "Design v2";
        },
        success: function () {
            Opa5.assert.ok(true, "version 2 is shown");
        }
    });
    // Ask for version 1 (held in flight), then go back to version 2 at once.
    When.waitFor({
        controlType: "sap.m.Select",
        viewName: "Ide",
        matchers: inTab("sap.m.Select", "doc:design"),
        success: function (controls: UI5Element[]) {
            const select = controls[0] as Select;
            release = backend.hold("GET sessions/s-2/artifacts/a-design-1");
            select.setSelectedKey("a-design-1");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
            select.setSelectedKey("a-design-2");
            select.fireChange({ selectedItem: select.getSelectedItem() ?? undefined });
        }
    });
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        viewName: "Ide",
        matchers: inTab("sap.ui.core.HTML", "doc:design"),
        check: function () {
            return backend.responses.filter((r) => r === "GET sessions/s-2/artifacts/a-design-2").length === 2;
        },
        success: function () {
            release?.();
        }
    });
    Then.waitFor({
        controlType: "sap.ui.core.HTML",
        viewName: "Ide",
        matchers: inTab("sap.ui.core.HTML", "doc:design"),
        check: function () {
            return backend.responses.includes("GET sessions/s-2/artifacts/a-design-1");
        },
        success: function (controls: UI5Element[]) {
            const html = controls[0] as HTML;
            Opa5.assert.strictEqual(html.getDomRef()?.querySelector("h1")?.textContent, "Design v2",
                "the late answer for version 1 was dropped");
            const tab = html.getBindingContext("ide")?.getObject() as { artifactId: string; busy: boolean };
            Opa5.assert.strictEqual(tab.artifactId, "a-design-2", "version 2 stays selected");
            Opa5.assert.strictEqual(tab.busy, false, "the tab is not busy");
        },
        errorMessage: "The held version-1 answer never arrived"
    });

    Then.iStopTheApp();
});

opaTest("a refresh that SAP refuses shows SAP's message with the user-mapping hint", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, withFix);

    openFix(When);
    editorShows(Then, PROPOSED, "the file is open");
    When.waitFor({
        controlType: "sap.m.Button",
        viewName: "Ide",
        matchers: inTab("sap.m.Button", `file:${PATH}`),
        success: function (controls: UI5Element[]) {
            backend.failNext = { path: "sessions/s-2/file/refresh", ...SAP_AUTH_FAILED };
            const refresh = controls.find((b) => b.getId().includes("refreshButton"));
            Opa5.assert.ok(refresh, "the file tab has a Refresh button");
            new Press().executeOn(refresh as unknown as Control);
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        searchOpenDialogs: true,
        matchers: function (control: UI5Element) {
            return /SAP logon failed.*user mapping/i.test(String((control as Text).getProperty("text")));
        },
        success: function () {
            Opa5.assert.ok(true, "the 502 is shown with the hint");
        },
        errorMessage: "No SAP error message"
    });

    Then.iStopTheApp();
});
