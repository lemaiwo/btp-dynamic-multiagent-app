import { test, expect } from "@playwright/test";
import {
    approve, byId, collectErrors, createSession, ensureTarget, field, getSession, insertArtifact, openApp, quote, saveCommentAndExpectClosed, sql, uuid
} from "./support/backend";

// The changes view on the session page, against the real backend and without
// any page.route: the stacked diff of a proposed class, next / previous
// change, a comment on a line of the revision, the "Open in ADT" href.
//
// Real backend: the session, the stages design -> plan -> propose (approve
// route), session detail, GET /file (+ revision), /file/revisions, the
// comment POST and list, approve refused while a comment is open.
// Written into SQLite (no model and no ARC-1 locally): the design and plan
// documents, the workspace file with the SAP source and its two proposal
// revisions, as a propose run with its base and syntax checks would leave them.
const TARGET = "DEMO_CHANGES";
const FILE = "src/CLAS/zcl_demo_fix.clas.abap";
const lines = (n: number) => Array.from({ length: n }, (_, i) => `    " step ${i + 1}`);
const BODY = lines(14);
const ORIGIN = ["CLASS zcl_demo_fix IMPLEMENTATION.", "  METHOD run.", "    rv = abap_false.", ...BODY,
    "    DATA(items) = get_items( ).", "  ENDMETHOD.", "ENDCLASS."].join("\n") + "\n";
const REV1 = ORIGIN.replace("rv = abap_false.", "rv = abap_true.");
// Revision 2: the first hunk changes line 3, the second adds a line after the loop (two change stops).
const REV2 = ["CLASS zcl_demo_fix IMPLEMENTATION.", "  METHOD run.", "    rv = abap_true.", ...BODY,
    "    DATA(items) = get_items( ).", "    \" <script>alert(1)</script>", "    SORT items BY id.", "  ENDMETHOD.", "ENDCLASS."].join("\n") + "\n";

function insertRevision(sid: string, revision: number, source: string, syntax: string | null): void {
    sql(`INSERT INTO ide_file_revisions (id, session_id, path, revision, proposed_source, syntax_status, syntax_json)
         VALUES (${quote(uuid())}, ${quote(sid)}, ${quote(FILE)}, ${revision}, ${quote(source)},
                 ${syntax ? quote(syntax) : "NULL"}, ${syntax ? quote("[]") : "NULL"})`);
}

