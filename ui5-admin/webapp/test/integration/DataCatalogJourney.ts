import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import EnterText from "sap/ui/test/actions/EnterText";
import HashChanger from "sap/ui/core/routing/HashChanger";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type ProgressIndicator from "sap/m/ProgressIndicator";
import type ObjectIdentifier from "sap/m/ObjectIdentifier";
import type Input from "sap/m/Input";
import type Text from "sap/m/Text";
import type List from "sap/m/List";
import type Select from "sap/m/Select";
import type MultiComboBox from "sap/m/MultiComboBox";
import type MessageStrip from "sap/m/MessageStrip";
import type Graph from "sap/suite/ui/commons/networkgraph/Graph";
import type Node from "sap/suite/ui/commons/networkgraph/Node";
import type Dialog from "sap/m/Dialog";
import type Link from "sap/m/Link";
import type Panel from "sap/m/Panel";
import type ObjectStatus from "sap/m/ObjectStatus";
import type UI5Element from "sap/ui/core/Element";
import Common, { backend } from "./pages/Common";
import { iSeeADialog } from "./pages/Dialogs";
import { DETAIL_VIEW, GRAPH_VIEW, LIST_VIEW, choose, linksOf, rowsOf } from "./pages/DataCatalog";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("Data catalogue journey");

/** The source name of a list row (the text of its identifier). */
function nameOf(row: ColumnListItem): string {
    return (row.getCells()[1] as ObjectIdentifier).getText();
}

function rowNamed(table: UI5Element, name: string): ColumnListItem | undefined {
    return rowsOf(table).filter((row) => nameOf(row) === name)[0];
}

/** The kind of a drawn graph node, from the row it is bound to. */
function kindOf(node: Node): string {
    return (node.getBindingContext("graph")!.getObject() as { kind: string }).kind;
}

/** The graph's nodes of `kind`. */
function nodesOfKind(graph: UI5Element, kind: string): Node[] {
    return (graph as Graph).getNodes().filter((node: Node) => kindOf(node) === kind);
}

/** Picks `keys` in the bound kinds filter the way a user ends a pick: the
 *  selection, then the control's `selectionFinish`. */
function pickKinds(box: MultiComboBox, keys: string[]): void {
    box.setSelectedKeys(keys);
    box.fireSelectionFinish({ selectedItems: box.getSelectedItems() });
}

opaTest("the list shows each source with its completeness", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog");

    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 5,
        success: function (table: UI5Element) {
            Opa5.assert.deepEqual(rowsOf(table).map(nameOf),
                ["purchase-requisitions", "business-partners", "sales-orders", "agent-data", "flight-revenue"],
                "every source is listed: the catalogue services (the one never imported included), then the InA query");
            const completeness = (name: string) => rowNamed(table, name)!.getCells()[8] as ProgressIndicator;
            Opa5.assert.strictEqual(completeness("purchase-requisitions").getDisplayValue(), "72%");
            Opa5.assert.strictEqual(completeness("purchase-requisitions").getState(), "Success");
            Opa5.assert.strictEqual(completeness("business-partners").getDisplayValue(), "25%");
            Opa5.assert.strictEqual(completeness("business-partners").getState(), "Warning",
                "below half is a warning");
            Opa5.assert.strictEqual(completeness("sales-orders").getDisplayValue(), "0%");
            const lastImport = rowNamed(table, "sales-orders")!.getCells()[7] as unknown as { getText(): string };
            Opa5.assert.strictEqual(lastImport.getText(), "Not imported yet");
            Opa5.assert.ok(backend.requests.indexOf("GET data-catalog/sources") !== -1, "the list was read");
        },
        errorMessage: "The data catalogue list does not show the five sources"
    });

    Then.iStopTheApp();
});

opaTest("rebuild of the selected source sends that source", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog");

    When.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 5,
        success: function (table: UI5Element) {
            const row = rowNamed(table, "business-partners")!;
            (table as Table).setSelectedItem(row, true);
            (table as Table).fireSelectionChange({ listItem: row, listItems: [row], selected: true });
        }
    });
    When.waitFor({ id: "dcRebuildSelected", viewName: LIST_VIEW, actions: new Press() });

    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: () => backend.requests.indexOf("POST data-catalog/rebuild") !== -1,
        success: function () {
            Opa5.assert.deepEqual(backend.bodies["POST data-catalog/rebuild"],
                { source: { kind: "odata", name: "business-partners" } }, "the selected source is named");
        },
        errorMessage: "No rebuild was sent"
    });

    Then.iStopTheApp();
});

