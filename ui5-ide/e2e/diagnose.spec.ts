import { test, expect } from "@playwright/test";
import {
    byId, clickWhenSettled, collectErrors, createSession, ensureTarget, fulfillJson, fulfillSse, getSession, insertArtifact, insertMessage,
    openApp, quote, sql, uuid
} from "./support/backend";

// A diagnose session on the session page: findings, a finding's source at its
// line, the details dialog, trace approval cards, the report and the handover.
//
// Real backend: target flag (POST/PUT conventions), diagnose create (422 on an
// unflagged target), session detail, messages, GET /findings and the stored
// finding detail, GET /approvals, Reject (a denial sends nothing to SAP), the
// handover that starts the change session, the lost-flag refusals.
// Written into SQLite (no model and no ARC-1 locally): the findings and trace
// proposals a diagnose run would leave.
// page.route, only where SAP or a model would answer:
//   - POST /findings/{fid}/open (reads the program from SAP): the handler
//     stores the workspace file as the route would and answers its JSON;
//   - POST /approvals/{aid} for an Approve (a Reject goes to the real route) (arms the trace in SAP): one answers
//     "armed", one "failed, outcome unknown" (arc1_timeout_unknown), each also
//     written to SQLite so a reload agrees;
//   - POST /report (a model run): stores the report and answers the SSE frames.
const TARGET = "DEMO_DIAG";
const PROGRAM = "ZDEMO_ORDER_LIST";
const SOURCE = Array.from({ length: 12 }, (_, i) => (i === 6 ? "    LOOP AT items INTO DATA(item)." : `    " line ${i + 1}`)).join("\n") + "\n";
const DUMP_DETAIL = "Runtime error: TIME_OUT\nProgram: ZDEMO_ORDER_LIST, line 7\nThe program exceeded the time limit.";
const REPORT_MD = "# Slow order list\n\n**Root cause:** a loop reads every order item.\n\n- 412 identical SELECTs";

function params(description: string) {
    return { processType: "http", objectType: "url", maxExecutions: 2, expiresHours: 1, sqlTrace: true, aggregate: true, description };
}

function seedApproval(sid: string, description: string, minutesAgo: number, body: object = params(description)): string {
    const id = uuid();
    sql(`INSERT INTO ide_approvals (id, session_id, action, params_json, status, created_at)
         VALUES (${quote(id)}, ${quote(sid)}, 'trace_start', ${quote(JSON.stringify(body))}, 'pending',
                 datetime('now', '-${minutesAgo} minutes'))`);
    return id;
}

function seedFinding(sid: string, kind: string, ref: string, title: string, program: string | null, line: number | null, detail: string | null): string {
    const id = uuid();
    sql(`INSERT INTO ide_findings (id, session_id, kind, ref_id, title, program, include, line, occurred_at, detail)
         VALUES (${quote(id)}, ${quote(sid)}, ${quote(kind)}, ${quote(ref)}, ${quote(title)},
                 ${program ? quote(program) : "NULL"}, NULL, ${line ?? "NULL"}, '2026-10-03T08:00:00Z',
                 ${detail ? quote(detail) : "NULL"})`);
    return id;
}

function approvalRow(id: string) {
    const row = sql(`SELECT action, params_json, status, result_json, error_code, created_at, decided_at
                     FROM ide_approvals WHERE id = ${quote(id)}`).split("|");
    return {
        id, action: row[0], params: JSON.parse(row[1]), status: row[2], result: row[3] ? JSON.parse(row[3]) : null,
        error_code: row[4] || null, created_at: row[5], ttl_min: 30, decided_at: row[6] || null
    };
}

