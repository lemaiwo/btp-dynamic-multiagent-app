import ResourceBundle from "sap/base/i18n/ResourceBundle";
import dataCatalog, {
    DEFAULT_GRAPH_KINDS, GRAPH_NODE_CAP, NODE_KINDS, candidateRows, cleanList,
    completenessPercent, completenessState, completenessText, entityRows, fitZoom, graphModel, graphQuery, hasChanges,
    hintErrors, hintsOf, hintsUpdate, nodeIcon, scoreText, statusKey
} from "com/agent/admin/model/dataCatalog";
import dataCatalogLinks, {
    LINK_KINDS, LINK_NODE_KINDS, linkCandidates, linkQuery, linkStarts, relationKindKey, relationRows, sameSource
} from "com/agent/admin/model/dataCatalogLinks";
import type {
    DataGraph, DataGraphNode, DataNode, DataNodeKind, DataSearchCandidate, DataSourceDetail, DataSourceKind
} from "com/agent/admin/service/dataCatalogTypes";

function node(id: number, kind: DataNodeKind, name: string, extra: Partial<DataNode> = {}): DataNode {
    return {
        id, kind, source_kind: "odata", source_name: "purchase-requisitions", name, label: "", purpose: "",
        not_for: "", synonyms: [], examples: [], unit: "", readable: true, writable: false, parent_id: null,
        updated_at: null, curated_at: null, ...extra
    };
}

const DETAIL: DataSourceDetail = {
    source: node(1, "source", "purchase-requisitions", { label: "Purchase requisitions" }),
    entities: [
        {
            node: node(10, "entity", "A_Header", { label: "Requisition" }),
            fields: [node(11, "field", "PurchaseRequisition", { label: "Number", parent_id: 10 })]
        },
        {
            node: node(20, "entity", "A_Item"),
            fields: [
                node(21, "field", "Quantity", { unit: "EA", writable: true, parent_id: 20 }),
                node(22, "field", "Supplier", { readable: false, parent_id: 20 })
            ]
        }
    ],
    edges: [
        { id: 1, from: 10, to: 20, kind: "navigation", label: "to_Item", curated: false },
        { id: 2, from: 22, to: 999, kind: "foreign_key", label: "", curated: false }
    ],
    used_by: ["btp-agent"]
};

QUnit.module("dataCatalog: hint completeness");

QUnit.test("the share becomes a percentage, text and state", function (assert) {
    assert.strictEqual(completenessPercent(0.724), 72);
    assert.strictEqual(completenessText(0.724), "72%");
    assert.strictEqual(completenessState(0.72), "Success");
    assert.strictEqual(completenessState(0.5), "Success", "half is enough");
    assert.strictEqual(completenessState(0.49), "Warning", "below half is a warning");
});

QUnit.test("a value out of range or not a number is clamped to 0..100", function (assert) {
    assert.strictEqual(completenessPercent(1.7), 100);
    assert.strictEqual(completenessPercent(-0.2), 0);
    assert.strictEqual(completenessPercent(undefined), 0);
    assert.strictEqual(completenessPercent(NaN), 0);
    assert.strictEqual(completenessState(null), "Warning");
});

QUnit.module("dataCatalog: the detail page");

QUnit.test("entities come first, each followed by its fields", function (assert) {
    const rows = entityRows(DETAIL);
    assert.deepEqual(rows.map((row) => `${row.depth}:${row.name}`),
        ["0:A_Header", "1:PurchaseRequisition", "0:A_Item", "1:Quantity", "1:Supplier"]);
    assert.strictEqual(rows[3].entity, "A_Item", "a field knows its entity");
    assert.strictEqual(rows[3].unit, "EA");
    assert.strictEqual(rows[3].writable, true);
    assert.strictEqual(rows[4].readable, false);
    assert.strictEqual(rows[1].savedLabel, "Number", "the stored label is kept beside the edited one");
    assert.strictEqual(rows[0].labelState, "None");
});