opaTest("a row opens the source with its entities, fields and relations", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog");

    When.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        matchers: (table: UI5Element) => rowNamed(table, "purchase-requisitions") ?? false,
        actions: new Press(),
        errorMessage: "No row for purchase-requisitions"
    });

    Then.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 12,
        success: function (table: UI5Element) {
            Opa5.assert.strictEqual(HashChanger.getInstance().getHash(), "data-catalog/odata/purchase-requisitions");
            const kinds = rowsOf(table).map((row) => (row.getBindingContext("src")!.getObject() as { kind: string }).kind);
            Opa5.assert.deepEqual(kinds.filter((kind) => kind === "entity").length, 3, "three entities");
            Opa5.assert.deepEqual(kinds.filter((kind) => kind === "field").length, 8, "their eight fields");
            Opa5.assert.strictEqual(kinds[kinds.length - 1], "function", "the function comes last");
        },
        errorMessage: "The entities table does not show 3 entities, 8 fields and a function"
    });
    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 3,
        success: function (table: UI5Element) {
            Opa5.assert.deepEqual(linksOf(table)[0], {
                from: "Requisition", relation: "Navigation", to: "Requisition item", origin: "Imported", remove: false
            }, "a relation with its kind's text, its two ends and its origin");
            Opa5.assert.ok(linksOf(table).every((link) => link.origin === "Imported" && !link.remove),
                "an imported link has no remove");
        },
        errorMessage: "The links are not listed"
    });
    Then.waitFor({
        id: "dcUsedBy",
        viewName: DETAIL_VIEW,
        check: (list: UI5Element) => (list as List).getItems().length === 1,
        success: function (list: UI5Element) {
            Opa5.assert.strictEqual((list as List).getItems()[0].getProperty("title"), "btp-agent");
        }
    });

    Then.iStopTheApp();
});

opaTest("an InA source shows its query, dimensions and measures and the link an admin drew", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/ina/flight-revenue");

    Then.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 4,
        success: function (table: UI5Element) {
            const rows = rowsOf(table).map((row) => row.getBindingContext("src")!.getObject() as
                { depth: number; kind: string; name: string; label: string; unit: string });
            Opa5.assert.deepEqual(rows.map((row) => `${row.depth}:${row.kind}:${row.name}`), [
                "0:query:ZFLIGHT_REV_Q001", "1:dimension:0CALMONTH", "1:dimension:ZCARRID", "1:measure:ZREVENUE"
            ], "the query, then its dimensions and its measure one level down");
            const label = rowsOf(table)[1].getCells()[1] as Input;
            Opa5.assert.strictEqual(label.getValue(), "Calendar month", "a dimension's label in the same inline input");
            Opa5.assert.strictEqual(rows[3].unit, "EUR");
        },
        errorMessage: "The InA source does not list its query with two dimensions and a measure"
    });
    Then.waitFor({
        id: "dcEntitiesPanel",
        viewName: DETAIL_VIEW,
        success: (panel: UI5Element) => Opa5.assert.strictEqual((panel as Panel).getHeaderText(),
            "Query, dimensions and measures", "the panel is named for an InA source")
    });
    Then.waitFor({
        id: "dcDetailKind",
        viewName: DETAIL_VIEW,
        success: (status: UI5Element) => Opa5.assert.strictEqual((status as ObjectStatus).getText(), "InA query",
            "the header names the kind")
    });
    Then.waitFor({
        id: "dcLinksTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 1,
        success: (table: UI5Element) => Opa5.assert.deepEqual(linksOf(table), [{
            from: "Flight revenue by month", relation: "Aggregates", to: "agent-data · Demo airlines",
            origin: "Added by an admin", remove: true
        }], "the other source's end as source · node; a drawn link offers a remove"),
        errorMessage: "The drawn link is not listed"
    });

    Then.iStopTheApp();
});

