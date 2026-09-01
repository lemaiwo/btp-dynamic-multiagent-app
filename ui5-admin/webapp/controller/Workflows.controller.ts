import JSONModel from "sap/ui/model/json/JSONModel";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import type Event from "sap/ui/base/Event";
import type { SearchField$LiveChangeEvent } from "sap/m/SearchField";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Control from "sap/ui/core/Control";
import type Table from "sap/m/Table";
import type ListBinding from "sap/ui/model/ListBinding";
import type { Workflow } from "../service/types";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class Workflows extends BaseController {

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "workflows");
        this.getRouter().getRoute("workflows")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const workflows = await this.run(
            this.getAdminService().listWorkflows(),
            "Could not load the workflows."
        );
        if (workflows) {
            (this.getModel("workflows") as JSONModel).setProperty("/items", workflows);
        }
    }

    public onSearch(event: SearchField$LiveChangeEvent): void {
        const query = event.getParameter("newValue") || "";
        const filters = query
            ? [new Filter({
                filters: [
                    new Filter("name", FilterOperator.Contains, query),
                    new Filter("description", FilterOperator.Contains, query)
                ],
                and: false
            })]
            : [];
        const table = this.byId("workflowsTable") as Table;
        (table.getBinding("items") as ListBinding).filter(filters);
    }

    public onCreate(): void {
        this.getRouter().navTo("workflowDetail", { workflowId: "new" });
    }

    public onOpen(event: Event): void {
        const workflow = (event.getSource() as ColumnListItem)
            .getBindingContext("workflows")?.getObject() as Workflow;
        this.getRouter().navTo("workflowDetail", { workflowId: String(workflow.id) });
    }

    public async onRunNow(event: Event): Promise<void> {
        const workflow = (event.getSource() as Control)
            .getBindingContext("workflows")?.getObject() as Workflow;
        const started = await this.run(
            this.getAdminService().runWorkflowNow(workflow.id),
            `Could not start a run for "${workflow.name}".`
        );
        if (started) {
            MessageToast.show(this.text("workflowRunStarted").replace("{0}", started.run_id));
        }
    }

    public onDelete(event: Event): void {
        const workflow = (event.getSource() as Control)
            .getBindingContext("workflows")?.getObject() as Workflow;

        MessageBox.confirm(this.text("deleteWorkflowConfirm").replace("{0}", workflow.name), {
            title: this.text("delete"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.doDelete(workflow);
                }
            }
        });
    }

    private async doDelete(workflow: Workflow): Promise<void> {
        const ok = await this.runOk(
            this.getAdminService().deleteWorkflow(workflow.id),
            `Could not delete the workflow "${workflow.name}".`
        );
        if (ok) {
            MessageToast.show(this.text("workflowDeleted"));
            void this.load();
        }
    }
}
