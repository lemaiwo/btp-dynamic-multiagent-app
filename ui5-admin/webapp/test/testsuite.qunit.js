sap.ui.define(function () {
    "use strict";
    return {
        name: "Agent Administration",
        defaults: {
            page: "ui5://test-resources/com/infrabel/agentadmin/Test.qunit.html?testsuite={suite}&test={name}",
            qunit: { version: 2 },
            ui5: { theme: "sap_horizon", language: "EN" },
            loader: { paths: { "com/infrabel/agentadmin": "../" } }
        },
        tests: {
            "unit/AdminService": { title: "Unit: AdminService" },
            "unit/formatter": { title: "Unit: formatter" },
            "unit/validators": { title: "Unit: validators" }
        }
    };
});
