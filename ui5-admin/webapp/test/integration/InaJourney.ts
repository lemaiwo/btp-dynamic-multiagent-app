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
import { iPressInDialog, iSeeADialog } from "./pages/Dialogs";
import { pressSegment } from "./pages/ODataDetail";
import { buttonsOf, messageOf } from "./pages/ODataList";
import {
    CREATE, DETAIL_VIEW, TABLE, VIEW, isOpen, itemOf, itemsOf, namesIn, rowTexts, type InaRowTexts
} from "./pages/InaQueries";
import FakeIna, { UNKNOWN_IN_SAP, VARIABLE_REQUIRED } from "./FakeIna";
import {
    BUSY_TEXT_SHOWN, ID, PAGE, detailOf, iSeeNoOpenDialog, stripText, tableRows, type DetailTexts
} from "./pages/InaQueryDetail";

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

// --- the detail page ---------------------------------------------------------

QUnit.module("Analytical query detail journey");

const FLIGHT = "ina-queries/flight-revenue";
const COST = "ina-queries/cost-centre-actuals";
const PUT_FLIGHT = "PUT ina/queries/flight-revenue";
const DOT = " · ";

function stored(name: string) {
    return backend.ina.queries.filter((q) => q.name === name)[0];
}

/** Waits until the form shows what `check` wants, then runs `assert` on it. */
function iSeeTheForm(Then: Common, what: string, check: (form: DetailTexts) => boolean, assert?: (form: DetailTexts) => void): void {
    Then.waitFor({
        id: PAGE,
        viewName: DETAIL_VIEW,
        check: function (page: UI5Element) { return check(detailOf(page)); },
        success: function (page: UI5Element) {
            Opa5.assert.ok(true, what);
            if (assert) {
                assert(detailOf(page));
            }
        },
        errorMessage: `The form does not show: ${what}`
    });
}

/** Waits until the table `id` shows `rows`. */
function iSeeTheTable(Then: Common, id: string, rows: string[][], what: string): void {
    Then.waitFor({
        id,
        viewName: DETAIL_VIEW,
        check: function (table: UI5Element) { return JSON.stringify(tableRows(table)) === JSON.stringify(rows); },
        success: function (table: UI5Element) { Opa5.assert.deepEqual(tableRows(table), rows, what); },
        errorMessage: `The table ${id} does not show: ${what}`
    });
}

/** Waits until the strip `id` shows a text for which `check` holds. */
function iSeeTheStrip(Then: Common, id: string, check: (text: string) => boolean, what: string, assert?: (text: string) => void): void {
    Then.waitFor({
        id,
        viewName: DETAIL_VIEW,
        visible: false,
        check: function (strip: UI5Element) { return check(stripText(strip)); },
        success: function (strip: UI5Element) {
            Opa5.assert.ok(true, what);
            if (assert) {
                assert(stripText(strip));
            }
        },
        errorMessage: `The strip ${id} does not show: ${what}`
    });
}

function iPress(When: Common, id: string): void {
    When.waitFor({ id, viewName: DETAIL_VIEW, actions: new Press(), errorMessage: `No ${id}` });
}

function iType(When: Common, id: string, text: string): void {
    When.waitFor({ id, viewName: DETAIL_VIEW, actions: new EnterText({ text, pressEnterKey: false }) });
}

function iChooseRunsAs(When: Common, key: string): void {
    When.waitFor({
        id: ID.runsAs,
        viewName: DETAIL_VIEW,
        actions: function (segmented: UI5Element | null) { pressSegment(segmented as UI5Element, key); }
    });
}

/** Runs `step` in the OPA queue, after what was queued before it. */
function meanwhile(Given: Common, step: () => void): void {
    Given.waitFor({ success: step });
}

