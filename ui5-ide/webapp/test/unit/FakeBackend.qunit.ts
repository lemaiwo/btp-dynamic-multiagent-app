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

QUnit.test("session JSON carries target_non_production as the conventions say now; a diagnose session that lost the flag refuses runs and live reads with 409", async function (assert) {
    fake.dataOf(sid)?.findings.push({
        id: "f-1", kind: "dump", ref_id: "D-1", title: "Dump", program: "ZDEMO", include: null, line: 3,
        occurred_at: null, created_at: "2026-10-03T08:00:00Z"
    });
    const before = (await call("GET", `sessions/${sid}`)).json;
    assert.strictEqual(before.target_non_production, true, "flagged non-production");
    assert.notOk("masked" in before, "`masked` is not served (the contract has target_non_production)");
    // A file the session read earlier (as a finding's source would be): its refresh reads SAP.
    fake.dataOf(sid)!.files.push({
        path: "src/PROG/zdemo.prog.abap", state: "read", object_type: "PROG", object_name: "ZDEMO",
        origin_source: "REPORT zdemo.", proposed_source: ""
    });
    fake.conventions.forEach((c) => { c.non_production = false; });
    assert.strictEqual((await call("GET", "sessions/s-1")).json.target_non_production, false, "a target without the flag");
    assert.strictEqual((await call("GET", `sessions/${sid}`)).json.target_non_production, false, "the flag was removed");
    const refusals = [
        await call("POST", `sessions/${sid}/messages`, { text: "hi" }),
        await call("POST", `sessions/${sid}/report`),
        await call("GET", `sessions/${sid}/findings/f-1?refresh=true`),
        await call("GET", `sessions/${sid}/findings/f-1`),
        await call("POST", `sessions/${sid}/findings/f-1/open`),
        await call("POST", `sessions/${sid}/file/refresh?path=${encodeURIComponent("src/PROG/zdemo.prog.abap")}`)
    ];
    assert.deepEqual(refusals.map((r) => `${r.status} ${String(r.json.code)}`),
        Array(6).fill("409 target_not_non_production"), "runs, reports, the detail (live or stored) and reads from SAP are refused");
    assert.strictEqual((await call("GET", `sessions/${sid}/findings`)).status, 200, "the finding list is still served");
    assert.strictEqual((await call("GET", `sessions/${sid}/messages`)).status, 200, "and the messages");
    assert.strictEqual((await call("POST", "sessions/s-1/messages", { text: "hi" })).status, 200, "a change session is not affected");
    assert.strictEqual((await call("DELETE", `sessions/${sid}`)).status, 204, "and the session can be deleted");
});

// --- comments, revisions, pins, request changes (contract §1.1-1.3) -------------

const PATH = "src/CLAS/zcl_demo.clas.abap";
let cs = "";

QUnit.module("FakeBackend: comments, revisions, pins", {
    beforeEach: function () {
        fake.reset();
        cs = fake.sessions[0].session.id;
        fake.install();
    },
    afterEach: function () {
        fake.restore();
    }
});

const fileComment = (body = "Rename this", revision = 1): Record<string, unknown> =>
    ({ anchor: "file", path: PATH, revision, line_start: 1, line_end: 2, body });
const patch = (cid: string, b: unknown) => call("PATCH", `sessions/${cs}/comments/${cid}`, b);
const session = async (): Promise<Record<string, unknown>> => (await call("GET", `sessions/${cs}`)).json;

QUnit.test("a comment is created open, with every field of CommentOut", async function (assert) {
    fake.addRevision(cs, PATH, "CLASS zcl_demo DEFINITION PUBLIC.\n* v1");
    const created = await call("POST", `sessions/${cs}/comments`, fileComment());
    assert.strictEqual(created.status, 201);
    assert.strictEqual(created.json.state, "open");
    assert.deepEqual(
        Object.keys(created.json).sort(),
        ["anchor", "answer", "body", "created_at", "id", "kind", "line_end", "line_start", "paragraph", "path", "quote",
            "revision", "state", "updated_at", "version"],
        "unused anchor fields are present as null");
    assert.strictEqual(created.json.kind, null);
    const s = await session();
    assert.strictEqual(s.open_comments, 1);
    assert.strictEqual(s.unresolved_comments, 1);
});

