import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import MessageBox from "sap/m/MessageBox";
import type Dialog from "sap/m/Dialog";
import type UI5Event from "sap/ui/base/Event";
import type Select from "sap/m/Select";
import BaseController from "./BaseController";
import IdeService, { IdeError } from "../service/IdeService";
import type { Conventions } from "../service/types";

/** The fields the PUT accepts (the route rejects any other key). */
const FIELDS = ["label", "destination", "namespace", "package", "atc_variant", "clean_core_level", "free_text"] as const;

/**
 * The shell: a ToolHeader with the app title, the open session's title and
 * its target system, and the Conventions button. Later tasks fill
 * `sessionTitle` and `target`. The conventions dialog is read-only unless
 * `GET /me` says the caller is an admin (the server enforces it as well).
 *
 * @namespace com.agent.ide.controller
 */
export default class App extends BaseController {

    private readonly service = new IdeService();
    private conventionsDialog?: Promise<Dialog>;
    private conventions: Conventions[] = [];

    public onInit(): void {
        this.setModel(new JSONModel({
            // Inside a launchpad shell the host already renders a header.
            showHeader: !App.isInShell(),
            sessionTitle: "",
            target: ""
        }), "appView");
        this.setModel(new JSONModel({
            isAdmin: false, busy: false, targets: [] as string[], target: "", form: {}
        }), "conv");
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
        const dialog = await this.dialog();
        dialog.open();
        try {
            const [me, conventions] = await Promise.all([this.service.getMe(), this.service.listConventions()]);
            this.conventions = conventions;
            this.conv().setProperty("/isAdmin", !!me.is_admin);
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
        this.showTarget((event.getSource() as Select).getSelectedKey());
    }

    private showTarget(target: string): void {
        const found = this.conventions.find((c) => c.target === target);
        const form: Record<string, string> = {};
        for (const key of FIELDS) {
            form[key] = String((found as Record<string, unknown> | undefined)?.[key] ?? "");
        }
        this.conv().setProperty("/target", target);
        this.conv().setProperty("/form", form);
    }

    public async onConventionsSave(): Promise<void> {
        const target = this.conv().getProperty("/target") as string;
        if (!target || this.conv().getProperty("/busy")) {
            return;
        }
        const form = this.conv().getProperty("/form") as Record<string, string>;
        const body: Record<string, string> = {};
        for (const key of FIELDS) {
            // An unset clean core level is left out: the route only accepts A-D.
            if (key !== "clean_core_level" || form[key]) {
                body[key] = form[key] ?? "";
            }
        }
        this.conv().setProperty("/busy", true);
        try {
            const saved = await this.service.putConventions(target, body as unknown as Conventions);
            this.conventions = this.conventions.filter((c) => c.target !== target).concat(saved);
            MessageToast.show(this.text("conventionsSaved"));
        } catch (e) {
            if (e instanceof IdeError && e.status === 403) {
                MessageBox.error(this.text("conventionsForbidden"));
            } else {
                this.showError(e);
            }
        } finally {
            this.conv().setProperty("/busy", false);
        }
    }

    public onConventionsClose(): void {
        void this.dialog().then((dialog) => dialog.close());
    }

    private dialog(): Promise<Dialog> {
        this.conventionsDialog ??= this.loadFragment({
            name: "com.agent.ide.fragment.ConventionsDialog"
        }) as Promise<Dialog>;
        return this.conventionsDialog;
    }
}