opaTest("the detail shows the identity, the texts and what Describe stored", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);

    iSeeTheForm(Then, "flight-revenue is loaded", (form) => form.name === "flight-revenue", function (form) {
        Opa5.assert.deepEqual(form, {
            title: "Flight revenue", name: "flight-revenue", nameEditable: false,
            purpose: "Revenue per airline and month", notFor: "Plan values", destination: "S4_ODATA_USER",
            runsAs: "user", query: "ZFLIGHT_REV_Q001", package: "ZFLIGHT", enabled: true, usedBy: "gmail-agent",
            described: `Dimensions: 2${DOT}Measures: 1${DOT}Variables: 0`
        }, "every field as stored; the name cannot be changed");
    });
    iSeeTheTable(Then, ID.dimensions, [["0CALMONTH", "Calendar month", ""], ["ZCARRID", "Airline", "ZCARRID_KEY"]],
        "the dimensions with their key field");
    iSeeTheTable(Then, ID.measures, [["ZREVENUE", "Revenue", "EUR"]], "the measure with its unit");
    iSeeTheTable(Then, ID.variables, [], "no variables");
    Then.waitFor({
        id: PAGE,
        viewName: DETAIL_VIEW,
        success: function (page: UI5Element) {
            Opa5.assert.strictEqual((page as Page).getTitle(), "Flight revenue", "the page is titled by the query");
        }
    });

    Then.iStopTheApp();
});

opaTest("Describe reads SAP, says the counts and fills the tables", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(COST);

    iSeeTheForm(Then, "not described yet", (form) => form.name === "cost-centre-actuals" && form.described === "Not described yet");
    iSeeTheTable(Then, ID.dimensions, [], "no dimensions before Describe");
    iPress(When, ID.describe);

    iSeeTheTable(Then, ID.dimensions, [["0COSTCENTER", "Cost centre", ""], ["0CALMONTH", "Calendar month", ""]], "the dimensions SAP described");
    iSeeTheTable(Then, ID.measures, [["0AMOUNT", "Amount", "EUR"], ["0QUANTITY", "Quantity", ""]], "the measures");
    iSeeTheTable(Then, ID.variables, [["ZV_YEAR", "Fiscal year", "Yes", "Single value"]], "the variable, mandatory");
    iSeeTheForm(Then, "the counts", (form) => form.described === `Dimensions: 2${DOT}Measures: 2${DOT}Variables: 1`, function () {
        Opa5.assert.strictEqual(backend.countRequests("POST ina/queries/cost-centre-actuals/describe"), 1, "one describe");
        Opa5.assert.deepEqual(backend.bodies["POST ina/queries/cost-centre-actuals/describe"], {}, "with an empty body");
    });

    Then.iStopTheApp();
});

opaTest("a Describe SAP refuses shows the code's text and changes nothing", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");
    meanwhile(Given, () => { backend.ina.sap = {}; });
    iPress(When, ID.describe);

    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.ok(messageOf(dialog).indexOf(UNKNOWN_IN_SAP) > -1, "the fixed text of query_unknown");
    }, "the refusal");
    iPressInDialog(When, "Close");
    iSeeTheTable(Then, ID.measures, [["ZREVENUE", "Revenue", "EUR"]], "the stored description stays");

    Then.iStopTheApp();
});

opaTest("Test shows status, duration and the column names; with unsaved changes it asks for a save first", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");

    iType(When, ID.title, "Flight revenue (EUR)");
    iPress(When, ID.test);
    iSeeTheStrip(Then, ID.resultStrip, (text) => text.indexOf("Save") === 0, "a save comes first", function () {
        Opa5.assert.strictEqual(backend.countRequests("POST ina/queries/flight-revenue/test"), 0, "nothing was sent");
    });
    iPress(When, ID.describe);
    iSeeTheStrip(Then, ID.resultStrip, (text) => text.indexOf("Save") === 0, "Describe as well", function () {
        Opa5.assert.strictEqual(backend.countRequests("POST ina/queries/flight-revenue/describe"), 0, "nothing was sent");
    });

    iType(When, ID.title, "Flight revenue");
    iPress(When, ID.test);
    iSeeTheStrip(Then, ID.resultStrip, (text) => text.indexOf("ZREVENUE") > -1, "the result", function (text) {
        Opa5.assert.strictEqual(text, "Test read: HTTP 200 in 87 ms. Columns: ZREVENUE.", "outcome, status, duration, columns");
    });

    Then.iStopTheApp();
});

