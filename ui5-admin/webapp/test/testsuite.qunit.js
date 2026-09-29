sap.ui.define(function () {
    "use strict";
    return {
        name: "Agent Administration",
        defaults: {
            page: "ui5://test-resources/com/agent/admin/Test.qunit.html?testsuite={suite}&test={name}",
            qunit: { version: 2 },
            ui5: { theme: "sap_horizon", language: "EN" },
            loader: { paths: { "com/agent/admin": "../" } }
        },
        tests: {
            "unit/AdminService": { title: "Unit: AdminService" },
            "unit/BaseController": { title: "Unit: BaseController" },
            "unit/ErrorHandler": { title: "Unit: ErrorHandler" },
            "unit/formatter": { title: "Unit: formatter" },
            "unit/validators": { title: "Unit: validators" },
            "unit/builtins": { title: "Unit: builtins catalog" },
            "unit/ReportRenderer": { title: "Unit: ReportRenderer" },
            "unit/processFlowGraph": { title: "Unit: processFlowGraph" },
            "unit/workflowOrder": { title: "Unit: workflowOrder" },
            "integration/opaTests": { title: "Integration: OPA5 journeys" }
        }
    };
});
