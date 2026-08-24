import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import Fragment from "sap/ui/core/Fragment";
import type Dialog from "sap/m/Dialog";
import BaseController from "./BaseController";
import type { ImportPayload } from "../service/types";

/** Shape of `POST /admin/api/restart`'s body (agents/admin.py:api_restart).
 * The endpoint always answers 200 even when the CF bounce itself did not
 * happen — success/failure of the actual restart is carried in
 * `cf_restart.ok`, not the HTTP status, so `runOk()` cannot be used here. */
interface RestartResult {
    cf_restart: { ok: boolean; reason?: string };
}

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class Settings extends BaseController {

    private importDialog?: Dialog;

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

    private async load(): Promise<void> {
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

    public async onReload(): Promise<void> {
        const ok = await this.runOk(
            this.getAdminService().reload(),
            "The reload failed."
        );
        if (ok) {
            MessageToast.show(this.text("reloadDone"));
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
                name: "com.infrabel.agentadmin.fragment.ImportDialog",
                controller: this
            }) as Dialog;
            this.getView()!.addDependent(this.importDialog);
        }
        this.importDialog.open();
    }

    public onCancelImport(): void {
        this.importDialog?.close();
    }

    public async onConfirmImport(): Promise<void> {
        const importModel = this.getModel("import") as JSONModel;
        const raw = importModel.getProperty("/text") as string;

        let payload: ImportPayload;
        try {
            payload = JSON.parse(raw) as ImportPayload;
        } catch {
            MessageBox.error(this.text("importInvalidJson"));
            return;
        }
        payload.replace = importModel.getProperty("/replace") as boolean;

        const ok = await this.runOk(
            this.getAdminService().importConfig(payload),
            "The import failed."
        );
        if (ok) {
            this.importDialog?.close();
            MessageToast.show(this.text("importDone"));
            void this.load();
        }
    }

}