QUnit.test("relations name both ends; an end outside the answer keeps its id", function (assert) {
    assert.deepEqual(relationRows(DETAIL), [
        { id: 1, kind: "navigation", label: "to_Item", from: "Requisition", to: "A_Item", curated: false, origin: "dcLinkOriginImported" },
        { id: 2, kind: "foreign_key", label: "", from: "A_Item.Supplier", to: "999", curated: false, origin: "dcLinkOriginImported" }
    ]);
});

QUnit.test("functions follow the entities; key and type are carried", function (assert) {
    const rows = entityRows({
        entities: [{ node: node(10, "entity", "A"), fields: [node(11, "field", "Id", { is_key: true, type_name: "Edm.String" })] }],
        functions: [node(50, "function", "ReleaseItem", { label: "Release item" })]
    });
    assert.deepEqual(rows.map((row) => `${row.depth}:${row.kind}:${row.name}`), ["0:entity:A", "1:field:Id", "0:function:ReleaseItem"]);
    assert.strictEqual(rows[1].isKey, true);
    assert.strictEqual(rows[1].typeName, "Edm.String");
    assert.strictEqual(rows[0].isKey, false, "absent is no key");
});

QUnit.test("an end in another source is named from the node the edge carries", function (assert) {
    const other = node(900, "entity", "A_Supplier", { label: "Supplier", source_name: "business-partners" });
    const rows = relationRows({
        source: DETAIL.source, entities: DETAIL.entities, functions: [],
        edges: [{ id: 3, from: 22, to: 900, kind: "same_object", label: "", curated: false, from_node: null, to_node: other }]
    });
    assert.deepEqual(rows, [{
        id: 3, kind: "same_object", label: "", from: "A_Item.Supplier", to: "business-partners · Supplier",
        curated: false, origin: "dcLinkOriginImported"
    }], "source · node, as the Links table's To column shows it");
    assert.deepEqual(relationRows({ source: null, entities: [], edges: [] }), [], "a source never imported has none");
});

QUnit.module("dataCatalog: an InA source and the links");

function ina(id: number, kind: DataNodeKind, name: string, extra: Partial<DataNode> = {}): DataNode {
    return node(id, kind, name, { source_kind: "ina", source_name: "flight-revenue", ...extra });
}

const INA_DETAIL: DataSourceDetail = {
    source: ina(300, "source", "flight-revenue", { label: "Flight revenue" }),
    query: {
        node: ina(310, "query", "ZFLIGHT_REV_Q001", { label: "Flight revenue by month", parent_id: 300 }),
        dimensions: [
            ina(311, "dimension", "0CALMONTH", { label: "Calendar month", parent_id: 310 }),
            ina(312, "dimension", "ZCARRID", { parent_id: 310 })
        ],
        measures: [ina(313, "measure", "ZREVENUE", { label: "Revenue", unit: "EUR", parent_id: 310 })]
    },
    edges: [
        {
            id: 4, from: 310, to: 210, kind: "aggregates", label: "", curated: true,
            from_node: { id: 310, kind: "query", source_kind: "ina", source_name: "flight-revenue", name: "ZFLIGHT_REV_Q001", label: "Flight revenue by month" },
            to_node: { id: 210, kind: "entity", source_kind: "generic", source_name: "agent-data", name: "SCARRSet", label: "Demo airlines" }
        },
        {
            id: 7, from: 210, to: 310, kind: "same_object", label: "", curated: false,
            from_node: { id: 210, kind: "entity", source_kind: "generic", source_name: "agent-data", name: "SCARRSet", label: "SCARRSet" },
            to_node: null
        }
    ],
    used_by: ["gmail-agent"]
};

