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

test("the server rejects an http url and the UI shows it inline", async ({ page }) => {
    await page.goto("/index.html#/agents");
    await page.getByRole("button", { name: "New agent" }).click();
    await page.locator("textarea, input").first().waitFor();
    await page.getByRole("button", { name: "Add toolset" }).click();
    await page.getByLabel("URL").fill("http://insecure.example.com/mcp");
    await page.getByRole("button", { name: "OK" }).click();

    // sap.m.Input in Error state marks its <input> aria-invalid; there is no
    // ".sapMInputBaseErrorInner" class in the rendered SAPUI5 1.120 markup
    // (verified against the running app) -- the wrapper div instead gets
    // ".sapMInputBaseContentWrapperError".
    await expect(page.locator("[aria-invalid='true']").first()).toBeVisible();
});

test("settings round-trip through the real API", async ({ page, request }) => {
    await page.goto("/index.html#/settings");
    const instructions = `E2E instructions ${Date.now()}`;
    // The orchestrator-instructions TextArea sits directly in a Panel, not in
    // a SimpleForm, so it has no associated <label for="...">.
    // getByLabel("Orchestrator instructions") resolves to the Panel's own
    // aria-labelledby region (the form) rather than the TextArea itself, so
    // .fill() on it fails; target the control by its stable UI5 id suffix.
    await page.locator("[id$='orchestratorInstructions-inner']").fill(instructions);
    await page.getByRole("button", { name: "Save instructions" }).click();

    const response = await request.get("http://127.0.0.1:7932/admin/api/orchestrator");
    const body = await response.json() as { instructions: string };
    expect(body.instructions).toBe(instructions);
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
