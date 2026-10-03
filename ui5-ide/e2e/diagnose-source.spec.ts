import { test, expect, type Page, type Route } from "@playwright/test";

// From a finding to its source, against canned answers (the reasoning of
// diagnose-findings.spec.ts): the session, its findings, the opened file and
// the finding's detail are routed here; the UI5 resources come from the
// real server.
const NOW = "2026-10-03T08:00:00Z";
const SESSION = {
    id: "e2e-diagnose-source", title: "Why does the calculation dump?", target: "e2e-system", type: "diagnose",
    stage: "investigate", status: "idle", owner: "jane.doe@example.com", created_at: NOW, updated_at: NOW
};
const FILE = { path: "src/CLAS/zcl_demo_calc.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_DEMO_CALC" };
const ORDER = { path: "src/CLAS/zcl_order.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_ORDER" };
const LINE = 142;
const SOURCE = Array.from({ length: 200 }, (_, i) => (
    i + 1 === LINE ? "    rv_result = iv_total / iv_count." : `    \" zcl_demo_calc line ${i + 1}`
)).join("\n");
const FINDINGS = [
    {
        id: "f-2", kind: "dump", ref_id: "DUMP-2", title: "COMPUTE_INT_ZERODIVIDE", program: "ZCL_DEMO_CALC=================CP",
        include: null, line: LINE, occurred_at: "2026-10-03T07:58:00Z", created_at: NOW
    },
    {
        id: "f-1", kind: "dump", ref_id: "DUMP-1", title: "CX_SY_ZERODIVIDE", program: "ZCL_ORDER=====CP",
        include: "ZCL_ORDER=====CM003", line: 42, occurred_at: "2026-10-03T07:41:00Z", created_at: NOW
    }
];
// Dump text with markup in it: it must show as text, never run or render.
const DETAIL = "Runtime error COMPUTE_INT_ZERODIVIDE\nUser DEVUSER01\n<img src=x onerror=\"window.__dumpMarkupRan = true\"><b>bold</b>";

function json(body: unknown) {
    return (route: Route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockDiagnose(page: Page): Promise<void> {
    const base = `**/backend/sessions/${SESSION.id}`;
    await page.route("**/backend/me", json({
        principal: "jane.doe@example.com", is_admin: false, targets: ["e2e-system"], diagnose_targets: ["e2e-system"]
    }));
    await page.route("**/backend/sessions", (route) => (
        route.request().method() === "GET" ? json([SESSION])(route) : route.fallback()
    ));
    await page.route(base, json({ ...SESSION, artifacts: [], files: [] }));
    await page.route(`${base}/messages`, json([]));
    await page.route(`${base}/findings`, json(FINDINGS));
    // A diagnose session also reads its trace approvals when it opens.
    await page.route(`${base}/approvals`, json([]));
    await page.route(`${base}/findings/f-2/open`, json({ file: FILE, line: LINE, hint: null }));
    await page.route(`${base}/findings/f-1/open`, json({ file: ORDER, line: null, hint: "Method include ZCL_ORDER=====CM003, line 42" }));
    await page.route(`${base}/findings/f-2`, json({ finding: FINDINGS[0], detail: DETAIL }));
    await page.route(`${base}/findings/f-2?refresh=true`, json({ finding: FINDINGS[0], detail: `${DETAIL}\nre-read` }));
    await page.route(`${base}/file?path=*`, (route) => {
        const path = new URL(route.request().url()).searchParams.get("path");
        const file = path === FILE.path ? FILE : ORDER;
        return json({ ...file, origin_source: path === FILE.path ? SOURCE : "CLASS zcl_order IMPLEMENTATION.\nENDCLASS.", proposed_source: "", lint: [] })(route);
    });
}

test("a finding opens its source at the highlighted line; a method include shows a hint; details are plain text", async ({ page }) => {
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

    const rows = page.locator(`[id$="--findingsList"] li.sapMLIB`);
    await expect(rows).toHaveCount(2);
    await expect(page.locator(`[id$="--workspaceTree"]`)).toBeHidden();
    await expect(page.locator(`[id$="--openObjectSearch"]`)).toBeVisible();

    // The exact line: scrolled into view (Ace only draws markers of visible rows) and highlighted.
    await rows.nth(0).click();
    const tab = page.locator(".ideEditorTab");
    await expect(tab).toHaveAttribute("data-finding-line", String(LINE));
    const marker = page.locator(".ideEditorTab .ace_marker-layer .ideFindingLine");
    await expect(marker).toBeVisible();
    await expect(page.locator(".ideEditorTab .ace_content")).toContainText("rv_result = iv_total / iv_count.");
    const background = await marker.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(background).not.toBe("rgba(0, 0, 0, 0)");
    // Ace's own active-line band is on the same row after the jump: the finding line is drawn over it.
    const stacking = await page.evaluate(() => [".ideFindingLine", ".ace_active-line"].map((selector) => (
        Number(getComputedStyle(document.querySelector(`.ideEditorTab .ace_marker-layer ${selector}`) as Element).zIndex))));
    expect(stacking[0]).toBeGreaterThan(stacking[1]);
    await page.screenshot({ path: "test-results/diagnose-source.png", fullPage: true });

    // A method include: the class opens, nothing is highlighted, the hint says where the finding is.
    await rows.nth(1).click();
    await expect(page.locator(".ideEditorTab .sapMMsgStrip")).toContainText("Method include ZCL_ORDER=====CM003, line 42");
    await expect(tab).toHaveAttribute("data-finding-line", "");
    await expect(page.locator(".ideEditorTab .ideFindingLine")).toHaveCount(0);
    await page.screenshot({ path: "test-results/diagnose-source-hint.png", fullPage: true });

    // Details: the dump text as text.
    await rows.nth(0).getByRole("button", { name: "Show details" }).click();
    const dialog = page.locator(`[id$="--findingDetailDialog"]`);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".ace_content")).toContainText("User DEVUSER01");
    await expect(dialog.locator(".ace_content")).toContainText("<b>bold</b>");
    await expect(dialog.locator("img")).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as { __dumpMarkupRan?: boolean }).__dumpMarkupRan)).toBeUndefined();
    await dialog.getByRole("button", { name: "Refresh from SAP" }).click();
    await expect(dialog.locator(".ace_content")).toContainText("re-read");
    await page.screenshot({ path: "test-results/diagnose-detail.png", fullPage: true });
    await dialog.getByRole("button", { name: "Close" }).click();
    await expect(dialog).toBeHidden();

    expect(errors).toEqual([]);
});
