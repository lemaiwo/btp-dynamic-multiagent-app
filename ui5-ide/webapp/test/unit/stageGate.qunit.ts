import {
    canApprove, canHandover, canReport, canRevise, canSend, nextStage, primaryAction, stageTokens, stagesFor, STAGES,
    type PrimarySession
} from "com/agent/ide/model/stageGate";
import type { ArtifactKind, FileState, SessionType, Stage } from "com/agent/ide/service/types";

function session(stage: Stage, status: "idle" | "running" = "idle"): { stage: Stage; status: "idle" | "running" } {
    return { stage, status };
}
const art = (...kinds: ArtifactKind[]): { kind: ArtifactKind }[] => kinds.map((kind) => ({ kind }));
const files = (...states: FileState[]): { state: FileState }[] => states.map((state) => ({ state }));

QUnit.module("stageGate: canApprove (plan §1.1)");

QUnit.test("chat: always, unless a run is in progress", function (assert) {
    assert.deepEqual(canApprove(session("chat"), [], []), { ok: true });
    assert.deepEqual(canApprove(session("chat", "running"), [], []), { ok: false, reasonKey: "gateRunInProgress" });
});

QUnit.test("design: needs at least one design artifact", function (assert) {
    assert.deepEqual(canApprove(session("design"), [], []), { ok: false, reasonKey: "gateNeedsDesign" });
    assert.deepEqual(canApprove(session("design"), art("plan", "note"), []), { ok: false, reasonKey: "gateNeedsDesign" },
        "another kind does not count");
    assert.deepEqual(canApprove(session("design"), art("design"), []), { ok: true });
});

QUnit.test("plan: needs at least one plan artifact", function (assert) {
    assert.deepEqual(canApprove(session("plan"), art("design"), []), { ok: false, reasonKey: "gateNeedsPlan" });
    assert.deepEqual(canApprove(session("plan"), art("design", "plan"), []), { ok: true });
});

QUnit.test("propose: needs a workspace file in state modified or new", function (assert) {
    assert.deepEqual(canApprove(session("propose"), art("note"), files("read")), { ok: false, reasonKey: "gateNoProposals" },
        "a note and a read file are not a proposal");
    assert.deepEqual(canApprove(session("propose"), [], files("read", "modified")), { ok: true });
    assert.deepEqual(canApprove(session("propose"), [], files("new")), { ok: true });
});

QUnit.test("review: needs at least one review artifact", function (assert) {
    assert.deepEqual(canApprove(session("review"), art("design", "plan", "note"), files("modified")),
        { ok: false, reasonKey: "gateNeedsReview" });
    assert.deepEqual(canApprove(session("review"), art("review"), []), { ok: true });
});

QUnit.test("done: refused; no session or an unknown stage: refused", function (assert) {
    assert.deepEqual(canApprove(session("done"), art("review"), []), { ok: false, reasonKey: "gateStageDone" });
    assert.deepEqual(canApprove(null, [], []), { ok: false, reasonKey: "gateNoSession" });
    assert.deepEqual(canApprove(session("weird" as Stage), [], []), { ok: false, reasonKey: "gateInvalidStage" });
});

QUnit.test("a running session refuses in every stage, before the stage's own rule", function (assert) {
    for (const stage of ["design", "plan", "propose", "review"] as Stage[]) {
        assert.deepEqual(canApprove(session(stage, "running"), art("design", "plan", "review"), files("new")),
            { ok: false, reasonKey: "gateRunInProgress" }, stage);
    }
});

QUnit.module("stageGate: revise and send");

QUnit.test("revise: design, plan, propose and review only; never while running", function (assert) {
    for (const stage of ["design", "plan", "propose", "review"] as Stage[]) {
        assert.deepEqual(canRevise(session(stage)), { ok: true }, stage);
    }
    assert.deepEqual(canRevise(session("chat")), { ok: false, reasonKey: "gateReviseNotAllowed" });
    assert.deepEqual(canRevise(session("done")), { ok: false, reasonKey: "gateStageDone" });
    assert.deepEqual(canRevise(session("plan", "running")), { ok: false, reasonKey: "gateRunInProgress" });
    assert.deepEqual(canRevise(null), { ok: false, reasonKey: "gateNoSession" });
});

QUnit.test("send: any stage but done; never while running", function (assert) {
    for (const stage of ["chat", "design", "plan", "propose", "review"] as Stage[]) {
        assert.deepEqual(canSend(session(stage)), { ok: true }, stage);
    }
    assert.deepEqual(canSend(session("done")), { ok: false, reasonKey: "gateStageDone" });
    assert.deepEqual(canSend(session("chat", "running")), { ok: false, reasonKey: "gateRunInProgress" });
    assert.deepEqual(canSend(null), { ok: false, reasonKey: "gateNoSession" });
});

QUnit.module("stageGate: stage bar");

QUnit.test("next stage follows §1.1; done has none", function (assert) {
    assert.deepEqual(STAGES, ["chat", "design", "plan", "propose", "review", "done"]);
    assert.strictEqual(nextStage("chat"), "design");
    assert.strictEqual(nextStage("review"), "done");
    assert.strictEqual(nextStage("done"), null);
});