QUnit.test("a quote is cleaned as the backend does: control and format characters out, collapsed, cut by code points, trimmed after the cut", async function (assert) {
    fake.addRevision(cs, PATH, "* v1");
    const create = (quote: unknown) => call("POST", `sessions/${cs}/comments`, { ...fileComment(), quote });
    const emoji = "\u{1F600}";
    const cleaned = await create(`  a\u0000b\u200Bc\u202E d\u0007\n\te  `);
    assert.strictEqual(cleaned.json.quote, "abc d e", "NUL, BEL, zero-width space and bidi override removed; whitespace collapsed");
    const long = await create(`${"x".repeat(199)}${emoji}rest`);
    assert.strictEqual(long.json.quote, `${"x".repeat(199)}${emoji}`, "200 code points, the pair whole");
    const blankAtCut = await create(`${"y".repeat(199)} z`);
    assert.strictEqual(blankAtCut.json.quote, "y".repeat(199), "trimmed after the cut");
    assert.strictEqual((await create("\u200B \u0001")).json.quote, null, "nothing left is no quote");
    assert.strictEqual((await create("a\u0085b")).json.quote, "a b", "U+0085 (NEL) is whitespace, as for Python's str.split()");
    const wrong = await create(5);
    assert.strictEqual(wrong.status, 422, "a quote that is not a string is refused on create");
    assert.strictEqual(wrong.json.code, "invalid_quote");
});

QUnit.test("an invalid anchor is 422 invalid_anchor", async function (assert) {
    fake.addRevision(cs, PATH, "* v1");
    const bad = [
        { ...fileComment(), path: "src/CLAS/zcl_other.clas.abap" },
        { ...fileComment(), revision: 2 },
        { ...fileComment(), line_start: 5, line_end: 4 },
        { anchor: "document", kind: "design", version: 1, paragraph: 0, body: "x" },
        { anchor: "document", kind: "design", version: 1, paragraph: -1, body: "x" }
    ];
    fake.addArtifact(cs, "plan");
    const codes = [];
    for (const b of bad) {
        const r = await call("POST", `sessions/${cs}/comments`, b);
        codes.push(`${r.status} ${String(r.json.code)}`);
    }
    assert.deepEqual(codes, Array(5).fill("422 invalid_anchor"));
    const empty = await call("POST", `sessions/${cs}/comments`, fileComment(""));
    assert.strictEqual(empty.status, 422, "an empty body is refused");
});

QUnit.test("user transitions follow the state machine; edits and deletes only while open", async function (assert) {
    fake.addRevision(cs, PATH, "* v1");
    const id = String((await call("POST", `sessions/${cs}/comments`, fileComment())).json.id);

    assert.strictEqual((await patch(id, { body: "Rename it to ZCL_ORDER" })).json.body, "Rename it to ZCL_ORDER", "body edit while open");
    assert.strictEqual((await patch(id, { state: "open" })).json.code, "invalid_transition", "open -> open is no transition");
    assert.strictEqual((await patch(id, { body: "x", state: "dismissed" })).status, 422, "body xor state");
    assert.strictEqual((await patch(id, { state: "sent" })).status, 422, "sent is not a user state");
    assert.strictEqual((await patch(id, { state: "dismissed" })).json.state, "dismissed", "open -> dismissed");
    assert.strictEqual((await patch(id, { body: "y" })).json.code, "comment_not_editable", "no edit when not open");
    assert.strictEqual((await call("DELETE", `sessions/${cs}/comments/${id}`)).json.code, "comment_not_editable", "no delete when not open");
    assert.strictEqual((await patch(id, { state: "open" })).json.state, "open", "dismissed -> open (reopen)");
    assert.strictEqual((await call("DELETE", `sessions/${cs}/comments/${id}`)).status, 204, "delete while open");
    assert.strictEqual((await patch(id, { state: "open" })).status, 404, "gone");

    const sentId = String((await call("POST", `sessions/${cs}/comments`, fileComment("again"))).json.id);
    fake.setCommentState(cs, sentId, "sent");
    assert.strictEqual((await patch(sentId, { state: "dismissed" })).json.code, "invalid_transition", "sent -> dismissed is refused");
    assert.strictEqual((await patch(sentId, { state: "open" })).json.code, "invalid_transition", "sent -> open is refused");
    fake.setCommentState(cs, sentId, "addressed");
    assert.strictEqual((await patch(sentId, { state: "open" })).json.state, "open", "addressed -> open");
    fake.setCommentState(cs, sentId, "addressed");
    assert.strictEqual((await patch(sentId, { state: "dismissed" })).json.state, "dismissed", "addressed -> dismissed");

    const listed = await call("GET", `sessions/${cs}/comments?state=dismissed`);
    assert.deepEqual((listed.json as unknown as { id: string }[]).map((c) => c.id), [sentId], "state filter");
});

