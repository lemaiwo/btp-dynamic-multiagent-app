import { findingAdtHref, findingRow, pendingApprovalCount, upsertFinding } from "com/agent/ide/model/findings";
import type { DiagnoseFinding } from "com/agent/ide/service/types";

function finding(id: string, ref: string, patch: Partial<DiagnoseFinding> = {}): DiagnoseFinding {
    return {
        id, kind: "dump", ref_id: ref, title: `Dump ${ref}`, program: null, include: null, line: null,
        occurred_at: null, created_at: null, ...patch
    };
}

QUnit.module("findings: upsertFinding");

QUnit.test("a new finding goes to the front (newest first) and the old list is not mutated", function (assert) {
    const list = [finding("f-1", "A")];
    const next = upsertFinding(list, finding("f-2", "B"));
    assert.deepEqual(next.map((f) => f.id), ["f-2", "f-1"]);
    assert.deepEqual(list.map((f) => f.id), ["f-1"], "the input is untouched");
});

QUnit.test("a finding with a known id replaces its row in place", function (assert) {
    const list = [finding("f-2", "B"), finding("f-1", "A")];
    const next = upsertFinding(list, finding("f-1", "A", { title: "Dump A, again", line: 12 }));
    assert.deepEqual(next.map((f) => f.id), ["f-2", "f-1"]);
    assert.strictEqual(next[1].title, "Dump A, again");
    assert.strictEqual(next[1].line, 12);
});

QUnit.test("the same kind and ref_id under another id is the same finding (the server's unique key)", function (assert) {
    const list = [finding("f-1", "A")];
    const next = upsertFinding(list, finding("f-9", "A", { title: "Updated" }));
    assert.strictEqual(next.length, 1);
    assert.strictEqual(next[0].id, "f-9");
    assert.strictEqual(next[0].title, "Updated");
    const other = upsertFinding(list, finding("f-3", "A", { kind: "trace" }));
    assert.strictEqual(other.length, 2, "another kind with the same ref is another finding");
});

QUnit.test("a frame without an id or a kind is ignored", function (assert) {
    const list = [finding("f-1", "A")];
    assert.strictEqual(upsertFinding(list, null as unknown as DiagnoseFinding), list);
    assert.strictEqual(upsertFinding(list, { title: "x" } as DiagnoseFinding), list);
});

// --- Task U11: the findings list of the session page -----------------------------

/** A text lookup that shows the key and its arguments, so the tests see which text is used. */
const t = (key: string, args?: (string | number)[]): string => (args?.length ? `${key}(${args.join("|")})` : key);
const time = (iso: string): string => `T[${iso}]`;
/** A class pool program name: the class name padded to 30 with "=", then "CP". */
const pool = (name: string): string => `${name.padEnd(30, "=")}CP`;

QUnit.module("findings: findingAdtHref");

QUnit.test("a report or program opens at the finding's line", function (assert) {
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER_LIST", line: 88 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zorder_list/source/main#start=88,0");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER_LIST", include: "ZORDER_LIST", line: 3 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zorder_list/source/main#start=3,0", "the include that is the program itself");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER_LIST", line: null })),
        "adt://DEMO/sap/bc/adt/programs/programs/zorder_list/source/main", "no line: no fragment");
});

QUnit.test("an include of a program opens the include at the line (the line is the include's)", function (assert) {
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "SAPLZORDER", include: "LZORDERU01", line: 12 })),
        "adt://DEMO/sap/bc/adt/programs/includes/lzorderu01/source/main#start=12,0");
});

QUnit.test("a class pool opens the class; the line only when it is not in another include", function (assert) {
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: pool("ZCL_ORDER_QUERY"), line: 88 })),
        "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/source/main#start=88,0");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", {
        program: pool("ZCL_ORDER_QUERY"), include: "ZCL_ORDER_QUERY===============CM001", line: 7
    })), "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/source/main",
    "a method include's line is not a line of the class source: no fragment");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: pool("/NS/CL_X"), line: 2 })),
        "adt://DEMO/sap/bc/adt/oo/classes/%2fns%2fcl_x/source/main#start=2,0", "a namespaced class");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZCP", line: 2 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zcp/source/main#start=2,0", "a short name ending in CP is a program");
});

QUnit.test("fix round U11 #6: a class pool part is recognised as the server's class_include does", function (assert) {
    const cm = "ZCL_ORDER_QUERY".padEnd(30, "=") + "CM001";
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: cm, line: 7 })),
        "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/source/main",
        "a method include as the program: the class, no line (not a line of the class source)");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZCL_X=====CU", line: 3 })),
        "adt://DEMO/sap/bc/adt/oo/classes/zcl_x/source/main", "any =-padding before the suffix; CU: no line");
    const thirty = "ZCL_A_CLASS_NAME_OF_THIRTY_CHR";
    assert.strictEqual(thirty.length, 30);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: `${thirty}CP`, line: 4 })),
        `adt://DEMO/sap/bc/adt/oo/classes/${thirty.toLowerCase()}/source/main#start=4,0`,
        "a class name of exactly 30 has no padding: longer than 30 decides, CP keeps the line");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: `${thirty}CM00A`, line: 4 })),
        `adt://DEMO/sap/bc/adt/oo/classes/${thirty.toLowerCase()}/source/main`, "its method include: no line");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZREPORT_CP", line: 2 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zreport_cp/source/main#start=2,0", "ZREPORT_CP is a program");
});

