import JSONModel from "sap/ui/model/json/JSONModel";
import Parameters from "sap/ui/core/theming/Parameters";
import ResizeHandler from "sap/ui/core/ResizeHandler";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import dataCatalog, { DEFAULT_GRAPH_KINDS, NODE_KINDS, type GraphModel } from "../model/dataCatalog";
import {
    THEME_COLOURS, draw, drawList, hitTest, initialView, layout, project, rotate, step, themeColours, zoomed,
    type Colours3d, type Layout, type Projected, type View3d
} from "../model/graph3d";
import type Event from "sap/ui/base/Event";
import type HTML from "sap/ui/core/HTML";
import type SegmentedButton from "sap/m/SegmentedButton";
import type Graph from "sap/suite/ui/commons/networkgraph/Graph";
import type Node from "sap/suite/ui/commons/networkgraph/Node";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";

/** The height of the control's own toolbar, which sits over its drawing area. */
const TOOLBAR_HEIGHT = 48;
const ROUTE = "dataCatalogGraph";
/** A press that moves further than this, px, is a drag: it turns the view and opens nothing. */
const DRAG_PX = 3;
/** The longest step of the auto-rotation, s: a tab that was in the background does not jump. */
const MAX_STEP_S = 0.1;

function emptyGraph(): GraphModel {
    return { groups: [], nodes: [], lines: [], capped: false };
}

/**
 * The data catalogue as a graph: one group per source, coloured by its
 * kind, filtered by node kind, agent scope and a search term. A node opens
 * its source. 2D is the network graph; 3D the same answer on a canvas,
 * drawn with `model/graph3d` (this controller only sizes the canvas, keeps
 * the view and forwards the pointer to it).
 *
 * @namespace com.agent.admin.controller
 */
export default class DataCatalogGraph extends BaseController {

    /** Answers that arrive after a newer request was sent are dropped. */
    private requestNumber = 0;
    /** The graph as shown, and its 3D layout, made once per answer. */
    private shown: GraphModel = emptyGraph();
    private placed: Layout = layout(this.shown);
    private view3d: View3d = initialView(0, 0);
    private colours: Colours3d = themeColours(undefined);
    /** The canvas the listeners are on, and its resize registration. */
    private canvas: HTMLCanvasElement | null = null;
    private resizeId: string | null = null;
    /** The pending animation frame of the auto-rotation. */
    private frame: number | null = null;
    /** The page's route is the current one: nothing turns behind another page. */
    private onRoute = false;
    /** The pressed pointer and where it was last. */
    private press: { id: number; x: number; y: number } | null = null;
    /** How far the last press moved, px: the click that ends a drag opens nothing. */
    private pressMoved = 0;
    private readonly canvasListeners: [string, EventListener, AddEventListenerOptions?][] = [
        ["pointerdown", (event) => this.pointerDown(event as PointerEvent)],
        ["pointermove", (event) => this.pointerMove(event as PointerEvent)],
        ["pointerup", (event) => this.pointerUp(event as PointerEvent)],
        ["pointercancel", (event) => this.pointerUp(event as PointerEvent)],
        ["pointerleave", () => this.pointerLeave()],
        ["wheel", (event) => this.wheel(event as WheelEvent), { passive: false }],
        ["click", (event) => this.canvasClick(event as MouseEvent)]
    ];

    public onInit(): void {
        this.setModel(new JSONModel(emptyGraph()), "graph");
        this.setModel(new JSONModel({
            // Every kind but fields: they would shrink a real service to 10%.
            mode: "2d", kinds: DEFAULT_GRAPH_KINDS.slice(), agent: "", q: "", agents: [], loadFailed: false, loadError: "",
            kindOptions: NODE_KINDS.map((kind) => ({ key: kind, text: this.text(`dcNodeKind_${kind}`) }))
        }), "graphView");
        this.getRouter().getRoute(ROUTE)?.attachPatternMatched(() => {
            this.onRoute = true;
            void this.loadAgents();
            void this.load();
            this.startTurning();
        });
        this.getRouter().attachRouteMatched(this.onAnyRouteMatched, this);
        // `routeMatched` does not fire for an address that matches no route
        // (the not-found page, see App.controller.ts).
        this.getRouter().attachBypassed(this.onLeft, this);
    }