QUnit.test("request-changes refuses in the server's gate order before the stream, nothing_to_send last", async function (assert) {
    const rc = (b: unknown = {}) => call("POST", `sessions/${cs}/request-changes`, b);
    const s = fake.sessions[0].session;
    const code = async (b: unknown = {}): Promise<string> => String((await rc(b)).json.code);
    // Everything wrong at once: each fix uncovers the next refusal.
    s.type = "diagnose";
    s.stage = "done";
    s.status = "running";
    fake.exhaustUsage = true;
    assert.strictEqual(await code(), "revise_not_allowed", "a diagnose session first");
    s.type = "change";
    assert.strictEqual(await code(), "stage_done", "then a done session");
    s.stage = "chat";
    assert.strictEqual(await code(), "run_in_progress", "then a running one");
    s.status = "idle";
    assert.strictEqual(await code(), "revise_not_allowed", "then a stage that cannot be reworked");
    s.stage = "design";
    fake.addArtifact(cs, "design");
    const exhausted = await rc();
    assert.deepEqual([exhausted.status, exhausted.json.code], [429, "usage_exhausted"], "then the request budget");
    fake.exhaustUsage = false;
    assert.strictEqual(await code(), "nothing_to_send", "last: no open comment and no note");
    assert.strictEqual((await rc({ note: "x".repeat(4001) })).status, 422, "note too long");
    const run = await rc({ note: "Use a CDS view" });
    assert.strictEqual(run.status, 200, "a note alone is enough");
    assert.notOk(/event: comments/.test(String(run.json.raw)), "no comments frame without comments");
    assert.ok(/event: artifact/.test(String(run.json.raw)), "a new version");
});

QUnit.test("request-changes streams comments sent, text, comments addressed, the artifact, then done", async function (assert) {
    fake.sessions[0].session.stage = "plan";
    fake.addArtifact(cs, "design");
    fake.addArtifact(cs, "plan");
    fake.dataOf(cs)!.pins.design = 1;
    const id = String((await call("POST", `sessions/${cs}/comments`,
        { anchor: "document", kind: "plan", version: 1, paragraph: 2, body: "Missing tests" })).json.id);
    const raw = String((await call("POST", `sessions/${cs}/request-changes`, {})).json.raw);
    const types = [...raw.matchAll(/event: (\w+)/g)].map((m) => m[1]).filter((t) => t !== "text");
    assert.deepEqual(types, ["run", "comments", "comments", "artifact", "usage", "done"]);
    assert.ok(raw.indexOf("event: text") < raw.indexOf('"state":"addressed"'), "the text comes before the resolve");
    assert.ok(raw.includes(`{"ids":["${id}"],"state":"sent","left":0}`) && raw.includes(`{"ids":["${id}"],"state":"addressed"}`));
    const detail = await session();
    const plan2 = (detail.artifacts as { kind: string; version: number; based_on: unknown }[]).find((a) => a.kind === "plan" && a.version === 2);
    assert.deepEqual(plan2?.based_on, { design: 1 }, "based on the pinned design");
    assert.strictEqual(detail.waiting, "comments", "addressed comments first in the waiting order");
});

QUnit.test("resolveNoComments: the run sends but resolves none; at its end they are open again and still block approve", async function (assert) {
    fake.sessions[0].session.stage = "design";
    fake.addArtifact(cs, "design");
    const id = String((await call("POST", `sessions/${cs}/comments`,
        { anchor: "document", kind: "design", version: 1, paragraph: 0, body: "x" })).json.id);
    fake.resolveNoComments = true;
    const raw = String((await call("POST", `sessions/${cs}/request-changes`, {})).json.raw);
    assert.ok(raw.includes(`{"ids":["${id}"],"state":"sent","left":0}`), "sent while the run was in flight");
    assert.notOk(raw.includes('"state":"addressed"'), "nothing addressed");
    const [comment] = (await call("GET", `sessions/${cs}/comments`)).json as unknown as { state: string }[];
    assert.strictEqual(comment.state, "open", "back to open once the run ended");
    const s = await session();
    assert.deepEqual([s.open_comments, s.unresolved_comments], [1, 1], "counted as open again");
    const refused = await call("POST", `sessions/${cs}/approve`, { version: 2 });
    assert.strictEqual(refused.json.code, "open_comments", "an open comment blocks approve");
});

