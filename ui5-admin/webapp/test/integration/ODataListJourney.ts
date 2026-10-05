import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Table from "sap/m/Table";
import type MessageStrip from "sap/m/MessageStrip";
import type SideNavigation from "sap/tnt/SideNavigation";
import type NavigationList from "sap/tnt/NavigationList";
import type UI5Element from "sap/ui/core/Element";
import type { ODataServiceInput } from "../../service/types";
import Common, { backend } from "./pages/Common";
import { iPressInDialog, iSeeADialog } from "./pages/Dialogs";
import {
    TABLE, VIEW, buttonsOf, controllerOf, deleteButtonOf, itemOf, itemsOf, messageOf, namesIn, rowTexts,
    serviceOf, type RowTexts
} from "./pages/ODataList";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("OData services list journey");

const LIST = "GET odata/services";
const CREATE = "POST odata/services";

/** The export shape of one service (`to_export()`): the payload fields and
 *  nothing else. */
const EXPORTED: ODataServiceInput = {
    name: "sales-orders", title: "Sales orders", purpose: "Read sales orders and their items",
    not_for: "", destination: "S4_ODATA_USER", user_context: true, odata_version: "v2",
    service_path: "/sap/opu/odata/sap/API_SALES_ORDER_SRV", enabled: true,
    definition: { entity_sets: [], operations: [] }, metadata_fetched_at: null
};

function fileOf(text: string): File {
    return new File([text], "service.json", { type: "application/json" });
}

function hash(): string {
    return HashChanger.getInstance().getHash();
}

/** Presses the delete button in the row of `name`. */
function iPressDeleteOf(When: Common, name: string): void {
    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        matchers: function (table: UI5Element) { return deleteButtonOf(itemOf(table, name)); },
        actions: new Press(),
        errorMessage: `No delete button in the row of ${name}`
    });
}

/** Waits until the table shows exactly the rows `names`, then runs `assert`. */
function iSeeTheRows(Then: Common, names: string[], what: string, assert?: (table: UI5Element) => void): void {
    Then.waitFor({
        id: TABLE,
        viewName: VIEW,
        check: function (table: UI5Element) {
            return JSON.stringify(namesIn(table)) === JSON.stringify(names);
        },
        success: function (table: UI5Element) {
            Opa5.assert.deepEqual(namesIn(table), names, what);
            if (assert) {
                assert(table);
            }
        },
        errorMessage: `The table does not show ${names.join(", ") || "no rows"}: ${what}`
    });
}

const ALL = ["business-partners", "purchase-requisitions", "purchase-requisitions-jobs", "purchase-requisitions-v4"];