opaTest("the kind filter keeps one kind of source", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog");

    When.waitFor({
        id: "dcKindFilter",
        viewName: LIST_VIEW,
        visible: false,
        autoWait: false,
        check: () => backend.requests.indexOf("GET data-catalog/sources") !== -1,
        success: (select: UI5Element) => choose(select as Select, "ina")
    });
    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 1,
        success: function (table: UI5Element) {
            Opa5.assert.deepEqual(rowsOf(table).map(nameOf), ["flight-revenue"]);
            Opa5.assert.strictEqual((rowsOf(table)[0].getCells()[0] as ObjectStatus).getText(), "InA query",
                "the Kind column names it");
        },
        errorMessage: "The kind filter did not leave the InA query"
    });
    When.waitFor({
        id: "dcKindFilter",
        viewName: LIST_VIEW,
        visible: false,
        autoWait: false,
        success: (select: UI5Element) => choose(select as Select, "generic")
    });
    When.waitFor({ id: "dcSearch", viewName: LIST_VIEW, actions: new EnterText({ text: "partner" }) });
    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 0,
        success: () => Opa5.assert.ok(true, "the kind and the search both hold: no generic source is a partner"),
        errorMessage: "The kind filter and the search are not applied together"
    });
    When.waitFor({ id: "dcSearch", viewName: LIST_VIEW, actions: new EnterText({ text: "agent", clearTextFirst: true }) });
    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 1,
        success: function (table: UI5Element) {
            Opa5.assert.deepEqual(rowsOf(table).map(nameOf), ["agent-data"]);
            Opa5.assert.strictEqual((rowsOf(table)[0].getCells()[0] as ObjectStatus).getText(), "Generic");
        },
        errorMessage: "The generic source is not listed"
    });

    Then.iStopTheApp();
});

opaTest("a name stays on one line with its type below it, cut with a tooltip", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/odata/purchase-requisitions");

    Then.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 12,
        success: function (table: UI5Element) {
            const cellOf = (id: number) => rowsOf(table).filter((item) =>
                (item.getBindingContext("src")!.getObject() as { id: number }).id === id)[0].getCells()[0];
            const texts = (id: number) => (cellOf(id).findAggregatedObjects(true,
                (control) => control.isA("sap.m.Text")) as unknown as Text[]).filter((text) => text.getVisible());
            const type = "API_PURCHASEREQ_PROCESS_SRV.A_PurchaseRequisitionHeaderType";
            const entity = texts(10);
            Opa5.assert.deepEqual(entity.map((text) => text.getText(false)), ["A_PurchaseRequisitionHeader", type],
                "the entity's name, then its type name");
            Opa5.assert.ok(entity.every((text) => !text.getWrapping()), "neither wraps: both are cut with an ellipsis");
            Opa5.assert.strictEqual(entity[1].getTooltip(), type, "the tooltip carries the whole type name");
            Opa5.assert.ok(entity[1].getDomRef()!.getBoundingClientRect().top
                > entity[0].getDomRef()!.getBoundingClientRect().top, "the type name sits below the name");
            const field = texts(23);
            Opa5.assert.deepEqual(field.map((text) => text.getText(false)), ["RequestedQuantity (EA)"],
                "a field without a type: its name (and unit) alone");
            Opa5.assert.strictEqual(texts(11)[1].getText(false), "Edm.String", "a key field's type below its name");
        },
        errorMessage: "The name cell does not show the name on one line with its type below it"
    });

    Then.iStopTheApp();
});

opaTest("a hint edit sends only what changed; an empty label is refused at the field", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/odata/purchase-requisitions");

    When.waitFor({
        id: "dcHintPurpose",
        viewName: DETAIL_VIEW,
        check: (input: UI5Element) => !!(input as Input).getValue(),
        actions: new EnterText({ text: "Requests to buy goods", clearTextFirst: true })
    });
    When.waitFor({ id: "dcHintsSave", viewName: DETAIL_VIEW, actions: new Press() });

    // Not the Save button: after a save there is nothing to save, and
    // autoWait waits only on controls that can be used.
    Then.waitFor({
        id: "dcHintPurpose",
        viewName: DETAIL_VIEW,
        check: () => !!backend.bodies["PUT data-catalog/nodes/1/hints"],
        success: function () {
            Opa5.assert.deepEqual(backend.bodies["PUT data-catalog/nodes/1/hints"], { purpose: "Requests to buy goods" },
                "the PUT body holds the changed field only");
        },
        errorMessage: "No hints PUT was sent"
    });

    When.waitFor({
        id: "dcHintLabel",
        viewName: DETAIL_VIEW,
        success: function (input: UI5Element) {
            (input as Input).setValue("");
            (input as Input).fireLiveChange({ value: "" });
        }
    });
    When.waitFor({ id: "dcHintsSave", viewName: DETAIL_VIEW, actions: new Press() });

    Then.waitFor({
        id: "dcHintLabel",
        viewName: DETAIL_VIEW,
        check: (input: UI5Element) => (input as Input).getValueState() === "Error",
        success: function (input: UI5Element) {
            Opa5.assert.deepEqual(backend.bodies["PUT data-catalog/nodes/1/hints"], { label: "" });
            Opa5.assert.strictEqual((input as Input).getValueStateText(), "Value error, label: must not be empty",
                "the server's message is shown at the label");
        },
        errorMessage: "The 422 is not shown at the label"
    });

    Then.iStopTheApp();
});

