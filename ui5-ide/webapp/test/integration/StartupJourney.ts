import opaTest from "sap/ui/test/opaQunit";
import Opa5 from "sap/ui/test/Opa5";
import PropertyStrictEquals from "sap/ui/test/matchers/PropertyStrictEquals";
import type UI5Element from "sap/ui/core/Element";
import type Common from "./pages/Common";
import { OPTS as WOPTS } from "./pages/Worklist";

QUnit.module("Startup journey");

const APP = { viewName: "App" };

opaTest("the app starts on the worklist and is called ABAP Assistant", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp();

    Then.waitFor({
        id: "appTitle",
        ...APP,
        matchers: new PropertyStrictEquals({ name: "text", value: "ABAP Assistant" }),
        success: function () {
            Opa5.assert.ok(true, "the shell bar shows the app name");
        },
        errorMessage: "The shell bar does not say ABAP Assistant"
    });
    Then.waitFor({
        id: "worklistTable",
        ...WOPTS,
        success: function () {
            Opa5.assert.ok(true, "the empty hash opens the worklist");
        },
        errorMessage: "The empty hash did not open the worklist"
    });
    Then.waitFor({
        id: "conventionsButton",
        ...APP,
        success: function () {
            Opa5.assert.ok(true, "Conventions stays reachable from the shell bar");
        }
    });
    Then.waitFor({
        id: "toolHeader",
        ...APP,
        success: function (header: UI5Element) {
            const ids = (header as unknown as { getContent(): UI5Element[] }).getContent().map((c) => c.getId());
            Opa5.assert.notOk(ids.some((id) => id.endsWith("--sessionTitle")), "the shell bar has no session title");
            Opa5.assert.notOk(ids.some((id) => id.endsWith("--targetStatus")), "the shell bar has no target status");
        }
    });

    Then.iStopTheApp();
});

opaTest("a deep link opens the session page", function (Given: Common, When: Common, Then: Common) {
    Given.iStartTheApp("sessions/s-1");

    Then.waitFor({
        id: "sessionTitle",
        viewName: "Session",
        matchers: new PropertyStrictEquals({ name: "text", value: "Explain the order class" }),
        success: function () {
            Opa5.assert.ok(true, "the session page shows the linked session");
        },
        errorMessage: "The deep link did not open the session page"
    });
    Then.waitFor({
        id: "backToWorklist",
        viewName: "Session",
        success: function () {
            Opa5.assert.ok(true, "the session page links back to the worklist");
        }
    });

    Then.iStopTheApp();
});
