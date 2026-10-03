import FakeBackend from "../integration/FakeBackend";
import type { Approval } from "com/agent/ide/service/types";

/**
 * The fake's own behaviour where journeys rely on it standing in for the
 * server (plan 1c §1.2, variant B): approvals are stored rows, and what the
 * decide route answers follows from the stored state.
 */
const fake = new FakeBackend();
let sid = "";

async function call(method: string, path: string, body?: unknown): Promise<{ status: number; json: Record<string, unknown> }> {
    const response = await fetch(`backend/${path}`, {
        method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body)
    });
    const text = await response.text();
    let json: Record<string, unknown> = {};
    try {
        json = JSON.parse(text) as Record<string, unknown>;
    } catch {
        json = { raw: text };
    }
    return { status: response.status, json };
}

const approvals = async (): Promise<Approval[]> => (await call("GET", `sessions/${sid}/approvals`)).json as unknown as Approval[];
const decide = (aid: string, decision: string) => call("POST", `sessions/${sid}/approvals/${aid}`, { decision });

QUnit.module("FakeBackend: approvals as stored state", {
    beforeEach: function () {
        fake.reset();
        fake.allowDiagnose();
        sid = fake.addSession("Diagnose", [], [], "diagnose").id;
        fake.install();
    },
    afterEach: function () {
        fake.restore();
    }
});

QUnit.test("approve arms and stores the result; a second decision is 409 approval_not_pending", async function (assert) {
    const a = fake.addApproval(sid);
    const ok = await decide(a.id, "approve");
    assert.strictEqual(ok.status, 200);
    assert.strictEqual(ok.json.status, "approved");
    assert.ok((ok.json.result as { trace_request_id?: string }).trace_request_id, "with a trace request id");
    const again = await decide(a.id, "deny");
    assert.strictEqual(again.status, 409);
    assert.strictEqual(again.json.code, "approval_not_pending");
    assert.strictEqual((await approvals())[0].status, "approved", "the stored row is unchanged");
});

QUnit.test("a pending approval past its expiry lists as pending; deciding it answers 410 and stores 'expired'", async function (assert) {
    const a = fake.addApproval(sid, { expiresAt: "2000-01-01T00:00:00Z" });
    assert.strictEqual((await approvals())[0].status, "pending", "still pending until someone decides");
    const late = await decide(a.id, "approve");
    assert.strictEqual(late.status, 410);
    assert.strictEqual(late.json.code, "approval_expired");
    assert.strictEqual((await approvals())[0].status, "expired", "the row is expired from then on");
    assert.strictEqual((await decide(a.id, "approve")).status, 410, "and stays refused");
    assert.strictEqual((await approvals())[0].result, null, "nothing was armed");
});

QUnit.test("a scripted status is the stored status", async function (assert) {
    fake.addApproval(sid, { status: "expired" });
    assert.strictEqual((await approvals())[0].status, "expired");
});

QUnit.test("an approve rechecks non_production: 403 and the row stays pending; a deny always succeeds", async function (assert) {
    const a = fake.addApproval(sid);
    fake.conventions.forEach((c) => { c.non_production = false; });
    const refused = await decide(a.id, "approve");
    assert.strictEqual(refused.status, 403);
    assert.strictEqual(refused.json.code, "target_not_non_production");
    assert.strictEqual((await approvals())[0].status, "pending");
    // agents/ide/approvals.py decide: a deny sends nothing, so the flag cannot block it.
    const denied = await decide(a.id, "deny");
    assert.strictEqual(denied.status, 200, "deny is not refused by the flag");
    assert.strictEqual(denied.json.status, "denied");
});

QUnit.test("pending and expiry are checked before the flag, as the server does", async function (assert) {
    const late = fake.addApproval(sid, { expiresAt: "2000-01-01T00:00:00Z" });
    const done = fake.addApproval(sid, { status: "denied" });
    fake.conventions.forEach((c) => { c.non_production = false; });
    assert.strictEqual((await decide(late.id, "approve")).json.code, "approval_expired", "410 before 403");
    assert.strictEqual((await decide(late.id, "deny")).status, 410, "an expired row cannot be denied either");
    assert.strictEqual((await decide(done.id, "approve")).json.code, "approval_not_pending", "409 before 403");
});

QUnit.test("a decision writes no chat message: the row is the record", async function (assert) {
    const messages = async (): Promise<unknown[]> => (await call("GET", `sessions/${sid}/messages`)).json as unknown as unknown[];
    await decide(fake.addApproval(sid).id, "approve");
    await decide(fake.addApproval(sid).id, "deny");
    fake.failArming = "arc1_timeout_unknown";
    await decide(fake.addApproval(sid).id, "approve");
    assert.deepEqual(await messages(), [], "approved, denied and failed: no system message");
});