QUnit.test("an InA source lists its query, then its dimensions and measures one level down", function (assert) {
    const rows = entityRows(INA_DETAIL);
    assert.deepEqual(rows.map((row) => `${row.depth}:${row.kind}:${row.name}`), [
        "0:query:ZFLIGHT_REV_Q001", "1:dimension:0CALMONTH", "1:dimension:ZCARRID", "1:measure:ZREVENUE"
    ]);
    assert.deepEqual(rows.map((row) => row.entity), ["ZFLIGHT_REV_Q001", "ZFLIGHT_REV_Q001", "ZFLIGHT_REV_Q001", "ZFLIGHT_REV_Q001"],
        "a member knows its query");
    assert.strictEqual(rows[1].label, "Calendar month", "the hint controls edit the label as for a field");
    assert.strictEqual(rows[1].savedLabel, "Calendar month");
    assert.strictEqual(rows[3].unit, "EUR", "a measure's unit is carried");
    assert.deepEqual(rows.map((row) => row.icon), [
        "sap-icon://multiple-bar-chart", "sap-icon://dimension", "sap-icon://dimension", "sap-icon://measure"
    ]);
    assert.deepEqual(entityRows({ query: null }), [], "a query never imported has no rows");
    assert.deepEqual(entityRows({}), [], "an answer without entities, functions or query");
});

QUnit.test("each row carries the icon of its kind", function (assert) {
    assert.deepEqual(entityRows(DETAIL).map((row) => row.icon), [
        "sap-icon://table-view", "sap-icon://text-formatting", "sap-icon://table-view", "sap-icon://text-formatting",
        "sap-icon://text-formatting"
    ]);
    assert.strictEqual(nodeIcon("function"), "sap-icon://fx");
    assert.strictEqual(nodeIcon("source"), "sap-icon://database");
    assert.strictEqual(nodeIcon("other" as DataNodeKind), "sap-icon://document", "an unknown kind");
});

QUnit.test("a link says where it comes from; an InA query names its own end", function (assert) {
    const texts: Record<string, string> = { dcLinkOriginAdmin: "Added by an admin", dcLinkOriginImported: "Imported" };
    const rows = relationRows(INA_DETAIL, (key) => texts[key]);
    assert.deepEqual(rows, [
        {
            id: 4, kind: "aggregates", label: "", from: "Flight revenue by month", to: "agent-data · Demo airlines",
            curated: true, origin: "Added by an admin"
        },
        {
            id: 7, kind: "same_object", label: "", from: "agent-data · SCARRSet", to: "Flight revenue by month",
            curated: false, origin: "Imported"
        }
    ], "the own end by its label, the other source's end as source · node, either way round");
    assert.deepEqual(relationRows(INA_DETAIL).map((row) => row.origin), ["dcLinkOriginAdmin", "dcLinkOriginImported"],
        "without a text function: the i18n keys");
    const flag = relationRows({
        ...INA_DETAIL, edges: [{ ...INA_DETAIL.edges[0], curated: "true" as unknown as boolean }]
    });
    assert.strictEqual(flag[0].curated, false, "drawn by an admin only for the boolean true: no remove on anything else");
});

QUnit.test("the relation kinds have a text; an unknown kind has none", function (assert) {
    assert.strictEqual(relationKindKey("same_object"), "dcRelKind_same_object");
    assert.strictEqual(relationKindKey("foreign_key"), "dcRelKind_foreign_key");
    assert.strictEqual(relationKindKey("contains"), "", "not a relation of the detail");
    assert.strictEqual(relationKindKey("toString"), "", "no key from the prototype");
    assert.deepEqual(LINK_KINDS.slice(), ["same_object", "aggregates"], "what an admin may draw, in the Select's order");
});

function graphOf(nodes: [number, DataNodeKind, DataSourceKind, string, string][]): DataGraph {
    return {
        nodes: nodes.map(([id, kind, sourceKind, sourceName, label]) => ({
            id, kind, source_kind: sourceKind, source_name: sourceName, name: `N${id}`, label, group: ""
        })),
        edges: []
    };
}

