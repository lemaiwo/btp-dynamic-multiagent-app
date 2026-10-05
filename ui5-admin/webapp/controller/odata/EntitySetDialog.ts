import JSONModel from "sap/ui/model/json/JSONModel";
import Fragment from "sap/ui/core/Fragment";
import Filter from "sap/ui/model/Filter";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import MessageBox from "sap/m/MessageBox";
import Device from "sap/ui/Device";
import { InvisibleMessageMode, ValueState } from "sap/ui/core/library";
import odataCatalog, { type ODataFieldFilter } from "../../model/odataCatalog";
import { canonical } from "../../model/runsPanel";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type View from "sap/ui/core/mvc/View";
import type CheckBox from "sap/m/CheckBox";
import type Dialog from "sap/m/Dialog";
import type SearchField from "sap/m/SearchField";
import type SegmentedButton from "sap/m/SegmentedButton";
import type ToggleButton from "sap/m/ToggleButton";
import type ListBinding from "sap/ui/model/ListBinding";
import type {
    ODataDefinition, ODataEntitySet, ODataExampleQuery, ODataField, ODataNavigation, ODataVersion
} from "../../service/types";

/** What the dialog needs from the page that opens it. */
export interface EntityDialogContext {
    /** The definition the entity set is part of: the other entity sets
     *  (names, navigation targets) and the operations bound to this one. */
    definition: ODataDefinition;
    version: ODataVersion;
    /** The technical name can still be changed (not stored yet). */
    canRename: boolean;
    text: (key: string, args?: (string | number)[]) => string;
}

/** How the dialog was left: with the entity set as edited, or with the
 *  wish to remove it. Cancel answers nothing. */
export type EntityDialogResult = { action: "apply"; entitySet: ODataEntitySet } | { action: "remove" };

type Grant = "selectable" | "filterable" | "writable";

/** One row of the fields table: the field, and what the row shows of it. */
interface FieldRow extends Omit<ODataField, "values"> {
    /** The field's position in the entity set. */
    index: number;
    /** The value meanings as text, "B = awaiting release; 05 = released". */
    meanings: string;
    meaningsError: string;
    isKey: boolean;
    /** Why the last tick was changed or not taken. */
    note: string;
    /** The row asks whether agents may read this personal data. */
    confirm: boolean;
    /** V4, and no Edm type: filterable only by the listed values. */
    enumHint: boolean;
    /** The personal-data tag and its state. */
    tag: string;
    tagState: ValueState;
}

interface NavigationRow extends ODataNavigation {
    kind: string;
    follow: string;
    followState: ValueState;
}

interface ExampleRow {
    description: string;
    select: string;
    filter: string;
    orderby: string;
    top: string;
    /** What agents will not see of it, or "". */
    warning: string;
    number: number;
}

/** How many problems the dialog spells out when Apply is not taken. */
const ISSUE_CAP = 3;

/** The dialog's own wording of a rule the page words for its table. */
const HERE: Record<string, string> = {
    odataNeedsSelectable: "odataEntityNeedsSelectable",
    odataNeedsWritable: "odataEntityNeedsWritable"
};

/**
 * The dialog of one entity set: its business name and description for the
 * agent, its keys, and three tabs -- the fields with what an agent may
 * read, filter on and write, the navigations, and the example queries.
 *
 * This is where it is decided which fields of an SAP business object reach
 * the model, so nothing is enabled by default and nothing implicitly: a
 * tick does what its checkbox says and no more, with two exceptions that
 * only take away (unticking Read also unticks Filter, since the server
 * refuses a filterable field that is not readable) and one that asks (Read
 * on a field marked as personal data is confirmed in its row).
 *
 * The dialog works on a copy and sends nothing. `open` answers the edited
 * entity set (Apply), the wish to remove it, or nothing (Cancel); the page
 * writes that into its form, and its Save stores it. Apply is not taken
 * while the server would refuse the entity set (`odataCatalog.entitySetIssues`).
 *
 * A plain class, not a controller: it is the controller object of its
 * fragment only.
 */
export default class EntitySetDialog {

