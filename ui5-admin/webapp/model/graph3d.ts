import type { GraphModel } from "./dataCatalog";
import type { DataNodeKind } from "../service/dataCatalogTypes";

/**
 * The 3D overview of the data catalogue graph: layout, rotation,
 * perspective projection, hit test and the draw list. Every function is
 * pure; `draw` paints a draw list on anything shaped like a 2D canvas
 * context. No DOM and no UI5 here: the controller sizes the canvas, clears
 * it, feeds pointer events to `rotate` / `zoomed` / `hitTest` and calls
 * `draw(ctx, drawList(...))` per frame.
 *
 * Layout: one place per source (per group, so the nodes of a source the
 * kind filter hides still circle that place) on a ring in the horizontal
 * plane; a source's entities, queries and functions on a ring of 45 around
 * that place, in the horizontal plane turned about the source's radial axis
 * by the source's angle, so neighbouring rings do not lie in one plane;
 * the children of those (fields, a query's dimensions and measures) on a
 * ring of 18 around their parent, standing upright on the parent's ring.
 * A node's parent is the `from` of a `contains` line to it.
 */

/** A point in world space. */
interface Vec { x: number; y: number; z: number }

/** A node placed in world space. */
export interface Placed extends Vec {
    key: string;
    kind: DataNodeKind;
}

/** What `layout` answers: the points, and the radius of the source ring
 *  (the camera stands three of those away from the origin). */
export interface Layout {
    points: Placed[];
    ring: number;
}

/** The view state the controller keeps. Angles in radians. */
export interface View3d {
    /** Turn about the vertical axis. */
    yaw: number;
    /** Tilt about the horizontal screen axis; positive looks from above. */
    pitch: number;
    zoom: number;
    /** The canvas size, CSS px. */
    width: number; height: number;
    /** A drag happened: the auto-rotation has stopped for good. */
    touched?: boolean;
    /** The key of the node under the pointer. */
    hover?: string | null;
}

/** A node on screen. */
export interface Projected {
    key: string;
    kind: DataNodeKind;
    /** CSS px from the top left of the canvas. */
    x: number; y: number;
    /** Behind the centre plane after rotation; larger is farther. */
    depth: number;
    /** Drawn radius, px. */
    radius: number;
}

/** A line between two nodes; `depth` is the mean of its ends'. `dashed`: an admin drew the link. */
export interface DrawLine {
    type: "line"; from: string; to: string; x1: number; y1: number; x2: number; y2: number;
    colour: string; alpha: number; width: number; dashed: boolean; depth: number;
}

/** A node; `outline` is the colour of the ring a hovered node gets. */
export interface DrawNode {
    type: "node"; key: string; kind: DataNodeKind; x: number; y: number; radius: number;
    colour: string; depth: number; hover: boolean; outline: string;
}

/** A label, left-aligned at (x, y), vertically centred. */
export interface DrawLabel {
    type: "label"; key: string; text: string; x: number; y: number; colour: string;
}

/** One thing to paint, in the order of the list. */
export type DrawItem = DrawLine | DrawNode | DrawLabel;

/** The colours, read from the theme by the controller. */
export interface Colours3d {
    /** One per source kind, as in the 2D legend; an unknown kind takes `generic`. */
    odata: string; ina: string; generic: string;
    /** Every line; `contains` lines are drawn faint. */
    line: string;
    /** Labels and the ring around the hovered node. */
    text: string;
}

/** The part of `CanvasRenderingContext2D` that `draw` uses. */
export interface Canvas2d {
    fillStyle: string | object; strokeStyle: string | object; globalAlpha: number; lineWidth: number;
    font: string; textAlign: string; textBaseline: string;
    save(): void;
    restore(): void;
    beginPath(): void;
    moveTo(x: number, y: number): void;
    lineTo(x: number, y: number): void;
    stroke(): void;
    arc(x: number, y: number, radius: number, startAngle: number, endAngle: number): void;
    fill(): void;
    fillText(text: string, x: number, y: number): void;
    setLineDash(segments: number[]): void;
}

/** The source ring: at least this radius, and this much per source. */
export const RING_MIN = 120;
export const RING_PER_SOURCE = 40;
/** A source's children ring, and the ring of their children. */
export const CHILD_RING = 45;
export const LEAF_RING = 18;
/** The camera stands this many source rings from the origin. */
export const CAMERA_RINGS = 3;
export const FOCAL = 600;
/** Pointer slack of the hit test, px beyond a node's radius. */
export const HIT_SLACK = 4;
/** Room between a node and its label, px. */
export const LABEL_GAP = 4;
/** The auto-rotation, radians per second. */
export const AUTO_YAW = 0.15;
/** Radians per dragged px; the tilt stops short of straight up or down. */
export const DRAG_RADIANS = 0.01;
export const PITCH_LIMIT = 1.4;
export const ZOOM_MIN = 0.25;
export const ZOOM_MAX = 4;
/** The dash of a curated line. */
export const DASH: readonly number[] = [4, 3];
/** Labels in the theme's font family. */
export const LABEL_FONT = "12px '72', '72full', Arial, Helvetica, sans-serif";

