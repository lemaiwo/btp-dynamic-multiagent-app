import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import type Dialog from "sap/m/Dialog";
import type UI5Event from "sap/ui/base/Event";
import type Select from "sap/m/Select";
import type Control from "sap/ui/core/Control";
import BaseController from "./BaseController";
import { MODEL_SIZE_LIMIT } from "../model/formatter";
import {
    CONVENTIONS_CHANGED, CONVENTIONS_CHANNEL, createBody, formOf, isValidTarget, resync, updateBody,
    type ConventionsChangedData, type Field, type Form
} from "../model/conventionsForm";
import { IdeError } from "../service/IdeService";
import type IdeService from "../service/IdeService";
import type { Conventions } from "../service/types";
import { applyThemeVars } from "../model/themeVars";

type Mode = "edit" | "create";

/**
 * The shell: a ToolHeader with the app name and the Conventions button.
 * The pages carry their own titles; the session page links back to the
 * worklist. `appView>/target` is the open session's target (set by the
 * session page, cleared on the worklist): the conventions dialog opens on it.
 *
 * The conventions dialog (Task U12) is read-only unless `GET /me` says the
 * caller is an admin (the server enforces it as well: 403). An admin
 * - creates a target (`POST /conventions`; the name checked against the
 *   server's rule first, `target_exists` shown on the name),
 * - saves only the fields changed against `/base`, the row as the form last
 *   loaded it (never a row another write replaced meanwhile), an emptied
 *   field as an explicit `clear` (`model/conventionsForm`),
 * - sets or clears `non_production` only through a confirmation that names
 *   the consequence. The confirmation has no emphasized action and starts on
 *   Cancel, so Enter or Escape change nothing. The row is read again before
 *   asking, so the text describes the real change (already as wished: no
 *   question). The confirmed flag is sent alone, at once; until the answer
 *   the switch shows the stored state, then the server's answer, and after a
 *   failure the row is read back so it shows the server's state, never the
 *   wished one. A flag answer re-syncs the fields the user did not touch.
 *   Save never sends the flag.
 * Every write publishes {@link CONVENTIONS_CHANGED} so the new-session dialog
 * offers what the server now has. Unsaved edits are never dropped silently:
 * switching target, New target, Close and Escape ask first (Discard or
 * Cancel, the focus on Cancel, no emphasized action).
 *
 * @namespace com.agent.ide.controller
 */
export default class App extends BaseController {

    private conventionsDialog?: Promise<Dialog>;
    private conventions: Conventions[] = [];
    /** In-flight guard of every conventions write (Save, create, flag). */
    private writing = false;
    /** The target shown before "New target", for Cancel. */
    private previousTarget = "";
    /** The target whose stored row the form shows (the Select's binding moves before its change event). */
    private shownTarget = "";

    public onInit(): void {
        // The app CSS (the worklist's too) reads theme parameters as CSS variables: publish them on any first route.
        applyThemeVars();
        this.setModel(new JSONModel({
            // Inside a launchpad shell the host already renders a header.
            showHeader: !App.isInShell(),
            target: ""
        }), "appView");
        const conv = new JSONModel({
            isAdmin: false, busy: false, targets: [] as string[], target: "", form: formOf(undefined), base: formOf(undefined),
            mode: "edit" as Mode, newTarget: "", newTargetState: "None", newTargetStateText: "",
            nonProduction: false, storedNonProduction: false, storedCleanCore: "", retentionDays: 0, flagStripText: ""
        });
        // The target list binds a Select: the default of 100 entries would cut it silently.
        conv.setSizeLimit(MODEL_SIZE_LIMIT);
        this.setModel(conv, "conv");
    }

    /** The component's shared service: one CSRF token for the whole app. */
    private service(): IdeService {
        return this.getOwnerComponentTyped().getIdeService();
    }

    private conv(): JSONModel {
        return this.getModel("conv") as JSONModel;
    }

