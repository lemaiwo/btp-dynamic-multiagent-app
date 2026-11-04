import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import HashChanger from "sap/ui/core/routing/HashChanger";
import MessageToast from "sap/m/MessageToast";
import type List from "sap/m/List";
import type Select from "sap/m/Select";
import type MessageStrip from "sap/m/MessageStrip";
import type Dialog from "sap/m/Dialog";
import type Button from "sap/m/Button";
import type SearchField from "sap/m/SearchField";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import { iPressInDialog, iSeeADialog } from "./pages/Dialogs";
import { messageOf } from "./pages/ODataList";
import { viewOf } from "./pages/ODataDetail";
import { DETAIL_VIEW, LIST_VIEW, candidatesOf, choose, linksOf, rowsOf } from "./pages/DataCatalog";

// The links of the data catalogue detail: the Add link dialog and the
// removal of a drawn link (the Links table itself is read in
// DataCatalogJourney). The link rules are unit-tested in
// test/unit/dataCatalog.qunit.ts.
Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Data catalogue links journey");

opaTest("an admin links the query to the generic entity, then removes the link", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/ina/flight-revenue");

    When.waitFor({ id: "dcAddLink", viewName: DETAIL_VIEW, actions: new Press() });
    Then.waitFor({
        id: "dcLinkCandidates",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        check: (list: UI5Element) => (list as List).getItems().length === 7,
        success: function (list: UI5Element) {
            Opa5.assert.deepEqual(candidatesOf(list), [
                "Requisition", "Requisition item", "Supplier", "Release requisition", "A_BusinessPartner",
                "Demo airlines", "Month names"
            ], "entities and functions of the other sources: not the own query, no field, no source, no member");
        },
        errorMessage: "The dialog does not offer the seven nodes of the other sources"
    });
    When.waitFor({
        id: "dcLinkSearch",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        success: function (field: UI5Element) {
            (field as SearchField).setValue("airline");
            (field as SearchField).fireSearch({ query: "airline" });
        }
    });
    When.waitFor({
        id: "dcLinkCandidates",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        // The airlines' field and the query's Airline dimension match too: neither is offered.
        matchers: (list: UI5Element) => candidatesOf(list).join() === "Demo airlines" ? (list as List).getItems()[0] : false,
        actions: new Press(),
        errorMessage: "The search does not leave the demo airlines"
    });
    When.waitFor({ id: "dcLinkAdd", viewName: DETAIL_VIEW, searchOpenDialogs: true, actions: new Press() });

    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 2,
        success: function (table: UI5Element) {
            Opa5.assert.deepEqual(backend.bodies["POST data-catalog/edges"], { from_id: 310, to_id: 210, kind: "same_object" },
                "from the query, to the entity, by the kind picked first");
            Opa5.assert.deepEqual(linksOf(table)[1], {
                from: "Flight revenue by month", relation: "Same object", to: "agent-data · Demo airlines",
                origin: "Added by an admin", remove: true
            }, "the new link is listed at once, beside the aggregates link of the same pair");
        },
        errorMessage: "The added link is not listed"
    });

    When.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        matchers: (table: UI5Element) => rowsOf(table)[1].getCells()[4] as Button,
        actions: new Press(),
        errorMessage: "No remove on the added link"
    });
    iSeeADialog(Then, (dialog: UI5Element) => {
        Opa5.assert.strictEqual(messageOf(dialog), "Remove the link Flight revenue by month \u2013 Same object \u2013 "
            + "agent-data \u00b7 Demo airlines? An admin drew it, so no import brings it back.", "the question names the link");
    }, "the question before a link is removed");
    iPressInDialog(When, "OK");

    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 1,
        success: function (table: UI5Element) {
            Opa5.assert.ok(backend.requests.indexOf("DELETE data-catalog/edges/5") !== -1, "the drawn link was removed");
            Opa5.assert.strictEqual(linksOf(table)[0].relation, "Aggregates", "the other link stays");
        },
        errorMessage: "The removed link is still listed"
    });

    Then.iStopTheApp();
});

