import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import { ValueState } from "sap/ui/core/library";
import Fragment from "sap/ui/core/Fragment";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import validators, { validateDeep } from "../model/validators";
import oauthConfig from "../model/oauthConfig";
import odataEntry from "../model/odataEntry";
import bitbucketEntry from "../model/bitbucketEntry";
import type { ODataEntryGiven, ODataEntryOpens, ODataEntryRow } from "../model/odataEntry";
import { AUTH_MODE_TEXT_KEYS, BUILTINS, authModesFor, findBuiltin } from "../model/builtins";
import formatter from "../model/formatter";
import { LAST_RUNS_LIMIT, RUN_REFRESH_DELAYS_MS, canonical, isDirty, runsCountLabel } from "../model/runsPanel";
import type Dialog from "sap/m/Dialog";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Event from "sap/ui/base/Event";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type Control from "sap/ui/core/Control";
import type {
    Agent, AgentInput, AuthMode, CredentialStatus, DeepConfig, JobRun, McpServer,
    ODataDefinition, ODataServiceSummary, ReloadOutcome, WhereUsedPeer, WhereUsedStep, WhereUsedWorkflow
} from "../service/types";
import { DEEP_DEFAULTS } from "../service/types";

const EMPTY_AGENT: AgentInput = {
    name: "",
    description: "",
    instructions: "",
    mcp_servers: [],
    skills: [],
    enabled: true,
    expose_chat: true,
    expose_api: false,
    api_slug: "",
    run_as_principal: "",
    run_prompt: "",
    run_timeout_seconds: 1800,
    peers: [],
    model_name: "",
    deep: { ...DEEP_DEFAULTS }
};

/** One entry of the model `<Select>`: a real model name, or the blank
 * "use the active model" option, or a stored override the current model
 * list no longer carries. */
interface ModelOption {
    key: string;
    text: string;
}

/**
 * @namespace com.agent.admin.controller
 */
export default class AgentDetail extends BaseController {

    public formatter = formatter;

    private agentId?: number;
    private serverDialog?: Dialog;
    /** Index being edited in the server dialog; -1 means "adding a new one". */
    private editingServerIndex = -1;
    private publicBaseUrl = "";
    /** `canonical()` of `/data` as loaded, for the Refresh button's dirty
     * check; undefined for a new agent or one that failed to load. */
    private snapshot?: string;
    /** Timers armed by Run now to reload the last-runs panel. */
    private runRefreshTimers: ReturnType<typeof setTimeout>[] = [];
    // --- odata ---
    /** The agent's servers as the server has them ([] for a new agent): what
     *  Save compares with to tell whether it newly gives writes. */
    private storedServers: McpServer[] = [];
    /** The name the agent is stored with (none for a new one): the other
     *  agents' peer lists hold this name until a rename is saved. */
    private storedName: string | undefined;
    /** The catalogue as read for the open server dialog; null while it is
     *  not read or could not be read. */
    private odataCatalogue: ODataServiceSummary[] | null = null;
    /** Definitions read for the open dialog, by "=<name>"; null = the read
     *  failed, absent = not read yet. */
    private odataDefinitions: Record<string, ODataDefinition | null> = {};
    /** Counts dialog openings, so an answer for an earlier one is dropped. */
    private odataOpening = 0;
    /** True from a Save press until its request is answered or its question
     *  is cancelled: one press, one question, one request. */
    private saving = false;

