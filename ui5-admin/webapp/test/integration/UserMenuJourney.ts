import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import Press from "sap/ui/test/actions/Press";
import type MenuButton from "sap/m/MenuButton";
import type UI5Element from "sap/ui/core/Element";
import Common from "./pages/Common";

Opa5.extendConfig({ viewNamespace: "com.agent.admin.view.", autoWait: true });

QUnit.module("User menu journey");

// FakeBackend answers whoami with this label.
const USER = "tester@example.com";

opaTest("the header's user icon opens a menu naming the signed-in user", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    // The point of the menu is that the name is *not* on the header: assert
    // the header's own text first, or a regression that puts the label back
    // beside the icon would still pass the menu assertion below.
    Then.waitFor({
        id: "toolHeader",
        viewName: "App",
        success: function (element: UI5Element) {
            const rendered = element.getDomRef()?.textContent || "";
            Opa5.assert.strictEqual(
                rendered.indexOf(USER), -1,
                "the header itself does not show the user name"
            );
        }
    });

    When.waitFor({ id: "userMenu", viewName: "App", actions: new Press() });

    // sap.m.Menu renders through a popup outside the view's DOM, so the open
    // menu is polled for in the document rather than matched as a control.
    Then.waitFor({
        check: function () {
            const menu = document.querySelector(".sapUiMnu");
            return menu !== null && (menu.textContent || "").indexOf(USER) > -1;
        },
        success: function () {
            Opa5.assert.ok(true, "the open menu names the signed-in user");
        }
    });

    // Closed by hand: the menu's popup is not a Dialog, so iStopTheApp()'s
    // cleanup would not take it down before the next journey starts.
    When.waitFor({
        id: "userMenu",
        viewName: "App",
        success: function (element: UI5Element) {
            (element as MenuButton).getMenu().close();
        }
    });

    Then.iStopTheApp();
});
