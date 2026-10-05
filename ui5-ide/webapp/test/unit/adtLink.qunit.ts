import { adtUri, classIncludeOf } from "com/agent/ide/model/adtLink";

QUnit.module("adtLink");

QUnit.test("each supported type maps to its ADT path, name lower-cased", function (assert) {
    const cases: [string, string, string][] = [
        ["CLAS", "ZCL_DEMO", "oo/classes/zcl_demo"],
        ["INTF", "ZIF_DEMO", "oo/interfaces/zif_demo"],
        ["PROG", "ZDEMO_REPORT", "programs/programs/zdemo_report"],
        ["INCL", "ZDEMO_TOP", "programs/includes/zdemo_top"],
        ["DDLS", "ZI_DEMO", "ddic/ddl/sources/zi_demo"],
        ["DCLS", "ZI_DEMO_DCL", "acm/dcl/sources/zi_demo_dcl"],
        ["DDLX", "ZC_DEMO_MDE", "ddic/ddlx/sources/zc_demo_mde"],
        ["BDEF", "ZI_DEMO", "bo/behaviordefinitions/zi_demo"],
        ["SRVD", "ZUI_DEMO", "ddic/srvd/sources/zui_demo"]
    ];
    cases.forEach(([type, name, path]) => {
        assert.strictEqual(adtUri("DEMO", type, name), `adt://DEMO/sap/bc/adt/${path}/source/main`, type);
    });
});

QUnit.test("a line adds the start fragment", function (assert) {
    assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", 42), "adt://DEMO/sap/bc/adt/oo/classes/zcl_demo/source/main#start=42,0");
    assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", null), "adt://DEMO/sap/bc/adt/oo/classes/zcl_demo/source/main");
});

QUnit.test("a namespaced name encodes its slashes as %2f", function (assert) {
    assert.strictEqual(adtUri("DEMO", "CLAS", "/ABC/CL_X"), "adt://DEMO/sap/bc/adt/oo/classes/%2fabc%2fcl_x/source/main");
});

QUnit.test("FUNC and unknown types have no link", function (assert) {
    ["FUNC", "FUGR", "TABL", "", "__proto__", "constructor", "toString"].forEach((type) => {
        assert.strictEqual(adtUri("DEMO", type, "ZX"), null, JSON.stringify(type));
    });
});

QUnit.test("invalid names are refused", function (assert) {
    [
        "", "ZCL X", "zcl_x/../../y", "../zcl_x", "ZCL_X?x=1", "ZCL_X#y", "ZCL%2FX", "/ABC/", "//CL_X", "/ABC/CL/X",
        "ZCL_X\n", "javascript:alert(1)", "Z".repeat(61), "/ABC/CL_X/"
    ].forEach((name) => {
        assert.strictEqual(adtUri("DEMO", "CLAS", name), null, JSON.stringify(name));
    });
});

QUnit.test("invalid targets are refused", function (assert) {
    ["", "DE MO", "DEMO/x", "DEMO@evil", "DEMO:1", "DEMO?x", "DEMO#x", "a".repeat(65), "DEMO\u0000"].forEach((target) => {
        assert.strictEqual(adtUri(target, "CLAS", "ZCL_DEMO"), null, JSON.stringify(target));
    });
    assert.strictEqual(adtUri("DEMO-1.x", "CLAS", "ZCL_DEMO"), "adt://DEMO-1.x/sap/bc/adt/oo/classes/zcl_demo/source/main",
        "the server's target pattern is accepted");
});

QUnit.test("invalid lines are refused rather than dropped silently", function (assert) {
    [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY].forEach((line) => {
        assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", line), null, String(line));
    });
});

QUnit.test("non-string input is refused", function (assert) {
    assert.strictEqual(adtUri(null as unknown as string, "CLAS", "ZCL_DEMO"), null);
    assert.strictEqual(adtUri("DEMO", undefined as unknown as string, "ZCL_DEMO"), null);
    assert.strictEqual(adtUri("DEMO", "CLAS", 5 as unknown as string), null);
});

QUnit.test("only the adt: scheme is ever emitted", function (assert) {
    const uri = adtUri("DEMO", "PROG", "ZDEMO");
    assert.ok(uri?.startsWith("adt://"));
});

QUnit.test("targets follow the server rule: first character a letter or digit, at most 64", function (assert) {
    ["..", ".x", "-x", "_x", ".", "-", "a".repeat(65)].forEach((target) => {
        assert.strictEqual(adtUri(target, "CLAS", "ZCL_DEMO"), null, JSON.stringify(target));
    });
    ["a".repeat(64), "1DEMO", "D", "x.-_y"].forEach((target) => {
        assert.ok(adtUri(target, "CLAS", "ZCL_DEMO")?.startsWith(`adt://${target}/`), JSON.stringify(target));
    });
});

QUnit.test("a class include links to its own ADT include URI, with the line of that include", function (assert) {
    const cases: [string, string][] = [
        ["testclasses", "testclasses"],
        ["locals_def", "definitions"],
        ["locals_imp", "implementations"],
        ["macros", "macros"]
    ];
    cases.forEach(([include, adt]) => {
        assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", 7, include),
            `adt://DEMO/sap/bc/adt/oo/classes/zcl_demo/includes/${adt}#start=7,0`, include);
        assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", null, include),
            `adt://DEMO/sap/bc/adt/oo/classes/zcl_demo/includes/${adt}`, `${include} without a line`);
    });
    assert.strictEqual(adtUri("DEMO", "CLAS", "/ABC/CL_X", 3, "testclasses"),
        "adt://DEMO/sap/bc/adt/oo/classes/%2fabc%2fcl_x/includes/testclasses#start=3,0", "namespaced");
});

QUnit.test("an include ADT cannot express gives no link; no include is the main source", function (assert) {
    ["main", "foo", "testclasses/../x", "__proto__", "TESTCLASSES", "locals"].forEach((include) => {
        assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", 1, include), null, JSON.stringify(include));
    });
    assert.strictEqual(adtUri("DEMO", "PROG", "ZDEMO", 1, "testclasses"), null, "only classes have includes");
    assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", 1, null), "adt://DEMO/sap/bc/adt/oo/classes/zcl_demo/source/main#start=1,0");
    assert.strictEqual(adtUri("DEMO", "CLAS", "ZCL_DEMO", 1, undefined), "adt://DEMO/sap/bc/adt/oo/classes/zcl_demo/source/main#start=1,0");
});

QUnit.test("classIncludeOf reads the include from an abapGit class path", function (assert) {
    assert.strictEqual(classIncludeOf("src/CLAS/zcl_x.clas.abap"), null, "the main source");
    assert.strictEqual(classIncludeOf("src/CLAS/zcl_x.clas.testclasses.abap"), "testclasses");
    assert.strictEqual(classIncludeOf("src/CLAS/zcl_x.clas.locals_def.abap"), "locals_def");
    assert.strictEqual(classIncludeOf("src/CLAS/zcl_x.clas.locals_imp.abap"), "locals_imp");
    assert.strictEqual(classIncludeOf("src/CLAS/zcl_x.clas.macros.abap"), "macros");
    assert.strictEqual(classIncludeOf("src/CLAS/#abc#cl_x.clas.testclasses.abap"), "testclasses", "namespaced file name");
    assert.strictEqual(classIncludeOf("src/PROG/zx.prog.abap"), null, "not a class");
    assert.strictEqual(classIncludeOf("notes/plan.md"), null, "a note");
});
