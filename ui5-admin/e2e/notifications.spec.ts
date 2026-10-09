import type { Page, Response } from "@playwright/test";
import {
    test, expect, clearRunsAndMarkers, seedFinishedAgentRun, seedFinishedWorkflowRun
} from "./fixtures";

// The bell, toast and list against the REAL backend: no `notifications` route is mocked.

const AGENT_RUN = "e2e-notify-run-agent";
const WORKFLOW_RUN = "e2e-notify-run-flow";
const isGet = (r: Response): boolean => r.url().endsWith("/notifications") && r.request().method() === "GET";
const isSeen = (r: Response): boolean => r.url().endsWith("/notifications/seen") && r.request().method() === "POST";

async function badge(page: Page): Promise<string> {
    // The badge is drawn by the button as an element with a data-badge attribute.
    const element = page.locator("[id$='notificationBell'] [data-badge], [id$='notificationBell'][data-badge]").first();
    return (await element.count()) === 0 ? "" : (await element.getAttribute("data-badge")) ?? "";
}

test("finished runs show a toast and a badge, opening the list reads them", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (message) => {
        if (message.type() === "error") {
            consoleErrors.push(message.text());
        }
    });
    page.on("pageerror", (error) => consoleErrors.push(String(error)));

    // a. first load: the real GET creates the caller's marker, nothing is unread.
    clearRunsAndMarkers();
    const firstGet = page.waitForResponse(isGet);
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    const first = await firstGet;
    expect(first.status()).toBe(200);
    expect((await first.json() as { unread_count: number }).unread_count).toBe(0);
    const bell = page.locator("[id$='notificationBell']");
    await expect(bell).toBeVisible();
    expect(await badge(page)).toBe("");

    // b. two runs finish while the app is open.
    seedFinishedAgentRun(AGENT_RUN, "e2e-notify-agent", "success");
    seedFinishedWorkflowRun(WORKFLOW_RUN, "e2e-notify-flow", "failed");

    // c. the next real poll (15 s) announces them.
    const toast = page.locator(".sapMMessageToast");
    await expect(toast).toContainText("2 runs finished", { timeout: 40_000 });
    await expect.poll(() => badge(page), { timeout: 5_000 }).toBe("2");
    await page.screenshot({ path: "../.sdd/run-notifications/task-4-badge-and-toast.png" });

    // d. opening the bell lists both and marks them read with one POST.
    const seenPost = page.waitForResponse(isSeen);
    await bell.click();
    const popover = page.locator(".sapMPopover");
    await expect(popover).toBeVisible();
    await expect(popover.getByText("e2e-notify-agent")).toBeVisible();
    await expect(popover.getByText("e2e-notify-flow")).toBeVisible();
    await expect(popover.getByText("success", { exact: true })).toBeVisible();
    await expect(popover.getByText("failed", { exact: true })).toBeVisible();
    const seen = await seenPost;
    expect(seen.status()).toBe(200);
    const body = seen.request().postDataJSON() as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(["up_to"]);
    expect(typeof body.up_to).toBe("string");
    await expect.poll(() => badge(page), { timeout: 5_000 }).toBe("");
    await page.screenshot({ path: "../.sdd/run-notifications/task-4-open-list.png" });

    // e. pressing the agent entry opens that run's detail page.
    await popover.getByText("e2e-notify-agent").click();
    await expect(page).toHaveURL(new RegExp(`#/runs/${AGENT_RUN}$`));

    // f. after a reload: no badge, entries still listed, none unread.
    const reloadGet = page.waitForResponse(isGet);
    await page.reload();
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    const afterReload = await (await reloadGet).json() as {
        unread_count: number; items: { run_id: string; unread: boolean }[];
    };
    expect(afterReload.unread_count).toBe(0);
    expect(afterReload.items.map((i) => i.run_id).sort()).toEqual([AGENT_RUN, WORKFLOW_RUN].sort());
    expect(afterReload.items.every((i) => !i.unread)).toBeTruthy();
    expect(await badge(page)).toBe("");
    await page.locator("[id$='notificationBell']").click();
    const reopened = page.locator(".sapMPopover");
    await expect(reopened.getByText("e2e-notify-agent")).toBeVisible();
    await expect(reopened.getByText("e2e-notify-flow")).toBeVisible();
    await expect(reopened.getByText("Unread", { exact: true })).toHaveCount(0);

    // g. nothing logged as an error.
    expect(consoleErrors).toEqual([]);
});
