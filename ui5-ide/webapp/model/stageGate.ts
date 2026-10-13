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

/**
 * A workspace file carries a proposal: its state is `modified` or `new`
 * (the server's `PROPOSAL_STATES`). The revision number does not decide: a
 * proposal that became byte-equal to SAP goes back to `read` and keeps its
 * revisions.
 */
export function isProposal(file: { state: FileState }): boolean {
    return PROPOSAL_STATES.includes(file.state);
}

/** A proposal of an ABAP object (not a scratch note): what a propose approve pins (`stages._proposed_revisions`). */
export function isProposedObject(file: { state: FileState; object_type?: string | null }): boolean {
    return isProposal(file) && file.object_type !== null && file.object_type !== undefined && file.object_type !== "";
}

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
    if (stage === "propose" && !files.some(isProposal)) {
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
    stage: Stage | null | undefined, type?: SessionType | null, options: { withDone?: boolean } = {}
): { stage: Stage; state: StageTokenState }[] {
    let stages = stagesFor(type);
    if (options.withDone === false && stage === "done") {
        // The session page has no "Done" token: a finished session shows every stage done.
        return stages.filter((s) => s !== "done").map((s) => ({ stage: s, state: "done" }));
    }
    if (options.withDone === false) {
        stages = stages.filter((s) => s !== "done");
    }
    const current = stage ? stages.indexOf(stage) : -1;
    return stages.map((s, i) => ({
        stage: s,
        state: current < 0 ? "upcoming" : i < current ? "done" : i === current ? "current" : "upcoming"
    }));
}


// --- The session page's primary action (Task U7) ------------------------------

/** What {@link primaryAction} reads of a session (`SessionOut`). */
export interface PrimarySession {
    type: SessionType;
    stage: Stage;
    status: SessionStatus;
    unresolved_comments: number;
    requests_used: number;
    request_cap: number;
    target_non_production: boolean;
}

export type PrimaryKey = "startDesign" | "approveDesign" | "approvePlan" | "approveChanges" | "finish" | "done"
    | "report" | "handover";

/**
 * The one primary action of the session page, named by its effect.
 *
 * - `textKey`/`textArgs`: the button text ("Approve design (v2) and plan").
 * - `version`: the document version the approve sends (D3), the one the
 *   text names; for `handover` the latest report.
 * - When `enabled` is false, `reason` is the server's refusal code
 *   (`run_in_progress`, `open_comments`, `missing_artifact`, `no_proposals`,
 *   `usage_exhausted`, `stage_done`, `target_not_non_production`) and
 *   `reasonKey`/`reasonArgs` the visible sentence.
 *
 * The order follows `stages.approve` / `assert_can_run`: done, running,
 * comments (change sessions), then the stage's own rule. An approve runs no
 * model, so the request cap only gates the diagnose report.
 */
export interface PrimaryAction {
    key: PrimaryKey;
    enabled: boolean;
    textKey: string;
    textArgs: (string | number)[];
    version?: number;
    reason?: string;
    reasonKey?: string;
    reasonArgs?: (string | number)[];
}

type Doc = { kind: ArtifactKind; version: number };
type ObjFile = { state: FileState; object_type?: string | null };

function latestVersion(artifacts: Doc[], kind: ArtifactKind): number | undefined {
    const versions = artifacts.filter((a) => a.kind === kind).map((a) => a.version);
    return versions.length ? Math.max(...versions) : undefined;
}

/** The change stages whose approve pins a document, with their texts and missing-document reason. */
const DOC_STAGES: Partial<Record<Stage, { key: PrimaryKey; kind: ArtifactKind; text: string; noVersion: string; need: string }>> = {
    design: { key: "approveDesign", kind: "design", text: "primaryApproveDesign", noVersion: "primaryApproveDesignNoVersion", need: "gateNeedsDesign" },
    plan: { key: "approvePlan", kind: "plan", text: "primaryApprovePlan", noVersion: "primaryApprovePlanNoVersion", need: "gateNeedsPlan" },
    review: { key: "finish", kind: "review", text: "primaryFinishVersion", noVersion: "primaryFinish", need: "gateNeedsReview" }
};

export function primaryAction(session: PrimarySession, artifacts: Doc[], files: ObjFile[] = []): PrimaryAction {
    const refuse = (action: Omit<PrimaryAction, "enabled">, reason: string, reasonKey: string,
        reasonArgs: (string | number)[] = []): PrimaryAction =>
        ({ ...action, enabled: false, reason, reasonKey, reasonArgs });

    if (session.type === "diagnose") {
        const report = latestVersion(artifacts, "report");
        const action: Omit<PrimaryAction, "enabled"> = report === undefined
            ? { key: "report", textKey: "primaryReport", textArgs: [] }
            : { key: "handover", textKey: "primaryHandover", textArgs: [], version: report };
        if (session.status === "running") {
            return refuse(action, "run_in_progress", "gateRunInProgress");
        }
        if (!session.target_non_production) {
            return refuse(action, "target_not_non_production", "targetNotNonProd");
        }
        if (action.key === "report" && session.request_cap > 0 && session.requests_used >= session.request_cap) {
            return refuse(action, "usage_exhausted", "gateUsageExhausted", [session.request_cap]);
        }
        return { ...action, enabled: true };
    }

    if (session.stage === "done") {
        return refuse({ key: "done", textKey: "primaryDone", textArgs: [] }, "stage_done", "gateStageDone");
    }
    const doc = DOC_STAGES[session.stage];
    const version = doc ? latestVersion(artifacts, doc.kind) : undefined;
    let action: Omit<PrimaryAction, "enabled">;
    if (doc) {
        action = version === undefined
            ? { key: doc.key, textKey: doc.noVersion, textArgs: [] }
            : { key: doc.key, textKey: doc.text, textArgs: [version], version };
    } else if (session.stage === "propose") {
        action = { key: "approveChanges", textKey: "primaryApproveChanges", textArgs: [] };
    } else {
        action = { key: "startDesign", textKey: "primaryStartDesign", textArgs: [] };
    }
    if (session.status === "running") {
        return refuse(action, "run_in_progress", "gateRunInProgress");
    }
    const unresolved = session.unresolved_comments ?? 0;
    if (unresolved > 0) {
        return refuse(action, "open_comments", unresolved === 1 ? "gateOpenCommentsOne" : "gateOpenComments", [unresolved]);
    }
    if (doc && version === undefined) {
        return refuse(action, "missing_artifact", doc.need);
    }
    if (session.stage === "propose"
        && !files.some(isProposedObject)) {
        return refuse(action, "no_proposals", "gateNoProposals");
    }
    return { ...action, enabled: true };
}