opaTest("the list shows each service with its texts, version, identity, counts and tags", function (Given: Common, When: Common, Then: Common) {
    const expected: RowTexts[] = [
        {
            title: "Business partners", name: "business-partners",
            purpose: "Look up suppliers and their addresses", tags: [],
            destination: "S4_ODATA_USER", version: "V2", runsAs: "Signed-in user", runsAsState: "Information",
            entitySets: "3", operations: "0", usedBy: "2 agents"
        },
        {
            title: "Purchase requisitions", name: "purchase-requisitions",
            purpose: "Read requisitions and their items to judge an approval", tags: [],
            destination: "S4_ODATA_USER", version: "V2", runsAs: "Signed-in user", runsAsState: "Information",
            entitySets: "5", operations: "0", usedBy: "1 agent"
        },
        {
            title: "Purchase requisitions (jobs)", name: "purchase-requisitions-jobs",
            purpose: "Nightly checks and release of requisitions", tags: ["Write"],
            destination: "S4_ODATA_TECH", version: "V2", runsAs: "Technical user", runsAsState: "None",
            entitySets: "5", operations: "1", usedBy: "1 agent"
        },
        {
            title: "Purchase requisitions (V4)", name: "purchase-requisitions-v4",
            purpose: "Same object over the V4 API", tags: ["Disabled"],
            destination: "S4_ODATA_USER", version: "V4", runsAs: "Signed-in user", runsAsState: "Information",
            entitySets: "2", operations: "3", usedBy: "not used"
        }
    ];

    Given.iStartTheApp("odata-services");

    iSeeTheRows(Then, ALL, "the four services, by name", function (table: UI5Element) {
        itemsOf(table).forEach((item, index) => {
            Opa5.assert.deepEqual(rowTexts(item), expected[index], `row ${expected[index].name} shows its texts`);
        });
    });
    Then.waitFor({
        id: "odataLoadFailed",
        viewName: VIEW,
        visible: false,
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual((strip as MessageStrip).getVisible(), false, "no load error is shown");
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

opaTest("searching filters by title, name and purpose, and says when nothing matches", function (Given: Common, When: Common, Then: Common) {
    const search = function (text: string): void {
        When.waitFor({ id: "odataSearch", viewName: VIEW, actions: new EnterText({ text }) });
    };

    Given.iStartTheApp("odata-services");

    search("partner");
    iSeeTheRows(Then, ["business-partners"], "'partner' is in one title only");

    // Only the jobs copy carries this in its technical name.
    search("tions-jobs");
    iSeeTheRows(Then, ["purchase-requisitions-jobs"], "'tions-jobs' is in one technical name only");

    // "judge" appears in one purpose and in no title or name.
    search("JUDGE");
    iSeeTheRows(Then, ["purchase-requisitions"], "'JUDGE' is in one purpose only, whatever the case");

    // The destination is not searched.
    search("S4_ODATA_TECH");
    iSeeTheRows(Then, [], "the destination is not searched", function (table: UI5Element) {
        Opa5.assert.strictEqual(
            (table as Table).getNoDataText(), "No service matches the search.",
            "an empty result of a search does not read like an empty catalogue"
        );
    });

    search("");
    iSeeTheRows(Then, ALL, "an empty search shows everything again", function (table: UI5Element) {
        Opa5.assert.strictEqual((table as Table).getNoDataText(), "No OData services yet.", "the empty text is back");
        Opa5.assert.strictEqual(backend.countRequests(LIST), 1, "searching never calls the backend");
    });

    Then.iStopTheApp();
});

opaTest("pressing a row opens the detail route with the service name", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        matchers: function (table: UI5Element) { return itemOf(table, "purchase-requisitions"); },
        actions: new Press()
    });
    Then.waitFor({
        check: function () { return hash() === "odata-services/purchase-requisitions"; },
        success: function () {
            Opa5.assert.strictEqual(hash(), "odata-services/purchase-requisitions", "the hash names the service");
        },
        errorMessage: "The row did not navigate to odata-services/purchase-requisitions"
    });

    Then.iStopTheApp();
});

opaTest("New service navigates to odata-services/new", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({ id: "addODataServiceButton", viewName: VIEW, actions: new Press() });
    Then.waitFor({
        check: function () { return hash() === "odata-services/new"; },
        success: function () { Opa5.assert.strictEqual(hash(), "odata-services/new", "the hash is odata-services/new"); },
        errorMessage: "New service did not navigate to odata-services/new"
    });

    Then.iStopTheApp();
});

opaTest("the delete button asks first, naming title and technical name, and does not open the row", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    iPressDeleteOf(When, "purchase-requisitions-v4");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog),
            "Delete the OData service \"Purchase requisitions (V4)\" (purchase-requisitions-v4)? "
            + "This cannot be undone.",
            "the question names the business title and the technical name"
        );
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Delete", "Cancel"], "the actions are Delete and Cancel");
        Opa5.assert.strictEqual(hash(), "odata-services", "the press did not also open the row");
    }, "the delete confirmation");

    iPressInDialog(When, "Cancel");
    iSeeTheRows(Then, ALL, "cancelling keeps every row", function () {
        Opa5.assert.strictEqual(
            backend.countRequests("DELETE odata/services/purchase-requisitions-v4"), 0, "nothing was sent"
        );
        Opa5.assert.strictEqual(hash(), "odata-services", "still on the list");
    });

    Then.iStopTheApp();
});

opaTest("deleting an unused service removes it from the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    iPressDeleteOf(When, "purchase-requisitions-v4");
    iPressInDialog(When, "Delete");

    iSeeTheRows(Then, ALL.slice(0, 3), "the deleted service is gone", function () {
        Opa5.assert.strictEqual(backend.odataServices.length, 3, "the backend lost the service");
        Opa5.assert.strictEqual(
            backend.countRequests("DELETE odata/services/purchase-requisitions-v4"), 1, "one delete was sent"
        );
    });

    Then.iStopTheApp();
});

opaTest("a delete the server refuses shows the server's answer and reloads the list", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    // The row is older than the truth: since the list was read, one agent
    // dropped the service and the other was renamed.
    iSeeTheRows(Then, ALL, "the list is loaded", function (table: UI5Element) {
        Opa5.assert.strictEqual(rowTexts(itemOf(table, "business-partners")).usedBy, "2 agents", "two agents then");
        const stored = backend.odataServices.filter((s) => s.name === "business-partners")[0];
        stored.used_by = [{ ...stored.used_by[0], agent: "renamed-agent" }];
    });

    iPressDeleteOf(When, "business-partners");
    iPressInDialog(When, "Delete");

    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog), "Service 'business-partners' is used by agent(s) 'renamed-agent'",
            "the message is the server's answer, not the row's old agents"
        );
        Opa5.assert.strictEqual(backend.odataServices.length, 4, "nothing was deleted");
    }, "the refusal");
    iPressInDialog(When, "Close");

    iSeeTheRows(Then, ALL, "the service is still listed", function (table: UI5Element) {
        Opa5.assert.strictEqual(backend.countRequests(LIST), 2, "the list was read again");
        Opa5.assert.strictEqual(
            rowTexts(itemOf(table, "business-partners")).usedBy, "1 agent", "and the row shows who uses it now"
        );
    });

    Then.iStopTheApp();
});

