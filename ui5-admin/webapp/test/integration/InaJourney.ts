import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Input from "sap/m/Input";
import type Page from "sap/m/Page";
import type Table from "sap/m/Table";
import type MessageStrip from "sap/m/MessageStrip";
import type SegmentedButton from "sap/m/SegmentedButton";
import type SideNavigation from "sap/tnt/SideNavigation";
import type NavigationList from "sap/tnt/NavigationList";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import { pressSegment } from "./pages/ODataDetail";
import {
    CREATE, DETAIL_VIEW, TABLE, VIEW, isOpen, itemOf, itemsOf, namesIn, rowTexts, type InaRowTexts
} from "./pages/InaQueries";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Analytical queries list journey");

const LIST = "GET ina/queries";
const POST = "POST ina/queries";
const ALL = ["cost-centre-actuals", "flight-revenue"];

/** The fixed texts of the page and of the server, as the admin reads them. */
const NAME_INVALID = "Use lower-case letters, digits and hyphens (at most 64 characters), "
    + "starting and ending with a letter or digit.";
const NAME_TAKEN = "name: already exists";

function hash(): string {
    return HashChanger.getInstance().getHash();
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

function iOpenTheCreateDialog(When: Common, Then: Common): void {
    When.waitFor({ id: "inaCreateButton", viewName: VIEW, actions: new Press() });
    Then.waitFor({
        id: CREATE.dialog,
        viewName: VIEW,
        searchOpenDialogs: true,
        check: isOpen,
        success: function () { Opa5.assert.ok(true, "the Create dialog is open"); },
        errorMessage: "The Create dialog did not open"
    });
}

/** Types `text` into the dialog's field `id` (no Enter: Enter creates). */
function iTypeIn(When: Common, id: string, text: string): void {
    When.waitFor({
        id,
        viewName: VIEW,
        searchOpenDialogs: true,
        actions: new EnterText({ text, pressEnterKey: false }),
        errorMessage: `No field ${id} in the Create dialog`
    });
}

interface CreateFields {
    name: string;
    title: string;
    destination: string;
    query: string;
    package: string;
    purpose: string;
}

function iFillTheDialog(When: Common, fields: CreateFields): void {
    iTypeIn(When, CREATE.name, fields.name);
    iTypeIn(When, CREATE.title, fields.title);
    iTypeIn(When, CREATE.destination, fields.destination);
    iTypeIn(When, CREATE.query, fields.query);
    if (fields.package) {
        iTypeIn(When, CREATE.package, fields.package);
    }
    iTypeIn(When, CREATE.purpose, fields.purpose);
}

function iPressCreate(When: Common): void {
    When.waitFor({ id: CREATE.confirm, viewName: VIEW, searchOpenDialogs: true, actions: new Press() });
}

/** Waits until the name field of the dialog says `text`. */
function iSeeTheNameRefused(Then: Common, text: string, what: string): void {
    Then.waitFor({
        id: CREATE.name,
        viewName: VIEW,
        searchOpenDialogs: true,
        check: function (field: UI5Element) { return (field as Input).getValueState() === "Error"; },
        success: function (field: UI5Element) {
            Opa5.assert.strictEqual((field as Input).getValueStateText(), text, what);
        },
        errorMessage: `The name field does not show an error: ${what}`
    });
}

const VALID: CreateFields = {
    name: "cost-centre-plan", title: "Cost centre plan", destination: "S4_ODATA_USER",
    query: "0CCA_C11_Q0002", package: "ZCCA", purpose: "Planned costs per cost centre and month"
};

opaTest("the list shows each query with its InA name, destination, identity, users, description and switch", function (Given: Common, When: Common, Then: Common) {
    // The fake stamps the described query with its first record time.
    const describedOn = new Date("2026-11-01T08:00:01+00:00").toLocaleDateString();
    const expected: InaRowTexts[] = [
        {
            title: "Cost centre actuals", name: "cost-centre-actuals", query: "0CCA_C11_Q0001", package: "",
            destination: "S4_ODATA_TECH", runsAs: "Technical user", runsAsState: "None",
            usedBy: "not used", usedByTooltip: "", described: "Not yet", enabled: "No"
        },
        {
            title: "Flight revenue", name: "flight-revenue", query: "ZFLIGHT_REV_Q001", package: "ZFLIGHT",
            destination: "S4_ODATA_USER", runsAs: "Signed-in user", runsAsState: "Information",
            usedBy: "1 agent", usedByTooltip: "gmail-agent", described: describedOn, enabled: "Yes"
        }
    ];

    Given.iStartTheApp("ina-queries");

    Then.waitFor({
        id: "inaQueriesPage",
        viewName: VIEW,
        success: function (page: UI5Element) {
            Opa5.assert.strictEqual((page as Page).getTitle(), "Analytical queries", "the page is titled");
        }
    });
    iSeeTheRows(Then, ALL, "the fake's two queries, by name", function (table: UI5Element) {
        itemsOf(table).forEach((item, index) => {
            Opa5.assert.deepEqual(rowTexts(item), expected[index], `row ${expected[index].name} shows its texts`);
        });
    });
    Then.waitFor({
        id: "inaLoadFailed",
        viewName: VIEW,
        visible: false,
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual((strip as MessageStrip).getVisible(), false, "no load error is shown");
        }
    });

    Then.iStopTheApp();
});

