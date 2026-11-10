import MessageBox from "sap/m/MessageBox";
import JSONModel from "sap/ui/model/json/JSONModel";
import { ValueState } from "sap/ui/core/library";
import hanaEntry from "../../model/hanaEntry";
import type { HanaEntry, HanaRefusal } from "../../model/hanaEntry";
import DestinationField from "../DestinationField";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type View from "sap/ui/core/mvc/View";
import type Input from "sap/m/Input";
import type Table from "sap/m/Table";
import type ColumnListItem from "sap/m/ColumnListItem";
import type AdminService from "../../service/AdminService";
import type { McpServer } from "../../service/types";

/** What the part needs from the agent page that holds the server dialog. */
export interface HanaEntryHost {
    /** The dialog's model ("server"). */
    serverModel(): JSONModel;
    /** The page's model ("agent"): its servers. */
    agentModel(): JSONModel;
    view(): View;
    service(): AdminService;
    text(key: string, args?: string[]): string;
    /** The row being edited in the dialog; -1 while a new one is added. */
    editingIndex(): number;
}

/** One system row of the dialog's table. */
export interface HanaRow {
    name: string;
    destination: string;
    nameState: ValueState;
    nameText: string;
    destinationState: ValueState;
    destinationText: string;
}

/** The state of the entry under `server>/hana`, apart from `server>/oauth`
 *  so that another toolset and back cannot lose the rows and nothing of
 *  them leaks into another toolset's block. */
export interface HanaEntryState {
    rows: HanaRow[];
    allowSelect: boolean;
    /** What the entry said when the dialog opened: the confirmation asks
     *  only when SELECT is switched on where it was not. */
    storedAllowSelect: boolean;
    duplicate: string;
}

/** A refusal of the server, with its text, kept for the server row it names. */
export interface HanaServerRefusal extends HanaRefusal {
    message: string;
}

/** The model the destination fields' list is read into. */
const DEST_MODEL = "hanaDest";

/**
 * The server dialog's "SAP HANA (monitoring)" entry: a table of systems (a
 * name the model sees and the destination of that one database), Add and
 * Remove, the switch "Allow SELECT on monitoring views" (asked before it is
 * switched on), and a second entry refused. Kept apart from
 * `AgentDetail.controller.ts`, which only hands its events over; the rules
 * are `model/hanaEntry.ts`, which mirrors `agents/hana/config.py`.
 *
 * The destination fields are the shared `DestinationField` (suggestions and
 * value help from `GET odata/destinations`). They are not judged against an
 * identity: a HANA destination holds a database user, which the list's
 * notices (signed-in user or technical) do not describe, so the list is a
 * help only.
 */
export default class HanaEntryPart {

    private readonly destinations: DestinationField;
    private readonly attached = new WeakSet<Input>();

    public constructor(private readonly host: HanaEntryHost) {
        const model = new JSONModel({ items: [], hint: "", notice: "", noticeText: "" });
        host.view().setModel(model, DEST_MODEL);
        this.destinations = new DestinationField(host.view(), model, () => host.service(),
            (key: string, args?: unknown[]) => host.text(key, (args ?? []).map((arg) => String(arg))));
    }

    /**
     * The state a dialog opened for `server` starts from: a stored entry's
     * systems as stored (never trimmed), any other server one empty row.
     * `refusal` is what the server said about this row at the last save; it
     * marks the system and field it names.
     */
    public begin(server: McpServer, refusal?: HanaServerRefusal | null): HanaEntryState {
        const isHana = hanaEntry.isHanaUrl(server.url);
        const stored = hanaEntry.clean(isHana ? server.oauth as Record<string, unknown> : undefined);
        const rows = stored.systems.map((system) => HanaEntryPart.row(system.name, system.destination));
        if (rows.length === 0) {
            rows.push(HanaEntryPart.row("", ""));
        }
        if (refusal && rows[refusal.system]) {
            HanaEntryPart.mark(rows[refusal.system], refusal.field || "name", refusal.message);
        }
        if (isHana) {
            void this.destinations.load();
        }
        const allow = stored.allow_select === true;
        return { rows, allowSelect: allow, storedAllowSelect: allow, duplicate: "" };
    }

    /** "SAP HANA (monitoring)" was picked in a dialog opened for something else. */
    public enter(): void {
        void this.destinations.load();
        this.show();
    }

    /** What the entry says about the agent's other servers. */
    public show(): void {
        const model = this.host.serverModel();
        if (!hanaEntry.isHanaUrl(model.getProperty("/url") as string)) {
            return;
        }
        const other = hanaEntry.entryIndex(
            this.host.agentModel().getProperty("/data/mcp_servers") as McpServer[], this.host.editingIndex());
        model.setProperty("/hana/duplicate", other === -1 ? "" : this.host.text("hanaEntryDuplicate", [String(other + 1)]));
    }

    public onAdd(): void {
        const rows = this.rows();
        if (rows.length < hanaEntry.MAX_HANA_SYSTEMS) {
            this.host.serverModel().setProperty("/hana/rows", rows.concat([HanaEntryPart.row("", "")]));
        }
    }

