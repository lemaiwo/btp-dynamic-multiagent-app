import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
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

    public onInit(): void {
        this.setModel(new JSONModel({ data: {}, preFanOutSteps: [], items: [] }), "run");
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
    }

    public onBack(): void {
        this.getRouter().navTo("workflowRuns");
    }
}
