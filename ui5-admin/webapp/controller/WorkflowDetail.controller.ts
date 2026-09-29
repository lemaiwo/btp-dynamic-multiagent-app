import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import { ValueState } from "sap/ui/core/library";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import { buildDefinitionGraph } from "../model/processFlowGraph";
import { canMoveWithinGroup, groupSteps, swap } from "../model/workflowOrder";
import {
    emptyRule, emptyUiConfig, kindOf, stepKindIcon, stepListSummary, uiConfigFromWire, wireConfigFromUi
} from "../model/stepKinds";
import type { UiStepConfig } from "../model/stepKinds";
import { counterText } from "../model/textStats";
import { MAIN_LINE_KEY } from "../model/NullableKey";
import { validateWorkflowSteps } from "../model/validators";
import type Event from "sap/ui/base/Event";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type Control from "sap/ui/core/Control";
import type Table from "sap/m/Table";
import type Dialog from "sap/m/Dialog";
import type Panel from "sap/m/Panel";
import type ListItemBase from "sap/m/ListItemBase";
import type { ListBase$SelectionChangeEvent } from "sap/m/ListBase";
import type ProcessFlowNode from "sap/suite/ui/commons/ProcessFlowNode";
import type { FlowNode } from "../model/processFlowGraph";
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
    // --- step kinds ---
    /** Flat editor copy of `config` for the per-kind forms; see
     * model/stepKinds.ts. Mapped back to `config` by `collectSteps()`. */
    cfg?: UiStepConfig;
    /** Whether the move buttons are live for this row; see `workflowOrder`.
     * Precomputed per row because the table binding has no row index to
     * hand a formatter. */
    canUp?: boolean;
    canDown?: boolean;
    /** 1-based row number shown in the step list; also what the
     * validation messages call "Step N". Set by `applyMoveFlags`. */
    rowNo?: number;
}

interface UiWorkflowData extends Omit<WorkflowInput, "steps"> {
    steps: UiStep[];
}

/**
 * @namespace com.agent.admin.controller
 */
export default class WorkflowDetail extends BaseController {

    private workflowId?: number;