QUnit.test("link candidates leave out the own source, fields and every kind that cannot be linked", function (assert) {
    const graph = graphOf([
        [300, "source", "ina", "flight-revenue", "Flight revenue"],
        [310, "query", "ina", "flight-revenue", "Flight revenue by month"],
        [311, "dimension", "ina", "flight-revenue", ""],
        [200, "source", "generic", "agent-data", ""],
        [210, "entity", "generic", "agent-data", "Demo airlines"],
        [211, "field", "generic", "agent-data", "Airline"],
        [220, "function", "generic", "agent-data", ""],
        [410, "query", "ina", "cost-centres", "Cost centres"],
        [413, "measure", "ina", "cost-centres", "Amount"]
    ]);
    const rows = linkCandidates(graph, { kind: "ina", name: "flight-revenue" });
    assert.deepEqual(rows.map((row) => row.id), [210, 220, 410], "entities, queries and functions of other sources, in the answer's order");
    assert.deepEqual(rows[0], {
        id: 210, title: "Demo airlines", subtitle: "agent-data · entity N210", kind: "entity", icon: "sap-icon://table-view"
    });
    assert.strictEqual(rows[1].title, "N220", "no label: the name");
    assert.deepEqual(linkCandidates({ nodes: [], edges: [] }, { kind: "ina", name: "flight-revenue" }), []);
});

QUnit.test("an OData and a generic service of one name are one source", function (assert) {
    const graph = graphOf([
        [210, "entity", "generic", "agent-data", ""],
        [10, "entity", "odata", "agent-data", ""],
        [510, "query", "ina", "agent-data", ""],
        [20, "entity", "odata", "business-partners", ""]
    ]);
    assert.deepEqual(linkCandidates(graph, { kind: "odata", name: "agent-data" }).map((row) => row.id), [510, 20],
        "one name is one row of the catalogue services, whatever its kind; an InA query of that name is another source");
    assert.ok(sameSource({ source_kind: "odata", source_name: "x" }, { source_kind: "generic", source_name: "x" }));
    assert.notOk(sameSource({ source_kind: "ina", source_name: "x" }, { source_kind: "odata", source_name: "x" }));
    assert.notOk(sameSource({ source_kind: "odata", source_name: "x" }, { source_kind: "odata", source_name: "y" }));
});

QUnit.test("the candidates are asked of the graph route by kind and search text", function (assert) {
    assert.deepEqual(LINK_NODE_KINDS.slice(), ["entity", "query", "function"]);
    assert.strictEqual(linkQuery(""), "?kind=entity,query,function");
    assert.strictEqual(linkQuery("  demo airline "), "?kind=entity,query,function&q=demo%20airline");
});

QUnit.test("a link starts at an entity or function of the source, or at its InA query", function (assert) {
    assert.deepEqual(linkStarts({
        ...DETAIL, functions: [node(40, "function", "Release", { label: "Release requisition" })]
    }), [
        { key: "10", text: "Requisition", kind: "entity" },
        { key: "20", text: "A_Item", kind: "entity" },
        { key: "40", text: "Release requisition", kind: "function" }
    ], "no field");
    assert.deepEqual(linkStarts(INA_DETAIL), [{ key: "310", text: "Flight revenue by month", kind: "query" }]);
    assert.deepEqual(linkStarts({ source: null, entities: [], functions: [] }), [], "never imported: nothing to link");
    assert.deepEqual(linkStarts({ ...INA_DETAIL, source: null }), [], "no source node, no start");
});

