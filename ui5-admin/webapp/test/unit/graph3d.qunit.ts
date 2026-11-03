import graph3d, {
    DASH, draw, drawList, hitTest, initialView, layout, project, rotate, step, zoomed
} from "com/agent/admin/model/graph3d";
import type { Canvas2d, Colours3d, DrawItem, Layout, Placed, Projected, View3d } from "com/agent/admin/model/graph3d";
import { graphModel } from "com/agent/admin/model/dataCatalog";
import type { DataGraphEdge, DataGraphNode, DataNodeKind, DataSourceKind } from "com/agent/admin/service/dataCatalogTypes";

/** The real 2D context fits the module's interface (checked by tsc, not at run time). */
type ContextFits = CanvasRenderingContext2D extends Canvas2d ? true : false;
const CONTEXT_FITS: ContextFits = true;

function graphNode(id: number, kind: DataNodeKind, sourceName: string, sourceKind: DataSourceKind = "odata"): DataGraphNode {
    return { id, kind, source_kind: sourceKind, source_name: sourceName, name: `N${id}`, label: "", group: "" };
}

function contains(from: number, to: number): DataGraphEdge {
    return { from, to, kind: "contains", label: "" };
}

/** Three sources: `pr` with two entities (the first with two fields), `sales` (InA) with a query, `bp` with an entity. */
function catalogue(kinds: readonly string[] = []) {
    return graphModel({
        nodes: [
            graphNode(1, "source", "pr"), graphNode(2, "entity", "pr"), graphNode(3, "entity", "pr"),
            graphNode(4, "field", "pr"), graphNode(5, "field", "pr"),
            graphNode(10, "source", "sales", "ina"), graphNode(11, "query", "sales", "ina"),
            graphNode(20, "source", "bp"), graphNode(21, "entity", "bp")
        ],
        edges: [
            contains(1, 2), contains(1, 3), contains(2, 4), contains(2, 5), contains(10, 11), contains(20, 21),
            { from: 2, to: 21, kind: "same_object", label: "", curated: true },
            { from: 3, to: 11, kind: "aggregates", label: "" }
        ]
    }, kinds);
}

const COLOURS: Colours3d = { odata: "#0070f2", ina: "#c35500", generic: "#5d36ff", line: "#556b82", text: "#1d2d3e" };

function view(extra: Partial<View3d> = {}): View3d {
    return { yaw: 0, pitch: 0, zoom: 1, width: 800, height: 600, touched: false, hover: null, ...extra };
}

function pointOf(placed: Layout, key: string): Placed {
    const point = placed.points.filter((p) => p.key === key)[0];
    if (!point) {
        throw new Error(`no point ${key}`);
    }
    return point;
}

function distance(a: { x: number; y: number; z: number }, b: { x: number; y: number; z: number }): number {
    return Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
}

function close(assert: Assert, actual: number, expected: number, message: string): void {
    assert.ok(Math.abs(actual - expected) < 1e-6, `${message} (${actual} ~ ${expected})`);
}

QUnit.module("graph3d: the layout");

QUnit.test("every node is placed once, at a finite point", function (assert) {
    const model = catalogue();
    const placed = layout(model);
    assert.deepEqual(placed.points.map((p) => p.key), model.nodes.map((n) => n.key), "in the model's order, none twice");
    assert.ok(placed.points.every((p) => [p.x, p.y, p.z].every(Number.isFinite)), "no NaN, no Infinity");
    assert.strictEqual(pointOf(placed, "4").kind, "field", "a point keeps its node's kind");
});

QUnit.test("sources sit on a ring in the horizontal plane, at equal angles", function (assert) {
    const placed = layout(catalogue());
    assert.strictEqual(placed.ring, 120, "three sources: the 120 floor");
    const sources = ["1", "10", "20"].map((key) => pointOf(placed, key));
    sources.forEach((s) => {
        close(assert, s.y, 0, "in the XZ plane");
        close(assert, Math.hypot(s.x, s.z), 120, "on the ring");
    });
    const angles = sources.map((s) => Math.atan2(s.z, s.x));
    const step12 = (angles[1] - angles[0] + 2 * Math.PI) % (2 * Math.PI);
    const step23 = (angles[2] - angles[1] + 2 * Math.PI) % (2 * Math.PI);
    close(assert, step12, 2 * Math.PI / 3, "first to second");
    close(assert, step23, 2 * Math.PI / 3, "second to third");
});

