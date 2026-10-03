import {
    canApprove, canRevise, canSend, nextStage, stageTokens, STAGES
} from "com/agent/ide/model/stageGate";
import type { ArtifactKind, FileState, Stage } from "com/agent/ide/service/types";

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
