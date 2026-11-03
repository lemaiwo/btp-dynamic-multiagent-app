import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import Fragment from "sap/ui/core/Fragment";
import type Dialog from "sap/m/Dialog";
import type View from "sap/ui/core/mvc/View";
import type { ListBase$SelectionChangeEvent } from "sap/m/ListBase";
import Poller, { type PollOutcome } from "../../model/autoRefresh";
import embeddings, { POLL_INTERVAL_MS, type EmbeddingsText } from "../../model/embeddingsSettings";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type AdminService from "../../service/AdminService";
import type { EmbeddingSetting, EmbeddingsStatus } from "../../service/dataCatalogTypes";

/** What the panel needs of the Settings page. */
export interface EmbeddingsHost {
    view(): View;
    service(): AdminService;
    text(key: string, args?: string[]): string;
}

/** After a failed poll the next one waits longer, as the run page's does. */
const SLOW_POLL_MS = 10000;

/**
 * The "Data catalogue embeddings" panel of the Settings page (design
 * addendum (l), point 4) and its deploy dialog, apart from the page's
 * controller so that one stays small. The logic is the pure
 * `model/embeddingsSettings.ts`; this class makes the calls and keeps the
 * `emb` model the panel binds to.
 *
 * The status is read when the page is shown and polled every 2 s only while
 * a run is in flight AND the page is shown: `hide()` (another route, a page
 * that matches none, the controller's exit) stops the polling, `show()`
 * reads the status again and resumes it.
 */
export default class Embeddings {

    /** The panel's state, bound as `emb`. */
    public readonly model = new JSONModel(Embeddings.emptyState());

    private readonly poller = new Poller(() => this.poll(), { intervalMs: POLL_INTERVAL_MS, slowIntervalMs: SLOW_POLL_MS });
    /** The setting as last read or saved: a save first checks it is still stored. */
    private baseline: EmbeddingSetting | null = null;
    private shown = false;
    private pollFailed = false;
    private dialog?: Dialog;

    public constructor(private readonly host: EmbeddingsHost) {}

    private static emptyState(): Record<string, unknown> {
        return {
            loaded: false, loading: false, loadError: "", busy: false,
            lines: {}, options: [], form: { enabled: false, modelKey: "" }, modelState: "None", modelStateText: "",
            envStrip: "", canRun: false, running: false,
            deploy: { options: [], selected: "", resourceGroup: "" }
        };
    }

    private say(text: EmbeddingsText): string {
        return this.host.text(text.key, text.args);
    }

    /** The page is shown: read the status (and resume the polling of a run in flight). */
    public show(): Promise<void> {
        this.shown = true;
        return this.load();
    }

    /** The page is no longer shown: nothing polls behind it. */
    public hide(): void {
        this.shown = false;
        this.poller.stop();
    }