QUnit.test("the ring grows by 40 per source beyond three", function (assert) {
    const nodes: DataGraphNode[] = [];
    for (let i = 0; i < 5; i++) {
        nodes.push(graphNode(i + 1, "source", `s${i}`));
    }
    const placed = layout(graphModel({ nodes, edges: [] }));
    assert.strictEqual(placed.ring, 200);
    placed.points.forEach((p) => close(assert, Math.hypot(p.x, p.z), 200, `source ${p.key} on the ring`));
    assert.strictEqual(layout(graphModel({ nodes: [], edges: [] })).ring, 120, "an empty graph keeps the floor");
    assert.deepEqual(layout(graphModel({ nodes: [], edges: [] })).points, []);
});

QUnit.test("entities, queries and functions circle their source at 45, fields their parent at 18", function (assert) {
    const placed = layout(catalogue());
    close(assert, distance(pointOf(placed, "2"), pointOf(placed, "1")), 45, "entity to its source");
    close(assert, distance(pointOf(placed, "3"), pointOf(placed, "1")), 45, "second entity to its source");
    close(assert, distance(pointOf(placed, "11"), pointOf(placed, "10")), 45, "query to its source");
    close(assert, distance(pointOf(placed, "4"), pointOf(placed, "2")), 18, "field to its entity");
    close(assert, distance(pointOf(placed, "5"), pointOf(placed, "2")), 18, "second field to its entity");
    assert.ok(distance(pointOf(placed, "2"), pointOf(placed, "3")) > 1, "two children do not share a point");
    assert.ok(distance(pointOf(placed, "4"), pointOf(placed, "5")) > 1, "two fields do not share a point");
});

QUnit.test("a source's ring is tilted by its angle: not every child ring lies in one plane", function (assert) {
    const placed = layout(catalogue());
    const heights = ["2", "3", "11", "21"].map((key) => Math.abs(pointOf(placed, key).y));
    assert.ok(heights.some((h) => h > 1), "some child leaves the horizontal plane");
});

QUnit.test("nodes whose source is filtered out still circle their source's place", function (assert) {
    const placed = layout(catalogue(["entity"]));
    assert.deepEqual(placed.points.map((p) => p.key), ["2", "3", "21"]);
    assert.strictEqual(placed.ring, 120, "two groups: the floor");
    const centreOfPr = { x: 120, y: 0, z: 0 };
    close(assert, distance(pointOf(placed, "2"), centreOfPr), 45, "on the ring of the first group's place");
    close(assert, distance(pointOf(placed, "3"), centreOfPr), 45, "the second entity too");
});

QUnit.test("contains lines that loop do not lose a node", function (assert) {
    const placed = layout(graphModel({
        nodes: [graphNode(1, "entity", "pr"), graphNode(2, "entity", "pr")],
        edges: [contains(1, 2), contains(2, 1)]
    }));
    assert.deepEqual(placed.points.map((p) => p.key), ["1", "2"]);
    assert.ok(placed.points.every((p) => Number.isFinite(p.x + p.y + p.z)));
});

QUnit.module("graph3d: the projection");

function at(x: number, y: number, z: number, kind: DataNodeKind = "entity", key = "p"): Placed {
    return { key, kind, x, y, z };
}

QUnit.test("the origin is drawn in the centre, whatever the rotation", function (assert) {
    [view(), view({ yaw: 1.2, pitch: -0.7, zoom: 2.5 })].forEach((v) => {
        const [p] = project({ points: [at(0, 0, 0)], ring: 120 }, v);
        close(assert, p.x, 400, "x");
        close(assert, p.y, 300, "y");
    });
});

QUnit.test("a point in front is drawn larger than one behind, and nearer", function (assert) {
    const placed: Layout = { points: [at(0, 0, -50, "entity", "front"), at(0, 0, 50, "entity", "back")], ring: 120 };
    const [front, back] = project(placed, view());
    assert.ok(front.radius > back.radius, "larger");
    assert.ok(front.depth < back.depth, "nearer");
    const [turnedFront, turnedBack] = project(placed, view({ yaw: Math.PI }));
    assert.ok(turnedFront.radius < turnedBack.radius, "half a turn swaps them");
});