const TURN = 2 * Math.PI;
const FAINT = 0.25;
const FULL = 0.9;
/** A point may come no nearer than this share of the camera distance (never at or behind the camera). */
const NEAREST = 0.1;

/** The drawn radius of a node at the centre plane: source 10, entity / query / function 6, field 3. */
export function radiusOf(kind: string): number {
    if (kind === "source") {
        return 10;
    }
    return kind === "field" || kind === "dimension" || kind === "measure" ? 3 : 6;
}

/** The source ring's radius for `count` sources. */
export function ringRadius(count: number): number {
    return Math.max(RING_MIN, RING_PER_SOURCE * count);
}

const ORIGIN: Vec = { x: 0, y: 0, z: 0 };
const UP: Vec = { x: 0, y: 1, z: 0 };

/** a + b * scale. */
function add(a: Vec, b: Vec, scale: number): Vec {
    return { x: a.x + b.x * scale, y: a.y + b.y * scale, z: a.z + b.z * scale };
}

/** e1 * c1 + e2 * c2. */
function mix(e1: Vec, c1: number, e2: Vec, c2: number): Vec {
    return add(add(ORIGIN, e1, c1), e2, c2);
}

function cross(a: Vec, b: Vec): Vec {
    return { x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z, z: a.x * b.y - a.y * b.x };
}

function unit(v: Vec, fallback: Vec): Vec {
    const length = Math.hypot(v.x, v.y, v.z);
    return length > 1e-9 ? { x: v.x / length, y: v.y / length, z: v.z / length } : fallback;
}

/** Where a ring's members sit: around `centre`, in the plane of `e1` and `e2` (orthonormal). */
interface Ring { centre: Vec; e1: Vec; e2: Vec; radius: number }

/** Every node of the model once, in its order. */
export function layout(model: GraphModel): Layout {
    const kindOf = new Map<string, DataNodeKind>();
    const groupOf = new Map<string, string>();
    /** The sources of each group, in the order groups first appear. */
    const sourcesOf = new Map<string, string[]>();
    for (const node of model.nodes) {
        if (kindOf.has(node.key)) {
            continue;
        }
        kindOf.set(node.key, node.kind);
        groupOf.set(node.key, node.group);
        const sources = sourcesOf.get(node.group) ?? [];
        sourcesOf.set(node.group, sources);
        if (node.kind === "source") {
            sources.push(node.key);
        }
    }
    const groups = Array.from(sourcesOf.keys());
    // A parent that is a source is the group's place itself, so only links
    // between two other nodes count; a source is always at its place.
    const parent = new Map<string, string>();
    for (const line of model.lines) {
        if (line.title === "contains" && line.from !== line.to && !parent.has(line.to)
            && kindOf.has(line.from) && kindOf.has(line.to)
            && kindOf.get(line.from) !== "source" && kindOf.get(line.to) !== "source") {
            parent.set(line.to, line.from);
        }
    }
    cutLoops(parent);

    const firstRing = new Map<string, string[]>();
    const children = new Map<string, string[]>();
    for (const key of kindOf.keys()) {
        if (kindOf.get(key) === "source") {
            continue;
        }
        const of = parent.get(key);
        const bucket = of === undefined ? firstRing : children;
        const owner = of === undefined ? groupOf.get(key) as string : of;
        const members = bucket.get(owner) ?? [];
        bucket.set(owner, members);
        members.push(key);
    }

    const ring = ringRadius(groups.length);
    const at = new Map<string, Vec>();
    /** The ring each placed non-source node sits on, for its own children's ring. */
    const sitsOn = new Map<string, Ring>();
    const queue: string[] = [];
    const place = (keys: string[], around: Ring) => {
        keys.forEach((key, index) => {
            const angle = (TURN * index) / keys.length;
            at.set(key, add(around.centre, mix(around.e1, Math.cos(angle), around.e2, Math.sin(angle)), around.radius));
            sitsOn.set(key, around);
            queue.push(key);
        });
    };
    groups.forEach((group, index) => {
        const angle = (TURN * index) / groups.length;
        const radial = { x: Math.cos(angle), y: 0, z: Math.sin(angle) };
        const tangent = { x: -Math.sin(angle), y: 0, z: Math.cos(angle) };
        const centre = add(ORIGIN, radial, ring);
        // The horizontal plane (tangent, radial) turned about the radial axis by the source's angle.
        const tilted = mix(tangent, Math.cos(angle), UP, Math.sin(angle));
        (sourcesOf.get(group) ?? []).forEach((key) => at.set(key, centre));
        place(firstRing.get(group) ?? [], { centre, e1: tilted, e2: radial, radius: CHILD_RING });
    });
    for (let next = 0; next < queue.length; next++) {
        const key = queue[next];
        const own = sitsOn.get(key) as Ring;
        const centre = at.get(key) as Vec;
        const normal = cross(own.e1, own.e2);
        // Upright on the parent's ring: through the parent's outward direction and that ring's normal.
        const outward = unit(add(centre, own.centre, -1), own.e1);
        place(children.get(key) ?? [], { centre, e1: outward, e2: normal, radius: LEAF_RING });
    }

    const points: Placed[] = [];
    for (const [key, kind] of kindOf) {
        const point = at.get(key) as Vec;
        points.push({ key, kind, x: point.x, y: point.y, z: point.z });
    }
    return { points, ring };
}