opaTest("deleting a service someone else already deleted reports it and drops the stale row", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    iSeeTheRows(Then, ALL, "the list is loaded", function () {
        backend.odataServices = backend.odataServices.filter((s) => s.name !== "purchase-requisitions-v4");
    });

    iPressDeleteOf(When, "purchase-requisitions-v4");
    iPressInDialog(When, "Delete");

    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), "Service not found", "the server's answer is shown");
    }, "the failed delete");
    iPressInDialog(When, "Close");

    iSeeTheRows(Then, ALL.slice(0, 3), "the stale row is gone", function () {
        Opa5.assert.strictEqual(backend.countRequests(LIST), 2, "the list was read again");
    });

    Then.iStopTheApp();
});

opaTest("a second delete while one is running is not sent", function (Given: Common, When: Common, Then: Common) {
    let outcomes: boolean[] | undefined;

    Given.iStartTheApp("odata-services");

    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        success: function (table: UI5Element) {
            const controller = controllerOf(table);
            const first = controller.deleteService(serviceOf(table, "purchase-requisitions-v4"));
            // Before the first answer: the same row again, and another one.
            const again = controller.deleteService(serviceOf(table, "purchase-requisitions-v4"));
            const other = controller.deleteService(serviceOf(table, "purchase-requisitions"));
            void Promise.all([first, again, other]).then((results) => { outcomes = results; });
        }
    });

    Then.waitFor({
        check: function () { return outcomes !== undefined; },
        success: function () {
            Opa5.assert.deepEqual(outcomes, [true, false, false], "only the first delete ran");
            Opa5.assert.strictEqual(
                backend.requests.filter((r) => r.indexOf("DELETE ") === 0).length, 1, "one DELETE reached the backend"
            );
        },
        errorMessage: "The deletes did not finish"
    });
    iSeeTheRows(Then, ALL.slice(0, 3), "one service is gone, the other two calls changed nothing");

    Then.iStopTheApp();
});

opaTest("Import configuration creates a service from an exported JSON file", function (Given: Common, When: Common, Then: Common) {
    let created: boolean | undefined;

    Given.iStartTheApp("odata-services");

    // importFile is what the file input's change handler calls with the
    // picked file; only the browser's own file picker is left out.
    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        success: function (table: UI5Element) {
            // A copied API answer: the read-only keys are left out, not sent.
            const file = fileOf(JSON.stringify({ ...EXPORTED, id: 7, counts: { entity_sets: 0, operations: 0 } }));
            void controllerOf(table).importFile(file).then((result) => { created = result; });
        }
    });

    iSeeTheRows(Then, ALL.concat(["sales-orders"]).sort(), "the imported service is listed", function (table: UI5Element) {
        Opa5.assert.strictEqual(created, true, "the import reports a created service");
        Opa5.assert.strictEqual(backend.countRequests(CREATE), 1, "one create was sent");
        Opa5.assert.strictEqual(backend.odataServices.length, 5, "the backend holds one more service");
        Opa5.assert.deepEqual(rowTexts(itemOf(table, "sales-orders")), {
            title: "Sales orders", name: "sales-orders", purpose: "Read sales orders and their items", tags: [],
            destination: "S4_ODATA_USER", version: "V2", runsAs: "Signed-in user", runsAsState: "Information",
            entitySets: "0", operations: "0", usedBy: "not used"
        }, "with the texts of the file");
    });

    Then.iStopTheApp();
});

