import Opa5 from "sap/ui/test/Opa5";
import Element from "sap/ui/core/Element";
import type Dialog from "sap/m/Dialog";
import FakeBackend, { type FailNext } from "../FakeBackend";

export const backend = new FakeBackend();

export default class Common extends Opa5 {

    /**
     * `failNext`, if given, is applied in the same queued step as
     * `backend.reset()` -- not set by the caller afterwards. reset()/install()
     * must run as a queued step, not inline here: every Given/When/Then call
     * in an opaTest body only *enqueues* work on OPA5's queue -- none of it
     * actually runs until the whole synchronous test function has returned.
     * A caller setting `backend.failNext = ...` on the next line, expecting
     * it to land after reset() but before the app's first fetch, would
     * instead have it cleared: reset() (queued here) now runs *after* that
     * synchronous assignment, not before it.
     */
    public iStartTheApp(hash = "", failNext?: FailNext): void {
        this.waitFor({
            success: function () {
                backend.reset();
                if (failNext) {
                    backend.failNext = failNext;
                }
                backend.install();
            }
        });
        this.iStartMyUIComponent({
            componentConfig: { name: "com.infrabel.agentadmin", async: true },
            hash
        });
    }

    public iStopTheApp(): void {
        // Defensive cleanup: some journeys deliberately leave a dialog open
        // to assert its state (an invalid MCP server url, an invalid import),
        // and a MessageBox popup is not part of the component tree at all, so
        // component teardown below closes neither. A dialog left open keeps
        // its modal block layer in the DOM, which would make every control
        // in the NEXT journey "not interactable" (OPA5's Interactable
        // matcher). destroy() (not close()) so no dialog's onClose handler
        // runs as a side effect of cleanup -- ErrorHandler's session-expired
        // dialog reloads the page from onClose.
        this.waitFor({
            success: function () {
                Element.registry.filter(function (element) {
                    return element.isA("sap.m.Dialog");
                }).forEach(function (element) {
                    (element as Dialog).destroy();
                });
            }
        });
        this.iTeardownMyUIComponent();
        // Queued (see iStartTheApp's comment) so it runs after teardown
        // actually completes, not synchronously alongside it.
        this.waitFor({
            success: function () {
                backend.restore();
            }
        });
    }
}

// Common is registered as the arrangements/actions/assertions instance in
// opaTests.qunit.ts, not here: several journeys use Common only as a type
// annotation (Given: Common, ...), and TypeScript elides an import that is
// never used as a value, so a self-registering side effect on this module
// would silently not run for those files. Registering once from the entry
// point that every journey is guaranteed to load through avoids that trap.