opaTest("a list that cannot be read says so, with a retry, instead of looking empty", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("ina-queries", { path: "ina/queries", method: "GET", status: 500, body: { detail: "database is down" } });

    Then.waitFor({
        id: "inaLoadFailed",
        viewName: VIEW,
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual(
                (strip as MessageStrip).getText(), "The analytical queries could not be loaded: database is down", "the reason is shown"
            );
        }
    });
    iSeeTheRows(Then, [], "nothing is listed", function (table: UI5Element) {
        Opa5.assert.strictEqual((table as Table).getNoDataText(), "The queries could not be loaded.", "the empty text says why");
    });
    When.waitFor({ id: "inaRetry", viewName: VIEW, actions: new Press() });
    iSeeTheRows(Then, ALL, "the retry reads the list again");

    Then.iStopTheApp();
});

opaTest("the side navigation item sits between OData services and Data catalogue and is selected on both routes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("ina-queries");

    Then.waitFor({
        id: "sideNavigation",
        viewName: "App",
        success: function (element: UI5Element) {
            const navigation = element as SideNavigation;
            const keys = (navigation.getItem() as NavigationList).getItems().map((item) => item.getKey());
            const index = keys.indexOf("inaQueries");
            Opa5.assert.strictEqual(keys[index - 1], "odataServices", "it follows OData services");
            Opa5.assert.strictEqual(keys[index + 1], "dataCatalog", "and precedes the data catalogue");
            Opa5.assert.strictEqual(navigation.getSelectedKey(), "inaQueries", "it is the selected item");
        }
    });

    When.waitFor({
        id: TABLE,
        viewName: VIEW,
        matchers: function (table: UI5Element) { return itemOf(table, "flight-revenue"); },
        actions: new Press()
    });
    Then.waitFor({
        id: "inaQueryDetailPage",
        viewName: DETAIL_VIEW,
        success: function () {
            Opa5.assert.strictEqual(hash(), "ina-queries/flight-revenue", "the row opened the detail route with its name");
        },
        errorMessage: "The row did not open the detail page"
    });
    Then.waitFor({
        id: "sideNavigation",
        viewName: "App",
        check: function (element: UI5Element) { return (element as SideNavigation).getSelectedKey() === "inaQueries"; },
        success: function () { Opa5.assert.ok(true, "the item stays selected on the detail route"); }
    });

    Then.iStopTheApp();
});

opaTest("searching filters by title, name and InA name, and says when nothing matches", function (Given: Common, When: Common, Then: Common) {
    const search = function (text: string): void {
        When.waitFor({ id: "inaSearch", viewName: VIEW, actions: new EnterText({ text }) });
    };

    Given.iStartTheApp("ina-queries");

    search("REVENUE");
    iSeeTheRows(Then, ["flight-revenue"], "'REVENUE' is in one title, whatever the case");
    search("centre-act");
    iSeeTheRows(Then, ["cost-centre-actuals"], "'centre-act' is in one technical name only");
    search("0CCA_C11");
    iSeeTheRows(Then, ["cost-centre-actuals"], "'0CCA_C11' is in one InA name only");
    search("S4_ODATA_TECH");
    iSeeTheRows(Then, [], "the destination is not searched", function (table: UI5Element) {
        Opa5.assert.strictEqual(
            (table as Table).getNoDataText(), "No query matches the search.",
            "an empty result of a search does not read like an empty list"
        );
    });
    search("");
    iSeeTheRows(Then, ALL, "an empty search shows everything again", function (table: UI5Element) {
        Opa5.assert.strictEqual((table as Table).getNoDataText(), "No analytical queries yet.", "the empty text is back");
        Opa5.assert.strictEqual(backend.countRequests(LIST), 1, "searching never calls the backend");
    });

    Then.iStopTheApp();
});

