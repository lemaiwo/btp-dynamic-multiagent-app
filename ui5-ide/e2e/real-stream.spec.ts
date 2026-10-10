import { test, expect, type Page, type Response } from "@playwright/test";
import { byId, clickWhenSettled, collectErrors, field, openApp, saveCommentAndExpectClosed } from "./support/backend";

// Real-stream e2e (run with `npm run test:e2e:stream`, playwright.stream.config.ts).
//
// The browser drives the REAL backend: the real runner, stages, SSE, comments,
// request-changes, base check, syntax check, findings and approvals. Only two
// things are stand-ins, both inside ../tests/e2e/ide_stream_server.py: a
// scripted pydantic-ai FunctionModel in place of AI Core, and a fake SAP
// system in place of ARC-1. No request is intercepted in this spec and no row
// is written into the database: everything on screen came through the UI's
// own requests. The SSE bodies are read passively (page.waitForResponse) to
// check the frames the UI consumed.
//
// Texts below are the server script's constants (see the E1 report).
const TARGET = "DEMO";
const CHAT_ANSWER_START = "ZCL_DEMO_EXISTING computes a total for the demo report.";
const DESIGN_V1_MARK = "Version 1 of the design.";
const DESIGN_V2_MARK = "Version 2 of the design: the review comments are addressed.";
const COMMENT_ANSWER = "Done: the design now names the package and the new class.";
const EXISTING = "ZCL_DEMO_EXISTING";
const NEW = "ZCL_DEMO_NEW";
const EXISTING_PATH = "src/CLAS/zcl_demo_existing.clas.abap";
const NEW_PATH = "src/CLAS/zcl_demo_new.clas.abap";
const VERSION_MARKER = "rev-1";
const SYNTAX_MESSAGE = 'Statement "SYNTAXERROR" is unknown.';
const DUMP_ID = "DUMP-20261003-0001";
const DUMP_ERROR = "COMPUTE_INT_ZERODIVIDE";
const TRACE_REQUEST_ID = "REQ-E2E-0001";

interface Frame { event: string; data: Record<string, unknown> }

function parseSse(body: string): Frame[] {
    return body.split(/\n\n/).map((block) => {
        let event = "message";
        const data: string[] = [];
        for (const line of block.split("\n")) {
            if (line.startsWith("event:")) {
                event = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
                data.push(line.slice(5).trim());
            }
        }
        return data.length ? { event, data: JSON.parse(data.join("\n")) as Record<string, unknown> } : null;
    }).filter((f): f is Frame => f !== null);
}

/** The POST the UI sends to `/sessions/{sid}/<route>`, as a promise of its response. */
function postTo(page: Page, route: string): Promise<Response> {
    return page.waitForResponse((r) => r.request().method() === "POST"
        && /\/backend\/sessions\/[^/]+\/[a-z-]+$/.test(new URL(r.url()).pathname)
        && new URL(r.url()).pathname.endsWith(`/${route}`), { timeout: 60_000 });
}

/** Reads a finished SSE answer and checks its framing: `run` first, `done` last, nothing in error. */
async function frames(response: Response): Promise<Frame[]> {
    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/event-stream");
    const all = parseSse(await response.text()).filter((f) => f.event !== "ping");
    expect(all[0]?.event).toBe("run");
    expect(all[all.length - 1]).toMatchObject({ event: "done", data: { status: "idle" } });
    expect(all.filter((f) => f.event === "error")).toEqual([]);
    return all;
}

/** Types a message into the composer, sends it and returns the run's frames once the stream ended. */
async function sendMessage(page: Page, text: string): Promise<Frame[]> {
    await field(page, "chatInput").fill(text);
    await expect(byId(page, "sendButton")).toBeEnabled();
    const answered = postTo(page, "messages");
    await byId(page, "sendButton").click();
    const result = await frames(await answered);
    // The page has taken in the end of the run as well.
    await expect(byId(page, "stopButton")).toBeHidden();
    return result;
}

async function sessionOf(page: Page, sid: string): Promise<Record<string, unknown>> {
    const response = await page.request.get(`/backend/sessions/${sid}`);
    expect(response.ok()).toBeTruthy();
    return await response.json() as Record<string, unknown>;
}

function sessionIdOf(page: Page): string {
    const sid = /#\/sessions\/([^/?]+)/.exec(page.url())?.[1];
    expect(sid, page.url()).toBeTruthy();
    return sid as string;
}