    public onInit(): void {
        this.setModel(new JSONModel({
            title: "",
            data: JSON.parse(JSON.stringify(EMPTY_AGENT)) as AgentInput,
            availableSkills: [],
            availableAgents: [],
            availableModels: [],
            errors: {},
            // --- run / refresh / last runs ---
            isExisting: false,
            canRun: false,
            runBusy: false,
            runs: [],
            runsCount: runsCountLabel(0)
        }), "agent");
        this.setModel(new JSONModel({}), "server");

        this.getRouter().getRoute("agentDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const id = (event.getParameter("arguments") as { agentId: string }).agentId;
            void this.load(id);
        });
    }

    public onExit(): void {
        this.clearRunRefreshTimers();
    }

    private load(id: string): Promise<void> {
        return this.withBusy(async () => {
            const model = this.getModel("agent") as JSONModel;
            model.setProperty("/errors", {});
            this.clearRunRefreshTimers();
            this.snapshot = undefined;
            this.storedServers = [];
            this.storedName = undefined;
            this.saving = false;

            // Issued together, not one after another: none of the three reads
            // the others' result, so awaiting them in sequence made opening an
            // agent cost three round trips instead of one. `run()` still
            // reports each failure on its own, and resolves undefined for it,
            // so one failing call no longer decides whether the others are
            // even attempted.
            //
            // Neither list blocks the form from opening: a peer picker with no
            // options, or a model list that fell back to just the stored
            // override, still leaves the agent editable. See the model
            // control's "not currently available" handling below.
            const [skills, agents, modelInfo] = await Promise.all([
                this.run(
                    this.getAdminService().listSkills(),
                    "Could not load the skill list."
                ),
                this.run(
                    this.getAdminService().listAgents(),
                    "Could not load the agent list."
                ),
                this.run(
                    this.getAdminService().getModel(),
                    "Could not load the LLM model list."
                )
            ]);
            model.setProperty("/availableSkills", skills ?? []);
            const availableModelNames = modelInfo?.available ?? [];

            if (id === "new") {
                this.agentId = undefined;
                model.setProperty("/isExisting", false);
                model.setProperty("/canRun", false);
                model.setProperty("/runs", []);
                model.setProperty("/runsCount", runsCountLabel(0));
                model.setProperty("/data", JSON.parse(JSON.stringify(EMPTY_AGENT)) as AgentInput);
                model.setProperty("/title", this.text("newAgent"));
                // Otherwise a credential table left over from whichever agent was
                // open before navigating here would still be showing.
                model.setProperty("/credentials", []);
                model.setProperty("/whereUsed", null); // nothing can refer to it yet
                // A new agent has no name yet, so it excludes nothing from its
                // own peer list — every existing agent is offered.
                model.setProperty("/availableAgents", agents ?? []);
                model.setProperty("/availableModels", this.buildModelOptions(availableModelNames, ""));
                return;
            }

            this.agentId = Number(id);
            model.setProperty("/isExisting", true);
            // Also independent of each other, so also issued together. The
            // base URL is only read further down, and fetching it for an agent
            // that turns out not to exist costs nothing. The last runs load
            // alongside; loadRuns() reports its own failure with a toast and
            // leaves the panel empty, so it can never take the form with it.
            const [agent, config] = await Promise.all([
                this.run(
                    this.getAdminService().getAgent(this.agentId),
                    "Could not load the agent."
                ),
                this.run(
                    this.getAdminService().getConfig(),
                    "Could not read the public base URL."
                ),
                this.loadRuns()
            ]);
            if (!agent) {
                model.setProperty("/canRun", false);
                return;
            }
            // Copy only the input fields; id/created_at/updated_at and the legacy
            // mcp_url/auth_mode pair must not be POSTed back.
            model.setProperty("/data", {
                name: agent.name,
                description: agent.description,
                instructions: agent.instructions,
                mcp_servers: agent.mcp_servers ?? [],
                skills: agent.skills ?? [],
                enabled: agent.enabled,
                expose_chat: agent.expose_chat,
                expose_api: agent.expose_api,
                api_slug: agent.api_slug ?? "",
                run_as_principal: agent.run_as_principal ?? "",
                run_prompt: agent.run_prompt ?? "",
                run_timeout_seconds: agent.run_timeout_seconds ?? 1800,
                peers: agent.peers ?? [],
                model_name: agent.model_name ?? "",
                // --- deep agents --- always resent so the panel can clear a
                // stored config; an older backend without the field gets defaults.
                deep: { ...DEEP_DEFAULTS, ...(agent.deep ?? {}) } as DeepConfig
            } as AgentInput);
            model.setProperty("/title", agent.name);
            // A copy: the form edits its own list, never this one.
            this.storedServers = JSON.parse(JSON.stringify(agent.mcp_servers ?? [])) as McpServer[];
            this.storedName = agent.name;
            this.snapshot = canonical(model.getProperty("/data"));
            model.setProperty("/canRun", AgentDetail.isRunnable(agent));
            // An agent must never be offered itself as a peer.
            model.setProperty("/availableAgents", (agents ?? []).filter((a) => a.name !== agent.name));
            model.setProperty(
                "/availableModels",
                this.buildModelOptions(availableModelNames, agent.model_name ?? "")
            );

            this.publicBaseUrl = config?.public_base_url ?? "";
            void this.loadCredentials();
            void this.loadWhereUsed();
        });
    }

    /**
     * Builds the model `<Select>`'s options: a blank "use the active model"
     * entry, the fetched models, and — only if the stored override is not
     * among them — the override itself, labelled as unavailable.
     *
     * Without that last entry, a `Select` with an unknown `selectedKey` shows
     * blank, which looks like a deliberate "use the active model" and
     * destroys the override on the next save. See `templates/admin.html`'s
     * `renderAgentModelOptions`, where this exact bug was found once already.
     */
    private buildModelOptions(available: string[], selected: string): ModelOption[] {
        const options: ModelOption[] = [{ key: "", text: this.text("useActiveModel") }];
        available.forEach((name) => options.push({ key: name, text: name }));
        if (selected && !available.includes(selected)) {
            options.push({ key: selected, text: this.text("modelNotAvailable", [selected]) });
        }
        return options;
    }

    // --- MCP server dialog -----------------------------------------------
    public onAddServer(): void {
        this.editingServerIndex = -1;
        this.newServer = true;
        this.userContextTouched = false;
        void this.openServerDialog({ url: "", auth_mode: "jwt" });
    }

    public onEditServer(event: Event): void {
        const context = (event.getSource() as Control).getBindingContext("agent");
        const path = context?.getPath() ?? "";
        this.editingServerIndex = Number(path.substring(path.lastIndexOf("/") + 1));
        this.newServer = false;
        const server = context?.getObject() as McpServer;
        void this.openServerDialog(JSON.parse(JSON.stringify(server)) as McpServer);
    }

    /** A server being added (not edited): its user_context starts from the default. */
    private newServer = false;
    /** Set once the user flips the switch, so the default stops following url/mode. */
    private userContextTouched = false;

    public onUserContextToggle(): void {
        this.userContextTouched = true;
    }

    /** New remote destination servers start with "Act as signed-in user" on. */
    private applyUserContextDefault(): void {
        if (!this.newServer || this.userContextTouched) {
            return;
        }
        const serverModel = this.getModel("server") as JSONModel;
        serverModel.setProperty("/oauth/user_context", oauthConfig.defaultUserContext(
            serverModel.getProperty("/auth_mode") as string, serverModel.getProperty("/url") as string));
    }

    private async openServerDialog(server: McpServer): Promise<void> {
        const hasStoredSecret = !!(server.oauth as { has_client_secret?: boolean })?.has_client_secret;
        // --- odata --- The entry's state is kept apart from `oauth` (see the
        // fragment). A stored entry is shown with the catalogue read first,
        // so that every stored name has its item from the start.
        const isOData = odataEntry.isODataUrl(server.url);
        const stored = odataEntry.clean(isOData ? server.oauth as Record<string, unknown> : undefined);
        // --- bitbucket --- As for OData: the entry's own state is kept apart
        // from `oauth`, and an entry stored in another spelling is held in the
        // one the fragment's bindings test for.
        const isBitbucket = bitbucketEntry.isBitbucketUrl(server.url);
        const pins = ((isBitbucket ? server.oauth : undefined) ?? {}) as Record<string, unknown>;
        const blankOAuth = { dcr: false, client_id: "", client_secret: "", uaa_url: "", authorize_url: "", token_url: "", scope: "", mailbox: "", allow_send: false, lookback: "", destination: "", project: "", status: "", api_base: "", labels: "", allow_comment: false, min_score: "", recipients: "", from: "", team: "", channels: "", user_context: false };
        const opening = ++this.odataOpening;
        this.odataCatalogue = null;
        this.odataDefinitions = {};
        const loadError = isOData ? await this.loadODataCatalogue() : "";
        if (opening !== this.odataOpening) {
            return;
        }
        (this.getModel("server") as JSONModel).setData({
            // The fragment shows the services box and "Allow writes" for
            // exactly `builtin:odata`: an entry stored in another spelling
            // (`Builtin:OData`) is held in that one, or OK would write back
            // an `allow_write` the admin never saw.
            url: isOData ? odataEntry.ODATA_URL : isBitbucket ? bitbucketEntry.BITBUCKET_URL : server.url,
            auth_mode: server.auth_mode,
            odata: {
                services: stored.services, allowWrite: stored.allow_write, loaded: isOData, loadError,
                options: [], rows: [], noData: "", duplicate: "", missing: "", disabled: "", warning: "", writeText: ""
            },
            oauth: isOData || !server.oauth ? blankOAuth
                : isBitbucket ? { ...blankOAuth, destination: typeof pins.destination === "string" ? pins.destination : "" }
                    : server.oauth,
            // --- bitbucket --- Workspace and branch as stored (never
            // trimmed); the switches as real booleans. No stored
            // `require_green_builds` means the builds must be successful:
            // the box is ticked.
            bitbucket: {
                workspace: typeof pins.workspace === "string" ? pins.workspace : "",
                repositoriesText: bitbucketEntry.formatRepositories(pins.repositories),
                branch: typeof pins.branch === "string" ? pins.branch : "",
                allowComment: pins.allow_comment === true,
                allowApprove: pins.allow_comment === true && pins.allow_approve === true,
                requireGreenBuilds: pins.require_green_builds !== false
            },
            // "mcp" for a remote server, otherwise the built-in's url.
            kind: findBuiltin(server.url)?.url ?? "mcp",
            kinds: [{ key: "mcp", text: this.text("toolsetRemoteMcp") }].concat(
                BUILTINS.map((b) => ({ key: b.url, text: this.text(b.titleKey) }))
            ),
            kindDescription: "",
            authModes: [],
            // Secrets are redacted by the server, so a blank field means
            // "keep the stored secret" — say so instead of looking empty.
            secretPlaceholder: hasStoredSecret ? this.text("secretStored") : "",
            // The mail theme is edited as JSON text; cleanOAuth gets the parsed object.
            themeJson: oauthConfig.formatMailTheme((server.oauth as { theme?: unknown } | undefined)?.theme),
            // --- sharepoint --- The views are edited as JSON text; cleanOAuth gets the parsed object.
            viewsJson: oauthConfig.formatViews((server.oauth as { views?: unknown } | undefined)?.views),
            scopeHint: "",
            errors: {}
        });

        this.syncServerKind();
        this.showODataOptions();
        this.showODataEntry();

        if (!this.serverDialog) {
            this.serverDialog = await Fragment.load({
                id: this.getView()!.getId(),
                name: "com.agent.admin.fragment.McpServerDialog",
                controller: this
            }) as Dialog;
            this.getView()!.addDependent(this.serverDialog);
        }
        this.serverDialog.open();
    }

    /** The toolset dropdown: a built-in fills in its url and a working auth mode. */
    public onServerKindChange(): void {
        const serverModel = this.getModel("server") as JSONModel;
        const kind = serverModel.getProperty("/kind") as string;
        const builtin = findBuiltin(kind);
        if (builtin) {
            serverModel.setProperty("/url", builtin.url);
            serverModel.setProperty("/auth_mode", builtin.defaultAuthMode);
            if (odataEntry.isODataUrl(builtin.url)) {
                void this.enterODataEntry();
            }
        } else if (findBuiltin(serverModel.getProperty("/url") as string)) {
            // Back to a remote server: the built-in's pseudo-url is no
            // starting point for a real one.
            serverModel.setProperty("/url", "");
        }
        serverModel.setProperty("/errors", {});
        this.syncServerKind();
    }

    /** A url typed by hand may itself name a built-in; keep the dropdown in step. */
    public onServerUrlChange(): void {
        const serverModel = this.getModel("server") as JSONModel;
        const url = serverModel.getProperty("/url") as string;
        serverModel.setProperty("/kind", findBuiltin(url)?.url ?? "mcp");
        this.syncServerKind();
        if (odataEntry.isODataUrl(url)) {
            // The spelling the fragment's bindings test for.
            serverModel.setProperty("/url", odataEntry.ODATA_URL);
            void this.enterODataEntry();
        } else if (bitbucketEntry.isBitbucketUrl(url)) {
            serverModel.setProperty("/url", bitbucketEntry.BITBUCKET_URL);
        }
    }

    /**
     * --- bitbucket --- Approving needs commenting: switching Commenting off
     * switches Approving off (its box is disabled then, and a tick nobody can
     * reach must not stay). The build requirement only matters while
     * approving, so it goes back to its default, required, when Approving
     * goes off: approving switched on again never starts without it.
     */
    public onBitbucketSwitch(): void {
        const serverModel = this.getModel("server") as JSONModel;
        if (serverModel.getProperty("/bitbucket/allowComment") !== true) {
            serverModel.setProperty("/bitbucket/allowApprove", false);
        }
        if (serverModel.getProperty("/bitbucket/allowApprove") !== true) {
            serverModel.setProperty("/bitbucket/requireGreenBuilds", true);
        }
    }

    // --- odata: the agent's OData services entry ---------------------------

    /** Reads the catalogue for the open dialog. Returns the text to show
     *  when that failed; never a dialog of its own, because the admin may be
     *  here for another toolset. */
    private async loadODataCatalogue(): Promise<string> {
        try {
            this.odataCatalogue = await this.getAdminService().listODataServices();
            return "";
        } catch {
            this.odataCatalogue = null;
            return this.text("odataEntryLoadFailed");
        }
    }

    /** "OData services" was picked in a dialog opened for something else:
     *  read the catalogue once, then show. The selection is not touched. */
    private async enterODataEntry(): Promise<void> {
        const serverModel = this.getModel("server") as JSONModel;
        if (serverModel.getProperty("/odata/loaded") !== true) {
            serverModel.setProperty("/odata/loaded", true);
            const opening = this.odataOpening;
            const loadError = await this.loadODataCatalogue();
            if (opening !== this.odataOpening) {
                return;
            }
            serverModel.setProperty("/odata/loadError", loadError);
            this.showODataOptions();
        }
        this.showODataEntry();
    }

    private odataSelection(): string[] {
        const selected = (this.getModel("server") as JSONModel).getProperty("/odata/services") as unknown;
        return odataEntry.clean({ services: selected }).services;
    }

    private odataIdentity(userContext: boolean): string {
        return this.text(userContext ? "odataRunsAsUser" : "odataRunsAsTechnical");
    }

    /** The choices of the box: the catalogue, plus the selected names it
     *  lacks, so that no selected key is ever without an item. The selection
     *  is written again afterwards: the box matches keys to items when the
     *  keys are set. The model is the selection; the box only shows it. */
    private showODataOptions(): void {
        const serverModel = this.getModel("server") as JSONModel;
        const selected = this.odataSelection();
        serverModel.setProperty("/odata/options", odataEntry.options(selected, this.odataCatalogue).map((option) => ({
            key: option.key,
            text: option.title,
            identity: option.state === "missing" ? this.text("odataEntryRowMissing")
                : option.state === "unknown" ? ""
                    : option.state === "disabled"
                        ? this.text("odataEntryOptionDisabled", [this.odataIdentity(option.userContext)])
                        : this.odataIdentity(option.userContext)
        })));
        serverModel.setProperty("/odata/services", selected.slice());
        // The box is told as well: an equal list is no change for the
        // binding, and the box may still hold the keys of the dialog shown
        // before (its items were gone and are back now).
        (this.byId("odataEntryServices") as unknown as { setSelectedKeys(keys: string[]): void } | undefined)
            ?.setSelectedKeys(selected.slice());
    }

    /** A tick, a selection or a removed token. The box's own keys are taken
     *  as the selection, whatever the binding has done by now. */
    public onODataEntryChange(event: Event): void {
        const source = event.getSource() as Control & { getSelectedKeys?: () => string[] };
        if (typeof source.getSelectedKeys === "function") {
            (this.getModel("server") as JSONModel).setProperty("/odata/services", source.getSelectedKeys().slice());
        }
        this.showODataEntry();
    }

    private static quoted(names: string[]): string {
        return names.map((name) => `"${name}"`).join(", ");
    }

    /**
     * Everything the entry says about its selection: the services with who
     * they run as, what is missing or disabled, the scheduled-run warning,
     * a second entry, and what "Allow writes" opens. Reads the definitions
     * it still needs and shows again when they are there.
     */
    private showODataEntry(): void {
        const serverModel = this.getModel("server") as JSONModel;
        if (!odataEntry.isODataUrl(serverModel.getProperty("/url") as string)) {
            return;
        }
        const agentModel = this.getModel("agent") as JSONModel;
        const rows = odataEntry.rows(this.odataSelection(), this.odataCatalogue);

        serverModel.setProperty("/odata/rows", rows.map((row) => {
            const runsAs = this.text("odataEntryRunsAs", [this.odataIdentity(row.userContext)]);
            return {
                title: row.title,
                purpose: row.purpose,
                info: row.state === "missing" ? this.text("odataEntryRowMissing")
                    : row.state === "unknown" ? ""
                        : row.state === "disabled" ? this.text("odataEntryRowDisabled", [runsAs]) : runsAs,
                infoState: row.state === "missing" ? ValueState.Error
                    : row.state === "disabled" ? ValueState.Warning
                        : row.userContext ? ValueState.Information : ValueState.None
            };
        }));
        serverModel.setProperty("/odata/noData", this.text(
            this.odataCatalogue && this.odataCatalogue.length === 0 ? "odataEntryNoCatalogue" : "odataEntryNoServices"));

        const other = odataEntry.entryIndex(
            agentModel.getProperty("/data/mcp_servers") as McpServer[], this.editingServerIndex);
        serverModel.setProperty("/odata/duplicate", other === -1 ? "" : this.text("odataEntryDuplicate", [String(other + 1)]));

        const missing = rows.filter((row) => row.state === "missing").map((row) => row.name);
        serverModel.setProperty("/odata/missing", missing.length === 0 ? ""
            : missing.length === 1 ? this.text("odataEntryMissingOne", [missing[0]])
                : this.text("odataEntryMissingMany", [AgentDetail.quoted(missing)]));

        const disabled = rows.filter((row) => row.state === "disabled").map((row) => row.title);
        serverModel.setProperty("/odata/disabled", disabled.length === 0 ? ""
            : disabled.length === 1 ? this.text("odataEntryDisabledOne", [disabled[0]])
                : this.text("odataEntryDisabledMany", [AgentDetail.quoted(disabled)]));

        // A run started through the run endpoint has no signed-in user.
        const asUser = agentModel.getProperty("/data/expose_api") === true
            ? rows.filter((row) => row.userContext && row.state !== "missing").map((row) => row.title) : [];
        serverModel.setProperty("/odata/warning", asUser.length === 0 ? ""
            : asUser.length === 1 ? this.text("odataEntryScheduledWarning", [asUser[0]])
                : this.text("odataEntryScheduledWarningMany", [AgentDetail.quoted(asUser)]));

        serverModel.setProperty("/odata/writeText",
            this.odataWriteText(rows, serverModel.getProperty("/odata/allowWrite") === true));
        void this.readODataDefinitions(rows);
    }

    /** One line per service: what "Allow writes" opens there. */
    private odataOpensLines(opens: ODataEntryOpens[], reading = false): string[] {
        return opens.map((entry) => (
            entry.items === null
                ? this.text(reading && !(`=${entry.name}` in this.odataDefinitions)
                    ? "odataEntryOpensReading" : "odataEntryOpensUnread", [entry.title])
                : entry.items.length === 0 ? this.text("odataEntryOpensNothing", [entry.title])
                    : this.text("odataEntryOpensLine", [entry.title, entry.items.join(", ")])
        ));
    }

    private odataWriteText(rows: ODataEntryRow[], allowWrite: boolean): string {
        const opens = odataEntry.opens(rows, this.odataDefinitions);
        if (opens.length === 0) {
            return this.text("odataEntryAllowWriteNoServices");
        }
        // "Nothing" is only said when every service was read and has none.
        if (opens.every((entry) => entry.items !== null && entry.items.length === 0)) {
            return this.text(allowWrite ? "odataEntryAllowWriteNothing" : "odataEntryAllowWriteNothingOff");
        }
        const lines = [this.text(allowWrite ? "odataEntryOpensOn" : "odataEntryOpensOff")]
            .concat(this.odataOpensLines(opens, true));
        if (allowWrite) {
            lines.push(this.text("odataEntryOpensLater"));
        }
        return lines.join("\n");
    }

    /** Reads the definitions of the selected services that are not read
     *  yet, then shows the entry again. A failed read is remembered as
     *  such and said; it is never shown as "no write operation". */
    private async readODataDefinitions(rows: ODataEntryRow[]): Promise<void> {
        const wanted = rows.filter((row) => (row.state === "ok" || row.state === "disabled")
            && !(`=${row.name}` in this.odataDefinitions) && !this.odataReading[`=${row.name}`]);
        if (wanted.length === 0) {
            return;
        }
        const opening = this.odataOpening;
        const reading = this.odataReading;
        const definitions = this.odataDefinitions;
        wanted.forEach((row) => { reading[`=${row.name}`] = true; });
        await Promise.all(wanted.map(async (row) => {
            try {
                definitions[`=${row.name}`] = (await this.getAdminService().getODataService(row.name)).definition;
            } catch {
                definitions[`=${row.name}`] = null;
            }
            delete reading[`=${row.name}`];
        }));
        if (opening === this.odataOpening && definitions === this.odataDefinitions) {
            this.showODataEntry();
        }
    }

    /** The reads of `readODataDefinitions` that are under way, by "=<name>". */
    private odataReading: Record<string, boolean> = {};

    /**
     * OK on the OData services entry. False when the dialog must stay open:
     * a second entry, no service, or a service the catalogue no longer has
     * (asked about; only the admin's answer removes it).
     */
    private confirmODataEntry(): boolean {
        const serverModel = this.getModel("server") as JSONModel;
        this.showODataEntry();
        const duplicate = serverModel.getProperty("/odata/duplicate") as string;
        if (duplicate) {
            MessageBox.error(duplicate);
            return false;
        }
        const rows = odataEntry.rows(this.odataSelection(), this.odataCatalogue);
        const missing = rows.filter((row) => row.state === "missing").map((row) => row.name);
        if (missing.length > 0) {
            const remove = this.text("odataEntryMissingRemove");
            MessageBox.warning(this.text("odataEntryMissingConfirm", [AgentDetail.quoted(missing)]), {
                title: this.text("odataEntryMissingTitle"),
                actions: [remove, MessageBox.Action.CANCEL],
                emphasizedAction: remove,
                initialFocus: MessageBox.Action.CANCEL,
                onClose: (action: string | null) => {
                    if (action !== remove) {
                        return;
                    }
                    serverModel.setProperty("/odata/services",
                        this.odataSelection().filter((name) => missing.indexOf(name) === -1));
                    this.showODataOptions();
                    this.showODataEntry();
                    this.onConfirmServer();
                }
            });
            return false;
        }
        if (rows.length === 0) {
            MessageBox.error(this.text(
                this.odataCatalogue && this.odataCatalogue.length === 0 ? "odataEntryNoCatalogue" : "odataEntryNoServices"));
            return false;
        }
        return true;
    }

    /**
     * The question of Save when it newly gives writes, compared with the
     * agent as the server has it: "Allow writes" goes on, or a service with
     * enabled writes joins an entry that already had it. Undefined when
     * there is nothing to ask. The services are read now, not taken from
     * the dialog: the catalogue may have changed since. A service that
     * cannot be read is asked about and said to be unread.
     */
    private async odataSaveQuestion(data: AgentInput, given: ODataEntryGiven): Promise<string | undefined> {
        if (!given.switchedOn && given.services.length === 0) {
            return undefined;
        }
        let catalogue: ODataServiceSummary[] | null = null;
        try {
            catalogue = await this.getAdminService().listODataServices();
        } catch {
            catalogue = null;
        }
        const definitions: Record<string, ODataDefinition | null> = {};
        const rows = odataEntry.rows(given.services, catalogue).filter((row) => row.state !== "missing");
        await Promise.all(rows.map(async (row) => {
            try {
                definitions[`=${row.name}`] = (await this.getAdminService().getODataService(row.name)).definition;
            } catch {
                definitions[`=${row.name}`] = null;
            }
        }));
        const opens = odataEntry.opens(rows, definitions)
            .filter((entry) => given.switchedOn || entry.items === null || entry.items.length > 0);
        if (!given.switchedOn && opens.length === 0) {
            return undefined;
        }
        const parts = [this.text(given.switchedOn ? "odataEntrySaveSwitchedOn" : "odataEntrySaveAdded", [data.name])];
        const writing = opens.filter((entry) => entry.items === null || entry.items.length > 0);
        if (writing.length > 0) {
            parts.push([this.text("odataEntrySaveOpens")].concat(this.odataOpensLines(writing)).join("\n"));
            parts.push(this.text("odataEntrySaveLater"));
        } else {
            parts.push(this.text("odataEntrySaveNothing"));
        }
        parts.push(this.text("odataWriteAudited"));
        return parts.join("\n\n");
    }

    /**
     * Derives what the dialog shows from the url: the built-in's description
     * and the auth modes the server accepts for it. An auth mode that is not
     * among them is replaced by the first that is, rather than left for the
     * server to refuse on save.
     */
    private syncServerKind(): void {
        const serverModel = this.getModel("server") as JSONModel;
        const url = serverModel.getProperty("/url") as string;
        const builtin = findBuiltin(url);
        serverModel.setProperty("/kindDescription", builtin ? this.text(builtin.descriptionKey) : "");
        const modes = authModesFor(url);
        serverModel.setProperty("/authModes", modes.map((m) => ({
            key: m, text: this.text(AUTH_MODE_TEXT_KEYS[m])
        })));
        const current = serverModel.getProperty("/auth_mode") as AuthMode;
        if (modes.indexOf(current) === -1) {
            serverModel.setProperty("/auth_mode", builtin?.defaultAuthMode ?? modes[0]);
        }
        // Picking a built-in may have changed the mode above or in
        // onServerKindChange; the placeholder has to follow either way.
        this.applyUserContextDefault();
        this.syncScopeHint();
    }

    public onAuthModeChange(): void {
        (this.getModel("server") as JSONModel).setProperty("/errors", {});
        this.applyUserContextDefault();
        this.syncScopeHint();
    }

    /**
     * The scope placeholder follows the auth mode, so it is recomputed
     * whenever the mode changes rather than once when the dialog opens.
     * App-only tokens carry no per-scope request: providers want the
     * ".default" form, and asking for individual scopes is rejected.
     */
    private syncScopeHint(): void {
        const serverModel = this.getModel("server") as JSONModel;
        const mode = serverModel.getProperty("/auth_mode") as AuthMode;
        serverModel.setProperty(
            "/scopeHint", mode === "app_only" ? "https://graph.microsoft.com/.default" : ""
        );
    }

    public onCancelServer(): void {
        this.serverDialog?.close();
    }

    public onConfirmServer(): void {
        const serverModel = this.getModel("server") as JSONModel;
        const url = (serverModel.getProperty("/url") as string).trim();
        const authMode = serverModel.getProperty("/auth_mode") as McpServer["auth_mode"];
        const oauthRaw = Object.assign(
            {}, serverModel.getProperty("/oauth") as Record<string, unknown>
        );
        delete oauthRaw.theme;
        // --- sharepoint --- As the theme: only what the text area holds now counts.
        delete oauthRaw.views;
        // --- bitbucket --- These three keys are refused by the server on
        // every other toolset, so they never ride along from a stored block.
        delete oauthRaw.repositories;
        delete oauthRaw.allow_approve;
        delete oauthRaw.require_green_builds;
        if (bitbucketEntry.isBitbucketUrl(url)) {
            // Only the entry's own values reach cleanOAuth: the destination
            // name and what the Bitbucket fields hold now. Workspace and
            // branch go on exactly as typed; the server refuses edge
            // whitespace instead of repairing it, and so does the validator.
            const destination = oauthRaw.destination;
            const form = serverModel.getProperty("/bitbucket") as Record<string, unknown>;
            Object.keys(oauthRaw).forEach((key) => { delete oauthRaw[key]; });
            oauthRaw.destination = destination;
            oauthRaw.workspace = form.workspace;
            oauthRaw.repositories = bitbucketEntry.parseRepositories(form.repositoriesText as string);
            oauthRaw.branch = form.branch;
            oauthRaw.allow_comment = form.allowComment === true;
            // The Approving box is disabled without Commenting; a stale tick is not sent.
            oauthRaw.allow_approve = form.allowComment === true && form.allowApprove === true;
            oauthRaw.require_green_builds = form.requireGreenBuilds !== false;
        }
        if (odataEntry.isODataUrl(url)) {
            // --- odata --- Only the entry's own two values reach cleanOAuth.
            if (!this.confirmODataEntry()) {
                return;
            }
            Object.keys(oauthRaw).forEach((key) => { delete oauthRaw[key]; });
            oauthRaw.services = this.odataSelection();
            oauthRaw.allow_write = serverModel.getProperty("/odata/allowWrite") === true;
        }
        if (oauthConfig.supportsMailTheme(url)) {
            const parsedTheme = oauthConfig.parseMailTheme(serverModel.getProperty("/themeJson") as string);
            if (parsedTheme.error) {
                serverModel.setProperty("/errors", {
                    theme: parsedTheme.error, themeState: ValueState.Error
                });
                return;
            }
            if (parsedTheme.theme) {
                oauthRaw.theme = parsedTheme.theme;
            }
        }
        if (oauthConfig.supportsViews(url)) {
            // --- sharepoint --- Only the JSON shape is refused here; the
            // rules of a view are validators.validateSharePoint and the server.
            const parsedViews = oauthConfig.parseViews(serverModel.getProperty("/viewsJson") as string);
            if (parsedViews.error) {
                serverModel.setProperty("/errors", {
                    views: parsedViews.error, viewsState: ValueState.Error
                });
                return;
            }
            if (parsedViews.views) {
                oauthRaw.views = parsedViews.views;
            }
        }

        const urlError = validators.validateServerUrl(url, authMode);
        if (urlError) {
            serverModel.setProperty("/errors", { url: urlError, urlState: ValueState.Error });
            return;
        }

        // `none` joins the list only for a built-in: those carry a public
        // config block (a CVSS floor, a window) with no credential in it.
        // For any other url on `none` there is nothing to configure, and
        // sending a block would be rejected server-side anyway.
        const publicBuiltin = authMode === "none" && validators.carriesPublicConfig(url);
        const carriesOAuth = authMode === "oauth2" || authMode === "app_only"
            || authMode === "destination" || publicBuiltin;
        const oauth = carriesOAuth
            ? oauthConfig.cleanOAuth(oauthRaw, authMode, url)
            : undefined;
        const oauthError = validators.validateOAuth(oauth, authMode, url);
        if (oauthError) {
            MessageBox.error(oauthError);
            return;
        }

        const entry: McpServer = { url, auth_mode: authMode };
        if (oauth) {
            // cleanOAuth drops has_client_secret (to_config() ignores it on
            // input), but this entry is also what re-opens the dialog if the
            // same server is edited again later in this session, and
            // openServerDialog reads has_client_secret to decide whether to
            // show the "stored" placeholder. Carry it forward or that hint
            // silently disappears the second time round.
            // A destination stores no credential of its own, so there is no
            // secret to remember; leaving the flag out keeps the posted block
            // exactly what the server stores.
            if (oauth.dcr !== true && !publicBuiltin && authMode !== "destination" && !odataEntry.isODataUrl(url)) {
                oauth.has_client_secret = !!oauth.client_secret
                    || !!(oauthRaw as { has_client_secret?: boolean }).has_client_secret;
            }
            entry.oauth = oauth;
        }

        const agentModel = this.getModel("agent") as JSONModel;
        const servers = (agentModel.getProperty("/data/mcp_servers") as McpServer[]).slice();
        if (this.editingServerIndex >= 0) {
            servers[this.editingServerIndex] = entry;
        } else {
            servers.push(entry);
        }
        agentModel.setProperty("/data/mcp_servers", servers);
        agentModel.setProperty("/errors/servers", "");
        this.serverDialog?.close();
    }

    public onRemoveServer(event: Event): void {
        const context = (event.getSource() as Control).getBindingContext("agent");
        const path = context?.getPath() ?? "";
        const index = Number(path.substring(path.lastIndexOf("/") + 1));

        const model = this.getModel("agent") as JSONModel;
        const servers = (model.getProperty("/data/mcp_servers") as McpServer[]).slice();
        servers.splice(index, 1);
        model.setProperty("/data/mcp_servers", servers);
    }

    // --- Save -------------------------------------------------------------
    public async onSave(): Promise<void> {
        if (this.saving) {
            return;
        }
        const model = this.getModel("agent") as JSONModel;
        model.setProperty("/errors", {});
        const data = model.getProperty("/data") as AgentInput;

        const serverErrors = validators.validateServers(data.mcp_servers);
        const keys = Object.keys(serverErrors);
        if (keys.length > 0) {
            const first = serverErrors[Number(keys[0])];
            model.setProperty("/errors/servers", first);
            MessageBox.error(first);
            return;
        }

        // --- bitbucket --- An agent that approves pull requests is not in
        // chat and nobody's peer (the server refuses it too). The text says
        // which of the two to switch off.
        const approver = validators.approverProblem(
            data, model.getProperty("/availableAgents") as Agent[] | undefined, this.storedName);
        if (approver) {
            const names = AgentDetail.quoted(approver.names);
            MessageBox.error(approver.rule === "chat" ? this.text("bitbucketApproveChatRefused")
                : approver.rule === "peerOf" ? this.text("bitbucketApprovePeerOfRefused", [names])
                    : this.text("bitbucketApprovePeerRefused", [names]));
            return;
        }

        // --- deep agents --- range errors land on their StepInput.
        const deepErrors = validateDeep(data.deep);
        const deepKeys = Object.keys(deepErrors);
        if (deepKeys.length > 0) {
            deepKeys.forEach((field) => {
                model.setProperty(`/errors/deep_${field}`, deepErrors[field]);
                model.setProperty(`/errors/deep_${field}State`, ValueState.Error);
            });
            MessageBox.error(deepErrors[deepKeys[0]]);
            return;
        }

        // --- odata --- One question when this save newly gives writes.
        // `toSave` is the form as it is now: the question is built from it
        // and the same object is sent, so an edit made while the question
        // reads the catalogue cannot be saved without having been asked about.
        this.saving = true;
        const agentId = this.agentId;
        const toSave = JSON.parse(JSON.stringify(data)) as AgentInput;
        // Decided before anything is read: whether this save gives writes
        // does not depend on a read that can fail.
        const given = odataEntry.newlyGiven(
            odataEntry.entryOf(this.storedServers), odataEntry.entryOf(toSave.mcp_servers));
        let question: string | undefined;
        try {
            question = await this.odataSaveQuestion(toSave, given);
        } catch {
            // What it opens could not be worked out: ask anyway, never save
            // newly given writes without a question.
            question = given.switchedOn || given.services.length > 0
                ? this.text("odataEntrySaveUnknown", [toSave.name]) : undefined;
        }
        if (this.agentId !== agentId || !this.saving) {
            return; // another agent was opened meanwhile
        }
        // --- bitbucket --- A second reason to ask: this save lets the agent
        // approve pull requests unattended, or approve more than it did.
        // Decided from the stored servers and the object that is sent (the
        // same snapshot the PUT carries), never from a read.
        const approvals = bitbucketEntry.approvalsToAsk(this.storedServers, toSave.mcp_servers);
        const asked = (question ? [question] : []).concat(
            approvals.map((ask) => bitbucketEntry.question(
                toSave.name, ask, (key, args) => this.text(key, args))));
        if (approvals.some((ask) => ask.reason === "approve")) {
            asked.push(this.text("bitbucketApprovalStays"));
        }
        if (asked.length === 0) {
            await this.saveAgent(toSave);
            return;
        }
        const titleKey = question && approvals.length > 0 ? "bitbucketUnattendedSaveTitle"
            : question ? "odataEntrySaveTitle"
            : approvals.some((ask) => ask.reason === "approve") ? "bitbucketApproveSaveTitle"
                : "bitbucketWidenSaveTitle";
        const save = this.text("save");
        MessageBox.warning(asked.join("\n\n"), {
            title: this.text(titleKey),
            actions: [save, MessageBox.Action.CANCEL],
            emphasizedAction: save,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === save && this.agentId === agentId && this.saving) {
                    void this.saveAgent(toSave);
                } else {
                    this.saving = false;
                }
            }
        });
    }

    private async saveAgent(data: AgentInput): Promise<void> {
        try {
            // --- odata --- The entry is sent as exactly { services,
            // allow_write: <boolean> }, also when its dialog was never opened
            // and the form still holds the form the server answered with.
            const sent = { ...data, mcp_servers: odataEntry.explicit(data.mcp_servers) } as AgentInput & ReloadOutcome;
            // What a save answers about the reload is no field of an agent;
            // it never goes back in a body, whatever the form was filled from.
            delete sent.reloaded;
            delete sent.reload_failed;
            const saved = await this.getAdminService().upsertAgent(sent, this.agentId);
            // Stored, but the running agent still has its services and its
            // "Allow writes" as they were: said in a box that stays.
            if (!this.warnIfNotLive(saved, "reloadFailedAgentSaved")) {
                MessageToast.show(this.text("agentSaved"));
            }
            this.agentId = saved.id;
            this.getRouter().navTo("agents");
        } catch (error) {
            if (error instanceof AdminError && Object.keys(error.fieldErrors).length > 0) {
                this.applyFieldErrors(error.fieldErrors);
                return;
            }
            ErrorHandler.handle(error, "Could not save the agent.");
        } finally {
            this.saving = false;
        }
    }

    /**
     * Attaches 422 messages to controls. Keys for nested server problems look
     * like `mcp_servers.0.url`; those have no single control, so they surface
     * on the toolsets panel instead.
     */
    private applyFieldErrors(fieldErrors: Record<string, string>): void {
        const model = this.getModel("agent") as JSONModel;
        const errors: Record<string, string> = {};

        ["name", "description", "instructions"].forEach((field) => {
            const message = fieldErrors[field];
            if (message) {
                errors[field] = message;
                errors[`${field}State`] = ValueState.Error;
            }
        });

        const serverMessages = Object.keys(fieldErrors)
            .filter((key) => key.indexOf("mcp_servers") === 0)
            .map((key) => fieldErrors[key]);
        if (serverMessages.length > 0) {
            errors.servers = serverMessages.join("; ");
        }

        model.setProperty("/errors", errors);
        if (Object.keys(errors).length === 0) {
            MessageBox.error(Object.values(fieldErrors).join("; "));
        }
    }

    public onBack(): void {
        this.getRouter().navTo("agents");
    }

    // --- Job settings and credentials -------------------------------------

    /** Fills the run-as field from the caller's own opaque XSUAA principal. */
    public async onUseMyPrincipal(): Promise<void> {
        const who = await this.run(
            this.getAdminService().whoami(),
            "Could not read your principal."
        );
        if (who) {
            (this.getModel("agent") as JSONModel).setProperty("/data/run_as_principal", who.principal);
            MessageToast.show(this.text("principalFilled", [who.label]));
            void this.loadCredentials();
        }
    }

    /**
     * Per-server token status for the configured run-as identity.
     *
     * Checking here is the point: a mismatch otherwise surfaces hours later as
     * a failed scheduled run.
     */
    public async loadCredentials(): Promise<void> {
        const model = this.getModel("agent") as JSONModel;
        if (this.agentId === undefined) {
            model.setProperty("/credentials", []);
            return;
        }
        const principal = model.getProperty("/data/run_as_principal") as string;
        const statuses = await this.run(
            this.getAdminService().agentCredentials(this.agentId, principal),
            "Could not read the credential status."
        );
        model.setProperty("/credentials", statuses ?? []);
    }

    public onRefreshCredentials(): void {
        void this.loadCredentials();
    }

    /**
     * Opens the OAuth sign-in on the backend host, absolutely.
     *
     * `/oauth/login` and the callback live outside this app's path, so a
     * relative link breaks the moment the app is served from a Work Zone site.
     */
    public onSignIn(event: Event): void {
        const status = (event.getSource() as Control)
            .getBindingContext("agent")?.getObject() as CredentialStatus;
        if (!this.publicBaseUrl) {
            MessageBox.error(this.text("noPublicBaseUrl"));
            return;
        }
        window.open(this.publicBaseUrl + status.login_url, "_blank", "noopener");
    }

    // --- where used ---

    /**
     * Fills `/whereUsed` for the agent being edited: the two lists plus the
     * three flags the panel's visibility bindings read (`hasWorkflows`,
     * `hasPeers`, `empty`), computed here so the view never takes `.length`
     * of a list that is not loaded yet. Cleared first, so a panel left over
     * from the agent opened before never shows against this one.
     */
    public async loadWhereUsed(): Promise<void> {
        const model = this.getModel("agent") as JSONModel;
        model.setProperty("/whereUsed", null);
        if (this.agentId === undefined) {
            return;
        }
        const id = this.agentId;
        const result = await this.run(
            this.getAdminService().getAgentWhereUsed(id),
            this.text("whereUsedLoadFailed")
        );
        // Another agent may have been opened while this was in flight.
        if (!result || this.agentId !== id) {
            return;
        }
        model.setProperty("/whereUsed", {
            workflows: result.workflows,
            peers: result.peers,
            hasWorkflows: result.workflows.length > 0,
            hasPeers: result.peers.length > 0,
            empty: result.workflows.length === 0 && result.peers.length === 0
        });
    }

    /** "Steps: main #1, support #2" -- one entry per step of that workflow
     * that runs this agent, positioned within its group as everywhere else. */
    public whereUsedSteps(steps: WhereUsedStep[] | undefined): string {
        const parts = (steps ?? []).map(
            (s) => `${s.branch_key ?? this.text("whereUsedMainLine")} #${s.position}`
        );
        return parts.length ? this.text("whereUsedSteps", [parts.join(", ")]) : "";
    }

    public onOpenWhereUsedWorkflow(event: Event): void {
        const workflow = (event.getSource() as Control)
            .getBindingContext("agent")?.getObject() as WhereUsedWorkflow | undefined;
        if (workflow) {
            this.getRouter().navTo("workflowDetail", { workflowId: String(workflow.id) });
        }
    }

    public onOpenWhereUsedPeer(event: Event): void {
        const peer = (event.getSource() as Control)
            .getBindingContext("agent")?.getObject() as WhereUsedPeer | undefined;
        if (peer) {
            this.getRouter().navTo("agentDetail", { agentId: String(peer.id) });
        }
    }

    // --- run now / refresh / last runs ---

    /** The list page shows its Run now button only for an agent exposed as a
     * job API; the same rule, on the saved state, enables this page's. */
    private static isRunnable(agent: Agent): boolean {
        return !!agent.expose_api;
    }

    /**
     * The newest runs of this agent, for the panel. Never throws and never
     * goes through ErrorHandler's dialog: a failure here shows one toast and
     * an empty panel, because it must not break opening the agent.
     */
    public async loadRuns(): Promise<void> {
        const model = this.getModel("agent") as JSONModel;
        const id = this.agentId;
        if (id === undefined) {
            model.setProperty("/runs", []);
            model.setProperty("/runsCount", runsCountLabel(0));
            return;
        }
        let runs: JobRun[] = [];
        try {
            runs = await this.getAdminService().listRuns({ agentId: id, limit: LAST_RUNS_LIMIT });
        } catch {
            MessageToast.show(this.text("lastRunsLoadFailed"));
        }
        // Another agent may have been opened while this was in flight.
        if (this.agentId !== id) {
            return;
        }
        model.setProperty("/runs", runs);
        model.setProperty("/runsCount", runsCountLabel(runs.length, LAST_RUNS_LIMIT));
    }

    public onRefreshRuns(): void {
        void this.loadRuns();
    }

    public onOpenRun(event: Event): void {
        const run = (event.getSource() as ColumnListItem)
            .getBindingContext("agent")?.getObject() as JobRun | undefined;
        if (run) {
            this.getRouter().navTo("runDetail", { runId: run.id });
        }
    }

    /**
     * Starts a run, the way the list page does (same toast, same error
     * handling), then reloads the panel shortly after so the run shows up
     * as running, and once more so a short run shows its outcome. Unlike
     * the list page it stays here rather than opening the run: the panel is
     * the point of this page.
     */
    public async onRunNow(): Promise<void> {
        const model = this.getModel("agent") as JSONModel;
        const id = this.agentId;
        if (id === undefined || model.getProperty("/runBusy")) {
            return;
        }
        const name = model.getProperty("/title") as string;
        model.setProperty("/runBusy", true);
        try {
            const started = await this.run(
                this.getAdminService().runNow(id),
                `Could not start a run for "${name}".`
            );
            if (!started) {
                return;
            }
            MessageToast.show(this.text("runStarted", [started.run_id]));
            this.clearRunRefreshTimers();
            RUN_REFRESH_DELAYS_MS.forEach((delay) => {
                this.runRefreshTimers.push(setTimeout(() => {
                    if (this.agentId === id) {
                        void this.loadRuns();
                    }
                }, delay));
            });
        } finally {
            model.setProperty("/runBusy", false);
        }
    }

    private clearRunRefreshTimers(): void {
        this.runRefreshTimers.forEach((t) => clearTimeout(t));
        this.runRefreshTimers = [];
    }

    /** Whether the form differs from what was loaded. */
    private isDirty(): boolean {
        return isDirty(this.snapshot, (this.getModel("agent") as JSONModel).getProperty("/data"));
    }

    /**
     * Reloads the agent from the server: the form, the credential status,
     * where-used and the last runs. Unsaved edits are lost, so it asks first
     * when there are any.
     */
    public onRefresh(): void {
        if (this.agentId === undefined) {
            return;
        }
        if (!this.isDirty()) {
            void this.reload();
            return;
        }
        MessageBox.confirm(this.text("refreshDiscardConfirm"), {
            title: this.text("refresh"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.reload();
                }
            }
        });
    }

    private async reload(): Promise<void> {
        if (this.agentId === undefined) {
            return;
        }
        await this.load(String(this.agentId));
        if (this.snapshot !== undefined) {
            MessageToast.show(this.text("reloadedFromServer"));
        }
    }

    // text(key) is inherited from BaseController — do not redeclare it.
}