    private dialog?: Dialog;
    private view!: View;
    private readonly model = new JSONModel();
    private context!: EntityDialogContext;
    /** The page's entity set (never changed here) and the copy it is compared with. */
    private original!: ODataEntitySet;
    private source!: ODataEntitySet;
    private resolve?: (result: EntityDialogResult | undefined) => void;
    /** The fields the last "Tick Read for ..." ticked, for its Undo. */
    private readAllTicked: number[] = [];

    public async open(
        view: View, entitySet: ODataEntitySet, context: EntityDialogContext
    ): Promise<EntityDialogResult | undefined> {
        this.view = view;
        this.context = context;
        this.original = entitySet;
        this.source = JSON.parse(JSON.stringify(entitySet)) as ODataEntitySet;
        this.readAllTicked = [];
        if (!this.dialog) {
            this.dialog = await Fragment.load({
                id: view.getId(), name: "com.agent.admin.fragment.ODataEntitySetDialog", controller: this
            }) as Dialog;
            this.model.setSizeLimit(1000);
            this.dialog.setModel(this.model, "entity");
            // Escape is Cancel: it asks before unapplied changes are lost.
            this.dialog.setEscapeHandler((escape) => {
                // Not closed by the key itself: Cancel decides.
                (escape.reject as () => void)();
                this.onCancel();
            });
            view.addDependent(this.dialog);
        }
        const source = this.source;
        const keys = (source.keys ?? []).map((key) => key.name);
        const rows: FieldRow[] = (source.fields ?? []).map((field, index) => {
            const { values, ...rest } = field;
            const row: FieldRow = {
                ...rest, index, meanings: odataCatalog.formatValueMeanings(values), meaningsError: "",
                isKey: keys.indexOf(field.name) !== -1, note: "", confirm: false,
                enumHint: context.version === "v4" && !/^Edm\./.test(field.type ?? ""),
                tag: "", tagState: ValueState.None
            };
            this.tag(row);
            return row;
        });
        this.model.setData({
            title: source.title ?? "", name: source.name, description: source.description ?? "",
            canRename: context.canRename, errors: {}, issues: "", keyWarning: "", tab: "fields", filter: "all",
            keys: keys.length
                ? source.keys.map((key) => ({ text: context.text("odataKeyItem", [key.name, key.type]) }))
                : [{ text: context.text("odataNoKeys") }],
            rows,
            navigations: (source.navigations ?? []).map((navigation): NavigationRow => {
                const follow = odataCatalog.navigationFollow(source, navigation, context.definition);
                return {
                    ...navigation,
                    kind: context.text(navigation.collection ? "odataNavCollection" : "odataNavSingle"),
                    follow: context.text(follow.key, follow.args),
                    // Information, not an error: nothing is wrong with the entity set.
                    followState: follow.ok ? ValueState.Success : ValueState.Information
                };
            }),
            examples: (source.examples ?? []).map((example, index): ExampleRow => ({
                description: example.description ?? "", select: (example.select ?? []).join(", "),
                filter: example.filter ?? "", orderby: example.orderby ?? "",
                top: example.top === null || example.top === undefined ? "" : String(example.top),
                warning: "", number: index + 1
            })),
            readCount: rows.filter((row) => row.selectable === true).length,
            total: rows.length, shown: rows.length, readAllCount: 0, readAllDone: ""
        });
        (view.byId("entityFieldSearch") as SearchField).setValue("");
        this.applyFilter();
        this.showKeyWarning();
        this.showExampleWarnings();
        this.dialog.setTitle(context.text("odataEntityDialogTitle", [odataCatalog.titleOf(source)]));
        this.dialog.setStretch(Device.system.phone === true);
        // A safe start: a text field, not a checkbox and not a button.
        this.dialog.setInitialFocus(view.byId("entityTitle") as Control);
        this.dialog.open();
        return new Promise((resolve) => {
            this.resolve = resolve;
        });
    }

    private close(result: EntityDialogResult | undefined): void {
        this.dialog?.close();
        const resolve = this.resolve;
        this.resolve = undefined;
        resolve?.(result);
    }

    // --- state --------------------------------------------------------------