/** Creates a session through the worklist's New Session dialog and lands on its page. */
async function createInUi(page: Page, title: string, type: "Change" | "Diagnose"): Promise<string> {
    await openApp(page);
    await byId(page, "worklistNewButton").click();
    const dialog = byId(page, "newSessionDialog");
    await expect(dialog).toBeVisible();
    await byId(page, "newSessionType").getByText(type, { exact: true }).click();
    await field(page, "newSessionTitle").fill(title);
    // The stream server knows one target (GET /me), and the dialog preselects a single target.
    await expect(byId(page, "newSessionTarget").locator(".sapMSltLabel")).toHaveText(TARGET);
    await byId(page, "newSessionCreate").click();
    await expect(dialog).toBeHidden();
    await expect(byId(page, "sessionTitle")).toHaveText(title);
    await expect(byId(page, "sessionType").locator(".sapMObjStatusText")).toHaveText(type);
    return sessionIdOf(page);
}

test.describe("real-stream", () => {
    test("change session: chat, design with a review round, plan, propose, review, done", async ({ page }) => {
        const errors = collectErrors(page);
        const title = `Count open items ${Date.now()}`;
        const sid = await createInUi(page, title, "Change");

        // Chat: the answer streams in as text frames.
        const chat = await sendMessage(page, "What does ZCL_DEMO_EXISTING do?");
        expect(chat.filter((f) => f.event === "text").length).toBeGreaterThanOrEqual(5);
        await expect(byId(page, "messageList")).toContainText(CHAT_ANSWER_START);

        // Chat -> design.
        await expect(byId(page, "primaryAction")).toHaveText("Start design");
        await clickWhenSettled(byId(page, "primaryAction"));
        await expect.poll(async () => (await sessionOf(page, sid)).stage).toBe("design");

        // Design: submit_document stores v1 mid-run (artifact before done) and the document view shows it.
        const design = await sendMessage(page, "Draft the design.");
        const events = design.map((f) => f.event);
        expect(events.indexOf("artifact")).toBeGreaterThan(events.indexOf("text"));
        expect(design.find((f) => f.event === "artifact")?.data).toMatchObject({ kind: "design", version: 1 });
        // The document column opens from the stage token (a run started from the conversation keeps the layout).
        const doc = byId(page, "documentView");
        await clickWhenSettled(page.getByRole("button", { name: /^Design\b/ }));
        await expect(doc).toBeVisible();
        expect(page.url()).toMatch(/view=document/);
        await expect(doc).toContainText(DESIGN_V1_MARK);
        await expect(byId(page, "primaryAction")).toHaveText("Approve design (v1) and plan");

        // A comment on block 1 ("Version 1 of the design.") blocks approve with the reason.
        const block = doc.locator(".ideDocRow").nth(1);
        await expect(block).toContainText(DESIGN_V1_MARK);
        await block.locator(".ideDocMarker").click();
        await field(page, "commentDraft").fill("Name the package and the new class.");
        await saveCommentAndExpectClosed(page);
        await expect(block).toContainText("Open");
        await expect(byId(page, "requestChangesButton")).toHaveText("Request changes (1)");
        await expect(byId(page, "primaryAction")).toBeDisabled();
        await expect(byId(page, "primaryReason")).toHaveText("Resolve or dismiss the 1 open review comment first.");

        // Request changes: the comment goes sent -> addressed in the stream, design v2 arrives.
        await byId(page, "requestChangesButton").click();
        await expect(byId(page, "requestChangesDialog")).toBeVisible();
        await expect(byId(page, "requestChangesList")).toContainText("Name the package and the new class.");
        const reworked = postTo(page, "request-changes");
        await byId(page, "requestChangesSend").click();
        const rc = await frames(await reworked);
        const states = rc.filter((f) => f.event === "comments").map((f) => f.data.state);
        expect(states).toEqual(["sent", "addressed"]);
        expect(rc.find((f) => f.event === "artifact")?.data).toMatchObject({ kind: "design", version: 2 });
        await expect(byId(page, "stopButton")).toBeHidden();
        await expect(doc).toContainText(DESIGN_V2_MARK);
        await expect(byId(page, "docVersions")).toContainText("v2");
        await expect(page.getByText(`Assistant: ${COMMENT_ANSWER}`).first()).toBeVisible();

        // Approve v2 -> plan.
        await expect(byId(page, "primaryAction")).toHaveText("Approve design (v2) and plan");
        await expect(byId(page, "primaryAction")).toBeEnabled();
        await clickWhenSettled(byId(page, "primaryAction"));
        await expect.poll(async () => (await sessionOf(page, sid)).stage).toBe("plan");
        expect((await sessionOf(page, sid)).pins).toMatchObject({ design: 2 });

        // Plan -> propose.
        const plan = await sendMessage(page, "Plan the change.");
        expect(plan.some((f) => f.event === "plan")).toBe(true);
        expect(plan.find((f) => f.event === "artifact")?.data).toMatchObject({ kind: "plan", version: 1 });
        await expect(byId(page, "primaryAction")).toHaveText("Approve plan (v1) and propose changes");
        await expect(byId(page, "primaryAction")).toBeEnabled();
        await clickWhenSettled(byId(page, "primaryAction"));
        await expect.poll(async () => (await sessionOf(page, sid)).stage).toBe("propose");

        // Propose: file events from open_object, the run-end base check and syntax dry run.
        const propose = await sendMessage(page, "Propose the changes.");
        const tools = propose.filter((f) => f.event === "tool").map((f) => `${String(f.data.tool)}:${String(f.data.status)}`);
        expect(tools).toEqual(expect.arrayContaining(["open_object:ok", "check_sap_base:ok", "check_syntax:ok"]));
        const lastCheck = propose.findIndex((f) => f.event === "tool" && f.data.tool === "check_syntax" && f.data.status === "ok");
        expect(propose.slice(lastCheck).filter((f) => f.event === "file")).toHaveLength(2);
        const files = propose.filter((f) => f.event === "file").map((f) => f.data);
        expect(files[files.length - 2]).toMatchObject({ path: EXISTING_PATH, state: "modified", revision: 1, base_status: "sap", syntax_status: "ok" });
        expect(files[files.length - 1]).toMatchObject({ path: NEW_PATH, state: "new", revision: 1, base_status: "absent", syntax_status: "errors" });

        // The primary action takes the developer to the changes view first.
        // Approve of changes happens only in the changes view: elsewhere the primary action opens it.
        await expect(byId(page, "primaryAction")).toHaveText("Review changes");
        await clickWhenSettled(byId(page, "primaryAction"));
        await expect(byId(page, "changesView")).toBeVisible();
        expect(page.url()).toMatch(/view=changes/);
        await expect(byId(page, "primaryAction")).toHaveText("Approve changes and review");
        await expect(byId(page, "changesSummary")).toContainText("2 objects");
        const cards = byId(page, "changesView").locator(".ideChangesObject");
        const existing = cards.filter({ has: page.locator(`[data-sap-ui*="--changesObjectName-"]`, { hasText: new RegExp(`^${EXISTING}$`) }) });
        const created = cards.filter({ has: page.locator(`[data-sap-ui*="--changesObjectName-"]`, { hasText: new RegExp(`^${NEW}$`) }) });
        await expect(existing).toHaveCount(1);
        await expect(created).toHaveCount(1);
        await expect(existing).toContainText(`Changed vs SAP version ${VERSION_MARKER}`);
        await expect(existing).toContainText("No syntax messages");
        await expect(existing.locator("tr.ideDiffDel")).not.toHaveCount(0);
        await expect(created).toContainText("New in SAP");
        await expect(created).toContainText("1 syntax error");
        await expect(created.locator('[data-sap-ui*="--changesSyntaxMessage-"]')).toHaveText(`Error, line 8: ${SYNTAX_MESSAGE}`);
        await expect(created.locator("tr[data-side='new'][data-line='8']")).toContainText("SYNTAXERROR.");
        await page.screenshot({ path: "test-results/real-stream-changes.png", fullPage: true });

        // A comment on the error line blocks "Approve changes" with the reason until it is dismissed.
        await created.locator("tr[data-side='new'][data-line='8'] .ideDiffCode").click();
        const commentButton = created.locator("button[data-sap-ui*='--changesCommentButton-']");
        await expect(commentButton).toHaveText("Comment on line 8");
        await commentButton.click();
        await field(page, "commentDraft").fill("Remove the stray statement.");
        await saveCommentAndExpectClosed(page);
        await expect(created.locator(".ideDocComments")).toContainText("Remove the stray statement.");
        await expect(byId(page, "primaryAction")).toBeDisabled();
        await expect(byId(page, "primaryReason")).toHaveText("Resolve or dismiss the 1 open review comment first.");
        const refused = await page.request.post(`/backend/sessions/${sid}/approve`, { data: {} });
        expect(refused.status()).toBe(409);
        expect((await refused.json() as { code: string }).code).toBe("open_comments");

        await commentButton.click();
        const popover = byId(page, "commentPopover");
        await expect(popover).toBeVisible();
        await expect(popover).toContainText("Remove the stray statement.");
        await popover.locator("button[data-sap-ui*='--commentDismiss-']").click();
        await expect(popover.locator(".sapMObjStatus[data-sap-ui*='--commentPopoverState-']")).toHaveText("Dismissed");
        await byId(page, "commentCancel").click();
        await expect(popover).toBeHidden();
        await expect(byId(page, "requestChangesButton")).toHaveText("Request changes");

        // Approve from the changes view: the request names the revisions shown.
        await expect(byId(page, "primaryAction")).toBeEnabled();
        const approved = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/sessions/${sid}/approve`));
        await clickWhenSettled(byId(page, "primaryAction"));
        expect((await approved).postDataJSON()).toMatchObject({ revisions: { [EXISTING_PATH]: 1, [NEW_PATH]: 1 } });
        await expect.poll(async () => (await sessionOf(page, sid)).stage).toBe("review");

        // Review -> done.
        const review = await sendMessage(page, "Review the changes.");
        expect(review.find((f) => f.event === "artifact")?.data).toMatchObject({ kind: "review", version: 1 });
        await expect(byId(page, "primaryAction")).toHaveText("Finish session (review v1)");
        await expect(byId(page, "primaryAction")).toBeEnabled();
        await clickWhenSettled(byId(page, "primaryAction"));
        await expect.poll(async () => (await sessionOf(page, sid)).stage).toBe("done");
        await expect(byId(page, "primaryAction")).toHaveText("Session finished");

        expect(errors).toEqual([]);
    });

    test("diagnose session: finding and trace proposal live, approve (fake arm), report, hand over", async ({ page }) => {
        const errors = collectErrors(page);
        const title = `Zero divide ${Date.now()}`;
        const sid = await createInUi(page, title, "Diagnose");
        await expect(byId(page, "primaryAction")).toHaveText("Create report");

        const run = await sendMessage(page, "Why does the demo report dump?");
        const finding = run.find((f) => f.event === "finding");
        expect(finding?.data).toMatchObject({ kind: "dump", ref_id: DUMP_ID });
        expect(run.find((f) => f.event === "approval_required")).toBeTruthy();
        await expect(byId(page, "diagnoseFindingsLink")).toHaveText("Findings (1)");

        // The trace proposal: every server-built value, Approve and Reject, no default action.
        const card = page.locator(".ideApprovalCard:visible");
        await expect(card).toHaveCount(1);
        await expect(card).toContainText(`Arm a profiler trace on ${TARGET}?`);
        for (const line of ["HTTP request", "URL", "Trace the demo report", "Your own SAP user"]) {
            await expect(card).toContainText(line);
        }
        expect(await page.evaluate(() => !!document.activeElement?.closest(".ideApprovalCard"))).toBe(false);
        await page.screenshot({ path: "test-results/real-stream-approval.png", fullPage: true });
        await clickWhenSettled(card.getByRole("button", { name: /Approve/ }));
        await expect(page.locator(".ideApprovalDecided:visible").filter({ hasText: `Trace armed: request ${TRACE_REQUEST_ID}` })).toHaveCount(1);
        const approvals = await (await page.request.get(`/backend/sessions/${sid}/approvals`)).json() as { status: string }[];
        expect(approvals.map((a) => a.status)).toEqual(["approved"]);

        // The finding in the list.
        await byId(page, "diagnoseFindingsLink").click();
        await expect(byId(page, "findingsList")).toContainText(DUMP_ERROR);

        // Report (a real report run) -> hand over.
        const reported = postTo(page, "report");
        await clickWhenSettled(byId(page, "primaryAction"));
        const report = await frames(await reported);
        expect(report.find((f) => f.event === "artifact")?.data).toMatchObject({ kind: "report", version: 1 });
        await expect(byId(page, "primaryAction")).toHaveText("Hand over to a change");
        await expect(byId(page, "diagnoseReportLink")).toBeVisible();
        await clickWhenSettled(byId(page, "primaryAction"));
        const confirm = page.getByRole("alertdialog");
        await expect(confirm).toContainText("Start a change session from this report?");
        await confirm.getByRole("button", { name: "OK" }).click();
        await expect(byId(page, "sessionTitle")).toHaveText(`Change: ${title}`);
        await expect(byId(page, "sessionType").locator(".sapMObjStatusText")).toHaveText("Change");
        await expect(byId(page, "documentView").locator("h1")).toHaveText("Diagnosis");

        expect(errors).toEqual([]);
    });
});