QUnit.test("a request-changes run that addresses only some comments returns the rest to open", async function (assert) {
    fake.sessions[0].session.stage = "design";
    fake.addArtifact(cs, "design");
    await call("POST", `sessions/${cs}/comments`, { anchor: "document", kind: "design", version: 1, paragraph: 0, body: "a" });
    await call("POST", `sessions/${cs}/request-changes`, {});
    const states = ((await call("GET", `sessions/${cs}/comments`)).json as unknown as { state: string }[]).map((c) => c.state);
    assert.deepEqual(states, ["addressed"], "the resolved one is addressed");
    assert.notOk(states.includes("sent"), "no comment is left sent after the run");
});

QUnit.test("admin/sessions answers the nine metadata fields only", async function (assert) {
    fake.isAdmin = true;
    const rows = (await call("GET", "admin/sessions")).json as unknown as Record<string, unknown>[];
    assert.deepEqual(Object.keys(rows[0]).sort(),
        ["created_at", "id", "owner", "stage", "status", "target", "title", "type", "updated_at"]);
});

QUnit.test("approve: version_changed for a stale version, then pins; propose pins the file revisions", async function (assert) {
    fake.sessions[0].session.stage = "design";
    fake.addArtifact(cs, "design");
    fake.addArtifact(cs, "design");
    assert.strictEqual((await session()).waiting, "document", "a document to approve");
    const stale = await call("POST", `sessions/${cs}/approve`, { version: 1 });
    assert.deepEqual([stale.status, stale.json.code], [409, "version_changed"]);
    const ok = await call("POST", `sessions/${cs}/approve`, { version: 2 });
    assert.deepEqual(ok.json.pins, { design: 2 });
    assert.strictEqual(ok.json.waiting, null, "the plan is not written yet");

    fake.sessions[0].session.stage = "propose";
    fake.addRevision(cs, PATH, "* v1");
    fake.addRevision(cs, PATH, "* v2");
    assert.strictEqual((await session()).waiting, "changes", "changes to review");
    const proposed = await call("POST", `sessions/${cs}/approve`, {});
    assert.deepEqual((proposed.json.pins as { files: unknown }).files, { [PATH]: 2 }, "the latest revision pinned");
    assert.strictEqual(proposed.json.stage, "review");
});

QUnit.test("approve in propose: revisions the user did not see answer 409 version_changed (U7 fix round)", async function (assert) {
    fake.sessions[0].session.stage = "propose";
    fake.addRevision(cs, PATH, "* v1");
    fake.addRevision(cs, PATH, "* v2");
    const stale = await call("POST", `sessions/${cs}/approve`, { revisions: { [PATH]: 1 } });
    assert.deepEqual([stale.status, stale.json.code], [409, "version_changed"], "revision 1 shown, 2 is the latest");
    const missing = await call("POST", `sessions/${cs}/approve`, { revisions: {} });
    assert.deepEqual([missing.status, missing.json.code], [409, "version_changed"], "a proposal the user did not see");
    assert.strictEqual((await session()).stage, "propose", "nothing was approved");
    const ok = await call("POST", `sessions/${cs}/approve`, { revisions: { [PATH]: 2 } });
    assert.strictEqual(ok.status, 200, "what the user saw is what is current");
    assert.strictEqual(ok.json.stage, "review");
    const elsewhere = await call("POST", `sessions/${cs}/approve`, { revisions: { [PATH]: 2 } });
    assert.deepEqual([elsewhere.status, elsewhere.json.code], [409, "stage_changed"], "revisions outside propose are refused");
});

