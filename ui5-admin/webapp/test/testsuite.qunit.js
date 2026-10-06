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
            "unit/odataCatalog": { title: "Unit: odataCatalog" },
            "unit/odataEntityDialog": { title: "Unit: odataCatalog, entity set dialog" },
            "unit/odataDestinations": { title: "Unit: odataDestinations" },
            "unit/odataImport": { title: "Unit: odataCatalog, import from $metadata" },
            "unit/oauthConfig": { title: "Unit: oauthConfig" },
            "unit/odataEntry": { title: "Unit: odataEntry, the agent's OData services entry" },
            "unit/importBundle": { title: "Unit: importBundle, what a configuration import opens" },
            "unit/ReportRenderer": { title: "Unit: ReportRenderer" },
            "unit/processFlowGraph": { title: "Unit: processFlowGraph" },
            "unit/workflowOrder": { title: "Unit: workflowOrder" },
            "unit/stepKinds": { title: "Unit: stepKinds" },
            "unit/textStats": { title: "Unit: textStats" },
            "unit/runsPanel": { title: "Unit: runsPanel" },
            "unit/autoRefresh": { title: "Unit: autoRefresh" },
            "unit/NullableKey": { title: "Unit: NullableKey binding type" },
            "integration/opaTests": { title: "Integration: OPA5 journeys" }
        }
    };
});
