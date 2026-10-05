import Opa5 from "sap/ui/test/Opa5";
import ElementRegistry from "sap/ui/core/ElementRegistry";
import type Dialog from "sap/m/Dialog";
import FakeBackend, { type FailNext } from "../FakeBackend";

export const backend = new FakeBackend();

/**
 * Shared arrangements. reset()/install() run as a queued step, not inline:
 * every Given/When/Then call only enqueues work on OPA5's queue (the same
 * reasoning as ui5-admin's pages/Common.ts).
 */
export default class Common extends Opa5 {

    public iStartTheApp(hash = "", failNext?: FailNext, setup?: (fake: FakeBackend) => void): void {
        this.waitFor({
            success: function () {
                backend.reset();
                if (failNext) {
                    backend.failNext = failNext;
                }
                setup?.(backend);
                backend.install();
            }
        });
        this.iStartMyUIComponent({
            componentConfig: { name: "com.agent.ide", async: true },
            hash
        });
    }

    public iStopTheApp(): void {
        // A dialog left open keeps its block layer in the DOM and makes the
        // next journey's controls "not interactable"; destroy, not close, so
        // no onClose handler runs as a side effect of cleanup.
        this.waitFor({
            success: function () {
                ElementRegistry.filter(function (element) {
                    return element.isA("sap.m.Dialog");
                }).forEach(function (element) {
                    (element as Dialog).destroy();
                });
            }
        });
        this.iTeardownMyUIComponent();
        this.waitFor({
            success: function () {
                backend.restore();
            }
        });
    }
}