    private async load(): Promise<void> {
        this.model.setProperty("/loading", true);
        try {
            this.apply(await this.host.service().getEmbeddings(), true);
            this.model.setProperty("/loadError", "");
        } catch (error) {
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
                return;
            }
            // In the panel, not in a box: the page's other settings work without it.
            const reason = ErrorHandler.messageFor(error, "");
            this.model.setProperty("/loadError", reason
                ? this.host.text("embLoadFailed", [reason]) : this.host.text("embLoadFailedPlain"));
        } finally {
            this.model.setProperty("/loading", false);
        }
    }

    /**
     * Shows a status. `withForm`: the switch and the Model select take the
     * stored setting as well (a load, a save); a poll leaves what the admin
     * is editing alone. A run that ended since the last answer says how in a
     * toast.
     */
    private apply(status: EmbeddingsStatus, withForm: boolean): void {
        const model = this.model;
        const wasRunning = model.getProperty("/running") === true;
        const lines: Record<string, { text: string; state: string }> = {};
        embeddings.statusLines(status).forEach((line) => {
            lines[line.label] = { text: this.say(line.text), state: line.state };
        });
        model.setProperty("/lines", lines);
        model.setProperty("/options", embeddings.modelOptions(status).map((option) => (
            { key: option.key, text: this.say(option.text) })));
        if (withForm) {
            const form = embeddings.formOf(status);
            model.setProperty("/form", form);
            this.baseline = embeddings.settingBody(form);
            this.clearModelError();
        }
        const strip = embeddings.envStripText(status);
        model.setProperty("/envStrip", strip ? this.say(strip) : "");
        model.setProperty("/canRun", embeddings.canRun(status));
        model.setProperty("/running", status.running === true);
        model.setProperty("/deploy/options", embeddings.deployOptions(status).map((offer) => ({
            model: offer.model, versions: offer.versions ? this.host.text("embDeployVersions", [offer.versions]) : ""
        })));
        model.setProperty("/deploy/resourceGroup", typeof status.resource_group === "string" ? status.resource_group : "");
        model.setProperty("/loaded", true);

        const outcome = wasRunning ? embeddings.runOutcome(status) : null;
        if (outcome) {
            MessageToast.show(this.say(outcome));
        }
        if (status.running === true && this.shown) {
            if (!this.poller.isActive()) {
                this.pollFailed = false;
                this.poller.start("live");
            }
        } else {
            this.poller.stop();
        }
    }

    /** One poll, without a busy indicator. */
    private async poll(): Promise<PollOutcome> {
        if (!this.shown) {
            return "done";
        }
        let status: EmbeddingsStatus;
        try {
            status = await this.host.service().getEmbeddings();
        } catch (error) {
            const kind = ErrorHandler.classify(error);
            if (kind === "session" || kind === "forbidden") {
                ErrorHandler.handle(error);
                return "done";
            }
            if (!this.pollFailed) {
                MessageToast.show(this.host.text("embPollFailed"));
            }
            this.pollFailed = true;
            return "failed";
        }
        this.pollFailed = false;
        if (!this.shown) {
            return "done";
        }
        this.apply(status, false);
        return status.running === true ? "live" : "done";
    }

    public clearModelError(): void {
        this.model.setProperty("/modelState", "None");
        this.model.setProperty("/modelStateText", "");
    }

    /** Runs `work` with the panel's buttons off. */
    private async busy<T>(work: () => Promise<T>): Promise<T> {
        this.model.setProperty("/busy", true);
        try {
            return await work();
        } finally {
            this.model.setProperty("/busy", false);
        }
    }

    /**
     * Save: the status is read first, and when the stored setting is no
     * longer the one this page read (another admin saved meanwhile) the
     * panel shows the stored one and nothing is sent; else the PUT. A
     * refusal that names the model is shown at the Model select.
     */
    public async save(): Promise<void> {
        if (this.model.getProperty("/busy") === true) {
            return;
        }
        const body = embeddings.settingBody(this.model.getProperty("/form") as { enabled: boolean; modelKey: string });
        this.clearModelError();
        await this.busy(async () => {
            try {
                const fresh = await this.host.service().getEmbeddings();
                const stored = embeddings.settingBody(embeddings.formOf(fresh));
                if (!this.baseline || stored.enabled !== this.baseline.enabled || stored.model !== this.baseline.model) {
                    this.apply(fresh, true);
                    MessageBox.warning(this.host.text("embChangedElsewhere"), {
                        title: this.host.text("embChangedElsewhereTitle")
                    });
                    return;
                }
                this.apply(await this.host.service().setEmbeddings(body), true);
                MessageToast.show(this.host.text("embSaved"));
            } catch (error) {
                const detail = error instanceof AdminError && error.status === 422 ? error.detail : "";
                if (embeddings.refusedField(detail) === "model") {
                    this.model.setProperty("/modelState", "Error");
                    this.model.setProperty("/modelStateText", detail);
                    return;
                }
                ErrorHandler.handle(error, this.host.text("embSaveFailed"));
            }
        });
    }

    /** "Compute embeddings now": 202, then the status is read at once and polled while the run is in flight. */
    public async compute(): Promise<void> {
        if (this.model.getProperty("/busy") === true || this.model.getProperty("/canRun") !== true) {
            return;
        }
        const started = await this.busy(async () => {
            try {
                await this.host.service().runEmbeddings();
                return true;
            } catch (error) {
                // A 409 says a run is in flight or no model is in effect: the server's sentence.
                ErrorHandler.handle(error, this.host.text("embRunStartFailed"));
                return false;
            }
        });
        if (started) {
            this.model.setProperty("/running", true);
            this.model.setProperty("/canRun", false);
            this.model.setProperty("/lines/embLineLastRun", { text: this.host.text("embRunRunning"), state: "Information" });
        }
        await this.refresh();
    }

    /** Reads the status without touching the form (after a run's start, a deployment). */
    private async refresh(): Promise<void> {
        if (!this.shown) {
            return;
        }
        try {
            this.apply(await this.host.service().getEmbeddings(), false);
        } catch (error) {
            ErrorHandler.handle(error, this.host.text("embLoadFailedPlain"));
        }
    }

    // --- the deploy dialog ---------------------------------------------------

    public async openDeploy(): Promise<void> {
        if (!this.dialog) {
            const view = this.host.view();
            this.dialog = await Fragment.load({
                id: view.getId(),
                name: "com.agent.admin.fragment.EmbeddingsDeployDialog",
                controller: this
            }) as Dialog;
            view.addDependent(this.dialog);
        }
        this.model.setProperty("/deploy/selected", "");
        this.dialog.open();
    }

    public onDeployPick(event: ListBase$SelectionChangeEvent): void {
        const item = event.getParameter("listItem");
        const model = item?.getBindingContext("emb")?.getProperty("model");
        this.model.setProperty("/deploy/selected", typeof model === "string" ? model : "");
    }

    public onDeployCancel(): void {
        this.dialog?.close();
    }

    /**
     * Asks before anything is deployed, naming the model, the resource group
     * and that AI Core bills for it; Cancel has the focus.
     */
    public onDeployConfirm(): void {
        const model = String(this.model.getProperty("/deploy/selected") ?? "");
        if (!model || this.model.getProperty("/busy") === true) {
            return;
        }
        const go = this.host.text("embDeployAction");
        MessageBox.warning(this.say(embeddings.deployConfirmText(model,
            this.model.getProperty("/deploy/resourceGroup") as string)), {
            title: this.host.text("embDeployTitle"),
            actions: [go, MessageBox.Action.CANCEL],
            emphasizedAction: go,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === go) {
                    void this.deploy(model);
                }
            }
        });
    }

    private async deploy(model: string): Promise<void> {
        const result = await this.busy(async () => {
            try {
                return await this.host.service().deployEmbeddingModel(model);
            } catch (error) {
                ErrorHandler.handle(error, this.host.text("embDeployFailed"));
                return undefined;
            }
        });
        if (!result) {
            return;
        }
        this.dialog?.close();
        MessageToast.show(this.host.text("embDeployRequested", [model, String(result.status ?? "")]));
        await this.refresh();
    }
}
