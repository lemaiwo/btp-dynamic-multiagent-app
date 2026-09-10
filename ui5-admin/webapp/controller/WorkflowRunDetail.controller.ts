import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import { buildDefinitionGraph, decorateWithRun, FlowGraph } from "../model/processFlowGraph";
import type Event from "sap/ui/base/Event";
import type Select from "sap/m/Select";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { WorkflowItemRun, WorkflowStepRun } from "../service/types";

/** A `WorkflowItemRun` with its own step runs nested underneath, so the view
 * can bind `run>steps` as a relative aggregation inside the items list
 * without a second round trip. */
type ItemRunDisplay = WorkflowItemRun & { steps: WorkflowStepRun[] };

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class WorkflowRunDetail extends BaseController {

    public formatter = formatter;

    private runId = "";

    /** The undecorated definition graph for this run's workflow, kept so
     * switching items only re-runs the (pure) decoration. */
    private baseGraph: FlowGraph = { lanes: [], nodes: [] };

    private stepRuns: WorkflowStepRun[] = [];

    public onInit(): void {
        this.setModel(new JSONModel({ data: {}, preFanOutSteps: [], items: [] }), "run");
        this.setModel(new JSONModel({
            lanes: [], nodes: [], itemOptions: [], selectedItem: "", available: false
        }), "flow");
        this.getRouter().getRoute("workflowRunDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            this.runId = (event.getParameter("arguments") as { runId: string }).runId;
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const model = this.getModel("run") as JSONModel;
        model.setData({ data: {}, preFanOutSteps: [], items: [] });

        const detail = await this.run(
            this.getAdminService().getWorkflowRun(this.runId),
            "Could not load the workflow run."
        );
        if (!detail) {
            return;
        }

        // The tree is implied by foreign keys, not nesting: a step run's
        // item_run_id is null when it ran before the fan-out step (once for
        // the whole run), otherwise it belongs to the item it names.
        const preFanOutSteps = detail.steps.filter((step) => step.item_run_id === null);
        const items: ItemRunDisplay[] = detail.items.map((item) => ({
            ...item,
            steps: detail.steps.filter((step) => step.item_run_id === item.id)
        }));

        model.setData({ data: detail.run, preFanOutSteps, items });
        await this.loadFlow(detail.run.workflow_id, detail.items, detail.steps);
    }

    /**
     * Draws the run against its workflow's definition, so branches no item
     * entered are visible as untaken rather than absent.
     *
     * The definition is today's, not a snapshot taken when the run started —
     * a workflow edited since then draws its current shape. If it has been
     * deleted outright there is nothing to draw against, and the flow panel
     * says so instead of erroring: the step lists below already carry the
     * whole run.
     */
    private async loadFlow(
        workflowId: number, items: WorkflowItemRun[], steps: WorkflowStepRun[]
    ): Promise<void> {
        const flow = this.getModel("flow") as JSONModel;
        this.stepRuns = steps;
        let workflow;
        try {
            workflow = await this.getAdminService().getWorkflow(workflowId);
        } catch {
            this.baseGraph = { lanes: [], nodes: [] };
            flow.setData({ lanes: [], nodes: [], itemOptions: [], selectedItem: "", available: false });
            return;
        }

        this.baseGraph = buildDefinitionGraph(workflow.branches ?? [], workflow.steps ?? []);
        const itemOptions = items.map((item) => ({
            key: item.id,
            text: item.title ? `${item.title} (${item.item_key})` : item.item_key
        }));
        flow.setData({
            lanes: this.baseGraph.lanes,
            nodes: [],
            itemOptions,
            selectedItem: itemOptions.length ? itemOptions[0].key : "",
            available: true
        });
        this.applySelectedItem();
    }

    /** Recolours the graph for whichever item is picked. */
    private applySelectedItem(): void {
        const flow = this.getModel("flow") as JSONModel;
        const selected = (flow.getProperty("/selectedItem") as string) || null;
        const decorated = decorateWithRun(this.baseGraph, this.stepRuns, selected);
        flow.setProperty("/lanes", decorated.lanes);
        flow.setProperty("/nodes", decorated.nodes);
    }

    public onSelectItem(event: Event): void {
        const key = (event.getSource() as Select).getSelectedKey();
        (this.getModel("flow") as JSONModel).setProperty("/selectedItem", key);
        this.applySelectedItem();
    }

    public onBack(): void {
        this.getRouter().navTo("workflowRuns");
    }
}
