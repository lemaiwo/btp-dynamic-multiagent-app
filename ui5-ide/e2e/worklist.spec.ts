import { test, expect, type Page } from "@playwright/test";
import { approve, byId, clickWhenSettled, collectErrors, field, createSession, ensureTarget, insertArtifact, openApp } from "./support/backend";

// The worklist against the real backend, without any page.route: create a
// session in the dialog (the Diagnose type offers only flagged targets), find
// it with search and filters, see why a session waits for its developer,
// rename and delete it. The "Document to approve" row is a design document
// written into the SQLite file (no model locally); its stage was reached
// through the real approve route.
const TARGET = "DEMO_WL";
const FLAGGED = "DEMO_WL_NP";

function rowOf(page: Page, title: string) {
    return byId(page, "worklistTable").locator("tr.sapMLIB").filter({ hasText: title });
}

test("worklist: create, filter, waiting marker, rename and delete on the real backend", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await ensureTarget(request, TARGET, false);
    await ensureTarget(request, FLAGGED, true);
    const stamp = Date.now();
    const waitingTitle = `Waiting design ${stamp}`;
    const waiting = await createSession(request, waitingTitle, TARGET);
    await approve(request, waiting.id);
    insertArtifact(waiting.id, "design", "design", 1, "# Design\n\nGuard the run.");

    const errors = collectErrors(page);
    await openApp(page);
    await expect(page.getByRole("heading", { name: "My sessions" })).toBeVisible();

    // The design waits for its approval: marker and reason, and the "Waiting for me" filter keeps it.
    const waitingRow = rowOf(page, waitingTitle);
    await expect(waitingRow).toContainText("Waiting for you");
    await expect(waitingRow).toContainText("Document to approve");
    await expect(waitingRow).toContainText(TARGET);
    await expect(waitingRow).toContainText("Design");

    // New session: Diagnose lists only the flagged targets.
    const title = `Worklist change ${stamp}`;
    await byId(page, "worklistNewButton").click();
    const dialog = byId(page, "newSessionDialog");
    await expect(dialog).toBeVisible();
    await byId(page, "newSessionType").getByText("Diagnose", { exact: true }).click();
    await byId(page, "newSessionTarget").click();
    const diagnoseItems = page.locator(".sapMSelectList li");
    await expect(diagnoseItems.filter({ hasText: FLAGGED })).toHaveCount(1);
    await expect(diagnoseItems.filter({ hasText: new RegExp(`^${TARGET}$`) })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(page.locator(".sapMSltPicker")).toBeHidden();
    await expect(byId(page, "newSessionDiagnoseHint")).toBeVisible();

    // Back to Change: every target, pick ours; Create opens the new session page.
    await byId(page, "newSessionType").getByText("Change", { exact: true }).click();
    await field(page, "newSessionTitle").fill(title);
    await byId(page, "newSessionTarget").click();
    // The picker opens and closes with an animation: the item is pressed once it stands still, and the
    // dialog is used again once the picker is gone (a press during either animation lands elsewhere).
    await clickWhenSettled(page.locator(".sapMSelectList li[data-sap-ui]").filter({ hasText: new RegExp(`^${TARGET}$`) }));
    await expect(page.locator(".sapMSltPicker")).toBeHidden();
    await expect(byId(page, "newSessionTarget").locator(".sapMSltLabel")).toHaveText(TARGET);
    await byId(page, "newSessionCreate").click();
    await expect(dialog).toBeHidden();
    await expect(byId(page, "sessionTitle")).toHaveText(title);
    await expect(byId(page, "sessionTarget")).toHaveText(TARGET);
    await expect(page).toHaveTitle(`${title} - ABAP Assistant`);

    // Back to the list: the new session is there, idle in Chat.
    await byId(page, "backToWorklist").click();
    await expect(page.getByRole("heading", { name: "My sessions" })).toBeVisible();
    const row = rowOf(page, title);
    await expect(row).toContainText("Chat");
    await expect(row).toContainText("Idle");

    // Search narrows the list; "Waiting for me" keeps only the waiting one.
    await field(page, "worklistSearch").fill(String(stamp));
    await expect(rowOf(page, title)).toHaveCount(1);
    await expect(rowOf(page, waitingTitle)).toHaveCount(1);
    await byId(page, "worklistWaitingToggle").click();
    await expect(rowOf(page, title)).toHaveCount(0);
    await expect(rowOf(page, waitingTitle)).toHaveCount(1);
    await byId(page, "worklistWaitingToggle").click();
    // Type Diagnose: neither of the two change sessions; no match is said so.
    await byId(page, "worklistTypeFilter").getByText("Diagnose", { exact: true }).click();
    await expect(page.getByText("No matching sessions")).toBeVisible();
    await byId(page, "worklistTypeFilter").getByText("All", { exact: true }).click();
    await expect(rowOf(page, title)).toHaveCount(1);
    await page.screenshot({ path: "test-results/e2e-worklist.png", fullPage: true });

    // Rename the selected row.
    const renamed = `${title} renamed`;
    await rowOf(page, title).locator(".sapMLIBSelectS, .sapMRb").first().click();
    await expect(byId(page, "worklistRenameButton")).toBeEnabled();
    await byId(page, "worklistRenameButton").click();
    await field(page, "renameInput").fill(renamed);
    await byId(page, "renameSave").click();
    await expect(rowOf(page, renamed)).toHaveCount(1);
    const listed = await (await request.get("/backend/sessions")).json() as { id: string; title: string }[];
    const mine = listed.find((s) => s.title === renamed);
    expect(mine).toBeTruthy();

    // Delete it after the confirmation.
    await rowOf(page, renamed).locator(".sapMLIBSelectS, .sapMRb").first().click();
    await byId(page, "worklistDeleteButton").click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText(`Delete the session "${renamed}"`);
    await confirm.getByRole("button", { name: "Delete" }).click();
    await expect(rowOf(page, renamed)).toHaveCount(0);
    expect((await request.get(`/backend/sessions/${mine!.id}`)).status()).toBe(404);

    // A deep link to a session that does not exist says so on the page.
    await openApp(page, "sessions/00000000-0000-0000-0000-000000000000");
    await expect(byId(page, "loadFailedStrip")).toContainText("This session does not exist or belongs to someone else.");

    expect(errors.filter((e) => !/status of 404/.test(e))).toEqual([]);
});
