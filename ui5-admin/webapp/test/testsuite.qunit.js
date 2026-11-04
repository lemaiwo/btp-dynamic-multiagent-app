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
            "unit/AdminServiceDataAccess": { title: "Unit: AdminService, InA queries and catalogue links" },
            "unit/BaseController": { title: "Unit: BaseController" },
            "unit/ErrorHandler": { title: "Unit: ErrorHandler" },
            "unit/formatter": { title: "Unit: formatter" },
            "unit/validators": { title: "Unit: validators" },
            "unit/builtins": { title: "Unit: builtins catalog" },
            "unit/odataCatalog": { title: "Unit: odataCatalog" },
            "unit/odataKind": { title: "Unit: odataCatalog, the service kind (generic)" },
            "unit/odataEntityDialog": { title: "Unit: odataCatalog, entity set dialog" },
            "unit/odataDestinations": { title: "Unit: odataDestinations" },
            "unit/odataImport": { title: "Unit: odataCatalog, import from $metadata" },
            "unit/oauthConfig": { title: "Unit: oauthConfig" },
            "unit/dataCatalog": { title: "Unit: dataCatalog, the data catalogue pages" },
            "unit/graph3d": { title: "Unit: graph3d, the 3D overview of the data catalogue graph" },
            "unit/embeddingsSettings": { title: "Unit: embeddingsSettings, the data catalogue embeddings of the Settings page" },
            "unit/inaQueries": { title: "Unit: inaQueries, the analytical queries pages" },
            "unit/odataEntry": { title: "Unit: odataEntry, the agent's OData services entry" },
            "unit/inaEntry": { title: "Unit: inaEntry, the agent's analytical queries entry" },
            "unit/bitbucketEntry": { title: "Unit: bitbucketEntry" },
            "unit/gitEntry": { title: "Unit: gitEntry" },
            "unit/invokeSettings": { title: "Unit: invokeSettings, the agent's invoke API panel" },
            "unit/importBundle": { title: "Unit: importBundle, what a configuration import opens" },
            "unit/ReportRenderer": { title: "Unit: ReportRenderer" },
            "unit/processFlowGraph": { title: "Unit: processFlowGraph" },
            "unit/workflowOrder": { title: "Unit: workflowOrder" },
            "unit/stepKinds": { title: "Unit: stepKinds" },
            "unit/textStats": { title: "Unit: textStats" },
            "unit/runsPanel": { title: "Unit: runsPanel" },
            "unit/autoRefresh": { title: "Unit: autoRefresh" },
            "unit/notifications": { title: "Unit: notifications" },
            "unit/NullableKey": { title: "Unit: NullableKey binding type" },
            "integration/opaTests": { title: "Integration: OPA5 journeys" }
        }
    };
});
