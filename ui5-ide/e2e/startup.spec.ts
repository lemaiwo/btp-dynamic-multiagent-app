import { test, expect, type Page } from "@playwright/test";

// A smoke test of the start page against the real backend. The flows of the
// worklist and the session page have their own specs in this folder.

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

test("the app opens on the worklist under the ABAP Assistant shell bar, without console errors", async ({ page }) => {
    const errors = collectErrors(page);

    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "ABAP Assistant" })).toBeVisible();
    await expect(page.locator(`[id$="--worklistTable"]`)).toBeVisible();
    await expect(page).toHaveTitle("ABAP Assistant");

    expect(errors).toEqual([]);
});
