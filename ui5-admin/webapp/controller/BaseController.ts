import Controller from "sap/ui/core/mvc/Controller";
import UIComponent from "sap/ui/core/UIComponent";
import Router from "sap/m/routing/Router";
import Model from "sap/ui/model/Model";
import type Component from "../Component";
import type AdminService from "../service/AdminService";
import ErrorHandler from "../service/ErrorHandler";

/**
 * Shared plumbing for every controller.
 *
 * Controllers reach the API through `getAdminService()` and never call `fetch`
 * themselves; `run()` is the standard way to await a call so that no controller
 * needs its own try/catch and no failure goes unreported.
 *
 * @namespace com.agent.admin.controller
 */
export default abstract class BaseController extends Controller {

    /**
     * How long a load may take before the indicator appears.
     *
     * A warm backend answers in well under this, so routine navigation stays
     * visually quiet; a cold one — the first call after the connection pool
     * has gone idle takes a second or more — shows the indicator promptly.
     */
    private static readonly BUSY_DELAY_MS = 200;

    /** Number of `withBusy` calls currently in flight on this controller. */
    private busyDepth = 0;

    public getOwnerComponentTyped(): Component {
        return this.getOwnerComponent() as Component;
    }

    public getAdminService(): AdminService {
        return this.getOwnerComponentTyped().getAdminService();
    }

    public getRouter(): Router {
        return (this.getOwnerComponent() as UIComponent).getRouter() as Router;
    }

    public getModel(name?: string): Model {
        // getView()/getModel() are typed as possibly undefined for callers
        // that run outside a view lifecycle; every BaseController subclass
        // calls this only from onInit() onward, once the view is set.
        return this.getView()!.getModel(name) as Model;
    }

    public setModel(model: Model, name?: string): void {
        this.getView()!.setModel(model, name);
    }

    /**
     * Awaits an API call, routing any failure to the central error handler.
     *
     * Resolves `undefined` on failure, so callers branch on the result rather
     * than wrapping every call in try/catch.
     */
    public async run<T>(work: Promise<T>, fallback = "The request failed."): Promise<T | undefined> {
        try {
            return await work;
        } catch (error) {
            ErrorHandler.handle(error, fallback);
            return undefined;
        }
    }

    /**
     * Awaits a call whose success carries no value, returning whether it worked.
     *
     * `run()` cannot express this: DELETE /agents/{id} and DELETE /skills/{id}
     * both return 204, so a *successful* call resolves `undefined` — exactly
     * what `run()` returns on failure. Branching on the value would silently
     * skip the toast and the list refresh on every successful delete.
     */
    public async runOk(work: Promise<unknown>, fallback = "The request failed."): Promise<boolean> {
        try {
            await work;
            return true;
        } catch (error) {
            ErrorHandler.handle(error, fallback);
            return false;
        }
    }

    /**
     * Marks the view busy for as long as `work` is running.
     *
     * Takes the whole load as a thunk rather than a single promise so that a
     * page fetching several things in sequence shows one indicator instead of
     * flickering between calls. Nested and overlapping calls are ref-counted,
     * so the indicator only clears once the last one has finished — including
     * when the work fails, which still propagates to the caller.
     */
    protected async withBusy<T>(work: () => Promise<T>): Promise<T> {
        const view = this.getView();
        if (this.busyDepth === 0 && view) {
            view.setBusyIndicatorDelay(BaseController.BUSY_DELAY_MS);
            view.setBusy(true);
        }
        this.busyDepth++;
        try {
            return await work();
        } finally {
            this.busyDepth--;
            if (this.busyDepth === 0 && view) {
                view.setBusy(false);
            }
        }
    }

    /**
     * Looks up a text from the `i18n` resource bundle.
     *
     * The component-scoped `getModel("i18n")` returns `Model | undefined`
     * (unlike this class's view-scoped `getModel()`), and `Model` itself has
     * no `getResourceBundle()` — only the concrete `ResourceModel` does. The
     * cast expresses just the shape used here without pulling in the
     * `ResourceModel` type.
     */
    protected text(key: string, args?: (string | number)[]): string {
        const model = this.getOwnerComponentTyped().getModel("i18n") as unknown as {
            getResourceBundle(): {
                getText(k: string, a?: (string | number)[]): string;
            };
        };
        return model.getResourceBundle().getText(key, args);
    }
}
