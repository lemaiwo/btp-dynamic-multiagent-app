import MessageBox from "sap/m/MessageBox";
import inaEntry from "../../model/inaEntry";
import type { InaEntry, InaEntryRow } from "../../model/inaEntry";
import type JSONModel from "sap/ui/model/json/JSONModel";
import type Event from "sap/ui/base/Event";
import type Control from "sap/ui/core/Control";
import type AdminService from "../../service/AdminService";
import type { InaQuerySummary } from "../../service/inaTypes";
import type { McpServer } from "../../service/types";

/** What the part needs from the agent page that holds the server dialog. */
export interface InaEntryHost {
    /** The dialog's model ("server"). */
    serverModel(): JSONModel;
    /** The page's model ("agent"): its servers and its run endpoint. */
    agentModel(): JSONModel;
    service(): AdminService;
    text(key: string, args?: string[]): string;
    /** The query box, once the dialog fragment is loaded. */
    box(): { setSelectedKeys(keys: string[]): void } | undefined;
    /** The row being edited in the dialog; -1 while a new one is added. */
    editingIndex(): number;
}

/** The state of the entry under `server>/ina`, apart from `server>/oauth`
 *  so that another toolset and back cannot lose the selection and nothing
 *  of it leaks into another toolset's block. */
export interface InaEntryState {
    queries: string[];
    loaded: boolean;
    loadError: string;
    options: { key: string; text: string; identity: string }[];
    empty: string;
    duplicate: string;
    missing: string;
    disabled: string;
    warning: string;
}

/**
 * The server dialog's "Analytical queries (InA)" entry (design addendum (k),
 * part 2 point 3): a box of the registered queries with who each runs as,
 * what is not registered or disabled, the scheduled-run warning, and a
 * second entry refused. Kept apart from `AgentDetail.controller.ts`, which
 * only hands its events over; the rules are `model/inaEntry.ts`.
 */
export default class InaEntryPart {

    /** The registered queries as read for the open dialog; null while they
     *  are not read or could not be read. */
    private queries: InaQuerySummary[] | null = null;
    /** Counts dialog openings, so an answer for an earlier one is dropped. */
    private opening = 0;

    public constructor(private readonly host: InaEntryHost) {}

    /**
     * The state a dialog opened for `server` starts from. A stored entry is
     * shown with the list read first, so that every stored name has its
     * item from the start; any other server starts with nothing selected,
     * whatever the last dialog showed.
     */
    public async begin(server: McpServer): Promise<InaEntryState> {
        const opening = ++this.opening;
        this.queries = null;
        const isIna = inaEntry.isInaUrl(server.url);
        const loadError = isIna ? await this.load(opening) : "";
        return {
            queries: inaEntry.clean(isIna ? server.oauth as Record<string, unknown> : undefined).queries,
            loaded: isIna, loadError, options: [], empty: "", duplicate: "", missing: "", disabled: "", warning: ""
        };
    }

    /** "Analytical queries (InA)" was picked in a dialog opened for
     *  something else: read the list once, then show. The selection is not
     *  touched. */
    public async enter(): Promise<void> {
        const model = this.host.serverModel();
        if (model.getProperty("/ina/loaded") !== true) {
            model.setProperty("/ina/loaded", true);
            const opening = this.opening;
            const loadError = await this.load(opening);
            if (opening !== this.opening) {
                return;
            }
            model.setProperty("/ina/loadError", loadError);
            this.showOptions();
        }
        this.show();
    }

    /** A tick, a selection or a removed token: the box's own keys are taken
     *  as the selection, whatever the binding has done by now. */
    public onChange(event: Event): void {
        const source = event.getSource() as Control & { getSelectedKeys?: () => string[] };
        if (typeof source.getSelectedKeys === "function") {
            this.host.serverModel().setProperty("/ina/queries", source.getSelectedKeys().slice());
        }
        this.show();
    }

    /** The block OK puts into the form: exactly `{queries}`. */
    public block(): InaEntry {
        return { queries: this.selection() };
    }

    /** The choices of the box, then the selection written again: the box
     *  matches keys to items when the keys are set. The model is the
     *  selection; the box only shows it. */
    public showOptions(): void {
        const model = this.host.serverModel();
        const selected = this.selection();
        model.setProperty("/ina/options", inaEntry.options(this.queries, { queries: selected }).map((option) => ({
            key: option.key,
            text: option.title,
            identity: option.state === "missing" ? this.host.text("inaEntryRowMissing")
                : option.state === "unknown" ? ""
                    : option.state === "disabled" ? this.host.text("inaEntryOptionDisabled", [this.runsAs(option.userContext)])
                        : this.runsAs(option.userContext)
        })));
        model.setProperty("/ina/queries", selected.slice());
        // The box is told as well: an equal list is no change for the
        // binding, and the box may still hold the keys of the dialog shown
        // before.
        this.host.box()?.setSelectedKeys(selected.slice());
    }

