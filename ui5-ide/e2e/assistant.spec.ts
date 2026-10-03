import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";

// The model cannot run locally (AI Core is an unreachable placeholder, see
// playwright.config.ts), so the streamed chat is covered by the OPA journey
// against the FakeBackend. Here the stage bar and the Approve gate run
// against the real backend: the server's 409 is provoked by removing the
// design artifact behind the page's back, so the client gate still allows
// Approve and the server refuses it.
const DB = path.join(__dirname, "..", "_e2e_registry.db");
const TARGET = "e2e-system";

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

async function currentStage(page: Page): Promise<string> {
    return (await page.locator(`[id$="--stageBar"] .sapMObjStatusInverted .sapMObjStatusText`).innerText()).trim();
}

test("stage bar, Approve to Design, the client gate, and a server gate refusal (409)", async ({ page, request }) => {
    expect((await request.put(`/backend/conventions/${TARGET}`, { data: { label: "E2E assistant", package: "$TMP" } })).ok()).toBeTruthy();
    const created = await request.post("/backend/sessions", { data: { title: `E2E assistant ${Date.now()}`, target: TARGET } });
    expect(created.status()).toBe(201);
    const sid = (await created.json() as { id: string }).id;

    const errors = collectErrors(page);
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: "Assistant" })).toBeVisible();

    const tokens = page.locator(`[id$="--stageBar"] .sapMObjStatusText`);
    await expect(tokens).toHaveCount(6);
    await expect(tokens).toHaveText(["Chat", "Design", "Plan", "Propose", "Review", "Done"]);
    expect(await currentStage(page)).toBe("Chat");
    await expect(page.getByRole("group", { name: "Stages" })).toBeVisible();
    await expect(page.getByPlaceholder(/Ctrl\+Enter/)).toBeEnabled();

    const approve = page.getByRole("button", { name: "Approve" });
    await expect(approve).toBeEnabled();
    await approve.click();
    await expect.poll(() => currentStage(page)).toBe("Design");
    // Client gate: no design artifact yet.
    await expect(approve).toBeDisabled();
    await expect(page.getByRole("button", { name: "Revise…" })).toBeEnabled();

    // A design artifact appears (as a design run would leave it); after a
    // reload the client gate allows Approve.
    const aid = randomUUID();
    sql(`INSERT INTO ide_artifacts (id, session_id, stage, kind, content, version)
         VALUES (${quote(aid)}, ${quote(sid)}, 'design', 'design', ${quote("# Design\n\nGuard the run.")}, 1)`);
    await page.reload();
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => currentStage(page)).toBe("Design");
    await expect(approve).toBeEnabled();

    // The artifact goes away behind the page's back: the server refuses.
    sql(`DELETE FROM ide_artifacts WHERE id = ${quote(aid)}`);
    const refused = page.waitForResponse((r) => r.url().endsWith(`/sessions/${sid}/approve`));
    await approve.click();
    const response = await refused;
    expect(response.status()).toBe(409);
    expect((await response.json() as { code: string }).code).toBe("missing_artifact");
    const box = page.getByRole("alertdialog");
    await expect(box).toContainText("has not produced its document yet");
    await page.screenshot({ path: "test-results/assistant.png", fullPage: true });
    await box.getByRole("button", { name: "Close" }).click();
    await expect.poll(() => currentStage(page)).toBe("Design");
    await expect(approve).toBeDisabled();

    // Only the refused approve's 409 response may show up as a console error.
    expect(errors.filter((e) => !/409/.test(e))).toEqual([]);
});

test("a message streams against the real backend and the run ends cleanly without a model", async ({ page, request }) => {
    const created = await request.post("/backend/sessions", { data: { title: `E2E stream ${Date.now()}`, target: TARGET } });
    expect(created.status()).toBe(201);

    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    const input = page.getByPlaceholder(/Ctrl\+Enter/);
    await input.fill("Hello");
    const stream = page.waitForResponse((r) => r.url().endsWith("/messages") && r.request().method() === "POST");
    await input.press("Control+Enter");
    const response = await stream;
    expect(response.headers()["content-type"]).toContain("text/event-stream");
    // No model locally: the run fails and says so in the chat; the session is idle again.
    await expect(page.locator(`[id$="--runError"]`)).toBeVisible({ timeout: 45_000 });
    await expect(page.getByRole("button", { name: "Stop" })).toBeHidden();
    await expect(page.locator(`[id$="--chatList"]`)).toContainText("Hello");
    await expect(input).toBeEnabled();
    await expect(input).toBeEditable();
    // The run's end gives the focus back to the input.
    await expect(input).toBeFocused();
    await page.screenshot({ path: "test-results/assistant-run-error.png", fullPage: true });
});
