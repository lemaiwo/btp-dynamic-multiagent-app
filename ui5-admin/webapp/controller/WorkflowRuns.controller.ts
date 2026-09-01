import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import type Event from "sap/ui/base/Event";
import type ColumnListItem from "sap/m/ColumnListItem";
import type { WorkflowRun } from "../service/types";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class WorkflowRuns extends BaseController {

    public formatter = formatter;

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "workflowRuns");
        this.getRouter().getRoute("workflowRuns")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const runs = await this.run(
            this.getAdminService().listWorkflowRuns({ limit: 50 }),
            "Could not load the workflow runs."
        );
        if (runs) {
            (this.getModel("workflowRuns") as JSONModel).setProperty("/items", runs);
        }
    }

    public onRefresh(): void {
        void this.load();
    }

    public onOpen(event: Event): void {
        const run = (event.getSource() as ColumnListItem)
            .getBindingContext("workflowRuns")?.getObject() as WorkflowRun;
        this.getRouter().navTo("workflowRunDetail", { runId: run.id });
    }
}