    /** Everything the entry says about its selection. */
    public show(): void {
        const model = this.host.serverModel();
        if (!inaEntry.isInaUrl(model.getProperty("/url") as string)) {
            return;
        }
        const agent = this.host.agentModel();
        const rows = this.rows();
        model.setProperty("/ina/empty", this.queries && this.queries.length === 0 ? this.host.text("inaEntryNone") : "");

        const other = inaEntry.entryIndex(agent.getProperty("/data/mcp_servers") as McpServer[], this.host.editingIndex());
        model.setProperty("/ina/duplicate", other === -1 ? "" : this.host.text("inaEntryDuplicate", [String(other + 1)]));

        const missing = rows.filter((row) => row.state === "missing").map((row) => row.name);
        model.setProperty("/ina/missing", this.plural(missing, "inaEntryMissingOne", "inaEntryMissingMany"));

        const disabled = rows.filter((row) => row.state === "disabled").map((row) => row.title);
        model.setProperty("/ina/disabled", this.plural(disabled, "inaEntryDisabledOne", "inaEntryDisabledMany"));

        // A run started through the run endpoint has no signed-in user.
        const asUser = agent.getProperty("/data/expose_api") === true
            ? rows.filter((row) => row.userContext && row.state !== "missing").map((row) => row.title) : [];
        model.setProperty("/ina/warning", this.plural(asUser, "inaEntryScheduledWarning", "inaEntryScheduledWarningMany"));
    }

    /**
     * OK on the entry. False when the dialog must stay open: a second
     * entry, no query, or a query that is no longer registered (asked
     * about; only the admin's answer removes it, then `again` is OK once
     * more).
     */
    public confirm(again: () => void): boolean {
        const model = this.host.serverModel();
        this.show();
        const duplicate = model.getProperty("/ina/duplicate") as string;
        if (duplicate) {
            MessageBox.error(duplicate);
            return false;
        }
        const rows = this.rows();
        const missing = rows.filter((row) => row.state === "missing").map((row) => row.name);
        if (missing.length > 0) {
            const remove = this.host.text("inaEntryMissingRemove");
            MessageBox.warning(this.host.text("inaEntryMissingConfirm", [InaEntryPart.quoted(missing)]), {
                title: this.host.text("inaEntryMissingTitle"),
                actions: [remove, MessageBox.Action.CANCEL],
                emphasizedAction: remove,
                initialFocus: MessageBox.Action.CANCEL,
                onClose: (action: string | null) => {
                    if (action !== remove) {
                        return;
                    }
                    model.setProperty("/ina/queries", this.selection().filter((name) => missing.indexOf(name) === -1));
                    this.showOptions();
                    this.show();
                    again();
                }
            });
            return false;
        }
        if (rows.length === 0) {
            MessageBox.error(this.host.text(this.queries && this.queries.length === 0 ? "inaEntryNone" : "inaEntryNoQueries"));
            return false;
        }
        return true;
    }

    /** Reads the registered queries for the opening `opening`. Returns the
     *  text to show when that failed; never a dialog of its own, because the
     *  admin may be here for another toolset. An answer for an earlier
     *  opening changes nothing. */
    private async load(opening: number): Promise<string> {
        try {
            const queries = await this.host.service().listInaQueries();
            if (opening === this.opening) {
                this.queries = queries;
            }
            return "";
        } catch {
            if (opening === this.opening) {
                this.queries = null;
            }
            return this.host.text("inaEntryLoadFailed");
        }
    }

    private selection(): string[] {
        return inaEntry.clean({ queries: this.host.serverModel().getProperty("/ina/queries") as unknown }).queries;
    }

    private rows(): InaEntryRow[] {
        return inaEntry.rowsOf(this.queries, { queries: this.selection() });
    }

    private runsAs(userContext: boolean): string {
        return this.host.text("inaEntryRunsAs", [this.host.text(userContext ? "inaEntryUser" : "inaEntryTechnical")]);
    }

    private plural(names: string[], one: string, many: string): string {
        return names.length === 0 ? "" : names.length === 1 ? this.host.text(one, [names[0]])
            : this.host.text(many, [InaEntryPart.quoted(names)]);
    }

    private static quoted(names: string[]): string {
        return names.map((name) => `"${name}"`).join(", ");
    }
}
