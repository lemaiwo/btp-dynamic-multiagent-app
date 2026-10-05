import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Table from "sap/m/Table";
import type Button from "sap/m/Button";
import type Dialog from "sap/m/Dialog";
import type Text from "sap/m/Text";
import type SideNavigation from "sap/tnt/SideNavigation";
import type NavigationList from "sap/tnt/NavigationList";
import type View from "sap/ui/core/mvc/View";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type UI5Element from "sap/ui/core/Element";
import type ManagedObject from "sap/ui/base/ManagedObject";
import type { ODataServiceInput, ODataServiceSummary } from "../../service/types";
import Common, { backend } from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("OData services list journey");

/** The row of the service called `name`, wherever the list's order puts it. */
function rowOf(table: Table, name: string): ODataServiceSummary {
    const rows = (table.getModel("odata") as JSONModel).getProperty("/items") as ODataServiceSummary[];
    return rows.filter((row) => row.name === name)[0];
}

/** The view a control lives in. */
function viewOf(element: UI5Element): View {
    let current: ManagedObject | null = element;
    while (current && current.getMetadata().getName() !== "sap.ui.core.mvc.XMLView") {
        current = current.getParent();
    }
    return current as View;
}

opaTest("the list shows the four services with version, identity, counts and tags", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    Then.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        success: function (element: UI5Element) {
            const table = element as Table;
            Opa5.assert.strictEqual(table.getItems().length, 4, "four rows");
            const jobs = rowOf(table, "purchase-requisitions-jobs");
            Opa5.assert.strictEqual(jobs.has_write, true, "the jobs service is marked Write");
            Opa5.assert.strictEqual(jobs.counts.entity_sets, 5, "the jobs service has five entity sets");
            Opa5.assert.strictEqual(jobs.counts.operations, 1, "and one operation");
            Opa5.assert.strictEqual(
                rowOf(table, "purchase-requisitions").has_write, false, "the read-only service is not"
            );
        }
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "Technical user" }),
        success: function (found: UI5Element[]) {
            Opa5.assert.strictEqual(found.length, 1, "one service runs as the technical user");
        }
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "Signed-in user" }),
        success: function (found: UI5Element[]) {
            Opa5.assert.strictEqual(found.length, 3, "three run as the signed-in user");
        }
    });
    // Invisible tags are not rendered, so only the services they apply to
    // are found: one with a write, one disabled.
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "Write" }),
        success: function (found: UI5Element[]) {
            Opa5.assert.strictEqual(found.length, 1, "one Write tag");
        }
    });
    Then.waitFor({
        controlType: "sap.m.ObjectStatus",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "Disabled" }),
        success: function (found: UI5Element[]) {
            Opa5.assert.strictEqual(found.length, 1, "one Disabled tag");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "2 agents" }),
        success: function () {
            Opa5.assert.ok(true, "the service two agents use says so");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "not used" }),
        success: function () {
            Opa5.assert.ok(true, "the unused service says so");
        }
    });
    Then.waitFor({
        controlType: "sap.m.Text",
        viewName: "ODataServices",
        matchers: new PropertyStrictEquals({ name: "text", value: "V4" }),
        success: function (found: UI5Element[]) {
            Opa5.assert.strictEqual(found.length, 1, "one V4 service");
        }
    });

    Then.iStopTheApp();
});

// The detail route maps to the same item (NAV_KEY_BY_ROUTE), but the router
// fires routeMatched only once the route's target is displayed, so that half
// is asserted by the detail journey, which comes with the detail view.
opaTest("the side navigation item sits between Skills and Job Runs and is selected on the list route", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    Then.waitFor({
        id: "sideNavigation",
        viewName: "App",
        success: function (element: UI5Element) {
            const navigation = element as SideNavigation;
            const keys = (navigation.getItem() as NavigationList).getItems().map((item) => item.getKey());
            const index = keys.indexOf("odataServices");
            Opa5.assert.strictEqual(keys[index - 1], "skills", "it follows Skills");
            Opa5.assert.strictEqual(keys[index + 1], "runs", "and precedes Job Runs");
            Opa5.assert.strictEqual(navigation.getSelectedKey(), "odataServices", "it is the selected item");
        }
    });

    Then.iStopTheApp();
});

opaTest("searching filters by title, name and purpose", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({ id: "odataSearch", viewName: "ODataServices", actions: new EnterText({ text: "partner" }) });
    Then.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        check: function (element: UI5Element) { return (element as unknown as Table).getItems().length === 1; },
        success: function (element: UI5Element) {
            const context = (element as Table).getItems()[0].getBindingContext("odata");
            Opa5.assert.strictEqual(context?.getProperty("name"), "business-partners", "found by its title");
        },
        errorMessage: "'partner' did not narrow the list to one row"
    });

    // The technical name: only the jobs copy carries "jobs" in it.
    When.waitFor({ id: "odataSearch", viewName: "ODataServices", actions: new EnterText({ text: "tions-jobs" }) });
    Then.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        check: function (element: UI5Element) {
            const items = (element as unknown as Table).getItems();
            return items.length === 1
                && items[0].getBindingContext("odata")?.getProperty("name") === "purchase-requisitions-jobs";
        },
        success: function () { Opa5.assert.ok(true, "found by its technical name"); },
        errorMessage: "'tions-jobs' did not find the jobs service"
    });

    // The purpose: "Nightly" appears nowhere else.
    When.waitFor({ id: "odataSearch", viewName: "ODataServices", actions: new EnterText({ text: "nightly" }) });
    Then.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        check: function (element: UI5Element) { return (element as unknown as Table).getItems().length === 1; },
        success: function () { Opa5.assert.ok(true, "found by its purpose"); },
        errorMessage: "'nightly' did not narrow the list to one row"
    });

    Then.iStopTheApp();
});

