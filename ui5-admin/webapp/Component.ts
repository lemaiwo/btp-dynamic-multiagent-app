import UIComponent from "sap/ui/core/UIComponent";
import Device from "sap/ui/Device";
import JSONModel from "sap/ui/model/json/JSONModel";
import type ResourceModel from "sap/ui/model/resource/ResourceModel";
import type ResourceBundle from "sap/base/i18n/ResourceBundle";
import AdminService from "./service/AdminService";
import ErrorHandler from "./service/ErrorHandler";

/**
 * @namespace com.agent.admin
 */
export default class Component extends UIComponent {

    public static metadata = {
        manifest: "json",
        interfaces: ["sap.ui.core.IAsyncContentCreation"]
    };

    // Assigned in init(), which UIComponent always calls before any other
    // lifecycle method runs.
    private adminService!: AdminService;

    /** Asked before the app leaves the page that set it; see `canLeave`. */
    private leaveGuard?: () => Promise<boolean>;

    public init(): void {
        super.init();
        this.adminService = new AdminService();
        this.setModel(new JSONModel(Device), "device");

        // The error dialogs speak from the same bundle as the views; until it
        // is there (or without one, in a test host) they use English texts.
        const i18n = this.getModel("i18n") as ResourceModel | undefined;
        if (i18n) {
            void Promise.resolve(i18n.getResourceBundle()).then((bundle: ResourceBundle) => {
                ErrorHandler.useBundle(bundle);
            });
        }

        // Guarded so a manifest without a routing section (a stripped-down
        // test host) still boots the component.
        if (this.getManifestEntry("/sap.ui5/routing")) {
            this.getRouter().initialize();
        }
    }

    /** The single AdminService instance; reached via BaseController. */
    public getAdminService(): AdminService {
        return this.adminService;
    }

    /**
     * Lets the page that is shown have a say before the app navigates away
     * from it: a form with unsaved changes sets a guard that asks the user.
     * The page clears it (`undefined`) when it is no longer shown.
     */
    public setLeaveGuard(guard?: () => Promise<boolean>): void {
        this.leaveGuard = guard;
    }

    /** Whether the app may leave the current page. Without a guard: yes. */
    public canLeave(): Promise<boolean> {
        return this.leaveGuard ? this.leaveGuard() : Promise.resolve(true);
    }

    /** The application's single error policy; reached via BaseController. */
    public getErrorHandler(): typeof ErrorHandler {
        return ErrorHandler;
    }
}
