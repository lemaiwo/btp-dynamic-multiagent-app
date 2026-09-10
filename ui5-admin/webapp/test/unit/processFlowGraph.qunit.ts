import {
    buildDefinitionGraph,
    decorateWithRun,
    FlowGraph
} from "com/infrabel/agentadmin/model/processFlowGraph";
import { WorkflowBranch, WorkflowStep, WorkflowStepRun } from "com/infrabel/agentadmin/service/types";

QUnit.module("processFlowGraph");

function step(over: Partial<WorkflowStep>): WorkflowStep {
    return {
        branch_key: null,
        position: 0,
        agent_name: "agent",
        instructions: "",
        fan_out: false,
        step_timeout_seconds: 300,
        ...over
    };
}

function branch(key: string, position: number): WorkflowBranch {
    return { key, description: key + " branch", position };
}

function stepRun(over: Partial<WorkflowStepRun>): WorkflowStepRun {
    return {
        id: "sr-" + Math.random().toString(36).slice(2),
        workflow_run_id: "run-1",
        item_run_id: null,
        branch_key: null,
        position: 0,
        agent_name: "agent",
        status: "success",
        output: null,
        error: null,
        started_at: null,
        finished_at: null,
        ...over
    };
}

/** The fork–join example from the workflow design doc: two pre steps are a
 * collector and a fan-out reader, two branches of different lengths, and a
 * two-step join tail. */
function forkJoinGraph(): FlowGraph {
    const branches = [branch("triage", 0), branch("escalate", 1)];
    const steps = [
        step({ position: 0, agent_name: "collector" }),
        step({ position: 1, agent_name: "reader", fan_out: true }),
        step({ branch_key: "triage", position: 0, agent_name: "triager" }),
        step({ branch_key: "triage", position: 1, agent_name: "labeler" }),
        step({ branch_key: "escalate", position: 0, agent_name: "escalator" }),
        step({ position: 2, agent_name: "writer" }),
        step({ position: 3, agent_name: "sender" })
    ];
    return buildDefinitionGraph(branches, steps);
}

QUnit.test("a fork-join workflow becomes one column-lane per step position", function (assert) {
    const graph = forkJoinGraph();

    // 1 pre column + fan-out + 2 branch columns (longest branch) + 2 join
    assert.strictEqual(graph.lanes.length, 6, "six lanes");
    assert.deepEqual(
        graph.lanes.map((l) => l.position),
        [0, 1, 2, 3, 4, 5],
        "lane positions are consecutive columns"
    );
    assert.strictEqual(graph.lanes[0].label, "Main line");
    assert.strictEqual(graph.lanes[1].label, "Fan-out");
    assert.strictEqual(graph.lanes[2].label, "Branches");
    assert.strictEqual(graph.lanes[3].label, "Branches");
    assert.strictEqual(graph.lanes[4].label, "Join");
    assert.strictEqual(graph.lanes[5].label, "Join");
});

QUnit.test("fan-out connects to every branch head and branch tails reconnect at the join", function (assert) {
    const graph = forkJoinGraph();
    const byId = new Map(graph.nodes.map((n) => [n.nodeId, n]));

    assert.deepEqual(byId.get("main-0")!.children, ["main-1"], "pre step chains to the fan-out");
    assert.deepEqual(
        byId.get("main-1")!.children,
        ["triage-0", "escalate-0"],
        "fan-out forks to both branch heads"
    );
    assert.deepEqual(byId.get("triage-0")!.children, ["triage-1"], "branch steps chain");
    assert.deepEqual(byId.get("triage-1")!.children, ["main-2"], "long branch tail joins");
    assert.deepEqual(
        byId.get("escalate-0")!.children,
        ["main-2"],
        "short branch tail joins across the unused column"
    );
    assert.deepEqual(byId.get("main-2")!.children, ["main-3"], "join steps chain");
    assert.deepEqual(byId.get("main-3")!.children, [], "last step has no children");
});

