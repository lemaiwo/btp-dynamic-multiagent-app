import { test, expect, type APIRequestContext, type Page } from "@playwright/test";
import { byId, collectErrors, field, openApp } from "./support/backend";

// The conventions dialog against the real backend, without any page.route.
// Locally there is no XSUAA, so the caller is an admin: create a target
// (POST), edit and save a field (PUT), and the non-production switch, which
// asks first (Cancel changes nothing) and sends the flag alone once confirmed.
const TARGET = `DEMO_CONV_${Date.now()}`;

async function stored(request: APIRequestContext): Promise<Record<string, unknown>> {
    const response = await request.get(`/backend/conventions/${TARGET}`);
    expect(response.ok()).toBeTruthy();
    return await response.json() as Record<string, unknown>;
}

function switchOf(page: Page) {
    return byId(page, "convNonProduction");
}

test("conventions: create a target, save a field, confirm the non-production flag on the real backend", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    const errors = collectErrors(page);
    await openApp(page);

    await byId(page, "conventionsButton").click();
    const dialog = byId(page, "conventionsDialog");
    await expect(dialog).toBeVisible();
    await expect(byId(page, "conventionsReadOnly")).toBeHidden();

    // New target: created with POST, then shown in edit mode.
    await byId(page, "convNewTargetButton").click();
    await expect(byId(page, "convNonProductionHint")).toHaveText("Create the target first; then it can be marked as non-production.");
    await field(page, "convNewTarget").fill(TARGET);
    await field(page, "convLabel").fill("Demo system");
    await field(page, "convPackage").fill("ZDEMO");
    await byId(page, "convSaveButton").click();
    await expect(byId(page, "convTarget").locator(".sapMSltLabel")).toHaveText(TARGET);
    expect(await stored(request)).toMatchObject({ target: TARGET, label: "Demo system", package: "ZDEMO", non_production: false });

    // Edit a field and save: PUT with the change only.
    await field(page, "convNamespace").fill("/DEMO/");
    await byId(page, "convSaveButton").click();
    await expect.poll(async () => (await stored(request)).namespace).toBe("/DEMO/");
    expect(await stored(request)).toMatchObject({ label: "Demo system", package: "ZDEMO", non_production: false });

    // The switch asks first; Cancel (the focused button) leaves the stored state.
    await expect(switchOf(page)).toBeEnabled();
    await switchOf(page).click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText(`Diagnose sessions on ${TARGET} will send dumps and traces`);
    await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
    await page.screenshot({ path: "test-results/e2e-conventions-confirm.png", fullPage: true });
    await confirm.getByRole("button", { name: "Cancel" }).click();
    await expect(confirm).toHaveCount(0);
    await expect(switchOf(page)).toHaveAttribute("aria-checked", "false");
    expect((await stored(request)).non_production).toBe(false);

    // Confirmed: the flag is stored and the warning strip appears.
    await switchOf(page).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Mark as Non-Production" }).click();
    await expect.poll(async () => (await stored(request)).non_production).toBe(true);
    await expect(switchOf(page)).toHaveAttribute("aria-checked", "true");
    await expect(byId(page, "convNonProductionStrip")).toContainText(`Diagnose sessions on ${TARGET} send dumps and traces`);
    const me = await (await request.get("/backend/me")).json() as { diagnose_targets: string[] };
    expect(me.diagnose_targets).toContain(TARGET);

    // And taken away again.
    await switchOf(page).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove Flag" }).click();
    await expect.poll(async () => (await stored(request)).non_production).toBe(false);
    await expect(byId(page, "convNonProductionStrip")).toBeHidden();
    await page.screenshot({ path: "test-results/e2e-conventions.png", fullPage: true });

    await byId(page, "convCloseButton").click();
    await expect(dialog).toBeHidden();
    expect(errors).toEqual([]);
});
