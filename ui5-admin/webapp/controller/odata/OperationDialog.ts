import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import MessageBox from "sap/m/MessageBox";
import Device from "sap/ui/Device";
import { ValueState } from "sap/ui/core/library";
import odataCatalog from "../../model/odataCatalog";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type View from "sap/ui/core/mvc/View";
import type Dialog from "sap/m/Dialog";
import type { ODataDefinition, ODataEntitySet, ODataOperation } from "../../service/types";

/** What the dialog needs from the page that opens it. */
export interface OperationDialogContext {
    /** The definition the operation is part of: the entity set it is bound
     *  to or returns is named by its title. */
    definition: ODataDefinition;
    text: (key: string, args?: (string | number)[]) => string;
}

/** What Apply answers: the two texts the dialog edits, and nothing else of
 *  the operation. Cancel answers nothing. */
export interface OperationDialogResult {
    title: string;
    description: string;
}

const KIND_TEXT: Record<string, string> = {
    function_import: "odataKindFunctionImport", action: "odataKindAction", function: "odataKindFunction"
};

/**
 * The dialog of one operation: its business name and its description for
 * the agent, which is what agents find it by, and -- read-only -- what
 * SAP's metadata says about it (name, method, kind, binding, return,
 * parameters).
 *
 * The dialog works on a copy and sends nothing. `open` answers the two
 * texts (Apply) or nothing (Cancel, Escape, and a dialog that was closed
 * for it: `dismiss`, a page that was left); the page writes them into its
 * form, and its Save stores them. Apply is not taken while the server would
 * refuse a text (`odataCatalog.operationTextProblems`).
 *
 * A plain class, not a controller: it is the controller object of its
 * fragment only.
 */
export default class OperationDialog {

    private dialog?: Dialog;
    private view!: View;
    private readonly model = new JSONModel();
    private context!: OperationDialogContext;
    private resolve?: (result: OperationDialogResult | undefined) => void;
    /** What `open` answers once the dialog has closed. */
    private result?: OperationDialogResult;
    /** The two texts as the dialog showed them when it opened. */
    private baseline = "";

    public async open(
        view: View, operation: ODataOperation, context: OperationDialogContext
    ): Promise<OperationDialogResult | undefined> {
        this.view = view;
        this.context = context;
        if (!this.dialog) {
            this.dialog = await Fragment.load({
                id: view.getId(), name: "com.agent.admin.fragment.ODataOperationDialog", controller: this
            }) as Dialog;
            this.dialog.setModel(this.model, "op");
            // Escape is Cancel: it asks before unapplied changes are lost.
            this.dialog.setEscapeHandler((escape) => {
                (escape.reject as () => void)();
                this.onCancel();
            });
            // Answered when the dialog has closed, whoever closed it.
            this.dialog.attachAfterClose(() => {
                const resolve = this.resolve;
                const result = this.result;
                this.resolve = undefined;
                this.result = undefined;
                resolve?.(result);
            });
            view.addDependent(this.dialog);
        }
        const text = context.text;
        const setLabel = (name: string): string => {
            const set = (context.definition.entity_sets ?? []).filter((e: ODataEntitySet) => e.name === name)[0];
            return set ? odataCatalog.titleOf(set) : name;
        };
        const bound = operation.bound_to === null || operation.bound_to === undefined ? "" : operation.bound_to;
        const returns = operation.returns;
        const parameters = operation.parameters ?? [];
        this.result = undefined;
        this.model.setData({
            title: operation.title ?? "", description: operation.description ?? "", errors: {},
            name: operation.name,
            method: operation.http_method,
            kind: text(KIND_TEXT[operation.kind] ?? "odataReturnsUnknown"),
            boundTo: bound ? text("odataBoundToSet", [setLabel(bound), bound]) : text("odataUnbound"),
            returns: returns && returns.entity_set
                ? text(returns.collection ? "odataReturnsMany" : "odataReturnsOne", [
                    setLabel(returns.entity_set), returns.entity_set
                ])
                : text("odataReturnsUnknown"),
            parameters: parameters.length
                ? parameters.map((p) => ({
                    text: text(p.required === false ? "odataParameterOptional" : "odataParameterRequired", [
                        p.name, p.type ?? "Edm.String"
                    ])
                }))
                : [{ text: text("odataNoParameters") }]
        });
        this.baseline = this.current();
        this.dialog.setTitle(text("odataOperationDialogTitle", [odataCatalog.operationTitle(operation)]));
        this.dialog.setStretch(Device.system.phone === true);
        this.dialog.setInitialFocus(view.byId("operationTitle") as Control);
        this.dialog.open();
        return new Promise((resolve) => {
            this.resolve = resolve;
        });
    }

    private current(): string {
        return JSON.stringify([this.model.getProperty("/title"), this.model.getProperty("/description")]);
    }

    private close(result: OperationDialogResult | undefined): void {
        this.result = result;
        this.dialog?.close();
    }

    /** Closes the dialog as Cancel does, without a question: the page it
     *  belongs to is left. Does nothing when it is not open. */
    public dismiss(): void {
        if (this.resolve && this.dialog && !this.dialog.isDestroyed() && this.dialog.isOpen()) {
            this.close(undefined);
        }
    }

    public formatErrorState(message: string | undefined): ValueState {
        return message ? ValueState.Error : ValueState.None;
    }

    /** Typing in a field takes its error away; Apply checks again. */
    public onEdit(event: Event): void {
        const id = (event.getSource() as Control).getId();
        this.model.setProperty(/operationTitle$/.test(id) ? "/errors/title" : "/errors/description", "");
    }

    /** Hands the two texts to the page -- unless the server would refuse
     *  one of them: that is said on its field, and the dialog stays. */
    public onApply(): void {
        const title = String(this.model.getProperty("/title") ?? "");
        const description = String(this.model.getProperty("/description") ?? "");
        const problems = odataCatalog.operationTextProblems(title, description);
        const errors: Record<string, string> = {};
        if (problems.title) {
            errors.title = this.context.text(problems.title);
        }
        if (problems.description) {
            errors.description = this.context.text(problems.description);
        }
        this.model.setProperty("/errors", errors);
        if (errors.title || errors.description) {
            (this.view.byId(errors.title ? "operationTitle" : "operationDescription") as Control | undefined)?.focus();
            return;
        }
        this.close({ title, description });
    }

    /** Closes without a result; asks first when a text was changed. */
    public onCancel(): void {
        if (this.current() === this.baseline) {
            this.close(undefined);
            return;
        }
        const discard = this.context.text("odataDiscard");
        const keep = this.context.text("odataKeepEditing");
        MessageBox.warning(this.context.text("odataOperationDiscard"), {
            title: this.context.text("odataUnsavedTitle"),
            actions: [discard, keep],
            emphasizedAction: keep,
            initialFocus: keep,
            onClose: (action: string | null) => {
                if (action === discard) {
                    this.close(undefined);
                }
            }
        });
    }
}
