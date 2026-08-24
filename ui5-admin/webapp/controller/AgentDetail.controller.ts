import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import { ValueState } from "sap/ui/core/library";
import Fragment from "sap/ui/core/Fragment";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import validators from "../model/validators";
import type Dialog from "sap/m/Dialog";
import type Event from "sap/ui/base/Event";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type Control from "sap/ui/core/Control";
import type { AgentInput, CredentialStatus, McpServer } from "../service/types";

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
    run_timeout_seconds: 1800
};

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class AgentDetail extends BaseController {

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
            errors: {}
        }), "agent");
        this.setModel(new JSONModel({}), "server");

        this.getRouter().getRoute("agentDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const id = (event.getParameter("arguments") as { agentId: string }).agentId;
            void this.load(id);
        });
    }

    private async load(id: string): Promise<void> {
        const model = this.getModel("agent") as JSONModel;
        model.setProperty("/errors", {});

        const skills = await this.run(
            this.getAdminService().listSkills(),
            "Could not load the skill list."
        );
        model.setProperty("/availableSkills", skills ?? []);

        if (id === "new") {
            this.agentId = undefined;
            model.setProperty("/data", JSON.parse(JSON.stringify(EMPTY_AGENT)) as AgentInput);
            model.setProperty("/title", this.text("newAgent"));
            // Otherwise a credential table left over from whichever agent was
            // open before navigating here would still be showing.
            model.setProperty("/credentials", []);
            return;
        }

        this.agentId = Number(id);
        const agent = await this.run(
            this.getAdminService().getAgent(this.agentId),
            "Could not load the agent."
        );
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
            run_timeout_seconds: agent.run_timeout_seconds ?? 1800
        } as AgentInput);
        model.setProperty("/title", agent.name);

        const config = await this.run(
            this.getAdminService().getConfig(),
            "Could not read the public base URL."
        );
        this.publicBaseUrl = config?.public_base_url ?? "";
        void this.loadCredentials();
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
            oauth: server.oauth ?? { dcr: false, client_id: "", client_secret: "", uaa_url: "", authorize_url: "", token_url: "", scope: "" },
            builtins: validators.BUILTIN_URLS.slice(),
            // Secrets are redacted by the server, so a blank field means
            // "keep the stored secret" — say so instead of looking empty.
            secretPlaceholder: hasStoredSecret ? this.text("secretStored") : "",
            errors: {}
        });

        if (!this.serverDialog) {
            this.serverDialog = await Fragment.load({
                id: this.getView()!.getId(),
                name: "com.infrabel.agentadmin.fragment.McpServerDialog",
                controller: this
            }) as Dialog;
            this.getView()!.addDependent(this.serverDialog);
        }
        this.serverDialog.open();
    }

    public onAuthModeChange(): void {
        (this.getModel("server") as JSONModel).setProperty("/errors", {});
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

        const oauth = authMode === "oauth2" ? AgentDetail.cleanOAuth(oauthRaw) : undefined;
        const oauthError = validators.validateOAuth(oauth, authMode);
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
            if (oauth.dcr !== true) {
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

    /** Drops blank fields so the server sees the same shape `to_config()` builds. */
    private static cleanOAuth(raw: Record<string, unknown>): McpServer["oauth"] {
        if (raw.dcr === true) {
            const scope = String(raw.scope ?? "").trim();
            return scope ? { dcr: true, scope } : { dcr: true };
        }
        const out: Record<string, unknown> = { client_id: String(raw.client_id ?? "").trim() };
        ["client_secret", "uaa_url", "authorize_url", "token_url", "scope"].forEach((key) => {
            const value = String(raw[key] ?? "").trim();
            if (value) {
                out[key] = value;
            }
        });
        return out as McpServer["oauth"];
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

    // text(key) is inherited from BaseController — do not redeclare it.
}
