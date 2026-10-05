import { test, expect } from "@playwright/test";
import {
    approve, byId, collectErrors, createSession, ensureTarget, field, fulfillSse, getSession, insertArtifact,
    insertMessage, openApp, quote, saveCommentAndExpectClosed, sql, uuid
} from "./support/backend";

// Document review on the session page: comment on a design paragraph,
// request changes, see the comment addressed, approve the new version.
//
// Real backend: the session, its stage (approve route), the comment (POST
// /comments), the comment list, the session detail, the approve that moves
// design -> plan, the worklist marker.
// Written into SQLite (no model locally): design v1 before the page opens.
// page.route: only POST /request-changes, the model run. Its handler does
// what the run would leave (comment `addressed` with an answer, design v2,
// the two messages) in SQLite and answers the run's SSE frames.
const TARGET = "DEMO_REVIEW";
const DESIGN_V1 = "# Order check design\n\nThe class checks every order item in a loop.\n\n"
    + "## Approach\n\nRead the items once with one SELECT and check them in memory.";
const DESIGN_V2 = "# Order check design\n\nThe class checks the order items in one pass.\n\n"
    + "## Approach\n\nRead the items once with one SELECT, keyed by item number, and check them in memory.";
const COMMENT = "Key the item table by item number so the check is a read, not a loop.";
const ANSWER = "Keyed by item number in the Approach section.";
const NOTE = "Keep the public method unchanged.";

test("document comment -> request changes -> addressed -> approve, on the real backend", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await ensureTarget(request, TARGET);
    const title = `Review design ${Date.now()}`;
    const session = await createSession(request, title, TARGET);
    const sid = session.id;
    expect((await approve(request, sid)).stage).toBe("design");
    insertArtifact(sid, "design", "design", 1, DESIGN_V1);

    let sentBody: unknown = null;
    await page.route(`**/backend/sessions/${sid}/request-changes`, async (route) => {
        sentBody = route.request().postDataJSON();
        const ids = sql(`SELECT id FROM ide_comments WHERE session_id = ${quote(sid)} AND state = 'open'`).split("\n").filter(Boolean);
        const runId = uuid();
        // What the run leaves: the comments addressed, design v2, the user turn and the answer.
        sql(`UPDATE ide_comments SET state = 'addressed', answer = ${quote(ANSWER)}, sent_run_id = ${quote(runId)}
             WHERE session_id = ${quote(sid)} AND state = 'open'`);
        const v2 = insertArtifact(sid, "design", "design", 2, DESIGN_V2);
        insertMessage(sid, "design", "user", `Rework the design to address the review comments.\n\n${NOTE}`);
        const answerId = insertMessage(sid, "design", "assistant", "I reworked the design: the items are keyed by item number.");
        await fulfillSse(route, [
            ["run", { run_id: runId, stage: "design", message_id: answerId }],
            ["comments", { ids, state: "sent", left: 0 }],
            ["text", { delta: "I reworked the design: the items are keyed by item number." }],
            ["comments", { ids, state: "addressed" }],
            ["artifact", { id: v2, kind: "design", version: 2 }],
            ["usage", { requests_used: 2, request_cap: 200 }],
            ["done", { message_id: answerId, stage: "design", status: "idle" }]
        ]);
    });

    const errors = collectErrors(page);
    await openApp(page, `sessions/${sid}?view=document&kind=design`);
    await expect(byId(page, "sessionTitle")).toHaveText(title);
    const doc = byId(page, "documentView");
    await expect(doc.locator(".ideDocRow").first()).toContainText("Order check design");
    await expect(byId(page, "primaryAction")).toHaveText("Approve design (v1) and plan");

    // Comment on the second block (the paragraph about the loop).
    const block = doc.locator(".ideDocRow").nth(1);
    await expect(block).toContainText("every order item in a loop");
    await block.locator(".ideDocMarker").click();
    const popover = byId(page, "commentPopover");
    await expect(popover).toBeVisible();
    await field(page, "commentDraft").fill(COMMENT);
    await saveCommentAndExpectClosed(page);
    await expect(block).toContainText(COMMENT);
    await expect(block).toContainText("Open");

    const stored = await (await request.get(`/backend/sessions/${sid}/comments`)).json() as
        { anchor: string; kind: string; version: number; paragraph: number; state: string; body: string }[];
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ anchor: "document", kind: "design", version: 1, paragraph: 1, state: "open", body: COMMENT });
    // An open comment blocks approve, on the server too.
    await expect(byId(page, "requestChangesButton")).toHaveText("Request changes (1)");
    await expect(byId(page, "primaryAction")).toBeDisabled();
    const refused = await request.post(`/backend/sessions/${sid}/approve`, { data: {} });
    expect(refused.status()).toBe(409);
    expect((await refused.json() as { code: string }).code).toBe("open_comments");
    await page.screenshot({ path: "test-results/e2e-review-comment.png", fullPage: true });

    // Request changes: the dialog lists the open comment, the note goes along.
    await byId(page, "requestChangesButton").click();
    const rc = byId(page, "requestChangesDialog");
    await expect(rc).toBeVisible();
    await expect(byId(page, "requestChangesList")).toContainText(COMMENT);
    await field(page, "requestChangesNote").fill(NOTE);
    await byId(page, "requestChangesSend").click();
    await expect(rc).toBeHidden();
    expect(sentBody).toEqual({ note: NOTE });

    // Addressed, with the assistant's answer; v2 exists; the answer is in the conversation.
    await expect(byId(page, "messageList")).toContainText("I reworked the design: the items are keyed by item number.");
    await expect(page.getByText(`Assistant: ${ANSWER}`).first()).toBeVisible();
    await expect(byId(page, "docVersions")).toContainText("v2");
    const after = await getSession(request, sid);
    expect(after.open_comments).toBe(0);
    expect(after.unresolved_comments).toBe(0);
    expect(after.waiting).toBe("comments");
    await page.screenshot({ path: "test-results/e2e-review-addressed.png", fullPage: true });

    // Approve v2: the real approve pins design v2 and moves on to plan.
    await expect(byId(page, "primaryAction")).toHaveText("Approve design (v2) and plan");
    await expect(byId(page, "primaryAction")).toBeEnabled();
    await byId(page, "primaryAction").click();
    await expect.poll(async () => (await getSession(request, sid)).stage).toBe("plan");
    expect((await getSession(request, sid)).pins).toMatchObject({ design: 2 });

    expect(errors).toEqual([]);
});

// Regression (found by U16 at 10be0ff, fixed in bac3de4): pressing a
// paragraph's comment marker with the mouse and saving a new comment closed
// the popover and its afterClose opened it again with the saved text still in
// the draft, so a second Save stored a duplicate.
test("saving a new document comment closes the popover (it does not open again with the saved draft)", async ({ page, request }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await ensureTarget(request, TARGET);
    const session = await createSession(request, `Popover close ${Date.now()}`, TARGET);
    await approve(request, session.id);
    insertArtifact(session.id, "design", "design", 1, DESIGN_V1);
    const errors = collectErrors(page);
    await openApp(page, `sessions/${session.id}?view=document&kind=design`);
    const block = byId(page, "documentView").locator(".ideDocRow").nth(1);
    await block.locator(".ideDocMarker").click();
    await field(page, "commentDraft").fill(COMMENT);
    await saveCommentAndExpectClosed(page);
    await expect(block).toContainText(COMMENT);
    const stored = await (await request.get(`/backend/sessions/${session.id}/comments`)).json() as unknown[];
    expect(stored).toHaveLength(1);
    expect(errors).toEqual([]);
});
