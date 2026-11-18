import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import type Event from "sap/ui/base/Event";
import type UI5Element from "sap/ui/core/Element";
import type { Model$PropertyChangeEvent } from "sap/ui/model/Model";
import providers, { PROVIDERS, type ProviderForm, type ProviderText } from "../../model/modelProviders";
import ErrorHandler from "../../service/ErrorHandler";
import { AdminError } from "../../service/AdminService";
import type AdminService from "../../service/AdminService";
import type {
    ModelProviderName, ModelProviderSave, ModelProvidersSaved, ModelProvidersStatus
} from "../../service/modelProviderTypes";
import type { ReloadOutcome } from "../../service/types";

/** What the panel needs of the Settings page. */
export interface ModelProvidersHost {
    service(): AdminService;
    text(key: string, args?: string[]): string;
    /** `BaseController.warnIfNotLive`: true when it showed the warning. */
    warnIfNotLive(outcome: ReloadOutcome | undefined, leadKey: string): boolean;
    /** The models on offer changed: the "LLM model" panel reads its list again. */
    modelsChanged(): void;
}

/** One provider's block as the `mp` model holds it (`/providers/<name>`). */
interface Block {
    provider: ModelProviderName;
    title: string;
    form: ProviderForm;
    /** The form of the stored setting, as last read or saved. */
    stored: ProviderForm;
    keyPlaceholder: string;
    keySource: { text: string; state: string };
    hasStoredKey: boolean;
    effective: string;
    dirty: boolean;
    busy: boolean;
    keyState: string;
    keyStateText: string;
    baseUrlState: string;
    baseUrlStateText: string;
    modelsState: string;
    modelsStateText: string;
    test: { visible: boolean; type: string; text: string };
}

/** The value state of each control a refusal can name, by field. */
const STATE_PATH: Record<string, [string, string]> = {
    apiKey: ["keyState", "keyStateText"],
    baseUrl: ["baseUrlState", "baseUrlStateText"],
    models: ["modelsState", "modelsStateText"]
};

/**
 * The "Model providers" panel of the Settings page (the direct model
 * providers Anthropic and OpenAI), apart from the page's controller so that
 * one stays small. The logic is the pure `model/modelProviders.ts`; this
 * class makes the calls and keeps the `mp` model the panel binds to.
 *
 * The key field is a password field that is always shown empty: the server
 * never answers a key. Test runs against what is STORED, so it is off while
 * a block holds unsaved changes.
 */
export default class ModelProviders {

    /** The panel's state, bound as `mp`. */
    public readonly model = new JSONModel(ModelProviders.emptyState());

    public constructor(private readonly host: ModelProvidersHost) {
        // Every edit through a two-way binding: the block's "unsaved" flag
        // (Test is off while it is set) and the mark of a refused field.
        this.model.attachPropertyChange((event: Model$PropertyChangeEvent) => {
            const context = event.getParameter("context");
            const path = String(context ? `${context.getPath()}/${event.getParameter("path")}` : event.getParameter("path"));
            const match = /^\/providers\/(anthropic|openai)\/form\/(\w+)$/.exec(path);
            if (match) {
                this.edited(match[1] as ModelProviderName, match[2]);
            }
        });
    }

    private static emptyState(): Record<string, unknown> {
        return { loaded: false, loading: false, loadError: "", info: "", providers: {} };
    }

    private say(text: ProviderText): string {
        return this.host.text(text.key, text.args);
    }

    private path(name: ModelProviderName, rest = ""): string {
        return `/providers/${name}${rest ? "/" + rest : ""}`;
    }

