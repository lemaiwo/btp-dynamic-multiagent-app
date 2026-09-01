import Opa5 from "sap/ui/test/Opa5";
import Common from "./pages/Common";

// Registered here, the one entry point every journey is guaranteed to load
// through, rather than as a side effect of pages/Common.ts itself: several
// journeys import Common only as a type annotation, and TypeScript elides an
// import that is never used as a value, so a self-registering side effect on
// that module would silently not run for those files.
Opa5.extendConfig({
    arrangements: new Common(),
    actions: new Common(),
    assertions: new Common()
});

import "./AgentJourney";
import "./SkillJourney";
import "./RunReportJourney";
import "./ReloadJourney";
import "./ImportJourney";
import "./SessionExpiredJourney";
import "./WorkflowJourney";
import "./WorkflowDetailJourney";
import "./WorkflowRunJourney";
