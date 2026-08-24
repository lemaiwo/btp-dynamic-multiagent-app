import UIComponent from "sap/ui/core/UIComponent";
import Device from "sap/ui/Device";
import JSONModel from "sap/ui/model/json/JSONModel";
import AdminService from "./service/AdminService";
import ErrorHandler from "./service/ErrorHandler";

/**
 * @namespace com.infrabel.agentadmin
 */
export default class Component extends UIComponent {

    public static metadata = {
        manifest: "json",
        interfaces: ["sap.ui.core.IAsyncContentCreation"]
    };

    // Assigned in init(), which UIComponent always calls before any other
    // lifecycle method runs.
    private adminService!: AdminService;

    public init(): void {
        super.init();
        this.adminService = new AdminService();
        this.setModel(new JSONModel(Device), "device");

        // Routing is added in Task 8; guard so the app still boots until then.
        if (this.getManifestEntry("/sap.ui5/routing")) {
            this.getRouter().initialize();
        }
    }

    /** The single AdminService instance; reached via BaseController. */
    public getAdminService(): AdminService {
        return this.adminService;
    }

    /** The application's single error policy; reached via BaseController. */
    public getErrorHandler(): typeof ErrorHandler {
        return ErrorHandler;
    }
}
