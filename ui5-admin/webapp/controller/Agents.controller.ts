import JSONModel from "sap/ui/model/json/JSONModel";
import Filter from "sap/ui/model/Filter";
import FilterOperator from "sap/ui/model/FilterOperator";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import formatter from "../model/formatter";
import type Event from "sap/ui/base/Event";
import type { SearchField$LiveChangeEvent } from "sap/m/SearchField";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Control from "sap/ui/core/Control";
import type Table from "sap/m/Table";
import type ListBinding from "sap/ui/model/ListBinding";
import type { Agent } from "../service/types";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class Agents extends BaseController {

    public formatter = formatter;

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "agents");
        this.getRouter().getRoute("agents")?.attachPatternMatched(() => {
            void this.load();
        });
        this.getRouter().getRoute("root")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const agents = await this.run(
            this.getAdminService().listAgents(),
            "Could not load the agents."
        );
        if (agents) {
            (this.getModel("agents") as JSONModel).setProperty("/items", agents);
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
        const table = this.byId("agentsTable") as Table;
        (table.getBinding("items") as ListBinding).filter(filters);
    }

    public onCreate(): void {
        this.getRouter().navTo("agentDetail", { agentId: "new" });
    }

    public onOpen(event: Event): void {
        const agent = (event.getSource() as ColumnListItem)
            .getBindingContext("agents")?.getObject() as Agent;
        this.getRouter().navTo("agentDetail", { agentId: String(agent.id) });
    }

    public async onRunNow(event: Event): Promise<void> {
        const agent = (event.getSource() as Control)
            .getBindingContext("agents")?.getObject() as Agent;
        const started = await this.run(
            this.getAdminService().runNow(agent.id),
            `Could not start a run for "${agent.name}".`
        );
        if (started) {
            MessageToast.show(this.text("runStarted").replace("{0}", started.run_id));
            this.getRouter().navTo("runDetail", { runId: started.run_id });
        }
    }

    public onDelete(event: Event): void {
        const agent = (event.getSource() as Control)
            .getBindingContext("agents")?.getObject() as Agent;

        MessageBox.confirm(this.text("deleteAgentConfirm").replace("{0}", agent.name), {
            title: this.text("delete"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.doDelete(agent);
                }
            }
        });
    }

    private async doDelete(agent: Agent): Promise<void> {
        const ok = await this.runOk(
            this.getAdminService().deleteAgent(agent.id),
            `Could not delete the agent "${agent.name}".`
        );
        if (ok) {
            MessageToast.show(this.text("agentDeleted"));
            void this.load();
        }
    }
}