opaTest("pressing a row opens the detail route with the service name", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        matchers: function (element: UI5Element) {
            return (element as Table).getItems().filter((item) => (
                item.getBindingContext("odata")?.getProperty("name") === "purchase-requisitions"
            ))[0];
        },
        actions: new Press()
    });
    Then.waitFor({
        check: function () {
            return HashChanger.getInstance().getHash() === "odata-services/purchase-requisitions";
        },
        success: function () { Opa5.assert.ok(true, "the hash names the service"); },
        errorMessage: "The row did not navigate to odata-services/purchase-requisitions"
    });

    Then.iStopTheApp();
});

opaTest("New service navigates to odata-services/new", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({ id: "addODataServiceButton", viewName: "ODataServices", actions: new Press() });
    Then.waitFor({
        check: function () { return HashChanger.getInstance().getHash() === "odata-services/new"; },
        success: function () { Opa5.assert.ok(true, "the hash is odata-services/new"); },
        errorMessage: "New service did not navigate to odata-services/new"
    });

    Then.iStopTheApp();
});

opaTest("deleting an attached service shows which agents use it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({
        controlType: "sap.m.Button",
        viewName: "ODataServices",
        matchers: function (element: UI5Element) {
            const button = element as Button;
            return button.getIcon() === "sap-icon://delete"
                && button.getBindingContext("odata")?.getProperty("name") === "business-partners";
        },
        actions: new Press()
    });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "OK" }),
        actions: new Press()
    });

    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) { return (element as Dialog).getTitle() === "Error"; },
        success: function (dialogs: UI5Element[]) {
            const texts = (dialogs[0] as Dialog)
                .findAggregatedObjects(true, (child: ManagedObject) => child.isA("sap.m.Text"))
                .map((child: ManagedObject) => (child as unknown as Text).getText(false))
                .join(" ");
            const agents = backend.odataServices.filter((s) => s.name === "business-partners")[0].used_by;
            Opa5.assert.strictEqual(agents.length, 2, "two agents use the service");
            agents.forEach((used) => {
                Opa5.assert.ok(texts.indexOf(used.agent) !== -1, `the message names the agent ${used.agent}`);
            });
            Opa5.assert.strictEqual(backend.odataServices.length, 4, "nothing was deleted");
        },
        errorMessage: "No error dialog appeared for the service that is still attached"
    });

    Then.iStopTheApp();
});

opaTest("deleting an unused service removes it from the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({
        controlType: "sap.m.Button",
        viewName: "ODataServices",
        matchers: function (element: UI5Element) {
            const button = element as Button;
            return button.getIcon() === "sap-icon://delete"
                && button.getBindingContext("odata")?.getProperty("name") === "purchase-requisitions-v4";
        },
        actions: new Press()
    });
    When.waitFor({
        controlType: "sap.m.Button",
        searchOpenDialogs: true,
        matchers: new PropertyStrictEquals({ name: "text", value: "OK" }),
        actions: new Press()
    });

    Then.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        check: function (element: UI5Element) { return (element as unknown as Table).getItems().length === 3; },
        success: function () {
            Opa5.assert.strictEqual(backend.odataServices.length, 3, "the backend lost the service");
        },
        errorMessage: "The deleted service is still listed"
    });

    Then.iStopTheApp();
});

opaTest("Import configuration creates a service from an exported JSON file", function (Given: Common, When: Common, Then: Common) {
    interface Importer { importConfiguration(text: string): Promise<boolean> }

    // The export shape of one service (`to_export()`): the payload fields
    // and nothing else.
    const exported: ODataServiceInput = {
        name: "sales-orders", title: "Sales orders", purpose: "Read sales orders and their items",
        not_for: "", destination: "S4_ODATA_USER", user_context: true, odata_version: "v2",
        service_path: "/sap/opu/odata/sap/API_SALES_ORDER_SRV", enabled: true,
        definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null
    };
    let outcomes: boolean[] | undefined;

    Given.iStartTheApp("odata-services");

    When.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        success: function (element: UI5Element) {
            const controller = viewOf(element).getController() as unknown as Importer;
            void (async () => {
                const created = await controller.importConfiguration(JSON.stringify(exported));
                // Refused in the browser, before any call: not JSON at all,
                // and a service the rules do not accept.
                const notJson = await controller.importConfiguration("not json");
                const invalid = await controller.importConfiguration(
                    JSON.stringify({ ...exported, name: "Not A Slug", service_path: "https://example.com/x" })
                );
                outcomes = [created, notJson, invalid];
            })();
        }
    });

    Then.waitFor({
        id: "odataServicesTable",
        viewName: "ODataServices",
        // The two refusals each leave a MessageBox open, which blocks the
        // table for autoWait; the dialogs are asserted below.
        autoWait: false,
        check: function (element: UI5Element) {
            return outcomes !== undefined && (element as unknown as Table).getItems().length === 5;
        },
        success: function (element: UI5Element) {
            Opa5.assert.deepEqual(outcomes, [true, false, false], "one import worked, two were refused");
            Opa5.assert.strictEqual(backend.odataServices.length, 5, "the backend holds one more service");
            Opa5.assert.strictEqual(
                rowOf(element as Table, "sales-orders").title, "Sales orders", "the list shows it"
            );
        },
        errorMessage: "The imported service did not reach the list"
    });
    Then.waitFor({
        controlType: "sap.m.Dialog",
        searchOpenDialogs: true,
        matchers: function (element: UI5Element) { return (element as Dialog).getTitle() === "Error"; },
        success: function (dialogs: UI5Element[]) {
            Opa5.assert.strictEqual(dialogs.length, 2, "each refused file is explained");
        },
        errorMessage: "The refused files were not reported"
    });

    Then.iStopTheApp();
});