QUnit.test("every text the detail's model names is in the bundle", async function (assert) {
    const bundle = await ResourceBundle.create({
        url: sap.ui.require.toUrl("com/agent/admin/i18n/i18n.properties"), async: true
    }) as ResourceBundle;
    const keys = ["dcLinkOriginAdmin", "dcLinkOriginImported"]
        .concat(["navigation", "association", "foreign_key", "same_object", "aggregates"].map(relationKindKey))
        .concat(NODE_KINDS.map((kind) => `dcNodeKind_${kind}`));
    keys.forEach((key) => assert.ok(key && bundle.hasText(key), key));
    assert.strictEqual(bundle.getText("dcLinkOriginAdmin"), "Added by an admin");
    assert.strictEqual(bundle.getText("dcLinkOriginImported"), "Imported");
});

QUnit.test("the links module's default export holds what the detail page and the Add link dialog call", function (assert) {
    assert.deepEqual(Object.keys(dataCatalogLinks).sort(), [
        "LINK_KINDS", "LINK_NODE_KINDS", "linkCandidates", "linkQuery", "linkStarts", "relationKindKey", "relationRows",
        "sameSource"
    ]);
    assert.strictEqual(dataCatalogLinks.relationRows, relationRows);
    assert.notOk("relationRows" in dataCatalog, "the catalogue model no longer carries the link block");
});

QUnit.module("dataCatalog: what a hint edit sends");

QUnit.test("only the changed fields are sent", function (assert) {
    const saved = hintsOf(DETAIL.source!);
    assert.deepEqual(hintsUpdate(saved, { ...saved }), {}, "nothing changed");
    assert.notOk(hasChanges(hintsUpdate(saved, { ...saved })));
    const body = hintsUpdate(saved, { ...saved, purpose: "Requests to buy", synonyms: ["PR"] });
    assert.deepEqual(body, { purpose: "Requests to buy", synonyms: ["PR"] });
    assert.ok(hasChanges(body));
});

QUnit.test("texts go as typed, an emptied label included; lists are cleaned", function (assert) {
    const saved = hintsOf(node(5, "entity", "A", { label: "Old", examples: ["How many?"] }));
    assert.deepEqual(hintsUpdate(saved, { ...saved, label: "" }), { label: "" },
        "the server decides about an empty label");
    assert.deepEqual(hintsUpdate(saved, { ...saved, examples: [" How many? ", "", "How many?"] }), {},
        "blank and repeated entries do not count as a change");
    assert.deepEqual(hintsUpdate(saved, { ...saved, examples: ["How many?", " Which? "] }),
        { examples: ["How many?", "Which?"] });
    assert.deepEqual(cleanList([" a ", "", "a", "b"]), ["a", "b"]);
});

QUnit.test("readable is sent only when it changes", function (assert) {
    const saved = hintsOf(node(5, "field", "F"));
    assert.deepEqual(hintsUpdate(saved, { ...saved, readable: false }), { readable: false });
    assert.deepEqual(hintsUpdate(saved, { ...saved, readable: undefined }), {});
});

QUnit.test("a refusal is read by field, in the route's string form and in detail[]", function (assert) {
    assert.deepEqual(hintErrors({}, "purpose: Value error, must be one line; synonyms.1: too long"),
        { purpose: "must be one line", synonyms: "too long" }, "the string form, an item counting for its list");
    assert.deepEqual(hintErrors({ label: "Value error, label: must not be empty" }, "Value error, label: must not be empty"),
        { label: "Value error, label: must not be empty" }, "detail[] as AdminError flattens it");
    assert.deepEqual(hintErrors({}, "readable: the catalogue does not let agents read this node"),
        { readable: "the catalogue does not let agents read this node" });
    assert.deepEqual(hintErrors({}, "Node not found"), {}, "no field named: shown as a whole");
    assert.deepEqual(hintErrors(undefined, undefined), {});
});

QUnit.module("dataCatalog: test search candidates");