QUnit.test("nodes carry the metadata run decoration needs", function (assert) {
    const graph = forkJoinGraph();
    const byId = new Map(graph.nodes.map((n) => [n.nodeId, n]));

    const fanOut = byId.get("main-1")!;
    assert.strictEqual(fanOut.title, "reader", "title is the agent name");
    assert.strictEqual(fanOut.branchKey, null);
    assert.strictEqual(fanOut.position, 1);
    assert.ok(fanOut.perRun, "pre-fan-out steps run once per run");

    const branchNode = byId.get("escalate-0")!;
    assert.strictEqual(branchNode.branchKey, "escalate");
    assert.notOk(branchNode.perRun, "branch steps run per item");
    assert.deepEqual(branchNode.texts, ["escalate"], "subtitle names the branch");

    const join = byId.get("main-2")!;
    assert.notOk(join.perRun, "post-fan-out main-line steps run per item");
    assert.deepEqual(join.texts, ["Main line"]);
});

QUnit.test("a workflow without a fan-out step is a plain chain", function (assert) {
    const graph = buildDefinitionGraph([], [
        step({ position: 0, agent_name: "one" }),
        step({ position: 1, agent_name: "two" })
    ]);

    assert.strictEqual(graph.lanes.length, 2);
    assert.deepEqual(graph.lanes.map((l) => l.label), ["Main line", "Main line"]);
    const byId = new Map(graph.nodes.map((n) => [n.nodeId, n]));
    assert.deepEqual(byId.get("main-0")!.children, ["main-1"]);
    assert.ok(byId.get("main-0")!.perRun, "without a fan-out everything runs once");
});

QUnit.test("an empty definition yields an empty graph rather than throwing", function (assert) {
    const graph = buildDefinitionGraph([], []);
    assert.deepEqual(graph.lanes, []);
    assert.deepEqual(graph.nodes, []);
});

QUnit.test("a step still missing its agent renders as Planned", function (assert) {
    const graph = buildDefinitionGraph([], [
        step({ position: 0, agent_name: "one" }),
        step({ position: 1, agent_name: "" })
    ]);
    assert.strictEqual(graph.nodes[0].state, "Neutral");
    assert.strictEqual(graph.nodes[1].state, "Planned");
});

QUnit.test("a step naming an undeclared branch still appears, after the declared ones", function (assert) {
    // Mid-edit this is a normal state, and validation already refuses to save
    // it. Dropping the step from the preview would leave the author looking
    // for work they can see in the table.
    const graph = buildDefinitionGraph([branch("known", 0)], [
        step({ position: 0, agent_name: "reader", fan_out: true }),
        step({ branch_key: "known", position: 0, agent_name: "a" }),
        step({ branch_key: "typo", position: 0, agent_name: "b" })
    ]);
    const ids = graph.nodes.map((n) => n.nodeId);
    assert.deepEqual(ids, ["main-0", "known-0", "typo-0"]);
    const byId = new Map(graph.nodes.map((n) => [n.nodeId, n]));
    assert.deepEqual(byId.get("main-0")!.children, ["known-0", "typo-0"]);
});

QUnit.test("run decoration colours the path the selected item took", function (assert) {
    const graph = forkJoinGraph();
    const runs = [
        stepRun({ position: 0, agent_name: "collector" }),
        stepRun({ position: 1, agent_name: "reader" }),
        stepRun({ item_run_id: "item-1", branch_key: "triage", position: 0, status: "success" }),
        stepRun({ item_run_id: "item-1", branch_key: "triage", position: 1, status: "failed" }),
        stepRun({ item_run_id: "item-2", branch_key: "escalate", position: 0, status: "success" })
    ];

    const decorated = decorateWithRun(graph, runs, "item-1");
    const byId = new Map(decorated.nodes.map((n) => [n.nodeId, n]));

    assert.strictEqual(byId.get("main-0")!.state, "Positive", "run-level step succeeded");
    assert.ok(byId.get("main-0")!.highlighted, "run-level steps are on every item's path");
    assert.strictEqual(byId.get("triage-0")!.state, "Positive");
    assert.ok(byId.get("triage-0")!.highlighted);
    assert.strictEqual(byId.get("triage-1")!.state, "Negative", "failed maps to Negative");
    assert.strictEqual(
        byId.get("escalate-0")!.state,
        "Planned",
        "another item's branch stays Planned for this item"
    );
    assert.notOk(byId.get("escalate-0")!.highlighted);
    assert.strictEqual(byId.get("main-2")!.state, "Planned", "join never ran for this item");
});

