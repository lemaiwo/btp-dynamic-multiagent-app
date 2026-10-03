import formatter from "com/agent/ide/model/formatter";
import ResourceModel from "sap/ui/model/resource/ResourceModel";
import DateFormat from "sap/ui/core/format/DateFormat";

// '.formatter.x' bindings run with the controller as `this`; a stand-in that
// answers getModel("i18n") with the app's bundle, as BaseController does, is enough.
const i18n = new ResourceModel({ bundleName: "com.agent.ide.i18n.i18n" });
const controller = { getModel: (name?: string) => (name === "i18n" ? i18n : undefined) };

QUnit.module("formatter");

QUnit.test("stateBadge maps a file state to a ValueState", function (assert) {
    assert.strictEqual(formatter.stateBadge("read"), "Information");
    assert.strictEqual(formatter.stateBadge("modified"), "Warning");
    assert.strictEqual(formatter.stateBadge("new"), "Success");
});

QUnit.test("stateBadge degrades to None for a folder or an unknown state", function (assert) {
    assert.strictEqual(formatter.stateBadge(null), "None");
    assert.strictEqual(formatter.stateBadge("bogus" as never), "None");
});

QUnit.test("stateText reads the badge text from i18n", function (assert) {
    assert.strictEqual(formatter.stateText.call(controller, "read"), "read");
    assert.strictEqual(formatter.stateText.call(controller, "modified"), "modified");
    assert.strictEqual(formatter.stateText.call(controller, "new"), "new");
});

QUnit.test("stateText is empty for a folder", function (assert) {
    assert.strictEqual(formatter.stateText.call(controller, null), "");
});

QUnit.test("stageText reads the stage name from i18n", function (assert) {
    assert.strictEqual(formatter.stageText.call(controller, "chat"), "Chat");
    assert.strictEqual(formatter.stageText.call(controller, "design"), "Design");
    assert.strictEqual(formatter.stageText.call(controller, "plan"), "Plan");
    assert.strictEqual(formatter.stageText.call(controller, "propose"), "Propose");
    assert.strictEqual(formatter.stageText.call(controller, "review"), "Review");
    assert.strictEqual(formatter.stageText.call(controller, "done"), "Done");
});

QUnit.test("an unknown stage falls back to its raw value; no bundle falls back too", function (assert) {
    assert.strictEqual(formatter.stageText.call(controller, "bogus" as never), "bogus");
    assert.strictEqual(formatter.stageText.call(undefined, "chat"), "chat");
    assert.strictEqual(formatter.stageText.call(controller, null), "");
});

QUnit.test("stageState: done is Success, everything else Information", function (assert) {
    assert.strictEqual(formatter.stageState("done"), "Success");
    assert.strictEqual(formatter.stageState("chat"), "Information");
});

QUnit.module("formatter: diagnose findings");

QUnit.test("formatFindingWhere joins program, include and line; missing parts are left out", function (assert) {
    assert.strictEqual(formatter.formatFindingWhere.call(controller, "ZCL_ORDER=====CP", "ZCL_ORDER=====CM003", 42),
        "ZCL_ORDER=====CP · ZCL_ORDER=====CM003 · line 42");
    assert.strictEqual(formatter.formatFindingWhere.call(controller, "ZREPORT", null, 7), "ZREPORT · line 7");
    assert.strictEqual(formatter.formatFindingWhere.call(controller, "ZREPORT", "ZREPORT", null), "ZREPORT",
        "an include equal to the program is not repeated");
    assert.strictEqual(formatter.formatFindingWhere.call(controller, null, null, 0), "line 0", "line 0 is a line");
});

QUnit.test("formatFindingWhere appends the time; an unparseable time stays as it is", function (assert) {
    const iso = "2026-10-03T08:15:00Z";
    const time = DateFormat.getDateTimeInstance({ style: "short" }).format(new Date(iso));
    assert.strictEqual(formatter.formatFindingWhere.call(controller, "ZREPORT", null, 7, iso), `ZREPORT · line 7 · ${time}`);
    assert.strictEqual(formatter.formatFindingWhere.call(controller, null, null, null, "yesterday-ish"), "yesterday-ish");
});

QUnit.test("formatFindingWhere says so when there is no source position at all", function (assert) {
    assert.strictEqual(formatter.formatFindingWhere.call(controller, null, null, null, null), "No source position");
    assert.strictEqual(formatter.formatFindingWhere.call(controller, undefined, "", undefined), "No source position");
});

QUnit.test("findingKindText reads the kind's name from i18n", function (assert) {
    assert.strictEqual(formatter.findingKindText.call(controller, "dump"), "Dump");
    assert.strictEqual(formatter.findingKindText.call(controller, "trace"), "Trace");
    assert.strictEqual(formatter.findingKindText.call(controller, "gateway_error"), "Gateway error");
    assert.strictEqual(formatter.findingKindText.call(controller, "auth_check"), "Authorization check");
    assert.strictEqual(formatter.findingKindText.call(controller, "odata_call"), "OData call");
    assert.strictEqual(formatter.findingKindText.call(controller, "bogus" as never), "bogus");
    assert.strictEqual(formatter.findingKindText.call(controller, null), "");
});

QUnit.test("findingIcon: one icon per kind, a neutral one for an unknown kind", function (assert) {
    assert.strictEqual(formatter.findingIcon("dump"), "sap-icon://error");
    assert.strictEqual(formatter.findingIcon("trace"), "sap-icon://performance");
    assert.strictEqual(formatter.findingIcon("auth_check"), "sap-icon://locked");
    assert.strictEqual(formatter.findingIcon("gateway_error"), "sap-icon://chain-link");
    assert.strictEqual(formatter.findingIcon("odata_call"), "sap-icon://cloud");
    assert.strictEqual(formatter.findingIcon("bogus" as never), "sap-icon://inspection");
    assert.strictEqual(formatter.findingIcon(null), "sap-icon://inspection");
});

QUnit.test("findingHasDetail: only kinds whose text SAP can be asked for", function (assert) {
    assert.strictEqual(formatter.findingHasDetail("dump"), true);
    assert.strictEqual(formatter.findingHasDetail("trace"), true);
    assert.strictEqual(formatter.findingHasDetail("gateway_error"), true);
    assert.strictEqual(formatter.findingHasDetail("auth_check"), false);
    assert.strictEqual(formatter.findingHasDetail("odata_call"), false);
    assert.strictEqual(formatter.findingHasDetail(null), false);
});