/** Searches the Add link dialog for `q` and picks the one candidate titled `title`. */
function iPickCandidate(When: Common, q: string, title: string): void {
    When.waitFor({
        id: "dcLinkSearch",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        success: function (field: UI5Element) {
            (field as SearchField).setValue(q);
            (field as SearchField).fireSearch({ query: q });
        }
    });
    When.waitFor({
        id: "dcLinkCandidates",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        matchers: (list: UI5Element) => candidatesOf(list).join() === title ? (list as List).getItems()[0] : false,
        actions: new Press(),
        errorMessage: `The search "${q}" does not leave ${title}`
    });
}

function iChooseTheLinkKind(When: Common, kind: string): void {
    When.waitFor({
        id: "dcLinkKind",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        success: (select: UI5Element) => choose(select as Select, kind)
    });
}

function iPressAdd(When: Common): void {
    When.waitFor({ id: "dcLinkAdd", viewName: DETAIL_VIEW, searchOpenDialogs: true, actions: new Press() });
}

/** Waits until the dialog's strip says `text`, then runs `assert`. */
function iSeeTheLinkRefused(Then: Common, text: string, what: string, assert?: () => void): void {
    Then.waitFor({
        id: "dcLinkError",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        check: (strip: UI5Element) => (strip as MessageStrip).getText() === text,
        success: function (strip: UI5Element) {
            Opa5.assert.strictEqual((strip as MessageStrip).getText(), text, what);
            Opa5.assert.strictEqual(((viewOf(strip).byId("dcLinkDialog")) as Dialog).isOpen(), true, "the dialog stays open");
            if (assert) {
                assert();
            }
        },
        errorMessage: `The dialog does not say: ${what}`
    });
}

opaTest("a refused link is said in the dialog: the pair already linked by that kind, a node gone since the search", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/ina/flight-revenue");

    When.waitFor({ id: "dcAddLink", viewName: DETAIL_VIEW, actions: new Press() });
    iPickCandidate(When, "airline", "Demo airlines");
    iChooseTheLinkKind(When, "aggregates");
    iPressAdd(When);
    iSeeTheLinkRefused(Then, "The link was not added: The two nodes are already linked",
        "the 409: an admin linked this pair by Aggregates already", function () {
            Opa5.assert.deepEqual(backend.bodies["POST data-catalog/edges"], { from_id: 310, to_id: 210, kind: "aggregates" },
                "the kind picked was sent");
            Opa5.assert.strictEqual(backend.dataCatalog.edges.length, 4, "nothing was stored");
        });

    // A re-import took the entity away since the search.
    When.waitFor({
        success: function () {
            backend.dataCatalog.nodes = backend.dataCatalog.nodes.filter((node) => node.id !== 210);
        }
    });
    iChooseTheLinkKind(When, "same_object");
    iPressAdd(When);
    iSeeTheLinkRefused(Then, "The link was not added: to_id: not a node of the catalogue",
        "the 422 names the field and the rule", function () {
            Opa5.assert.strictEqual(backend.dataCatalog.edges.length, 4, "nothing was stored");
        });

    When.waitFor({ id: "dcLinkCancel", viewName: DETAIL_VIEW, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        success: function (table: UI5Element) {
            Opa5.assert.strictEqual(((viewOf(table).byId("dcLinkDialog")) as Dialog).isOpen(), false, "Cancel closes the dialog");
            Opa5.assert.deepEqual(linksOf(table).map((link) => link.relation), ["Aggregates"], "the Links table as it was");
        }
    });

    Then.iStopTheApp();
});