    private static isInShell(): boolean {
        return typeof (window as { sap?: { ushell?: unknown } }).sap?.ushell !== "undefined";
    }

    // --- Conventions ---------------------------------------------------------

    public async onConventions(): Promise<void> {
        this.conv().setProperty("/busy", true);
        this.conv().setProperty("/mode", "edit");
        const dialog = await this.dialog();
        dialog.open();
        try {
            const [me, conventions] = await Promise.all([this.service().getMe(), this.service().listConventions()]);
            this.conventions = conventions;
            this.conv().setProperty("/isAdmin", !!me.is_admin);
            this.conv().setProperty("/retentionDays", Number(me.diagnose_retention_days) || 0);
            this.conv().setProperty("/targets", conventions.map((c) => c.target));
            const open = this.conv().getProperty("/target") as string;
            const current = (this.getModel("appView") as JSONModel).getProperty("/target") as string;
            const wanted = [open, current].find((t) => t && conventions.some((c) => c.target === t));
            this.showTarget(wanted ?? conventions[0]?.target ?? "");
        } catch (e) {
            this.showError(e);
        } finally {
            this.conv().setProperty("/busy", false);
        }
    }

    public onConventionsTarget(event: UI5Event): void {
        const next = (event.getSource() as Select).getSelectedKey();
        if (next === this.shownTarget) {
            return;
        }
        if (!this.dirty()) {
            this.showTarget(next);
            return;
        }
        // Until the answer, the edited target stays selected.
        this.conv().setProperty("/target", this.shownTarget);
        this.confirmDiscard(() => this.showTarget(next), () => this.focus("convTarget"));
    }

    /** The form holds edits the server does not have: changed fields, or a new target being typed. */
    private dirty(): boolean {
        if (!this.conv().getProperty("/isAdmin")) {
            return false;
        }
        const form = this.conv().getProperty("/form") as Record<Field, string>;
        if (this.conv().getProperty("/mode") === "create") {
            return !!String(this.conv().getProperty("/newTarget") ?? "").trim()
                || Object.values(form).some((value) => !!value && !!value.trim());
        }
        if (!this.conventions.some((c) => c.target === this.shownTarget)) {
            return false;
        }
        const { body, clear } = updateBody(this.base(), form);
        return Object.keys(body).length > 0 || clear.length > 0;
    }

    /**
     * Runs `proceed` at once when nothing is unsaved; otherwise only after the
     * explicit Discard. Cancel, Escape and Enter (the focus starts on Cancel,
     * no action is emphasized) keep the edits and run `keep`.
     */
    private confirmDiscard(proceed: () => void, keep?: () => void): void {
        if (!this.dirty()) {
            proceed();
            return;
        }
        const discard = this.text("conventionsDiscardAction");
        const message = this.conv().getProperty("/mode") === "create"
            ? this.text("conventionsDiscardNewText") : this.text("conventionsDiscardText", [this.shownTarget]);
        MessageBox.warning(message, {
            title: this.text("conventionsDiscardTitle"),
            actions: [discard, MessageBox.Action.CANCEL],
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (answer: string | null) => {
                if (answer === discard) {
                    proceed();
                } else {
                    keep?.();
                }
            }
        });
    }

    /**
     * Shows `target` as stored (the last server answer); unsaved edits of another target are dropped.
     * The only place the Save baseline is taken from a whole row (`resync` moves it on afterwards).
     */
    private showTarget(target: string): void {
        const found = this.conventions.find((c) => c.target === target);
        this.shownTarget = target;
        this.conv().setProperty("/target", target);
        this.conv().setProperty("/base", formOf(found));
        this.conv().setProperty("/form", formOf(found));
        this.showFlag(found);
    }

    private base(): Form {
        return this.conv().getProperty("/base") as Form;
    }

