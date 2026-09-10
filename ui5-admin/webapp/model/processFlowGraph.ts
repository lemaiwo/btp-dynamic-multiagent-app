import type { WorkflowBranch, WorkflowStep, WorkflowStepRun } from "../service/types";

/**
 * Turns a workflow definition into the lanes and nodes a
 * `sap.suite.ui.commons.ProcessFlow` binds to, and decorates that graph with
 * what one run actually did.
 *
 * The topology mirrors `agents/workflow_runner.py`: the main line runs in
 * position order until the single fan-out step, which produces the work
 * items; each item then enters the branches the fan-out step selected for it,
 * and the remaining main-line steps join those branches back together and run
 * once per item.
 *
 * Both functions are pure, so the shape of the graph is testable without
 * rendering anything.
 */

/** `sap.suite.ui.commons.ProcessFlowNodeState` values, as plain strings so
 * this module stays free of control imports. */
export type FlowNodeState = "Positive" | "Negative" | "Neutral" | "Planned" | "Critical";

/** One column of the flow. Lanes are positional only: the label says what
 * kind of step lives in the column, not which branch. */
export interface FlowLane {
    laneId: string;
    label: string;
    position: number;
}

export interface FlowNode {
    nodeId: string;
    laneId: string;
    title: string;
    texts: string[];
    children: string[];
    state: FlowNodeState;
    stateText: string;
    highlighted: boolean;
    /** Which step this node stands for, for matching step runs. */
    branchKey: string | null;
    position: number;
    /** True for steps that run once for the whole run (everything up to and
     * including the fan-out step), false for steps that run per work item. */
    perRun: boolean;
}

export interface FlowGraph {
    lanes: FlowLane[];
    nodes: FlowNode[];
}

const LANE_MAIN = "Main line";
const LANE_FAN_OUT = "Fan-out";
const LANE_BRANCHES = "Branches";
const LANE_JOIN = "Join";

const RUN_STATE: Record<string, FlowNodeState> = {
    success: "Positive",
    failed: "Negative",
    running: "Critical",
    // Cancelled by a shutdown or a reload rather than by anything the step
    // did — neither a success nor a failure of the workflow.
    interrupted: "Neutral"
};

function byPosition<T extends { position: number }>(items: T[]): T[] {
    return items.slice().sort((a, b) => a.position - b.position);
}

/** Branch keys in the order their columns should appear: declared branches
 * first, in declared order, then any key only a step mentions. A step naming
 * an undeclared branch cannot be saved, but during editing it is a normal
 * intermediate state and hiding it would be worse than showing it. */
function branchOrder(branches: WorkflowBranch[], steps: WorkflowStep[]): string[] {
    const keys = byPosition(branches).map((b) => b.key);
    const seen = new Set(keys);
    for (const step of steps) {
        if (step.branch_key && !seen.has(step.branch_key)) {
            seen.add(step.branch_key);
            keys.push(step.branch_key);
        }
    }
    return keys;
}