    public onExit(): void {
        this.stopTurning();
        this.unwire();
        this.getRouter().detachRouteMatched(this.onAnyRouteMatched, this);
        this.getRouter().detachBypassed(this.onLeft, this);
    }

    /** Leaving by any route stops the auto-rotation. */
    private onAnyRouteMatched(event: Router$RouteMatchedEvent): void {
        if (event.getParameter("name") !== ROUTE) {
            this.onLeft();
        }
    }

    /** Another page is shown: nothing turns behind it. */
    private onLeft(): void {
        this.onRoute = false;
        this.stopTurning();
    }

    /** A method, not a getter: UI5's extend reads getters before there is a view. */
    private viewModel(): JSONModel {
        return this.getModel("graphView") as JSONModel;
    }

    private async loadAgents(): Promise<void> {
        let names: string[] = [];
        try {
            names = (await this.getAdminService().listAgents()).map((agent) => agent.name);
        } catch {
            names = [];
        }
        this.viewModel().setProperty("/agents", [{ key: "", text: this.text("dcGraphAllAgents") }]
            .concat(names.map((name) => ({ key: name, text: name }))));
    }

    /** Reads the graph for the filters as they are set; resolves when it is shown. */
    public load(): Promise<void> {
        const kinds = (this.viewModel().getProperty("/kinds") as string[]) ?? [];
        const query = dataCatalog.graphQuery({
            agent: this.viewModel().getProperty("/agent") as string,
            kinds,
            q: this.viewModel().getProperty("/q") as string
        });
        const number = ++this.requestNumber;
        return this.withBusy(async () => {
            let model: GraphModel;
            try {
                const answer = await this.getAdminService().getDataGraph(query);
                // The kinds are applied here as well: the graph never shows a
                // node of a kind that is not picked, whatever the server did.
                model = dataCatalog.graphModel(answer, kinds);
                this.viewModel().setProperty("/loadFailed", false);
            } catch (error) {
                if (number !== this.requestNumber) {
                    return;
                }
                model = emptyGraph();
                const reason = ErrorHandler.messageFor(error, "");
                this.viewModel().setProperty("/loadError", reason
                    ? this.text("dcGraphLoadFailedReason", [reason]) : this.text("dcGraphLoadFailed"));
                this.viewModel().setProperty("/loadFailed", true);
                const kind = ErrorHandler.classify(error);
                if (kind === "session" || kind === "forbidden") {
                    ErrorHandler.handle(error);
                }
            }
            if (number === this.requestNumber) {
                (this.getModel("graph") as JSONModel).setData(model);
                this.show3d(model);
            }
        });
    }

    /**
     * After each layout: every source in view, so a source that would lie
     * below the fold is not taken for a missing one. Measured from the groups
     * and nodes as drawn, at the zoom level they are drawn with.
     */
    public onGraphReady(): void {
        const graph = this.byId("dcGraph") as Graph;
        const boxes = [...graph.getGroups(), ...graph.getNodes()]
            .map((element) => element.getDomRef()?.getBoundingClientRect())
            .filter((box): box is DOMRect => !!box && box.width > 0);
        const view = graph.getDomRef()?.getBoundingClientRect();
        if (!boxes.length || !view) {
            return;
        }
        const zoom = graph.getCurrentZoomLevel() || 1;
        const width = Math.max(...boxes.map((box) => box.right)) - Math.min(...boxes.map((box) => box.left));
        const height = Math.max(...boxes.map((box) => box.bottom)) - Math.min(...boxes.map((box) => box.top));
        const fit = dataCatalog.fitZoom(
            { width: width / zoom, height: height / zoom },
            { width: view.width, height: view.height - TOOLBAR_HEIGHT },
            graph.getZoomLevelMilestones());
        if (fit !== zoom) {
            graph.zoom({ zoomLevel: fit });
        }
    }