opaTest("a query with a mandatory variable cannot be tested: the reason is the outcome, not an error", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(COST);
    iSeeTheForm(Then, "loaded", (form) => form.name === "cost-centre-actuals");

    iPress(When, ID.test);
    Then.waitFor({
        id: ID.resultStrip,
        viewName: DETAIL_VIEW,
        check: function (strip: UI5Element) { return stripText(strip) !== ""; },
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual(stripText(strip), `Test not run: ${VARIABLE_REQUIRED}`, "the server's fixed text");
            Opa5.assert.strictEqual((strip as MessageStrip).getType(), "Information", "as information");
            Opa5.assert.strictEqual(backend.countRequests("POST ina/queries/cost-centre-actuals/test"), 1, "one test");
            iSeeNoOpenDialog();
        },
        errorMessage: "The variable_required outcome is not shown"
    });

    Then.iStopTheApp();
});

opaTest("a Describe while every call slot is taken says so as a warning, without a dialog", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");
    meanwhile(Given, () => { backend.ina.busy = true; });

    iPress(When, ID.describe);
    Then.waitFor({
        id: ID.resultStrip,
        viewName: DETAIL_VIEW,
        check: function (strip: UI5Element) { return stripText(strip) !== ""; },
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual(stripText(strip), BUSY_TEXT_SHOWN, "the fixed text");
            Opa5.assert.strictEqual((strip as MessageStrip).getType(), "Warning", "as a warning");
            iSeeNoOpenDialog();
        },
        errorMessage: "The busy Describe is not said"
    });
    iSeeTheTable(Then, ID.measures, [["ZREVENUE", "Revenue", "EUR"]], "the stored description stays");

    Then.iStopTheApp();
});

opaTest("a Test while every call slot is taken says so as a warning, not as an error", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");
    meanwhile(Given, () => { backend.ina.busy = true; });

    iPress(When, ID.test);
    Then.waitFor({
        id: ID.resultStrip,
        viewName: DETAIL_VIEW,
        check: function (strip: UI5Element) { return stripText(strip) !== ""; },
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual(stripText(strip), BUSY_TEXT_SHOWN, "the fixed text, not the server's");
            Opa5.assert.strictEqual((strip as MessageStrip).getType(), "Warning", "as a warning");
            iSeeNoOpenDialog();
        },
        errorMessage: "The busy Test is not said"
    });

    Then.iStopTheApp();
});

opaTest("leaving with unsaved changes asks; Cancel stays with the input, Discard goes", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");

    iType(When, ID.title, "Flight revenue (EUR)");
    When.waitFor({ id: `${PAGE}-navButton`, viewName: DETAIL_VIEW, actions: new Press() });
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), "You have unsaved changes to this query. Discard them?");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Discard", "Cancel"]);
    }, "the question");
    iPressInDialog(When, "Cancel");
    iSeeTheForm(Then, "Cancel keeps the page and the input", (form) => form.title === "Flight revenue (EUR)", function () {
        Opa5.assert.strictEqual(hash(), FLIGHT, "still on the query");
    });

    When.waitFor({ id: `${PAGE}-navButton`, viewName: DETAIL_VIEW, actions: new Press() });
    iPressInDialog(When, "Discard");
    iSeeTheRows(Then, ALL, "Discard goes back to the list", function () {
        Opa5.assert.strictEqual(hash(), "ina-queries", "the list route");
        Opa5.assert.strictEqual(backend.countRequests(PUT_FLIGHT), 0, "nothing was saved");
    });

    Then.iStopTheApp();
});

