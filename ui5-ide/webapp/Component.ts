import UIComponent from "sap/ui/core/UIComponent";
import Device from "sap/ui/Device";
import JSONModel from "sap/ui/model/json/JSONModel";

/**
 * @namespace com.agent.ide
 */
export default class Component extends UIComponent {

    public static metadata = {
        manifest: "json",
        interfaces: ["sap.ui.core.IAsyncContentCreation"]
    };

    public init(): void {
        super.init();
        this.setModel(new JSONModel(Device), "device");

        // Guarded so a manifest without a routing section (a stripped-down
        // test host) still boots the component.
        if (this.getManifestEntry("/sap.ui5/routing")) {
            this.getRouter().initialize();
        }
    }
}
