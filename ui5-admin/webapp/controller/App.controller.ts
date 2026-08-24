import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import type NavigationListItem from "sap/tnt/NavigationListItem";
import type { Router$RouteMatchedEvent } from "sap/ui/core/routing/Router";
import type { SideNavigation$ItemSelectEvent } from "sap/tnt/SideNavigation";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class App extends BaseController {

    public onInit(): void {
        const model = new JSONModel({
            sideExpanded: true,
            // Inside a Work Zone / launchpad shell the host already renders a
            // header; showing ours too would stack two title bars.
            showHeader: !App.isInShell(),
            selectedKey: "agents",
            userLabel: ""
        });
        this.setModel(model, "appView");

        this.getRouter().attachRouteMatched((event: Router$RouteMatchedEvent) => {
            const name = event.getParameter("name") || "";
            const key = name.replace(/Detail$/, "");
            model.setProperty("/selectedKey", key === "root" ? "agents" : key);
        });

        void this.loadUser();
    }

    private static isInShell(): boolean {
        return typeof (window as { sap?: { ushell?: unknown } }).sap?.ushell !== "undefined";
    }

    private async loadUser(): Promise<void> {
        const who = await this.run(
            this.getAdminService().whoami(),
            "Could not read the signed-in user."
        );
        if (who) {
            (this.getModel("appView") as JSONModel).setProperty("/userLabel", who.label);
        }
    }

    public onToggleSideNav(): void {
        const model = this.getModel("appView") as JSONModel;
        model.setProperty("/sideExpanded", !model.getProperty("/sideExpanded"));
    }

    public onNavItemSelect(event: SideNavigation$ItemSelectEvent): void {
        const item = event.getParameter("item") as NavigationListItem;
        this.getRouter().navTo(item.getKey());
    }
}
