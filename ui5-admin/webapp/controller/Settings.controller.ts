import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import Fragment from "sap/ui/core/Fragment";
import type Dialog from "sap/m/Dialog";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import importBundle, { type BundleOpens } from "../model/importBundle";
import bitbucketEntry from "../model/bitbucketEntry";
import type { ImportPayload, ImportResult } from "../service/types";

/** How an import names the fields of `odata_identity_changes`. */
const IDENTITY_FIELD_TEXT: Record<string, string> = {
    destination: "importIdentityFieldDestination", user_context: "importIdentityFieldRunsAs"
};

/** Shape of `POST /admin/api/restart`'s body (agents/admin.py:api_restart).
 * The endpoint always answers 200 even when the CF bounce itself did not
 * happen — success/failure of the actual restart is carried in
 * `cf_restart.ok`, not the HTTP status, so `runOk()` cannot be used here. */
interface RestartResult {
    cf_restart: { ok: boolean; reason?: string };
}

/**
 * @namespace com.agent.admin.controller
 */
export default class Settings extends BaseController {

    private importDialog?: Dialog;

    /** An import is being asked about or sent: no second one meanwhile. */
    private importing = false;

    public onInit(): void {
        this.setModel(new JSONModel({
            model: { model_name: "", available: [] },
            orchestrator: { instructions: "" }
        }), "settings");
        this.setModel(new JSONModel({ text: "", replace: false }), "import");

        this.getRouter().getRoute("settings")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private load(): Promise<void> {
        return this.withBusy(async () => {
            const model = this.getModel("settings") as JSONModel;

            const modelInfo = await this.run(
                this.getAdminService().getModel(),
                "Could not load the LLM model configuration."
            );
            if (modelInfo) {
                model.setProperty("/model", modelInfo);
            }

            const orchestrator = await this.run(
                this.getAdminService().getOrchestrator(),
                "Could not load the orchestrator instructions."
            );
            if (orchestrator) {
                model.setProperty("/orchestrator", orchestrator);
            }
        });
    }

    public async onSaveModel(): Promise<void> {
        const name = (this.getModel("settings") as JSONModel)
            .getProperty("/model/model_name") as string;
        const saved = await this.run(
            this.getAdminService().setModel(name),
            "Could not save the model."
        );
        if (saved) {
            MessageToast.show(this.text("modelSaved"));
        }
    }

    public async onSaveOrchestrator(): Promise<void> {
        const instructions = (this.getModel("settings") as JSONModel)
            .getProperty("/orchestrator/instructions") as string;
        const saved = await this.run(
            this.getAdminService().setOrchestrator(instructions),
            "Could not save the orchestrator instructions."
        );
        if (saved) {
            MessageToast.show(this.text("instructionsSaved"));
        }
    }

    /**
     * Reload rebuilds the orchestrator AND every specialist from the database
     * -- toolsets, auth and all. The toast used to say only "Orchestrator
     * reloaded", which read as though the agents needed a separate step and
     * sent at least one operator hunting for a button that does not exist.
     * Reporting the counts the endpoint already returns makes the scope
     * self-evident.
     */
    public async onReload(): Promise<void> {
        const result = await this.run(
            this.getAdminService().reload(),
            "The reload failed."
        );
        if (result) {
            MessageToast.show(this.text("reloadDone", [
                String(result.agents ?? "?"), String(result.enabled ?? "?")
            ]));
        }
    }

    /**
     * Restart bounces the Cloud Foundry app, dropping in-flight chats, so it
     * asks first. `reload` covers almost every configuration change.
     */
    public onRestart(): void {
        MessageBox.confirm(this.text("restartConfirm"), {
            title: this.text("restart"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.doRestart();
                }
            }
        });
    }

    /**
     * `POST /admin/api/restart` always answers 200 — it reloads the
     * orchestrator in memory unconditionally, then makes a best-effort CF
     * API call that requires CF_API_URL/CF_USERNAME/CF_PASSWORD (or a bound
     * `cf-api` user-provided service). Locally, and on any deployment missing
     * those credentials, the CF bounce never happens even though the request
     * "succeeds" — so `runOk()` (HTTP status only) would report success. The
     * real outcome is `cf_restart.ok` in the body, which is checked here
     * instead so a local click reports the truth rather than a false toast.
     */
    private async doRestart(): Promise<void> {
        const result = await this.run(
            this.getAdminService().restart(),
            "The restart failed."
        ) as RestartResult | undefined;
        if (!result) {
            return;
        }
        if (result.cf_restart.ok) {
            MessageToast.show(this.text("restartTriggered"));
        } else {
            MessageBox.error(this.text("restartNotConfigured"));
        }
    }

    /**
     * Downloads the export as a file.
     *
     * Built from a Blob rather than linking at the endpoint because the anchor
     * would not carry the approuter session in every browser.
     */
    public async onExport(): Promise<void> {
        const config = await this.run(
            this.getAdminService().exportConfig(),
            "Could not export the configuration."
        );
        if (!config) {
            return;
        }
        const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "agents-config.json";
        anchor.click();
        URL.revokeObjectURL(url);
    }

    public async onOpenImport(): Promise<void> {
        (this.getModel("import") as JSONModel).setData({ text: "", replace: false });
        if (!this.importDialog) {
            this.importDialog = await Fragment.load({
                id: this.getView()!.getId(),
                name: "com.agent.admin.fragment.ImportDialog",
                controller: this
            }) as Dialog;
            this.getView()!.addDependent(this.importDialog);
        }
        this.importDialog.open();
    }

    public onCancelImport(): void {
        this.importDialog?.close();
    }

