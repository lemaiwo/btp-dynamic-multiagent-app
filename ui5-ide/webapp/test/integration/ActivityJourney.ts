import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type Panel from "sap/m/Panel";
import type Common from "./pages/Common";

QUnit.module("Activity journey");

const OPTS = { viewName: "Ide" };

opaTest("the activity panel is collapsed, then shows the plan and the tool calls of a run", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("", undefined, (fake) => { fake.withActivity = true; });

    Then.waitFor({
        id: "activityPanel",
        ...OPTS,
        success: function (control: UI5Element) {
            const panel = control as Panel;
            Opa5.assert.strictEqual(panel.getExpandable(), true, "the panel is expandable");
            Opa5.assert.strictEqual(panel.getExpanded(), false, "and collapsed by default");
        }
    });
    When.waitFor({
        id: "chatInput",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "enabled", value: true }),
        actions: new EnterText({ text: "Explain ZCL_DEMO" })
    });
    When.waitFor({ id: "sendButton", ...OPTS, actions: new Press() });
    When.waitFor({
        id: "activityPanel",
        ...OPTS,
        matchers: new PropertyStrictEquals({ name: "headerText", value: "Activity (1 tool calls, 1/2 todos)" }),
        actions: function (control: UI5Element | null) {
            (control as Panel).setExpanded(true);
        },
        errorMessage: "The panel header does not count the run's activity"
    });
    Then.waitFor({
        id: "activityTodos",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function (list: UI5Element) {
            const marks = (list as unknown as { getItems(): UI5Element[] }).getItems()
                .map((i) => i.getBindingContext("ide")?.getProperty("mark"));
            Opa5.assert.deepEqual(marks, ["[x]", "[~]"], "the plan shows done and in-progress todos");
        },
        errorMessage: "The plan is not shown"
    });
    Then.waitFor({
        id: "activityTimeline",
        ...OPTS,
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (list: UI5Element) {
            const item = (list as unknown as { getItems(): UI5Element[] }).getItems()[0];
            const ctx = item.getBindingContext("ide");
            Opa5.assert.strictEqual(ctx?.getProperty("title"), "SAPRead");
            Opa5.assert.strictEqual(ctx?.getProperty("state"), "Success", "start and end share one row, now ok");
            Opa5.assert.ok(String(ctx?.getProperty("output")).includes("CLASS zcl_demo"), "the output is kept");
        },
        errorMessage: "The timeline does not hold one row for the call"
    });

    Then.iStopTheApp();
});
