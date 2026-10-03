import type { ArtifactKind, FileState, SessionStatus, Stage } from "../service/types";

/**
 * The client mirror of the stage gates (plan §1.1), for enabling Approve,
 * Revise and Send. The server stays authoritative: whatever is allowed
 * here can still be refused with a 409 (another tab, a concurrent run).
 *
 * `reasonKey` is the i18n key of the sentence that says why not, shown as
 * the button's tooltip.
 */
export interface Gate { ok: boolean; reasonKey?: string }

type SessionLike = { stage: Stage; status: SessionStatus } | null | undefined;

export const STAGES: Stage[] = ["chat", "design", "plan", "propose", "review", "done"];

/** The artifact each stage must have produced before it can be approved. */
const NEEDS: Partial<Record<Stage, { kind: ArtifactKind; reasonKey: string }>> = {
    design: { kind: "design", reasonKey: "gateNeedsDesign" },
    plan: { kind: "plan", reasonKey: "gateNeedsPlan" },
    review: { kind: "review", reasonKey: "gateNeedsReview" }
};

const REVISABLE: Stage[] = ["design", "plan", "propose", "review"];
const PROPOSAL_STATES: FileState[] = ["modified", "new"];

const OK: Gate = { ok: true };
const refuse = (reasonKey: string): Gate => ({ ok: false, reasonKey });

/** The refusals every action shares, in the server's order: no session, unknown stage, done, running. */
function common(session: SessionLike): Gate | null {
    if (!session) {
        return refuse("gateNoSession");
    }
    if (!STAGES.includes(session.stage)) {
        return refuse("gateInvalidStage");
    }
    if (session.stage === "done") {
        return refuse("gateStageDone");
    }
    if (session.status === "running") {
        return refuse("gateRunInProgress");
    }
    return null;
}

export function nextStage(stage: Stage): Stage | null {
    const i = STAGES.indexOf(stage);
    return i >= 0 && i < STAGES.length - 1 ? STAGES[i + 1] : null;
}

/** Whether the session may move to the next stage (§1.1 "approve allowed when"). */
export function canApprove(
    session: SessionLike, artifacts: { kind: ArtifactKind }[], files: { state: FileState }[]
): Gate {
    const refused = common(session);
    if (refused) {
        return refused;
    }
    const stage = session!.stage;
    const need = NEEDS[stage];
    if (need && !artifacts.some((a) => a.kind === need.kind)) {
        return refuse(need.reasonKey);
    }
    if (stage === "propose" && !files.some((f) => PROPOSAL_STATES.includes(f.state))) {
        return refuse("gateNoProposals");
    }
    return OK;
}

/** Whether the current stage may be rerun with feedback. */
export function canRevise(session: SessionLike): Gate {
    const refused = common(session);
    if (refused) {
        return refused;
    }
    return REVISABLE.includes(session!.stage) ? OK : refuse("gateReviseNotAllowed");
}

/** Whether a message may be sent. */
export function canSend(session: SessionLike): Gate {
    return common(session) ?? OK;
}

export type StageTokenState = "done" | "current" | "upcoming";

/** The six stage-bar tokens for a session in `stage` (all upcoming without one). */
export function stageTokens(stage: Stage | null | undefined): { stage: Stage; state: StageTokenState }[] {
    const current = stage ? STAGES.indexOf(stage) : -1;
    return STAGES.map((s, i) => ({
        stage: s,
        state: current < 0 ? "upcoming" : i < current ? "done" : i === current ? "current" : "upcoming"
    }));
}