QUnit.test("approve refuses in stages.approve's order: diagnose, revisions outside propose, done, running, comments, then the stage's rule", async function (assert) {
    const s = fake.sessions[0].session;
    const code = async (body: Record<string, unknown>): Promise<string> => String((await call("POST", `sessions/${cs}/approve`, body)).json.code);
    const open = (): void => {
        fake.dataOf(cs)!.comments.push({
            id: "c-order", anchor: "document", path: null, revision: null, line_start: null, line_end: null,
            kind: "design", version: 1, paragraph: 0, body: "b", state: "open", answer: null, quote: null,
            created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00"
        });
    };
    s.type = "diagnose";
    s.status = "running";
    assert.strictEqual(await code({}), "approve_not_allowed", "diagnose first, before running");
    s.type = "change";
    s.stage = "done";
    assert.strictEqual(await code({ revisions: {} }), "stage_changed", "revisions outside propose before done");
    assert.strictEqual(await code({}), "stage_done", "then done");
    s.stage = "design";
    open();
    assert.strictEqual(await code({ version: 7 }), "run_in_progress", "running before comments and the document");
    s.status = "idle";
    assert.strictEqual(await code({ version: 7 }), "open_comments", "comments before the document");
    fake.dataOf(cs)!.comments.length = 0;
    assert.strictEqual(await code({ version: 7 }), "missing_artifact", "no design: missing_artifact, not version_changed");
    fake.addArtifact(cs, "design");
    assert.strictEqual(await code({ version: 7 }), "version_changed", "then the version");
});

QUnit.test("approve in propose: only proposed objects count and are pinned (stages._proposed_revisions)", async function (assert) {
    const s = fake.sessions[0].session;
    s.stage = "propose";
    const NOTE = "notes/plan.md";
    const READ = "src/INTF/zif_read.intf.abap";
    fake.addRevision(cs, NOTE, "# a note");
    const noteOnly = await call("POST", `sessions/${cs}/approve`, {});
    assert.deepEqual([noteOnly.status, noteOnly.json.code], [409, "no_proposals"], "a note is no proposal");
    fake.addRevision(cs, READ, "INTERFACE zif_read PUBLIC.");
    fake.dataOf(cs)!.files.find((f) => f.path === READ)!.state = "read";
    fake.addRevision(cs, PATH, "* v1");
    const extra = await call("POST", `sessions/${cs}/approve`, { revisions: { [PATH]: 1, [NOTE]: 1 } });
    assert.deepEqual([extra.status, extra.json.code], [409, "version_changed"], "a note in revisions is not what would be pinned");
    const ok = await call("POST", `sessions/${cs}/approve`, { revisions: { [PATH]: 1 } });
    assert.strictEqual(ok.status, 200);
    assert.deepEqual((ok.json.pins as { files: unknown }).files, { [PATH]: 1 }, "only the proposed object is pinned, not the note or the read file");
});

QUnit.test("waiting: a session in stage done waits for nothing, even with addressed comments (backend e021a23)", async function (assert) {
    fake.sessions[0].session.stage = "done";
    fake.addRevision(cs, PATH, "* v1");
    fake.dataOf(cs)!.comments.push({
        id: "c-done", anchor: "document", path: null, revision: null, line_start: null, line_end: null,
        kind: "design", version: 1, paragraph: 0, body: "x", state: "addressed", answer: "y",
        created_at: "2026-10-03T10:00:00", updated_at: "2026-10-03T10:00:00"
    });
    assert.strictEqual((await session()).waiting, null);
});

QUnit.test("waiting: a pending approval wins, then addressed comments, then changes", async function (assert) {
    fake.allowDiagnose();
    const d = fake.addSession("Diagnose", [], [], "diagnose").id;
    fake.addApproval(d);
    const ds = (await call("GET", `sessions/${d}`)).json;
    assert.strictEqual(ds.waiting, "approval");
    assert.strictEqual(ds.target_non_production, true);
    assert.notOk("flagLost" in ds, "the fake's test knob is never served");

    fake.sessions[0].session.stage = "propose";
    fake.addRevision(cs, PATH, "* v1");
    assert.strictEqual((await session()).waiting, "changes");
    const id = String((await call("POST", `sessions/${cs}/comments`, fileComment())).json.id);
    fake.setCommentState(cs, id, "addressed");
    assert.strictEqual((await session()).waiting, "comments", "comments before changes");
    fake.sessions[0].session.status = "running";
    await patch(id, { state: "dismissed" });
    assert.strictEqual((await session()).waiting, null, "a running session waits for nothing else");
});

