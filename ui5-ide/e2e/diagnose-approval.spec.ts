import { test, expect, type Page, type Route } from "@playwright/test";

// The approval card, the report and the handover against canned answers (the
// reasoning of diagnose-findings.spec.ts): every /backend call of the two
// sessions is routed here, the UI5 resources come from the real server. The
// spec against the real backend routes is diagnose.spec.ts (Task 23).
const NOW = "2026-10-03T08:00:00Z";
const SESSION = {
    id: "e2e-diagnose-approval", title: "Why is the order list slow?", target: "e2e-system", type: "diagnose",
    stage: "investigate", status: "idle", owner: "jane.doe@example.com", created_at: NOW, updated_at: NOW
};
const CHANGE = {
    id: "e2e-change-from-report", title: "Change: Why is the order list slow?", target: "e2e-system", type: "change",
    stage: "chat", status: "idle", owner: "jane.doe@example.com", created_at: NOW, updated_at: NOW
};
const PARAMS = {
    processType: "http", objectType: "url", maxExecutions: 2, expiresHours: 1,
    sqlTrace: true, aggregate: true, description: "Trace the slow order list call"
};
const PENDING = {
    id: "ap-2", action: "trace_start", params: PARAMS, status: "pending",
    created_at: "2026-10-03T08:10:00Z", decided_at: null, result: null, error_code: null
};
const OLD = {
    id: "ap-1", action: "trace_start", params: { ...PARAMS, maxExecutions: 1 }, status: "expired",
    created_at: "2026-10-03T07:00:00Z", decided_at: null, result: null, error_code: null
};
const ARMED = {
    ...PENDING, status: "approved", decided_at: "2026-10-03T08:11:00Z",
    result: { trace_request_id: "TRC-4711", expires_at: "2026-10-03T09:11:00Z" }
};
const REPORT = { id: "a-report-1", kind: "report", version: 1, created_at: NOW };
const REPORT_MD = "# Slow order list\n\n**Root cause:** the list reads every order item in a loop.\n\n- Trace request TRC-4711\n- 412 identical SELECTs on the item table";