opaTest("a Describe over a query saved meanwhile stores nothing, says so and reloads the query", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");
    meanwhile(Given, () => { backend.ina.describeMoves = true; });

    iPress(When, ID.describe);
    iSeeTheStrip(Then, ID.resultStrip, (text) => text !== "", "the moved text", function (text) {
        Opa5.assert.strictEqual(text, "The query was changed while SAP was asked, so nothing was stored. "
            + "The page shows the current version now; press Describe again.");
        Opa5.assert.strictEqual(backend.countRequests("GET ina/queries/flight-revenue"), 2, "the query was read again");
    });
    // The form carries the stamp the server has now: a save goes through.
    iType(When, ID.title, "Flight revenue (EUR)");
    iPress(When, ID.save);
    Then.waitFor({
        id: PAGE,
        viewName: DETAIL_VIEW,
        check: function () { return stored("flight-revenue").title === "Flight revenue (EUR)"; },
        success: function () { Opa5.assert.ok(true, "saved over the version the reload showed"); },
        errorMessage: "The save after the reload did not go through"
    });

    Then.iStopTheApp();
});

opaTest("a query changed elsewhere is not overwritten, and a stale PUT shows the moved text", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");

    iType(When, ID.title, "Flight revenue (EUR)");
    meanwhile(Given, () => { stored("flight-revenue").updated_at = "2026-11-02T09:00:00+00:00"; });
    iPress(When, ID.save);
    iSeeTheStrip(Then, ID.changedElsewhere, (text) => text !== "", "the query moved since it was loaded", function (text) {
        Opa5.assert.strictEqual(text,
            "This query was changed elsewhere. Reload to see the current version; your changes are kept until you reload.");
        Opa5.assert.strictEqual(backend.countRequests(PUT_FLIGHT), 0, "nothing was sent over it");
    });
    iSeeTheForm(Then, "the input stays", (form) => form.title === "Flight revenue (EUR)");

    When.waitFor({ id: ID.reload, viewName: DETAIL_VIEW, actions: new Press() });
    iSeeTheStrip(Then, ID.changedElsewhere, (text) => text === "", "the strip goes with the reload");
    iSeeTheForm(Then, "the stored version is back", (form) => form.title === "Flight revenue");

    iType(When, ID.title, "Flight revenue (EUR)");
    meanwhile(Given, () => {
        backend.failNext = {
            path: "ina/queries/flight-revenue", method: "PUT", status: 409,
            body: { detail: FakeIna.changedElsewhere("flight-revenue") }
        };
    });
    iPress(When, ID.save);
    iSeeTheStrip(Then, ID.saveError, (text) => text !== "", "the server's refusal", function (text) {
        Opa5.assert.strictEqual(text, "The query was not saved. The server answered: "
            + "Query 'flight-revenue' was changed since it was loaded; reload it and save again");
        Opa5.assert.strictEqual(backend.countRequests(PUT_FLIGHT), 1, "one PUT was sent");
        Opa5.assert.strictEqual(backend.bodies[PUT_FLIGHT]?.expected_updated_at, "2026-11-02T09:00:00+00:00",
            "with the version the form was loaded from");
    });
    iSeeTheStrip(Then, ID.changedElsewhere, (text) => text !== "", "and the moved strip");

    Then.iStopTheApp();
});