    public onRemove(event: Event): void {
        const path = (event.getSource() as Control).getBindingContext("server")?.getPath() ?? "";
        const index = Number(path.substring(path.lastIndexOf("/") + 1));
        const rows = this.rows();
        if (index >= 0 && index < rows.length) {
            rows.splice(index, 1);
            this.host.serverModel().setProperty("/hana/rows", rows);
        }
    }

    /** A key in a row: what a refused save said about that row is gone. */
    public onEdited(event: Event): void {
        const path = (event.getSource() as Control).getBindingContext("server")?.getPath();
        if (path) {
            HanaEntryPart.clearRow(this.host.serverModel(), path);
        }
    }

    /**
     * The switch. Switched on where the entry did not already allow it, it
     * asks first, naming the systems; Cancel is the default and puts it
     * back off.
     */
    public onAllowSelectChange(event: Event): void {
        const model = this.host.serverModel();
        const on = (event.getSource() as unknown as { getState(): boolean }).getState() === true;
        model.setProperty("/hana/allowSelect", on);
        if (!on || !hanaEntry.selectNewlyAllowed(
            { allow_select: model.getProperty("/hana/storedAllowSelect") === true }, { allow_select: true })) {
            return;
        }
        const names = this.rows().map((row) => row.name).filter((name) => name !== "");
        const which = names.length > 0 ? names.map((name) => `"${name}"`).join(", ")
            : this.host.text("hanaAllowSelectTheseSystems");
        const allow = this.host.text("hanaAllowSelectYes");
        MessageBox.warning(this.host.text("hanaAllowSelectConfirm", [which]), {
            title: this.host.text("hanaAllowSelectTitle"),
            actions: [allow, MessageBox.Action.CANCEL],
            emphasizedAction: allow,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action !== allow) {
                    model.setProperty("/hana/allowSelect", false);
                }
            }
        });
    }

    /** The table's rows were (re)drawn: each new destination field becomes
     *  a shared destination field. */
    public onRowsUpdated(event: Event): void {
        const table = event.getSource() as Table;
        table.getItems().forEach((item) => {
            const field = (item as ColumnListItem).getCells()[1] as Input | undefined;
            if (!field || this.attached.has(field)) {
                return;
            }
            this.attached.add(field);
            this.destinations.attach(field, {
                path: "",
                userContext: () => false,
                judged: () => false,
                stored: () => {
                    const path = field.getBindingContext("server")?.getPath();
                    return path ? String(this.host.serverModel().getProperty(`${path}/destination`) ?? "") : "";
                },
                hasError: () => field.getValueState() === ValueState.Error,
                edited: () => {
                    const path = field.getBindingContext("server")?.getPath();
                    if (path) {
                        HanaEntryPart.clearRow(this.host.serverModel(), path);
                    }
                }
            });
        });
    }

    /**
     * OK on the entry. False when the dialog must stay open: a second entry,
     * or a block the server would refuse (the row and field it names are
     * marked).
     */
    public confirm(table: Table | undefined): boolean {
        const model = this.host.serverModel();
        table?.getItems().forEach((item) => {
            const field = (item as ColumnListItem).getCells()[1] as Input | undefined;
            if (field) {
                this.destinations.storeShown(field);
            }
        });
        this.show();
        const duplicate = model.getProperty("/hana/duplicate") as string;
        if (duplicate) {
            MessageBox.error(duplicate);
            return false;
        }
        const problem = hanaEntry.problem(this.block() as McpServer["oauth"], "destination");
        if (problem) {
            if (problem.system !== undefined) {
                const rows = this.rows();
                if (rows[problem.system]) {
                    HanaEntryPart.mark(rows[problem.system], problem.field || "name", problem.text);
                    model.setProperty("/hana/rows", rows);
                }
            }
            MessageBox.error(problem.text);
            return false;
        }
        return true;
    }

    /** The block OK puts into the form: exactly `{systems, allow_select?}`. */
    public block(): HanaEntry {
        return hanaEntry.clean({
            systems: this.rows().map((row) => ({ name: row.name, destination: row.destination })),
            allow_select: this.host.serverModel().getProperty("/hana/allowSelect") === true
        });
    }

    /** The dialog closed: what the list said about its fields is gone. */
    public clear(): void {
        this.destinations.clear();
    }

    private rows(): HanaRow[] {
        const rows = this.host.serverModel().getProperty("/hana/rows") as HanaRow[] | undefined;
        return (rows ?? []).map((row) => ({ ...row }));
    }

    private static row(name: string, destination: string): HanaRow {
        return { name, destination, nameState: ValueState.None, nameText: "",
            destinationState: ValueState.None, destinationText: "" };
    }

    private static mark(row: HanaRow, field: "name" | "destination", text: string): void {
        if (field === "destination") {
            row.destinationState = ValueState.Error;
            row.destinationText = text;
        } else {
            row.nameState = ValueState.Error;
            row.nameText = text;
        }
    }

    private static clearRow(model: JSONModel, path: string): void {
        model.setProperty(`${path}/nameState`, ValueState.None);
        model.setProperty(`${path}/nameText`, "");
        model.setProperty(`${path}/destinationState`, ValueState.None);
        model.setProperty(`${path}/destinationText`, "");
    }
}
