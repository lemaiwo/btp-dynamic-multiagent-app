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
 * @namespace com.infrabel.agentadmin.controller
 */
export default abstract class BaseController extends Controller {

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
}
