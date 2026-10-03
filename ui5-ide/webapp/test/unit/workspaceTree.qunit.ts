import { buildTree, OBJECT_TYPES } from "com/agent/ide/model/workspaceTree";
import type { FileSummary } from "com/agent/ide/service/types";

function file(path: string, state: FileSummary["state"] = "read", type: string | null = null, name: string | null = null): FileSummary {
    return { path, state, object_type: type, object_name: name };
}

QUnit.module("workspaceTree");

QUnit.test("an empty workspace is an empty tree", function (assert) {
    assert.deepEqual(buildTree([]), []);
});

QUnit.test("an abapGit path becomes src > CLAS > leaf", function (assert) {
    const tree = buildTree([file("src/CLAS/zcl_demo.clas.abap", "read", "CLAS", "ZCL_DEMO")]);
    assert.strictEqual(tree.length, 1, "one root");
    const src = tree[0];
    assert.strictEqual(src.text, "src");
    assert.strictEqual(src.path, "src");
    assert.strictEqual(src.folder, true, "src is a folder");
    const clas = src.nodes[0];
    assert.strictEqual(clas.text, "CLAS");
    assert.strictEqual(clas.path, "src/CLAS");
    assert.strictEqual(clas.folder, true);
    const leaf = clas.nodes[0];
    assert.strictEqual(leaf.text, "zcl_demo.clas.abap");
    assert.strictEqual(leaf.path, "src/CLAS/zcl_demo.clas.abap");
    assert.strictEqual(leaf.folder, false);
    assert.strictEqual(leaf.state, "read");
    assert.strictEqual(leaf.objectType, "CLAS");
    assert.strictEqual(leaf.objectName, "ZCL_DEMO");
    assert.deepEqual(leaf.nodes, [], "a leaf has no children");
});

QUnit.test("files of one type share a folder; folders come before files, both sorted", function (assert) {
    const tree = buildTree([
        file("src/DDLS/zi_demo.ddls.asddls", "new", "DDLS", "ZI_DEMO"),
        file("notes/impact.md", "modified"),
        file("src/CLAS/zcl_b.clas.abap", "modified", "CLAS", "ZCL_B"),
        file("src/CLAS/zcl_a.clas.abap", "read", "CLAS", "ZCL_A"),
        file("readme.md", "new")
    ]);
    assert.deepEqual(tree.map((n) => n.text), ["notes", "src", "readme.md"], "folders first, then files");
    const src = tree[1];
    assert.deepEqual(src.nodes.map((n) => n.text), ["CLAS", "DDLS"]);
    assert.deepEqual(src.nodes[0].nodes.map((n) => n.text), ["zcl_a.clas.abap", "zcl_b.clas.abap"]);
    assert.strictEqual(src.nodes[0].nodes[1].state, "modified");
    assert.strictEqual(tree[0].nodes[0].objectType, null, "a scratch file has no object type");
});

QUnit.test("folders carry no state", function (assert) {
    const tree = buildTree([file("src/CLAS/zcl_a.clas.abap", "new")]);
    assert.strictEqual(tree[0].state, null);
    assert.strictEqual(tree[0].nodes[0].state, null);
});

QUnit.test("the object types offered match the workspace path contract", function (assert) {
    assert.deepEqual(OBJECT_TYPES, ["CLAS", "INTF", "PROG", "DDLS", "BDEF", "DCLS", "DDLX", "SRVD", "FUNC"]);
});
