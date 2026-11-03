import Fragment from "sap/ui/core/Fragment";
import InvisibleMessage from "sap/ui/core/InvisibleMessage";
import Filter from "sap/ui/model/Filter";
import { InvisibleMessageMode, ValueState } from "sap/ui/core/library";
import odataDestinations, { type DestinationNotice, type DestinationsState, type Translate } from "../model/odataDestinations";
import type AdminService from "../service/AdminService";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type View from "sap/ui/core/mvc/View";
import type Input from "sap/m/Input";
import type { Input$LiveChangeEvent } from "sap/m/Input";
import type Item from "sap/ui/core/Item";
import type SelectDialog from "sap/m/SelectDialog";
import type { SelectDialog$ConfirmEvent, SelectDialog$LiveChangeEvent } from "sap/m/SelectDialog";
import type ListBinding from "sap/ui/model/ListBinding";

/** What one destination field is judged against, and where it says so. */
export interface DestinationTarget {
    /** Where in the `dest` model the field's `notice` and `noticeText`
     *  go: "" for the root, or a path such as "/create". */
    path: string;
    /** The identity the name is judged for: true is the signed-in user. */
    userContext(): boolean;
    /** False while there is nothing to judge (the form is not shown). */
    judged(): boolean;
    /** What the field's binding has stored. */
    stored(): string;
    /** The field shows an error (a refused save): a notice is not said then. */
    hasError(): boolean;
    /** A name is being typed, or was picked: what a refused save said about it is gone. */
    edited(): void;
}

/**
 * A destination field of the shared destination list
 * (`GET odata/destinations`): the same field as the OData service page's
 * (see `ODataServiceDetail.controller.ts`, where each rule below is
 * explained in full), for the pages that came after it. A plain class, not
 * a controller: it is the controller object of the picker fragment only.
 *
 * The input never completes what is typed (`autocomplete="false"` in the
 * view): it holds what the admin typed or the name the admin picked and
 * nothing else. Not on a phone, the listed names that hold the typed text
 * are offered under it; on a phone it is a plain text field and the value
 * help opens the list as a dialog (`ODataDestinationPicker`).
 *
 * The owner sets a `dest` model of `{items, hint}` plus `{notice,
 * noticeText}` at each target's path, binds the field's `suggestionItems`
 * to `dest>/items` and its value state to `formatState` /
 * `formatStateText`, and calls `attach` once per field and `load` once per
 * visit. The list is a help, never a rule: nothing here stands in the way
 * of a save.
 */
export default class DestinationField {

    private readonly targets = new Map<Input, DestinationTarget>();
    private destinations: DestinationsState = "loading";
    /** Counts the reads of the list: the answer of an earlier one is dropped. */
    private count = 0;
    private picker?: SelectDialog;
    private pickerLoading?: Promise<SelectDialog>;
    private pickerField?: Input;
    /** What a field held when the value help showed its list; gone with the
     *  next key that changes the text, the next `change`, and focus out. */
    private heldAtValueHelp?: { field: Input; value: string };

    public constructor(
        private readonly view: View,
        private readonly model: JSONModel,
        private readonly service: () => AdminService,
        private readonly text: Translate
    ) {}

    /** An error of the field comes before what the list says about the name. */
    public static formatState(error: string | undefined, notice: DestinationNotice | undefined): ValueState {
        return error ? ValueState.Error : ValueState[odataDestinations.noticeState(notice ?? "")];
    }

    public static formatStateText(error: string | undefined, words: string | undefined): string {
        return error || words || "";
    }

    /** Makes `field` a destination field judged as `target` says. */
    public attach(field: Input, target: DestinationTarget): void {
        this.targets.set(field, target);
        field.setFilterFunction((typed: string, item: Item) => odataDestinations.suggests(typed, item.getText()));
        field.attachValueHelpRequest(() => this.onValueHelp(field));
        field.attachLiveChange((event: Input$LiveChangeEvent) => this.onLiveChange(field, event));
        field.attachChange(() => {
            this.heldAtValueHelp = undefined;
            target.edited();
            this.judge(field);
        });
        // These run after the input's own handlers.
        field.addEventDelegate({
            onfocusout: () => {
                this.heldAtValueHelp = undefined;
                this.storeShown(field);
                this.judge(field);
            },
            onsapenter: () => this.storeShown(field),
            onsapescape: () => this.restoreHeld(field)
        });
    }

    /** Reads the list. Quiet whatever happens: without it the field is a
     *  text field with a hint. Only `dest` is written, never a form. */
    public async load(): Promise<void> {
        const count = ++this.count;
        this.heldAtValueHelp = undefined;
        this.show("loading");
        let state: DestinationsState;
        try {
            state = odataDestinations.read(await this.service().listODataDestinations());
        } catch {
            state = "unavailable";
        }
        if (count === this.count) {
            this.show(state, true);
        }
    }