    /** The page is shown: read the providers. */
    public async load(): Promise<void> {
        this.model.setProperty("/loading", true);
        try {
            this.apply(await this.host.service().getModelProviders(), PROVIDERS);
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
                ? this.host.text("mpLoadFailed", [reason]) : this.host.text("mpLoadFailedPlain"));
        } finally {
            this.model.setProperty("/loading", false);
        }
    }

    /**
     * Shows an answer. The providers in `reset` take the stored setting into
     * their form (a load, the provider just saved); any other keeps what the
     * admin is editing when it holds unsaved changes.
     */
    private apply(status: ModelProvidersStatus, reset: ModelProviderName[]): void {
        PROVIDERS.forEach((name) => {
            const provider = providers.providerOf(status, name);
            const stored = providers.toForm(provider);
            const before = this.model.getProperty(this.path(name)) as Block | undefined;
            const keep = !!before && reset.indexOf(name) === -1 && before.dirty;
            const form = keep ? before!.form : stored;
            const source = providers.keySource(provider);
            const block: Block = {
                provider: name,
                title: this.host.text(providers.titleKey(name)),
                form: { ...form },
                stored,
                keyPlaceholder: this.say(providers.keyPlaceholder(provider)),
                keySource: { text: this.say(source.text), state: source.state },
                hasStoredKey: provider?.has_stored_key === true,
                effective: this.say(providers.effectiveText(provider)),
                dirty: providers.isDirty(form, stored),
                busy: before?.busy === true,
                keyState: "None", keyStateText: "", baseUrlState: "None", baseUrlStateText: "",
                modelsState: "None", modelsStateText: "",
                test: keep && before ? before.test : { visible: false, type: "Information", text: "" }
            };
            this.model.setProperty(this.path(name), block);
        });
        this.model.setProperty("/info", providers.infoTexts(status).map((t) => this.say(t)).join(" "));
        this.model.setProperty("/loaded", true);
    }

    /** A field of a form was edited: recompute "unsaved", clear its mark and an old test result. */
    private edited(name: ModelProviderName, field: string): void {
        const block = this.model.getProperty(this.path(name)) as Block | undefined;
        if (!block) {
            return;
        }
        this.model.setProperty(this.path(name, "dirty"), providers.isDirty(block.form, block.stored));
        const state = STATE_PATH[field];
        if (state) {
            this.model.setProperty(this.path(name, state[0]), "None");
            this.model.setProperty(this.path(name, state[1]), "");
        }
    }

    /** The provider of the block an event's control is bound to. */
    public providerOfEvent(event: Event): ModelProviderName | null {
        const source = event.getSource() as UI5Element;
        const provider = source?.getBindingContext("mp")?.getProperty("provider");
        return provider === "anthropic" || provider === "openai" ? provider : null;
    }

    private clearMarks(name: ModelProviderName): void {
        Object.keys(STATE_PATH).forEach((field) => {
            this.model.setProperty(this.path(name, STATE_PATH[field][0]), "None");
            this.model.setProperty(this.path(name, STATE_PATH[field][1]), "");
        });
    }

    /** Runs `work` with the block's buttons off. */
    private async busy<T>(name: ModelProviderName, work: () => Promise<T>): Promise<T> {
        this.model.setProperty(this.path(name, "busy"), true);
        try {
            return await work();
        } finally {
            this.model.setProperty(this.path(name, "busy"), false);
        }
    }

    /** Save: the form's body (the key only when one was typed). */
    public save(name: ModelProviderName): Promise<void> {
        const block = this.model.getProperty(this.path(name)) as Block | undefined;
        if (!block || block.busy) {
            return Promise.resolve();
        }
        return this.send(name, providers.fromForm(block.form), "mpSaved", "mpReloadFailedSaved");
    }

    /** "Remove stored key": asks first (Cancel has the focus), then sends `api_key: null`. */
    public removeKey(name: ModelProviderName): void {
        const block = this.model.getProperty(this.path(name)) as Block | undefined;
        if (!block || block.busy || !block.hasStoredKey) {
            return;
        }
        const go = this.host.text("mpRemoveKeyAction");
        MessageBox.warning(this.host.text("mpRemoveKeyConfirm", [block.title]), {
            title: this.host.text("mpRemoveKeyTitle"),
            actions: [go, MessageBox.Action.CANCEL],
            emphasizedAction: go,
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (action: string | null) => {
                if (action === go) {
                    void this.send(name, providers.removeKeyBody(block.stored), "mpKeyRemoved", "mpReloadFailedKeyRemoved",
                        true);
                }
            }
        });
    }

    /**
     * The PUT. After it the key field is empty and the block shows the
     * answer; a removal of the key keeps what else the admin was editing. A
     * refusal that names a field of the block is shown at that field.
     */
    private async send(name: ModelProviderName, body: ModelProviderSave, doneKey: string, leadKey: string,
        keepEdits = false): Promise<void> {
        this.clearMarks(name);
        const answer = await this.busy(name, async (): Promise<ModelProvidersSaved | undefined> => {
            try {
                return await this.host.service().saveModelProvider(name, body);
            } catch (error) {
                const detail = error instanceof AdminError && error.status === 422 ? error.detail : "";
                const field = providers.refusedField(detail);
                if (field) {
                    this.model.setProperty(this.path(name, STATE_PATH[field][0]), "Error");
                    this.model.setProperty(this.path(name, STATE_PATH[field][1]), detail);
                    return undefined;
                }
                ErrorHandler.handle(error, this.host.text("mpSaveFailed"));
                return undefined;
            }
        });
        if (!answer) {
            return;
        }
        const edits = keepEdits ? (this.model.getProperty(this.path(name, "form")) as ProviderForm) : null;
        this.apply(answer, keepEdits ? [] : [name]);
        if (edits) {
            // The key field is empty after any answer; the other edits stay.
            const form = { ...edits, apiKey: "" };
            const stored = this.model.getProperty(this.path(name, "stored")) as ProviderForm;
            this.model.setProperty(this.path(name, "form"), form);
            this.model.setProperty(this.path(name, "dirty"), providers.isDirty(form, stored));
        }
        this.model.setProperty(this.path(name, "test"), { visible: false, type: "Information", text: "" });
        const title = this.host.text(providers.titleKey(name));
        if (!this.host.warnIfNotLive(answer, leadKey)) {
            MessageToast.show(this.host.text(doneKey, [title]));
        }
        this.host.modelsChanged();
    }

    /** Test: one call with the STORED key; not while the block holds unsaved changes. */
    public async test(name: ModelProviderName): Promise<void> {
        const block = this.model.getProperty(this.path(name)) as Block | undefined;
        if (!block || block.busy || block.dirty) {
            return;
        }
        this.model.setProperty(this.path(name, "test"), { visible: false, type: "Information", text: "" });
        const result = await this.busy(name, async () => {
            try {
                return await this.host.service().testModelProvider(name);
            } catch (error) {
                ErrorHandler.handle(error, this.host.text("mpTestFailed"));
                return undefined;
            }
        });
        if (!result) {
            return;
        }
        const outcome = providers.testOutcome(result);
        this.model.setProperty(this.path(name, "test"), { visible: true, type: outcome.type, text: this.say(outcome.text) });
    }
}