opaTest("changing destination and Runs as of a query in use asks first, naming the agents", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");

    iType(When, ID.destination, "S4_ODATA_TECH");
    iChooseRunsAs(When, "technical");
    iPress(When, ID.save);
    iSeeADialog(Then, function (dialog: UI5Element) {
        const text = messageOf(dialog);
        Opa5.assert.ok(text.indexOf("The agent gmail-agent uses this query.") > -1, "the agent is named");
        Opa5.assert.ok(text.indexOf("Runs as changes from \"Signed-in user\" to \"Technical user\".") > -1, "the identity change");
        Opa5.assert.ok(text.indexOf("The destination changes from S4_ODATA_USER to S4_ODATA_TECH.") > -1, "the destination change");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Save", "Cancel"], "Save or Cancel");
        Opa5.assert.strictEqual(backend.countRequests(PUT_FLIGHT), 0, "nothing sent before the answer");
    }, "the identity question");
    iPressInDialog(When, "Cancel");
    iSeeTheForm(Then, "Cancel keeps the input", (form) => form.destination === "S4_ODATA_TECH", function () {
        Opa5.assert.strictEqual(backend.countRequests(PUT_FLIGHT), 0, "and sends nothing");
    });

    iPress(When, ID.save);
    iPressInDialog(When, "Save");
    Then.waitFor({
        id: PAGE,
        viewName: DETAIL_VIEW,
        check: function () { return backend.countRequests(PUT_FLIGHT) === 1 && stored("flight-revenue").user_context === false; },
        success: function () {
            const body = backend.bodies[PUT_FLIGHT] as Record<string, unknown>;
            Opa5.assert.strictEqual(body.destination, "S4_ODATA_TECH", "the new destination is saved");
            Opa5.assert.strictEqual(body.user_context, false, "as the technical user");
            Opa5.assert.notOk("definition" in body, "the description is not sent");
        },
        errorMessage: "The confirmed save was not sent"
    });

    Then.iStopTheApp();
});

opaTest("the same change of a query nobody uses is saved without a question", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(COST);
    iSeeTheForm(Then, "loaded", (form) => form.name === "cost-centre-actuals");

    iType(When, ID.destination, "S4_ODATA_USER");
    iChooseRunsAs(When, "user");
    iPress(When, ID.save);
    Then.waitFor({
        id: PAGE,
        viewName: DETAIL_VIEW,
        check: function () { return stored("cost-centre-actuals").destination === "S4_ODATA_USER"; },
        success: function () {
            Opa5.assert.strictEqual(stored("cost-centre-actuals").user_context, true, "saved as the signed-in user");
        },
        errorMessage: "The save was not sent"
    });

    Then.iStopTheApp();
});

opaTest("a save the running agents did not take up says so in a box that stays", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");
    meanwhile(Given, () => { backend.reloadOutcome = "failed"; });

    iType(When, ID.title, "Flight revenue (EUR)");
    iPress(When, ID.save);
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.ok(messageOf(dialog).indexOf("The query was saved, but the running agents could not be reloaded") === 0,
            "the warning names what was stored");
    }, "the not-live warning");
    iPressInDialog(When, "OK");

    Then.iStopTheApp();
});

opaTest("Delete asks; a query in use is refused with the server's text, an unused one is gone", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp(FLIGHT);
    iSeeTheForm(Then, "loaded", (form) => form.name === "flight-revenue");

    iPress(When, ID.remove);
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog),
            "Delete the analytical query \"Flight revenue\" (flight-revenue)? This cannot be undone.", "title and name");
        Opa5.assert.deepEqual(buttonsOf(dialog), ["Delete", "Cancel"]);
    }, "the question");
    iPressInDialog(When, "Delete");
    iSeeADialog(Then, function (dialog: UI5Element) {
        Opa5.assert.strictEqual(messageOf(dialog), "Query 'flight-revenue' is used by agent(s) 'gmail-agent'", "the 409 text");
    }, "the refusal");
    iPressInDialog(When, "Close");
    iSeeTheForm(Then, "the page stays", (form) => form.name === "flight-revenue", function () {
        Opa5.assert.strictEqual(hash(), FLIGHT, "still on the query");
    });
    Then.iStopTheApp();

    Given.iStartTheApp(COST);
    iSeeTheForm(Then, "loaded", (form) => form.name === "cost-centre-actuals");
    iPress(When, ID.remove);
    iPressInDialog(When, "Delete");
    iSeeTheRows(Then, ["flight-revenue"], "back on the list, without it", function () {
        Opa5.assert.strictEqual(hash(), "ina-queries", "the list route");
        Opa5.assert.strictEqual(backend.ina.queries.length, 1, "deleted");
    });

    Then.iStopTheApp();
});
