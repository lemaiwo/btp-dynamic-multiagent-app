import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import { buildDefinitionGraph, decorateWithRun, FlowGraph } from "../model/processFlowGraph";
import Poller, { clockText, isLiveStatus, statusLine } from "../model/autoRefresh";
import type { PollOutcome } from "../model/autoRefresh";
import type Event from "sap/ui/base/Event";
import type Select from "sap/m/Select";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";
import type { WorkflowItemRun, WorkflowRunDetail as WorkflowRunDetailData, WorkflowStepRun } from "../service/types";

/** A `WorkflowItemRun` with its own step runs nested underneath, so the view
 * can bind `run>steps` as a relative aggregation inside the items list
 * without a second round trip. */
type ItemRunDisplay = WorkflowItemRun & { steps: WorkflowStepRun[] };

/**
 * @namespace com.agent.admin.controller
 */
export default class WorkflowRunDetail extends BaseController {

    public formatter = formatter;

    private runId = "";

    /** The undecorated definition graph for this run's workflow, kept so
     * switching items only re-runs the (pure) decoration. */
    private baseGraph: FlowGraph = { lanes: [], nodes: [] };

    private stepRuns: WorkflowStepRun[] = [];

    /** Re-fetches the run (items and steps included) while it is live; see
     * model/autoRefresh.ts. */
    private poller = new Poller(() => this.poll());
    private lastRefreshed = "";
    private lastPollFailed = false;

    public onInit(): void {
        this.setModel(new JSONModel({
            data: {}, preFanOutSteps: [], items: [],
            auto: { enabled: true, status: "" }
        }), "run");
        this.setModel(new JSONModel({
            lanes: [], nodes: [], itemOptions: [], selectedItem: "", available: false
        }), "flow");
        this.getRouter().getRoute("workflowRunDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            this.poller.stop();
            this.runId = (event.getParameter("arguments") as { runId: string }).runId;
            this.lastRefreshed = "";
            this.lastPollFailed = false;
            void this.load();
        });
        this.getRouter().attachRouteMatched((event: Router$RouteMatchedEvent) => {
            if (event.getParameter("name") !== "workflowRunDetail") {
                this.poller.stop();
            }
        });
    }

    public onExit(): void {
        this.poller.stop();
    }

    private load(): Promise<void> {
        return this.withBusy(async () => {
            const model = this.getModel("run") as JSONModel;
            model.setProperty("/data", {});
            model.setProperty("/preFanOutSteps", []);
            model.setProperty("/items", []);

            const detail = await this.run(
                this.getAdminService().getWorkflowRun(this.runId),
                "Could not load the workflow run."
            );
            if (!detail) {
                this.updateStatusLine();
                return;
            }
            this.applyRun(detail);
            await this.loadFlow(detail.run.workflow_id, detail.items, detail.steps);
            this.syncPolling();
        });
    }

    /** Puts a fetched run, its items and its steps into the model. */
    private applyRun(detail: WorkflowRunDetailData): void {
        const model = this.getModel("run") as JSONModel;
        // The tree is implied by foreign keys, not nesting: a step run's
        // item_run_id is null when it ran before the fan-out step (once for
        // the whole run), otherwise it belongs to the item it names.
        const preFanOutSteps = detail.steps.filter((step) => step.item_run_id === null);
        const items: ItemRunDisplay[] = detail.items.map((item) => ({
            ...item,
            steps: detail.steps.filter((step) => step.item_run_id === item.id)
        }));
        model.setProperty("/data", detail.run);
        model.setProperty("/preFanOutSteps", preFanOutSteps);
        model.setProperty("/items", items);
        this.lastRefreshed = clockText(new Date());
        this.updateStatusLine();
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
        flow.setData({
            lanes: this.baseGraph.lanes,
            nodes: [],
            itemOptions: WorkflowRunDetail.itemOptions(items),
            selectedItem: items.length ? items[0].id : "",
            available: true
        });
        this.applySelectedItem();
    }

    private static itemOptions(items: WorkflowItemRun[]): { key: string; text: string }[] {
        return items.map((item) => ({
            key: item.id,
            text: item.title ? `${item.title} (${item.item_key})` : item.item_key
        }));
    }

    /**
     * A poll's refresh of the flow: the definition graph is kept (the
     * workflow itself is not re-fetched every few seconds), the item picker
     * gains items the fan-out step has produced since, and the colours are
     * recomputed from the new step runs. The picked item stays picked.
     */
    private refreshFlow(items: WorkflowItemRun[], steps: WorkflowStepRun[]): void {
        const flow = this.getModel("flow") as JSONModel;
        this.stepRuns = steps;
        if (!flow.getProperty("/available")) {
            return;
        }
        const options = WorkflowRunDetail.itemOptions(items);
        const selected = flow.getProperty("/selectedItem") as string;
        flow.setProperty("/itemOptions", options);
        if (!options.some((o) => o.key === selected)) {
            flow.setProperty("/selectedItem", options.length ? options[0].key : "");
        }
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

    // --- refresh ---

    /** One poll: fetch, apply, and tell the poller whether to continue.
     * No busy indicator, so the page does not flicker every few seconds. */
    private async poll(): Promise<PollOutcome> {
        const id = this.runId;
        try {
            const detail = await this.getAdminService().getWorkflowRun(id);
            if (id !== this.runId) {
                return "done";
            }
            this.lastPollFailed = false;
            this.applyRun(detail);
            this.refreshFlow(detail.items, detail.steps);
            return isLiveStatus(detail.run.status) ? "live" : "done";
        } catch {
            if (!this.lastPollFailed) {
                MessageToast.show(this.text("autoRefreshFailed"));
            }
            this.lastPollFailed = true;
            this.updateStatusLine();
            return "failed";
        }
    }

    /** Starts or stops the poller from the toggle and the run's status. */
    private syncPolling(): void {
        const model = this.getModel("run") as JSONModel;
        const enabled = !!model.getProperty("/auto/enabled");
        const live = isLiveStatus((model.getProperty("/data/status") as string) || "");
        if (enabled && live) {
            if (!this.poller.isActive()) {
                this.poller.start("live");
            }
        } else {
            this.poller.stop();
        }
        this.updateStatusLine();
    }

    private updateStatusLine(): void {
        const model = this.getModel("run") as JSONModel;
        model.setProperty("/auto/status", statusLine({
            enabled: !!model.getProperty("/auto/enabled"),
            live: isLiveStatus((model.getProperty("/data/status") as string) || ""),
            failed: this.lastPollFailed,
            lastRefreshed: this.lastRefreshed,
            intervals: this.poller.intervals
        }));
    }

    public onToggleAutoRefresh(): void {
        // The two-way binding has already flipped /auto/enabled.
        this.syncPolling();
    }

    public onRefresh(): void {
        void this.load();
    }

    public onBack(): void {
        this.poller.stop();
        this.getRouter().navTo("workflowRuns");
    }
}
