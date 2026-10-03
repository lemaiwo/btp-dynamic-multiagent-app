import Component from "com/agent/ide/Component";

QUnit.module("Scaffold");

QUnit.test("the component class loads under the app namespace", function (assert) {
    assert.strictEqual(typeof Component, "function", "Component is a class");
    assert.strictEqual(
        Component.getMetadata().getName(), "com.agent.ide.Component",
        "the @namespace annotation produced the expected class name"
    );
});
