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
import { AUTH_MODE_TEXT_KEYS, BUILTINS, authModesFor, findBuiltin } from "../model/builtins";
import formatter from "../model/formatter";
import type Dialog from "sap/m/Dialog";
import type Event from "sap/ui/base/Event";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type Control from "sap/ui/core/Control";
import type {
    AgentInput, AuthMode, CredentialStatus, DeepConfig, McpServer,
    WhereUsedPeer, WhereUsedStep, WhereUsedWorkflow
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

    public onInit(): void {
        this.setModel(new JSONModel({
            title: "",
            data: JSON.parse(JSON.stringify(EMPTY_AGENT)) as AgentInput,
            availableSkills: [],
            availableAgents: [],
            availableModels: [],
            errors: {}
        }), "agent");
        this.setModel(new JSONModel({}), "server");

        this.getRouter().getRoute("agentDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const id = (event.getParameter("arguments") as { agentId: string }).agentId;
            void this.load(id);
        });
    }

    private load(id: string): Promise<void> {
        return this.withBusy(async () => {
            const model = this.getModel("agent") as JSONModel;
            model.setProperty("/errors", {});

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
            // Also independent of each other, so also issued together. The
            // base URL is only read further down, and fetching it for an agent
            // that turns out not to exist costs nothing.
            const [agent, config] = await Promise.all([
                this.run(
                    this.getAdminService().getAgent(this.agentId),
                    "Could not load the agent."
                ),
                this.run(
                    this.getAdminService().getConfig(),
                    "Could not read the public base URL."
                )
            ]);
            if (!agent) {
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
        void this.openServerDialog({ url: "", auth_mode: "jwt" });
    }

    public onEditServer(event: Event): void {
        const context = (event.getSource() as Control).getBindingContext("agent");
        const path = context?.getPath() ?? "";
        this.editingServerIndex = Number(path.substring(path.lastIndexOf("/") + 1));
        const server = context?.getObject() as McpServer;
        void this.openServerDialog(JSON.parse(JSON.stringify(server)) as McpServer);
    }

    private async openServerDialog(server: McpServer): Promise<void> {
        const hasStoredSecret = !!(server.oauth as { has_client_secret?: boolean })?.has_client_secret;
        (this.getModel("server") as JSONModel).setData({
            url: server.url,
            auth_mode: server.auth_mode,
            oauth: server.oauth ?? { dcr: false, client_id: "", client_secret: "", uaa_url: "", authorize_url: "", token_url: "", scope: "", mailbox: "", allow_send: false, lookback: "", destination: "", project: "", status: "", api_base: "", labels: "", allow_comment: false, min_score: "", recipients: "", team: "", channels: "", user_context: false },
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
            scopeHint: "",
            errors: {}
        });

        this.syncServerKind();

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
        this.syncScopeHint();
    }

    public onAuthModeChange(): void {
        (this.getModel("server") as JSONModel).setProperty("/errors", {});
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
        const oauthRaw = serverModel.getProperty("/oauth") as Record<string, unknown>;

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
            if (oauth.dcr !== true && !publicBuiltin) {
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

        try {
            const saved = await this.getAdminService().upsertAgent(data, this.agentId);
            MessageToast.show(this.text("agentSaved"));
            this.agentId = saved.id;
            this.getRouter().navTo("agents");
        } catch (error) {
            if (error instanceof AdminError && Object.keys(error.fieldErrors).length > 0) {
                this.applyFieldErrors(error.fieldErrors);
                return;
            }
            ErrorHandler.handle(error, "Could not save the agent.");
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
            MessageToast.show(this.text("principalFilled").replace("{0}", who.label));
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

    // text(key) is inherited from BaseController — do not redeclare it.
}