QUnit.test("tokens are done before the current stage and upcoming after it", function (assert) {
    assert.deepEqual(stageTokens("plan").map((t) => `${t.stage}:${t.state}`), [
        "chat:done", "design:done", "plan:current", "propose:upcoming", "review:upcoming", "done:upcoming"
    ]);
    assert.deepEqual(stageTokens("done").map((t) => t.state), ["done", "done", "done", "done", "done", "current"]);
    assert.ok(stageTokens(null).every((t) => t.state === "upcoming"), "no session: all upcoming");
});

QUnit.module("stageGate: diagnose sessions (plan 1c §1.1)");

function diag(status: "idle" | "running" = "idle", stage: Stage = "investigate"): { stage: Stage; status: "idle" | "running"; type: SessionType } {
    return { stage, status, type: "diagnose" };
}

QUnit.test("stagesFor: change walks six stages, diagnose has investigate only", function (assert) {
    assert.deepEqual(stagesFor("change"), STAGES);
    assert.deepEqual(stagesFor("diagnose"), ["investigate"]);
    assert.deepEqual(stagesFor(undefined), STAGES, "no type reads as change");
});

QUnit.test("diagnose: the stage bar is the single step investigate, current", function (assert) {
    assert.deepEqual(stageTokens("investigate", "diagnose"), [{ stage: "investigate", state: "current" }]);
    assert.deepEqual(stageTokens(null, "diagnose"), [{ stage: "investigate", state: "upcoming" }]);
    assert.strictEqual(stageTokens("chat", "change").length, 6, "a change session keeps its six tokens");
});

QUnit.test("diagnose: approve and revise are refused with their own reasons", function (assert) {
    assert.deepEqual(canApprove(diag(), [], []), { ok: false, reasonKey: "gateDiagnoseNoApprove" });
    assert.deepEqual(canRevise(diag()), { ok: false, reasonKey: "gateDiagnoseNoRevise" });
    assert.deepEqual(canApprove(diag("running"), [], []), { ok: false, reasonKey: "gateDiagnoseNoApprove" });
});

QUnit.test("diagnose: send is allowed in investigate unless running", function (assert) {
    assert.deepEqual(canSend(diag()), { ok: true });
    assert.deepEqual(canSend(diag("running")), { ok: false, reasonKey: "gateRunInProgress" });
});

QUnit.test("canReport: diagnose and idle only", function (assert) {
    assert.deepEqual(canReport(diag()), { ok: true });
    assert.deepEqual(canReport(diag("running")), { ok: false, reasonKey: "gateRunInProgress" });
    assert.deepEqual(canReport({ stage: "chat", status: "idle", type: "change" }), { ok: false, reasonKey: "gateNotDiagnose" });
    assert.deepEqual(canReport(null), { ok: false, reasonKey: "gateNoSession" });
});

QUnit.test("canHandover: diagnose, idle and at least one report", function (assert) {
    assert.deepEqual(canHandover(diag(), []), { ok: false, reasonKey: "gateNeedsReportToHandOver" });
    assert.deepEqual(canHandover(diag(), art("note")), { ok: false, reasonKey: "gateNeedsReportToHandOver" });
    assert.deepEqual(canHandover(diag(), art("report")), { ok: true });
    assert.deepEqual(canHandover(diag("running"), art("report")), { ok: false, reasonKey: "gateRunInProgress" });
    assert.deepEqual(canHandover({ stage: "chat", status: "idle", type: "change" }, art("report")),
        { ok: false, reasonKey: "gateNotDiagnose" });
    assert.deepEqual(canHandover(null, art("report")), { ok: false, reasonKey: "gateNoSession" });
});

// --- primaryAction (Task U7) -------------------------------------------------

function ps(stage: Stage, extra: Partial<PrimarySession> = {}): PrimarySession {
    return {
        type: "change", stage, status: "idle", unresolved_comments: 0, requests_used: 3, request_cap: 200,
        target_non_production: false, ...extra
    };
}
const docs = (...list: [ArtifactKind, number][]): { kind: ArtifactKind; version: number }[] =>
    list.map(([kind, version]) => ({ kind, version }));
const obj = (state: FileState, objectType: string | null = "CLAS"): { state: FileState; object_type: string | null } =>
    ({ state, object_type: objectType });

QUnit.module("stageGate: primaryAction (Task U7)");