function json(body: unknown, status = 200) {
    return (route: Route) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function sse(frames: [string, unknown][]) {
    return (route: Route) => route.fulfill({
        status: 200, contentType: "text/event-stream",
        body: frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("")
    });
}

interface State { decisions: unknown[]; reported: boolean; handedOver: number }

async function mock(page: Page): Promise<State> {
    const state: State = { decisions: [], reported: false, handedOver: 0 };
    const base = `**/backend/sessions/${SESSION.id}`;
    const change = `**/backend/sessions/${CHANGE.id}`;
    await page.route("**/backend/me", json({
        principal: "jane.doe@example.com", is_admin: false, targets: ["e2e-system"], diagnose_targets: ["e2e-system"]
    }));
    await page.route("**/backend/sessions", (route) => (
        route.request().method() === "GET" ? json(state.handedOver ? [CHANGE, SESSION] : [SESSION])(route) : route.fallback()
    ));
    await page.route(base, (route) => json({ ...SESSION, artifacts: state.reported ? [REPORT] : [], files: [] })(route));
    await page.route(`${base}/messages`, (route) => json([
        { id: "m-1", role: "user", stage: "investigate", content: "Why is the order list slow?", created_at: NOW },
        {
            id: "m-2", role: "assistant", stage: "investigate", created_at: NOW,
            content: "I would like to trace the order list call. **A trace request is waiting for your approval.**"
        },
        ...(state.reported ? [{ id: "m-4", role: "assistant", stage: "investigate", content: "Report written.", created_at: NOW }] : [])
    ])(route));
    await page.route(`${base}/findings`, json([]));
    await page.route(`${base}/approvals`, (route) => json(state.decisions.length ? [ARMED, OLD] : [PENDING, OLD])(route));
    await page.route(`${base}/approvals/ap-2`, (route) => {
        state.decisions.push(route.request().postDataJSON());
        return json(ARMED)(route);
    });
    await page.route(`${base}/report`, (route) => {
        state.reported = true;
        return sse([
            ["run", { run_id: "r-1", stage: "investigate", message_id: "m-4" }],
            ["text", { delta: "Report written." }],
            ["artifact", REPORT],
            ["usage", { requests_used: 3, request_cap: 200 }],
            ["done", { message_id: "m-4", stage: "investigate", status: "idle" }]
        ])(route);
    });
    await page.route(`**/backend/sessions/*/artifacts/${REPORT.id}`, json({ ...REPORT, content: REPORT_MD }));
    await page.route(`${base}/handover`, (route) => {
        state.handedOver++;
        return json(CHANGE, 201)(route);
    });
    await page.route(change, json({ ...CHANGE, artifacts: [REPORT], files: [] }));
    await page.route(`${change}/messages`, json([]));
    await page.route(`${change}/files`, json([]));
    return state;
}

test("a trace request is approved on its card, the report opens, and the handover starts a change session with it", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
        if (message.type() === "error") {
            errors.push(message.text());
        }
    });
    page.on("pageerror", (error) => errors.push(error.message));
    const state = await mock(page);

    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });

    // The card says exactly what would be armed, and for whom.
    const card = page.locator(".ideApprovalCard");
    await expect(card).toHaveCount(1);
    await expect(card).toContainText("Arm a profiler trace on e2e-system?");
    for (const line of [
        "Process type: HTTP request", "Object type: URL", "Maximum executions: 2", "Trace expires after: 1 h",
        "SQL trace: On", "Aggregated measurement: On", "Description: Trace the slow order list call",
        "Traced user: Your own SAP user"
    ]) {
        await expect(card).toContainText(line);
    }
    await expect(card).toContainText("The trace runs for your own SAP user only and expires automatically.");
    const approve = card.getByRole("button", { name: "Approve trace" });
    const reject = card.getByRole("button", { name: "Reject" });
    await expect(approve).toBeVisible();
    await expect(reject).toBeVisible();
    // Approve is never the default: the focus is not on the card when it appears.
    expect(await page.evaluate(() => !!document.activeElement?.closest(".ideApprovalCard"))).toBe(false);
    // The earlier request that expired is a read-only line without buttons.
    await expect(page.locator(".ideApprovalDecided:visible")).toContainText("Request expired without a decision");
    await expect(page.locator(".ideApprovals button")).toHaveCount(2);
    expect(state.decisions).toEqual([]);
    await page.screenshot({ path: "test-results/diagnose-approval.png", fullPage: true });

    await approve.click();
    await expect(card).toHaveCount(0);
    await expect(page.locator(".ideApprovalDecided:visible").first()).toContainText("Trace armed: request TRC-4711, expires");
    await expect(page.locator(".ideApprovals button")).toHaveCount(0);
    expect(state.decisions).toEqual([{ decision: "approve" }]);
    // The backend writes no chat message for a decision: the line above is the record.
    await expect(page.locator(`[id$="--chatList"] li`)).toHaveCount(2);

    // Report: the document opens in front and is listed in the document menu.
    await expect(page.locator(`[id$="--handoverButton"]`)).toBeDisabled();
    await page.locator(`[id$="--reportButton"]`).click();
    await expect(page.locator(".ideEditorTab h1")).toHaveText("Slow order list");
    await expect(page.locator(`[id$="--handoverButton"]`)).toBeEnabled();
    await page.screenshot({ path: "test-results/diagnose-report.png", fullPage: true });

    // Handover: confirmed first, then the new change session is selected with the report.
    await page.locator(`[id$="--handoverButton"]`).click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("Start a change session from this report?");
    expect(state.handedOver).toBe(0);
    await confirm.getByRole("button", { name: "OK" }).click();
    await expect(page.locator(`[id$="--sessionList"] li.sapMLIBSelected`)).toContainText("Change: Why is the order list slow?");
    await expect(page.locator(".ideEditorTab h1")).toHaveText("Slow order list");
    await expect(page.locator(`[id$="--diagnoseBanner"]`)).toBeHidden();
    await expect(page.locator(".ideApprovalCard, .ideApprovalDecided:visible")).toHaveCount(0);
    expect(state.handedOver).toBe(1);

    expect(errors).toEqual([]);
});