    private text(key: string, args?: (string | number)[]): string {
        return this.context.text(key, args);
    }

    private rows(): FieldRow[] {
        return this.model.getProperty("/rows") as FieldRow[];
    }

    private say(text: string, mode: InvisibleMessageMode = InvisibleMessageMode.Polite): void {
        InvisibleMessage.getInstance().announce(text, mode);
    }

    /** The row a control of the fields table belongs to. */
    private rowOf(event: Event): FieldRow {
        return (event.getSource() as Control).getBindingContext("entity")!.getObject() as FieldRow;
    }

    /** Sets the personal-data tag of a row: in words, and a warning only
     *  while agents can read the field. */
    private tag(row: FieldRow): void {
        const read = row.selectable === true;
        row.tag = row.personal_data === true ? this.text(read ? "odataPersonalRead" : "odataPersonalUnread") : "";
        row.tagState = row.personal_data === true && read ? ValueState.Warning : ValueState.None;
    }

    private static topOf(text: string): number | null {
        const trimmed = text.trim();
        // Not a number: kept as one that is none, so that Apply refuses it.
        return trimmed ? (/^\d+$/.test(trimmed) ? Number(trimmed) : Number.NaN) : null;
    }

    private static exampleOf(row: ExampleRow): ODataExampleQuery {
        return {
            description: row.description, filter: row.filter,
            select: row.select.split(",").map((name) => name.trim()).filter(Boolean),
            orderby: row.orderby, top: EntitySetDialog.topOf(row.top)
        };
    }

    /**
     * The entity set as the dialog holds it. Value meanings nobody typed in
     * stay the stored pairs: text that is parsed again could split a
     * meaning that holds a ";" or "=".
     */
    private current(): ODataEntitySet {
        const data = this.model.getData() as {
            title: string; name: string; description: string; navigations: NavigationRow[]; examples: ExampleRow[];
        };
        const source = this.source;
        return {
            ...source,
            name: data.name, title: data.title, description: data.description,
            fields: this.rows().map((row): ODataField => {
                const stored = source.fields[row.index];
                return {
                    ...stored,
                    label: row.label, selectable: row.selectable === true, filterable: row.filterable === true,
                    writable: row.writable === true, hint: row.hint,
                    values: row.meanings === odataCatalog.formatValueMeanings(stored.values)
                        ? stored.values : odataCatalog.parseValueMeanings(row.meanings),
                    personal_data: row.personal_data === true
                };
            }),
            navigations: data.navigations.map((row, index): ODataNavigation => (
                { ...source.navigations[index], description: row.description }
            )),
            examples: data.examples.map(EntitySetDialog.exampleOf)
        };
    }

    private isDirty(): boolean {
        return canonical(this.current()) !== canonical(this.source);
    }

    /** The entity set with the ticks of the rows, for the rules that look
     *  at the fields only. */
    private ticked(): ODataEntitySet {
        return { ...this.source, fields: this.rows() as unknown as ODataField[] };
    }

    private showKeyWarning(): void {
        const hidden = odataCatalog.hiddenKeys(this.ticked());
        this.model.setProperty("/keyWarning", hidden.length
            ? this.text(hidden.length === 1 ? "odataKeyWarningOne" : "odataKeyWarningMany", [hidden.join(", ")]) : "");
    }

    /** Says per example query what agents will not see of it. */
    private showExampleWarnings(): void {
        const entitySet = this.ticked();
        (this.model.getProperty("/examples") as ExampleRow[]).forEach((row) => {
            const warning = odataCatalog.exampleWarning(entitySet, EntitySetDialog.exampleOf(row));
            row.warning = warning ? this.text(warning.key, warning.args) : "";
        });
        this.model.refresh();
    }

    private matches(row: FieldRow): boolean {
        const search = (this.view.byId("entityFieldSearch") as SearchField).getValue();
        return odataCatalog.fieldMatches(row, search, this.model.getProperty("/filter") as ODataFieldFilter);
    }

    /** How many fields "Tick Read for ..." would tick: those listed that
     *  are neither readable nor marked as personal data. */
    private showReadAll(): void {
        this.model.setProperty("/readAllCount", this.rows().filter((row) => (
            row.selectable !== true && row.personal_data !== true && this.matches(row)
        )).length);
    }