    public onFilterChange(): void {
        void this.load();
    }

    /** Opens the node's source instead of the graph's own detail popover. */
    public onNodePress(event: Event): void {
        event.preventDefault();
        const node = event.getSource() as Node;
        const data = node.getBindingContext("graph")?.getObject() as GraphModel["nodes"][number] | undefined;
        if (data) {
            this.openSource(data);
        }
    }

    public onBack(): void {
        this.getRouter().navTo("dataCatalog");
    }

    // --- the 3D view ---------------------------------------------------------

    /** 2D or 3D; the visible bindings swap the network graph and the canvas. */
    public onModeChange(event: Event): void {
        const key = (event.getSource() as SegmentedButton).getSelectedKey();
        this.viewModel().setProperty("/mode", key === "3d" ? "3d" : "2d");
        if (this.is3d()) {
            this.startTurning();
        } else {
            this.stopTurning();
        }
    }

    /** The canvas was (re)rendered: listeners on it, its pixels to its box, the theme's colours, drawn. */
    public onCanvasRendered(): void {
        const element = (this.byId("dcGraph3d") as HTML).getDomRef();
        if (!(element instanceof HTMLCanvasElement)) {
            return;
        }
        this.wire(element);
        this.readColours();
        this.render();
        this.startTurning();
    }

    private is3d(): boolean {
        return this.viewModel().getProperty("/mode") === "3d";
    }

    private openSource(node: GraphModel["nodes"][number]): void {
        this.getRouter().navTo("dataCatalogDetail", { kind: node.source_kind, name: node.source_name });
    }

    /** A new answer: laid out once here, drawn per frame from that layout. */
    private show3d(model: GraphModel): void {
        this.shown = model;
        this.placed = layout(model);
        this.view3d = { ...this.view3d, hover: null };
        if (this.canvas) {
            this.canvas.style.cursor = "";
        }
        this.render();
    }

    /** The three kind colours of the legend, the line and the text colour, from the theme. */
    private readColours(): void {
        const apply = (values: unknown): void => {
            this.colours = themeColours(values);
            this.render();
        };
        const names = (Object.keys(THEME_COLOURS) as (keyof Colours3d)[]).map((colour) => THEME_COLOURS[colour][0]);
        const now = Parameters.get({ name: names, scopeElement: this.getView(), callback: apply });
        if (now !== undefined) {
            apply(now);
        }
    }

    private wire(canvas: HTMLCanvasElement): void {
        if (canvas === this.canvas) {
            return;
        }
        this.unwire();
        this.canvas = canvas;
        this.canvasListeners.forEach(([type, listener, options]) => canvas.addEventListener(type, listener, options));
        this.resizeId = ResizeHandler.register(canvas, () => this.render());
    }

    private unwire(): void {
        const canvas = this.canvas;
        if (!canvas) {
            return;
        }
        this.canvasListeners.forEach(([type, listener]) => canvas.removeEventListener(type, listener));
        if (this.resizeId) {
            ResizeHandler.deregister(this.resizeId);
            this.resizeId = null;
        }
        this.canvas = null;
    }