    /**
     * Another answer for the shown target (the flag, a re-read): the flag as the server has it, the
     * fields the user did not touch from that row, the edited ones as typed; the row is the new baseline.
     */
    private showFresh(row: Conventions): void {
        if (this.conv().getProperty("/mode") !== "edit" || this.conv().getProperty("/target") !== row.target) {
            return;
        }
        const next = resync(this.base(), this.conv().getProperty("/form") as Form, row);
        this.conv().setProperty("/base", next.base);
        this.conv().setProperty("/form", next.form);
        this.showFlag(row);
    }

    /** The flag as the server answered it: the switch, the stored copy and the warning strip. */
    private showFlag(row: Conventions | undefined): void {
        const on = row?.non_production === true;
        this.conv().setProperty("/nonProduction", on);
        this.conv().setProperty("/storedNonProduction", on);
        this.conv().setProperty("/storedCleanCore", row?.clean_core_level ?? "");
        this.conv().setProperty("/flagStripText", row ? this.text("conventionsNonProductionStrip", [row.target]) : "");
    }

    /** Replaces the stored row with the server's answer. */
    private remember(saved: Conventions): void {
        const index = this.conventions.findIndex((c) => c.target === saved.target);
        if (index >= 0) {
            this.conventions[index] = saved;
        } else {
            this.conventions.push(saved);
        }
        this.conv().setProperty("/targets", this.conventions.map((c) => c.target));
        // Only the target answered for: a page merges it into its own (maybe newer) list.
        const data: ConventionsChangedData = { target: saved.target, nonProduction: saved.non_production === true };
        this.getOwnerComponent()?.getEventBus().publish(CONVENTIONS_CHANNEL, CONVENTIONS_CHANGED, data);
    }

    private stored(): Conventions | undefined {
        const target = this.conv().getProperty("/target") as string;
        return this.conventions.find((c) => c.target === target);
    }

    // --- create --------------------------------------------------------------

    public onConventionsNewTarget(): void {
        this.confirmDiscard(() => this.startNewTarget());
    }

    private startNewTarget(): void {
        this.previousTarget = this.shownTarget;
        this.conv().setProperty("/mode", "create");
        this.conv().setProperty("/newTarget", "");
        this.setNewTargetState("None", "");
        this.conv().setProperty("/form", formOf(undefined));
        this.showFlag(undefined);
        this.focus("convNewTarget");
    }

    public onConventionsCancelNew(): void {
        this.conv().setProperty("/mode", "edit");
        this.showTarget(this.previousTarget);
        this.focus("convNewTargetButton");
    }

    /** Typing a name re-checks it against the server's rule (an empty field is not an error yet). */
    public onConventionsNewTargetLive(event: UI5Event): void {
        // liveChange runs before the binding takes the value: read it from the event.
        const name = String((event.getParameters() as { value?: string }).value ?? "").trim();
        if (!name || isValidTarget(name)) {
            this.setNewTargetState("None", "");
        } else {
            this.setNewTargetState("Error", this.text("conventionsTargetInvalid"));
        }
    }

    private setNewTargetState(state: "None" | "Error", text: string): void {
        this.conv().setProperty("/newTargetState", state);
        this.conv().setProperty("/newTargetStateText", text);
    }

    private async create(): Promise<void> {
        const name = String(this.conv().getProperty("/newTarget") ?? "").trim();
        if (!isValidTarget(name)) {
            this.setNewTargetState("Error", this.text(name ? "conventionsTargetInvalid" : "conventionsTargetRequired"));
            this.focus("convNewTarget");
            return;
        }
        const form = this.conv().getProperty("/form") as Record<Field, string>;
        await this.write(async () => {
            const saved = await this.service().createConventions(createBody(name, form));
            this.remember(saved);
            this.conv().setProperty("/mode", "edit");
            this.showTarget(saved.target);
            MessageToast.show(this.text("conventionsCreated", [saved.target]));
        }, (e) => {
            if (e instanceof IdeError && e.code === "target_exists") {
                this.setNewTargetState("Error", this.text("conventionsTargetExists"));
                this.focus("convNewTarget");
                return true;
            }
            if (e instanceof IdeError && e.status === 422 && "target" in e.fieldErrors) {
                this.setNewTargetState("Error", this.text("conventionsTargetInvalid"));
                this.focus("convNewTarget");
                return true;
            }
            if (e instanceof IdeError && e.status === 422) {
                // Another field: the name is fine, the server says what it refused.
                MessageBox.error(this.text("conventionsRefused", [App.refusal(e)]));
                return true;
            }
            return false;
        });
    }