opaTest("Import configuration refuses a file it cannot use, with the reason and without a backend call", function (Given: Common, When: Common, Then: Common) {
    let read = 0;
    const cases: { what: string; file: { size: number; text(): Promise<string> }; message: string }[] = [
        {
            what: "not JSON",
            file: fileOf("not json"),
            message: "The file is not valid JSON."
        },
        {
            what: "a list instead of one service",
            file: fileOf(JSON.stringify([EXPORTED])),
            message: "The file must hold the configuration of one service (a JSON object)."
        },
        {
            what: "fields the rules refuse",
            file: fileOf(JSON.stringify({
                ...EXPORTED, name: "Not A Slug", title: 5, service_path: "https://example.com/x", odata_version: "v3"
            })),
            message: "The file is not a valid service configuration. Check these fields: "
                + "name, title, odata_version, service_path"
        },
        {
            what: "an export bundle, which has none of the fields",
            file: fileOf(JSON.stringify({ odata_services: [EXPORTED] })),
            message: "The file is not a valid service configuration. Check these fields: "
                + "name, title, purpose, destination, odata_version, service_path"
        },
        {
            what: "a file above the size limit",
            file: { size: 2 * 1024 * 1024 + 1, text: () => { read++; return Promise.resolve("{}"); } },
            message: "The file is larger than 2 MB, which is more than a service configuration can be."
        },
        {
            what: "a file that cannot be read",
            file: { size: 10, text: () => Promise.reject(new Error("gone")) },
            message: "The file could not be read."
        }
    ];

    Given.iStartTheApp("odata-services");

    cases.forEach(function (entry) {
        let outcome: boolean | undefined;
        When.waitFor({
            id: TABLE,
            viewName: VIEW,
            success: function (table: UI5Element) {
                void controllerOf(table).importFile(entry.file).then((result) => { outcome = result; });
            }
        });
        iSeeADialog(Then, function (dialog: UI5Element) {
            Opa5.assert.strictEqual(messageOf(dialog), entry.message, `${entry.what}: the reason is shown`);
            Opa5.assert.strictEqual(outcome, false, `${entry.what}: nothing was created`);
            Opa5.assert.strictEqual(backend.countRequests(CREATE), 0, `${entry.what}: no create was sent`);
        }, entry.what);
        iPressInDialog(When, "Close");
    });

    iSeeTheRows(Then, ALL, "the list is unchanged", function () {
        Opa5.assert.strictEqual(read, 0, "the oversized file was not even read");
        Opa5.assert.strictEqual(backend.countRequests(LIST), 1, "and the list was not reloaded");
    });

    Then.iStopTheApp();
});

opaTest("Import configuration with a name that is taken shows the server's answer until it is closed", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services");

    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        success: function (table: UI5Element) {
            void controllerOf(table).importFile(fileOf(JSON.stringify({ ...EXPORTED, name: "business-partners" })));
        }
    });
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog), "Service name 'business-partners' already exists", "the server's answer, in a dialog"
        );
        Opa5.assert.strictEqual(backend.countRequests(CREATE), 1, "the create was sent once");
        Opa5.assert.strictEqual(backend.odataServices.length, 4, "nothing was created");
    }, "the taken name");
    iPressInDialog(When, "Close");

    // What only the server checks comes back as its own text too.
    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        success: function (table: UI5Element) {
            // The rules inside a definition are the server's alone.
            const entitySet = {
                name: "A_Item", title: "", path: "", entity_type: "", description: "", keys: [],
                operations: [], fields: [], navigations: [], examples: []
            };
            void controllerOf(table).importFile(fileOf(JSON.stringify({
                ...EXPORTED, definition: { entity_sets: [entitySet, entitySet], operations: [] }
            })));
        }
    });
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(
            messageOf(dialog), "definition: Value error, duplicate entity set 'A_Item'",
            "the server's refusal is shown as it came"
        );
    }, "the server's 422");
    iPressInDialog(When, "Close");

    Then.iStopTheApp();
});

opaTest("a list that cannot be loaded says so, offers a retry and does not look empty", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("odata-services", { path: "odata/services", status: 500, body: { detail: "database is down" } });

    Then.waitFor({
        id: "odataLoadFailed",
        viewName: VIEW,
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual(
                (strip as MessageStrip).getText(), "The OData services could not be loaded: database is down",
                "the page says the load failed, with the server's reason"
            );
        },
        errorMessage: "No load error is shown"
    });
    iSeeTheRows(Then, [], "no rows", function (table: UI5Element) {
        Opa5.assert.strictEqual(
            (table as Table).getNoDataText(), "The services could not be loaded.",
            "the table does not claim the catalogue is empty"
        );
    });

    When.waitFor({ id: "odataRetry", viewName: VIEW, actions: new Press() });

    iSeeTheRows(Then, ALL, "the retry loads the list", function () {
        Opa5.assert.strictEqual(backend.countRequests(LIST), 2, "with a second call");
    });
    Then.waitFor({
        id: "odataLoadFailed",
        viewName: VIEW,
        visible: false,
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual((strip as MessageStrip).getVisible(), false, "the error is gone");
        }
    });

    Then.iStopTheApp();
});
