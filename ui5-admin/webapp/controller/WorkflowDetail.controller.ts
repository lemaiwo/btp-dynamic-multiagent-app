import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import { ValueState } from "sap/ui/core/library";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import type Event from "sap/ui/base/Event";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type Control from "sap/ui/core/Control";
import type {
    Agent, WorkflowBranch, WorkflowInput, WorkflowStep
} from "../service/types";

const EMPTY_WORKFLOW: WorkflowInput = {
    name: "",
    description: "",
    api_slug: "",
    run_as_principal: "",
    run_timeout_seconds: 1800,
    skip_seen_items: true,
    max_parallel_items: 1,
    on_unknown_branch: "fail",
    enabled: true,
    branches: [],
    steps: []
};

/** One entry of a row `<Select>`'s items: a real choice, or a disabled
 * placeholder carrying a stored value the row's own list no longer offers. */
interface RowOption {
    key: string;
    text: string;
    enabled: boolean;
}

/** A step as held in the `/data/steps` model array: the wire shape plus the
 * two per-row option lists its Selects bind to. Recomputed by
 * `refreshStepOptions()` whenever the agent list is fetched or the branch
 * rows change, so a step whose stored agent/branch is no longer valid stays
 * visible and selected -- never silently swapped for whatever a `<Select>`
 * with no matching key would otherwise fall back to. Stripped back out to a
 * plain `WorkflowStep` by `collectSteps()` before every save. */
interface UiStep extends WorkflowStep {
    agentOptions: RowOption[];
    branchOptions: RowOption[];
}

