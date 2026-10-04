import Controller from "sap/ui/core/mvc/Controller";
import UIComponent from "sap/ui/core/UIComponent";
import type Router from "sap/m/routing/Router";
import type Model from "sap/ui/model/Model";
import MessageBox from "sap/m/MessageBox";
import type Component from "../Component";
import { errorText } from "../model/errorText";

/**
 * Shared plumbing for every controller.
 *
 * @namespace com.agent.ide.controller
 */
export default abstract class BaseController extends Controller {

    public getOwnerComponentTyped(): Component {
        return this.getOwnerComponent() as Component;
    }

    public getRouter(): Router {
        return (this.getOwnerComponent() as UIComponent).getRouter() as Router;
    }

    public getModel(name?: string): Model {
        // Only called from onInit() onward, once the view is set.
        return this.getView()!.getModel(name) as Model;
    }

    public setModel(model: Model, name?: string): void {
        this.getView()!.setModel(model, name);
    }

    /** Looks up a text from the component's `i18n` resource bundle. */
    protected text(key: string, args?: (string | number)[]): string {
        const model = this.getOwnerComponentTyped().getModel("i18n") as unknown as {
            getResourceBundle(): { getText(k: string, a?: (string | number)[]): string };
        };
        // Formatted only with arguments: an empty list would run the text through MessageFormat,
        // which drops a single apostrophe ("target's" -> "targets").
        return model.getResourceBundle().getText(key, args?.length ? args : undefined);
    }

    /** Shows a failed call as a message box, worded by its status and code (model/errorText). */
    protected showError(e: unknown): void {
        MessageBox.error(errorText(e, (key, args) => this.text(key, args)));
    }
}