    /**
     * Lists the fields the search text and the filter leave. A filter on
     * the binding: no row is worked out again, and only fifty are rendered.
     * It runs when the search or the filter changes, not on a tick, so a
     * row does not vanish under the pointer.
     */
    private applyFilter(): void {
        const binding = this.view.byId("entityFieldsTable")?.getBinding("items") as ListBinding | undefined;
        // The rows are looked up when the filter runs, not captured here:
        // the binding keeps its filter and runs it again on the rows of the
        // entity set the dialog is opened for next.
        binding?.filter(new Filter({
            path: "index",
            test: (index: number) => {
                const row = this.rows()[index];
                return !!row && this.matches(row);
            }
        }));
        this.model.setProperty("/shown", binding ? binding.getLength() : this.rows().length);
        this.showReadAll();
    }

    /** After ticks changed: the counts and what follows from Read. */
    private ticksChanged(keys: boolean): void {
        this.model.setProperty("/readCount", this.rows().filter((row) => row.selectable === true).length);
        this.model.setProperty("/issues", "");
        if (keys) {
            this.showKeyWarning();
        }
        this.showReadAll();
        this.showExampleWarnings();
    }

    // --- formatters ---------------------------------------------------------

    public formatErrorState(message: string | undefined): ValueState {
        return message ? ValueState.Error : ValueState.None;
    }

    /** A ticked Write stands out, as a ticked write operation does on the page. */
    public formatWriteState(writable: boolean | undefined): ValueState {
        return writable ? ValueState.Warning : ValueState.None;
    }

    public formatTabFields(read: number | undefined, total: number | undefined): string {
        return this.text("odataTabFields", [read ?? 0, total ?? 0]);
    }

    public formatTabNavigations(count: number | undefined): string {
        return this.text("odataTabNavigations", [count ?? 0]);
    }

    public formatTabExamples(count: number | undefined): string {
        return this.text("odataTabExamples", [count ?? 0]);
    }

    public formatShowing(shown: number | undefined, total: number | undefined): string {
        return this.text("odataFieldsShowing", [shown ?? 0, total ?? 0]);
    }

    public formatReadAll(count: number | undefined): string {
        if (!count) {
            return this.text("odataReadAllNone");
        }
        return count === 1 ? this.text("odataReadAllOne") : this.text("odataReadAll", [count]);
    }

    public formatPersonalQuestion(name: string | undefined): string {
        return this.text("odataPersonalDataReadConfirm", [name ?? ""]);
    }

    /** Not by colour alone: a warning tag carries the warning icon. */
    public formatTagIcon(state: string | undefined): string {
        return state === ValueState.Warning ? "sap-icon://alert" : "";
    }

    public formatFollowIcon(state: string | undefined): string {
        return state === ValueState.Success ? "sap-icon://accept" : "sap-icon://message-information";
    }

    public formatRemoveExample(number: number | undefined): string {
        return this.text("odataRemoveExample", [number ?? 0]);
    }

    // --- the fields ---------------------------------------------------------

    /**
     * Read, Filter or Write of a field was clicked. The boxes are bound one
     * way: a click that is not taken is put back.
     *
     * - Read on a field marked as personal data is not ticked: the row asks.
     * - Unticking Read also unticks Filter, and the row says so.
     * - Filter on a field that is not readable is refused; Read is not
     *   ticked for the admin.
     * - Write stands for itself: a write-only field is legitimate.
     */
    public onGrant(event: Event): void {
        const box = event.getSource() as CheckBox;
        const grant = box.data("grant") as Grant;
        const on = box.getSelected();
        const row = this.rowOf(event);
        row.note = "";
        if (grant === "selectable" && on && row.personal_data === true) {
            row.confirm = true;
            box.setSelected(false);
            this.model.refresh();
            this.say(this.formatPersonalQuestion(row.name));
            return;
        }
        if (grant === "filterable" && on && row.selectable !== true) {
            row.note = this.text("odataFilterNeedsRead");
            box.setSelected(false);
            this.model.refresh();
            this.say(row.note, InvisibleMessageMode.Assertive);
            return;
        }
        row[grant] = on;
        if (grant === "selectable") {
            row.confirm = false;
            if (!on && row.filterable === true) {
                row.filterable = false;
                row.note = this.text("odataFilterUnticked");
                this.say(row.note);
            }
            this.tag(row);
        }
        this.ticksChanged(row.isKey);
    }

