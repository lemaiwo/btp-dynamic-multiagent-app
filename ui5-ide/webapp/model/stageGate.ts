import type { ArtifactKind, FileState, SessionStatus, SessionType, Stage } from "../service/types";

/**
 * The client mirror of the stage gates (plan §1.1), for enabling Approve,
 * Revise and Send. The server stays authoritative: whatever is allowed
 * here can still be refused with a 409 (another tab, a concurrent run).
 *
 * `reasonKey` is the i18n key of the sentence that says why not, shown as
 * the button's tooltip.
 */
export interface Gate { ok: boolean; reasonKey?: string }

type SessionLike = { stage: Stage; status: SessionStatus; type?: SessionType } | null | undefined;

export const STAGES: Stage[] = ["chat", "design", "plan", "propose", "review", "done"];

/** The stages per session type (plan 1c §1.1): a diagnose session never leaves `investigate`. */
export const STAGES_BY_TYPE: Record<SessionType, Stage[]> = {
    change: STAGES,
    diagnose: ["investigate"]
};

/** The stage list of a session type; no type reads as `change`. */
export function stagesFor(type: SessionType | null | undefined): Stage[] {
    return STAGES_BY_TYPE[type ?? "change"] ?? STAGES;
}

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
    if (!stagesFor(session.type).includes(session.stage)) {
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
    if (session?.type === "diagnose") {
        return refuse("gateDiagnoseNoApprove");
    }
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
    if (session?.type === "diagnose") {
        return refuse("gateDiagnoseNoRevise");
    }
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

/** Whether the findings so far may be written up as a report (diagnose sessions, not while running). */
export function canReport(session: SessionLike): Gate {
    const refused = common(session);
    if (refused) {
        return refused;
    }
    return session!.type === "diagnose" ? OK : refuse("gateNotDiagnose");
}

/** Whether a diagnose session may be handed over to a change session: it needs a report. */
export function canHandover(session: SessionLike, artifacts: { kind: ArtifactKind }[]): Gate {
    const refused = canReport(session);
    if (refused.ok === false) {
        return refused;
    }
    return artifacts.some((a) => a.kind === "report") ? OK : refuse("gateNeedsReportToHandOver");
}

export type StageTokenState = "done" | "current" | "upcoming";

/**
 * The stage-bar tokens for a session of `type` in `stage` (all upcoming
 * without one): six for a change session, the single step `investigate`
 * for a diagnose session.
 */
export function stageTokens(
    stage: Stage | null | undefined, type?: SessionType | null
): { stage: Stage; state: StageTokenState }[] {
    const stages = stagesFor(type);
    const current = stage ? stages.indexOf(stage) : -1;
    return stages.map((s, i) => ({
        stage: s,
        state: current < 0 ? "upcoming" : i < current ? "done" : i === current ? "current" : "upcoming"
    }));
}
