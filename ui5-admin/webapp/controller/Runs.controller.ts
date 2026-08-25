import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import type Event from "sap/ui/base/Event";
import type ColumnListItem from "sap/m/ColumnListItem";
import type { JobRun } from "../service/types";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class Runs extends BaseController {

    public formatter = formatter;

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "runs");
        this.getRouter().getRoute("runs")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const runs = await this.run(
            this.getAdminService().listRuns({ limit: 50 }),
            "Could not load the job runs."
        );
        if (runs) {
            (this.getModel("runs") as JSONModel).setProperty("/items", runs);
        }
    }

    public onRefresh(): void {
        void this.load();
    }

    public onOpen(event: Event): void {
        const run = (event.getSource() as ColumnListItem)
            .getBindingContext("runs")?.getObject() as JobRun;
        this.getRouter().navTo("runDetail", { runId: run.id });
    }
}