opaTest("while Add is on its way the dialog cannot be cancelled, and a navigation does not lose the link", function (Given: Common, When: Common, Then: Common) {
    // Every toast of this test, in order.
    const toasts: string[] = [];
    const show = MessageToast.show;
    Given.waitFor({
        success: function () {
            MessageToast.show = function (text: string, options?: object): void {
                toasts.push(text);
                show.call(MessageToast, text, options);
            };
        }
    });
    Given.iStartTheApp("data-catalog/ina/flight-revenue");

    When.waitFor({ id: "dcAddLink", viewName: DETAIL_VIEW, actions: new Press() });
    iPickCandidate(When, "airline", "Demo airlines");
    When.waitFor({ success: function () { backend.dataCatalog.holdNextEdge = true; } });
    iPressAdd(When);
    Then.waitFor({
        id: "dcLinkCancel",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        enabled: false,
        check: (button: UI5Element) => !(button as Button).getEnabled(),
        success: function (button: UI5Element) {
            Opa5.assert.ok(true, "Cancel is off while the server is asked");
            Opa5.assert.strictEqual((viewOf(button).byId("dcLinkAdd") as Button).getEnabled(), false, "and so is Add");
            (viewOf(button).byId("dcLinkDialog") as Dialog).getDomRef()!.dispatchEvent(new KeyboardEvent("keydown", {
                key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true
            }));
        },
        errorMessage: "Cancel can be pressed while Add is on its way"
    });
    Then.waitFor({
        id: "dcLinkDialog",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        success: function (dialog: UI5Element) {
            Opa5.assert.strictEqual((dialog as Dialog).isOpen(), true, "Escape does not close it either");
            backend.dataCatalog.releaseEdges();
        },
        errorMessage: "Escape closed the dialog while Add was on its way"
    });
    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 2,
        success: function (table: UI5Element) {
            Opa5.assert.strictEqual(((viewOf(table).byId("dcLinkDialog")) as Dialog).isOpen(), false, "the answer closes the dialog");
            Opa5.assert.strictEqual(linksOf(table)[1].relation, "Same object", "and the link is listed");
            Opa5.assert.deepEqual(toasts, ["Link added"], "and said");
        },
        errorMessage: "The link added after the wait is not listed"
    });

    // Away while the server is asked: the router closes the dialog, the
    // link is stored all the same, and it is said.
    When.waitFor({ id: "dcAddLink", viewName: DETAIL_VIEW, actions: new Press() });
    iPickCandidate(When, "month names", "Month names");
    When.waitFor({ success: function () { backend.dataCatalog.holdNextEdge = true; } });
    iPressAdd(When);
    When.waitFor({
        id: "dcLinkCancel",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        enabled: false,
        check: (button: UI5Element) => !(button as Button).getEnabled(),
        success: function () { HashChanger.getInstance().setHash("data-catalog"); }
    });
    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        success: function () {
            Opa5.assert.strictEqual(document.querySelectorAll(".sapMDialogOpen").length, 0, "the dialog went with the page");
            HashChanger.getInstance().setHash("data-catalog/ina/flight-revenue");
        },
        errorMessage: "The list did not open"
    });
    // Back, and Add link again before the server has answered.
    When.waitFor({ id: "dcAddLink", viewName: DETAIL_VIEW, actions: new Press() });
    Then.waitFor({
        id: "dcLinkCancel",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        enabled: false,
        check: (button: UI5Element) => ((viewOf(button).byId("dcLinkDialog")) as Dialog).isOpen(),
        success: function (button: UI5Element) {
            Opa5.assert.strictEqual((button as Button).getEnabled(), false, "Cancel waits for the Add still on its way");
            backend.dataCatalog.releaseEdges();
        },
        errorMessage: "The dialog did not open again"
    });
    Then.waitFor({
        id: "dcLinkCancel",
        viewName: DETAIL_VIEW,
        searchOpenDialogs: true,
        check: (button: UI5Element) => toasts.length === 2 && (button as Button).getEnabled(),
        success: function (button: UI5Element) {
            MessageToast.show = show;
            Opa5.assert.deepEqual(toasts, ["Link added", "Link added"], "the second link is said too");
            Opa5.assert.ok(backend.dataCatalog.edges.some((edge) => edge.from === 310 && edge.to === 220 && edge.kind === "same_object"),
                "and stored");
            Opa5.assert.strictEqual(((viewOf(button).byId("dcLinkDialog")) as Dialog).isOpen(), true,
                "its answer does not close the dialog opened since");
            Opa5.assert.strictEqual(((viewOf(button).byId("dcLinkError")) as MessageStrip).getVisible(), false, "which says nothing of it");
        },
        errorMessage: "The link stored after the dialog had closed was not said"
    });
    When.waitFor({ id: "dcLinkCancel", viewName: DETAIL_VIEW, searchOpenDialogs: true, actions: new Press() });
    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 3,
        success: function (table: UI5Element) {
            Opa5.assert.strictEqual(((viewOf(table).byId("dcLinkDialog")) as Dialog).isOpen(), false, "Cancel closes it");
            Opa5.assert.strictEqual(toasts.length, 2, "with no third toast");
        },
        errorMessage: "The page does not list both links"
    });

    Then.iStopTheApp();
});
