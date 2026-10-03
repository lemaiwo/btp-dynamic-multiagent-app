sap.ui.define(function () {
    "use strict";
    return {
        name: "ABAP Workbench",
        defaults: {
            page: "ui5://test-resources/com/agent/ide/Test.qunit.html?testsuite={suite}&test={name}",
            qunit: { version: 2 },
            ui5: { theme: "sap_horizon", language: "EN" },
            loader: { paths: { "com/agent/ide": "../" } }
        },
        tests: {
            "unit/unitTests": { title: "Unit: scaffold" },
            "unit/SseParser": { title: "Unit: SseParser" },
            "unit/IdeService": { title: "Unit: IdeService" },
            "unit/workspaceTree": { title: "Unit: workspaceTree" },
            "unit/formatter": { title: "Unit: formatter" },
            "unit/errorText": { title: "Unit: errorText" },
            "unit/diffModel": { title: "Unit: diffModel" },
            "unit/lintAnnotations": { title: "Unit: lintAnnotations" },
            "unit/markdown": { title: "Unit: markdown" },
            "unit/editorTabs": { title: "Unit: editorTabs" },
            "unit/stageGate": { title: "Unit: stageGate" },
            "unit/chatRun": { title: "Unit: chatRun" },
            "unit/activity": { title: "Unit: activity" },
            "unit/runWatch": { title: "Unit: runWatch" },
            "unit/approvals": { title: "Unit: approvals" },
            "unit/findings": { title: "Unit: findings" },
            "unit/findingHighlight": { title: "Unit: findingHighlight" },
            "unit/FakeBackend": { title: "Unit: FakeBackend (approvals)" },
            "integration/opaTests": { title: "Integration: OPA5 journeys" }
        }
    };
});