opaTest("a test search lists the candidates for the chosen agent", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/odata/purchase-requisitions");

    When.waitFor({
        id: "dcSearchAgent",
        viewName: DETAIL_VIEW,
        // Set through the control's API: no need for it to be on screen,
        // which a narrow test window may not give.
        visible: false,
        autoWait: false,
        check: (select: UI5Element) => (select as Select).getItems().length === 3,
        success: (select: UI5Element) => choose(select as Select, "btp-agent")
    });
    When.waitFor({
        id: "dcSearchQuestion",
        viewName: DETAIL_VIEW,
        actions: new EnterText({ text: "Which supplier delivers this requisition item" })
    });
    When.waitFor({ id: "dcSearchRun", viewName: DETAIL_VIEW, actions: new Press() });

    Then.waitFor({
        id: "dcSearchResults",
        viewName: DETAIL_VIEW,
        check: (list: UI5Element) => (list as List).getItems().length > 0,
        success: function (list: UI5Element) {
            Opa5.assert.deepEqual(backend.bodies["POST data-catalog/search"],
                { question: "Which supplier delivers this requisition item", agent: "btp-agent" });
            const first = (list as List).getItems()[0].getBindingContext("search")!.getObject() as
                { title: string; score: string; relations: string[] };
            Opa5.assert.strictEqual(first.title, "Requisition item", "the best candidate comes first");
            Opa5.assert.strictEqual(first.score, "0.50");
            Opa5.assert.deepEqual(first.relations, ["association → Supplier (purchase-requisitions)"]);
        },
        errorMessage: "No candidates are listed"
    });
    Then.waitFor({
        id: "dcSearchScope",
        viewName: DETAIL_VIEW,
        success: (strip: UI5Element) => {
            Opa5.assert.strictEqual((strip as MessageStrip).getText(),
                "Limited to the sources the chosen agent attaches.");
        }
    });

    Then.iStopTheApp();
});

opaTest("the graph opens with every kind but fields and the agent filter reduces it", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/graph");

    Then.waitFor({
        id: "dcGraphKinds",
        viewName: GRAPH_VIEW,
        visible: false,
        success: function (box: UI5Element) {
            Opa5.assert.deepEqual((box as MultiComboBox).getSelectedKeys(),
                ["source", "entity", "query", "dimension", "measure", "function"], "every kind but Field is picked");
        }
    });
    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 15,
        success: function (graph: UI5Element) {
            Opa5.assert.strictEqual((graph as Graph).getNodes().length, 15,
                "4 sources, 5 entities, 2 functions and a query with its 3 members: the 12 fields are left out");
            Opa5.assert.strictEqual(nodesOfKind(graph, "field").length, 0, "no field node by default");
            Opa5.assert.strictEqual((graph as Graph).getGroups().length, 4, "one group per source");
        },
        errorMessage: "The graph does not open with the 15 nodes that are not fields"
    });

    When.waitFor({
        id: "dcGraphAgent",
        viewName: GRAPH_VIEW,
        // Set through the control's API: no need for it to be on screen,
        // which a narrow test window may not give.
        visible: false,
        autoWait: false,
        check: (select: UI5Element) => (select as Select).getItems().length === 3,
        success: (select: UI5Element) => choose(select as Select, "btp-agent")
    });

    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 5,
        success: function (graph: UI5Element) {
            Opa5.assert.strictEqual((graph as Graph).getNodes().length, 5, "only btp-agent's source is left");
            Opa5.assert.strictEqual((graph as Graph).getGroups().length, 1);
            Opa5.assert.ok(backend.requests.indexOf("GET data-catalog/graph") !== -1);
        },
        errorMessage: "The agent filter did not reduce the graph to 5 nodes"
    });

    Then.iStopTheApp();
});

