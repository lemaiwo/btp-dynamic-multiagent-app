import Opa5 from "sap/ui/test/Opa5";
import Common from "./pages/Common";

// Registered here, the one entry point every journey loads through (see
// ui5-admin/webapp/test/integration/opaTests.qunit.ts for why not in Common).
Opa5.extendConfig({
    arrangements: new Common(),
    actions: new Common(),
    assertions: new Common(),
    viewNamespace: "com.agent.ide.view.",
    autoWait: true
});

import "./StartupJourney";
import "./SessionJourney";
import "./ExplorerJourney";
import "./DiffJourney";
import "./ChatJourney";
import "./AssistantRunJourney";
import "./ActivityJourney";
import "./ConventionsJourney";
