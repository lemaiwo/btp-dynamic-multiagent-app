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
import "./ConventionsJourney";
import "./WorklistJourney";
import "./SessionPageJourney";
import "./SessionPageStatesJourney";
import "./ConversationJourney";
import "./DocumentReviewJourney";
import "./DocumentReviewFixJourney";
import "./ChangesReviewJourney";
import "./ReviewFollowupsJourney";
import "./DiagnosePageJourney";
import "./ReviewFollowups2Journey";
import "./DiagnoseReviewFixJourney";
import "./ReviewFollowups3Journey";
import "./U12FixJourney";
import "./U12FixSessionJourney";
import "./RunLifecycleJourney";
import "./ReviewFollowups4Journey";
import "./KeyboardJourney";
import "./FinalFixJourney";
import "./FinalFix2Journey";
import "./FinalFix3Journey";
