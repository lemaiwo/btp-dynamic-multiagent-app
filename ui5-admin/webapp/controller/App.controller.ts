import JSONModel from "sap/ui/model/json/JSONModel";
import BaseController from "./BaseController";
import type NavigationListItem from "sap/tnt/NavigationListItem";
import type { Router$RouteMatchedEvent, Router$BypassedEvent } from "sap/ui/core/routing/Router";
import type SideNavigation from "sap/tnt/SideNavigation";
import type { SideNavigation$ItemSelectEvent } from "sap/tnt/SideNavigation";

/** Which side-nav item is highlighted for each route. Detail routes keep
 *  their list's item selected. Anything unlisted (including the bypassed
 *  notFound target) selects nothing. */
const NAV_KEY_BY_ROUTE: Record<string, string> = {
    root: "agents",
    agents: "agents",
    agentDetail: "agents",
    skills: "skills",
    skillDetail: "skills",
    runs: "runs",
    runDetail: "runs",
    settings: "settings"
};

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
            // Left unselected: on a cold load straight to a bad hash, the
            // router's "bypassed" event can fire before this controller's
            // onInit attaches its listener (Component.init() calls
            // getRouter().initialize() before the App view/controller
            // exist), so that miss must default to "nothing highlighted"
            // rather than to a specific item. attachRouteMatched below
            // still fires in time for every real route, cold load included.
            selectedKey: "",
            userLabel: ""
        });
        this.setModel(model, "appView");

        this.getRouter().attachRouteMatched((event: Router$RouteMatchedEvent) => {
            const name = (event.getParameter("name") as string) || "";
            model.setProperty("/selectedKey", NAV_KEY_BY_ROUTE[name] ?? "");
        });

        // routeMatched never fires for the bypassed target, so notFound
        // would otherwise leave whichever nav item was selected before it.
        // sap.tnt.SideNavigation#setSelectedKey("") is a no-op for clearing
        // the rendered highlight (it only reacts to keys that match an
        // item), so the control has to be told directly via
        // setSelectedItem(""); the model property is kept in sync too so
        // any future binding-driven reads stay consistent.
        this.getRouter().attachBypassed((_event: Router$BypassedEvent) => {
            model.setProperty("/selectedKey", "");
            (this.byId("sideNavigation") as SideNavigation).setSelectedItem("");
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
