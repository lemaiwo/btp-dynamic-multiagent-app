import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";

// A stored assistant message carries its run's activity (what the runner saves
// when a run ends); loading the session fills the activity panel from it. The
// conventions dialog runs against the real backend: locally there is no XSUAA,
// so the caller is whatever /me says.
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

test("the activity panel shows the stored plan and tool calls of the last answer", async ({ page, request }) => {
    expect((await request.put(`/backend/conventions/${TARGET}`, { data: { label: "E2E activity", package: "$TMP" } })).ok()).toBeTruthy();
    const created = await request.post("/backend/sessions", { data: { title: `E2E activity ${Date.now()}`, target: TARGET } });
    expect(created.status()).toBe(201);
    const sid = (await created.json() as { id: string }).id;
    const activity = {
        events: [
            { ts: "2026-10-03T10:00:00Z", agent: "abap", kind: "tool", id: "c1", tool: "SAPRead", detail: "ZCL_DEMO", status: "ok", output: "CLASS zcl_demo DEFINITION PUBLIC.\nENDCLASS." },
            { ts: "2026-10-03T10:00:01Z", agent: "abap", kind: "tool", id: "c2", tool: "SAPLint", detail: "ZCL_DEMO", status: "error", output: "(interrupted)" }
        ],
        plan: [
            { content: "Read the class", status: "completed" },
            { content: "Write the design", status: "in_progress" },
            { content: "Review", status: "pending" }
        ]
    };
    sql(`INSERT INTO ide_messages (id, session_id, stage, role, content, activity_json)
         VALUES (${quote(randomUUID())}, ${quote(sid)}, 'chat', 'user', 'Explain ZCL_DEMO', NULL)`);
    sql(`INSERT INTO ide_messages (id, session_id, stage, role, content, activity_json, created_at)
         VALUES (${quote(randomUUID())}, ${quote(sid)}, 'chat', 'assistant', 'It is a demo class.', ${quote(JSON.stringify(activity))}, datetime('now', '+1 second'))`);

    const errors = collectErrors(page);
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });

    const header = page.getByText("Activity (2 tool calls, 1/3 todos)");
    await expect(header).toBeVisible({ timeout: 15_000 });
    // Collapsed by default: the lists are not visible until expanded.
    await expect(page.locator(`[id$="--activityTimeline"]`)).toBeHidden();
    await header.click();
    await expect(page.locator(`[id$="--activityTodos"] li`)).toHaveCount(3);
    await expect(page.locator(`[id$="--activityTodos"]`)).toContainText("[~] Write the design");
    await expect(page.locator(`[id$="--activityTimeline"] li`)).toHaveCount(2);
    await expect(page.locator(`[id$="--activityTimeline"]`)).toContainText("SAPRead");
    await page.getByRole("link", { name: "Show full output" }).first().click();
    await expect(page.getByRole("link", { name: "Show less" })).toBeVisible();
    await page.screenshot({ path: "test-results/activity.png", fullPage: true });
    expect(errors).toEqual([]);
});

test("the conventions dialog lists the target's conventions", async ({ page, request }) => {
    expect((await request.put(`/backend/conventions/${TARGET}`, { data: { label: "E2E conventions", package: "$TMP", namespace: "Z" } })).ok()).toBeTruthy();
    const me = await (await request.get("/backend/me")).json() as { is_admin: boolean };
    const errors = collectErrors(page);
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "Conventions" }).click();
    const dialog = page.getByRole("dialog", { name: "Conventions" });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(`input[value="$TMP"]`).first()).toBeVisible({ timeout: 15_000 });
    if (me.is_admin) {
        await expect(dialog.getByRole("button", { name: "Save" })).toBeVisible();
    } else {
        await expect(dialog.getByRole("button", { name: "Save" })).toHaveCount(0);
        await expect(dialog).toContainText("maintained by an administrator");
    }
    await page.screenshot({ path: "test-results/conventions.png", fullPage: true });
    expect(errors).toEqual([]);
});