    /**
     * Says what the list knows about the name in `field` for its target's
     * identity. `announce`: the cause is not the field itself (Runs as
     * changed, the list arrived), so a new notice is said; `always`: said
     * even when it is the notice that was there before.
     */
    public check(field: Input, announce = false, always = false, name?: string): void {
        const target = this.targets.get(field);
        if (!target) {
            return;
        }
        const notice: DestinationNotice = target.judged()
            ? odataDestinations.notice(this.destinations, name ?? target.stored(), target.userContext()) : "";
        const words = odataDestinations.noticeText(
            notice, this.text, odataDestinations.listedAs(this.destinations, name ?? target.stored())
        );
        const changed = notice !== this.model.getProperty(`${target.path}/notice`)
            || words !== this.model.getProperty(`${target.path}/noticeText`);
        this.model.setProperty(`${target.path}/notice`, notice);
        this.model.setProperty(`${target.path}/noticeText`, words);
        if (announce && words && (changed || always) && !target.hasError()) {
            InvisibleMessage.getInstance().announce(words, InvisibleMessageMode.Polite);
        }
    }

    /**
     * Makes what `field` shows the stored name, where the input itself may
     * not have (it writes "" the moment the field is emptied, and a name
     * brought back by Escape or the list is no `change` to it). Call it
     * before a save reads the form: a click on a button does not always
     * move the focus.
     */
    public storeShown(field: Input): void {
        const target = this.targets.get(field);
        const shown = field.getValue();
        if (target && shown !== (target.stored() ?? "")) {
            // Through the field: its binding writes the model.
            field.setValue(shown);
        }
    }

    /** Clears what the list said about every field (a form was closed). */
    public clear(): void {
        this.heldAtValueHelp = undefined;
        this.targets.forEach((target) => {
            this.model.setProperty(`${target.path}/notice`, "");
            this.model.setProperty(`${target.path}/noticeText`, "");
        });
    }

    private show(state: DestinationsState, announce = false): void {
        this.destinations = state;
        this.model.setProperty("/items", odataDestinations.choices(state, this.text));
        this.model.setProperty("/hint", odataDestinations.hint(state, this.text));
        if (announce && state === "unavailable") {
            InvisibleMessage.getInstance().announce(odataDestinations.hint(state, this.text), InvisibleMessageMode.Polite);
        }
        this.targets.forEach((_target, field) => this.check(field, announce));
    }

    private judge(field: Input): void {
        this.check(field, false, false, field.getValue());
    }

    /** Escape after the value help: the field gets back what it held. */
    private restoreHeld(field: Input): void {
        const held = this.heldAtValueHelp;
        this.heldAtValueHelp = undefined;
        if (held && held.field === field && field.getValue() !== held.value) {
            field.setValue(held.value);
            this.judge(field);
        }
    }

    /** A key in the field: while a name is typed nothing is said about it,
     *  except after Escape, which puts back a name and sends no `change`. */
    private onLiveChange(field: Input, event: Input$LiveChangeEvent): void {
        this.targets.get(field)?.edited();
        if (event.getParameter("escPressed")) {
            this.storeShown(field);
            this.judge(field);
        } else {
            this.heldAtValueHelp = undefined;
            const path = this.targets.get(field)?.path ?? "";
            this.model.setProperty(`${path}/notice`, "");
            this.model.setProperty(`${path}/noticeText`, "");
        }
    }

    /** The value help icon or F4: the whole list, under the field or as a
     *  dialog where it has no list. */
    private onValueHelp(field: Input): void {
        if (field.getShowSuggestion()) {
            this.heldAtValueHelp = { field, value: field.getValue() };
            field.showItems(() => true);
        } else {
            void this.openPicker(field);
        }
    }

    private async openPicker(field: Input): Promise<void> {
        this.pickerField = field;
        if (this.pickerLoading) {
            return;
        }
        if (!this.picker) {
            this.pickerLoading = Fragment.load({
                id: this.view.getId(), name: "com.agent.admin.fragment.ODataDestinationPicker", controller: this
            }) as Promise<SelectDialog>;
            try {
                this.picker = await this.pickerLoading;
            } finally {
                this.pickerLoading = undefined;
            }
            this.view.addDependent(this.picker);
        }
        (this.picker.getBinding("items") as ListBinding).filter([]);
        this.picker.open("");
    }

    /** The picker's search field: the names that hold the text. */
    public onDestinationPickerSearch(event: SelectDialog$LiveChangeEvent): void {
        const typed = event.getParameter("value") ?? "";
        (event.getParameter("itemsBinding") as ListBinding).filter(typed ? [new Filter({
            path: "name", test: (name: string) => odataDestinations.suggests(typed, name)
        })] : []);
    }

    /** A name was tapped in the picker: it is the field's name now. */
    public onDestinationPicked(event: SelectDialog$ConfirmEvent): void {
        const item = event.getParameter("selectedItem");
        const field = this.pickerField;
        if (item && field) {
            field.setValue(item.getTitle());
            this.targets.get(field)?.edited();
            this.judge(field);
        }
    }
}
