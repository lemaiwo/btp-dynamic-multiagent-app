import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import AggregationLengthEquals from "sap/ui/test/matchers/AggregationLengthEquals";
import type UI5Element from "sap/ui/core/Element";
import type List from "sap/m/List";
import type ObjectListItem from "sap/m/ObjectListItem";
import type Tree from "sap/m/Tree";
import type ObjectStatus from "sap/m/ObjectStatus";
import type Common from "./pages/Common";
import { backend } from "./pages/Common";

QUnit.module("Session journey");

opaTest("the explorer lists the caller's sessions and selects the newest", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (control: UI5Element) {
            const list = control as List;
            const item = list.getSelectedItem() as ObjectListItem | null;
            Opa5.assert.ok(item, "a session is selected");
            Opa5.assert.strictEqual(item?.getTitle(), "Explain the order class", "the seeded session is selected");
        }
    });

    Then.waitFor({
        id: "workspaceTree",
        viewName: "Ide",
        success: function (control: UI5Element) {
            const tree = control as Tree;
            Opa5.assert.ok(tree.getAriaLabelledBy().length > 0, "the tree has an accessible name");
        }
    });

    Then.iStopTheApp();
});

opaTest("create a session, then open ZCL_DEMO into its workspace", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    // Wait for the initial load so the new session is not raced by it.
    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function () {
            Opa5.assert.ok(true, "the session list has loaded");
        }
    });

    When.waitFor({
        id: "newSessionButton",
        viewName: "Ide",
        actions: new Press(),
        errorMessage: "No 'New session' button"
    });
    When.waitFor({
        id: "newSessionTitle",
        viewName: "Ide",
        searchOpenDialogs: true,
        actions: new EnterText({ text: "Refactor the demo class" }),
        errorMessage: "The new-session dialog did not open"
    });
    Then.waitFor({
        id: "newSessionTarget",
        viewName: "Ide",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "selectedKey", value: "dev-system" }),
        success: function () {
            Opa5.assert.ok(true, "the only target is preselected");
        }
    });
    When.waitFor({
        id: "newSessionCreate",
        viewName: "Ide",
        searchOpenDialogs: true,
        actions: new Press()
    });

    Then.waitFor({
        id: "sessionList",
        viewName: "Ide",
        matchers: new AggregationLengthEquals({ name: "items", length: 2 }),
        success: function (control: UI5Element) {
            const list = control as List;
            const item = list.getSelectedItem() as ObjectListItem | null;
            Opa5.assert.strictEqual(item?.getTitle(), "Refactor the demo class", "the new session is selected");
            Opa5.assert.strictEqual(item?.getFirstStatus()?.getText(), "Chat", "its stage reads Chat");
            Opa5.assert.ok(backend.requests.includes("POST sessions"), "the session was created through the API");
        },
        errorMessage: "The new session did not appear selected"
    });

    When.waitFor({
        id: "openObjectSearch",
        viewName: "Ide",
        actions: new EnterText({ text: "ZCL_DEMO", pressEnterKey: true })
    });
    When.waitFor({
        id: "openObjectName",
        viewName: "Ide",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "value", value: "ZCL_DEMO" }),
        actions: new EnterText({ text: "ZCL_DEMO", pressEnterKey: true }),
        errorMessage: "The open-object dialog did not take over the search text"
    });
    When.waitFor({
        id: "openObjectResults",
        viewName: "Ide",
        searchOpenDialogs: true,
        matchers: new AggregationLengthEquals({ name: "items", length: 1 }),
        success: function (control: UI5Element) {
            const list = control as List;
            new Press().executeOn(list.getItems()[0]);
        },
        errorMessage: "The object search found nothing"
    });

    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        viewName: "Ide",
        matchers: function (status: UI5Element) {
            const ctx = status.getBindingContext("ide");
            return !!ctx && ctx.getProperty("path") === "src/CLAS/zcl_demo.clas.abap";
        },
        success: function (controls: UI5Element[]) {
            const statuses = controls as ObjectStatus[];
            Opa5.assert.strictEqual(statuses[0].getText(), "read", "the file carries the badge 'read'");
            Opa5.assert.strictEqual(statuses[0].getState(), "Information", "the read badge is Information");
            Opa5.assert.ok(backend.requests.some((r) => /^POST sessions\/[^/]+\/open$/.test(r)), "opened through the API");
        },
        errorMessage: "The tree does not show src/CLAS/zcl_demo.clas.abap"
    });

    Then.iStopTheApp();
});
