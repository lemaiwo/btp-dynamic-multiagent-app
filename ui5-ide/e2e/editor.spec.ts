import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";

// A proposal only comes out of a model run with ARC-1 behind it, and neither
// exists locally. The session is created over the real API; its workspace
// file and design artifact are written straight into the throw-away SQLite
// database the backend runs on (playwright.config.ts), the rows a propose
// run would leave. Everything the UI then reads goes through the backend.
const DB = path.join(__dirname, "..", "_e2e_registry.db");
// The same target as ide.spec.ts: that test expects it to be the only one.
const TARGET = "e2e-system";
const FILE = "src/CLAS/zcl_e2e_fix.clas.abap";
const ORIGIN = "CLASS zcl_e2e_fix IMPLEMENTATION.\n  METHOD run.\n  ENDMETHOD.\nENDCLASS.\n";
const PROPOSED = "CLASS zcl_e2e_fix IMPLEMENTATION.\n  METHOD run.\n    \" <script>alert(1)</script>\n    rv = abap_true.\n  ENDMETHOD.\nENDCLASS.\n";

function sql(statement: string): void {
    execFileSync("sqlite3", [DB, statement]);
}

function quote(text: string): string {
    return `'${text.replace(/'/g, "''")}'`;
}

function collectErrors(page: Page): string[] {
    const errors: string[] = [];
    page.on("console", (message) => {
        if (message.type() === "error") {
            errors.push(message.text());
        }
    });
    page.on("pageerror", (error) => errors.push(error.message));
    return errors;
}

test("open a proposed file, toggle Source / Proposed / Diff and open the design", async ({ page, request }) => {
    expect((await request.put(`/backend/conventions/${TARGET}`, { data: { label: "E2E editor", package: "$TMP" } })).ok()).toBeTruthy();
    const created = await request.post("/backend/sessions", { data: { title: `E2E editor ${Date.now()}`, target: TARGET } });
    expect(created.status()).toBe(201);
    const sid = (await created.json() as { id: string }).id;
    sql(`INSERT INTO ide_workspace_files (id, session_id, path, object_type, object_name, origin_source, proposed_source, state)
         VALUES (${quote(randomUUID())}, ${quote(sid)}, ${quote(FILE)}, 'CLAS', 'ZCL_E2E_FIX', ${quote(ORIGIN)}, ${quote(PROPOSED)}, 'modified')`);
    sql(`INSERT INTO ide_artifacts (id, session_id, stage, kind, content, version)
         VALUES (${quote(randomUUID())}, ${quote(sid)}, 'design', 'design', ${quote("# Design v1\n\nGuard <script>alert(2)</script> the run.")}, 1)`);

    const errors = collectErrors(page);
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "Editor" })).toBeVisible();

    // The newest session (this one) is selected; open its proposed file.
    await page.locator(`[id$="--workspaceTree"]`).getByText("zcl_e2e_fix.clas.abap").click();
    const tabs = page.locator(`[id$="--editorTabs"]`);
    await expect(tabs.getByText("zcl_e2e_fix.clas.abap").first()).toBeVisible();
    const editor = tabs.locator(".ace_editor").first();
    await expect(editor).toContainText("rv = abap_true.");
    // The editor fills its tab (flex), with no height tied to the shell.
    expect((await editor.boundingBox())?.height ?? 0).toBeGreaterThan(300);

    await tabs.getByText("Source", { exact: true }).click();
    await expect(editor).not.toContainText("rv = abap_true.");
    await expect(editor).toContainText("ENDMETHOD.");

    await tabs.getByText("Diff", { exact: true }).click();
    const diff = tabs.locator(".ideDiff");
    await expect(diff).toBeVisible();
    await expect(diff.locator("tr.ideDiffAdd")).toHaveCount(2);
    await expect(diff.locator("tr.ideDiffAdd").first()).toContainText("<script>alert(1)</script>");
    expect(await diff.locator("script").count()).toBe(0);
    // Not colour alone: each added row has a + marker with hidden text.
    await expect(diff.locator("tr.ideDiffAdd .ideDiffMark").first()).toHaveText("+added line");
    expect((await diff.boundingBox())?.height ?? 0).toBeGreaterThan(300);
    await page.screenshot({ path: "test-results/editor-diff.png", fullPage: true });

    await page.getByRole("button", { name: "Documents" }).click();
    await page.getByRole("menuitem", { name: "Design" }).click();
    const doc = tabs.locator(".ideMarkdown");
    await expect(doc.locator("h1")).toHaveText("Design v1");
    expect(await doc.locator("script").count()).toBe(0);
    await page.screenshot({ path: "test-results/editor-design.png", fullPage: true });

    expect(errors).toEqual([]);
});