test("diagnose: findings, source at the line, approval cards, report and handover", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    // Only a flagged target takes a diagnose session.
    await ensureTarget(request, TARGET, false);
    const refused = await request.post("/backend/sessions", { data: { title: "refused", target: TARGET, type: "diagnose" } });
    expect(refused.status()).toBe(422);
    await ensureTarget(request, TARGET, true);
    const title = `Slow order list ${Date.now()}`;
    const session = await createSession(request, title, TARGET, "diagnose");
    const sid = session.id;
    expect(session.stage).toBe("investigate");
    insertMessage(sid, "investigate", "user", "Why is the order list slow?");
    insertMessage(sid, "investigate", "assistant", "I found a time-out dump. **A trace request is waiting for your approval.**");
    const dump = seedFinding(sid, "dump", "DUMP-0001", "TIME_OUT in ZDEMO_ORDER_LIST", PROGRAM, 7, DUMP_DETAIL);
    seedFinding(sid, "gateway_error", "GW-0001", "Gateway error without a program", null, null, null);
    const armed = seedApproval(sid, "Trace the slow order list call", 4);
    const rejected = seedApproval(sid, "Trace the posting job", 3);
    const unknown = seedApproval(sid, "Trace the export", 2);
    // A stored proposal whose parameters are incomplete: it can only be rejected.
    const invalid = seedApproval(sid, "", 1, { processType: "http" });

    // Approve arms a trace in SAP: answered here, and written as the route would.
    const decisions: string[] = [];
    await page.route(`**/backend/sessions/${sid}/approvals/*`, async (route) => {
        const aid = route.request().url().split("/").pop()!;
        const decision = (route.request().postDataJSON() as { decision: string }).decision;
        decisions.push(`${aid}:${decision}`);
        if (decision !== "approve") {
            return route.fallback();
        }
        if (aid === armed) {
            sql(`UPDATE ide_approvals SET status = 'approved', decided_at = CURRENT_TIMESTAMP,
                 result_json = ${quote(JSON.stringify({ trace_request_id: "TRC-0001", expires_at: "2026-10-04T10:00:00Z" }))}
                 WHERE id = ${quote(aid)}`);
        } else {
            sql(`UPDATE ide_approvals SET status = 'failed', decided_at = CURRENT_TIMESTAMP, error_code = 'arc1_timeout_unknown'
                 WHERE id = ${quote(aid)}`);
        }
        return fulfillJson(route, approvalRow(aid));
    });
    // Opening a finding reads its program from SAP: the workspace row is stored as the route would.
    const opened: string[] = [];
    await page.route(`**/backend/sessions/${sid}/findings/*/open`, async (route) => {
        opened.push(route.request().url());
        const path = `src/PROG/${PROGRAM.toLowerCase()}.prog.abap`;
        sql(`INSERT INTO ide_workspace_files (id, session_id, path, object_type, object_name, origin_source, state, revision, base_status)
             VALUES (${quote(uuid())}, ${quote(sid)}, ${quote(path)}, 'PROG', ${quote(PROGRAM)}, ${quote(SOURCE)}, 'read', 0, 'sap')`);
        return fulfillJson(route, {
            file: { path, state: "read", object_type: "PROG", object_name: PROGRAM, revision: 0, base_status: "sap", syntax_status: null },
            line: 7, hint: null
        });
    });
    // The report is a model run: the artifact and message it leaves, and its frames.
    await page.route(`**/backend/sessions/${sid}/report`, async (route) => {
        const aid = insertArtifact(sid, "investigate", "report", 1, REPORT_MD);
        const mid = insertMessage(sid, "investigate", "assistant", "Report written.");
        await fulfillSse(route, [
            ["run", { run_id: uuid(), stage: "investigate", message_id: mid }],
            ["text", { delta: "Report written." }],
            ["artifact", { id: aid, kind: "report", version: 1 }],
            ["usage", { requests_used: 3, request_cap: 200 }],
            ["done", { message_id: mid, stage: "investigate", status: "idle" }]
        ]);
    });

    const errors = collectErrors(page);
    await openApp(page, `sessions/${sid}`);
    await expect(byId(page, "sessionTitle")).toHaveText(title);
    await expect(byId(page, "sessionType").locator(".sapMObjStatusText")).toHaveText("Diagnose");
    await expect(byId(page, "diagnoseBannerText")).toContainText("sent to the AI model as they are");
    await expect(byId(page, "stageTimeline")).toBeHidden();
    await expect(byId(page, "primaryAction")).toHaveText("Create report");

    // Three cards: what would be armed, for whom, Approve and Reject; no default action.
    const cards = page.locator(".ideApprovalCard:visible");
    await expect(cards).toHaveCount(4);
    const first = cards.filter({ hasText: "Trace the slow order list call" });
    await expect(first).toContainText(`Arm a profiler trace on ${TARGET}?`);
    for (const line of ["HTTP request", "URL", "Your own SAP user", "The trace runs for your own SAP user only and expires automatically."]) {
        await expect(first).toContainText(line);
    }
    expect(await page.evaluate(() => !!document.activeElement?.closest(".ideApprovalCard"))).toBe(false);
    await page.screenshot({ path: "test-results/e2e-diagnose-cards.png", fullPage: true });

    // Approve -> armed (answer as SAP would give it).
    await clickWhenSettled(first.getByRole("button", { name: /Approve/ }));
    await expect(page.locator(".ideApprovalDecided:visible").filter({ hasText: "Trace armed: request TRC-0001" })).toHaveCount(1);
    // Reject -> real backend; nothing is sent to SAP.
    await cards.filter({ hasText: "Trace the posting job" }).getByRole("button", { name: "Reject" }).click();
    await expect(page.locator(".ideApprovalDecided:visible").filter({ hasText: 'Request rejected: nothing was changed in SAP (trace "Trace the posting job")' }))
        .toHaveCount(1);
    // Approve -> SAP did not answer in time: never shown as armed.
    await cards.filter({ hasText: "Trace the export" }).getByRole("button", { name: /Approve/ }).click();
    await expect(page.locator(".ideApprovalDecided:visible").filter({ hasText: "SAP did not answer in time, so the trace may have been armed" }))
        .toHaveCount(1);
    // ...and says so in an error box as well.
    const box = page.getByRole("alertdialog");
    await expect(box).toContainText("SAP did not answer in time, so the trace may have been armed");
    await box.getByRole("button", { name: "Close" }).click();
    await expect(box).toHaveCount(0);
    // The incomplete request lists nothing to consent to and offers Reject only.
    const invalidCard = cards.filter({ hasText: "This request is not valid and cannot be approved" });
    await expect(invalidCard).toHaveCount(1);
    await expect(invalidCard.getByRole("button", { name: /Approve/ })).toHaveCount(0);
    await invalidCard.getByRole("button", { name: "Reject" }).click();
    await expect(cards).toHaveCount(0);
    expect(decisions).toEqual([`${armed}:approve`, `${rejected}:deny`, `${unknown}:approve`, `${invalid}:deny`]);
    const listed = await (await request.get(`/backend/sessions/${sid}/approvals`)).json() as { id: string; status: string }[];
    expect(Object.fromEntries(listed.map((a) => [a.id, a.status]))).toEqual({
        [armed]: "approved", [rejected]: "denied", [unknown]: "failed", [invalid]: "denied"
    });
    // A decided request is refused on the real route.
    const again = await request.post(`/backend/sessions/${sid}/approvals/${rejected}`, { data: { decision: "deny" } });
    expect(again.status()).toBe(409);
    await page.screenshot({ path: "test-results/e2e-diagnose-decided.png", fullPage: true });

    // Findings: the list in the artifact column, newest first; one without a program says so.
    await expect(byId(page, "diagnoseFindingsLink")).toHaveText("Findings (2)");
    await byId(page, "diagnoseFindingsLink").click();
    const findings = byId(page, "findingsList");
    await expect(findings.locator(".ideFinding")).toHaveCount(2);
    const noProgram = findings.locator(".ideFinding").filter({ hasText: "Gateway error without a program" });
    await expect(noProgram).toContainText("SAP recorded no program for this finding, so there is no source to open.");
    const dumpRow = findings.locator(".ideFinding").filter({ hasText: "TIME_OUT in ZDEMO_ORDER_LIST" });
    await expect(dumpRow).toContainText("Dump");

    // Details: the stored text, read-only.
    await dumpRow.getByRole("button", { name: "Details" }).click();
    const detail = byId(page, "findingDetailDialog");
    await expect(detail).toBeVisible();
    await expect(detail.locator("textarea")).toHaveValue(DUMP_DETAIL);
    await expect(detail.locator("textarea")).toHaveAttribute("readonly", /.*/);
    await byId(page, "findingDetailCloseButton").click();
    await expect(detail).toBeHidden();

    // A row opens the SAP source at the finding's line.
    await dumpRow.locator(".ideFindingTitle").click();
    await expect(byId(page, "sourceView")).toBeVisible();
    const highlighted = byId(page, "sourceView").locator("tr.ideSourceHighlight");
    await expect(highlighted).toHaveCount(1);
    await expect(highlighted).toHaveAttribute("data-line", "7");
    await expect(highlighted).toContainText("LOOP AT items INTO DATA(item).");
    await expect(byId(page, "sourceFindingNote")).toContainText("Line 7: TIME_OUT in ZDEMO_ORDER_LIST");
    expect(opened).toEqual([expect.stringContaining(`/findings/${dump}/open`)]);
    await expect(page).toHaveURL(/view=source/);
    await page.screenshot({ path: "test-results/e2e-diagnose-source.png", fullPage: true });
    await byId(page, "sourceBackToFindings").click();
    await expect(findings).toBeVisible();

    // Report (model run) -> handover (real): the change session opens with the report.
    expect((await request.post(`/backend/sessions/${sid}/handover`)).status()).toBe(409);
    await byId(page, "primaryAction").click();
    await expect(byId(page, "messageList")).toContainText("Report written.");
    await expect(byId(page, "primaryAction")).toHaveText("Hand over to a change");
    await expect(byId(page, "diagnoseReportLink")).toBeVisible();
    await byId(page, "primaryAction").click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toContainText("Start a change session from this report?");
    await confirm.getByRole("button", { name: "OK" }).click();
    await expect(byId(page, "sessionTitle")).toHaveText(`Change: ${title}`);
    await expect(byId(page, "sessionType").locator(".sapMObjStatusText")).toHaveText("Change");
    await expect(byId(page, "documentView").locator("h1")).toHaveText("Slow order list");
    const sessions = await (await request.get("/backend/sessions")).json() as { id: string; title: string; type: string; stage: string }[];
    const change = sessions.find((s) => s.title === `Change: ${title}`);
    expect(change).toMatchObject({ type: "change", stage: "chat" });
    await page.screenshot({ path: "test-results/e2e-diagnose-handover.png", fullPage: true });

    // The flag is taken away: the diagnose session says reads are blocked, finding details are off.
    try {
        await ensureTarget(request, TARGET, false);
        expect((await getSession(request, sid)).waiting).toBeNull();
        await openApp(page, `sessions/${sid}?view=findings`);
        await expect(byId(page, "lostFlagStrip")).toContainText("no longer flagged non-production");
        await expect(byId(page, "findingsLostReason")).toBeVisible();
        const details = byId(page, "findingsList").getByRole("button", { name: "Details" });
        await expect(details).toHaveCount(2);
        for (const button of await details.all()) {
            await expect(button).toBeDisabled();
        }
        const blocked = await request.post(`/backend/sessions/${sid}/findings/${dump}/open`);
        expect(blocked.status()).toBe(409);
        expect((await blocked.json() as { code: string }).code).toBe("target_not_non_production");
    } finally {
        await ensureTarget(request, TARGET, true);
    }

    expect(errors).toEqual([]);
});