opaTest("fields are drawn once the admin picks Field", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/graph");

    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 15,
        success: function (graph: UI5Element) {
            Opa5.assert.strictEqual(nodesOfKind(graph, "field").length, 0, "no field before the pick");
        },
        errorMessage: "The graph does not open without fields"
    });
    When.waitFor({
        id: "dcGraphKinds",
        viewName: GRAPH_VIEW,
        // Set through the control's API, as the agent Select above.
        visible: false,
        autoWait: false,
        check: (box: UI5Element) => (box as MultiComboBox).getItems().length === 7,
        success: (box: UI5Element) => pickKinds(box as MultiComboBox,
            ["source", "entity", "field", "query", "dimension", "measure", "function"])
    });
    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 27,
        success: function (graph: UI5Element) {
            Opa5.assert.strictEqual((graph as Graph).getNodes().length, 27, "27 nodes of four sources");
            Opa5.assert.strictEqual(nodesOfKind(graph, "field").length, 12, "the 12 fields are drawn");
        },
        errorMessage: "Picking Field did not draw the fields"
    });
    When.waitFor({
        id: "dcGraphKinds",
        viewName: GRAPH_VIEW,
        visible: false,
        autoWait: false,
        success: (box: UI5Element) => pickKinds(box as MultiComboBox, ["field"])
    });
    Then.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 12,
        success: function (graph: UI5Element) {
            Opa5.assert.strictEqual(nodesOfKind(graph, "field").length, 12, "Field alone: only fields");
        },
        errorMessage: "Field alone did not leave only the fields"
    });

    Then.iStopTheApp();
});

opaTest("a source never imported says so instead of showing empty panels", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/odata/sales-orders");

    Then.waitFor({
        id: "dcNotImported",
        viewName: DETAIL_VIEW,
        success: function () {
            Opa5.assert.ok(true, "the not-imported strip is shown");
        },
        errorMessage: "No not-imported strip"
    });
    Then.waitFor({
        id: "dcHintsPanel",
        viewName: DETAIL_VIEW,
        visible: false,
        check: (panel: UI5Element) => !(panel as unknown as { getVisible(): boolean }).getVisible(),
        success: () => Opa5.assert.ok(true, "no hints to edit before an import")
    });

    When.waitFor({ id: "dcImportNow", viewName: DETAIL_VIEW, actions: new Press() });

    Then.waitFor({
        id: "dcNotImported",
        viewName: DETAIL_VIEW,
        visible: false,
        check: () => !!backend.bodies["POST data-catalog/rebuild"],
        success: function () {
            Opa5.assert.deepEqual(backend.bodies["POST data-catalog/rebuild"],
                { source: { kind: "odata", name: "sales-orders" } }, "Import now rebuilds that source");
        }
    });

    Then.iStopTheApp();
});

opaTest("a readable switch the catalogue refuses goes back", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/odata/purchase-requisitions");

    When.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 12,
        success: function (table: UI5Element) {
            const row = rowsOf(table).filter((item) =>
                (item.getBindingContext("src")!.getObject() as { id: number }).id === 32)[0];
            const toggle = row.getCells()[3] as unknown as { setState(v: boolean): void; fireChange(p: object): void };
            toggle.setState(true);
            toggle.fireChange({ state: true });
        }
    });

    Then.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        // The refusal opens a message box over the page: wait without
        // asking for an interactable table.
        autoWait: false,
        check: function (table: UI5Element) {
            const row = rowsOf(table).filter((item) =>
                (item.getBindingContext("src")!.getObject() as { id: number }).id === 32)[0];
            return !!backend.bodies["PUT data-catalog/nodes/32/hints"]
                && (row.getBindingContext("src")!.getObject() as { readable: boolean }).readable === false;
        },
        success: function () {
            Opa5.assert.deepEqual(backend.bodies["PUT data-catalog/nodes/32/hints"], { readable: true });
            Opa5.assert.ok(true, "the refused switch is off again");
        },
        errorMessage: "The refused readable switch did not go back"
    });

    Then.iStopTheApp();
});