export function buildDefinitionGraph(
    branches: WorkflowBranch[],
    steps: WorkflowStep[]
): FlowGraph {
    const mainLine = byPosition(steps.filter((s) => s.branch_key === null));
    const keys = branchOrder(branches, steps);
    const branchSteps = new Map<string, WorkflowStep[]>();
    for (const key of keys) {
        branchSteps.set(key, byPosition(steps.filter((s) => s.branch_key === key)));
    }

    const fanIndex = mainLine.findIndex((s) => s.fan_out);
    const hasFanOut = fanIndex >= 0;
    const branchWidth = hasFanOut
        ? Math.max(0, ...Array.from(branchSteps.values(), (g) => g.length))
        : 0;
    const joinStart = hasFanOut ? fanIndex + 1 + branchWidth : 0;

    const lanes: FlowLane[] = [];
    const laneId = (position: number): string => `lane-${position}`;
    const addLane = (position: number, label: string): void => {
        if (!lanes.some((l) => l.position === position)) {
            lanes.push({ laneId: laneId(position), label, position });
        }
    };

    const nodes: FlowNode[] = [];
    const mainNodeId = (index: number): string => `main-${index}`;
    const branchNodeId = (key: string, index: number): string => `${key}-${index}`;

    /** The first per-item main-line step, where every branch reconnects. An
     * item that selected no branch goes straight here too. */
    const firstJoinId = hasFanOut && fanIndex + 1 < mainLine.length
        ? mainNodeId(fanIndex + 1)
        : null;

    mainLine.forEach((step, index) => {
        const isFanOut = hasFanOut && index === fanIndex;
        const isJoin = hasFanOut && index > fanIndex;
        const position = isJoin ? joinStart + (index - fanIndex - 1) : index;
        let label = LANE_MAIN;
        if (isFanOut) {
            label = LANE_FAN_OUT;
        } else if (isJoin) {
            label = LANE_JOIN;
        }
        addLane(position, label);

        let children: string[];
        if (isFanOut) {
            const heads = keys
                .map((key) => (branchSteps.get(key)!.length ? branchNodeId(key, 0) : null))
                .filter((id): id is string => id !== null);
            // No branches declared yet: the fan-out step still hands its items
            // straight to the per-item main line.
            children = heads.length ? heads : (firstJoinId ? [firstJoinId] : []);
        } else {
            children = index + 1 < mainLine.length ? [mainNodeId(index + 1)] : [];
        }

        nodes.push(makeNode({
            nodeId: mainNodeId(index),
            laneId: laneId(position),
            agentName: step.agent_name,
            texts: [LANE_MAIN],
            children,
            branchKey: null,
            position: step.position,
            perRun: !isJoin
        }));
    });

    if (hasFanOut) {
        for (const key of keys) {
            const group = branchSteps.get(key)!;
            group.forEach((step, index) => {
                const position = fanIndex + 1 + index;
                addLane(position, LANE_BRANCHES);
                const isLast = index + 1 === group.length;
                const next = isLast ? firstJoinId : branchNodeId(key, index + 1);
                nodes.push(makeNode({
                    nodeId: branchNodeId(key, index),
                    laneId: laneId(position),
                    agentName: step.agent_name,
                    texts: [key],
                    children: next ? [next] : [],
                    branchKey: key,
                    position: step.position,
                    perRun: false
                }));
            });
        }
    }

    lanes.sort((a, b) => a.position - b.position);
    return { lanes, nodes };
}

function makeNode(spec: {
    nodeId: string;
    laneId: string;
    agentName: string;
    texts: string[];
    children: string[];
    branchKey: string | null;
    position: number;
    perRun: boolean;
}): FlowNode {
    return {
        nodeId: spec.nodeId,
        laneId: spec.laneId,
        // A step whose agent is still empty is a real row in the editor; it
        // renders as Planned rather than being dropped from the preview.
        title: spec.agentName || "(no agent)",
        texts: spec.texts,
        children: spec.children,
        state: spec.agentName ? "Neutral" : "Planned",
        stateText: "",
        highlighted: false,
        branchKey: spec.branchKey,
        position: spec.position,
        perRun: spec.perRun
    };
}

/**
 * Returns a copy of `graph` coloured by `stepRuns`.
 *
 * Steps that ran once for the whole run take their state from the run-level
 * step runs and are always on the path. Per-item steps take theirs from the
 * selected item's step runs, so branches another item entered stay Planned —
 * which is what makes the highlighted path readable.
 */
export function decorateWithRun(
    graph: FlowGraph,
    stepRuns: WorkflowStepRun[],
    itemRunId: string | null
): FlowGraph {
    const find = (node: FlowNode): WorkflowStepRun | undefined =>
        stepRuns.find((r) =>
            r.item_run_id === (node.perRun ? null : itemRunId)
            && r.branch_key === node.branchKey
            && r.position === node.position
        );

    const nodes = graph.nodes.map((node) => {
        // With no item selected, a per-item node looks for a step run carrying
        // no item either. Normally there is none — a branch step always runs
        // inside an item — but a run refused before it produced any items
        // records its stopping point exactly that way, and that failure has
        // nowhere else to show.
        const run = find(node);
        if (!run) {
            return { ...node, state: "Planned" as FlowNodeState, stateText: "not run", highlighted: false };
        }
        return {
            ...node,
            state: RUN_STATE[run.status] ?? "Neutral",
            stateText: run.status,
            highlighted: true
        };
    });

    return { lanes: graph.lanes.slice(), nodes };
}