    public onInit(): void {
        this.setModel(new JSONModel({
            title: "",
            data: JSON.parse(JSON.stringify(EMPTY_WORKFLOW)) as UiWorkflowData,
            availableAgents: [],
            errors: {},
            // --- step editor --- which row of /data/steps the editor shows
            // (-1: none, empty state), and the text dialog's scratch value.
            ui: { selectedStep: -1, dialogText: "", dialogTitle: "" }
        }), "workflow");
        // Its own model, so redrawing the graph cannot feed back into the
        // editor state it was drawn from.
        this.setModel(new JSONModel({ lanes: [], nodes: [] }), "flow");

        this.getRouter().getRoute("workflowDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const id = (event.getParameter("arguments") as { workflowId: string }).workflowId;
            void this.load(id);
        });
    }

    /**
     * Redraws the preview from the current editor state, saved or not.
     *
     * Called from each handler that can change the graph's shape rather than
     * from a model listener: `JSONModel.setProperty` fires no `propertyChange`,
     * so a model-level listener would never run. The rows' free-text fields
     * (instructions, timeout) are deliberately not wired up — they cannot
     * change the shape.
     */
    private refreshFlow(): void {
        const model = this.getModel("workflow") as JSONModel;
        const data = model.getProperty("/data") as UiWorkflowData;
        // Positions are recomputed the way collectBranches/collectSteps will
        // on save (1..n within each group), so the preview shows the order the
        // rows are actually in rather than whatever positions were loaded.
        const graph = buildDefinitionGraph(
            WorkflowDetail.collectBranches(data.branches),
            WorkflowDetail.collectSteps(data.steps)
        );
        (this.getModel("flow") as JSONModel).setData(graph);
    }

    private load(id: string): Promise<void> {
        return this.withBusy(async () => {
            const model = this.getModel("workflow") as JSONModel;
            model.setProperty("/errors", {});
            // The router reuses this controller across workflows, so a
            // selection left over from the previous one must not point the
            // editor at a row of the next.
            this.selectStep(-1);

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
                this.selectStep(-1);
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
                // --- step kinds --- a row written before kinds existed has
                // none; it is an agent step. `cfg` is the editor copy of the
                // stored config, built once here and mapped back on save.
                steps: (workflow.steps ?? []).map((s) => ({
                    ...s, kind: kindOf(s), cfg: uiConfigFromWire(kindOf(s), s.config)
                }))
            } as UiWorkflowData);
            model.setProperty("/title", workflow.name);
            this.refreshStepOptions();
            // Open on the first step so the editor is not empty for a
            // workflow that has steps.
            this.selectStep((model.getProperty("/data/steps") as UiStep[]).length ? 0 : -1);
        });
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
            // MAIN_LINE_KEY, not "": sap.m.Select shows nothing for an empty
            // selectedKey. The NullableKey binding type on the dropdown maps
            // the sentinel to the model's null.
            { key: MAIN_LINE_KEY, text: this.text("stepMainLine"), enabled: true },
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
     * current agent list and branch rows, regroups the rows so each branch
     * sits together, and marks which move buttons are live.
     *
     * Grouping happens here rather than only on load because a step's branch
     * can change at any moment, and a row that has just left the main line
     * belongs with its new group before the operator can move it. It is also
     * what makes the move buttons meaningful: neighbours always share a
     * group, so a swap always changes a position. */
    private refreshStepOptions(): void {
        const model = this.getModel("workflow") as JSONModel;
        const steps = (model.getProperty("/data/steps") as UiStep[]).map((s) => ({
            ...s,
            agentOptions: this.buildAgentOptions(s.agent_name),
            branchOptions: this.buildBranchOptionsForStep(s.branch_key)
        }));
        model.setProperty("/data/steps", steps);
        this.regroupSteps();
    }

    /**
     * Regroups the rows and recomputes which move buttons are live.
     *
     * Deliberately separate from `refreshStepOptions`: rebuilding a row's
     * `agentOptions` re-creates the item list behind its `<Select>`, and a
     * `<Select>` whose key is a *placeholder* for an agent that no longer
     * exists loses that key when its items are replaced -- silently turning a
     * missing agent into a real one. So an agent or fan-out change, which
     * cannot move a row between groups anyway, must never rebuild options.
     */
    private regroupSteps(): void {
        const model = this.getModel("workflow") as JSONModel;
        const branches = model.getProperty("/data/branches") as WorkflowBranch[];
        const steps = model.getProperty("/data/steps") as UiStep[];
        // groupSteps keeps the row objects, so the selected one can be
        // found again wherever grouping moved it.
        const selected = steps[this.selectedIndex()];
        const grouped = groupSteps(steps, branches);
        model.setProperty("/data/steps", grouped);
        if (selected) {
            model.setProperty("/ui/selectedStep", grouped.indexOf(selected));
        }
        this.applyMoveFlags();
    }

    /**
     * Marks which move buttons are live, leaving row order alone.
     *
     * Used wherever the order is already right: adding a row (it belongs at
     * the bottom until its branch says otherwise), removing one, and moving
     * one. Grouping on *add* would be actively wrong -- the row would be
     * pulled up into the main-line block the moment it appeared, away from
     * the button that created it, and when its branch was then chosen, stable
     * grouping would drop it at the *front* of that branch instead of the end
     * an operator expects. Left at the bottom, it lands where it should.
     */
    private applyMoveFlags(): void {
        const model = this.getModel("workflow") as JSONModel;
        const steps = model.getProperty("/data/steps") as UiStep[];
        const branches = model.getProperty("/data/branches") as WorkflowBranch[];

        model.setProperty("/data/steps", steps.map((s, index) => ({
            ...s,
            rowNo: index + 1,
            canUp: canMoveWithinGroup(steps, index, -1),
            canDown: canMoveWithinGroup(steps, index, 1)
        })));
        // Branches are one flat list, so their bounds are the whole table.
        model.setProperty("/data/branches", branches.map((b, index) => ({
            ...b,
            canUp: index > 0,
            canDown: index < branches.length - 1
        })));
        // Replacing the array re-creates the list items, so the selection
        // has to be put back on the row the editor is showing.
        this.applySelection();
        this.refreshFlow();
    }

    /** Swap a step with its neighbour in the same group. `position` is derived
     * from row order on save, so moving the row *is* the reordering. */
    private moveStep(event: Event, delta: number): void {
        const model = this.getModel("workflow") as JSONModel;
        const steps = model.getProperty("/data/steps") as UiStep[];
        const index = WorkflowDetail.rowIndex(event);
        if (!canMoveWithinGroup(steps, index, delta)) {
            return;
        }
        const selected = this.selectedIndex();
        model.setProperty("/data/steps", swap(steps, index, delta));
        if (selected === index) {
            model.setProperty("/ui/selectedStep", index + delta);
        } else if (selected === index + delta) {
            model.setProperty("/ui/selectedStep", index);
        }
        this.applyMoveFlags();
    }

    public onMoveStepUp(event: Event): void {
        this.moveStep(event, -1);
    }

    public onMoveStepDown(event: Event): void {
        this.moveStep(event, 1);
    }

    /** Branch order decides the order of the branch blocks in the steps table
     * and of the lanes in the preview, so moving one regroups the steps. */
    private moveBranch(event: Event, delta: number): void {
        const model = this.getModel("workflow") as JSONModel;
        const branches = model.getProperty("/data/branches") as WorkflowBranch[];
        const index = WorkflowDetail.rowIndex(event);
        const target = index + delta;
        if (index < 0 || target < 0 || target >= branches.length) {
            return;
        }
        model.setProperty("/data/branches", swap(branches, index, delta));
        this.refreshStepOptions();
    }

    public onMoveBranchUp(event: Event): void {
        this.moveBranch(event, -1);
    }

    public onMoveBranchDown(event: Event): void {
        this.moveBranch(event, 1);
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
            kind: "agent",
            config: {},
            cfg: emptyUiConfig("agent"),
            agentOptions: this.buildAgentOptions(""),
            branchOptions: this.buildBranchOptionsForStep(null)
        });
        model.setProperty("/data/steps", steps);
        model.setProperty("/ui/selectedStep", steps.length - 1);
        this.applyMoveFlags();
    }

    public onRemoveStep(event: Event): void {
        const index = WorkflowDetail.rowIndex(event);
        const model = this.getModel("workflow") as JSONModel;
        const steps = (model.getProperty("/data/steps") as UiStep[]).slice();
        steps.splice(index, 1);
        model.setProperty("/data/steps", steps);
        const selected = this.selectedIndex();
        if (selected === index) {
            // The neighbour that took its place, else the one before it.
            model.setProperty("/ui/selectedStep", Math.min(index, steps.length - 1));
        } else if (selected > index) {
            model.setProperty("/ui/selectedStep", selected - 1);
        }
        this.applyMoveFlags();
    }

    // --- step editor (master-detail) ------------------------------------

    private selectedIndex(): number {
        const value = (this.getModel("workflow") as JSONModel).getProperty("/ui/selectedStep") as number;
        return typeof value === "number" && value >= 0 ? value : -1;
    }

    /** Shows step `index` in the editor and selects its row; -1 (or an
     * index past the end) clears both and shows the empty state. */
    public selectStep(index: number): void {
        const model = this.getModel("workflow") as JSONModel;
        const count = ((model.getProperty("/data/steps") as UiStep[]) || []).length;
        model.setProperty("/ui/selectedStep", index >= 0 && index < count ? index : -1);
        this.applySelection();
    }

    /**
     * Points the editor and the list at `/ui/selectedStep`.
     *
     * The editor is element-bound to the row's path, so its fields keep
     * their plain relative two-way bindings; a replaced `/data/steps`
     * array resolves through the same path to the new row object. The
     * list's own selection is index-based too (`rememberSelections`), so
     * it is set explicitly after every re-order.
     */
    private applySelection(): void {
        const model = this.getModel("workflow") as JSONModel;
        const count = ((model.getProperty("/data/steps") as UiStep[]) || []).length;
        let index = this.selectedIndex();
        if (index >= count) {
            index = -1;
            model.setProperty("/ui/selectedStep", -1);
        }
        const editor = this.byId("stepEditor") as Panel | undefined;
        const table = this.byId("stepsTable") as Table | undefined;
        if (index < 0) {
            editor?.unbindElement("workflow");
            table?.removeSelections(true);
            return;
        }
        const path = `/data/steps/${index}`;
        if (editor && editor.getElementBinding("workflow")?.getPath() !== path) {
            editor.bindElement({ path, model: "workflow" });
        }
        const item = table?.getItems()[index];
        if (table && item && table.getSelectedItem() !== item) {
            table.setSelectedItem(item, true);
        }
    }

    public onStepSelectionChange(event: ListBase$SelectionChangeEvent): void {
        const item = event.getParameter("listItem") as ListItemBase | undefined;
        const path = item?.getBindingContext("workflow")?.getPath() ?? "";
        const index = Number(path.substring(path.lastIndexOf("/") + 1));
        if (Number.isFinite(index) && event.getParameter("selected")) {
            this.selectStep(index);
        }
    }

    /** "Step N" over the editor: the same number the list shows and the
     * validation messages use. */
    public stepEditorTitle(index: number): string {
        return index >= 0 ? this.text("stepEditorTitle", [index + 1]) : "";
    }

    public stepBranchText(branchKey: string | null): string {
        return (branchKey || "").trim() || this.text("stepMainLine");
    }

    public stepKindIcon(kind: string | undefined): string {
        return stepKindIcon(kind);
    }

    public stepKindText(kind: string | undefined): string {
        const key: Record<string, string> = {
            agent: "stepKindAgent", condition: "stepKindCondition", transform: "stepKindTransform",
            http: "stepKindHttp", python: "stepKindPython"
        };
        return this.text(key[kindOf({ kind: kind as UiStep["kind"] })]);
    }

    /** The list row's summary, from the fields that can change it, bound
     * as parts: a JSONModel binding to the `cfg` object itself would not
     * notice a field edited in place, and the formatter runs bound to the
     * controller, so the row cannot be read off the control either. */
    public stepRowSummary(
        kind: UiStep["kind"], instructions: string, rules: UiStepConfig["rules"] | undefined,
        extractJson: string, regexPattern: string, template: string, truncate: string,
        method: UiStepConfig["method"], destination: string, path: string, code: string
    ): string {
        const k = kindOf({ kind });
        const cfg: UiStepConfig = {
            ...emptyUiConfig(k),
            rules: rules || [],
            extract_json: extractJson || "",
            regex_pattern: regexPattern || "",
            template: template || "",
            truncate: truncate || "",
            method: method || "GET",
            destination: destination || "",
            path: path || "/",
            code: code || ""
        };
        return stepListSummary({ kind: k, instructions: instructions || "", cfg });
    }

    public textCounter(text: string | undefined | null): string {
        return counterText(text);
    }

    // --- text dialog ---

    /** Model path of the field the open text dialog edits. */
    private textDialogPath = "";

    /** Opens the large text dialog on `path` (absolute, in the `workflow`
     * model). The dialog edits a scratch copy; Done writes it back, Cancel
     * (or Escape) leaves the field exactly as it was when the dialog opened. */
    public openTextDialog(path: string, title: string): void {
        const model = this.getModel("workflow") as JSONModel;
        this.textDialogPath = path;
        model.setProperty("/ui/dialogTitle", title);
        model.setProperty("/ui/dialogText", String(model.getProperty(path) ?? ""));
        (this.byId("stepTextDialog") as Dialog).open();
    }

    /** An Expand button: `app:prop` names the field relative to the step
     * row the button is bound to, `app:titleKey` the i18n key of its label. */
    public onExpandText(event: Event): void {
        const source = event.getSource() as Control;
        const prop = String(source.data("prop") ?? "");
        const titleKey = String(source.data("titleKey") ?? "");
        const rowPath = WorkflowDetail.stepPath(event);
        if (!prop || !rowPath) {
            return;
        }
        this.openTextDialog(`${rowPath}/${prop}`, titleKey ? this.text(titleKey) : "");
    }

    public onTextDialogDone(): void {
        const model = this.getModel("workflow") as JSONModel;
        if (this.textDialogPath) {
            model.setProperty(this.textDialogPath, model.getProperty("/ui/dialogText"));
        }
        this.closeTextDialog();
    }

    public onTextDialogCancel(): void {
        this.closeTextDialog();
    }

    /** Escape behaves like Cancel. */
    public onTextDialogEscape(promise: { resolve(): void }): void {
        this.closeTextDialog();
        promise.resolve();
    }

    private closeTextDialog(): void {
        this.textDialogPath = "";
        (this.byId("stepTextDialog") as Dialog | undefined)?.close();
    }

    /** A step row's branch, agent or fan-out changed. The two-way binding has
     * already written it; this only redraws the preview. */
    public onStepRowChange(): void {
        this.refreshFlow();
    }

    /** A step's branch changed, so the row belongs to another group now. */
    public onStepBranchChange(): void {
        this.regroupSteps();
    }

    // --- step kinds ---------------------------------------------------------

    /** The model path of the step row a press/change event came from
     * (`/data/steps/N`), whether the control sits directly in the row or in
     * a nested rule list under it. */
    private static stepPath(event: Event): string {
        const path = (event.getSource() as Control).getBindingContext("workflow")?.getPath() ?? "";
        const match = /^(\/data\/steps\/\d+)/.exec(path);
        return match ? match[1] : path;
    }

    /**
     * A step's kind changed. The two-way binding has written `kind`; this
     * clears what the new kind cannot use (an agent, the fan-out flag) and
     * gives the row a fresh editor config for that kind -- unless it is the
     * kind the config was built for, so switching away and back within one
     * edit keeps what was typed.
     */
    public onStepKindChange(event: Event): void {
        const model = this.getModel("workflow") as JSONModel;
        const path = WorkflowDetail.stepPath(event);
        const step = model.getProperty(path) as UiStep;
        const kind = kindOf(step);
        if (kind !== "agent") {
            model.setProperty(`${path}/agent_name`, "");
            model.setProperty(`${path}/fan_out`, false);
        }
        if (!step.cfg || step.cfg.kind !== kind) {
            model.setProperty(`${path}/cfg`, emptyUiConfig(kind));
        }
        this.refreshFlow();
    }

    public onAddConditionRule(event: Event): void {
        const model = this.getModel("workflow") as JSONModel;
        const path = `${WorkflowDetail.stepPath(event)}/cfg/rules`;
        const rules = ((model.getProperty(path) as UiStepConfig["rules"]) || []).slice();
        rules.push(emptyRule());
        model.setProperty(path, rules);
    }

    public onRemoveConditionRule(event: Event): void {
        const model = this.getModel("workflow") as JSONModel;
        const rulePath = (event.getSource() as Control).getBindingContext("workflow")?.getPath() ?? "";
        const index = Number(rulePath.substring(rulePath.lastIndexOf("/") + 1));
        const listPath = rulePath.substring(0, rulePath.lastIndexOf("/"));
        const rules = ((model.getProperty(listPath) as UiStepConfig["rules"]) || []).slice();
        rules.splice(index, 1);
        model.setProperty(listPath, rules);
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
    private static collectSteps(steps: UiStep[], strict = false): WorkflowStep[] {
        const counters: Record<string, number> = {};
        return steps.map((s, index) => {
            const branchKey = (s.branch_key || "").trim() || null;
            const groupKey = branchKey ?? "";
            counters[groupKey] = (counters[groupKey] || 0) + 1;
            // --- step kinds --- a non-agent step names no agent and cannot
            // be the fan-out step; its settings travel in `config`, mapped
            // back from the flat editor copy. In lenient mode (the preview)
            // a JSON field that does not parse yet is simply left empty; the
            // save is strict and reports it with the row number.
            const kind = kindOf(s);
            let config: WorkflowStep["config"] = {};
            if (kind !== "agent") {
                try {
                    config = wireConfigFromUi(kind, s.cfg);
                } catch (error) {
                    if (strict) {
                        throw new Error(`${index + 1}\u0000${(error as Error).message}`);
                    }
                }
            }
            return {
                branch_key: branchKey,
                position: counters[groupKey],
                agent_name: kind === "agent" ? s.agent_name : "",
                instructions: kind === "agent" ? s.instructions : "",
                fan_out: kind === "agent" && s.fan_out,
                step_timeout_seconds: s.step_timeout_seconds,
                kind,
                config
            };
        });
    }

    public async onSave(): Promise<void> {
        const model = this.getModel("workflow") as JSONModel;
        model.setProperty("/errors", {});
        const data = model.getProperty("/data") as UiWorkflowData;

        // --- step kinds --- strict: a JSON field on an http step that does
        // not parse stops the save here, naming the row, instead of sending
        // an empty object the operator never typed.
        let steps: WorkflowStep[];
        try {
            steps = WorkflowDetail.collectSteps(data.steps, true);
        } catch (error) {
            const [row, message] = String((error as Error).message).split("\u0000");
            // Select the row the message names, so the field is in view
            // once the message is dismissed.
            this.selectStep(Number(row) - 1);
            MessageBox.error(this.text("workflowStepInvalid", [row, message ?? row]));
            return;
        }
        const stepErrors = validateWorkflowSteps(steps);
        const stepErrorRows = Object.keys(stepErrors).map(Number).sort((a, b) => a - b);
        if (stepErrorRows.length) {
            this.selectStep(stepErrorRows[0]);
            MessageBox.error(stepErrorRows
                .map((i) => this.text("workflowStepInvalid", [String(i + 1), stepErrors[i]]))
                .join("\n"));
            return;
        }

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
            steps
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

    // --- where used ---

    /** The link text over the step editor: "Open <agent>", blank while the
     * step names no agent (the link is hidden then anyway). */
    public openStepAgentText(agentName: string): string {
        return agentName ? this.text("openStepAgent", [agentName]) : "";
    }

    public onOpenStepAgent(event: Event): void {
        const step = (event.getSource() as Control)
            .getBindingContext("workflow")?.getObject() as WorkflowStep | undefined;
        void this.openAgentByName(step?.agent_name ?? "");
    }

    /**
     * A click on a preview node opens its agent.
     *
     * The runtime hands the pressed `ProcessFlowNode` itself as the event's
     * parameter object (`fireNodePress(this)` in ProcessFlowNode's click
     * handler), and that control is bound to the `FlowNode` it was drawn
     * from. The node is matched back to a step by group and position -- the
     * same recomputed positions `refreshFlow` drew it with -- rather than by
     * its title, which is a display string ("(no agent)" for an empty row).
     */
    public onFlowNodePress(event: Event): void {
        const node = event.getParameters() as unknown as ProcessFlowNode | undefined;
        const flowNode = node?.getBindingContext?.("flow")?.getObject() as FlowNode | undefined;
        if (!flowNode) {
            return;
        }
        const data = (this.getModel("workflow") as JSONModel).getProperty("/data") as UiWorkflowData;
        const step = WorkflowDetail.collectSteps(data.steps).find(
            (s) => s.branch_key === flowNode.branchKey && s.position === flowNode.position
        );
        void this.openAgentByName(step?.agent_name ?? "");
    }

    /**
     * Resolves an agent name to its id through the list the step selects
     * already use, refetching once in case the agent was created after this
     * page loaded, then navigates to it. A name that still matches nothing
     * is a step the server will refuse to save; say so instead of opening
     * a blank page.
     */
    private async openAgentByName(name: string): Promise<void> {
        if (!name) {
            return;
        }
        const model = this.getModel("workflow") as JSONModel;
        let agents = model.getProperty("/availableAgents") as Agent[];
        let match = agents.find((a) => a.name === name);
        if (!match) {
            const fresh = await this.run(
                this.getAdminService().listAgents(),
                this.text("workflowLoadAgentsFailed")
            );
            if (fresh) {
                agents = fresh;
                model.setProperty("/availableAgents", agents);
                match = agents.find((a) => a.name === name);
            }
        }
        if (!match) {
            MessageToast.show(this.text("stepAgentNotFound", [name]));
            return;
        }
        this.getRouter().navTo("agentDetail", { agentId: String(match.id) });
    }

    // text(key) is inherited from BaseController -- do not redeclare it.
}
