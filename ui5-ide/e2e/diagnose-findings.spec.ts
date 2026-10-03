import { test, expect, type Page, type Route } from "@playwright/test";

// The diagnose layout against canned answers: the session, its messages and
// its findings are routed here, so the spec does not depend on which
// diagnose routes the local backend already has. Everything else (the UI5
// resources, any other /backend call) goes to the real servers.
const NOW = "2026-10-03T08:00:00Z";
const SESSION = {
    id: "e2e-diagnose", title: "Why does the order dump?", target: "e2e-system", type: "diagnose",
    stage: "investigate", status: "idle", owner: "jane.doe@example.com", created_at: NOW, updated_at: NOW
};
const FINDINGS = [
    {
        id: "f-3", kind: "gateway_error", ref_id: "GW-3", title: "Order service 500", program: "ZCL_ORDER_DPC_EXT=CP",
        include: null, line: 9, occurred_at: "2026-10-03T07:58:00Z", created_at: NOW
    },
    {
        id: "f-2", kind: "trace", ref_id: "TRC-7", title: "Slow order list", program: null, include: null,
        line: null, occurred_at: null, created_at: NOW
    },
    {
        id: "f-1", kind: "dump", ref_id: "DUMP-1", title: "CX_SY_ZERODIVIDE", program: "ZCL_ORDER=====CP",
        include: "ZCL_ORDER=====CM003", line: 42, occurred_at: "2026-10-03T07:41:00Z", created_at: NOW
    }
];

function json(body: unknown) {
    return (route: Route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockDiagnose(page: Page): Promise<void> {
    await page.route("**/backend/me", json({
        principal: "jane.doe@example.com", is_admin: false, targets: ["e2e-system"], diagnose_targets: ["e2e-system"]
    }));
    await page.route("**/backend/sessions", (route) => (
        route.request().method() === "GET" ? json([SESSION])(route) : route.fallback()
    ));
    await page.route(`**/backend/sessions/${SESSION.id}`, json({ ...SESSION, artifacts: [], files: [] }));
    await page.route(`**/backend/sessions/${SESSION.id}/messages`, json([]));
    await page.route(`**/backend/sessions/${SESSION.id}/findings`, json(FINDINGS));
    // A diagnose session also reads its trace approvals when it opens.
    await page.route(`**/backend/sessions/${SESSION.id}/approvals`, json([]));
}

test("a diagnose session shows the banner, the Investigate step and its findings", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
        if (message.type() === "error") {
            errors.push(message.text());
        }
    });
    page.on("pageerror", (error) => errors.push(error.message));
    await mockDiagnose(page);

    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });

    await expect(page.locator(`[id$="--diagnoseBanner"]`)).toContainText("sent to the AI model as they are");
    const tokens = page.locator(`[id$="--stageBar"] .ideStageToken`);
    await expect(tokens).toHaveCount(1);
    await expect(tokens).toContainText("Investigate");
    await expect(page.getByRole("button", { name: "Report" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Hand over" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);

    await expect(page.getByRole("heading", { name: "Findings (3)" })).toBeVisible();
    const rows = page.locator(`[id$="--findingsList"] li.sapMLIB`);
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("Order service 500");
    await expect(rows.nth(0)).toContainText("Gateway error");
    await expect(rows.nth(2)).toContainText("ZCL_ORDER=====CP · ZCL_ORDER=====CM003 · line 42");

    await page.screenshot({ path: "test-results/diagnose-findings.png", fullPage: true });
    expect(errors).toEqual([]);
});
