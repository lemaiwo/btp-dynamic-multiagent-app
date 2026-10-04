sap.ui.define(function () {
    "use strict";
    return {
        name: "ABAP Assistant",
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
            "unit/formatter": { title: "Unit: formatter" },
            "unit/errorText": { title: "Unit: errorText" },
            "unit/diffModel": { title: "Unit: diffModel" },
            "unit/sourceView": { title: "Unit: sourceView" },
            "unit/markdown": { title: "Unit: markdown" },
            "unit/docView": { title: "Unit: docView" },
            "unit/comments": { title: "Unit: comments" },
            "unit/docReview": { title: "Unit: docReview" },
            "unit/changesView": { title: "Unit: changesView" },
            "unit/adtLink": { title: "Unit: adtLink" },
            "unit/conventionsForm": { title: "Unit: conventionsForm" },
            "unit/worklist": { title: "Unit: worklist" },
            "unit/stickyScroll": { title: "Unit: stickyScroll" },
            "unit/conversation": { title: "Unit: conversation" },
            "unit/stageGate": { title: "Unit: stageGate" },
            "unit/chatRun": { title: "Unit: chatRun" },
            "unit/activity": { title: "Unit: activity" },
            "unit/runWatch": { title: "Unit: runWatch" },
            "unit/RunController": { title: "Unit: RunController" },
            "unit/approvals": { title: "Unit: approvals" },
            "unit/findings": { title: "Unit: findings" },
            "unit/FakeBackend": { title: "Unit: FakeBackend (approvals)" },
            "unit/BaseController": { title: "Unit: BaseController" },
            "unit/a11y": { title: "Unit: a11y (static)" },
            "unit/contract": { title: "Unit: contract (FakeBackend against the API schema)" },
            "unit/dialogForms": { title: "Unit: dialog forms" },
            "integration/opaTests": { title: "Integration: OPA5 journeys" }
        }
    };
});