/** Drops the parent link that closes a loop, so every chain ends at a node on its source's ring. */
function cutLoops(parent: Map<string, string>): void {
    const settled = new Set<string>();
    for (const start of Array.from(parent.keys())) {
        const chain = new Set<string>();
        let key: string | undefined = start;
        while (key !== undefined && !settled.has(key)) {
            chain.add(key);
            const up = parent.get(key);
            if (up !== undefined && chain.has(up)) {
                parent.delete(key);
                break;
            }
            key = up;
        }
        chain.forEach((done) => settled.add(done));
    }
}

/**
 * The layout as seen through `view`: turned by yaw about the vertical axis,
 * then tilted by pitch, seen from `CAMERA_RINGS` rings in front of the
 * origin with focal length `FOCAL`. A radius is scaled by f / (f + z), z
 * the depth on the screen's scale (which is d / (d + depth) in world units),
 * so a node on the centre plane keeps its radius by kind; zoom scales all.
 */
export function project(placed: Layout, view: View3d): Projected[] {
    const camera = CAMERA_RINGS * placed.ring;
    const zoom = view.zoom > 0 && Number.isFinite(view.zoom) ? view.zoom : 1;
    const cosYaw = Math.cos(view.yaw || 0);
    const sinYaw = Math.sin(view.yaw || 0);
    const cosPitch = Math.cos(view.pitch || 0);
    const sinPitch = Math.sin(view.pitch || 0);
    const cx = view.width / 2;
    const cy = view.height / 2;
    return placed.points.map((p) => {
        const x = p.x * cosYaw - p.z * sinYaw;
        const turned = p.x * sinYaw + p.z * cosYaw;
        const y = p.y * cosPitch + turned * sinPitch;
        const depth = turned * cosPitch - p.y * sinPitch;
        const distance = Math.max(camera + depth, camera * NEAREST);
        const scale = (FOCAL / distance) * zoom;
        return {
            key: p.key, kind: p.kind, x: cx + x * scale, y: cy - y * scale, depth,
            radius: radiusOf(p.kind) * (camera / distance) * zoom
        };
    });
}

/** The node under (x, y): the nearest centre within its radius plus `HIT_SLACK`; at equal distance the nearer node. */
export function hitTest(projected: readonly Projected[], x: number, y: number): Projected | null {
    let best: Projected | null = null;
    let bestDistance = Infinity;
    for (const p of projected) {
        const distance = Math.hypot(p.x - x, p.y - y);
        if (distance > p.radius + HIT_SLACK) {
            continue;
        }
        if (distance < bestDistance || (distance === bestDistance && best !== null && p.depth < best.depth)) {
            best = p;
            bestDistance = distance;
        }
    }
    return best;
}

function colourOf(sourceKind: string, colours: Colours3d): string {
    if (sourceKind === "odata") {
        return colours.odata;
    }
    return sourceKind === "ina" ? colours.ina : colours.generic;
}

function farFirst(a: { depth: number }, b: { depth: number }): number {
    return b.depth - a.depth;
}

/**
 * What to paint, in order: the lines far to near (`contains` faint, the
 * others full, curated ones dashed), the nodes far to near in their source
 * kind's colour, then the labels: every source's name, and the hovered
 * node's name and kind last, so it is on top.
 */