QUnit.test("fix round U11 #6: a class-local include (CCDEF/CCIMP/CCMAC/CCAU) opens that include at the line", function (assert) {
    const part = (suffix: string): string => "ZCL_ORDER_QUERY".padEnd(30, "=") + suffix;
    const base = "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/includes/";
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: pool("ZCL_ORDER_QUERY"), include: part("CCIMP"), line: 9 })),
        `${base}implementations#start=9,0`);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: pool("ZCL_ORDER_QUERY"), include: part("CCDEF"), line: 2 })),
        `${base}definitions#start=2,0`);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: pool("ZCL_ORDER_QUERY"), include: part("CCMAC"), line: 1 })),
        `${base}macros#start=1,0`);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: part("CCAU"), line: 30 })),
        `${base}testclasses#start=30,0`, "also when the program itself is the section");
});

QUnit.test("no program, a name that is not an ABAP name or a bad target: no link", function (assert) {
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A")), null);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "Z<img src=x>", line: 1 })), null);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER", include: "L{x}U01", line: 1 })), null,
        "an include that does not validate gives no link (not the program at a line of another source)");
    assert.strictEqual(findingAdtHref("bad target!", finding("f", "A", { program: "ZORDER", line: 1 })), null);
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER", line: -3 })), null);
});

QUnit.test("follow-ups 3 (6-a): program and include are compared as the server does (trimmed, upper case)", function (assert) {
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER_LIST", include: "zorder_list ", line: 3 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zorder_list/source/main#start=3,0",
        "an include that is the program in another case or with blanks is the program, not an include");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: " zorder_list", line: 4 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zorder_list/source/main#start=4,0", "the program is trimmed");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "SAPLZORDER", include: " lzorderu01", line: 12 })),
        "adt://DEMO/sap/bc/adt/programs/includes/lzorderu01/source/main#start=12,0", "the include is trimmed");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "ZORDER", include: "   ", line: 5 })),
        "adt://DEMO/sap/bc/adt/programs/programs/zorder/source/main#start=5,0", "a blank include is no include");
});

QUnit.test("follow-ups 3 (6-b): a function pool frame (SAPL...) has no link, as the server opens none", function (assert) {
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "SAPLZORDER", line: 20 })), null, "no include");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "SAPLZORDER", include: "SAPLZORDER", line: 20 })), null,
        "the include is the pool itself");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "saplzorder", include: " SAPLZORDER", line: 20 })), null,
        "in any case, with blanks");
    assert.strictEqual(findingAdtHref("DEMO", finding("f", "A", { program: "SAPLZORDER", include: "LZORDERF01", line: 20 })),
        "adt://DEMO/sap/bc/adt/programs/includes/lzorderf01/source/main#start=20,0", "its includes still open");
});

QUnit.module("findings: findingRow");

QUnit.test("kind as text and icon, title as given, where and when", function (assert) {
    const row = findingRow(finding("f-1", "DUMP-1", {
        title: "TSV_TNEW_PAGE_ALLOC_FAILED {/session/title}", program: pool("ZCL_ORDER_QUERY"), line: 88,
        occurred_at: "2026-10-03T13:52:00", created_at: "2026-10-03T14:00:00"
    }), "DEMO", t, time);
    assert.strictEqual(row.id, "f-1");
    assert.strictEqual(row.kindText, "findingKindDump");
    assert.strictEqual(row.kindIcon, "sap-icon://error");
    assert.strictEqual(row.kindState, "Indication01", "a colour, not the Error value state (no \"invalid entry\")");
    assert.strictEqual(row.title, "TSV_TNEW_PAGE_ALLOC_FAILED {/session/title}", "the title is data, unchanged");
    assert.strictEqual(row.where, `${pool("ZCL_ORDER_QUERY")} · findingLine(88)`);
    assert.strictEqual(row.time, "T[2026-10-03T13:52:00]", "when it happened, not when it was stored");
    assert.ok(row.hasSource, "a program: the source can be opened");
    assert.ok(row.hasDetail, "a dump has a text");
    assert.strictEqual(row.adtHref, "adt://DEMO/sap/bc/adt/oo/classes/zcl_order_query/source/main#start=88,0");
});

QUnit.test("an include that differs from the program is shown; the time falls back to when it was stored", function (assert) {
    const row = findingRow(finding("f", "A", {
        kind: "trace", program: "SAPLZORDER", include: "LZORDERU01", line: 12, created_at: "2026-10-03T14:00:00"
    }), "DEMO", t, time);
    assert.strictEqual(row.where, "SAPLZORDER · LZORDERU01 · findingLine(12)");
    assert.strictEqual(row.time, "T[2026-10-03T14:00:00]");
    assert.strictEqual(row.kindText, "findingKindTrace");
    assert.strictEqual(row.kindState, "Indication03");
});

QUnit.test("no program: no source, no ADT link, the position says it is unknown; no time: empty", function (assert) {
    const row = findingRow(finding("f", "A", { kind: "auth_check" }), "DEMO", t, time);
    assert.notOk(row.hasSource);
    assert.strictEqual(row.adtHref, "");
    assert.strictEqual(row.where, "findingWhereUnknown");
    assert.strictEqual(row.time, "");
    assert.notOk(row.hasDetail, "an authorization check has no text to read");
    assert.strictEqual(row.kindIcon, "sap-icon://locked");
});

QUnit.test("an unknown kind still has a text, an icon and a state", function (assert) {
    const row = findingRow(finding("f", "A", { kind: "weird" as DiagnoseFinding["kind"] }), "DEMO", t, time);
    assert.strictEqual(row.kindText, "weird");
    assert.strictEqual(row.kindIcon, "sap-icon://inspection");
    assert.strictEqual(row.kindState, "None");
    assert.notOk(row.hasDetail);
});

QUnit.module("findings: pendingApprovalCount");

QUnit.test("counts the pending approvals only", function (assert) {
    assert.strictEqual(pendingApprovalCount([]), 0);
    assert.strictEqual(pendingApprovalCount([{ status: "pending" }, { status: "denied" }, { status: "pending" }, { status: "expired" }]), 2);
});