opaTest("the search filters the list and a used-by count opens the names", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog");

    When.waitFor({ id: "dcSearch", viewName: LIST_VIEW, actions: new EnterText({ text: "partner" }) });
    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: (table: UI5Element) => rowsOf(table).length === 1,
        success: (table: UI5Element) => Opa5.assert.deepEqual(rowsOf(table).map(nameOf), ["business-partners"]),
        errorMessage: "The search did not leave one row"
    });

    When.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        matchers: (table: UI5Element) => rowsOf(table)[0].getCells()[5] as Link,
        actions: new Press(),
        errorMessage: "No used-by link"
    });
    Then.waitFor({
        id: "dcUsedByList",
        viewName: LIST_VIEW,
        check: (list: UI5Element) => (list as List).getItems().length === 1,
        success: (list: UI5Element) => Opa5.assert.strictEqual((list as List).getItems()[0].getProperty("title"), "gmail-agent"),
        errorMessage: "The used-by popover does not name the agent"
    });

    Then.iStopTheApp();
});

opaTest("rebuild all sends no source; a rebuild that fails says so", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog");

    When.waitFor({ id: "dcRebuildAll", viewName: LIST_VIEW, actions: new Press() });
    Then.waitFor({
        id: "dcSourcesTable",
        viewName: LIST_VIEW,
        check: () => backend.countRequests("POST data-catalog/rebuild") === 1,
        success: () => Opa5.assert.deepEqual(backend.bodies["POST data-catalog/rebuild"], {}, "no source: all of them"),
        errorMessage: "No rebuild was sent"
    });
    When.waitFor({
        id: "dcRebuildAll",
        viewName: LIST_VIEW,
        success: function () {
            backend.failNext = { path: "data-catalog/rebuild", status: 503, body: { detail: "Rebuild failed" } };
        }
    });
    When.waitFor({ id: "dcRebuildAll", viewName: LIST_VIEW, actions: new Press() });
    iSeeADialog(Then, (dialog: UI5Element) => {
        Opa5.assert.strictEqual((dialog as Dialog).getType(), "Message");
    }, "the 503 of a rebuild");

    Then.iStopTheApp();
});

opaTest("a refusal in the route's string form is shown at its field", function (Given: Common, When: Common, Then: Common) {
    const detail = "purpose: Value error, must be one line of text without control characters";
    Given.iStartTheApp("data-catalog/odata/purchase-requisitions", {
        path: "data-catalog/nodes/1/hints", status: 422, body: { detail }, method: "PUT"
    });

    When.waitFor({
        id: "dcHintPurpose",
        viewName: DETAIL_VIEW,
        check: (input: UI5Element) => !!(input as Input).getValue(),
        actions: new EnterText({ text: "Anything", clearTextFirst: true })
    });
    When.waitFor({ id: "dcHintsSave", viewName: DETAIL_VIEW, actions: new Press() });

    Then.waitFor({
        id: "dcHintPurpose",
        viewName: DETAIL_VIEW,
        check: (input: UI5Element) => (input as Input).getValueState() === "Error",
        success: function (input: UI5Element) {
            Opa5.assert.strictEqual((input as Input).getValueStateText(),
                "must be one line of text without control characters");
        },
        errorMessage: "The string refusal is not shown at the purpose"
    });

    Then.iStopTheApp();
});

opaTest("a graph node opens its source", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("data-catalog/graph");

    When.waitFor({
        id: "dcGraph",
        viewName: GRAPH_VIEW,
        check: (graph: UI5Element) => (graph as Graph).getNodes().length === 15,
        success: function (graph: UI5Element) {
            const node = (graph as Graph).getNodes().filter((one: Node) => one.getTitle() === "Business partners")[0];
            node.firePress();
        },
        errorMessage: "The graph has no nodes to press"
    });
    Then.waitFor({
        id: "dcEntitiesTable",
        viewName: DETAIL_VIEW,
        success: function () {
            Opa5.assert.strictEqual(HashChanger.getInstance().getHash(), "data-catalog/odata/business-partners");
        },
        errorMessage: "The node did not open its source"
    });

    Then.iStopTheApp();
});