export function drawList(model: GraphModel, view: View3d, colours: Colours3d): DrawItem[] {
    const projected = project(layout(model), view);
    const at = new Map<string, Projected>();
    projected.forEach((p) => at.set(p.key, p));
    const nodeOf = new Map<string, GraphModel["nodes"][number]>();
    model.nodes.forEach((node) => {
        if (!nodeOf.has(node.key)) {
            nodeOf.set(node.key, node);
        }
    });

    const lines: DrawLine[] = [];
    for (const line of model.lines) {
        const a = at.get(line.from);
        const b = at.get(line.to);
        if (!a || !b) {
            continue;
        }
        const containment = line.title === "contains";
        lines.push({
            type: "line", from: line.from, to: line.to, x1: a.x, y1: a.y, x2: b.x, y2: b.y, colour: colours.line,
            alpha: containment ? FAINT : FULL, width: containment ? 1 : 1.5, dashed: line.curated === true,
            depth: (a.depth + b.depth) / 2
        });
    }
    lines.sort(farFirst);

    const hover = view.hover ?? null;
    const nodes: DrawNode[] = projected.map((p) => ({
        type: "node", key: p.key, kind: p.kind, x: p.x, y: p.y, radius: p.radius,
        colour: colourOf(String(nodeOf.get(p.key)?.source_kind), colours), depth: p.depth, hover: p.key === hover,
        outline: colours.text
    }));
    nodes.sort(farFirst);

    const labels: DrawLabel[] = [];
    let hovered: DrawLabel | null = null;
    for (const node of nodes) {
        if (node.kind !== "source" && !node.hover) {
            continue;
        }
        const title = nodeOf.get(node.key)?.title ?? node.key;
        const label: DrawLabel = {
            type: "label", key: node.key, text: node.hover ? `${title} (${node.kind})` : title,
            x: node.x + node.radius + LABEL_GAP, y: node.y, colour: colours.text
        };
        if (node.hover) {
            hovered = label;
        } else {
            labels.push(label);
        }
    }
    if (hovered) {
        labels.push(hovered);
    }
    return [...lines, ...nodes, ...labels];
}

/** Paints `list` in its order; the context's state is put back afterwards. Clearing the canvas is the caller's. */
export function draw(ctx: Canvas2d, list: readonly DrawItem[]): void {
    ctx.save();
    ctx.font = LABEL_FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (const item of list) {
        if (item.type === "line") {
            ctx.globalAlpha = item.alpha;
            ctx.strokeStyle = item.colour;
            ctx.lineWidth = item.width;
            ctx.beginPath();
            ctx.moveTo(item.x1, item.y1);
            ctx.lineTo(item.x2, item.y2);
            ctx.setLineDash(item.dashed ? DASH.slice() : []);
            ctx.stroke();
        } else if (item.type === "node") {
            ctx.globalAlpha = 1;
            ctx.fillStyle = item.colour;
            ctx.beginPath();
            ctx.arc(item.x, item.y, item.radius, 0, TURN);
            ctx.fill();
            if (item.hover) {
                ctx.strokeStyle = item.outline;
                ctx.lineWidth = 2;
                ctx.setLineDash([]);
                ctx.stroke();
            }
        } else {
            ctx.globalAlpha = 1;
            ctx.fillStyle = item.colour;
            ctx.fillText(item.text, item.x, item.y);
        }
    }
    ctx.restore();
}

/** The view the 3D overview opens with: slightly from above, turning by itself. */
export function initialView(width: number, height: number): View3d {
    return { yaw: 0, pitch: 0.35, zoom: 1, width, height, touched: false, hover: null };
}

/** The slow auto-rotation: `AUTO_YAW` radians per second of `dt`, until the first drag. */
export function step(view: View3d, dt: number): View3d {
    if (view.touched || !(dt > 0) || !Number.isFinite(dt)) {
        return view;
    }
    return { ...view, yaw: (((view.yaw + AUTO_YAW * dt) % TURN) + TURN) % TURN };
}

/** A drag by (dx, dy) px: right turns, down looks more from above; it stops the auto-rotation. */
export function rotate(view: View3d, dx: number, dy: number): View3d {
    const pitch = view.pitch + (Number.isFinite(dy) ? dy : 0) * DRAG_RADIANS;
    return {
        ...view, yaw: view.yaw + (Number.isFinite(dx) ? dx : 0) * DRAG_RADIANS,
        pitch: Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, pitch)), touched: true
    };
}

/** A wheel turn by `deltaY` (as `WheelEvent.deltaY`): up zooms in, within `ZOOM_MIN`..`ZOOM_MAX`. */
export function zoomed(view: View3d, deltaY: number): View3d {
    if (!Number.isFinite(deltaY)) {
        return view;
    }
    const zoom = (view.zoom > 0 ? view.zoom : 1) * Math.exp(-deltaY * 0.001);
    return { ...view, zoom: Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoom)) };
}

export default {
    layout, project, hitTest, drawList, draw, step, rotate, zoomed, initialView, radiusOf, ringRadius
};