QUnit.test("node radius by kind on the centre plane: 10, 6, 3", function (assert) {
    const placed: Layout = {
        points: [at(0, 0, 0, "source", "s"), at(0, 0, 0, "entity", "e"), at(0, 0, 0, "query", "q"),
            at(0, 0, 0, "function", "f"), at(0, 0, 0, "field", "d")],
        ring: 300
    };
    assert.deepEqual(project(placed, view()).map((p) => p.radius), [10, 6, 6, 6, 3]);
    assert.deepEqual(project(placed, view({ zoom: 2 })).map((p) => p.radius), [20, 12, 12, 12, 6], "zoom scales them");
});

QUnit.test("y points up, zoom moves a point away from the centre", function (assert) {
    const [up] = project({ points: [at(0, 50, 0)], ring: 120 }, view());
    assert.ok(up.y < 300, "above the centre on screen");
    const [one] = project({ points: [at(100, 0, 0)], ring: 120 }, view());
    const [two] = project({ points: [at(100, 0, 0)], ring: 120 }, view({ zoom: 2 }));
    close(assert, two.x - 400, 2 * (one.x - 400), "twice as far");
    close(assert, one.x - 400, 100 * 600 / 360, "the centre plane at focal length over camera distance");
});

QUnit.test("a positive pitch looks from above: the far side rises", function (assert) {
    const [far, near] = project({ points: [at(0, 0, 50, "entity", "far"), at(0, 0, -50, "entity", "near")], ring: 120 },
        view({ pitch: 0.5 }));
    assert.ok(far.y < 300 && near.y > 300);
});

QUnit.test("the projection is a pure function of its arguments", function (assert) {
    const placed = layout(catalogue());
    const before = JSON.stringify(placed);
    const v = view({ yaw: 0.4, pitch: 0.3 });
    const first = project(placed, v);
    project(placed, view({ yaw: 2, zoom: 3 }));
    assert.deepEqual(project(placed, v), first, "the same answer for the same arguments");
    assert.strictEqual(JSON.stringify(placed), before, "the layout is not changed");
    assert.deepEqual(v, view({ yaw: 0.4, pitch: 0.3 }), "nor the view");
});

QUnit.test("a zoom that is no number is taken as 1", function (assert) {
    const placed: Layout = { points: [at(100, 0, 0)], ring: 120 };
    assert.deepEqual(project(placed, view({ zoom: NaN })), project(placed, view()));
    assert.deepEqual(project(placed, view({ zoom: 0 })), project(placed, view()));
});

QUnit.module("graph3d: the hit test");

function projected(key: string, x: number, y: number, radius: number, depth = 0): Projected {
    return { key, kind: "entity", x, y, radius, depth };
}

QUnit.test("the nearest centre within radius + 4 px wins", function (assert) {
    const list = [projected("a", 100, 100, 6), projected("b", 110, 100, 6)];
    assert.strictEqual(hitTest(list, 103, 100)?.key, "a");
    assert.strictEqual(hitTest(list, 108, 100)?.key, "b");
    assert.strictEqual(hitTest(list, 100, 109.9)?.key, "a", "inside the 4 px slack");
    assert.strictEqual(hitTest(list, 100, 110.1), null, "outside it");
    assert.strictEqual(hitTest([], 0, 0), null, "nothing to hit");
});

QUnit.test("at the same distance the nearer node, drawn on top, wins", function (assert) {
    const list = [projected("behind", 100, 100, 6, 40), projected("front", 100, 100, 6, -40)];
    assert.strictEqual(hitTest(list, 101, 100)?.key, "front");
    assert.strictEqual(hitTest(list.slice().reverse(), 101, 100)?.key, "front", "whatever the order");
});

QUnit.module("graph3d: the draw list");

function lines(list: DrawItem[]) {
    return list.filter((item): item is Extract<DrawItem, { type: "line" }> => item.type === "line");
}

function nodes(list: DrawItem[]) {
    return list.filter((item): item is Extract<DrawItem, { type: "node" }> => item.type === "node");
}

function labels(list: DrawItem[]) {
    return list.filter((item): item is Extract<DrawItem, { type: "label" }> => item.type === "label");
}

QUnit.test("lines come before nodes, labels last", function (assert) {
    const list = drawList(catalogue(), view({ yaw: 0.3, pitch: 0.4 }), COLOURS);
    const order = list.map((item) => item.type);
    const lastLine = order.lastIndexOf("line");
    const firstNode = order.indexOf("node");
    const lastNode = order.lastIndexOf("node");
    const firstLabel = order.indexOf("label");
    assert.ok(lastLine < firstNode, "every line before the first node");
    assert.ok(lastNode < firstLabel, "every node before the first label");
    assert.strictEqual(nodes(list).length, 9, "one item per node");
    assert.strictEqual(lines(list).length, 8, "one item per line");
});