QUnit.test("revisions: listed newest first, served per revision; a propose run adds one and its file frame carries it", async function (assert) {
    fake.addRevision(cs, PATH, "* v1");
    fake.addRevision(cs, PATH, "* v2");
    const revs = (await call("GET", `sessions/${cs}/file/revisions?path=${encodeURIComponent(PATH)}`)).json as unknown as
        { revision: number; chars: number; syntax_status: unknown }[];
    assert.deepEqual(revs.map((r) => r.revision), [2, 1]);
    assert.strictEqual(revs[0].chars, 4);
    const v1 = await call("GET", `sessions/${cs}/file?path=${encodeURIComponent(PATH)}&revision=1`);
    assert.strictEqual(v1.json.proposed_source, "* v1");
    assert.strictEqual(v1.json.revision, 1);
    assert.strictEqual((await call("GET", `sessions/${cs}/file?path=${encodeURIComponent(PATH)}&revision=9`)).json.code, "unknown_revision");
    const latest = await call("GET", `sessions/${cs}/file?path=${encodeURIComponent(PATH)}`);
    assert.strictEqual(latest.json.proposed_source, "* v2");
    assert.strictEqual(latest.json.base_status, "sap");

    fake.sessions[0].session.stage = "propose";
    const run = String((await call("POST", `sessions/${cs}/messages`, { text: "go" })).json.raw);
    assert.ok(/event: file\ndata: [^\n]*"revision":3/.test(run), "the file frame carries the new revision");
    const files = (await call("GET", `sessions/${cs}/files`)).json as unknown as { revision: number; syntax_status: string }[];
    assert.strictEqual(files[0].revision, 3);
    assert.strictEqual(files[0].syntax_status, "ok", "checked at run end");
});

QUnit.test("file/syntax: scripted ok, errors with lines and unavailable (never ok) are stored on the revision", async function (assert) {
    fake.addRevision(cs, PATH, "* v1");
    const check = (rev?: number) => call("POST",
        `sessions/${cs}/file/syntax?path=${encodeURIComponent(PATH)}${rev ? `&revision=${rev}` : ""}`);
    fake.scriptSyntax(PATH, "unavailable");
    const unavailable = await check();
    assert.deepEqual([unavailable.status, unavailable.json.status, unavailable.json.items], [200, "unavailable", []], "ARC-1 failed: unavailable, never ok");
    fake.scriptSyntax(PATH, "errors", [{ line: 3, message: "Unknown field", severity: "error" }]);
    const errors = await check(1);
    assert.strictEqual(errors.json.status, "errors");
    assert.deepEqual(errors.json.items, [{ line: 3, message: "Unknown field", severity: "error" }]);
    assert.strictEqual(errors.json.revision, 1);
    const detail = await call("GET", `sessions/${cs}/file?path=${encodeURIComponent(PATH)}&revision=1`);
    assert.strictEqual(detail.json.syntax_status, "errors");
    assert.deepEqual(detail.json.syntax, [{ line: 3, message: "Unknown field", severity: "error" }]);
    fake.scriptSyntax(PATH, "ok");
    assert.strictEqual((await check()).json.status, "ok");

    assert.strictEqual((await check(7)).json.code, "unknown_revision");
    fake.sessions[0].session.status = "running";
    assert.strictEqual((await check()).json.code, "run_in_progress");
    fake.sessions[0].session.status = "idle";
    fake.dataOf(cs)!.files.push({
        path: "notes/impact.md", state: "new", object_type: null, object_name: null, origin_source: "", proposed_source: ""
    });
    assert.strictEqual((await call("POST", `sessions/${cs}/file/syntax?path=notes%2Fimpact.md`)).json.code, "not_an_object");
    fake.dataOf(cs)!.files.push({
        path: "src/CLAS/zcl_new.clas.abap", state: "read", object_type: "CLAS", object_name: "ZCL_NEW", origin_source: "", proposed_source: ""
    });
    assert.strictEqual((await call("POST", `sessions/${cs}/file/syntax?path=src%2FCLAS%2Fzcl_new.clas.abap`)).json.code, "no_proposal");
});

QUnit.test("activity: served per message; 404 no_activity otherwise; messages carry has_activity", async function (assert) {
    fake.withActivity = true;
    await call("POST", `sessions/${cs}/messages`, { text: "hi" });
    const messages = (await call("GET", `sessions/${cs}/messages`)).json as unknown as { id: string; role: string; has_activity: boolean }[];
    const [user, reply] = messages;
    assert.deepEqual([user.has_activity, reply.has_activity], [false, true]);
    const activity = await call("GET", `sessions/${cs}/messages/${reply.id}/activity`);
    assert.strictEqual(activity.status, 200);
    assert.strictEqual(activity.json.dropped, 0);
    assert.strictEqual((activity.json.events as unknown[]).length, 1);
    const none = await call("GET", `sessions/${cs}/messages/${user.id}/activity`);
    assert.deepEqual([none.status, none.json.code], [404, "no_activity"]);
});

