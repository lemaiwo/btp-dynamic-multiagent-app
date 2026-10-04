import ResourceModel from "sap/ui/model/resource/ResourceModel";

const bundle = new ResourceModel({ bundleName: "com.agent.ide.i18n.i18n" }).getResourceBundle() as unknown as {
    getText(key: string): string;
};

QUnit.module("missingRole text");

QUnit.test("names the role collection by its unchanged name", function (assert) {
    const text = bundle.getText("missingRole");
    assert.ok(text.includes("Ask your administrator for the \"ABAP IDE Developer\" role collection."),
        `names the "ABAP IDE Developer" role collection (${text})`);
    assert.notOk(text.includes("ABAP Assistant Developer"), "no role collection that does not exist");
});
