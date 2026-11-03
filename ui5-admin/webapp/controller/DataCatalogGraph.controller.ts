import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import dataCatalog, { DEFAULT_GRAPH_KINDS, NODE_KINDS, type GraphModel } from "../model/dataCatalog";
import type Event from "sap/ui/base/Event";
import type Graph from "sap/suite/ui/commons/networkgraph/Graph";
import type Node from "sap/suite/ui/commons/networkgraph/Node";

/** The height of the control's own toolbar, which sits over its drawing area. */
const TOOLBAR_HEIGHT = 48;

/**
 * The data catalogue as a graph: one group per source, coloured by its
 * kind, filtered by node kind, agent scope and a search term. A node opens
 * its source.
 *
 * @namespace com.agent.admin.controller
 */
export default class DataCatalogGraph extends BaseController {

    /** Answers that arrive after a newer request was sent are dropped. */
    private requestNumber = 0;

    public onInit(): void {
        this.setModel(new JSONModel({ groups: [], nodes: [], lines: [], capped: false }), "graph");
        this.setModel(new JSONModel({
            // Every kind but fields: they would shrink a real service to 10%.
            kinds: DEFAULT_GRAPH_KINDS.slice(), agent: "", q: "", agents: [], loadFailed: false, loadError: "",
            kindOptions: NODE_KINDS.map((kind) => ({ key: kind, text: this.text(`dcNodeKind_${kind}`) }))
        }), "graphView");
        this.getRouter().getRoute("dataCatalogGraph")?.attachPatternMatched(() => {
            void this.loadAgents();
            void this.load();
        });
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
                model = { groups: [], nodes: [], lines: [], capped: false };
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
            this.getRouter().navTo("dataCatalogDetail", { kind: data.source_kind, name: data.source_name });
        }
    }

    public onBack(): void {
        this.getRouter().navTo("dataCatalog");
    }
}