QUnit.test("far before near, nodes and lines alike", function (assert) {
    const list = drawList(catalogue(), view({ yaw: 0.9, pitch: 0.2 }), COLOURS);
    const nodeDepths = nodes(list).map((n) => n.depth);
    const lineDepths = lines(list).map((l) => l.depth);
    assert.ok(nodeDepths.every((d, i) => i === 0 || d <= nodeDepths[i - 1]), "nodes far to near");
    assert.ok(lineDepths.every((d, i) => i === 0 || d <= lineDepths[i - 1]), "lines far to near");
});

QUnit.test("curated lines are dashed, contains lines faint", function (assert) {
    const all = lines(drawList(catalogue(), view(), COLOURS));
    const byEnds = (from: string, to: string) => all.filter((l) => l.from === from && l.to === to)[0];
    assert.strictEqual(byEnds("2", "21").dashed, true, "the curated same_object link");
    assert.strictEqual(byEnds("3", "11").dashed, false, "an imported link");
    assert.strictEqual(byEnds("1", "2").dashed, false);
    assert.ok(byEnds("1", "2").alpha < byEnds("3", "11").alpha, "contains is fainter than a cross-source link");
    assert.ok(all.every((l) => l.colour === COLOURS.line));
});

QUnit.test("a node takes the colour of its source kind, an unknown kind the generic one", function (assert) {
    const model = catalogue();
    model.nodes.push({ ...model.nodes[0], key: "99", source_kind: "other" as DataSourceKind });
    const byKey: Record<string, string> = {};
    nodes(drawList(model, view(), COLOURS)).forEach((n) => { byKey[n.key] = n.colour; });
    assert.strictEqual(byKey["1"], COLOURS.odata);
    assert.strictEqual(byKey["11"], COLOURS.ina);
    assert.strictEqual(byKey["99"], COLOURS.generic);
});

QUnit.test("labels: every source, and the hovered node with its kind", function (assert) {
    const plain = labels(drawList(catalogue(), view(), COLOURS));
    assert.deepEqual(plain.map((l) => l.text).sort(), ["N1", "N10", "N20"], "sources only");
    const hovered = labels(drawList(catalogue(), view({ hover: "4" }), COLOURS));
    assert.strictEqual(hovered.length, 4);
    assert.strictEqual(hovered[3].text, "N4 (field)", "the hovered one last, so it is on top");
    assert.strictEqual(hovered[3].colour, COLOURS.text);
    const node = nodes(drawList(catalogue(), view({ hover: "4" }), COLOURS)).filter((n) => n.key === "4")[0];
    assert.strictEqual(node.hover, true, "the hovered node is marked");
    close(assert, hovered[3].x, node.x + node.radius + 4, "beside its node");
    const source = labels(drawList(catalogue(), view({ hover: "1" }), COLOURS));
    assert.strictEqual(source.length, 3, "a hovered source is not labelled twice");
    assert.strictEqual(source[2].text, "N1 (source)");
});

QUnit.module("graph3d: drawing");

interface Call { name: string; args: unknown[] }

function recorder(): { ctx: Canvas2d; calls: Call[] } {
    const calls: Call[] = [];
    const record = (name: string) => (...args: unknown[]) => { calls.push({ name, args }); };
    const ctx: Canvas2d = {
        fillStyle: "", strokeStyle: "", globalAlpha: 1, lineWidth: 1, font: "", textAlign: "start", textBaseline: "alphabetic",
        save: record("save"), restore: record("restore"), beginPath: record("beginPath"), moveTo: record("moveTo"),
        lineTo: record("lineTo"), stroke: record("stroke"), arc: record("arc"), fill: record("fill"),
        fillText: record("fillText"), setLineDash: record("setLineDash")
    };
    return { ctx, calls };
}