    /** The admin confirmed, in the row, that agents may read this personal data. */
    public onPersonalConfirm(event: Event): void {
        const row = this.rowOf(event);
        row.confirm = false;
        row.selectable = true;
        row.note = "";
        this.tag(row);
        this.ticksChanged(row.isKey);
        this.say(row.tag);
    }

    public onPersonalKeep(event: Event): void {
        this.rowOf(event).confirm = false;
        this.model.refresh();
    }

    /** The manual personal-data marker. It changes no permission; on a
     *  field agents already read, the tag warns at once. */
    public onPersonal(event: Event): void {
        const row = this.rowOf(event);
        row.personal_data = (event.getSource() as ToggleButton).getPressed();
        row.confirm = false;
        this.tag(row);
        this.model.setProperty("/issues", "");
        this.showReadAll();
        this.model.refresh();
        if (row.tag) {
            this.say(row.tag);
        }
    }

    public onMeanings(event: Event): void {
        const row = this.rowOf(event);
        const problem = odataCatalog.valueMeaningsProblem(row.meanings);
        row.meaningsError = problem ? this.text(`${problem}Short`) : "";
        this.model.setProperty("/issues", "");
        this.model.refresh();
    }

    public onFieldSearch(): void {
        this.applyFilter();
    }

    public onFieldFilter(event: Event): void {
        this.model.setProperty("/filter", (event.getSource() as SegmentedButton).getSelectedKey());
        this.applyFilter();
    }

    /**
     * Ticks Read -- and only Read -- for the fields listed now that are not
     * readable yet. Never a field marked as personal data: those are ticked
     * one by one. The button says how many before it is pressed, and what
     * was done can be undone.
     */
    public onReadAll(): void {
        const targets = this.rows().filter((row) => (
            row.selectable !== true && row.personal_data !== true && this.matches(row)
        ));
        if (!targets.length) {
            return;
        }
        targets.forEach((row) => {
            row.selectable = true;
            row.note = "";
        });
        this.readAllTicked = targets.map((row) => row.index);
        const done = this.text(targets.length === 1 ? "odataReadAllDoneOne" : "odataReadAllDone", [targets.length]);
        this.model.setProperty("/readAllDone", done);
        this.ticksChanged(targets.some((row) => row.isKey));
        this.say(done);
    }

    public onReadAllUndo(): void {
        const rows = this.rows();
        const undone = this.readAllTicked.map((index) => rows[index]).filter((row) => row && row.selectable === true);
        undone.forEach((row) => {
            row.selectable = false;
            // Filter cannot stay on a field that is no longer readable.
            row.filterable = false;
            this.tag(row);
        });
        this.readAllTicked = [];
        this.model.setProperty("/readAllDone", "");
        this.ticksChanged(undone.some((row) => row.isKey));
        this.say(this.text("odataReadAllUndone", [undone.length]));
        // The link that was pressed went with its strip.
        (this.view.byId("entityFieldSearch") as Control | undefined)?.focus();
    }

    // --- example queries ----------------------------------------------------

    private setExamples(examples: ExampleRow[]): void {
        examples.forEach((row, index) => {
            row.number = index + 1;
        });
        this.model.setProperty("/examples", examples);
        this.model.setProperty("/issues", "");
    }

    public onAddExample(): void {
        this.setExamples((this.model.getProperty("/examples") as ExampleRow[]).concat([{
            description: "", select: "", filter: "", orderby: "", top: "", warning: "", number: 0
        }]));
    }

    public onRemoveExample(event: Event): void {
        const row = (event.getSource() as Control).getBindingContext("entity")!.getObject() as ExampleRow;
        this.setExamples((this.model.getProperty("/examples") as ExampleRow[]).filter((candidate) => candidate !== row));
        (this.view.byId("entityAddExample") as Control | undefined)?.focus();
    }