opaTest("Create opens an empty dialog on the technical user with the destinations to pick from; Cancel sends nothing", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("ina-queries");
    iSeeTheRows(Then, ALL, "the list is there");
    iOpenTheCreateDialog(When, Then);

    Then.waitFor({
        id: CREATE.runsAs,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function (segmented: UI5Element) {
            Opa5.assert.strictEqual((segmented as SegmentedButton).getSelectedKey(), "technical", "a new query runs as the technical user");
        }
    });
    Then.waitFor({
        id: CREATE.destination,
        viewName: VIEW,
        searchOpenDialogs: true,
        check: function (field: UI5Element) { return (field as Input).getSuggestionItems().length > 0; },
        success: function (field: UI5Element) {
            const input = field as Input;
            Opa5.assert.strictEqual(input.getValue(), "", "the field is empty");
            Opa5.assert.deepEqual(
                input.getSuggestionItems().map((item) => item.getText()),
                ["S4_DEV", "S4_DEV_BASIC", "S4_DEV_USER", "S4_ODATA_TECH", "S4_ODATA_USER"],
                "the usable destinations of the shared list are offered"
            );
        },
        errorMessage: "The destination field offers no destinations"
    });
    [CREATE.name, CREATE.title, CREATE.query, CREATE.package, CREATE.purpose].forEach((id) => {
        Then.waitFor({
            id,
            viewName: VIEW,
            searchOpenDialogs: true,
            success: function (field: UI5Element) {
                Opa5.assert.strictEqual((field as Input).getValue(), "", `${id} is empty`);
            }
        });
    });

    iTypeIn(When, CREATE.name, "abandoned");
    When.waitFor({ id: CREATE.cancel, viewName: VIEW, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: TABLE,
        viewName: VIEW,
        check: function () { return backend.countRequests(POST) === 0 && hash() === "ina-queries"; },
        success: function () { Opa5.assert.ok(true, "nothing was sent and the list stays"); }
    });
    // Opened again, the dialog starts over.
    iOpenTheCreateDialog(When, Then);
    Then.waitFor({
        id: CREATE.name,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function (field: UI5Element) {
            Opa5.assert.strictEqual((field as Input).getValue(), "", "what was typed before is gone");
        }
    });

    Then.iStopTheApp();
});

opaTest("Create with a name the server would refuse shows the fixed text and sends nothing", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("ina-queries");
    iSeeTheRows(Then, ALL, "the list is there");
    iOpenTheCreateDialog(When, Then);
    iFillTheDialog(When, { ...VALID, name: "Cost Centre Plan" });
    iPressCreate(When);

    iSeeTheNameRefused(Then, NAME_INVALID, "the rule, not the typed name");
    Then.waitFor({
        id: CREATE.dialog,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function (dialog: UI5Element) {
            Opa5.assert.ok(isOpen(dialog), "the dialog stays open");
            Opa5.assert.strictEqual(backend.countRequests(POST), 0, "nothing was sent");
        }
    });

    // Typing in the field takes the error away.
    iTypeIn(When, CREATE.name, "cost-centre-plan");
    Then.waitFor({
        id: CREATE.name,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function (field: UI5Element) {
            Opa5.assert.strictEqual((field as Input).getValueState(), "None", "the error is gone");
        }
    });

    Then.iStopTheApp();
});

opaTest("Create with a name that is taken shows the server's text on the name and stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("ina-queries");
    iSeeTheRows(Then, ALL, "the list is there");
    iOpenTheCreateDialog(When, Then);
    iFillTheDialog(When, { ...VALID, name: "flight-revenue" });
    iPressCreate(When);

    iSeeTheNameRefused(Then, NAME_TAKEN, "the server's fixed text");
    Then.waitFor({
        id: CREATE.dialog,
        viewName: VIEW,
        searchOpenDialogs: true,
        success: function (dialog: UI5Element) {
            Opa5.assert.ok(isOpen(dialog), "the dialog stays open");
            Opa5.assert.strictEqual(backend.countRequests(POST), 1, "one create was sent");
            Opa5.assert.strictEqual(hash(), "ina-queries", "and the page did not move");
            Opa5.assert.strictEqual(backend.ina.queries.length, 2, "nothing was added");
        }
    });

    Then.iStopTheApp();
});

opaTest("a valid Create sends the form as typed and lands on the new query's detail", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("ina-queries");
    iSeeTheRows(Then, ALL, "the list is there");
    iOpenTheCreateDialog(When, Then);
    iFillTheDialog(When, VALID);
    When.waitFor({
        id: CREATE.runsAs,
        viewName: VIEW,
        searchOpenDialogs: true,
        actions: function (segmented: UI5Element | null) { pressSegment(segmented as UI5Element, "user"); }
    });
    iPressCreate(When);

    Then.waitFor({
        id: "inaQueryDetailPage",
        viewName: DETAIL_VIEW,
        check: function () { return hash() === "ina-queries/cost-centre-plan"; },
        success: function () {
            Opa5.assert.strictEqual(hash(), "ina-queries/cost-centre-plan", "the detail route names the new query");
            Opa5.assert.deepEqual(backend.bodies[POST], {
                name: "cost-centre-plan", title: "Cost centre plan", purpose: "Planned costs per cost centre and month",
                not_for: "", destination: "S4_ODATA_USER", user_context: true, query: "0CCA_C11_Q0002",
                package: "ZCCA", enabled: true
            }, "exactly the payload fields, as typed, without a description");
            Opa5.assert.ok(backend.ina.queries.some((q) => q.name === "cost-centre-plan"), "the query is stored");
        },
        errorMessage: "The valid Create did not land on the detail page"
    });
    Then.waitFor({
        id: CREATE.dialog,
        viewName: VIEW,
        searchOpenDialogs: false,
        visible: false,
        success: function (dialog: UI5Element) {
            Opa5.assert.notOk(isOpen(dialog), "the dialog is closed");
        }
    });

    Then.iStopTheApp();
});
