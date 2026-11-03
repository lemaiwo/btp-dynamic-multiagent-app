import dataCatalog, {
    DEFAULT_GRAPH_KINDS, GRAPH_NODE_CAP, NODE_KINDS, candidateRows, cleanList, completenessPercent, completenessState, completenessText, entityRows, fitZoom,
    graphModel, graphQuery, hasChanges, hintErrors, hintsOf, hintsUpdate, relationRows, scoreText, statusKey
} from "com/agent/admin/model/dataCatalog";
import type {
    DataGraphNode, DataNode, DataNodeKind, DataSearchCandidate, DataSourceDetail
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
        { from: 10, to: 20, kind: "navigation", label: "to_Item" },
        { from: 22, to: 999, kind: "foreign_key", label: "" }
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
        { kind: "navigation", label: "to_Item", from: "Requisition", to: "A_Item" },
        { kind: "foreign_key", label: "", from: "A_Item.Supplier", to: "999" }
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
        edges: [{ from: 22, to: 900, kind: "same_object", label: "", from_node: null, to_node: other }]
    });
    assert.deepEqual(rows, [{ kind: "same_object", label: "", from: "A_Item.Supplier", to: "Supplier (business-partners)" }]);
    assert.deepEqual(relationRows({ source: null, entities: [], edges: [] }), [], "a source never imported has none");
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
        { from: "1", to: "2", title: "aggregates", description: "" },
        { from: "2", to: "3", title: "aggregates", description: "has" }
    ], "the kind is the title, the label the description; a line to a node that is not there is dropped");
    assert.notOk(model.capped);
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
