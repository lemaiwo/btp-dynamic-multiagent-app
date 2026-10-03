import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";

// The diagnose flow against the real backend routes. No model and no ARC-1
// exist locally, so no agent turn runs and no trace is armed: a session is
// created over the API on a target flagged non_production, and the rows a
// diagnose run would leave (a trace proposal, a report) are written straight
// into the throw-away SQLite database (the reasoning of editor.spec.ts).
// The spec with canned answers is diagnose-approval.spec.ts.
const DB = path.join(__dirname, "..", "_e2e_registry.db");
const TARGET = "e2e-system";
const PARAMS = {
    processType: "http", objectType: "url", maxExecutions: 2, expiresHours: 1,
    sqlTrace: true, aggregate: true, description: "Trace the slow order list call"
};
const REPORT_MD = "# Slow order list\n\n**Root cause:** a loop reads every order item.\n\n- 412 identical SELECTs";

function sql(statement: string): void {
    execFileSync("sqlite3", [DB, statement]);
}

function quote(text: string): string {
    return `'${text.replace(/'/g, "''")}'`;
}

function seedApproval(sid: string, ageMinutes: number): string {
    const id = randomUUID();
    sql(`INSERT INTO ide_approvals (id, session_id, action, params_json, status, created_at)
         VALUES (${quote(id)}, ${quote(sid)}, 'trace_start', ${quote(JSON.stringify(PARAMS))}, 'pending',
                 datetime('now', '-${ageMinutes} minutes'))`);
    return id;
}

function collectErrors(page: Page): string[] {
    const errors: string[] = [];
    page.on("console", (message) => {
        // The refusals this spec provokes on purpose are logged by the browser.
        if (message.type() === "error" && !/status of (409|410|424)/.test(message.text())) {
            errors.push(message.text());
        }
    });
    page.on("pageerror", (error) => errors.push(error.message));
    return errors;
}