QUnit.test("each change stage names its effect, with the version it approves", function (assert) {
    assert.deepEqual(primaryAction(ps("chat"), []),
        { key: "startDesign", enabled: true, textKey: "primaryStartDesign", textArgs: [] });
    assert.deepEqual(primaryAction(ps("design"), docs(["design", 1], ["design", 2])),
        { key: "approveDesign", enabled: true, textKey: "primaryApproveDesign", textArgs: [2], version: 2 },
        "the latest design version, whatever the list order");
    assert.deepEqual(primaryAction(ps("plan"), docs(["design", 2], ["plan", 1])),
        { key: "approvePlan", enabled: true, textKey: "primaryApprovePlan", textArgs: [1], version: 1 });
    assert.deepEqual(primaryAction(ps("propose"), docs(["note", 1]), [obj("modified")]),
        { key: "approveChanges", enabled: true, textKey: "primaryApproveChanges", textArgs: [] });
    assert.deepEqual(primaryAction(ps("review"), docs(["review", 3])),
        { key: "finish", enabled: true, textKey: "primaryFinishVersion", textArgs: [3], version: 3 });
});

QUnit.test("missing_artifact: a document stage without its document is disabled with the reason", function (assert) {
    assert.deepEqual(primaryAction(ps("design"), docs(["plan", 1])), {
        key: "approveDesign", enabled: false, textKey: "primaryApproveDesignNoVersion", textArgs: [],
        reason: "missing_artifact", reasonKey: "gateNeedsDesign", reasonArgs: []
    });
    assert.strictEqual(primaryAction(ps("plan"), docs(["design", 1])).reasonKey, "gateNeedsPlan");
    assert.strictEqual(primaryAction(ps("review"), []).reasonKey, "gateNeedsReview");
});

QUnit.test("no_proposals: propose needs a new or modified object file (a note is not one)", function (assert) {
    const refused = primaryAction(ps("propose"), [], [obj("read"), obj("new", null)]);
    assert.deepEqual([refused.enabled, refused.reason, refused.reasonKey], [false, "no_proposals", "gateNoProposals"]);
    assert.ok(primaryAction(ps("propose"), [], [obj("new")]).enabled, "a new object is a proposal");
});

QUnit.test("open_comments: unresolved comments block, with their number, before any artifact rule", function (assert) {
    const blocked = primaryAction(ps("design", { unresolved_comments: 2 }), []);
    assert.deepEqual([blocked.enabled, blocked.reason, blocked.reasonKey, blocked.reasonArgs],
        [false, "open_comments", "gateOpenComments", [2]], "comments are named before the missing design");
    assert.strictEqual(primaryAction(ps("design", { unresolved_comments: 1 }), []).reasonKey, "gateOpenCommentsOne",
        "one comment has its own sentence");
    assert.strictEqual(primaryAction(ps("chat", { unresolved_comments: 1 }), []).reason, "open_comments", "in chat too");
});

QUnit.test("run_in_progress comes before comments; stage_done before everything", function (assert) {
    const running = primaryAction(ps("design", { status: "running", unresolved_comments: 1 }), docs(["design", 1]));
    assert.deepEqual([running.enabled, running.reason, running.reasonKey], [false, "run_in_progress", "gateRunInProgress"]);
    const done = primaryAction(ps("done", { status: "running" }), docs(["review", 1]));
    assert.deepEqual([done.key, done.enabled, done.reason, done.reasonKey, done.textKey],
        ["done", false, "stage_done", "gateStageDone", "primaryDone"]);
});

QUnit.test("approve does not use the model: an exhausted request cap does not block it", function (assert) {
    assert.ok(primaryAction(ps("design", { requests_used: 200 }), docs(["design", 1])).enabled);
});

QUnit.test("diagnose: Create report until a report exists, then Hand over to a change", function (assert) {
    const d = (extra: Partial<PrimarySession> = {}): PrimarySession =>
        ps("investigate", { type: "diagnose", target_non_production: true, ...extra });
    assert.deepEqual(primaryAction(d(), []),
        { key: "report", enabled: true, textKey: "primaryReport", textArgs: [] });
    assert.deepEqual(primaryAction(d(), docs(["report", 1])),
        { key: "handover", enabled: true, textKey: "primaryHandover", textArgs: [], version: 1 });
    const exhausted = primaryAction(d({ requests_used: 200 }), []);
    assert.deepEqual([exhausted.enabled, exhausted.reason, exhausted.reasonKey, exhausted.reasonArgs],
        [false, "usage_exhausted", "gateUsageExhausted", [200]], "a report run needs a model request");
    assert.ok(primaryAction(d({ requests_used: 200 }), docs(["report", 1])).enabled, "the handover runs no model");
    const running = primaryAction(d({ status: "running" }), docs(["report", 1]));
    assert.strictEqual(running.reason, "run_in_progress");
    const lost = primaryAction(d({ target_non_production: false }), []);
    assert.deepEqual([lost.enabled, lost.reason, lost.reasonKey], [false, "target_not_non_production", "targetNotNonProd"],
        "a target that lost its flag refuses the report");
});

QUnit.test("stageTokens of the session page leave out `done` for a change session", function (assert) {
    assert.deepEqual(stageTokens("done", "change", { withDone: false }).map((t) => t.state),
        ["done", "done", "done", "done", "done"], "five tokens, all done once the session is finished");
    assert.deepEqual(stageTokens("plan", "change", { withDone: false }).map((t) => t.stage),
        ["chat", "design", "plan", "propose", "review"]);
});