test("changes view: diff, next/previous change, a line comment and the ADT link on the real backend", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await ensureTarget(request, TARGET);
    const title = `Changes ${Date.now()}`;
    const sid = (await createSession(request, title, TARGET)).id;
    await approve(request, sid);
    insertArtifact(sid, "design", "design", 1, "# Design\n\nReturn true and sort the items.");
    await approve(request, sid);
    insertArtifact(sid, "plan", "plan", 1, "# Plan\n\n1. Return true.\n2. Sort the items.", { design: 1 });
    expect((await approve(request, sid)).stage).toBe("propose");
    sql(`INSERT INTO ide_workspace_files (id, session_id, path, object_type, object_name, origin_source, proposed_source,
             state, revision, origin_version, base_status)
         VALUES (${quote(uuid())}, ${quote(sid)}, ${quote(FILE)}, 'CLAS', 'ZCL_DEMO_FIX', ${quote(ORIGIN)}, ${quote(REV2)},
                 'modified', 2, '00042', 'sap')`);
    insertRevision(sid, 1, REV1, "ok");
    insertRevision(sid, 2, REV2, "ok");
    expect((await getSession(request, sid)).waiting).toBe("changes");

    const errors = collectErrors(page);
    await openApp(page, `sessions/${sid}?view=changes`);
    await expect(byId(page, "sessionTitle")).toHaveText(title);
    const view = byId(page, "changesView");
    await expect(byId(page, "changesSummary")).toHaveText("1 object, +3 −1 lines");
    const card = view.locator(".ideChangesObject").first();
    await expect(card).toContainText("ZCL_DEMO_FIX");
    await expect(card).toContainText("Changed vs SAP version 00042");
    await expect(card).toContainText("No syntax messages");
    const revision = card.locator("div.sapMSlt[data-sap-ui*='--changesRevision-']");
    await expect(revision.locator(".sapMSltLabel")).toHaveText("Revision 2 (latest)");

    // The diff: removed and added lines as <del>/<ins> with −/+ markers; source text is escaped.
    const diff = card.locator(".ideUnified");
    await expect(diff.locator("tr.ideDiffDel")).toHaveCount(1);
    await expect(diff.locator("tr.ideDiffAdd")).toHaveCount(3);
    await expect(diff.locator("tr.ideDiffDel del")).toHaveText("    rv = abap_false.");
    await expect(diff.locator("tr.ideDiffAdd ins").nth(1)).toHaveText("    \" <script>alert(1)</script>");
    expect(await diff.locator("script").count()).toBe(0);
    await page.screenshot({ path: "test-results/e2e-changes.png", fullPage: true });

    // Next / previous change move the focus between the two hunks.
    const focusedStop = () => page.evaluate(() => document.activeElement?.getAttribute("data-stop") ?? null);
    await byId(page, "changesNext").click();
    await expect.poll(focusedStop).toBe("0");
    await byId(page, "changesNext").click();
    await expect.poll(focusedStop).toBe("1");
    await byId(page, "changesPrevious").click();
    await expect.poll(focusedStop).toBe("0");

    // Select the added SORT line (line 20 of revision 2) and comment on it.
    const sortRow = diff.locator("tr[data-side='new'][data-line='20']");
    await expect(sortRow).toContainText("SORT items BY id.");
    await sortRow.locator(".ideDiffCode").click();
    const commentButton = card.locator("button[data-sap-ui*='--changesCommentButton-']");
    await expect(commentButton).toHaveText("Comment on line 20");
    await commentButton.click();
    const popover = byId(page, "commentPopover");
    await expect(popover).toBeVisible();
    await expect(popover).toContainText("Comments on line 20");
    const body = "Sort by the item number, not the database id.";
    await field(page, "commentDraft").fill(body);
    await saveCommentAndExpectClosed(page);
    await expect(card.locator(".ideDocComments")).toContainText(body);
    await expect(card.locator(".ideDocComments")).toContainText("zcl_demo_fix.clas.abap, revision 2, line 20");

    const comments = await (await request.get(`/backend/sessions/${sid}/comments`)).json() as Record<string, unknown>[];
    expect(comments).toHaveLength(1);
    expect(comments[0]).toMatchObject({ anchor: "file", path: FILE, revision: 2, line_start: 20, line_end: 20, state: "open", body });
    await expect(byId(page, "requestChangesButton")).toHaveText("Request changes (1)");
    // An open comment blocks approve on the server as well.
    const refused = await request.post(`/backend/sessions/${sid}/approve`, { data: {} });
    expect(refused.status()).toBe(409);
    expect((await refused.json() as { code: string }).code).toBe("open_comments");

    // Open in ADT: the class in the session's target, as model/adtLink builds it.
    const adt = card.locator("a[href^='adt://']");
    await expect(adt).toHaveText("Open in ADT");
    expect(await adt.getAttribute("href")).toMatch(/^adt:\/\/DEMO_CHANGES\/sap\/bc\/adt\/oo\/classes\/zcl_demo_fix\/source\/main(#start=\d+,0)?$/);
    await page.screenshot({ path: "test-results/e2e-changes-comment.png", fullPage: true });

    // An older revision is a different diff (one change: line 3 only).
    await revision.click();
    await page.locator(".sapMSelectList li[data-sap-ui]").filter({ hasText: /^Revision 1$/ }).click();
    await expect(diff.locator("tr.ideDiffAdd")).toHaveCount(1);
    await expect(diff.locator("tr.ideDiffAdd ins")).toHaveText("    rv = abap_true.");

    expect(errors).toEqual([]);
});