interface UiWorkflowData extends Omit<WorkflowInput, "steps"> {
    steps: UiStep[];
}

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class WorkflowDetail extends BaseController {

    private workflowId?: number;

    public onInit(): void {
        this.setModel(new JSONModel({
            title: "",
            data: JSON.parse(JSON.stringify(EMPTY_WORKFLOW)) as UiWorkflowData,
            availableAgents: [],
            errors: {}
        }), "workflow");

        this.getRouter().getRoute("workflowDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const id = (event.getParameter("arguments") as { workflowId: string }).workflowId;
            void this.load(id);
        });
    }

    private async load(id: string): Promise<void> {
        const model = this.getModel("workflow") as JSONModel;
        model.setProperty("/errors", {});

        // Fetched before either branch below returns: a step's agent <Select>
        // needs the full agent list (disabled agents included -- a disabled
        // agent is still a real, nameable choice that the server alone
        // rejects, exactly as the missing-agent case below is left to the
        // server) whether the workflow is new or existing.
        const agents = await this.run(
            this.getAdminService().listAgents(),
            this.text("workflowLoadAgentsFailed")
        );
        model.setProperty("/availableAgents", agents ?? []);

        if (id === "new") {
            this.workflowId = undefined;
            model.setProperty("/data", JSON.parse(JSON.stringify(EMPTY_WORKFLOW)) as UiWorkflowData);
            model.setProperty("/title", this.text("newWorkflow"));
            this.refreshStepOptions();
            return;
        }

        this.workflowId = Number(id);
        const workflow = await this.run(
            this.getAdminService().getWorkflow(this.workflowId),
            this.text("workflowLoadFailed")
        );
        if (!workflow) {
            return;
        }
        // Copy only the input fields; id/created_at/updated_at must not be
        // POSTed back. branches/steps are copied as plain objects -- their
        // per-row option lists are filled in by refreshStepOptions() below,
        // not carried from the server.
        model.setProperty("/data", {
            name: workflow.name,
            description: workflow.description,
            api_slug: workflow.api_slug ?? "",
            run_as_principal: workflow.run_as_principal ?? "",
            run_timeout_seconds: workflow.run_timeout_seconds ?? 1800,
            skip_seen_items: workflow.skip_seen_items,
            max_parallel_items: workflow.max_parallel_items,
            on_unknown_branch: workflow.on_unknown_branch,
            enabled: workflow.enabled,
            branches: (workflow.branches ?? []).map((b) => ({ ...b })),
            steps: (workflow.steps ?? []).map((s) => ({ ...s }))
        } as UiWorkflowData);
        model.setProperty("/title", workflow.name);
        this.refreshStepOptions();
    }

    // --- Per-row option lists ----------------------------------------------

    /**
     * The agent choices for one step's `<Select>`.
     *
     * If `agentName` names no agent in `/availableAgents`, it is prepended as
     * a disabled entry carrying its own name rather than left out: a `<Select>`
     * whose `selectedKey` matches nothing picks the first item and reports it
     * as selected, which would silently retarget the step at a different
     * agent, unattended, under a different principal, on save. See
     * AgentDetail.controller.ts's `buildModelOptions` for the same pattern
     * applied to a stored model override.
     */
    private buildAgentOptions(agentName: string): RowOption[] {
        const agents = (this.getModel("workflow") as JSONModel).getProperty("/availableAgents") as Agent[];
        const options: RowOption[] = agents.map((a) => ({ key: a.name, text: a.name, enabled: true }));
        if (agentName && !agents.some((a) => a.name === agentName)) {
            options.unshift({
                key: agentName, text: this.text("stepAgentMissing", [agentName]), enabled: false
            });
        }
        return options;
    }

    /**
     * The branch choices for one step's `<Select>`: the main line, plus every
     * branch row *currently in the model* -- not a snapshot taken at load --
     * so a branch added moments ago is selectable without saving first.
     * Blank-keyed (not yet named) branch rows are omitted, same as the
     * classic admin's `addWorkflowStepRow`.
     *
     * `branchKey` is carried forward as a disabled placeholder when it names
     * no current branch, for the same reason `buildAgentOptions` does: a
     * `<Select>` with no matching key would otherwise default to the first
     * item -- here, silently turning a branch step into a main-line one.
     */
    private buildBranchOptionsForStep(branchKey: string | null): RowOption[] {
        const branches = (this.getModel("workflow") as JSONModel).getProperty("/data/branches") as WorkflowBranch[];
        const keys = branches.map((b) => (b.key || "").trim()).filter(Boolean);
        const options: RowOption[] = [
            { key: "", text: this.text("stepMainLine"), enabled: true },
            ...keys.map((k) => ({ key: k, text: k, enabled: true }))
        ];
        const normalized = (branchKey || "").trim();
        if (normalized && !keys.includes(normalized)) {
            options.push({
                key: normalized, text: this.text("stepBranchMissing", [normalized]), enabled: false
            });
        }
        return options;
    }

    /** Recomputes every step's `agentOptions`/`branchOptions` from the
     * current agent list and branch rows. Called after load, and after any
     * change to the branches table (add, remove, or a key edited in place). */
    private refreshStepOptions(): void {
        const model = this.getModel("workflow") as JSONModel;
        const steps = (model.getProperty("/data/steps") as UiStep[]).map((s) => ({
            ...s,
            agentOptions: this.buildAgentOptions(s.agent_name),
            branchOptions: this.buildBranchOptionsForStep(s.branch_key)
        }));
        model.setProperty("/data/steps", steps);
    }

    // --- Branches -----------------------------------------------------------
    public onAddBranch(): void {
        const model = this.getModel("workflow") as JSONModel;
        const branches = (model.getProperty("/data/branches") as WorkflowBranch[]).slice();
        branches.push({ key: "", description: "", position: branches.length + 1 });
        model.setProperty("/data/branches", branches);
        this.refreshStepOptions();
    }

    public onRemoveBranch(event: Event): void {
        const index = WorkflowDetail.rowIndex(event);
        const model = this.getModel("workflow") as JSONModel;
        const branches = (model.getProperty("/data/branches") as WorkflowBranch[]).slice();
        branches.splice(index, 1);
        model.setProperty("/data/branches", branches);
        this.refreshStepOptions();
    }

    /** Live so a branch just renamed is immediately selectable/named in every
     * step's `<Select>`, not only after the next add/remove. */
    public onBranchKeyChange(): void {
        this.refreshStepOptions();
    }

    // --- Steps ---------------------------------------------------------------
    public onAddStep(): void {
        const model = this.getModel("workflow") as JSONModel;
        const steps = (model.getProperty("/data/steps") as UiStep[]).slice();
        steps.push({
            branch_key: null,
            position: 1,
            agent_name: "",
            instructions: "",
            fan_out: false,
            step_timeout_seconds: 600,
            agentOptions: this.buildAgentOptions(""),
            branchOptions: this.buildBranchOptionsForStep(null)
        });
        model.setProperty("/data/steps", steps);
    }

    public onRemoveStep(event: Event): void {
        const index = WorkflowDetail.rowIndex(event);
        const model = this.getModel("workflow") as JSONModel;
        const steps = (model.getProperty("/data/steps") as UiStep[]).slice();
        steps.splice(index, 1);
        model.setProperty("/data/steps", steps);
    }

    /** The row index of a press event's binding context, for tables whose
     * rows carry no id of their own (add/remove make a stable per-row id
     * impossible) -- same approach as AgentDetail's server table. */
    private static rowIndex(event: Event): number {
        const path = (event.getSource() as Control).getBindingContext("workflow")?.getPath() ?? "";
        return Number(path.substring(path.lastIndexOf("/") + 1));
    }

    // --- Save -------------------------------------------------------------

    /** Branch rows in table order, positioned 1..n within that one group --
     * never globally: `validate_workflow_parts` requires branch positions to
     * be contiguous from 1 on their own.
     *
     * Blank-keyed rows (added, then abandoned before being named) are dropped
     * rather than submitted, matching the classic admin's collect step. Left
     * in, one would reach `validate_workflow_parts`, which 400s on "A branch
     * key must not be empty." -- an operator who adds a row and changes their
     * mind would have to find and delete it themselves instead of the save
     * just working. That server-side rule stays as the backstop for a blank
     * key that reaches it some other way. */
    private static collectBranches(branches: WorkflowBranch[]): WorkflowBranch[] {
        return branches
            .filter((b) => (b.key || "").trim())
            .map((b, index) => ({
                key: b.key.trim(),
                description: b.description || "",
                position: index + 1
            }));
    }

    /**
     * Step rows in table order, positioned 1..n *within each group* -- the
     * main line and each branch counted separately. This is the single most
     * important rule in `validate_workflow_parts`: computing a position
     * globally instead of per group rejects every save for a reason the
     * operator did nothing to cause.
     */
    private static collectSteps(steps: UiStep[]): WorkflowStep[] {
        const counters: Record<string, number> = {};
        return steps.map((s) => {
            const branchKey = (s.branch_key || "").trim() || null;
            const groupKey = branchKey ?? "";
            counters[groupKey] = (counters[groupKey] || 0) + 1;
            return {
                branch_key: branchKey,
                position: counters[groupKey],
                agent_name: s.agent_name,
                instructions: s.instructions,
                fan_out: s.fan_out,
                step_timeout_seconds: s.step_timeout_seconds
            };
        });
    }

    public async onSave(): Promise<void> {
        const model = this.getModel("workflow") as JSONModel;
        model.setProperty("/errors", {});
        const data = model.getProperty("/data") as UiWorkflowData;

        const payload: WorkflowInput = {
            name: data.name,
            description: data.description,
            api_slug: data.api_slug,
            run_as_principal: data.run_as_principal,
            run_timeout_seconds: data.run_timeout_seconds,
            skip_seen_items: data.skip_seen_items,
            max_parallel_items: data.max_parallel_items,
            on_unknown_branch: data.on_unknown_branch,
            enabled: data.enabled,
            branches: WorkflowDetail.collectBranches(data.branches),
            steps: WorkflowDetail.collectSteps(data.steps)
        };

        try {
            const saved = await this.getAdminService().upsertWorkflow(payload, this.workflowId);
            MessageToast.show(this.text("workflowSaved"));
            this.workflowId = saved.id;
            this.getRouter().navTo("workflows");
        } catch (error) {
            // A rejected save (validate_workflow_parts' 400, or a 422 body
            // error) must leave the form exactly as the operator left it, so
            // they can correct it -- never cleared or reloaded. Nothing below
            // touches "/data", only "/errors" (for the few fields that map to
            // a single control) and the message itself, which ErrorHandler
            // renders from AdminError.detail -- already a readable string
            // whether the server sent one message or a list of them.
            if (error instanceof AdminError) {
                this.applyFieldErrors(error);
            }
            ErrorHandler.handle(error, this.text("workflowSaveFailed"));
        }
    }

    /** Attaches a 422's field errors to the scalar controls that have one.
     * Business-rule 400s (validate_workflow_parts) and 422s on branches/steps
     * name no single control -- those reach the operator only through the
     * message ErrorHandler shows, which is why onSave calls it unconditionally
     * rather than only when this finds nothing to attach. */
    private applyFieldErrors(error: AdminError): void {
        const model = this.getModel("workflow") as JSONModel;
        const errors: Record<string, string> = {};
        [
            "name", "description", "api_slug", "run_as_principal",
            "run_timeout_seconds", "max_parallel_items"
        ].forEach((field) => {
            const message = error.fieldErrors[field];
            if (message) {
                errors[field] = message;
                errors[`${field}State`] = ValueState.Error;
            }
        });
        model.setProperty("/errors", errors);
    }

    public onBack(): void {
        this.getRouter().navTo("workflows");
    }

    /** The scheduler endpoint hint under the API slug field, kept live by
     * `valueLiveUpdate` on the `<Input>` it is bound from. The classic admin
     * shows this only as a static parenthetical next to the label; this
     * fills in the entered slug the way its own agent-endpoint hint does
     * (`updateEndpointHint()` in templates/admin.html), since an operator
     * copying the shown path is less likely to typo it than one filling in
     * a placeholder by hand. */
    public endpointHint(apiSlug: string): string {
        const slug = (apiSlug || "").trim() || this.text("apiSlugPlaceholder");
        return this.text("apiSlugRunHint", [slug]);
    }

    // text(key) is inherited from BaseController -- do not redeclare it.
}