QUnit.test("candidates are listed best first with their hints and relations", function (assert) {
    const candidates: DataSearchCandidate[] = [
        {
            source_kind: "odata", source_name: "business-partners", kind: "entity", name: "A_BusinessPartner",
            label: "", purpose: "", not_for: "", keys: [], relations: [], score: 0.2
        },
        {
            source_kind: "odata", source_name: "purchase-requisitions", kind: "entity", name: "A_Item",
            label: "Requisition item", purpose: "A line of a requisition", not_for: "Orders",
            keys: ["PurchaseRequisition", "PurchaseRequisitionItem"],
            relations: [{ kind: "association", to: { source_name: "purchase-requisitions", name: "A_Supplier", label: "Supplier" } }],
            score: 0.876
        }
    ];
    const rows = candidateRows(candidates);
    assert.deepEqual(rows.map((row) => row.title), ["Requisition item", "A_BusinessPartner"],
        "sorted by score; no label falls back to the name");
    assert.deepEqual(rows[0], {
        title: "Requisition item",
        subtitle: "purchase-requisitions · entity A_Item",
        score: "0.88",
        purpose: "A line of a requisition",
        notFor: "Orders",
        keys: "PurchaseRequisition, PurchaseRequisitionItem",
        relations: ["association → Supplier (purchase-requisitions)"]
    });
    assert.deepEqual(candidateRows(undefined), []);
    assert.strictEqual(scoreText("x"), "–");
});

QUnit.module("dataCatalog: the graph");

function graphNode(id: number, kind: DataNodeKind, sourceName: string, sourceKind: "odata" | "ina" = "odata"): DataGraphNode {
    return { id, kind, source_kind: sourceKind, source_name: sourceName, name: `N${id}`, label: id === 1 ? "One" : "", group: "" };
}

QUnit.test("one group per source, coloured by its kind, and only lines between shown nodes", function (assert) {
    const model = graphModel({
        nodes: [graphNode(1, "source", "pr"), graphNode(2, "entity", "pr"), graphNode(3, "field", "pr"),
            graphNode(4, "query", "sales", "ina")],
        edges: [
            { from: 1, to: 2, kind: "aggregates", label: "" },
            { from: 2, to: 3, kind: "aggregates", label: "has" },
            { from: 3, to: 77, kind: "foreign_key", label: "" }
        ]
    });
    assert.deepEqual(model.groups, [
        { key: "odata:pr", title: "pr", status: "dc-odata" },
        { key: "ina:sales", title: "sales", status: "dc-ina" }
    ]);
    assert.strictEqual(model.nodes.length, 4);
    assert.strictEqual(model.nodes[0].title, "One", "the label when there is one");
    assert.strictEqual(model.nodes[1].title, "N2", "else the name");
    assert.strictEqual(model.nodes[0].shape, "Box");
    assert.strictEqual(model.nodes[2].shape, "Circle");
    assert.deepEqual(model.lines, [
        { from: "1", to: "2", title: "aggregates", description: "", curated: false },
        { from: "2", to: "3", title: "aggregates", description: "has", curated: false }
    ], "the kind is the title, the label the description; a line to a node that is not there is dropped");
    assert.notOk(model.capped);
});

QUnit.test("a line knows whether an admin drew it", function (assert) {
    const model = graphModel({
        nodes: [graphNode(1, "entity", "pr"), graphNode(2, "entity", "bp"), graphNode(3, "query", "sales", "ina")],
        edges: [
            { from: 1, to: 2, kind: "same_object", label: "", curated: true },
            { from: 1, to: 3, kind: "aggregates", label: "", curated: false },
            { from: 2, to: 3, kind: "aggregates", label: "" }
        ]
    });
    assert.deepEqual(model.lines.map((l) => l.curated), [true, false, false],
        "curated only for the boolean true; an answer without the flag is an imported edge");
});

QUnit.test("picked kinds filter the nodes and their lines", function (assert) {
    const model = graphModel({
        nodes: [graphNode(1, "source", "pr"), graphNode(2, "entity", "pr"), graphNode(3, "field", "pr")],
        edges: [{ from: 1, to: 2, kind: "aggregates", label: "" }, { from: 2, to: 3, kind: "aggregates", label: "" }]
    }, ["entity", "field"]);
    assert.deepEqual(model.nodes.map((n) => n.key), ["2", "3"]);
    assert.deepEqual(model.lines.map((l) => `${l.from}-${l.to}`), ["2-3"]);
});