QUnit.test("running and interrupted step statuses get their own states", function (assert) {
    const graph = forkJoinGraph();
    const runs = [
        stepRun({ position: 0, status: "running" }),
        stepRun({ position: 1, status: "interrupted" })
    ];
    const decorated = decorateWithRun(graph, runs, null);
    const byId = new Map(decorated.nodes.map((n) => [n.nodeId, n]));
    assert.strictEqual(byId.get("main-0")!.state, "Critical");
    assert.strictEqual(byId.get("main-0")!.stateText, "running");
    assert.strictEqual(byId.get("main-1")!.state, "Neutral");
});

QUnit.test("decorating with no item selected only colours the run-level steps", function (assert) {
    const graph = forkJoinGraph();
    const runs = [
        stepRun({ position: 0 }),
        stepRun({ item_run_id: "item-1", branch_key: "triage", position: 0 })
    ];
    const decorated = decorateWithRun(graph, runs, null);
    const byId = new Map(decorated.nodes.map((n) => [n.nodeId, n]));
    assert.strictEqual(byId.get("main-0")!.state, "Positive");
    assert.strictEqual(byId.get("triage-0")!.state, "Planned", "item steps need a selected item");
});

QUnit.test("a run blocked before any item still colours the step it stopped at", function (assert) {
    // A preflight refusal records one failed step run with no item attached
    // (there are none yet). Without matching it, a run that never started
    // would draw as a uniformly grey diagram that says nothing about where it
    // stopped -- see _record_blocked_step in agents/workflow_runner.py.
    const graph = forkJoinGraph();
    const runs = [stepRun({ item_run_id: null, branch_key: "triage", position: 0, status: "failed" })];

    const decorated = decorateWithRun(graph, runs, null);
    const byId = new Map(decorated.nodes.map((n) => [n.nodeId, n]));

    assert.strictEqual(byId.get("triage-0")!.state, "Negative", "the blocked branch step is red");
    assert.ok(byId.get("triage-0")!.highlighted, "and on the highlighted path");
    assert.strictEqual(
        byId.get("escalate-0")!.state, "Planned",
        "a step the run never reached stays planned"
    );
});

QUnit.test("with an item selected, per-item nodes ignore run-level step runs", function (assert) {
    // The fallback above must not leak into a normal run: a node showing this
    // item's outcome must never borrow a row belonging to no item.
    const graph = forkJoinGraph();
    const runs = [
        stepRun({ item_run_id: null, branch_key: "triage", position: 0, status: "failed" }),
        stepRun({ item_run_id: "item-1", branch_key: "escalate", position: 0, status: "success" })
    ];

    const decorated = decorateWithRun(graph, runs, "item-1");
    const byId = new Map(decorated.nodes.map((n) => [n.nodeId, n]));

    assert.strictEqual(byId.get("escalate-0")!.state, "Positive", "this item's own step is coloured");
    assert.strictEqual(
        byId.get("triage-0")!.state, "Planned",
        "the item-less row does not colour a step this item did not take"
    );
});

QUnit.test("decoration does not mutate the definition graph", function (assert) {
    const graph = forkJoinGraph();
    decorateWithRun(graph, [stepRun({ position: 0 })], null);
    assert.strictEqual(graph.nodes[0].state, "Neutral", "original stays untouched");
    assert.notOk(graph.nodes[0].highlighted);
});