    /**
     * Imports the pasted bundle.
     *
     * A bundle can create or replace catalogue services (with their writes,
     * their destination and the identity they run as), give an agent "Allow
     * writes" and, with "replace", delete catalogue services. So what it
     * opens is read from the bundle first and asked about, with Cancel as
     * the default; a bundle without any of it imports as it always did.
     * When that cannot be read from the bundle the question is asked all the
     * same, in general words: never an import of this kind unseen.
     */
    public async onConfirmImport(): Promise<void> {
        if (this.importing) {
            return;
        }
        const importModel = this.getModel("import") as JSONModel;
        const raw = importModel.getProperty("/text") as string;

        let parsed: unknown;
        try {
            parsed = JSON.parse(raw);
        } catch {
            MessageBox.error(this.text("importInvalidJson"));
            return;
        }
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
            // No bundle at all: nothing the server could import.
            MessageBox.error(this.text("importNotBundle"));
            return;
        }
        const payload = parsed as ImportPayload;
        payload.replace = importModel.getProperty("/replace") === true;

        // --- bitbucket --- An agent of the bundle may get to approve pull
        // requests unattended, or to approve more than it does now. What it
        // does now is the stored agent list; when that cannot be read there
        // is no baseline and every approval of the bundle is asked about.
        this.importing = true;
        let stored: unknown;
        try {
            stored = await this.getAdminService().listAgents();
        } catch {
            stored = undefined;
        } finally {
            this.importing = false;
        }
        const found = importBundle.opens(payload, stored);
        const question = !found
            ? this.text("importAskUnknown")
            : importBundle.mustAsk(found, payload.replace) ? this.importQuestion(found, payload.replace) : "";
        if (!question) {
            void this.sendImport(payload);
            return;
        }
        const go = this.text("importConfig");
        this.importing = true;
        MessageBox.warning(question, {
            title: this.text(found && found.approvals.length > 0 ? "importAskUnattendedTitle" : "importAskTitle"),
            actions: [go, MessageBox.Action.CANCEL],
            emphasizedAction: go,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                this.importing = false;
                if (action === go) {
                    void this.sendImport(payload);
                }
            }
        });
    }

    /** What the import brings, service by service and agent by agent. */
    private importQuestion(found: BundleOpens, replace: boolean): string {
        const odata = found.services.length > 0 || found.writers.length > 0 || (replace && found.hasCatalogue);
        const parts: string[] = odata ? [this.text("importAskIntro")] : [];
        if (found.services.length) {
            parts.push([this.text("importAskServices")].concat(found.services.map((service) => this.text(
                service.writes.length ? "importAskService" : "importAskServiceNoWrites", [
                    service.title, service.name,
                    this.text(service.userContext ? "odataRunsAsUser" : "odataRunsAsTechnical"),
                    service.destination, service.writes.join("; ")
                ]
            ))).join("\n"));
        }
        if (found.writers.length) {
            parts.push([this.text("importAskWriters")].concat(found.writers.map((writer) => (
                this.text("importAskWriter", [writer.agent, writer.services.join(", ")])
            ))).join("\n"));
        }
        if (replace && found.hasCatalogue) {
            parts.push(this.text("importAskReplace"));
        }
        if (found.approvals.length) {
            // --- bitbucket --- The same words as the agent page's Save.
            const lines = [this.text("importAskApprovers")];
            found.approvals.forEach((approval) => {
                approval.asks.forEach((ask) => {
                    lines.push(bitbucketEntry.question(approval.agent, ask, (key, args) => this.text(key, args)));
                });
            });
            if (found.approvals.some((approval) => approval.asks.some((ask) => ask.reason === "approve"))) {
                lines.push(this.text("bitbucketApprovalStays"));
            }
            parts.push(lines.join("\n\n"));
        }
        return parts.join("\n\n");
    }

    private async sendImport(payload: ImportPayload): Promise<void> {
        if (this.importing) {
            return;
        }
        this.importing = true;
        let result: ImportResult;
        try {
            result = await this.getAdminService().importConfig(payload);
        } catch (error) {
            ErrorHandler.handle(error, "The import failed.");
            return;
        } finally {
            this.importing = false;
        }
        this.importDialog?.close();
        const notes = this.importNotes(result ?? { status: "" });
        if (notes.length) {
            // What the import changed for agents in use, and a reload that
            // failed, stay on screen until they are closed.
            MessageBox.warning([this.text("importDone")].concat(notes).join("\n\n"), {
                title: this.text("importNotesTitle")
            });
        } else {
            MessageToast.show(this.text("importDone"));
        }
        void this.load();
    }

    /**
     * What the answer of an import says besides "imported": the catalogue
     * services it deleted, the services in use whose destination or identity
     * it changed, the server's other warnings (its text, shown as text), and
     * a reload of the running agents that failed.
     */
    private importNotes(result: ImportResult): string[] {
        const notes: string[] = [];
        const removed = (result.removed_odata_service_names ?? []).filter((name) => typeof name === "string");
        if (removed.length) {
            notes.push(this.text("importRemovedServices", [removed.join(", ")]));
        }
        (result.odata_identity_changes ?? []).forEach((change) => {
            const fields = (change.changed ?? []).map((field) => (
                IDENTITY_FIELD_TEXT[field] ? this.text(IDENTITY_FIELD_TEXT[field]) : String(field)
            ));
            notes.push(this.text("importIdentityChange", [
                String(change.service), fields.join(", "), (change.agents ?? []).join(", ")
            ]));
        });
        const others = importBundle.otherWarnings(result);
        if (others.length) {
            notes.push([this.text("importWarnings")].concat(others).join("\n"));
        }
        if (result.reload_failed === true) {
            notes.push(this.text("reloadFailedText", [this.text("reloadFailedImported")]));
        }
        return notes;
    }

}