QUnit.test("conventions: POST creates (admin, 409 target_exists), PUT no longer creates and clears fields", async function (assert) {
    assert.strictEqual((await call("POST", "conventions", { target: "DEMO" })).status, 403, "admin only");
    fake.isAdmin = true;
    const created = await call("POST", "conventions", { target: "DEMO", package: "ZDEMO", namespace: "/DEMO/", non_production: true });
    assert.strictEqual(created.status, 201);
    assert.strictEqual(created.json.non_production, true);
    assert.strictEqual((await call("POST", "conventions", { target: "DEMO" })).json.code, "target_exists");
    assert.strictEqual((await call("POST", "conventions", { target: "bad target!" })).status, 422, "bad target pattern");
    assert.strictEqual((await call("POST", "conventions", { target: "DEMO2", non_production: "yes" })).status, 422, "StrictBool");
    const missing = await call("PUT", "conventions/NOPE", { package: "Z" });
    assert.deepEqual([missing.status, missing.json.code], [404, "unknown_target"]);
    const cleared = await call("PUT", "conventions/DEMO", { package: "ZNEW", clear: ["namespace"] });
    assert.strictEqual(cleared.json.package, "ZNEW");
    assert.strictEqual(cleared.json.namespace, "", "namespace cleared: stored as an empty string, as the server does");
    assert.strictEqual(cleared.json.non_production, true, "fields not sent are kept");
    assert.strictEqual((await call("PUT", "conventions/DEMO", { package: "Z", clear: ["package"] })).status, 422, "set and cleared");
    const notBool = await call("PUT", "conventions/DEMO", { non_production: "true" });
    assert.strictEqual(notBool.status, 422, "PUT is StrictBool too");
    assert.strictEqual((await call("GET", "conventions/DEMO")).json.non_production, true, "the flag is unchanged");
    assert.strictEqual((await call("PUT", "conventions/DEMO", { non_production: false })).json.non_production, false, "a real boolean is taken");
    assert.strictEqual((await call("POST", "conventions", { target: "A".repeat(64) })).status, 201, "64 characters: the server's limit");
    assert.strictEqual((await call("POST", "conventions", { target: "A".repeat(65) })).status, 422, "65 characters are refused");
    assert.strictEqual((await call("POST", "conventions", { target: "-DEMO" })).status, 422, "the first character is a letter or digit");
});

QUnit.test("conventions: PUT refuses what the server's ConventionsUpdate refuses (unknown keys, lengths, clean core level)", async function (assert) {
    fake.isAdmin = true;
    const put = (body: Record<string, unknown>) => call("PUT", "conventions/dev-system", body);
    const unknown = await put({ target: "dev-system", package: "ZX" });
    assert.strictEqual(unknown.status, 422, "extra=forbid: target is not a PUT field");
    assert.strictEqual((await put({ naming: "x" })).status, 422, "any unknown key");
    const limits: [string, number][] = [["label", 120], ["destination", 200], ["namespace", 30], ["package", 30], ["atc_variant", 30], ["free_text", 20000]];
    for (const [field, max] of limits) {
        assert.strictEqual((await put({ [field]: "x".repeat(max + 1) })).status, 422, `${field} longer than ${max} is refused`);
        assert.strictEqual((await put({ [field]: "x".repeat(max) })).status, 200, `${field} of ${max} is taken`);
    }
    for (const bad of ["E", "a", "AB", "", 1]) {
        assert.strictEqual((await put({ clean_core_level: bad })).status, 422, `clean_core_level ${JSON.stringify(bad)} is refused`);
    }
    for (const ok of ["A", "B", "C", "D"]) {
        assert.strictEqual((await put({ clean_core_level: ok })).status, 200, `clean_core_level ${ok} is taken`);
    }
    assert.strictEqual((await put({ package: 5 })).status, 422, "a text field takes a string");
    assert.strictEqual(fake.conventions.find((c) => c.target === "dev-system")?.clean_core_level, "D", "refused bodies stored nothing");
});