QUnit.test("an answer of the full cap is marked as capped", function (assert) {
    const nodes: DataGraphNode[] = [];
    for (let i = 0; i < GRAPH_NODE_CAP; i++) {
        nodes.push(graphNode(i + 10, "field", "big"));
    }
    assert.ok(graphModel({ nodes, edges: [] }).capped);
});

QUnit.test("the server's truncated flag and group are used", function (assert) {
    const answer = { nodes: [{ ...graphNode(1, "entity", "pr"), group: "odata:pr" }], edges: [], truncated: true };
    const model = graphModel(answer);
    assert.ok(model.capped, "truncated marks the graph as capped");
    assert.strictEqual(model.nodes[0].group, "odata:pr");
    assert.deepEqual(model.groups.map((g) => g.key), ["odata:pr"]);
});

QUnit.test("the graph opens at the largest zoom step that fits, never above 100%", function (assert) {
    const steps = [0.25, 0.5, 0.75, 1, 1.5];
    const view = { width: 1000, height: 600 };
    assert.strictEqual(fitZoom({ width: 400, height: 300 }, view, steps), 1, "small content is not blown up");
    assert.strictEqual(fitZoom({ width: 1600, height: 500 }, view, steps), 0.5, "width decides");
    assert.strictEqual(fitZoom({ width: 900, height: 800 }, view, steps), 0.75, "height decides");
    assert.strictEqual(fitZoom({ width: 90000, height: 500 }, view, steps), 0.25, "the smallest step when none fits");
    assert.strictEqual(fitZoom({ width: 0, height: 0 }, view, steps), 1, "an empty graph");
});

QUnit.test("the query carries only the filters that are set", function (assert) {
    assert.strictEqual(graphQuery({}), "");
    assert.strictEqual(graphQuery({ agent: "", kinds: [], q: "  " }), "");
    assert.strictEqual(graphQuery({ agent: "btp-agent", kinds: ["entity"], q: "supplier name" }),
        "?agent=btp-agent&kind=entity&q=supplier%20name");
    assert.strictEqual(graphQuery({ kinds: ["entity", "field"] }), "?kind=entity,field",
        "several kinds go to the route comma-separated, so they apply before its node cap");
    assert.strictEqual(graphQuery({ kinds: NODE_KINDS.slice() }), "", "every kind is no filter");
});

QUnit.test("the graph opens with every kind but fields", function (assert) {
    assert.deepEqual(DEFAULT_GRAPH_KINDS.slice(), ["source", "entity", "query", "dimension", "measure", "function"],
        "53 fields of one service would shrink the graph to 10%: fields only when picked");
    assert.strictEqual(DEFAULT_GRAPH_KINDS.indexOf("field"), -1);
    assert.strictEqual(graphQuery({ kinds: DEFAULT_GRAPH_KINDS }),
        "?kind=source,entity,query,dimension,measure,function");
    const model = graphModel({
        nodes: [graphNode(1, "source", "pr"), graphNode(2, "entity", "pr"), graphNode(3, "field", "pr")],
        edges: [{ from: 1, to: 2, kind: "contains", label: "" }, { from: 2, to: 3, kind: "contains", label: "" }]
    }, DEFAULT_GRAPH_KINDS);
    assert.deepEqual(model.nodes.map((n) => n.key), ["1", "2"], "no field node by default");
});

QUnit.test("an unknown source kind is coloured as generic", function (assert) {
    assert.strictEqual(statusKey("odata"), "dc-odata");
    assert.strictEqual(statusKey("other"), "dc-generic");
    assert.strictEqual(dataCatalog.displayName({ label: "  ", name: "X" }), "X");
});
