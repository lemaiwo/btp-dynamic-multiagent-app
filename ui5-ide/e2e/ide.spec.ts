import { test, expect, type Page } from "@playwright/test";

// Locally the backend runs without XSUAA, so every caller is an admin and
// may seed a conventions target, which is what a session needs.
const TARGET = "e2e-system";

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

test.beforeAll(async ({ request }) => {
    const response = await request.put(`/backend/conventions/${TARGET}`, {
        data: { label: "E2E system", package: "$TMP" }
    });
    expect(response.ok()).toBeTruthy();
});

test("the workbench loads with three panes and no console errors", async ({ page }) => {
    const errors = collectErrors(page);

    await page.goto("/index.html");
    // UI5 boots asynchronously; wait for the shell rather than a fixed delay.
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "ABAP Workbench" })).toBeVisible();
    for (const pane of ["explorerPane", "editorPane", "assistantPane"]) {
        await expect(page.locator(`[id$="--${pane}"]`)).toBeAttached();
    }
    await expect(page.getByRole("heading", { name: "Explorer" })).toBeVisible();

    expect(errors).toEqual([]);
});

test("a new session appears selected in stage Chat", async ({ page }) => {
    const errors = collectErrors(page);
    const title = `E2E session ${Date.now()}`;

    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });

    await page.getByRole("button", { name: "New session" }).click();
    const dialog = page.getByRole("dialog", { name: "New Session" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("textbox", { name: "Title" }).fill(title);
    // The only target is preselected.
    await expect(dialog.getByRole("combobox", { name: "Target system" })).toContainText(TARGET);
    await dialog.getByRole("button", { name: "Create" }).click();
    await expect(dialog).toBeHidden();

    // UI5 1.120 renders role=listitem without aria-selected; the selected
    // item carries sapMLIBSelected.
    const item = page.locator(`[id$="--sessionList"] li.sapMLIBSelected`);
    await expect(item).toContainText(title);
    await expect(item).toContainText("Chat");
    // The shell header shows the open session.
    await expect(page.locator(`[id$="--sessionTitle"]`)).toHaveText(title);

    await page.screenshot({ path: "test-results/explorer-new-session.png", fullPage: true });
    expect(errors).toEqual([]);
});
