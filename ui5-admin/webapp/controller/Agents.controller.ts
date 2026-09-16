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
 * @namespace com.agent.admin.controller
 */
export default class Agents extends BaseController {

    public formatter = formatter;

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "agents");
        this.setModel(new JSONModel({ problems: [], summary: "" }), "credhealth");
        this.getRouter().getRoute("agents")?.attachPatternMatched(() => {
            void this.load();
        });
        this.getRouter().getRoute("root")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private load(): Promise<void> {
        return this.withBusy(async () => {
            const agents = await this.run(
                this.getAdminService().listAgents(),
                "Could not load the agents."
            );
            if (agents) {
                (this.getModel("agents") as JSONModel).setProperty("/items", agents);
            }
            await this.loadCredentialHealth();
        });
    }

    /**
     * Refresh the credential warning strip.
     *
     * Deliberately silent on failure: this is a health indicator, not the
     * page's purpose. Raising a dialog because the *check* failed would be
     * more disruptive than the thing it warns about, and it would fire on
     * every transient blip. A failed check leaves the strip hidden.
     */
    private async loadCredentialHealth(): Promise<void> {
        const model = this.getModel("credhealth") as JSONModel;
        try {
            const health = await this.getAdminService().credentialHealth();
            const problems = health.problems ?? [];
            model.setProperty("/problems", problems);
            model.setProperty("/summary", Agents.summarise(problems));
        } catch {
            model.setProperty("/problems", []);
            model.setProperty("/summary", "");
        }
    }

    /**
     * One sentence naming what is broken and what it costs.
     *
     * Names the agents rather than counting them: "1 agent" sends the reader
     * hunting, and there are rarely enough of these to be worth truncating.
     */
    private static summarise(problems: { agent: string; token_state: string }[]): string {
        if (problems.length === 0) {
            return "";
        }
        const names = [...new Set(problems.map((p) => p.agent))];
        const list = names.length === 1
            ? `"${names[0]}"`
            : names.map((n) => `"${n}"`).join(", ");
        const expired = problems.some((p) => p.token_state === "expired");
        const why = expired
            ? "its stored credential has expired and cannot be refreshed"
            : "no credential is stored for its run-as principal";
        return `Scheduled runs of ${list} will fail: ${why}. `
            + "Someone must sign in again for that principal.";
    }

    /** Open the first affected agent, where the credential panel lives. */
    public onOpenCredentialProblem(): void {
        const problems = (this.getModel("credhealth") as JSONModel)
            .getProperty("/problems") as { agent: string }[];
        const first = problems?.[0];
        if (!first) {
            return;
        }
        const agents = (this.getModel("agents") as JSONModel)
            .getProperty("/items") as Agent[];
        const match = agents?.find((a) => a.name === first.agent);
        if (match) {
            this.getRouter().navTo("agentDetail", { agentId: String(match.id) });
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