    // --- save and clear --------------------------------------------------------

    public onConventionsClear(field: Field): void {
        this.conv().setProperty(`/form/${field}`, "");
        // The clear button disables itself: keep the focus on the emptied field.
        this.focus(CLEAR_TARGETS[field] ?? "convLabel");
    }

    public async onConventionsSave(): Promise<void> {
        if (this.writing || this.conv().getProperty("/busy")) {
            return;
        }
        if (this.conv().getProperty("/mode") === "create") {
            await this.create();
            return;
        }
        const row = this.stored();
        if (!row) {
            return;
        }
        const form = this.conv().getProperty("/form") as Form;
        const { body, clear } = updateBody(this.base(), form);
        if (!Object.keys(body).length && !clear.length) {
            MessageToast.show(this.text("conventionsNothingToSave"));
            return;
        }
        await this.write(async () => {
            const saved = await this.service().putConventions(row.target, body, clear);
            this.remember(saved);
            this.showTarget(saved.target);
            MessageToast.show(this.text("conventionsSaved"));
        });
    }

    /**
     * Runs one write under the in-flight guard and the busy indicator.
     * `handled` may word a refusal itself; otherwise a 403 is the missing admin
     * role and anything else goes through errorText (a refused CSRF token among them).
     */
    private async write(run: () => Promise<void>, handled?: (e: unknown) => boolean): Promise<void> {
        if (this.writing) {
            return;
        }
        this.writing = true;
        this.conv().setProperty("/busy", true);
        try {
            await run();
        } catch (e) {
            if (handled?.(e)) {
                return;
            }
            if (e instanceof IdeError && e.status === 403 && e.code !== "csrf_failed") {
                MessageBox.error(this.text("conventionsForbidden"));
            } else {
                this.showError(e);
            }
        } finally {
            this.writing = false;
            this.conv().setProperty("/busy", false);
        }
    }

    // --- the non-production flag ----------------------------------------------

    /**
     * The switch moved: nothing is sent until the confirmation is answered with
     * its explicit action. Cancel, Escape or Enter (the focus starts on Cancel,
     * no action is emphasized) put the switch back to the stored state.
     */
    public onConventionsFlag(): void {
        const row = this.stored();
        const wanted = this.conv().getProperty("/nonProduction") === true;
        const stored = this.conv().getProperty("/storedNonProduction") === true;
        if (!row || this.writing || wanted === stored || this.conv().getProperty("/mode") !== "edit") {
            this.conv().setProperty("/nonProduction", stored);
            return;
        }
        void this.confirmFlag(row.target, wanted);
    }

