import { ensureDiff } from "com/agent/ide/model/vendor";
import { newFileTab, showsLintedText, withFile, withView } from "com/agent/ide/model/editorTabs";
import type { FileDetail } from "com/agent/ide/service/types";

const LABELS = { left: "Source", right: "Proposed" };

function detail(origin: string, proposed: string | null): FileDetail {
    return { path: "src/CLAS/zcl_x.clas.abap", state: proposed ? "modified" : "read", origin_source: origin, proposed_source: proposed, lint: [] };
}

QUnit.module("editorTabs", {
    before: function () {
        return ensureDiff();
    }
});

QUnit.test("a file tab is named after the file and picks the editor type from the extension", function (assert) {
    const tab = newFileTab("src/CLAS/zcl_x.clas.abap");
    assert.strictEqual(tab.title, "zcl_x.clas.abap");
    assert.strictEqual(tab.editorType, "abap");
    assert.strictEqual(tab.isObject, true);
    const ddls = newFileTab("src/DDLS/zi_x.ddls.asddls");
    assert.strictEqual(ddls.editorType, "text", "only .abap gets ABAP highlighting");
    assert.strictEqual(newFileTab("notes/impact.md").isObject, false, "a scratch file cannot be linted or refreshed");
});

QUnit.test("a proposal opens on Proposed; a read file on Source with the proposal toggles off", function (assert) {
    const modified = withView(withFile(newFileTab("src/CLAS/zcl_x.clas.abap"), detail("a", "b")), LABELS);
    assert.strictEqual(modified.mode, "proposed");
    assert.strictEqual(modified.text, "b");
    assert.strictEqual(modified.hasProposal, true);
    const read = withView(withFile(newFileTab("src/CLAS/zcl_x.clas.abap"), detail("a", null)), LABELS);
    assert.strictEqual(read.mode, "source");
    assert.strictEqual(read.text, "a");
    assert.strictEqual(read.hasProposal, false);
});

QUnit.test("diff mode renders the diff and empties the editor text", function (assert) {
    const tab = withView({ ...withFile(newFileTab("src/CLAS/zcl_x.clas.abap"), detail("a\n", "a\nb\n")), mode: "diff" }, LABELS);
    assert.strictEqual(tab.text, "");
    assert.ok(tab.diffHtml.includes("ideDiffAdd"));
});

QUnit.test("a refresh keeps the chosen mode unless the proposal is gone", function (assert) {
    const tab = { ...withFile(newFileTab("src/CLAS/zcl_x.clas.abap"), detail("a", "b")), mode: "diff" as const };
    assert.strictEqual(withFile(tab, detail("a2", "b"), true).mode, "diff");
    assert.strictEqual(withFile(tab, detail("a2", null), true).mode, "source");
});

QUnit.test("lint markers show where the linted text is on screen", function (assert) {
    const proposal = withFile(newFileTab("src/CLAS/zcl_x.clas.abap"), detail("a", "b"));
    assert.ok(showsLintedText(proposal), "Proposed shows the linted proposal");
    assert.notOk(showsLintedText({ ...proposal, mode: "source" }), "Source is not what was linted");
    assert.notOk(showsLintedText({ ...proposal, mode: "diff" }));
    const read = withFile(newFileTab("src/CLAS/zcl_x.clas.abap"), detail("a", null));
    assert.ok(showsLintedText(read), "without a proposal the source was linted");
});