QUnit.test("a line, a node and a label are painted in that order", function (assert) {
    const { ctx, calls } = recorder();
    const list: DrawItem[] = [
        { type: "line", from: "1", to: "2", x1: 1, y1: 2, x2: 3, y2: 4, colour: "#111", alpha: 0.25, width: 1, dashed: true, depth: 5 },
        { type: "line", from: "2", to: "3", x1: 5, y1: 6, x2: 7, y2: 8, colour: "#111", alpha: 0.9, width: 1.5, dashed: false, depth: 4 },
        { type: "node", key: "1", kind: "source", x: 10, y: 20, radius: 10, colour: "#222", depth: 0, hover: false, outline: "#333" },
        { type: "label", key: "1", text: "Purchase requisitions", x: 24, y: 20, colour: "#333" }
    ];
    draw(ctx, list);
    const painting = calls.filter((c) => ["moveTo", "lineTo", "arc", "fillText", "setLineDash"].indexOf(c.name) !== -1);
    assert.deepEqual(painting, [
        { name: "moveTo", args: [1, 2] }, { name: "lineTo", args: [3, 4] }, { name: "setLineDash", args: [DASH] },
        { name: "moveTo", args: [5, 6] }, { name: "lineTo", args: [7, 8] }, { name: "setLineDash", args: [[]] },
        { name: "arc", args: [10, 20, 10, 0, 2 * Math.PI] },
        { name: "fillText", args: ["Purchase requisitions", 24, 20] }
    ], "the path first, the dash right before its stroke");
    assert.deepEqual(calls.map((c) => c.name), [
        "save", "beginPath", "moveTo", "lineTo", "setLineDash", "stroke",
        "beginPath", "moveTo", "lineTo", "setLineDash", "stroke",
        "beginPath", "arc", "fill", "fillText", "restore"
    ], "each shape its own path, the context's state put back at the end");
    assert.ok(CONTEXT_FITS);
});

QUnit.test("a hovered node gets a solid ring", function (assert) {
    const { ctx, calls } = recorder();
    draw(ctx, [
        { type: "line", from: "1", to: "2", x1: 0, y1: 0, x2: 1, y2: 1, colour: "#111", alpha: 1, width: 1, dashed: true, depth: 0 },
        { type: "node", key: "2", kind: "entity", x: 1, y: 1, radius: 6, colour: "#222", depth: 0, hover: true, outline: "#333" }
    ]);
    assert.deepEqual(calls.slice(-4).map((c) => c.name), ["fill", "setLineDash", "stroke", "restore"]);
    assert.deepEqual(calls[calls.length - 3].args, [[]], "not dashed like the line before it");
});

QUnit.test("an empty list paints nothing", function (assert) {
    const { ctx, calls } = recorder();
    draw(ctx, []);
    assert.deepEqual(calls.map((c) => c.name), ["save", "restore"]);
});

QUnit.module("graph3d: the view");

QUnit.test("the auto-rotation turns slowly until the first drag", function (assert) {
    const v = view({ yaw: 1 });
    close(assert, step(v, 2).yaw, 1.3, "0.15 per second");
    assert.strictEqual(v.yaw, 1, "the view itself is not changed");
    assert.strictEqual(step(view({ yaw: 1, touched: true }), 2).yaw, 1, "a dragged view stays");
    assert.strictEqual(step(v, NaN).yaw, 1, "no time, no turn");
    assert.strictEqual(step(v, -1).yaw, 1);
    const turned = step(view({ yaw: 2 * Math.PI - 0.01 }), 1).yaw;
    assert.ok(turned >= 0 && turned < 2 * Math.PI, "kept within one turn");
});

QUnit.test("a drag turns and tilts the view and stops the auto-rotation", function (assert) {
    const v = rotate(view(), 100, 50);
    close(assert, v.yaw, 1, "100 px right");
    close(assert, v.pitch, 0.5, "50 px down looks more from above");
    assert.strictEqual(v.touched, true);
    close(assert, rotate(view(), 0, 10000).pitch, 1.4, "the tilt stops short of straight down");
    close(assert, rotate(view(), 0, -10000).pitch, -1.4, "and of straight up");
});

QUnit.test("the wheel zooms within bounds", function (assert) {
    assert.ok(zoomed(view(), -100).zoom > 1, "wheel up zooms in");
    assert.ok(zoomed(view(), 100).zoom < 1, "wheel down zooms out");
    assert.strictEqual(zoomed(view(), -1e6).zoom, 4);
    assert.strictEqual(zoomed(view(), 1e6).zoom, 0.25);
    assert.strictEqual(zoomed(view(), NaN).zoom, 1, "no delta, no change");
});

QUnit.test("the first view looks slightly from above and turns by itself", function (assert) {
    const v = initialView(640, 480);
    assert.deepEqual(v, { yaw: 0, pitch: 0.35, zoom: 1, width: 640, height: 480, touched: false, hover: null });
    assert.strictEqual(graph3d.layout, layout, "the default export carries the functions");
});
