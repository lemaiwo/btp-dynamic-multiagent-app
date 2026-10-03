import formatter from "com/agent/ide/model/formatter";
import ResourceModel from "sap/ui/model/resource/ResourceModel";

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
