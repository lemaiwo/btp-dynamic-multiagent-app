import Log, { type Listener } from "sap/base/Log";
import Fragment from "sap/ui/core/Fragment";
import JSONModel from "sap/ui/model/json/JSONModel";
import ResourceModel from "sap/ui/model/resource/ResourceModel";
import type Dialog from "sap/m/Dialog";
import type Control from "sap/ui/core/Control";

/**
 * The dialogs with a form open without the two UI5 warnings a form gives
 * for content it cannot lay out ("... is not valid Form content") or for a
 * layout that is not there yet when it renders first ("Layout missing!").
 * Runs before any journey, so the form layout module is not loaded yet.
 */
QUnit.module("dialog forms");

const FORM_WARNING = /is not valid Form content|Layout missing!/;

/** A controller for a fragment loaded on its own: every handler exists and does nothing. */
const noopController = new Proxy({}, { get: () => () => undefined });

function nextRender(ms = 200): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function warningsWhileOpening(name: string, models: Record<string, JSONModel>, id: string): Promise<string[]> {
    const warnings: string[] = [];
    const listener = {
        onLogEntry: (entry: { level: number; message: string }) => {
            if (entry.level <= (Log.Level.WARNING as unknown as number) && FORM_WARNING.test(entry.message)) {
                warnings.push(entry.message);
            }
        }
    } as unknown as Listener;
    const level = Log.getLevel();
    Log.setLevel(Log.Level.WARNING);
    Log.addLogListener(listener);
    let dialog: Dialog | undefined;
    try {
        dialog = await Fragment.load({ id, name, controller: noopController }) as Dialog;
        dialog.setModel(new ResourceModel({ bundleName: "com.agent.ide.i18n.i18n" }), "i18n");
        Object.entries(models).forEach(([key, model]) => dialog!.setModel(model, key));
        await new Promise<void>((resolve) => {
            dialog!.attachEventOnce("afterOpen", () => resolve());
            dialog!.open();
        });
        await nextRender();
    } finally {
        Log.removeLogListener(listener);
        Log.setLevel(level);
        dialog?.destroy();
    }
    return warnings;
}

QUnit.test("the new-session dialog: no form warning, and the diagnose hint describes the type field", async function (assert) {
    const ide = new JSONModel({
        diagnoseTargets: ["DEV"],
        newSession: { title: "", type: "diagnose", targets: ["DEV"], target: "DEV", titleState: "None", targetState: "None" }
    });
    const warnings = await warningsWhileOpening("com.agent.ide.fragment.NewSessionDialog", { ide }, "nsd");
    assert.deepEqual(warnings, [], "no 'not valid Form content' and no 'Layout missing!' warning");

    // The hint is still on screen with the type field and named as its description.
    const dialog = await Fragment.load({ id: "nsd2", name: "com.agent.ide.fragment.NewSessionDialog", controller: noopController }) as Dialog;
    try {
        dialog.setModel(new ResourceModel({ bundleName: "com.agent.ide.i18n.i18n" }), "i18n");
        dialog.setModel(ide, "ide");
        await new Promise<void>((resolve) => {
            dialog.attachEventOnce("afterOpen", () => resolve());
            dialog.open();
        });
        await nextRender();
        const type = Fragment.byId("nsd2", "newSessionType") as Control;
        const hint = Fragment.byId("nsd2", "newSessionDiagnoseHint") as Control;
        assert.ok(hint.getDomRef(), "the hint is shown for a diagnose session");
        assert.ok((type.getDomRef()?.getAttribute("aria-describedby") ?? "").split(" ").includes(hint.getId()),
            "the type field names the hint as its description");
        ide.setProperty("/newSession/type", "change");
        await nextRender();
        assert.notOk(hint.getDomRef(), "and hidden for a change session");
    } finally {
        dialog.destroy();
    }
});

QUnit.test("the conventions dialog: no form warning", async function (assert) {
    const conv = new JSONModel({ isAdmin: true, mode: "edit", targets: ["DEV"], target: "DEV", busy: false });
    const warnings = await warningsWhileOpening("com.agent.ide.fragment.ConventionsDialog", { conv }, "cvd");
    assert.deepEqual(warnings, [], "no 'not valid Form content' and no 'Layout missing!' warning");
});