    /** Paints the view on the canvas, its pixels sized to its box at the screen's pixel ratio. */
    private render(): void {
        const canvas = this.canvas;
        const context = canvas?.getContext("2d");
        if (!canvas || !context || !this.is3d() || !canvas.clientWidth || !canvas.clientHeight) {
            return;
        }
        const ratio = window.devicePixelRatio || 1;
        const width = Math.round(canvas.clientWidth * ratio);
        const height = Math.round(canvas.clientHeight * ratio);
        if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
        }
        if (this.view3d.width !== canvas.clientWidth || this.view3d.height !== canvas.clientHeight) {
            this.view3d = { ...this.view3d, width: canvas.clientWidth, height: canvas.clientHeight };
        }
        context.setTransform(1, 0, 0, 1, 0, 0);
        context.clearRect(0, 0, width, height);
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        draw(context, drawList(this.shown, this.view3d, this.colours, this.placed));
    }

    /** The auto-rotation turns while the 3D view is on screen, until the first drag. */
    private turns(): boolean {
        return this.onRoute && this.is3d() && !this.view3d.touched && this.canvas !== null;
    }

    private startTurning(): void {
        if (this.frame !== null || !this.turns()) {
            return;
        }
        let last: number | null = null;
        const tick = (now: number): void => {
            this.frame = null;
            if (!this.turns()) {
                return;
            }
            if (last !== null) {
                this.view3d = step(this.view3d, Math.min((now - last) / 1000, MAX_STEP_S));
            }
            last = now;
            this.render();
            this.frame = window.requestAnimationFrame(tick);
        };
        this.frame = window.requestAnimationFrame(tick);
    }

    private stopTurning(): void {
        if (this.frame !== null) {
            window.cancelAnimationFrame(this.frame);
            this.frame = null;
        }
    }

    /** Where each node is drawn now, CSS px from the canvas's top left. */
    private nodesOnScreen(): Projected[] {
        return project(this.placed, this.view3d);
    }

    /** The node under the pointer of `event`, if any. */
    private under(event: MouseEvent): Projected | null {
        const box = this.canvas?.getBoundingClientRect();
        return box ? hitTest(this.nodesOnScreen(), event.clientX - box.left, event.clientY - box.top) : null;
    }

    private pointerDown(event: PointerEvent): void {
        if (event.button !== 0) {
            return;
        }
        this.press = { id: event.pointerId, x: event.clientX, y: event.clientY };
        this.pressMoved = 0;
        try {
            this.canvas?.setPointerCapture(event.pointerId);
        } catch {
            // A pointer the browser has no record of (a synthetic one) cannot be captured.
        }
    }

    /** A drag turns the view; without a press the pointer hovers. */
    private pointerMove(event: PointerEvent): void {
        const press = this.press;
        if (!press || event.pointerId !== press.id) {
            this.hover(this.under(event));
            return;
        }
        const dx = event.clientX - press.x;
        const dy = event.clientY - press.y;
        press.x = event.clientX;
        press.y = event.clientY;
        this.pressMoved += Math.abs(dx) + Math.abs(dy);
        if (this.pressMoved > DRAG_PX) {
            this.view3d = rotate(this.view3d, dx, dy);
            this.stopTurning();
            this.render();
        }
    }

    private pointerUp(event: PointerEvent): void {
        if (!this.press || event.pointerId !== this.press.id) {
            return;
        }
        this.press = null;
        try {
            this.canvas?.releasePointerCapture(event.pointerId);
        } catch {
            // As at the press: nothing was captured.
        }
    }

    private pointerLeave(): void {
        if (!this.press) {
            this.hover(null);
        }
    }

    /** The hovered node is drawn with its name and kind; the pointer says it can be pressed. */
    private hover(hit: Projected | null): void {
        const key = hit ? hit.key : null;
        if (this.canvas) {
            this.canvas.style.cursor = key ? "pointer" : "";
        }
        if (key !== (this.view3d.hover ?? null)) {
            this.view3d = { ...this.view3d, hover: key };
            this.render();
        }
    }

    private wheel(event: WheelEvent): void {
        event.preventDefault();
        this.view3d = zoomed(this.view3d, event.deltaY);
        this.render();
    }

    /** A click on a node opens its source, as a node of the network graph does. */
    private canvasClick(event: MouseEvent): void {
        if (this.pressMoved > DRAG_PX) {
            return;
        }
        const hit = this.under(event);
        const node = hit ? this.shown.nodes.filter((one) => one.key === hit.key)[0] : undefined;
        if (node) {
            this.openSource(node);
        }
    }
}