QUnit.test("failArming: the approve answers 200 with status failed and the error code", async function (assert) {
    const a = fake.addApproval(sid);
    fake.failArming = "arc1_timeout_unknown";
    const failed = await decide(a.id, "approve");
    assert.strictEqual(failed.status, 200, "a failed arming is a decided approval, not an HTTP error");
    assert.strictEqual(failed.json.status, "failed");
    assert.strictEqual(failed.json.error_code, "arc1_timeout_unknown");
    assert.ok((failed.json.result as { note?: string }).note, "with the note that it may have been armed");
    assert.strictEqual((await approvals())[0].status, "failed");
    // A deny never arms, so it cannot fail that way.
    const b = fake.addApproval(sid);
    assert.strictEqual((await decide(b.id, "deny")).json.status, "denied");
});

QUnit.test("armingWithoutResult: approved with result null (outcome unknown)", async function (assert) {
    const a = fake.addApproval(sid);
    fake.armingWithoutResult = true;
    const answer = await decide(a.id, "approve");
    assert.strictEqual(answer.json.status, "approved");
    assert.strictEqual(answer.json.result, null);
});

QUnit.test("scripted finding / approval_required / refused-proposal frames only reach a diagnose session", async function (assert) {
    const script = {
        findings: [{ kind: "dump" as const, ref_id: "D-1", title: "Dump" }], approval: {}, proposalRefused: "too_many_pending"
    };
    fake.scriptRun(script);
    const change = fake.sessions[0].session.id;
    const changeRun = (await call("POST", `sessions/${change}/messages`, { text: "hi" })).json.raw as string;
    assert.notOk(/event: (finding|approval_required)/.test(changeRun), "a change session gets no diagnose frames");
    assert.notOk(changeRun.includes("too_many_pending"));
    assert.strictEqual(fake.dataOf(change)?.approvals.length, 0, "and stores no approval");
    assert.strictEqual(fake.dataOf(change)?.findings.length, 0);

    fake.scriptRun(script);
    const run = (await call("POST", `sessions/${sid}/messages`, { text: "hi" })).json.raw as string;
    assert.ok(/event: finding/.test(run) && /event: approval_required/.test(run), "the diagnose session gets them");
    assert.ok(/event: tool\ndata: [^\n]*"code":"too_many_pending"/.test(run), "and the refused proposal as a tool event with its code");
    assert.strictEqual((await approvals()).length, 1);
});

QUnit.test("handover rechecks non_production after not_diagnose", async function (assert) {
    fake.conventions.forEach((c) => { c.non_production = false; });
    const refused = await call("POST", `sessions/${sid}/handover`);
    assert.strictEqual(refused.status, 409, "agents/ide/routes.py answers 409 on handover, not 403");
    assert.strictEqual(refused.json.code, "target_not_non_production");
});

QUnit.test("session JSON carries masked as the conventions say now; a masked diagnose session refuses runs and live reads with 409", async function (assert) {
    fake.dataOf(sid)?.findings.push({
        id: "f-1", kind: "dump", ref_id: "D-1", title: "Dump", program: "ZDEMO", include: null, line: 3,
        occurred_at: null, created_at: "2026-10-03T08:00:00Z"
    });
    assert.strictEqual((await call("GET", `sessions/${sid}`)).json.masked, false, "flagged non-production: not masked");
    fake.conventions.forEach((c) => { c.non_production = false; });
    assert.strictEqual((await call("GET", "sessions/s-1")).json.masked, true, "every session on a target without the flag reads as masked");
    assert.strictEqual((await call("GET", `sessions/${sid}`)).json.masked, true, "the flag was removed: masked");
    const refusals = [
        await call("POST", `sessions/${sid}/messages`, { text: "hi" }),
        await call("POST", `sessions/${sid}/report`),
        await call("GET", `sessions/${sid}/findings/f-1?refresh=true`),
        await call("GET", `sessions/${sid}/findings/f-1`),
        await call("POST", `sessions/${sid}/findings/f-1/open`),
        await call("POST", `sessions/${sid}/open`, { type: "CLAS", name: "ZCL_DEMO" })
    ];
    assert.deepEqual(refusals.map((r) => `${r.status} ${String(r.json.code)}`),
        Array(6).fill("409 target_not_non_production"), "runs, reports, the detail (live or stored) and reads from SAP are refused");
    assert.strictEqual((await call("GET", `sessions/${sid}/findings`)).status, 200, "the finding list is still served");
    assert.strictEqual((await call("GET", `sessions/${sid}/messages`)).status, 200, "and the messages");
    assert.strictEqual((await call("POST", "sessions/s-1/messages", { text: "hi" })).status, 200, "a change session is not affected");
    assert.strictEqual((await call("DELETE", `sessions/${sid}`)).status, 204, "and the session can be deleted");
});