    public onExampleEdit(): void {
        this.model.setProperty("/issues", "");
        this.showExampleWarnings();
    }

    // --- leaving ------------------------------------------------------------

    public onHeadEdit(): void {
        this.model.setProperty("/errors", {});
        this.model.setProperty("/issues", "");
    }

    /**
     * Writes the entity set back, unless the server would refuse it: then
     * the dialog stays, says what is wrong (the first few, and how many
     * more), marks the field where it has one and shows the tab it is on.
     */
    public onApply(): void {
        const rows = this.rows();
        const messages: string[] = [];
        const errors: Record<string, string> = {};
        const unparsed: number[] = [];
        let tab = "";
        rows.forEach((row) => {
            // Only text that was typed: stored pairs are kept as they are.
            const typed = row.meanings !== odataCatalog.formatValueMeanings(this.source.fields[row.index].values);
            const problem = typed ? odataCatalog.valueMeaningsProblem(row.meanings) : "";
            row.meaningsError = problem ? this.text(`${problem}Short`) : "";
            if (problem) {
                unparsed.push(row.index);
                messages.push(this.text(problem, [row.name]));
                tab = tab || "fields";
            }
        });
        const entitySet = this.current();
        const others = this.context.definition.entity_sets
            .filter((candidate) => candidate !== this.original).map((candidate) => candidate.name);
        odataCatalog.entitySetIssues(entitySet, others).forEach((issue) => {
            const value = /^fields\.(\d+)\.values\./.exec(issue.loc);
            if (value && unparsed.indexOf(Number(value[1])) !== -1) {
                return;
            }
            const message = this.text(HERE[issue.key] ?? issue.key, issue.args);
            messages.push(message);
            if (issue.loc === "name" || issue.loc === "title" || issue.loc === "description") {
                errors[issue.loc] = message;
            }
            tab = tab || (/^(fields|navigations|examples)/.exec(issue.loc)?.[1] ?? "");
        });
        this.model.setProperty("/errors", errors);
        if (!messages.length) {
            this.model.setProperty("/issues", "");
            this.close({ action: "apply", entitySet });
            return;
        }
        const shown = messages.slice(0, ISSUE_CAP).join(" ")
            + (messages.length > ISSUE_CAP ? ` ${this.text("odataEntityMoreIssues", [messages.length - ISSUE_CAP])}` : "");
        const issues = this.text("odataEntityNotApplied", [shown]);
        this.model.setProperty("/issues", issues);
        if (tab) {
            this.model.setProperty("/tab", tab);
        }
        this.model.refresh();
        this.say(issues, InvisibleMessageMode.Assertive);
    }

    /** Cancel, and Escape: asks before unapplied changes are discarded. */
    public onCancel(): void {
        if (!this.isDirty()) {
            this.close(undefined);
            return;
        }
        const discard = this.text("odataDiscard");
        const keep = this.text("odataKeepEditing");
        MessageBox.warning(this.text("odataEntityDiscard"), {
            title: this.text("odataUnsavedTitle"),
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

    /**
     * Removes the entity set from the service, after asking. Not while an
     * operation is bound to it: the server refuses a definition in which an
     * operation names an entity set that is not there.
     */
    public onRemove(): void {
        const title = odataCatalog.titleOf(this.source);
        const bound = odataCatalog.boundOperations(this.context.definition, this.source.name);
        if (bound.length) {
            MessageBox.error(this.text("odataRemoveEntityBound", [title, this.source.name, bound.join(", ")]));
            return;
        }
        const remove = this.text("odataRemove");
        const keep = this.text("odataKeepEntitySet");
        MessageBox.warning(this.text("odataRemoveEntityConfirm", [title, this.source.name]), {
            title: this.text("odataRemoveEntitySet"),
            actions: [remove, keep],
            emphasizedAction: keep,
            initialFocus: keep,
            onClose: (action: string | null) => {
                if (action === remove) {
                    this.close({ action: "remove" });
                }
            }
        });
    }
}
