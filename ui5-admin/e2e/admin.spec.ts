import { test, expect } from "./fixtures";

test.beforeEach(async ({ page }) => {
    await page.goto("/index.html");
    // UI5 boots asynchronously; wait for the shell rather than a fixed delay.
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
});

test("the seeded agent appears in the list", async ({ page, seed }) => {
    await seed("e2e-agent-list");
    await page.goto("/index.html#/agents");
    await expect(page.getByText("e2e-agent-list")).toBeVisible();
});

test("an agent created in the UI is readable back from the API", async ({ page, request }) => {
    await page.goto("/index.html#/agents");
    await page.getByRole("button", { name: "New agent" }).click();

    await page.locator("textarea, input").first().waitFor();
    await page.getByLabel("Name").fill("e2e-created");
    await page.getByLabel("Description").fill("Created through the UI");
    await page.getByLabel("Instructions").fill("Do the created thing.");

    await page.getByRole("button", { name: "Add toolset" }).click();
    await page.getByLabel("URL").fill("builtin:gmail");
    await page.getByRole("button", { name: "OK" }).click();

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("e2e-created")).toBeVisible();

    // The contract check: what the UI sent must be what the API stored.
    const response = await request.get("http://127.0.0.1:7932/admin/api/agents");
    const agents = await response.json() as { name: string; mcp_servers: { url: string }[] }[];
    const created = agents.find((a) => a.name === "e2e-created");
    expect(created).toBeDefined();
    expect(created?.mcp_servers[0].url).toBe("builtin:gmail");
});

test("the server rejects a url outside the BTP host allow-list and the UI shows it inline", async ({ page }) => {
    await page.goto("/index.html#/agents");
    await page.getByRole("button", { name: "New agent" }).click();
    await page.locator("textarea, input").first().waitFor();
    await page.getByLabel("Name").fill("e2e-bad-host");
    await page.getByLabel("Description").fill("Should be rejected by the server");
    await page.getByLabel("Instructions").fill("n/a");

    await page.getByRole("button", { name: "Add toolset" }).click();
    // https:// satisfies validators.ts's client-side check, so this reaches
    // the server. The E2E backend runs with MCP_URL_ALLOWLIST unset (see
    // playwright.config.ts), so agents/admin.py falls back to requiring a
    // *.hana.ondemand.com host -- a check validators.ts deliberately does
    // not mirror (see its file comment), so only the server can reject this.
    await page.getByLabel("URL").fill("https://insecure.example.com/mcp");
    await page.getByRole("button", { name: "OK" }).click();
    await page.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText(/BTP-hosted URL/)).toBeVisible();
});

test("settings round-trip through the real API", async ({ page, request }) => {
    await page.goto("/index.html#/settings");

    // Non-destructive even if backend isolation is somehow bypassed (e.g. a
    // dev server reused despite reuseExistingServer: false): capture the
    // live orchestrator instructions and restore them afterwards.
    const before = await request.get("http://127.0.0.1:7932/admin/api/orchestrator");
    const original = (await before.json() as { instructions: string }).instructions;

    const instructions = `E2E instructions ${Date.now()}`;
    try {
        // The Panel wrapping the field is itself an ARIA "form" region named
        // by the same "Orchestrator instructions" header text, so a plain
        // getByLabel() match is ambiguous between the region and the
        // textarea; getByRole scopes it to the textbox.
        await page.getByRole("textbox", { name: "Orchestrator instructions" }).fill(instructions);
        await page.getByRole("button", { name: "Save instructions" }).click();

        const response = await request.get("http://127.0.0.1:7932/admin/api/orchestrator");
        const body = await response.json() as { instructions: string };
        expect(body.instructions).toBe(instructions);
    } finally {
        await request.put("http://127.0.0.1:7932/admin/api/orchestrator", {
            data: { instructions: original }
        });
    }
});

test("a skill created in the UI is attached to an agent", async ({ page, request }) => {
    await page.goto("/index.html#/skills");
    await page.getByRole("button", { name: "New skill" }).click();
    await page.getByLabel("Name").fill("e2e-skill-two");
    await page.getByLabel("Description").fill("Second e2e skill");
    await page.getByLabel("Content").fill("More instructions.");
    await page.getByRole("button", { name: "Save" }).click();

    // Scoped to the list table: SkillDetail.controller sets its page title to
    // the saved name before navigating back, and the NavContainer keeps that
    // page (off-screen) in the DOM, so an unscoped getByText("e2e-skill-two")
    // resolves to two elements and toBeVisible() fails strict-mode.
    await expect(page.locator("[id$='skillsTable']").getByText("e2e-skill-two")).toBeVisible();

    const response = await request.get("http://127.0.0.1:7932/admin/api/skills");
    const skills = await response.json() as { name: string }[];
    expect(skills.some((s) => s.name === "e2e-skill-two")).toBeTruthy();
});