test("diagnose session: banner, reject a trace request, refused decisions and handover on the real backend", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    // Only a non_production target takes a diagnose session.
    expect((await request.put(`/backend/conventions/${TARGET}`, {
        data: { label: "E2E diagnose", package: "$TMP", non_production: false }
    })).ok()).toBeTruthy();
    const refused = await request.post("/backend/sessions", { data: { title: "refused", target: TARGET, type: "diagnose" } });
    expect(refused.status()).toBe(422);
    expect((await refused.json() as { code: string }).code).toBe("target_not_non_production");
    expect((await request.put(`/backend/conventions/${TARGET}`, {
        data: { label: "E2E diagnose", package: "$TMP", non_production: true }
    })).ok()).toBeTruthy();

    const title = `E2E diagnose ${Date.now()}`;
    const created = await request.post("/backend/sessions", { data: { title, target: TARGET, type: "diagnose" } });
    expect(created.status()).toBe(201);
    const session = await created.json() as { id: string; type: string; stage: string };
    const sid = session.id;
    expect(session.type).toBe("diagnose");
    expect(session.stage).toBe("investigate");

    // The loader of a diagnose session reads /approvals: seed a fresh proposal
    // (to reject in the UI) and one past its lifetime (to refuse).
    const fresh = seedApproval(sid, 1);
    const stale = seedApproval(sid, 24 * 60);

    // Handover without a report is refused.
    const noReport = await request.post(`/backend/sessions/${sid}/handover`);
    expect(noReport.status()).toBe(409);
    expect((await noReport.json() as { code: string }).code).toBe("missing_artifact");
    // A diagnose session never moves on.
    expect((await request.post(`/backend/sessions/${sid}/approve`, { data: {} })).status()).toBe(409);

    const errors = collectErrors(page);
    await page.goto("/index.html");
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(`[id$="--sessionTitle"]`)).toHaveText(title);
    await expect(page.locator(`[id$="--diagnoseBanner"]`)).toContainText("sent to the AI model as they are");
    await expect(page.locator(`[id$="--stageBar"] .ideStageToken`)).toHaveCount(1);
    await expect(page.locator(`[id$="--stageBar"] .ideStageToken`).first()).toContainText("Investigate");
    await expect(page.locator(`[id$="--handoverButton"]`)).toBeDisabled();

    // One card for the fresh proposal, a read-only line for the expired one.
    const card = page.locator(".ideApprovalCard");
    await expect(card).toHaveCount(1);
    await expect(card).toContainText("Arm a profiler trace on e2e-system?");
    await expect(card).toContainText("Description: Trace the slow order list call");
    await expect(page.locator(".ideApprovalDecided:visible")).toContainText("Request expired without a decision");
    await page.screenshot({ path: "test-results/diagnose-e2e-approval.png", fullPage: true });

    // Reject works and says so; nothing is armed (no ARC-1 call on a denial).
    await card.getByRole("button", { name: "Reject" }).click();
    await expect(card).toHaveCount(0);
    await expect(page.locator(".ideApprovalDecided:visible").first()).toContainText(/denied|rejected/i);
    const listed = await (await request.get(`/backend/sessions/${sid}/approvals`)).json() as { id: string; status: string }[];
    expect(listed.find((a) => a.id === fresh)?.status).toBe("denied");
    expect(listed.find((a) => a.id === stale)?.status).toBe("expired");
    await page.screenshot({ path: "test-results/diagnose-e2e-denied.png", fullPage: true });

    // Decisions that are no longer open are refused, once each.
    const late = await request.post(`/backend/sessions/${sid}/approvals/${stale}`, { data: { decision: "approve" } });
    expect(late.status()).toBe(410);
    expect((await late.json() as { code: string }).code).toBe("approval_expired");
    const again = await request.post(`/backend/sessions/${sid}/approvals/${fresh}`, { data: { decision: "approve" } });
    expect(again.status()).toBe(409);
    expect((await again.json() as { code: string }).code).toBe("approval_not_pending");
    // Another session's id space: an unknown approval is a 404.
    expect((await request.post(`/backend/sessions/${sid}/approvals/${randomUUID()}`, { data: { decision: "deny" } })).status()).toBe(404);

    // A report (what POST /report captures) enables the handover.
    sql(`INSERT INTO ide_artifacts (id, session_id, stage, kind, content, version)
         VALUES (${quote(randomUUID())}, ${quote(sid)}, 'investigate', 'report', ${quote(REPORT_MD)}, 1)`);
    await page.reload();
    await expect(page.locator(".sapTntToolPage")).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(`[id$="--sessionTitle"]`)).toHaveText(title);
    await expect(page.locator(`[id$="--handoverButton"]`)).toBeEnabled();
    await page.locator(`[id$="--handoverButton"]`).click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("Start a change session from this report?");
    await confirm.getByRole("button", { name: "OK" }).click();

    // The new change session is selected and opens with the report.
    await expect(page.locator(`[id$="--sessionTitle"]`)).toHaveText(`Change: ${title}`);
    await expect(page.locator(".ideEditorTab h1")).toHaveText("Slow order list");
    await expect(page.locator(`[id$="--diagnoseBanner"]`)).toBeHidden();
    await page.screenshot({ path: "test-results/diagnose-e2e-handover.png", fullPage: true });
    const sessions = await (await request.get("/backend/sessions")).json() as { title: string; type: string; stage: string }[];
    const change = sessions.find((s) => s.title === `Change: ${title}`);
    expect(change?.type).toBe("change");
    expect(change?.stage).toBe("chat");

    // An admin takes the flag away: the diagnose session reads `masked`, its banner says reads
    // and runs are blocked, and Send / Report answer the backend's 409. A pending request can
    // still be rejected (a deny sends nothing to SAP).
    const pending = seedApproval(sid, 1);
    try {
        expect((await request.put(`/backend/conventions/${TARGET}`, {
            data: { label: "E2E diagnose", package: "$TMP", non_production: false }
        })).ok()).toBeTruthy();
        expect((await (await request.get(`/backend/sessions/${sid}`)).json() as { masked: boolean }).masked).toBe(true);
        await page.locator(`[id$="--sessionList"]`).getByText(title, { exact: true }).click();
        await expect(page.locator(`[id$="--sessionTitle"]`)).toHaveText(title);
        const banner = page.locator(`[id$="--diagnoseBanner"]`);
        await expect(banner).toContainText(
            "This system is no longer flagged non-production: diagnose reads and runs are blocked. "
            + "Content already stored stays until the session is deleted or its retention ends.");
        await expect(banner).toHaveClass(/sapMMsgStripWarning/);
        await page.screenshot({ path: "test-results/diagnose-e2e-lost-flag.png", fullPage: true });

        const box = page.getByRole("alertdialog");
        const notAvailable = "The target system is not flagged as non-production, so this is not available there.";
        await page.locator(`[id$="--chatInput-inner"]`).fill("Look at the dumps of today");
        await page.locator(`[id$="--sendButton"]`).click();
        await expect(box).toContainText(notAvailable);
        await box.getByRole("button", { name: "Close" }).click();
        await expect(page.locator(`[id$="--chatInput-inner"]`)).toHaveValue("Look at the dumps of today");
        await page.locator(`[id$="--reportButton"]`).click();
        await expect(box).toContainText(notAvailable);
        await box.getByRole("button", { name: "Close" }).click();
        await expect(box).toHaveCount(0);

        const lostCard = page.locator(".ideApprovalCard");
        await expect(lostCard).toHaveCount(1);
        await lostCard.getByRole("button", { name: "Reject" }).click();
        await expect(lostCard).toHaveCount(0);
        await expect(page.locator(".ideApprovalDecided:visible").first())
            .toHaveText('Request rejected: nothing was changed in SAP (trace "Trace the slow order list call")');
        const after = await (await request.get(`/backend/sessions/${sid}/approvals`)).json() as { id: string; status: string; ttl_min?: number }[];
        expect(after.find((a) => a.id === pending)?.status).toBe("denied");
        expect(typeof after[0].ttl_min).toBe("number");
        // No chat message is written for a decision.
        expect(await (await request.get(`/backend/sessions/${sid}/messages`)).json()).toEqual([]);
    } finally {
        await request.put(`/backend/conventions/${TARGET}`, {
            data: { label: "E2E diagnose", package: "$TMP", non_production: true }
        });
    }

    expect(errors).toEqual([]);
});