    /**
     * Reads the row again first, so the question describes the real change: when the server already
     * has the wished state (another admin), it is shown and nothing is asked or sent. A failed read
     * asks nothing either: the switch goes back to the stored state.
     */
    private async confirmFlag(target: string, wanted: boolean): Promise<void> {
        let fresh: Conventions | undefined;
        this.writing = true;
        this.conv().setProperty("/busy", true);
        try {
            fresh = await this.service().getConventions(target);
        } catch (e) {
            this.showError(e);
        } finally {
            this.writing = false;
            this.conv().setProperty("/busy", false);
        }
        if (!fresh) {
            this.conv().setProperty("/nonProduction", this.conv().getProperty("/storedNonProduction") === true);
            return;
        }
        this.remember(fresh);
        if (this.conv().getProperty("/target") !== target || this.conv().getProperty("/mode") !== "edit") {
            return;
        }
        this.showFresh(fresh);
        if ((fresh.non_production === true) === wanted) {
            MessageToast.show(this.text(wanted ? "conventionsFlagAlreadyOn" : "conventionsFlagAlreadyOff", [target]));
            return;
        }
        // The wish stays visible while it is asked about.
        this.conv().setProperty("/nonProduction", wanted);
        const days = Number(this.conv().getProperty("/retentionDays")) || 0;
        const action = this.text(wanted ? "conventionsFlagOnAction" : "conventionsFlagOffAction");
        const key = wanted ? "conventionsFlagOnText" : "conventionsFlagOffText";
        const message = days > 0 ? this.text(key, [target, days]) : this.text(`${key}Forever`, [target]);
        MessageBox.warning(message, {
            title: this.text(wanted ? "conventionsFlagOnTitle" : "conventionsFlagOffTitle"),
            actions: [action, MessageBox.Action.CANCEL],
            initialFocus: MessageBox.Action.CANCEL,
            onClose: (answer: string | null) => {
                // Until the server answers, the switch shows the stored state, never the wish.
                this.conv().setProperty("/nonProduction", this.conv().getProperty("/storedNonProduction") === true);
                if (answer === action) {
                    void this.saveFlag(target, wanted);
                }
                this.focus("convNonProduction");
            }
        });
    }

    private async saveFlag(target: string, wanted: boolean): Promise<void> {
        let failed = false;
        await this.write(async () => {
            const saved = await this.service().putConventions(target, { non_production: wanted });
            this.remember(saved);
            // The flag and the untouched fields as answered: unsaved edits of the other fields stay in the form.
            this.showFresh(saved);
            MessageToast.show(this.text(saved.non_production ? "conventionsFlagOnDone" : "conventionsFlagOffDone", [target]));
        }, () => {
            failed = true;
            return false;
        });
        if (failed) {
            await this.rereadFlag(target);
        }
    }

    /** After a failed flag write the server's state is unknown here: read it back and show that. */
    private async rereadFlag(target: string): Promise<void> {
        try {
            const fresh = await this.service().getConventions(target);
            this.remember(fresh);
            this.showFresh(fresh);
        } catch {
            // Keep the last known server state.
            if (this.conv().getProperty("/target") === target) {
                this.showFlag(this.conventions.find((c) => c.target === target));
            }
        }
    }

    /** A 422's reason: the server's sentence, or its field messages. */
    private static refusal(e: IdeError): string {
        return e.detail || Object.entries(e.fieldErrors).map(([field, msg]) => `${field}: ${msg}`).join("; ") || e.message;
    }

    // --- helpers ---------------------------------------------------------------

    private focus(id: string): void {
        (this.byId(id) as Control | undefined)?.focus();
    }

    public onConventionsClose(): void {
        this.confirmDiscard(() => {
            void this.dialog().then((dialog) => dialog.close());
        });
    }

    private dialog(): Promise<Dialog> {
        this.conventionsDialog ??= (this.loadFragment({
            name: "com.agent.ide.fragment.ConventionsDialog"
        }) as Promise<Dialog>).then((dialog) => {
            // Escape closes like Close: with unsaved edits only after Discard.
            dialog.setEscapeHandler((handlers: { resolve: Function; reject: Function }) => {
                this.confirmDiscard(() => { handlers.resolve(); }, () => { handlers.reject(); });
            });
            return dialog;
        });
        return this.conventionsDialog;
    }
}

/** The input each clear button empties, to keep the focus there. */
const CLEAR_TARGETS: Partial<Record<Field, string>> = {
    label: "convLabel", destination: "convDestination", namespace: "convNamespace",
    package: "convPackage", atc_variant: "convAtc", free_text: "convFreeText"
};