QUnit.test("csrf: refuseCsrf(n) answers the next n changes 403 Required even with a fresh token", async function (assert) {
    fake.isAdmin = true;
    fake.csrf = true;
    const fetched = await fetch("backend/me", { headers: { "X-CSRF-Token": "Fetch" } });
    const token = fetched.headers.get("X-CSRF-Token") ?? "";
    fake.refuseCsrf(2);
    const put = () => fetch("backend/conventions/dev-system", {
        method: "PUT", headers: { "Content-Type": "application/json", "X-CSRF-Token": token }, body: JSON.stringify({ package: "ZX" })
    });
    for (const attempt of [1, 2]) {
        const refused = await put();
        assert.deepEqual([refused.status, refused.headers.get("X-CSRF-Token")], [403, "Required"], `attempt ${attempt} refused`);
    }
    assert.strictEqual((await put()).status, 200, "the third goes through");
    assert.strictEqual(fake.conventions.find((c) => c.target === "dev-system")?.package, "ZX");
});

QUnit.test("csrf: off by default; on, a change needs the fetched token and an expired one answers 403 Required", async function (assert) {
    const rename = (headers: Record<string, string>) => fetch(`backend/sessions/${cs}`, {
        method: "PATCH", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ title: "x" })
    });
    assert.strictEqual((await rename({})).status, 200, "off: no token needed");
    const offFetch = await fetch("backend/me", { headers: { "X-CSRF-Token": "Fetch" } });
    assert.strictEqual(offFetch.headers.get("X-CSRF-Token"), null, "off: no token handed out");

    fake.csrf = true;
    const refused = await rename({});
    assert.deepEqual([refused.status, refused.headers.get("X-CSRF-Token")], [403, "Required"], "on: refused without a token");
    assert.strictEqual((await fetch(`backend/sessions/${cs}`)).status, 200, "a GET needs none");
    const token = (await fetch("backend/me", { headers: { "X-CSRF-Token": "Fetch" } })).headers.get("X-CSRF-Token") ?? "";
    assert.ok(token, "the fetch hands one out");
    assert.strictEqual(fake.csrfFetches, 1, "counted");
    assert.strictEqual((await rename({ "X-CSRF-Token": "wrong" })).status, 403, "a wrong token is refused");
    assert.strictEqual((await rename({ "X-CSRF-Token": token })).status, 200, "the token is accepted");
    fake.expireCsrfToken();
    assert.strictEqual((await rename({ "X-CSRF-Token": token })).status, 403, "expired");
    const fresh = (await fetch("backend/me", { headers: { "X-CSRF-Token": "Fetch" } })).headers.get("X-CSRF-Token");
    assert.notStrictEqual(fresh, token, "a new token after expiry");
});

QUnit.test("artifact summaries carry the stage that wrote them", async function (assert) {
    fake.addArtifact(cs, "design");
    fake.addArtifact(cs, "note");
    const arts = (await session()).artifacts as { kind: string; stage: string }[];
    assert.deepEqual(arts.map((a) => `${a.kind}:${a.stage}`).sort(), ["design:design", "note:propose"]);
});

QUnit.test("B9/B10: request-changes ends with comments open for what stayed sent; session JSON has usage and objects", async function (assert) {
    fake.sessions[0].session.stage = "design";
    fake.addArtifact(cs, "design");
    const id = String((await call("POST", `sessions/${cs}/comments`,
        { anchor: "document", kind: "design", version: 1, paragraph: 0, body: "x" })).json.id);
    fake.resolveNoComments = true;
    const raw = String((await call("POST", `sessions/${cs}/request-changes`, { note: "Also rename it" })).json.raw);
    const types = [...raw.matchAll(/event: (\w+)/g)].map((m) => m[1]).filter((t) => t !== "text");
    assert.deepEqual(types, ["run", "comments", "artifact", "usage", "comments", "done"], "the open frame comes last before done");
    assert.ok(raw.includes(`{"ids":["${id}"],"state":"open"}`), "the server reports the comment open again");
    const messages = (await call("GET", `sessions/${cs}/messages`)).json as unknown as { role: string; content: string }[];
    assert.strictEqual(messages[messages.length - 2].content, "Request changes: 1 comment(s)\n\nAlso rename it", "stored as the server does");
    const s = await session();
    assert.deepEqual([s.requests_used, s.request_cap], [1, 200], "a run used one request");
    assert.deepEqual([s.objects, s.objects_total, s.changed_objects, s.findings_count], [["ZCL_DEMO"], 1, 0, null]);
});
